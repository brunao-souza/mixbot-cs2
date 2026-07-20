import discord
import re
from discord.ext import commands
from discord import app_commands
from discord import HTTPException
from loguru import logger
from typing import Optional
from datetime import date

from bot.database import (
    get_player_rank, get_top_ranking, get_player_stats,
    get_player_history, db,
    get_tournament_team_name_by_discord, get_tournament_wl_by_steamid,
)
from bot.config import CANAL_RANKING_ID, CANAL_GERAL_ID, SEASON_START_DATE
from bot.utils.faceit_api import get_faceit_profile


def _faceit_ball(level: Optional[int]) -> str:
    if level is None:
        return "\u26AA"
    if level == 1:
        return "\u26AA"
    if level in (2, 3):
        return "\U0001F7E2"
    if 4 <= level <= 7:
        return "\U0001F7E1"
    if level in (8, 9):
        return "\U0001F7E0"
    return "\U0001F534"


_EMOJI_RE = re.compile(
    "["
    "\\U0001F1E6-\\U0001F1FF"
    "\\U0001F300-\\U0001F5FF"
    "\\U0001F600-\\U0001F64F"
    "\\U0001F680-\\U0001F6FF"
    "\\U0001F700-\\U0001F77F"
    "\\U0001F780-\\U0001F7FF"
    "\\U0001F800-\\U0001F8FF"
    "\\U0001F900-\\U0001F9FF"
    "\\U0001FA00-\\U0001FAFF"
    "\\U00002600-\\U000026FF"
    "\\U00002700-\\U000027BF"
    "]+",
    flags=re.UNICODE,
)


def _strip_emojis(text: str) -> str:
    if not text:
        return text
    cleaned = _EMOJI_RE.sub("", text)
    cleaned = cleaned.replace("\\uFE0F", "").replace("\\u200D", "")
    return " ".join(cleaned.split())


def _format_last_match(match: Optional[dict]) -> str:
    if not match:
        return "Sem partidas recentes."

    player_team = match.get("team")
    team1 = match.get("team1_name")
    team2 = match.get("team2_name")
    score1 = match.get("map_score1", 0) or 0
    score2 = match.get("map_score2", 0) or 0

    won = ((player_team == team1 and score1 > score2) or
           (player_team == team2 and score2 > score1))
    result_text = "VITORIA" if won else "DERROTA"

    total_rounds = score1 + score2
    damage = match.get("damage", 0) or 0
    kills = match.get("kills", 0) or 0
    deaths = match.get("deaths", 0) or 0
    assists = match.get("assists", 0) or 0
    adr = (damage / total_rounds) if total_rounds > 0 else 0
    kd = (kills / deaths) if deaths > 0 else kills

    mapname = (match.get("mapname") or "").replace("de_", "").capitalize()
    score = f"{score1}-{score2}"
    return (
        f"{result_text} {score} {mapname}\n"
        f"K/D/A: {kills}/{deaths}/{assists} | K/D: {kd:.2f} | ADR: {adr:.0f}"
    )


