from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from .bot_reply import BotReply
from .db import Database
from .health import source_health
from .santa_fe import (
    ARGENTINA_TIMEZONE,
    IibbGenerationUnknown,
    IibbQuoteChanged,
    SantaFeIibbConnector,
)
from .service import format_date, format_money, format_month_year


LOGGER = logging.getLogger(__name__)
PAYMENT_URL = "https://seti.afip.gob.ar/setiweb/#/pago/consulta-veps"
SANTANDER_VEP_HELP_URL = (
    "https://ayuda.santander.com.ar/3WR6AZDV3B-1/article/"
    "KFCQ44ND0R-que-es-vep-como-pagarlo/"
)
ARCA_VEP_HELP_URL = (
    "https://serviciosweb.arca.gob.ar/genericos/guiasPasoPaso/VerGuia.aspx?id=66"
)
INTERBANKING_HELP_URL = "https://prehome.interbanking.com.ar/nuevo-horario"
NETWORKS = {
    "1001": "Red Link",
    "1002": "Santander / Pago Mis Cuentas",
    "1003": "Interbanking",
}
NETWORK_GUIDANCE = {
    "1001": (
        "Después de generarlo, ingresá al home banking de un banco adherido a "
        "Red Link y buscá el pago de VEP/ARCA. El recorrido exacto depende del "
        "banco, por eso el bot no indica nombres de menú que no estén verificados."
    ),
    "1002": (
        "En Santander: Transacciones → Pago de Servicios → Nuevo pago; buscá "
        "VEP e ingresá el CUIT contribuyente y el CUIT generador. Santander "
        "advierte que puede tardar al menos 3 horas y que no siempre aparece "
        "automáticamente en el calendario de pagos."
    ),
    "1003": (
        "Después de generarlo, ingresá a Interbanking, localizá el VEP enviado "
        "a esa red y autorizá el pago. Interbanking informa que los pagos enviados "
        "hasta las 16:00 se procesan el mismo día hábil; después se procesan el "
        "siguiente día hábil y, si el VEP vence ese día, habrá que generar otro."
    ),
}
NETWORK_HELP_URLS = {
    "1001": ARCA_VEP_HELP_URL,
    "1002": SANTANDER_VEP_HELP_URL,
    "1003": INTERBANKING_HELP_URL,
}
ACTIVE_STATES = (
    "QUOTE_REQUESTED",
    "QUOTING",
    "QUOTED",
    "GENERATION_REQUESTED",
    "GENERATING",
    "GENERATED",
    "UNKNOWN",
)


