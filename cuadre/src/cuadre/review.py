"""The exception queue and the approval path.

The correction and override path is a first-class function, not a secondary
form: it is the documented complaint about the nearest competitor (CLAUDE.md).
Every override writes a `corrections` row and, where it teaches something about
the client, a `rules` row that phase 2 will learn from. Nothing is discarded.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .enums import (
    ExceptionCategory,
    ExceptionStatus,
    LineState,
    MatchMethod,
    MatchStatus,
    PeriodStatus,
    ReconciliationStatus,
    Role,
    Side,
)
from .models import (
    Account,
    BankMovement,
    Correction,
    Entity,
    Exception_,
    LedgerEntry,
    Match,
    MatchEntry,
    MatchMovement,
    Period,
    Reconciliation,
    Rule,
    User,
)
from .tenancy import (
    RoleError,
    Tenant,
    audit,
    require_approver,
    require_writable,
    require_writable_period,
    segregation_ok,
)


class ReviewError(ValueError):
    """The action is not valid on this object in its current state."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _client_id(session: Session, reconciliation: Reconciliation) -> int | None:
    account = session.get(Account, reconciliation.bank_account_id)
    entity = session.get(Entity, account.entity_id) if account else None
    return entity.client_id if entity else None


def _record_correction(
    session: Session,
    tenant: Tenant,
    reconciliation: Reconciliation,
    *,
    action: str,
    proposed: dict,
    corrected: dict,
    reason: str | None,
    match_id: int | None = None,
    exception_id: int | None = None,
) -> Correction:
    correction = Correction(
        firm_id=tenant.firm_id,
        client_id=_client_id(session, reconciliation),
        reconciliation_id=reconciliation.id,
        match_id=match_id,
        exception_id=exception_id,
        action=action,
        proposed=proposed,
        corrected=corrected,
        reason=reason,
        user_id=tenant.user_id,
    )
    session.add(correction)
    session.flush()
    return correction


# --------------------------------------------------------------------------- #
# Matches
# --------------------------------------------------------------------------- #


def approve_match(session: Session, tenant: Tenant, match: Match, reason: str | None = None) -> Match:
    reconciliation = session.get(Reconciliation, match.reconciliation_id)
    require_writable(session, reconciliation)
    if match.status == MatchStatus.APPROVED:
        return match
    match.status = MatchStatus.APPROVED
    match.approved_by_id = tenant.user_id
    match.approved_at = _now()
    for line in list(match.movements) + list(match.entries):
        line.state = LineState.MATCHED
        line.match_id = match.id
    _close_exceptions_for(session, match)
    audit(session, tenant, "match.approved", object_type="Match", object_id=match.id,
          detail={"metodo": match.method, "monto": match.amount_cents, "motivo": reason})
    return match


def reject_match(session: Session, tenant: Tenant, match: Match, reason: str) -> Match:
    """Undo a proposal. The lines go back to the queue and the disagreement is
    stored: this is the training data of the learning loop."""
    reconciliation = session.get(Reconciliation, match.reconciliation_id)
    require_writable(session, reconciliation)
    if not reason or not reason.strip():
        raise ReviewError("Anular un emparejamiento exige un motivo.")

    proposed = _match_payload(match)
    match.status = MatchStatus.REJECTED
    for line in list(match.movements) + list(match.entries):
        line.match_id = None
        line.state = LineState.EXCEPTION
    _record_correction(
        session, tenant, reconciliation,
        action="match.rejected", proposed=proposed, corrected={}, reason=reason,
        match_id=match.id,
    )
    audit(session, tenant, "match.rejected", object_type="Match", object_id=match.id,
          detail={"motivo": reason})
    return match


