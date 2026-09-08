"""Minimal PDF writer built on the standard library.

Same reason as the XLSX writer: evidence has to leave the platform in the
formats a company already uses, and pulling a rendering library in for a
table of runs is not worth the dependency.

Scope is deliberately narrow: A4 pages, the base-14 Helvetica family, headings,
paragraphs and simple tables. Text is encoded as WinAnsi, which covers Spanish
accents and the characters this report needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN = 48
LINE_HEIGHT = 14


@dataclass
class _Page:
    commands: list[str] = field(default_factory=list)
    cursor: int = PAGE_HEIGHT - MARGIN


class Document:
    """Builds a PDF one text block at a time."""

    def __init__(self, title: str = ""):
        self.title = title
        self.pages: list[_Page] = [_Page()]

    # -- content ------------------------------------------------------------

    @property
    def page(self) -> _Page:
        return self.pages[-1]

    def new_page(self) -> None:
        self.pages.append(_Page())

    def _ensure(self, height: int) -> None:
        if self.page.cursor - height < MARGIN:
            self.new_page()

    def heading(self, text: str, *, size: int = 16) -> None:
        self._ensure(size + 10)
        self.page.cursor -= size + 6
        self._text(text, size=size, font="F2", x=MARGIN)
        self.page.cursor -= 4

    def paragraph(self, text: str, *, size: int = 10, indent: int = 0) -> None:
        for line in _wrap(text, width=int((PAGE_WIDTH - 2 * MARGIN - indent) / (size * 0.5))):
            self._ensure(LINE_HEIGHT)
            self.page.cursor -= LINE_HEIGHT
            self._text(line, size=size, font="F1", x=MARGIN + indent)

    def key_value(self, label: str, value: str, *, size: int = 10) -> None:
        self._ensure(LINE_HEIGHT)
        self.page.cursor -= LINE_HEIGHT
        self._text(label, size=size, font="F2", x=MARGIN)
        self._text(str(value), size=size, font="F1", x=MARGIN + 170)

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[object]],
              *, widths: Sequence[int] | None = None, size: int = 9) -> None:
        columns = len(headers)
        available = PAGE_WIDTH - 2 * MARGIN
        widths = list(widths or [available // columns] * columns)

        def draw(values, font):
            self._ensure(LINE_HEIGHT)
            self.page.cursor -= LINE_HEIGHT
            x = MARGIN
            for index, value in enumerate(values):
                text = _clip(str(value), widths[index], size)
                self._text(text, size=size, font=font, x=x)
                x += widths[index]

        draw(headers, "F2")
        self._rule()
        for row in rows:
            draw(row, "F1")
        self.page.cursor -= 6

    def spacer(self, height: int = 10) -> None:
        self._ensure(height)
        self.page.cursor -= height

    def _rule(self) -> None:
        y = self.page.cursor - 3
        self.page.commands.append(
            f"0.7 w 0.55 0.6 0.65 RG {MARGIN} {y} m {PAGE_WIDTH - MARGIN} {y} l S"
        )

    def _text(self, text: str, *, size: int, font: str, x: int) -> None:
        self.page.commands.append(
            f"BT /{font} {size} Tf 0.1 0.12 0.15 rg {x} {self.page.cursor} Td "
            f"({_escape(text)}) Tj ET"
        )

    # -- output -------------------------------------------------------------

    def save(self, path: str) -> str:
        with open(path, "wb") as handle:
            handle.write(self.render())
        return path

    def render(self) -> bytes:
        objects: list[bytes] = []

        def add(payload: bytes) -> int:
            objects.append(payload)
            return len(objects)

        font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                           b"/Encoding /WinAnsiEncoding >>")
        font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                        b"/Encoding /WinAnsiEncoding >>")

        page_ids: list[int] = []
        content_ids: list[int] = []
        for page in self.pages:
            stream = "\n".join(page.commands).encode("latin-1", "replace")
            content_ids.append(
                add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                    + stream + b"\nendstream")
            )
            page_ids.append(0)  # placeholder, filled after the pages node exists

        pages_id = len(objects) + len(self.pages) + 1
        for index, content_id in enumerate(content_ids):
            page_ids[index] = add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
                f"/Contents {content_id} 0 R >>".encode("latin-1")
            )

        kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
        add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1"))
        catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1"))
        info_id = add(
            f"<< /Title ({_escape(self.title)}) /Producer (automation-platform) >>".encode(
                "latin-1", "replace"
            )
        )

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, payload in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode("latin-1") + payload + b"\nendobj\n"

        xref_offset = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for offset in offsets[1:]:
            out += f"{offset:010d} 00000 n \n".encode("latin-1")
        out += (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R "
            f"/Info {info_id} 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
        ).encode("latin-1")
        return bytes(out)


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap(text: str, width: int) -> list[str]:
    width = max(width, 20)
    words = text.split()
    if not words:
        return [""]
    lines, current = [], words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _clip(text: str, width_points: int, size: int) -> str:
    limit = max(int(width_points / (size * 0.52)) - 1, 4)
    return text if len(text) <= limit else text[: limit - 1] + "…"
