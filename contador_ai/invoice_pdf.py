from __future__ import annotations

import base64
import json
from io import BytesIO

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ARCA_QR_URL = "https://www.arca.gob.ar/fe/qr/"


def _date(value: str) -> str:
    parts = (value or "").split("-")
    return "/".join(reversed(parts)) if len(parts) == 3 else value


def _money(value: object) -> str:
    amount = float(value)
    formatted = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"$ {formatted}"


def qr_payload(invoice) -> dict:
    return {
        "ver": 1,
        "fecha": invoice["issue_date"],
        "cuit": int(invoice["emitter_cuit"]),
        "ptoVta": int(invoice["point_of_sale"]),
        "tipoCmp": int(invoice["invoice_type"]),
        "nroCmp": int(invoice["number"]),
        "importe": float(invoice["total"]),
        "moneda": invoice["currency"],
        "ctz": float(invoice["exchange_rate"]),
        "tipoDocRec": 80,
        "nroDocRec": int(invoice["receiver_cuit"]),
        "tipoCodAut": "E",
        "codAut": int(invoice["cae"]),
    }


def qr_url(invoice) -> str:
    encoded = base64.b64encode(
        json.dumps(qr_payload(invoice), separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).decode("ascii")
    return f"{ARCA_QR_URL}?p={encoded}"


def _qr_drawing(data: str, size: float = 30 * mm) -> Drawing:
    widget = QrCodeWidget(data)
    bounds = widget.getBounds()
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    drawing = Drawing(size, size, transform=[size / width, 0, 0, size / height, 0, 0])
    drawing.add(widget)
    return drawing


def build_invoice_pdf(invoice) -> bytes:
    if not invoice["cae"]:
        raise ValueError("No se puede generar un comprobante fiscal sin CAE")

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=13 * mm,
        rightMargin=13 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=(
            f"Factura C {int(invoice['point_of_sale']):05d}-"
            f"{int(invoice['number']):08d}"
        ),
        author=invoice["emitter_name"],
    )
    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "InvoiceNormal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.4,
        leading=11,
        textColor=colors.HexColor("#1F2937"),
    )
    small = ParagraphStyle("InvoiceSmall", parent=normal, fontSize=7.4, leading=9)
    label = ParagraphStyle(
        "InvoiceLabel",
        parent=normal,
        fontName="Helvetica-Bold",
        fontSize=8,
        textColor=colors.HexColor("#374151"),
    )
    title = ParagraphStyle(
        "InvoiceTitle",
        parent=normal,
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=18,
    )
    centered = ParagraphStyle("InvoiceCentered", parent=normal, alignment=TA_CENTER)
    right = ParagraphStyle("InvoiceRight", parent=normal, alignment=TA_RIGHT)
    story = []

    issuer = [
        Paragraph(invoice["emitter_name"], title),
        Paragraph(f"Domicilio comercial: {invoice['emitter_address']}", normal),
        Paragraph(f"Condición frente al IVA: {invoice['emitter_iva_condition']}", normal),
    ]
    if invoice["emitter_activity"]:
        issuer.append(Paragraph(f"Actividad: {invoice['emitter_activity']}", small))

    document_box = Table(
        [
            [Paragraph("C", ParagraphStyle("Letter", parent=title, fontSize=25, alignment=TA_CENTER))],
            [Paragraph("CÓD. 011", centered)],
        ],
        colWidths=[25 * mm],
        rowHeights=[14 * mm, 7 * mm],
    )
    document_box.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 1.2, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    document_box.hAlign = "CENTER"
    number_block = [
        Paragraph("FACTURA", title),
        Paragraph("ORIGINAL", label),
        Spacer(1, 2 * mm),
        Paragraph(
            f"Punto de venta: {int(invoice['point_of_sale']):05d}&nbsp;&nbsp;&nbsp; "
            f"Comprobante N°: {int(invoice['number']):08d}",
            normal,
        ),
        Paragraph(f"Fecha de emisión: {_date(invoice['issue_date'])}", normal),
        Paragraph(f"CUIT: {invoice['emitter_cuit']}", normal),
        Paragraph(f"Ingresos Brutos: {invoice['emitter_iibb']}", normal),
        Paragraph(f"Inicio de actividades: {_date(invoice['emitter_activity_start'])}", normal),
    ]
    header = Table(
        [[issuer, document_box, number_block]],
        colWidths=[76 * mm, 27 * mm, 77 * mm],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9CA3AF")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
            ("LEFTPADDING", (1, 0), (1, 0), 0),
            ("RIGHTPADDING", (1, 0), (1, 0), 0),
            ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ],
    )
    story.extend([header, Spacer(1, 5 * mm)])

    period = Table(
        [
            [
                Paragraph("Período facturado", label),
                Paragraph(f"Desde: {_date(invoice['service_from'])}", normal),
                Paragraph(f"Hasta: {_date(invoice['service_to'])}", normal),
                Paragraph(f"Vencimiento: {_date(invoice['due_date'])}", normal),
            ]
        ],
        colWidths=[42 * mm, 42 * mm, 42 * mm, 54 * mm],
        style=[
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#9CA3AF")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ],
    )
    story.extend([period, Spacer(1, 4 * mm)])

    receiver = Table(
        [
            [Paragraph("CUIT", label), Paragraph(invoice["receiver_cuit"], normal),
             Paragraph("Razón social", label), Paragraph(invoice["receiver_name"], normal)],
            [Paragraph("Condición IVA", label), Paragraph(invoice["receiver_iva_condition"], normal),
             Paragraph("Domicilio", label), Paragraph(invoice["receiver_address"], normal)],
            [Paragraph("Condición de venta", label), Paragraph(invoice["sale_condition"], normal), "", ""],
        ],
        colWidths=[28 * mm, 55 * mm, 31 * mm, 66 * mm],
        style=[
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#9CA3AF")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D1D5DB")),
            ("SPAN", (1, 2), (3, 2)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ],
    )
    story.extend([receiver, Spacer(1, 5 * mm)])

    items = Table(
        [
            [
                Paragraph("Descripción", centered),
                Paragraph("Cantidad", centered),
                Paragraph("Unidad", centered),
                Paragraph("Precio unitario", centered),
                Paragraph("Subtotal", centered),
            ],
            [
                Paragraph(invoice["description"], normal),
                Paragraph(invoice["quantity"], right),
                Paragraph(invoice["unit_description"], centered),
                Paragraph(_money(invoice["unit_price"]), right),
                Paragraph(_money(invoice["total"]), right),
            ],
        ],
        colWidths=[72 * mm, 21 * mm, 31 * mm, 28 * mm, 28 * mm],
        rowHeights=[9 * mm, 19 * mm],
        style=[
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E5E7EB")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#6B7280")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9CA3AF")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
        ],
    )
    story.extend([items, Spacer(1, 6 * mm)])

    total = Table(
        [[Paragraph("Importe total", ParagraphStyle("TotalLabel", parent=title, fontSize=12)),
          Paragraph(_money(invoice["total"]), ParagraphStyle("Total", parent=title, alignment=TA_RIGHT))]],
        colWidths=[125 * mm, 55 * mm],
        style=[
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
        ],
    )
    story.extend([total, Spacer(1, 9 * mm), HRFlowable(color=colors.HexColor("#D1D5DB")), Spacer(1, 4 * mm)])

    authorization = [
        Paragraph("Comprobante autorizado por ARCA", label),
        Spacer(1, 2 * mm),
        Paragraph(f"CAE: {invoice['cae']}", normal),
        Paragraph(f"Vencimiento del CAE: {_date(invoice['cae_expiry'])}", normal),
        Spacer(1, 2 * mm),
        Paragraph("El QR permite consultar los datos fiscales del comprobante.", small),
    ]
    footer = Table(
        [[_qr_drawing(qr_url(invoice)), authorization]],
        colWidths=[36 * mm, 144 * mm],
        style=[
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        ],
    )
    story.append(footer)
    document.build(story)
    return output.getvalue()


def invoice_filename(invoice) -> str:
    return (
        f"{invoice['emitter_cuit']}_{int(invoice['invoice_type']):03d}_"
        f"{int(invoice['point_of_sale']):05d}_{int(invoice['number']):08d}.pdf"
    )
