"""Provider selection.

One function, one place. Changing laboratory is an environment variable, not a
code change anywhere outside this package.
"""

from __future__ import annotations

from ..config import ModelSettings, get_settings
from .base import LLMGateway, LLMProvider
from .stub import StubProvider

_PROVIDERS = {"stub", "anthropic", "openai"}


def build_provider(settings: ModelSettings | None = None) -> LLMProvider:
    settings = settings or get_settings().models
    name = (settings.provider or "stub").lower()
    if name not in _PROVIDERS:
        raise ValueError(
            f"Proveedor de modelos desconocido: {name}. Opciones: {', '.join(sorted(_PROVIDERS))}"
        )
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    if name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(settings)
    return StubProvider()


def build_gateway(settings: ModelSettings | None = None) -> LLMGateway:
    settings = settings or get_settings().models
    return LLMGateway(
        provider=build_provider(settings),
        max_retries=settings.max_retries,
        use_batch=settings.prefer_batch_api,
    )
