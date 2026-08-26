from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable
from zoneinfo import ZoneInfo

from .db import Database
from .seti import SANTA_FE_IIBB_DESCRIPTION
from .service import (
    PROVIDERS,
    TaxMonitorService,
    format_date,
    format_money,
    format_month_year,
)


LOGGER = logging.getLogger(__name__)
ARGENTINA_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")
REMINDER_DAYS = frozenset({14, 7, 3, 1, 0})
MONOTRIBUTO_GRACE_BUSINESS_DAYS = 2
MONOTRIBUTO_COMPONENT_LABELS = {
    "020": "Impuesto integrado",
    "021": "Aporte jubilatorio",
    "024": "Obra social",
}
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
STALE_NOTIFICATION_GRACE = timedelta(minutes=15)


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
        broken_reminder_interval: timedelta = timedelta(hours=1),
        stale_notification_grace: timedelta = STALE_NOTIFICATION_GRACE,
    ):
        self.db = db
        self.service = service
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.refresh_interval = refresh_interval
        self.broken_reminder_interval = broken_reminder_interval
        self.stale_notification_grace = stale_notification_grace

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

    @staticmethod
    def _business_days_after(start: date, current: date) -> int:
        days = 0
        cursor = start
        while cursor < current:
            cursor += timedelta(days=1)
            if cursor.weekday() < 5:
                days += 1
        return days

    @staticmethod
    def _monotributo_components(
        payload: dict,
        period: str,
    ) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        paid: dict[str, Decimal] = {}
        for item in payload.get("payments", []) or []:
            description = str(item.get("description", "")).casefold()
            if (
                str(item.get("period", "")) != period
                or "pago oblig" not in description
                or "mensual" not in description
            ):
                continue
            try:
                amount = Decimal(str(item.get("amount", "0")))
            except InvalidOperation:
                continue
            if amount <= 0:
                continue
            tax = str(item.get("tax", ""))
            paid[tax] = paid.get(tax, Decimal("0")) + amount

        pending: dict[str, Decimal] = {}
        for item in payload.get("overdue", []) or []:
            if str(item.get("period", "")) != period:
                continue
            try:
                amount = Decimal(str(item["principal_outstanding"]))
            except (InvalidOperation, KeyError):
                continue
            if amount <= 0:
                continue
            tax = str(item.get("tax", ""))
            pending[tax] = pending.get(tax, Decimal("0")) + amount
        return paid, pending

    @staticmethod
    def _component_label(tax: str) -> str:
        return MONOTRIBUTO_COMPONENT_LABELS.get(
            tax,
            f"Concepto {tax or 'sin identificar'}",
        )

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

        def add(
            section: str,
            event_key: str,
            fingerprint: str,
            line: str,
            *,
            repeat_after: timedelta | None = None,
        ) -> None:
            if repeat_after is None:
                if self.db.notification_was_sent(chat_id, event_key, fingerprint):
                    return
            else:
                last_sent = self._parse_datetime(
                    self.db.notification_sent_at(chat_id, event_key, fingerprint)
                )
                if last_sent is not None and now - last_sent < repeat_after:
                    return
            sections.setdefault(section, []).append(line)
            deliveries.append(NotificationDelivery(event_key, fingerprint))

        seti = self._snapshot(chat_id, "ARCA_SETI")
        seti_payload = seti[0] if seti is not None else {}
        known_iibb_periods = self.db.get_iibb_vep_periods(chat_id)

        def iibb_period(vep: dict) -> str | None:
            number = str(vep.get("number", ""))
            if number in known_iibb_periods:
                return known_iibb_periods[number]
            if str(vep.get("tax_type", "")).upper() == "SANTA_FE_IIBB":
                return str(vep.get("tax_period", ""))
            match = SANTA_FE_IIBB_DESCRIPTION.fullmatch(
                str(vep.get("description", "")).strip()
            )
            if match:
                return f"20{match.group('year')}-{match.group('month')}"
            return None

        paid_iibb_veps = [
            (vep, period)
            for vep in (seti_payload.get("veps", []) or [])
            if str(vep.get("status", "")).upper() == "PAID"
            and (period := iibb_period(vep)) is not None
        ]
        paid_iibb_periods = {period for _vep, period in paid_iibb_veps if period}
        payment_reconciliation = seti_payload.get("ccma_reconciliation", {})
        if not isinstance(payment_reconciliation, dict):
            payment_reconciliation = {}
        elif paid_iibb_veps and "excluded_iibb_veps" not in payment_reconciliation:
            payment_reconciliation = {}
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
            overdue = ccma_payload.get("overdue", []) or []
            upcoming = ccma_payload.get("upcoming", []) or []
            obligations = ccma_payload.get("obligations", []) or []
            obligation_by_period = {
                str(item.get("period", "")): item
                for item in obligations
                if isinstance(item, dict) and str(item.get("period", ""))
            }
            current_periods = {
                str(item.get("period", ""))
                for item in (*overdue, *upcoming, *obligations)
                if str(item.get("period", ""))
            }
            overdue_grouped: dict[tuple[str, str], Decimal] = {}
            due_grouped: dict[tuple[str, str], Decimal] = {}
            undated_debt = Decimal("0")
            for item in overdue:
                period = str(item.get("period", ""))
                due_value = str(item.get("due_date", ""))
                try:
                    amount = Decimal(str(item.get("balance", "0")))
                except InvalidOperation:
                    continue
                try:
                    due = date.fromisoformat(due_value)
                except ValueError:
                    undated_debt += amount
                    continue
                current_period = today.strftime("%Y-%m")
                target = (
                    due_grouped
                    if due > today or (due == today and period >= current_period)
                    else overdue_grouped
                )
                target[(period, due_value)] = target.get((period, due_value), Decimal("0")) + amount

            for item in upcoming:
                key = (str(item.get("period", "")), str(item.get("due_date", "")))
                try:
                    due_grouped[key] = due_grouped.get(key, Decimal("0")) + Decimal(
                        str(item.get("balance", "0"))
                    )
                except InvalidOperation:
                    continue

            if not payment_update_pending:
                for (period, due_value), amount in overdue_grouped.items():
                    due = date.fromisoformat(due_value)
                    obligation = obligation_by_period.get(period, {})
                    status = str(obligation.get("status", "")).upper()
                    breakdown_known = all(
                        key in obligation
                        for key in ("principal", "principal_outstanding", "interest")
                    )
                    try:
                        principal_outstanding = Decimal(
                            str(obligation.get("principal_outstanding", "0"))
                        )
                        interest = Decimal(str(obligation.get("interest", "0")))
                    except InvalidOperation:
                        principal_outstanding = interest = Decimal("0")
                        breakdown_known = False
                    latest_payment_date = str(
                        obligation.get("latest_payment_date", "")
                    )
                    if breakdown_known and status == "INTEREST_ONLY":
                        add(
                            "Pagos pendientes",
                            f"residual-interest:arca_ccma:{period}",
                            f"{due_value}:{latest_payment_date or 'POSTED'}:v1",
                            (
                                f"• Monotributo de {format_month_year(date.fromisoformat(f'{period}-01'))}: "
                                "ARCA ya registró el capital, pero todavía deja "
                                f"{format_money(interest)} de intereses. Requiere revisar "
                                "la fecha de imputación; no los pagues nuevamente sin verificarla."
                            ),
                        )
                        continue
                    if breakdown_known and status == "PARTIALLY_POSTED":
                        paid_components, pending_components = self._monotributo_components(
                            ccma_payload,
                            period,
                        )
                        component_signature = json.dumps(
                            [
                                sorted(
                                    (tax, str(value))
                                    for tax, value in paid_components.items()
                                ),
                                sorted(
                                    (tax, str(value))
                                    for tax, value in pending_components.items()
                                ),
                            ],
                            separators=(",", ":"),
                        )
                        month = format_month_year(
                            date.fromisoformat(f"{period}-01")
                        )
                        detail_lines = [
                            f"• Monotributo de {month}"
                        ]
                        if paid_components:
                            detail_lines.append("  ARCA ya registró:")
                            detail_lines.extend(
                                f"  - {self._component_label(tax)}: {format_money(value)}"
                                for tax, value in sorted(paid_components.items())
                            )
                            detail_lines.append(
                                "  Total registrado: "
                                f"{format_money(sum(paid_components.values(), Decimal('0')))}"
                            )
                        if pending_components:
                            detail_lines.append("  Sigue pendiente:")
                            detail_lines.extend(
                                f"  - {self._component_label(tax)}: {format_money(value)}"
                                for tax, value in sorted(pending_components.items())
                            )
                        else:
                            detail_lines.append(
                                f"  Capital todavía pendiente: {format_money(principal_outstanding)}"
                            )
                        detail_lines.extend(
                            [
                                f"  Intereses visibles: {format_money(interest)}",
                                f"  Saldo actual: {format_money(amount)}",
                            ]
                        )
                        add(
                            "Pago parcialmente registrado por ARCA",
                            f"partial-posting:arca_ccma:{period}",
                            (
                                f"{due_value}:{latest_payment_date or 'POSTED'}:"
                                f"v2:{component_signature}"
                            ),
                            "\n".join(detail_lines),
                        )
                        continue
                    business_days = self._business_days_after(due, today)
                    if business_days < MONOTRIBUTO_GRACE_BUSINESS_DAYS:
                        detail = (
                            f" Capital: {format_money(principal_outstanding)}; "
                            f"intereses: {format_money(interest)}."
                            if breakdown_known
                            else ""
                        )
                        add(
                            "Pagos",
                            f"pending:arca_ccma:{period}",
                            f"{due_value}:v2",
                            (
                                f"• Monotributo: ARCA todavía muestra {format_money(amount)} "
                                f"pendiente.{detail} El débito automático puede estar "
                                "procesándose; lo sigo controlando."
                            ),
                        )
                    else:
                        detail = (
                            f" Capital: {format_money(principal_outstanding)}; "
                            f"intereses: {format_money(interest)}."
                            if breakdown_known
                            else ""
                        )
                        add(
                            "Pagos pendientes",
                            f"review:arca_ccma:{period}",
                            f"{due_value}:v2",
                            (
                                f"• Monotributo: ARCA todavía muestra {format_money(amount)} "
                                f"pendiente después de dos días hábiles.{detail} "
                                "Requiere revisión de la imputación."
                            ),
                        )
                if undated_debt > 0:
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
                        f"• Monotributo: {format_money(undated_debt)} de deuda vencida.",
                    )

            tracked_periods: set[str] = set()
            for prefix in ("pending:arca_ccma:", "review:arca_ccma:"):
                tracked_periods.update(
                    key.removeprefix(prefix)
                    for key in self.db.notification_event_keys(chat_id, prefix)
                )
            tracked_periods.update(
                key.removeprefix("due:arca_ccma:").split(":", 1)[0]
                for key in self.db.notification_event_keys(chat_id, "due:arca_ccma:")
            )
            for period in tracked_periods - current_periods:
                try:
                    period_label = format_month_year(date.fromisoformat(f"{period}-01"))
                except ValueError:
                    period_label = period
                add(
                    "Pagos",
                    f"confirmed:arca_ccma:{period}",
                    "CLEARED",
                    (
                        f"• Monotributo de {period_label}: ARCA ya no muestra "
                        "saldo pendiente."
                    ),
                )

            for (period, due_value), amount in due_grouped.items():
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
                    f"{due_value}:v2",
                    f"• Monotributo: {format_money(amount)} — {format_date(due_value)} ({suffix}).",
                )

        if seti is not None:
            reconciliation = payment_reconciliation
            if isinstance(reconciliation, dict) and reconciliation:
                matched = int(reconciliation.get("matched_veps", 0))
                total = int(reconciliation.get("paid_veps", 0))
                unmatched = int(
                    reconciliation.get("unmatched_veps", max(0, total - matched))
                )
                status = str(reconciliation.get("status", "UNKNOWN"))
                fingerprint = (
                    f"{status}:{matched}:{total}:"
                    f"{reconciliation.get('ccma_debtor_total')}"
                )
                event_key = "seti:ccma:v2"
                if status == "COMPLETE" and self.db.has_notification_event(
                    chat_id, event_key
                ):
                    line = "• Tu pago de Monotributo ya quedó reflejado por completo."
                elif status != "COMPLETE" and total > 0 and unmatched > 0:
                    line = (
                        "• Tu pago de Monotributo está confirmado, pero la cuenta todavía "
                        "no terminó de actualizarse. No vuelvas a pagarlo."
                    )
                else:
                    line = ""
                if line:
                    add(
                        "Pagos",
                        event_key,
                        fingerprint,
                        line,
                    )

        iibb = self._snapshot(chat_id, "SANTA_FE_IIBB")
        if iibb is not None:
            payload, _fetched_at = iibb
            paid_periods = {
                str(item.get("period", ""))
                for item in payload.get("vepai", {}).get("payments", [])
            }
            for vep, period in paid_iibb_veps:
                number = str(vep.get("number", ""))
                if period and period in paid_periods:
                    if number and self.db.has_notification_event(
                        chat_id, f"seti:iibb:{number}"
                    ):
                        add(
                            "Pagos",
                            f"confirmed:iibb:{number}",
                            f"POSTED:{period}",
                            (
                                "• Tu pago de Ingresos Brutos ya quedó reflejado "
                                "en la cuenta provincial."
                            ),
                        )
                    continue
                if not number:
                    continue
                period_label = ""
                try:
                    year, month = (int(part) for part in period.split("-"))
                    period_label = f" de {format_month_year(date(year, month, 1))}"
                except (TypeError, ValueError):
                    pass
                add(
                    "Pagos",
                    f"seti:iibb:{number}",
                    f"PAID:{period}:{vep.get('amount')}:{vep.get('paid_on')}",
                    (
                        f"• Tu pago de Ingresos Brutos{period_label} está confirmado, "
                        "pero la cuenta provincial todavía no terminó de actualizarse. "
                        "No vuelvas a pagarlo."
                    ),
                )
            reconciliation = payload.get("vepai", {}).get("reconciliation", {})
            missing = tuple(
                str(item)
                for item in reconciliation.get("missing_periods", [])
                if str(item) not in paid_iibb_periods
            )
            if missing:
                add(
                    "Pagos pendientes",
                    "debt:santa_fe_iibb",
                    ",".join(missing),
                    f"• Ingresos Brutos: hay {len(missing)} período(s) sin pago identificado.",
                )
            obligation = payload.get("next_obligation")
            if (
                isinstance(obligation, dict)
                and str(obligation.get("period", "")) not in paid_iibb_periods
            ):
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
        portal_providers = {"ARCA_CCMA", "ARCA_SETI", "SANTA_FE_IIBB"}
        identity = self.db.get_tax_identity(chat_id)
        portal_access = self.db.get_portal_access(chat_id)
        portal_enabled = any(
            provider in self.service.live_synchronizers for provider in portal_providers
        )
        portal_needs_credentials = (
            portal_enabled
            and identity is not None
            and identity["status"] == "VERIFIED"
            and (
                portal_access is None
                or portal_access["represented_cuit"] != identity["represented_cuit"]
            )
        )
        if portal_needs_credentials:
            had_portal_access = any(
                connection is not None and connection["last_success_at"]
                for provider, connection in connections.items()
                if provider in portal_providers
            )
            add(
                "Acceso a ARCA",
                "portal-access",
                "missing",
                "• El acceso guardado dejó de funcionar. Volvé a cargar tu clave "
                "fiscal una vez."
                if had_portal_access
                else "• Falta completar la conexión una sola vez para consultar "
                "Monotributo e Ingresos Brutos.",
                repeat_after=self.broken_reminder_interval,
            )
        for provider, maximum_age in STALE_AFTER.items():
            if provider not in self.service.live_synchronizers:
                continue
            if portal_needs_credentials and provider in portal_providers:
                continue
            connection = connections.get(provider)
            snapshot = self._snapshot(chat_id, provider)
            last_success = self._parse_datetime(connection["last_success_at"]) if connection else None
            if last_success is None and snapshot is not None:
                last_success = snapshot[1]
            status = str(connection["status"]) if connection else "MISSING"
            needs_credentials = status in {"LOGIN_REQUIRED", "CREDENTIALS_REQUIRED"}
            stale = (
                last_success is None
                or now - last_success > maximum_age + self.stale_notification_grace
            )
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
                repeat_after=self.broken_reminder_interval,
            )

        if not deliveries:
            return None
        lines = ["Hay novedades"]
        for heading, items in sections.items():
            lines.extend(["", heading, *items])
        return NotificationBatch("\n".join(lines), tuple(deliveries))

    def mark_sent(self, chat_id: int, batch: NotificationBatch) -> None:
        sent_at = self.now().isoformat()
        for delivery in batch.deliveries:
            self.db.mark_notification_sent(
                chat_id,
                delivery.event_key,
                delivery.fingerprint,
                sent_at,
            )
