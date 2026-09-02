from __future__ import annotations

import json
import hashlib
import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable
from zoneinfo import ZoneInfo

from .bot_reply import BotReply
from .commands import INVOICE_COMMAND_NAMES, CommandCatalog
from .db import Database
from .health import (
    SOURCE_POLICIES,
    SourceCondition,
    render_health_brief,
    render_health_detail,
    source_health,
    stale_snapshot_warning,
)
from .arca.errors import WsfePointOfSaleUnavailable
from .identity import is_valid_cuit, mask_cuit, normalize_cuit
from .monotributo import (
    CategoryLimits,
    completed_months_between,
    next_recategorization,
    projected_category,
    projection_window,
)
from .seti import SANTA_FE_IIBB_DESCRIPTION


PROVIDERS = {
    "ARCA_PROFILE": "Categoría y situación en ARCA",
    "ARCA_DFE": "Bandeja de ARCA",
    "ARCA_CCMA": "Deuda de monotributo",
    "ARCA_SETI": "Pagos de ARCA",
    "ARCA_ACCOUNTS": "Otras deudas con ARCA",
    "ARCA_INVOICES": "Facturación",
    "SANTA_FE_IIBB": "Ingresos Brutos de Santa Fe",
}

PROVIDER_COMMANDS = {
    "ARCA_PROFILE": "/categoria",
    "ARCA_DFE": "/bandeja",
    "ARCA_CCMA": "/monotributo",
    "ARCA_SETI": "/pagos_arca",
    "ARCA_INVOICES": "/facturacion",
    "SANTA_FE_IIBB": "/iibb",
}

ARGENTINA_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")
MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
WEEKDAYS = (
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
)
LOGGER = logging.getLogger(__name__)


def month_keys_between(window_from: date, window_to: date) -> tuple[str, ...]:
    current = window_from.replace(day=1)
    result: list[str] = []
    while current <= window_to:
        result.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return tuple(result)


def format_datetime_argentina(value: str | None) -> str:
    if not value:
        return "sin actualizar"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(ARGENTINA_TIMEZONE)
    except (TypeError, ValueError):
        return "sin actualizar"
    return local.strftime("%d/%m/%Y %H:%M")


def format_date(value: str | None) -> str:
    try:
        return date.fromisoformat(value or "").strftime("%d/%m/%Y")
    except ValueError:
        return value or "sin fecha"


def format_period(value: str | None) -> str:
    raw = str(value or "")
    if len(raw) == 6 and raw.isdigit() and 1 <= int(raw[4:]) <= 12:
        return f"{MONTHS[int(raw[4:]) - 1]} de {raw[:4]}"
    return raw or "sin fecha"


def format_category(value: object) -> str:
    raw = " ".join(str(value or "").split())
    if len(raw) > 2 and raw[0].isalpha() and raw[1] == " ":
        return f"{raw[0].upper()} — {raw[2:].lower().capitalize()}"
    return raw or "Sin categoría"


def category_letter(value: object) -> str:
    raw = " ".join(str(value or "").upper().split())
    if raw and raw[0] in "ABCDEFGHIJK" and (len(raw) == 1 or not raw[1].isalpha()):
        return raw[0]
    return ""


def format_money(value: object) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return "$0,00"
    formatted = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"${formatted}"


def format_month_year(value: date) -> str:
    return f"{MONTHS[value.month - 1]} de {value.year}"


