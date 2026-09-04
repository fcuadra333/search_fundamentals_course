"""Anthropic provider.

Uses the official `anthropic` SDK, imported lazily so the package stays an
optional dependency: the offline stub has to run on a machine that never talks
to a provider.

Model capabilities differ inside the family, so the request is assembled from a
small table instead of sending every parameter to every model and hoping.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..config import ModelSettings
from ..enums import ModelTier
from .base import ModelError, ModelRequest, ModelResponse, extract_json


@dataclass(frozen=True)
class _Capabilities:
    adaptive_thinking: bool
    effort: bool


def _capabilities(model: str) -> _Capabilities:
    """Adaptive thinking and the effort control exist on the Sonnet 5 and Opus
    generations; Haiku 4.5 rejects both."""
    if model.startswith(("claude-opus-", "claude-sonnet-5", "claude-fable-")):
        return _Capabilities(adaptive_thinking=True, effort=True)
    return _Capabilities(adaptive_thinking=False, effort=False)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: ModelSettings):
        self.settings = settings
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on install
                raise ModelError(
                    "Falta el paquete 'anthropic'. Instale con: pip install anthropic"
                ) from exc
            self._client = anthropic.Anthropic(
                api_key=self.settings.anthropic_api_key or None,
                timeout=float(self.settings.request_timeout_seconds),
            )
        return self._client

    def model_for(self, tier: ModelTier) -> str:
        return (
            self.settings.economy_model
            if tier is ModelTier.ECONOMY
            else self.settings.mid_model
        )

    def supports_batch(self) -> bool:
        return True

    def _params(self, request: ModelRequest, model: str) -> dict:
        capabilities = _capabilities(model)
        output_config: dict = {
            "format": {"type": "json_schema", "schema": request.schema}
        }
        if capabilities.effort:
            output_config["effort"] = request.effort
        params: dict = {
            "model": model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
            "output_config": output_config,
        }
        if capabilities.adaptive_thinking:
            params["thinking"] = {"type": "adaptive"}
        return params

    def complete_json(self, request: ModelRequest) -> ModelResponse:
        model = self.model_for(request.tier)
        started = time.perf_counter()
        message = self.client.messages.create(**self._params(request, model))
        return self._to_response(message, model, started)

    def complete_json_batch(self, requests: list[ModelRequest]) -> list[ModelResponse]:
        """Submit the whole run as one batch and wait.

        Reconciliation is night work: the latency does not matter and the batch
        API bills at half price.
        """
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request as BatchRequest

        started = time.perf_counter()
        batch = self.client.messages.batches.create(
            requests=[
                BatchRequest(
                    custom_id=f"exc-{index}",
                    params=MessageCreateParamsNonStreaming(
                        **self._params(request, self.model_for(request.tier))
                    ),
                )
                for index, request in enumerate(requests)
            ]
        )
        deadline = time.monotonic() + 24 * 3600
        while time.monotonic() < deadline:
            status = self.client.messages.batches.retrieve(batch.id)
            if status.processing_status == "ended":
                break
            time.sleep(30)
        else:  # pragma: no cover - only on a 24 hour batch
            raise ModelError("El lote no terminó dentro de la ventana permitida")

        # Results come back in any order; key by custom_id, never by position.
        by_id: dict[str, ModelResponse] = {}
        for result in self.client.messages.batches.results(batch.id):
            model = self.model_for(ModelTier.MID)
            if result.result.type != "succeeded":
                by_id[result.custom_id] = ModelResponse(
                    data={}, raw_text="", model=model, provider=self.name,
                    ok=False, batched=True,
                    schema_error=f"lote: {result.result.type}",
                )
                continue
            response = self._to_response(result.result.message, model, started)
            response.batched = True
            by_id[result.custom_id] = response
        return [
            by_id.get(
                f"exc-{index}",
                ModelResponse(data={}, raw_text="", model=self.model_for(request.tier),
                              provider=self.name, ok=False, batched=True,
                              schema_error="lote: sin resultado"),
            )
            for index, request in enumerate(requests)
        ]

    def _to_response(self, message, model: str, started: float) -> ModelResponse:
        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            raise ModelError(f"El modelo declinó la solicitud ({getattr(details, 'category', '?')})")
        text = next((block.text for block in message.content if block.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = extract_json(text)
        usage = message.usage
        return ModelResponse(
            data=data,
            raw_text=text,
            model=model,
            provider=self.name,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
