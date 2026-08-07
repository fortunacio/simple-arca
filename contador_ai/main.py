from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys
import urllib.parse
from datetime import timedelta
from pathlib import Path

from .arca.dfe import DfeClient
from .arca.padron import ConstanciaInscripcionClient
from .arca.sync import ArcaDfeSynchronizer, ArcaInvoicesSynchronizer, ArcaProfileSynchronizer
from .arca.wsfe import WsfeClient
from .arca.wsaa import (
    ArcaEnvironment,
    CachedTicketProvider,
    EncryptedTicketStore,
    OpenSslCmsSigner,
    WsaaClient,
)
from .db import Database
from .invoice_bot import InvoiceBotService
from .invoice_gateway import InvoiceGateway
from .invoice_store import InvoiceStore
from .iibb_payment import IibbPaymentBotService, IibbPaymentStore
from .monotributo import ArcaCategoryLimitsProvider
from .notifications import TaxNotificationMonitor
from .service import TaxMonitorService
from .telegram import TelegramClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor tributario por Telegram")
    parser.add_argument("--init-db", action="store_true", help="inicializa la base y termina")
    parser.add_argument("--healthcheck", action="store_true", help="verifica la base y termina")
    parser.add_argument("--print-status", action="store_true", help="imprime /estado y termina")
    parser.add_argument("--poll-once", action="store_true", help="consulta Telegram una sola vez")
    parser.add_argument(
        "--sync-arca-profile",
        action="store_true",
        help="verifica la delegación y sincroniza la Constancia para --chat-id",
    )
    parser.add_argument(
        "--arca-dummy",
        action="store_true",
        help="verifica disponibilidad de la Constancia sin consultar contribuyentes",
    )
    parser.add_argument(
        "--sync-arca-dfe",
        action="store_true",
        help="sincroniza resúmenes no leídos del DFE para --chat-id",
    )
    parser.add_argument("--chat-id", type=int, default=1, help="chat local para --command")
    parser.add_argument(
        "--command",
        help="ejecuta localmente un comando del bot, por ejemplo /estado",
    )
    parser.add_argument(
        "--import-monthly-billing",
        metavar="JSON",
        help="importa totales mensuales verificados desde un archivo JSON o '-'",
    )
    return parser


def build_database() -> Database:
    path = os.environ.get("TAX_MONITOR_DB", "./data/contador_ai.sqlite3")
    db = Database(Path(path))
    db.initialize()
    return db


def build_arca_environment() -> ArcaEnvironment:
    value = os.environ.get("ARCA_ENVIRONMENT", "homologation").strip().lower()
    try:
        return ArcaEnvironment(value)
    except ValueError as exc:
        allowed = ", ".join(environment.value for environment in ArcaEnvironment)
        raise SystemExit(f"ARCA_ENVIRONMENT inválido. Valores permitidos: {allowed}") from exc


def load_session_encryption_key() -> str:
    direct_value = os.environ.get("SESSION_ENCRYPTION_KEY", "").strip()
    file_value = os.environ.get("SESSION_ENCRYPTION_KEY_FILE", "").strip()
    if direct_value and file_value:
        raise SystemExit(
            "Configurar sólo SESSION_ENCRYPTION_KEY o SESSION_ENCRYPTION_KEY_FILE, no ambas."
        )
    if direct_value:
        return direct_value
    if not file_value:
        return ""
    key_path = Path(file_value).expanduser().resolve()
    try:
        return key_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise SystemExit(f"No se pudo leer SESSION_ENCRYPTION_KEY_FILE: {key_path}") from exc


def arca_enrollment_base_url() -> str:
    value = os.environ.get("ARCA_ENROLLMENT_BASE_URL", "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    tailscale_http_enabled = os.environ.get(
        "ARCA_ENROLLMENT_ALLOW_TAILSCALE_HTTP", "false"
    ).strip().lower() in {"1", "true", "yes"}
    tailscale_host = False
    if parsed.hostname:
        try:
            tailscale_host = ipaddress.ip_address(parsed.hostname) in ipaddress.ip_network(
                "100.64.0.0/10"
            )
        except ValueError:
            tailscale_host = parsed.hostname.lower().endswith(".ts.net")
    allowed_http = parsed.scheme == "http" and (
        local or (tailscale_http_enabled and tailscale_host)
    )
    if parsed.scheme != "https" and not allowed_http:
        raise SystemExit(
            "ARCA_ENROLLMENT_BASE_URL debe usar HTTPS. HTTP sólo se permite en "
            "localhost o, con ARCA_ENROLLMENT_ALLOW_TAILSCALE_HTTP=true, en una "
            "IP de Tailscale o un nombre terminado en .ts.net."
        )
    return value


def build_profile_synchronizer(db: Database) -> ArcaProfileSynchronizer:
    certificate = os.environ.get("ARCA_CERTIFICATE_PATH", "").strip()
    private_key = os.environ.get("ARCA_PRIVATE_KEY_PATH", "").strip()
    if not certificate or not private_key:
        raise SystemExit(
            "Faltan ARCA_CERTIFICATE_PATH y ARCA_PRIVATE_KEY_PATH para sincronizar Padrón."
        )
    environment = build_arca_environment()
    signer = OpenSslCmsSigner(Path(certificate), Path(private_key))
    return ArcaProfileSynchronizer(
        db=db,
        wsaa=WsaaClient(environment, signer),
        padron=ConstanciaInscripcionClient(environment),
    )


