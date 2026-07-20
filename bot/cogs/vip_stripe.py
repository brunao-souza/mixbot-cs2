"""
bot/cogs/vip_stripe.py
======================
Monthly VIP cog via Stripe Subscriptions — discord.py 2.x

STACK: MySQL (aiomysql via bot.database.db), loguru, aiohttp.web, discord.ext.tasks

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TABLES CREATED AUTOMATICALLY (cog_load)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  vip_subscriptions
    discord_user_id        BIGINT       PRIMARY KEY
    stripe_customer_id     TEXT         DEFAULT NULL
    stripe_subscription_id TEXT         DEFAULT NULL
    vip_active             TINYINT(1)   NOT NULL DEFAULT 0
    vip_expires_at         DATETIME     DEFAULT NULL   -- UTC naive
    updated_at             DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                        ON UPDATE CURRENT_TIMESTAMP

  stripe_events_processed
    event_id               VARCHAR(255) PRIMARY KEY
    processed_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP

EXISTING TABLES (required):
    players        (discord_id BIGINT, steamid64 VARCHAR)
    wp_player_skins (steamid VARCHAR, ...)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ENVIRONMENT VARIABLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  STRIPE_SECRET_KEY       sk_live_... or sk_test_...
  STRIPE_WEBHOOK_SECRET   whsec_...
  STRIPE_PRICE_ID         price_...  (monthly plan)
  GUILD_ID                Discord server ID
  VIP_ROLE_ID             VIP role ID
  PUBLIC_BASE_URL         https://yoursite.com  (no trailing slash)
  WEBHOOK_HOST            0.0.0.0  (default)
  WEBHOOK_PORT            8765     (different from Render/main port)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK SETUP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Install dependencies:
      pip install stripe>=5.0.0

2. Stripe Dashboard → Product catalog:
   • Create product → Add recurring price (monthly)
   • Copy price_xxx → STRIPE_PRICE_ID in .env

3. Stripe Dashboard → Developers → Webhooks → Add endpoint:
   • URL: https://yoursite.com/stripe/webhook
     (or expose WEBHOOK_PORT via nginx proxy_pass / Cloudflare Tunnel)
   • Events to select:
       checkout.session.completed
       invoice.paid
       invoice.payment_succeeded
       customer.subscription.updated
       customer.subscription.deleted
   • Copy whsec_xxx → STRIPE_WEBHOOK_SECRET in .env

4. Nginx on VPS — add inside the domain server block:
      location /stripe/webhook {
          proxy_pass         http://127.0.0.1:8765;
          proxy_http_version 1.1;
          proxy_set_header   Host $host;
          proxy_set_header   X-Real-IP $remote_addr;
          client_max_body_size 1m;
      }
   Then: nginx -t && systemctl reload nginx

5. Stripe Dashboard → Developers → Webhooks → Add endpoint:
      https://yourdomain.com/stripe/webhook

6. Add the cog in main.py (see the end of this file).

7. Test with Stripe CLI (optional, local dev):
      stripe listen --forward-to localhost:8765/stripe/webhook
      stripe trigger invoice.paid

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ABOUT cancel_at_period_end
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When the user cancels but chooses to keep access until the end of the
paid period, Stripe sets cancel_at_period_end=True:
  • subscription.updated   → cancel_at_period_end=True → we keep VIP
  • subscription.deleted   → only fires AFTER period_end expires
In both cases, revoking on subscription.deleted is correct and safe.
"""

from __future__ import annotations

import asyncio
import functools
import os
from datetime import datetime, timezone, time as dt_time
from typing import Optional

import discord
import stripe
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from loguru import logger

from bot.database import db

# ══════════════════════════════════════════════════════════════════════════════
# Configuration via ENV
# ══════════════════════════════════════════════════════════════════════════════

STRIPE_SECRET_KEY        = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET    = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID          = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_PRICE_ID_SEMESTRAL = os.getenv("STRIPE_PRICE_ID_SEMESTRAL", "")
STRIPE_PRICE_ID_ANUAL     = os.getenv("STRIPE_PRICE_ID_ANUAL", "")
GUILD_ID              = int(os.getenv("GUILD_ID", "0"))
VIP_ROLE_ID           = int(os.getenv("VIP_ROLE_IDS", os.getenv("VIP_ROLE_ID", "0")).split(",")[0].strip())
PUBLIC_BASE_URL       = os.getenv("PUBLIC_BASE_URL", "https://example.com").rstrip("/")
WEBHOOK_HOST          = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT          = int(os.getenv("WEBHOOK_PORT", "8765"))

stripe.api_key = STRIPE_SECRET_KEY


# ══════════════════════════════════════════════════════════════════════════════
# Stripe helpers — wraps synchronous SDK in executor to not block the event loop
# ══════════════════════════════════════════════════════════════════════════════

