"""
bot/cogs/vip_stripe.py
======================
Cog de VIP mensal via Stripe Subscriptions — discord.py 2.x

STACK: MySQL (aiomysql via bot.database.db), loguru, aiohttp.web, discord.ext.tasks

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TABELAS CRIADAS AUTOMATICAMENTE (cog_load)
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

TABELAS JÁ EXISTENTES (necessárias):
    players        (discord_id BIGINT, steamid64 VARCHAR)
    wp_player_skins (steamid VARCHAR, ...)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VARIÁVEIS DE AMBIENTE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  STRIPE_SECRET_KEY       sk_live_... ou sk_test_...
  STRIPE_WEBHOOK_SECRET   whsec_...
  STRIPE_PRICE_ID         price_...  (plano mensal)
  GUILD_ID                ID do servidor Discord
  VIP_ROLE_ID             ID do cargo VIP
  PUBLIC_BASE_URL         https://seusite.com  (sem barra final)
  WEBHOOK_HOST            0.0.0.0  (padrão)
  WEBHOOK_PORT            8765     (porta diferente da porta Render/main)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP RÁPIDO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Instalar dependências:
      pip install stripe>=5.0.0

2. Stripe Dashboard → Catálogo de produtos:
   • Criar produto → Adicionar price recorrente (mensal)
   • Copiar price_xxx → STRIPE_PRICE_ID no .env

3. Stripe Dashboard → Developers → Webhooks → Add endpoint:
   • URL: https://seusite.com/stripe/webhook
     (ou exponha WEBHOOK_PORT via nginx proxy_pass / Cloudflare Tunnel)
   • Eventos a selecionar:
       checkout.session.completed
       invoice.paid
       invoice.payment_succeeded
       customer.subscription.updated
       customer.subscription.deleted
   • Copiar whsec_xxx → STRIPE_WEBHOOK_SECRET no .env

4. Nginx na VPS — adicionar dentro do server block do domínio:
      location /stripe/webhook {
          proxy_pass         http://127.0.0.1:8765;
          proxy_http_version 1.1;
          proxy_set_header   Host $host;
          proxy_set_header   X-Real-IP $remote_addr;
          client_max_body_size 1m;
      }
   Depois: nginx -t && systemctl reload nginx

5. Stripe Dashboard → Developers → Webhooks → Add endpoint:
      https://seudominio.com/stripe/webhook

6. Adicionar a cog em main.py (veja o final deste arquivo).

7. Testar com Stripe CLI (opcional, dev local):
      stripe listen --forward-to localhost:8765/stripe/webhook
      stripe trigger invoice.paid

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOBRE cancel_at_period_end (cancelamento ao fim do período)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Quando o usuário cancela mas escolhe manter acesso até o fim do
período pago, o Stripe define cancel_at_period_end=True:
  • subscription.updated   → cancel_at_period_end=True → mantemos VIP
  • subscription.deleted   → só dispara APÓS o period_end expirar
Em ambos os casos revogar em subscription.deleted é correto e seguro.
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
# Configuração via ENV
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
# Stripe helpers — wraps SDK síncrono em executor para não bloquear o event loop
# ══════════════════════════════════════════════════════════════════════════════

async def _stripe_call(fn, *args, **kwargs):
    """Executa chamada Stripe síncrona em thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


def _get_period_end(sub) -> datetime:
    """
    Extrai current_period_end de um objeto Subscription do Stripe.
    Compatível com API clássica e API ≥2025-12-15.clover (campo movido).
    """
    # Caminho clássico
    ts = None
    try:
        ts = sub["current_period_end"]
    except (KeyError, TypeError):
        pass

    # API ≥2025-12-15.clover: pode estar nos items
    if not ts:
        try:
            items_data = sub["items"]["data"]
            if items_data:
                ts = items_data[0]["current_period_end"]
        except (KeyError, TypeError, IndexError):
            pass

    # Fallback: billing_cycle_anchor + 31 dias (último recurso)
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
            f"current_period_end não encontrado na subscription. "
            f"Campos disponíveis: {keys}"
        )

    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


