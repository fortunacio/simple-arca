from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import httpx

from contador_ai.db import Database
from contador_ai.iibb_payment import (
    IibbPaymentBotService,
    IibbPaymentProcessor,
    IibbPaymentStore,
    PAYMENT_URL,
)
from contador_ai.santa_fe import (
    IibbGenerationUnknown,
    IibbQuoteChanged,
    REGSIM_AJAX_HEADERS,
    SantaFeIibbConnector,
    SantaFeIibbError,
)
from contador_ai.arca_http_auth import ArcaHttpLogin


QUOTE = {
    "period": "2026-07",
    "due_date": "2026-08-20",
    "payment_date": "2026-08-06",
    "principal": "42882.00",
    "interest": "0.00",
    "total": "42882.00",
}


class FakeConnector:
    def __init__(self):
        self.quote_calls = 0
        self.generation_calls = 0
        self.quote_result = dict(QUOTE)
        self.generation_error = None

    def quote_payment(self, *args, **kwargs):
        self.quote_calls += 1
        return dict(self.quote_result)

    def generate_vep(self, *args, **kwargs):
        self.generation_calls += 1
        if self.generation_error:
            raise self.generation_error
        return {
            "vep_number": "1234567890",
            "quote": dict(self.quote_result),
            "pdf": b"%PDF-test",
        }


class FakeResponse:
    def __init__(self, text="", content=None, *, status_code=200, url="https://example.test"):
        self.text = text
        self.content = text.encode() if content is None else content
        self.status_code = status_code
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", self.url)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )


class FakeHttpClient:
    def __init__(self, *, generation_status=200):
        self.posts = []
        self.generation_status = generation_status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url, data, headers=None):
        self.posts.append((url, data, headers or {}))
        if "actualizarPeriodoVep" in url:
            return FakeResponse(
                '<input id="fe_vencimi_periodo_vep" value="20/08/2026">'
                '<input id="importe_periodo_vep" value="42882,00">'
            )
        if "actualizarInteres" in url:
            return FakeResponse("0.00")
        if "liquidar_VEP" in url:
            return FakeResponse("1234567890", status_code=self.generation_status, url=url)
        return FakeResponse("ok")

    def get(self, url):
        if "imprimirVEP" in url:
            return FakeResponse(content=b"%PDF-official")
        return FakeResponse(
            '<input id="nro_cuenta_iibb_vep" value="1311234567">',
            url=url,
        )


class ConnectorUnderTest(SantaFeIibbConnector):
    def __init__(self, client):
        self.client = client
        self.timeout = 30

    def _login(self, chat_id, represented_cuit, system):
        return ArcaHttpLogin([], "https://www.santafe.gob.ar/", "")

    def _client(self, login):
        return self.client