def _build_profile_embed(target: discord.Member, rank_data: dict, stats: dict,
                         faceit_text: str, position, last_match: Optional[dict],
                         tournament_team_name: Optional[str], tournament_wins: int, tournament_losses: int) -> discord.Embed:
    
    # --- 1. Cálculos Estatísticos ---
    total_matches = rank_data.get("total_matches", 0) or 0
    wins = rank_data.get("wins", 0) or 0
    losses = rank_data.get("losses", 0) or 0
    winrate = (wins / total_matches * 100) if total_matches > 0 else 0

    avg_kills = stats.get("avg_kills", 0) or 0
    avg_deaths = stats.get("avg_deaths", 0) or 0
    avg_assists = stats.get("avg_assists", 0) or 0
    total_aces = stats.get("total_aces", 0) or 0
    
    # Multi-Kills
    total_2ks = stats.get("total_2ks", 0) or 0
    total_3ks = stats.get("total_3ks", 0) or 0
    total_4ks = stats.get("total_4ks", 0) or 0
    total_5ks = stats.get("total_5ks", 0) or 0
    multi_kills = total_2ks + total_3ks + total_4ks + total_5ks

    shots_fired = stats.get("shots_fired_total", 0) or 0
    shots_hit = stats.get("shots_on_target_total", 0) or 0
    entry_wins = stats.get("entry_wins", 0) or 0
    utility_damage_total = stats.get("utility_damage_total", 0) or 0
    utility_damage_avg = (utility_damage_total / total_matches) if total_matches > 0 else 0

    # ADR
    raw_val = stats.get("avg_adr", 0) or 0
    if raw_val > 200: calculated_adr = raw_val / 20
    elif raw_val <= 0: calculated_adr = 0
    else: calculated_adr = raw_val

    if avg_kills > 15 and calculated_adr < 50:
        calculated_adr = (raw_val * total_matches) / (total_matches * 20)

    kd = avg_kills / avg_deaths if avg_deaths > 0 else avg_kills
    headshot_kills = stats.get("head_shot_kills_total", 0) or 0
    precision = (headshot_kills / (avg_kills * total_matches) * 100) if (avg_kills > 0 and total_matches > 0) else 0

    # --- 2. Visual ---
    
    # Barra de Winrate
    blocks = int(winrate / 10)
    progress_bar = "🟩" * blocks + "⬛" * (10 - blocks)

    rating = rank_data.get('rating', 0)
    embed_color = 0x2ecc71

    # --- 3. Embed ---
    
    team_label = tournament_team_name or "Sem Time"
    embed = discord.Embed(
        title=f"👤 {target.display_name}    |    🛡️ Time: {team_label}",
        description=f"Análise de performance da Temporada Atual",
        color=embed_color,
        timestamp=discord.utils.utcnow(),
    )
    
    embed.set_thumbnail(url=target.avatar.url if target.avatar else None)

    # [MUDANÇA] Removido as crases (`) em volta do faceit_text para os asteriscos funcionarem
    embed.add_field(
        name="🏆 Classificação",
        value=(
            f"> **Rating:** `{rating} pts`\n"
            f"> **Ranking:** `#{position}`\n"
            f"> **Faceit** `{faceit_text}`" 
        ),
        inline=True
    )

    embed.add_field(
        name="🔥 Performance Média",
        value=(
            f"> **K/D Ratio:** `{kd:.2f}`\n"
            f"> **ADR:** `{calculated_adr:.1f}`\n"
            f"> **HS%:** `{precision:.0f}%`"
        ),
        inline=True
    )

    embed.add_field(
        name=f"📜 Histórico ({total_matches} partidas)",
        value=f"**{wins}V** - **{losses}D** (Streak: **{rank_data.get('win_streak', 0)}**)\n`{progress_bar}` **{winrate:.1f}%**",
        inline=False
    )

    # Tabela de Dados
    stats_block = (
        f"Kills:    {avg_kills:<5.1f} | Aces:      {total_aces}\n"
        f"Deaths:   {avg_deaths:<5.1f} | Multi-K:   {multi_kills}\n"
        f"Assists:  {avg_assists:<5.1f} | 1st Kills: {entry_wins}\n"
        f"Util Dmg: {utility_damage_avg:<5.0f} | HS%:      {precision:.0f}%"
    )
    
    embed.add_field(
        name="📈 Dados de Combate",
        value=f"```yaml\n{stats_block}\n```", 
        inline=False
    )

    embed.add_field(
        name=f"🛡️ Time - {team_label}",
        value=f"Torneio: `{tournament_wins}` W | `{tournament_losses}` L",
        inline=False
    )

    if last_match:
        # 1. Lógica de Vitória/Derrota
        player_team = last_match.get('team')
        s1 = last_match.get('map_score1', 0)
        s2 = last_match.get('map_score2', 0)
        
        # Verifica quem ganhou
        won = False
        if player_team == last_match.get('team1_name') and s1 > s2:
            won = True
        elif player_team == last_match.get('team2_name') and s2 > s1:
            won = True
            
        # 2. Define Ícone e Texto
        if won:
            icon = "🟢"
            status = "VITÓRIA"
        else:
            icon = "🔴"
            status = "DERROTA"

        # 3. Formata os Dados
        map_name = last_match.get('mapname', '').replace('de_', '').capitalize()
        score = f"{s1}-{s2}"
        
        k = last_match.get('kills', 0)
        d = last_match.get('deaths', 0)
        a = last_match.get('assists', 0)
        
        # Cálculos rápidos dessa partida específica
        kd_match = k / d if d > 0 else k
        rounds = s1 + s2
        adr_match = last_match.get('damage', 0) / rounds if rounds > 0 else 0
        bonus = max(0, min(20, int((adr_match / 100) * 20)))
        pts = (30 + bonus) if won else (-50 + bonus)
        pts_str = f"{pts:+d} pts"

        # 4. Monta o Texto Visual
        last_match_text = (
            f"{icon} **{status}** ({score}) • **{map_name}**\n"
            f"> 🔫 K/D/A: `{k}/{d}/{a}` • K/D: `{kd_match:.2f}`\n"
            f"> 💥 ADR: `{adr_match:.0f}` • 📊 Rank: `{pts_str}`"
        )

        embed.add_field(
            name="🕒 Última Partida",
            value=last_match_text,
            inline=False,
        )

    embed.set_footer(text="Dados sincronizados a cada partida", icon_url=target.display_avatar.url)
    
    return embed