async def _fetch_subscription(sub_id: str, retries: int = 3) -> stripe.Subscription:
    """Busca subscription no Stripe com retry + backoff exponencial."""
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
    """Cria as tabelas VIP se não existirem (idempotente)."""
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
    logger.info("[VIP] Tabelas VIP verificadas/criadas")


async def _try_claim_event(event_id: str) -> bool:
    """
    Tenta reivindicar um event_id atomicamente via INSERT IGNORE.
    Retorna True se este processo foi o primeiro (deve processar).
    Retorna False se já existia (duplicado — ignorar).
    """
    rows = await db.execute(
        "INSERT IGNORE INTO stripe_events_processed (event_id) VALUES (%s)",
        (event_id,),
    )
    return rows > 0


async def _unclaim_event(event_id: str) -> None:
    """Reverte claim de evento que falhou para permitir retry do Stripe."""
    await db.execute(
        "DELETE FROM stripe_events_processed WHERE event_id = %s",
        (event_id,),
    )


async def _upsert_link(discord_uid: int, customer_id: str, sub_id: str) -> None:
    """Salva/atualiza vínculo discord_user_id ↔ customer/subscription."""
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
    """Ativa VIP no DB com data de expiração."""
    # MySQL não guarda timezone — armazena como UTC naive
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
    VIPs marcados como ativos no DB mas com vip_expires_at já passado.
    Usado pelo expiry check para recovery após restart do bot.
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
    """Retorna nome do plano ('mensal'/'semestral'/'anual') a partir do price_id configurado."""
    if price_id and price_id == STRIPE_PRICE_ID:
        return "mensal"
    if price_id and price_id == STRIPE_PRICE_ID_SEMESTRAL:
        return "semestral"
    if price_id and price_id == STRIPE_PRICE_ID_ANUAL:
        return "anual"
    return None


async def _get_steamid(discord_uid: int) -> Optional[str]:
    row = await db.fetchone(
        "SELECT steamid64 FROM players WHERE discord_id = %s",
        (discord_uid,),
    )
    return row["steamid64"] if row else None


# ══════════════════════════════════════════════════════════════════════════════
# UI — Confirmação de revogação manual
# ══════════════════════════════════════════════════════════════════════════════