class IibbPaymentTest(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.sqlite3")
        self.db.initialize()
        self.db.ensure_user(1001)
        self.db.set_pending_tax_identity(1001, "20123456786", "production")
        self.db.mark_tax_identity_verified(1001)
        fetched_at = datetime.now(timezone.utc)
        self.db.upsert_live_snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
            fetched_at.isoformat(),
            (fetched_at + timedelta(days=1)).isoformat(),
            json.dumps(
                {
                    "account": "131-123456-7",
                    "next_obligation": {
                        "period": "2026-07",
                        "due_date": "2026-08-20",
                        "amount": "42882.00",
                    },
                    "vepai": {"status": "AVAILABLE", "payments": []},
                }
            ),
        )
        self.store = IibbPaymentStore(self.db)
        self.connector = FakeConnector()
        self.processor = IibbPaymentProcessor(self.store, self.connector)
        self.bot = IibbPaymentBotService(self.store, frozenset({1001}), enabled=True)

    def tearDown(self):
        self.tempdir.cleanup()

    def request(self):
        return self.store.request_quote(
            chat_id=1001,
            represented_cuit="20123456786",
            account="131-123456-7",
            period="2026-07",
            network_id="1002",
            payment_date=date(2026, 8, 6),
        )

    def test_complete_flow_requires_quote_and_explicit_confirmation(self):
        row = self.request()
        self.assertEqual(row["status"], "QUOTE_REQUESTED")
        self.assertTrue(self.processor.process_one())
        quoted = self.store.get(row["id"])
        self.assertEqual(quoted["status"], "QUOTED")
        self.assertEqual(quoted["total"], "42882.00")

        reply = self.bot.render(quoted)
        self.assertIn("Generar VEP", str(reply.keyboard))
        self.assertIn("Transacciones → Pago de Servicios → Nuevo pago", reply.text)
        self.store.request_generation(row["id"], 1001)
        self.assertTrue(self.processor.process_one())

        generated = self.store.get(row["id"])
        self.assertEqual(generated["status"], "GENERATED")
        self.assertEqual(generated["vep_number"], "1234567890")
        reply = self.bot.render(generated)
        self.assertEqual(reply.document, b"%PDF-test")
        self.assertIn(PAYMENT_URL, str(reply.keyboard))
        self.assertIn("puede tardar al menos 3 horas", reply.text)

    def test_each_network_has_specific_non_invented_next_steps(self):
        from contador_ai.iibb_payment import NETWORK_GUIDANCE, NETWORK_HELP_URLS

        self.assertEqual(set(NETWORK_GUIDANCE), {"1001", "1002", "1003"})
        self.assertEqual(set(NETWORK_HELP_URLS), {"1001", "1002", "1003"})
        self.assertIn("depende del banco", NETWORK_GUIDANCE["1001"])
        self.assertIn("Santander", NETWORK_GUIDANCE["1002"])
        self.assertIn("16:00", NETWORK_GUIDANCE["1003"])

    def test_double_confirmation_generates_only_once(self):
        row = self.request()
        self.processor.process_one()
        self.store.request_generation(row["id"], 1001)
        self.store.request_generation(row["id"], 1001)
        self.processor.process_one()
        self.assertFalse(self.processor.process_one())
        self.assertEqual(self.connector.generation_calls, 1)

    def test_timeout_is_unknown_and_never_retried(self):
        row = self.request()
        self.processor.process_one()
        self.connector.generation_error = IibbGenerationUnknown("resultado incierto")
        self.store.request_generation(row["id"], 1001)
        self.processor.process_one()

        self.assertEqual(self.store.get(row["id"])["status"], "UNKNOWN")
        self.assertFalse(self.processor.process_one())
        self.assertEqual(self.connector.generation_calls, 1)

    def test_changed_quote_requires_a_new_confirmation(self):
        row = self.request()
        self.processor.process_one()
        changed = dict(QUOTE, interest="100.00", total="42982.00")
        self.connector.generation_error = IibbQuoteChanged(changed)
        self.store.request_generation(row["id"], 1001)
        self.processor.process_one()

        result = self.store.get(row["id"])
        self.assertEqual(result["status"], "QUOTED")
        self.assertEqual(result["total"], "42982.00")

    def test_unknown_preserves_pdf_without_enabling_a_second_emission(self):
        row = self.request()
        self.processor.process_one()
        self.connector.generation_error = IibbGenerationUnknown('PDF no validado', pdf=b'%PDF-original')
        self.store.request_generation(row['id'], 1001)
        self.processor.process_one()
        result = self.store.get(row['id'])
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['official_pdf'], b'%PDF-original')
        self.store.request_generation(row['id'], 1001)
        self.assertFalse(self.processor.process_one())
        self.assertEqual(self.connector.generation_calls, 1)

    def test_storage_failure_after_emission_never_returns_to_quoted(self):
        row = self.request()
        self.processor.process_one()
        self.store.request_generation(row['id'], 1001)
        with patch.object(self.store, 'generation_succeeded', side_effect=RuntimeError('storage')):
            self.processor.process_one()
        result = self.store.get(row['id'])
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['official_pdf'], b'%PDF-test')
        self.assertEqual(result['vep_number'], '1234567890')
        self.assertFalse(self.processor.process_one())

    def test_same_period_is_idempotent(self):
        first = self.request()
        second = self.request()
        self.assertEqual(first["id"], second["id"])

    def test_preserves_siat_expiration_instead_of_payment_preparation_date(self):
        self.connector.quote_result['vep_expires_on'] = '2026-08-20'
        row = self.request()
        self.processor.process_one()
        self.assertEqual(self.store.get(row['id'])['vep_expires_on'], '2026-08-20')
        reply = self.bot.render(self.store.get(row['id']))
        self.assertIn('Vencimiento del VEP informado por Santa Fe: 20/08/2026', reply.text)
        self.store.request_generation(row['id'], 1001)
        self.processor.process_one()
        self.assertEqual(self.store.get(row['id'])['vep_expires_on'], '2026-08-20')
        self.assertEqual(self.store.expire_generated(date(2026, 8, 7)), 0)

    def test_only_vepai_marks_a_generated_vep_paid(self):
        row = self.request()
        self.processor.process_one()
        self.store.request_generation(row["id"], 1001)
        self.processor.process_one()
        snapshot = self.db.get_live_snapshot(1001, "SANTA_FE_IIBB", "account_status")
        payload = json.loads(snapshot["payload_json"])
        payload["vepai"]["payments"] = [{"period": "2026-07"}]
        self.db.upsert_live_snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
            snapshot["fetched_at"],
            snapshot["expires_at"],
            json.dumps(payload),
        )

        self.assertEqual(self.store.reconcile_paid(1001), 1)
        self.assertEqual(self.store.get(row["id"])["status"], "PAID")

    def test_seti_paid_marks_generated_vep_paid_before_vepai_updates(self):
        row = self.request()
        self.processor.process_one()
        self.store.request_generation(row["id"], 1001)
        self.processor.process_one()
        self.db.upsert_live_snapshot(
            1001,
            "ARCA_SETI",
            "payment_status",
            "2026-08-07T12:00:00+00:00",
            "2026-08-08T12:00:00+00:00",
            json.dumps(
                {
                    "veps": [
                        {
                            "number": "1234567890",
                            "status": "PAID",
                            "tax_type": "SANTA_FE_IIBB",
                            "tax_period": "2026-07",
                        }
                    ]
                }
            ),
        )

        self.assertEqual(self.store.reconcile_paid(1001), 1)
        self.assertEqual(self.store.get(row["id"])["status"], "PAID")

    def test_paid_seti_vep_cannot_expire(self):
        row = self.request()
        self.processor.process_one()
        self.store.request_generation(row["id"], 1001)
        self.processor.process_one()
        self.db.upsert_live_snapshot(
            1001,
            "ARCA_SETI",
            "payment_status",
            "2026-08-07T12:00:00+00:00",
            "2026-08-08T12:00:00+00:00",
            json.dumps(
                {
                    "veps": [
                        {"number": "1234567890", "status": "PAID"}
                    ]
                }
            ),
        )

        self.assertEqual(self.store.expire_generated(date(2026, 8, 7)), 0)
        self.assertEqual(self.store.get(row["id"])["status"], "PAID")

    def test_seti_repairs_an_expired_vep_once_payment_is_visible(self):
        row = self.request()
        self.processor.process_one()
        self.store.request_generation(row["id"], 1001)
        self.processor.process_one()
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE iibb_payment_drafts SET status = 'EXPIRED' WHERE id = ?",
                (row["id"],),
            )
        self.db.upsert_live_snapshot(
            1001,
            "ARCA_SETI",
            "payment_status",
            "2026-08-07T12:00:00+00:00",
            "2026-08-08T12:00:00+00:00",
            json.dumps(
                {"veps": [{"number": "1234567890", "status": "PAID"}]}
            ),
        )

        self.assertEqual(self.store.reconcile_paid(1001), 1)
        self.assertEqual(self.store.get(row["id"])["status"], "PAID")

    def test_unauthorized_chat_cannot_start(self):
        reply = self.bot.start(999)
        self.assertIn("no está habilitado", reply.text)

    def test_stale_account_cannot_start_a_payment(self):
        snapshot = self.db.get_live_snapshot(1001, "SANTA_FE_IIBB", "account_status")
        stale_at = datetime.now(timezone.utc) - timedelta(hours=13)
        self.db.upsert_live_snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
            stale_at.isoformat(),
            (stale_at + timedelta(days=1)).isoformat(),
            snapshot["payload_json"],
        )

        reply = self.bot.start(1001)

        self.assertIn("Primero necesito actualizar", reply.text)

    def test_stale_account_cannot_confirm_an_already_quoted_vep(self):
        row = self.request()
        self.processor.process_one()
        snapshot = self.db.get_live_snapshot(1001, "SANTA_FE_IIBB", "account_status")
        stale_at = datetime.now(timezone.utc) - timedelta(hours=13)
        self.db.upsert_live_snapshot(
            1001,
            "SANTA_FE_IIBB",
            "account_status",
            stale_at.isoformat(),
            (stale_at + timedelta(days=1)).isoformat(),
            snapshot["payload_json"],
        )

        reply = self.bot.handle_callback(f"iibbpay:confirm:{row['id']}", 1001)

        self.assertIn("Primero necesito actualizar", reply.text)
        self.assertEqual(self.store.get(row["id"])["status"], "QUOTED")

    def test_disconnected_chat_cannot_continue_an_old_payment_callback(self):
        with self.db.connect() as connection:
            connection.execute("DELETE FROM tax_identities WHERE chat_id = 1001")

        reply = self.bot.handle_callback("iibbpay:network:1002", 1001)

        self.assertIn("cuenta está desconectada", reply.text)

    def test_worker_rejects_a_job_injected_for_an_unauthorized_chat(self):
        row = self.request()
        processor = IibbPaymentProcessor(
            self.store,
            self.connector,
            allowed_chat_ids=frozenset({999}),
        )

        self.assertTrue(processor.process_one())
        result = self.store.get(row["id"])
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(self.connector.quote_calls, 0)

    def test_connector_sends_the_official_form_fields_and_downloads_pdf(self):
        client = FakeHttpClient()
        connector = ConnectorUnderTest(client)

        result = connector.generate_vep(
            1001,
            "20123456786",
            account="131-123456-7",
            period="2026-07",
            payment_date=date(2026, 8, 6),
            network_id="1002",
            expected_quote=QUOTE,
        )

        generation, headers = next(
            (data, headers)
            for url, data, headers in client.posts
            if "liquidar_VEP" in url
        )
        self.assertEqual(
            generation,
            {
                "nro_iibb": "1311234567",
                "id_entidad": "1002",
                "periodo": "202607",
                "importe": "42882,00",
                "fe_vencimi": "20/08/2026",
                "fe_pago": "06/08/2026",
                "interes": "0.00",
            },
        )
        self.assertEqual(headers, REGSIM_AJAX_HEADERS)
        ajax_headers = [
            headers
            for url, _data, headers in client.posts
            if "header=AJAX" in url
        ]
        self.assertTrue(ajax_headers)
        self.assertTrue(all(headers == REGSIM_AJAX_HEADERS for headers in ajax_headers))
        self.assertEqual(result["vep_number"], "1234567890")
        self.assertEqual(result["pdf"], b"%PDF-official")

    def test_connector_rejects_a_different_account_from_the_official_form(self):
        client = FakeHttpClient()
        connector = ConnectorUnderTest(client)

        with self.assertRaisesRegex(SantaFeIibbError, "cuenta distinta"):
            connector.generate_vep(
                1001,
                "20123456786",
                account="131-999999-9",
                period="2026-07",
                payment_date=date(2026, 8, 6),
                network_id="1002",
                expected_quote=QUOTE,
            )

        self.assertFalse(any("liquidar_VEP" in url for url, _data, _headers in client.posts))

    def test_generation_http_error_is_unknown_and_cannot_be_retried(self):
        client = FakeHttpClient(generation_status=500)
        connector = ConnectorUnderTest(client)

        with self.assertRaisesRegex(IibbGenerationUnknown, "HTTP 500"):
            connector.generate_vep(
                1001,
                "20123456786",
                account="131-123456-7",
                period="2026-07",
                payment_date=date(2026, 8, 6),
                network_id="1002",
                expected_quote=QUOTE,
            )

        generation_calls = [url for url, _data, _headers in client.posts if "liquidar_VEP" in url]
        self.assertEqual(len(generation_calls), 1)
