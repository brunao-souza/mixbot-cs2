import discord
from bot.config import (
    CANAL_STEAMID_ID, CANAL_AJUDA_ID, SALA_PROXIMO_ID,
    MEMBER_ROLE_ID, MEMBER_ROLE_NAME, CANAL_BOAS_VINDAS_ID
)
from bot.database import has_welcome_message, mark_welcome_message, get_registered_player


def build_welcome_embed(guild):
    steam_channel = guild.get_channel(CANAL_STEAMID_ID) if CANAL_STEAMID_ID else None
    next_channel = guild.get_channel(SALA_PROXIMO_ID) if SALA_PROXIMO_ID else None
    help_channel = guild.get_channel(CANAL_AJUDA_ID) if CANAL_AJUDA_ID else None

    steam_text = f"<#{steam_channel.id}>" if steam_channel else "#registration"
    next_text = f"<#{next_channel.id}>" if next_channel else "Next Room"
    help_text = f"<#{help_channel.id}>" if help_channel else "#help"

    embed = discord.Embed(
        title="👋 Welcome to BRASIL MIX",
        color=0x2ecc71,
    )
    embed.description = (
        "To play, you need to link your Steam account:\n"
        f"1) Go to the registration channel: {steam_text}\n"
        "2) Click the registration button pinned in the channel\n"
        "3) Fill in your SteamID and nickname, then finish linking\n\n"
        f"After that, join the {next_text} room to enter the queue.\n"
        "For the complete step-by-step, use `/help`.\n"
        f"If you need help, join the {help_text} channel."
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

    # Requires complete registration in the new model (players) before restoring the role.
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
        await member.add_roles(role, reason="Complete registration in players")
        return True
    except Exception:
        return False
