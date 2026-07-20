import sys
import asyncio
import os
from pathlib import Path

import aiohttp
from aiohttp import web

import discord
from discord.ext import commands
from loguru import logger

# Local imports
from bot.config import (
    DISCORD_BOT_TOKEN,
    CANAL_LOGS_ID as CHANNEL_LOGS_ID,
    QUEUE_VOICE_CHANNEL_ID as QUEUE_VOICE_CHANNEL_ID,
    SERVERS,
    TICKET_PANEL_CHANNEL_ID,
    PUNISHMENT_PANEL_CHANNEL_ID as PUNISHMENT_PANEL_CHANNEL_ID,
    STEAMID_CHANNEL_ID as STEAMID_CHANNEL_ID,
    INSTAGRAM_URL, SKINS_ACTIVITY_APPLICATION_ID, STEAM_GROUP_URL, WHATSAPP_GROUP_URL
)
from bot.database import db
from bot.utils.helpers import format_timestamp
 
# ================= 1. LOG CONFIGURATION (RENDER) =================
# Configures Loguru to send logs to the Render console immediately
logger.remove()


def _stderr_log_filter(record):
    msg = str(record.get("message") or "")
    if "Cog loaded:" in msg or "Cog already loaded:" in msg:
        return False
    return True


logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    level="INFO",
    filter=_stderr_log_filter,
)
logger.add(
    "logs/bot.log",
    rotation="1 day",
    retention="7 days",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    level="DEBUG"
)

# ================= 2. BOT CONFIGURATION =================
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    help_command=None
)

_persistent_views_loaded = False
_app_commands_synced = False


def _build_groups_embed(guild: discord.Guild | None) -> discord.Embed:
    embed = discord.Embed(
        title="🌐 Groups Hub",
        description=(
            "Connect with our crew beyond Discord! "
            "Stay up to date with all news, giveaways and important announcements.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Choose one of the platforms below to join:"
        ),
        color=0xFFD700,  # Vibrant GOLD
        timestamp=discord.utils.utcnow(),
    )
    
    embed.add_field(
        name="📱 WhatsApp",
        value="> Quick chat, mix announcements and networking with players.",
        inline=False,
    )
    embed.add_field(
        name="🎮 Steam Group",
        value="> Our official home on Steam. Join to see stats and announcements.",
        inline=False,
    )
    embed.add_field(
        name="📸 Instagram",
        value="> Follow the best moments, posts and the project's identity.",
        inline=False,
    )

    embed.set_footer(
        text="Verified official links • Use responsibly",
        icon_url=guild.icon.url if guild and guild.icon else None
    )

    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    return embed


def _build_groups_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if WHATSAPP_GROUP_URL:
        view.add_item(
            discord.ui.Button(
                label="Join WhatsApp",
                style=discord.ButtonStyle.link,
                url=WHATSAPP_GROUP_URL,
            )
        )
    if STEAM_GROUP_URL:
        view.add_item(
            discord.ui.Button(
                label="Steam Group",
                style=discord.ButtonStyle.link,
                url=STEAM_GROUP_URL,
            )
        )
    if INSTAGRAM_URL:
        view.add_item(
            discord.ui.Button(
                label="Instagram",
                style=discord.ButtonStyle.link,
                url=INSTAGRAM_URL,
            )
        )
    return view

async def _delete_all_panel_messages(channel: discord.TextChannel, title: str) -> None:
    try:
        to_delete = []
        async for msg in channel.history(limit=50):
            if msg.author != channel.guild.me:
                continue
            if not msg.embeds:
                continue
            if msg.embeds[0].title == title:
                to_delete.append(msg)
        for msg in to_delete:
            await msg.delete()
    except Exception:
        pass


# ================= 3. WEB SERVER (INTERNAL HEALTHCHECK) =================
# Keeps Render happy by having an open port so it doesn't restart the bot
async def health_check_handler(request):
    return web.Response(text="Bot is running correctly!", status=200)


async def cs2_chat_proxy_handler(request):
    handler = getattr(bot, "cs2bridge_http_handler", None)
    if handler is None:
        return web.json_response(
            {"ok": False, "error": "cs2bridge_not_ready"},
            status=503
        )

    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.exception(f"❌ Error on endpoint /cs2/chat: {e}")
        return web.json_response(
            {"ok": False, "error": "internal_error"},
            status=500
        )

async def cs2_poll_proxy_handler(request):
    handler = getattr(bot, "cs2bridge_poll_http_handler", None)
    if handler is None:
        return web.json_response(
            {"ok": False, "error": "cs2bridge_not_ready"},
            status=503
        )

    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.exception(f"❌ Error on endpoint /cs2/bridge/poll: {e}")
        return web.json_response(
            {"ok": False, "error": "internal_error"},
            status=500
        )


