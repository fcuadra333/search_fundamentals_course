"""Stage 2 - exact deterministic matching.

Identical amount, identical date or within one day, matching reference.
Resolves roughly 75% of the lines at zero cost. Pure and testable, which is the
whole point: this is the part that must never surprise anybody.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Iterable

from ..config import MatchingSettings
from ..enums import MatchMethod
from .values import Line, MatchProposal, StageResult, remaining, sort_lines

KeyFn = Callable[[Line], Iterable[object]]


def match_exact(
    movements: list[Line],
    entries: list[Line],
    settings: MatchingSettings | None = None,
) -> StageResult:
    settings = settings or MatchingSettings()
    movements, entries = sort_lines(movements), sort_lines(entries)
    used_movements: set[int] = set()
    used_entries: set[int] = set()
    proposals: list[MatchProposal] = []
    stats = {"by_reference": 0, "by_date": 0, "by_date_slack": 0}

    def pair_on(
        movement_key: KeyFn, entry_key: KeyFn, label: str, confidence: float, stat: str
    ) -> None:
        """Bucket both sides on a key and pair within each bucket.

        Only one side may widen its key (the date slack). Widening both would
        turn a one-day tolerance into two without anybody noticing.
        """
        buckets: dict[object, tuple[list[Line], list[Line]]] = defaultdict(lambda: ([], []))
        for line in movements:
            if line.id not in used_movements:
                for key in movement_key(line):
                    buckets[key][0].append(line)
        for line in entries:
            if line.id not in used_entries:
                for key in entry_key(line):
                    buckets[key][1].append(line)

        for key in sorted(buckets, key=str):
            bucket_movements, bucket_entries = buckets[key]
            free_movements = [m for m in bucket_movements if m.id not in used_movements]
            free_entries = [e for e in bucket_entries if e.id not in used_entries]
            # Same amount and same key on both sides: the lines are
            # indistinguishable, so pairing them in id order is not a choice.
            ambiguous = len(free_movements) > 1 and len(free_entries) > 1
            for movement, entry in zip(free_movements, free_entries, strict=False):
                used_movements.add(movement.id)
                used_entries.add(entry.id)
                stats[stat] += 1
                detail = label
                if ambiguous:
                    detail += f" (grupo de {min(len(free_movements), len(free_entries))} idénticos)"
                proposals.append(
                    MatchProposal(
                        movement_ids=(movement.id,),
                        entry_ids=(entry.id,),
                        method=MatchMethod.EXACT,
                        stage=2,
                        confidence=confidence if not ambiguous else round(confidence - 0.05, 4),
                        rationale=detail,
                        amount_cents=movement.amount_cents,
                    )
                )

    min_reference = settings.min_reference_length
    slack = settings.exact_date_slack_days

    def by_amount_reference(line: Line) -> Iterable[object]:
        if line.reference and len(line.reference) >= min_reference:
            return [("ref", line.amount_cents, line.reference)]
        return []

    def by_amount_date(line: Line) -> Iterable[object]:
        return [("date", line.amount_cents, line.date)]

    def by_amount_date_slack(line: Line) -> Iterable[object]:
        return [
            ("date", line.amount_cents, line.date + dt.timedelta(days=offset))
            for offset in range(-slack, slack + 1)
        ]

    pair_on(by_amount_reference, by_amount_reference,
            "Monto y referencia idénticos", 1.0, "by_reference")
    pair_on(by_amount_date, by_amount_date, "Monto y fecha idénticos", 1.0, "by_date")
    if slack > 0:
        pair_on(by_amount_date, by_amount_date_slack,
                f"Monto idéntico, fecha con ±{slack} día", 0.97, "by_date_slack")

    return StageResult(
        proposals=proposals,
        movements=remaining(movements, used_movements),
        entries=remaining(entries, used_entries),
        stats=stats,
    )