def manual_match(
    session: Session,
    tenant: Tenant,
    reconciliation: Reconciliation,
    *,
    movement_ids: list[int],
    entry_ids: list[int],
    reason: str | None = None,
    replaces: Match | None = None,
) -> Match:
    """The accountant pairs lines the engine did not. N:M like any other match."""
    require_writable(session, reconciliation)
    if not movement_ids or not entry_ids:
        raise ReviewError("Un emparejamiento manual necesita al menos una partida de cada lado.")

    movements = _fetch_lines(session, BankMovement, movement_ids, reconciliation)
    entries = _fetch_lines(session, LedgerEntry, entry_ids, reconciliation)
    busy = [line for line in movements + entries if line.match_id is not None]
    if busy:
        raise ReviewError("Alguna de las partidas ya está emparejada. Anule primero ese emparejamiento.")

    movement_total = sum(line.amount_cents for line in movements)
    entry_total = sum(line.amount_cents for line in entries)
    if movement_total != entry_total:
        raise ReviewError(
            f"Los dos lados no suman igual: banco {movement_total} contra libros {entry_total}."
        )

    match = Match(
        firm_id=tenant.firm_id,
        reconciliation_id=reconciliation.id,
        method=MatchMethod.MANUAL,
        stage=6,
        confidence=1.0,
        status=MatchStatus.APPROVED,
        amount_cents=movement_total,
        rationale=reason or "Emparejamiento manual del preparador.",
        fingerprint="",
        created_by_id=tenant.user_id,
        approved_by_id=tenant.user_id,
        approved_at=_now(),
    )
    session.add(match)
    session.flush()
    for line in movements:
        session.add(MatchMovement(match_id=match.id, movement_id=line.id))
        line.state, line.match_id = LineState.MATCHED, match.id
    for line in entries:
        session.add(MatchEntry(match_id=match.id, entry_id=line.id))
        line.state, line.match_id = LineState.MATCHED, match.id

    _close_exceptions_for(session, match)
    correction = _record_correction(
        session, tenant, reconciliation,
        action="match.manual",
        proposed=_match_payload(replaces) if replaces else {},
        corrected=_match_payload(match),
        reason=reason,
        match_id=match.id,
    )
    _derive_rule(session, tenant, reconciliation, movements, entries, correction)
    audit(session, tenant, "match.manual", object_type="Match", object_id=match.id,
          detail={"movimientos": movement_ids, "asientos": entry_ids, "motivo": reason})
    return match


def _match_payload(match: Match | None) -> dict:
    if match is None:
        return {}
    return {
        "match_id": match.id,
        "metodo": match.method,
        "etapa": match.stage,
        "confianza": match.confidence,
        "movimientos": [line.id for line in match.movements],
        "asientos": [line.id for line in match.entries],
        "justificacion": match.rationale,
    }


def _fetch_lines(session: Session, model, ids: list[int], reconciliation: Reconciliation) -> list:
    rows = list(
        session.execute(
            select(model).where(model.id.in_(ids), model.firm_id == reconciliation.firm_id)
        ).scalars()
    )
    if len(rows) != len(set(ids)):
        raise ReviewError("Alguna partida no existe o pertenece a otra firma.")
    return rows


def _close_exceptions_for(session: Session, match: Match) -> None:
    movement_ids = [line.id for line in match.movements]
    entry_ids = [line.id for line in match.entries]
    conditions = []
    if movement_ids:
        conditions.append(Exception_.movement_id.in_(movement_ids))
    if entry_ids:
        conditions.append(Exception_.entry_id.in_(entry_ids))
    if not conditions:
        return
    from sqlalchemy import or_

    for exception in session.execute(select(Exception_).where(or_(*conditions))).scalars():
        if ExceptionStatus(exception.status).is_open:
            exception.status = ExceptionStatus.RESOLVED
            exception.resolved_at = _now()
            exception.resolved_by_id = match.approved_by_id


