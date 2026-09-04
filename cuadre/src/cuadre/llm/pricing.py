"""Published price list, in US dollars per million tokens.

Each row says where its number came from. A cost that lands in the code or in a
report has to declare whether it is measured or estimated (CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    model: str
    input_per_mtok: float
    output_per_mtok: float
    # "tarifa publicada" = taken from the provider's price list.
    # "estimado"         = our own figure, to be replaced once measured.
    source: str


PRICES: dict[str, Price] = {
    # Anthropic list prices.
    "claude-sonnet-5": Price("claude-sonnet-5", 2.00, 10.00, "tarifa publicada"),
    "claude-haiku-4-5": Price("claude-haiku-4-5", 1.00, 5.00, "tarifa publicada"),
    # Taken from docs/ESPECIFICACION.md and CLAUDE.md; not verified against a
    # provider price list by this codebase.
    "gpt-5.6-luna": Price("gpt-5.6-luna", 0.20, 1.20, "estimado (según especificación)"),
    "gpt-5.6-terra": Price("gpt-5.6-terra", 2.00, 12.00, "estimado (según especificación)"),
    # Offline provider used by tests, the demo and the on-premise dry run.
    "stub": Price("stub", 0.0, 0.0, "sin costo"),
}

# The batch APIs of both providers bill at half price. Reconciliation is night
# work, so the discount is the default whenever the provider offers it.
BATCH_DISCOUNT = 0.5


def cost_micros(model: str, input_tokens: int, output_tokens: int, *, batched: bool = False) -> int:
    """Cost in millionths of a dollar.

    Dollars per million tokens times tokens gives micro-dollars directly, so the
    whole ledger stays in integers and never rounds through a float balance.
    """
    price = PRICES.get(model)
    if price is None:
        return 0
    total = input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok
    if batched:
        total *= BATCH_DISCOUNT
    return int(round(total))


def format_micros(micros: int) -> str:
    return f"US$ {micros / 1_000_000:,.4f}"
