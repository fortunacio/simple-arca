from __future__ import annotations

import hashlib
import html
import logging
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Callable

from .ccma import CcmaCredentialsRejected, CcmaError, CcmaSessionRequired
from .db import Database

if TYPE_CHECKING:
    from .ccma_worker import CcmaBrowserConnector


LOGGER = logging.getLogger(__name__)
TOKEN_PATTERN = re.compile(r"^/arca/setup/([A-Za-z0-9_-]{32,128})$")
PASSWORD_TOGGLE_JS = """const password = document.querySelector('#password');
const toggle = document.querySelector('#toggle-password');
const form = document.querySelector('#arca-form');
const submit = document.querySelector('#submit-credentials');
const status = document.querySelector('#verification-status');
if (password && toggle) {
  toggle.addEventListener('click', () => {
    const show = password.type === 'password';
    password.type = show ? 'text' : 'password';
    toggle.textContent = show ? 'Ocultar contraseña' : 'Mostrar contraseña';
    toggle.setAttribute('aria-pressed', show ? 'true' : 'false');
  });
}
if (form && submit && status) {
  form.addEventListener('submit', () => {
    submit.disabled = true;
    toggle.disabled = true;
    submit.textContent = 'Verificando con ARCA…';
    status.hidden = false;
  });
}
""".encode("utf-8")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _mask_cuit(cuit: str) -> str:
    return f"{cuit[:2]}-••••••••-{cuit[-1:]}"


