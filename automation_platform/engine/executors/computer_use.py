"""`computer_use` executor.

This is the executor the whole system is bent around. Its job is to keep one
unit of work inside the short-horizon regime where these agents reach ~85%
instead of the ~20% they reach over long horizons.

What it enforces, in order, for every run:

1. The skill starts on its anchor screen (L1).
2. The screen still looks like it did at certification, or the run stops and the
   skill moves to `needs_review` — detecting the change beats adapting to it.
3. Every element is found through the selector cascade, and every fallback is
   recorded as a degradation.
4. Every action is verified (L2); one failure buys one retry, the second stops
   the run and escalates.
5. Screen text is quarantined as data. A dialog that reads like an order is
   reported, never followed.
6. The postcondition holds (L3), and the application is back on its anchor.
7. Never more than three screens. Crossing that ceiling mid-run is a hard stop,
   because it means the skill in production is not the skill that was certified.
"""

from __future__ import annotations

import time
from typing import Any

from ...domain.enums import ExecutionStatus, InterventionReason, StepStatus
from ...domain.errors import ProhibitedAction, SelectorNotFound
from ...security import risk as risk_policy
from ...security import screen_content
from .. import conditions, drift, selectors, validation
from ..drivers import PREVIEW_HALT, SAFE_DISMISS_NAMES, ScreenObservation, UiElement
from ..validation import ValidationTrace
from .base import Executor, RunContext, RunOutcome, StepRecord

# The constraint that defines the system (§1).
MAX_SCREENS = 3


