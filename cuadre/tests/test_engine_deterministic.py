"""Stages 2, 3 and 5: pure functions, so these tests need no database."""

from __future__ import annotations

import datetime as dt

from conftest import line

from cuadre.config import MatchingSettings
from cuadre.engine.stage2_exact import match_exact
from cuadre.engine.stage3_fuzzy import match_fuzzy
from cuadre.engine.stage5_verify import verify, verify_movement_totals
from cuadre.enums import ExceptionCategory, MatchMethod, Side

D = dt.date


# --------------------------------------------------------------------------- #
# Stage 2
# --------------------------------------------------------------------------- #


def test_exact_matches_same_amount_and_date():
    result = match_exact(
        [line(1, Side.BANK, D(2026, 1, 2), 1500)],
        [line(11, Side.LEDGER, D(2026, 1, 2), 1500)],
    )
    assert len(result.proposals) == 1
    assert result.proposals[0].movement_ids == (1,)
    assert result.proposals[0].entry_ids == (11,)
    assert result.proposals[0].confidence == 1.0
    assert not result.movements and not result.entries


def test_exact_matches_on_reference_across_dates():
    result = match_exact(
        [line(1, Side.BANK, D(2026, 1, 20), -4250, reference="1234")],
        [line(11, Side.LEDGER, D(2026, 1, 2), -4250, reference="1234")],
    )
    assert result.stats["by_reference"] == 1


def test_one_day_slack_applies_once_not_twice():
    """Widening both sides would silently turn ±1 day into ±2."""
    settings = MatchingSettings(exact_date_slack_days=1)
    two_days = match_exact(
        [line(1, Side.BANK, D(2026, 1, 5), 1000)],
        [line(11, Side.LEDGER, D(2026, 1, 7), 1000)],
        settings,
    )
    assert not two_days.proposals
    one_day = match_exact(
        [line(1, Side.BANK, D(2026, 1, 5), 1000)],
        [line(11, Side.LEDGER, D(2026, 1, 6), 1000)],
        settings,
    )
    assert len(one_day.proposals) == 1


def test_amount_never_has_tolerance():
    result = match_exact(
        [line(1, Side.BANK, D(2026, 1, 2), 1500)],
        [line(11, Side.LEDGER, D(2026, 1, 2), 1499)],
    )
    assert not result.proposals


def test_identical_lines_pair_deterministically():
    movements = [line(i, Side.BANK, D(2026, 1, 2), 1000) for i in (3, 1, 2)]
    entries = [line(i, Side.LEDGER, D(2026, 1, 2), 1000) for i in (13, 11, 12)]
    first = match_exact(movements, entries)
    second = match_exact(list(reversed(movements)), list(reversed(entries)))
    assert [p.key() for p in first.proposals] == [p.key() for p in second.proposals]
    assert len(first.proposals) == 3


# --------------------------------------------------------------------------- #
# Stage 3
# --------------------------------------------------------------------------- #


def test_fuzzy_pairs_a_unique_candidate_inside_the_window():
    result = match_fuzzy(
        [line(1, Side.BANK, D(2026, 1, 25), -120000, "PAGO ENERGIA")],
        [line(11, Side.LEDGER, D(2026, 1, 22), -120000, "PAGO ENERGIA ELECTRICA")],
    )
    assert result.stats["by_window"] == 1
    assert result.proposals[0].method is MatchMethod.FUZZY


def test_fuzzy_leaves_ambiguity_alone_when_nothing_distinguishes_the_options():
    """Two candidates, no reference and unrelated wording: the queue is the
    right answer, not a coin flip."""
    result = match_fuzzy(
        [line(1, Side.BANK, D(2026, 1, 10), -5000, "CARGO VARIOS")],
        [
            line(11, Side.LEDGER, D(2026, 1, 9), -5000, "ALQUILER OFICINA"),
            line(12, Side.LEDGER, D(2026, 1, 11), -5000, "COMPRA PAPELERIA"),
        ],
    )
    assert not result.proposals
    assert len(result.entries) == 2


def test_reference_carries_a_match_over_a_long_gap():
    result = match_fuzzy(
        [line(1, Side.BANK, D(2026, 3, 10), -75000, "CHEQUE 002001 PAGADO", "2001")],
        [line(11, Side.LEDGER, D(2026, 1, 5), -75000, "CHEQUE CK-002001", "2001")],
    )
    assert result.stats["by_reference"] == 1


def test_groups_several_entries_into_one_deposit():
    result = match_fuzzy(
        [line(1, Side.BANK, D(2026, 1, 20), 500000, "DEPOSITO CONSOLIDADO")],
        [
            line(11, Side.LEDGER, D(2026, 1, 18), 200000, "COBRO A"),
            line(12, Side.LEDGER, D(2026, 1, 19), 180000, "COBRO B"),
            line(13, Side.LEDGER, D(2026, 1, 19), 120000, "COBRO C"),
        ],
    )
    assert result.stats["grouped"] == 1
    proposal = result.proposals[0]
    assert proposal.cardinality == "1:3"
    assert proposal.entry_ids == (11, 12, 13)


