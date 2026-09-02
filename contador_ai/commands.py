from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from .db import Database


class CommandAvailability(str, Enum):
    ALWAYS = "always"
    VERIFIED_IDENTITY = "verified_identity"
    LINKED_IDENTITY = "linked_identity"
    SOURCE = "source"
    INVOICE_MODULE = "invoice_module"
    INVOICE_EMISSION = "invoice_emission"
    ARCA_ENROLLMENT = "arca_enrollment"
    ACTIVE_INVOICE_FLOW = "active_invoice_flow"


class CommandContext(str, Enum):
    NONE = "none"
    INITIAL = "initial"
    INVOICE_FLOW = "invoice_flow"
    COMPATIBILITY_ALIAS = "compatibility_alias"


@dataclass(frozen=True)
class CommandGroup:
    key: str
    title: str
    menu_prefix: str


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    group: str
    availability: CommandAvailability = CommandAvailability.ALWAYS
    visible_in_menu: bool = True
    context: CommandContext = CommandContext.NONE
    aliases: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


COMMAND_GROUPS = (
    CommandGroup("general", "Vista general", "General"),
    CommandGroup("payments", "Pagos e impuestos", "Pagos"),
    CommandGroup("category", "Categoría y facturación", "Categoría"),
    CommandGroup("communications", "Comunicaciones", "Comunicaciones"),
    CommandGroup("invoices", "Comprobantes", "Comprobantes"),
    CommandGroup("account", "Cuenta y acceso", "Cuenta"),
    CommandGroup("help", "Ayuda y contexto", "Ayuda"),
)


COMMAND_SPECS = (
    CommandSpec("resumen", "ver qué requiere atención y qué tenés que pagar", "general"),
    CommandSpec("estado", "ver la salud y última actualización de cada fuente", "general"),
    CommandSpec(
        "vencimientos",
        "consultar los próximos vencimientos",
        "payments",
        CommandAvailability.SOURCE,
        sources=("ARCA_CCMA", "SANTA_FE_IIBB"),
    ),
    CommandSpec(
        "monotributo",
        "consultar la cuenta corriente de Monotributo",
        "payments",
        CommandAvailability.SOURCE,
        aliases=("deuda_arca",),
        sources=("ARCA_CCMA",),
    ),
    CommandSpec(
        "iibb",
        "consultar Ingresos Brutos de Santa Fe",
        "payments",
        CommandAvailability.SOURCE,
        sources=("SANTA_FE_IIBB",),
    ),
    CommandSpec(
        "pagos_arca",
        "consultar los pagos registrados en ARCA",
        "payments",
        CommandAvailability.SOURCE,
        sources=("ARCA_SETI",),
    ),
    CommandSpec(
        "categoria",
        "consultar la categoría actual y proyectada",
        "category",
        CommandAvailability.SOURCE,
        sources=("ARCA_PROFILE",),
    ),
    CommandSpec(
        "facturacion",
        "consultar la facturación registrada",
        "category",
        CommandAvailability.SOURCE,
        sources=("ARCA_INVOICES",),
    ),
    CommandSpec(
        "bandeja",
        "consultar mensajes nuevos de ARCA",
        "communications",
        CommandAvailability.SOURCE,
        sources=("ARCA_DFE",),
    ),
    CommandSpec(
        "facturas",
        "ver y descargar comprobantes",
        "invoices",
        CommandAvailability.INVOICE_MODULE,
    ),
    CommandSpec(
        "nueva_factura",
        "preparar una nueva Factura C",
        "invoices",
        CommandAvailability.INVOICE_EMISSION,
    ),
    CommandSpec(
        "nuevo_cliente",
        "guardar un cliente para futuras facturas",
        "invoices",
        CommandAvailability.INVOICE_EMISSION,
    ),
    CommandSpec("conectar", "vincular y verificar tu cuenta de ARCA", "account"),
    CommandSpec(
        "configurar_arca",
        "guardar o actualizar el acceso web a ARCA",
        "account",
        CommandAvailability.ARCA_ENROLLMENT,
    ),
    CommandSpec(
        "desconectar",
        "desvincular la cuenta y detener las consultas",
        "account",
        CommandAvailability.LINKED_IDENTITY,
    ),
    CommandSpec(
        "ayuda",
        "ver todos los comandos y su disponibilidad",
        "help",
        aliases=("help",),
    ),
    CommandSpec(
        "start",
        "abrir la entrada inicial del bot",
        "help",
        visible_in_menu=False,
        context=CommandContext.INITIAL,
    ),
    CommandSpec(
        "help",
        "alias compatible de /ayuda",
        "help",
        visible_in_menu=False,
        context=CommandContext.COMPATIBILITY_ALIAS,
    ),
    CommandSpec(
        "cancelar",
        "cancelar una carga de factura en curso",
        "help",
        CommandAvailability.ACTIVE_INVOICE_FLOW,
        visible_in_menu=False,
        context=CommandContext.INVOICE_FLOW,
    ),
    CommandSpec(
        "deuda_arca",
        "alias compatible de /monotributo",
        "help",
        CommandAvailability.SOURCE,
        visible_in_menu=False,
        context=CommandContext.COMPATIBILITY_ALIAS,
        sources=("ARCA_CCMA",),
    ),
)


