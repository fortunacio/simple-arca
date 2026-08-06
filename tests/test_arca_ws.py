from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from xml.etree import ElementTree as ET

from cryptography.fernet import Fernet

from contador_ai.arca.errors import ArcaSoapFault
from contador_ai.arca.models import AccessTicket
from contador_ai.arca.padron import (
    ConstanciaInscripcionClient,
    build_get_persona_envelope,
    parse_dummy_response,
    parse_taxpayer_profile,
)
from contador_ai.arca.wsaa import (
    ArcaEnvironment,
    CachedTicketProvider,
    EncryptedTicketStore,
    WsaaClient,
    build_login_ticket_request,
    parse_access_ticket,
)
from contador_ai.db import Database


class FakeSigner:
    def __init__(self):
        self.payload = b""

    def sign(self, payload: bytes) -> str:
        self.payload = payload
        return "CMS-BASE64"


class FakeTransport:
    def __init__(self, response: bytes):
        self.response = response
        self.calls = []

    def post(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.response


WSAA_RESPONSE = b"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <loginCmsResponse>
      <loginCmsReturn>&lt;loginTicketResponse version="1.0"&gt;
        &lt;header&gt;
          &lt;source&gt;CN=wsaahomo&lt;/source&gt;
          &lt;destination&gt;SERIALNUMBER=CUIT 20123456789&lt;/destination&gt;
          &lt;generationTime&gt;2026-08-04T20:00:00-03:00&lt;/generationTime&gt;
          &lt;expirationTime&gt;2026-08-05T08:00:00-03:00&lt;/expirationTime&gt;
        &lt;/header&gt;
        &lt;credentials&gt;&lt;token&gt;TOKEN&lt;/token&gt;&lt;sign&gt;SIGN&lt;/sign&gt;&lt;/credentials&gt;
      &lt;/loginTicketResponse&gt;</loginCmsReturn>
    </loginCmsResponse>
  </soap:Body>
</soap:Envelope>"""


CONSTANCIA_RESPONSE = b"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <getPersona_v2Response>
      <personaReturn>
        <metadata><fechaHora>2026-08-04T21:10:00-03:00</fechaHora></metadata>
        <datosGenerales>
          <idPersona>20123456789</idPersona>
          <tipoPersona>FISICA</tipoPersona>
          <estadoClave>ACTIVO</estadoClave>
          <nombre>FEDERICO</nombre><apellido>PRUEBA</apellido>
          <fechaInscripcion>2020-01-01T12:00:00-03:00</fechaInscripcion>
        </datosGenerales>
        <datosMonotributo>
          <impuesto><idImpuesto>20</idImpuesto><descripcionImpuesto>MONOTRIBUTO</descripcionImpuesto><estado>ACTIVO</estado><periodo>202001</periodo></impuesto>
          <categoriaMonotributo><idCategoria>106</idCategoria><idImpuesto>20</idImpuesto><descripcionCategoria>F</descripcionCategoria><periodo>202608</periodo></categoriaMonotributo>
          <actividadMonotributista><idActividad>620100</idActividad><descripcionActividad>SERVICIOS DE PROGRAMACION</descripcionActividad><orden>1</orden><periodo>202001</periodo></actividadMonotributista>
        </datosMonotributo>
      </personaReturn>
    </getPersona_v2Response>
  </soap:Body>
</soap:Envelope>"""

DUMMY_RESPONSE = b"""<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><dummyResponse><return><appserver>OK</appserver><authserver>OK</authserver><dbserver>OK</dbserver></return></dummyResponse></soap:Body></soap:Envelope>"""


class WsaaTest(TestCase):
    def test_login_ticket_request_uses_clock_skew_and_service(self):
        now = datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc)
        root = ET.fromstring(
            build_login_ticket_request("ws_sr_constancia_inscripcion", now=now, unique_id=123)
        )
        self.assertEqual(root.findtext("header/uniqueId"), "123")
        self.assertEqual(root.findtext("header/generationTime"), "2026-08-04T20:50:00+00:00")
        self.assertEqual(root.findtext("header/expirationTime"), "2026-08-04T21:10:00+00:00")
        self.assertEqual(root.findtext("service"), "ws_sr_constancia_inscripcion")

    def test_parse_access_ticket(self):
        ticket = parse_access_ticket(WSAA_RESPONSE, "ws_sr_constancia_inscripcion")
        self.assertEqual(ticket.token, "TOKEN")
        self.assertEqual(ticket.sign, "SIGN")
        self.assertEqual(ticket.destination, "SERIALNUMBER=CUIT 20123456789")
        self.assertEqual(ticket.expiration_time.utcoffset().total_seconds(), -10800)

    def test_wsaa_client_signs_and_posts_soap(self):
        signer = FakeSigner()
        transport = FakeTransport(WSAA_RESPONSE)
        client = WsaaClient(ArcaEnvironment.HOMOLOGATION, signer, transport)
        ticket = client.request_ticket("ws_sr_constancia_inscripcion")
        self.assertEqual(ticket.service, "ws_sr_constancia_inscripcion")
        self.assertIn(b"loginTicketRequest", signer.payload)
        url, body, headers = transport.calls[0]
        self.assertEqual(url, ArcaEnvironment.HOMOLOGATION.wsaa_url)
        self.assertIn(b"CMS-BASE64", body)
        self.assertEqual(headers["SOAPAction"], "urn:LoginCms")

    def test_soap_fault_is_not_hidden(self):
        payload = b"""<Envelope><Body><Fault><faultcode>ns1:coe.notAuthorized</faultcode><faultstring>No autorizado</faultstring></Fault></Body></Envelope>"""
        with self.assertRaisesRegex(ArcaSoapFault, "No autorizado"):
            parse_access_ticket(payload, "ws_sr_constancia_inscripcion")

    def test_ticket_provider_reuses_valid_ticket(self):
        ticket = AccessTicket(
            service="ws_sr_constancia_inscripcion",
            token="TOKEN",
            sign="SIGN",
            generation_time=datetime.now(timezone.utc),
            expiration_time=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        class Client:
            def __init__(self):
                self.calls = 0

            def request_ticket(self, service):
                self.calls += 1
                return ticket

        client = Client()
        provider = CachedTicketProvider(client)
        first = provider.request_ticket("ws_sr_constancia_inscripcion")
        second = provider.request_ticket("ws_sr_constancia_inscripcion")
        self.assertIs(first, second)
        self.assertEqual(client.calls, 1)

    def test_ticket_provider_reuses_encrypted_ticket_after_restart(self):
        ticket = AccessTicket(
            service="ws_sr_constancia_inscripcion",
            token="TOKEN-SECRETO",
            sign="FIRMA-SECRETA",
            generation_time=datetime.now(timezone.utc),
            expiration_time=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        class Client:
            def __init__(self):
                self.calls = 0

            def request_ticket(self, service):
                self.calls += 1
                return ticket

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.sqlite3")
            database.initialize()
            key = Fernet.generate_key().decode("ascii")
            first_client = Client()
            first_store = EncryptedTicketStore(database, key, "homo:operador:cert")
            first = CachedTicketProvider(first_client, first_store).request_ticket(
                ticket.service
            )

            row = database.get_access_ticket("homo:operador:cert", ticket.service)
            self.assertNotIn(b"TOKEN-SECRETO", row["encrypted_payload"])

            second_client = Client()
            second_store = EncryptedTicketStore(database, key, "homo:operador:cert")
            second = CachedTicketProvider(second_client, second_store).request_ticket(
                ticket.service
            )
            self.assertEqual(first, second)
            self.assertEqual(first_client.calls, 1)
            self.assertEqual(second_client.calls, 0)


class ConstanciaInscripcionTest(TestCase):
    def test_dummy_reports_all_official_components(self):
        self.assertEqual(
            parse_dummy_response(DUMMY_RESPONSE),
            {"appserver": "OK", "authserver": "OK", "dbserver": "OK"},
        )
        transport = FakeTransport(DUMMY_RESPONSE)
        result = ConstanciaInscripcionClient(
            ArcaEnvironment.HOMOLOGATION, transport
        ).dummy()
        self.assertEqual(result["dbserver"], "OK")
        self.assertEqual(
            transport.calls[0][0],
            ArcaEnvironment.HOMOLOGATION.constancia_inscripcion_url,
        )

    def test_build_request_keeps_represented_and_target_cuit_separate(self):
        ticket = AccessTicket(
            service="ws_sr_constancia_inscripcion",
            token="TOKEN&SAFE",
            sign="SIGN",
            generation_time=datetime(2026, 8, 4, tzinfo=timezone.utc),
            expiration_time=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
        payload = build_get_persona_envelope(ticket, "20111111111", "20222222222")
        root = ET.fromstring(payload)
        values = {element.tag.rsplit("}", 1)[-1]: element.text for element in root.iter()}
        self.assertIn("getPersona_v2", values)
        self.assertEqual(values["cuitRepresentada"], "20111111111")
        self.assertEqual(values["idPersona"], "20222222222")
        self.assertEqual(values["token"], "TOKEN&SAFE")

    def test_parse_profile_normalizes_official_response(self):
        profile = parse_taxpayer_profile(CONSTANCIA_RESPONSE)
        self.assertEqual(profile.cuit, "20123456789")
        self.assertEqual(profile.display_name, "FEDERICO PRUEBA")
        self.assertEqual(profile.taxes[0].description, "MONOTRIBUTO")
        self.assertEqual(profile.categories[0].description, "F")
        self.assertEqual(profile.categories[0].period, "202608")
        self.assertEqual(profile.activities[0].activity_id, 620100)
        self.assertEqual(profile.observed_at, "2026-08-04T21:10:00-03:00")
