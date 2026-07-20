import discord
from bot.config import (
    CANAL_STEAMID_ID, CANAL_AJUDA_ID, SALA_PROXIMO_ID,
    MEMBER_ROLE_ID, MEMBER_ROLE_NAME, CANAL_BOAS_VINDAS_ID
)
from bot.database import has_welcome_message, mark_welcome_message, get_registered_player


def build_welcome_embed(guild):
    steam_channel = guild.get_channel(CANAL_STEAMID_ID) if CANAL_STEAMID_ID else None
    proximo_channel = guild.get_channel(SALA_PROXIMO_ID) if SALA_PROXIMO_ID else None
    ajuda_channel = guild.get_channel(CANAL_AJUDA_ID) if CANAL_AJUDA_ID else None

    steam_text = f"<#{steam_channel.id}>" if steam_channel else "#cadastro"
    proximo_text = f"<#{proximo_channel.id}>" if proximo_channel else "Proximo"
    ajuda_text = f"<#{ajuda_channel.id}>" if ajuda_channel else "#ajuda"

    embed = discord.Embed(
        title="👋 Bem-vindo(a) ao BRASIL MIX",
        color=0x2ecc71,
    )
    embed.description = (
        "Para jogar, voce precisa vincular sua conta Steam:\n"
        f"1) Va no canal de cadastro: {steam_text}\n"
        "2) Clique no botao de cadastro fixado no canal\n"
        "3) Preencha SteamID e nickname e finalize o vinculo\n\n"
        f"Depois disso, entre na sala {proximo_text} para entrar na fila.\n"
        "Para ver o passo a passo completo, use `/ajuda`.\n"
        f"Se precisar de ajuda, entre no canal {ajuda_text}."
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


async def send_welcome_if_needed(member):
    if member.bot:
        return
    try:
        already_sent = await has_welcome_message(member.id)
    except Exception:
        already_sent = False
    if already_sent:
        return
    embed = build_welcome_embed(member.guild)
    if CANAL_BOAS_VINDAS_ID:
        try:
            welcome_channel = member.guild.get_channel(CANAL_BOAS_VINDAS_ID)
            if welcome_channel:
                await welcome_channel.send(content=member.mention, embed=embed)
        except Exception:
            pass
    try:
        await mark_welcome_message(member.id)
    except Exception:
        pass


async def sync_member_role_if_registered(member):
    if member.bot:
        return False

    try:
        player = await get_registered_player(member.id)
    except Exception:
        return False

    if not player:
        return False

    # Exige cadastro completo no modelo novo (players) antes de devolver o cargo.
    steamid64 = str(player.get("steamid64") or "").strip()
    if not steamid64:
        return False

    role = None
    if MEMBER_ROLE_ID:
        role = member.guild.get_role(MEMBER_ROLE_ID)
    if not role and MEMBER_ROLE_NAME:
        role = discord.utils.get(member.guild.roles, name=MEMBER_ROLE_NAME)
    if not role or role in member.roles:
        return False

    try:
        await member.add_roles(role, reason="Cadastro completo no players")
        return True
    except Exception:
        return False
