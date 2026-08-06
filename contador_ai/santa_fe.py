from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from .arca_http_auth import ArcaHttpAuthenticator, ArcaHttpLogin, _visible_text
from .ccma import CcmaCredentialsRejected, CcmaSessionRequired
from .db import Database


REGSIM_URL = "https://www.santafe.gob.ar/e-regsim/index.php"
VEPAI_URL = "https://www.santafe.gob.ar/e-vepa/index.php"
ARGENTINA_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")


class SantaFeIibbError(RuntimeError):
    pass


class SantaFeMaintenance(SantaFeIibbError):
    pass


class CredentialsStore(Protocol):
    def load(self, chat_id: int) -> dict[str, str] | None: ...


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._rows = []
        elif self._table_depth == 1 and tag == "tr":
            self._row = []
        elif self._table_depth == 1 and tag in {"td", "th"}:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._table_depth == 1 and tag in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif self._table_depth == 1 and tag == "tr" and self._row is not None:
            if any(self._row) and self._rows is not None:
                self._rows.append(self._row)
            self._row = None
        elif tag == "table":
            if self._table_depth == 1 and self._rows is not None:
                self.tables.append(self._rows)
                self._rows = None
            self._table_depth = max(0, self._table_depth - 1)


def _normalized(value: str) -> str:
    return " ".join(html.unescape(value).replace("\ufeff", " ").split())


def _money(value: str) -> str:
    raw = re.sub(r"[^0-9,.-]", "", value)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return str(Decimal(raw).quantize(Decimal("0.01")))
    except InvalidOperation as exc:
        raise SantaFeIibbError(f"Santa Fe devolvió un importe inválido: {value!r}") from exc


