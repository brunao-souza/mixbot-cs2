import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from bot.config import STAFF_ROLE_IDS
from bot.database import db


def elo_to_level(elo: int) -> int:
    if elo <= 800:  return 1
    if elo <= 950:  return 2
    if elo <= 1100: return 3
    if elo <= 1250: return 4
    if elo <= 1400: return 5
    if elo <= 1550: return 6
    if elo <= 1700: return 7
    if elo <= 1850: return 8
    if elo <= 2000: return 9
    return 10


def _is_staff(member: discord.Member) -> bool:
    if not STAFF_ROLE_IDS:
        return member.guild_permissions.administrator
    return any(r.id in STAFF_ROLE_IDS for r in member.roles)


class SmurfCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    smurf_group = app_commands.Group(name="smurf", description="Gerenciamento anti-smurf")

    @smurf_group.command(name="set", description="Marca jogador como smurf com ELO da conta principal")
    @app_commands.describe(
        usuario="Jogador a marcar como smurf",
        elo="ELO real da conta principal (ex: 2500)"
    )
    async def smurf_set(self, interaction: discord.Interaction, usuario: discord.Member, elo: int):
        if not _is_staff(interaction.user):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)

        if elo < 1 or elo > 99999:
            return await interaction.response.send_message("❌ ELO inválido.", ephemeral=True)

        level = elo_to_level(elo)

        await db.execute(
            """
            INSERT INTO smurf_overrides (discord_id, override_elo, override_level, set_by)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                override_elo = VALUES(override_elo),
                override_level = VALUES(override_level),
                set_by = VALUES(set_by),
                updated_at = CURRENT_TIMESTAMP
            """,
            (usuario.id, elo, level, interaction.user.id)
        )

        logger.info(f"Smurf registrado: {usuario} (discord_id={usuario.id}) ELO={elo} Lv={level} por {interaction.user}")

        embed = discord.Embed(
            title="🔵 SMURF REGISTRADO",
            color=0x3498DB
        )
        embed.add_field(name="Jogador", value=usuario.mention, inline=True)
        embed.add_field(name="ELO atribuído", value=str(elo), inline=True)
        embed.add_field(name="Level derivado", value=str(level), inline=True)
        embed.set_footer(text=f"Registrado por {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    @smurf_group.command(name="remove", description="Remove o override de smurf de um jogador")
    @app_commands.describe(usuario="Jogador para remover o override")
    async def smurf_remove(self, interaction: discord.Interaction, usuario: discord.Member):
        if not _is_staff(interaction.user):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)

        rowcount = await db.execute(
            "DELETE FROM smurf_overrides WHERE discord_id = %s",
            (usuario.id,)
        )

        if not rowcount:
            return await interaction.response.send_message(
                f"⚠️ {usuario.mention} não estava marcado como smurf.", ephemeral=True
            )

        logger.info(f"Smurf removido: {usuario} (discord_id={usuario.id}) por {interaction.user}")
        await interaction.response.send_message(
            f"✅ Override de smurf removido para {usuario.mention}. Faceit real será usado novamente."
        )

    @smurf_group.command(name="list", description="Lista todos os smurfs registrados")
    async def smurf_list(self, interaction: discord.Interaction):
        if not _is_staff(interaction.user):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)

        rows = await db.fetchall(
            "SELECT discord_id, override_elo, override_level, set_by, updated_at FROM smurf_overrides ORDER BY updated_at DESC"
        )

        if not rows:
            return await interaction.response.send_message("✅ Nenhum smurf registrado.", ephemeral=True)

        embed = discord.Embed(title="🔵 SMURFS REGISTRADOS", color=0x3498DB)
        guild = interaction.guild

        lines = []
        for row in rows:
            member = guild.get_member(row["discord_id"]) if guild else None
            name = member.display_name if member else f"ID {row['discord_id']}"
            mod = guild.get_member(row["set_by"]) if guild else None
            mod_name = mod.display_name if mod else f"ID {row['set_by']}"
            lines.append(
                f"🔵 **{name}** — ELO `{row['override_elo']}` · Lv `{row['override_level']}` · por *{mod_name}*"
            )

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"{len(rows)} smurf(s) registrado(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SmurfCog(bot))
    logger.info("Cog carregado: SmurfCog")
