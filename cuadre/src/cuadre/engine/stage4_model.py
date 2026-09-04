"""Stage 4 - model resolution of the residue.

The 15% the deterministic stages could not settle. Per call: up to fifteen
exceptions, each with five to eight candidates already retrieved by code, plus
the client's rules. The model chooses among those candidates or says there is
no match. It never sees the whole ledger.

Whatever comes back is checked here before it becomes a proposal: the
identifiers have to be ones we offered, and the two sides have to add up to the
same number. Stage 5 checks the period as a whole afterwards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..config import MatchingSettings, ModelSettings
from ..enums import MatchMethod, ModelTier, Side
from ..llm.base import LLMGateway, ModelRequest, ModelResponse
from ..llm.schemas import Stage4Output, stage4_json_schema
from .candidates import Candidate, rank_candidates
from .values import Line, MatchProposal, StageResult, remaining, sort_lines

SYSTEM_PROMPT = """\
Eres un asistente de conciliación bancaria que trabaja para una firma contable.

Recibes partidas que no se pudieron conciliar de forma determinista y, para cada \
una, los únicos candidatos posibles. Tu trabajo es decidir, partida por partida, \
si alguno de esos candidatos es su contrapartida.

Reglas que no puedes romper:
1. Solo puedes usar identificadores que aparezcan en la lista de candidatos de esa \
   misma partida. No inventes identificadores.
2. La suma de los movimientos elegidos debe ser exactamente igual a la suma de los \
   asientos elegidos. Si no cuadra al centavo, no es un emparejamiento.
3. Ante la duda, responde "no_match". Un falso positivo cuesta minutos de revisión; \
   un emparejamiento incorrecto aprobado cuesta el cliente.
4. La justificación es una sola frase en español, concreta, que un contador pueda \
   leer en el papel de trabajo.

