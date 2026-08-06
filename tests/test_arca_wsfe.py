from datetime import date, datetime, timezone
from unittest import TestCase
from xml.etree import ElementTree as ET

from contador_ai.arca.models import AccessTicket
from contador_ai.arca.errors import WsfePointOfSaleUnavailable
from contador_ai.arca.transport import HttpxSoapTransport
from contador_ai.arca.wsaa import ArcaEnvironment
from contador_ai.arca.wsfe import (
    WsfeClient,
    build_wsfe_envelope,
    parse_invoice,
    parse_last_authorized,
    parse_points_of_sale,
    parse_wsfe_activities,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.responses.pop(0)


def envelope(content: str) -> bytes:
    return f'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>{content}</soap:Body></soap:Envelope>'.encode()


def last_response(number: int) -> bytes:
    return envelope(
        f"<FECompUltimoAutorizadoResponse><FECompUltimoAutorizadoResult>"
        f"<PtoVta>1</PtoVta><CbteTipo>11</CbteTipo><CbteNro>{number}</CbteNro>"
        "</FECompUltimoAutorizadoResult></FECompUltimoAutorizadoResponse>"
    )


def invoice_response(invoice_type: int, number: int, issued_at: str, total: str) -> bytes:
    return envelope(
        "<FECompConsultarResponse><FECompConsultarResult><ResultGet>"
        f"<CbteTipo>{invoice_type}</CbteTipo><PtoVta>1</PtoVta>"
        f"<CbteDesde>{number}</CbteDesde><CbteFch>{issued_at}</CbteFch>"
        f"<ImpTotal>{total}</ImpTotal><MonId>PES</MonId><MonCotiz>1</MonCotiz>"
        "<CodAutorizacion>123</CodAutorizacion>"
        "</ResultGet></FECompConsultarResult></FECompConsultarResponse>"
    )


POINTS_RESPONSE = envelope(
    "<FEParamGetPtosVentaResponse><FEParamGetPtosVentaResult><ResultGet>"
    "<PtoVenta><Nro>1</Nro><EmisionTipo>CAE</EmisionTipo><Bloqueado>N</Bloqueado>"
    "<FchBaja>NULL</FchBaja></PtoVenta>"
    "</ResultGet></FEParamGetPtosVentaResult></FEParamGetPtosVentaResponse>"
)


class WsfeTest(TestCase):
    def setUp(self):
        self.ticket = AccessTicket(
            "wsfe",
            "TOKEN",
            "SIGN",
            datetime(2026, 8, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 6, tzinfo=timezone.utc),
        )

    def test_builds_read_only_request(self):
        payload = build_wsfe_envelope(
            "FECompConsultar",
            self.ticket,
            "20123456786",
            {"CbteTipo": 11, "CbteNro": 10, "PtoVta": 1},
            request_name="FeCompConsReq",
        )
        root = ET.fromstring(payload)
        names = [element.tag.rsplit("}", 1)[-1] for element in root.iter()]
        self.assertIn("FECompConsultar", names)
        self.assertNotIn("FECAESolicitar", names)

    def test_default_transport_limits_legacy_tls_compatibility_to_wsfe(self):
        client = WsfeClient(ArcaEnvironment.PRODUCTION)
        self.assertIsInstance(client.transport, HttpxSoapTransport)
        self.assertTrue(client.transport.legacy_dh_compatibility)
        client.transport.close()

    def test_parsers_normalize_official_responses(self):
        point = parse_points_of_sale(POINTS_RESPONSE)[0]
        self.assertEqual(point.number, 1)
        self.assertEqual(point.disabled_at, "")
        self.assertEqual(parse_last_authorized(last_response(7)), 7)
        invoice = parse_invoice(invoice_response(11, 7, "20260715", "3000000"))
        self.assertEqual(invoice.issued_at, "2026-07-15")
        self.assertEqual(str(invoice.total_pesos), "3000000")

    def test_parses_official_activity_element_name(self):
        response = envelope(
            "<FEParamGetActividadesResponse><FEParamGetActividadesResult><ResultGet>"
            "<ActividadesTipo><Id>639900</Id><Orden>1</Orden>"
            "<Desc>SERVICIOS DE INFORMACION N.C.P.</Desc></ActividadesTipo>"
            "</ResultGet></FEParamGetActividadesResult></FEParamGetActividadesResponse>"
        )
        activity = parse_wsfe_activities(response)[0]
        self.assertEqual(activity.activity_id, 639900)
        self.assertEqual(activity.order, 1)

    def test_reports_when_cuit_has_no_wsfe_point_of_sale(self):
        response = envelope(
            "<FEParamGetPtosVentaResponse><FEParamGetPtosVentaResult><Errors>"
            "<Err><Code>602</Code><Msg>Sin Resultados: - Metodo FEParamGetPtosVenta</Msg></Err>"
            "</Errors></FEParamGetPtosVentaResult></FEParamGetPtosVentaResponse>"
        )
        with self.assertRaises(WsfePointOfSaleUnavailable):
            parse_points_of_sale(response)

    def test_reads_facturas_and_subtracts_credit_notes(self):
        transport = FakeTransport(
            [
                POINTS_RESPONSE,
                last_response(2),
                invoice_response(11, 2, "20260801", "3000000"),
                invoice_response(11, 1, "20260715", "3000000"),
                last_response(0),
                last_response(1),
                invoice_response(13, 1, "20260720", "100000"),
                last_response(0),
            ]
        )
        client = WsfeClient(ArcaEnvironment.HOMOLOGATION, transport)

        invoices = client.invoices_between(
            self.ticket,
            "20123456786",
            date(2025, 8, 1),
            date(2026, 7, 31),
        )

        self.assertEqual(len(invoices), 2)
        self.assertEqual(sum(invoice.net_total_pesos for invoice in invoices), 2900000)
        self.assertTrue(all("FECAESolicitar" not in body.decode() for _, body, _ in transport.calls))
