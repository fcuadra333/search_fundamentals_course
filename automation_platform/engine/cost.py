"""Cost accounting (§9).

Computer use burns screenshots and screenshots are expensive. Two rules follow:

- Every number the platform shows says whether it was **measured** or
  **estimated**. The token counts a driver reports back are measured; the
  screenshot size used for a forecast is estimated.
- Prompt caching is the main lever, because the skill definition and its context
  repeat on every run. `PriceTable` therefore has a separate cached-input price
  and `estimate_run` shows the difference.

The prices below are placeholders in the configuration, not a quote. An operator
sets the real ones for the model they run; until then everything derived from
them is reported as estimated.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import Measurement

# Estimated range for one screenshot, in input tokens (§9).
SCREENSHOT_TOKENS_LOW = 1100
SCREENSHOT_TOKENS_HIGH = 1600
SCREENSHOT_TOKENS_MID = (SCREENSHOT_TOKENS_LOW + SCREENSHOT_TOKENS_HIGH) // 2

# A three-screen skill takes this many screenshots counting verifications (§9).
SCREENSHOTS_PER_SKILL_LOW = 15
SCREENSHOTS_PER_SKILL_HIGH = 25

# Context beyond the screenshots, and the terse action output.
CONTEXT_TOKENS = 8_000
OUTPUT_TOKENS_LOW = 2_000
OUTPUT_TOKENS_HIGH = 3_000


@dataclass(frozen=True)
class PriceTable:
    """USD per million tokens. Configure per model before trusting any total."""

    input_per_million: float = 2.0
    cached_input_per_million: float = 0.20
    output_per_million: float = 10.0
    model_label: str = "configurable"

    def cost(self, *, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        billable_input = max(input_tokens - cached_tokens, 0)
        return (
            billable_input * self.input_per_million
            + cached_tokens * self.cached_input_per_million
            + output_tokens * self.output_per_million
        ) / 1_000_000


DEFAULT_PRICES = PriceTable()


@dataclass(frozen=True)
class CostBreakdown:
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float
    measurement: Measurement

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "measurement": self.measurement.value,
        }


@dataclass(frozen=True)
class CostRange:
    """A forecast, always estimated, never presented as a measurement."""

    low_usd: float
    high_usd: float
    measurement: Measurement = Measurement.ESTIMATED

    def to_dict(self) -> dict:
        return {
            "low_usd": round(self.low_usd, 4),
            "high_usd": round(self.high_usd, 4),
            "measurement": self.measurement.value,
        }


def estimate_skill_run(prices: PriceTable = DEFAULT_PRICES, *, cached_context: bool = True) -> CostRange:
    """Forecast for one `computer_use` skill run, following §9's arithmetic."""
    low_input = SCREENSHOTS_PER_SKILL_LOW * SCREENSHOT_TOKENS_LOW + CONTEXT_TOKENS
    high_input = SCREENSHOTS_PER_SKILL_HIGH * SCREENSHOT_TOKENS_HIGH + CONTEXT_TOKENS
    cached = CONTEXT_TOKENS if cached_context else 0
    low = prices.cost(input_tokens=low_input, output_tokens=OUTPUT_TOKENS_LOW, cached_tokens=cached)
    high = prices.cost(input_tokens=high_input, output_tokens=OUTPUT_TOKENS_HIGH, cached_tokens=cached)
    return CostRange(low_usd=low, high_usd=high)


class CostAccumulator:
    """Adds up what a run actually consumed.

    Tokens reported by the driver are measured. If nothing reports tokens, the
    accumulator falls back to the estimated screenshot size and says so.
    """

    def __init__(self, prices: PriceTable = DEFAULT_PRICES):
        self.prices = prices
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self._any_measured = False
        self._any_estimated = False

    def add(self, *, input_tokens: int = 0, output_tokens: int = 0,
            cached_tokens: int = 0, measured: bool = True) -> None:
        self.input_tokens += max(input_tokens, 0)
        self.output_tokens += max(output_tokens, 0)
        self.cached_tokens += max(cached_tokens, 0)
        if measured:
            self._any_measured = True
        else:
            self._any_estimated = True

    def add_estimated_screenshot(self, count: int = 1) -> None:
        self.add(input_tokens=SCREENSHOT_TOKENS_MID * count, measured=False)

    @property
    def measurement(self) -> Measurement:
        # A total that mixes measured and estimated parts is reported as
        # estimated. Overstating precision is worse than admitting the mix.
        if self._any_estimated or not self._any_measured:
            return Measurement.ESTIMATED
        return Measurement.MEASURED

    def breakdown(self) -> CostBreakdown:
        return CostBreakdown(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens,
            cost_usd=self.prices.cost(
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                cached_tokens=self.cached_tokens,
            ),
            measurement=self.measurement,
        )
