"""Stage 3 - fuzzy deterministic matching.

Exact amount inside a date window, reference as a substring, and N:1 / 1:N
aggregation by sum. Roughly another 10% at zero cost.

"Fuzzy" is about the date and the description, never about the amount: the
amount always ties to the cent. A reconciliation that accepts a near amount is
not a reconciliation, it is an estimate.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from ..config import MatchingSettings
from ..enums import MatchMethod
from ..ingest.normalize import reference_matches, similarity
from ..money import format_amount
from .values import Line, MatchProposal, StageResult, remaining, sort_lines


def match_fuzzy(
    movements: list[Line],
    entries: list[Line],
    settings: MatchingSettings | None = None,
) -> StageResult:
    settings = settings or MatchingSettings()
    movements, entries = sort_lines(movements), sort_lines(entries)
    used_movements: set[int] = set()
    used_entries: set[int] = set()
    proposals: list[MatchProposal] = []
    stats = {"by_window": 0, "by_reference": 0, "grouped": 0}

    _pair_within_window(
        movements, entries, used_movements, used_entries, proposals, stats, settings
    )
    _pair_by_reference(
        movements, entries, used_movements, used_entries, proposals, stats, settings
    )
    _pair_by_grouping(
        movements, entries, used_movements, used_entries, proposals, stats, settings
    )

    return StageResult(
        proposals=proposals,
        movements=remaining(movements, used_movements),
        entries=remaining(entries, used_entries),
        stats=stats,
    )


def _score(movement: Line, entry: Line, window: int) -> tuple[float, float, int, bool]:
    days = abs((movement.date - entry.date).days)
    date_score = max(0.0, 1.0 - days / window) if window else 1.0
    text_score = similarity(movement.description, entry.description)
    reference_hit = reference_matches(movement.reference, entry.reference)
    score = 0.50 * date_score + 0.35 * text_score + 0.15 * (1.0 if reference_hit else 0.0)
    return round(score, 4), text_score, days, reference_hit


def _pair_within_window(
    movements: list[Line],
    entries: list[Line],
    used_movements: set[int],
    used_entries: set[int],
    proposals: list[MatchProposal],
    stats: dict[str, int],
    settings: MatchingSettings,
) -> None:
    """Exact amount, date within the window.

    A pair is taken when a reference agrees, when the descriptions are close
    enough, or when neither side has any other candidate: an amount that occurs
    once on each side inside five days is the same transaction.
    """
    window = settings.fuzzy_date_window_days
    by_amount: dict[int, list[Line]] = defaultdict(list)
    for entry in entries:
        by_amount[entry.amount_cents].append(entry)

    candidates: dict[int, list[Line]] = {}
    for movement in movements:
        candidates[movement.id] = [
            entry
            for entry in by_amount.get(movement.amount_cents, ())
            if abs((movement.date - entry.date).days) <= window
        ]
    entry_demand: dict[int, int] = defaultdict(int)
    for options in candidates.values():
        for entry in options:
            entry_demand[entry.id] += 1

    scored: list[tuple[float, int, int, str]] = []
    for movement in movements:
        options = candidates[movement.id]
        for entry in options:
            score, text_score, days, reference_hit = _score(movement, entry, window)
            unique = len(options) == 1 and entry_demand[entry.id] == 1
            if not (reference_hit or text_score >= settings.min_description_similarity or unique):
                continue
            if reference_hit:
                why = f"Monto idéntico, referencia {entry.reference}, {days} día(s) de diferencia"
            elif unique:
                why = f"Monto idéntico y único candidato dentro de {window} días ({days} día(s))"
            else:
                why = (
                    f"Monto idéntico, {days} día(s) de diferencia, "
                    f"descripción {int(text_score * 100)}% similar"
                )
            scored.append((score, movement.id, entry.id, why))

    # Highest score first; ties resolved by id so the outcome never depends on
    # dictionary or database ordering.
    for score, movement_id, entry_id, why in sorted(scored, key=lambda s: (-s[0], s[1], s[2])):
        if movement_id in used_movements or entry_id in used_entries:
            continue
        used_movements.add(movement_id)
        used_entries.add(entry_id)
        stats["by_window"] += 1
        amount = next(m.amount_cents for m in movements if m.id == movement_id)
        proposals.append(
            MatchProposal(
                movement_ids=(movement_id,),
                entry_ids=(entry_id,),
                method=MatchMethod.FUZZY,
                stage=3,
                confidence=min(0.95, round(0.60 + score * 0.35, 4)),
                rationale=why,
                amount_cents=amount,
            )
        )


def _pair_by_reference(
    movements: list[Line],
    entries: list[Line],
    used_movements: set[int],
    used_entries: set[int],
    proposals: list[MatchProposal],
    stats: dict[str, int],
    settings: MatchingSettings,
) -> None:
    """Exact amount and a reference that contains the other, over a long window.

    This is the check written in March and cashed in May. The reference carries
    the match, so the date is allowed to be far.
    """
    window = settings.reference_date_window_days
    free_entries = [e for e in entries if e.id not in used_entries and e.reference]
    for movement in movements:
        if movement.id in used_movements or not movement.reference:
            continue
        for entry in free_entries:
            if entry.id in used_entries:
                continue
            if entry.amount_cents != movement.amount_cents:
                continue
            if abs((movement.date - entry.date).days) > window:
                continue
            if not reference_matches(
                movement.reference, entry.reference, settings.min_reference_length
            ):
                continue
            used_movements.add(movement.id)
            used_entries.add(entry.id)
            stats["by_reference"] += 1
            proposals.append(
                MatchProposal(
                    movement_ids=(movement.id,),
                    entry_ids=(entry.id,),
                    method=MatchMethod.FUZZY,
                    stage=3,
                    confidence=0.90,
                    rationale=(
                        (
                            f"Referencia {movement.reference} coincide"
                            if movement.reference == entry.reference
                            else f"Referencia {movement.reference} contenida en {entry.reference}"
                        )
                        + f", monto idéntico, {abs((movement.date - entry.date).days)} día(s)"
                    ),
                    amount_cents=movement.amount_cents,
                )
            )
            break


def _pair_by_grouping(
    movements: list[Line],
    entries: list[Line],
    used_movements: set[int],
    used_entries: set[int],
    proposals: list[MatchProposal],
    stats: dict[str, int],
    settings: MatchingSettings,
) -> None:
    """N:1 and 1:N by sum.

    One deposit clears five invoices; one ledger entry covers three bank
    charges. This is the case a 1:1 model cannot express at all, which is why
    the match table is N:M from the first line.
    """
    _group_direction(
        targets=[m for m in movements if m.id not in used_movements],
        pool=[e for e in entries if e.id not in used_entries],
        used_targets=used_movements,
        used_pool=used_entries,
        target_is_movement=True,
        proposals=proposals,
        stats=stats,
        settings=settings,
    )
    _group_direction(
        targets=[e for e in entries if e.id not in used_entries],
        pool=[m for m in movements if m.id not in used_movements],
        used_targets=used_entries,
        used_pool=used_movements,
        target_is_movement=False,
        proposals=proposals,
        stats=stats,
        settings=settings,
    )


def _group_direction(
    *,
    targets: list[Line],
    pool: list[Line],
    used_targets: set[int],
    used_pool: set[int],
    target_is_movement: bool,
    proposals: list[MatchProposal],
    stats: dict[str, int],
    settings: MatchingSettings,
) -> None:
    window = settings.group_date_window_days
    for target in sort_lines(targets):
        if target.id in used_targets:
            continue
        # Same sign and inside the window. A group that mixes inflows and
        # outflows to reach a total is a coincidence, not a transaction.
        available = [
            line
            for line in pool
            if line.id not in used_pool
            and (line.amount_cents > 0) == (target.amount_cents > 0)
            and abs((line.date - target.date).days) <= window
            and abs(line.amount_cents) <= abs(target.amount_cents)
        ]
        available.sort(key=lambda line: (abs((line.date - target.date).days), line.id))
        available = available[: settings.max_group_pool]
        if len(available) < 2:
            continue

        group = _find_subset(available, target.amount_cents, settings.max_group_size)
        if group is None:
            continue

        ids = tuple(sorted(line.id for line in group))
        used_targets.add(target.id)
        used_pool.update(ids)
        stats["grouped"] += 1
        spread = (max(l.date for l in group) - min(l.date for l in group)).days
        rationale = (
            f"Agrupación {len(group)} partidas por suma exacta de "
            f"{format_amount(abs(target.amount_cents))} "
            f"({spread} día(s) entre la primera y la última)"
        )
        proposals.append(
            MatchProposal(
                movement_ids=(target.id,) if target_is_movement else ids,
                entry_ids=ids if target_is_movement else (target.id,),
                method=MatchMethod.FUZZY,
                stage=3,
                confidence=0.85,
                rationale=rationale,
                amount_cents=target.amount_cents,
            )
        )


def _find_subset(pool: list[Line], total: int, max_size: int) -> list[Line] | None:
    """Smallest subset that sums exactly to the target, then tightest in time.

    Exhaustive over a pool capped by configuration, so the answer is the same on
    every run and the cost is bounded.
    """
    best: list[Line] | None = None
    best_key: tuple[int, int, tuple[int, ...]] | None = None
    for size in range(2, max_size + 1):
        if size > len(pool):
            break
        for combination in combinations(pool, size):
            if sum(line.amount_cents for line in combination) != total:
                continue
            spread = (max(l.date for l in combination) - min(l.date for l in combination)).days
            key = (size, spread, tuple(line.id for line in combination))
            if best_key is None or key < best_key:
                best, best_key = list(combination), key
        if best is not None:
            break  # A smaller group always wins over a larger one.
    return best
