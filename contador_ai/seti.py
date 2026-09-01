from __future__ import annotations

from collections import defaultdict
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import urlencode

from .db import Database
from .external import ExternalContractError, ExternalUnavailable


SANTA_FE_IIBB_DESCRIPTION = re.compile(
    r"^SF-RGS(?P<month>0[1-9]|1[0-2])/(?P<year>\d{2})$",
    re.IGNORECASE,
)


class SetiError(RuntimeError):
    pass


class SetiConnector(Protocol):
    def sync(self, chat_id: int, represented_cuit: str) -> dict[str, object]: ...


class CredentialsStore(Protocol):
    def load(self, chat_id: int) -> dict[str, str] | None: ...


class BrowserSetiConnector:
    PORTAL_URL = "https://portalcf.cloud.afip.gob.ar/portal/app/"
    SETI_HOME = "https://seti.afip.gob.ar/setiweb/#/inicio"
    PAYMENTS_API = "https://seti.afip.gob.ar/setiweb/api/pagos"

    def __init__(self, credentials: CredentialsStore):
        self.credentials = credentials
        self.operation_lock = threading.Lock()

    @staticmethod
    def _launch(playwright):
        headless = os.environ.get("CCMA_BROWSER_HEADLESS", "true").strip().lower()
        return playwright.chromium.launch(
            headless=headless not in {"0", "false", "no"},
            args=("--disable-dev-shm-usage", "--no-sandbox"),
        )

    @staticmethod
    def _normalize_payment(item: dict[str, object]) -> dict[str, object]:
        paid_at = str(item.get("fechaDePago") or "")
        state = int(item.get("estado", -1))
        number = str(item.get("nroVep") or "")
        description = str(item.get("descripcion") or "").strip()
        iibb_match = SANTA_FE_IIBB_DESCRIPTION.fullmatch(description)
        return {
            "number": number,
            "status": "PAID" if state == 2 else "UNKNOWN",
            "seti_state": state,
            "amount": str(_decimal(item.get("importe", "0"))),
            "description": description,
            "paid_on": paid_at[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", paid_at) else "",
            "paid_at": paid_at,
            "payment_entity": str(item.get("entidadPago") or ""),
            "is_consolidated": bool(item.get("isSuperVep")),
            "is_sub_vep": bool(item.get("isSubVep")),
            "parent_number": str(item.get("superVep") or ""),
            "tax_type": "SANTA_FE_IIBB" if iibb_match else "ARCA",
            "tax_period": (
                f"20{iibb_match.group('year')}-{iibb_match.group('month')}"
                if iibb_match
                else ""
            ),
            "components": [],
        }

    @staticmethod
    def _query_url(cuit: str, *, number: str = "0") -> str:
        params = {
            "usuario": "TODOS",
            "contribuyente": cuit,
            "estado": "seticommon.todos.rotulo",
            "tipoDePago": "TODOS",
            "mes": "-",
            "anio": "-",
            "nroVep": number,
            "transaccion": "0",
            "ultimosMeses": "12",
            "orden": json.dumps({"valor": "vepId", "tipo": "desc"}, separators=(",", ":")),
            "pagina": "1",
            "totalPagina": "20",
            "cantTotal": "0",
        }
        return f"{BrowserSetiConnector.PAYMENTS_API}?{urlencode(params)}"

    def sync(self, chat_id: int, represented_cuit: str) -> dict[str, object]:
        from playwright.sync_api import sync_playwright

        credentials = self.credentials.load(chat_id)
        if credentials is None:
            raise SetiError("Falta guardar la clave fiscal para consultar SETI")
        if credentials["cuit"] != represented_cuit:
            raise SetiError("La clave guardada corresponde a otro CUIT")

        with self.operation_lock, sync_playwright() as playwright:
            browser = self._launch(playwright)
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(
                    "https://auth.afip.gov.ar/contribuyente_/login.xhtml",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.locator('input[name="F1:username"]').fill(credentials["cuit"])
                page.locator('input[name="F1:btnSiguiente"]').click()
                password = page.locator('input[name="F1:password"]')
                password.wait_for(timeout=30_000)
                password.fill(credentials["password"])
                page.locator('input[name="F1:btnIngresar"]').click()
                page.wait_for_url("**/portal/app/**", wait_until="domcontentloaded", timeout=60_000)
                search = page.locator("#buscadorInput")
                search.wait_for(timeout=30_000)
                search.fill("Presentación de DDJJ y Pagos")
                result = page.locator("p.small.text-muted").filter(
                    has_text=re.compile("Presentaci.n de DDJJ y Pagos", re.IGNORECASE)
                ).first
                result.wait_for(timeout=30_000)
                with context.expect_page(timeout=30_000) as popup:
                    result.click()
                seti = popup.value
                seti.wait_for_url("**/setiweb/**", wait_until="domcontentloaded", timeout=60_000)
                if "Unauthorized" in seti.title():
                    raise SetiError("SETI rechazó la autorización emitida por el portal")

                response = seti.request.get(self._query_url(represented_cuit))
                if not response.ok:
                    raise SetiError(f"SETI no respondió la consulta de pagos ({response.status})")
                envelope = response.json()
                if not envelope.get("success"):
                    raise SetiError(str(envelope.get("error") or "SETI rechazó la consulta"))
                data = envelope.get("data", {})
                observed_cuit = {
                    re.sub(r"\D", "", str(item.get("cuit", "")))
                    for item in data.get("vepsConsultados", [])
                }
                if observed_cuit and observed_cuit != {represented_cuit}:
                    raise SetiError("SETI devolvió un CUIT distinto del vinculado al chat")
                return {
                    "window_months": 12,
                    "veps": [
                        self._normalize_payment(item)
                        for item in data.get("vepsConsultados", [])
                    ],
                }
            finally:
                context.close()
                browser.close()


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise SetiError(f"SETI devolvió un importe inválido: {value!r}") from exc


def reconcile_vep_with_ccma(vep: dict, ccma: dict) -> dict[str, object]:
    """Compara importes pagados en SETI con movimientos 'Pago' visibles en CCMA."""

    paid_on = str(vep.get("paid_on", ""))
    paid_components = [
        component
        for component in vep.get("components", [])
        if str(component.get("status", "")).upper() == "PAID"
    ]
    available: dict[Decimal, list[dict]] = defaultdict(list)
    for payment in ccma.get("payments", []):
        if paid_on and str(payment.get("paid_on", "")) != paid_on:
            continue
        available[_decimal(payment.get("amount", "0"))].append(payment)

    matched: list[dict] = []
    unmatched: list[dict] = []
    for component in paid_components:
        amount = _decimal(component.get("amount", "0"))
        candidates = available.get(amount, [])
        if candidates:
            matched.append(component)
            candidates.pop()
        else:
            unmatched.append(component)

    matched_total = sum(
        (_decimal(component.get("amount", "0")) for component in matched),
        Decimal("0"),
    )
    unmatched_total = sum(
        (_decimal(component.get("amount", "0")) for component in unmatched),
        Decimal("0"),
    )
    return {
        "status": "COMPLETE" if paid_components and not unmatched else "PARTIAL",
        "paid_components": len(paid_components),
        "matched_components": len(matched),
        "unmatched_components": len(unmatched),
        "matched_total": str(matched_total),
        "unmatched_total": str(unmatched_total),
        "ccma_debtor_total": str(_decimal(ccma.get("debtor_total", "0"))),
    }


def reconcile_seti_payments(veps: list[dict], ccma: dict) -> dict[str, object]:
    """Cruza cada VEP-obligación pagado con un único movimiento de CCMA."""

    available: dict[tuple[str, Decimal], list[dict]] = defaultdict(list)
    for payment in ccma.get("payments", []):
        key = (
            str(payment.get("paid_on", "")),
            _decimal(payment.get("amount", "0")),
        )
        available[key].append(payment)

    paid_veps = [
        vep
        for vep in veps
        if str(vep.get("status", "")).upper() == "PAID"
        and not bool(vep.get("is_consolidated"))
        and str(vep.get("tax_type", "ARCA")).upper() != "SANTA_FE_IIBB"
    ]
    excluded_iibb = [
        vep
        for vep in veps
        if str(vep.get("status", "")).upper() == "PAID"
        and str(vep.get("tax_type", "")).upper() == "SANTA_FE_IIBB"
    ]
    matched: list[dict] = []
    unmatched: list[dict] = []
    for vep in paid_veps:
        key = (str(vep.get("paid_on", "")), _decimal(vep.get("amount", "0")))
        candidates = available.get(key, [])
        if candidates:
            vep["ccma_match"] = True
            candidates.pop()
            matched.append(vep)
        else:
            vep["ccma_match"] = False
            unmatched.append(vep)

    matched_total = sum(
        (_decimal(vep.get("amount", "0")) for vep in matched),
        Decimal("0"),
    )
    unmatched_total = sum(
        (_decimal(vep.get("amount", "0")) for vep in unmatched),
        Decimal("0"),
    )
    return {
        "status": "COMPLETE" if paid_veps and not unmatched else "PARTIAL",
        "paid_veps": len(paid_veps),
        "matched_veps": len(matched),
        "unmatched_veps": len(unmatched),
        "matched_total": str(matched_total),
        "unmatched_total": str(unmatched_total),
        "ccma_debtor_total": str(_decimal(ccma.get("debtor_total", "0"))),
        "excluded_iibb_veps": len(excluded_iibb),
    }


class SetiPaymentsWorker:
    def __init__(
        self,
        db: Database,
        connector: SetiConnector,
        *,
        snapshot_ttl: timedelta,
    ):
        self.db = db
        self.connector = connector
        self.snapshot_ttl = snapshot_ttl

    def sync_chat(self, chat_id: int) -> None:
        identity = self.db.get_tax_identity(chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            raise SetiError("El chat no tiene un CUIT verificado")
        ccma_row = self.db.get_live_snapshot(chat_id, "ARCA_CCMA", "account_status")
        if ccma_row is None:
            raise SetiError("Falta una consulta de CCMA para comparar los pagos")
        try:
            ccma = json.loads(ccma_row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SetiError("La última consulta de CCMA no es válida") from exc

        try:
            payload = self.connector.sync(chat_id, identity["represented_cuit"])
            veps = list(payload.get("veps", []))
            iibb_periods = self.db.get_iibb_vep_periods(chat_id)
            for vep in veps:
                period = iibb_periods.get(str(vep.get("number", "")))
                if period:
                    vep["tax_type"] = "SANTA_FE_IIBB"
                    vep["tax_period"] = period
            payload["ccma_reconciliation"] = reconcile_seti_payments(veps, ccma)
        except ExternalUnavailable as exc:
            self.db.upsert_connection(
                chat_id,
                "ARCA_SETI",
                "EXTERNAL_UNAVAILABLE",
                last_error=str(exc),
            )
            raise
        except ExternalContractError as exc:
            self.db.upsert_connection(
                chat_id,
                "ARCA_SETI",
                "INTEGRATION_CHANGED",
                last_error=str(exc),
            )
            raise
        except Exception as exc:
            self.db.upsert_connection(
                chat_id,
                "ARCA_SETI",
                "INTERNAL_ERROR",
                last_error=str(exc),
            )
            raise

        fetched_at = datetime.now(timezone.utc)
        self.db.upsert_live_snapshot(
            chat_id,
            "ARCA_SETI",
            "payment_status",
            fetched_at.isoformat(),
            (fetched_at + self.snapshot_ttl).isoformat(),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        self.db.upsert_connection(
            chat_id,
            "ARCA_SETI",
            "CONNECTED",
            last_success_at=fetched_at.isoformat(),
        )
