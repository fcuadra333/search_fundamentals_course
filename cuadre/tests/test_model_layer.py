"""Stage 4 and the gateway every model call goes through."""

from __future__ import annotations

import datetime as dt

from conftest import line
from pydantic import ValidationError

from cuadre.config import ModelSettings
from cuadre.engine.candidates import rank_candidates
from cuadre.engine.stage4_model import build_cases, resolve
from cuadre.enums import MatchMethod, ModelTier, Side
from cuadre.llm.base import LLMGateway, ModelRequest, ModelResponse
from cuadre.llm.pricing import cost_micros
from cuadre.llm.registry import build_gateway
from cuadre.llm.schemas import Decision, Stage4Output, stage4_json_schema
from cuadre.llm.stub import StubProvider

D = dt.date


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_a_match_without_both_sides_is_rejected():
    try:
        Decision(exception_id=1, outcome="match", movement_ids=[1], entry_ids=[],
                 confidence=0.9, rationale="x")
    except ValidationError as exc:
        assert "match" in str(exc)
    else:
        raise AssertionError("debió rechazarse")


def test_a_no_match_carrying_ids_is_rejected():
    try:
        Decision(exception_id=1, outcome="no_match", movement_ids=[1], entry_ids=[2],
                 confidence=0.9, rationale="x")
    except ValidationError:
        pass
    else:
        raise AssertionError("debió rechazarse")


def test_two_decisions_for_one_exception_are_rejected():
    good = dict(outcome="no_match", confidence=0.5, rationale="x")
    try:
        Stage4Output(decisions=[Decision(exception_id=1, **good), Decision(exception_id=1, **good)])
    except ValidationError:
        pass
    else:
        raise AssertionError("debió rechazarse")


def test_the_schema_is_closed_and_fully_required():
    schema = stage4_json_schema()
    decision = schema["$defs"]["Decision"]
    assert decision["additionalProperties"] is False
    assert set(decision["required"]) == set(decision["properties"])


# --------------------------------------------------------------------------- #
# Gateway
# --------------------------------------------------------------------------- #


