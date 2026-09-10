from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from pypdf import PdfWriter
from reportlab.pdfgen.canvas import Canvas

from contador_ai.arca_http_auth import ArcaHttpLogin
from contador_ai.external import ExternalContractError, ExternalUnavailable
from contador_ai.db import Database
from contador_ai.iibb_payment import IibbPaymentStore, IibbPaymentProcessor, IibbPaymentBotService
from contador_ai.santa_fe import ARGENTINA_TIMEZONE, IibbGenerationUnknown, IibbQuoteChanged, SantaFeIibbError
from contador_ai.siat import Page, SiatIibbConnector, official_url, parse_account, parse_vep_text, parse_vep_pdf


CUIT = "20123456786"
ACCOUNT = "1311234567"
CURRENT = datetime.now(ARGENTINA_TIMEZONE).date()
PERIOD = CURRENT.strftime("%Y-%m")
DUE = CURRENT + timedelta(days=8)
PREVIOUS = (CURRENT.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
MENU = f"""<form id="f1"><input name="mrk" value="session-one"></form>
<div>CUIT {CUIT} Número de Cuenta {ACCOUNT} RÉGIMEN / CATEGORÍA Regimen Simplificado / 3</div>
<a href="/siat/app/afip/gde/seleccionarcuenta/session-one">Estado de Cuenta</a>"""
ENTRY = f"""<form id="f1" method="post"><input name="r" value="formularios">
<select name="idCuitSeleccionado" onchange="wl.get('f1', '/siat/app/afip/pwlocalcuit/session-one/refresh');">
<option value="">Seleccionar</option><option value="7">{CUIT}</option></select></form>"""


def debt_row(period, *, paid=False, total="42.882,00"):
    year, month = period.split("-")
    return f"""<tr><td><input type="checkbox" name="listIdDeudaSelected" value="{'2' if paid else '1'}-1"></td><td></td>
    <td>{month}/{year} RS</td><td>{DUE.strftime('%d/%m/%Y')}</td><td>$ 42.882,00</td><td></td>
    <td>{CURRENT.strftime('%d/%m/%Y') if paid else ''}</td><td>$ 0,00</td><td>$ {'42.882,00' if paid else '0,00'}</td>
    <td>$ {'0,00' if paid else '42.882,00'}</td><td>$ 0,00</td><td>$ {'0,00' if paid else total}</td></tr>"""


def account_html():
    return f"""<h1>Estado de Cuenta</h1><div>Número: {ACCOUNT} Número de CUIT {CUIT}
    Fecha de Inicio de Actividad 01/01/2024 Regimen simplificado fecha adhesión 31/01/2024
    Regimen simplificado ingreso bruto anual gravado 21000000 Regimen simplificado importe mensual 28300</div>
    <h2>Gestión Administrativa</h2><form id="filter" action="/siat/gde/AdministrarLiqDeuda.do">
    <input name="cuentaId" value="42"><input name="method" value=""><input name="selectedId" value="">
    <button name="btnReimp" onclick="submitForm('reconfeccionar', '42');">Reimpresión / Pago</button>
    <table><tr><th></th><th></th><th>Período Deuda</th><th>Fecha de Vencimiento</th><th>Impuesto</th>
    <th></th><th>Fecha Pago</th><th>Otros Pagos</th><th>Importe Pago</th><th>Saldo</th><th>Actualización</th><th>Total</th></tr>
    {debt_row(PERIOD)}{debt_row(PREVIOUS, paid=True)}</table></form>"""


REPRINT = """<form id="filter" action="/siat/gde/AdministrarLiqReconfeccion.do">
<input name="isSubmittedForm" value="true"><input name="method" value="">
<a onclick="submitForm('generarVep','');">Generar VEP</a></form>"""


def expected_quote():
    return {'period': PERIOD, 'payment_date': CURRENT.isoformat(), 'due_date': DUE.isoformat(),
            'principal': '42882.00', 'interest': '0.00', 'total': '42882.00',
            'vep_expires_on': DUE.isoformat()}


def receipt_pdf(*, cuit=CUIT, total='42.882,00', pages=1):
    # Synthetic fiscal values only; never copy the user's receipt into fixtures.
    buffer = BytesIO()
    canvas = Canvas(buffer, pageCompression=1)
    for _ in range(pages):
        lines = ['Nº de VEP: 1234567890', 'Tipo de Pago: Ingresos Brutos', 'Formulario: 5439',
                 f'CUIT: {cuit}', 'Concepto: 19', 'Subconcepto: 19', f'Período: {PERIOD}',
                 f'Generado por el usuario: {cuit}', f'Día de expiración: {DUE.strftime("%d/%m/%Y")}',
                 f'Importe total a pagar: {total}']
        for i, line in enumerate(lines):
            canvas.drawString(50, 780-i*20, line)
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def quote_html(**overrides):
    fields = {"contribuyenteCUIT": CUIT, "usuarioCUIT": CUIT, "numeroCuenta": ACCOUNT,
              "periodoFiscal": PERIOD.replace("-", ""), "nroFormulario": "5426",
              "importe_imp": "42882.00", "importe_int": "0.00", "importeTotal": "42882.00",
              "fechaExpiracion": DUE.isoformat(), "hashVep": "synthetic-only"}
    fields.update(overrides)
    return '<form id="f1" action="/siat/app/emp/vepddjjiibbform">' + ''.join(
        f'<input type="hidden" name="{k}" value="{v}">' for k, v in fields.items()
    ) + '<input disabled name="datos.importeLiquidado" value="42882.00">' + ''.join(
        f'<input type="radio" name="datos.destino" value="{v}">' for v in ['1001','1002','1003']
    ) + "<a onclick=\"wl.confirmPopUpX('confirmmessageDDJJVEP','post','f1', '//www.santafe.gob.ar/e-vep-siat/index.php?header=AJAX&amp;action=gestionVEP::liquidar_VEP')\">Generar VEP</a></form>"


class SiatTest(TestCase):
    def test_pdf_reader_reads_compressed_receipt_without_rewriting(self):
        document = receipt_pdf()
        self.assertEqual(parse_vep_pdf(document, CUIT, expected_quote()),
                         {'vep_number': '1234567890', 'form': '5439'})

    def test_pdf_reader_rejects_invalid_encrypted_or_multiple_receipts(self):
        writer = PdfWriter()
        writer.add_blank_page(width=500, height=700)
        writer.encrypt('synthetic-password')
        buffer = BytesIO()
        writer.write(buffer)
        for document in [b'<html>Error</html>', b'%PDF-broken\n%%EOF', buffer.getvalue(),
                         receipt_pdf(pages=2), receipt_pdf(cuit='20999999999'),
                         receipt_pdf(total='1,00'), receipt_pdf()[:-20]]:
            with self.subTest(size=len(document)), self.assertRaises(ExternalContractError):
                parse_vep_pdf(document, CUIT, expected_quote())

    def test_emission_posts_live_fields_once_and_receives_pdf_directly(self):
        for network in ('1001', '1002', '1003'):
            calls = []
            def handler(request):
                calls.append(request)
                values = parse_qs(request.content.decode())
                self.assertEqual(values['datos.destino'], [network])
                self.assertEqual(values['hashVep'], ['synthetic-only'])
                self.assertNotIn('datos.importeLiquidado', values)
                self.assertEqual(request.url.path, '/e-vep-siat/index.php')
                return httpx.Response(200, content=b'%PDF-test\n%%EOF',
                                      headers={'Content-Type': 'application/pdf'})
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                result = SiatIibbConnector._emit_pdf(client, Page(quote_html()), network)
            self.assertEqual(result, b'%PDF-test\n%%EOF')
            self.assertEqual(len(calls), 1)

    def test_emission_failures_never_follow_redirects_or_retry(self):
        for status in (302, 307, 400, 500, 200, 'timeout'):
            calls = []
            def handler(request):
                calls.append(request)
                if status == 'timeout':
                    raise httpx.ReadTimeout('synthetic timeout', request=request)
                return httpx.Response(status, text='unexpected page',
                                      headers={'Location':'https://www.santafe.gob.ar/e-vep-siat/retry'})
            with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
                with self.subTest(status=status), self.assertRaises(IibbGenerationUnknown):
                    SiatIibbConnector._emit_pdf(client, Page(quote_html()), '1002')
            self.assertEqual(len(calls), 1)

    def test_unexpected_emission_target_is_rejected_before_post(self):
        html = quote_html().replace('//www.santafe.gob.ar/e-vep-siat/', '//evil.test/e-vep-siat/')
        with self.assertRaises(ExternalContractError):
            SiatIibbConnector._emit_pdf(None, Page(html), '1002')

    def test_official_pdf_fields_are_verified_independently(self):
        text = (f'Nº de VEP: 1234567890 Tipo de Pago: Ingresos Brutos Formulario: 5439 '
                f'CUIT: {CUIT} Concepto: 19 Subconcepto: 19 Período: {PERIOD} '
                f'Generado por el usuario: {CUIT} Día de expiración: {DUE.strftime("%d/%m/%Y")} '
                'Importe total a pagar: 42.882,00')
        quote = {'period': PERIOD, 'vep_expires_on': DUE.isoformat(), 'total': '42882.00'}
        self.assertEqual(parse_vep_text(text, CUIT, quote)['vep_number'], '1234567890')
        for wrong in [text.replace(CUIT, '20999999999'), text.replace(PERIOD, '2000-01'),
                      text.replace('42.882,00', '1,00'), text.replace('5439', '9999'),
                      text.replace('Concepto: 19', 'Concepto: 20'),
                      text.replace(DUE.strftime('%d/%m/%Y'), '01/01/2000')]:
            with self.subTest(text=wrong), self.assertRaises(ExternalContractError):
                parse_vep_text(wrong, CUIT, quote)

    def test_account_uses_period_tax_not_obsolete_registration_amount(self):
        result = parse_account(Page(MENU), Page(account_html()), CUIT, CURRENT)
        self.assertEqual(result['monthly_amount'], '42882.00')
        self.assertEqual(result['category'], '3')
        self.assertEqual(result['effective_period'], '2024-01')
        self.assertEqual(len(result['payments']), 1)
        self.assertEqual(result['payments'][0]['period'], PREVIOUS)

    def test_rejects_identity_and_regime_mismatch(self):
        for menu, account in [(MENU.replace(CUIT, '20999999999'), account_html()),
                              (MENU, account_html().replace(ACCOUNT, '9999999999')),
                              (MENU.replace('Regimen Simplificado / 3','Régimen General'), account_html())]:
            with self.subTest(menu=menu[:30]), self.assertRaises(ExternalContractError):
                parse_account(Page(menu), Page(account), CUIT, CURRENT)

    def test_rejects_changed_table_or_mismatched_total(self):
        for html in [account_html().replace('Actualización','Unknown'),
                     account_html().replace(debt_row(PERIOD), debt_row(PERIOD,total='40.000,00'))]:
            with self.assertRaises(ExternalContractError):
                parse_account(Page(MENU), Page(html), CUIT, CURRENT)

    def test_only_successful_fields_are_submitted(self):
        page = Page('<form id="f1"><input name="token" value="state"><input disabled name="amount" value="9">'
                    '<input type="checkbox" name="debt" value="1"><input type="radio" checked name="network" value="1002">'
                    '<button name="emit">Emit</button></form>')
        self.assertEqual(page.fields('f1'), {'token':'state','network':'1002'})

    def test_rejects_external_or_non_siat_destinations(self):
        for url in ['http://www.santafe.gob.ar/siat/','https://evil.test/siat/',
                    '//www.santafe.gob.ar.evil.test/siat/','/unrelated/']:
            with self.assertRaises(ExternalContractError):
                official_url('https://www.santafe.gob.ar/siat/',url)

    def connector(self, quote=None, pdf=None):
        calls=[]
        def handler(request):
            calls.append(request)
            path = request.url.path
            if path == '/e-vep-siat/index.php':
                return httpx.Response(200, content=pdf if pdf is not None else receipt_pdf(),
                                      headers={'Content-Type': 'application/pdf'})
            if '/pwlocalcuit/' in path:
                self.assertEqual(request.url.params['idCuitSeleccionado'],'7')
                return httpx.Response(200,text=MENU)
            if '/seleccionarcuenta/' in path:
                return httpx.Response(200,text=f"<script>irAEstadoCuenta('{ACCOUNT}', '15');</script>")
            if path.endswith('AdministrarLiqDeuda.do'):
                if request.method=='GET': return httpx.Response(200,text=account_html())
                values=parse_qs(request.content.decode())
                self.assertEqual(values['method'],['reconfeccionar'])
                self.assertEqual(values['listIdDeudaSelected'],['1-1'])
                return httpx.Response(200,text=REPRINT)
            if path.endswith('AdministrarLiqReconfeccion.do'):
                self.assertEqual(parse_qs(request.content.decode())['method'],['generarVep'])
                return httpx.Response(200,text=quote or quote_html())
            self.fail(f'Unexpected request: {path}')
        connector=SiatIibbConnector(None)
        connector._login=lambda *args: ArcaHttpLogin([], 'https://www.santafe.gob.ar/siat/app/afip/pwlocal', ENTRY)
        connector._client=lambda *args: httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True)
        return connector,calls

    def test_http_quote_follows_live_forms_without_emitting(self):
        connector,calls=self.connector()
        result=connector.quote_payment(1,CUIT,period=PERIOD,payment_date=CURRENT)
        self.assertEqual(result['total'],'42882.00')
        self.assertEqual(result['vep_expires_on'],DUE.isoformat())
        self.assertFalse(any('/e-vep-siat/' in r.url.path for r in calls))

    def test_paid_period_cannot_be_quoted(self):
        connector,_=self.connector()
        with self.assertRaises(SantaFeIibbError):
            connector.quote_payment(1,CUIT,period=PREVIOUS,payment_date=CURRENT)

    def test_quote_identity_and_amount_are_cross_checked(self):
        for fields in [{'contribuyenteCUIT':'20999999999'}, {'periodoFiscal':'200101'},
                       {'importeTotal':'1.00'}, {'nroFormulario':'9999'},
                       {'importe_imp':'1.00','importeTotal':'1.00'}]:
            with self.subTest(fields=fields), self.assertRaises(ExternalContractError):
                connector,_=self.connector(quote_html(**fields))
                connector.quote_payment(1,CUIT,period=PERIOD,payment_date=CURRENT)

    def test_sync_keeps_siat_account_when_vepai_fails(self):
        connector, _ = self.connector()
        with patch.object(connector, 'fetch_vepai', side_effect=ExternalUnavailable(
            host='www.santafe.gob.ar', stage='vepai_account'
        )):
            result = connector.sync(1, CUIT, today=CURRENT)
        self.assertEqual(result['vepai']['status'], 'UNAVAILABLE')
        self.assertEqual(result['next_obligation']['period'], PERIOD)
        self.assertEqual(result['monthly_amount'], '42882.00')
        self.assertEqual(len(result['payments']), 1)

    def test_zero_siat_balance_is_not_debt_when_vepai_has_missing_history(self):
        connector, _ = self.connector()
        with patch.object(connector, 'fetch_vepai', return_value={'status':'AVAILABLE', 'payments':[]}):
            result = connector.sync(1, CUIT, today=CURRENT)
        self.assertEqual(result['next_obligation']['period'], PERIOD)
        self.assertNotEqual(result['next_obligation']['period'], PREVIOUS)

    def test_vepai_payment_suppresses_siat_balance_pending_update(self):
        connector, _ = self.connector()
        with patch.object(connector, 'fetch_vepai', return_value={
            'status':'AVAILABLE', 'payments':[{'period':PERIOD}]
        }):
            result = connector.sync(1, CUIT, today=CURRENT)
        self.assertNotIn('next_obligation', result)

    def test_full_generation_recotizes_and_returns_original_pdf(self):
        document = receipt_pdf()
        connector, calls = self.connector(pdf=document)
        result = connector.generate_vep(1, CUIT, account=ACCOUNT, period=PERIOD,
                                       payment_date=CURRENT, network_id='1002', expected_quote=expected_quote())
        self.assertEqual(result['vep_number'], '1234567890')
        self.assertEqual(result['pdf'], document)
        self.assertEqual(result['quote'], expected_quote())
        self.assertEqual(sum('/e-vep-siat/' in r.url.path for r in calls), 1)
        self.assertFalse(any('/e-regsim/' in r.url.path for r in calls))

    def test_siat_processor_stores_and_reuses_the_same_receipt(self):
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'test.sqlite3')
            db.initialize()
            db.ensure_user(1001)
            store = IibbPaymentStore(db)
            document = receipt_pdf()
            connector, calls = self.connector(pdf=document)
            processor = IibbPaymentProcessor(store, connector, frozenset({1001}))
            draft = store.request_quote(chat_id=1001, represented_cuit=CUIT, account=ACCOUNT,
                                        period=PERIOD, network_id='1002', payment_date=CURRENT)
            self.assertTrue(processor.process_one())
            self.assertEqual(store.get(draft['id'])['status'], 'QUOTED')
            self.assertFalse(any('/e-vep-siat/' in r.url.path for r in calls))
            store.request_generation(draft['id'], 1001)
            self.assertTrue(processor.process_one())
            generated = store.get(draft['id'])
            self.assertEqual(generated['status'], 'GENERATED')
            self.assertEqual(generated['vep_expires_on'], DUE.isoformat())
            self.assertEqual(generated['official_pdf'], document)
            bot = IibbPaymentBotService(store, frozenset({1001}), enabled=True)
            self.assertEqual(bot.render(generated).document, document)
            store.request_generation(draft['id'], 1001)
            self.assertFalse(processor.process_one())
            self.assertEqual(sum('/e-vep-siat/' in r.url.path for r in calls), 1)

    def test_changed_quote_never_emits_and_requires_reconfirmation(self):
        for key in ('due_date', 'principal', 'interest', 'total', 'vep_expires_on'):
            expected = expected_quote()
            expected[key] = 'different'
            connector, calls = self.connector()
            with self.subTest(key=key), self.assertRaises(IibbQuoteChanged) as error:
                connector.generate_vep(1, CUIT, account=ACCOUNT, period=PERIOD,
                                       payment_date=CURRENT, network_id='1002', expected_quote=expected)
            self.assertEqual(error.exception.quote, expected_quote())
            self.assertFalse(any('/e-vep-siat/' in r.url.path for r in calls))

    def test_pdf_validation_after_emission_is_unknown_with_document_preserved(self):
        for document in (b'%PDF-broken\n%%EOF', receipt_pdf(cuit='20999999999')):
            connector, calls = self.connector(pdf=document)
            with self.assertRaises(IibbGenerationUnknown) as error:
                connector.generate_vep(1, CUIT, account=ACCOUNT, period=PERIOD,
                                       payment_date=CURRENT, network_id='1002', expected_quote=expected_quote())
            self.assertEqual(error.exception.pdf, document)
            self.assertEqual(sum('/e-vep-siat/' in r.url.path for r in calls), 1)

    def test_invalid_network_never_starts_authentication(self):
        connector, calls = self.connector()
        with self.assertRaises(SantaFeIibbError):
            connector.generate_vep(1, CUIT, account=ACCOUNT, period=PERIOD,
                                   payment_date=CURRENT, network_id='9999', expected_quote=expected_quote())
        self.assertEqual(calls, [])

    def test_expired_quote_and_mismatched_account_are_rejected(self):
        connector, _ = self.connector(quote_html(fechaExpiracion=(CURRENT-timedelta(days=1)).isoformat()))
        with self.assertRaises(SantaFeIibbError):
            connector.quote_payment(1, CUIT, period=PERIOD, payment_date=CURRENT)
        connector, _ = self.connector()
        login = connector._login()
        with connector._client() as client, self.assertRaises(ExternalContractError):
            connector._prepare(client, login, CUIT, PERIOD, CURRENT, account='9999')
