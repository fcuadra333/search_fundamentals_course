"""`data_transform` executor.

This is where reconciliation lives, and it is the reason the platform treats a
reconciliation as one more task rather than a separate product: it is a process
whose `computer_use` skills export from the ERP and whose `data_transform`
skills reconcile the result.

Its verification is arithmetic or schema-based, which is exactly the L4 check
that `computer_use` skills usually cannot have. A skill with an arithmetic
postcondition can be sold as a guaranteed result; one without cannot.

Operations are declared per step: the step's `action_type` names the operation
and `value_template` carries its JSON configuration.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from ...domain.enums import ExecutionStatus, InterventionReason, StepStatus
from .. import conditions, validation
from ..validation import ValidationTrace
from .base import Executor, RunContext, RunOutcome, StepRecord


class DataTransformExecutor(Executor):
    name = "data_transform"

    def run(self, context: RunContext) -> RunOutcome:
        trace = ValidationTrace()
        skill = context.skill
        # Named row sets arrive either under `sets` or as plain list inputs, which
        # is what an edge of a process carries when it hands over an export.
        sets: dict[str, Any] = dict(context.inputs.get("sets", {}))
        for key, value in context.inputs.items():
            if key not in ("rows", "sets") and isinstance(value, list):
                sets.setdefault(key, value)
        state: dict[str, Any] = {
            "rows": list(context.inputs.get("rows", [])),
            "sets": sets,
            "summary": {},
        }

        condition_context = conditions.ConditionContext(
            variables=dict(context.inputs), rows=state["rows"]
        )
        precondition = trace.add(
            validation.check_precondition(skill["precondition"], condition_context)
        )
        if not precondition.passed:
            return RunOutcome(
                status=ExecutionStatus.ESCALATED,
                trace=trace.to_list(),
                cost=context.cost.breakdown(),
                error_key="error.precondition_failed",
                intervention=InterventionReason.PRECONDITION_FAILED,
            )

        for position, step in enumerate(context.steps, start=1):
            started = time.monotonic()
            operation = step["action_type"]
            config = _config(step["value_template"], context.inputs)
            record = StepRecord(
                position=position,
                action={"type": operation, "config": config},
                status=StepStatus.PASSED,
                skill_step_id=step["id"],
            )
            handler = OPERATIONS.get(operation)
            if handler is None:
                record.status = StepStatus.FAILED
                record.detail = "data_transform.unknown_operation"
                record.duration_ms = _elapsed(started)
                context.emit(record)
                return RunOutcome(
                    status=ExecutionStatus.ESCALATED,
                    trace=trace.to_list(),
                    cost=context.cost.breakdown(),
                    error_key="data_transform.unknown_operation",
                    error_detail=operation,
                    intervention=InterventionReason.SEMANTIC_CHECK_FAILED,
                )

            try:
                detail = handler(state, config)
            except (KeyError, TypeError, ValueError) as exc:
                record.status = StepStatus.FAILED
                record.detail = f"data_transform.operation_failed: {exc}"
                record.duration_ms = _elapsed(started)
                context.emit(record)
                return RunOutcome(
                    status=ExecutionStatus.ESCALATED,
                    trace=trace.to_list(),
                    cost=context.cost.breakdown(),
                    error_key="data_transform.operation_failed",
                    error_detail=str(exc),
                    intervention=InterventionReason.SEMANTIC_CHECK_FAILED,
                )

            record.detail = detail
            record.duration_ms = _elapsed(started)
            # No screenshots here, but the step log is the same shape so the
            # execution view and the evidence export do not special-case it.
            context.emit(record)

        condition_context = conditions.ConditionContext(
            variables={**context.inputs, **state["summary"]}, rows=state["rows"]
        )
        postcondition = trace.add(
            validation.check_postcondition(skill["postcondition"], condition_context)
        )
        if not postcondition.passed:
            return RunOutcome(
                status=ExecutionStatus.ESCALATED,
                trace=trace.to_list(),
                cost=context.cost.breakdown(),
                error_key="error.postcondition_failed",
                intervention=InterventionReason.POSTCONDITION_FAILED,
                outputs={"rows": state["rows"], "summary": state["summary"]},
            )

        semantic = trace.add(
            validation.check_semantic(skill["semantic_check"], condition_context)
        )
        status = ExecutionStatus.SUCCEEDED if semantic.passed else ExecutionStatus.ESCALATED

        return RunOutcome(
            status=status,
            outputs={"rows": state["rows"], "summary": state["summary"]},
            trace=trace.to_list(),
            cost=context.cost.breakdown(),
            intervention=None if semantic.passed else InterventionReason.SEMANTIC_CHECK_FAILED,
        )


# ------------------------------------------------------------- operations ---


def _op_load(state: dict, config: dict) -> str:
    """Load rows from a named set carried in by an edge of the process."""
    name = config["set"]
    state["rows"] = list(state["sets"].get(name, []))
    return f"loaded {len(state['rows'])} rows from {name}"


def _op_filter(state: dict, config: dict) -> str:
    column, value = config["column"], config.get("equals")
    before = len(state["rows"])
    state["rows"] = [row for row in state["rows"] if row.get(column) == value]
    return f"filtered {before} -> {len(state['rows'])}"


def _op_aggregate(state: dict, config: dict) -> str:
    column = config["column"]
    total = sum(_number(row.get(column)) for row in state["rows"])
    state["summary"][config.get("as", f"sum_{column}")] = round(total, 2)
    return f"sum({column}) = {round(total, 2)}"


def _op_reconcile(state: dict, config: dict) -> str:
    """Match two sets by key and report the differences.

    The arithmetic here is the whole point: the result is verifiable, so the
    skill can promise an outcome rather than assistance.
    """
    left_rows = state["sets"].get(config["left"], [])
    right_rows = state["sets"].get(config["right"], [])
    key = config.get("key", "id")
    amount = config.get("amount", "amount")
    tolerance = float(config.get("tolerance", 0.01))

    left = {row.get(key): row for row in left_rows}
    right = {row.get(key): row for row in right_rows}

    matched, differences, only_left, only_right = [], [], [], []
    for identifier, row in left.items():
        counterpart = right.get(identifier)
        if counterpart is None:
            only_left.append(row)
            continue
        gap = _number(row.get(amount)) - _number(counterpart.get(amount))
        if abs(gap) <= tolerance:
            matched.append(row)
        else:
            differences.append({**row, "difference": round(gap, 2)})
    for identifier, row in right.items():
        if identifier not in left:
            only_right.append(row)

    state["rows"] = differences + only_left + only_right
    state["summary"].update(
        {
            "matched": len(matched),
            "differences": len(differences),
            "only_left": len(only_left),
            "only_right": len(only_right),
            "left_count": len(left_rows),
            "right_count": len(right_rows),
            "left_total": round(sum(_number(row.get(amount)) for row in left_rows), 2),
            "right_total": round(sum(_number(row.get(amount)) for row in right_rows), 2),
        }
    )
    return (
        f"matched={len(matched)} differences={len(differences)} "
        f"only_left={len(only_left)} only_right={len(only_right)}"
    )


def _op_require_columns(state: dict, config: dict) -> str:
    """Schema check: every row carries the declared columns."""
    required = list(config["columns"])
    missing = [
        index
        for index, row in enumerate(state["rows"])
        if any(column not in row for column in required)
    ]
    if missing:
        raise ValueError(f"rows missing columns {required}: {missing[:5]}")
    state["summary"]["schema_ok"] = True
    return f"schema ok for {len(state['rows'])} rows"


def _op_project(state: dict, config: dict) -> str:
    columns = list(config["columns"])
    state["rows"] = [{column: row.get(column) for column in columns} for row in state["rows"]]
    return f"projected {columns}"


OPERATIONS: dict[str, Callable[[dict, dict], str]] = {
    "load": _op_load,
    "filter": _op_filter,
    "aggregate": _op_aggregate,
    "reconcile": _op_reconcile,
    "require_columns": _op_require_columns,
    "project": _op_project,
}


def _config(raw: str | None, inputs: dict[str, Any]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        rendered = raw.format(**inputs)
    except (KeyError, IndexError, ValueError):
        rendered = raw
    try:
        parsed = json.loads(rendered)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
