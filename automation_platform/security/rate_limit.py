"""Runs-per-hour ceiling per skill (§7).

A runaway loop against a real ERP is both a cost problem and a blast-radius
problem, so the ceiling is stored on the skill and checked before every run.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.errors import RateLimitExceeded


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    def raise_if_blocked(self, skill_name: str = "") -> None:
        if not self.allowed:
            raise RateLimitExceeded(skill=skill_name, limit=self.limit, used=self.used)


def check(repos, skill_row) -> RateDecision:
    limit = int(skill_row["max_runs_per_hour"])
    used = repos.executions.runs_in_last_hour(skill_row["id"])
    return RateDecision(allowed=used < limit, used=used, limit=limit)
