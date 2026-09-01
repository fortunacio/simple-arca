from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping
from zoneinfo import ZoneInfo


STALE_GRACE = timedelta(minutes=15)
ARGENTINA_TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")


class SourceCondition(str, Enum):
    HEALTHY = "HEALTHY"
    PARTIAL = "PARTIAL"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CREDENTIALS_REJECTED = "CREDENTIALS_REJECTED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    UNEXPECTED_RESPONSE = "UNEXPECTED_RESPONSE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    STALE = "STALE"
    PENDING = "PENDING"


class UserAction(str, Enum):
    NONE = "NONE"
    COMPLETE_ACCESS = "COMPLETE_ACCESS"
    MANUAL_VALIDATION = "MANUAL_VALIDATION"
    TECHNICAL_REVIEW = "TECHNICAL_REVIEW"


@dataclass(frozen=True)
class SourcePolicy:
    provider: str
    label: str
    snapshot_kind: str
    refresh_interval: timedelta
    stale_after: timedelta


SOURCE_POLICIES = {
    "ARCA_PROFILE": SourcePolicy(
        "ARCA_PROFILE",
        "Categoría y situación en ARCA",
        "taxpayer_profile",
        timedelta(hours=6),
        timedelta(hours=30),
    ),
    "ARCA_DFE": SourcePolicy(
        "ARCA_DFE",
        "Bandeja de ARCA",
        "unread_communications",
        timedelta(hours=6),
        timedelta(hours=30),
    ),
    "ARCA_CCMA": SourcePolicy(
        "ARCA_CCMA",
        "Deuda de Monotributo",
        "account_status",
        timedelta(hours=6),
        timedelta(hours=12),
    ),
    "ARCA_SETI": SourcePolicy(
        "ARCA_SETI",
        "Pagos de ARCA",
        "payment_status",
        timedelta(hours=6),
        timedelta(hours=12),
    ),
    "ARCA_INVOICES": SourcePolicy(
        "ARCA_INVOICES",
        "Facturación",
        "issued_invoices",
        timedelta(hours=6),
        timedelta(hours=30),
    ),
    "SANTA_FE_IIBB": SourcePolicy(
        "SANTA_FE_IIBB",
        "Ingresos Brutos",
        "account_status",
        timedelta(hours=6),
        timedelta(hours=12),
    ),
}


@dataclass(frozen=True)
class FailureDetail:
    kind: str
    stage: str = ""
    host: str = ""
    code: str = ""
    legacy: bool = False

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "stage": self.stage,
                "host": self.host,
                "code": self.code,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_value(cls, value: object, *, fallback_kind: str = "") -> FailureDetail | None:
        if not value:
            return None
        try:
            payload = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return cls(fallback_kind or "UNKNOWN", legacy=True)
        if not isinstance(payload, dict):
            return cls(fallback_kind or "UNKNOWN", legacy=True)
        return cls(
            kind=str(payload.get("kind") or fallback_kind or "UNKNOWN"),
            stage=str(payload.get("stage") or ""),
            host=str(payload.get("host") or ""),
            code=str(payload.get("code") or ""),
        )


@dataclass(frozen=True)
class SnapshotView:
    payload: dict | None
    fetched_at: datetime | None
    expires_at: datetime | None
    is_stale: bool

    @property
    def is_fresh(self) -> bool:
        return self.payload is not None and not self.is_stale


@dataclass(frozen=True)
class SourceHealth:
    policy: SourcePolicy
    condition: SourceCondition
    action: UserAction
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    snapshot: SnapshotView
    failure: FailureDetail | None = None

    @property
    def incident_fingerprint(self) -> str:
        success = self.last_success_at.isoformat() if self.last_success_at else "never"
        detail = self.failure.kind if self.failure else ""
        return f"{self.condition.value}:{detail}:{success}"


def parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def snapshot_view(
    row: Mapping[str, object] | None,
    policy: SourcePolicy,
    now: datetime,
) -> SnapshotView:
    if row is None:
        return SnapshotView(None, None, None, True)
    try:
        payload = json.loads(str(row["payload_json"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        payload = None
    fetched_at = parse_datetime(row["fetched_at"])
    expires_at = parse_datetime(row["expires_at"])
    stale = fetched_at is None or now - fetched_at > policy.stale_after + STALE_GRACE
    return SnapshotView(payload if isinstance(payload, dict) else None, fetched_at, expires_at, stale)


def evaluate_source(
    policy: SourcePolicy,
    connection: Mapping[str, object] | None,
    snapshot: SnapshotView,
    now: datetime,
) -> SourceHealth:
    status = str(connection["status"]) if connection is not None else "MISSING"
    last_attempt = parse_datetime(connection["updated_at"]) if connection is not None else None
    last_success = (
        parse_datetime(connection["last_success_at"])
        if connection is not None
        else None
    ) or snapshot.fetched_at
    failure = (
        FailureDetail.from_value(connection["last_error"], fallback_kind=status)
        if connection is not None
        else None
    )
    if status == "ERROR" and failure is None:
        failure = FailureDetail("UNKNOWN", legacy=True)

    mapping = {
        "LOGIN_REQUIRED": (SourceCondition.AUTH_REQUIRED, UserAction.MANUAL_VALIDATION),
        "CREDENTIALS_REQUIRED": (
            SourceCondition.CREDENTIALS_REJECTED,
            UserAction.COMPLETE_ACCESS,
        ),
        "EXTERNAL_UNAVAILABLE": (
            SourceCondition.UPSTREAM_UNAVAILABLE,
            UserAction.NONE,
        ),
        "UPSTREAM_ERROR": (SourceCondition.UPSTREAM_ERROR, UserAction.TECHNICAL_REVIEW),
        "INTEGRATION_CHANGED": (
            SourceCondition.UNEXPECTED_RESPONSE,
            UserAction.TECHNICAL_REVIEW,
        ),
        "UNEXPECTED_RESPONSE": (
            SourceCondition.UNEXPECTED_RESPONSE,
            UserAction.TECHNICAL_REVIEW,
        ),
        "INTERNAL_ERROR": (SourceCondition.INTERNAL_ERROR, UserAction.TECHNICAL_REVIEW),
        "ERROR": (SourceCondition.INTERNAL_ERROR, UserAction.TECHNICAL_REVIEW),
    }
    if status in mapping:
        condition, action = mapping[status]
    elif status == "PARTIAL":
        condition, action = SourceCondition.PARTIAL, UserAction.NONE
    elif status == "CONNECTED":
        condition, action = SourceCondition.HEALTHY, UserAction.NONE
    elif connection is None and snapshot.payload is not None:
        condition, action = SourceCondition.HEALTHY, UserAction.NONE
    else:
        condition, action = SourceCondition.PENDING, UserAction.NONE

    if condition in {SourceCondition.HEALTHY, SourceCondition.PARTIAL} and (
        snapshot.is_stale
        or last_success is None
        or now - last_success > policy.stale_after + STALE_GRACE
    ):
        condition = SourceCondition.STALE
        action = UserAction.NONE

    return SourceHealth(
        policy=policy,
        condition=condition,
        action=action,
        last_attempt_at=last_attempt,
        last_success_at=last_success,
        snapshot=snapshot,
        failure=failure,
    )


def source_health(db, chat_id: int, provider: str, now: datetime) -> SourceHealth:
    policy = SOURCE_POLICIES[provider]
    connections = {row["provider"]: row for row in db.get_connections(chat_id)}
    snapshot_row = db.get_live_snapshot(chat_id, provider, policy.snapshot_kind)
    view = snapshot_view(snapshot_row, policy, now)
    return evaluate_source(policy, connections.get(provider), view, now)


def format_health_datetime(value: datetime | None) -> str:
    if value is None:
        return "sin registro"
    return value.astimezone(ARGENTINA_TIMEZONE).strftime("%d/%m/%Y %H:%M ART")


def render_health_brief(health: SourceHealth) -> str:
    label = health.policy.label
    condition = health.condition
    if condition == SourceCondition.HEALTHY:
        return f"{label} está actualizado."
    if condition == SourceCondition.PARTIAL:
        message = f"{label} se actualizó parcialmente. Se reintentará automáticamente."
    elif condition == SourceCondition.AUTH_REQUIRED:
        message = f"{label} necesita una validación manual del acceso. Revisá /estado."
    elif condition == SourceCondition.CREDENTIALS_REJECTED:
        message = (
            f"{label} rechazó la clave fiscal guardada. Abrí /resumen y elegí "
            "Completar acceso."
        )
    elif condition == SourceCondition.UPSTREAM_UNAVAILABLE:
        message = (
            f"{label} no está disponible en el organismo. No necesitás hacer nada; "
            "se reintentará automáticamente."
        )
    elif condition == SourceCondition.UPSTREAM_ERROR:
        message = f"{label} rechazó la consulta. Requiere revisión técnica en /estado."
    elif condition == SourceCondition.UNEXPECTED_RESPONSE:
        message = (
            f"{label} respondió de una forma inesperada. Requiere revisión técnica "
            "en /estado."
        )
    elif condition == SourceCondition.INTERNAL_ERROR:
        if health.failure is not None and health.failure.legacy:
            message = f"{label} falló por una causa todavía indeterminada. Revisá /estado."
        else:
            message = f"{label} falló dentro del sistema. Requiere revisión en /estado."
    elif condition == SourceCondition.STALE:
        message = (
            f"{label} no pudo completar las actualizaciones programadas. "
            "Se reintentará automáticamente."
        )
    else:
        return f"{label} todavía no completó su primera actualización."
    if health.last_success_at is not None:
        message += f" Último dato válido: {format_health_datetime(health.last_success_at)}."
    else:
        message += " Todavía no hubo una actualización correcta."
    return message


def render_health_detail(health: SourceHealth) -> str:
    icons = {
        SourceCondition.HEALTHY: "🟢",
        SourceCondition.PARTIAL: "🟡",
        SourceCondition.PENDING: "⚪",
    }
    icon = icons.get(health.condition, "🔴" if health.action != UserAction.NONE else "🟡")
    lines = [
        f"{icon} {health.policy.label}",
        f"Estado: {health.condition.value}",
        f"Último intento: {format_health_datetime(health.last_attempt_at)}",
        f"Último dato válido: {format_health_datetime(health.last_success_at)}",
    ]
    if health.condition == SourceCondition.PARTIAL:
        lines.append("Detalle: la consulta quedó incompleta")
    if health.failure is not None and not health.failure.legacy:
        if health.failure.host:
            lines.append(f"Proveedor: {health.failure.host}")
        if health.failure.stage:
            lines.append(f"Etapa: {health.failure.stage}")
        if health.failure.code:
            lines.append(f"Código: {health.failure.code}")
    if health.action == UserAction.COMPLETE_ACCESS:
        lines.append("Acción: completar nuevamente el acceso desde /resumen")
    elif health.action == UserAction.MANUAL_VALIDATION:
        lines.append("Acción: validar manualmente el acceso")
    elif health.action == UserAction.TECHNICAL_REVIEW:
        lines.append("Acción: revisión técnica")
    elif health.condition in {SourceCondition.HEALTHY, SourceCondition.PARTIAL}:
        lines.append("Acción: ninguna")
    else:
        lines.append("Acción: ninguna; el sistema reintenta automáticamente")
    return "\n".join(lines)


def stale_snapshot_warning(health: SourceHealth) -> str | None:
    if health.snapshot.payload is None or not health.snapshot.is_stale:
        return None
    return (
        "⚠️ Estos son los últimos datos disponibles, consultados el "
        f"{format_health_datetime(health.snapshot.fetched_at)}. No se usarán para "
        "pagos ni otras operaciones."
    )