class RevokeConfirmView(discord.ui.View):
    def __init__(self, target_id: int, cog: "VipStripeCog"):
        super().__init__(timeout=30)
        self.target_id = target_id
        self.cog = cog

    @discord.ui.button(label="Confirmar Revogação", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(content="⏳ Revogando VIP...", view=self)
        await self.cog._revoke_vip(self.target_id, reason="revogado manualmente por admin")
        await interaction.followup.send(
            f"✅ VIP do usuário `{self.target_id}` revogado com sucesso.",
            ephemeral=True,
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Operação cancelada.", view=None)


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
                logger.error(f"[VIP] Erro em /viplimparskins ao deletar de {table}: {exc}")
                errors.append(table)
        logger.info(
            f"[VIP] viplimparskins: {total} linha(s) removidas de {len(self.steamids)} steamid(s) não-VIP"
        )
        if errors:
            await interaction.followup.send(
                f"⚠️ Limpeza parcial: **{total}** linha(s) removidas de **{len(self.steamids)}** jogador(es).\n"
                f"Erros nas tabelas: {', '.join(errors)}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ Limpeza concluída! **{total}** linha(s) removidas de **{len(self.steamids)}** jogador(es) sem VIP.",
                ephemeral=True,
            )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Operação cancelada.", view=None)


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
            logger.warning("[VIP] STRIPE_WEBHOOK_SECRET ausente — webhooks sem validação de assinatura")

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
        Executa uma vez por dia às 00:01 UTC.
        """
        try:
            rows = await _get_expired_active()
            for row in rows:
                uid = int(row["discord_user_id"])
                logger.info(f"[VIP] Expiry check → revogando VIP expirado user={uid}")
                await self._revoke_vip(uid, reason="expirado (expiry check)")
        except Exception as exc:
            logger.error(f"[VIP] Erro no expiry check: {exc}")

    @_expiry_check.before_loop
    async def _before_expiry_check(self) -> None:
        await self.bot.wait_until_ready()

    # ── Servidor HTTP para webhooks Stripe ────────────────────────────────────

    async def _start_webhook_server(self) -> None:
        app = web.Application(client_max_size=1 * 1024 * 1024)  # 1 MB máx
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
        # Lê payload bruto — obrigatório para validação de assinatura
        try:
            payload = await request.read()
        except Exception as exc:
            logger.error(f"[VIP] Falha ao ler payload: {exc}")
            return web.Response(status=400, text="bad request")

        sig_header = request.headers.get("Stripe-Signature", "")

        # 1) Valida assinatura (HMAC síncrono — sem I/O, não bloqueia)
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except stripe.SignatureVerificationError:
            logger.warning("[VIP] Assinatura Stripe inválida — descartando")
            return web.Response(status=400, text="invalid signature")
        except Exception as exc:
            logger.error(f"[VIP] Erro ao construir evento Stripe: {exc}")
            return web.Response(status=400, text="bad payload")

        event_id   = event["id"]
        event_type = event["type"]
        logger.debug(f"[VIP] Webhook recebido: {event_type} ({event_id})")

        # 2) Idempotência atômica — INSERT IGNORE garante "first-write-wins"
        claimed = await _try_claim_event(event_id)
        if not claimed:
            logger.info(f"[VIP] Evento {event_id} já processado — ignorando")
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
                logger.debug(f"[VIP] Evento ignorado (não tratado): {event_type}")
        except Exception as exc:
            logger.exception(
                f"[VIP] Erro ao processar {event_type} ({event_id}): {exc}"
            )
            await _unclaim_event(event_id)  # permite retry pelo Stripe
            return web.Response(status=500, text="internal error")

        return web.Response(status=200, text="ok")

    # ── Handlers de eventos Stripe ─────────────────────────────────────────────

    async def _on_checkout_completed(self, session: dict) -> None:
        """
        checkout.session.completed

        Salva o vínculo user ↔ customer/subscription.
        Se payment_status == "paid" (cartão), concede VIP imediatamente.
        Caso contrário (SEPA/boleto), aguarda invoice.paid.
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
                f"[VIP] Vínculo salvo: user={discord_uid} "
                f"customer={customer_id} sub={sub_id}"
            )

            # Pagamento confirmado (cartão) → concede VIP agora sem esperar invoice.paid
            if payment_status == "paid":
                try:
                    sub = await _fetch_subscription(sub_id)
                    period_end = _get_period_end(sub)
                    await _set_active(discord_uid, customer_id, sub_id, period_end)
                    await self._grant_vip(discord_uid, until=period_end)
                    logger.info(
                        f"[VIP] VIP concedido via checkout: user={discord_uid} "
                        f"sub={sub_id} até {period_end.isoformat()}"
                    )
                except Exception as exc:
                    logger.error(f"[VIP] Erro ao conceder VIP no checkout: {exc}")
                    raise  # propaga → Stripe reenvía

    async def _on_invoice_paid(self, invoice: dict) -> None:
        """
        invoice.paid / invoice.payment_succeeded

        Concede/renova VIP pelo período pago.
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
                logger.warning(f"[VIP] Fallback checkout session lookup falhou: {exc}")

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
            raise  # propaga → desfaz claim → Stripe reenvía

        period_end = _get_period_end(sub)

        await _set_active(discord_uid, customer_id, sub_id, period_end)
        await self._grant_vip(discord_uid, until=period_end)
        logger.info(
            f"[VIP] VIP concedido: user={discord_uid} "
            f"sub={sub_id} até {period_end.isoformat()}"
        )

    async def _on_subscription_updated(self, sub: dict) -> None:
        """
        customer.subscription.updated

        Regras de revogação respeitando o período pago:

          active / trialing
              → atualiza expiração (renovação automática confirmada)

          past_due
              → mantém VIP; usuário tem chance de pagar a fatura atrasada

          canceled / unpaid / incomplete_expired:
              + cancel_at_period_end=True → mantém VIP até period_end
                (subscription.deleted chegará depois, no momento certo)
              + now >= period_end         → revoga imediatamente
              + now < period_end          → mantém até fim do período pago

          Qualquer outro status → apenas log (sem ação)
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
                # Usuário cancelou com acesso até o fim do período pago.
                # Stripe só dispara subscription.deleted após period_end.
                logger.info(
                    f"[VIP] Sub {sub_id} status={status} cancel_at_period_end=True "
                    f"— aguardando period_end {period_end.isoformat()}"
                )
                return

            now = datetime.now(tz=timezone.utc)
            if now >= period_end:
                logger.info(
                    f"[VIP] Sub {sub_id} status={status} e período expirado → revogando"
                )
                await self._revoke_vip(discord_uid, reason=f"status={status}")
            else:
                remaining_h = int((period_end - now).total_seconds() / 3600)
                logger.info(
                    f"[VIP] Sub {sub_id} status={status} mas período ainda válido "
                    f"(~{remaining_h}h) → mantendo VIP"
                )
        else:
            logger.debug(f"[VIP] Sub {sub_id} status desconhecido: {status}")

    async def _on_subscription_deleted(self, sub: dict) -> None:
        """
        customer.subscription.deleted

        Comportamento garantido pelo Stripe:
          • cancel_at_period_end=True  → evento disparado SOMENTE após period_end
          • Cancelamento imediato       → evento disparado imediatamente
        Em ambos os casos, revogar agora é a ação correta e segura.
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
            logger.error(f"[VIP] Guild {GUILD_ID} não encontrada no cache")
            return

        role = guild.get_role(VIP_ROLE_ID)
        if not role:
            logger.error(f"[VIP] Cargo VIP {VIP_ROLE_ID} não encontrado")
            return

        member = guild.get_member(discord_uid)
        if member is None:
            try:
                member = await guild.fetch_member(discord_uid)
            except discord.NotFound:
                logger.warning(
                    f"[VIP] Membro {discord_uid} não está na guild — "
                    "VIP salvo no DB mas cargo não aplicado"
                )
                return
            except discord.Forbidden:
                logger.error(f"[VIP] Sem permissão para buscar membro {discord_uid}")
                return

        if role in member.roles:
            logger.debug(f"[VIP] {member} já tem o cargo VIP — grant ignorado")
            return

        try:
            await member.add_roles(role, reason="VIP Stripe — pagamento confirmado")
            exp_str = until.strftime("%d/%m/%Y") if until else "indefinido"
            logger.info(f"[VIP] Cargo VIP concedido a {member} (até {exp_str})")
        except discord.Forbidden:
            logger.error(f"[VIP] Sem permissão para adicionar cargo a {member}")
        except discord.HTTPException as exc:
            logger.error(f"[VIP] HTTP erro ao adicionar cargo a {member}: {exc}")

    async def _revoke_vip(self, discord_uid: int, *, reason: str = "VIP expirado") -> None:
        """
        Revoga VIP em três passos:
          1. Remove skins do player via steamid (falha isolada — não bloqueia)
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
            logger.error(f"[VIP] Guild {GUILD_ID} não encontrada")
            return

        role = guild.get_role(VIP_ROLE_ID)
        if not role:
            logger.error(f"[VIP] Cargo VIP {VIP_ROLE_ID} não encontrado")
            return

        member = guild.get_member(discord_uid)
        if member is None:
            try:
                member = await guild.fetch_member(discord_uid)
            except discord.NotFound:
                logger.warning(
                    f"[VIP] Membro {discord_uid} não está na guild — "
                    "DB atualizado mas cargo não removido"
                )
                return
            except discord.Forbidden:
                logger.error(f"[VIP] Sem permissão para buscar membro {discord_uid}")
                return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason=f"VIP revogado: {reason}")
                logger.info(f"[VIP] Cargo VIP removido de {member} ({reason})")
            else:
                logger.debug(f"[VIP] {member} não tinha o cargo VIP — nada a remover")
        except discord.Forbidden:
            logger.error(f"[VIP] Sem permissão para remover cargo de {member}")
        except discord.HTTPException as exc:
            logger.error(f"[VIP] HTTP erro ao remover cargo de {member}: {exc}")

    async def _remove_skins(self, discord_uid: int) -> None:
        """
        1. Busca steamid64 em players.
        2. Deleta todas as skins do player em todas as tabelas wp_player_*.
        Falha silenciosa com log — não interrompe a revogação do VIP.
        """
        steamid: Optional[str] = None
        try:
            steamid = await _get_steamid(discord_uid)
        except Exception as exc:
            logger.error(f"[VIP] Erro ao buscar steamid de {discord_uid}: {exc}")
            return

        if not steamid:
            logger.info(
                f"[VIP] Sem steamid para user {discord_uid} — skins não removidas"
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
        Com VIP ativo   → detecta plano atual e mostra opções de upgrade/downgrade.
        Upgrade/downgrade → modifica assinatura via API Stripe (proration imediata).
        """
        await interaction.response.defer(ephemeral=True)

        if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
            await interaction.followup.send(
                "❌ Sistema VIP não configurado. Contate um administrador.",
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
                logger.warning(f"[VIP] Não foi possível detectar plano atual: {exc}")

        # ── Tabelas de planos ─────────────────────────────────────────────────
        _PRICE_MAP = {
            "mensal":    STRIPE_PRICE_ID,
            "semestral": STRIPE_PRICE_ID_SEMESTRAL,
            "anual":     STRIPE_PRICE_ID_ANUAL,
        }
        _PLAN_INFO = [
            ("mensal",    "Mensal",    "€5,00 / mês",      "📅"),
            ("semestral", "Semestral", "€25,00 / 6 meses", "💰"),
            ("anual",     "Anual",     "€45,00 / ano",      "🏆"),
        ]
        _PLAN_ORDER = {"mensal": 0, "semestral": 1, "anual": 2}
        current_order = _PLAN_ORDER.get(current_plan, -1)

        # ── Monta opções do Select (exclui plano atual; marca upgrade/downgrade) ──
        options: list[discord.SelectOption] = []
        for plan_id, plan_label, plan_desc, emoji in _PLAN_INFO:
            if not _PRICE_MAP.get(plan_id):
                continue  # price não configurado
            if plan_id == current_plan:
                continue  # plano actual — omite do select

            if current_plan:
                if _PLAN_ORDER[plan_id] > current_order:
                    label = f"⬆️ Upgrade → {plan_label}"
                    desc  = f"{plan_desc} · upgrade do plano actual"
                else:
                    label = f"⬇️ Downgrade → {plan_label}"
                    desc  = f"{plan_desc} · downgrade do plano actual"
            else:
                label = plan_label
                desc  = plan_desc

            options.append(discord.SelectOption(
                label=label, value=plan_id, description=desc, emoji=emoji,
            ))

        if not options:
            await interaction.followup.send(
                "❌ Nenhum plano disponível para selecionar.", ephemeral=True
            )
            return

        # ── Embed ─────────────────────────────────────────────────────────────
        expires = record.get("vip_expires_at") if record else None
        exp_str = expires.strftime("%d/%m/%Y") if isinstance(expires, datetime) else "N/A"

        if current_plan:
            embed = discord.Embed(
                title="⭐ Alterar Plano VIP",
                description=(
                    f"Plano actual: **{current_plan.capitalize()}** (ativo até **{exp_str}**)\n\n"
                    "Escolha abaixo para fazer **upgrade** ou **downgrade**.\n"
                    "A diferença proporcional é calculada automaticamente pelo Stripe.\n"
                    "O novo plano só é aplicado após a confirmação do pagamento."
                ),
                color=discord.Color.gold(),
            )
        else:
            embed = discord.Embed(
                title="⭐ Assinar VIP",
                description=(
                    "Escolha o plano que preferir abaixo.\n"
                    "O cargo VIP é aplicado automaticamente após o pagamento."
                ),
                color=discord.Color.gold(),
            )

        for plan_id, plan_label, plan_desc, emoji in _PLAN_INFO:
            if not _PRICE_MAP.get(plan_id):
                continue
            suffix = " ✅ atual" if plan_id == current_plan else ""
            embed.add_field(
                name=f"{emoji} {plan_label}{suffix}",
                value=plan_desc,
                inline=True,
            )

        # ── Inner Select UI ───────────────────────────────────────────────────
        class PlanSelect(discord.ui.Select):
            def __init__(self_inner):
                super().__init__(
                    placeholder="Escolha seu plano...",
                    options=options,
                    min_values=1,
                    max_values=1,
                )

            async def callback(self_inner, sel: discord.Interaction):
                await sel.response.defer(ephemeral=True)
                plan     = self_inner.values[0]
                price_id = _PRICE_MAP.get(plan, STRIPE_PRICE_ID)
                plan_labels = {
                    "mensal":    "Mensal — €5,00/mês",
                    "semestral": "Semestral — €25,00/6 meses",
                    "anual":     "Anual — €45,00/ano",
                }

                # Já tem sub activa → modifica plano via API (sem novo Checkout)
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
                                f"⏳ **{action}** para **{plan_labels[plan]}** criado, "
                                "mas ainda está **pendente de pagamento**.\n"
                                f"O plano atual continua ativo até o Stripe confirmar a cobrança."
                            )
                            if hosted_invoice_url:
                                msg += f"\n[**Pagar ajuste agora →**]({hosted_invoice_url})"
                            else:
                                msg += (
                                    "\nAbra o portal Stripe em `/vipcancelar` para concluir "
                                    "ou verificar a cobrança pendente."
                                )
                            await sel.followup.send(msg, ephemeral=True)
                            logger.info(
                                f"[VIP] Alteração pendente de pagamento: user={discord_uid} "
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
                            f"✅ **{action}** para **{plan_labels[plan]}** concluído!\n"
                            f"VIP ativo até **{exp_new}**.\n"
                            "A alteração só foi aplicada após a confirmação do pagamento.",
                            ephemeral=True,
                        )
                        logger.info(
                            f"[VIP] Plano alterado com pagamento confirmado: user={discord_uid} "
                            f"{current_plan}→{plan} sub={existing_sub_id}"
                        )
                    except stripe.StripeError as exc:
                        logger.error(
                            f"[VIP] Erro ao modificar subscription {existing_sub_id}: {exc}"
                        )
                        await sel.followup.send(
                            "❌ Erro ao alterar plano. Tente novamente ou use "
                            "`/vipcancelar` para aceder ao portal Stripe.",
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
                    logger.error(f"[VIP] Erro ao criar Checkout Session: {exc}")
                    await sel.followup.send(
                        "❌ Erro ao gerar link de pagamento. Tente novamente em instantes.",
                        ephemeral=True,
                    )
                    return

                result_embed = discord.Embed(
                    title=f"⭐ VIP {plan.capitalize()}",
                    description=(
                        f"Plano selecionado: **{plan_labels[plan]}**\n\n"
                        "Clique no link abaixo para concluir o pagamento.\n"
                        "O cargo VIP será aplicado automaticamente após a confirmação."
                    ),
                    color=discord.Color.gold(),
                )
                result_embed.add_field(
                    name="Link de Pagamento",
                    value=f"[**Assinar VIP agora →**]({session.url})",
                    inline=False,
                )
                result_embed.set_footer(
                    text="O link expira em 24h • Pagamento processado com segurança pelo Stripe"
                )
                await sel.followup.send(embed=result_embed, ephemeral=True)

        view = discord.ui.View(timeout=120)
        view.add_item(PlanSelect())
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="vipcancelar",
        description="Gerencie ou cancele sua assinatura VIP.",
    )
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def vipcancelar(self, interaction: discord.Interaction) -> None:
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
                "❌ Erro ao gerar link de gestão. Tente novamente.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Gestão de Assinatura VIP",
            description=(
                "Clique no link abaixo para gerir ou cancelar a sua assinatura.\n\n"
                "Se cancelar, o VIP permanece ativo até ao fim do período já pago."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Portal Stripe",
            value=f"[**Gerir assinatura →**]({portal.url})",
            inline=False,
        )
        embed.set_footer(text="O link expira em 5 minutos")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @vipcancelar.error
    async def vipcancelar_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Aguarde **{error.retry_after:.0f}s** antes de usar /vipcancelar novamente.",
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
        description="Verifica o status VIP de um usuário.",
    )
    async def vipstatus(
        self, interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        record = await _get_record(membro.id)
        if not record:
            await interaction.followup.send(
                f"❌ Nenhum registro VIP encontrado para {membro.mention}.",
                ephemeral=True,
            )
            return

        active  = bool(record.get("vip_active"))
        expires = record.get("vip_expires_at")  # datetime naive UTC vindo do MySQL

        if expires and isinstance(expires, datetime):
            exp_str = expires.strftime("%d/%m/%Y %H:%M UTC")
            if datetime.utcnow() > expires and active:
                exp_str += " ⚠️ (expirado localmente — aguardando revogação)"
        else:
            exp_str = "N/A"

        embed = discord.Embed(
            title=f"Status VIP — {membro.display_name}",
            color=discord.Color.green() if active else discord.Color.red(),
        )
        embed.set_thumbnail(url=membro.display_avatar.url)
        embed.add_field(name="Ativo",     value="✅ Sim" if active else "❌ Não", inline=True)
        embed.add_field(name="Expira em", value=exp_str, inline=True)
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
        description="Concede VIP manual sem vínculo Stripe (dias a definir).",
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
                f"✅ VIP manual concedido a {membro.mention} por **{dias} dias** (até **{exp_str}**).",
                ephemeral=True,
            )
            logger.info(
                f"[VIP] VIP manual por {interaction.user} → user={membro.id} dias={dias} até {period_end.isoformat()}"
            )
        except Exception as exc:
            logger.error(f"[VIP] Erro em /vipmanual: {exc}")
            await interaction.followup.send(f"❌ Erro: {exc}", ephemeral=True)

    @app_commands.command(
        name="vipgrant",
        description="Concede VIP manualmente a um usuário (pagamento já confirmado).",
    )
    async def vipgrant(
        self, interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        record = await _get_record(membro.id)
        if not record:
            await interaction.followup.send(
                f"❌ Nenhum vínculo Stripe encontrado para {membro.mention}.\n"
                "O usuário precisa ter usado /vip antes.",
                ephemeral=True,
            )
            return

        sub_id      = record.get("stripe_subscription_id")
        customer_id = record.get("stripe_customer_id")

        if not sub_id:
            await interaction.followup.send(
                f"❌ Sem subscription_id no DB para {membro.mention}.",
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
                f"✅ VIP concedido manualmente a {membro.mention} até **{exp_str}**.",
                ephemeral=True,
            )
            logger.info(
                f"[VIP] VIP concedido manualmente por {interaction.user} "
                f"→ user={membro.id} sub={sub_id} até {period_end.isoformat()}"
            )
        except Exception as exc:
            logger.error(f"[VIP] Erro em /vipgrant: {exc}")
            await interaction.followup.send(
                f"❌ Erro ao conceder VIP: {exc}",
                ephemeral=True,
            )

    @app_commands.command(
        name="viprevoke",
        description="Revoga o VIP de um usuário manualmente.",
    )
    async def viprevoke(
        self, interaction: discord.Interaction, membro: discord.Member
    ) -> None:
        record = await _get_record(membro.id)

        if not record or not record.get("vip_active"):
            await interaction.response.send_message(
                f"ℹ️ {membro.mention} não possui VIP ativo no momento.",
                ephemeral=True,
            )
            return

        view = RevokeConfirmView(membro.id, self)
        await interaction.response.send_message(
            f"⚠️ **Confirma a revogação do VIP de {membro.mention}?**\n\n"
            "Esta ação irá:\n"
            "• Remover o cargo VIP do usuário\n"
            "• **Deletar todas as skins** vinculadas ao SteamID do player\n\n"
            "_A assinatura no Stripe **não** será cancelada automaticamente. "
            "Faça isso manualmente no Dashboard do Stripe se necessário._",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(
        name="viplimparskins",
        description="Remove skins de todos os jogadores sem VIP ativo.",
    )
    async def viplimparskins(self, interaction: discord.Interaction) -> None:
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
            logger.error(f"[VIP] Erro em /viplimparskins ao consultar: {exc}")
            await interaction.followup.send(f"❌ Erro ao consultar banco: {exc}", ephemeral=True)
            return

        if not rows:
            await interaction.followup.send(
                "✅ Nenhum jogador sem VIP possui skins. Nada a limpar.",
                ephemeral=True,
            )
            return

        steamids = [row["steamid"] for row in rows]
        view = LimparSkinsConfirmView(self, steamids)
        await interaction.followup.send(
            f"⚠️ **Limpeza de skins de não-VIPs**\n\n"
            f"Encontrado(s) **{len(steamids)}** jogador(es) sem VIP ativo com skins no banco.\n\n"
            "Deseja remover **todas** as skins desses jogadores?",
            view=view,
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Setup — chamado por bot.load_extension()
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VipStripeCog(bot))


# ══════════════════════════════════════════════════════════════════════════════
# COMO CARREGAR A COG EM main.py
# ══════════════════════════════════════════════════════════════════════════════
#
# Adicione "bot.cogs.vip_stripe" à lista de cogs em main.py:
#
#   cogs = [
#       "bot.cogs.steam",
#       "bot.cogs.admin",
#       ...
#       "bot.cogs.vip_stripe",   # <-- adicionar aqui
#   ]
#
# EXPONDO O WEBHOOK VIA NGINX (VPS)
# O bot sobe um servidor aiohttp interno na WEBHOOK_PORT (padrão: 8765).
# O nginx já está rodando na VPS para o site — basta adicionar um location
# no bloco do seu domínio para fazer proxy para essa porta interna:
#
#   # Dentro do server block do seu domínio:
#   location /stripe/webhook {
#       proxy_pass         http://127.0.0.1:8765;
#       proxy_http_version 1.1;
#       proxy_set_header   Host $host;
#       proxy_set_header   X-Real-IP $remote_addr;
#       # Stripe envia payloads de até ~64 KB; 1m é suficiente
#       client_max_body_size 1m;
#   }
#
# URL a cadastrar no Stripe Dashboard → Developers → Webhooks:
#   https://seudominio.com/stripe/webhook
#
# VARIÁVEIS DE AMBIENTE NECESSÁRIAS (.env):
#   STRIPE_SECRET_KEY=sk_live_...
#   STRIPE_WEBHOOK_SECRET=whsec_...
#   STRIPE_PRICE_ID=price_...
#   GUILD_ID=123456789
#   VIP_ROLE_ID=987654321
#   PUBLIC_BASE_URL=https://seudominio.com
#   WEBHOOK_HOST=127.0.0.1   # bind só local, nginx faz o proxy
#   WEBHOOK_PORT=8765