SERVICE_COMMAND_NAMES = frozenset(
    {
        "start",
        "ayuda",
        "help",
        "conectar",
        "resumen",
        "estado",
        "bandeja",
        "monotributo",
        "configurar_arca",
        "facturacion",
        "categoria",
        "deuda_arca",
        "iibb",
        "pagos_arca",
        "vencimientos",
        "desconectar",
    }
)
INVOICE_COMMAND_NAMES = frozenset(
    {"facturas", "nueva_factura", "nuevo_cliente", "cancelar"}
)


class CommandCatalog:
    def __init__(
        self,
        db: Database,
        *,
        enabled_sources: Iterable[str] = (),
        invoice_module_enabled: bool = False,
        invoice_emission_chat_ids: Iterable[int] = (),
        arca_enrollment_enabled: bool = False,
        invoice_flow_active: Callable[[int], bool] | None = None,
    ):
        self.db = db
        self.enabled_sources = frozenset(enabled_sources)
        self.invoice_module_enabled = invoice_module_enabled
        self.invoice_emission_chat_ids = frozenset(invoice_emission_chat_ids)
        self.arca_enrollment_enabled = arca_enrollment_enabled
        self.invoice_flow_active = invoice_flow_active or (lambda _chat_id: False)
        self._groups = {group.key: group for group in COMMAND_GROUPS}

    def _identity(self, chat_id: int):
        return self.db.get_tax_identity(chat_id)

    def is_connected(self, chat_id: int) -> bool:
        identity = self._identity(chat_id)
        return identity is not None and identity["status"] == "VERIFIED"

    def unavailable_reason(self, spec: CommandSpec, chat_id: int) -> str | None:
        identity = self._identity(chat_id)
        connected = identity is not None and identity["status"] == "VERIFIED"
        if spec.availability is CommandAvailability.ALWAYS:
            return None
        if spec.availability is CommandAvailability.LINKED_IDENTITY:
            return (
                None
                if connected
                else "no disponible: no hay una cuenta vinculada"
            )
        if not connected:
            return "no disponible: la cuenta no está conectada"
        if spec.availability is CommandAvailability.VERIFIED_IDENTITY:
            return None
        if spec.availability is CommandAvailability.SOURCE:
            if any(source in self.enabled_sources for source in spec.sources):
                return None
            return "no disponible: la fuente no está configurada"
        if spec.availability is CommandAvailability.INVOICE_MODULE:
            return (
                None
                if self.invoice_module_enabled
                else "no disponible: el módulo de comprobantes no está configurado"
            )
        if spec.availability is CommandAvailability.INVOICE_EMISSION:
            if not self.invoice_module_enabled:
                return "no disponible: el módulo de comprobantes no está configurado"
            if chat_id not in self.invoice_emission_chat_ids:
                return "no disponible: la emisión no está autorizada para este chat"
            return None
        if spec.availability is CommandAvailability.ARCA_ENROLLMENT:
            return (
                None
                if self.arca_enrollment_enabled
                else "no disponible: el alta web de ARCA no está configurada"
            )
        if spec.availability is CommandAvailability.ACTIVE_INVOICE_FLOW:
            if not self.invoice_module_enabled:
                return "no disponible: el módulo de comprobantes no está configurado"
            if not self.invoice_flow_active(chat_id):
                return "disponible sólo durante una carga de factura"
            return None
        raise ValueError(f"Disponibilidad desconocida: {spec.availability}")

    def is_available(self, name: str, chat_id: int) -> bool:
        return self.reason_for(name, chat_id) is None

    def reason_for(self, name: str, chat_id: int) -> str | None:
        spec = next(spec for spec in COMMAND_SPECS if spec.name == name)
        return self.unavailable_reason(spec, chat_id)

    def menu_commands(self, chat_id: int) -> tuple[tuple[str, str], ...]:
        connected = self.is_connected(chat_id)
        result: list[tuple[str, str]] = []
        for spec in COMMAND_SPECS:
            if not spec.visible_in_menu or self.unavailable_reason(spec, chat_id):
                continue
            if spec.name == "conectar" and connected:
                continue
            group = self._groups[spec.group]
            result.append((spec.name, f"{group.menu_prefix} · {spec.description}"))
        return tuple(result)

    def render_help(self, chat_id: int) -> str:
        lines = ["Comandos disponibles", ""]
        for group in COMMAND_GROUPS:
            lines.append(group.title)
            for spec in (item for item in COMMAND_SPECS if item.group == group.key):
                reason = self.unavailable_reason(spec, chat_id)
                suffix = f" ({reason})" if reason else ""
                lines.append(f"/{spec.name} — {spec.description}{suffix}")
            lines.append("")
        lines.extend(
            [
                "El bot también te avisa automáticamente cuando aparece algo importante.",
                "Actualmente revisa sólo las fuentes que figuran como configuradas arriba.",
            ]
        )
        return "\n".join(lines)

    @property
    def command_names(self) -> frozenset[str]:
        return frozenset(spec.name for spec in COMMAND_SPECS)