class TaxMonitorService:
    """Presenta únicamente información obtenida por conectores del usuario."""

    def __init__(
        self,
        db: Database,
        today: date | None = None,
        operator_cuit: str = "",
        certificate_alias: str = "",
        arca_environment: str = "homologation",
        live_synchronizers: dict[str, Callable[[int], None]] | None = None,
        category_limits_provider: Callable[[date], CategoryLimits] | None = None,
        ccma_login_url: str = "",
        arca_enrollment_base_url: str = "",
        arca_enrollment_ttl: timedelta = timedelta(minutes=10),
        command_catalog: CommandCatalog | None = None,
    ):
        self.db = db
        # Keep an explicit date only for deterministic tests. In the running bot
        # the service is long-lived, so the current date must be evaluated for
        # every request instead of being frozen at process startup.
        self._today_override = today
        self.operator_cuit = normalize_cuit(operator_cuit)
        self.certificate_alias = certificate_alias.strip()
        self.arca_environment = arca_environment
        self.live_synchronizers = live_synchronizers or {}
        self.category_limits_provider = category_limits_provider
        self.ccma_login_url = ccma_login_url.strip()
        self.arca_enrollment_base_url = arca_enrollment_base_url.rstrip("/")
        self.arca_enrollment_ttl = arca_enrollment_ttl
        self.command_catalog = command_catalog or CommandCatalog(
            db,
            enabled_sources=self.live_synchronizers,
            arca_enrollment_enabled=bool(self.arca_enrollment_base_url),
        )

    @property
    def today(self) -> date:
        if self._today_override is not None:
            return self._today_override
        return datetime.now(ARGENTINA_TIMEZONE).date()

    @property
    def now(self) -> datetime:
        if self._today_override is not None:
            return datetime.combine(
                self._today_override,
                datetime.min.time().replace(hour=12),
                tzinfo=ARGENTINA_TIMEZONE,
            ).astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    def help(self, chat_id: int) -> str:
        return self.command_catalog.render_help(chat_id)

    def configure_arca(self, chat_id: int) -> str:
        identity = self.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            return "Primero conectá y verificá tu cuenta de ARCA con /conectar."
        return self._portal_setup_message(
            chat_id,
            "Actualizá el acceso guardado",
            "Abrí este enlace privado y cargá tu clave fiscal:",
        )

    def _portal_setup_message(self, chat_id: int, title: str, instruction: str) -> str:
        if not self.arca_enrollment_base_url:
            return "El alta web de ARCA todavía no está configurada en este servidor."
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        expires_at = datetime.now(timezone.utc) + self.arca_enrollment_ttl
        self.db.create_onboarding_token(digest, chat_id, expires_at.isoformat())
        return "\n".join(
            [
                title,
                "",
                instruction,
                f"{self.arca_enrollment_base_url}/arca/setup/{token}",
                "",
                "El enlace vence en 10 minutos y puede usarse una sola vez.",
            ]
        )

    def _portal_access_enabled(self) -> bool:
        return any(
            provider in self.live_synchronizers
            for provider in {"ARCA_CCMA", "ARCA_SETI", "SANTA_FE_IIBB"}
        )

    def _complete_connection(self, chat_id: int, cuit: str) -> str:
        verified = f"Listo, verifiqué el CUIT {mask_cuit(cuit)} en ARCA."
        if not self._portal_access_enabled():
            return verified
        access = self.db.get_portal_access(chat_id)
        if self.db.portal_access_matches(chat_id, cuit):
            if access["ready_at"]:
                return f"{verified}\n\nTu cuenta ya está lista para usar."
            return f"{verified}\n\nEl acceso ya está guardado. Estoy actualizando tu información."
        setup = self._portal_setup_message(
            chat_id,
            "Terminemos de conectar tu cuenta",
            "Para consultar Monotributo e Ingresos Brutos, cargá tu clave fiscal una sola vez:",
        )
        return f"{verified}\n\n{setup}"

    def connection_map(self, chat_id: int) -> dict[str, object]:
        return {row["provider"]: row for row in self.db.get_connections(chat_id)}

    def provider_status(self, chat_id: int, provider: str) -> str:
        if provider not in self.live_synchronizers:
            return f"⚪ {PROVIDERS[provider]} — todavía no disponible"
        health = source_health(self.db, chat_id, provider, self.now)
        if health.condition == SourceCondition.PENDING:
            identity = self.db.get_tax_identity(chat_id)
            if identity is None or identity["status"] != "VERIFIED":
                return f"⚪ {PROVIDERS[provider]} — falta conectar ARCA con /conectar"
        return render_health_detail(health)

    def status(self, chat_id: int) -> str:
        lines = ["Estado de tu cuenta"]
        identity = self.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            lines.append("⚪ Cuenta ARCA — falta conectar y verificar con /conectar")
        lines.extend(
            self.provider_status(chat_id, provider)
            for provider in PROVIDERS
            if provider in self.live_synchronizers
        )
        unavailable = [
            PROVIDERS[provider]
            for provider in PROVIDERS
            if provider not in self.live_synchronizers
        ]
        if unavailable:
            lines.append(
                "Todavía no disponible:\n"
                + "\n".join(f"• {label}" for label in unavailable)
            )
        return "\n\n".join(lines)

    def connect(self, chat_id: int, argument: str = "") -> str:
        identity = self.db.get_tax_identity(chat_id)
        if not argument:
            if identity and identity["status"] == "VERIFIED":
                return self._complete_connection(
                    chat_id, str(identity["represented_cuit"])
                )
            if identity:
                return "La conexión con ARCA todavía está pendiente."
            return "Enviá /conectar seguido de tu CUIT. Por ejemplo: /conectar 20123456786"

        cuit = normalize_cuit(argument)
        if not is_valid_cuit(cuit):
            return "CUIT inválido. Revisá los 11 dígitos y volvé a intentar."
        if identity:
            if identity["represented_cuit"] == cuit:
                if identity["status"] == "VERIFIED":
                    return self._complete_connection(chat_id, cuit)
                return self.verify_pending_identity(chat_id, cuit)
            return "Ya hay otro CUIT vinculado. Usá /desconectar antes de cambiarlo."
        try:
            self.db.set_pending_tax_identity(chat_id, cuit, self.arca_environment)
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                return "Ese CUIT ya está vinculado a otro chat. Primero debe revocarse esa vinculación."
            raise
        return self.verify_pending_identity(chat_id, cuit)

    def verify_pending_identity(self, chat_id: int, cuit: str) -> str:
        synchronizer = self.live_synchronizers.get("ARCA_PROFILE")
        if synchronizer is None:
            return (
                f"Guardé el CUIT {mask_cuit(cuit)}, pero este servidor todavía no "
                "tiene configurada la conexión oficial con ARCA."
            )
        try:
            synchronizer(chat_id)
        except Exception:
            LOGGER.exception("No se pudo verificar el CUIT con ARCA")
            return (
                f"Guardé el CUIT {mask_cuit(cuit)}, pero no pude verificarlo con "
                "ARCA ahora. Volvé a enviar el mismo comando para reintentar."
            )
        identity = self.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            return (
                f"Guardé el CUIT {mask_cuit(cuit)}, pero ARCA todavía no confirmó "
                "la conexión. Volvé a enviar el mismo comando para reintentar."
            )
        return self._complete_connection(chat_id, cuit)

    def valid_snapshot(self, chat_id: int, provider: str, kind: str) -> dict | None:
        if provider in SOURCE_POLICIES:
            health = source_health(self.db, chat_id, provider, self.now)
            if not health.snapshot.is_fresh or health.snapshot.payload is None:
                return None
            payload = dict(health.snapshot.payload)
            payload["fetched_at"] = (
                health.snapshot.fetched_at.isoformat()
                if health.snapshot.fetched_at is not None
                else None
            )
            return payload
        row = self.db.get_live_snapshot(chat_id, provider, kind)
        if row is None:
            return None
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return None
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        payload["fetched_at"] = row["fetched_at"]
        return payload

    def available_snapshot(self, chat_id: int, provider: str, kind: str) -> dict | None:
        row = self.db.get_live_snapshot(chat_id, provider, kind)
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload["fetched_at"] = row["fetched_at"]
        return payload

    def source_warning(self, chat_id: int, provider: str) -> str | None:
        if provider not in SOURCE_POLICIES:
            return None
        health = source_health(self.db, chat_id, provider, self.now)
        stale_warning = stale_snapshot_warning(health)
        if health.condition == SourceCondition.STALE:
            return stale_warning
        notices = []
        if health.condition not in {SourceCondition.HEALTHY, SourceCondition.PENDING}:
            notices.append(f"⚠️ {render_health_brief(health)}")
        if stale_warning:
            notices.append(stale_warning)
        return "\n".join(notices) if notices else None

    def profile_snapshot(self, chat_id: int) -> dict | None:
        return self.available_snapshot(chat_id, "ARCA_PROFILE", "taxpayer_profile")

    def invoice_snapshot(self, chat_id: int) -> dict | None:
        return self.available_snapshot(chat_id, "ARCA_INVOICES", "issued_invoices")

    def projection_lines(self, snapshot: dict) -> list[str]:
        invoices = snapshot.get("invoices", [])
        snapshot_monthly_totals = snapshot.get("monthly_totals", [])
        if not invoices and not snapshot_monthly_totals:
            return [
                "Categoría proyectada para recategorización",
                "Todavía no hay facturas registradas en el período analizado.",
            ]

        recategorization = next_recategorization(self.today)
        target_from, target_to = projection_window(recategorization)
        try:
            completed_to = min(
                date.fromisoformat(snapshot.get("window_to", "")),
                target_to,
            )
        except ValueError:
            return [
                "Categoría proyectada para recategorización",
                "No pude calcularla con la información recibida.",
            ]
        months = completed_months_between(target_from, completed_to)
        if months < 1:
            return [
                "Categoría proyectada para recategorización",
                "Todavía no hay meses completos para hacer la proyección.",
            ]

        monthly_totals: dict[str, Decimal] = {}
        for item in snapshot_monthly_totals:
            period = str(item.get("period", ""))
            try:
                amount = Decimal(str(item.get("total", "0")))
                issued_at = date.fromisoformat(f"{period}-01")
            except (InvalidOperation, ValueError):
                continue
            if target_from <= issued_at <= completed_to:
                monthly_totals[period] = amount
        if not snapshot_monthly_totals:
            for invoice in invoices:
                try:
                    issued_at = date.fromisoformat(invoice.get("issued_at", ""))
                    amount = Decimal(str(invoice.get("net_total_pesos", "0")))
                except (InvalidOperation, ValueError):
                    continue
                if target_from <= issued_at <= completed_to:
                    month = issued_at.strftime("%Y-%m")
                    monthly_totals[month] = monthly_totals.get(
                        month, Decimal("0")
                    ) + amount

        expected_completed_months = set(month_keys_between(target_from, completed_to))
        complete_months = {
            str(period) for period in snapshot.get("complete_months", [])
        }
        history_complete = snapshot.get(
            "history_complete",
            snapshot.get("manual_history_complete", False),
        )
        if history_complete:
            complete_months.update(expected_completed_months)
        missing_months = expected_completed_months - complete_months
        if not snapshot.get("complete_months") and not history_complete:
            missing_months = expected_completed_months - set(monthly_totals)
        missing_completed_months = len(missing_months)
        projection_is_complete = missing_completed_months == 0
        registered_months = len(expected_completed_months & set(monthly_totals))
        accumulated = sum(monthly_totals.values(), Decimal("0"))
        if projection_is_complete:
            average = accumulated / months
            average_description = f"promedio mensual de {format_money(average)}"
        elif monthly_totals:
            average = accumulated / registered_months
            noun = "mes registrado" if registered_months == 1 else "meses registrados"
            average_description = (
                f"promedio de {format_money(average)} sobre {registered_months} {noun}"
            )
        else:
            return [
                "Categoría proyectada para recategorización",
                "Todavía no hay meses facturados para proyectar la próxima categoría.",
            ]
        target_months = completed_months_between(target_from, target_to)
        remaining_months = max(0, target_months - months)
        if not projection_is_complete:
            missing_noun = (
                "mes ya cerrado"
                if missing_completed_months == 1
                else "meses ya cerrados"
            )
            remaining_noun = "mes" if remaining_months == 1 else "meses"
            return [
                "Categoría proyectada para recategorización",
                (
                    "Todavía no puedo determinarla: faltan datos de "
                    f"{missing_completed_months} {missing_noun}."
                ),
                (
                    f"La revisión de {format_month_year(recategorization)} toma "
                    f"{format_month_year(target_from)}–{format_month_year(target_to)}; "
                    f"quedan {remaining_months} {remaining_noun} por cerrar."
                ),
            ]
        annual_projection = accumulated + average * remaining_months
        if self.category_limits_provider is None:
            return [
                "Categoría proyectada para recategorización",
                "No pude verificar los topes vigentes de ARCA.",
                f"Facturación anual proyectada: {format_money(annual_projection)}",
            ]
        try:
            category_limits = self.category_limits_provider(self.today)
        except Exception:
            LOGGER.exception("No se pudieron verificar los topes de monotributo")
            return [
                "Categoría proyectada para recategorización",
                "No pude verificar los topes vigentes de ARCA.",
                f"Facturación anual proyectada: {format_money(annual_projection)}",
            ]
        category = projected_category(annual_projection, category_limits)
        projected = category or "supera el tope del monotributo"
        lines = [
            "Categoría proyectada para recategorización",
            f"{projected} — {average_description}",
            f"Facturación proyectada al cierre: {format_money(annual_projection)}",
            (
                f"Período {format_month_year(target_from)}–{format_month_year(target_to)}: "
                f"{months} meses cerrados y {remaining_months} por cerrar."
            ),
            (
                "Resultado calculado con los topes vigentes desde "
                f"{format_date(category_limits.effective_from.isoformat())}; "
                "ARCA puede actualizarlos antes de la próxima recategorización."
            ),
            f"Próxima recategorización: {format_month_year(recategorization)}",
            "Esta proyección usa la facturación; ARCA también evalúa los demás parámetros del régimen.",
        ]
        if category_limits.verification_pending:
            lines.append(
                "No pude volver a verificar la tabla hoy; usé la última versión oficial guardada."
            )
        return lines

    def category(self, chat_id: int) -> str:
        error = self.sync_provider(chat_id, "ARCA_PROFILE")
        profile = self.profile_snapshot(chat_id)
        if profile is None:
            return error or self.source_command(chat_id, "ARCA_PROFILE")
        categories = [
            item
            for item in profile.get("categories", [])
            if item.get("status", "").upper() in {"", "ACTIVO"}
        ]
        category_lines = [
            f"• {format_category(item.get('description') or item.get('category_id'))}\n"
            f"  Vigente desde {format_period(item.get('period'))}"
            for item in categories
        ] or ["ARCA no informó una categoría de monotributo."]
        projection = [
            "Categoría proyectada para recategorización",
            "Todavía no está disponible.",
        ]
        if "ARCA_INVOICES" in self.live_synchronizers:
            invoice_error = self.sync_provider(chat_id, "ARCA_INVOICES")
            invoice_snapshot = self.invoice_snapshot(chat_id)
            if invoice_snapshot is not None:
                projection = self.projection_lines(invoice_snapshot)
            elif invoice_error:
                projection = [
                    "Categoría proyectada para recategorización",
                    invoice_error,
                ]
        lines = [
                "Tu categoría de monotributo",
                "",
                *category_lines,
                "",
                f"Estado en ARCA: {(profile.get('key_status') or 'sin dato').lower()}",
                f"Actualizado: {format_datetime_argentina(profile.get('fetched_at'))}",
                "",
                *projection,
            ]
        for provider in ("ARCA_PROFILE", "ARCA_INVOICES"):
            warning = self.source_warning(chat_id, provider)
            if warning:
                lines.extend(["", warning])
        return "\n".join(lines)

    def invoicing(self, chat_id: int) -> str:
        error = self.sync_provider(chat_id, "ARCA_INVOICES")
        snapshot = self.invoice_snapshot(chat_id)
        if snapshot is None:
            return error or self.source_command(chat_id, "ARCA_INVOICES")
        count = int(snapshot.get("invoice_count", 0))
        monthly_count = len(snapshot.get("monthly_totals", []))
        if count == 0 and monthly_count == 0:
            return "Todavía no hay facturación registrada para el período consultado."
        total = Decimal(str(snapshot.get("total_pesos", "0")))
        history_complete = snapshot.get(
            "history_complete",
            snapshot.get("manual_history_complete", False),
        ) is True
        divisor = 12 if history_complete else monthly_count
        average = total / divisor if divisor else None
        lines = [
                "Facturación",
                "",
                (
                    f"Últimos 12 meses completos: {format_money(total)}"
                    if history_complete
                    else f"Facturación registrada: {format_money(total)}"
                ),
                (
                    f"Promedio mensual: {format_money(average)}"
                    if average is not None
                    else "Promedio mensual: todavía no disponible"
                ),
                f"Meses con datos: {monthly_count} de 12",
                f"Facturas individuales registradas: {count}",
                f"Período: {format_date(snapshot.get('window_from'))} al {format_date(snapshot.get('window_to'))}",
                f"Actualizado: {format_datetime_argentina(snapshot.get('fetched_at'))}",
            ]
        warning = self.source_warning(chat_id, "ARCA_INVOICES")
        if warning:
            lines.extend(["", warning])
        return "\n".join(lines)

    def inbox(self, chat_id: int) -> str:
        error = self.sync_provider(chat_id, "ARCA_DFE")
        snapshot = self.available_snapshot(
            chat_id, "ARCA_DFE", "unread_communications"
        )
        if snapshot is None:
            return error or self.source_command(chat_id, "ARCA_DFE")
        communications = snapshot.get("communications", [])
        if not communications:
            detail = "No tenés comunicaciones sin leer de los últimos 30 días."
        else:
            lines = [f"Tenés {len(communications)} comunicación{'es' if len(communications) != 1 else ''} sin leer:"]
            for item in communications[:20]:
                attachment = " · con adjunto" if item.get("has_attachment") else ""
                lines.append(
                    f"• {format_date(item.get('published_at', '')[:10])} · {item.get('publisher') or 'ARCA'}\n"
                    f"  {item.get('subject') or 'Sin asunto'}{attachment}"
                )
            if len(communications) > 20:
                lines.append(f"… y {len(communications) - 20} más")
            detail = "\n".join(lines)
        result = [
            "Bandeja de ARCA",
            "",
            detail,
            "",
            f"Revisado: {format_datetime_argentina(snapshot.get('fetched_at'))}",
        ]
        warning = self.source_warning(chat_id, "ARCA_DFE")
        if warning:
            result.extend(["", warning])
        return "\n".join(result)

    @staticmethod
    def _period_totals(items: list[dict]) -> list[tuple[str, Decimal]]:
        totals: dict[str, Decimal] = {}
        for item in items:
            period = str(item.get("period", ""))
            try:
                amount = Decimal(str(item.get("balance", "0")))
            except InvalidOperation:
                continue
            totals[period] = totals.get(period, Decimal("0")) + abs(amount)
        return sorted(totals.items())

    @classmethod
    def _balance_total(cls, items: list[dict]) -> Decimal:
        return sum((total for _period, total in cls._period_totals(items)), Decimal("0"))

    @staticmethod
    def _business_days_after(start: date, current: date) -> int:
        days = 0
        cursor = start
        while cursor < current:
            cursor += timedelta(days=1)
            if cursor.weekday() < 5:
                days += 1
        return days

    def _ccma_balance_buckets(self, snapshot: dict) -> tuple[Decimal, Decimal]:
        if "overdue" not in snapshot:
            try:
                return Decimal(str(snapshot.get("debtor_total", "0"))), Decimal("0")
            except InvalidOperation:
                return Decimal("0"), Decimal("0")
        actionable = Decimal("0")
        processing = Decimal("0")
        for item in snapshot.get("overdue", []):
            try:
                amount = abs(Decimal(str(item.get("balance", "0"))))
                due = date.fromisoformat(str(item.get("due_date", "")))
            except (InvalidOperation, ValueError):
                try:
                    actionable += abs(Decimal(str(item.get("balance", "0"))))
                except InvalidOperation:
                    continue
                continue
            if self._business_days_after(due, self.today) < 2:
                processing += amount
            else:
                actionable += amount
        return actionable, processing

    def _ccma_overdue_total(self, snapshot: dict) -> Decimal:
        return self._ccma_balance_buckets(snapshot)[0]

    def _ccma_tracking_details(self, snapshot: dict) -> dict[str, object] | None:
        candidates: list[dict[str, object]] = []
        for obligation in snapshot.get("obligations", []):
            if not isinstance(obligation, dict):
                continue
            try:
                due = date.fromisoformat(str(obligation.get("due_date", "")))
                balance = Decimal(str(obligation.get("balance", "0")))
                principal = Decimal(str(obligation["principal"]))
                principal_outstanding = Decimal(
                    str(obligation["principal_outstanding"])
                )
                interest = Decimal(str(obligation["interest"]))
            except (InvalidOperation, KeyError, ValueError):
                continue
            if due > self.today or balance <= 0:
                continue
            candidates.append(
                {
                    "period": str(obligation.get("period", "")),
                    "due_date": due,
                    "balance": balance,
                    "principal": principal,
                    "principal_outstanding": principal_outstanding,
                    "interest": interest,
                    "status": str(obligation.get("status", "")).upper(),
                }
            )
        return max(candidates, key=lambda item: str(item["period"])) if candidates else None

    def _paid_iibb_periods(self, chat_id: int) -> set[str]:
        snapshot = self.valid_snapshot(chat_id, "ARCA_SETI", "payment_status")
        if snapshot is None:
            return set()
        known_periods = self.db.get_iibb_vep_periods(chat_id)
        periods: set[str] = set()
        for vep in snapshot.get("veps", []):
            if str(vep.get("status", "")).upper() != "PAID":
                continue
            number = str(vep.get("number", ""))
            if number in known_periods:
                periods.add(known_periods[number])
                continue
            if str(vep.get("tax_type", "")).upper() == "SANTA_FE_IIBB":
                period = str(vep.get("tax_period", ""))
                if period:
                    periods.add(period)
                continue
            match = SANTA_FE_IIBB_DESCRIPTION.fullmatch(
                str(vep.get("description", "")).strip()
            )
            if match:
                periods.add(f"20{match.group('year')}-{match.group('month')}")
        return periods

    def _ccma_balance_timeline(
        self,
        chat_id: int,
        snapshot: dict,
    ) -> list[tuple[date, Decimal]]:
        daily: dict[date, Decimal] = {}
        for row in self.db.notification_deliveries(
            chat_id,
            "due:arca_ccma:",
            limit=100,
        ):
            fingerprint = str(row["fingerprint"])
            _due_value, separator, amount_value = fingerprint.partition(":")
            if not separator:
                continue
            try:
                sent_at = datetime.fromisoformat(
                    str(row["sent_at"]).replace("Z", "+00:00")
                )
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                observed_on = sent_at.astimezone(ARGENTINA_TIMEZONE).date()
                daily[observed_on] = Decimal(amount_value)
            except (InvalidOperation, ValueError):
                continue
        for row in self.db.get_ccma_observations(chat_id, limit=60):
            try:
                daily[date.fromisoformat(str(row["calculated_on"]))] = Decimal(
                    str(row["debtor_total"])
                )
            except (InvalidOperation, ValueError):
                continue
        try:
            calculated_on = date.fromisoformat(str(snapshot.get("calculated_on", "")))
            daily[calculated_on] = Decimal(str(snapshot.get("debtor_total", "0")))
        except (InvalidOperation, ValueError):
            pass
        return sorted(daily.items())[-10:]

    @staticmethod
    def _display_month(period: str) -> str:
        try:
            value = date.fromisoformat(f"{period}-01")
        except ValueError:
            return period
        return format_month_year(value)

    def monotributo_account(self, chat_id: int) -> str:
        if "ARCA_CCMA" not in self.live_synchronizers:
            return self.source_command(chat_id, "ARCA_CCMA")
        snapshot = self.available_snapshot(chat_id, "ARCA_CCMA", "account_status")
        connection = self.connection_map(chat_id).get("ARCA_CCMA")
        if snapshot is None:
            if connection and connection["status"] == "CREDENTIALS_REQUIRED":
                return "ARCA rechazó la clave fiscal guardada. Hay que volver a cargarla."
            if connection and connection["status"] == "LOGIN_REQUIRED":
                lines = ["ARCA pidió una validación manual para consultar CCMA."]
                if self.ccma_login_url:
                    lines.extend(["", self.ccma_login_url])
                return "\n".join(lines)
            return self.source_command(chat_id, "ARCA_CCMA")

        debtor_total, processing_total = self._ccma_balance_buckets(snapshot)
        creditor_total = Decimal(str(snapshot.get("creditor_total", "0")))
        lines = ["Monotributo", ""]
        if debtor_total:
            lines.append(f"Deuda vencida: {format_money(debtor_total)}")
            for period, total in self._period_totals(snapshot.get("overdue", [])):
                lines.append(f"• {self._display_month(period)}: {format_money(total)}")
        else:
            lines.append("No registra deuda vencida en CCMA.")
        if processing_total:
            lines.extend(
                [
                    "",
                    f"ARCA todavía muestra {format_money(processing_total)} pendiente.",
                    "El débito automático puede estar procesándose.",
                ]
            )

        for obligation in snapshot.get("obligations", []):
            if not isinstance(obligation, dict):
                continue
            try:
                due = date.fromisoformat(str(obligation.get("due_date", "")))
                balance = Decimal(str(obligation.get("balance", "0")))
                principal = Decimal(str(obligation["principal"]))
                principal_outstanding = Decimal(
                    str(obligation["principal_outstanding"])
                )
                interest = Decimal(str(obligation["interest"]))
            except (InvalidOperation, KeyError, ValueError):
                continue
            if balance <= 0 or due > self.today:
                continue
            period = str(obligation.get("period", ""))
            status = str(obligation.get("status", "")).upper()
            lines.extend(
                [
                    "",
                    f"Seguimiento de {self._display_month(period)}",
                    f"• Vencimiento original: {format_date(due.isoformat())}",
                    f"• Capital original: {format_money(principal)}",
                    f"• Capital todavía pendiente en ARCA: {format_money(principal_outstanding)}",
                    f"• Intereses informados por ARCA: {format_money(interest)}",
                    f"• Saldo total informado: {format_money(balance)}",
                ]
            )
            if status == "INTEREST_ONLY":
                lines.append(
                    "• ARCA ya registró el capital, pero todavía mantiene intereses."
                )
            elif status == "PARTIALLY_POSTED":
                lines.append("• ARCA registró el pago sólo parcialmente.")
            elif interest > 0:
                lines.append(
                    "• ARCA todavía no refleja un pago aplicado a este período y sigue calculando intereses."
                )

        timeline = self._ccma_balance_timeline(chat_id, snapshot)
        if len(timeline) > 1:
            lines.extend(["", "Evolución del saldo informado por ARCA"])
            previous: Decimal | None = None
            for observed_on, amount in timeline:
                delta = amount - previous if previous is not None else Decimal("0")
                delta_label = (
                    f" ({'+' if delta > 0 else ''}{format_money(delta)})"
                    if previous is not None and delta
                    else ""
                )
                lines.append(
                    f"• {format_date(observed_on.isoformat())}: "
                    f"{format_money(amount)}{delta_label}"
                )
                previous = amount
        if creditor_total:
            lines.extend(["", f"Saldo a favor: {format_money(creditor_total)}"])

        latest_payment_date = str(snapshot.get("latest_payment_date", ""))
        try:
            latest_payment_total = Decimal(str(snapshot.get("latest_payment_total", "0")))
        except InvalidOperation:
            latest_payment_total = Decimal("0")
        if latest_payment_date and latest_payment_total:
            lines.extend(
                [
                    "",
                    "Último pago visible en CCMA",
                    f"{format_date(latest_payment_date)}: {format_money(latest_payment_total)}",
                ]
            )
            if snapshot.get("payment_reconciliation") == "PARTIAL":
                lines.append(
                    f"Saldo deudor que CCMA todavía informa: {format_money(debtor_total)}"
                )

        upcoming = self._period_totals(snapshot.get("upcoming", []))
        if upcoming:
            period, total = upcoming[-1]
            due_dates = sorted(
                {
                    item.get("due_date", "")
                    for item in snapshot.get("upcoming", [])
                    if item.get("period") == period
                }
            )
            due = format_date(due_dates[0]) if due_dates else "sin fecha"
            lines.extend(
                [
                    "",
                    "Próximo vencimiento",
                    f"{self._display_month(period)}: {format_money(total)} — {due}",
                ]
            )
        lines.extend(
            [
                "",
                f"Calculado por CCMA al {format_date(snapshot.get('calculated_on'))}.",
                f"Consultado: {format_datetime_argentina(snapshot.get('fetched_at'))}",
            ]
        )
        warning = self.source_warning(chat_id, "ARCA_CCMA")
        if warning:
            lines.extend(["", warning])
        return "\n".join(lines)

    def iibb_account(self, chat_id: int) -> str:
        if "SANTA_FE_IIBB" not in self.live_synchronizers:
            return self.source_command(chat_id, "SANTA_FE_IIBB")
        snapshot = self.available_snapshot(chat_id, "SANTA_FE_IIBB", "account_status")
        connection = self.connection_map(chat_id).get("SANTA_FE_IIBB")
        if snapshot is None:
            if connection and connection["status"] == "CREDENTIALS_REQUIRED":
                return "ARCA rechazó la clave fiscal guardada. Hay que volver a cargarla."
            if connection and connection["status"] == "LOGIN_REQUIRED":
                return "Falta guardar la clave fiscal con /configurar_arca."
            return self.source_command(chat_id, "SANTA_FE_IIBB")

        lines = [
            "Ingresos Brutos de Santa Fe",
            "",
            f"{snapshot.get('regime', 'Régimen Simplificado')}: {(snapshot.get('status') or 'sin dato').lower()}",
            f"Categoría {snapshot.get('category', 'sin dato')} — {format_money(snapshot.get('monthly_amount'))} por mes",
            f"Cuenta: {snapshot.get('account', 'sin dato')}",
            f"Adhesión: {format_date(snapshot.get('adhesion_date'))}",
        ]
        vepai = snapshot.get("vepai", {})
        vepai_payments = vepai.get("payments", []) if isinstance(vepai, dict) else []
        registered_payments = snapshot.get("payments", [])
        payments = vepai_payments or registered_payments
        reconciliation = vepai.get("reconciliation", {}) if isinstance(vepai, dict) else {}
        paid_iibb_periods = self._paid_iibb_periods(chat_id)
        missing_periods = {
            str(period)
            for period in reconciliation.get("missing_periods", [])
            if str(period) not in paid_iibb_periods
        }
        if reconciliation.get("status") == "COMPLETE" or (
            reconciliation.get("status") == "MISSING" and not missing_periods
        ):
            lines.extend(["", "Deuda vencida en Régimen Simplificado: no."])
            if reconciliation.get("status") == "MISSING" and paid_iibb_periods:
                lines.append(
                    "El pago está confirmado; Santa Fe todavía no terminó de actualizar la cuenta."
                )
        elif missing_periods:
            lines.extend(["", "Deuda vencida: hay períodos sin pago identificado."])

        next_obligation = snapshot.get("next_obligation")
        if (
            isinstance(next_obligation, dict)
            and str(next_obligation.get("period", "")) not in paid_iibb_periods
        ):
            title = "Pago vencido" if next_obligation.get("overdue") else "Próximo pago"
            due_label = "Venció" if next_obligation.get("overdue") else "Vence"
            lines.extend(
                [
                    "",
                    title,
                    (
                        f"{self._display_month(str(next_obligation.get('period', '')))}: "
                        f"{format_money(next_obligation.get('amount'))}"
                    ),
                    f"{due_label}: {format_date(str(next_obligation.get('due_date', '')))}",
                ]
            )
        if payments:
            source = "VEPAI" if vepai_payments else "Régimen Simplificado"
            if vepai_payments and isinstance(vepai, dict) and vepai.get("payment_groups"):
                groups = vepai["payment_groups"]
                latest_date = max(str(group.get("paid_on", "")) for group in groups)
                latest_groups = [
                    group for group in groups if str(group.get("paid_on", "")) == latest_date
                ]
                latest_total = sum(
                    (Decimal(str(group.get("total", "0"))) for group in latest_groups),
                    Decimal("0"),
                )
                latest_periods = sorted(
                    {
                        str(period)
                        for group in latest_groups
                        for period in group.get("periods", [])
                    }
                )
                lines.extend(
                    [
                        "",
                        f"Períodos con pagos en {source}: {len({str(item.get('period')) for item in payments})}",
                        f"Último pago: {format_money(latest_total)} el {format_date(latest_date)}",
                        f"Períodos incluidos en ese pago: {len(latest_periods)}",
                    ]
                )
            else:
                latest = max(
                    payments,
                    key=lambda item: (str(item.get("paid_on", "")), str(item.get("period", ""))),
                )
                lines.extend(
                    [
                        "",
                        f"Pagos que muestra {source}: {len(payments)}",
                        (
                            f"Último: {self._display_month(str(latest.get('period', '')))} — "
                            f"{format_money(latest.get('amount'))}, pagado el "
                            f"{format_date(latest.get('paid_on'))}"
                        ),
                    ]
                )
        payments_as_of = snapshot.get("payments_as_of")
        if payments_as_of and not vepai_payments:
            lines.append(f"Registro de pagos actualizado al {format_date(str(payments_as_of))}.")

        if isinstance(vepai, dict) and vepai.get("status") == "MAINTENANCE":
            lines.extend(
                [
                    "",
                    "VEPAI está en mantenimiento.",
                    "Hasta que responda no puedo confirmar si todos los pagos quedaron imputados ni si existe saldo pendiente.",
                ]
            )
        elif isinstance(vepai, dict) and vepai.get("status") == "AVAILABLE":
            if reconciliation.get("status") == "COMPLETE":
                lines.extend(
                    [
                        "",
                        (
                            "Pagos conciliados en VEPAI hasta "
                            f"{self._display_month(str(reconciliation.get('through', '')))}."
                        ),
                    ]
                )
            elif reconciliation.get("status") == "MISSING":
                missing = list(reconciliation.get("missing_periods", []))
                detail = ", ".join(self._display_month(period) for period in missing[:6])
                if len(missing) > 6:
                    detail += f" y {len(missing) - 6} más"
                lines.extend(
                    [
                        "",
                        f"VEPAI no muestra pagos para: {detail}.",
                        "Esto requiere revisión antes de generar cualquier pago.",
                    ]
                )
        lines.extend(["", f"Consultado: {format_datetime_argentina(snapshot.get('fetched_at'))}"])
        warning = self.source_warning(chat_id, "SANTA_FE_IIBB")
        if warning:
            lines.extend(["", warning])
        return "\n".join(lines)

    def arca_payments(self, chat_id: int) -> str:
        if "ARCA_SETI" not in self.live_synchronizers:
            return self.source_command(chat_id, "ARCA_SETI")
        snapshot = self.available_snapshot(chat_id, "ARCA_SETI", "payment_status")
        if snapshot is None:
            return self.source_command(chat_id, "ARCA_SETI")

        veps = list(snapshot.get("veps", []))
        if not veps:
            return "SETI no informó VEP generados durante los últimos 12 meses."
        lines = ["Pagos de ARCA"]
        consolidated = next(
            (
                vep
                for vep in veps
                if vep.get("is_consolidated")
                and str(vep.get("status", "")).upper() == "PAID"
            ),
            None,
        )
        if consolidated is not None:
            lines.extend(
                [
                    "",
                    "Último pago consolidado",
                    f"• VEP: {consolidated.get('number', 'sin número')}",
                    f"• Importe: {format_money(consolidated.get('amount'))}",
                    f"• Pagado: {format_date(str(consolidated.get('paid_on', '')))}",
                ]
            )

        reconciliation = snapshot.get("ccma_reconciliation", {})
        if isinstance(reconciliation, dict) and reconciliation:
            matched = int(reconciliation.get("matched_veps", 0))
            paid = int(reconciliation.get("paid_veps", 0))
            lines.extend(
                [
                    "",
                    "Cómo aparece en tu cuenta de monotributo",
                    f"• SETI confirma {paid} obligaciones pagadas.",
                    f"• CCMA muestra el movimiento de {matched} de esas {paid} obligaciones.",
                    "• Saldo deudor que CCMA informa hoy: "
                    f"{format_money(reconciliation.get('ccma_debtor_total'))}",
                ]
            )
            unmatched = [
                vep
                for vep in veps
                if str(vep.get("status", "")).upper() == "PAID"
                and not vep.get("is_consolidated")
                and vep.get("ccma_match") is False
            ]
            if unmatched:
                lines.extend(["", "Pagadas en SETI, todavía sin movimiento visible en CCMA"])
                for vep in unmatched:
                    description = str(vep.get("description") or "")
                    label = description
                    if (
                        description.startswith("ARCA")
                        and len(description) == 9
                        and description[4:6].isdigit()
                        and description[7:9].isdigit()
                    ):
                        label = self._display_month(
                            f"20{description[7:9]}-{description[4:6]}"
                        ).capitalize()
                    lines.append(
                        f"• {label or 'Obligación'} — VEP {vep.get('number', '')}: "
                        f"{format_money(vep.get('amount'))}"
                    )
                lines.extend(
                    [
                        "",
                        "Esto no significa que debas pagarlas otra vez: SETI ya confirma el pago.",
                    ]
                )
        lines.extend(["", f"Consultado: {format_datetime_argentina(snapshot.get('fetched_at'))}"])
        warning = self.source_warning(chat_id, "ARCA_SETI")
        if warning:
            lines.extend(["", warning])
        return "\n".join(lines)

    def _next_payment_items(self, chat_id: int) -> list[tuple[str, str, str, Decimal]]:
        items: list[tuple[str, str, str, Decimal]] = []
        ccma = self.valid_snapshot(chat_id, "ARCA_CCMA", "account_status")
        if ccma is not None:
            grouped: dict[tuple[str, str], Decimal] = {}
            for item in ccma.get("upcoming", []):
                key = (str(item.get("period", "")), str(item.get("due_date", "")))
                try:
                    grouped[key] = grouped.get(key, Decimal("0")) + Decimal(
                        str(item.get("balance", "0"))
                    )
                except InvalidOperation:
                    continue
            items.extend(
                (due_date, "Monotributo", period, amount)
                for (period, due_date), amount in grouped.items()
            )

        iibb = self.valid_snapshot(chat_id, "SANTA_FE_IIBB", "account_status")
        if iibb is not None and isinstance(iibb.get("next_obligation"), dict):
            obligation = iibb["next_obligation"]
            if str(obligation.get("period", "")) not in self._paid_iibb_periods(chat_id):
                try:
                    amount = Decimal(str(obligation.get("amount", "0")))
                except InvalidOperation:
                    amount = Decimal("0")
                items.append(
                    (
                        str(obligation.get("due_date", "")),
                        "Ingresos Brutos",
                        str(obligation.get("period", "")),
                        amount,
                    )
                )
        return sorted(items, key=lambda item: (item[0], item[1]))

    def _confirmed_payment_pending(self, chat_id: int) -> dict[str, object] | None:
        payment_snapshot = self.valid_snapshot(chat_id, "ARCA_SETI", "payment_status")
        account_snapshot = self.valid_snapshot(chat_id, "ARCA_CCMA", "account_status")
        if payment_snapshot is None or account_snapshot is None:
            return None
        reconciliation = payment_snapshot.get("ccma_reconciliation", {})
        if not isinstance(reconciliation, dict):
            return None
        if (
            str(reconciliation.get("status", "")).upper() != "PARTIAL"
            or int(reconciliation.get("unmatched_veps", 0)) < 1
        ):
            return None
        consolidated = next(
            (
                item
                for item in payment_snapshot.get("veps", [])
                if item.get("is_consolidated")
                and str(item.get("status", "")).upper() == "PAID"
            ),
            None,
        )
        if consolidated is None:
            return None
        try:
            paid_on = date.fromisoformat(str(consolidated.get("paid_on", "")))
            amount = Decimal(str(consolidated.get("amount", "0")))
            balance = Decimal(str(account_snapshot.get("debtor_total", "0")))
        except (InvalidOperation, ValueError):
            return None
        if balance <= 0:
            return None
        return {
            "paid_on": paid_on,
            "amount": amount,
            "balance": balance,
            "age_days": (self.today - paid_on).days,
        }

    def _plain_source_alerts(self, chat_id: int) -> list[str]:
        now = self.now
        identity = self.db.get_tax_identity(chat_id)
        portal_access_matches = identity is not None and self.db.portal_access_matches(
            chat_id,
            str(identity["represented_cuit"]),
        )
        portal_needs_credentials = self._portal_access_enabled() and not portal_access_matches
        if portal_needs_credentials:
            return [
                "Falta completar el acceso a Monotributo e Ingresos Brutos. "
                "Abrí /resumen y elegí Completar acceso."
            ]
        alerts: list[str] = []
        for provider in SOURCE_POLICIES:
            if provider not in self.live_synchronizers:
                continue
            health = source_health(self.db, chat_id, provider, now)
            if health.condition in {SourceCondition.HEALTHY, SourceCondition.PENDING}:
                continue
            alerts.append(render_health_brief(health))
        return alerts

    def _summary_keyboard(self, chat_id: int) -> tuple[tuple[tuple[str, str], ...], ...]:
        rows: list[tuple[tuple[str, str], ...]] = []
        identity = self.db.get_tax_identity(chat_id)
        if (
            self._portal_access_enabled()
            and self.command_catalog.is_available("configurar_arca", chat_id)
            and identity is not None
            and not self.db.portal_access_matches(
                chat_id,
                str(identity["represented_cuit"]),
            )
        ):
            rows.append((("Completar acceso", "tax:setup"),))
        if self.command_catalog.is_available("vencimientos", chat_id):
            rows.append((("Pagos y vencimientos", "tax:payments"),))
        if any(
            self.command_catalog.is_available(command, chat_id)
            for command in ("categoria", "facturacion")
        ):
            rows.append((("Categoría y facturación", "tax:category"),))
        if self.command_catalog.is_available("bandeja", chat_id):
            rows.append((("Mensajes de ARCA", "tax:inbox"),))
        if self.command_catalog.is_available("facturas", chat_id):
            rows.append((("Facturas", "inv:list:0"),))
        rows.append((("Estado de fuentes", "tax:status"),))
        return tuple(rows)

    @staticmethod
    def _back_keyboard() -> tuple[tuple[tuple[str, str], ...], ...]:
        return ((("Volver al resumen", "tax:summary"),),)

    def summary(self, chat_id: int) -> str:
        identity = self.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            return "Para empezar, conectá y verificá tu cuenta de ARCA con /conectar."

        lines = ["Resumen"]
        attention: list[str] = []
        clear_statuses: list[str] = []
        source_alerts = self._plain_source_alerts(chat_id)
        pending = self._confirmed_payment_pending(chat_id)
        ccma = self.valid_snapshot(chat_id, "ARCA_CCMA", "account_status")
        payment_snapshot = self.valid_snapshot(chat_id, "ARCA_SETI", "payment_status")
        if pending is not None:
            attention.append(
                "⚠️ Tu pago de Monotributo del "
                f"{format_date(pending['paid_on'].isoformat())} por "
                f"{format_money(pending['amount'])} está confirmado.\n"
                f"ARCA todavía muestra un saldo de {format_money(pending['balance'])} "
                "en la cuenta. No vuelvas a pagarlo por ahora; te avisaré cuando cambie."
            )
        elif ccma is not None:
            debt, processing = self._ccma_balance_buckets(ccma)
            tracking = self._ccma_tracking_details(ccma)
            if tracking is not None and tracking["status"] == "INTEREST_ONLY":
                attention.append(
                    "🟡 ARCA ya registró el capital de Monotributo de "
                    f"{self._display_month(str(tracking['period']))}, pero todavía deja "
                    f"{format_money(tracking['interest'])} de intereses. Requiere revisar "
                    "la fecha de imputación antes de pagarlos."
                )
            elif debt > 0 and tracking is not None and Decimal(
                str(tracking["interest"])
            ) > 0:
                attention.append(
                    f"🔴 ARCA todavía mantiene {format_money(tracking['balance'])} "
                    f"de Monotributo de {self._display_month(str(tracking['period']))}: "
                    f"{format_money(tracking['principal_outstanding'])} de capital y "
                    f"{format_money(tracking['interest'])} de intereses. Si ya se debitó, "
                    "no vuelvas a pagarlo; seguimos la imputación."
                )
            elif debt > 0:
                if "ARCA_SETI" in self.live_synchronizers and payment_snapshot is None:
                    attention.append(
                        f"⚠️ La cuenta muestra {format_money(debt)}, pero no pude verificar "
                        "los pagos recientes. Revisalo antes de pagar."
                    )
                else:
                    attention.append(f"🔴 Tenés {format_money(debt)} de Monotributo vencido.")
            elif processing > 0:
                attention.append(
                    f"🟡 ARCA todavía muestra {format_money(processing)} de Monotributo "
                    "pendiente; el débito automático puede estar procesándose."
                )
            else:
                clear_statuses.append("🟢 Sin deuda vencida de Monotributo.")
        elif (
            "ARCA_CCMA" in self.live_synchronizers
            and not any("Monotributo" in item for item in source_alerts)
        ):
            attention.append("⚠️ No pude confirmar el estado de Monotributo.")

        iibb = self.valid_snapshot(chat_id, "SANTA_FE_IIBB", "account_status")
        if iibb is not None:
            reconciliation = iibb.get("vepai", {}).get("reconciliation", {})
            if isinstance(reconciliation, dict) and reconciliation.get("status") == "MISSING":
                paid_periods = self._paid_iibb_periods(chat_id)
                missing = [
                    period
                    for period in reconciliation.get("missing_periods", [])
                    if str(period) not in paid_periods
                ]
                if missing:
                    attention.append(
                        f"🔴 Ingresos Brutos tiene {len(missing)} período(s) sin pago identificado."
                    )
                else:
                    attention.append(
                        "🟡 El pago de Ingresos Brutos está confirmado; Santa Fe todavía "
                        "no terminó de actualizar la cuenta. No vuelvas a pagarlo."
                    )
            elif isinstance(reconciliation, dict) and reconciliation.get("status") == "COMPLETE":
                clear_statuses.append("🟢 Sin deuda vencida de Ingresos Brutos.")
        elif (
            "SANTA_FE_IIBB" in self.live_synchronizers
            and not any("Ingresos Brutos" in item for item in source_alerts)
        ):
            attention.append("⚠️ No pude confirmar el estado de Ingresos Brutos.")

        next_items = self._next_payment_items(chat_id)
        if next_items:
            grouped: dict[str, dict[str, object]] = {}
            for due_date, label, _period, amount in next_items:
                entry = grouped.setdefault(
                    due_date,
                    {"total": Decimal("0"), "labels": []},
                )
                entry["total"] = Decimal(str(entry["total"])) + amount
                labels = entry["labels"]
                if isinstance(labels, list) and label not in labels:
                    labels.append(label)

            block = ["Próximos vencimientos"]
            for due_date, entry in sorted(grouped.items()):
                try:
                    due = date.fromisoformat(due_date)
                    days = (due - self.today).days
                    timing = "hoy" if days == 0 else (
                        "mañana" if days == 1 else (
                            f"dentro de {days} días" if days > 1 else f"hace {-days} días"
                        )
                    )
                    due_label = f"{WEEKDAYS[due.weekday()]} {format_date(due_date)}"
                except ValueError:
                    timing = "fecha sin verificar"
                    due_label = format_date(due_date)
                labels = entry["labels"] if isinstance(entry["labels"], list) else []
                label_text = " y ".join(str(label) for label in labels)
                block.extend(["", f"{due_label} — {timing}"])
                if len(labels) > 1:
                    block.extend(
                        [
                            f"• Total: {format_money(entry['total'])}",
                            f"• Incluye: {label_text}",
                        ]
                    )
                else:
                    block.append(f"• {label_text}: {format_money(entry['total'])}")
            upcoming = "\n".join(block)
        else:
            upcoming = ""

        inbox = self.valid_snapshot(chat_id, "ARCA_DFE", "unread_communications")
        if inbox is not None and inbox.get("communications"):
            count = len(inbox["communications"])
            attention.append(
                f"📩 Tenés {count} mensaje{'s' if count != 1 else ''} nuevo"
                f"{'s' if count != 1 else ''} de ARCA."
            )
        elif inbox is not None:
            clear_statuses.append("🟢 Sin mensajes nuevos de ARCA.")

        profile = self.profile_snapshot(chat_id)
        invoices = self.invoice_snapshot(chat_id)
        if profile is not None and invoices is not None:
            current_description = next(
                (
                    str(item.get("description") or item.get("category_id") or "").strip()
                    for item in profile.get("categories", [])
                    if str(item.get("status", "")).upper() in {"", "ACTIVO"}
                ),
                "",
            )
            current = category_letter(current_description)
            projection = self.projection_lines(invoices)
            projected = projection[1].split(" — ", 1)[0] if len(projection) > 1 else ""
            if current and projected and len(projected) == 1:
                if projected != current:
                    direction = "subir" if projected > current else "bajar"
                    attention.append(
                        f"⚠️ Con tu facturación actual podrías {direction} de la categoría "
                        f"{current} a la {projected}."
                    )
                else:
                    clear_statuses.append(
                        f"🟢 Categoría {current}; la proyección sigue en {projected}."
                    )

        attention.extend(f"⚠️ {item}" for item in source_alerts)
        if attention:
            lines.extend(["", "Necesita atención"])
            for item in attention:
                lines.extend(["", item])
        else:
            lines.extend(["", "🟢 Todo en orden"])
        if clear_statuses:
            lines.extend(["", *clear_statuses])
        if upcoming:
            lines.extend(["", upcoming])
        lines.extend(
            [
                "",
                "Este resumen cubre Monotributo, Ingresos Brutos de Santa Fe, "
                "facturación y mensajes de ARCA.",
            ]
        )
        return "\n".join(lines)

    def payments_overview(self, chat_id: int) -> str:
        lines = ["Pagos y vencimientos"]
        items = self._next_payment_items(chat_id)
        if items:
            lines.extend(["", "Qué tenés que pagar"])
            totals: dict[str, Decimal] = {}
            for due_date, label, period, amount in items:
                try:
                    parsed = date.fromisoformat(due_date)
                    due_label = f"{WEEKDAYS[parsed.weekday()]} {format_date(due_date)}"
                    verb = "Vence" if parsed >= self.today else "Venció"
                except ValueError:
                    due_label = format_date(due_date)
                    verb = "Fecha"
                lines.extend(
                    [
                        "",
                        label,
                        f"• Período: {self._display_month(period)}",
                        f"• Importe: {format_money(amount)}",
                        f"• {verb}: {due_label}",
                    ]
                )
                totals[due_date] = totals.get(due_date, Decimal("0")) + amount
            for due_date, total in totals.items():
                if sum(1 for item in items if item[0] == due_date) > 1:
                    lines.append(f"Total del {format_date(due_date)}: {format_money(total)}")
        else:
            ccma_available = self.valid_snapshot(chat_id, "ARCA_CCMA", "account_status")
            iibb_available = self.valid_snapshot(chat_id, "SANTA_FE_IIBB", "account_status")
            if (
                "ARCA_CCMA" in self.live_synchronizers
                and ccma_available is None
            ) or (
                "SANTA_FE_IIBB" in self.live_synchronizers
                and iibb_available is None
            ):
                lines.extend(["", "No pude confirmar todos los próximos pagos."])
            else:
                lines.extend(["", "No encontré próximos pagos."])

        lines.extend(["", "Estado de los pagos anteriores"])
        pending = self._confirmed_payment_pending(chat_id)
        ccma = self.valid_snapshot(chat_id, "ARCA_CCMA", "account_status")
        if pending is not None:
            lines.extend(
                [
                    "",
                    "Monotributo",
                    (
                        f"• Pago confirmado el {format_date(pending['paid_on'].isoformat())}: "
                        f"{format_money(pending['amount'])}."
                    ),
                    f"• La cuenta todavía muestra {format_money(pending['balance'])}.",
                ]
            )
            if int(pending["age_days"]) <= 7:
                lines.append(
                    "• La cuenta todavía se está actualizando. No vuelvas a pagar ese saldo."
                )
            else:
                lines.append(
                    "• El saldo requiere revisión, pero el pago sigue confirmado. No lo pagues nuevamente."
                )
        elif ccma is not None:
            debt, processing = self._ccma_balance_buckets(ccma)
            tracking = self._ccma_tracking_details(ccma)
            lines.extend(
                [
                    "",
                    "Monotributo",
                    (
                        (
                            "• ARCA ya registró el capital, pero todavía deja "
                            f"{format_money(tracking['interest'])} de intereses."
                        )
                        if tracking is not None
                        and tracking["status"] == "INTEREST_ONLY"
                        else (
                            f"• ARCA informa {format_money(tracking['principal_outstanding'])} "
                            "de capital y "
                            f"{format_money(tracking['interest'])} de intereses pendientes."
                        )
                        if debt
                        and tracking is not None
                        and Decimal(str(tracking["interest"])) > 0
                        else f"• Deuda vencida: {format_money(debt)}."
                        if debt
                        else (
                            f"• ARCA todavía muestra {format_money(processing)} pendiente; "
                            "el débito automático puede estar procesándose."
                            if processing
                            else "• Sin deuda vencida."
                        )
                    ),
                ]
            )

        iibb = self.valid_snapshot(chat_id, "SANTA_FE_IIBB", "account_status")
        if iibb is not None:
            reconciliation = iibb.get("vepai", {}).get("reconciliation", {})
            groups = iibb.get("vepai", {}).get("payment_groups", [])
            lines.extend(["", "Ingresos Brutos"])
            if isinstance(reconciliation, dict) and reconciliation.get("status") == "COMPLETE":
                lines.append("• Sin deuda vencida.")
            elif isinstance(reconciliation, dict) and reconciliation.get("status") == "MISSING":
                paid_periods = self._paid_iibb_periods(chat_id)
                missing = [
                    period
                    for period in reconciliation.get("missing_periods", [])
                    if str(period) not in paid_periods
                ]
                lines.append(
                    "• Hay períodos sin un pago identificado."
                    if missing
                    else "• Pago confirmado; pendiente de actualización provincial."
                )
            if groups:
                latest_date = max(str(group.get("paid_on", "")) for group in groups)
                latest_total = sum(
                    (
                        Decimal(str(group.get("total", "0")))
                        for group in groups
                        if str(group.get("paid_on", "")) == latest_date
                    ),
                    Decimal("0"),
                )
                lines.append(
                    f"• Último pago: {format_money(latest_total)} el {format_date(latest_date)}."
                )
        issues = self._plain_source_alerts(chat_id)
        if issues:
            lines.extend(["", "Datos que necesitan actualización"])
            lines.extend(f"• {issue}" for issue in issues)
        return "\n".join(lines)

    def category_overview(self, chat_id: int) -> str:
        profile_error = self.sync_provider(chat_id, "ARCA_PROFILE")
        invoice_error = self.sync_provider(chat_id, "ARCA_INVOICES")
        profile = self.profile_snapshot(chat_id)
        invoices = self.invoice_snapshot(chat_id)
        if profile is None or invoices is None:
            return (
                profile_error
                or invoice_error
                or "Todavía no pude obtener tu categoría y facturación."
            )

        current_description = next(
            (
                str(item.get("description") or item.get("category_id") or "sin dato")
                for item in profile.get("categories", [])
                if str(item.get("status", "")).upper() in {"", "ACTIVO"}
            ),
            "sin dato",
        )
        current = category_letter(current_description)
        current_display = format_category(current_description)
        try:
            total = Decimal(str(invoices.get("total_pesos", "0")))
        except InvalidOperation:
            total = Decimal("0")
        history_complete = invoices.get(
            "history_complete",
            invoices.get("manual_history_complete", False),
        ) is True
        monthly_totals: dict[str, Decimal] = {}
        for item in invoices.get("monthly_totals", []):
            period = str(item.get("period", ""))
            if not period:
                continue
            try:
                monthly_totals[period] = Decimal(str(item.get("total", "0")))
            except InvalidOperation:
                continue
        if not monthly_totals:
            for invoice in invoices.get("invoices", []):
                period = str(invoice.get("issued_at", ""))[:7]
                if len(period) != 7:
                    continue
                try:
                    amount = Decimal(str(invoice.get("net_total_pesos", "0")))
                except InvalidOperation:
                    continue
                monthly_totals[period] = monthly_totals.get(period, Decimal("0")) + amount
        average_divisor = 12 if history_complete else len(monthly_totals)
        average = total / average_divisor if average_divisor else None
        projection = self.projection_lines(invoices)
        projection_detail = projection[1] if len(projection) > 1 else ""
        projected_candidate = projection_detail.split(" — ", 1)[0]
        pace_detail = (
            projection_detail.split(" — ", 1)[1]
            if " — " in projection_detail
            else ""
        )
        projected = (
            projected_candidate
            if projected_candidate
            in {"A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"}
            else "Todavía no disponible"
        )
        if projected == "Todavía no disponible":
            conclusion = projection_detail or "Todavía no puedo determinar tu próxima categoría."
            projection_context = projection[2:4]
        elif not current:
            conclusion = "Todavía no puedo determinar tu categoría actual."
            projection_context = projection[2:4]
        elif projected == current:
            conclusion = (
                f"Si mantenés este ritmo, con los topes actuales seguirías en la "
                f"categoría {current}."
            )
            projection_context = ([pace_detail.capitalize()] if pace_detail else []) + projection[2:5]
        elif projected > current:
            conclusion = (
                f"Si mantenés este ritmo, con los topes actuales podrías subir de "
                f"{current} a {projected}."
            )
            projection_context = ([pace_detail.capitalize()] if pace_detail else []) + projection[2:5]
        else:
            conclusion = (
                f"Si mantenés este ritmo, con los topes actuales podrías bajar de "
                f"{current} a {projected}."
            )
            projection_context = ([pace_detail.capitalize()] if pace_detail else []) + projection[2:5]
        billing_lines = [
            (
                f"Facturación de los últimos 12 meses: {format_money(total)}"
                if history_complete
                else f"Facturación registrada en los últimos 12 meses: {format_money(total)}"
            ),
            (
                f"Promedio mensual: {format_money(average)}"
                if history_complete
                else (
                    f"Promedio mensual registrado ({average_divisor} "
                    f"{'mes' if average_divisor == 1 else 'meses'}): {format_money(average)}"
                    if average is not None
                    else "Promedio mensual registrado: todavía no disponible"
                )
            ),
            f"Meses con datos: {len(monthly_totals)} de 12",
        ]
        lines = [
                "Categoría y facturación",
                "",
                f"Categoría actual: {current_display}",
                (
                    f"Categoría proyectada: {projected} (con topes actuales)"
                    if len(projected) == 1
                    else f"Categoría proyectada: {projected}"
                ),
                "",
                *billing_lines,
                "",
                conclusion,
                *projection_context,
                f"Próxima revisión: {format_date(next_recategorization(self.today).isoformat())}",
            ]
        for provider in ("ARCA_PROFILE", "ARCA_INVOICES"):
            warning = self.source_warning(chat_id, provider)
            if warning:
                lines.extend(["", warning])
        return "\n".join(lines)

    def navigation(self, action: str, chat_id: int, *, replace: bool = False) -> BotReply:
        identity = self.db.get_tax_identity(chat_id)
        connected = identity is not None and identity["status"] == "VERIFIED"
        if action == "start":
            if connected:
                return BotReply(self.summary(chat_id), self._summary_keyboard(chat_id), replace)
            return BotReply(self.help(chat_id), replace_message=replace)
        if action == "help":
            rows: list[tuple[tuple[str, str], ...]] = [
                (("Ver resumen", "tax:summary"),),
                (("Estado de fuentes", "tax:status"),),
            ]
            if self.command_catalog.is_available("facturas", chat_id):
                rows.append((("Facturas", "inv:list:0"),))
            if self.command_catalog.is_available("configurar_arca", chat_id):
                rows.append((("Configurar acceso a ARCA", "tax:setup"),))
            keyboard = tuple(rows)
            return BotReply(self.help(chat_id), keyboard, replace)
        if action == "summary":
            return BotReply(
                self.summary(chat_id),
                self._summary_keyboard(chat_id) if connected else (),
                replace,
            )
        if action == "status":
            return BotReply(self.status(chat_id), self._back_keyboard(), replace)
        if not connected:
            return BotReply(
                "Tu cuenta está desconectada. Usá /conectar seguido de tu CUIT.",
                replace_message=replace,
            )
        if action == "setup":
            response = self.configure_arca(chat_id)
            setup_url = next(
                (line for line in response.splitlines() if line.startswith(("http://", "https://"))),
                "",
            )
            keyboard = (("Abrir configuración de ARCA", setup_url),) if setup_url else ()
            return BotReply(response, (keyboard,) if keyboard else (), replace)
        if action == "payments":
            keyboard = (
                (("Pagar Ingresos Brutos", "iibbpay:start"),),
                (("Volver al resumen", "tax:summary"),),
            )
            return BotReply(self.payments_overview(chat_id), keyboard, replace)
        if action == "category":
            return BotReply(self.category_overview(chat_id), self._back_keyboard(), replace)
        if action == "inbox":
            return BotReply(self.inbox(chat_id), self._back_keyboard(), replace)
        raise ValueError("Esa opción ya no está disponible")

    def due_dates(self, chat_id: int) -> str:
        items: list[tuple[str, str, str, Decimal]] = []
        ccma = self.valid_snapshot(chat_id, "ARCA_CCMA", "account_status")
        if ccma is not None:
            grouped: dict[tuple[str, str], Decimal] = {}
            for item in ccma.get("upcoming", []):
                key = (str(item.get("period", "")), str(item.get("due_date", "")))
                try:
                    grouped[key] = grouped.get(key, Decimal("0")) + Decimal(
                        str(item.get("balance", "0"))
                    )
                except InvalidOperation:
                    continue
            for (period, due_date), amount in grouped.items():
                items.append((due_date, "Monotributo", period, amount))

        iibb = self.valid_snapshot(chat_id, "SANTA_FE_IIBB", "account_status")
        if iibb is not None and isinstance(iibb.get("next_obligation"), dict):
            obligation = iibb["next_obligation"]
            if str(obligation.get("period", "")) not in self._paid_iibb_periods(chat_id):
                try:
                    amount = Decimal(str(obligation.get("amount", "0")))
                except InvalidOperation:
                    amount = Decimal("0")
                items.append(
                    (
                        str(obligation.get("due_date", "")),
                        "Ingresos Brutos",
                        str(obligation.get("period", "")),
                        amount,
                    )
                )

        lines = ["Próximos pagos"]
        if not items:
            lines.extend(["", "No pude obtener próximos pagos de las fuentes conectadas."])
        else:
            totals_by_date: dict[str, Decimal] = {}
            counts_by_date: dict[str, int] = {}
            for due_date, _label, _period, amount in items:
                totals_by_date[due_date] = totals_by_date.get(due_date, Decimal("0")) + amount
                counts_by_date[due_date] = counts_by_date.get(due_date, 0) + 1

            for due_date, label, period, amount in sorted(items, key=lambda item: (item[0], item[1])):
                try:
                    parsed_due_date = date.fromisoformat(due_date)
                    days = (parsed_due_date - self.today).days
                    due_label = f"{WEEKDAYS[parsed_due_date.weekday()]} {format_date(due_date)}"
                except ValueError:
                    days = None
                    due_label = format_date(due_date)
                if days == 0:
                    timing = "hoy"
                elif days == 1:
                    timing = "mañana"
                elif days is not None and days > 1:
                    timing = f"dentro de {days} días"
                elif days == -1:
                    timing = "ayer"
                elif days is not None:
                    timing = f"hace {-days} días"
                else:
                    timing = "sin plazo calculado"
                verb = "Vence" if days is None or days >= 0 else "Venció"
                lines.extend(
                    [
                        "",
                        label,
                        f"• Período: {self._display_month(period)}",
                        f"• {verb}: {due_label} ({timing})",
                        f"• Importe: {format_money(amount)}",
                    ]
                )

            for due_date in sorted(totals_by_date):
                if counts_by_date[due_date] > 1:
                    lines.extend(
                        [
                            "",
                            f"Total con vencimiento el {format_date(due_date)}: "
                            f"{format_money(totals_by_date[due_date])}",
                        ]
                    )

        previous_status: list[str] = []
        if iibb is not None:
            vepai = iibb.get("vepai", {})
            reconciliation = vepai.get("reconciliation", {}) if isinstance(vepai, dict) else {}
            if reconciliation.get("status") == "COMPLETE":
                detail = (
                    "Ingresos Brutos\n"
                    "• Sin deuda vencida en VEPAI.\n"
                    "• Pagos imputados hasta "
                    f"{self._display_month(str(reconciliation.get('through', '')))}."
                )
                groups = vepai.get("payment_groups", []) if isinstance(vepai, dict) else []
                if groups:
                    latest_date = max(str(group.get("paid_on", "")) for group in groups)
                    latest_total = sum(
                        (
                            Decimal(str(group.get("total", "0")))
                            for group in groups
                            if str(group.get("paid_on", "")) == latest_date
                        ),
                        Decimal("0"),
                    )
                    detail += (
                        f"\n• Último pago imputado: {format_money(latest_total)} "
                        f"el {format_date(latest_date)}"
                    )
                previous_status.append(detail)

        if ccma is not None:
            try:
                debtor_total = Decimal(str(ccma.get("debtor_total", "0")))
                latest_total = Decimal(str(ccma.get("latest_payment_total", "0")))
            except InvalidOperation:
                debtor_total = latest_total = Decimal("0")
            latest_date = str(ccma.get("latest_payment_date", ""))
            if ccma.get("payment_reconciliation") == "PARTIAL" and latest_date:
                previous_status.append(
                    "Monotributo\n"
                    f"• Movimientos identificados como pago en CCMA el {format_date(latest_date)}: "
                    f"{format_money(latest_total)}\n"
                    f"• Saldo deudor que CCMA todavía informa: {format_money(debtor_total)}"
                )
            elif not debtor_total:
                detail = "Monotributo\n• Sin deuda vencida en CCMA."
                if latest_date and latest_total:
                    detail += (
                        f"\n• Último pago visible: {format_money(latest_total)} "
                        f"el {format_date(latest_date)}"
                    )
                previous_status.append(detail)
            else:
                previous_status.append(
                    "Monotributo\n"
                    f"• Saldo deudor informado por CCMA: {format_money(debtor_total)}"
                )

        if previous_status:
            lines.extend(["", "Pagos anteriores"])
            for status in previous_status:
                lines.extend(["", status])

        recategorization = next_recategorization(self.today)
        lines.extend(
            [
                "",
                "Próxima revisión de categoría",
                format_date(recategorization.isoformat()),
            ]
        )
        return "\n".join(lines)

    def sync_provider(self, chat_id: int, provider: str) -> str | None:
        synchronizer = self.live_synchronizers.get(provider)
        if synchronizer is None:
            return None
        identity = self.db.get_tax_identity(chat_id)
        if identity is None:
            return "Primero conectá tu cuenta de ARCA con /conectar."
        if provider in {"ARCA_DFE", "ARCA_INVOICES"} and identity["status"] != "VERIFIED":
            return "La conexión con ARCA todavía está pendiente."
        try:
            synchronizer(chat_id)
        except WsfePointOfSaleUnavailable:
            LOGGER.info("El CUIT no tiene puntos de venta habilitados para WSFE")
            return (
                "Tus comprobantes de Comprobantes en línea no se pueden consultar "
                "por WSFE. Para calcular tu facturación automáticamente necesitamos "
                "usar un punto de venta Web Services."
            )
        except Exception:
            LOGGER.exception("Falló la consulta de %s", provider)
            return f"No pude consultar {PROVIDERS[provider].lower()} ahora. Probá de nuevo en unos minutos."
        return None

    def source_command(self, chat_id: int, provider: str) -> str:
        if provider not in self.live_synchronizers:
            return f"{PROVIDERS[provider]} todavía no está disponible."
        identity = self.db.get_tax_identity(chat_id)
        if not identity or identity["status"] != "VERIFIED":
            return "Primero conectá tu cuenta de ARCA con /conectar."
        return render_health_brief(
            source_health(self.db, chat_id, provider, self.now)
        )

    def disconnect(self, chat_id: int) -> str:
        with self.db.connect() as connection:
            connection.execute("DELETE FROM notification_deliveries WHERE chat_id = ?", (chat_id,))
            connection.execute("DELETE FROM live_snapshots WHERE chat_id = ?", (chat_id,))
            connection.execute("DELETE FROM connections WHERE chat_id = ?", (chat_id,))
            connection.execute("DELETE FROM onboarding_tokens WHERE chat_id = ?", (chat_id,))
            connection.execute("DELETE FROM invoice_conversation_state WHERE chat_id = ?", (chat_id,))
            connection.execute("DELETE FROM tax_identities WHERE chat_id = ?", (chat_id,))
        return (
            "Listo. Desvinculé tu cuenta y dejé de consultar y mostrar sus datos.\n\n"
            "Conservé las facturas emitidas y el acceso cifrado para que puedas "
            "reconectarla sin repetir toda la configuración."
        )

    def dispatch(self, text: str, chat_id: int) -> str:
        self.db.ensure_user(chat_id)
        parts = text.strip().split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        command_name = command.removeprefix("/")
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command_name not in self.command_catalog.command_names:
            return "Comando no reconocido. Usá /ayuda."
        if command == "/start":
            identity = self.db.get_tax_identity(chat_id)
            if identity is not None and identity["status"] == "VERIFIED":
                return self.summary(chat_id)
            return self.help(chat_id)
        if command in {"/ayuda", "/help"}:
            return self.help(chat_id)
        if command == "/conectar":
            return self.connect(chat_id, argument)
        if command == "/resumen":
            return self.summary(chat_id)
        if command == "/estado":
            return self.status(chat_id)
        if command == "/bandeja":
            return self.inbox(chat_id)
        if command == "/monotributo":
            return self.monotributo_account(chat_id)
        if command == "/configurar_arca":
            return self.configure_arca(chat_id)
        if command == "/facturacion":
            return self.invoicing(chat_id)
        if command == "/categoria":
            return self.category(chat_id)
        if command == "/deuda_arca":
            monotributo = self.monotributo_account(chat_id)
            return "\n".join(
                [
                    "Deuda con ARCA",
                    "",
                    monotributo,
                    "",
                    "Otros impuestos de ARCA todavía no están incorporados.",
                ]
            )
        if command == "/iibb":
            return self.iibb_account(chat_id)
        if command == "/pagos_arca":
            return self.arca_payments(chat_id)
        if command == "/vencimientos":
            return self.due_dates(chat_id)
        if command == "/desconectar":
            return self.disconnect(chat_id)
        if command_name in INVOICE_COMMAND_NAMES:
            reason = self.command_catalog.reason_for(command_name, chat_id)
            if reason:
                return f"/{command_name} {reason}."
            return f"/{command_name} está disponible únicamente desde Telegram."
        return "Comando no reconocido. Usá /ayuda."
