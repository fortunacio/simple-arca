from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .arca_http_auth import ArcaHttpAuthenticator
from .ccma import (
    CcmaCredentialsRejected,
    CcmaError,
    CcmaSessionRequired,
    parse_ccma_account,
)
from .db import Database
from .santa_fe import SantaFeIibbConnector, SantaFeIibbWorker


LOGGER = logging.getLogger(__name__)
MONOTRIBUTO_URL = "https://monotributo.afip.gob.ar/app/Inicio.aspx"
CCMA_URL_PART = "/tramites_con_clave_fiscal/ccam/"


def _encryption_key() -> str:
    direct = os.environ.get("SESSION_ENCRYPTION_KEY", "").strip()
    filename = os.environ.get("SESSION_ENCRYPTION_KEY_FILE", "").strip()
    if direct and filename:
        raise SystemExit("Configurar sólo una fuente para SESSION_ENCRYPTION_KEY")
    if filename:
        try:
            direct = Path(filename).read_text(encoding="ascii").strip()
        except OSError as exc:
            raise SystemExit("No pude leer la clave de cifrado de sesiones") from exc
    if not direct:
        raise SystemExit("SESSION_ENCRYPTION_KEY es obligatoria para CCMA")
    return direct


class EncryptedBrowserState:
    def __init__(self, directory: str | Path, encryption_key: str):
        self.directory = Path(directory).expanduser().resolve()
        try:
            self.fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise SystemExit("SESSION_ENCRYPTION_KEY no es una clave Fernet válida") from exc

    def _path(self, chat_id: int) -> Path:
        return self.directory / f"ccma-{chat_id}.enc"

    def load(self, chat_id: int) -> dict | None:
        path = self._path(chat_id)
        try:
            clear = self.fernet.decrypt(path.read_bytes())
            value = json.loads(clear)
        except FileNotFoundError:
            return None
        except (InvalidToken, json.JSONDecodeError, TypeError, ValueError):
            path.unlink(missing_ok=True)
            return None
        return value if isinstance(value, dict) else None

    def save(self, chat_id: int, state: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(chat_id)
        temporary = path.with_suffix(".tmp")
        payload = json.dumps(state, separators=(",", ":")).encode("utf-8")
        temporary.write_bytes(self.fernet.encrypt(payload))
        temporary.chmod(0o600)
        temporary.replace(path)

    def delete(self, chat_id: int) -> None:
        self._path(chat_id).unlink(missing_ok=True)


class EncryptedArcaCredentials:
    def __init__(self, directory: str | Path, encryption_key: str):
        self.directory = Path(directory).expanduser().resolve()
        try:
            self.fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise SystemExit("SESSION_ENCRYPTION_KEY no es una clave Fernet válida") from exc

    def _path(self, chat_id: int) -> Path:
        return self.directory / f"ccma-credentials-{chat_id}.enc"

    def load(self, chat_id: int) -> dict[str, str] | None:
        path = self._path(chat_id)
        try:
            value = json.loads(self.fernet.decrypt(path.read_bytes()))
        except FileNotFoundError:
            return None
        except (InvalidToken, json.JSONDecodeError, TypeError, ValueError):
            path.unlink(missing_ok=True)
            return None
        if not isinstance(value, dict):
            path.unlink(missing_ok=True)
            return None
        cuit = re.sub(r"\D", "", str(value.get("cuit", "")))
        password = value.get("password")
        if len(cuit) != 11 or not isinstance(password, str) or not password:
            path.unlink(missing_ok=True)
            return None
        return {"cuit": cuit, "password": password}

    def save(self, chat_id: int, cuit: str, password: str) -> None:
        if not re.fullmatch(r"\d{11}", cuit) or not password:
            raise ValueError("Las credenciales de ARCA son inválidas")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(chat_id)
        temporary = path.with_suffix(".tmp")
        clear = json.dumps(
            {"version": 1, "cuit": cuit, "password": password},
            separators=(",", ":"),
        ).encode("utf-8")
        temporary.write_bytes(self.fernet.encrypt(clear))
        temporary.chmod(0o600)
        temporary.replace(path)

    def delete(self, chat_id: int) -> None:
        self._path(chat_id).unlink(missing_ok=True)


class CcmaBrowserConnector:
    def __init__(
        self,
        state: EncryptedBrowserState,
        credentials: EncryptedArcaCredentials | None = None,
        authenticator: ArcaHttpAuthenticator | None = None,
    ):
        self.state = state
        self.credentials = credentials
        self.authenticator = authenticator or ArcaHttpAuthenticator()
        self.operation_lock = threading.Lock()

    @staticmethod
    def _launch(playwright, *, headless: bool):
        configured_headless = os.environ.get("CCMA_BROWSER_HEADLESS", "true").strip().lower()
        if headless and configured_headless in {"0", "false", "no"}:
            headless = False
        return playwright.chromium.launch(
            headless=headless,
            args=("--disable-dev-shm-usage", "--no-sandbox"),
        )

    @staticmethod
    def _check_identity(text: str, represented_cuit: str) -> None:
        matches = re.findall(
            r"\bCUIT\b\s*:?\s*(\d{2})[-\s]?(\d{8})[-\s]?(\d)",
            text,
            re.IGNORECASE,
        )
        observed = {"".join(parts) for parts in matches}
        if represented_cuit not in observed:
            raise CcmaError("CCMA devolvió un CUIT distinto del vinculado al chat")

    @staticmethod
    def _open_account(context, button):
        with context.expect_page(timeout=30_000) as popup:
            button.click()
        account = popup.value
        try:
            account.wait_for_url(
                re.compile(r"/tramites_con_clave_fiscal/ccam/"),
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except Exception as exc:
            raise CcmaSessionRequired("ARCA no abrió CCMA con la sesión guardada")
        if CCMA_URL_PART not in account.url:
            raise CcmaSessionRequired("ARCA no abrió CCMA con la sesión guardada")
        return account

    @staticmethod
    def _calculate_account(account) -> None:
        account.get_by_role("button", name="CALCULO DE DEUDA").click()
        account.wait_for_url(re.compile(r"/P04_ctacte\.asp"), timeout=60_000)

    @staticmethod
    def _credentials_were_rejected(text: str) -> bool:
        normalized = " ".join(text.lower().split())
        return bool(
            re.search(
                r"(clave|contrase(?:ñ|n)a).{0,80}(incorrect|inv[aá]lid|err[oó]ne)",
                normalized,
            )
            or re.search(
                r"(incorrect|inv[aá]lid|err[oó]ne).{0,80}(clave|contrase(?:ñ|n)a)",
                normalized,
            )
        )

    def _authenticate(self, page, cuit: str, password: str) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        cookies = self.authenticator.authenticate(cuit, password)
        if not cookies:
            raise CcmaSessionRequired("ARCA no devolvió una sesión autenticada")
        page.context.add_cookies(cookies)
        try:
            page.goto(MONOTRIBUTO_URL, wait_until="domcontentloaded", timeout=60_000)
            account_button = page.get_by_role("button", name="Ver Saldo / Pagar")
            account_button.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeoutError as exc:
            safe_url = page.url.split("?", 1)[0]
            try:
                title = page.title()
            except Exception:
                title = ""
            LOGGER.warning(
                "ARCA no aceptó la sesión HTTP en Monotributo: url=%s título=%s",
                safe_url,
                title,
            )
            raise CcmaSessionRequired(
                "ARCA no aceptó la sesión HTTP en Monotributo"
            ) from exc

    def verify_and_store_credentials(
        self,
        chat_id: int,
        represented_cuit: str,
        password: str,
    ) -> None:
        if self.credentials is None:
            raise CcmaError("El almacén cifrado de credenciales no está configurado")
        from playwright.sync_api import sync_playwright

        if not password:
            raise CcmaError("La clave fiscal está vacía")
        with self.operation_lock, sync_playwright() as playwright:
            browser = self._launch(playwright, headless=True)
            context = browser.new_context()
            page = context.new_page()
            try:
                self._authenticate(page, represented_cuit, password)
                button = page.get_by_role("button", name="Ver Saldo / Pagar")
                account = self._open_account(context, button)
                self._calculate_account(account)
                self._check_identity(account.locator("body").inner_text(), represented_cuit)
                self.credentials.save(chat_id, represented_cuit, password)
                self.state.save(chat_id, context.storage_state())
                LOGGER.info("Credenciales de ARCA verificadas y cifradas para chat_id=%s", chat_id)
            finally:
                context.close()
                browser.close()

    def login(self, chat_id: int, represented_cuit: str) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = self._launch(playwright, headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(MONOTRIBUTO_URL, wait_until="domcontentloaded", timeout=60_000)
            LOGGER.info("Esperando inicio de sesión de CCMA para chat_id=%s", chat_id)
            while True:
                try:
                    button = page.get_by_role("button", name="Ver Saldo / Pagar")
                except Exception:
                    LOGGER.debug("ARCA todavía no completó el ingreso", exc_info=True)
                    time.sleep(2)
                    continue
                if button.is_visible():
                    account = self._open_account(context, button)
                    self._calculate_account(account)
                    self._check_identity(account.locator("body").inner_text(), represented_cuit)
                    self.state.save(chat_id, context.storage_state())
                    LOGGER.info("Sesión de CCMA guardada para chat_id=%s", chat_id)
                    return
                time.sleep(2)

    def sync(self, chat_id: int, represented_cuit: str) -> dict[str, object]:
        with self.operation_lock:
            return self._sync(chat_id, represented_cuit)

    def _sync(self, chat_id: int, represented_cuit: str) -> dict[str, object]:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        storage_state = self.state.load(chat_id)
        credentials = self.credentials.load(chat_id) if self.credentials is not None else None
        if storage_state is None and credentials is None:
            raise CcmaSessionRequired("Falta vincular el acceso de ARCA")
        with sync_playwright() as playwright:
            browser = self._launch(playwright, headless=True)
            context = (
                browser.new_context(storage_state=storage_state)
                if storage_state is not None
                else browser.new_context()
            )
            page = context.new_page()
            try:
                page.goto(MONOTRIBUTO_URL, wait_until="domcontentloaded", timeout=60_000)
                button = page.get_by_role("button", name="Ver Saldo / Pagar")
                if not button.is_visible():
                    if credentials is None:
                        raise CcmaSessionRequired("La sesión de ARCA venció")
                    try:
                        self._authenticate(
                            page,
                            credentials["cuit"],
                            credentials["password"],
                        )
                    except CcmaCredentialsRejected:
                        self.credentials.delete(chat_id)
                        self.state.delete(chat_id)
                        raise
                    button = page.get_by_role("button", name="Ver Saldo / Pagar")
                account = self._open_account(context, button)
                self._calculate_account(account)
                range_selector = account.locator('select[name="rango"]')
                if range_selector.count():
                    range_selector.select_option("48")
                    account.wait_for_timeout(400)
                body = account.locator("body").inner_text()
                self._check_identity(body, represented_cuit)
                rows = account.locator("tr").evaluate_all(
                    """rows => rows.map(row =>
                        Array.from(row.cells || []).map(cell =>
                            (cell.innerText || '').replace(/\\s+/g, ' ').trim()
                        )
                    )"""
                )
                result = parse_ccma_account(body, rows)
                self.state.save(chat_id, context.storage_state())
                return result
            except PlaywrightTimeoutError as exc:
                raise CcmaError("CCMA tardó demasiado en responder") from exc
            finally:
                context.close()
                browser.close()


class CcmaWorker:
    def __init__(
        self,
        db: Database,
        connector: CcmaBrowserConnector,
        *,
        snapshot_ttl: timedelta,
    ):
        self.db = db
        self.connector = connector
        self.snapshot_ttl = snapshot_ttl

    def sync_chat(self, chat_id: int) -> None:
        identity = self.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            raise CcmaError("El chat no tiene un CUIT verificado")
        try:
            payload = self.connector.sync(chat_id, identity["represented_cuit"])
        except CcmaCredentialsRejected as exc:
            self.db.upsert_connection(
                chat_id,
                "ARCA_CCMA",
                "CREDENTIALS_REQUIRED",
                last_error=str(exc),
            )
            raise
        except CcmaSessionRequired as exc:
            self.db.upsert_connection(chat_id, "ARCA_CCMA", "LOGIN_REQUIRED", last_error=str(exc))
            raise
        except Exception as exc:
            self.db.upsert_connection(chat_id, "ARCA_CCMA", "ERROR", last_error=str(exc))
            raise
        fetched_at = datetime.now(timezone.utc)
        self.db.upsert_live_snapshot(
            chat_id,
            "ARCA_CCMA",
            "account_status",
            fetched_at.isoformat(),
            (fetched_at + self.snapshot_ttl).isoformat(),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        self.db.upsert_connection(
            chat_id,
            "ARCA_CCMA",
            "CONNECTED",
            last_success_at=fetched_at.isoformat(),
        )


def _chat_ids(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise SystemExit("CCMA_CHAT_IDS debe contener números separados por coma") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Sincronizador personal de CCMA")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--chat-id", type=int)
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    database = Database(os.environ.get("TAX_MONITOR_DB", "/data/tax_monitor.sqlite3"))
    database.initialize()
    state = EncryptedBrowserState(
        os.environ.get("CCMA_SESSION_DIR", "/portal-state"),
        _encryption_key(),
    )
    credentials = EncryptedArcaCredentials(
        os.environ.get("CCMA_SESSION_DIR", "/portal-state"),
        _encryption_key(),
    )
    connector = CcmaBrowserConnector(state, credentials)
    worker = CcmaWorker(
        database,
        connector,
        snapshot_ttl=timedelta(
            seconds=int(os.environ.get("CCMA_SNAPSHOT_TTL_SECONDS", "86400"))
        ),
    )
    santa_fe_worker = None
    if os.environ.get("SANTA_FE_IIBB_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        santa_fe_worker = SantaFeIibbWorker(
            database,
            SantaFeIibbConnector(credentials),
            snapshot_ttl=timedelta(
                seconds=int(os.environ.get("SANTA_FE_IIBB_SNAPSHOT_TTL_SECONDS", "86400"))
            ),
        )
    configured = _chat_ids(
        os.environ.get("CCMA_CHAT_IDS", "")
        or os.environ.get("INVOICE_EMISSION_CHAT_IDS", "")
    )
    chat_ids = (args.chat_id,) if args.chat_id is not None else configured
    if not chat_ids:
        raise SystemExit("Falta configurar CCMA_CHAT_IDS")

    if args.login:
        for chat_id in chat_ids:
            identity = database.get_tax_identity(chat_id)
            if identity is None or identity["status"] != "VERIFIED":
                raise SystemExit(f"El chat {chat_id} no tiene un CUIT verificado")
            connector.login(chat_id, identity["represented_cuit"])
        return

    enrollment_server = None
    if os.environ.get("ARCA_ENROLLMENT_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        from .enrollment import ArcaEnrollmentServer

        enrollment_server = ArcaEnrollmentServer(
            (
                os.environ.get("ARCA_ENROLLMENT_BIND", "0.0.0.0"),
                int(os.environ.get("ARCA_ENROLLMENT_PORT", "8090")),
            ),
            database,
            connector,
        )
        enrollment_server.start()
        LOGGER.info("Alta privada de ARCA disponible en el puerto %s", enrollment_server.server_port)

    interval = int(os.environ.get("CCMA_SYNC_INTERVAL_SECONDS", "21600"))
    running = True

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        for chat_id in chat_ids:
            try:
                worker.sync_chat(chat_id)
            except CcmaCredentialsRejected:
                LOGGER.error("ARCA rechazó las credenciales guardadas para chat_id=%s", chat_id)
            except CcmaSessionRequired:
                LOGGER.warning("CCMA necesita iniciar sesión para chat_id=%s", chat_id)
            except Exception:
                LOGGER.exception("Falló la sincronización CCMA para chat_id=%s", chat_id)
            if santa_fe_worker is not None:
                try:
                    santa_fe_worker.sync_chat(chat_id)
                except CcmaCredentialsRejected:
                    LOGGER.error(
                        "ARCA rechazó las credenciales de Santa Fe para chat_id=%s",
                        chat_id,
                    )
                except CcmaSessionRequired:
                    LOGGER.warning(
                        "Ingresos Brutos necesita configurar ARCA para chat_id=%s",
                        chat_id,
                    )
                except Exception:
                    LOGGER.exception(
                        "Falló la sincronización de Ingresos Brutos para chat_id=%s",
                        chat_id,
                    )
        if args.once:
            return
        for _ in range(max(1, interval)):
            if not running:
                return
            time.sleep(1)


if __name__ == "__main__":
    main()
