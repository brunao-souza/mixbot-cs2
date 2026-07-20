import discord
from typing import List
from datetime import datetime


def format_team(team: List[discord.Member]) -> str:
    """Formata lista de membros para exibição"""
    return "\n".join(f"- {p.display_name}" for p in team)


def format_timestamp() -> str:
    """Retorna timestamp formatado para logs"""
    return datetime.now().strftime("%H:%M:%S")


def truncate_name(name: str, max_length: int = 12) -> str:
    """Trunca um nome mantendo comprimento máximo"""
    return name[:max_length] if len(name) > max_length else name


def calculate_adr(damage: int, rounds: int) -> float:
    """Calcula ADR (Average Damage per Round)"""
    return damage / rounds if rounds > 0 else 0.0


def calculate_kd(kills: int, deaths: int) -> float:
    """Calcula K/D ratio"""
    return kills / deaths if deaths > 0 else float(kills)