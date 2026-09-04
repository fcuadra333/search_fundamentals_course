"""The override path: manual matches, corrections, rules and approval."""

from __future__ import annotations

import pytest

from cuadre.engine.runner import refresh_totals, run_reconciliation
from cuadre.enums import ExceptionCategory, ExceptionStatus, MatchMethod, MatchStatus, Role
from cuadre.models import Correction, Exception_, Match, Rule
from cuadre.review import (
    ReviewError,
    approval_blockers,
    approve_match,
    approve_reconciliation,
    close_period,
    defer_exception,
    manual_match,
    mark_reconciling_item,
    reject_match,
    reopen_period,
)
from cuadre.tenancy import RoleError


def test_a_manual_match_needs_both_sides_to_add_up(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    movements = [
        row for row in session.query(Exception_).filter_by(side="BANK").all() if row.movement_id
    ]
    entries = [
        row for row in session.query(Exception_).filter_by(side="LEDGER").all() if row.entry_id
    ]
    with pytest.raises(ReviewError) as caught:
        manual_match(session, tenant, loaded,
                     movement_ids=[movements[0].movement_id],
                     entry_ids=[entries[0].entry_id], reason="prueba")
    assert "no suman igual" in str(caught.value)


def test_a_manual_match_records_a_correction_and_a_candidate_rule(session, tenant, loaded):
    from cuadre.models import BankMovement, LedgerEntry

    run_reconciliation(session, tenant, loaded)
    # Pair the two bank charges against a ledger entry created for the purpose,
    # so the amounts tie exactly.
    charges = (
        session.query(BankMovement)
        .filter(BankMovement.match_id.is_(None), BankMovement.amount_cents < 0)
        .all()
    )
    total = sum(charge.amount_cents for charge in charges)
    entry = LedgerEntry(
        firm_id=loaded.firm_id, reconciliation_id=loaded.id, value_date=charges[0].value_date,
        amount_cents=total, currency="NIO", raw_description="COMISIONES BANCARIAS DEL MES",
        normalized_description="COMISIONES BANCARIAS MES",
    )
    session.add(entry)
    session.flush()

    match = manual_match(
        session, tenant, loaded,
        movement_ids=[charge.id for charge in charges], entry_ids=[entry.id],
        reason="El cliente registró las comisiones en un solo asiento.",
    )
    assert match.method is MatchMethod.MANUAL
    assert match.status is MatchStatus.APPROVED
    assert len(match.movements) == len(charges)

    correction = session.query(Correction).filter_by(match_id=match.id).one()
    assert correction.action == "match.manual"
    assert correction.reason
    # The exceptions those lines had are closed by the pairing.
    closed = session.query(Exception_).filter(
        Exception_.movement_id.in_([charge.id for charge in charges])
    ).all()
    assert all(row.status == ExceptionStatus.RESOLVED for row in closed)


def test_rejecting_a_match_demands_a_reason_and_frees_the_lines(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    match = session.query(Match).filter_by(status=MatchStatus.APPROVED).first()
    movement_ids = [line.id for line in match.movements]

    with pytest.raises(ReviewError):
        reject_match(session, tenant, match, "")

    reject_match(session, tenant, match, "El cheque corresponde a otro proveedor.")
    assert match.status is MatchStatus.REJECTED
    from cuadre.models import BankMovement

    for movement_id in movement_ids:
        assert session.get(BankMovement, movement_id).match_id is None
    assert session.query(Correction).filter_by(match_id=match.id).count() == 1


def test_a_derived_rule_is_recorded_but_not_applied(session, tenant, loaded):
    from cuadre.models import BankMovement, LedgerEntry

    run_reconciliation(session, tenant, loaded)
    movement = BankMovement(
        firm_id=loaded.firm_id, reconciliation_id=loaded.id,
        value_date=loaded.period.start_date, amount_cents=-777, currency="NIO",
        raw_description="PAGO SEGURO VEHICULAR", normalized_description="PAGO SEGURO VEHICULAR")
    entry = LedgerEntry(
        firm_id=loaded.firm_id, reconciliation_id=loaded.id,
        value_date=loaded.period.start_date, amount_cents=-777, currency="NIO",
        raw_description="Pago seguro vehicular", normalized_description="PAGO SEGURO VEHICULAR")
    session.add_all([movement, entry])
    session.flush()
    manual_match(session, tenant, loaded, movement_ids=[movement.id], entry_ids=[entry.id],
                 reason="Siempre se llaman igual.")
    rule = session.query(Rule).one()
    assert rule.auto_apply is False
    assert "SEGURO" in rule.name.upper()
    assert rule.derived_from_correction_id is not None


def test_deferring_keeps_the_item_alive_and_needs_a_reason(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    exception = session.query(Exception_).filter_by(status=ExceptionStatus.OPEN).first()
    with pytest.raises(ReviewError):
        defer_exception(session, tenant, exception, "  ")
    defer_exception(session, tenant, exception, "Esperando el soporte del cliente.")
    assert exception.status is ExceptionStatus.DEFERRED
    assert ExceptionStatus(exception.status).is_open


def test_approval_is_blocked_until_everything_is_settled(
    session, tenant, reviewer_tenant, loaded
):
    run_reconciliation(session, tenant, loaded)
    blockers = approval_blockers(session, loaded, reviewer_tenant)
    assert any("propuesta" in blocker for blocker in blockers)
    assert any("excepci" in blocker for blocker in blockers)

    for match in session.query(Match).filter_by(status=MatchStatus.PROPOSED).all():
        approve_match(session, reviewer_tenant, match)
    for exception in session.query(Exception_).filter_by(status=ExceptionStatus.OPEN).all():
        mark_reconciling_item(session, reviewer_tenant, exception,
                              category=ExceptionCategory(exception.category),
                              reason="Verificada contra el soporte.")
    refresh_totals(session, loaded)

    assert approval_blockers(session, loaded, reviewer_tenant) == []
    approve_reconciliation(session, reviewer_tenant, loaded, notes="Revisada.")
    assert loaded.reviewed_by_id == reviewer_tenant.user_id
    assert loaded.approved_at is not None


def test_the_preparer_cannot_approve_their_own_work(session, tenant, loaded):
    run_reconciliation(session, tenant, loaded)
    with pytest.raises(RoleError):
        approve_reconciliation(session, tenant, loaded)


def test_a_period_only_closes_once_everything_is_approved(
    session, tenant, reviewer_tenant, users, loaded
):
    from cuadre.enums import PeriodStatus
    from cuadre.tenancy import Tenant

    run_reconciliation(session, tenant, loaded)
    period = loaded.period
    with pytest.raises(ReviewError):
        close_period(session, reviewer_tenant, period)

    for match in session.query(Match).filter_by(status=MatchStatus.PROPOSED).all():
        approve_match(session, reviewer_tenant, match)
    for exception in session.query(Exception_).filter_by(status=ExceptionStatus.OPEN).all():
        mark_reconciling_item(session, reviewer_tenant, exception,
                              category=ExceptionCategory(exception.category))
    refresh_totals(session, loaded)
    approve_reconciliation(session, reviewer_tenant, loaded)
    close_period(session, reviewer_tenant, period)
    assert period.status is PeriodStatus.CLOSED

    admin = Tenant(firm_id=period.firm_id, user_id=users[Role.ADMIN].id, role=Role.ADMIN)
    with pytest.raises(ReviewError):
        reopen_period(session, admin, period, "")
    reopen_period(session, admin, period, "El banco envió un estado corregido.")
    assert period.status is PeriodStatus.IN_REVIEW
    with pytest.raises(RoleError):
        reopen_period(session, reviewer_tenant, period, "sin permiso")
