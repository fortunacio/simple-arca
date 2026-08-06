from datetime import date, datetime, timezone
from unittest import TestCase
from xml.etree import ElementTree as ET

from contador_ai.arca.dfe import DfeClient, build_query_envelope, parse_communication_page
from contador_ai.arca.models import AccessTicket
from contador_ai.arca.wsaa import ArcaEnvironment


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.responses.pop(0)


def response(page, total_pages, communication_id):
    return f"""<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body><consultarComunicacionesResponse><RespuestaPaginada><pagina>{page}</pagina><totalPaginas>{total_pages}</totalPaginas><itemsPorPagina>1</itemsPorPagina><totalItems>{total_pages}</totalItems><items><ComunicacionSimplificada><idComunicacion>{communication_id}</idComunicacion><cuitDestinatario>20123456786</cuitDestinatario><fechaPublicacion>2026-08-04 10:00:00</fechaPublicacion><fechaVencimiento>2026-08-10</fechaVencimiento><sistemaPublicador>19</sistemaPublicador><sistemaPublicadorDesc>ARCA</sistemaPublicadorDesc><estado>1</estado><estadoDesc>Comunicacion No Leida</estadoDesc><asunto>Revisar situación</asunto><prioridad>1</prioridad><tieneAdjunto>false</tieneAdjunto></ComunicacionSimplificada></items></RespuestaPaginada></consultarComunicacionesResponse></soap:Body></soap:Envelope>""".encode()


class DfeTest(TestCase):
    def setUp(self):
        self.ticket = AccessTicket(
            "veconsumerws",
            "TOKEN",
            "SIGN",
            datetime(2026, 8, 4, tzinfo=timezone.utc),
            datetime(2026, 8, 5, tzinfo=timezone.utc),
        )

    def test_query_is_read_only_and_has_required_filter(self):
        payload = build_query_envelope(
            self.ticket,
            "20123456786",
            date(2026, 8, 1),
            date(2026, 8, 4),
            status=1,
        )
        text = payload.decode()
        self.assertIn("consultarComunicaciones", text)
        self.assertNotIn("consumirComunicacion", text)
        root = ET.fromstring(payload)
        values = {element.tag.rsplit("}", 1)[-1]: element.text for element in root.iter()}
        self.assertEqual(values["estado"], "1")
        self.assertEqual(values["resultadosPorPagina"], "500")

    def test_query_rejects_windows_over_31_days(self):
        with self.assertRaisesRegex(ValueError, "31 días"):
            build_query_envelope(
                self.ticket,
                "20123456786",
                date(2026, 1, 1),
                date(2026, 3, 1),
            )

    def test_parse_and_paginate(self):
        parsed = parse_communication_page(response(1, 2, 100))
        self.assertEqual(parsed.communications[0].subject, "Revisar situación")
        transport = FakeTransport([response(1, 2, 100), response(2, 2, 101)])
        client = DfeClient(ArcaEnvironment.HOMOLOGATION, transport)
        items = client.query_all(
            self.ticket,
            "20123456786",
            date(2026, 8, 1),
            date(2026, 8, 4),
            status=1,
        )
        self.assertEqual([item.communication_id for item in items], [100, 101])
        self.assertEqual(len(transport.calls), 2)

    def test_soap12_fault_keeps_reason(self):
        payload = b"""<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"><soap:Body><soap:Fault><soap:Code><soap:Value>soap:Receiver</soap:Value></soap:Code><soap:Reason><soap:Text>Error 105: CUIT incorrecta</soap:Text></soap:Reason></soap:Fault></soap:Body></soap:Envelope>"""
        with self.assertRaisesRegex(Exception, "Error 105"):
            parse_communication_page(payload)
