"""The five stages end to end, on the database."""

from __future__ import annotations

import datetime as dt

import demo_data
from conftest import make_period, make_reconciliation

from cuadre.engine.runner import current_verification, run_reconciliation
from cuadre.enums import (
    ExceptionCategory,
    ExceptionStatus,
    LineState,
    MatchMethod,
    MatchStatus,
    ReconciliationStatus,
    RunStatus,
    UploadKind,
)
from cuadre.ingest import service as ingest
from cuadre.models import BankMovement, Exception_, LedgerEntry, Match, ModelCall, Run


def test_a_full_month_ties_to_zero(session, tenant, loaded):
    outcome = run_reconciliation(session, tenant, loaded)
    expected = demo_data.expected()
    verification = outcome.verification

    assert verification.balanced
    assert verification.difference_cents == expected["difference_cents"]
    assert verification.bank_closing_cents == expected["bank_closing_cents"]
    assert verification.ledger_closing_cents == expected["ledger_closing_cents"]
    assert verification.deposits_in_transit_cents == expected["deposits_in_transit_cents"]
    assert verification.outstanding_checks_cents == expected["outstanding_checks_cents"]
    assert verification.bank_charges_unrecorded_cents == expected["bank_charges_unrecorded_cents"]
    assert verification.bank_credits_unrecorded_cents == expected["bank_credits_unrecorded_cents"]
    assert len(verification.items) == expected["reconciling_items"]
    assert not verification.problems
    assert outcome.run.status is RunStatus.COMPLETED
    assert loaded.status is ReconciliationStatus.IN_REVIEW


def test_the_deterministic_stages_carry_most_of_the_load(session, tenant, loaded):
    outcome = run_reconciliation(session, tenant, loaded)
    stats = outcome.stats
    deterministic = sum(stats["etapa2"].values()) + sum(stats["etapa3"].values())
    assert deterministic >= 5
    assert stats["etapa4"]["emparejados"] <= 2
    # Everything the deterministic stages found is approved by the system;
    # what the model proposed waits for a person.
    for match in session.query(Match).all():
        if MatchMethod(match.method) in (MatchMethod.EXACT, MatchMethod.FUZZY):
            assert match.status is MatchStatus.APPROVED
            assert match.approved_by_id is None  # the system, not a person
        else:
            assert match.status is MatchStatus.PROPOSED