async def _stripe_call(fn, *args, **kwargs):
    """Executes synchronous Stripe call in thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


def _get_period_end(sub) -> datetime:
    """
    Extracts current_period_end from a Stripe Subscription object.
    Compatible with classic API and API ≥2025-12-15.clover (moved field).
    """
    # Classic path
    ts = None
    try:
        ts = sub["current_period_end"]
    except (KeyError, TypeError):
        pass

    # API ≥2025-12-15.clover: may be in items
    if not ts:
        try:
            items_data = sub["items"]["data"]
            if items_data:
                ts = items_data[0]["current_period_end"]
        except (KeyError, TypeError, IndexError):
            pass

    # Fallback: billing_cycle_anchor + 31 days (last resort)
    if not ts:
        try:
            ts = int(sub.get("billing_cycle_anchor") or 0)
            if ts:
                from datetime import timedelta
                return datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(days=31)
        except Exception:
            pass

    if not ts:
        keys = list(sub.keys()) if hasattr(sub, "keys") else "?"
        raise KeyError(
            f"current_period_end not found in the subscription. "
            f"Available fields: {keys}"
        )

    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


async def _fetch_subscription(sub_id: str, retries: int = 3) -> stripe.Subscription:
    """Fetches subscription from Stripe with retry + exponential backoff."""
    delay = 0.5
    last_err: Exception = RuntimeError("unreachable")
    for attempt in range(retries):
        try:
            return await _stripe_call(stripe.Subscription.retrieve, sub_id)
        except (stripe.RateLimitError, stripe.APIConnectionError) as exc:
            last_err = exc
            logger.warning(f"[VIP] Stripe retry {attempt + 1}/{retries}: {exc}")
            await asyncio.sleep(delay)
            delay *= 2
        except stripe.StripeError:
            raise
    raise last_err


# ══════════════════════════════════════════════════════════════════════════════
# DB helpers — MySQL / aiomysql via bot.database.db
# ══════════════════════════════════════════════════════════════════════════════

async def _ensure_vip_tables() -> None:
    """Creates VIP tables if they do not exist (idempotent)."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vip_subscriptions (
                discord_user_id        BIGINT    NOT NULL PRIMARY KEY,
                stripe_customer_id     TEXT      DEFAULT NULL,
                stripe_subscription_id TEXT      DEFAULT NULL,
                vip_active             TINYINT   NOT NULL DEFAULT 0,
                vip_expires_at         DATETIME  DEFAULT NULL,
                updated_at             DATETIME  NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stripe_events_processed (
                event_id     VARCHAR(255) NOT NULL PRIMARY KEY,
                processed_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
    logger.info("[VIP] VIP tables checked/created")


async def _try_claim_event(event_id: str) -> bool:
    """
    Attempts to claim an event_id atomically via INSERT IGNORE.
    Returns True if this process was the first (should process).
    Returns False if it already exists (duplicate — ignore).
    """
    rows = await db.execute(
        "INSERT IGNORE INTO stripe_events_processed (event_id) VALUES (%s)",
        (event_id,),
    )
    return rows > 0


async def _unclaim_event(event_id: str) -> None:
    """Reverses claim of a failed event to allow retry from Stripe."""
    await db.execute(
        "DELETE FROM stripe_events_processed WHERE event_id = %s",
        (event_id,),
    )


async def _upsert_link(discord_uid: int, customer_id: str, sub_id: str) -> None:
    """Saves/updates discord_user_id ↔ customer/subscription binding."""
    await db.execute(
        """
        INSERT INTO vip_subscriptions
            (discord_user_id, stripe_customer_id, stripe_subscription_id)
        VALUES (%s, %s, %s) AS new_row
        ON DUPLICATE KEY UPDATE
            stripe_customer_id     = new_row.stripe_customer_id,
            stripe_subscription_id = new_row.stripe_subscription_id,
            updated_at             = CURRENT_TIMESTAMP
        """,
        (discord_uid, customer_id, sub_id),
    )


async def _set_active(
    discord_uid: int,
    customer_id: str,
    sub_id: str,
    expires: datetime,
) -> None:
    """Activates VIP in the DB with expiration date."""
    # MySQL doesn't store timezone — store as naive UTC
    naive = expires.replace(tzinfo=None)
    await db.execute(
        """
        INSERT INTO vip_subscriptions
            (discord_user_id, stripe_customer_id, stripe_subscription_id,
             vip_active, vip_expires_at)
        VALUES (%s, %s, %s, 1, %s) AS new_row
        ON DUPLICATE KEY UPDATE
            stripe_customer_id     = new_row.stripe_customer_id,
            stripe_subscription_id = new_row.stripe_subscription_id,
            vip_active             = 1,
            vip_expires_at         = new_row.vip_expires_at,
            updated_at             = CURRENT_TIMESTAMP
        """,
        (discord_uid, customer_id, sub_id, naive),
    )


async def _set_inactive(discord_uid: int) -> None:
    """Desativa VIP no DB."""
    await db.execute(
        """
        UPDATE vip_subscriptions
        SET vip_active = 0, updated_at = CURRENT_TIMESTAMP
        WHERE discord_user_id = %s
        """,
        (discord_uid,),
    )


async def _get_record(discord_uid: int) -> Optional[dict]:
    return await db.fetchone(
        "SELECT * FROM vip_subscriptions WHERE discord_user_id = %s",
        (discord_uid,),
    )


async def _get_expired_active() -> list[dict]:
    """
    VIPs marked as active in the DB but with vip_expires_at already past.
    Used by the expiry check for recovery after bot restart.
    """
    return await db.fetchall(
        """
        SELECT discord_user_id
        FROM vip_subscriptions
        WHERE vip_active = 1
          AND vip_expires_at IS NOT NULL
          AND vip_expires_at < UTC_TIMESTAMP()
        """
    )


async def _find_uid_by_customer(customer_id: str) -> Optional[int]:
    row = await db.fetchone(
        "SELECT discord_user_id FROM vip_subscriptions WHERE stripe_customer_id = %s",
        (customer_id,),
    )
    return int(row["discord_user_id"]) if row else None


async def _find_uid_by_sub(sub_id: str) -> Optional[int]:
    row = await db.fetchone(
        "SELECT discord_user_id FROM vip_subscriptions WHERE stripe_subscription_id = %s",
        (sub_id,),
    )
    return int(row["discord_user_id"]) if row else None


def _get_plan_name(price_id: str) -> Optional[str]:
    """Returns the plan name ('monthly'/'semiannual'/'annual') from the configured price_id."""
    if price_id and price_id == STRIPE_PRICE_ID:
        return "monthly"
    if price_id and price_id == STRIPE_PRICE_ID_SEMESTRAL:
        return "semiannual"
    if price_id and price_id == STRIPE_PRICE_ID_ANUAL:
        return "annual"
    return None


async def _get_steamid(discord_uid: int) -> Optional[str]:
    row = await db.fetchone(
        "SELECT steamid64 FROM players WHERE discord_id = %s",
        (discord_uid,),
    )
    return row["steamid64"] if row else None


# ══════════════════════════════════════════════════════════════════════════════
    # UI — Manual revocation confirmation
# ══════════════════════════════════════════════════════════════════════════════

class RevokeConfirmView(discord.ui.View):
    def __init__(self, target_id: int, cog: "VipStripeCog"):
        super().__init__(timeout=30)
        self.target_id = target_id
        self.cog = cog

    @discord.ui.button(label="Confirm Revocation", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(content="⏳ Revogando VIP...", view=self)
        await self.cog._revoke_vip(self.target_id, reason="revogado manualmente por admin")
        await interaction.followup.send(
            f"✅ VIP for user `{self.target_id}` revoked successfully.",
            ephemeral=True,
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Operation cancelled.", view=None)


class LimparSkinsConfirmView(discord.ui.View):
    def __init__(self, cog: "VipStripeCog", steamids: list[str]):
        super().__init__(timeout=60)
        self.cog = cog
        self.steamids = steamids

    @discord.ui.button(label="Confirmar limpeza", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(content="⏳ Removendo skins...", view=self)
        _SKIN_TABLES = [
            "wp_player_skins",
            "wp_player_knife",
            "wp_player_agents",
            "wp_player_gloves",
            "wp_player_music",
            "wp_player_pins",
        ]
        placeholders = ",".join(["%s"] * len(self.steamids))
        total = 0
        errors = []
        for table in _SKIN_TABLES:
            try:
                deleted = await db.execute(
                    f"DELETE FROM {table} WHERE steamid IN ({placeholders})",
                    tuple(self.steamids),
                )
                total += deleted or 0
            except Exception as exc:
                logger.error(f"[VIP] Error in /vipclearskins deleting from {table}: {exc}")
                errors.append(table)
        logger.info(
            f"[VIP] vipclearskins: {total} line(s) removed de {len(self.steamids)} steamid(s) non-VIP"
        )
        if errors:
            await interaction.followup.send(
                f"⚠️ Limpeza parcial: **{total}** line(s) removed de **{len(self.steamids)}** jogador(es).\n"
                f"Erros nas tabelas: {', '.join(errors)}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ Cleanup completed! **{total}** line(s) removed de **{len(self.steamids)}** player(s) without VIP.",
                ephemeral=True,
            )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Operation cancelled.", view=None)


# ══════════════════════════════════════════════════════════════════════════════
# Cog principal
# ══════════════════════════════════════════════════════════════════════════════

class VipStripeCog(commands.Cog):
    """Gerencia assinaturas VIP mensais via Stripe Subscriptions."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._runner: Optional[web.AppRunner] = None
        self._server_task: Optional[asyncio.Task] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        await _ensure_vip_tables()

        if not STRIPE_SECRET_KEY:
            logger.error("[VIP] STRIPE_SECRET_KEY ausente — cog carregada mas inativa")
            return
        if not STRIPE_WEBHOOK_SECRET:
            logger.warning("[VIP] STRIPE_WEBHOOK_SECRET missing — webhooks without signature validation")

        self._server_task = asyncio.create_task(
            self._start_webhook_server(),
            name="vip-stripe-webhook",
        )

        # Safety net: verifica VIPs expirados a cada 30 min (cobre restarts do bot)
        self._expiry_check.start()

        logger.info(f"[VIP] Servidor webhook agendado em {WEBHOOK_HOST}:{WEBHOOK_PORT}")

    async def cog_unload(self) -> None:
        self._expiry_check.cancel()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
            self._server_task = None
        logger.info("[VIP] Webhook server encerrado")

    # ── Background task: expiry check ─────────────────────────────────────────

    @tasks.loop(time=dt_time(0, 1, tzinfo=timezone.utc))
    async def _expiry_check(self) -> None:
        """
        Safety net que revoga VIPs com vip_expires_at expirado no DB.
        Runs once daily at 00:01 UTC.
        """
        try:
            rows = await _get_expired_active()
            for row in rows:
                uid = int(row["discord_user_id"])
                logger.info(f"[VIP] Expiry check → revogando VIP expirado user={uid}")
                await self._revoke_vip(uid, reason="expirado (expiry check)")
        except Exception as exc:
            logger.error(f"[VIP] Error in expiry check: {exc}")

    @_expiry_check.before_loop
    async def _before_expiry_check(self) -> None:
        await self.bot.wait_until_ready()

    # ── Servidor HTTP para webhooks Stripe ────────────────────────────────────

    async def _start_webhook_server(self) -> None:
        app = web.Application(client_max_size=1 * 1024 * 1024)  # 1 MB max
        app.router.add_post("/stripe/webhook", self._handle_webhook_request)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, WEBHOOK_HOST, WEBHOOK_PORT)
        await site.start()
        logger.success(
            f"[VIP] Webhook Stripe escutando em "
            f"http://{WEBHOOK_HOST}:{WEBHOOK_PORT}/stripe/webhook"
        )

    async def _handle_webhook_request(self, request: web.Request) -> web.Response:
        # Reads raw payload — required for signature validation
        try:
            payload = await request.read()
        except Exception as exc:
            logger.error(f"[VIP] Falha ao ler payload: {exc}")
            return web.Response(status=400, text="bad request")

        sig_header = request.headers.get("Stripe-Signature", "")

        # 1) Validates signature (HMAC synchronous — no I/O, does not block)
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except stripe.SignatureVerificationError:
            logger.warning("[VIP] Invalid Stripe signature — discarding")
            return web.Response(status=400, text="invalid signature")
        except Exception as exc:
            logger.error(f"[VIP] Erro ao construir evento Stripe: {exc}")
            return web.Response(status=400, text="bad payload")

        event_id   = event["id"]
        event_type = event["type"]
        logger.debug(f"[VIP] Webhook recebido: {event_type} ({event_id})")

        # 2) Atomic idempotency — INSERT IGNORE garante "first-write-wins"
        claimed = await _try_claim_event(event_id)
        if not claimed:
            logger.info(f"[VIP] Evento {event_id} already processed — ignoring")
            return web.Response(status=200, text="already processed")

        # 3) Processa evento; em falha, desfaz o claim para o Stripe reenviar
        obj = event["data"]["object"]
        try:
            if event_type == "checkout.session.completed":
                await self._on_checkout_completed(obj)
            elif event_type in ("invoice.paid", "invoice.payment_succeeded"):
                await self._on_invoice_paid(obj)
            elif event_type == "customer.subscription.updated":
                await self._on_subscription_updated(obj)
            elif event_type == "customer.subscription.deleted":
                await self._on_subscription_deleted(obj)
            else:
                logger.debug(f"[VIP] Event ignored (not handled): {event_type}")
        except Exception as exc:
            logger.exception(
                f"[VIP] Error processing {event_type} ({event_id}): {exc}"
            )
            await _unclaim_event(event_id)  # permite retry pelo Stripe
            return web.Response(status=500, text="internal error")

        return web.Response(status=200, text="ok")

    # ── Handlers de eventos Stripe ─────────────────────────────────────────────

    async def _on_checkout_completed(self, session: dict) -> None:
        """
        checkout.session.completed

        Saves the binding user ↔ customer/subscription.
        If payment_status == "paid" (card), grants VIP immediately.
        Otherwise (SEPA/boleto), waits for invoice.paid.
        """
        customer_id    = session.get("customer")
        sub_id         = session.get("subscription")
        meta           = session.get("metadata") or {}
        payment_status = session.get("payment_status", "")

        # discord_user_id vem via metadata OU client_reference_id (fallback)
        discord_uid = int(meta.get("discord_user_id") or 0)
        if not discord_uid:
            try:
                discord_uid = int(session.get("client_reference_id") or 0)
            except (ValueError, TypeError):
                pass

        if not discord_uid:
            logger.warning(
                f"[VIP] checkout.session.completed sem discord_user_id "
                f"(customer={customer_id})"
            )
            return

        if customer_id and sub_id:
            await _upsert_link(discord_uid, customer_id, sub_id)
            logger.info(
                f"[VIP] Binding saved: user={discord_uid} "
                f"customer={customer_id} sub={sub_id}"
            )

            # Payment confirmed (card) → grants VIP now without waiting for invoice.paid
            if payment_status == "paid":
                try:
                    sub = await _fetch_subscription(sub_id)
                    period_end = _get_period_end(sub)
                    await _set_active(discord_uid, customer_id, sub_id, period_end)
                    await self._grant_vip(discord_uid, until=period_end)
                    logger.info(
                        f"[VIP] VIP concedido via checkout: user={discord_uid} "
                        f"sub={sub_id} until {period_end.isoformat()}"
                    )
                except Exception as exc:
                    logger.error(f"[VIP] Error granting VIP on checkout: {exc}")
                    raise  # propagates → Stripe resends

    async def _on_invoice_paid(self, invoice: dict) -> None:
        """
        invoice.paid / invoice.payment_succeeded

        Grants/renews VIP for the paid period.
        Busca current_period_end na Subscription para definir vip_expires_at.
        """
        customer_id = invoice.get("customer")

        # API ≥2025-12-15.clover: subscription migrou para parent.subscription_details.subscription
        sub_id = invoice.get("subscription")
        if not sub_id:
            parent = invoice.get("parent") or {}
            sub_details = parent.get("subscription_details") or {}
            sub_id = sub_details.get("subscription")

        if not customer_id or not sub_id:
            logger.warning(
                f"[VIP] invoice.paid sem customer ou subscription "
                f"(customer={customer_id} sub={sub_id}) — ignorando"
            )
            return

        # Resolve discord_user_id a partir do customer ou sub
        discord_uid = await _find_uid_by_customer(customer_id)
        if not discord_uid:
            discord_uid = await _find_uid_by_sub(sub_id)

        # Fallback: race condition — checkout.session.completed pode chegar
        # milissegundos depois do invoice.paid. Busca o discord_user_id
        # diretamente do checkout session no Stripe.
        if not discord_uid and sub_id:
            try:
                sessions = await _stripe_call(
                    stripe.checkout.Session.list,
                    subscription=sub_id,
                    limit=1,
                )
                if sessions.data:
                    cs = sessions.data[0]
                    meta = cs.get("metadata") or {}
                    discord_uid = int(meta.get("discord_user_id") or 0)
                    if not discord_uid:
                        try:
                            discord_uid = int(cs.get("client_reference_id") or 0)
                        except (ValueError, TypeError):
                            pass
                    if discord_uid:
                        logger.info(
                            f"[VIP] discord_uid resolvido via checkout session "
                            f"(fallback race condition): user={discord_uid}"
                        )
                        await _upsert_link(discord_uid, customer_id, sub_id)
            except Exception as exc:
                logger.warning(f"[VIP] Fallback checkout session lookup failed: {exc}")

        if not discord_uid:
            logger.warning(
                f"[VIP] invoice.paid sem discord_user_id mapeado "
                f"(customer={customer_id} sub={sub_id})"
            )
            return

        # Busca subscription no Stripe para obter current_period_end
        try:
            sub = await _fetch_subscription(sub_id)
        except Exception as exc:
            logger.error(f"[VIP] Falha ao buscar subscription {sub_id}: {exc}")
            raise  # propagates → undoes claim → Stripe resends

        period_end = _get_period_end(sub)

        await _set_active(discord_uid, customer_id, sub_id, period_end)
        await self._grant_vip(discord_uid, until=period_end)
        logger.info(
            f"[VIP] VIP concedido: user={discord_uid} "
            f"sub={sub_id} until {period_end.isoformat()}"
        )

    async def _on_subscription_updated(self, sub: dict) -> None:
        """
        customer.subscription.updated

        Revocation rules respecting the paid period:

          active / trialing
              → updates expiration (auto-renewal confirmed)

          past_due
              → keeps VIP; user has a chance to pay the overdue invoice

          canceled / unpaid / incomplete_expired:
              + cancel_at_period_end=True → keeps VIP until period_end
                (subscription.deleted will arrive later, at the right time)
              + now >= period_end         → revoga imediatamente
              + now < period_end          → keeps until end of paid period

          Any other status → just log (no action)
        """
        customer_id   = sub.get("customer")
        sub_id        = sub.get("id")
        status        = sub.get("status", "")
        cancel_at_end = bool(sub.get("cancel_at_period_end", False))
        try:
            period_end = _get_period_end(sub)
        except KeyError:
            period_end = datetime.now(tz=timezone.utc)

        discord_uid = await _find_uid_by_customer(customer_id)
        if not discord_uid:
            discord_uid = await _find_uid_by_sub(sub_id)
        if not discord_uid:
            logger.debug(
                f"[VIP] subscription.updated sem user mapeado (sub={sub_id})"
            )
            return

        GOOD  = {"active", "trialing"}
        GRACE = {"past_due"}
        BAD   = {"canceled", "unpaid", "incomplete_expired"}

        if status in GOOD:
            await _set_active(discord_uid, customer_id, sub_id, period_end)
            logger.info(
                f"[VIP] Sub {sub_id} status={status} → "
                f"expiry atualizada para {period_end.isoformat()}"
            )

        elif status in GRACE:
            remaining_h = max(
                0, int((period_end - datetime.now(tz=timezone.utc)).total_seconds() / 3600)
            )
            logger.info(
                f"[VIP] Sub {sub_id} status={status} — "
                f"mantendo VIP (~{remaining_h}h restantes)"
            )

        elif status in BAD:
            if cancel_at_end:
                # User cancelled with access until the end of the paid period.
                # Stripe only fires subscription.deleted after period_end.
                logger.info(
                    f"[VIP] Sub {sub_id} status={status} cancel_at_period_end=True "
                    f"— aguardando period_end {period_end.isoformat()}"
                )
                return

            now = datetime.now(tz=timezone.utc)
            if now >= period_end:
                logger.info(
                    f"[VIP] Sub {sub_id} status={status} and period expired → revoking"
                )
                await self._revoke_vip(discord_uid, reason=f"status={status}")
            else:
                remaining_h = int((period_end - now).total_seconds() / 3600)
                logger.info(
                    f"[VIP] Sub {sub_id} status={status} but period still valid "
                    f"(~{remaining_h}h) → mantendo VIP"
                )
        else:
            logger.debug(f"[VIP] Sub {sub_id} status desconhecido: {status}")

    async def _on_subscription_deleted(self, sub: dict) -> None:
        """
        customer.subscription.deleted

        Comportamento garantido pelo Stripe:
          • cancel_at_period_end=True  → evento disparado SOMENTE after period_end
          • Cancelamento imediato       → evento disparado imediatamente
        In both cases, revoking now is the correct and safe action.
        """
        customer_id = sub.get("customer")
        sub_id      = sub.get("id")

        discord_uid = await _find_uid_by_customer(customer_id)
        if not discord_uid:
            discord_uid = await _find_uid_by_sub(sub_id)
        if not discord_uid:
            logger.warning(
                f"[VIP] subscription.deleted sem user mapeado (sub={sub_id})"
            )
            return

        logger.info(
            f"[VIP] Sub {sub_id} encerrada → revogando VIP user={discord_uid}"
        )
        await self._revoke_vip(discord_uid, reason="subscription.deleted")

    # ── VIP grant / revoke ─────────────────────────────────────────────────────

    async def _grant_vip(
        self, discord_uid: int, *, until: Optional[datetime] = None
    ) -> None:
        """Adiciona o cargo VIP ao membro no Discord."""
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            logger.error(f"[VIP] Guild {GUILD_ID} not found no cache")
            return

        role = guild.get_role(VIP_ROLE_ID)
        if not role:
            logger.error(f"[VIP] Cargo VIP {VIP_ROLE_ID} not found")
            return

        member = guild.get_member(discord_uid)
        if member is None:
            try:
                member = await guild.fetch_member(discord_uid)
            except discord.NotFound:
                logger.warning(
                    f"[VIP] Member {discord_uid} is not in the guild — "
                    "VIP saved in DB but role not applied"
                )
                return
            except discord.Forbidden:
                logger.error(f"[VIP] No permission to fetch member {discord_uid}")
                return

        if role in member.roles:
            logger.debug(f"[VIP] {member} already has the VIP role — grant skipped")
            return

        try:
            await member.add_roles(role, reason="VIP Stripe — pagamento confirmado")
            exp_str = until.strftime("%d/%m/%Y") if until else "indefinido"
            logger.info(f"[VIP] VIP role granted to {member} (until {exp_str})")
        except discord.Forbidden:
            logger.error(f"[VIP] No permission to add role to {member}")
        except discord.HTTPException as exc:
            logger.error(f"[VIP] HTTP erro ao adicionar cargo a {member}: {exc}")

    async def _revoke_vip(self, discord_uid: int, *, reason: str = "VIP expirado") -> None:
        """
        Revokes VIP in three steps:
          1. Removes player skins via steamid (isolated failure — does not block)
          2. Atualiza DB → vip_active=0
          3. Remove cargo VIP no Discord
        """
        # 1) Remover skins (falha silenciosa)
        await self._remove_skins(discord_uid)

        # 2) Atualizar DB
        await _set_inactive(discord_uid)

        # 3) Remover cargo Discord
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            logger.error(f"[VIP] Guild {GUILD_ID} not found")
            return

        role = guild.get_role(VIP_ROLE_ID)
        if not role:
            logger.error(f"[VIP] Cargo VIP {VIP_ROLE_ID} not found")
            return

        member = guild.get_member(discord_uid)
        if member is None:
            try:
                member = await guild.fetch_member(discord_uid)
            except discord.NotFound:
                logger.warning(
                    f"[VIP] Member {discord_uid} is not in the guild — "
                    "DB updated but role not removed"
                )
                return
            except discord.Forbidden:
                logger.error(f"[VIP] No permission to fetch member {discord_uid}")
                return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason=f"VIP revogado: {reason}")
                logger.info(f"[VIP] Cargo VIP removido de {member} ({reason})")
            else:
                logger.debug(f"[VIP] {member} did not have the VIP role — nothing to remove")
        except discord.Forbidden:
            logger.error(f"[VIP] No permission to remove role from {member}")
        except discord.HTTPException as exc:
            logger.error(f"[VIP] HTTP erro ao remover cargo de {member}: {exc}")

    async def _remove_skins(self, discord_uid: int) -> None:
        """
        1. Busca steamid64 em players.
        2. Deleta todas as skins do player em todas as tabelas wp_player_*.
        Silent failure with log — does not interrupt VIP revocation.
        """
        steamid: Optional[str] = None
        try:
            steamid = await _get_steamid(discord_uid)
        except Exception as exc:
            logger.error(f"[VIP] Erro ao buscar steamid de {discord_uid}: {exc}")
            return

        if not steamid:
            logger.info(
                f"[VIP] No steamid for user {discord_uid} — skins not removed"
            )
            return

        _SKIN_TABLES = [
            "wp_player_skins",
            "wp_player_knife",
            "wp_player_agents",
            "wp_player_gloves",
            "wp_player_music",
            "wp_player_pins",
        ]
        total = 0
        for table in _SKIN_TABLES:
            try:
                deleted = await db.execute(
                    f"DELETE FROM {table} WHERE steamid = %s", (steamid,)
                )
                total += deleted or 0
            except Exception as exc:
                logger.error(f"[VIP] Erro ao remover skins de {steamid} em {table}: {exc}")
        logger.info(f"[VIP] Skins removidas: steamid={steamid} ({total} linha(s) no total)")

    # ── Slash commands ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="vip",
        description="Adquira ou gerencie o seu plano VIP!",
    )
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def vip_cmd(self, interaction: discord.Interaction) -> None:
        """
        Sem VIP ativo   → mostra todos os planos para assinar.
        With active VIP   → detecta plano atual e mostra options de upgrade/downgrade.
        Upgrade/downgrade → modifica assinatura via API Stripe (proration imediata).
        """
        await interaction.response.defer(ephemeral=True)

        if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
            await interaction.followup.send(
                "❌ VIP system not configured. Contact an administrator.",
                ephemeral=True,
            )
            return

        record      = await _get_record(interaction.user.id)
        discord_uid = interaction.user.id

        # ── Detecta plano atual (apenas para VIPs com sub Stripe activa) ──────
        current_plan:   Optional[str] = None
        existing_sub_id: Optional[str] = None
        sub_item_id:    Optional[str] = None

        if record and record.get("vip_active") and record.get("stripe_subscription_id"):
            existing_sub_id = record["stripe_subscription_id"]
            try:
                _sub   = await _fetch_subscription(existing_sub_id)
                _items = _sub["items"]["data"]
                if _items:
                    current_plan = _get_plan_name(_items[0]["price"]["id"])
                    sub_item_id  = _items[0]["id"]
            except Exception as exc:
                logger.warning(f"[VIP] Could not detect current plan: {exc}")

        # ── Tabelas de planos ─────────────────────────────────────────────────
        _PRICE_MAP = {
            "monthly":    STRIPE_PRICE_ID,
            "semiannual": STRIPE_PRICE_ID_SEMESTRAL,
            "annual":     STRIPE_PRICE_ID_ANUAL,
        }
        _PLAN_INFO = [
            ("monthly",    "Monthly",    "€5,00 / month",      "📅"),
            ("semiannual", "Semiannual", "€25,00 / 6 months", "💰"),
            ("annual",     "Annual",     "€45,00 / year",      "🏆"),
        ]
        _PLAN_ORDER = {"monthly": 0, "semiannual": 1, "annual": 2}
        current_order = _PLAN_ORDER.get(current_plan, -1)

        # ── Build Select options (exclude current plan; mark upgrade/downgrade) ──
        options: list[discord.SelectOption] = []
        for plan_id, plan_label, plan_desc, emoji in _PLAN_INFO:
            if not _PRICE_MAP.get(plan_id):
                continue  # price not configured
            if plan_id == current_plan:
                continue  # current plan — omit from select

            if current_plan:
                if _PLAN_ORDER[plan_id] > current_order:
                    label = f"⬆️ Upgrade → {plan_label}"
                    desc  = f"{plan_desc} · upgrade from current plan"
                else:
                    label = f"⬇️ Downgrade → {plan_label}"
                    desc  = f"{plan_desc} · downgrade from current plan"
            else:
                label = plan_label
                desc  = plan_desc

            options.append(discord.SelectOption(
                label=label, value=plan_id, description=desc, emoji=emoji,
            ))

        if not options:
            await interaction.followup.send(
                "❌ No plan available to select.", ephemeral=True
            )
            return

        # ── Embed ─────────────────────────────────────────────────────────────
        expires = record.get("vip_expires_at") if record else None
        exp_str = expires.strftime("%d/%m/%Y") if isinstance(expires, datetime) else "N/A"

        if current_plan:
            embed = discord.Embed(
                title="⭐ Change VIP Plan",
                description=(
                    f"Current plan: **{current_plan.capitalize()}** (active until **{exp_str}**)\n\n"
                    "Choose below to **upgrade** or **downgrade**.\n"
                    "The prorated difference is calculated automatically by Stripe.\n"
                    "The new plan is only applied after payment confirmation."
                ),
                color=discord.Color.gold(),
            )
        else:
            embed = discord.Embed(
                title="⭐ Subscribe to VIP",
                description=(
                    "Choose the plan you prefer below.\n"
                    "The VIP role is applied automatically after payment."
                ),
                color=discord.Color.gold(),
            )

        for plan_id, plan_label, plan_desc, emoji in _PLAN_INFO:
            if not _PRICE_MAP.get(plan_id):
                continue
            suffix = " ✅ current" if plan_id == current_plan else ""
            embed.add_field(
                name=f"{emoji} {plan_label}{suffix}",
                value=plan_desc,
                inline=True,
            )

        # ── Inner Select UI ───────────────────────────────────────────────────
        class PlanSelect(discord.ui.Select):
            def __init__(self_inner):
                super().__init__(
                    placeholder="Choose your plan...",
                    options=options,
                    min_values=1,
                    max_values=1,
                )

            async def callback(self_inner, sel: discord.Interaction):
                await sel.response.defer(ephemeral=True)
                plan     = self_inner.values[0]
                price_id = _PRICE_MAP.get(plan, STRIPE_PRICE_ID)
                plan_labels = {
                    "monthly":    "Monthly — €5,00/month",
                    "semiannual": "Semiannual — €25,00/6 months",
                    "annual":     "Annual — €45,00/year",
                }

                # Already has active sub → modifies plan via API (no new Checkout)
                if existing_sub_id and sub_item_id:
                    try:
                        updated_sub = await _stripe_call(
                            stripe.Subscription.modify,
                            existing_sub_id,
                            items=[{"id": sub_item_id, "price": price_id}],
                            proration_behavior="always_invoice",
                            payment_behavior="pending_if_incomplete",
                            expand=["latest_invoice"],
                        )
                        action  = (
                            "⬆️ Upgrade" if _PLAN_ORDER.get(plan, 0) > current_order
                            else "⬇️ Downgrade"
                        )
                        latest_invoice = updated_sub.get("latest_invoice") or {}
                        invoice_status = latest_invoice.get("status", "")
                        hosted_invoice_url = latest_invoice.get("hosted_invoice_url")
                        invoice_paid = bool(latest_invoice.get("paid")) or invoice_status == "paid"
                        pending_update = updated_sub.get("pending_update")

                        if pending_update and not invoice_paid:
                            msg = (
                                f"⏳ **{action}** to **{plan_labels[plan]}** created, "
                                "but it is still **pending payment**.\n"
                                f"The current plan remains active until Stripe confirms the charge."
                            )
                            if hosted_invoice_url:
                                msg += f"\n[**Pay adjustment now →**]({hosted_invoice_url})"
                            else:
                                msg += (
                                    "\nOpen the Stripe portal at `/vipcancelar` to complete "
                                    "or check the pending charge."
                                )
                            await sel.followup.send(msg, ephemeral=True)
                            logger.info(
                                f"[VIP] Change pending payment: user={discord_uid} "
                                f"{current_plan}→{plan} sub={existing_sub_id} "
                                f"invoice_status={invoice_status}"
                            )
                            return

                        updated_sub = await _fetch_subscription(existing_sub_id)
                        period_end  = _get_period_end(updated_sub)
                        cust_id     = (record.get("stripe_customer_id") or "") if record else ""
                        await _set_active(discord_uid, cust_id, existing_sub_id, period_end)
                        exp_new = period_end.strftime("%d/%m/%Y")
                        await sel.followup.send(
                            f"✅ **{action}** to **{plan_labels[plan]}** completed!\n"
                            f"VIP active until **{exp_new}**.\n"
                            "The change was applied after payment confirmation.",
                            ephemeral=True,
                        )
                        logger.info(
                            f"[VIP] Plan changed with confirmed payment: user={discord_uid} "
                            f"{current_plan}→{plan} sub={existing_sub_id}"
                        )
                    except stripe.StripeError as exc:
                        logger.error(
                            f"[VIP] Error modifying subscription {existing_sub_id}: {exc}"
                        )
                        await sel.followup.send(
                            "❌ Error changing plan. Try again or use "
                            "`/vipcancelar` to access the Stripe portal.",
                            ephemeral=True,
                        )
                    return

                # Sem sub activa → novo Checkout Session
                try:
                    session = await _stripe_call(
                        stripe.checkout.Session.create,
                        mode="subscription",
                        line_items=[{"price": price_id, "quantity": 1}],
                        metadata={"discord_user_id": str(discord_uid)},
                        client_reference_id=str(discord_uid),
                        success_url=(
                            f"{PUBLIC_BASE_URL}/vip/sucesso"
                            "?session_id={CHECKOUT_SESSION_ID}"
                        ),
                        cancel_url=f"{PUBLIC_BASE_URL}/vip/cancelado",
                        allow_promotion_codes=True,
                    )
                except stripe.StripeError as exc:
                    logger.error(f"[VIP] Error creating Checkout Session: {exc}")
                    await sel.followup.send(
                        "❌ Error generating payment link. Try again shortly.",
                        ephemeral=True,
                    )
                    return

                result_embed = discord.Embed(
                    title=f"⭐ VIP {plan.capitalize()}",
                    description=(
                        f"Plano selecionado: **{plan_labels[plan]}**\n\n"
                        "Clique no link abaixo para concluir o pagamento.\n"
                        "The VIP role will be applied automatically after confirmation."
                    ),
                    color=discord.Color.gold(),
                )
                result_embed.add_field(
                    name="Link de Pagamento",
                    value=f"[**Assinar VIP agora →**]({session.url})",
                    inline=False,
                )
                result_embed.set_footer(
                    text="The link expires in 24h • Payment securely processed by Stripe"
                )
                await sel.followup.send(embed=result_embed, ephemeral=True)

        view = discord.ui.View(timeout=120)
        view.add_item(PlanSelect())
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="vipcancel",
        description="Manage or cancel your VIP subscription.",
    )
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def vipcancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        record = await _get_record(interaction.user.id)
        customer_id = record.get("stripe_customer_id") if record else None

        if not customer_id:
            await interaction.followup.send(
                "❌ Nenhuma assinatura Stripe encontrada para a sua conta.",
                ephemeral=True,
            )
            return

        try:
            portal = await _stripe_call(
                stripe.billing_portal.Session.create,
                customer=customer_id,
                return_url=PUBLIC_BASE_URL,
            )
        except stripe.StripeError as exc:
            logger.error(f"[VIP] Erro ao criar portal session: {exc}")
            await interaction.followup.send(
                "❌ Error generating management link. Try again.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="VIP Subscription Management",
            description=(
                "Click the link below to manage or cancel your subscription.\n\n"
                "If you cancel, VIP remains active until the end of the already paid period."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Stripe Portal",
            value=f"[**Manage subscription →**]({portal.url})",
            inline=False,
        )
        embed.set_footer(text="The link expires in 5 minutes")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @vipcancel.error
    async def vipcancel_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Wait **{error.retry_after:.0f}s** before using /vipcancel again.",
                ephemeral=True,
            )

    @vip_cmd.error
    async def vip_cmd_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Aguarde **{error.retry_after:.0f}s** antes de usar /vip novamente.",
                ephemeral=True,
            )

    @app_commands.command(
        name="vipstatus",
        description="Checks the VIP status of a user.",
    )
    async def vipstatus(
        self, interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        record = await _get_record(membro.id)
        if not record:
            await interaction.followup.send(
                f"❌ No VIP record found for {membro.mention}.",
                ephemeral=True,
            )
            return

        active  = bool(record.get("vip_active"))
        expires = record.get("vip_expires_at")  # naive UTC datetime from MySQL

        if expires and isinstance(expires, datetime):
            exp_str = expires.strftime("%d/%m/%Y %H:%M UTC")
            if datetime.utcnow() > expires and active:
                exp_str += " ⚠️ (expired locally — awaiting revocation)"
        else:
            exp_str = "N/A"

        embed = discord.Embed(
            title=f"VIP Status — {membro.display_name}",
            color=discord.Color.green() if active else discord.Color.red(),
        )
        embed.set_thumbnail(url=membro.display_avatar.url)
        embed.add_field(name="Active",     value="✅ Yes" if active else "❌ No", inline=True)
        embed.add_field(name="Expires in", value=exp_str, inline=True)
        embed.add_field(name="\u200b",    value="\u200b", inline=True)
        embed.add_field(
            name="Customer ID",
            value=f"`{record.get('stripe_customer_id') or 'N/A'}`",
            inline=False,
        )
        embed.add_field(
            name="Subscription ID",
            value=f"`{record.get('stripe_subscription_id') or 'N/A'}`",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="vipmanual",
        description="Grants manual VIP without Stripe link (days to set).",
    )
    async def vipmanual(
        self,
        interaction: discord.Interaction,
        membro: discord.Member,
        dias: int = 30,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        from datetime import timedelta
        period_end = datetime.now(tz=timezone.utc) + timedelta(days=dias)
        try:
            await _set_active(membro.id, "", "", period_end)
            await self._grant_vip(membro.id, until=period_end)
            exp_str = period_end.strftime("%d/%m/%Y %H:%M UTC")
            await interaction.followup.send(
                f"✅ Manual VIP granted to {membro.mention} for **{dias} days** (until **{exp_str}**).",
                ephemeral=True,
            )
            logger.info(
                f"[VIP] Manual VIP by {interaction.user} → user={membro.id} days={dias} until {period_end.isoformat()}"
            )
        except Exception as exc:
            logger.error(f"[VIP] Error in /vipmanual: {exc}")
            await interaction.followup.send(f"❌ Error: {exc}", ephemeral=True)

    @app_commands.command(
        name="vipgrant",
        description="Manually grants VIP to a user (payment already confirmed).",
    )
    async def vipgrant(
        self, interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        record = await _get_record(membro.id)
        if not record:
            await interaction.followup.send(
                f"❌ No Stripe link found for {membro.mention}.\n"
                "The user must have used /vip first.",
                ephemeral=True,
            )
            return

        sub_id      = record.get("stripe_subscription_id")
        customer_id = record.get("stripe_customer_id")

        if not sub_id:
            await interaction.followup.send(
                f"❌ No subscription_id in DB for {membro.mention}.",
                ephemeral=True,
            )
            return

        try:
            sub = await _fetch_subscription(sub_id)
            period_end = _get_period_end(sub)
            await _set_active(membro.id, customer_id or "", sub_id, period_end)
            await self._grant_vip(membro.id, until=period_end)
            exp_str = period_end.strftime("%d/%m/%Y %H:%M UTC")
            await interaction.followup.send(
                f"✅ VIP manually granted to {membro.mention} until **{exp_str}**.",
                ephemeral=True,
            )
            logger.info(
                f"[VIP] VIP manually granted by {interaction.user} "
                f"→ user={membro.id} sub={sub_id} until {period_end.isoformat()}"
            )
        except Exception as exc:
            logger.error(f"[VIP] Error in /vipgrant: {exc}")
            await interaction.followup.send(
                f"❌ Error granting VIP: {exc}",
                ephemeral=True,
            )

    @app_commands.command(
        name="viprevoke",
        description="Manually revokes a user's VIP.",
    )
    async def viprevoke(
        self, interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        record = await _get_record(membro.id)

        if not record or not record.get("vip_active"):
            await interaction.response.send_message(
                f"ℹ️ {membro.mention} does not have an active VIP at the moment.",
                ephemeral=True,
            )
            return

        view = RevokeConfirmView(membro.id, self)
        await interaction.response.send_message(
            f"⚠️ **Confirm revocation of VIP for {membro.mention}?**\n\n"
            "This action will:\n"
            "• Remove the VIP role from the user\n"
            "• **Delete all skins** linked to the player's SteamID\n\n"
            "_The Stripe subscription will **not** be cancelled automatically. "
            "Do it manually in the Stripe Dashboard if needed._",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="vipclearskins",
        description="Removes skins from all players without active VIP.",
    )
    async def vipclearskins(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            rows = await db.fetchall(
                """
                SELECT DISTINCT s.steamid
                FROM (
                    SELECT steamid FROM wp_player_skins
                    UNION
                    SELECT steamid FROM wp_player_knife
                    UNION
                    SELECT steamid FROM wp_player_agents
                    UNION
                    SELECT steamid FROM wp_player_gloves
                    UNION
                    SELECT steamid FROM wp_player_music
                    UNION
                    SELECT steamid FROM wp_player_pins
                ) s
                LEFT JOIN players pl ON pl.steamid64 COLLATE utf8mb4_unicode_ci = s.steamid
                LEFT JOIN vip_subscriptions vs
                       ON vs.discord_user_id = pl.discord_id
                      AND vs.vip_active = 1
                WHERE vs.discord_user_id IS NULL
                """
            )
        except Exception as exc:
            logger.error(f"[VIP] Error in /vipclearskins on query: {exc}")
            await interaction.followup.send(f"❌ Error querying database: {exc}", ephemeral=True)
            return

        if not rows:
            await interaction.followup.send(
                "✅ No non-VIP players have skins. Nothing to clean.",
                ephemeral=True,
            )
            return

        steamids = [row["steamid"] for row in rows]
        view = LimparSkinsConfirmView(self, steamids)
        await interaction.followup.send(
            f"⚠️ **Non-VIP skin cleanup**\n\n"
            f"Found **{len(steamids)}** player(s) without active VIP with skins in the database.\n\n"
            "Do you want to remove **all** skins from these players?",
            view=view,
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Setup — called by bot.load_extension()
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VipStripeCog(bot))


# ══════════════════════════════════════════════════════════════════════════════
# HOW TO LOAD THE COG IN main.py
# ══════════════════════════════════════════════════════════════════════════════
#
# Add "bot.cogs.vip_stripe" to the cog list in main.py:
#
#   cogs = [
#       "bot.cogs.steam",
#       "bot.cogs.admin",
#       ...
#       "bot.cogs.vip_stripe",   # <-- add here
#   ]
#
# EXPOSING THE WEBHOOK VIA NGINX (VPS)
# The bot spins up an internal aiohttp server on WEBHOOK_PORT (default: 8765).
# nginx is already running on the VPS for the site — just add a location
# block in your domain's server block to proxy to that internal port:
#
#   # Inside your domain's server block:
#   location /stripe/webhook {
#       proxy_pass         http://127.0.0.1:8765;
#       proxy_http_version 1.1;
#       proxy_set_header   Host $host;
#       proxy_set_header   X-Real-IP $remote_addr;
#       # Stripe sends payloads up to ~64 KB; 1m is enough
#       client_max_body_size 1m;
#   }
#
# URL to register in Stripe Dashboard → Developers → Webhooks:
#   https://yourdomain.com/stripe/webhook
#
# REQUIRED ENVIRONMENT VARIABLES (.env):
#   STRIPE_SECRET_KEY=sk_live_...
#   STRIPE_WEBHOOK_SECRET=whsec_...
#   STRIPE_PRICE_ID=price_...
#   GUILD_ID=123456789
#   VIP_ROLE_ID=987654321
#   PUBLIC_BASE_URL=https://yourdomain.com
#   WEBHOOK_HOST=127.0.0.1   # local bind only, nginx does the proxy
#   WEBHOOK_PORT=8765
