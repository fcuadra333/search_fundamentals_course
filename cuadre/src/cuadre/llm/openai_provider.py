"""OpenAI provider.

Kept behind the same interface as every other provider so the choice between
laboratories stays a configuration line. Uses the official `openai` SDK,
imported lazily; the package is optional.
"""

from __future__ import annotations

import json
import time

from ..config import ModelSettings
from ..enums import ModelTier
from .base import ModelError, ModelRequest, ModelResponse, extract_json


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: ModelSettings):
        self.settings = settings
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - depends on install
                raise ModelError(
                    "Falta el paquete 'openai'. Instale con: pip install openai"
                ) from exc
            self._client = OpenAI(
                api_key=self.settings.openai_api_key or None,
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
        # The batch endpoint exists, but it is file-based and asynchronous in a
        # different shape. Until it is implemented here, say no rather than
        # pretend: the gateway then falls back to one call per group.
        return False

    def complete_json(self, request: ModelRequest) -> ModelResponse:
        model = self.model_for(request.tier)
        started = time.perf_counter()
        completion = self.client.chat.completions.create(
            model=model,
            max_completion_tokens=request.max_output_tokens,
            messages=[
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.schema,
                },
            },
        )
        text = completion.choices[0].message.content or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = extract_json(text)
        usage = completion.usage
        return ModelResponse(
            data=data,
            raw_text=text,
            model=model,
            provider=self.name,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
