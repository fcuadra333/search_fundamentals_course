"""Immediate stop switch, per organization, per process and per skill (§7)."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.errors import ExecutionStopped


@dataclass(frozen=True)
class StopState:
    stopped: bool
    scope: str = ""
    reason: str = ""

    def raise_if_stopped(self) -> None:
        if self.stopped:
            raise ExecutionStopped(scope=self.scope, reason=self.reason)


def check(repos, *, process_id: str | None = None, skill_id: str | None = None) -> StopState:
    """Check every scope, widest first. Any one of them stops the run."""
    organization = repos.organizations.get(repos.tenant.organization_id)
    if organization is not None and organization["stopped"]:
        return StopState(True, "organization", organization["stopped_reason"] or "")

    if process_id:
        process = repos.processes.get(process_id)
        if process is not None and process["stopped"]:
            return StopState(True, "process", process["name"])

    if skill_id:
        skill = repos.skills.get(skill_id)
        if skill is not None and skill["stopped"]:
            return StopState(True, "skill", skill["name"])

    return StopState(False)
