"""PDF summary of the workpaper: one page, signed and dated.

This is the sheet that goes in the file and in front of an external auditor.
It carries the statement, the verdict, who prepared and who reviewed, the hash
of every source file and the fingerprint of the workpaper itself.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
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

from ..money import format_amount
from .model import Workpaper, generated_stamp

BRAND = colors.HexColor("#0F5666")
INK = colors.HexColor("#0F1A20")
INK_2 = colors.HexColor("#45565F")
LINE = colors.HexColor("#DCE3E7")
SOFT = colors.HexColor("#F0F3F5")
OK = colors.HexColor("#0B6B4F")
BAD = colors.HexColor("#A52E21")
OK_HEX = "#0B6B4F"
BAD_HEX = "#A52E21"


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold",
                                fontSize=15, textColor=BRAND, alignment=0, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontName="Helvetica",
                              fontSize=9, textColor=INK_2, spaceAfter=8),
        "h2": ParagraphStyle("h", parent=base["Normal"], fontName="Helvetica-Bold",
                             fontSize=9.5, textColor=BRAND, spaceBefore=10, spaceAfter=4),
        "body": ParagraphStyle("b", parent=base["Normal"], fontName="Helvetica",
                               fontSize=8.5, textColor=INK, leading=11),
        "small": ParagraphStyle("sm", parent=base["Normal"], fontName="Helvetica",
                                fontSize=7, textColor=INK_2, leading=9),
    }


def _statement_table(workpaper: Workpaper, styles) -> Table:
    currency = workpaper.account.currency
    rows = [["Concepto", f"Importe ({currency})"]]
    highlight: list[int] = []
    for index, (label, cents, kind) in enumerate(workpaper.statement, start=1):
        rows.append([label, format_amount(cents, currency)])
        if kind.endswith("total") or kind == "difference":
            highlight.append(index)

    table = Table(rows, colWidths=[110 * mm, 45 * mm], hAlign="LEFT")
    style = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK_2),
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for index in highlight:
        style += [
            ("FONT", (0, index), (-1, index), "Helvetica-Bold", 8.5),
            ("BACKGROUND", (0, index), (-1, index), SOFT),
        ]
    table.setStyle(TableStyle(style))
    return table


def _meta_table(workpaper: Workpaper) -> Table:
    account = workpaper.account
    rows = [
        ["Cliente", workpaper.client.name, "Período", workpaper.period.label],
        ["Entidad", workpaper.entity.name, "Moneda", account.currency],
        ["Cuenta", f"{account.name}", "Número", account.number or "—"],
    ]
    table = Table(rows, colWidths=[22 * mm, 78 * mm, 22 * mm, 33 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 7.5),
                ("FONT", (2, 0), (2, -1), "Helvetica-Bold", 7.5),
                ("FONT", (1, 0), (1, -1), "Helvetica", 8.5),
                ("FONT", (3, 0), (3, -1), "Helvetica", 8.5),
                ("TEXTCOLOR", (0, 0), (0, -1), INK_2),
                ("TEXTCOLOR", (2, 0), (2, -1), INK_2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _exceptions_table(workpaper: Workpaper) -> Table | None:
    if not workpaper.exceptions.rows:
        return None
    summary: dict[tuple[str, str], list] = {}
    for row in workpaper.exceptions.rows:
        side, _, _, _, amount, category, status, age, *_ = row
        key = (category, status)
        bucket = summary.setdefault(key, [0, 0.0, 0])
        bucket[0] += 1
        bucket[1] += amount
        bucket[2] = max(bucket[2], age)

    currency = workpaper.account.currency
    rows = [["Categoría", "Estado", "Partidas", f"Importe ({currency})", "Antigüedad máx."]]
    for (category, status), (count, amount, age) in sorted(summary.items()):
        rows.append(
            [category, status, str(count), format_amount(int(round(amount * 100)), currency),
             f"{age} días"]
        )
    table = Table(rows, colWidths=[52 * mm, 34 * mm, 18 * mm, 33 * mm, 24 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5),
                ("BACKGROUND", (0, 0), (-1, 0), SOFT),
                ("TEXTCOLOR", (0, 0), (-1, 0), INK_2),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ]
        )
    )
    return table


def _signatures(workpaper: Workpaper, styles) -> Table:
    prepared = workpaper.prepared_by.name if workpaper.prepared_by else "—"
    reviewed = workpaper.reviewed_by.name if workpaper.reviewed_by else "Pendiente"
    approved = (
        workpaper.reconciliation.approved_at.strftime("%d/%m/%Y %H:%M")
        if workpaper.reconciliation.approved_at
        else "Pendiente"
    )
    rows = [
        [
            Paragraph("<b>Preparado por</b>", styles["small"]),
            Paragraph("<b>Revisado y aprobado por</b>", styles["small"]),
        ],
        [
            Paragraph(f"{prepared}<br/><br/>___________________________", styles["body"]),
            Paragraph(
                f"{reviewed}<br/>{approved}<br/>___________________________", styles["body"]
            ),
        ],
    ]
    table = Table(rows, colWidths=[78 * mm, 78 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 6)]))
    return table


def write_pdf(workpaper: Workpaper, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = _styles()
    document = SimpleDocTemplate(
        str(path), pagesize=letter,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=workpaper.title, author=workpaper.firm.name,
    )
    story = [
        Paragraph(workpaper.firm.name, styles["title"]),
        Paragraph(
            f"Conciliación bancaria · {workpaper.account.name} · {workpaper.period.label}",
            styles["sub"],
        ),
        HRFlowable(width="100%", thickness=1.4, color=BRAND, spaceAfter=8),
        _meta_table(workpaper),
        Paragraph("Conciliación", styles["h2"]),
        _statement_table(workpaper, styles),
        Spacer(1, 6),
    ]

    verdict = "LA CONCILIACIÓN CUADRA" if workpaper.balanced else (
        f"NO CUADRA · diferencia de "
        f"{format_amount(workpaper.difference_cents, workpaper.account.currency)}"
    )
    story.append(
        Paragraph(
            f'<font color="{OK_HEX if workpaper.balanced else BAD_HEX}" size="10">'
            f"<b>{verdict}</b></font>",
            styles["body"],
        )
    )
    story.append(
        Paragraph(
            f"Emparejamiento automático: {workpaper.match_rate:.1%} de las partidas. "
            f"Emparejamientos registrados: {len(workpaper.matches.rows)}. "
            f"Correcciones humanas: {len(workpaper.corrections.rows)}.",
            styles["small"],
        )
    )

    exceptions = _exceptions_table(workpaper)
    if exceptions is not None:
        story += [Paragraph("Partidas conciliatorias y excepciones", styles["h2"]), exceptions]

    if workpaper.sources.rows:
        story.append(Paragraph("Archivos de origen", styles["h2"]))
        for row in workpaper.sources.rows:
            story.append(
                Paragraph(
                    f"{row[0]} ({row[1]}) · SHA-256 {row[2]} · "
                    f"{row[5]} de {row[4]} filas importadas",
                    styles["small"],
                )
            )

    story += [
        Spacer(1, 10),
        HRFlowable(width="100%", thickness=0.5, color=LINE, spaceAfter=6),
        _signatures(workpaper, styles),
        Spacer(1, 8),
        Paragraph(
            f"Huella del papel de trabajo: {workpaper.fingerprint}<br/>"
            f"Generado el {generated_stamp()} por Cuadre. "
            "Este documento se reconstruye de forma idéntica desde la base de datos.",
            styles["small"],
        ),
    ]
    document.build(story)
    return path
