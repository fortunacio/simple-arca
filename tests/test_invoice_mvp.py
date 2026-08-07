from __future__ import annotations

import base64
import re
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from xml.etree import ElementTree as ET

from contador_ai.arca.errors import ArcaTransportError
from contador_ai.arca.models import (
    AccessTicket,
    CaeRequest,
    CaeResult,
    EconomicActivity,
    IssuedInvoice,
    IvaCondition,
    PointOfSale,
    RegisteredTax,
    TaxAddress,
    TaxpayerProfile,
    WsfeActivity,
)
from contador_ai.arca.wsfe import (
    build_cae_request_envelope,
    parse_cae_response,
    parse_iva_conditions,
)
from contador_ai.db import Database
from contador_ai.invoice_bot import InvoiceBotService
from contador_ai.invoice_gateway import InvoiceGateway
from contador_ai.invoice_pdf import build_invoice_pdf, qr_payload, qr_url
from contador_ai.invoice_store import InvoiceStore, monthly_dates


def envelope(content: str) -> bytes:
    return (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<soap:Body>{content}</soap:Body></soap:Envelope>"
    ).encode()


class TicketProvider:
    def __init__(self):
        self.ticket = AccessTicket(
            "wsfe",
            "TOKEN",
            "SIGN",
            datetime(2026, 8, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 6, tzinfo=timezone.utc),
        )

    def request_ticket(self, service):
        return self.ticket


def profile(cuit: str, *, monotributo: bool) -> TaxpayerProfile:
    return TaxpayerProfile(
        cuit=cuit,
        key_status="ACTIVO",
        person_type="JURIDICA" if cuit.startswith("30") else "FISICA",
        display_name="CONCEPT DESIGN S.A." if cuit.startswith("30") else "FORTUNACIO FEDERICO",
        registration_date="2019-07-01T00:00:00-03:00",
        taxes=(
            RegisteredTax(
                20 if monotributo else 30,
                "MONOTRIBUTO" if monotributo else "IVA",
                "ACTIVO",
                "201907",
            ),
        ),
        activities=(EconomicActivity(620100, "SERVICIOS DE PROGRAMACION", 1, "201907"),)
        if monotributo
        else (),
        addresses=(
            TaxAddress(
                "FISCAL",
                "Chile 2169" if monotributo else "Republica Argentina 160",
                "Casilda",
                "2170",
                "Santa Fe",
            ),
        ),
    )


class Padron:
    def get_persona(self, ticket, represented_cuit, person_cuit):
        return profile(person_cuit, monotributo=person_cuit == represented_cuit)


class Wsfe:
    def __init__(self, timeout=False):
        self.timeout = timeout
        self.requests = []

    def iva_conditions(self, ticket, represented_cuit, invoice_class):
        return (IvaCondition(1, "IVA Responsable Inscripto", "C"),)

    def points_of_sale(self, ticket, represented_cuit):
        return (PointOfSale(2, "CAE", False),)

    def activities(self, ticket, represented_cuit):
        return (WsfeActivity(620100, "SERVICIOS DE PROGRAMACION", 1),)

    def last_authorized(self, ticket, represented_cuit, point_of_sale, invoice_type):
        return 0

    def request_cae(self, ticket, represented_cuit, request):
        self.requests.append(request)
        if self.timeout:
            raise ArcaTransportError("timeout")
        return CaeResult(
            True,
            request.point_of_sale,
            request.invoice_type,
            request.number,
            "86273007521762",
            "20260815",
            (),
            "<approved/>",
        )

    def invoice(self, ticket, represented_cuit, point_of_sale, invoice_type, number):
        return IssuedInvoice(
            invoice_type,
            point_of_sale,
            number,
            "2026-08-05",
            total=3000000,
            currency="PES",
            exchange_rate=1,
            authorization_code="86273007521762",
            authorization_expiry="20260815",
        )


