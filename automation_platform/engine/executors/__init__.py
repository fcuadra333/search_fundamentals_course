"""Executor registry.

Version 1 ships `computer_use` and `data_transform`. `api_call` and
`llm_extract` exist in the data model because the abstraction has to be right
from the start, but asking for one raises rather than pretending (§10).
"""

from __future__ import annotations

from ...domain.enums import Executor as ExecutorKind
from ...domain.errors import ExecutorUnavailable
from .base import Executor, RunContext, RunOutcome, StepRecord
from .computer_use import ComputerUseExecutor
from .data_transform import DataTransformExecutor

REGISTRY: dict[str, Executor] = {
    ExecutorKind.COMPUTER_USE.value: ComputerUseExecutor(),
    ExecutorKind.DATA_TRANSFORM.value: DataTransformExecutor(),
}


def get(executor: str) -> Executor:
    try:
        return REGISTRY[executor]
    except KeyError:
        raise ExecutorUnavailable(executor=executor) from None


__all__ = [
    "Executor",
    "RunContext",
    "RunOutcome",
    "StepRecord",
    "ComputerUseExecutor",
    "DataTransformExecutor",
    "REGISTRY",
    "get",
]