def test_groups_in_the_other_direction_too():
    result = match_fuzzy(
        [
            line(1, Side.BANK, D(2026, 1, 5), -35000, "COMISION"),
            line(2, Side.BANK, D(2026, 1, 5), -5250, "IVA COMISION"),
        ],
        [line(11, Side.LEDGER, D(2026, 1, 5), -40250, "COMISIONES BANCARIAS DEL MES")],
    )
    assert result.proposals[0].cardinality == "2:1"


def test_a_group_never_mixes_inflows_and_outflows():
    result = match_fuzzy(
        [line(1, Side.BANK, D(2026, 1, 10), 10000, "DEPOSITO")],
        [
            line(11, Side.LEDGER, D(2026, 1, 10), 30000, "COBRO"),
            line(12, Side.LEDGER, D(2026, 1, 10), -20000, "DEVOLUCION"),
        ],
    )
    assert not result.proposals


def test_stage3_is_order_independent():
    movements = [line(1, Side.BANK, D(2026, 1, 20), 500000, "DEPOSITO CONSOLIDADO")]
    entries = [
        line(11, Side.LEDGER, D(2026, 1, 18), 200000, "COBRO A"),
        line(12, Side.LEDGER, D(2026, 1, 19), 180000, "COBRO B"),
        line(13, Side.LEDGER, D(2026, 1, 19), 120000, "COBRO C"),
    ]
    a = match_fuzzy(movements, entries)
    b = match_fuzzy(movements, list(reversed(entries)))
    assert [p.key() for p in a.proposals] == [p.key() for p in b.proposals]


# --------------------------------------------------------------------------- #
# Stage 5
# --------------------------------------------------------------------------- #


def _demo_case():
    movements = [
        line(1, Side.BANK, D(2026, 1, 2), 1500000, "DEPOSITO"),
        line(2, Side.BANK, D(2026, 1, 3), -425075, "CHEQUE 1234"),
        line(3, Side.BANK, D(2026, 1, 5), -3500, "COMISION MANEJO"),
    ]
    entries = [
        line(11, Side.LEDGER, D(2026, 1, 2), 1500000, "DEPOSITO VENTAS"),
        line(12, Side.LEDGER, D(2026, 1, 3), -425075, "PAGO ACME"),
        line(13, Side.LEDGER, D(2026, 1, 31), -89000, "CHEQUE NO COBRADO"),
    ]
    return movements, entries


def test_verification_ties_when_the_statement_is_right():
    movements, entries = _demo_case()
    matches = match_exact(movements, entries).proposals
    opening = 10_000_000
    result = verify(
        movements=movements, entries=entries, matches=matches,
        bank_closing_cents=opening + sum(m.amount_cents for m in movements),
        ledger_closing_cents=opening + sum(e.amount_cents for e in entries),
    )
    assert result.balanced
    assert result.difference_cents == 0
    assert result.outstanding_checks_cents == -89000
    assert result.bank_charges_unrecorded_cents == -3500
    assert result.deposits_in_transit_cents == 0
    assert result.adjusted_bank_cents == result.adjusted_ledger_cents
    categories = {item.category for item in result.items}
    assert categories == {
        ExceptionCategory.OUTSTANDING_CHECK,
        ExceptionCategory.BANK_CHARGE_UNRECORDED,
    }


def test_verification_reports_a_wrong_closing_balance():
    movements, entries = _demo_case()
    matches = match_exact(movements, entries).proposals
    opening = 10_000_000
    result = verify(
        movements=movements, entries=entries, matches=matches,
        bank_closing_cents=opening + sum(m.amount_cents for m in movements) + 100,
        ledger_closing_cents=opening + sum(e.amount_cents for e in entries),
    )
    assert not result.balanced
    assert result.difference_cents == 100


def test_verification_catches_a_line_used_in_two_matches():
    from cuadre.engine.values import MatchProposal

    movements, entries = _demo_case()
    duplicated = [
        MatchProposal((1,), (11,), MatchMethod.MANUAL, 6, 1.0, "", 1500000),
        MatchProposal((1,), (12,), MatchMethod.MANUAL, 6, 1.0, "", 1500000),
    ]
    result = verify(movements=movements, entries=entries, matches=duplicated,
                    bank_closing_cents=0, ledger_closing_cents=0)
    assert not result.balanced
    assert any("más de un emparejamiento" in problem for problem in result.problems)


def test_verification_catches_an_unbalanced_match():
    from cuadre.engine.values import MatchProposal

    movements, entries = _demo_case()
    bad = [MatchProposal((1,), (13,), MatchMethod.MODEL, 4, 0.6, "", 1500000)]
    result = verify(movements=movements, entries=entries, matches=bad,
                    bank_closing_cents=0, ledger_closing_cents=0)
    assert any("descuadra" in problem for problem in result.problems)


def test_movement_totals_flag_a_truncated_export():
    movements, _ = _demo_case()
    problem = verify_movement_totals(movements, 10_000_000, 99_999_999, "Estado de cuenta")
    assert problem and "archivo está completo" in problem
    assert verify_movement_totals(
        movements, 10_000_000, 10_000_000 + sum(m.amount_cents for m in movements), "x"
    ) is None
