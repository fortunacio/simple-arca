from __future__ import annotations

import json
import hashlib
import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable
from zoneinfo import ZoneInfo

from .db import Database
from .arca.errors import WsfePointOfSaleUnavailable
from .identity import is_valid_cuit, mask_cuit, normalize_cuit
from .monotributo import (
    CategoryLimits,
    completed_months_between,
    next_recategorization,
    projected_category,
    projection_window,
)


PROVIDERS = {
    "ARCA_PROFILE": "Categoría y situación en ARCA",
    "ARCA_DFE": "Bandeja de ARCA",
    "ARCA_CCMA": "Deuda de monotributo",
    "ARCA_ACCOUNTS": "Otras deudas con ARCA",
    "ARCA_INVOICES": "Facturación",
    "SANTA_FE_IIBB": "Ingresos Brutos de Santa Fe",
}

PROVIDER_COMMANDS = {
    "ARCA_PROFILE": "/categoria",
    "ARCA_DFE": "/bandeja",
    "ARCA_CCMA": "/monotributo",
    "ARCA_INVOICES": "/facturacion",
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
LOGGER = logging.getLogger(__name__)


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
    ):
        self.db = db
        self.today = today or date.today()
        self.operator_cuit = normalize_cuit(operator_cuit)
        self.certificate_alias = certificate_alias.strip()
        self.arca_environment = arca_environment
        self.live_synchronizers = live_synchronizers or {}
        self.category_limits_provider = category_limits_provider
        self.ccma_login_url = ccma_login_url.strip()
        self.arca_enrollment_base_url = arca_enrollment_base_url.rstrip("/")
        self.arca_enrollment_ttl = arca_enrollment_ttl

    def help(self, chat_id: int) -> str:
        lines = ["¿Qué querés consultar?", ""]
        if self.db.get_tax_identity(chat_id) is None:
            lines.extend(["/conectar — vincular tu cuenta de ARCA", ""])
        lines.extend(
            [
                "/estado — resumen de tu cuenta",
                "/categoria — categoría actual de monotributo",
                "/facturacion — facturación de los últimos 12 meses",
                "/facturas — ver y repetir las últimas facturas",
                "/nueva_factura — crear una Factura C desde una plantilla",
                "/nuevo_cliente — guardar un cliente verificado en ARCA",
                "/bandeja — comunicaciones sin leer de ARCA",
                "/monotributo — deuda, créditos y próximo vencimiento en CCMA",
                "/configurar_arca — guardar o actualizar la clave fiscal de forma segura",
                "",
                "Todavía estamos trabajando en otras deudas de ARCA, vencimientos e Ingresos Brutos de Santa Fe.",
                "",
                "/desconectar — desvincular tu cuenta",
            ]
        )
        return "\n".join(lines)

    def configure_arca(self, chat_id: int) -> str:
        identity = self.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            return "Primero conectá y verificá tu cuenta de ARCA con /conectar."
        if not self.arca_enrollment_base_url:
            return "El alta web de ARCA todavía no está configurada en este servidor."
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        expires_at = datetime.now(timezone.utc) + self.arca_enrollment_ttl
        self.db.create_onboarding_token(digest, chat_id, expires_at.isoformat())
        return "\n".join(
            [
                "Configuración de ARCA",
                "",
                "Abrí este enlace privado y cargá la clave fiscal:",
                f"{self.arca_enrollment_base_url}/arca/setup/{token}",
                "",
                "El enlace vence en 10 minutos y puede usarse una sola vez.",
            ]
        )

    def connection_map(self, chat_id: int) -> dict[str, object]:
        return {row["provider"]: row for row in self.db.get_connections(chat_id)}

    def provider_status(self, chat_id: int, provider: str) -> str:
        if provider not in self.live_synchronizers:
            return f"⚪ {PROVIDERS[provider]} — todavía no disponible"
        row = self.connection_map(chat_id).get(provider)
        if not row:
            identity = self.db.get_tax_identity(chat_id)
            if identity and identity["status"] == "VERIFIED":
                command = PROVIDER_COMMANDS.get(provider, "/estado")
                return f"🟡 {PROVIDERS[provider]} — usá {command} para consultar"
            return f"⚪ {PROVIDERS[provider]} — falta conectar ARCA con /conectar"
        if row["status"] == "LOGIN_REQUIRED":
            return f"🟡 {PROVIDERS[provider]} — ARCA pidió una validación manual"
        if row["status"] == "CREDENTIALS_REQUIRED":
            return f"🔴 {PROVIDERS[provider]} — hay que actualizar la clave fiscal guardada"
        if row["status"] != "CONNECTED":
            return f"🔴 {PROVIDERS[provider]} — hubo un problema en la última consulta"
        observed = format_datetime_argentina(row["last_success_at"])
        return f"🟢 {PROVIDERS[provider]}\nActualizado: {observed}"

    def status(self, chat_id: int) -> str:
        lines = ["Estado de tu cuenta"]
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
                return "Tu cuenta de ARCA ya está conectada."
            if identity:
                return "La conexión con ARCA todavía está pendiente."
            return "Enviá /conectar seguido de tu CUIT. Por ejemplo: /conectar 20123456786"

        cuit = normalize_cuit(argument)
        if not is_valid_cuit(cuit):
            return "CUIT inválido. Revisá los 11 dígitos y volvé a intentar."
        if identity:
            if identity["represented_cuit"] == cuit:
                if identity["status"] == "VERIFIED":
                    return "Tu cuenta de ARCA ya está conectada."
                return "Ese CUIT ya está cargado y la conexión sigue pendiente."
            return "Ya hay otro CUIT vinculado. Usá /desconectar antes de cambiarlo."
        try:
            self.db.set_pending_tax_identity(chat_id, cuit, self.arca_environment)
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                return "Ese CUIT ya está vinculado a otro chat. Primero debe revocarse esa vinculación."
            raise

        operator = mask_cuit(self.operator_cuit) if self.operator_cuit else "pendiente de configurar"
        alias = self.certificate_alias or "pendiente de configurar"
        return f"""Listo, guardé el CUIT {mask_cuit(cuit)}.

Ahora falta autorizar el certificado {alias} en ARCA. La cuenta operadora es {operator}."""

    def valid_snapshot(self, chat_id: int, provider: str, kind: str) -> dict | None:
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

    def profile_snapshot(self, chat_id: int) -> dict | None:
        return self.valid_snapshot(chat_id, "ARCA_PROFILE", "taxpayer_profile")

    def invoice_snapshot(self, chat_id: int) -> dict | None:
        return self.valid_snapshot(chat_id, "ARCA_INVOICES", "issued_invoices")

    def projection_lines(self, snapshot: dict) -> list[str]:
        invoices = snapshot.get("invoices", [])
        if not invoices:
            return [
                "Categoría proyectada para recategorización",
                "No puedo calcularla porque WSFE no devolvió comprobantes.",
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

        total = Decimal("0")
        for invoice in invoices:
            try:
                issued_at = date.fromisoformat(invoice.get("issued_at", ""))
                amount = Decimal(str(invoice.get("net_total_pesos", "0")))
            except (InvalidOperation, ValueError):
                continue
            if target_from <= issued_at <= completed_to:
                total += amount
        average = total / months
        annual_projection = average * 12
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
            f"{projected} — promedio mensual de {format_money(average)}",
            f"Facturación anual proyectada: {format_money(annual_projection)}",
            f"Próxima recategorización: {format_month_year(recategorization)}",
            f"Topes de ARCA vigentes desde {format_date(category_limits.effective_from.isoformat())}.",
            "Esta proyección usa la facturación; ARCA también evalúa los demás parámetros del régimen.",
        ]
        if category_limits.verification_pending:
            lines.append(
                "No pude volver a verificar la tabla hoy; usé la última versión oficial guardada."
            )
        return lines

    def category(self, chat_id: int) -> str:
        error = self.sync_provider(chat_id, "ARCA_PROFILE")
        if error:
            return error
        profile = self.profile_snapshot(chat_id)
        if profile is None:
            return self.source_command(chat_id, "ARCA_PROFILE")
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
            invoice_snapshot = self.invoice_snapshot(chat_id) if not invoice_error else None
            if invoice_snapshot is not None:
                projection = self.projection_lines(invoice_snapshot)
            elif invoice_error:
                projection = [
                    "Categoría proyectada para recategorización",
                    invoice_error,
                ]
        return "\n".join(
            [
                "Tu categoría de monotributo",
                "",
                *category_lines,
                "",
                f"Estado en ARCA: {(profile.get('key_status') or 'sin dato').lower()}",
                f"Actualizado: {format_datetime_argentina(profile.get('fetched_at'))}",
                "",
                *projection,
            ]
        )

    def invoicing(self, chat_id: int) -> str:
        error = self.sync_provider(chat_id, "ARCA_INVOICES")
        if error:
            return error
        snapshot = self.invoice_snapshot(chat_id)
        if snapshot is None:
            return self.source_command(chat_id, "ARCA_INVOICES")
        count = int(snapshot.get("invoice_count", 0))
        if count == 0:
            return "ARCA no devolvió comprobantes en WSFE. Todavía no puedo calcular tu facturación."
        total = Decimal(str(snapshot.get("total_pesos", "0")))
        average = total / 12
        return "\n".join(
            [
                "Facturación",
                "",
                f"Últimos 12 meses completos: {format_money(total)}",
                f"Promedio mensual: {format_money(average)}",
                f"Comprobantes: {count}",
                f"Período: {format_date(snapshot.get('window_from'))} al {format_date(snapshot.get('window_to'))}",
                f"Actualizado: {format_datetime_argentina(snapshot.get('fetched_at'))}",
            ]
        )

    def inbox(self, chat_id: int) -> str:
        error = self.sync_provider(chat_id, "ARCA_DFE")
        if error:
            return error
        snapshot = self.valid_snapshot(
            chat_id, "ARCA_DFE", "unread_communications"
        )
        if snapshot is None:
            return self.source_command(chat_id, "ARCA_DFE")
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
        return "\n".join(
            [
                "Bandeja de ARCA",
                "",
                detail,
                "",
                f"Revisado: {format_datetime_argentina(snapshot.get('fetched_at'))}",
            ]
        )

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
        snapshot = self.valid_snapshot(chat_id, "ARCA_CCMA", "account_status")
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

        debtor_total = Decimal(str(snapshot.get("debtor_total", "0")))
        creditor_total = Decimal(str(snapshot.get("creditor_total", "0")))
        lines = ["Monotributo", ""]
        if debtor_total:
            lines.append(f"Deuda vencida: {format_money(debtor_total)}")
            for period, total in self._period_totals(snapshot.get("overdue", [])):
                lines.append(f"• {self._display_month(period)}: {format_money(total)}")
        else:
            lines.append("No registra deuda vencida en CCMA.")
        if creditor_total:
            lines.extend(["", f"Saldo a favor: {format_money(creditor_total)}"])

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
        if connection and connection["status"] == "LOGIN_REQUIRED":
            lines.append("ARCA pidió una validación manual después de esta consulta.")
        if connection and connection["status"] == "CREDENTIALS_REQUIRED":
            lines.append("ARCA rechazó la clave guardada; hay que volver a cargarla.")
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
        row = self.connection_map(chat_id).get(provider)
        if not row or row["status"] != "CONNECTED":
            identity = self.db.get_tax_identity(chat_id)
            if not identity or identity["status"] != "VERIFIED":
                return "Primero conectá tu cuenta de ARCA con /conectar."
        return f"Todavía no pude obtener {PROVIDERS[provider].lower()}. Probá de nuevo."

    def disconnect(self, chat_id: int) -> str:
        with self.db.connect() as connection:
            connection.execute("DELETE FROM live_snapshots WHERE chat_id = ?", (chat_id,))
            connection.execute("DELETE FROM connections WHERE chat_id = ?", (chat_id,))
            connection.execute("DELETE FROM tax_identities WHERE chat_id = ?", (chat_id,))
        return "Listo. Desvinculé tu cuenta y borré los datos guardados."

    def dispatch(self, text: str, chat_id: int) -> str:
        self.db.ensure_user(chat_id)
        parts = text.strip().split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command in {"/start", "/ayuda", "/help"}:
            return self.help(chat_id)
        if command == "/conectar":
            return self.connect(chat_id, argument)
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
            return self.source_command(chat_id, "SANTA_FE_IIBB")
        if command == "/vencimientos":
            return "Vencimientos todavía no está disponible."
        if command == "/desconectar":
            return self.disconnect(chat_id)
        return "Comando no reconocido. Usá /ayuda."
