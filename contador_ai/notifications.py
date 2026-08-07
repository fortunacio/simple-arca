from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable
from zoneinfo import ZoneInfo

from .db import Database
from .service import PROVIDERS, TaxMonitorService, format_date, format_money


LOGGER = logging.getLogger(__name__)
ARGENTINA_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")
REMINDER_DAYS = frozenset({14, 7, 3, 1, 0})
SNAPSHOT_KINDS = {
    "ARCA_PROFILE": "taxpayer_profile",
    "ARCA_DFE": "unread_communications",
    "ARCA_CCMA": "account_status",
    "ARCA_SETI": "payment_status",
    "ARCA_INVOICES": "issued_invoices",
    "SANTA_FE_IIBB": "account_status",
}
STALE_AFTER = {
    "ARCA_PROFILE": timedelta(hours=30),
    "ARCA_DFE": timedelta(hours=30),
    "ARCA_INVOICES": timedelta(hours=30),
    "ARCA_CCMA": timedelta(hours=12),
    "ARCA_SETI": timedelta(hours=12),
    "SANTA_FE_IIBB": timedelta(hours=12),
}


@dataclass(frozen=True)
class NotificationDelivery:
    event_key: str
    fingerprint: str


@dataclass(frozen=True)
class NotificationBatch:
    text: str
    deliveries: tuple[NotificationDelivery, ...]


