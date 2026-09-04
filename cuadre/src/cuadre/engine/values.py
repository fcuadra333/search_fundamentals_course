"""Value objects the deterministic stages work on.

Frozen and hashable on purpose: a stage may not mutate its input, so a later
stage always sees exactly what the previous one saw.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..enums import MatchMethod, Side


@dataclass(frozen=True, slots=True)
class Line:
    """One statement line or one ledger entry, already normalized."""

    id: int
    side: Side
    date: dt.date
    amount_cents: int
    description: str = ""
    reference: str | None = None
    # True when the line came in from an earlier period still unresolved.
    carried: bool = False

    @property
    def is_inflow(self) -> bool:
        return self.amount_cents > 0


@dataclass(frozen=True, slots=True)
class MatchProposal:
    """N:M by construction: both sides are tuples, never single ids."""

    movement_ids: tuple[int, ...]
    entry_ids: tuple[int, ...]
    method: MatchMethod
    stage: int
    confidence: float
    rationale: str
    amount_cents: int
    difference_cents: int = 0

    @property
    def cardinality(self) -> str:
        return f"{len(self.movement_ids)}:{len(self.entry_ids)}"

    def key(self) -> tuple:
        return (tuple(sorted(self.movement_ids)), tuple(sorted(self.entry_ids)))


@dataclass
class StageResult:
    """What a stage produced and what it left for the next one."""

    proposals: list[MatchProposal] = field(default_factory=list)
    movements: list[Line] = field(default_factory=list)
    entries: list[Line] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def matched_lines(self) -> int:
        return sum(len(p.movement_ids) + len(p.entry_ids) for p in self.proposals)


def remaining(lines: list[Line], used: set[int]) -> list[Line]:
    return [line for line in lines if line.id not in used]


def sort_lines(lines: list[Line]) -> list[Line]:
    """Canonical order. Every stage iterates in it so results never depend on
    the order rows came out of the database."""
    return sorted(lines, key=lambda line: (line.date, line.amount_cents, line.id))