def build_ranking_embed(top_players, guild, title, top3_label, classif_label, logo_url):
    embed = discord.Embed(
        title=title,
        color=0xFFD700, # Gold
        timestamp=discord.utils.utcnow()
    )
    if logo_url:
        embed.set_thumbnail(url=logo_url)

    # Top 3 Section
    top_3_text = ""
    others_list = []
    pos_real = 0

    for p in top_players:
        try:
            raw_id = int(p.get('discord_id'))
        except Exception:
            raw_id = None
        member = guild.get_member(raw_id) if raw_id else None
        nome_db = p.get('name', '')

        if not member and (not nome_db or nome_db.lower() in ['desconhecido', 'unknown']):
            continue

        pos_real += 1
        if pos_real > 50:
            break

        nome_bruto = member.display_name if member else nome_db
        wins = p.get('wins', 0)
        losses = p.get('losses', 0)
        rating = p.get('rating', 0)
        total = p.get('total_matches', 0)
        wr = (wins / total * 100) if total > 0 else 0

        if pos_real <= 3:
            nome_top3 = (nome_bruto[:14] + '..') if len(nome_bruto) > 14 else nome_bruto
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            mention = f"<@{raw_id}>" if raw_id else f"**{nome_top3}**"
            top_3_text += (
                f"{medals[pos_real]} {mention}\n"
                f"╰ **{rating} pts** • `{wins}V-{losses}D` • `{wr:.0f}% WR`\n\n"
            )
        else:
            nome_limpo = _strip_emojis(nome_bruto)
            nome_base = nome_limpo if nome_limpo else nome_bruto
            nome_final = nome_base[:14]
            # Precise alignment: POS(5)|PTS(7)|JOGADOR(16)|V-D(6)
            line = f"  {pos_real:02d} | {rating:>5} | {nome_final:<14} | {wins:>2}-{losses:<2} "
            others_list.append(line)

    header_table = " POS |  PTS  | JOGADOR        |  V-D \n"
    header_line  = "─────|───────|────────────────|──────\n"
    table_content = "\n".join(others_list)
    embed.description = (
        f"**{top3_label}**\n{top_3_text}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"**{classif_label}**\n"
        f"```format\n{header_table}{header_line}{table_content}```"
    )
    
    embed.set_footer(
        text="Ranking atualizado em tempo real • MixBot", 
        icon_url=guild.icon.url if guild.icon else None
    )
    
    return embed


