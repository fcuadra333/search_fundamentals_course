"""The single interface every model call passes through.

Responsibilities that live here and nowhere else:
  - one request shape, whatever the provider
  - JSON output validated against a schema, with a bounded retry
  - tokens, cost, latency and the raw response recorded for every call

Rule 5 of CLAUDE.md: nothing unvalidated reaches the user.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from ..enums import ModelTier
from .pricing import cost_micros

T = TypeVar("T", bound=BaseModel)


class ModelError(RuntimeError):
    """The provider failed, or kept returning output the schema rejects."""


@dataclass
class ModelRequest:
    system: str
    user: str
    schema: dict[str, Any]
    schema_name: str
    tier: ModelTier = ModelTier.MID
    max_output_tokens: int = 4096
    effort: str = "low"
    temperature: float | None = None
    purpose: str = "exception_resolution"

    def fingerprint(self, model: str) -> str:
        """Stable hash of everything that determines the answer. Two identical
        requests are the same call, which is what makes a run replayable."""
        payload = json.dumps(
            {
                "model": model,
                "system": self.system,
                "user": self.user,
                "schema": self.schema,
                "effort": self.effort,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class ModelResponse:
    data: dict[str, Any]
    raw_text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    batched: bool = False
    ok: bool = True
    schema_error: str | None = None
    cost_micros_value: int = 0
    fingerprint: str = ""

    @property
    def usd(self) -> float:
        return self.cost_micros_value / 1_000_000


class LLMProvider(Protocol):
    """What a provider has to offer. Three methods, no leakage upward."""

    name: str

    def model_for(self, tier: ModelTier) -> str: ...

    def complete_json(self, request: ModelRequest) -> ModelResponse: ...

    def supports_batch(self) -> bool: ...


@dataclass
class LLMGateway:
    """Wraps a provider with validation, retry and accounting."""

    provider: LLMProvider
    max_retries: int = 2
    use_batch: bool = False
    calls: list[ModelResponse] = field(default_factory=list)

    def complete(self, request: ModelRequest, model_cls: type[T]) -> tuple[T | None, ModelResponse]:
        """Return the parsed object, or None with the failure recorded.

        A schema violation is retried with the error appended to the prompt.
        When the retries run out the caller gets None, and everything in that
        batch goes to the exception queue: preferring the queue over a guess is
        the house rule.
        """
        model = self.provider.model_for(request.tier)
        fingerprint = request.fingerprint(model)
        attempt = 0
        last_error: str | None = None
        current = request
        started = time.perf_counter()
        total_input = total_output = 0

        while attempt <= self.max_retries:
            attempt += 1
            try:
                response = self.provider.complete_json(current)
            except Exception as exc:  # provider transport failure
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt > self.max_retries:
                    break
                continue

            total_input += response.input_tokens
            total_output += response.output_tokens
            try:
                parsed = model_cls.model_validate(response.data)
            except ValidationError as exc:
                last_error = _short_validation_error(exc)
                if attempt <= self.max_retries:
                    current = _with_repair_note(request, last_error)
                    continue
                break

            response.attempts = attempt
            response.input_tokens = total_input
            response.output_tokens = total_output
            response.latency_ms = int((time.perf_counter() - started) * 1000)
            response.fingerprint = fingerprint
            response.cost_micros_value = cost_micros(
                response.model, total_input, total_output, batched=response.batched
            )
            self.calls.append(response)
            return parsed, response

        failure = ModelResponse(
            data={},
            raw_text="",
            model=model,
            provider=self.provider.name,
            input_tokens=total_input,
            output_tokens=total_output,
            latency_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempt,
            ok=False,
            schema_error=last_error,
            fingerprint=fingerprint,
            cost_micros_value=cost_micros(model, total_input, total_output),
        )
        self.calls.append(failure)
        return None, failure

    def complete_many(
        self, requests: list[ModelRequest], model_cls: type[T]
    ) -> list[tuple[T | None, ModelResponse]]:
        """Resolve a list of requests, using the provider batch API when there
        is one. Anything the batch returns broken is retried one by one."""
        if not requests:
            return []
        use_batch = self.use_batch and self.provider.supports_batch() and len(requests) > 1
        if not use_batch:
            return [self.complete(request, model_cls) for request in requests]

        try:
            responses = self.provider.complete_json_batch(requests)  # type: ignore[attr-defined]
        except Exception:
            # A batch that cannot be submitted is not a reason to lose the run.
            return [self.complete(request, model_cls) for request in requests]

        results: list[tuple[T | None, ModelResponse]] = []
        for request, response in zip(requests, responses, strict=False):
            if not response.ok:
                results.append(self.complete(request, model_cls))
                continue
            try:
                parsed = model_cls.model_validate(response.data)
            except ValidationError as exc:
                response.ok = False
                response.schema_error = _short_validation_error(exc)
                self.calls.append(response)
                results.append(self.complete(request, model_cls))
                continue
            response.fingerprint = request.fingerprint(response.model)
            response.cost_micros_value = cost_micros(
                response.model, response.input_tokens, response.output_tokens, batched=True
            )
            self.calls.append(response)
            results.append((parsed, response))
        return results

    @property
    def total_cost_micros(self) -> int:
        return sum(call.cost_micros_value for call in self.calls)

    @property
    def total_tokens(self) -> tuple[int, int]:
        return (
            sum(call.input_tokens for call in self.calls),
            sum(call.output_tokens for call in self.calls),
        )


def _short_validation_error(exc: ValidationError) -> str:
    problems = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]]
    return "; ".join(problems)


def _with_repair_note(request: ModelRequest, error: str) -> ModelRequest:
    return ModelRequest(
        system=request.system,
        user=(
            f"{request.user}\n\n"
            f"[Corrección] La respuesta anterior no cumplió el esquema: {error}. "
            "Responda únicamente con JSON válido según el esquema indicado."
        ),
        schema=request.schema,
        schema_name=request.schema_name,
        tier=request.tier,
        max_output_tokens=request.max_output_tokens,
        effort=request.effort,
        temperature=request.temperature,
        purpose=request.purpose,
    )


def extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a response that may carry prose around it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ModelError("La respuesta no contiene un objeto JSON")
    return json.loads(text[start : end + 1])
