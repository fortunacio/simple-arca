from __future__ import annotations

from datetime import datetime
from xml.etree import ElementTree as ET

from .errors import ArcaResponseError, ArcaSoapFault


SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def first_local(root: ET.Element, name: str) -> ET.Element | None:
    return next((element for element in root.iter() if local_name(element.tag) == name), None)


def children_local(root: ET.Element, name: str) -> list[ET.Element]:
    return [element for element in root.iter() if local_name(element.tag) == name]


def child_text(root: ET.Element, name: str, default: str = "") -> str:
    element = next(
        (child for child in list(root) if local_name(child.tag) == name),
        None,
    )
    return (element.text or "").strip() if element is not None else default


def parse_xml(payload: bytes | str, context: str) -> ET.Element:
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ArcaResponseError(f"{context}: XML inválido") from exc


def raise_for_soap_fault(root: ET.Element) -> None:
    fault = first_local(root, "Fault")
    if fault is None:
        return
    reason = first_local(fault, "Text")
    soap12_code = first_local(fault, "Value")
    message = (
        child_text(fault, "faultstring")
        or ((reason.text or "").strip() if reason is not None else "")
        or child_text(fault, "Reason")
    )
    code = child_text(fault, "faultcode") or (
        (soap12_code.text or "").strip() if soap12_code is not None else ""
    )
    detail = ": ".join(value for value in (code, message) if value)
    raise ArcaSoapFault(detail or "ARCA devolvió un SOAP Fault")


def parse_datetime(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ArcaResponseError(f"Fecha inválida en respuesta de ARCA: {value!r}") from exc


def optional_int(value: str) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None
