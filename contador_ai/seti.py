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
from .external import (
    TRANSIENT_HTTP_STATUSES,
    ExternalContractError,
    ExternalRejected,
    ExternalUnavailable,
    failure_detail,
)
from .health import FailureDetail


SANTA_FE_IIBB_DESCRIPTION = re.compile(
    r"^SF-RGS(?P<month>0[1-9]|1[0-2])/(?P<year>\d{2})$",
    re.IGNORECASE,
)


class SetiError(RuntimeError):
    pass


class SetiAccessRequired(SetiError):
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
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        credentials = self.credentials.load(chat_id)
        if credentials is None:
            raise SetiAccessRequired("Falta guardar el acceso de ARCA")
        if credentials["cuit"] != represented_cuit:
            raise SetiAccessRequired("El acceso guardado corresponde a otro CUIT")

        with self.operation_lock, sync_playwright() as playwright:
            browser = self._launch(playwright)
            context = browser.new_context()
            page = context.new_page()
            try:
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
                except PlaywrightTimeoutError as exc:
                    raise ExternalUnavailable(
                        host="auth.afip.gov.ar",
                        stage="seti_authentication",
                    ) from exc
                try:
                    page.wait_for_url(
                        "**/portal/app/**",
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                except PlaywrightTimeoutError as exc:
                    if page.locator('input[name="F1:password"]').count():
                        raise SetiAccessRequired(
                            "ARCA requiere completar la validación del acceso"
                        ) from exc
                    raise ExternalUnavailable(
                        host="portalcf.cloud.afip.gob.ar",
                        stage="seti_portal_navigation",
                    ) from exc
                try:
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
                    seti.wait_for_url(
                        "**/setiweb/**",
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                except PlaywrightTimeoutError as exc:
                    raise ExternalUnavailable(
                        host="seti.afip.gob.ar",
                        stage="seti_navigation",
                    ) from exc

                try:
                    response = seti.request.get(self._query_url(represented_cuit))
                except PlaywrightError as exc:
                    raise ExternalUnavailable(
                        host="seti.afip.gob.ar",
                        stage="seti_payments",
                    ) from exc
                if response.status == 401:
                    raise SetiAccessRequired("SETI no recibió una sesión autenticada")
                if response.status in TRANSIENT_HTTP_STATUSES or response.status >= 500:
                    raise ExternalUnavailable(
                        host="seti.afip.gob.ar",
                        stage="seti_payments",
                        status_code=response.status,
                    )
                if response.status >= 400:
                    raise ExternalRejected(
                        host="seti.afip.gob.ar",
                        stage="seti_payments",
                        status_code=response.status,
                    )
                try:
                    envelope = response.json()
                except (TypeError, ValueError) as exc:
                    raise ExternalContractError(
                        host="seti.afip.gob.ar",
                        stage="seti_payments_contract",
                    ) from exc
                if not isinstance(envelope, dict):
                    raise ExternalContractError(
                        host="seti.afip.gob.ar",
                        stage="seti_payments_contract",
                    )
                if envelope.get("success") is not True:
                    raise ExternalRejected(
                        host="seti.afip.gob.ar",
                        stage="seti_payments",
                    )
                data = envelope.get("data")
                if not isinstance(data, dict) or not isinstance(
                    data.get("vepsConsultados"), list
                ):
                    raise ExternalContractError(
                        host="seti.afip.gob.ar",
                        stage="seti_payments_contract",
                    )
                items = data["vepsConsultados"]
                if any(not isinstance(item, dict) for item in items):
                    raise ExternalContractError(
                        host="seti.afip.gob.ar",
                        stage="seti_payments_contract",
                    )
                observed_cuit = {
                    re.sub(r"\D", "", str(item.get("cuit", "")))
                    for item in items
                }
                if observed_cuit and observed_cuit != {represented_cuit}:
                    raise ExternalContractError(
                        host="seti.afip.gob.ar",
                        stage="seti_identity_contract",
                    )
                try:
                    veps = [self._normalize_payment(item) for item in items]
                except (SetiError, TypeError, ValueError) as exc:
                    raise ExternalContractError(
                        host="seti.afip.gob.ar",
                        stage="seti_payments_contract",
                    ) from exc
                return {
                    "window_months": 12,
                    "veps": veps,
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
            self.db.record_connection_failure(
                chat_id,
                "ARCA_SETI",
                "PENDING",
                FailureDetail("PENDING", stage="ccma_prerequisite").to_json(),
            )
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
        except SetiAccessRequired:
            self.db.record_connection_failure(
                chat_id,
                "ARCA_SETI",
                "LOGIN_REQUIRED",
                FailureDetail("AUTH_REQUIRED", stage="seti_session").to_json(),
            )
            raise
        except ExternalUnavailable as exc:
            self.db.record_connection_failure(
                chat_id,
                "ARCA_SETI",
                "EXTERNAL_UNAVAILABLE",
                failure_detail(exc).to_json(),
            )
            raise
        except ExternalRejected as exc:
            self.db.record_connection_failure(
                chat_id,
                "ARCA_SETI",
                "UPSTREAM_ERROR",
                failure_detail(exc).to_json(),
            )
            raise
        except ExternalContractError as exc:
            self.db.record_connection_failure(
                chat_id,
                "ARCA_SETI",
                "UNEXPECTED_RESPONSE",
                failure_detail(exc).to_json(),
            )
            raise
        except Exception as exc:
            self.db.record_connection_failure(
                chat_id,
                "ARCA_SETI",
                "INTERNAL_ERROR",
                FailureDetail(
                    "INTERNAL_ERROR",
                    stage="seti_sync",
                    code=exc.__class__.__name__,
                ).to_json(),
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
        self.db.record_connection_success(
            chat_id,
            "ARCA_SETI",
            fetched_at.isoformat(),
        )
