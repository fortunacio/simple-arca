"""Santa Fe SIAT web forms. Routes and state are read from each live page."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import httpx
from pypdf import PdfReader

from .external import ExternalContractError, ExternalDependencyError, checked_request, failure_detail
from .santa_fe import (
    ARGENTINA_TIMEZONE, IibbGenerationUnknown, IibbQuoteChanged,
    SantaFeIibbConnector, SantaFeIibbError, _external_payload, _iso_date, _money,
    reconcile_vepai,
)

HOST = "www.santafe.gob.ar"
ORIGIN = f"https://{HOST}"
MAX_VEP_PDF_BYTES = 5 * 1024 * 1024


def contract(stage: str):
    return ExternalContractError(host=HOST, stage=f"siat_{stage}")


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list = field(default_factory=list)

    def text(self) -> str:
        if self.tag in {"script", "style"}:
            return ""
        return " ".join(
            " ".join(c.text() if isinstance(c, Node) else c for c in self.children).split()
        )

    def find(self, tag: str | None = None, **attrs) -> list[Node]:
        result = []
        for child in self.children:
            if not isinstance(child, Node):
                continue
            if (tag is None or child.tag == tag) and all(child.attrs.get(k) == v for k, v in attrs.items()):
                result.append(child)
            result.extend(child.find(tag, **attrs))
        return result


class Page(HTMLParser):
    def __init__(self, html: str, url: str = "https://www.santafe.gob.ar/siat/"):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.root = Node("root", {})
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict((k, v or "") for k, v in attrs))
        self.stack[-1].children.append(node)
        if tag not in {"input", "br", "hr", "img", "meta", "link", "area", "base", "wbr", "source", "param", "embed", "col"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def form(self, ident: str) -> Node:
        matches = self.root.find("form", id=ident)
        if len(matches) != 1:
            raise contract("form")
        return matches[0]

    def fields(self, ident: str) -> dict[str, str]:
        result = {}
        for node in self.form(ident).find():
            name = node.attrs.get("name")
            if not name or "disabled" in node.attrs:
                continue
            if node.tag == "input":
                kind = node.attrs.get("type", "text").lower()
                if kind in {"button", "submit", "reset", "file", "image"}:
                    continue
                if kind in {"checkbox", "radio"} and "checked" not in node.attrs:
                    continue
                result[name] = node.attrs.get("value", "")
            elif node.tag == "select":
                options = node.find("option")
                selected = next((o for o in options if "selected" in o.attrs), options[0] if options else None)
                if selected:
                    result[name] = selected.attrs.get("value", selected.text())
            elif node.tag == "textarea":
                result[name] = node.text()
        return result

    def action(self, ident: str) -> str:
        return urljoin(self.url, self.form(ident).attrs.get("action", ""))

    def scripts(self) -> str:
        return "\n".join("".join(c for c in n.children if isinstance(c, str)) for n in self.root.find("script"))


def official_url(base: str, target: str) -> str:
    url = urljoin(base, target)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != HOST or not parts.path.startswith(("/siat/", "/e-vep-siat/")):
        raise contract("destination")
    return url


def _required(pattern: str, value: str, stage: str) -> str:
    match = re.search(pattern, value, re.IGNORECASE)
    if not match:
        raise contract(stage)
    return match.group(1)


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def parse_vep_text(text: str, cuit: str, quote: dict) -> dict[str, str]:
    """Validate the actual SIAT receipt, not just its PDF signature.

    Live SIAT currently prepares form 5426 but emits a 5439 receipt. SETI
    independently identifies that same VEP as SF-RGSmm/yy.
    """
    text = " ".join(text.split())
    number = _required(r"N[º°] de VEP:\s*(\d{6,12})\b", text, "pdf_number")
    form = _required(r"Formulario:\s*(\d+)\b", text, "pdf_form")
    observed = _required(r"CUIT:\s*([\d-]{11,13})\b", text, "pdf_identity")
    generated_by = _required(r"Generado por el usuario:\s*([\d-]{11,13})\b", text, "pdf_identity")
    period = _required(r"Período:\s*(\d{4}-\d{2})\b", text, "pdf_period")
    expires = _required(r"Día de expiración:\s*(\d{2}/\d{2}/\d{4})\b", text, "pdf_expiration")
    total = _required(r"Importe total a pagar:\s*([\d.,]+)\b", text, "pdf_total")
    concept = _required(r"\bConcepto:\s*(\d+)\b", text, "pdf_concept")
    subconcept = _required(r"\bSubconcepto:\s*(\d+)\b", text, "pdf_concept")
    if (form != "5439" or not re.search(r"Tipo de Pago:\s*Ingresos Brutos\b", text)
            or concept != "19" or subconcept != "19"
            or _digits(observed) != cuit or _digits(generated_by) != cuit
            or period != quote["period"]):
        raise contract("pdf_identity")
    try:
        if _iso_date(expires) != quote["vep_expires_on"] or Decimal(_money(total)) != Decimal(quote["total"]):
            raise contract("pdf_values")
    except (ValueError, SantaFeIibbError) as exc:
        raise contract("pdf_values") from exc
    return {"vep_number": number, "form": form}


def parse_vep_pdf(document: bytes, cuit: str, quote: dict) -> dict[str, str]:
    """Read the single official receipt in memory, without rewriting it."""
    if (not document.startswith(b"%PDF-") or len(document) > MAX_VEP_PDF_BYTES
            or b"%%EOF" not in document[-1024:]):
        raise contract("pdf_document")
    try:
        reader = PdfReader(BytesIO(document), strict=True)
        if reader.is_encrypted or len(reader.pages) != 1:
            raise contract("pdf_document")
        text = reader.pages[0].extract_text()
        if not text or len(text) > 100_000:
            raise contract("pdf_text")
        return parse_vep_text(text, cuit, quote)
    except ExternalContractError:
        raise
    except Exception as exc:
        # Parser diagnostics may contain fiscal data; expose a bounded stage only.
        raise contract("pdf_document") from exc


def parse_account(menu: Page, page: Page, cuit: str, current: date) -> dict:
    menu_text, text = menu.root.text(), page.root.text()
    observed = _required(r"CUIT\s*([\d-]{11,13})", menu_text, "identity")
    account = _required(r"Número de Cuenta\s*(\d+)", menu_text, "account")
    category = _required(r"R[eé]gimen\s+Simplificado\s*/\s*(\d+)", menu_text, "regime")
    if _digits(observed) != cuit or not re.search(r"Número:\s*" + re.escape(account) + r"\b", text):
        raise contract("identity")
    if _digits(_required(r"Número de CUIT\s*([\d-]{11,13})", text, "identity")) != cuit:
        raise contract("identity")
    if "Estado de Cuenta" not in text or "Gestión Administrativa" not in text:
        raise contract("account")

    expected_headers = ["Período Deuda", "Fecha de Vencimiento", "Impuesto", "Fecha Pago", "Otros Pagos", "Importe Pago", "Saldo", "Actualización", "Total"]
    tables = [t for t in page.root.find("table") if all(h in t.text() for h in expected_headers)]
    if not tables:
        raise contract("debt_table")
    rows = []
    for row in tables[-1].find("tr"):
        cells = [c for c in row.children if isinstance(c, Node) and c.tag == "td"]
        matches = [(i, re.fullmatch(r"(\d{2})/(\d{4}) RS", c.text())) for i, c in enumerate(cells)]
        matches = [(i, m) for i, m in matches if m]
        if not matches:
            continue
        i, m = matches[0]
        values = [c.text() for c in cells[i:]]
        # The empty separator after Impuesto is present in the official table.
        if len(values) != 10 or values[3]:
            raise contract("debt_row")
        selector = row.find("input", name="listIdDeudaSelected")
        amounts = [_money(values[j]) for j in (2, 5, 6, 7, 8, 9)]
        if any(not Decimal(v).is_finite() or Decimal(v) < 0 for v in amounts):
            raise contract("debt_amount")
        if Decimal(amounts[3]) + Decimal(amounts[4]) != Decimal(amounts[5]):
            raise contract("debt_total")
        rows.append({
            "period": f"{m.group(2)}-{m.group(1)}", "due_date": _iso_date(values[1]),
            "tax": amounts[0], "paid_on": _iso_date(values[4]) if values[4] else "",
            "other_payments": amounts[1], "paid_amount": amounts[2], "amount": amounts[3],
            "interest": amounts[4], "total": amounts[5],
            "selection": selector[0].attrs.get("value", "") if len(selector) == 1 else "",
        })
    if not rows or len({r["period"] for r in rows}) != len(rows):
        raise contract("debt_periods")
    active_rows = [r for r in rows if r["period"] <= current.strftime("%Y-%m")]
    if not active_rows:
        raise contract("current_period")
    latest = max(active_rows, key=lambda r: r["period"])
    adhesion = _iso_date(_required(r"Regimen simplificado fecha adhesión\s*(\d{2}/\d{2}/\d{4})", text, "adhesion"))
    return {
        "source": "SIAT", "regime": "Régimen Simplificado", "status": "ACTIVO",
        "account": account, "category": category, "monthly_amount": latest["tax"],
        "activity_start": _iso_date(_required(r"Fecha de Inicio de Actividad\s*(\d{2}/\d{2}/\d{4})", text, "activity_start")),
        "adhesion_date": adhesion, "effective_period": adhesion[:7],
        "declared_annual_income": _money(_required(r"Regimen simplificado ingreso bruto anual gravado\s*([\d.,]+)", text, "income")),
        "payments_as_of": current.isoformat(),
        "payments": [{"period": r["period"], "paid_on": r["paid_on"], "amount": r["paid_amount"]} for r in rows if r["paid_on"] and Decimal(r["total"]) == 0],
        "period_quotes": [{"period": r["period"], "due_date": r["due_date"], "amount": r["amount"]} for r in rows],
        "siat_obligations": rows,
    }


class SiatIibbConnector(SantaFeIibbConnector):
    """Uses the existing credential store and VEPAI connector; no browser needed."""

    @staticmethod
    def _request(client, method, url, *, stage, **kwargs) -> Page:
        url = official_url(ORIGIN, url)
        response = checked_request(lambda: client.request(method, url, **kwargs), stage=f"siat_{stage}")
        official_url(ORIGIN, str(response.url))
        return Page(response.text, str(response.url))

    def _submit(self, client, page, ident, values, *, stage, url=None, method="POST"):
        data = page.fields(ident)
        data.update(values)
        return self._request(client, method, url or page.action(ident), stage=stage,
                             headers={"Referer": page.url}, **({"params": data} if method == "GET" else {"data": data}))

    def _account(self, client, login, cuit, current):
        entry = Page(login.html, login.url)
        selects = entry.root.find("select", name="idCuitSeleccionado")
        if len(selects) != 1:
            raise contract("representation")
        options = [o for o in selects[0].find("option") if _digits(o.text()) == cuit]
        if len(options) != 1:
            raise contract("representation")
        target = _required(r"wl\.get\('f1',\s*'([^']+)'", selects[0].attrs.get("onchange", ""), "representation_action")
        menu = self._submit(client, entry, "f1", {"idCuitSeleccionado": options[0].attrs["value"]}, stage="representation", url=target, method="GET")
        links = [n.attrs["href"] for n in menu.root.find("a") if "/gde/seleccionarcuenta/" in n.attrs.get("href", "")]
        if len(links) != 1:
            raise contract("account_link")
        selection = self._request(client, "GET", links[0], stage="account_selection")
        # This server-generated JS only redirects to the selected account.
        matches = re.findall(r"irAEstadoCuenta\('([0-9]+)',\s*'([0-9]+)'\)", selection.scripts())
        if len(matches) != 1 or matches[0][1] != "15":
            raise contract("account_selection")
        account, resource = matches[0]
        mrk = urlsplit(selection.url).path.rsplit("/", 1)[-1]
        page = self._request(client, "GET", "/siat/gde/AdministrarLiqDeuda.do", stage="account", params={
            "method": "ingresarLiqDeudaGr", "cuenta.numeroCuenta": account,
            "cuenta.idRecurso": resource, "afm": mrk, "back": f"/app/afip/pwlocalcuit?mrk={mrk}",
        })
        profile = _external_payload(
            lambda: parse_account(menu, page, cuit, current), stage="siat_account_contract"
        )
        return profile, page

    def fetch_regsim(self, chat_id, represented_cuit, *, current):
        login = self._login(chat_id, represented_cuit, "santafe_pwlocal")
        with self._client(login) as client:
            profile, _ = self._account(client, login, represented_cuit, current)
        return profile

    def sync(self, chat_id, represented_cuit, *, today=None):
        current = today or datetime.now(ARGENTINA_TIMEZONE).date()
        profile = self.fetch_regsim(chat_id, represented_cuit, current=current)
        try:
            vepai = self.fetch_vepai(
                chat_id, represented_cuit, profile["account"],
                from_date=date.fromisoformat(profile["activity_start"]), to_date=current,
            )
        except ExternalDependencyError as exc:
            # A separate payment-register outage must not discard a valid account.
            vepai = {"status": "UNAVAILABLE", "failure": failure_detail(exc).to_json()}
        paid_periods = set()
        if vepai.get("status") == "AVAILABLE":
            vepai["reconciliation"] = reconcile_vepai(
                profile["effective_period"], list(vepai.get("payments", [])), current,
                profile["period_quotes"],
            )
            paid_periods = {p["period"] for p in vepai.get("payments", [])}
        # SIAT's actual balance takes precedence over missing VEPAI history.
        # A registered VEPAI payment suppresses payment while SIAT catches up.
        pending = [r for r in profile["siat_obligations"]
                   if Decimal(r["total"]) > 0 and r["period"] not in paid_periods]
        if pending:
            debt = min(pending, key=lambda r: (r["due_date"], r["period"]))
            profile["next_obligation"] = {
                "period": debt["period"], "due_date": debt["due_date"],
                "amount": debt["amount"], "overdue": date.fromisoformat(debt["due_date"]) < current,
            }
        profile["vepai"] = vepai
        return profile

    def _prepare(self, client, login, cuit, period, payment_date, account=None):
        current = datetime.now(ARGENTINA_TIMEZONE).date()
        if payment_date < current or payment_date > current + timedelta(days=25):
            raise SantaFeIibbError("La fecha de pago debe estar entre hoy y los próximos 25 días")
        profile, page = self._account(client, login, cuit, current)
        if account is not None and _digits(profile["account"]) != _digits(account):
            raise contract("payment_account")
        rows = [r for r in profile["siat_obligations"] if r["period"] == period]
        if len(rows) != 1 or Decimal(rows[0]["total"]) <= 0 or not rows[0]["selection"]:
            raise SantaFeIibbError("SIAT no informa una obligación pendiente para ese período")
        debt = rows[0]
        action = next((n.attrs.get("onclick", "") for n in page.root.find("button", name="btnReimp")), "")
        selected = _required(r"submitForm\('reconfeccionar',\s*'([0-9]+)'\)", action, "reprint_action")
        page = self._submit(client, page, "filter", {"method": "reconfeccionar", "selectedId": selected, "listIdDeudaSelected": debt["selection"]}, stage="quote_selection")
        if not any("submitForm('generarVep'" in n.attrs.get("onclick", "") for n in page.root.find("a")):
            raise contract("quote_action")
        page = self._submit(client, page, "filter", {"method": "generarVep", "selectedId": ""}, stage="quote")
        data = page.fields("f1")
        if any(data.get(k) != v for k, v in {"contribuyenteCUIT": cuit, "usuarioCUIT": cuit,
                "numeroCuenta": profile["account"], "periodoFiscal": period.replace("-", ""), "nroFormulario": "5426"}.items()):
            raise contract("quote_identity")
        try:
            principal, interest, total = (_money(data[k]) for k in ("importe_imp", "importe_int", "importeTotal"))
            expires = date.fromisoformat(data["fechaExpiracion"])
        except (KeyError, ValueError, SantaFeIibbError) as exc:
            raise contract("quote_values") from exc
        if (Decimal(principal) + Decimal(interest) != Decimal(total)
                or Decimal(principal) != Decimal(debt["amount"])
                or any(not Decimal(v).is_finite() or Decimal(v) < 0 for v in (principal, interest, total))):
            raise contract("quote_total")
        if payment_date > expires:
            raise SantaFeIibbError("La fecha solicitada supera la fecha de pago calculada por SIAT")
        quote = {"period": period, "due_date": debt["due_date"], "payment_date": payment_date.isoformat(),
                 "principal": principal, "interest": interest, "total": total, "vep_expires_on": expires.isoformat()}
        return quote, page

    def quote_payment(self, chat_id, represented_cuit, *, period, payment_date):
        login = self._login(chat_id, represented_cuit, "santafe_pwlocal")
        with self._client(login) as client:
            quote, _ = self._prepare(client, login, represented_cuit, period, payment_date)
        return quote

    @staticmethod
    def _emit_pdf(client, page, network_id, before_emit=None):
        if network_id not in {"1001", "1002", "1003"}:
            raise SantaFeIibbError("La red de pago no es válida")
        if len(page.form("f1").find("input", name="datos.destino", value=network_id)) != 1:
            raise contract("emission_network")
        actions = [n.attrs["onclick"] for n in page.root.find("a")
                   if "gestionVEP::liquidar_VEP" in n.attrs.get("onclick", "")]
        if len(actions) != 1:
            raise contract("emission_action")
        action = _required(
            r"wl\.confirmPopUpX\('[^']*',\s*'post',\s*'f1',\s*'([^']+)'\)",
            actions[0], "emission_action",
        )
        target = official_url(page.url, action)
        fields = page.fields("f1")
        fields["datos.destino"] = network_id
        if before_emit is not None:
            before_emit()
        # No redirects: a 307/308 could repeat this non-idempotent POST.
        try:
            response = client.post(target, data=fields, headers={"Referer": page.url},
                                   follow_redirects=False)
        except httpx.HTTPError as exc:
            raise IibbGenerationUnknown(
                "No se pudo confirmar si SIAT generó el VEP; no se reintentará",
                diagnostic={"stage": "emission_transport", "error": type(exc).__name__},
            ) from exc
        media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if (response.status_code != 200
                or not response.content.startswith(b"%PDF-")
                or b"%%EOF" not in response.content[-1024:]
                or len(response.content) > MAX_VEP_PDF_BYTES):
            raise IibbGenerationUnknown(
                "SIAT no devolvió el PDF esperado después de emitir; no se reintentará",
                pdf=(response.content if response.content.startswith(b"%PDF-")
                     and len(response.content) <= MAX_VEP_PDF_BYTES else None),
                diagnostic={
                    "stage": "emission_response", "http_status": response.status_code,
                    # Never persist response bodies, URLs, cookies or arbitrary headers.
                    "media_type": (media_type if media_type in {
                        "application/pdf", "application/octet-stream", "application/json",
                        "text/html", "text/plain",
                    } else "other"),
                    "response_bytes": len(response.content),
                    "pdf_signature": response.content.startswith(b"%PDF-"),
                    "pdf_eof": b"%%EOF" in response.content[-1024:],
                },
            )
        return response.content

    def generate_vep(self, chat_id, represented_cuit, *, account, period,
                     payment_date, network_id, expected_quote, before_emit=None):
        if network_id not in {"1001", "1002", "1003"}:
            raise SantaFeIibbError("La red de pago no es válida")
        login = self._login(chat_id, represented_cuit, "santafe_pwlocal")
        with self._client(login) as client:
            quote, page = self._prepare(
                client, login, represented_cuit, period, payment_date, account=account,
            )
            comparable = ("due_date", "principal", "interest", "total", "vep_expires_on")
            if any(str(quote[k]) != str(expected_quote.get(k, "")) for k in comparable):
                raise IibbQuoteChanged(quote)
            document = self._emit_pdf(client, page, network_id, before_emit)
            try:
                receipt = parse_vep_pdf(document, represented_cuit, quote)
            except Exception as exc:
                # The POST already happened. Never allow another confirmation to
                # repeat it, even when only extraction/validation failed.
                raise IibbGenerationUnknown(
                    "SIAT devolvió un PDF pero no se pudo validar el VEP; no se reintentará",
                    pdf=document,
                    diagnostic={"stage": (exc.stage if isinstance(exc, ExternalContractError)
                                          else "pdf_validation"), "error": type(exc).__name__},
                ) from exc
            return {"vep_number": receipt["vep_number"], "quote": quote, "pdf": document}
