"""Stage 5 - deterministic verification.

The reconciliation ties or it does not. This is arithmetic, not judgement, and
it is the stage that lets a mid-sized model be sold with a guaranteed result:
whatever the model proposed, the number either closes or the item goes back to
the queue.

Never optional, never skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..enums import ExceptionCategory, Side
from .classify import classify
from .values import Line, MatchProposal


@dataclass
class ReconcilingItem:
    line: Line
    side: Side
    category: ExceptionCategory

    @property
    def amount_cents(self) -> int:
        return self.line.amount_cents


@dataclass
class VerificationResult:
    """The four-column bank reconciliation, plus the integrity checks."""

    bank_closing_cents: int
    ledger_closing_cents: int
    deposits_in_transit_cents: int
    outstanding_checks_cents: int
    bank_credits_unrecorded_cents: int
    bank_charges_unrecorded_cents: int
    adjusted_bank_cents: int
    adjusted_ledger_cents: int
    difference_cents: int
    balanced: bool
    items: list[ReconcilingItem] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    matched_movements: int = 0
    matched_entries: int = 0
    open_movements: int = 0
    open_entries: int = 0

    @property
    def integrity_ok(self) -> bool:
        return not self.problems

    @property
    def match_rate(self) -> float:
        """Share of lines resolved without a human. What the client feels."""
        total = self.matched_movements + self.matched_entries + self.open_movements + self.open_entries
        if not total:
            return 0.0
        return round((self.matched_movements + self.matched_entries) / total, 4)


def verify(
    *,
    movements: list[Line],
    entries: list[Line],
    matches: list[MatchProposal],
    bank_closing_cents: int,
    ledger_closing_cents: int,
    carried_items: list[ReconcilingItem] | None = None,
) -> VerificationResult:
    """Check every match, build the statement, and report the difference.

    `carried_items` are reconciling items still open from earlier periods. They
    belong in the statement: a check written in March and uncashed in June is
    exactly why the two balances differ in June.
    """
    problems: list[str] = []
    movement_by_id = {line.id: line for line in movements}
    entry_by_id = {line.id: line for line in entries}

    matched_movements: set[int] = set()
    matched_entries: set[int] = set()

    for index, match in enumerate(matches):
        movement_sum = 0
        entry_sum = 0
        for movement_id in match.movement_ids:
            line = movement_by_id.get(movement_id)
            if line is None:
                problems.append(f"Emparejamiento {index + 1}: movimiento {movement_id} inexistente")
                continue
            if movement_id in matched_movements:
                problems.append(
                    f"Movimiento {movement_id} aparece en más de un emparejamiento aprobado"
                )
            matched_movements.add(movement_id)
            movement_sum += line.amount_cents
        for entry_id in match.entry_ids:
            line = entry_by_id.get(entry_id)
            if line is None:
                problems.append(f"Emparejamiento {index + 1}: asiento {entry_id} inexistente")
                continue
            if entry_id in matched_entries:
                problems.append(f"Asiento {entry_id} aparece en más de un emparejamiento aprobado")
            matched_entries.add(entry_id)
            entry_sum += line.amount_cents
        if not match.movement_ids or not match.entry_ids:
            problems.append(f"Emparejamiento {index + 1}: le falta un lado")
        elif movement_sum != entry_sum:
            problems.append(
                f"Emparejamiento {index + 1} descuadra por {movement_sum - entry_sum} centavos"
            )

    open_movements = [line for line in movements if line.id not in matched_movements]
    open_entries = [line for line in entries if line.id not in matched_entries]

    items = [ReconcilingItem(line, Side.BANK, classify(line, Side.BANK)) for line in open_movements]
    items += [
        ReconcilingItem(line, Side.LEDGER, classify(line, Side.LEDGER)) for line in open_entries
    ]
    items += list(carried_items or [])

    deposits_in_transit = sum(
        item.amount_cents for item in items
        if item.side is Side.LEDGER and item.amount_cents > 0
    )
    outstanding_checks = sum(
        item.amount_cents for item in items
        if item.side is Side.LEDGER and item.amount_cents < 0
    )
    bank_credits = sum(
        item.amount_cents for item in items if item.side is Side.BANK and item.amount_cents > 0
    )
    bank_charges = sum(
        item.amount_cents for item in items if item.side is Side.BANK and item.amount_cents < 0
    )

    # Books-side items adjust the bank balance and vice versa: an item is on the
    # statement precisely because the other side has not seen it yet.
    adjusted_bank = bank_closing_cents + deposits_in_transit + outstanding_checks
    adjusted_ledger = ledger_closing_cents + bank_credits + bank_charges
    difference = adjusted_bank - adjusted_ledger

    return VerificationResult(
        bank_closing_cents=bank_closing_cents,
        ledger_closing_cents=ledger_closing_cents,
        deposits_in_transit_cents=deposits_in_transit,
        outstanding_checks_cents=outstanding_checks,
        bank_credits_unrecorded_cents=bank_credits,
        bank_charges_unrecorded_cents=bank_charges,
        adjusted_bank_cents=adjusted_bank,
        adjusted_ledger_cents=adjusted_ledger,
        difference_cents=difference,
        balanced=difference == 0 and not problems,
        items=items,
        problems=problems,
        matched_movements=len(matched_movements),
        matched_entries=len(matched_entries),
        open_movements=len(open_movements),
        open_entries=len(open_entries),
    )


def verify_movement_totals(
    lines: list[Line], opening_cents: int, closing_cents: int, label: str
) -> str | None:
    """Opening plus the movements has to be the closing balance.

    Catches a truncated export before anybody spends a day hunting a difference
    that is really a missing page.
    """
    total = opening_cents + sum(line.amount_cents for line in lines)
    if total != closing_cents:
        return (
            f"{label}: saldo inicial más movimientos da {total} centavos "
            f"pero el saldo final declarado es {closing_cents}. "
            "Revise si el archivo está completo."
        )
    return None
