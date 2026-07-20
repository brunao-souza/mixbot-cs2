#!/usr/bin/env python3
"""
Season 1 role sync utility.

What it does:
1) Remove Season 1 roles from all members who already have them.
2) Read current DB ranking and assign:
   - TOP 1 / TOP 2 / TOP 3
   - TOP 10 for ranks 4..10
   - TOP 50 for ranks 11..50

Default mode is dry-run. Use --apply to execute mutations.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pymysql
import requests

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


API_BASE = "https://discord.com/api/v10"

ROLE_NAME_DEFAULTS = {
    "top1": "\U0001F947TOP 1 - SEASON 1",
    "top2": "\U0001F948TOP 2 - SEASON 1",
    "top3": "\U0001F949TOP 3 - SEASON 1",
    "top10": "\U0001F396️TOP 10 - SEASON 1",
    "top50": "\U0001F3C5TOP 50 - SEASON 1",
}

ROLE_ENV_OVERRIDES = {
    "top1": "ROLE_TOP1_SEASON1_ID",
    "top2": "ROLE_TOP2_SEASON1_ID",
    "top3": "ROLE_TOP3_SEASON1_ID",
    "top10": "ROLE_TOP10_SEASON1_ID",
    "top50": "ROLE_TOP50_SEASON1_ID",
}


class DiscordClient:
    def __init__(self, token: str, timeout: int = 30):
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "mixbot-season1-role-sync/1.0",
            }
        )

    def request(self, method: str, path: str, **kwargs):
        url = f"{API_BASE}{path}"
        while True:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if resp.status_code == 429:
                retry_after = 1.5
                try:
                    retry_after = float(resp.json().get("retry_after") or 1.5)
                except Exception:
                    pass
                time.sleep(max(0.5, retry_after))
                continue

            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                raise RuntimeError(f"Discord API {method} {path} -> {resp.status_code}: {body}")

            if resp.status_code == 204:
                return None

            if resp.text:
                return resp.json()
            return None

    def maybe_request(self, method: str, path: str, **kwargs):
        url = f"{API_BASE}{path}"
        while True:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if resp.status_code == 429:
                retry_after = 1.5
                try:
                    retry_after = float(resp.json().get("retry_after") or 1.5)
                except Exception:
                    pass
                time.sleep(max(0.5, retry_after))
                continue
            if resp.status_code == 204:
                return 204, None
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                return resp.status_code, body
            if resp.text:
                return resp.status_code, resp.json()
            return resp.status_code, None

    def get_guild_id_from_channel(self, channel_id: str) -> str:
        data = self.request("GET", f"/channels/{channel_id}")
        guild_id = str(data.get("guild_id") or "").strip()
        if not guild_id:
            raise RuntimeError(f"Could not resolve guild_id from channel {channel_id}")
        return guild_id

    def get_roles(self, guild_id: str) -> List[Dict]:
        return self.request("GET", f"/guilds/{guild_id}/roles") or []

    def iter_guild_members(self, guild_id: str) -> Iterable[Dict]:
        after = "0"
        while True:
            page = self.request(
                "GET",
                f"/guilds/{guild_id}/members",
                params={"limit": 1000, "after": after},
            ) or []
            if not page:
                break
            for row in page:
                yield row
            after = str(page[-1].get("user", {}).get("id") or after)
            if len(page) < 1000:
                break

    def list_role_member_ids(self, guild_id: str, role_id: str) -> Set[str]:
        status, body = self.maybe_request(
            "GET",
            f"/guilds/{guild_id}/roles/{role_id}/members",
            params={"limit": 1000},
        )
        if status == 200:
            ids: Set[str] = set()
            for row in body or []:
                uid = str(((row or {}).get("user") or {}).get("id") or "").strip()
                if uid.isdigit():
                    ids.add(uid)
            return ids

        # Fallback: scan guild members and filter by role.
        ids: Set[str] = set()
        for member in self.iter_guild_members(guild_id):
            user_id = str(((member or {}).get("user") or {}).get("id") or "").strip()
            roles = {str(r) for r in (member or {}).get("roles") or []}
            if user_id.isdigit() and str(role_id) in roles:
                ids.add(user_id)
        return ids

    def add_role(self, guild_id: str, user_id: str, role_id: str) -> Tuple[bool, str]:
        status, body = self.maybe_request("PUT", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}")
        if status in (200, 201, 204):
            return True, "ok"
        if status == 404:
            return False, "not_in_guild_or_role_not_found"
        return False, f"error_{status}:{body}"

    def remove_role(self, guild_id: str, user_id: str, role_id: str) -> Tuple[bool, str]:
        status, body = self.maybe_request("DELETE", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}")
        if status in (200, 201, 204):
            return True, "ok"
        if status == 404:
            return False, "not_in_guild_or_role_not_found"
        return False, f"error_{status}:{body}"


def env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def pick_guild_id(dc: DiscordClient, explicit: str = "") -> str:
    if explicit and explicit.isdigit():
        return explicit

    from_env = env("DISCORD_GUILD_ID")
    if from_env.isdigit():
        return from_env

    channel_candidates = [env("CANAL_GERAL_ID"), env("CANAL_STEAMID_ID"), env("CANAL_FILA_ID")]
    for cid in channel_candidates:
        if cid.isdigit():
            return dc.get_guild_id_from_channel(cid)

    raise RuntimeError("Could not determine guild_id. Set DISCORD_GUILD_ID or pass --guild-id.")


def resolve_role_ids(dc: DiscordClient, guild_id: str, roles: List[Dict], create_missing: bool) -> Dict[str, str]:
    by_name = {str(r.get("name") or ""): str(r.get("id") or "") for r in roles}
    resolved: Dict[str, str] = {}
    missing: List[str] = []

    for key, role_name in ROLE_NAME_DEFAULTS.items():
        env_id = env(ROLE_ENV_OVERRIDES[key])
        if env_id.isdigit():
            resolved[key] = env_id
            continue
        role_id = by_name.get(role_name, "")
        if role_id.isdigit():
            resolved[key] = role_id
            continue
        if create_missing:
            created = dc.request(
                "POST",
                f"/guilds/{guild_id}/roles",
                json={"name": role_name, "mentionable": False, "hoist": False},
            )
            created_id = str((created or {}).get("id") or "").strip()
            if created_id.isdigit():
                resolved[key] = created_id
                by_name[role_name] = created_id
                print(f"Created missing role: {role_name} ({created_id})")
                continue
        missing.append(f"{key} ({role_name})")

    if missing:
        raise RuntimeError("Missing roles: " + ", ".join(missing))
    return resolved


def read_ranking_rows() -> List[Tuple[str, int, int]]:
    """Reads the current ranking from the Season 1 database (players + ranking)."""
    cfg = {
        "host": env("DB_HOST"),
        "user": env("DB_USER"),
        "password": env("DB_PASSWORD"),
        "database": env("DB_NAME"),
        "port": int(env("DB_PORT", "3306")),
        "charset": "utf8mb4",
    }
    conn = pymysql.connect(**cfg)
    try:
        with conn.cursor() as c:
            c.execute(
                """
                SELECT
                    p.discord_id,
                    COALESCE(r.rating, 1000) AS rating,
                    COALESCE(p.total_matches, 0) AS total_matches
                FROM players p
                LEFT JOIN ranking r ON r.id = p.id
                WHERE p.discord_id IS NOT NULL
                  AND p.discord_id <> ''
                  AND COALESCE(p.total_matches, 0) > 0
                ORDER BY COALESCE(r.rating, 1000) DESC,
                         COALESCE(p.total_matches, 0) DESC,
                         CAST(p.discord_id AS UNSIGNED) ASC
                """
            )
            rows = c.fetchall()
    finally:
        conn.close()

    clean: List[Tuple[str, int, int]] = []
    seen: Set[str] = set()
    for discord_id, rating, total_matches in rows:
        did = str(discord_id or "").strip()
        if not did.isdigit():
            continue
        if did in seen:
            continue
        seen.add(did)
        clean.append((did, int(rating or 1000), int(total_matches or 0)))
    return clean


def build_cohorts(rows: List[Tuple[str, int, int]]) -> Dict[str, List[str]]:
    ids = [r[0] for r in rows]
    cohorts = {
        "top1": ids[0:1],
        "top2": ids[1:2],
        "top3": ids[2:3],
        "top10": ids[3:10],
        "top50": ids[10:50],
    }
    return cohorts


def apply_or_preview(
    dc: DiscordClient,
    guild_id: str,
    role_ids: Dict[str, str],
    cohorts: Dict[str, List[str]],
    rows: List[Tuple[str, int, int]],
    apply: bool,
) -> None:
    season_role_keys = ["top1", "top2", "top3", "top10", "top50"]

    print("== Preview ==")
    print(f"guild_id={guild_id}")
    print(f"Total players in ranking: {len(rows)}")
    for key in season_role_keys:
        print(f"{key}: {len(cohorts[key])} candidates")

    # Print top 10 for validation
    print("\n== Top 10 players (discord_id | rating | matches) ==")
    for i, (did, rating, total_matches) in enumerate(rows[:10], 1):
        print(f"  #{i:2d}  discord_id={did}  rating={rating}  matches={total_matches}")

    print("\n== Reset existing roles ==")
    for key in season_role_keys:
        rid = role_ids[key]
        members = dc.list_role_member_ids(guild_id, rid)
        print(f"role {key} ({rid}): {len(members)} members currently")
        if not apply:
            continue
        removed_ok = 0
        removed_fail = 0
        for uid in members:
            ok, _ = dc.remove_role(guild_id, uid, rid)
            if ok:
                removed_ok += 1
            else:
                removed_fail += 1
        print(f"  removed_ok={removed_ok} removed_fail={removed_fail}")

    print("\n== Assign roles ==")
    for key in season_role_keys:
        rid = role_ids[key]
        targets = cohorts[key]
        print(f"assign {key} ({rid}) -> {len(targets)} targets")
        if not apply:
            continue

        ok_count = 0
        not_in_guild = 0
        fail_count = 0
        for uid in targets:
            ok, reason = dc.add_role(guild_id, uid, rid)
            if ok:
                ok_count += 1
            elif reason == "not_in_guild_or_role_not_found":
                not_in_guild += 1
            else:
                fail_count += 1
        print(f"  ok={ok_count} not_in_guild={not_in_guild} fail={fail_count}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Season 1 Discord roles from current ranking.")
    parser.add_argument("--apply", action="store_true", help="Apply mutations. Default is dry-run.")
    parser.add_argument("--guild-id", default="", help="Discord guild id (optional).")
    parser.add_argument(
        "--create-missing-roles",
        action="store_true",
        help="Create missing season roles automatically.",
    )
    args = parser.parse_args()

    if load_dotenv:
        load_dotenv()

    token = env("DISCORD_BOT_TOKEN") or env("DISCORD_TOKEN")
    if not token:
        print("Missing DISCORD_BOT_TOKEN/DISCORD_TOKEN in environment.", file=sys.stderr)
        return 1

    dc = DiscordClient(token=token)

    guild_id = pick_guild_id(dc, explicit=str(args.guild_id or ""))
    roles = dc.get_roles(guild_id)
    create_missing = bool(args.create_missing_roles and args.apply)
    if args.create_missing_roles and not args.apply:
        print("Note: --create-missing-roles without --apply runs as preview only (no role creation).")
    role_ids = resolve_role_ids(dc, guild_id, roles, create_missing=create_missing)

    rows = read_ranking_rows()
    cohorts = build_cohorts(rows)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Mode: {mode}\n")
    apply_or_preview(dc, guild_id, role_ids, cohorts, rows, apply=bool(args.apply))
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
