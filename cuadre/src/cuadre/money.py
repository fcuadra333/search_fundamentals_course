"""Money handling.

Amounts live in the database as signed integer minor units (cents). Floats are
never used for money: the verification stage compares to the cent and a float
would make the answer depend on the machine.

Sign convention, applied to both sides at normalization time:
    positive = the cash balance goes up   (deposit, bank credit, debit to cash)
    negative = the cash balance goes down (check, bank charge, credit to cash)

With that convention a bank movement and its ledger entry carry the same signed
amount, which is what makes the matching stages symmetric.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Currency exponents for the markets served. Anything not listed uses 2.
_EXPONENTS = {"CLP": 0, "PYG": 0, "COP": 2, "CRC": 2, "NIO": 2, "USD": 2, "GTQ": 2, "PAB": 2}

_CLEAN_RE = re.compile(r"[^\d,.\-+()]")
_TRAILING_SIGN_RE = re.compile(r"\b(CR|DB|DR|C|D)\b\s*$", re.IGNORECASE)


def exponent_for(currency: str) -> int:
    return _EXPONENTS.get((currency or "USD").upper(), 2)


class AmountParseError(ValueError):
    """The cell could not be read as an amount. Never guessed, always reported."""


def parse_amount(raw: object, currency: str = "USD") -> int:
    """Parse a spreadsheet cell into signed minor units.

    Handles both decimal conventions (1.234,56 and 1,234.56), parentheses for
    negatives, leading/trailing signs, currency symbols and the CR/DB suffix
    used by several Latin American banks.
    """
    if raw is None:
        raise AmountParseError("valor vacío")
    if isinstance(raw, bool):
        raise AmountParseError("valor booleano")

    exponent = exponent_for(currency)
    if isinstance(raw, int):
        return raw * 10**exponent
    if isinstance(raw, (float, Decimal)):
        return _quantize(Decimal(str(raw)), exponent)

    text = str(raw).strip()
    if not text:
        raise AmountParseError("valor vacío")

    negative = False
    suffix = _TRAILING_SIGN_RE.search(text)
    if suffix and suffix.group(1).upper() in {"CR", "C"}:
        # CR on a bank statement column means a credit to the account holder;
        # the caller decides polarity per column, so only DB/DR flips here.
        pass
    if suffix and suffix.group(1).upper() in {"DB", "DR", "D"}:
        negative = True
    text = _TRAILING_SIGN_RE.sub("", text).strip()

    text = _CLEAN_RE.sub("", text)
    if not text:
        raise AmountParseError("sin dígitos")

    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.startswith("-"):
        negative = not negative
        text = text[1:]
    text = text.lstrip("+").replace("(", "").replace(")", "")

    text = _collapse_separators(text)
    try:
        value = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex
        raise AmountParseError(f"no es un número: {raw!r}") from exc

    cents = _quantize(value, exponent)
    return -cents if negative else cents


def _collapse_separators(text: str) -> str:
    """Resolve which of '.' and ',' is the decimal separator."""
    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        # The rightmost separator is the decimal one.
        decimal_sep = "." if text.rfind(".") > text.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        return text.replace(thousands_sep, "").replace(decimal_sep, ".")
    if has_comma:
        return _single_separator(text, ",")
    if has_dot:
        return _single_separator(text, ".")
    return text


def _single_separator(text: str, sep: str) -> str:
    parts = text.split(sep)
    # Grouped thousands: every group after the first is exactly three digits.
    if len(parts) > 2 and all(len(p) == 3 for p in parts[1:]):
        return "".join(parts)
    if len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3:
        # Ambiguous: 1.234 / 1,234. Treated as thousands, the common case in
        # bank exports, and flagged upstream when the profile is confirmed.
        return "".join(parts)
    return text.replace(sep, ".")


def _quantize(value: Decimal, exponent: int) -> int:
    scaled = value * (10**exponent)
    rounded = scaled.quantize(Decimal(1))
    if abs(scaled - rounded) > Decimal("0.5"):  # pragma: no cover - defensive
        raise AmountParseError(f"precisión perdida en {value}")
    return int(rounded)


def format_amount(cents: int, currency: str = "USD", *, with_symbol: bool = False) -> str:
    """Render minor units for the Spanish-language interface: 1,234.56."""
    exponent = exponent_for(currency)
    negative = cents < 0
    digits = str(abs(cents)).rjust(exponent + 1, "0")
    whole, frac = (digits[:-exponent], digits[-exponent:]) if exponent else (digits, "")
    grouped = f"{int(whole):,}"
    text = f"{grouped}.{frac}" if frac else grouped
    if negative:
        text = f"-{text}"
    return f"{currency} {text}" if with_symbol else text


def to_decimal(cents: int, currency: str = "USD") -> Decimal:
    """Major units, for spreadsheet cells that must stay numeric."""
    return Decimal(cents) / (10 ** exponent_for(currency))