class _Flaky:
    """Returns junk the first time, then something valid."""

    name = "flaky"

    def __init__(self):
        self.calls = 0

    def model_for(self, tier):
        return "claude-sonnet-5"

    def supports_batch(self):
        return False

    def complete_json(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        data = (
            {"decisions": [{"exception_id": 1, "outcome": "maybe"}]}
            if self.calls == 1
            else {"decisions": []}
        )
        return ModelResponse(data=data, raw_text="", model="claude-sonnet-5",
                             provider=self.name, input_tokens=100, output_tokens=20)


def test_invalid_output_is_retried_with_the_error_in_the_prompt():
    provider = _Flaky()
    gateway = LLMGateway(provider=provider, max_retries=2)
    parsed, response = gateway.complete(_request(), Stage4Output)
    assert parsed is not None
    assert provider.calls == 2
    assert response.attempts == 2
    assert response.input_tokens == 200  # both attempts are paid for and counted


class _AlwaysBad(_Flaky):
    def complete_json(self, request):
        self.calls += 1
        return ModelResponse(data={"decisions": [{"nope": 1}]}, raw_text="",
                             model="claude-sonnet-5", provider="bad",
                             input_tokens=10, output_tokens=1)


def test_giving_up_returns_nothing_rather_than_a_guess():
    gateway = LLMGateway(provider=_AlwaysBad(), max_retries=1)
    parsed, response = gateway.complete(_request(), Stage4Output)
    assert parsed is None
    assert not response.ok
    assert response.schema_error


def _request() -> ModelRequest:
    return ModelRequest(system="s", user="u", schema=stage4_json_schema(),
                        schema_name="x", tier=ModelTier.MID)


def test_cost_is_integer_micro_dollars_at_the_published_rate():
    # 1M input tokens of Sonnet 5 at US$2.00 is exactly 2,000,000 micro-dollars.
    assert cost_micros("claude-sonnet-5", 1_000_000, 0) == 2_000_000
    assert cost_micros("claude-sonnet-5", 0, 1_000_000) == 10_000_000
    assert cost_micros("claude-sonnet-5", 1_000_000, 0, batched=True) == 1_000_000
    assert cost_micros("modelo-inexistente", 1_000_000, 1_000_000) == 0


def test_the_same_request_has_the_same_fingerprint():
    a, b = _request(), _request()
    assert a.fingerprint("m") == b.fingerprint("m")
    other = ModelRequest(system="s", user="otro", schema=stage4_json_schema(),
                         schema_name="x", tier=ModelTier.MID)
    assert other.fingerprint("m") != a.fingerprint("m")


class _NoBatch(StubProvider):
    """Advertises batching and then fails, to prove the fallback works."""

    def supports_batch(self):
        return True

    def complete_json_batch(self, requests):
        raise RuntimeError("el lote no se pudo enviar")


def test_a_failed_batch_falls_back_to_one_call_at_a_time():
    gateway = LLMGateway(provider=_NoBatch(), use_batch=True)
    results = gateway.complete_many([_request(), _request()], Stage4Output)
    assert len(results) == 2
    assert all(parsed is not None for parsed, _ in results)


# --------------------------------------------------------------------------- #
# Candidate retrieval and stage 4
# --------------------------------------------------------------------------- #


def test_candidates_never_cross_the_sign_or_the_order_of_magnitude():
    target = line(1, Side.BANK, D(2026, 1, 10), -235000, "PAGO PROVEEDOR")
    pool = [
        line(11, Side.LEDGER, D(2026, 1, 9), -235000, "PROVEEDOR FACTURA"),
        line(12, Side.LEDGER, D(2026, 1, 9), 235000, "COBRO"),          # wrong sign
        line(13, Side.LEDGER, D(2026, 1, 9), -100, "COMISION"),         # wrong size
    ]
    ranked = rank_candidates(target, pool)
    assert [candidate.line.id for candidate in ranked] == [11]


def test_stage4_asks_about_each_line_once():
    movements = [line(1, Side.BANK, D(2026, 1, 10), -235000, "ACH PROVEEDOR")]
    entries = [line(11, Side.LEDGER, D(2026, 1, 2), -235000, "PAGO PROVEEDOR")]
    cases = build_cases(movements, entries)
    assert len(cases) == 1  # the entry was already offered as a candidate
    assert cases[0].side is Side.BANK


def test_stage4_resolves_what_the_deterministic_stages_could_not():
    movements = [line(1, Side.BANK, D(2026, 1, 26), -235000, "TRANSFERENCIA ACH 4471 UNION")]
    entries = [line(11, Side.LEDGER, D(2026, 1, 14), -235000, "PAGO DISTRIBUIDORA LA UNION")]
    outcome = resolve(movements, entries, gateway=build_gateway(ModelSettings(provider="stub")))
    assert len(outcome.result.proposals) == 1
    proposal = outcome.result.proposals[0]
    assert proposal.method is MatchMethod.MODEL
    assert proposal.movement_ids == (1,) and proposal.entry_ids == (11,)
    assert proposal.confidence <= 0.95  # a model never gets full confidence
    assert outcome.notes[(Side.BANK.value, 1)]


class _Hallucinating(StubProvider):
    def complete_json(self, request):
        return ModelResponse(
            data={"decisions": [{"exception_id": 1, "outcome": "match",
                                 "movement_ids": [1], "entry_ids": [999],
                                 "confidence": 0.99, "rationale": "inventado"}]},
            raw_text="", model="stub", provider="stub")


def test_an_identifier_we_never_offered_is_refused():
    movements = [line(1, Side.BANK, D(2026, 1, 26), -235000, "ACH")]
    entries = [line(11, Side.LEDGER, D(2026, 1, 14), -235000, "PAGO")]
    outcome = resolve(movements, entries, gateway=LLMGateway(provider=_Hallucinating()))
    assert not outcome.result.proposals
    assert any("fuera de la lista" in reason for reason in outcome.rejected)


class _Unbalanced(StubProvider):
    def complete_json(self, request):
        return ModelResponse(
            data={"decisions": [{"exception_id": 1, "outcome": "match",
                                 "movement_ids": [1], "entry_ids": [11],
                                 "confidence": 0.99, "rationale": "no cuadra"}]},
            raw_text="", model="stub", provider="stub")


def test_a_match_whose_sides_do_not_add_up_is_refused():
    movements = [line(1, Side.BANK, D(2026, 1, 26), -235000, "ACH")]
    entries = [line(11, Side.LEDGER, D(2026, 1, 14), -200000, "PAGO PARCIAL")]
    outcome = resolve(movements, entries, gateway=LLMGateway(provider=_Unbalanced()))
    assert not outcome.result.proposals
    assert any("no suman igual" in reason for reason in outcome.rejected)
