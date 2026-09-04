"""Normalization of the raw cells of a statement or ledger export.

This is where the product either works or loses the user: if somebody uploads a
file and the system does not understand it, they leave and do not come back
(ESPECIFICACION 3, stage 1). Everything here is deterministic and pure so the
same file always produces the same lines.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

# Ordered by how often they appear in Latin American bank exports.
_DATE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("%d/%m/%Y", "dmy"),
    ("%d-%m-%Y", "dmy"),
    ("%d.%m.%Y", "dmy"),
    ("%Y-%m-%d", "ymd"),
    ("%Y/%m/%d", "ymd"),
    ("%m/%d/%Y", "mdy"),
    ("%m-%d-%Y", "mdy"),
    ("%d/%m/%y", "dmy"),
    ("%m/%d/%y", "mdy"),
    ("%Y%m%d", "ymd"),
    ("%d%m%Y", "dmy"),
)

_MONTHS_ES = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "SET": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}
_TEXT_DATE_RE = re.compile(r"^(\d{1,2})[-/ ]([A-Za-zÁÉÍÓÚáéíóú]{3,10})[-/ ](\d{2,4})$")

_WS_RE = re.compile(r"\s+")
_NOISE_RE = re.compile(r"[^A-Z0-9 ]")
# Filler that appears on every line of a statement and carries no signal.
_STOPWORDS = frozenset(
    """DE DEL LA EL LOS LAS Y A EN POR PARA CON SU SUS UN UNA AL SE NO
    TRANSACCION TRANSACCIÓN OPERACION OPERACIÓN MOVIMIENTO REF REFERENCIA
    NRO NUM NUMERO NO DOC DOCUMENTO COMPROBANTE""".split()
)

# The keyword is anchored with \b on both sides and the longest spelling comes
# first, so "TRANSFERENCIA ACH 4471" cannot match "TRANSF" inside
# "TRANSFERENCIA" and capture the leftover "ERENCIA".
_REFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:CHEQUE|CHQ|CH|CK)\b[\s.:#-]*0*(\d{3,10})\b", re.IGNORECASE),
    re.compile(
        r"\b(?:REFERENCIA|REF|COMPROBANTE|COMP|DOCUMENTO|DOC|FACTURA|FACT|FAC)\b"
        r"[\s.:#-]*([A-Z0-9][A-Z0-9-]{2,19})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:TRANSFERENCIA|TRANSF|TRF|ACH|SPEI|SINPE)\b[\s.:#-]*([A-Z0-9][A-Z0-9-]{3,19})\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(\d{6,16})\b"),
)


class DateParseError(ValueError):
    """The cell could not be read as a date. Never guessed."""


def parse_date(value: object, fmt: str | None = None) -> dt.date:
    """Parse one cell into a date. `fmt` comes from the confirmed format profile
    and is tried first; without it the patterns are tried in order."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if value is None:
        raise DateParseError("fecha vacía")

    text = str(value).strip()
    if not text:
        raise DateParseError("fecha vacía")
    # Drop a time component: '31/12/2025 00:00:00'
    text = text.split(" ")[0] if " " in text and ":" in text else text

    if fmt:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    match = _TEXT_DATE_RE.match(text)
    if match:
        day, month_name, year = match.groups()
        month = _MONTHS_ES.get(_strip_accents(month_name).upper()[:3])
        if month:
            year_i = int(year)
            year_i += 2000 if year_i < 100 else 0
            return dt.date(year_i, month, int(day))

    for pattern, _ in _DATE_PATTERNS:
        try:
            return dt.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise DateParseError(f"no es una fecha reconocible: {value!r}")


def infer_date_format(samples: list[object]) -> str | None:
    """Pick the format that parses the most samples without ambiguity.

    31/12 versus 12/31 is decided by the sample that only one order can read;
    when no sample disambiguates, day-first wins because it is the convention of
    every market served.
    """
    scores: Counter[str] = Counter()
    for raw in samples:
        if isinstance(raw, (dt.date, dt.datetime)):
            return None  # Already typed by the spreadsheet reader.
        text = str(raw).strip().split(" ")[0]
        if not text:
            continue
        for pattern, _ in _DATE_PATTERNS:
            try:
                dt.datetime.strptime(text, pattern)
            except ValueError:
                continue
            scores[pattern] += 1
    if not scores:
        return None
    best = max(scores.values())
    winners = [p for p, _ in _DATE_PATTERNS if scores.get(p) == best]
    return winners[0]


def _strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn"
    )


def normalize_description(raw: object) -> str:
    """Upper case, accent-free, punctuation-free, filler removed.

    Used for similarity and for substring reference matching, never displayed:
    the interface always shows the raw description so the accountant recognises
    their own statement.
    """
    if raw is None:
        return ""
    text = _strip_accents(str(raw)).upper()
    text = _NOISE_RE.sub(" ", text)
    tokens = [t for t in _WS_RE.split(text) if t and t not in _STOPWORDS]
    return " ".join(tokens)


def extract_reference(raw: object, explicit: object = None) -> str | None:
    """Pull the document reference out of a description.

    A dedicated reference column wins when the profile maps one; otherwise the
    patterns run in order of how much they mean, ending with a bare long digit
    run, which is what most cores print for a check number.
    """
    if explicit not in (None, ""):
        cleaned = re.sub(r"[^A-Za-z0-9-]", "", str(explicit)).upper().lstrip("0")
        if cleaned:
            return cleaned[:80]
    if raw is None:
        return None
    text = str(raw)
    for pattern in _REFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1).upper().lstrip("0")
            # A reference carries digits. Without this the keyword patterns
            # happily capture the next word ("ACH", "DEL", a payee's name) and
            # the matching stages then compare two pieces of prose.
            if value and any(character.isdigit() for character in value):
                return value[:80]
    return None


def similarity(left: str, right: str) -> float:
    """Description similarity in [0, 1].

    Token overlap and character ratio are averaged: the first catches reordered
    words ('PAGO PROVEEDOR X' / 'X PAGO PROVEEDOR'), the second catches typos
    and truncation, which is what bank exports actually do to a name.
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens, right_tokens = set(left.split()), set(right.split())
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    ratio = SequenceMatcher(None, left, right).ratio()
    return round((jaccard + ratio) / 2, 4)


def reference_matches(left: str | None, right: str | None, min_length: int = 4) -> bool:
    """True when one reference contains the other, both long enough to mean it.

    Banks truncate and cores pad; 'CHQ 0001234' on one side and '1234567' on the
    other are not the same document, so containment is required in full.
    """
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) < min_length or len(right) < min_length:
        return False
    return left in right or right in left