def test_grouping_and_the_model_each_do_their_part(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    grouped = [m for m in session.query(Match).all() if len(m.entries) > 1]
    assert grouped and len(grouped[0].entries) == 3
    model = session.query(Match).filter_by(method=MatchMethod.MODEL).all()
    assert len(model) == 1
    assert model[0].rationale


def test_every_line_ends_matched_or_in_the_queue(session, tenant, loaded):
    """Invariant 4: approved matches plus open exceptions account for the whole
    period, with nothing counted twice and nothing missing."""
    run_reconciliation(session, tenant, loaded)
    for model, side in ((BankMovement, "BANK"), (LedgerEntry, "LEDGER")):
        for row in session.query(model).filter_by(reconciliation_id=loaded.id).all():
            in_match = row.match_id is not None
            exception = (
                session.query(Exception_)
                .filter_by(reconciliation_id=loaded.id, side=side)
                .filter(
                    (Exception_.movement_id == row.id)
                    if side == "BANK"
                    else (Exception_.entry_id == row.id)
                )
                .one_or_none()
            )
            assert in_match != (exception is not None and ExceptionStatus(exception.status).is_open)
            assert row.state in (LineState.MATCHED, LineState.EXCEPTION)


def test_running_twice_gives_the_same_answer_and_no_duplicates(session, tenant, loaded):
    first = run_reconciliation(session, tenant, loaded)
    count_after_first = session.query(Match).count()
    second = run_reconciliation(session, tenant, loaded)

    assert second.verification.difference_cents == first.verification.difference_cents
    assert second.verification.match_rate == first.verification.match_rate
    assert session.query(Match).count() == count_after_first
    assert session.query(Run).count() == 2


def test_a_human_decision_survives_a_re_run(session, tenant, loaded):
    from cuadre.review import reject_match

    run_reconciliation(session, tenant, loaded)
    approved = session.query(Match).filter_by(status=MatchStatus.APPROVED).first()
    reject_match(session, tenant, approved, "El contador dice que no corresponde.")
    run_reconciliation(session, tenant, loaded)
    session.refresh(approved)
    assert approved.status is MatchStatus.REJECTED


def test_the_model_calls_are_recorded_with_their_cost(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    calls = session.query(ModelCall).all()
    assert calls
    for call in calls:
        assert call.provider and call.model
        assert call.request_hash
        assert call.response is not None  # persisted, never re-derived
        assert call.cost_micros >= 0


def test_a_truncated_statement_is_reported_not_swallowed(session, tenant, loaded):
    loaded.closing_bank_cents += 100_00
    session.flush()
    outcome = run_reconciliation(session, tenant, loaded)
    assert outcome.warnings
    assert any("archivo está completo" in warning for warning in outcome.warnings)
    assert not outcome.verification.balanced


def test_classification_puts_each_item_on_the_right_side(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    by_category = {}
    for exception in session.query(Exception_).all():
        by_category.setdefault(ExceptionCategory(exception.category), []).append(exception)
    assert len(by_category[ExceptionCategory.OUTSTANDING_CHECK]) == 1
    assert len(by_category[ExceptionCategory.DEPOSIT_IN_TRANSIT]) == 1
    assert len(by_category[ExceptionCategory.BANK_CHARGE_UNRECORDED]) == 2
    assert len(by_category[ExceptionCategory.BANK_CREDIT_UNRECORDED]) == 1


def test_an_item_carries_forward_and_clears_in_the_next_period(
    session, tenant, firm, entity, accounts, loaded
):
    """A check written in January and cashed in February is matched across the
    period boundary, and January's exception closes when it happens."""
    run_reconciliation(session, tenant, loaded)
    check = (
        session.query(Exception_)
        .filter_by(category=ExceptionCategory.OUTSTANDING_CHECK)
        .one()
    )
    assert check.status is ExceptionStatus.OPEN
    check_amount = check.amount_cents

    february = make_period(session, firm, entity, 2026, 2)
    next_month = make_reconciliation(
        session, firm, february, accounts,
        opening_bank_cents=loaded.closing_bank_cents,
        opening_ledger_cents=loaded.closing_ledger_cents,
    )

    statement = (
        "Fecha;Descripcion;No. Documento;Debitos;Creditos;Saldo\n"
        f"03/02/2026;CHEQUE PAGADO No. 001236;001236;{abs(check_amount) / 100:,.2f};;0.00\n"
    ).encode()
    ledger = "Fecha,Comprobante,Cuenta,Descripcion,Monto\n".encode()

    for kind, name, data in (
        (UploadKind.BANK_STATEMENT, "feb-banco.csv", statement),
        (UploadKind.LEDGER, "feb-mayor.csv", ledger),
    ):
        upload, _ = ingest.stage_upload(session, tenant, next_month, kind=kind,
                                        filename=name, data=data)
        if kind is UploadKind.BANK_STATEMENT:
            table, proposal = ingest.propose(session, upload, currency="NIO")
            ingest.import_upload(session, tenant, upload, mapping=proposal.mapping,
                                 header_row=table.header_row, date_format=proposal.date_format,
                                 sign_convention=proposal.sign_convention)

    next_month.closing_bank_cents = next_month.opening_bank_cents + check_amount
    next_month.closing_ledger_cents = next_month.opening_ledger_cents
    session.flush()

    outcome = run_reconciliation(session, tenant, next_month)
    session.refresh(check)
    assert check.status is ExceptionStatus.RESOLVED
    assert outcome.verification.balanced
    assert outcome.verification.outstanding_checks_cents == 0


def test_the_statement_on_screen_always_matches_the_database(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    live = current_verification(session, loaded)
    assert live.difference_cents == loaded.difference_cents
    assert live.balanced == loaded.balanced
