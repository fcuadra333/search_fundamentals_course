"""Health metrics (§8).

The intervention rate is the main metric. If it rises for one skill, the target
application changed. If it rises for every skill of one application, that
application was updated.

Success rate matters too, but a skill that "succeeds" only because a human
rescues it every third run is not healthy, and the intervention rate is what
makes that visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain.enums import ExecutionStatus, Measurement

RISING_THRESHOLD = 0.05  # five points of intervention rate is a real move


@dataclass
class SkillHealth:
    skill_id: str
    name: str
    application_id: str | None
    status: str
    fragile: bool
    executions: int = 0
    successes: int = 0
    interventions: int = 0
    degradations: int = 0
    drift_detections: int = 0
    avg_cost_usd: float = 0.0
    cost_measurement: str = Measurement.ESTIMATED.value
    recent_intervention_rate: float = 0.0
    previous_intervention_rate: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.executions if self.executions else 0.0

    @property
    def intervention_rate(self) -> float:
        return self.interventions / self.executions if self.executions else 0.0

    @property
    def trend_key(self) -> str:
        delta = self.recent_intervention_rate - self.previous_intervention_rate
        if delta > RISING_THRESHOLD:
            return "health.trend_rising"
        if delta < -RISING_THRESHOLD:
            return "health.trend_falling"
        return "health.trend_stable"

    @property
    def needs_attention(self) -> bool:
        return (
            self.trend_key == "health.trend_rising"
            or self.fragile
            or self.drift_detections > 0
            or self.status == "needs_review"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "executions": self.executions,
            "success_rate": round(self.success_rate, 4),
            "intervention_rate": round(self.intervention_rate, 4),
            "degradations": self.degradations,
            "drift_detections": self.drift_detections,
            "avg_cost_usd": round(self.avg_cost_usd, 6),
            "cost_measurement": self.cost_measurement,
            "trend": self.trend_key,
        }


def skill_health(repos, skill_row) -> SkillHealth:
    organization_id = repos.tenant.organization_id
    skill_id = skill_row["id"]
    conn = repos.conn

    totals = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS successes, "
        "       AVG(cost_usd) AS avg_cost, "
        "       SUM(CASE WHEN cost_measurement = 'estimated' THEN 1 ELSE 0 END) AS estimated "
        "FROM executions WHERE organization_id = ? AND skill_id = ? AND effects_enabled = 1",
        (ExecutionStatus.SUCCEEDED.value, organization_id, skill_id),
    ).fetchone()

    interventions = conn.execute(
        "SELECT COUNT(*) AS total FROM interventions "
        "WHERE organization_id = ? AND skill_id = ?",
        (organization_id, skill_id),
    ).fetchone()["total"]

    recent, previous = _intervention_windows(conn, organization_id, skill_id)

    return SkillHealth(
        skill_id=skill_id,
        name=skill_row["name"],
        application_id=skill_row["application_id"],
        status=skill_row["status"],
        fragile=bool(skill_row["fragile"]),
        executions=totals["total"] or 0,
        successes=totals["successes"] or 0,
        interventions=interventions or 0,
        degradations=repos.degradations.count_last_week(skill_id),
        drift_detections=repos.drift.count_for_skill(skill_id),
        avg_cost_usd=totals["avg_cost"] or 0.0,
        # A mixed set of measured and estimated costs is reported as estimated.
        cost_measurement=(
            Measurement.ESTIMATED.value if (totals["estimated"] or 0) else Measurement.MEASURED.value
        ),
        recent_intervention_rate=recent,
        previous_intervention_rate=previous,
    )


def _intervention_windows(conn, organization_id: str, skill_id: str) -> tuple[float, float]:
    """Intervention rate over the last 7 days versus the 7 before that."""

    def rate(since: str, until: str | None) -> float:
        clause = "AND created_at >= datetime('now', ?)"
        params: list[Any] = [organization_id, skill_id, since]
        if until:
            clause += " AND created_at < datetime('now', ?)"
            params.append(until)
        runs = conn.execute(
            f"SELECT COUNT(*) AS total FROM executions WHERE organization_id = ? "
            f"AND skill_id = ? AND effects_enabled = 1 {clause}",
            params,
        ).fetchone()["total"]
        if not runs:
            return 0.0
        touched = conn.execute(
            f"SELECT COUNT(*) AS total FROM interventions WHERE organization_id = ? "
            f"AND skill_id = ? {clause}",
            params,
        ).fetchone()["total"]
        return touched / runs

    return rate("-7 days", None), rate("-14 days", "-7 days")


def organization_health(repos) -> list[SkillHealth]:
    return [skill_health(repos, row) for row in repos.skills.list()]


def application_alerts(repos) -> list[dict[str, Any]]:
    """Applications where every skill is trending worse: likely an update (§8)."""
    by_application: dict[str, list[SkillHealth]] = {}
    for health in organization_health(repos):
        if health.application_id and health.executions:
            by_application.setdefault(health.application_id, []).append(health)

    alerts = []
    for application_id, entries in by_application.items():
        if len(entries) < 2:
            continue
        if all(entry.trend_key == "health.trend_rising" for entry in entries):
            application = repos.applications.get(application_id)
            alerts.append(
                {
                    "application_id": application_id,
                    "application": application["name"] if application else application_id,
                    "skills": [entry.name for entry in entries],
                }
            )
    return alerts


def organization_summary(repos) -> dict[str, Any]:
    """Numbers for the dashboard header."""
    conn = repos.conn
    organization_id = repos.tenant.organization_id
    totals = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS successes, "
        "       AVG(cost_usd) AS avg_cost, "
        "       SUM(CASE WHEN cost_measurement = 'estimated' THEN 1 ELSE 0 END) AS estimated "
        "FROM executions WHERE organization_id = ? AND effects_enabled = 1",
        (ExecutionStatus.SUCCEEDED.value, organization_id),
    ).fetchone()
    interventions = conn.execute(
        "SELECT COUNT(*) AS total FROM interventions WHERE organization_id = ?",
        (organization_id,),
    ).fetchone()["total"]

    executions = totals["total"] or 0
    return {
        "executions": executions,
        "successes": totals["successes"] or 0,
        "success_rate": (totals["successes"] or 0) / executions if executions else 0.0,
        "interventions": interventions or 0,
        "intervention_rate": (interventions or 0) / executions if executions else 0.0,
        "avg_cost_usd": totals["avg_cost"] or 0.0,
        "cost_measurement": (
            Measurement.ESTIMATED.value if (totals["estimated"] or 0) else Measurement.MEASURED.value
        ),
        "queue": repos.executions.queue_counts(),
        "pending_approvals": len(repos.approvals.pending()),
    }
