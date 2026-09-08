"""Layered validation (§6).

| Layer | What it checks                        | On failure                |
|-------|---------------------------------------|---------------------------|
| L1    | Correct starting state                | Aborts before touching    |
| L2    | The click registered, the field took  | Retries once              |
| L3    | The expected artifact appeared        | Marks failed and escalates|
| L4    | The result is correct (domain check)  | Review queue              |
| L5    | Anything doubtful or irreversible     | Approval queue            |

Layer 4 is where domain-specific verification lives. When a skill has no L4
check it cannot be sold as a guaranteed result, only as assistance with human
review, and the product says so out loud (`skill.no_semantic_check`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.enums import FailurePolicy, RiskClass, ValidationLayer
from . import conditions
from .conditions import ConditionContext, ConditionError

LAYER_POLICY: dict[ValidationLayer, FailurePolicy] = {
    ValidationLayer.L1_PRECONDITION: FailurePolicy.ABORT,
    ValidationLayer.L2_ACTION: FailurePolicy.RETRY_ONCE,
    ValidationLayer.L3_POSTCONDITION: FailurePolicy.ESCALATE,
    ValidationLayer.L4_SEMANTIC: FailurePolicy.REVIEW_QUEUE,
    ValidationLayer.L5_HUMAN: FailurePolicy.APPROVAL_QUEUE,
}


@dataclass(frozen=True)
class LayerResult:
    """Outcome of one layer. `detail_key` is an i18n key, never a sentence."""

    layer: ValidationLayer
    passed: bool
    detail_key: str = ""
    detail_params: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

    @property
    def policy(self) -> FailurePolicy:
        return LAYER_POLICY[self.layer]

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "passed": self.passed,
            "skipped": self.skipped,
            "detail_key": self.detail_key,
            "detail_params": self.detail_params,
            "policy": self.policy.value,
        }


def _evaluate(expression: str | None, context: ConditionContext, layer: ValidationLayer,
              *, when_missing_passes: bool, missing_key: str) -> LayerResult:
    if not expression or not expression.strip():
        return LayerResult(layer, when_missing_passes, missing_key, skipped=True)
    try:
        passed = conditions.evaluate(expression, context)
    except ConditionError as exc:
        # An unevaluable condition is a failed condition. Fail closed (rule 3).
        return LayerResult(layer, False, "validation.not_evaluable", {"detail": str(exc)})
    return LayerResult(
        layer,
        passed,
        "validation.passed" if passed else "validation.failed",
        {"expression": expression},
    )


def check_precondition(expression: str | None, context: ConditionContext) -> LayerResult:
    """L1. A skill with no precondition still has to be on its anchor screen,
    which the runner checks separately."""
    return _evaluate(
        expression,
        context,
        ValidationLayer.L1_PRECONDITION,
        when_missing_passes=True,
        missing_key="validation.no_precondition",
    )


def check_action(*, ok: bool, expected_value: str | None, context: ConditionContext) -> LayerResult:
    """L2. Did the action register at all: did the click land, did the field
    accept the value."""
    if not ok:
        return LayerResult(ValidationLayer.L2_ACTION, False, "validation.action_rejected")
    if not expected_value:
        return LayerResult(ValidationLayer.L2_ACTION, True, "validation.action_ok", skipped=True)
    return _evaluate(
        expected_value,
        context,
        ValidationLayer.L2_ACTION,
        when_missing_passes=True,
        missing_key="validation.action_ok",
    )


def check_postcondition(expression: str | None, context: ConditionContext) -> LayerResult:
    """L3. Mandatory: a skill without a postcondition cannot be published, so an
    empty expression here is a failure, not a pass."""
    return _evaluate(
        expression,
        context,
        ValidationLayer.L3_POSTCONDITION,
        when_missing_passes=False,
        missing_key="validation.no_postcondition",
    )


def check_semantic(expression: str | None, context: ConditionContext) -> LayerResult:
    """L4. Optional by design. When absent the result is marked as assistance
    with human review rather than a guaranteed result."""
    if not expression or not expression.strip():
        return LayerResult(
            ValidationLayer.L4_SEMANTIC,
            True,
            "validation.no_semantic_check",
            skipped=True,
        )
    return _evaluate(
        expression,
        context,
        ValidationLayer.L4_SEMANTIC,
        when_missing_passes=True,
        missing_key="validation.no_semantic_check",
    )


def check_human(risk: RiskClass, *, approved: bool) -> LayerResult:
    """L5. Irreversible writes always need a prior human decision (§7)."""
    if not risk.requires_prior_approval:
        return LayerResult(ValidationLayer.L5_HUMAN, True, "validation.no_approval_needed", skipped=True)
    return LayerResult(
        ValidationLayer.L5_HUMAN,
        approved,
        "validation.approved" if approved else "validation.approval_pending",
    )


class ValidationTrace:
    """Ordered record of every layer checked during a step or a run.

    It is what the execution view renders and what the evidence export carries.
    """

    def __init__(self) -> None:
        self.results: list[LayerResult] = []

    def add(self, result: LayerResult) -> LayerResult:
        self.results.append(result)
        return result

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    def first_failure(self) -> LayerResult | None:
        for result in self.results:
            if not result.passed:
                return result
        return None

    def to_list(self) -> list[dict[str, Any]]:
        return [result.to_dict() for result in self.results]

    def layers_checked(self) -> list[ValidationLayer]:
        return [result.layer for result in self.results]