class ComputerUseExecutor(Executor):
    name = "computer_use"

    def run(self, context: RunContext) -> RunOutcome:
        driver = context.driver
        trace = ValidationTrace()
        skill = context.skill
        artifacts: dict[str, Any] = {"files": {}}
        screens_seen: list[str] = []

        observation = self._observe(driver, context)
        screens_seen.append(observation.screen_key)

        # -- clear anything sitting on top of the screen ---------------------
        # A modal that appeared since the recording is not interface drift: the
        # application underneath has not changed. So it is dismissed safely
        # first, and only the screen under it is compared against the certified
        # fingerprint. Measuring drift with a dialog on top would report every
        # unexpected pop-up as a changed application.
        observation, dialog_failure = self._clear_dialog(context, observation, trace)
        if dialog_failure is not None:
            return self._abort(context, trace, dialog_failure[0], dialog_failure[1], screens_seen)

        # -- anchor and precondition (L1) -----------------------------------
        anchor = skill["start_anchor"] or ""
        if anchor and observation.screen_key != anchor:
            trace.add(
                validation.LayerResult(
                    validation.ValidationLayer.L1_PRECONDITION,
                    False,
                    "validation.anchor_mismatch",
                    {"expected": anchor, "actual": observation.screen_key},
                )
            )
            return self._abort(
                context, trace, "error.precondition_failed",
                InterventionReason.PRECONDITION_FAILED, screens_seen,
            )

        condition_context = conditions.context_from_observation(
            observation, anchor=anchor, artifacts=artifacts, variables=context.inputs
        )
        precondition = trace.add(
            validation.check_precondition(skill["precondition"], condition_context)
        )
        if not precondition.passed:
            return self._abort(
                context, trace, "error.precondition_failed",
                InterventionReason.PRECONDITION_FAILED, screens_seen,
            )

        # -- drift (§5) ------------------------------------------------------
        reading = drift.compare(observation, context.baseline)
        if reading.exceeded:
            trace.add(
                validation.LayerResult(
                    validation.ValidationLayer.L1_PRECONDITION,
                    False,
                    "error.drift_detected",
                    reading.summary(),
                )
            )
            outcome = self._abort(
                context, trace, "error.drift_detected",
                InterventionReason.DRIFT_DETECTED, screens_seen,
            )
            outcome.needs_review_reason = "error.drift_detected"
            outcome.outputs["drift"] = reading.summary()
            return outcome

        # -- steps -----------------------------------------------------------
        for position, step in enumerate(context.steps, start=1):
            record, observation, failure = self._run_step(
                context, step, position, observation, artifacts
            )
            context.emit(record)
            if observation.screen_key not in screens_seen:
                screens_seen.append(observation.screen_key)

            if len(screens_seen) > MAX_SCREENS:
                # The certified skill fits in three screens; this run does not.
                # That is a different skill, so it stops rather than continues.
                trace.add(
                    validation.LayerResult(
                        validation.ValidationLayer.L2_ACTION,
                        False,
                        "error.screen_budget",
                        {"screens": len(screens_seen)},
                    )
                )
                outcome = self._abort(
                    context, trace, "error.screen_budget",
                    InterventionReason.DRIFT_DETECTED, screens_seen,
                )
                outcome.needs_review_reason = "error.screen_budget"
                return outcome

            for entry in record.verification:
                trace.results.append(_as_layer_result(entry))

            if failure is not None:
                if failure[0] == PREVIEW_HALT:
                    # Not an error: the preview reached the point of no return
                    # and stopped, which is what it was asked to do.
                    return RunOutcome(
                        status=ExecutionStatus.WAITING_APPROVAL,
                        outputs={
                            "screens": screens_seen,
                            "proposed_action": getattr(driver, "proposed", []),
                            "preview": True,
                        },
                        trace=trace.to_list(),
                        cost=context.cost.breakdown(),
                        screens_observed=len(screens_seen),
                    )
                outcome = self._abort(context, trace, failure[0], failure[1], screens_seen)
                outcome.outputs["failed_step"] = position
                return outcome

        # -- postcondition (L3) ----------------------------------------------
        artifacts.update(driver.artifacts())
        artifacts.setdefault("files", {})
        condition_context = conditions.context_from_observation(
            observation, anchor=anchor, artifacts=artifacts, variables=context.inputs
        )
        postcondition = trace.add(
            validation.check_postcondition(skill["postcondition"], condition_context)
        )
        if not postcondition.passed:
            return self._abort(
                context, trace, "error.postcondition_failed",
                InterventionReason.POSTCONDITION_FAILED, screens_seen,
            )

        # -- back to the anchor ----------------------------------------------
        # Skills are stateless with respect to the screen: each one returns the
        # application to the anchor it started from, which is what lets a process
        # of eight skills stay eight short tasks (rule 1).
        if anchor and observation.screen_key != anchor:
            returned = self._return_to_anchor(context, anchor, observation)
            observation = returned or observation
            if observation.screen_key != anchor:
                trace.add(
                    validation.LayerResult(
                        validation.ValidationLayer.L3_POSTCONDITION,
                        False,
                        "validation.anchor_not_restored",
                        {"expected": anchor, "actual": observation.screen_key},
                    )
                )
                return self._abort(
                    context, trace, "validation.anchor_not_restored",
                    InterventionReason.POSTCONDITION_FAILED, screens_seen,
                )

        # -- semantic check (L4) ----------------------------------------------
        semantic = trace.add(
            validation.check_semantic(skill["semantic_check"], condition_context)
        )
        status = ExecutionStatus.SUCCEEDED
        intervention: InterventionReason | None = None
        if not semantic.passed:
            # L4 does not fail the run: it sends the result to the review queue.
            status = ExecutionStatus.ESCALATED
            intervention = InterventionReason.SEMANTIC_CHECK_FAILED

        return RunOutcome(
            status=status,
            outputs={
                "artifacts": artifacts,
                "screens": screens_seen,
                "assistance_only": not bool(skill["semantic_check"]),
            },
            trace=trace.to_list(),
            cost=context.cost.breakdown(),
            intervention=intervention,
            screens_observed=len(screens_seen),
        )

    # -- one step ------------------------------------------------------------

    def _run_step(
        self,
        context: RunContext,
        step,
        position: int,
        observation: ScreenObservation,
        artifacts: dict[str, Any],
    ) -> tuple[StepRecord, ScreenObservation, tuple[str, InterventionReason] | None]:
        """The step cycle of §4, with its single retry."""
        started = time.monotonic()
        action_type = step["action_type"]
        value = _render(step["value_template"], context.inputs)
        declared = selectors.parse_selectors(_json(step["selectors_json"]))

        record = StepRecord(
            position=position,
            screen_index=step["screen_index"],
            action={"type": action_type, "value": value},
            status=StepStatus.FAILED,
            screenshot_before=observation.screenshot_ref,
            accessibility_hash=observation.fingerprint(),
            skill_step_id=step["id"],
        )

        # Absolute prohibitions are checked before anything touches the machine.
        try:
            risk_policy.assert_allowed(action_type, value=value)
        except ProhibitedAction:
            record.detail = "error.prohibited_action"
            record.duration_ms = _elapsed(started)
            return record, observation, ("error.prohibited_action", InterventionReason.OPERATOR_STOP)

        # Screen text is inspected, never obeyed.
        report = screen_content.inspect(observation, strict=context.strict_screen_content)
        if report.quarantined.suspicious:
            record.verification.append(
                {
                    "layer": "l2_action",
                    "passed": not report.intervention_needed,
                    "detail_key": "execution.suspicious_content",
                    "detail_params": {
                        "severity": report.quarantined.severity,
                        "matches": list(report.quarantined.matches)[:3],
                    },
                    "policy": "escalate",
                    "skipped": False,
                }
            )
            if report.intervention_needed:
                record.detail = "execution.suspicious_content"
                record.duration_ms = _elapsed(started)
                return record, observation, (
                    "execution.suspicious_content",
                    InterventionReason.SCREEN_INSTRUCTION_ATTEMPT,
                )

        # An unexpected dialog blocks the flow. It is dismissed through a safe
        # affordance and never through the one it asks for: agreeing to a dialog
        # the platform did not expect is exactly the deception this guards
        # against. With no safe way out, the run stops (rule 3).
        if observation.dialog:
            dismissed = self._dismiss_dialog(context, observation)
            record.verification.append(
                {
                    "layer": "l2_action",
                    "passed": dismissed is not None,
                    "detail_key": "execution.dialog_dismissed" if dismissed
                    else "execution.dialog_blocking",
                    "detail_params": {"text": observation.dialog[:160]},
                    "policy": "escalate",
                    "skipped": False,
                }
            )
            if dismissed is None:
                record.detail = "execution.dialog_blocking"
                record.duration_ms = _elapsed(started)
                return record, observation, (
                    "execution.dialog_blocking",
                    InterventionReason.UNEXPECTED_DIALOG,
                )
            observation = dismissed

        failure: tuple[str, InterventionReason] | None = None
        attempts = 0
        while attempts < 2:
            attempts += 1
            element, resolution = self._locate(context, step, observation, declared)
            if element is None:
                failure = ("error.selector_not_found", InterventionReason.SELECTOR_NOT_FOUND)
                record.detail = "error.selector_not_found"
                if attempts < 2:
                    observation = self._observe(context.driver, context)
                    continue
                break

            record.selector_tier_used = int(resolution.tier_used) if resolution else None
            result = context.driver.act(action_type, element, value)
            context.cost.add(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                measured=True,
            )
            observation = result.observation or self._observe(context.driver, context)
            record.screenshot_after = observation.screenshot_ref

            if result.detail == PREVIEW_HALT:
                # The proposal stops here: describing the irreversible action is
                # the whole output of a preview run.
                record.status = StepStatus.SKIPPED
                record.detail = PREVIEW_HALT
                record.attempts = attempts
                record.duration_ms = _elapsed(started)
                return record, observation, (PREVIEW_HALT, InterventionReason.APPROVAL_REJECTED)

            artifacts.update(context.driver.artifacts())
            condition_context = conditions.context_from_observation(
                observation, artifacts=artifacts, variables=context.inputs
            )
            layer2 = validation.check_action(
                ok=result.ok,
                expected_value=step["expected_value"],
                context=condition_context,
            )
            record.verification.append(layer2.to_dict())

            if layer2.passed:
                record.status = StepStatus.RETRIED if attempts > 1 else StepStatus.PASSED
                failure = None
                break

            # Failed once: retry. Failed twice: stop and escalate (§4).
            record.status = StepStatus.FAILED
            record.detail = result.detail or "validation.action_rejected"
            failure = ("validation.action_rejected", InterventionReason.UNEXPECTED_DIALOG
                       if observation.dialog else InterventionReason.POSTCONDITION_FAILED)

        record.attempts = attempts
        record.duration_ms = _elapsed(started)
        record.input_tokens = context.cost.input_tokens
        record.output_tokens = context.cost.output_tokens
        return record, observation, failure

    # -- helpers -------------------------------------------------------------

    def _locate(self, context: RunContext, step, observation: ScreenObservation, declared):
        try:
            resolution = selectors.resolve(observation, declared)
        except SelectorNotFound:
            return None, None
        if resolution.degraded:
            # The degradation is recorded even when the run succeeds: three in a
            # week are what tell us the application moved under us.
            context.record_degradation(
                step_id=step["id"],
                from_tier=int(resolution.best_tier),
                to_tier=int(resolution.tier_used),
                marks_fragile=resolution.marks_fragile,
            )
        return resolution.element, resolution

    def _clear_dialog(self, context: RunContext, observation: ScreenObservation, trace):
        """Handle a dialog present before the run starts.

        Returns the observation to work from and, when the run cannot proceed,
        the failure to abort with.
        """
        if not observation.dialog:
            return observation, None

        report = screen_content.inspect(observation, strict=context.strict_screen_content)
        if report.intervention_needed:
            trace.add(
                validation.LayerResult(
                    validation.ValidationLayer.L1_PRECONDITION,
                    False,
                    "execution.suspicious_content",
                    {
                        "severity": report.quarantined.severity,
                        "matches": list(report.quarantined.matches)[:3],
                    },
                )
            )
            return observation, (
                "execution.suspicious_content",
                InterventionReason.SCREEN_INSTRUCTION_ATTEMPT,
            )

        dismissed = self._dismiss_dialog(context, observation)
        trace.add(
            validation.LayerResult(
                validation.ValidationLayer.L1_PRECONDITION,
                dismissed is not None,
                "execution.dialog_dismissed" if dismissed else "execution.dialog_blocking",
                {"text": observation.dialog[:160]},
            )
        )
        if dismissed is None:
            return observation, ("execution.dialog_blocking", InterventionReason.UNEXPECTED_DIALOG)
        return dismissed, None

    def _dismiss_dialog(self, context: RunContext, observation: ScreenObservation):
        """Close an unexpected dialog with a safe affordance, or give up."""
        for element in observation.elements:
            name = f"{element.name} {element.automation_id or ''}".strip().lower()
            if any(safe in name for safe in SAFE_DISMISS_NAMES):
                result = context.driver.act("click", element, None)
                context.cost.add(
                    input_tokens=result.input_tokens, output_tokens=result.output_tokens
                )
                if result.ok:
                    return result.observation or context.driver.observe()
        return None

    def _return_to_anchor(self, context: RunContext, anchor: str, observation: ScreenObservation):
        """One best-effort attempt to get the application back to its anchor."""
        candidate: UiElement | None = None
        for element in observation.elements:
            if element.role in ("button", "menuitem") and anchor.split(".")[-1] in (
                element.name or ""
            ).lower():
                candidate = element
                break
        if candidate is None:
            return None
        result = context.driver.act("click", candidate, None)
        context.cost.add(input_tokens=result.input_tokens, output_tokens=result.output_tokens)
        return result.observation

    def _observe(self, driver, context: RunContext) -> ScreenObservation:
        observation = driver.observe()
        context.cost.add(input_tokens=0, measured=True)
        return observation

    def _abort(self, context: RunContext, trace: ValidationTrace, error_key: str,
               intervention: InterventionReason, screens: list[str]) -> RunOutcome:
        """Fail closed: stop and escalate, never improvise (rule 3)."""
        return RunOutcome(
            status=ExecutionStatus.ESCALATED,
            trace=trace.to_list(),
            cost=context.cost.breakdown(),
            error_key=error_key,
            intervention=intervention,
            outputs={"screens": screens},
            screens_observed=len(screens),
        )


def _as_layer_result(entry: dict) -> validation.LayerResult:
    return validation.LayerResult(
        layer=validation.ValidationLayer(entry["layer"]),
        passed=entry["passed"],
        detail_key=entry.get("detail_key", ""),
        detail_params=entry.get("detail_params", {}),
        skipped=entry.get("skipped", False),
    )


def _json(raw: str | None) -> list:
    import json

    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _render(template: str | None, inputs: dict[str, Any]) -> str | None:
    """Fill `{parameter}` placeholders from the run inputs."""
    if not template:
        return None
    try:
        return template.format(**inputs)
    except (KeyError, IndexError, ValueError):
        return template


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