class RankingCog(commands.Cog):
    """Cog para comandos de ranking e estatisticas"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._season_start_date: Optional[str] = None
        raw_cutoff = str(SEASON_START_DATE or "").strip()
        if raw_cutoff:
            try:
                date.fromisoformat(raw_cutoff)
                self._season_start_date = raw_cutoff
            except ValueError:
                logger.warning(
                    f"SEASON_START_DATE invalida ('{raw_cutoff}'). /perfil sem filtro de season."
                )

    @app_commands.command(name="ranking", description="Mostra o ranking atual dos jogadores.")
    async def ranking(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        """Exibe o Top 50 unificado, filtrando jogadores desconhecidos"""

        if ctx.channel.id != CANAL_RANKING_ID:
            if CANAL_RANKING_ID:
                await ctx.send(f"🔴 Use este comando em <#{CANAL_RANKING_ID}>.")
            return

        try:
            # Buscamos 100 para garantir que, apos filtrar os "Desconhecidos", sobrem 50 validos
            top_players = await get_top_ranking(100)

            if not top_players:
                await ctx.send("🔴 O ranking ainda esta vazio.")
                return

            logo_url = "https://cdn.discordapp.com/attachments/1452985230565834804/1466928923702071339/LogoMixLeve.png?ex=698081c5&is=697f3045&hm=68516ac68aae3734d5faee8835f6bfa197edc5fc0711eb6380d32c858e89ee25&"
            embed = build_ranking_embed(
                top_players,
                ctx.guild,
                "\U0001F3C6 RANKING TEMPORADA ATUAL",
                "\u2B50 LIDERES (TOP 3)",
                "\U0001F4CA CLASSIFICACAO",
                logo_url
            )
            await ctx.send(embed=embed)

        except Exception as e:
            logger.error(f"🔴 Erro no comando ranking: {e}")
            await ctx.send("🔴 Erro ao processar o ranking.")

    @app_commands.command(name="perfil", description="Mostra o perfil detalhado de um jogador.")
    async def perfil(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        ctx = await commands.Context.from_interaction(interaction)
        """Mostra o perfil do jogador em formato dashboard"""
        interaction = getattr(ctx, "interaction", None)
        if interaction and not interaction.response.is_done():
            try:
                await interaction.response.defer(thinking=False)
            except HTTPException as e:
                # Ja reconhecida por outro fluxo (ex.: bridge/app command).
                if getattr(e, "code", None) != 40060:
                    raise

        async def _send(*args, **kwargs):
            if interaction:
                return await interaction.followup.send(*args, **kwargs)
            return await ctx.send(*args, **kwargs)

        if ctx.channel.id != CANAL_GERAL_ID:
            if CANAL_GERAL_ID:
                await _send(f"🔴 Use este comando em <#{CANAL_GERAL_ID}>.")
            return
        target = member or ctx.author
        try:
            rank_data = await get_player_rank(target.id)
            if not rank_data or not rank_data.get('steamid64'):
                await _send(f"🔴 {target.display_name} ainda nao vinculou a conta Steam!")
                return
            if rank_data['total_matches'] == 0:
                await _send(f"🔴 {target.display_name} ainda nao jogou nenhuma partida!")
                return

            stats = await get_player_stats(rank_data['steamid64'], start_date=self._season_start_date)
            faceit_profile = await get_faceit_profile(str(rank_data['steamid64']))
            if faceit_profile and faceit_profile.get("elo") is not None:
                lvl = faceit_profile.get("level")
                ball = _faceit_ball(lvl)
                faceit_text = f"lvl {lvl} {ball} Elo {faceit_profile.get('elo')}"
            else:
                faceit_text = "Nao encontrado"

            position = "N/A"
            all_ranks = await get_top_ranking(1000)
            for i, p in enumerate(all_ranks, 1):
                if str(p.get('discord_id')) == str(target.id):
                    position = i
                    break

            history = await get_player_history(
                rank_data['steamid64'],
                1,
                start_date=self._season_start_date,
            )
            last_match = history[0] if history else None
            tournament_team_name = await get_tournament_team_name_by_discord(target.id)
            twl = await get_tournament_wl_by_steamid(
                str(rank_data["steamid64"]),
                start_date=self._season_start_date,
            )
            embed = _build_profile_embed(
                target,
                rank_data,
                stats,
                faceit_text,
                position,
                last_match,
                tournament_team_name,
                int(twl.get("wins", 0)),
                int(twl.get("losses", 0)),
            )
            await _send(embed=embed)
        except Exception as e:
            logger.error(f"🔴 Erro no comando perfil: {e}")
            await _send("🔴 Erro ao buscar perfil.")

    @app_commands.command(name="historico", description="Mostra o historico recente de partidas de um jogador.")
    async def historico(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        ctx = await commands.Context.from_interaction(interaction)
        """Mostra histórico das últimas 10 partidas com visual detalhado"""
        target = member or ctx.author
        
        try:
            rank_data = await get_player_rank(target.id)
            if not rank_data or not rank_data.get('steamid64'):
                await ctx.send(f"⚠️ **{target.display_name}** ainda não vinculou a conta Steam!")
                return
            
            # Busca histórico
            history = await get_player_history(rank_data['steamid64'], 10)
            if not history:
                await ctx.send(f"⚠️ **{target.display_name}** ainda não jogou nenhuma partida!")
                return

            # --- CÁLCULO DE RESUMO (W/L RECENTE) ---
            recent_wins = 0
            recent_losses = 0
            
            # Pré-processa para contar vitórias
            processed_matches = []
            for match in history:
                player_team = match['team']
                # Lógica de vitória
                won = ((player_team == match['team1_name'] and match['map_score1'] > match['map_score2']) or
                       (player_team == match['team2_name'] and match['map_score2'] > match['map_score1']))
                
                if won: recent_wins += 1
                else: recent_losses += 1
                
                processed_matches.append((match, won))

            # Define a cor baseada no desempenho recente (Mais vitórias = Verde, Mais derrotas = Vermelho)
            embed_color = 0x2ecc71 if recent_wins >= recent_losses else 0xe74c3c

            embed = discord.Embed(
                title=f"📜 Histórico de Partidas",
                description=f"Análise recente de **{target.display_name}**",
                color=embed_color
            )
            embed.set_thumbnail(url=target.avatar.url if target.avatar else None)
            
            # Campo de Resumo
            win_pct = (recent_wins / len(history)) * 100
            embed.add_field(
                name="📊 Desempenho Recente (Last 10)",
                value=f"**{recent_wins}V** - **{recent_losses}D** ({win_pct:.0f}% Winrate)",
                inline=False
            )

            # --- LOOP DAS PARTIDAS ---
            for match, won in processed_matches:
                # 1. Ícones e Títulos
                if won:
                    icon = "🟢"
                    status = "VITÓRIA"
                else:
                    icon = "🔴"
                    status = "DERROTA"

                # 2. Dados da partida
                mapa = match['mapname'].replace('de_', '').capitalize()
                score = f"{match['map_score1']}-{match['map_score2']}"
                
                # 3. Estatísticas Individuais
                total_rounds = match['map_score1'] + match['map_score2']
                adr = match['damage'] / total_rounds if total_rounds > 0 else 0
                kd = match['kills'] / match['deaths'] if match['deaths'] > 0 else match['kills']
                
                # 4. Pontos (Cálculo)
                bonus = max(0, min(20, int((adr / 100) * 20)))
                pts = (30 + bonus) if won else (-50 + bonus)
                pts_str = f"+{pts}" if pts > 0 else str(pts)
                
                # 5. Montagem do Campo Visual
                # Título: Ícone | Mapa (Placar)
                field_name = f"{icon} {status} | {mapa} ({score})"
                
                # Texto: Formatado com Blockquote (>)
                # K/D/A: 20/10/5 • K/D: 2.0 • ADR: 100 • +35 pts
                field_value = (
                    f"> 🔫 **K/D/A:** `{match['kills']}/{match['deaths']}/{match['assists']}`\n"
                    f"> 📊 **Stats:** `{kd:.2f} KD` • `{adr:.0f} ADR`\n"
                    f"> 📈 **Rank:** `{pts_str} pts` • ID: `#{match['matchid']}`"
                )

                embed.add_field(name=field_name, value=field_value, inline=False)

            embed.set_footer(text="MixBot System • Histórico detalhado", icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
            await ctx.send(embed=embed)

        except Exception as e:
            # logger.error(f"❌ Erro no comando historico: {e}") # Descomente se tiver logger
            print(f"Erro historico: {e}")
            await ctx.send("❌ Erro ao buscar histórico.")

async def setup(bot: commands.Bot):
    await bot.add_cog(RankingCog(bot))
    logger.debug("RankingCog carregado")
