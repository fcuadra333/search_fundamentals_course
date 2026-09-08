"""Minimal XLSX writer built on the standard library.

An evidence export that a company can hand to an auditor has to be a real
spreadsheet, not a CSV with an .xlsx extension. This writes the small subset of
the format that a report needs — inline strings, numbers, a bold header row,
frozen panes and column widths — using nothing but `zipfile` and string
formatting, so the platform keeps its single dependency.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{sheets}
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

# Two fonts (regular, bold) and two fills so the header row reads as a header.
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1F3A5F"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
</cellXfs>
</styleSheet>"""


@dataclass
class Sheet:
    """One worksheet: a header row and its data rows."""

    name: str
    headers: Sequence[str]
    rows: Iterable[Sequence[Any]] = field(default_factory=list)
    widths: Sequence[int] | None = None

    @property
    def safe_name(self) -> str:
        # Excel refuses these characters and anything past 31 chars.
        cleaned = "".join(character for character in self.name if character not in "[]:*?/\\")
        return cleaned[:31] or "Hoja"


class Workbook:
    def __init__(self) -> None:
        self.sheets: list[Sheet] = []

    def add_sheet(self, sheet: Sheet) -> Sheet:
        self.sheets.append(sheet)
        return sheet

    def save(self, path: str) -> str:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES.format(
                sheets="\n".join(
                    f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
                    f'ContentType="application/vnd.openxmlformats-officedocument.'
                    f'spreadsheetml.worksheet+xml"/>'
                    for index in range(1, len(self.sheets) + 1)
                )
            ))
            archive.writestr("_rels/.rels", _ROOT_RELS)
            archive.writestr("xl/workbook.xml", self._workbook_xml())
            archive.writestr("xl/_rels/workbook.xml.rels", self._workbook_rels())
            archive.writestr("xl/styles.xml", _STYLES)
            for index, sheet in enumerate(self.sheets, start=1):
                archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(sheet))
        return path

    def _workbook_xml(self) -> str:
        entries = "".join(
            f'<sheet name="{escape(sheet.safe_name)}" sheetId="{index}" r:id="rId{index}"/>'
            for index, sheet in enumerate(self.sheets, start=1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{entries}</sheets></workbook>"
        )

    def _workbook_rels(self) -> str:
        entries = "".join(
            f'<Relationship Id="rId{index}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(self.sheets) + 1)
        )
        style_id = len(self.sheets) + 1
        entries += (
            f'<Relationship Id="rId{style_id}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            f'Target="styles.xml"/>'
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{entries}</Relationships>"
        )


def _sheet_xml(sheet: Sheet) -> str:
    widths = sheet.widths or [max(12, min(48, len(str(header)) + 6)) for header in sheet.headers]
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    body = [_row_xml(1, sheet.headers, style=1)]
    for number, row in enumerate(sheet.rows, start=2):
        body.append(_row_xml(number, row, style=0))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{columns}</cols><sheetData>{''.join(body)}</sheetData></worksheet>"
    )


def _row_xml(number: int, values: Sequence[Any], *, style: int) -> str:
    cells = []
    for index, value in enumerate(values):
        reference = f"{_column_name(index)}{number}"
        cells.append(_cell_xml(reference, value, style))
    return f'<row r="{number}">{"".join(cells)}</row>'


def _cell_xml(reference: str, value: Any, style: int) -> str:
    attributes = f' s="{style}"' if style else ""
    if value is None:
        return f'<c r="{reference}"{attributes}/>'
    if isinstance(value, bool):
        value = "sí" if value else "no"
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"{attributes}><v>{value}</v></c>'
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc).isoformat(timespec="seconds")
    text = escape(str(value))
    return f'<c r="{reference}"{attributes} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name
