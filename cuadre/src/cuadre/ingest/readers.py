"""Reading CSV and Excel exports into a raw table.

The reader makes no decision about meaning: it finds the header row and hands
back cells. Meaning is the format profile's job.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from typing import Any

from openpyxl import load_workbook

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_DELIMITERS = (";", ",", "\t", "|")
_HEADER_SCAN_ROWS = 25


class UnsupportedFileError(ValueError):
    """Neither CSV nor Excel. v1 ingests files only, and only these two."""


@dataclass
class RawTable:
    headers: list[str]
    rows: list[list[Any]]
    header_row: int = 0
    sheet_name: str | None = None
    notes: list[str] = field(default_factory=list)
    # Physical 1-based line of each retained row in the original file. Blank
    # lines are dropped before parsing, so without this a rejection would tell
    # the user to look at a row that is not the one that failed.
    row_numbers: list[int] = field(default_factory=list)

    def column(self, name: str) -> list[Any]:
        try:
            index = self.headers.index(name)
        except ValueError:
            return []
        return [row[index] if index < len(row) else None for row in self.rows]

    def sample(self, limit: int = 5) -> list[dict[str, Any]]:
        return [
            {h: (row[i] if i < len(row) else None) for i, h in enumerate(self.headers)}
            for row in self.rows[:limit]
        ]


def read_table(data: bytes, filename: str, header_row: int | None = None) -> RawTable:
    lowered = filename.lower()
    if lowered.endswith((".xlsx", ".xlsm")):
        return _read_excel(data, header_row)
    if lowered.endswith((".csv", ".txt", ".tsv")):
        return _read_csv(data, header_row)
    raise UnsupportedFileError(
        "Formato no soportado. La versión 1 acepta Excel (.xlsx) y CSV (.csv)."
    )


def _read_csv(data: bytes, header_row: int | None) -> RawTable:
    text, encoding = _decode(data)
    delimiter = _sniff_delimiter(text)
    parsed = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows, numbers = _drop_blank(parsed)
    if not rows:
        raise UnsupportedFileError("El archivo está vacío.")
    table = _split_header(rows, numbers, header_row)
    table.notes.append(f"CSV, separador '{delimiter}', codificación {encoding}")
    return table


def _read_excel(data: bytes, header_row: int | None) -> RawTable:
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    parsed = [list(r) for r in sheet.iter_rows(values_only=True)]
    workbook.close()
    rows, numbers = _drop_blank(parsed)
    if not rows:
        raise UnsupportedFileError("La hoja está vacía.")
    table = _split_header(rows, numbers, header_row)
    table.sheet_name = sheet.title
    table.notes.append(f"Excel, hoja '{sheet.title}'")
    return table


def _decode(data: bytes) -> tuple[str, str]:
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8 (con reemplazos)"


def _sniff_delimiter(text: str) -> str:
    head = "\n".join(text.splitlines()[:20])
    counts = {d: head.count(d) for d in _DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _drop_blank(rows: list[list[Any]]) -> tuple[list[list[Any]], list[int]]:
    """Keep the rows that carry something, and remember where each one was."""
    kept: list[list[Any]] = []
    numbers: list[int] = []
    for position, row in enumerate(rows, start=1):
        if any(cell is not None and str(cell).strip() for cell in row):
            kept.append(list(row))
            numbers.append(position)
    return kept, numbers


def _split_header(
    rows: list[list[Any]], numbers: list[int], header_row: int | None
) -> RawTable:
    index = header_row if header_row is not None else _find_header_row(rows)
    index = min(index, len(rows) - 1)
    headers = _dedupe([_clean_header(c, i) for i, c in enumerate(rows[index])])
    return RawTable(
        headers=headers,
        rows=rows[index + 1 :],
        header_row=index,
        row_numbers=numbers[index + 1 :],
    )


def _find_header_row(rows: list[list[Any]]) -> int:
    """Bank exports carry a title block, the account number and a blank line
    before the real header. Pick the row that reads like labels and is followed
    by rows of the same width."""
    widths = [sum(1 for c in r if c is not None and str(c).strip()) for r in rows]
    modal_width = max(set(widths), key=widths.count) if widths else 0
    best_index, best_score = 0, -1.0
    for index, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        filled = [c for c in row if c is not None and str(c).strip()]
        if len(filled) < 2:
            continue
        textish = sum(1 for c in filled if _looks_like_label(c))
        score = textish / len(filled)
        score += 0.5 if len(filled) >= modal_width else 0.0
        # Prefer the earliest row that qualifies, so a data row that happens to
        # be all text does not beat the real header below it.
        score -= index * 0.01
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def _looks_like_label(cell: Any) -> bool:
    if isinstance(cell, (int, float, dt.date, dt.datetime)):
        return False
    text = str(cell).strip()
    if not text:
        return False
    letters = sum(1 for ch in text if ch.isalpha())
    return letters >= max(2, len(text) // 2)


def _clean_header(cell: Any, index: int) -> str:
    text = "" if cell is None else str(cell).strip()
    text = " ".join(text.split())
    return text or f"columna_{index + 1}"


def _dedupe(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for header in headers:
        if header in seen:
            seen[header] += 1
            result.append(f"{header} ({seen[header]})")
        else:
            seen[header] = 1
            result.append(header)
    return result