def _derive_rule(
    session: Session,
    tenant: Tenant,
    reconciliation: Reconciliation,
    movements: list,
    entries: list,
    correction: Correction,
) -> None:
    """Turn a manual pairing into a candidate rule for this client.

    Recorded only. v1 does not apply rules automatically; phase 2 does, and it
    needs these rows to exist from the first month.
    """
    client_id = _client_id(session, reconciliation)
    if client_id is None or len(movements) != 1 or len(entries) != 1:
        return
    bank_words = set((movements[0].normalized_description or "").split()[:6])
    ledger_words = set((entries[0].normalized_description or "").split()[:6])
    shared = sorted(bank_words & ledger_words)
    if not shared:
        return
    name = f"Banco «{' '.join(shared[:3])}» corresponde a asientos con la misma descripción"
    if session.execute(
        select(Rule).where(Rule.client_id == client_id, Rule.name == name)
    ).scalar_one_or_none():
        return
    session.add(
        Rule(
            firm_id=tenant.firm_id,
            client_id=client_id,
            name=name,
            definition={
                "tipo": "coincidencia_descripcion",
                "tokens": shared,
                "dias_tolerancia": abs((movements[0].value_date - entries[0].value_date).days),
            },
            derived_from_correction_id=correction.id,
            auto_apply=False,
        )
    )


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


def mark_reconciling_item(
    session: Session,
    tenant: Tenant,
    exception: Exception_,
    *,
    category: ExceptionCategory,
    reason: str | None = None,
) -> Exception_:
    """The item is legitimate: an outstanding check, a deposit in transit. It
    stays in the statement instead of waiting for a counterpart."""
    reconciliation = session.get(Reconciliation, exception.reconciliation_id)
    require_writable(session, reconciliation)
    previous = {"estado": exception.status, "categoria": exception.category}
    exception.status = ExceptionStatus.RECONCILING_ITEM
    exception.category = category
    exception.reason = reason
    _record_correction(
        session, tenant, reconciliation,
        action="exception.reconciling_item",
        proposed=previous,
        corrected={"estado": exception.status, "categoria": category.value},
        reason=reason,
        exception_id=exception.id,
    )
    audit(session, tenant, "exception.reconciling_item", object_type="Exception",
          object_id=exception.id, detail={"categoria": category.value})
    return exception


def defer_exception(
    session: Session, tenant: Tenant, exception: Exception_, reason: str
) -> Exception_:
    """Push it to the next period. It stays alive and keeps ageing."""
    reconciliation = session.get(Reconciliation, exception.reconciliation_id)
    require_writable(session, reconciliation)
    if not reason or not reason.strip():
        raise ReviewError("Diferir una partida exige un motivo.")
    exception.status = ExceptionStatus.DEFERRED
    exception.reason = reason
    _record_correction(
        session, tenant, reconciliation,
        action="exception.deferred", proposed={}, corrected={"estado": "DEFERRED"},
        reason=reason, exception_id=exception.id,
    )
    audit(session, tenant, "exception.deferred", object_type="Exception",
          object_id=exception.id, detail={"motivo": reason})
    return exception


def assign_exception(
    session: Session, tenant: Tenant, exception: Exception_, user_id: int | None
) -> Exception_:
    reconciliation = session.get(Reconciliation, exception.reconciliation_id)
    require_writable(session, reconciliation)
    if user_id is not None:
        assignee = session.get(User, user_id)
        if assignee is None or assignee.firm_id != tenant.firm_id:
            raise ReviewError("El usuario asignado no pertenece a esta firma.")
    exception.assigned_to_id = user_id
    audit(session, tenant, "exception.assigned", object_type="Exception",
          object_id=exception.id, detail={"asignado_a": user_id})
    return exception


# --------------------------------------------------------------------------- #
# Approval and closing
# --------------------------------------------------------------------------- #


def pending_proposals(session: Session, reconciliation: Reconciliation) -> int:
    return len(
        list(
            session.execute(
                select(Match.id).where(
                    Match.reconciliation_id == reconciliation.id,
                    Match.status == MatchStatus.PROPOSED,
                )
            ).scalars()
        )
    )


