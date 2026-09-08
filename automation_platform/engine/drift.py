"""Interface drift detection (§5).

Before acting, the current screen fingerprint is compared against the one
recorded at certification. Above the threshold the run stops and the skill moves
to `needs_review`.

Detecting the change and reporting it is worth more than adapting silently. A
silent adaptation that is right 80% of the time is worse than a clean failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .drivers import ScreenObservation

# Fraction of the structural signature that may change before the run stops.
DEFAULT_DRIFT_THRESHOLD = 0.15


@dataclass(frozen=True)
class DriftReading:
    distance: float
    threshold: float
    baseline_size: int
    observed_size: int
    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def exceeded(self) -> bool:
        return self.distance > self.threshold

    def summary(self) -> dict:
        return {
            "distance": round(self.distance, 4),
            "threshold": self.threshold,
            "added": list(self.added)[:10],
            "removed": list(self.removed)[:10],
        }


def serialize_baseline(observation: ScreenObservation) -> str:
    """What certification stores: the screen key plus its structural signature."""
    return json.dumps(
        {
            "screen_key": observation.screen_key,
            "signature": sorted(observation.structural_signature()),
            "fingerprint": observation.fingerprint(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def deserialize_baseline(raw: str | None) -> tuple[str, frozenset[str]]:
    if not raw:
        return "", frozenset()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return "", frozenset()
    return payload.get("screen_key", ""), frozenset(payload.get("signature", []))


def compare(
    observation: ScreenObservation,
    baseline_raw: str | None,
    *,
    threshold: float = DEFAULT_DRIFT_THRESHOLD,
) -> DriftReading:
    """Jaccard distance between the certified signature and the current one."""
    _, baseline = deserialize_baseline(baseline_raw)
    observed = observation.structural_signature()

    if not baseline:
        # Nothing to compare against yet: no drift, and the caller decides
        # whether running without a baseline is acceptable.
        return DriftReading(0.0, threshold, 0, len(observed), (), ())

    union = baseline | observed
    intersection = baseline & observed
    distance = 0.0 if not union else 1.0 - (len(intersection) / len(union))
    return DriftReading(
        distance=distance,
        threshold=threshold,
        baseline_size=len(baseline),
        observed_size=len(observed),
        added=tuple(sorted(observed - baseline)),
        removed=tuple(sorted(baseline - observed)),
    )
