import sys
import asyncio
import os
from pathlib import Path

import aiohttp
from aiohttp import web

import discord
from discord.ext import commands
from loguru import logger

# Imports locais 
from bot.config import (
    DISCORD_BOT_TOKEN, CANAL_LOGS_ID, SALA_PROXIMO_ID, SERVERS,
    CANAL_PAINEL_TICKETS_ID, CANAL_PAINEL_PUNICOES_ID, CANAL_STEAMID_ID,
    INSTAGRAM_URL, SKINS_ACTIVITY_APPLICATION_ID, STEAM_GROUP_URL, WHATSAPP_GROUP_URL
)
from bot.database import db
from bot.utils.helpers import format_timestamp
 
# ================= 1. CONFIGURAÃ‡ÃƒO DE LOGS (RENDER) =================
# Configura o Loguru para enviar logs para o console do Render imediatamente
logger.remove()


def _stderr_log_filter(record):
    msg = str(record.get("message") or "")
    if "Cog carregado:" in msg or "Cog ja carregado:" in msg:
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

# ================= 2. CONFIGURAÃ‡ÃƒO DO BOT =================
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


def _build_grupos_embed(guild: discord.Guild | None) -> discord.Embed:
    embed = discord.Embed(
        title="🌐 Central de Grupos",
        description=(
            "Conecte-se com a nossa galera além do Discord! "
            "Fique por dentro de todas as novidades, sorteios e avisos importantes.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Escolha uma das plataformas abaixo para participar:"
        ),
        color=0xFFD700,  # Vibrant GOLD
        timestamp=discord.utils.utcnow(),
    )
    
    embed.add_field(
        name="📱 WhatsApp",
        value="> Bate-papo rápido, avisos de mixes e networking com os players.",
        inline=False,
    )
    embed.add_field(
        name="🎮 Grupo Steam",
        value="> Nossa casa oficial na Steam. Junte-se para ver as estatísticas e anúncios.",
        inline=False,
    )
    embed.add_field(
        name="📸 Instagram",
        value="> Acompanhe os melhores momentos, posts e a identidade do projeto.",
        inline=False,
    )

    embed.set_footer(
        text="Links oficiais verificados • Use com responsabilidade",
        icon_url=guild.icon.url if guild and guild.icon else None
    )

    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    return embed


