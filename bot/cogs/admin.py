import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger
import copy
from typing import Callable, Awaitable
from bot.config import SERVERS, SALA_SAIDA_ID
from bot.cogs.mix import sessions, reset_session, DEFAULT_SESSION_STATE
from bot.database import get_player_rank, fix_match_winner_from_maps, get_active_matches, get_player_team_in_match
from bot.utils.cs2 import send_rcon
from discord.ui import View, Button


class AdminCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def get_server(self, server_num: str):
        """Returns the server config based on the number (1 or 2)"""
        key = f"server{str(server_num).strip()}"
        return SERVERS.get(key)

    async def _ensure_active_session(self, s_id: str):
        session = sessions.get(s_id)
        if not session:
            sessions[s_id] = copy.deepcopy(DEFAULT_SESSION_STATE)
            session = sessions[s_id]
        if session.get("active"):
            return session, True
        try:
            rows = await get_active_matches()
        except Exception:
            rows = []
        row = next((r for r in rows if r.get("server_id") == s_id), None)
        if row and row.get("match_id"):
            session.update({"active": True, "status": "LIVE", "match_id": int(row["match_id"])})
        return session, bool(session.get("active"))

    async def _confirm_server_action(self, ctx, server, action_text: str, on_confirm: Callable[[], Awaitable[None]]):
        if not server:
            return
        prompt = f"Are you sure you want to execute this command on:\n**{server['name']}**?\n`{action_text}`"
        async def _cancel():
            return
        view = AdminConfirmView(ctx.author.id, on_confirm, _cancel)
        await ctx.send(prompt, view=view)

    # ================= GENERAL COMMANDS =================

    @app_commands.command(name="clear", description="Deletes messages from the current channel (up to 100).")
    async def clear(self, interaction: discord.Interaction, quantidade: int = 100):
        ctx = await commands.Context.from_interaction(interaction)
        """Clears messages from the current channel"""
        interaction = getattr(ctx, "interaction", None)
        if interaction and not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        async def _reply(msg: str):
            if interaction:
                return await interaction.followup.send(msg, ephemeral=True)
            return await ctx.send(msg)

        me = ctx.guild.me if ctx.guild else None
        if not me or not me.guild_permissions.manage_messages:
            return await _reply("❌ I don't have 'Manage Messages' permission in this server.")

        quantidade = max(1, min(int(quantidade or 1), 100))
        try:
            deleted = await ctx.channel.purge(limit=quantidade + (0 if interaction else 1))
            removed = max(0, len(deleted) - (0 if interaction else 1))
            await _reply(f"✅ **{removed} messages deleted!**")
        except Exception as e:
            logger.error(f"❌ Error clearing messages: {e}")
            await _reply(f"❌ Error deleting: {e}")


    # ================= MANUAL COMMANDS (MULTI-SERVER) =================

    @app_commands.command(name="rcon", description="Sends RCON command to a server.")
    async def rcon(self, interaction: discord.Interaction, server_num: str, command: str):
        ctx = await commands.Context.from_interaction(interaction)

        server = self.get_server(server_num)
        if not server: return await ctx.send("Invalid server.")

        async def _do():
            await ctx.send(f"Sending to **{server['name']}**...")
            resp = await send_rcon(server, command)
            if resp:
                if len(resp) > 1900: resp = resp[:1900] + "..."
                await ctx.send(f"```\n{resp}\n```")
            else:
                await ctx.send("Command sent.")

        await self._confirm_server_action(ctx, server, f"/rcon {server_num} {command}", _do)



    @app_commands.command(name="say", description="Sends a message in the server chat via RCON.")
    async def say(self, interaction: discord.Interaction, server_num: str, msg: str):
        ctx = await commands.Context.from_interaction(interaction)

        server = self.get_server(server_num)
        if not server: return await ctx.send("Invalid server.")

        async def _do():
            safe_msg = msg.replace('"', '').replace(';', '')
            await send_rcon(server, f'say ADMIN: {safe_msg}')
            if getattr(ctx, "message", None):
                await ctx.message.add_reaction("OK")
            else:
                await ctx.send("OK")

        await self._confirm_server_action(ctx, server, f"/say {server_num} {msg}", _do)

    _SERVICES = {
        "1":      "cs2-mix1",
        "2":      "cs2-mix2",
        "3":      "cs2-mix3",
        "4":      "cs2-mix4",
        "5":      "cs2-mix5",
        "retake": "cs2-retake",
    }
    _LABELS = {
        "1": "Mix 1", "2": "Mix 2", "3": "Mix 3",
        "4": "Mix 4", "5": "Mix 5",
        "retake": "Retake", "all": "All servers",
        "bot": "MixBot", "tudo": "Everything (servers + bot)",
    }

    @app_commands.command(name="restart", description="Restarts a game server or the bot itself.")
    @app_commands.describe(servidor="Which server/service to restart")
    @app_commands.choices(servidor=[
        app_commands.Choice(name="Mix 1",                    value="1"),
        app_commands.Choice(name="Mix 2",                    value="2"),
        app_commands.Choice(name="Mix 3",                    value="3"),
        app_commands.Choice(name="Mix 4",                    value="4"),
        app_commands.Choice(name="Mix 5",                    value="5"),
        app_commands.Choice(name="Retake",                   value="retake"),
        app_commands.Choice(name="All servers",              value="all"),
        app_commands.Choice(name="MixBot",                   value="bot"),
        app_commands.Choice(name="Everything (servers + bot)", value="tudo"),
    ])
    async def reiniciar(self, interaction: discord.Interaction, servidor: str):
        await interaction.response.defer(ephemeral=True)

        label = self._LABELS.get(servidor, servidor)

        restart_bot = servidor in ("bot", "tudo")
        if servidor == "bot":
            services = []
        elif servidor == "tudo":
            services = list(self._SERVICES.values())
        elif servidor == "all":
            services = list(self._SERVICES.values())
        else:
            services = [self._SERVICES[servidor]]

        await interaction.edit_original_response(content=f"⏳ Restarting **{label}**...")

        erros = []
        for svc in services:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "sudo", "systemctl", "restart", svc,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                if proc.returncode != 0:
                    erros.append(f"`{svc}`: {stderr.decode().strip()[:200]}")
            except asyncio.TimeoutError:
                erros.append(f"`{svc}`: timeout (30s)")
            except Exception as exc:
                erros.append(f"`{svc}`: {exc}")

        if erros:
            msg = f"⚠️ Error restarting **{label}**:\n" + "\n".join(erros)
            await interaction.edit_original_response(content=msg)
            logger.info(f"[Admin] {interaction.user} restarted '{label}' — ERROR")
            return

        if restart_bot:
            suffix = " Servers restarted." if services else ""
            await interaction.edit_original_response(
                content=f"✅ **{label}** — restarting the bot now...{suffix}"
            )
            logger.info(f"[Admin] {interaction.user} restarted '{label}' — OK (bot restarting)")
            await asyncio.sleep(1)
            asyncio.create_task(self._restart_bot())
            return

        await interaction.edit_original_response(content=f"✅ **{label}** restarted successfully!")
        logger.info(f"[Admin] {interaction.user} restarted '{label}' — OK")

    async def _restart_bot(self):
        await asyncio.sleep(1)
        proc = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "restart", "mixbot",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    @app_commands.command(name="fixmatch", description="Fixes the match winner based on map scores.")
    async def fixmatch(self, interaction: discord.Interaction, match_id: int | None = None):
        ctx = await commands.Context.from_interaction(interaction)
        if match_id is None:
            s_id = next((i for i, s in SERVERS.items() if ctx.channel.id == s["channels"]["picks_text"]), None)
            if s_id and sessions.get(s_id, {}).get("match_id"):
                match_id = int(sessions[s_id]["match_id"])
        if match_id is None:
            await ctx.send("❌ Inform the match_id or use in the picks channel with an active match.")
            return

        async def _do():
            result = await fix_match_winner_from_maps(match_id)
            if not result:
                await ctx.send("❌ Could not determine winner (match does not exist or tied score).")
                return
            s1 = int(result.get("map_score1") or 0)
            s2 = int(result.get("map_score2") or 0)
            winner = result.get("winner") or "unknown"
            await ctx.send(
                f"✅ Match #{match_id} adjusted. Winner: **{winner}** (map {s1}x{s2})."
            )

        prompt = f"Are you sure you want to execute this command?\n`/fixmatch {match_id}`"
        async def _cancel():
            return
        view = AdminConfirmView(ctx.author.id, _do, _cancel)
        await ctx.send(prompt, view=view)

    @app_commands.command(name="admincommands", description="Shows the list of admin commands.")
    async def comandosadmin(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        embed = discord.Embed(title="Admin Commands", color=0x2ecc71)
        embed.add_field(
            name="Moderation",
            value=(
                "`/clear <amount>` - deletes up to 100 messages from the current channel\n"
                "`/say <server> <msg>` - sends message in server chat via RCON"
            ),
            inline=False,
        )
        embed.add_field(
            name="RCON/Map",
            value=(
                "`/rcon <server> <command>` - sends raw RCON command\n"
                "`/rcon 1 status` - shows server status\n"
                "`/rcon 1 changelevel de_mirage` - changes current map\n"
                "`/rcon 1 mp_pause_match 1` - pauses the match\n"
                "`/rcon 1 mp_unpause_match` - unpauses the match"
            ),
            inline=False,
        )
        embed.add_field(
            name="Servers",
            value=(
                "`/restart 1-5` - restarts Mix 1 through 5\n"
                "`/restart retake` - restarts the Retake server\n"
                "`/restart all` - restarts all servers"
            ),
            inline=False,
        )
        embed.add_field(
            name="Mix/Panel",
            value=(
                "`/fixpanel` - resets the monitoring panel\n"
                "`/fixmatch [match id]` - fixes the match winner\n"
                "`/cancelmix` - cancels the active mix in the picks channel"
            ),
            inline=False,
        )
        embed.add_field(
            name="Substitution",
            value="`/swap @user1 @user2` - replaces a player during the match",
            inline=False,
        )
        await ctx.send(embed=embed)

    @app_commands.command(name="swap", description="Replaces a player with another during the mix.")
    async def trocar(self, interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
        ctx = await commands.Context.from_interaction(interaction)
        s_id = next((i for i, s in SERVERS.items() if ctx.channel.id == s["channels"]["picks_text"]), None)
        if not s_id and user1.voice:
            v_id = user1.voice.channel.id
            s_id = next((i for i, s in SERVERS.items() if v_id in [
                s["channels"].get("picks_voice"),
                s["channels"].get("team1_voice"),
                s["channels"].get("team2_voice"),
            ]), None)
        if not s_id:
            await ctx.send("❌ Use this command in the server's picks channel.")
            return
        session, active = await self._ensure_active_session(s_id)
        if not active:
            await ctx.send("❌ There is no active mix on this server.")
            return
        if user1.bot or user2.bot:
            await ctx.send("❌ Cannot swap bots.")
            return

        embed = discord.Embed(
            title="Confirm swap?",
            description=f"Do you want to swap **{user1.display_name}** with **{user2.display_name}**?",
            color=0xf1c40f,
        )
        embed.set_thumbnail(url=user1.display_avatar.url)
        embed.set_image(url=user2.display_avatar.url)
        view = TrocarConfirmView(self.bot, s_id, user1, user2)
        await ctx.send(embed=embed, view=view)



class AdminConfirmView(View):
    def __init__(self, requester_id: int, on_confirm: Callable[[], Awaitable[None]], on_cancel: Callable[[], Awaitable[None]]):
        super().__init__(timeout=30)
        self.requester_id = requester_id
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel

    async def _check_user(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the command executor can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="YES", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if not await self._check_user(interaction):
            return
        await interaction.response.edit_message(content="Action confirmed.", view=None)
        await self.on_confirm()

    @discord.ui.button(label="NO", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        if not await self._check_user(interaction):
            return
        await interaction.response.edit_message(content="Action cancelled.", view=None)
        await self.on_cancel()

# === THIS PART IS REQUIRED FOR THE FILE TO LOAD ===
async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
    logger.debug("AdminCog loaded")


class TrocarConfirmView(View):
    def __init__(self, bot, s_id, user1: discord.Member, user2: discord.Member):
        super().__init__(timeout=30)
        self.bot = bot
        self.s_id = s_id
        self.user1 = user1
        self.user2 = user2

    @discord.ui.button(label="YES", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        session = sessions.get(self.s_id)
        if not session:
            sessions[self.s_id] = copy.deepcopy(DEFAULT_SESSION_STATE)
            session = sessions[self.s_id]
        if not session.get("active"):
            try:
                rows = await get_active_matches()
            except Exception:
                rows = []
            row = next((r for r in rows if r.get("server_id") == self.s_id), None)
            if row and row.get("match_id"):
                session.update({"active": True, "status": "LIVE", "match_id": int(row["match_id"])})
        if not session.get("active"):
            await interaction.response.edit_message(content="❌ Mix is no longer active.", embed=None, view=None)
            return
        server = SERVERS.get(self.s_id)
        if not server:
            await interaction.response.edit_message(content="❌ Invalid server.", embed=None, view=None)
            return

        try:
            rank1 = await get_player_rank(self.user1.id)
            rank2 = await get_player_rank(self.user2.id)
        except Exception as e:
            await interaction.response.edit_message(content=f"❌ Error fetching SteamID: {e}", embed=None, view=None)
            return
        steamid1 = rank1.get("steamid64") if rank1 else None
        steamid2 = rank2.get("steamid64") if rank2 else None
        if not steamid2:
            await interaction.response.edit_message(content="❌ The player joining does not have a linked SteamID.", embed=None, view=None)
            return
        try:
            team_label = None
            match_id = session.get("match_id")
            if match_id and steamid1:
                try:
                    team_label = await get_player_team_in_match(int(match_id), str(steamid1))
                except:
                    team_label = None
            if not team_label:
                await interaction.response.edit_message(
                    content="❌ Could not identify the team in the Database.",
                    embed=None,
                    view=None,
                )
                return
            safe_name = (self.user2.display_name or "Sub").replace('"', "").strip()
            if steamid1:
                await send_rcon(server, f'matchzy_removeplayer "{steamid1}"')
            await send_rcon(server, f'matchzy_addplayer {steamid2} {team_label} "{safe_name}"')
        except Exception as e:
            await interaction.response.edit_message(content=f"❌ Error updating MatchZy: {e}", embed=None, view=None)
            return

        def replace_in_list(lst):
            return [self.user2 if p == self.user1 else p for p in lst]

        session["players"] = replace_in_list(session["players"])
        session["team1"] = replace_in_list(session["team1"])
        session["team2"] = replace_in_list(session["team2"])
        session["available"] = replace_in_list(session["available"])
        session["captains"] = replace_in_list(session["captains"])
        if session.get("turn") == self.user1:
            session["turn"] = self.user2
        if self.user1.id in session["accepts"]:
            session["accepts"].discard(self.user1.id)
            session["accepts"].add(self.user2.id)

        if self.user1.id in session["player_ratings"]:
            session["player_ratings"][self.user2.id] = session["player_ratings"].get(self.user1.id, 1000)
            session["player_ratings"].pop(self.user1.id, None)

        user1_vc = self.user1.voice.channel if self.user1.voice else None
        if user1_vc:
            try:
                await self.user2.move_to(user1_vc)
            except:
                pass
            sala_saida = interaction.guild.get_channel(SALA_SAIDA_ID) if SALA_SAIDA_ID else None
            if sala_saida:
                try:
                    await self.user1.move_to(sala_saida)
                except:
                    pass

        await interaction.response.edit_message(content=f"Swap completed: {self.user1.display_name} -> {self.user2.display_name}", embed=None, view=None)

    @discord.ui.button(label="NO", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(content="❌ Operation cancelled.", embed=None, view=None)
