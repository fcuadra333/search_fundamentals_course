"""Candidate retrieval for stage 4.

The model never sees the ledger. For each exception the code retrieves five to
eight plausible counterparts, ranked, and that is the entire universe the model
chooses from (ESPECIFICACION 3, stage 4). This keeps the call near 10K tokens,
keeps the cost flat per exception, and makes a hallucinated identifier
impossible to act on.

The rejected candidates are kept too: the exception queue shows the accountant
what the system considered and why it discarded each one.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ingest.normalize import reference_matches, similarity
from ..money import format_amount
from .values import Line


@dataclass(frozen=True)
class Candidate:
    line: Line
    score: float
    day_offset: int
    amount_gap_cents: int
    reference_hit: bool
    text_similarity: float

    def reason_discarded(self) -> str:
        """Why this candidate is not obviously the answer, in one phrase."""
        parts = []
        if self.amount_gap_cents:
            parts.append(f"difiere {format_amount(abs(self.amount_gap_cents))}")
        if abs(self.day_offset) > 5:
            parts.append(f"{abs(self.day_offset)} días de separación")
        if not self.reference_hit:
            parts.append("sin referencia coincidente")
        if self.text_similarity < 0.4:
            parts.append("descripción distinta")
        return ", ".join(parts) or "coincidencia parcial"

    def as_payload(self) -> dict:
        return {
            "id": self.line.id,
            "date": self.line.date.isoformat(),
            "amount": self.line.amount_cents,
            "description": self.line.description[:120],
            "reference": self.line.reference,
            "day_offset": self.day_offset,
            "amount_gap": self.amount_gap_cents,
            "reference_hit": self.reference_hit,
            "text_similarity": self.text_similarity,
        }


def rank_candidates(target: Line, pool: list[Line], limit: int = 8) -> list[Candidate]:
    """Rank the other side by how plausible each line is as the counterpart.

    Amount dominates on purpose: in a reconciliation the amount is the fact and
    everything else is context.
    """
    scored: list[Candidate] = []
    for line in pool:
        if (line.amount_cents > 0) != (target.amount_cents > 0):
            continue  # An inflow never clears an outflow.
        gap = line.amount_cents - target.amount_cents
        base = abs(target.amount_cents) or 1
        amount_score = max(0.0, 1 - abs(gap) / base)
        if amount_score < 0.5:
            continue  # More than double or less than half: not the same item.
        days = (line.date - target.date).days
        day_score = max(0.0, 1 - abs(days) / 45)
        reference_hit = reference_matches(target.reference, line.reference)
        text_score = similarity(target.description, line.description)
        score = round(
            0.45 * amount_score + 0.20 * day_score + 0.20 * (1.0 if reference_hit else 0.0)
            + 0.15 * text_score,
            4,
        )
        scored.append(
            Candidate(
                line=line,
                score=score,
                day_offset=days,
                amount_gap_cents=gap,
                reference_hit=reference_hit,
                text_similarity=text_score,
            )
        )
    scored.sort(key=lambda candidate: (-candidate.score, candidate.line.id))
    return scored[:limit]