class TaxNotificationMonitor:
    def __init__(
        self,
        db: Database,
        service: TaxMonitorService,
        *,
        now: Callable[[], datetime] | None = None,
        refresh_interval: timedelta = timedelta(hours=6),
    ):
        self.db = db
        self.service = service
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.refresh_interval = refresh_interval

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

    def _snapshot(self, chat_id: int, provider: str) -> tuple[dict, datetime] | None:
        row = self.db.get_live_snapshot(chat_id, provider, SNAPSHOT_KINDS[provider])
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
            fetched_at = self._parse_datetime(row["fetched_at"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return (payload, fetched_at) if fetched_at is not None else None

    def refresh_official_apis(self, chat_id: int) -> None:
        now = self.now()
        state_key = f"monitor:last_api_refresh:{chat_id}"
        last_refresh = self._parse_datetime(self.db.get_bot_state(state_key))
        if last_refresh is not None and now - last_refresh < self.refresh_interval:
            return
        for provider in ("ARCA_PROFILE", "ARCA_DFE", "ARCA_INVOICES"):
            synchronizer = self.service.live_synchronizers.get(provider)
            if synchronizer is None:
                continue
            try:
                synchronizer(chat_id)
            except Exception:
                LOGGER.exception("Falló la actualización automática de %s", provider)
        self.db.set_bot_state(state_key, now.isoformat())

    def check_chat(self, chat_id: int) -> NotificationBatch | None:
        self.refresh_official_apis(chat_id)
        now = self.now()
        today = now.astimezone(ARGENTINA_TIMEZONE).date()
        sections: dict[str, list[str]] = {}
        deliveries: list[NotificationDelivery] = []

        def add(section: str, event_key: str, fingerprint: str, line: str) -> None:
            if self.db.notification_was_sent(chat_id, event_key, fingerprint):
                return
            sections.setdefault(section, []).append(line)
            deliveries.append(NotificationDelivery(event_key, fingerprint))

        seti = self._snapshot(chat_id, "ARCA_SETI")
        seti_payload = seti[0] if seti is not None else {}
        payment_reconciliation = seti_payload.get("ccma_reconciliation", {})
        payment_update_pending = False
        if isinstance(payment_reconciliation, dict):
            paid = int(payment_reconciliation.get("paid_veps", 0))
            matched = int(payment_reconciliation.get("matched_veps", 0))
            unmatched = int(
                payment_reconciliation.get("unmatched_veps", max(0, paid - matched))
            )
            payment_update_pending = (
                str(payment_reconciliation.get("status", "")).upper() == "PARTIAL"
                and paid > 0
                and unmatched > 0
            )

        ccma = self._snapshot(chat_id, "ARCA_CCMA")
        if ccma is not None:
            ccma_payload, _fetched_at = ccma
            try:
                debt = Decimal(str(ccma_payload.get("debtor_total", "0")))
            except InvalidOperation:
                debt = Decimal("0")
            overdue = ccma_payload.get("overdue", [])
            if debt > 0 and not payment_update_pending:
                fingerprint = "ACTIONABLE:" + json.dumps(
                    [
                        str(debt),
                        sorted(
                            (str(item.get("period")), str(item.get("balance")))
                            for item in overdue
                        ),
                    ],
                    separators=(",", ":"),
                )
                add(
                    "Pagos pendientes",
                    "debt:arca_ccma",
                    fingerprint,
                    f"• Monotributo: {format_money(debt)} de deuda vencida.",
                )

            grouped: dict[tuple[str, str], Decimal] = {}
            for item in ccma_payload.get("upcoming", []):
                key = (str(item.get("period", "")), str(item.get("due_date", "")))
                try:
                    grouped[key] = grouped.get(key, Decimal("0")) + Decimal(
                        str(item.get("balance", "0"))
                    )
                except InvalidOperation:
                    continue
            for (period, due_value), amount in grouped.items():
                try:
                    due = date.fromisoformat(due_value)
                except ValueError:
                    continue
                days = (due - today).days
                if days not in REMINDER_DAYS:
                    continue
                suffix = "vence hoy" if days == 0 else f"vence en {days} días"
                add(
                    "Próximos vencimientos",
                    f"due:arca_ccma:{period}:{days}",
                    f"{due_value}:{amount}",
                    f"• Monotributo: {format_money(amount)} — {format_date(due_value)} ({suffix}).",
                )

        if seti is not None:
            reconciliation = payment_reconciliation
            if isinstance(reconciliation, dict) and reconciliation:
                matched = int(reconciliation.get("matched_veps", 0))
                total = int(reconciliation.get("paid_veps", 0))
                status = str(reconciliation.get("status", "UNKNOWN"))
                fingerprint = f"{status}:{matched}:{total}:{reconciliation.get('ccma_debtor_total')}"
                if status == "COMPLETE":
                    line = "• Tu pago de Monotributo ya quedó reflejado por completo."
                else:
                    line = (
                        "• Tu pago de Monotributo está confirmado, pero la cuenta todavía "
                        "no terminó de actualizarse. No vuelvas a pagarlo."
                    )
                add(
                    "Pagos",
                    "seti:ccma",
                    fingerprint,
                    line,
                )

        iibb = self._snapshot(chat_id, "SANTA_FE_IIBB")
        if iibb is not None:
            payload, _fetched_at = iibb
            reconciliation = payload.get("vepai", {}).get("reconciliation", {})
            missing = tuple(str(item) for item in reconciliation.get("missing_periods", []))
            if missing:
                add(
                    "Pagos pendientes",
                    "debt:santa_fe_iibb",
                    ",".join(missing),
                    f"• Ingresos Brutos: hay {len(missing)} período(s) sin pago identificado.",
                )
            obligation = payload.get("next_obligation")
            if isinstance(obligation, dict):
                due_value = str(obligation.get("due_date", ""))
                try:
                    due = date.fromisoformat(due_value)
                except ValueError:
                    due = None
                if due is not None:
                    days = (due - today).days
                    if days in REMINDER_DAYS:
                        suffix = "vence hoy" if days == 0 else f"vence en {days} días"
                        add(
                            "Próximos vencimientos",
                            f"due:santa_fe_iibb:{obligation.get('period')}:{days}",
                            f"{due_value}:{obligation.get('amount')}",
                            (
                                f"• Ingresos Brutos: {format_money(obligation.get('amount'))} "
                                f"— {format_date(due_value)} ({suffix})."
                            ),
                        )

        dfe = self._snapshot(chat_id, "ARCA_DFE")
        if dfe is not None:
            payload, _fetched_at = dfe
            for item in payload.get("communications", []):
                communication_id = str(item.get("communication_id", ""))
                if not communication_id:
                    continue
                subject = str(item.get("subject") or "Sin asunto")
                add(
                    "Mensajes de ARCA",
                    f"dfe:{communication_id}",
                    f"{item.get('published_at')}:{subject}",
                    f"• {subject}",
                )

        invoices = self._snapshot(chat_id, "ARCA_INVOICES")
        if invoices is not None:
            payload, _fetched_at = invoices
            projection = self.service.projection_lines(payload)
            if len(projection) >= 2 and " — " in projection[1]:
                category = projection[1].split(" — ", 1)[0]
                event_key = "category:projected"
                if not self.db.has_notification_event(chat_id, event_key):
                    self.db.mark_notification_sent(chat_id, event_key, category)
                elif not self.db.notification_was_sent(chat_id, event_key, category):
                    add(
                        "Cambios de categoría",
                        event_key,
                        category,
                        f"• La categoría proyectada cambió a {category}. Revisá /resumen.",
                    )

        connection_labels = {
            "ARCA_PROFILE": "Categoría y facturación",
            "ARCA_DFE": "Mensajes de ARCA",
            "ARCA_CCMA": "Monotributo",
            "ARCA_SETI": "Monotributo",
            "ARCA_INVOICES": "Categoría y facturación",
            "SANTA_FE_IIBB": "Ingresos Brutos",
        }
        connections = {row["provider"]: row for row in self.db.get_connections(chat_id)}
        for provider, maximum_age in STALE_AFTER.items():
            if provider not in self.service.live_synchronizers:
                continue
            connection = connections.get(provider)
            snapshot = self._snapshot(chat_id, provider)
            last_success = self._parse_datetime(connection["last_success_at"]) if connection else None
            if last_success is None and snapshot is not None:
                last_success = snapshot[1]
            status = str(connection["status"]) if connection else "MISSING"
            needs_credentials = status in {"LOGIN_REQUIRED", "CREDENTIALS_REQUIRED"}
            stale = last_success is None or now - last_success > maximum_age
            if not needs_credentials and not stale:
                continue
            fingerprint = f"{status}:{last_success.isoformat() if last_success else 'never'}"
            reason = (
                "necesita volver a validar el acceso"
                if needs_credentials
                else "lleva demasiado tiempo sin actualizarse"
            )
            add(
                "Servicios desactualizados",
                f"stale:{provider}",
                fingerprint,
                f"• {connection_labels.get(provider, PROVIDERS[provider])} {reason}.",
            )

        if not deliveries:
            return None
        lines = ["Hay novedades"]
        for heading, items in sections.items():
            lines.extend(["", heading, *items])
        return NotificationBatch("\n".join(lines), tuple(deliveries))

    def mark_sent(self, chat_id: int, batch: NotificationBatch) -> None:
        for delivery in batch.deliveries:
            self.db.mark_notification_sent(
                chat_id,
                delivery.event_key,
                delivery.fingerprint,
            )