class InvoiceMvpTest(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.sqlite3")
        self.db.initialize()
        self.store = InvoiceStore(self.db)
        self.chat_id = 1001
        self.db.ensure_user(self.chat_id)
        self.db.set_pending_tax_identity(self.chat_id, "20383717419", "production")
        self.db.mark_tax_identity_verified(self.chat_id)
        client = self.store.upsert_client(
            self.chat_id,
            "30709625043",
            "CLIENTE DE PRUEBA S.A.",
            "IVA Responsable Inscripto",
            "Domicilio de prueba",
            iva_condition_id=1,
        )
        self.store.create_template(
            self.chat_id,
            client["id"],
            "Servicio mensual",
            "Servicios informáticos",
            "3000000",
        )
        self.invoice_values = {
            "chat_id": self.chat_id,
            "source": "TEST_FIXTURE",
            "emitter_cuit": "20383717419",
            "emitter_name": "EMISOR DE PRUEBA",
            "emitter_address": "Domicilio emisor",
            "emitter_iibb": "20383717419",
            "emitter_activity_start": "2019-07-01",
            "emitter_iva_condition": "Responsable Monotributo",
            "emitter_activity_id": None,
            "emitter_activity": "",
            "receiver_cuit": "30709625043",
            "receiver_name": "CLIENTE DE PRUEBA S.A.",
            "receiver_iva_condition_id": 1,
            "receiver_iva_condition": "IVA Responsable Inscripto",
            "receiver_address": "Domicilio de prueba",
            "invoice_type": 11,
            "point_of_sale": 1,
            "number": 73,
            "issue_date": "2026-07-08",
            "service_from": "2026-07-01",
            "service_to": "2026-07-31",
            "due_date": "2026-07-15",
            "description": "Servicios informáticos",
            "quantity": "1.00",
            "unit_description": "otras unidades",
            "unit_price": "3000000.00",
            "total": "3000000.00",
            "currency": "PES",
            "exchange_rate": "1",
            "sale_condition": "Transferencia Bancaria",
            "cae": "86273007521761",
            "cae_expiry": "2026-07-18",
            "observations": "",
            "raw_response": "",
        }
        self.store.insert_invoice(self.invoice_values)

    def tearDown(self):
        self.tempdir.cleanup()

    def gateway(self, wsfe=None, point=2):
        return InvoiceGateway(
            self.store,
            TicketProvider(),
            Padron(),
            wsfe or Wsfe(),
            point,
            frozenset({self.chat_id}),
            "20383717419",
            today_provider=lambda: date(2026, 8, 5),
        )

    def draft(self):
        template = self.store.list_templates(self.chat_id)[0]
        draft = self.store.create_draft_from_template(
            self.chat_id, template["id"], date(2026, 8, 5)
        )
        return self.store.update_draft(
            self.chat_id, draft["id"], manual_duplicate_confirmed=1
        )

    def test_imported_invoice_is_idempotent_and_isolated(self):
        self.store.insert_invoice(self.invoice_values)
        self.assertEqual(self.store.invoice_count(self.chat_id), 1)
        self.assertEqual(len(self.store.list_templates(self.chat_id)), 1)
        self.db.ensure_user(2002)
        self.assertEqual(self.store.invoice_count(2002), 0)

    def test_template_can_be_edited(self):
        template = self.store.list_templates(self.chat_id)[0]
        self.store.create_template(
            self.chat_id,
            template["client_id"],
            "Servicio mensual",
            "Servicio editado por el usuario",
            "3500000",
        )

        preserved = self.store.list_templates(self.chat_id)[0]
        self.assertEqual(preserved["description"], "Servicio editado por el usuario")
        self.assertEqual(preserved["unit_price"], "3500000.00")

    def test_monthly_due_rule(self):
        self.assertEqual(monthly_dates(date(2026, 8, 5))[2], date(2026, 8, 15))
        self.assertEqual(monthly_dates(date(2026, 8, 20))[2], date(2026, 8, 27))

    def test_wsfe_request_contains_required_factura_c_fields(self):
        ticket = TicketProvider().ticket
        request = CaeRequest(
            2,
            11,
            1,
            2,
            80,
            "30709625043",
            1,
            "2026-08-05",
            "2026-08-01",
            "2026-08-31",
            "2026-08-15",
            total=3000000,
        )
        root = ET.fromstring(build_cae_request_envelope(ticket, "20383717419", request))
        values = {node.tag.rsplit("}", 1)[-1]: node.text for node in root.iter()}
        self.assertEqual(values["CbteTipo"], "11")
        self.assertEqual(values["Concepto"], "2")
        self.assertEqual(values["ImpNeto"], "3000000.00")
        self.assertEqual(values["ImpIVA"], "0.00")
        self.assertEqual(values["CondicionIVAReceptorId"], "1")
        self.assertEqual(values["FchServDesde"], "20260801")

    def test_parses_cae_and_iva_parameters(self):
        cae = parse_cae_response(
            envelope(
                "<FECAESolicitarResponse><FECAESolicitarResult>"
                "<FeCabResp><PtoVta>2</PtoVta><CbteTipo>11</CbteTipo><Resultado>A</Resultado></FeCabResp>"
                "<FeDetResp><FECAEDetResponse><CbteDesde>1</CbteDesde><Resultado>A</Resultado>"
                "<CAE>86273007521762</CAE><CAEFchVto>20260815</CAEFchVto>"
                "</FECAEDetResponse></FeDetResp></FECAESolicitarResult></FECAESolicitarResponse>"
            )
        )
        self.assertTrue(cae.approved)
        self.assertEqual(cae.number, 1)
        conditions = parse_iva_conditions(
            envelope(
                "<FEParamGetCondicionIvaReceptorResponse><FEParamGetCondicionIvaReceptorResult>"
                "<ResultGet><CondicionIvaReceptor><Id>1</Id><Desc>IVA Responsable Inscripto</Desc>"
                "<Cmp_Clase>C</Cmp_Clase></CondicionIvaReceptor></ResultGet>"
                "</FEParamGetCondicionIvaReceptorResult></FEParamGetCondicionIvaReceptorResponse>"
            )
        )
        self.assertEqual(conditions[0].condition_id, 1)

    def test_issue_is_persisted_once(self):
        draft = self.draft()
        wsfe = Wsfe()
        invoice_id = self.gateway(wsfe).issue(self.chat_id, draft["id"])
        invoice = self.store.get_invoice(self.chat_id, invoice_id)
        self.assertEqual(invoice["point_of_sale"], 2)
        self.assertEqual(invoice["number"], 1)
        self.assertEqual(invoice["cae"], "86273007521762")
        self.assertEqual(self.store.get_draft(self.chat_id, draft["id"])["status"], "ISSUED")
        with self.assertRaisesRegex(ValueError, "procesado"):
            self.gateway(wsfe).issue(self.chat_id, draft["id"])

    def test_timeout_recovers_without_second_cae_request(self):
        draft = self.draft()
        wsfe = Wsfe(timeout=True)
        invoice_id = self.gateway(wsfe).issue(self.chat_id, draft["id"])
        self.assertEqual(len(wsfe.requests), 1)
        self.assertEqual(self.store.get_invoice(self.chat_id, invoice_id)["number"], 1)

    def test_point_one_is_never_used(self):
        draft = self.draft()
        with self.assertRaisesRegex(ValueError, "distinto de 00001"):
            self.gateway(point=1).issue(self.chat_id, draft["id"])

    def test_pdf_is_one_page_and_qr_matches_invoice(self):
        invoice = self.store.list_invoices(self.chat_id)[0]
        pdf = build_invoice_pdf(invoice)
        self.assertEqual(len(re.findall(rb"/Type\s*/Page\b", pdf)), 1)
        url = qr_url(invoice)
        decoded = base64.b64decode(url.split("?p=", 1)[1]).decode("ascii")
        self.assertIn('"tipoCmp":11', decoded)
        self.assertEqual(qr_payload(invoice)["codAut"], 86273007521761)

    def test_bot_creates_editable_draft_but_never_emits_on_repeat(self):
        bot = InvoiceBotService(
            self.store, self.gateway(), today_provider=lambda: date(2026, 8, 5)
        )
        invoice = self.store.list_invoices(self.chat_id)[0]
        reply = bot.handle_callback(f"inv:repeat:{invoice['id']}", self.chat_id)
        self.assertIn("BORRADOR", reply.text)
        with self.db.connect() as connection:
            draft = connection.execute(
                "SELECT * FROM invoice_drafts WHERE chat_id = ?", (self.chat_id,)
            ).fetchone()
        self.assertEqual(draft["status"], "DRAFT")
        self.assertEqual(self.store.invoice_count(self.chat_id), 1)

    def test_invoice_list_can_return_to_the_summary(self):
        bot = InvoiceBotService(
            self.store, self.gateway(), today_provider=lambda: date(2026, 8, 5)
        )

        reply = bot.invoice_list(self.chat_id, 0)

        self.assertIn(
            ("Volver al resumen", "tax:summary"),
            [button for row in reply.keyboard for button in row],
        )

    def test_disconnected_account_cannot_view_saved_invoices(self):
        bot = InvoiceBotService(
            self.store, self.gateway(), today_provider=lambda: date(2026, 8, 5)
        )
        invoice = self.store.list_invoices(self.chat_id)[0]
        with self.db.connect() as connection:
            connection.execute("DELETE FROM tax_identities WHERE chat_id = ?", (self.chat_id,))

        message_reply = bot.handle_message("/facturas", self.chat_id)
        callback_reply = bot.handle_callback(f"inv:view:{invoice['id']}", self.chat_id)

        self.assertIn("cuenta está desconectada", message_reply.text)
        self.assertNotIn("00001-00000073", message_reply.text)
        self.assertIn("cuenta está desconectada", callback_reply.text)
