"""Amount parsing and formatting: the layer everything else trusts."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cuadre.money import AmountParseError, format_amount, parse_amount, to_decimal


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.234,56", 123456),          # European convention
        ("1,234.56", 123456),          # US convention
        ("1234.56", 123456),
        ("(500.00)", -50000),          # accounting parentheses
        ("-1.500,00", -150000),
        ("+250.00", 25000),
        ("12345", 1234500),            # integers are whole units
        ("C$ 8,900.10", 890010),       # currency symbol
        ("250.00 DB", -25000),         # trailing debit marker
        ("250.00 CR", 25000),
        ("0,05", 5),
        ("1.234.567,89", 123456789),
        (1500, 150000),
        (Decimal("12.34"), 1234),
        (12.34, 1234),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "sin monto", True])
def test_parse_amount_refuses_rather_than_guesses(raw):
    with pytest.raises(AmountParseError):
        parse_amount(raw)


def test_zero_decimal_currency():
    assert parse_amount("1500", "CLP") == 1500
    assert format_amount(1500, "CLP") == "1,500"


def test_format_round_trip():
    for cents in (0, 5, -5, 123456, -123456, 100000000):
        assert parse_amount(format_amount(cents)) == cents


def test_to_decimal_is_exact():
    assert to_decimal(123456) == Decimal("1234.56")
