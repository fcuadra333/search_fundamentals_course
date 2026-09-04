"""Excel workpaper.

One statement sheet per account, plus the supporting detail. Amounts are
written as numbers, not strings, so a reviewer can foot the columns.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side as XlSide
from openpyxl.utils import get_column_letter

from .model import Workpaper, generated_stamp

_INK = "FF0F1A20"
_BRAND = "FF0F5666"
_HEADER_FILL = PatternFill("solid", fgColor="FFF0F3F5")
_TOTAL_FILL = PatternFill("solid", fgColor="FFE0EEF1")
_THIN = XlSide(style="thin", color="FFDCE3E7")
_BORDER = Border(bottom=_THIN)
_MONEY = '#,##0.00;[Red]-#,##0.00'


def _title(sheet, row: int, text: str, size: int = 12) -> int:
    cell = sheet.cell(row=row, column=1, value=text)
    cell.font = Font(bold=True, size=size, color=_BRAND)
    return row + 1


def _header_row(sheet, row: int, columns: list[str]) -> int:
    for index, name in enumerate(columns, start=1):
        cell = sheet.cell(row=row, column=index, value=name)
        cell.font = Font(bold=True, size=9, color=_INK)
        cell.fill = _HEADER_FILL
        cell.border = _BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    return row + 1


def _autosize(sheet, widths: dict[int, int]) -> None:
    for index, width in widths.items():
        sheet.column_dimensions[get_column_letter(index)].width = width


def build_workbook(workpapers: list[Workpaper]) -> Workbook:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for workpaper in workpapers:
        _statement_sheet(workbook, workpaper)
    for workpaper in workpapers:
        _detail_sheets(workbook, workpaper)
    return workbook


def _sheet_name(base: str, workbook: Workbook) -> str:
    """Excel caps sheet names at 31 characters and forbids duplicates."""
    clean = "".join(ch for ch in base if ch not in "[]:*?/\\")[:31] or "Hoja"
    name, counter = clean, 2
    while name in workbook.sheetnames:
        suffix = f" {counter}"
        name = clean[: 31 - len(suffix)] + suffix
        counter += 1
    return name


def _statement_sheet(workbook: Workbook, workpaper: Workpaper):
    account = workpaper.account
    sheet = workbook.create_sheet(
        _sheet_name(f"{account.bank_name or account.name} {account.masked_number}", workbook)
    )
    row = _title(sheet, 1, workpaper.firm.name, 13)
    row = _title(sheet, row, "Conciliación bancaria", 11)
    for label, value in (
        ("Cliente", workpaper.client.name),
        ("Entidad", f"{workpaper.entity.name} · {workpaper.entity.tax_id or ''}"),
        ("Cuenta", f"{account.name} · {account.number or ''} · {account.currency}"),
        ("Período", workpaper.period.label),
        ("Preparado por", workpaper.prepared_by.name if workpaper.prepared_by else "—"),
        ("Revisado por", workpaper.reviewed_by.name if workpaper.reviewed_by else "—"),
        ("Generado", generated_stamp()),
        ("Huella del papel", workpaper.fingerprint),
    ):
        sheet.cell(row=row, column=1, value=label).font = Font(bold=True, size=9)
        sheet.cell(row=row, column=2, value=value).font = Font(size=9)
        row += 1

    row += 1
    row = _header_row(sheet, row, ["Concepto", f"Importe ({account.currency})"])
    for label, cents, kind in workpaper.statement:
        label_cell = sheet.cell(row=row, column=1, value=label)
        amount_cell = sheet.cell(row=row, column=2, value=cents / 100)
        amount_cell.number_format = _MONEY
        if kind.endswith("total") or kind == "difference":
            label_cell.font = Font(bold=True)
            amount_cell.font = Font(bold=True)
            label_cell.fill = _TOTAL_FILL
            amount_cell.fill = _TOTAL_FILL
        row += 1

    row += 1
    verdict = "LA CONCILIACIÓN CUADRA" if workpaper.balanced else "LA CONCILIACIÓN NO CUADRA"
    cell = sheet.cell(row=row, column=1, value=verdict)
    cell.font = Font(bold=True, size=11, color="FF0B6B4F" if workpaper.balanced else "FFA52E21")
    row += 2
    sheet.cell(row=row, column=1, value="Emparejamiento automático").font = Font(bold=True, size=9)
    sheet.cell(row=row, column=2, value=workpaper.match_rate).number_format = "0.0%"
    _autosize(sheet, {1: 42, 2: 22})
    return sheet


def _detail_sheets(workbook: Workbook, workpaper: Workpaper) -> None:
    account = workpaper.account
    prefix = (account.number or account.name)[-4:]
    for section in (
        workpaper.matches,
        workpaper.exceptions,
        workpaper.corrections,
        workpaper.sources,
        workpaper.model_usage,
    ):
        sheet = workbook.create_sheet(_sheet_name(f"{section.title} {prefix}", workbook))
        row = _title(sheet, 1, f"{section.title} — {account.name} — {workpaper.period.label}", 11)
        row += 1
        row = _header_row(sheet, row, section.columns)
        for values in section.rows:
            for index, value in enumerate(values, start=1):
                cell = sheet.cell(row=row, column=index, value=value)
                cell.font = Font(size=9)
                cell.alignment = Alignment(vertical="top", wrap_text=isinstance(value, str))
                if isinstance(value, float):
                    cell.number_format = _MONEY
            row += 1
        if not section.rows:
            sheet.cell(row=row, column=1, value="Sin registros.").font = Font(size=9, italic=True)
        _autosize(sheet, {index: 20 for index in range(1, len(section.columns) + 1)})
        sheet.freeze_panes = "A4"


def write_workbook(workpapers: list[Workpaper], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(workpapers).save(path)
    return path