def open_exceptions(session: Session, reconciliation: Reconciliation) -> list[Exception_]:
    return list(
        session.execute(
            select(Exception_).where(
                Exception_.reconciliation_id == reconciliation.id,
                Exception_.status == ExceptionStatus.OPEN,
            )
        ).scalars()
    )


def approval_blockers(session: Session, reconciliation: Reconciliation, tenant: Tenant) -> list[str]:
    """Everything standing between this reconciliation and a signature."""
    blockers: list[str] = []
    if not tenant.can_approve:
        blockers.append("Solo el revisor puede aprobar una conciliación.")
    if not segregation_ok(reconciliation.prepared_by_id, tenant.user_id):
        blockers.append("Quien preparó la conciliación no puede aprobarla.")
    pending = pending_proposals(session, reconciliation)
    if pending:
        blockers.append(f"Hay {pending} propuesta(s) del modelo sin revisar.")
    unresolved = open_exceptions(session, reconciliation)
    if unresolved:
        blockers.append(
            f"Hay {len(unresolved)} excepción(es) abiertas sin clasificar como partida "
            "conciliatoria ni diferir."
        )
    if not reconciliation.balanced:
        blockers.append(
            f"La conciliación no cuadra: diferencia de {reconciliation.difference_cents} centavos."
        )
    return blockers


def approve_reconciliation(
    session: Session, tenant: Tenant, reconciliation: Reconciliation, *, notes: str | None = None
) -> Reconciliation:
    require_writable(session, reconciliation)
    require_approver(tenant)
    blockers = approval_blockers(session, reconciliation, tenant)
    if blockers:
        raise ReviewError(" ".join(blockers))
    reconciliation.status = ReconciliationStatus.APPROVED
    reconciliation.reviewed_by_id = tenant.user_id
    reconciliation.approved_at = _now()
    if notes:
        reconciliation.notes = notes
    audit(session, tenant, "reconciliation.approved", object_type="Reconciliation",
          object_id=reconciliation.id, detail={"diferencia": reconciliation.difference_cents})
    return reconciliation


def close_period(session: Session, tenant: Tenant, period: Period) -> Period:
    """A closed period is immutable (invariant 2). Everything in it has to be
    approved first, otherwise the workpaper would freeze unfinished work."""
    require_writable_period(session, period)
    if Role(tenant.role) not in (Role.REVIEWER, Role.ADMIN):
        raise RoleError("Cerrar un período está reservado al revisor o al administrador.")
    unfinished = [
        reconciliation
        for reconciliation in period.reconciliations
        if reconciliation.status != ReconciliationStatus.APPROVED
    ]
    if unfinished:
        raise ReviewError(
            f"Faltan {len(unfinished)} conciliación(es) por aprobar en {period.label}."
        )
    period.status = PeriodStatus.CLOSED
    period.closed_at = _now()
    period.closed_by_id = tenant.user_id
    audit(session, tenant, "period.closed", object_type="Period", object_id=period.id,
          detail={"periodo": period.label})
    return period


def reopen_period(session: Session, tenant: Tenant, period: Period, reason: str) -> Period:
    """Reopening is allowed, never silent: it is logged with who and why."""
    if Role(tenant.role) is not Role.ADMIN:
        raise RoleError("Reabrir un período cerrado está reservado al administrador.")
    if not reason or not reason.strip():
        raise ReviewError("Reabrir un período exige un motivo.")
    period.status = PeriodStatus.IN_REVIEW
    period.closed_at = None
    period.closed_by_id = None
    audit(session, tenant, "period.reopened", object_type="Period", object_id=period.id,
          detail={"periodo": period.label, "motivo": reason})
    return period


def exception_side_line(session: Session, exception: Exception_):
    return (
        session.get(BankMovement, exception.movement_id)
        if exception.side == Side.BANK
        else session.get(LedgerEntry, exception.entry_id)
    )