def build_dfe_synchronizer(db: Database) -> ArcaDfeSynchronizer:
    certificate = os.environ.get("ARCA_CERTIFICATE_PATH", "").strip()
    private_key = os.environ.get("ARCA_PRIVATE_KEY_PATH", "").strip()
    if not certificate or not private_key:
        raise SystemExit(
            "Faltan ARCA_CERTIFICATE_PATH y ARCA_PRIVATE_KEY_PATH para sincronizar DFE."
        )
    environment = build_arca_environment()
    signer = OpenSslCmsSigner(Path(certificate), Path(private_key))
    return ArcaDfeSynchronizer(
        db=db,
        wsaa=WsaaClient(environment, signer),
        dfe=DfeClient(environment),
    )


def build_live_synchronizers(db: Database):
    certificate = os.environ.get("ARCA_CERTIFICATE_PATH", "").strip()
    private_key = os.environ.get("ARCA_PRIVATE_KEY_PATH", "").strip()
    if not certificate or not private_key:
        if certificate or private_key:
            raise SystemExit(
                "ARCA_CERTIFICATE_PATH y ARCA_PRIVATE_KEY_PATH deben configurarse juntos."
            )
        synchronizers = {}
    else:
        environment = build_arca_environment()
        tickets = build_ticket_provider(db, environment, certificate, private_key)
        snapshot_ttl = timedelta(
            seconds=int(os.environ.get("ARCA_OFFICIAL_SNAPSHOT_TTL_SECONDS", "129600"))
        )
        profile = ArcaProfileSynchronizer(
            db=db,
            wsaa=tickets,
            padron=ConstanciaInscripcionClient(environment),
            snapshot_ttl=snapshot_ttl,
        )
        dfe = ArcaDfeSynchronizer(
            db=db,
            wsaa=tickets,
            dfe=DfeClient(environment),
            snapshot_ttl=snapshot_ttl,
        )
        invoices = ArcaInvoicesSynchronizer(
            db=db,
            wsaa=tickets,
            wsfe=WsfeClient(environment),
            snapshot_ttl=snapshot_ttl,
        )
        synchronizers = {
            "ARCA_PROFILE": profile.sync_chat,
            "ARCA_DFE": dfe.sync_chat,
            "ARCA_INVOICES": invoices.sync_chat,
        }
    if os.environ.get("CCMA_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
        synchronizers["ARCA_CCMA"] = lambda _chat_id: None
        synchronizers["ARCA_SETI"] = lambda _chat_id: None
        if os.environ.get("SANTA_FE_IIBB_ENABLED", "true").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            synchronizers["SANTA_FE_IIBB"] = lambda _chat_id: None
    return synchronizers


def build_ticket_provider(
    db: Database,
    environment: ArcaEnvironment,
    certificate: str,
    private_key: str,
) -> CachedTicketProvider:
    signer = OpenSslCmsSigner(Path(certificate), Path(private_key))
    ticket_store = None
    encryption_key = load_session_encryption_key()
    if encryption_key:
        operator_cuit = os.environ.get("ARCA_OPERATOR_CUIT", "").strip()
        certificate_alias = os.environ.get("ARCA_CERTIFICATE_ALIAS", "").strip()
        if not operator_cuit or not certificate_alias:
            raise SystemExit(
                "ARCA_OPERATOR_CUIT y ARCA_CERTIFICATE_ALIAS son obligatorios para "
                "persistir Tickets de Acceso."
            )
        ticket_store = EncryptedTicketStore(
            db,
            encryption_key,
            f"{environment.value}:{operator_cuit}:{certificate_alias}",
        )
    elif environment is ArcaEnvironment.PRODUCTION:
        raise SystemExit(
            "SESSION_ENCRYPTION_KEY es obligatoria para usar ARCA en producción."
        )
    return CachedTicketProvider(WsaaClient(environment, signer), ticket_store)


def parse_chat_ids(value: str, setting: str) -> frozenset[int]:
    if not value.strip():
        return frozenset()
    try:
        return frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise SystemExit(f"{setting} debe contener números separados por coma") from exc


def build_invoice_bot(db: Database) -> InvoiceBotService | None:
    certificate = os.environ.get("ARCA_CERTIFICATE_PATH", "").strip()
    private_key = os.environ.get("ARCA_PRIVATE_KEY_PATH", "").strip()
    if not certificate and not private_key:
        return None
    if not certificate or not private_key:
        raise SystemExit(
            "ARCA_CERTIFICATE_PATH y ARCA_PRIVATE_KEY_PATH deben configurarse juntos."
        )
    environment = build_arca_environment()
    tickets = build_ticket_provider(db, environment, certificate, private_key)
    point_raw = os.environ.get("ARCA_WSFE_POINT_OF_SALE", "0").strip() or "0"
    try:
        point_of_sale = int(point_raw)
    except ValueError as exc:
        raise SystemExit("ARCA_WSFE_POINT_OF_SALE debe ser un número") from exc
    allowed_chat_ids = parse_chat_ids(
        os.environ.get("INVOICE_EMISSION_CHAT_IDS", ""),
        "INVOICE_EMISSION_CHAT_IDS",
    )
    store = InvoiceStore(db)
    gateway = InvoiceGateway(
        store=store,
        tickets=tickets,
        padron=ConstanciaInscripcionClient(environment),
        wsfe=WsfeClient(environment),
        point_of_sale=point_of_sale,
        allowed_chat_ids=allowed_chat_ids,
        issuer_iibb=os.environ.get("INVOICE_ISSUER_IIBB", "").strip(),
    )
    return InvoiceBotService(store, gateway)


def build_iibb_payment_bot(db: Database) -> IibbPaymentBotService:
    enabled = os.environ.get("IIBB_PAYMENTS_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    allowed = parse_chat_ids(
        os.environ.get("IIBB_PAYMENT_CHAT_IDS", "").strip()
        or os.environ.get("INVOICE_EMISSION_CHAT_IDS", ""),
        "IIBB_PAYMENT_CHAT_IDS",
    )
    return IibbPaymentBotService(IibbPaymentStore(db), allowed, enabled=enabled)


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db = build_database()
    if args.init_db:
        print(f"Base inicializada en {db.path}")
        return
    if args.healthcheck:
        print("ok")
        return
    if args.import_monthly_billing:
        identity = db.get_tax_identity(args.chat_id)
        if identity is None or identity["status"] != "VERIFIED":
            raise SystemExit("El chat debe tener un CUIT verificado antes de importar")
        try:
            if args.import_monthly_billing == "-":
                payload = json.load(sys.stdin)
            else:
                with Path(args.import_monthly_billing).open(encoding="utf-8") as stream:
                    payload = json.load(stream)
            source = str(payload["source"])
            months = payload["months"]
            if not isinstance(months, list):
                raise ValueError("months debe ser una lista")
            imported = db.import_monthly_billing_history(
                args.chat_id,
                months,
                source=source,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"No pude importar el historial mensual: {exc}") from exc
        print(f"Meses importados: {imported}")
        return

    arca_environment = build_arca_environment()
    service = TaxMonitorService(
        db,
        operator_cuit=os.environ.get("ARCA_OPERATOR_CUIT", ""),
        certificate_alias=os.environ.get("ARCA_CERTIFICATE_ALIAS", ""),
        arca_environment=arca_environment.value,
        live_synchronizers=build_live_synchronizers(db),
        category_limits_provider=ArcaCategoryLimitsProvider(
            db,
            refresh_after=timedelta(
                seconds=int(
                    os.environ.get("ARCA_CATEGORY_LIMITS_REFRESH_SECONDS", "86400")
                )
            ),
        ).current,
        ccma_login_url=os.environ.get("CCMA_LOGIN_URL", ""),
        arca_enrollment_base_url=arca_enrollment_base_url(),
    )
    if args.print_status:
        print(service.status(args.chat_id))
        return
    if args.command:
        print(service.dispatch(args.command, args.chat_id))
        return
    if args.arca_dummy:
        state = ConstanciaInscripcionClient(arca_environment).dummy()
        print(" ".join(f"{key}={value}" for key, value in state.items()))
        return
    if args.sync_arca_profile:
        synchronizer = build_profile_synchronizer(db)
        synchronizer.sync_chat(args.chat_id)
        print(service.category(args.chat_id))
        return
    if args.sync_arca_dfe:
        synchronizer = build_dfe_synchronizer(db)
        synchronizer.sync_chat(args.chat_id)
        print(service.inbox(args.chat_id))
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Falta TELEGRAM_BOT_TOKEN. Copiá config.example.env a .env y completalo."
        )

    allowed_chat_ids = parse_chat_ids(
        os.environ.get("BOT_ALLOWED_CHAT_IDS", ""),
        "BOT_ALLOWED_CHAT_IDS",
    )
    if not allowed_chat_ids:
        raise SystemExit("BOT_ALLOWED_CHAT_IDS es obligatorio; vacío bloquea el inicio del bot.")
    client = TelegramClient(
        token,
        db,
        service,
        allowed_chat_ids=allowed_chat_ids,
        invoice_bot=build_invoice_bot(db),
        iibb_payment_bot=build_iibb_payment_bot(db),
        notification_monitor=(
            TaxNotificationMonitor(
                db,
                service,
                refresh_interval=timedelta(
                    seconds=int(os.environ.get("AUTOMATIC_SYNC_INTERVAL_SECONDS", "21600"))
                ),
            )
            if os.environ.get("AUTOMATIC_MONITOR_ENABLED", "true").strip().lower()
            in {"1", "true", "yes"}
            else None
        ),
        monitor_interval_seconds=int(
            os.environ.get("AUTOMATIC_MONITOR_CHECK_SECONDS", "300")
        ),
    )
    if args.poll_once:
        handled = client.poll_once(timeout=1)
        print(f"Mensajes procesados: {handled}")
        return
    client.run()