class IibbPaymentStore:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        row_id: str,
        chat_id: int,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO iibb_payment_events(draft_id, chat_id, event_type, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                row_id,
                chat_id,
                event_type,
                json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def get(self, row_id: str, chat_id: int | None = None):
        query = "SELECT * FROM iibb_payment_drafts WHERE id = ?"
        parameters: tuple[object, ...] = (row_id,)
        if chat_id is not None:
            query += " AND chat_id = ?"
            parameters += (chat_id,)
        with self.db.connect() as connection:
            return connection.execute(query, parameters).fetchone()

    def active_for_period(self, chat_id: int, account: str, period: str):
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self.db.connect() as connection:
            return connection.execute(
                f"""
                SELECT * FROM iibb_payment_drafts
                WHERE chat_id = ? AND account = ? AND period = ?
                  AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1
                """,
                (chat_id, account, period, *ACTIVE_STATES),
            ).fetchone()

    def request_quote(
        self,
        *,
        chat_id: int,
        represented_cuit: str,
        account: str,
        period: str,
        network_id: str,
        payment_date: date,
    ):
        if network_id not in NETWORKS:
            raise ValueError("La red de pago no es válida")
        existing = self.active_for_period(chat_id, account, period)
        if existing is not None:
            return existing
        row_id = uuid.uuid4().hex[:16]
        try:
            with self.db.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO iibb_payment_drafts(
                        id, chat_id, represented_cuit, account, period, status,
                        network_id, network_name, payment_date
                    ) VALUES (?, ?, ?, ?, ?, 'QUOTE_REQUESTED', ?, ?, ?)
                    """,
                    (
                        row_id,
                        chat_id,
                        represented_cuit,
                        account,
                        period,
                        network_id,
                        NETWORKS[network_id],
                        payment_date.isoformat(),
                    ),
                )
                self._event(connection, row_id, chat_id, "QUOTE_REQUESTED")
        except sqlite3.IntegrityError:
            existing = self.active_for_period(chat_id, account, period)
            if existing is not None:
                return existing
            raise
        return self.get(row_id, chat_id)

    def recover_stale_jobs(self) -> int:
        recovered = 0
        with self.db.connect() as connection:
            quote_rows = connection.execute(
                """
                SELECT id, chat_id FROM iibb_payment_drafts
                WHERE status = 'QUOTING'
                  AND datetime(updated_at) <= datetime('now', '-10 minutes')
                """
            ).fetchall()
            for row in quote_rows:
                connection.execute(
                    """
                    UPDATE iibb_payment_drafts SET status = 'QUOTE_REQUESTED',
                        updated_at = CURRENT_TIMESTAMP WHERE id = ?
                    """,
                    (row["id"],),
                )
                self._event(connection, row["id"], row["chat_id"], "QUOTE_RECOVERED")
                recovered += 1
            generation_rows = connection.execute(
                """
                SELECT id, chat_id FROM iibb_payment_drafts
                WHERE status = 'GENERATING'
                  AND datetime(updated_at) <= datetime('now', '-10 minutes')
                """
            ).fetchall()
            for row in generation_rows:
                connection.execute(
                    """
                    UPDATE iibb_payment_drafts SET status = 'UNKNOWN',
                        last_error = 'El servicio se reinició durante la generación',
                        notification_pending = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (row["id"],),
                )
                self._event(connection, row["id"], row["chat_id"], "UNKNOWN_AFTER_RESTART")
                recovered += 1
        return recovered

    def _paid_seti_vep_numbers(self, chat_id: int) -> set[str]:
        snapshot = self.db.get_live_snapshot(chat_id, "ARCA_SETI", "payment_status")
        if snapshot is None:
            return set()
        try:
            payload = json.loads(snapshot["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return set()
        return {
            str(vep.get("number", ""))
            for vep in payload.get("veps", [])
            if str(vep.get("status", "")).upper() == "PAID"
            and str(vep.get("number", ""))
        }

    def expire_generated(self, current: date) -> int:
        with self.db.connect() as connection:
            chat_ids = {
                int(row["chat_id"])
                for row in connection.execute(
                    """
                    SELECT DISTINCT chat_id FROM iibb_payment_drafts
                    WHERE status = 'GENERATED' AND vep_expires_on < ?
                    """,
                    (current.isoformat(),),
                ).fetchall()
            }
        for chat_id in chat_ids:
            self.reconcile_paid(chat_id)

        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id FROM iibb_payment_drafts
                WHERE status = 'GENERATED' AND vep_expires_on < ?
                """,
                (current.isoformat(),),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE iibb_payment_drafts SET status = 'EXPIRED',
                        notification_pending = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (row["id"],),
                )
                self._event(connection, row["id"], row["chat_id"], "EXPIRED")
        return len(rows)

    def request_generation(self, row_id: str, chat_id: int):
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE iibb_payment_drafts
                SET status = 'GENERATION_REQUESTED', last_error = NULL,
                    notification_pending = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND chat_id = ? AND status = 'QUOTED'
                """,
                (row_id, chat_id),
            )
            if cursor.rowcount:
                self._event(connection, row_id, chat_id, "GENERATION_REQUESTED")
        return self.get(row_id, chat_id)

    def cancel(self, row_id: str, chat_id: int):
        with self.db.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE iibb_payment_drafts
                SET status = 'CANCELLED', notification_pending = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND chat_id = ?
                  AND status IN ('QUOTE_REQUESTED','QUOTING','QUOTED')
                """,
                (row_id, chat_id),
            )
            if cursor.rowcount:
                self._event(connection, row_id, chat_id, "CANCELLED")
        return self.get(row_id, chat_id)

    def claim_next(self):
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM iibb_payment_drafts
                WHERE status IN ('QUOTE_REQUESTED','GENERATION_REQUESTED')
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            claimed = "QUOTING" if row["status"] == "QUOTE_REQUESTED" else "GENERATING"
            cursor = connection.execute(
                """
                UPDATE iibb_payment_drafts
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = ?
                """,
                (claimed, row["id"], row["status"]),
            )
            if not cursor.rowcount:
                return None
            self._event(connection, row["id"], row["chat_id"], claimed)
        return self.get(str(row["id"]))

    def quote_succeeded(self, row_id: str, quote: dict[str, str], *, changed=False) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE iibb_payment_drafts SET
                    status = 'QUOTED', obligation_due_date = ?, principal = ?,
                    interest = ?, total = ?, vep_expires_on = ?, last_error = NULL,
                    notification_pending = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    quote["due_date"],
                    quote["principal"],
                    quote["interest"],
                    quote["total"],
                    quote.get("vep_expires_on"),
                    row_id,
                ),
            )
            row = connection.execute(
                "SELECT chat_id FROM iibb_payment_drafts WHERE id = ?", (row_id,)
            ).fetchone()
            self._event(
                connection,
                row_id,
                int(row["chat_id"]),
                "QUOTE_CHANGED" if changed else "QUOTED",
                quote,
            )

    def failed(self, row_id: str, message: str, *, keep_quote: bool = False) -> None:
        status = "QUOTED" if keep_quote else "ERROR"
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE iibb_payment_drafts
                SET status = ?, last_error = ?, notification_pending = 1,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (status, message[:500], row_id),
            )
            row = connection.execute(
                "SELECT chat_id FROM iibb_payment_drafts WHERE id = ?", (row_id,)
            ).fetchone()
            self._event(connection, row_id, int(row["chat_id"]), "ERROR", {"message": message[:500]})

    def generation_unknown(self, row_id: str, message: str, *, pdf: bytes | None = None,
                           vep_number: str | None = None) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE iibb_payment_drafts
                SET status = 'UNKNOWN', last_error = ?, notification_pending = 1,
                    official_pdf = COALESCE(?, official_pdf),
                    vep_number = COALESCE(?, vep_number),
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (message[:500], pdf, vep_number, row_id),
            )
            row = connection.execute(
                "SELECT chat_id FROM iibb_payment_drafts WHERE id = ?", (row_id,)
            ).fetchone()
            self._event(connection, row_id, int(row["chat_id"]), "UNKNOWN", {"message": message[:500]})

    def generation_succeeded(self, row_id: str, result: dict[str, object]) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE iibb_payment_drafts
                SET status = 'GENERATED', vep_number = ?, official_pdf = ?,
                    vep_expires_on = COALESCE(?, payment_date), last_error = NULL,
                    notification_pending = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(result["vep_number"]), bytes(result["pdf"]),
                 result.get("quote", {}).get("vep_expires_on"), row_id),
            )
            row = connection.execute(
                "SELECT chat_id FROM iibb_payment_drafts WHERE id = ?", (row_id,)
            ).fetchone()
            self._event(
                connection,
                row_id,
                int(row["chat_id"]),
                "GENERATED",
                {"vep_number": str(result["vep_number"])},
            )

    def pending_notifications(self, chat_ids: frozenset[int]):
        if not chat_ids:
            return []
        placeholders = ",".join("?" for _ in chat_ids)
        with self.db.connect() as connection:
            return connection.execute(
                f"""
                SELECT * FROM iibb_payment_drafts
                WHERE notification_pending = 1 AND chat_id IN ({placeholders})
                ORDER BY updated_at, id
                """,
                tuple(sorted(chat_ids)),
            ).fetchall()

    def mark_notified(self, row_id: str) -> None:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE iibb_payment_drafts SET notification_pending = 0,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (row_id,),
            )

    def reconcile_paid(self, chat_id: int) -> int:
        snapshot = self.db.get_live_snapshot(chat_id, "SANTA_FE_IIBB", "account_status")
        try:
            payload = json.loads(snapshot["payload_json"]) if snapshot is not None else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        paid_periods = {
            str(item.get("period", ""))
            for item in payload.get("vepai", {}).get("payments", [])
        }
        paid_vep_numbers = self._paid_seti_vep_numbers(chat_id)
        if not paid_periods and not paid_vep_numbers:
            return 0
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id, period, vep_number FROM iibb_payment_drafts
                WHERE chat_id = ? AND status IN ('GENERATED', 'UNKNOWN', 'EXPIRED')
                """,
                (chat_id,),
            ).fetchall()
            for row in rows:
                if (
                    str(row["period"]) not in paid_periods
                    and str(row["vep_number"] or "") not in paid_vep_numbers
                ):
                    continue
                connection.execute(
                    """
                    UPDATE iibb_payment_drafts SET status = 'PAID',
                        notification_pending = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (row["id"],),
                )
                self._event(connection, row["id"], row["chat_id"], "PAID")
        return len(rows)


class IibbPaymentProcessor:
    def __init__(
        self,
        store: IibbPaymentStore,
        connector: SantaFeIibbConnector,
        allowed_chat_ids: frozenset[int] | None = None,
    ):
        self.store = store
        self.connector = connector
        self.allowed_chat_ids = allowed_chat_ids

    def process_one(self) -> bool:
        row = self.store.claim_next()
        if row is None:
            return False
        if self.allowed_chat_ids is not None and int(row["chat_id"]) not in self.allowed_chat_ids:
            self.store.failed(str(row["id"]), "Este chat no está autorizado para generar VEP")
            return True
        try:
            payment_date = date.fromisoformat(str(row["payment_date"]))
            if row["status"] == "QUOTING":
                quote = self.connector.quote_payment(
                    int(row["chat_id"]),
                    str(row["represented_cuit"]),
                    period=str(row["period"]),
                    payment_date=payment_date,
                )
                self.store.quote_succeeded(str(row["id"]), quote)
            else:
                result = self.connector.generate_vep(
                    int(row["chat_id"]),
                    str(row["represented_cuit"]),
                    account=str(row["account"]),
                    period=str(row["period"]),
                    payment_date=payment_date,
                    network_id=str(row["network_id"]),
                    expected_quote={
                        "due_date": str(row["obligation_due_date"]),
                        "principal": str(row["principal"]),
                        "interest": str(row["interest"]),
                        "total": str(row["total"]),
                        "vep_expires_on": str(row["vep_expires_on"] or ""),
                    },
                )
                try:
                    self.store.generation_succeeded(str(row["id"]), result)
                except Exception as exc:
                    raise IibbGenerationUnknown(
                        "El VEP fue emitido, pero falló su registro; no se reintentará",
                        pdf=bytes(result["pdf"]), vep_number=str(result["vep_number"]),
                    ) from exc
        except IibbQuoteChanged as exc:
            self.store.quote_succeeded(str(row["id"]), exc.quote, changed=True)
        except IibbGenerationUnknown as exc:
            self.store.generation_unknown(
                str(row["id"]), str(exc), pdf=exc.pdf, vep_number=exc.vep_number,
            )
        except Exception as exc:
            LOGGER.exception("Falló una operación de pago de Ingresos Brutos")
            self.store.failed(
                str(row["id"]),
                str(exc) or exc.__class__.__name__,
                keep_quote=row["status"] == "GENERATING",
            )
        return True


class IibbPaymentBotService:
    def __init__(
        self,
        store: IibbPaymentStore,
        allowed_chat_ids: frozenset[int],
        *,
        enabled: bool,
    ):
        self.store = store
        self.allowed_chat_ids = allowed_chat_ids
        self.enabled = enabled

    def _authorized(self, chat_id: int) -> bool:
        return self.enabled and chat_id in self.allowed_chat_ids

    def _fresh_iibb_snapshot(self, chat_id: int):
        health = source_health(
            self.store.db,
            chat_id,
            "SANTA_FE_IIBB",
            datetime.now(timezone.utc),
        )
        if not health.snapshot.is_fresh:
            return None
        return self.store.db.get_live_snapshot(
            chat_id,
            "SANTA_FE_IIBB",
            "account_status",
        )

    def start(self, chat_id: int, *, replace: bool = False) -> BotReply:
        if not self._authorized(chat_id):
            return BotReply("El pago de Ingresos Brutos no está habilitado para este chat.", replace_message=replace)
        identity = self.store.db.get_tax_identity(chat_id)
        snapshot = self._fresh_iibb_snapshot(chat_id)
        if identity is None or identity["status"] != "VERIFIED" or snapshot is None:
            return BotReply("Primero necesito actualizar tu cuenta de Ingresos Brutos.", replace_message=replace)
        payload = json.loads(snapshot["payload_json"])
        obligation = payload.get("next_obligation")
        if not isinstance(obligation, dict):
            return BotReply(
                "No encontré una cuota pendiente para generar un VEP.",
                ((('Volver a pagos', 'tax:payments'),),),
                replace,
            )
        period = str(obligation.get("period", ""))
        account = str(payload.get("account", ""))
        existing = self.store.active_for_period(chat_id, account, period)
        if existing is not None:
            return self.render(existing, replace=replace)
        year, month = (int(item) for item in period.split("-"))
        text = "\n".join(
            [
                "Pagar Ingresos Brutos",
                "",
                f"Cuota: {format_month_year(date(year, month, 1))}",
                f"Importe informado: {format_money(obligation.get('amount'))}",
                f"Vencimiento: {format_date(str(obligation.get('due_date', '')))}",
                "",
                "Elegí dónde querés recibir el VEP. Antes de generarlo voy a consultar el importe y los intereses de hoy.",
            ]
        )
        keyboard = (
            (("Santander / Pago Mis Cuentas", "iibbpay:network:1002"),),
            (("Red Link", "iibbpay:network:1001"),),
            (("Interbanking", "iibbpay:network:1003"),),
            (("Volver a pagos", "tax:payments"),),
        )
        return BotReply(text, keyboard, replace)

    def handle_callback(self, data: str, chat_id: int) -> BotReply:
        if not self._authorized(chat_id):
            return BotReply("El pago de Ingresos Brutos no está habilitado para este chat.")
        identity = self.store.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            return BotReply(
                "Tu cuenta está desconectada. Usá /conectar seguido de tu CUIT.",
                replace_message=True,
            )
        parts = data.split(":")
        if data == "iibbpay:start":
            return self.start(chat_id, replace=True)
        if len(parts) == 3 and parts[1] == "network":
            return self._choose_network(chat_id, parts[2])
        if len(parts) == 3 and parts[1] == "confirm":
            if self._fresh_iibb_snapshot(chat_id) is None:
                return BotReply(
                    "Primero necesito actualizar tu cuenta de Ingresos Brutos.",
                    replace_message=True,
                )
            row = self.store.request_generation(parts[2], chat_id)
            if row is None:
                return BotReply("Ese VEP ya no está disponible.", replace_message=True)
            if row["status"] != "GENERATION_REQUESTED":
                return self.render(row, replace=True)
            return BotReply(
                "Estoy generando el VEP en Santa Fe. Te lo envío apenas quede confirmado.",
                replace_message=True,
            )
        if len(parts) == 3 and parts[1] == "cancel":
            row = self.store.cancel(parts[2], chat_id)
            if row is None:
                return BotReply("Ese pago ya no está disponible.", replace_message=True)
            return BotReply(
                "Cancelé la preparación del VEP.",
                ((('Volver a pagos', 'tax:payments'),),),
                True,
            )
        raise ValueError("Esa opción de pago ya no está disponible")

    def _choose_network(self, chat_id: int, network_id: str) -> BotReply:
        identity = self.store.db.get_tax_identity(chat_id)
        snapshot = self._fresh_iibb_snapshot(chat_id)
        if identity is None or identity["status"] != "VERIFIED" or snapshot is None:
            return BotReply("Primero necesito actualizar tu cuenta de Ingresos Brutos.", replace_message=True)
        payload = json.loads(snapshot["payload_json"])
        obligation = payload.get("next_obligation")
        if not isinstance(obligation, dict):
            return BotReply("No encontré una cuota pendiente para pagar.", replace_message=True)
        row = self.store.request_quote(
            chat_id=chat_id,
            represented_cuit=str(identity["represented_cuit"]),
            account=str(payload["account"]),
            period=str(obligation["period"]),
            network_id=network_id,
            payment_date=datetime.now(ARGENTINA_TIMEZONE).date(),
        )
        if row["status"] != "QUOTE_REQUESTED":
            return self.render(row, replace=True)
        return BotReply(
            "Estoy consultando en Santa Fe el importe exacto para pagar hoy. Te aviso en unos segundos.",
            replace_message=True,
        )

    def render(self, row, *, replace: bool = False) -> BotReply:
        status = str(row["status"])
        if status in {"QUOTE_REQUESTED", "QUOTING"}:
            return BotReply("Estoy consultando el importe exacto en Santa Fe.", replace_message=replace)
        if status in {"GENERATION_REQUESTED", "GENERATING"}:
            return BotReply("Estoy generando el VEP. Te lo envío apenas quede confirmado.", replace_message=replace)
        if status == "QUOTED":
            intro = (
                f"Santa Fe no generó el VEP: {row['last_error']}\nPodés revisarlo y volver a confirmar."
                if row["last_error"]
                else "Importe confirmado por Santa Fe"
            )
            text = "\n".join(
                [
                    intro,
                    "",
                    f"Período: {row['period']}",
                    f"Cuota: {format_money(row['principal'])}",
                    f"Intereses: {format_money(row['interest'])}",
                    f"Total: {format_money(row['total'])}",
                    f"Fecha de pago: {format_date(row['payment_date'])}",
                    *([f"Vencimiento del VEP informado por Santa Fe: {format_date(row['vep_expires_on'])}"]
                      if row["vep_expires_on"] else []),
                    f"Red: {row['network_name']}",
                    "",
                    NETWORK_GUIDANCE[str(row["network_id"])],
                    "",
                    "Generar el VEP no debita dinero. Después tendrás que abrirlo y confirmar el pago.",
                ]
            )
            keyboard = (
                (("Generar VEP", f"iibbpay:confirm:{row['id']}"),),
                (("Cancelar", f"iibbpay:cancel:{row['id']}"),),
            )
            return BotReply(text, keyboard, replace)
        if status == "GENERATED":
            text = "\n".join(
                [
                    "VEP de Ingresos Brutos generado",
                    "",
                    f"VEP: {row['vep_number']}",
                    f"Período: {row['period']}",
                    f"Total: {format_money(row['total'])}",
                    f"Pagar hasta: {format_date(row['vep_expires_on'])}",
                    "",
                    "Cómo pagarlo",
                    NETWORK_GUIDANCE[str(row["network_id"])],
                    "",
                    "También podés abrir ARCA y elegir Pago con QR para este VEP. El bot no guarda datos bancarios.",
                ]
            )
            return BotReply(
                text,
                (
                    (("Abrir ARCA para pagar con QR", PAYMENT_URL),),
                    (("Ver instrucciones oficiales", NETWORK_HELP_URLS[str(row["network_id"])]),),
                    (("Volver a pagos", "tax:payments"),),
                ),
                replace,
                document=bytes(row["official_pdf"]) if row["official_pdf"] else None,
                filename=f"VEP-IIBB-{row['vep_number']}.pdf",
            )
        if status == "PAID":
            return BotReply(
                f"Pago de Ingresos Brutos confirmado\n\nPeríodo: {row['period']}\nTotal: {format_money(row['total'])}",
                ((('Volver a pagos', 'tax:payments'),),),
                replace,
            )
        if status == "UNKNOWN":
            return BotReply(
                "No pude confirmar si Santa Fe generó el VEP. No voy a repetirlo automáticamente para evitar duplicados. Revisalo desde ARCA antes de volver a intentar.",
                ((('Abrir ARCA', PAYMENT_URL),), (('Volver a pagos', 'tax:payments'),)),
                replace,
            )
        if status == "EXPIRED":
            return BotReply(
                "El VEP alcanzó su fecha límite y todavía no pude confirmar el pago. Revisá SETI o el banco antes de generar otro para evitar un pago duplicado.",
                ((('Abrir ARCA', PAYMENT_URL),), (('Volver a pagos', 'tax:payments'),)),
                replace,
            )
        if status == "ERROR":
            return BotReply(
                f"No pude preparar el VEP: {row['last_error']}",
                ((('Volver a pagos', 'tax:payments'),),),
                replace,
            )
        return BotReply("Este pago ya no está activo.", replace_message=replace)

    def pending_replies(self):
        return [
            (int(row["chat_id"]), str(row["id"]), self.render(row))
            for row in self.store.pending_notifications(self.allowed_chat_ids)
        ]
