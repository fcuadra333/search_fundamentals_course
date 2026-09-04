"""Offline provider.

Deterministic, free and always available. It is what the test suite, the demo
seed and an on-premise dry run use, and it is the control arm of the regression
bench: whatever a real model scores, it has to beat this.

It reads the same JSON payload the real prompt carries and applies one rule:
the closest candidate by amount, date and reference wins if it clears a floor.
"""

from __future__ import annotations

import json
import re

from ..enums import ModelTier
from .base import ModelRequest, ModelResponse

_PAYLOAD_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_MIN_SCORE = 0.55
# Two candidates this close are not distinguishable by the rule below, and the
# house rule is explicit: prefer the queue over a guess. A wrong match that
# somebody approves costs the client; a false positive costs minutes.
_TIE_MARGIN = 0.06


class StubProvider:
    name = "stub"

    def model_for(self, tier: ModelTier) -> str:
        return "stub"

    def supports_batch(self) -> bool:
        return False

    def complete_json(self, request: ModelRequest) -> ModelResponse:
        payload = _read_payload(request.user)
        decisions = [_decide(exception) for exception in payload.get("exceptions", [])]
        data = {"decisions": decisions}
        text = json.dumps(data, ensure_ascii=False)
        return ModelResponse(
            data=data,
            raw_text=text,
            model="stub",
            provider=self.name,
            # Counted so the accounting path is exercised even offline.
            input_tokens=len(request.system + request.user) // 4,
            output_tokens=len(text) // 4,
        )


def _read_payload(user_message: str) -> dict:
    match = _PAYLOAD_RE.search(user_message)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def _decide(exception: dict) -> dict:
    candidates = exception.get("candidates") or []
    exception_id = exception["id"]
    if not candidates:
        return {
            "exception_id": exception_id,
            "outcome": "no_match",
            "movement_ids": [],
            "entry_ids": [],
            "confidence": 0.9,
            "rationale": "No se recuperó ningún candidato para esta partida.",
        }

    scored = sorted(
        ((_score(exception, candidate), candidate) for candidate in candidates),
        key=lambda pair: (-pair[0], pair[1]["id"]),
    )
    score, best = scored[0]
    if len(scored) > 1 and (score - scored[1][0]) < _TIE_MARGIN:
        return {
            "exception_id": exception_id,
            "outcome": "no_match",
            "movement_ids": [],
            "entry_ids": [],
            "confidence": 0.6,
            "rationale": (
                "Hay dos candidatos igual de plausibles; se envía a revisión en lugar de elegir."
            ),
        }
    if score < _MIN_SCORE:
        return {
            "exception_id": exception_id,
            "outcome": "no_match",
            "movement_ids": [],
            "entry_ids": [],
            "confidence": round(0.5 + (1 - score) * 0.4, 3),
            "rationale": "Ningún candidato alcanza la similitud mínima de monto, fecha y referencia.",
        }

    side = exception.get("side")
    if side == "BANK":
        movement_ids, entry_ids = [exception_id], [best["id"]]
    else:
        movement_ids, entry_ids = [best["id"]], [exception_id]
    return {
        "exception_id": exception_id,
        "outcome": "match",
        "movement_ids": movement_ids,
        "entry_ids": entry_ids,
        "confidence": round(min(0.95, score), 3),
        "rationale": (
            f"Candidato con diferencia de {abs(best.get('amount', 0) - exception.get('amount', 0))} "
            f"centavos y {abs(best.get('day_offset', 0))} día(s) de separación."
        ),
    }


def _score(exception: dict, candidate: dict) -> float:
    amount_gap = abs(candidate.get("amount", 0) - exception.get("amount", 0))
    base = abs(exception.get("amount", 0)) or 1
    amount_score = max(0.0, 1 - amount_gap / base)
    day_score = max(0.0, 1 - abs(candidate.get("day_offset", 0)) / 30)
    reference_score = 1.0 if candidate.get("reference_hit") else 0.0
    text_score = float(candidate.get("text_similarity", 0.0))
    return round(
        0.45 * amount_score + 0.20 * day_score + 0.20 * reference_score + 0.15 * text_score, 4
    )
