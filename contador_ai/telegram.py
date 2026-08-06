from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .db import Database
from .invoice_bot import BotReply, InvoiceBotService
from .service import TaxMonitorService


LOGGER = logging.getLogger(__name__)


class TelegramClient:
    def __init__(
        self,
        token: str,
        db: Database,
        service: TaxMonitorService,
        allowed_chat_ids: frozenset[int],
        invoice_bot: InvoiceBotService | None = None,
    ):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.db = db
        self.service = service
        self.allowed_chat_ids = allowed_chat_ids
        self.invoice_bot = invoice_bot

    def request(self, method: str, payload: dict) -> dict:
        data = urllib.parse.urlencode(payload).encode()
        request = urllib.request.Request(f"{self.base_url}/{method}", data=data)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read())
                description = error.get("description", str(exc))
            except (json.JSONDecodeError, UnicodeDecodeError):
                description = str(exc)
            raise RuntimeError(f"Telegram rechazó {method}: {description}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram no respondió: {exc}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram rechazó {method}: {result}")
        return result["result"]

    @staticmethod
    def _reply_markup(keyboard) -> str:
        def button(label: str, target: str) -> dict[str, str]:
            scheme = urllib.parse.urlsplit(target).scheme
            if scheme in {"http", "https"}:
                return {"text": label, "url": target}
            return {"text": label, "callback_data": target}

        return json.dumps(
            {
                "inline_keyboard": [
                    [button(label, target) for label, target in row]
                    for row in keyboard
                ]
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def send_message(self, chat_id: int, text: str, keyboard=()) -> None:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = self._reply_markup(keyboard)
        self.request("sendMessage", payload)

    def edit_message(self, chat_id: int, message_id: int, text: str, keyboard=()) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = self._reply_markup(keyboard)
        self.request("editMessageText", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        self.request("answerCallbackQuery", payload)

    def send_document(
        self, chat_id: int, content: bytes, filename: str, caption: str = ""
    ) -> None:
        boundary = f"----ContadorAI{uuid.uuid4().hex}"
        pieces = []

        def field(name: str, value: object) -> None:
            pieces.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    str(value).encode("utf-8"),
                    b"\r\n",
                ]
            )

        field("chat_id", chat_id)
        if caption:
            field("caption", caption)
        pieces.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                    "Content-Type: application/pdf\r\n\r\n"
                ).encode("utf-8"),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = urllib.request.Request(
            f"{self.base_url}/sendDocument",
            data=b"".join(pieces),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read())
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram no respondió: {exc}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram rechazó sendDocument: {result}")

    def deliver(
        self,
        chat_id: int,
        reply: BotReply,
        *,
        message_id: int | None = None,
    ) -> None:
        if reply.replace_message and message_id is not None:
            self.edit_message(chat_id, message_id, reply.text, reply.keyboard)
        else:
            self.send_message(chat_id, reply.text, reply.keyboard)
        if reply.document is not None:
            self.send_document(chat_id, reply.document, reply.filename)

    def poll_once(self, timeout: int = 30) -> int:
        offset = int(self.db.get_bot_state("telegram_offset", "0") or 0)
        updates = self.request(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
        )
        handled = 0
        for update in updates:
            next_offset = int(update["update_id"]) + 1
            self.db.set_bot_state("telegram_offset", str(next_offset))
            callback = update.get("callback_query") or {}
            if callback:
                callback_id = callback.get("id")
                message = callback.get("message") or {}
                chat_id = (message.get("chat") or {}).get("id")
                message_id = message.get("message_id")
                data = callback.get("data")
                if not isinstance(chat_id, int) or chat_id not in self.allowed_chat_ids:
                    continue
                if (
                    self.invoice_bot is not None
                    and isinstance(message_id, int)
                    and isinstance(data, str)
                ):
                    try:
                        reply = self.invoice_bot.handle_callback(data, chat_id)
                        self.answer_callback(callback_id)
                        self.deliver(chat_id, reply, message_id=message_id)
                    except Exception as exc:
                        LOGGER.exception("Falló una acción de facturación")
                        self.answer_callback(callback_id, "No pude completar la acción")
                        self.send_message(chat_id, f"No pude completar la acción: {exc}")
                    handled += 1
                continue

            message = update.get("message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            text = message.get("text")
            if not isinstance(chat_id, int) or chat_id not in self.allowed_chat_ids:
                continue
            if not text:
                self.send_message(chat_id, "Por ahora sólo entiendo comandos de texto. Usá /ayuda.")
                continue
            if self.invoice_bot is not None and self.invoice_bot.handles(text, chat_id):
                reply = self.invoice_bot.handle_message(text, chat_id)
                if reply is not None:
                    self.deliver(chat_id, reply)
                    handled += 1
                    continue
            response = self.service.dispatch(text, chat_id)
            command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
            keyboard = ()
            if command == "/configurar_arca":
                setup_url = next(
                    (
                        line.strip()
                        for line in response.splitlines()
                        if urllib.parse.urlsplit(line.strip()).scheme in {"http", "https"}
                    ),
                    "",
                )
                if setup_url:
                    keyboard = ((("Abrir configuración de ARCA", setup_url),),)
            self.send_message(chat_id, response, keyboard)
            handled += 1
        return handled

    def run(self) -> None:
        LOGGER.info("Bot multiusuario iniciado")
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                LOGGER.exception("Falló el ciclo de Telegram; se reintentará")
                time.sleep(3)