def _build_grupos_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if WHATSAPP_GROUP_URL:
        view.add_item(
            discord.ui.Button(
                label="Entrar no WhatsApp",
                style=discord.ButtonStyle.link,
                url=WHATSAPP_GROUP_URL,
            )
        )
    if STEAM_GROUP_URL:
        view.add_item(
            discord.ui.Button(
                label="Grupo na Steam",
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


# ================= 3. SERVIDOR WEB (HEALTHCHECK INTERNO) =================
# MantÃ©m o Render feliz para nÃ£o reiniciar o bot por falta de porta aberta
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
        logger.exception(f"\u274C Erro no endpoint /cs2/chat: {e}")
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
        logger.exception(f"\u274C Erro no endpoint /cs2/bridge/poll: {e}")
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
        logger.exception(f"\u274C Erro no endpoint /matchzy/webhook: {e}")
        return web.json_response(
            {"ok": False, "error": "internal_error"},
            status=500
        )


async def start_web_server():
    """Inicia um servidor web leve na porta exigida pelo Render"""
    app = web.Application()
    app.router.add_get('/', health_check_handler)
    app.router.add_get('/health', health_check_handler)
    app.router.add_post('/cs2/chat', cs2_chat_proxy_handler)
    app.router.add_post('/cs2/bridge/poll', cs2_poll_proxy_handler)
    app.router.add_post('/matchzy/webhook', match_webhook_proxy_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Pega a porta da variÃ¡vel de ambiente PORT (ObrigatÃ³rio no Render)
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    
    await site.start()
    logger.success(f"\U0001F30D Web Server iniciado na porta {port}")

# ================= 4. EVENTOS DE CONEXÃƒO =================

@bot.event
async def on_ready():
    """Evento disparado quando o bot estÃ¡ pronto"""
    logger.success(f"\u2705 Bot online como {bot.user.name} (ID: {bot.user.id})")
    
    cs2_count = sum(1 for s in SERVERS.values() if s["active"])
    logger.info(f"\U0001F4E1 Conectado a {len(bot.guilds)} Guild(s) do Discord")
    logger.info(f"\U0001F3AE Configurado com {cs2_count} Servidor(es) de CS2")

    # ConexÃ£o com Banco de Dados
    try:
        await db.connect()
    except Exception as e:
        logger.critical(f"\u274C Falha ao conectar no DB: {e}")

    # Carregamento de ExtensÃµes (Cogs)
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
                logger.info(f"\u267b️ Cog ja carregado: {cog} (ignorando)")
                continue
            await bot.load_extension(cog)
            logger.info(f"\U0001F9E9 Cog carregado: {cog}")
        except Exception as e:
            logger.error(f"\u274C Erro ao carregar {cog}: {e}")

    global _persistent_views_loaded, _app_commands_synced
    if not _persistent_views_loaded:
        try:
            from bot.cogs.punicoes import PunicoesView
            from bot.cogs.denuncias import TicketPanelView, ensure_open_ticket_controls
            from bot.cogs.steam import CadastroPanelView
            bot.add_view(PunicoesView())
            bot.add_view(TicketPanelView())
            bot.add_view(CadastroPanelView())
            _persistent_views_loaded = True
            logger.info("\u2705 Views persistentes registradas")
        except Exception as e:
            logger.error(f"\u274C Erro ao registrar views persistentes: {e}")

    try:
        from bot.cogs.punicoes import PunicoesView
        from bot.cogs.denuncias import TicketPanelView, ensure_open_ticket_controls
        from bot.cogs.steam import CadastroPanelView, build_cadastro_panel_embed

        if CANAL_PAINEL_TICKETS_ID:
            channel = bot.get_channel(CANAL_PAINEL_TICKETS_ID)
            if channel:
                embed = discord.Embed(
                    title="🎫 Central de Suporte",
                    description=(
                        "Precisa de ajuda ou quer realizar uma denúncia? "
                        "Abra um ticket clicando no botão abaixo para conversar com a nossa Staff em um canal privado.\n\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    color=0x3498DB
                )
                embed.set_footer(text="Atendimento via tickets prioritário")
                await _delete_all_panel_messages(channel, "🎫 Central de Suporte")
                await channel.send(embed=embed, view=TicketPanelView())

        if CANAL_PAINEL_PUNICOES_ID:
            channel = bot.get_channel(CANAL_PAINEL_PUNICOES_ID)
            if channel:
                await _delete_all_panel_messages(channel, "⚖️ Painel de Punições")

        if CANAL_STEAMID_ID:
            channel = bot.get_channel(CANAL_STEAMID_ID)
            if channel:
                embed = build_cadastro_panel_embed(channel.guild)
                await _delete_all_panel_messages(channel, "🛡️ Registro de Atleta")
                await channel.send(embed=embed, view=CadastroPanelView())
        try:
            await ensure_open_ticket_controls(bot)
        except Exception as e:
            logger.error(f"\u274C Erro ao renovar tickets abertos: {e}")
    except Exception as e:
        logger.error(f"\u274C Erro ao criar paineis automaticos: {e}")

    if not _app_commands_synced:
        try:
            # Prefer guild-scoped slash commands for immediate availability after deploys.
            # (Global app commands can take a while to propagate to all users.)
            for guild in bot.guilds:
                bot.tree.clear_commands(guild=guild)
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)

            _app_commands_synced = True
            logger.info("\u2705 Comandos slash sincronizados")
        except Exception as e:
            logger.error(f"\u274c Erro ao sincronizar comandos slash: {e}")

    logger.success("\U0001F680 Sistema inicializado e pronto para uso!")

@bot.event
async def on_disconnect():
    logger.warning("\u26A0\uFE0F Conexao instavel...")

@bot.event
async def on_resumed():
    logger.success("\u26A1 Conexao restaurada.")

@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound): return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("\u274C Voce nao tem permissao.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"\u274C Falta argumento: `{error.param.name}`")
    else:
        logger.error(f"\u274C Erro comando {ctx.command}: {error}")

# ================= 5. COMANDOS BÃ SICOS =================

@bot.tree.command(name="ping", description="Mostra a latencia atual do bot.")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! Latencia: **{latency}ms**")


@bot.tree.command(name="ajuda", description="Mostra o guia rapido de como jogar mix.")
async def ajuda(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Guia do Atleta - Como Jogar",
        description=(
            "Bem-vindo à nossa comunidade! Siga este guia rápido para entrar na ação e competir no MIX.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0x9B59B6,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="1️⃣ Vincule sua conta",
        value=(
            f"Acesse o canal <#{CANAL_STEAMID_ID}> e faça seu registro.\n"
            "*Sem o vínculo, você não poderá entrar nos servidores.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣ Entre na fila",
        value=(
            f"Conecte-se na sala de voz <#{SALA_PROXIMO_ID}>.\n"
            "*O bot iniciará o match automático assim que houver 10 jogadores.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣ Aceite o Mix",
        value=(
            "Fique atento ao seu Discord! Clique em **ACEITAR** quando o bot convocar.\n"
            "*Recusar ou demorar para aceitar gera punição automática.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="4️⃣ No Servidor",
        value=(
            "O bot informará o IP. O Draft de capitães e a partida ocorrem automaticamente.\n"
            "*Seus stats e ranking serão atualizados ao final de cada jogo.*"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Comandos Úteis",
        value=(
            "> `/startmix` • `/perfil` • `/historico` • `/ranking` • `/cadastro` • `/grupos`"
        ),
        inline=False,
    )
    embed.set_footer(text="Dica: entrou no Discord hoje? Comece pelo painel de cadastro e depois va para a fila.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="grupos", description="Publica a embed com os grupos oficiais da comunidade.")
async def grupos(interaction: discord.Interaction):
    if not WHATSAPP_GROUP_URL and not STEAM_GROUP_URL and not INSTAGRAM_URL:
        await interaction.response.send_message(
            "ERRO: nenhum link de grupo esta configurado.",
            ephemeral=True,
        )
        return

    embed = _build_grupos_embed(interaction.guild)
    view = _build_grupos_view()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("Embed de grupos publicada neste canal.", ephemeral=True)

# ================= 6. INICIALIZAÃ‡ÃƒO =================

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


@bot.tree.command(name="skins", description="Abre a Activity de skins.")
async def skins(interaction: discord.Interaction):
    if SKINS_ACTIVITY_APPLICATION_ID <= 0:
        await interaction.response.send_message(
            "ERRO: Activity de skins nao esta configurada. Defina `SKINS_ACTIVITY_APPLICATION_ID` no ambiente.",
            ephemeral=True,
        )
        return

    ok, reason = await _launch_activity_native(interaction, SKINS_ACTIVITY_APPLICATION_ID)
    if ok:
        logger.info(
            f"Native /skins launch OK guild={getattr(interaction.guild, 'id', 0)} user={interaction.user.id}"
        )
        return

    logger.error(f"ERRO ao iniciar Activity nativa /skins: {reason}")
    if not interaction.response.is_done():
        await interaction.response.send_message(
            "ERRO: Falha ao abrir a Activity. Verifique se o app da Activity esta instalado/publicado no Discord Developer Portal.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "ERRO: Falha ao abrir a Activity. Tente novamente em instantes.",
            ephemeral=True,
        )
async def main():
    Path("logs").mkdir(exist_ok=True)

    # 1. Inicia o Web Server (Essencial para o Render)
    await start_web_server()

    # 2. Inicia o Bot
    try:
        await bot.start(DISCORD_BOT_TOKEN)
    except Exception as e:
        logger.critical(f"\u274C Falha critica ao iniciar: {e}")
    finally:
        logger.info("\U0001F6D1 Encerrando...")
        await db.close()
        await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
