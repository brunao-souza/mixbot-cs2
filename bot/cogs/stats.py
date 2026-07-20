import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger
import re
from typing import Dict

from bot.database import db

_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "]+",
    flags=re.UNICODE,
)

def _strip_emojis(text: str) -> str:
    if not text:
        return text
    cleaned = _EMOJI_RE.sub("", text)
    cleaned = cleaned.replace("\uFE0F", "").replace("\u200D", "")
    return " ".join(cleaned.split())

STAT_DEFS: Dict[str, Dict[str, str]] = {
    "entry": {"column": "entry_count", "label": "Entry Frags"},
    "entrywins": {"column": "entry_wins", "label": "Entry Wins"},
    "hs": {"column": "head_shot_kills", "label": "Headshots"},
    "kdr": {"column": "kills", "label": "K/D Ratio", "format": "float"},
    "kills": {"column": "kills", "label": "Kills"},
    "deaths": {"column": "deaths", "label": "Deaths"},
    "assists": {"column": "assists", "label": "Assists"},
    "dmg": {"column": "damage", "label": "Damage"},
    "enemy2k": {"column": "enemy2ks", "label": "Multi-kill 2K"},
    "enemy3k": {"column": "enemy3ks", "label": "Multi-kill 3K"},
    "enemy4k": {"column": "enemy4ks", "label": "Multi-kill 4K"},
    "enemy5k": {"column": "enemy5ks", "label": "Multi-kill 5K"},
    "utilcount": {"column": "utility_count", "label": "Utility Usada"},
    "util": {"column": "utility_damage", "label": "Utility Damage"},
    "utilsuccess": {"column": "utility_successes", "label": "Utility Sucessos"},
    "utilenemies": {"column": "utility_enemies", "label": "Utility Enemies"},
    "flashcount": {"column": "flash_count", "label": "Flash Usadas"},
    "flash": {"column": "flash_successes", "label": "Flash Sucessos"},
    "hpr": {"column": "health_points_removed_total", "label": "HP Removido"},
    "hpd": {"column": "health_points_dealt_total", "label": "HP Dealt"},
    "shots": {"column": "shots_fired_total", "label": "Tiros Disparados"},
    "shotshit": {"column": "shots_on_target_total", "label": "Tiros no Alvo"},
    "v1count": {"column": "v1_count", "label": "1v1 Jogados"},
    "v1": {"column": "v1_wins", "label": "1v1 Wins"},
    "v2count": {"column": "v2_count", "label": "1v2 Jogados"},
    "v2": {"column": "v2_wins", "label": "1v2 Wins"},
    "equip": {"column": "equipment_value", "label": "Valor de Equipamento"},
    "moneysaved": {"column": "money_saved", "label": "Dinheiro Salvo"},
    "killreward": {"column": "kill_reward", "label": "Kill Reward"},
    "livetime": {"column": "live_time", "label": "Tempo Vivo"},
    "cashearned": {"column": "cash_earned", "label": "Dinheiro Ganho"},
    "enemiesflashed": {"column": "enemies_flashed", "label": "Inimigos Flasheados"},
}

async def _get_top_stat(metric_key: str, days: int, limit: int = 10):
    if metric_key == "kdr":
        query = """
            SELECT p.steamid64,
                   MAX(p.name) as name,
                   pl.discord_id,
                   COUNT(DISTINCT p.matchid) as matches,
                   CASE
                       WHEN SUM(p.deaths) > 0 THEN SUM(p.kills) / SUM(p.deaths)
                       ELSE SUM(p.kills)
                   END as total
            FROM matchzy_stats_players p
            JOIN matchzy_stats_maps mp
              ON p.matchid = mp.matchid AND p.mapnumber = mp.mapnumber
            LEFT JOIN players pl ON pl.steamid64 = p.steamid64
            WHERE COALESCE(mp.end_time, mp.start_time) >= DATE_SUB(NOW(), INTERVAL %s DAY)
            GROUP BY p.steamid64, pl.discord_id
            HAVING COUNT(DISTINCT p.matchid) > 5
               AND SUM(p.kills) > 0
            ORDER BY total DESC, SUM(p.kills) DESC
            LIMIT %s
        """
        return await db.fetchall(query, (days, limit))

    metric = STAT_DEFS[metric_key]["column"]
    query = f"""
        SELECT p.steamid64,
               MAX(p.name) as name,
               pl.discord_id,
               SUM(p.{metric}) as total
        FROM matchzy_stats_players p
        JOIN matchzy_stats_maps mp
          ON p.matchid = mp.matchid AND p.mapnumber = mp.mapnumber
        LEFT JOIN players pl ON pl.steamid64 = p.steamid64
        WHERE COALESCE(mp.end_time, mp.start_time) >= DATE_SUB(NOW(), INTERVAL %s DAY)
        GROUP BY p.steamid64, pl.discord_id
        HAVING total > 0
        ORDER BY total DESC
        LIMIT %s
    """
    return await db.fetchall(query, (days, limit))

def _render_total(metric_key: str, value) -> str:
    if STAT_DEFS[metric_key].get("format") == "float":
        return f"{float(value or 0):.2f}"
    return str(int(value or 0))

def _render_table(rows, guild, metric_key: str, label: str, days: int):
    lines = []
    show_matches = metric_key == "kdr"
    for i, row in enumerate(rows, 1):
        discord_id = row.get("discord_id")
        name_db = row.get("name") or "Unknown"
        member = guild.get_member(int(discord_id)) if discord_id else None
        name = member.display_name if member else name_db
        name = _strip_emojis(name)[:14]
        total = _render_total(metric_key, row.get("total"))
        if show_matches:
            matches = int(row.get("matches") or 0)
            lines.append(f"{i:02} | {name:<14} | {matches:>3} | {total:>5}")
        else:
            lines.append(f"{i:02} | {name:<14} | {total:>5}")
    header = "POS| JOGADOR        | JG | TOTAL\n" if show_matches else "POS| JOGADOR        | TOTAL\n"
    body = "\n".join(lines) if lines else "Sem dados no período."
    return f"{label} (últimos {days} dias)\n```text\n{header}{body}\n```"

class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def rank_metric_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        keys = sorted(STAT_DEFS.keys())
        filtered = [k for k in keys if current.lower() in k.lower()]
        return [app_commands.Choice(name=k, value=k) for k in filtered[:25]]

    @app_commands.command(name="rank", description="Top 10 por metrica")
    @app_commands.describe(metric="Metrica, ex: entry, kills, hs, kdr", days="Dias para filtro")
    @app_commands.autocomplete(metric=rank_metric_autocomplete)
    async def rank_slash(
        self,
        interaction: discord.Interaction,
        metric: str,
        days: app_commands.Range[int, 1, 365] = 30,
    ):
        metric_key = (metric or "").strip().lower()
        if metric_key not in STAT_DEFS:
            await interaction.response.send_message(
                "Metrica invalida. Use o autocomplete do parametro `metric` no /rank.",
                ephemeral=True,
            )
            return
        await self._handle_stat_interaction(interaction, metric_key, int(days))

    async def _handle_stat_interaction(self, interaction: discord.Interaction, metric_key: str, days: int = 30):
        if interaction.guild is None:
            await interaction.response.send_message("Use este comando dentro do servidor.", ephemeral=True)
            return

        await interaction.response.defer(thinking=False)
        rows = await _get_top_stat(metric_key, days)
        label = STAT_DEFS[metric_key]["label"]
        embed = discord.Embed(
            title=f"Top {label}",
            description=_render_table(rows, interaction.guild, metric_key, label, days),
            color=0x3498db,
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
    logger.debug("StatsCog carregado")
