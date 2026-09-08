"""Executor interface.

A process is a graph of skills; each skill has an executor. Orchestration,
validation, the dashboard and the audit trail are common to every executor, so
an executor only has to answer one question: given this skill definition and
these inputs, what happened, and what is the evidence.

Precedence rule (§4): if an API exists, use the API. Computer use is the last
resort, not the first. Every `computer_use` skill is maintenance debt, and the
skill detail view says so.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ...domain.enums import ExecutionStatus, InterventionReason, StepStatus
from ..cost import CostAccumulator, CostBreakdown


@dataclass
class StepRecord:
    """One executed step, with its before/after evidence."""

    position: int
    action: dict[str, Any]
    status: StepStatus
    screen_index: int = 0
    screenshot_before: str | None = None
    screenshot_after: str | None = None
    accessibility_hash: str | None = None
    selector_tier_used: int | None = None
    verification: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 1
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    detail: str = ""
    skill_step_id: str | None = None


@dataclass
class RunContext:
    """Everything an executor is allowed to see."""

    skill: sqlite3.Row
    steps: list[sqlite3.Row]
    inputs: dict[str, Any]
    execution_id: str
    effects_enabled: bool = True
    driver: Any = None
    baseline: str | None = None
    record_step: Callable[[StepRecord], None] = lambda record: None
    # Called every time the selector cascade had to fall back a tier. The runner
    # is what turns three of these in a week into a review flag (§5).
    record_degradation: Callable[..., None] = lambda **kwargs: None
    cost: CostAccumulator = field(default_factory=CostAccumulator)
    strict_screen_content: bool = True

    def emit(self, record: StepRecord) -> StepRecord:
        """Persist evidence as it happens, not at the end.

        A run that dies halfway still has to leave its working papers behind.
        """
        self.record_step(record)
        return record


@dataclass
class RunOutcome:
    """What the runner writes to the execution row."""

    status: ExecutionStatus
    outputs: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    cost: CostBreakdown | None = None
    error_key: str | None = None
    error_detail: str = ""
    intervention: InterventionReason | None = None
    needs_review_reason: str = ""
    screens_observed: int = 0
    approval_request: dict[str, Any] | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.SUCCEEDED


class Executor(Protocol):
    name: str

    def run(self, context: RunContext) -> RunOutcome: ...