def _page(title: str, content: str) -> bytes:
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{font:17px system-ui;background:#f4f6f8;color:#1d2733;margin:0;padding:24px}}
main{{max-width:520px;margin:48px auto;background:#fff;padding:28px;border-radius:16px;box-shadow:0 8px 30px #0001}}
label,input,button{{display:block;width:100%;box-sizing:border-box}}
input{{font-size:20px;padding:12px;margin:8px 0 20px}}
button{{font-size:18px;padding:13px;background:#1677c8;color:#fff;border:0;border-radius:8px}}
.secondary{{margin:-10px 0 20px;background:#e8eef3;color:#1d2733}}
button:disabled{{opacity:.65;cursor:wait}}
.status{{padding:12px;margin:18px 0;background:#eef6fc;border-radius:8px;line-height:1.45}}
small{{display:block;margin-top:20px;line-height:1.4;color:#52606d}}
</style></head><body><main><h1>{html.escape(title)}</h1>{content}</main>
<script src="/arca/setup.js" defer></script></body></html>""".encode(
        "utf-8"
    )


class ArcaEnrollmentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        db: Database,
        connector: CcmaBrowserConnector,
        on_verified: Callable[[int], None] | None = None,
    ):
        self.db = db
        self.connector = connector
        self.on_verified = on_verified
        super().__init__(address, ArcaEnrollmentHandler)

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.serve_forever,
            name="arca-enrollment-http",
            daemon=True,
        )
        thread.start()
        return thread

    def refresh_after_verification(self, chat_id: int) -> None:
        if self.on_verified is None:
            return

        def run() -> None:
            try:
                self.on_verified(chat_id)
            except Exception:
                LOGGER.exception(
                    "No se pudo actualizar la cuenta después de guardar el acceso"
                )

        threading.Thread(
            target=run,
            name=f"arca-initial-sync-{chat_id}",
            daemon=True,
        ).start()


class ArcaEnrollmentHandler(BaseHTTPRequestHandler):
    server: ArcaEnrollmentServer

    def log_message(self, _format: str, *_args) -> None:
        LOGGER.debug("Solicitud recibida por el alta privada de ARCA")

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("El navegador cerró la solicitud antes de recibir la respuesta")

    def _token(self) -> str | None:
        path = urllib.parse.urlsplit(self.path).path
        match = TOKEN_PATTERN.fullmatch(path)
        return match.group(1) if match else None

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return
        if path == "/arca/setup.js":
            self._send(
                200,
                PASSWORD_TOGGLE_JS,
                "text/javascript; charset=utf-8",
            )
            return
        token = self._token()
        if token is None:
            self._send(404, _page("Enlace inválido", "<p>Este enlace no existe.</p>"))
            return
        row = self.server.db.get_valid_onboarding_token(
            token_hash(token),
            datetime.now(timezone.utc).isoformat(),
        )
        if row is None:
            self._send(
                410,
                _page("Enlace vencido", "<p>Pedí un enlace nuevo desde Telegram.</p>"),
            )
            return
        content = f"""
<p>CUIT: <strong>{html.escape(_mask_cuit(row['represented_cuit']))}</strong></p>
<form id="arca-form" method="post"><label for="password">Clave fiscal</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button id="toggle-password" class="secondary" type="button" aria-pressed="false">Mostrar contraseña</button>
<button id="submit-credentials" type="submit">Guardar y verificar</button>
<p id="verification-status" class="status" role="status" aria-live="polite" hidden>
Verificando tus datos con ARCA. Puede tardar hasta dos minutos; no cierres esta página.
</p></form>
<small>La clave se envía directamente a este servidor por el enlace privado, se verifica en ARCA y se guarda cifrada. No pasa por Telegram.</small>"""
        self._send(200, _page("Configurar acceso de ARCA", content))

    def do_POST(self) -> None:
        token = self._token()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        content_type = self.headers.get("Content-Type", "")
        if (
            token is None
            or not 0 < length <= 4096
            or not content_type.startswith("application/x-www-form-urlencoded")
        ):
            self._send(400, _page("Solicitud inválida", "<p>Volvé a generar el enlace.</p>"))
            return
        try:
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", errors="strict"),
                keep_blank_values=True,
            )
        except (UnicodeDecodeError, ValueError):
            self._send(400, _page("Solicitud inválida", "<p>Volvé a generar el enlace.</p>"))
            return
        password = (form.get("password") or [""])[0]
        if not password or len(password) > 256:
            self._send(400, _page("Clave inválida", "<p>La clave no puede estar vacía.</p>"))
            return
        row = self.server.db.claim_onboarding_token(
            token_hash(token),
            datetime.now(timezone.utc).isoformat(),
        )
        if row is None:
            self._send(410, _page("Enlace vencido", "<p>Pedí un enlace nuevo desde Telegram.</p>"))
            return
        try:
            self.server.connector.verify_and_store_credentials(
                int(row["chat_id"]),
                row["represented_cuit"],
                password,
            )
        except CcmaCredentialsRejected:
            LOGGER.warning("ARCA rechazó las credenciales para chat_id=%s", row["chat_id"])
            self._send(
                401,
                _page("ARCA rechazó la clave", "<p>Generá un enlace nuevo y volvé a intentarlo.</p>"),
            )
            return
        except CcmaSessionRequired as exc:
            LOGGER.warning(
                "ARCA no completó el ingreso para chat_id=%s: %s",
                row["chat_id"],
                exc,
            )
            self._send(
                409,
                _page(
                    "ARCA no completó el ingreso",
                    f"<p>{html.escape(str(exc))}</p>"
                    "<p>No se guardó la clave.</p>",
                ),
            )
            return
        except CcmaError:
            LOGGER.exception("ARCA no pudo verificar el alta cifrada")
            self._send(
                502,
                _page("ARCA no respondió", "<p>No se guardó la clave. Probá nuevamente más tarde.</p>"),
            )
            return
        except Exception:
            LOGGER.exception(
                "Fallo inesperado durante el alta cifrada para chat_id=%s",
                row["chat_id"],
            )
            self._send(
                502,
                _page(
                    "No pudimos completar la verificación",
                    "<p>No se guardó la clave. Generá un enlace nuevo y volvé a intentarlo.</p>",
                ),
            )
            return
        chat_id = int(row["chat_id"])
        self.server.db.mark_portal_access_verified(
            chat_id,
            str(row["represented_cuit"]),
            datetime.now(timezone.utc).isoformat(),
        )
        self._send(
            200,
            _page(
                "Acceso guardado",
                "<p>La clave quedó verificada y cifrada.</p>"
                "<p>Estoy actualizando Monotributo e Ingresos Brutos. Te aviso "
                "por Telegram cuando la cuenta esté lista.</p>"
                "<p>Ya podés cerrar esta página.</p>",
            ),
        )
        self.server.refresh_after_verification(chat_id)
