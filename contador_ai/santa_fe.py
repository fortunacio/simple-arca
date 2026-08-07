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


class IibbGenerationUnknown(SantaFeIibbError):
    """La solicitud pudo haber llegado a Santa Fe y no debe repetirse."""


class IibbQuoteChanged(SantaFeIibbError):
    def __init__(self, quote: dict[str, str]):
        super().__init__("El importe cambió antes de generar el VEP")
        self.quote = quote


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


class _InputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "input":
            return
        attributes = dict(attrs)
        key = attributes.get("id") or attributes.get("name")
        if key:
            self.values[key] = html.unescape(attributes.get("value", ""))


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


def _parse_vepai_payments(
    html_text: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    payments: list[dict[str, object]] = []
    groups: list[dict[str, object]] = []
    current_date = ""
    current_total = ""
    current_payment: dict[str, object] | None = None
    current_group: dict[str, object] | None = None

    for row in _table_rows(html_text):
        cells = row + [""] * max(0, 5 - len(row))
        if re.fullmatch(r"\d{2}/\d{2}/20\d{2}", cells[0]):
            current_date = _iso_date(cells[0])
        if cells[1] and re.search(r"\d", cells[1]):
            current_total = _money(cells[1])
            current_group = {
                "paid_on": current_date,
                "total": current_total,
                "periods": [],
            }
            groups.append(current_group)

        period_match = re.fullmatch(r"(20\d{2})-(0[1-9]|1[0-2])M?", cells[2])
        if period_match:
            current_payment = {
                "period": f"{period_match.group(1)}-{period_match.group(2)}",
                "paid_on": current_date,
                "amount": "0.00",
                "payment_total": current_total,
                "components": [],
            }
            payments.append(current_payment)
            if current_group is not None:
                current_group["periods"].append(current_payment["period"])

        if current_payment is None or not cells[3] or not cells[4]:
            continue
        try:
            component_amount = Decimal(_money(cells[4]))
        except SantaFeIibbError:
            continue
        current_payment["components"].append(
            {"description": cells[3], "amount": str(component_amount)}
        )
        current_payment["amount"] = str(
            Decimal(str(current_payment["amount"])) + component_amount
        )
    return payments, groups


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
    payments, groups = _parse_vepai_payments(html_text)
    if not payments and not any(
        phrase in lowered
        for phrase in ("no se encontraron", "sin pagos", "no registra pagos")
    ):
        raise SantaFeIibbError("VEPAI no devolvió un detalle de pagos reconocible")
    return {"status": "AVAILABLE", "payments": payments, "payment_groups": groups}


def parse_period_quote(html_text: str, period: str) -> dict[str, object]:
    parser = _InputParser()
    parser.feed(html_text)
    try:
        due_date = _iso_date(parser.values["fe_vencimi_periodo_vep"])
        amount = _money(parser.values["importe_periodo_vep"])
    except KeyError as exc:
        raise SantaFeIibbError(
            f"Régimen Simplificado no informó el vencimiento de {period}"
        ) from exc
    return {
        "period": period,
        "due_date": due_date,
        "amount": amount,
    }


def parse_interest(value: str) -> str:
    text = _normalized(_visible_text(value))
    if "error" in text.lower():
        raise SantaFeIibbError(text[:240])
    return _money(text)


def _periods_between(first: str, last: str) -> list[str]:
    start_year, start_month = (int(part) for part in first.split("-"))
    end_year, end_month = (int(part) for part in last.split("-"))
    periods: list[str] = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        periods.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return periods


def _two_months_before(value: date) -> str:
    year, month = value.year, value.month - 2
    while month < 1:
        year -= 1
        month += 12
    return f"{year:04d}-{month:02d}"


def _period_offset(value: date, offset: int) -> str:
    year, month = value.year, value.month + offset
    while month < 1:
        year -= 1
        month += 12
    while month > 12:
        year += 1
        month -= 12
    return f"{year:04d}-{month:02d}"


def reconcile_vepai(
    effective_period: str,
    payments: list[dict[str, object]],
    current: date,
    period_quotes: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    through = _two_months_before(current)
    for quote in period_quotes or []:
        try:
            is_due = date.fromisoformat(str(quote.get("due_date", ""))) <= current
        except ValueError:
            continue
        period = str(quote.get("period", ""))
        if is_due and period > through:
            through = period
    expected = _periods_between(effective_period, through)
    paid = {str(payment.get("period", "")) for payment in payments}
    missing = [period for period in expected if period not in paid]
    return {
        "status": "COMPLETE" if not missing else "MISSING",
        "through": through,
        "expected_periods": len(expected),
        "paid_periods": len(set(expected) & paid),
        "missing_periods": missing,
    }


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

    @staticmethod
    def _open_regsim(client: httpx.Client, represented_cuit: str) -> None:
        menu = client.post(
            REGSIM_URL,
            data={"action": "Ingreso::menu", "cuit": represented_cuit},
        )
        menu.raise_for_status()
        form = client.get(f"{REGSIM_URL}?action=Registro::boletasPago")
        form.raise_for_status()

    @staticmethod
    def _quote_with_client(
        client: httpx.Client,
        period: str,
        payment_date: date,
    ) -> dict[str, str]:
        period_response = client.post(
            f"{REGSIM_URL}?header=AJAX&action=Registro::actualizarPeriodoVep",
            data={"periodo_osiris": period.replace("-", "")},
        )
        period_response.raise_for_status()
        base = parse_period_quote(period_response.text, period)
        interest_response = client.post(
            f"{REGSIM_URL}?header=AJAX&action=Registro::actualizarInteres",
            data={
                "vencimi_periodo": datetime.strptime(
                    str(base["due_date"]), "%Y-%m-%d"
                ).strftime("%d/%m/%Y"),
                "fecha_pago": payment_date.strftime("%d/%m/%Y"),
                "importe": str(base["amount"]).replace(".", ","),
            },
        )
        interest_response.raise_for_status()
        interest = parse_interest(interest_response.text)
        principal = str(base["amount"])
        return {
            "period": period,
            "due_date": str(base["due_date"]),
            "payment_date": payment_date.isoformat(),
            "principal": principal,
            "interest": interest,
            "total": str(Decimal(principal) + Decimal(interest)),
        }

    def quote_payment(
        self,
        chat_id: int,
        represented_cuit: str,
        *,
        period: str,
        payment_date: date,
    ) -> dict[str, str]:
        current = datetime.now(ARGENTINA_TIMEZONE).date()
        if payment_date < current or payment_date > current + timedelta(days=25):
            raise SantaFeIibbError("La fecha de pago debe estar entre hoy y los próximos 25 días")
        login = self._login(chat_id, represented_cuit, "santafe_regsim")
        with self._client(login) as client:
            self._open_regsim(client, represented_cuit)
            return self._quote_with_client(client, period, payment_date)

    def generate_vep(
        self,
        chat_id: int,
        represented_cuit: str,
        *,
        account: str,
        period: str,
        payment_date: date,
        network_id: str,
        expected_quote: dict[str, str],
    ) -> dict[str, object]:
        if network_id not in {"1001", "1002", "1003"}:
            raise SantaFeIibbError("La red de pago no es válida")
        login = self._login(chat_id, represented_cuit, "santafe_regsim")
        with self._client(login) as client:
            self._open_regsim(client, represented_cuit)
            quote = self._quote_with_client(client, period, payment_date)
            comparable = ("due_date", "principal", "interest", "total")
            if any(str(quote[key]) != str(expected_quote.get(key, "")) for key in comparable):
                raise IibbQuoteChanged(quote)
            try:
                response = client.post(
                    f"{REGSIM_URL}?header=AJAX&action=Registro::liquidar_VEP",
                    data={
                        "nro_iibb": account,
                        "id_entidad": network_id,
                        "periodo": period.replace("-", ""),
                        "importe": quote["principal"].replace(".", ","),
                        "fe_vencimi": datetime.strptime(
                            quote["due_date"], "%Y-%m-%d"
                        ).strftime("%d/%m/%Y"),
                        "fe_pago": payment_date.strftime("%d/%m/%Y"),
                        "interes": quote["interest"].replace(".", ","),
                    },
                )
                response.raise_for_status()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise IibbGenerationUnknown(
                    "No se pudo confirmar si Santa Fe generó el VEP"
                ) from exc
            body = _normalized(_visible_text(response.text))
            if body.upper().startswith("ERROR;"):
                raise SantaFeIibbError(body.split(";", 1)[1][:240])
            match = re.search(r"\b\d{6,}\b", body)
            if not match:
                raise IibbGenerationUnknown(
                    "Santa Fe respondió sin un número de VEP reconocible"
                )
            vep_number = match.group(0)
            document = client.get(
                f"{REGSIM_URL}?action=ImpresionReportes::imprimirVEP&nro_vep={vep_number}"
            )
            document.raise_for_status()
            if not document.content.startswith(b"%PDF"):
                raise SantaFeIibbError("Santa Fe no devolvió el comprobante PDF del VEP")
            return {"vep_number": vep_number, "quote": quote, "pdf": document.content}

    def fetch_regsim(
        self,
        chat_id: int,
        represented_cuit: str,
        *,
        current: date,
    ) -> dict[str, object]:
        login = self._login(chat_id, represented_cuit, "santafe_regsim")
        with self._client(login) as client:
            self._open_regsim(client, represented_cuit)
            response = client.get(f"{REGSIM_URL}?action=Registro::boletasPago")
            response.raise_for_status()
            quotes = []
            for period in (_period_offset(current, -1), _period_offset(current, 0)):
                quote = client.post(
                    f"{REGSIM_URL}?header=AJAX&action=Registro::actualizarPeriodoVep",
                    data={"periodo_osiris": period.replace("-", "")},
                )
                quote.raise_for_status()
                quotes.append(parse_period_quote(quote.text, period))
        result = parse_regsim(response.text, represented_cuit)
        result["period_quotes"] = quotes
        return result

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
        profile = self.fetch_regsim(chat_id, represented_cuit, current=current)
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
        if vepai.get("status") == "AVAILABLE":
            vepai["reconciliation"] = reconcile_vepai(
                str(profile["effective_period"]),
                list(vepai.get("payments", [])),
                current,
                list(profile.get("period_quotes", [])),
            )
            paid_periods = {
                str(payment.get("period", "")) for payment in vepai.get("payments", [])
            }
            obligations = [
                quote
                for quote in profile.get("period_quotes", [])
                if str(quote.get("period", "")) not in paid_periods
            ]
            if obligations:
                next_obligation = min(obligations, key=lambda quote: str(quote["due_date"]))
                next_obligation["overdue"] = (
                    date.fromisoformat(str(next_obligation["due_date"])) < current
                )
                profile["next_obligation"] = next_obligation
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
