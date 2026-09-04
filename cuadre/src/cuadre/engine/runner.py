"""The orchestrator: five stages over one reconciliation, with persistence.

Reproducibility (CLAUDE.md rule 4): the deterministic stages are pure, so a
re-run reproduces them exactly; the model's output is persisted on the match and
on the model_calls row, and a second run of the same period reuses the stored
result instead of paying to re-derive it.

Carry-forward: lines left open in the previous period are loaded into this
period's pools. A check written in March and cashed in April is matched here,
across the period boundary, which is the whole reason the two balances differ.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..enums import (
    ExceptionStatus,
    LineState,
    MatchMethod,
    MatchStatus,
    ReconciliationStatus,
    RunStatus,
    Side,
)
from ..llm.base import LLMGateway
from ..llm.registry import build_gateway
from ..models import (
    BankMovement,
    Exception_,
    LedgerEntry,
    Match,
    MatchEntry,
    MatchMovement,
    ModelCall,
    Period,
    Reconciliation,
    Rule,
    Run,
)
from ..tenancy import Tenant, audit, require_writable
from . import stage4_model
from .classify import classify
from .stage2_exact import match_exact
from .stage3_fuzzy import match_fuzzy
from .stage5_verify import VerificationResult, verify, verify_movement_totals
from .values import Line, MatchProposal

MAX_STAGE4_PASSES = 3
ENGINE_VERSION = "1.0.0"


@dataclass
class RunOutcome:
    run: Run
    verification: VerificationResult
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def run_reconciliation(
    session: Session,
    tenant: Tenant,
    reconciliation: Reconciliation,
    *,
    gateway: LLMGateway | None = None,
    settings: Settings | None = None,
    use_model: bool = True,
) -> RunOutcome:
    settings = settings or get_settings()
    require_writable(session, reconciliation)
    gateway = gateway or build_gateway(settings.models)

    run = Run(
        firm_id=tenant.firm_id,
        reconciliation_id=reconciliation.id,
        engine_version=ENGINE_VERSION,
        status=RunStatus.RUNNING,
        triggered_by_id=tenant.user_id,
    )
    session.add(run)
    session.flush()
    reconciliation.status = ReconciliationStatus.RUNNING

    warnings: list[str] = []
    try:
        _clear_previous_proposals(session, reconciliation)
        movements, entries, carried_exceptions = _load_lines(session, reconciliation)
        locked_movements, locked_entries = _locked_line_ids(session, reconciliation)

        open_movements = [line for line in movements if line.id not in locked_movements]
        open_entries = [line for line in entries if line.id not in locked_entries]

        warnings.extend(_balance_warnings(session, reconciliation, movements, entries))

        proposals: list[MatchProposal] = []
        stats: dict = {}

        stage2 = match_exact(open_movements, open_entries, settings.matching)
        proposals += stage2.proposals
        stats["etapa2"] = stage2.stats

        stage3 = match_fuzzy(stage2.movements, stage2.entries, settings.matching)
        proposals += stage3.proposals
        stats["etapa3"] = stage3.stats

        residual_movements, residual_entries = stage3.movements, stage3.entries
        stats["etapa4"] = {"pasadas": 0, "casos": 0, "emparejados": 0, "costo_micros": 0}
        notes: dict[tuple[str, int], list[dict]] = {}
        rejected: list[str] = []

        if use_model and (residual_movements and residual_entries):
            client_rules = _client_rules(session, reconciliation)
            for pass_number in range(1, MAX_STAGE4_PASSES + 1):
                outcome = stage4_model.resolve(
                    residual_movements,
                    residual_entries,
                    gateway=gateway,
                    model_settings=settings.models,
                    matching=settings.matching,
                    client_rules=client_rules,
                )
                notes.update(outcome.notes)
                rejected.extend(outcome.rejected)
                _persist_model_calls(session, tenant, run, outcome.calls)
                proposals += outcome.result.proposals
                stats["etapa4"]["pasadas"] = pass_number
                stats["etapa4"]["casos"] += outcome.result.stats.get("cases", 0)
                stats["etapa4"]["emparejados"] += len(outcome.result.proposals)
                stats["etapa4"]["costo_micros"] += outcome.cost_micros
                residual_movements = outcome.result.movements
                residual_entries = outcome.result.entries
                # Another pass only pays for itself if the last one moved.
                if not outcome.result.proposals:
                    break

        _persist_matches(session, tenant, reconciliation, run, proposals, settings)
        session.flush()

        approved_matches = _effective_matches(session, reconciliation)
        verification = verify(
            movements=movements,
            entries=entries,
            matches=approved_matches,
            bank_closing_cents=reconciliation.closing_bank_cents,
            ledger_closing_cents=reconciliation.closing_ledger_cents,
        )
        _persist_exceptions(
            session, tenant, reconciliation, verification, notes, carried_exceptions
        )

        reconciliation.difference_cents = verification.difference_cents
        reconciliation.balanced = verification.balanced
        reconciliation.status = ReconciliationStatus.IN_REVIEW
        if reconciliation.prepared_by_id is None:
            reconciliation.prepared_by_id = tenant.user_id

        stats["etapa5"] = {
            "cuadra": verification.balanced,
            "diferencia_centavos": verification.difference_cents,
            "tasa_emparejamiento": verification.match_rate,
            "problemas": verification.problems,
        }
        stats["rechazos_modelo"] = rejected
        stats["advertencias"] = warnings

        run.status = RunStatus.COMPLETED
        run.finished_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        run.stats = stats
        session.flush()

        audit(
            session, tenant, "run.completed", object_type="Reconciliation",
            object_id=reconciliation.id,
            detail={"corrida": run.id, "cuadra": verification.balanced,
                    "diferencia": verification.difference_cents},
        )
        return RunOutcome(run=run, verification=verification, stats=stats, warnings=warnings)

    except Exception as exc:
        run.status = RunStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        reconciliation.status = ReconciliationStatus.FAILED
        session.flush()
        raise


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _as_line(row: BankMovement | LedgerEntry, side: Side, *, carried: bool = False) -> Line:
    return Line(
        id=row.id,
        side=side,
        date=row.value_date,
        amount_cents=row.amount_cents,
        description=row.normalized_description or row.raw_description,
        reference=row.reference,
        carried=carried,
    )


def _load_lines(
    session: Session, reconciliation: Reconciliation
) -> tuple[list[Line], list[Line], list[Exception_]]:
    movements = [
        _as_line(row, Side.BANK)
        for row in session.execute(
            select(BankMovement).where(BankMovement.reconciliation_id == reconciliation.id)
        ).scalars()
    ]
    entries = [
        _as_line(row, Side.LEDGER)
        for row in session.execute(
            select(LedgerEntry).where(LedgerEntry.reconciliation_id == reconciliation.id)
        ).scalars()
    ]

    carried = _carried_exceptions(session, reconciliation)
    for exception in carried:
        if exception.side == Side.BANK and exception.movement is not None:
            movements.append(_as_line(exception.movement, Side.BANK, carried=True))
        elif exception.side == Side.LEDGER and exception.entry is not None:
            entries.append(_as_line(exception.entry, Side.LEDGER, carried=True))
    return movements, entries, carried


def _carried_exceptions(session: Session, reconciliation: Reconciliation) -> list[Exception_]:
    """Open items from the immediately preceding period of the same account."""
    period = session.get(Period, reconciliation.period_id)
    previous = session.execute(
        select(Reconciliation)
        .join(Period, Period.id == Reconciliation.period_id)
        .where(
            Reconciliation.firm_id == reconciliation.firm_id,
            Reconciliation.bank_account_id == reconciliation.bank_account_id,
            Period.end_date < period.start_date,
        )
        .order_by(Period.end_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if previous is None:
        return []
    return list(
        session.execute(
            select(Exception_).where(
                Exception_.reconciliation_id == previous.id,
                Exception_.status.in_(
                    [
                        ExceptionStatus.OPEN,
                        ExceptionStatus.DEFERRED,
                        ExceptionStatus.RECONCILING_ITEM,
                    ]
                ),
            )
        ).scalars()
    )


def _locked_line_ids(session: Session, reconciliation: Reconciliation) -> tuple[set[int], set[int]]:
    """Lines already inside a human-approved or manual match are untouchable:
    the engine may not undo what a person decided."""
    matches = session.execute(
        select(Match).where(
            Match.reconciliation_id == reconciliation.id,
            Match.status == MatchStatus.APPROVED,
        )
    ).scalars()
    movements: set[int] = set()
    entries: set[int] = set()
    for match in matches:
        movements.update(line.id for line in match.movements)
        entries.update(line.id for line in match.entries)
    return movements, entries


def _client_rules(session: Session, reconciliation: Reconciliation) -> list[str]:
    """Rules are recorded from corrections and shown to the model as context.
    They are not applied automatically in v1 (ESPECIFICACION 2)."""
    from ..models import Account, Entity

    account = session.get(Account, reconciliation.bank_account_id)
    entity = session.get(Entity, account.entity_id) if account else None
    if entity is None:
        return []
    rules = session.execute(
        select(Rule).where(Rule.client_id == entity.client_id).order_by(Rule.hits.desc()).limit(20)
    ).scalars()
    return [rule.name for rule in rules]


def _balance_warnings(
    session: Session, reconciliation: Reconciliation, movements: list[Line], entries: list[Line]
) -> list[str]:
    own_movements = [line for line in movements if not line.carried]
    own_entries = [line for line in entries if not line.carried]
    warnings = []
    for lines, opening, closing, label in (
        (own_movements, reconciliation.opening_bank_cents, reconciliation.closing_bank_cents,
         "Estado de cuenta"),
        (own_entries, reconciliation.opening_ledger_cents, reconciliation.closing_ledger_cents,
         "Libro mayor"),
    ):
        problem = verify_movement_totals(lines, opening, closing, label)
        if problem:
            warnings.append(problem)
    return warnings


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _clear_previous_proposals(session: Session, reconciliation: Reconciliation) -> None:
    """Drop the previous run's unapproved proposals so a re-run is idempotent.
    Approved and rejected matches survive: those carry a human decision."""
    stale = session.execute(
        select(Match).where(
            Match.reconciliation_id == reconciliation.id,
            Match.status == MatchStatus.PROPOSED,
        )
    ).scalars()
    for match in stale:
        for line in list(match.movements) + list(match.entries):
            line.match_id = None
            if line.state == LineState.MATCHED:
                line.state = LineState.OPEN
        session.delete(match)
    session.flush()


def _fingerprint(proposal: MatchProposal) -> str:
    payload = f"{sorted(proposal.movement_ids)}|{sorted(proposal.entry_ids)}|{proposal.method}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _persist_matches(
    session: Session,
    tenant: Tenant,
    reconciliation: Reconciliation,
    run: Run,
    proposals: list[MatchProposal],
    settings: Settings,
) -> None:
    """Deterministic matches are approved by the system; a model match always
    waits for a person. The reconciliation as a whole is approved by the
    reviewer either way, and any of these can be overridden."""
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    existing = {
        row.fingerprint
        for row in session.execute(
            select(Match).where(Match.reconciliation_id == reconciliation.id)
        ).scalars()
    }
    for proposal in proposals:
        fingerprint = _fingerprint(proposal)
        if fingerprint in existing:
            continue
        existing.add(fingerprint)
        deterministic = proposal.method in (MatchMethod.EXACT, MatchMethod.FUZZY)
        match = Match(
            firm_id=tenant.firm_id,
            reconciliation_id=reconciliation.id,
            run_id=run.id,
            method=proposal.method,
            stage=proposal.stage,
            confidence=proposal.confidence,
            status=MatchStatus.APPROVED if deterministic else MatchStatus.PROPOSED,
            amount_cents=proposal.amount_cents,
            difference_cents=proposal.difference_cents,
            rationale=proposal.rationale,
            fingerprint=fingerprint,
            approved_at=now if deterministic else None,
        )
        session.add(match)
        session.flush()
        for movement_id in proposal.movement_ids:
            session.add(MatchMovement(match_id=match.id, movement_id=movement_id))
            line = session.get(BankMovement, movement_id)
            if line is not None:
                line.state = LineState.MATCHED
                line.match_id = match.id
        for entry_id in proposal.entry_ids:
            session.add(MatchEntry(match_id=match.id, entry_id=entry_id))
            line = session.get(LedgerEntry, entry_id)
            if line is not None:
                line.state = LineState.MATCHED
                line.match_id = match.id


def _effective_matches(session: Session, reconciliation: Reconciliation) -> list[MatchProposal]:
    """What the preparer is working with: everything not rejected. Approval of
    the reconciliation requires no proposal left pending."""
    matches = session.execute(
        select(Match).where(
            Match.reconciliation_id == reconciliation.id,
            Match.status != MatchStatus.REJECTED,
        )
    ).scalars()
    return [
        MatchProposal(
            movement_ids=tuple(line.id for line in match.movements),
            entry_ids=tuple(line.id for line in match.entries),
            method=MatchMethod(match.method),
            stage=match.stage,
            confidence=match.confidence,
            rationale=match.rationale or "",
            amount_cents=match.amount_cents,
        )
        for match in matches
    ]


def _persist_exceptions(
    session: Session,
    tenant: Tenant,
    reconciliation: Reconciliation,
    verification: VerificationResult,
    notes: dict[tuple[str, int], list[dict]],
    carried: list[Exception_],
) -> None:
    """Refresh the queue: one exception per unmatched line, ageing from the
    line's own date and linked to the item it came from in an earlier period."""
    period = session.get(Period, reconciliation.period_id)
    reference_date = period.end_date if period else dt.date.today()

    carried_by_line = {
        (exception.side, exception.movement_id or exception.entry_id): exception
        for exception in carried
    }

    existing = {
        (row.side, row.movement_id or row.entry_id): row
        for row in session.execute(
            select(Exception_).where(Exception_.reconciliation_id == reconciliation.id)
        ).scalars()
    }
    open_keys: set[tuple[str, int]] = set()

    for item in verification.items:
        side = item.side
        key = (side.value, item.line.id)
        open_keys.add(key)
        row = existing.get(key)
        age = (reference_date - item.line.date).days
        if row is None:
            source = carried_by_line.get(key)
            row = Exception_(
                firm_id=tenant.firm_id,
                reconciliation_id=reconciliation.id,
                side=side,
                movement_id=item.line.id if side is Side.BANK else None,
                entry_id=item.line.id if side is Side.LEDGER else None,
                first_seen_period_id=(
                    source.first_seen_period_id if source else reconciliation.period_id
                ),
                carried_from_id=source.id if source else None,
            )
            session.add(row)
        row.category = item.category
        row.amount_cents = item.amount_cents
        row.value_date = item.line.date
        row.age_days = max(age, 0)
        # The queue always shows what was considered and why it was discarded.
        # Stage 4's own notes win when it looked at this line; otherwise the
        # candidates are ranked here, so an item the model never saw is not
        # presented as if nothing had been examined.
        row.candidates = notes.get(key) or _candidate_notes(item, verification.items)
        if row.status not in (ExceptionStatus.RECONCILING_ITEM, ExceptionStatus.DEFERRED):
            row.status = ExceptionStatus.OPEN
        line = (
            session.get(BankMovement, item.line.id)
            if side is Side.BANK
            else session.get(LedgerEntry, item.line.id)
        )
        if line is not None and line.reconciliation_id == reconciliation.id:
            line.state = LineState.EXCEPTION

    # Anything no longer open got matched on this run.
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    for key, row in existing.items():
        if key not in open_keys and ExceptionStatus(row.status).is_open:
            row.status = ExceptionStatus.RESOLVED
            row.resolved_at = now
            row.reason = (row.reason or "") + " Emparejada por el motor."

    # A carried item cleared in this period closes in the period it came from.
    matched_lines = {
        (Side.BANK.value, line_id)
        for match in _effective_matches(session, reconciliation)
        for line_id in match.movement_ids
    } | {
        (Side.LEDGER.value, line_id)
        for match in _effective_matches(session, reconciliation)
        for line_id in match.entry_ids
    }
    for key, exception in carried_by_line.items():
        if key in matched_lines and ExceptionStatus(exception.status).is_open:
            exception.status = ExceptionStatus.RESOLVED
            exception.resolved_at = now
            exception.reason = (
                f"Conciliada en el período siguiente (conciliación {reconciliation.id})."
            )


