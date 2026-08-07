from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Callable
from zoneinfo import ZoneInfo

from .bot_reply import BotReply
from .identity import is_valid_cuit, normalize_cuit
from .invoice_gateway import InvoiceGateway
from .invoice_pdf import build_invoice_pdf, invoice_filename
from .invoice_store import InvoiceStore, decimal_text


ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")


def _today() -> date:
    return datetime.now(ARGENTINA).date()


def _money(value: object) -> str:
    amount = Decimal(str(value))
    formatted = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"${formatted}"


def _date(value: str) -> str:
    try:
        return date.fromisoformat(value).strftime("%d/%m/%Y")
    except ValueError:
        return value


def _parse_date(value: str) -> date:
    raw = value.strip()
    for separator in ("/", "-"):
        parts = raw.split(separator)
        if len(parts) == 3:
            try:
                if len(parts[0]) == 4:
                    return date(int(parts[0]), int(parts[1]), int(parts[2]))
                return date(int(parts[2]), int(parts[1]), int(parts[0]))
            except ValueError:
                break
    raise ValueError("Usá una fecha válida con formato DD/MM/AAAA")


def _parse_amount(value: str) -> str:
    raw = value.strip().replace("$", "").replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    return decimal_text(raw)


class InvoiceBotService:
    COMMANDS = {"/facturas", "/nueva_factura", "/nuevo_cliente", "/cancelar"}

    def __init__(
        self,
        store: InvoiceStore,
        gateway: InvoiceGateway,
        today_provider: Callable[[], date] = _today,
    ):
        self.store = store
        self.gateway = gateway
        self.today_provider = today_provider

    def handles(self, text: str, chat_id: int) -> bool:
        command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower() if text.strip() else ""
        return command in self.COMMANDS or self.store.get_state(chat_id) is not None

    def _connected(self, chat_id: int) -> bool:
        identity = self.store.db.get_tax_identity(chat_id)
        return identity is not None and identity["status"] == "VERIFIED"

    @staticmethod
    def _disconnected_reply(*, replace: bool = False) -> BotReply:
        return BotReply(
            "Tu cuenta está desconectada. Usá /conectar seguido de tu CUIT para "
            "acceder a las facturas.",
            replace_message=replace,
        )

    def handle_message(self, text: str, chat_id: int) -> BotReply | None:
        if not self._connected(chat_id):
            self.store.clear_state(chat_id)
            return self._disconnected_reply()
        stripped = text.strip()
        command = stripped.split(maxsplit=1)[0].split("@", 1)[0].lower() if stripped else ""
        if command == "/facturas":
            return self.invoice_list(chat_id, 0)
        if command == "/nueva_factura":
            return self.template_list(chat_id)
        if command == "/nuevo_cliente":
            return self.start_new_client(chat_id)
        if command == "/cancelar":
            self.store.clear_state(chat_id)
            return BotReply("Listo, cancelé la carga en curso.")
        state = self.store.get_state(chat_id)
        if state is None:
            return None
        payload = json.loads(state["payload_json"] or "{}")
        try:
            return self._handle_state(chat_id, state["action"], payload, stripped)
        except Exception as exc:
            return BotReply(f"No pude guardar ese dato: {exc}\n\nPodés cancelar con /cancelar.")

    def handle_callback(self, data: str, chat_id: int) -> BotReply:
        if not self._connected(chat_id):
            self.store.clear_state(chat_id)
            return self._disconnected_reply(replace=True)
        parts = data.split(":")
        try:
            if parts[:2] == ["inv", "list"]:
                return self.invoice_list(chat_id, int(parts[2]), replace=True)
            if parts[:2] == ["inv", "view"]:
                return self.invoice_detail(chat_id, int(parts[2]), replace=True)
            if parts[:2] == ["inv", "pdf"]:
                return self.invoice_pdf(chat_id, int(parts[2]))
            if parts[:2] == ["inv", "repeat"]:
                draft = self.store.create_draft_from_invoice(
                    chat_id, int(parts[2]), self.today_provider()
                )
                return self.draft_detail(draft, replace=True)
            if parts[:2] == ["tpl", "list"]:
                return self.template_list(chat_id, replace=True)
            if parts[:2] == ["tpl", "use"]:
                draft = self.store.create_draft_from_template(
                    chat_id, int(parts[2]), self.today_provider()
                )
                return self.draft_detail(draft, replace=True)
            if parts[:2] == ["tpl", "new"]:
                return self.start_new_client(chat_id, replace=True)
            if parts[0] == "d":
                return self._draft_callback(parts[1], parts[2], chat_id)
        except (IndexError, ValueError) as exc:
            return BotReply(f"No pude procesar esa opción: {exc}")
        return BotReply("Esa opción ya no está disponible.")

    def invoice_list(self, chat_id: int, offset: int, replace: bool = False) -> BotReply:
        invoices = self.store.list_invoices(chat_id, offset, 3)
        count = self.store.invoice_count(chat_id)
        if not invoices:
            return BotReply(
                "Todavía no hay facturas guardadas.",
                keyboard=((('Nueva factura', 'tpl:list:0'),),),
                replace_message=replace,
            )
        lines = ["Últimas facturas", ""]
        keyboard = []
        for invoice in invoices:
            number = f"{invoice['point_of_sale']:05d}-{invoice['number']:08d}"
            lines.append(
                f"• {number} · {_date(invoice['issue_date'])} · {_money(invoice['total'])}\n"
                f"  {invoice['receiver_name']}"
            )
            keyboard.append(((f"Ver {number}", f"inv:view:{invoice['id']}"),))
        navigation = []
        if offset > 0:
            navigation.append(("Más nuevas", f"inv:list:{max(0, offset - 3)}"))
        if offset + len(invoices) < count:
            navigation.append(("3 anteriores", f"inv:list:{offset + 3}"))
        if navigation:
            keyboard.append(tuple(navigation))
        keyboard.append((("Nueva factura", "tpl:list:0"),))
        keyboard.append((("Volver al resumen", "tax:summary"),))
        return BotReply("\n".join(lines), tuple(keyboard), replace)

    def invoice_detail(self, chat_id: int, invoice_id: int, replace: bool = False) -> BotReply:
        invoice = self.store.get_invoice(chat_id, invoice_id)
        if invoice is None:
            raise ValueError("La factura no existe")
        number = f"{invoice['point_of_sale']:05d}-{invoice['number']:08d}"
        text = "\n".join(
            [
                f"Factura C {number}",
                "",
                invoice["receiver_name"],
                f"CUIT {invoice['receiver_cuit']}",
                f"{invoice['description']}",
                f"Período: {_date(invoice['service_from'])} al {_date(invoice['service_to'])}",
                f"Total: {_money(invoice['total'])}",
                f"CAE: {invoice['cae']}",
            ]
        )
        keyboard = (
            (("Descargar PDF", f"inv:pdf:{invoice_id}"), ("Repetir", f"inv:repeat:{invoice_id}")),
            (("Volver", "inv:list:0"),),
        )
        return BotReply(text, keyboard, replace)

    def invoice_pdf(self, chat_id: int, invoice_id: int) -> BotReply:
        invoice = self.store.get_invoice(chat_id, invoice_id)
        if invoice is None:
            raise ValueError("La factura no existe")
        return BotReply(
            "Acá tenés el Original.",
            document=build_invoice_pdf(invoice),
            filename=invoice_filename(invoice),
        )

    def template_list(self, chat_id: int, replace: bool = False) -> BotReply:
        if chat_id not in self.gateway.allowed_chat_ids:
            return BotReply("Este chat no está autorizado para emitir facturas.")
        templates = self.store.list_templates(chat_id)
        keyboard = [
            ((template["name"], f"tpl:use:{template['id']}"),)
            for template in templates
        ]
        keyboard.append((("Nuevo cliente", "tpl:new:0"),))
        text = "Elegí una plantilla:" if templates else "Todavía no tenés plantillas."
        return BotReply(text, tuple(keyboard), replace)

    def start_new_client(self, chat_id: int, replace: bool = False) -> BotReply:
        if chat_id not in self.gateway.allowed_chat_ids:
            return BotReply("Este chat no está autorizado para emitir facturas.")
        self.store.set_state(chat_id, "NEW_CLIENT_CUIT")
        return BotReply(
            "Enviame el CUIT del cliente. Voy a verificar sus datos en ARCA.",
            replace_message=replace,
        )

    def draft_detail(self, draft, replace: bool = False, *, final: bool = False) -> BotReply:
        if draft is None:
            raise ValueError("El borrador no existe")
        title = "Revisión final" if final else "Borrador de Factura C"
        lines = [
            title,
            "",
            draft["receiver_name"],
            f"CUIT {draft['receiver_cuit']}",
            f"Descripción: {draft['description']}",
            f"Período: {_date(draft['service_from'])} al {_date(draft['service_to'])}",
            f"Vencimiento: {_date(draft['due_date'])}",
            f"Total: {_money(draft['total'])}",
            "",
            "BORRADOR - NO VÁLIDO COMO FACTURA",
        ]
        draft_id = draft["id"]
        if final:
            lines.extend(
                [
                    "",
                    "Antes de emitir, confirmá que no hiciste esta misma factura por Comprobantes en Línea.",
                ]
            )
            keyboard = (
                (("Confirmo que no la emití manualmente", f"d:ack:{draft_id}"),),
                (("Volver al borrador", f"d:view:{draft_id}"),),
            )
        else:
            keyboard = (
                (("Editar importe", f"d:amount:{draft_id}"), ("Editar descripción", f"d:desc:{draft_id}")),
                (("Editar período", f"d:period:{draft_id}"), ("Editar vencimiento", f"d:due:{draft_id}")),
                (("Continuar", f"d:review:{draft_id}"), ("Cancelar", f"d:cancel:{draft_id}")),
            )
        return BotReply("\n".join(lines), keyboard, replace)

    def _draft_callback(self, action: str, draft_id: str, chat_id: int) -> BotReply:
        draft = self.store.get_draft(chat_id, draft_id)
        if draft is None:
            raise ValueError("El borrador no existe")
        if action == "view":
            return self.draft_detail(draft, replace=True)
        if action in {"amount", "desc", "period", "due"}:
            prompts = {
                "amount": "Enviame el nuevo importe. Ejemplo: 125000",
                "desc": "Enviame la nueva descripción del servicio.",
                "period": "Enviame el período como DD/MM/AAAA - DD/MM/AAAA.",
                "due": "Enviame el vencimiento con formato DD/MM/AAAA.",
            }
            self.store.set_state(chat_id, f"EDIT_{action.upper()}", {"draft_id": draft_id})
            return BotReply(prompts[action])
        if action == "review":
            if self.store.known_duplicate(draft):
                return BotReply(
                    "Ya existe una factura guardada para el mismo cliente, período e importe. No voy a emitirla otra vez.",
                    keyboard=((('Volver', f'd:view:{draft_id}'),),),
                    replace_message=True,
                )
            return self.draft_detail(draft, replace=True, final=True)
        if action == "ack":
            draft = self.store.update_draft(
                chat_id, draft_id, manual_duplicate_confirmed=1
            )
            return BotReply(
                self.draft_detail(draft, final=True).text + "\n\nEsta acción emitirá una factura real en ARCA.",
                keyboard=(
                    (("Emitir Factura C", f"d:issue:{draft_id}"),),
                    (("Volver al borrador", f"d:view:{draft_id}"),),
                ),
                replace_message=True,
            )
        if action == "issue":
            invoice_id = self.gateway.issue(chat_id, draft_id)
            invoice = self.store.get_invoice(chat_id, invoice_id)
            number = f"{invoice['point_of_sale']:05d}-{invoice['number']:08d}"
            try:
                document = build_invoice_pdf(invoice)
                filename = invoice_filename(invoice)
                extra = "Te envío el Original en PDF."
            except Exception:
                document = None
                filename = ""
                extra = "La factura está emitida. No pude generar el PDF ahora; podés descargarlo desde /facturas."
            return BotReply(
                f"Factura C {number} emitida por {_money(invoice['total'])}.\nCAE: {invoice['cae']}\n\n{extra}",
                keyboard=((('Ver factura', f'inv:view:{invoice_id}'),),),
                replace_message=True,
                document=document,
                filename=filename,
            )
        if action == "cancel":
            self.store.mark_draft(chat_id, draft_id, "CANCELLED")
            return BotReply("Borrador cancelado.", replace_message=True)
        raise ValueError("La opción del borrador no existe")

    def _handle_state(self, chat_id: int, action: str, payload: dict, text: str) -> BotReply:
        draft_id = payload.get("draft_id", "")
        if action == "EDIT_AMOUNT":
            amount = _parse_amount(text)
            draft = self.store.update_draft(
                chat_id, draft_id, unit_price=amount, total=amount, manual_duplicate_confirmed=0
            )
            self.store.clear_state(chat_id)
            return self.draft_detail(draft)
        if action == "EDIT_DESC":
            if not text or len(text) > 500:
                raise ValueError("La descripción debe tener entre 1 y 500 caracteres")
            draft = self.store.update_draft(
                chat_id, draft_id, description=text, manual_duplicate_confirmed=0
            )
            self.store.clear_state(chat_id)
            return self.draft_detail(draft)
        if action == "EDIT_PERIOD":
            match = re.fullmatch(r"\s*(.+?)\s+[-–]\s+(.+?)\s*", text)
            if not match:
                raise ValueError("Usá DD/MM/AAAA - DD/MM/AAAA")
            start, end = _parse_date(match.group(1)), _parse_date(match.group(2))
            if start > end:
                raise ValueError("La fecha inicial no puede ser posterior a la final")
            draft = self.store.update_draft(
                chat_id,
                draft_id,
                service_from=start.isoformat(),
                service_to=end.isoformat(),
                manual_duplicate_confirmed=0,
            )
            self.store.clear_state(chat_id)
            return self.draft_detail(draft)
        if action == "EDIT_DUE":
            due = _parse_date(text)
            draft = self.store.get_draft(chat_id, draft_id)
            if due < date.fromisoformat(draft["issue_date"]):
                raise ValueError("El vencimiento no puede ser anterior a la emisión")
            draft = self.store.update_draft(
                chat_id, draft_id, due_date=due.isoformat(), manual_duplicate_confirmed=0
            )
            self.store.clear_state(chat_id)
            return self.draft_detail(draft)
        if action == "NEW_CLIENT_CUIT":
            cuit = normalize_cuit(text)
            if not is_valid_cuit(cuit):
                raise ValueError("El CUIT no es válido")
            client = self.gateway.refresh_client(chat_id, cuit)
            self.store.set_state(chat_id, "NEW_TEMPLATE_DESCRIPTION", {"client_id": client["id"]})
            return BotReply(
                f"Encontré a {client['name']}.\n\nEnviame la descripción habitual del servicio."
            )
        if action == "NEW_TEMPLATE_DESCRIPTION":
            if not text or len(text) > 500:
                raise ValueError("La descripción debe tener entre 1 y 500 caracteres")
            self.store.set_state(
                chat_id,
                "NEW_TEMPLATE_AMOUNT",
                {"client_id": payload["client_id"], "description": text},
            )
            return BotReply("Enviame el importe habitual. Ejemplo: 125000")
        if action == "NEW_TEMPLATE_AMOUNT":
            amount = _parse_amount(text)
            with self.store.db.connect() as connection:
                client = connection.execute(
                    "SELECT * FROM invoice_clients WHERE chat_id = ? AND id = ?",
                    (chat_id, int(payload["client_id"])),
                ).fetchone()
            if client is None:
                raise ValueError("El cliente ya no existe")
            base_name = f"{client['name'][:40]} mensual"
            template = self.store.create_template(
                chat_id,
                client["id"],
                base_name,
                payload["description"],
                amount,
            )
            self.store.clear_state(chat_id)
            draft = self.store.create_draft_from_template(
                chat_id, template["id"], self.today_provider()
            )
            return self.draft_detail(draft)
        self.store.clear_state(chat_id)
        return BotReply("La carga anterior venció. Volvé a comenzar con /nueva_factura.")
