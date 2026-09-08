"""Orchestration.

The runner is what every executor shares: the stop switches, the rate ceiling,
the approval gate, the evidence trail, the intervention record and the health
flags. An executor only knows how to do the work; the runner is what makes the
work auditable and safe to repeat.

Approval flow for irreversible writes (§7). Approval has to come *before* the
write, and the approver has to see what the agent intends to do. So the first
pass runs with effects disabled: it produces a proposal and its evidence, the
execution parks in `waiting_approval`, and only an approved decision starts a
second pass that is allowed to touch the target system.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..db import dumps, loads, transaction
from ..db.database import utcnow
from ..domain.enums import (
    Executor as ExecutorKind,
    ExecutionStatus,
    InterventionReason,
    Measurement,
    RiskClass,
    SkillStatus,
    Trigger,
)
from ..domain.errors import (
    ApprovalRequired,
    NotCertified,
    PlatformError,
)
from ..security import authz, killswitch, rate_limit
from . import executors
from .cost import DEFAULT_PRICES, CostAccumulator, PriceTable
from .drift import DEFAULT_DRIFT_THRESHOLD
from .drivers import DriverMode
from .executors.base import RunContext, RunOutcome, StepRecord
from .selectors import DEGRADATION_REVIEW_THRESHOLD


@dataclass
class ExecutionResult:
    """What a caller gets back: the row id, the status and why."""

    execution_id: str
    status: ExecutionStatus
    outcome: RunOutcome | None = None
    approval_id: str | None = None
    message_key: str = ""
    message_params: dict[str, Any] = field(default_factory=dict)

    @property
    def waiting_approval(self) -> bool:
        return self.status is ExecutionStatus.WAITING_APPROVAL


class Runner:
    """Runs one skill, or a whole process, for one tenant."""

    def __init__(
        self,
        repos,
        *,
        driver_factory: Callable[[Any], Any] | None = None,
        prices: PriceTable = DEFAULT_PRICES,
        drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
    ):
        self.repos = repos
        self.tenant = repos.tenant
        self.driver_factory = driver_factory
        self.prices = prices
        self.drift_threshold = drift_threshold

    # ------------------------------------------------------------ one skill ---

    def run_skill(
        self,
        skill_id: str,
        *,
        inputs: dict[str, Any] | None = None,
        trigger: Trigger = Trigger.MANUAL,
        effects_enabled: bool = True,
        driver: Any = None,
        process_id: str | None = None,
        node_key: str | None = None,
        parent_execution_id: str | None = None,
    ) -> ExecutionResult:
        inputs = dict(inputs or {})
        skill = self.repos.skills.require(skill_id)
        dry_run = trigger is Trigger.DRY_RUN or not effects_enabled

        if not dry_run:
            authz.require(self.tenant, "process.run")

        # Stop switches, widest scope first (§7).
        killswitch.check(self.repos, process_id=process_id, skill_id=skill_id).raise_if_stopped()

        # Only a published skill runs in production. Dry runs are how a draft
        # gets certified, so they are allowed on any status.
        if not dry_run and skill["status"] != SkillStatus.PUBLISHED.value:
            raise NotCertified(skill=skill["name"], status=skill["status"])

        if not dry_run:
            rate_limit.check(self.repos, skill).raise_if_blocked(skill["name"])

        risk = RiskClass(skill["risk_classification"])
        needs_approval = risk.requires_prior_approval and not dry_run

        execution_id = self.repos.executions.create(
            skill_id=skill_id,
            process_id=process_id,
            node_key=node_key,
            parent_execution_id=parent_execution_id,
            trigger=trigger.value,
            status=ExecutionStatus.RUNNING.value,
            effects_enabled=0 if dry_run else 1,
            input_json=dumps(inputs),
            requested_by=self.tenant.user_id,
            started_at=utcnow(),
        )
        self.repos.audit.record("execution.start", "executions", execution_id,
                                skill_id=skill_id, trigger=trigger.value, dry_run=dry_run)

        if needs_approval:
            # Preview pass: same steps, no effects. Its evidence is what the
            # approver reads before deciding.
            preview = self._execute(
                skill, execution_id, inputs, effects_enabled=False, driver=driver, preview=True
            )
            approval_id = self.repos.approvals.request(
                execution_id=execution_id,
                skill_id=skill_id,
                proposed_action=skill["name"],
                evidence={
                    "inputs": inputs,
                    "trace": preview.trace,
                    "outputs": preview.outputs,
                    "proposed_action": preview.outputs.get("proposed_action", []),
                    "screens": preview.outputs.get("screens", []),
                    "estimated_cost": preview.cost.to_dict() if preview.cost else None,
                },
            )
            self.repos.executions.set_status(
                execution_id,
                ExecutionStatus.WAITING_APPROVAL,
                result_json=dumps(preview.outputs),
            )
            self.repos.audit.record("approval.requested", "approvals", approval_id,
                                    execution_id=execution_id)
            return ExecutionResult(
                execution_id=execution_id,
                status=ExecutionStatus.WAITING_APPROVAL,
                outcome=preview,
                approval_id=approval_id,
                message_key="error.approval_required",
            )

        outcome = self._execute(
            skill, execution_id, inputs, effects_enabled=not dry_run, driver=driver
        )
        self._finish(skill, execution_id, outcome)
        return ExecutionResult(execution_id=execution_id, status=outcome.status, outcome=outcome)

    # ------------------------------------------------------------- approvals ---

    def decide_approval(self, approval_id: str, *, approved: bool, comment: str = "") -> ExecutionResult:
        """Record the human decision and, when approved, run for real."""
        authz.require(self.tenant, "approval.decide")
        approval = self.repos.approvals.get(approval_id)
        if approval is None:
            raise ApprovalRequired(approval_id=approval_id)

        execution_id = approval["execution_id"]
        self.repos.approvals.decide(
            approval_id,
            approved=approved,
            decided_by=self.tenant.user_id or "",
            comment=comment,
        )
        self.repos.audit.record(
            "approval.decided", "approvals", approval_id,
            approved=approved, execution_id=execution_id,
        )

        if not approved:
            self.repos.executions.set_status(
                execution_id, ExecutionStatus.STOPPED,
                finished_at=utcnow(), error_code="approval.rejected",
            )
            self.repos.interventions.create(
                reason=InterventionReason.APPROVAL_REJECTED.value,
                skill_id=approval["skill_id"],
                execution_id=execution_id,
                actor_user_id=self.tenant.user_id,
                action_taken=comment,
            )
            return ExecutionResult(execution_id, ExecutionStatus.STOPPED, message_key="flash.rejected")

        return self.resume_approved(execution_id)

    def resume_approved(self, execution_id: str, *, driver: Any = None) -> ExecutionResult:
        """Second pass, with effects enabled, after an approval."""
        execution = self.repos.executions.require(execution_id)
        if not self.repos.approvals.has_approval(execution_id):
            raise ApprovalRequired(execution_id=execution_id)

        skill = self.repos.skills.require(execution["skill_id"])
        killswitch.check(self.repos, skill_id=skill["id"]).raise_if_stopped()

        self.repos.executions.set_status(execution_id, ExecutionStatus.RUNNING)
        inputs = loads(execution["input_json"], {}) or {}
        outcome = self._execute(skill, execution_id, inputs, effects_enabled=True, driver=driver)
        self._finish(skill, execution_id, outcome)
        return ExecutionResult(execution_id=execution_id, status=outcome.status, outcome=outcome)

    # ------------------------------------------------------------- processes ---

    def run_process(
        self,
        process_id: str,
        *,
        inputs: dict[str, Any] | None = None,
        trigger: Trigger = Trigger.MANUAL,
        driver: Any = None,
    ) -> ExecutionResult:
        """Walk the graph. Data travels along the edges; screen state never does.

        Each node is a full skill run with its own anchor, so an eight-node
        process is still eight short tasks rather than one long one.
        """
        authz.require(self.tenant, "process.run")
        process = self.repos.processes.get(process_id)
        if process is None:
            raise PlatformError("process not found")
        killswitch.check(self.repos, process_id=process_id).raise_if_stopped()

        parent_id = self.repos.executions.create(
            process_id=process_id,
            trigger=trigger.value,
            status=ExecutionStatus.RUNNING.value,
            input_json=dumps(inputs or {}),
            requested_by=self.tenant.user_id,
            started_at=utcnow(),
        )

        nodes = {row["node_key"]: row for row in self.repos.processes.nodes(process_id)}
        edges = self.repos.processes.edges(process_id)
        bus: dict[str, Any] = dict(inputs or {})
        results: list[dict[str, Any]] = []
        status = ExecutionStatus.SUCCEEDED

        for node_key in _traverse(nodes, edges):
            node = nodes[node_key]
            node_inputs = _map_inputs(node_key, edges, bus, inputs or {})
            try:
                result = self.run_skill(
                    node["skill_id"],
                    inputs=node_inputs,
                    trigger=trigger,
                    driver=driver,
                    process_id=process_id,
                    node_key=node_key,
                    parent_execution_id=parent_id,
                )
            except PlatformError as exc:
                status = ExecutionStatus.ESCALATED
                results.append({"node": node_key, "error": exc.message_key})
                break

            results.append({"node": node_key, "execution_id": result.execution_id,
                            "status": result.status.value})

            if result.waiting_approval:
                status = ExecutionStatus.WAITING_APPROVAL
                break
            if result.status is not ExecutionStatus.SUCCEEDED:
                status = result.status
                break
            if result.outcome:
                # Only structured data crosses the edge.
                bus[node_key] = result.outcome.outputs
                bus.update(result.outcome.outputs.get("summary", {}))

        self.repos.executions.set_status(
            parent_id, status,
            finished_at=utcnow(),
            result_json=dumps({"nodes": results}),
        )
        return ExecutionResult(execution_id=parent_id, status=status)

    # --------------------------------------------------------------- internals ---

    def _execute(
        self,
        skill,
        execution_id: str,
        inputs: dict[str, Any],
        *,
        effects_enabled: bool,
        driver: Any = None,
        preview: bool = False,
    ) -> RunOutcome:
        executor = executors.get(skill["executor"])
        steps = self.repos.skills.steps(skill["id"])
        position = {"value": 0}

        def record_step(record: StepRecord) -> None:
            position["value"] += 1
            self.repos.executions.add_step(
                execution_id,
                skill_step_id=record.skill_step_id,
                position=record.position or position["value"],
                screen_index=record.screen_index,
                action_json=dumps(record.action),
                screenshot_before=record.screenshot_before,
                screenshot_after=record.screenshot_after,
                accessibility_hash=record.accessibility_hash,
                selector_tier_used=record.selector_tier_used,
                verification_json=dumps(record.verification),
                status=record.status.value,
                attempts=record.attempts,
                duration_ms=record.duration_ms,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                detail=record.detail,
            )

        def record_degradation(*, step_id: str, from_tier: int, to_tier: int,
                               marks_fragile: bool = False) -> None:
            self.repos.degradations.record(
                skill_id=skill["id"],
                skill_step_id=step_id,
                execution_id=execution_id,
                from_tier=from_tier,
                to_tier=to_tier,
            )
            if marks_fragile:
                # Falling to pixel coordinates is what makes a skill fragile (§5).
                self.repos.skills.mark_fragile(skill["id"], True)
            if self.repos.degradations.count_last_week(skill["id"]) >= DEGRADATION_REVIEW_THRESHOLD:
                self.repos.skills.flag_for_review(skill["id"], "health.degradations")

        # PRODUCTION does the work; SANDBOX does it in a throwaway session;
        # PREVIEW walks up to the irreversible action and describes it.
        mode = (
            DriverMode.PRODUCTION if effects_enabled
            else DriverMode.PREVIEW if preview
            else DriverMode.SANDBOX
        )
        active_driver = driver or (self.driver_factory(skill) if self.driver_factory else None)
        if active_driver is not None and hasattr(active_driver, "mode"):
            active_driver.mode = mode

        certification = self.repos.certifications.latest(skill["id"])
        context = RunContext(
            skill=skill,
            steps=steps,
            inputs=inputs,
            execution_id=execution_id,
            effects_enabled=effects_enabled,
            driver=active_driver,
            baseline=certification["baseline_fingerprint"] if certification else None,
            record_step=record_step,
            record_degradation=record_degradation,
            cost=CostAccumulator(self.prices),
        )

        started = time.monotonic()
        outcome = executor.run(context)
        outcome.outputs.setdefault("duration_ms", int((time.monotonic() - started) * 1000))

        if skill["executor"] == ExecutorKind.COMPUTER_USE.value and active_driver is not None:
            self._check_application_version(skill, active_driver, execution_id)
        return outcome

    def _finish(self, skill, execution_id: str, outcome: RunOutcome) -> None:
        """Write the result, the cost, the intervention and any review flag."""
        with transaction(self.repos.conn):
            if outcome.cost:
                self.repos.executions.record_cost(
                    execution_id,
                    input_tokens=outcome.cost.input_tokens,
                    output_tokens=outcome.cost.output_tokens,
                    cached_tokens=outcome.cost.cached_tokens,
                    cost_usd=outcome.cost.cost_usd,
                    measurement=Measurement(outcome.cost.measurement.value),
                )

            self.repos.executions.set_status(
                execution_id,
                outcome.status,
                finished_at=utcnow(),
                duration_ms=outcome.outputs.get("duration_ms", 0),
                result_json=dumps({"outputs": outcome.outputs, "trace": outcome.trace}),
                error_code=outcome.error_key,
                error_detail=outcome.error_detail,
            )

            if outcome.intervention is not None:
                self.repos.interventions.create(
                    reason=outcome.intervention.value,
                    skill_id=skill["id"],
                    execution_id=execution_id,
                    detail=outcome.error_key or "",
                    marked_for_review=bool(outcome.needs_review_reason),
                )

            if outcome.needs_review_reason:
                self.repos.skills.flag_for_review(skill["id"], outcome.needs_review_reason)

            drift_summary = outcome.outputs.get("drift")
            if drift_summary:
                self.repos.drift.record(
                    skill_id=skill["id"],
                    execution_id=execution_id,
                    distance=float(drift_summary.get("distance", 0.0)),
                    threshold=float(drift_summary.get("threshold", self.drift_threshold)),
                )

            self.repos.audit.record(
                "execution.finish", "executions", execution_id,
                status=outcome.status.value, error=outcome.error_key,
            )

    def _check_application_version(self, skill, driver, execution_id: str) -> None:
        """A version change sends every bound skill to recertification (§5).

        This is the point where classic RPA failed for a decade: the application
        moved, nobody noticed, and the automation kept "working".
        """
        application_id = skill["application_id"]
        if not application_id:
            return
        application = self.repos.applications.get(application_id)
        if application is None:
            return
        observed = driver.app_version()
        if not observed or observed == application["detected_version"]:
            return

        previous = application["detected_version"]
        self.repos.applications.set_version(application_id, observed)
        self.repos.applications.set_health(application_id, "version_changed")
        affected = self.repos.skills.flag_application_for_review(
            application_id, "certification.app_version_changed"
        )
        self.repos.audit.record(
            "application.version_changed", "applications", application_id,
            previous=previous, current=observed, affected=len(affected),
            execution_id=execution_id,
        )


# ------------------------------------------------------------------ graph ---


def _traverse(nodes: dict[str, Any], edges) -> list[str]:
    """Topological order of the node graph, stable and cycle-safe."""
    incoming: dict[str, set[str]] = {key: set() for key in nodes}
    outgoing: dict[str, list[str]] = {key: [] for key in nodes}
    for edge in edges:
        source, target = edge["from_node_key"], edge["to_node_key"]
        if target not in nodes:
            continue
        if source and source in nodes:
            incoming[target].add(source)
            outgoing[source].append(target)

    ready = sorted(key for key, sources in incoming.items() if not sources)
    order: list[str] = []
    seen: set[str] = set()
    while ready:
        key = ready.pop(0)
        if key in seen:
            continue
        seen.add(key)
        order.append(key)
        for target in outgoing.get(key, []):
            incoming[target].discard(key)
            if not incoming[target] and target not in seen:
                ready.append(target)
                ready.sort()
    # A cycle would leave nodes out; run them last rather than dropping them.
    order.extend(sorted(key for key in nodes if key not in seen))
    return order


def _map_inputs(node_key: str, edges, bus: dict[str, Any],
                base_inputs: dict[str, Any]) -> dict[str, Any]:
    """What a node receives: the run parameters, plus what its edges carry.

    The process inputs are the parameters of the whole run — the period being
    reconciled, say — so every node sees them. Edge mappings are how one skill
    hands structured data to the next, and they win over the base inputs.
    """
    node_inputs: dict[str, Any] = dict(base_inputs)
    for edge in edges:
        if edge["to_node_key"] != node_key:
            continue
        mapping = loads(edge["data_mapping_json"], {}) or {}
        for target_key, source_path in mapping.items():
            node_inputs[target_key] = _read_path(bus, str(source_path))
    return node_inputs


def _read_path(bus: dict[str, Any], path: str) -> Any:
    current: Any = bus
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current
