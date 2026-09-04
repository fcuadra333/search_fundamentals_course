"""Date, description and reference normalization."""

from __future__ import annotations

import datetime as dt

import pytest

from cuadre.ingest.normalize import (
    DateParseError,
    extract_reference,
    infer_date_format,
    normalize_description,
    parse_date,
    reference_matches,
    similarity,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("31/12/2025", dt.date(2025, 12, 31)),
        ("2025-12-31", dt.date(2025, 12, 31)),
        ("31-12-2025", dt.date(2025, 12, 31)),
        ("31.12.2025", dt.date(2025, 12, 31)),
        ("20251231", dt.date(2025, 12, 31)),
        ("15-ene-2026", dt.date(2026, 1, 15)),
        ("15/DIC/2025", dt.date(2025, 12, 15)),
        ("31/12/2025 00:00:00", dt.date(2025, 12, 31)),
        (dt.datetime(2025, 12, 31, 9, 30), dt.date(2025, 12, 31)),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_parse_date_refuses_garbage():
    with pytest.raises(DateParseError):
        parse_date("no es fecha")


def test_explicit_format_wins():
    # 01/02 is ambiguous; the confirmed profile decides, not a guess.
    assert parse_date("01/02/2026", "%m/%d/%Y") == dt.date(2026, 1, 2)
    assert parse_date("01/02/2026", "%d/%m/%Y") == dt.date(2026, 2, 1)


def test_infer_date_format_prefers_day_first_and_uses_the_disambiguating_sample():
    assert infer_date_format(["31/12/2025", "01/02/2026"]) == "%d/%m/%Y"
    assert infer_date_format(["12/31/2025", "02/01/2026"]) == "%m/%d/%Y"


def test_infer_date_format_returns_none_for_typed_cells():
    assert infer_date_format([dt.date(2026, 1, 1)]) is None


def test_normalize_description_strips_accents_punctuation_and_filler():
    assert normalize_description("Pago  de  Proveedor  ACME, S.A.  Ref: 8891") == (
        "PAGO PROVEEDOR ACME S 8891"
    )
    assert normalize_description(None) == ""


@pytest.mark.parametrize(
    "text,expected",
    [
        ("PAGO CHEQUE No. 001234 ACME", "1234"),
        ("CHQ 5678", "5678"),
        ("TRF ACH 99881122 NOMINA", "99881122"),
        ("DEPOSITO 4455667788", "4455667788"),
        ("DEPOSITO EN EFECTIVO", None),
        # The keyword must not be allowed to swallow itself: this used to come
        # back as "ERENCIA", which then matched nothing on the other side.
        ("TRANSFERENCIA ACH 4471 PROV DISTR UNION", "4471"),
        ("TRANSFERENCIA SINPE 998877 NOMINA", "998877"),
        ("PAGO ACH PROVEEDOR SIN NUMERO", None),
        ("REF: FAC-2026-0091", "FAC-2026-0091"),
    ],
)
def test_extract_reference(text, expected):
    assert extract_reference(text) == expected


def test_explicit_reference_column_wins_and_loses_padding():
    assert extract_reference("CHEQUE 999", explicit="0001234") == "1234"


def test_similarity_is_symmetric_and_bounded():
    a, b = normalize_description("PAGO PROVEEDOR ACME"), normalize_description("ACME PAGO PROVEEDOR")
    assert similarity(a, b) == similarity(b, a)
    assert 0.0 <= similarity(a, b) <= 1.0
    assert similarity(a, a) == 1.0
    assert similarity(a, "") == 0.0


def test_reference_matching_requires_containment_and_length():
    assert reference_matches("1234", "CK-1234")
    assert reference_matches("CK-1234", "1234")
    assert not reference_matches("1234", "98765")
    # Too short to mean anything on its own.
    assert not reference_matches("12", "123456")