def _candidate_notes(item, all_items) -> list[dict]:
    """Rank the still-open lines of the other side as candidates for this one."""
    from .candidates import rank_candidates

    pool = [other.line for other in all_items if other.side is not item.side]
    return [
        {
            "id": candidate.line.id,
            "fecha": candidate.line.date.strftime("%d/%m/%Y"),
            "monto": candidate.line.amount_cents,
            "descripcion": candidate.line.description[:120],
            "referencia": candidate.line.reference,
            "puntaje": candidate.score,
            "motivo": candidate.reason_discarded(),
        }
        for candidate in rank_candidates(item.line, pool, limit=5)
    ]


def _persist_model_calls(session: Session, tenant: Tenant, run: Run, calls) -> None:
    from ..enums import ModelTier

    for call in calls:
        session.add(
            ModelCall(
                firm_id=tenant.firm_id,
                run_id=run.id,
                purpose="exception_resolution",
                tier=ModelTier.MID,
                provider=call.provider,
                model=call.model,
                batched=call.batched,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cost_micros=call.cost_micros_value,
                latency_ms=call.latency_ms,
                attempts=call.attempts,
                ok=call.ok,
                schema_error=call.schema_error,
                request_hash=call.fingerprint,
                response=call.data,
            )
        )


def current_verification(session: Session, reconciliation: Reconciliation) -> VerificationResult:
    """Recompute the statement from what is stored right now.

    Cheap and pure, so the screen never shows a figure that disagrees with the
    database: every approval or override is reflected the moment it is saved.
    """
    movements, entries, _ = _load_lines(session, reconciliation)
    return verify(
        movements=movements,
        entries=entries,
        matches=_effective_matches(session, reconciliation),
        bank_closing_cents=reconciliation.closing_bank_cents,
        ledger_closing_cents=reconciliation.closing_ledger_cents,
    )


def refresh_totals(session: Session, reconciliation: Reconciliation) -> VerificationResult:
    """Recompute and store the difference. Called after any manual change."""
    verification = current_verification(session, reconciliation)
    reconciliation.difference_cents = verification.difference_cents
    reconciliation.balanced = verification.balanced
    session.flush()
    return verification