async def match_webhook_proxy_handler(request):
    handler = getattr(bot, "match_webhook_handler", None)
    if handler is None:
        return web.json_response(
            {"ok": False, "error": "match_webhook_not_ready"},
            status=503
        )

    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.exception(f"❌ Error on endpoint /matchzy/webhook: {e}")
        return web.json_response(
            {"ok": False, "error": "internal_error"},
            status=500
        )


async def start_web_server():
    """Starts a lightweight web server on the port required by Render"""
    app = web.Application()
    app.router.add_get('/', health_check_handler)
    app.router.add_get('/health', health_check_handler)
    app.router.add_post('/cs2/chat', cs2_chat_proxy_handler)
    app.router.add_post('/cs2/bridge/poll', cs2_poll_proxy_handler)
    app.router.add_post('/matchzy/webhook', match_webhook_proxy_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Gets the port from the PORT environment variable (Required on Render)
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    
    await site.start()
    logger.success(f"🌍 Web Server started on port {port}")

# ================= 4. CONNECTION EVENTS =================

@bot.event
async def on_ready():
    """Event triggered when the bot is ready"""
    logger.success(f"✅ Bot online as {bot.user.name} (ID: {bot.user.id})")
    
    cs2_count = sum(1 for s in SERVERS.values() if s["active"])
    logger.info(f"📡 Connected to {len(bot.guilds)} Discord Guild(s)")
    logger.info(f"🎮 Configured with {cs2_count} CS2 Server(s)")

    # Database Connection
    try:
        await db.connect()
    except Exception as e:
        logger.critical(f"❌ Failed to connect to DB: {e}")

    # Extension Loading (Cogs)
    cogs = [
        "bot.cogs.steam",
        "bot.cogs.admin",
        "bot.cogs.fila",
        "bot.cogs.mix",
        "bot.cogs.denuncias",
        "bot.cogs.punicoes",
        "bot.cogs.ranking",
        "bot.cogs.matches",
        "bot.cogs.stats",
        "bot.cogs.torneio",
        "bot.cogs.cs2bridge",
        "bot.cogs.vip_stripe",
        "bot.cogs.smurf",
    ]

    for cog in cogs:
        try:
            if cog in bot.extensions:
                logger.info(f"♻️ Cog already loaded: {cog} (skipping)")
                continue
            await bot.load_extension(cog)
            logger.info(f"🧩 Cog loaded: {cog}")
        except Exception as e:
            logger.error(f"❌ Error loading {cog}: {e}")

    global _persistent_views_loaded, _app_commands_synced
    if not _persistent_views_loaded:
        try:
            from bot.cogs.punicoes import PunicoesView as PunishmentsView
            from bot.cogs.denuncias import TicketPanelView, ensure_open_ticket_controls
            from bot.cogs.steam import RegistrationPanelView
            bot.add_view(PunishmentsView())
            bot.add_view(TicketPanelView())
            bot.add_view(RegistrationPanelView())
            _persistent_views_loaded = True
            logger.info("✅ Persistent views registered")
        except Exception as e:
            logger.error(f"❌ Error registering persistent views: {e}")

    try:
        from bot.cogs.punicoes import PunicoesView as PunishmentsView
        from bot.cogs.denuncias import TicketPanelView, ensure_open_ticket_controls
        from bot.cogs.steam import RegistrationPanelView, build_registration_panel_embed

        if TICKET_PANEL_CHANNEL_ID:
            channel = bot.get_channel(TICKET_PANEL_CHANNEL_ID)
            if channel:
                embed = discord.Embed(
                    title="🎫 Support Center",
                    description=(
                        "Need help or want to submit a report? "
                        "Open a ticket by clicking the button below to talk to our Staff in a private channel.\n\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    color=0x3498DB
                )
                embed.set_footer(text="Priority support via tickets")
                await _delete_all_panel_messages(channel, "🎫 Support Center")
                await channel.send(embed=embed, view=TicketPanelView())

        if PUNISHMENT_PANEL_CHANNEL_ID:
            channel = bot.get_channel(PUNISHMENT_PANEL_CHANNEL_ID)
            if channel:
                await _delete_all_panel_messages(channel, "⚖️ Punishment Panel")

        if STEAMID_CHANNEL_ID:
            channel = bot.get_channel(STEAMID_CHANNEL_ID)
            if channel:
                embed = build_registration_panel_embed(channel.guild)
                await _delete_all_panel_messages(channel, "🛡️ Player Registration")
                await channel.send(embed=embed, view=RegistrationPanelView())
        try:
            await ensure_open_ticket_controls(bot)
        except Exception as e:
            logger.error(f"❌ Error renewing open tickets: {e}")
    except Exception as e:
        logger.error(f"❌ Error creating automatic panels: {e}")

    if not _app_commands_synced:
        try:
            # Prefer guild-scoped slash commands for immediate availability after deploys.
            # (Global app commands can take a while to propagate to all users.)
            for guild in bot.guilds:
                bot.tree.clear_commands(guild=guild)
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)

            _app_commands_synced = True
            logger.info("✅ Slash commands synced")
        except Exception as e:
            logger.error(f"❌ Error syncing slash commands: {e}")

    logger.success("🚀 System initialized and ready to use!")

@bot.event
async def on_disconnect():
    logger.warning("⚠️ Unstable connection...")

@bot.event
async def on_resumed():
    logger.success("⚡ Connection restored.")

@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound): return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`")
    else:
        logger.error(f"❌ Error on command {ctx.command}: {error}")

# ================= 5. BASIC COMMANDS =================

@bot.tree.command(name="ping", description="Shows the bot's current latency.")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! Latency: **{latency}ms**")


@bot.tree.command(name="help", description="Shows the quick guide on how to play mix.")
async def help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Player Guide - How to Play",
        description=(
            "Welcome to our community! Follow this quick guide to get in the action and compete in MIX.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x9B59B6,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="1️⃣ Link your account",
        value=(
            f"Go to channel <#{STEAMID_CHANNEL_ID}> and register.\n"
            "*Without linking, you won't be able to join the servers.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣ Join the queue",
        value=(
            f"Connect to the voice channel <#{QUEUE_VOICE_CHANNEL_ID}>.\n"
            "*The bot will start the automatic match as soon as there are 10 players.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣ Accept the Mix",
        value=(
            "Stay tuned to your Discord! Click **ACCEPT** when the bot calls you.\n"
            "*Declining or taking too long to accept results in an automatic penalty.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="4️⃣ On the Server",
        value=(
            "The bot will provide the IP. Captain draft and the match happen automatically.\n"
            "*Your stats and ranking will be updated at the end of each game.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Useful Commands",
        value=(
            "> `/startmix` • `/profile` • `/history` • `/ranking` • `/register` • `/groups`"
        ),
        inline=False,
    )
    embed.set_footer(text="Tip: just joined Discord today? Start at the registration panel and then head to the queue.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="groups", description="Posts the embed with the community's official groups.")
async def groups(interaction: discord.Interaction):
    if not WHATSAPP_GROUP_URL and not STEAM_GROUP_URL and not INSTAGRAM_URL:
        await interaction.response.send_message(
            "ERROR: no group links are configured.",
            ephemeral=True,
        )
        return

    embed = _build_groups_embed(interaction.guild)
    view = _build_groups_view()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("Groups embed posted in this channel.", ephemeral=True)

# ================= 6. INITIALIZATION =================

async def _launch_activity_native(interaction: discord.Interaction, application_id: int) -> tuple[bool, str]:
    app_id = str(application_id or "").strip()
    if not app_id:
        return False, "missing_application_id"
    if interaction.response.is_done():
        return False, "interaction_already_responded"

    callback_url = f"https://discord.com/api/v10/interactions/{interaction.id}/{interaction.token}/callback"
    payload = {"type": 12, "data": {"application_id": app_id}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(callback_url, json=payload) as resp:
                body = await resp.text()
                if 200 <= resp.status < 300:
                    return True, f"ok:{resp.status}"
                return False, f"http_{resp.status}:{body[:300]}"
    except Exception as exc:
        return False, f"exception:{type(exc).__name__}:{exc}"


@bot.tree.command(name="skins", description="Opens the skins Activity.")
async def skins(interaction: discord.Interaction):
    if SKINS_ACTIVITY_APPLICATION_ID <= 0:
        await interaction.response.send_message(
            "ERROR: Skins Activity is not configured. Set `SKINS_ACTIVITY_APPLICATION_ID` in the environment.",
            ephemeral=True,
        )
        return

    ok, reason = await _launch_activity_native(interaction, SKINS_ACTIVITY_APPLICATION_ID)
    if ok:
        logger.info(
            f"Native /skins launch OK guild={getattr(interaction.guild, 'id', 0)} user={interaction.user.id}"
        )
        return

    logger.error(f"ERROR launching native /skins Activity: {reason}")
    if not interaction.response.is_done():
        await interaction.response.send_message(
            "ERROR: Failed to open the Activity. Check if the Activity app is installed/published in the Discord Developer Portal.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "ERROR: Failed to open the Activity. Try again in a moment.",
            ephemeral=True,
        )
async def main():
    Path("logs").mkdir(exist_ok=True)

    # 1. Start the Web Server (Essential for Render)
    await start_web_server()

    # 2. Start the Bot
    try:
        await bot.start(DISCORD_BOT_TOKEN)
    except Exception as e:
        logger.critical(f"❌ Critical failure on startup: {e}")
    finally:
        logger.info("🛑 Shutting down...")
        await db.close()
        await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