def _iso_date(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()
    except ValueError as exc:
        raise SantaFeIibbError(f"Santa Fe devolvió una fecha inválida: {value!r}") from exc


def _match(text: str, pattern: str, label: str) -> str:
    found = re.search(pattern, text, re.IGNORECASE)
    if not found:
        raise SantaFeIibbError(f"Régimen Simplificado no informó {label}")
    return _normalized(found.group(1))


def _table_rows(html_text: str) -> list[list[str]]:
    parser = _TableParser()
    parser.feed(html_text)
    return [row for table in parser.tables for row in table]


def _parse_payment_rows(html_text: str) -> list[dict[str, str]]:
    payments: list[dict[str, str]] = []
    for row in _table_rows(html_text):
        period_index = next(
            (index for index, cell in enumerate(row) if re.fullmatch(r"20\d{2}/(?:0[1-9]|1[0-2])", cell)),
            None,
        )
        if period_index is None:
            continue
        cells = row[period_index:]
        paid_on = next((cell for cell in cells[1:] if re.fullmatch(r"\d{2}/\d{2}/20\d{2}", cell)), "")
        amount = next((cell for cell in reversed(cells[1:]) if re.search(r"\d", cell) and ("," in cell or "$" in cell)), "")
        if not paid_on or not amount:
            continue
        payments.append(
            {
                "period": cells[0].replace("/", "-"),
                "paid_on": _iso_date(paid_on),
                "amount": _money(amount),
            }
        )
    return payments


def parse_regsim(html_text: str, expected_cuit: str) -> dict[str, object]:
    text = _normalized(_visible_text(html_text))
    observed_cuit = re.sub(
        r"\D",
        "",
        _match(text, r"CUIT:\s*([\d-]{11,13})", "el CUIT"),
    )
    if observed_cuit != expected_cuit:
        raise SantaFeIibbError("Santa Fe devolvió un CUIT distinto del vinculado al chat")
    account = _match(text, r"Identificaci[oó]n:\s*([\d-]+)", "la cuenta de Ingresos Brutos")
    category_match = re.search(
        r"Corresponde encuadre en la Categor[ií]a\s+([IVXLCDM]+):\s*\$?\s*([\d.,]+)\s+mensual",
        text,
        re.IGNORECASE,
    )
    if not category_match:
        raise SantaFeIibbError("Régimen Simplificado no informó la categoría provincial")
    payments_as_of = _match(
        text,
        r"Pagos registrados al\s+(\d{2}/\d{2}/\d{4})",
        "la fecha de actualización de pagos",
    )
    return {
        "regime": "Régimen Simplificado",
        "status": "ACTIVO",
        "account": account,
        "activity_start": _iso_date(
            _match(text, r"Fecha de Inicio actividades:\s*(\d{2}/\d{2}/\d{4})", "el inicio de actividades")
        ),
        "adhesion_date": _iso_date(
            _match(text, r"Fecha Adhesi[oó]n:\s*(\d{2}/\d{2}/\d{4})", "la fecha de adhesión")
        ),
        "effective_period": _match(
            text,
            r"Per[ií]odo Incio de Vigencia:\s*(\d{4}/\d{2})",
            "el período de vigencia",
        ).replace("/", "-"),
        "category": category_match.group(1).upper(),
        "monthly_amount": _money(category_match.group(2)),
        "declared_annual_income": _money(
            _match(text, r"Ingresos Brutos Anuales:\s*\$?\s*([\d.,]+)", "los ingresos anuales declarados")
        ),
        "payments_as_of": _iso_date(payments_as_of),
        "payments": _parse_payment_rows(html_text),
    }


def parse_vepai(html_text: str) -> dict[str, object]:
    text = _normalized(_visible_text(html_text))
    if "horario reservado para tareas de mantenimiento" in text.lower():
        raise SantaFeMaintenance("VEPAI está en mantenimiento")
    lowered = text.lower()
    if "error:" in lowered and not _parse_payment_rows(html_text):
        error = text[lowered.index("error:") :][:240]
        raise SantaFeIibbError(error)
    payments = _parse_payment_rows(html_text)
    if not payments and not any(
        phrase in lowered
        for phrase in ("no se encontraron", "sin pagos", "no registra pagos")
    ):
        raise SantaFeIibbError("VEPAI no devolvió un detalle de pagos reconocible")
    return {"status": "AVAILABLE", "payments": payments}


def _cookie_jar(login: ArcaHttpLogin) -> httpx.Cookies:
    jar = httpx.Cookies()
    for item in login.cookies:
        domain = str(item.get("domain") or "")
        path = str(item.get("path") or "/")
        if domain:
            jar.set(str(item["name"]), str(item["value"]), domain=domain, path=path)
        else:
            jar.set(str(item["name"]), str(item["value"]), path=path)
    return jar


class SantaFeIibbConnector:
    def __init__(
        self,
        credentials: CredentialsStore,
        authenticator: ArcaHttpAuthenticator | None = None,
        *,
        timeout: float = 30,
    ):
        self.credentials = credentials
        self.authenticator = authenticator or ArcaHttpAuthenticator(timeout=timeout)
        self.timeout = timeout

    def _login(self, chat_id: int, represented_cuit: str, system: str) -> ArcaHttpLogin:
        credentials = self.credentials.load(chat_id)
        if credentials is None:
            raise CcmaSessionRequired("Falta guardar la clave fiscal")
        if credentials["cuit"] != represented_cuit:
            raise SantaFeIibbError("La clave guardada pertenece a otro CUIT")
        return self.authenticator.login(represented_cuit, credentials["password"], system)

    def _client(self, login: ArcaHttpLogin) -> httpx.Client:
        return httpx.Client(
            cookies=_cookie_jar(login),
            follow_redirects=True,
            timeout=self.timeout,
            headers={"User-Agent": "Mozilla/5.0", "Referer": login.url},
        )

    def fetch_regsim(self, chat_id: int, represented_cuit: str) -> dict[str, object]:
        login = self._login(chat_id, represented_cuit, "santafe_regsim")
        with self._client(login) as client:
            menu = client.post(
                REGSIM_URL,
                data={"action": "Ingreso::menu", "cuit": represented_cuit},
            )
            menu.raise_for_status()
            response = client.get(f"{REGSIM_URL}?action=Registro::boletasPago")
            response.raise_for_status()
        return parse_regsim(response.text, represented_cuit)

    def fetch_vepai(
        self,
        chat_id: int,
        represented_cuit: str,
        account: str,
        *,
        from_date: date,
        to_date: date,
    ) -> dict[str, object]:
        login = self._login(chat_id, represented_cuit, "santafe_vepai")
        with self._client(login) as client:
            response = client.post(
                VEPAI_URL,
                data={
                    "action": "Ingreso::valido",
                    "cuit": represented_cuit,
                    "tipo_de_pago": "B",
                    "nupartida": re.sub(r"\D", "", account),
                    "solicitud": "",
                    "tipo": "mau",
                    "fechaDesde": from_date.strftime("%d/%m/%Y"),
                    "fechaHasta": to_date.strftime("%d/%m/%Y"),
                },
            )
            response.raise_for_status()
        return parse_vepai(response.text)

    def sync(self, chat_id: int, represented_cuit: str, *, today: date | None = None) -> dict[str, object]:
        current = today or datetime.now(ARGENTINA_TIMEZONE).date()
        profile = self.fetch_regsim(chat_id, represented_cuit)
        try:
            vepai = self.fetch_vepai(
                chat_id,
                represented_cuit,
                str(profile["account"]),
                from_date=date.fromisoformat(str(profile["activity_start"])),
                to_date=current,
            )
        except SantaFeMaintenance:
            vepai = {"status": "MAINTENANCE", "payments": []}
        profile["vepai"] = vepai
        return profile


class SantaFeIibbWorker:
    def __init__(
        self,
        db: Database,
        connector: SantaFeIibbConnector,
        *,
        snapshot_ttl: timedelta,
    ):
        self.db = db
        self.connector = connector
        self.snapshot_ttl = snapshot_ttl

    def sync_chat(self, chat_id: int) -> None:
        identity = self.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            raise SantaFeIibbError("El chat no tiene un CUIT verificado")
        try:
            payload = self.connector.sync(chat_id, identity["represented_cuit"])
        except CcmaCredentialsRejected as exc:
            self.db.upsert_connection(
                chat_id,
                "SANTA_FE_IIBB",
                "CREDENTIALS_REQUIRED",
                last_error=str(exc),
            )
            raise
        except CcmaSessionRequired as exc:
            self.db.upsert_connection(
                chat_id,
                "SANTA_FE_IIBB",
                "LOGIN_REQUIRED",
                last_error=str(exc),
            )
            raise
        except Exception as exc:
            self.db.upsert_connection(chat_id, "SANTA_FE_IIBB", "ERROR", last_error=str(exc))
            raise
        fetched_at = datetime.now(timezone.utc)
        self.db.upsert_live_snapshot(
            chat_id,
            "SANTA_FE_IIBB",
            "account_status",
            fetched_at.isoformat(),
            (fetched_at + self.snapshot_ttl).isoformat(),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        vepai_status = str(payload.get("vepai", {}).get("status", ""))
        self.db.upsert_connection(
            chat_id,
            "SANTA_FE_IIBB",
            "CONNECTED" if vepai_status == "AVAILABLE" else "PARTIAL",
            last_success_at=fetched_at.isoformat(),
            last_error=None if vepai_status == "AVAILABLE" else "VEPAI está en mantenimiento",
        )