Responde únicamente con el JSON del esquema indicado."""


@dataclass
class ExceptionCase:
    """One unresolved line and the universe the model may choose from."""

    line: Line
    side: Side
    candidates: list[Candidate]

    def as_payload(self) -> dict:
        return {
            "id": self.line.id,
            "side": self.side.value,
            "date": self.line.date.isoformat(),
            "amount": self.line.amount_cents,
            "description": self.line.description[:160],
            "reference": self.line.reference,
            "candidates": [candidate.as_payload() for candidate in self.candidates],
        }

    def candidate_notes(self) -> list[dict]:
        """What the queue shows the accountant: considered, and why discarded."""
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
            for candidate in self.candidates
        ]


@dataclass
class Stage4Result:
    result: StageResult
    calls: list[ModelResponse] = field(default_factory=list)
    # Keyed by (side, line id): the two sides have independent id sequences, so
    # the id alone would hand an entry the notes of an unrelated movement.
    notes: dict[tuple[str, int], list[dict]] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)

    @property
    def cost_micros(self) -> int:
        return sum(call.cost_micros_value for call in self.calls)


def build_cases(
    movements: list[Line],
    entries: list[Line],
    *,
    candidates_per_exception: int = 8,
) -> list[ExceptionCase]:
    """One case per unresolved line that has at least one candidate.

    A ledger entry already offered as a candidate on the bank side is not asked
    about again: the model has seen the pair once, and asking twice would double
    the token bill for the same decision.
    """
    cases: list[ExceptionCase] = []
    offered: set[int] = set()
    for movement in sort_lines(movements):
        ranked = rank_candidates(movement, entries, limit=candidates_per_exception)
        if not ranked:
            continue
        offered.update(candidate.line.id for candidate in ranked)
        cases.append(ExceptionCase(line=movement, side=Side.BANK, candidates=ranked))
    for entry in sort_lines(entries):
        if entry.id in offered:
            continue
        ranked = rank_candidates(entry, movements, limit=candidates_per_exception)
        if not ranked:
            continue
        cases.append(ExceptionCase(line=entry, side=Side.LEDGER, candidates=ranked))
    return cases


def resolve(
    movements: list[Line],
    entries: list[Line],
    *,
    gateway: LLMGateway,
    model_settings: ModelSettings | None = None,
    matching: MatchingSettings | None = None,
    client_rules: list[str] | None = None,
) -> Stage4Result:
    model_settings = model_settings or ModelSettings()
    cases = build_cases(
        movements, entries, candidates_per_exception=model_settings.candidates_per_exception
    )
    notes = {(case.side.value, case.line.id): case.candidate_notes() for case in cases}
    if not cases:
        return Stage4Result(
            result=StageResult(proposals=[], movements=movements, entries=entries,
                               stats={"cases": 0, "matched": 0}),
            notes=notes,
        )

    size = max(1, model_settings.exceptions_per_call)
    groups = [cases[start : start + size] for start in range(0, len(cases), size)]
    requests = [_request(group, client_rules, model_settings) for group in groups]
    outcomes = gateway.complete_many(requests, Stage4Output)

    movement_by_id = {line.id: line for line in movements}
    entry_by_id = {line.id: line for line in entries}
    used_movements: set[int] = set()
    used_entries: set[int] = set()
    proposals: list[MatchProposal] = []
    calls: list[ModelResponse] = []
    rejected: list[str] = []
    matched = 0

    for group, (parsed, response) in zip(groups, outcomes, strict=False):
        calls.append(response)
        if parsed is None:
            rejected.append(
                f"Lote de {len(group)} excepciones sin respuesta válida del modelo "
                f"({response.schema_error or 'error del proveedor'}); van a la cola."
            )
            continue
        allowed = {case.line.id: {c.line.id for c in case.candidates} for case in group}
        sides = {case.line.id: case.side for case in group}
        for decision in parsed.decisions:
            if decision.outcome == "no_match":
                continue
            problem = _validate(
                decision, allowed, sides, movement_by_id, entry_by_id,
                used_movements, used_entries,
            )
            if problem:
                rejected.append(f"Excepción {decision.exception_id}: {problem}")
                continue
            used_movements.update(decision.movement_ids)
            used_entries.update(decision.entry_ids)
            matched += 1
            proposals.append(
                MatchProposal(
                    movement_ids=tuple(sorted(decision.movement_ids)),
                    entry_ids=tuple(sorted(decision.entry_ids)),
                    method=MatchMethod.MODEL,
                    stage=4,
                    # The model's own confidence is trusted as a ranking signal,
                    # never as permission: every model match lands as PROPOSED
                    # and a human approves it.
                    confidence=round(min(decision.confidence, 0.95), 4),
                    rationale=decision.rationale,
                    amount_cents=sum(
                        movement_by_id[i].amount_cents for i in decision.movement_ids
                    ),
                )
            )

    return Stage4Result(
        result=StageResult(
            proposals=proposals,
            movements=remaining(movements, used_movements),
            entries=remaining(entries, used_entries),
            stats={"cases": len(cases), "calls": len(groups), "matched": matched},
        ),
        calls=calls,
        notes=notes,
        rejected=rejected,
    )


def _request(
    group: list[ExceptionCase], client_rules: list[str] | None, settings: ModelSettings
) -> ModelRequest:
    payload = {"exceptions": [case.as_payload() for case in group]}
    rules_block = ""
    if client_rules:
        listed = "\n".join(f"- {rule}" for rule in client_rules[:20])
        rules_block = f"\nReglas propias de este cliente:\n{listed}\n"
    user = (
        f"Resuelve {len(group)} partida(s) sin conciliar. Los montos están en centavos "
        "y el signo positivo significa entrada de efectivo.\n"
        "`day_offset` es la diferencia en días del candidato respecto de la partida.\n"
        f"{rules_block}\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=1)}\n"
        "```"
    )
    return ModelRequest(
        system=SYSTEM_PROMPT,
        user=user,
        schema=stage4_json_schema(),
        schema_name="conciliacion_excepciones",
        tier=ModelTier.MID,
        max_output_tokens=min(4096, 220 * len(group) + 512),
        effort="low",
    )


def _validate(
    decision,
    allowed: dict[int, set[int]],
    sides: dict[int, Side],
    movement_by_id: dict[int, Line],
    entry_by_id: dict[int, Line],
    used_movements: set[int],
    used_entries: set[int],
) -> str | None:
    """Reject anything we cannot act on. Returns the reason, or None if sound."""
    if decision.exception_id not in allowed:
        return "la decisión no corresponde a ninguna excepción de este lote"
    side = sides[decision.exception_id]
    own = decision.exception_id
    permitted = allowed[decision.exception_id]

    chosen_side = decision.movement_ids if side is Side.BANK else decision.entry_ids
    other_side = decision.entry_ids if side is Side.BANK else decision.movement_ids
    if list(chosen_side) != [own]:
        return "el lado de la propia partida debe contener solo su identificador"
    if not set(other_side).issubset(permitted):
        return "propuso identificadores fuera de la lista de candidatos"

    if any(i not in movement_by_id for i in decision.movement_ids):
        return "referenció un movimiento inexistente"
    if any(i not in entry_by_id for i in decision.entry_ids):
        return "referenció un asiento inexistente"
    if used_movements & set(decision.movement_ids) or used_entries & set(decision.entry_ids):
        return "usa una partida ya emparejada en este mismo lote"

    movement_total = sum(movement_by_id[i].amount_cents for i in decision.movement_ids)
    entry_total = sum(entry_by_id[i].amount_cents for i in decision.entry_ids)
    if movement_total != entry_total:
        return f"los dos lados no suman igual ({movement_total} contra {entry_total})"
    return None
