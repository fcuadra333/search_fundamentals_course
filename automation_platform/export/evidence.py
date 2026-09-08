"""Evidence export (§8).

Same principle as an auditor's working papers: without exportable evidence there
is no product a company can buy. A report covers a range of runs and carries,
for every one of them, the steps, what each validation layer checked, the
interventions and the cost.

The export is bilingual like the rest of the product: headings and labels come
from the same catalog the interface uses, so a Spanish operator and an English
reviewer export the same rows under their own headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..db import loads
from ..db.database import utcnow
from ..domain.enums import ExecutionStatus, Measurement
from ..security import authz
from .pdf import Document
from .xlsx import Sheet, Workbook


@dataclass
class EvidenceRange:
    start: str
    end: str

    @classmethod
    def all_time(cls) -> "EvidenceRange":
        return cls("0000-01-01", "9999-12-31")


class EvidenceReport:
    """Collects a range of runs and renders it to a spreadsheet or a PDF."""

    def __init__(self, repos, translate: Callable[..., str], *, span: EvidenceRange | None = None):
        authz.require(repos.tenant, "execution.export")
        self.repos = repos
        self.t = translate
        self.span = span or EvidenceRange.all_time()

    # -- data ----------------------------------------------------------------

    def dataset(self) -> dict[str, Any]:
        executions = self.repos.executions.in_range(self.span.start, self.span.end)
        skills = {row["id"]: row for row in self.repos.skills.list()}
        rows, steps, interventions = [], [], []

        for execution in executions:
            skill = skills.get(execution["skill_id"])
            rows.append(
                {
                    "id": execution["id"],
                    "skill": skill["name"] if skill else "",
                    "status": execution["status"],
                    "trigger": execution["trigger"],
                    "effects": bool(execution["effects_enabled"]),
                    "started_at": execution["started_at"] or execution["created_at"],
                    "finished_at": execution["finished_at"] or "",
                    "duration_ms": execution["duration_ms"] or 0,
                    "input_tokens": execution["input_tokens"],
                    "output_tokens": execution["output_tokens"],
                    "cost_usd": execution["cost_usd"],
                    "cost_measurement": execution["cost_measurement"],
                    "error": execution["error_code"] or "",
                }
            )
            for step in self.repos.executions.steps(execution["id"]):
                verification = loads(step["verification_json"], []) or []
                steps.append(
                    {
                        "execution_id": execution["id"],
                        "position": step["position"],
                        "screen_index": step["screen_index"],
                        "action": (loads(step["action_json"], {}) or {}).get("type", ""),
                        "status": step["status"],
                        "attempts": step["attempts"],
                        "selector_tier": step["selector_tier_used"] or "",
                        "layers": ", ".join(
                            f"{entry['layer']}:{'ok' if entry['passed'] else 'x'}"
                            for entry in verification
                        ),
                        "screenshot_before": step["screenshot_before"] or "",
                        "screenshot_after": step["screenshot_after"] or "",
                        "duration_ms": step["duration_ms"],
                        "detail": step["detail"],
                    }
                )

        for row in self.repos.interventions.list(limit=1000):
            interventions.append(
                {
                    "id": row["id"],
                    "execution_id": row["execution_id"] or "",
                    "skill": (skills.get(row["skill_id"]) or {"name": ""})["name"]
                    if row["skill_id"] else "",
                    "reason": row["reason"],
                    "detail": row["detail"],
                    "marked_for_review": bool(row["marked_for_review"]),
                    "created_at": row["created_at"],
                }
            )

        succeeded = sum(1 for row in rows if row["status"] == ExecutionStatus.SUCCEEDED.value)
        total_cost = sum(row["cost_usd"] for row in rows)
        estimated = any(
            row["cost_measurement"] == Measurement.ESTIMATED.value for row in rows
        )
        return {
            "executions": rows,
            "steps": steps,
            "interventions": interventions,
            "summary": {
                "total": len(rows),
                "succeeded": succeeded,
                "success_rate": succeeded / len(rows) if rows else 0.0,
                "interventions": len(interventions),
                "intervention_rate": len(interventions) / len(rows) if rows else 0.0,
                "total_cost_usd": round(total_cost, 4),
                # A total mixing measured and estimated parts is reported as
                # estimated, never dressed up as measured.
                "cost_measurement": (
                    Measurement.ESTIMATED.value if estimated else Measurement.MEASURED.value
                ),
            },
        }

    # -- renderers -----------------------------------------------------------

    def to_xlsx(self, path: str) -> str:
        data = self.dataset()
        workbook = Workbook()
        workbook.add_sheet(
            Sheet(
                self.t("export.sheet_executions"),
                [
                    self.t("common.skill"), self.t("common.status"), self.t("common.trigger"),
                    self.t("common.started_at"), self.t("common.duration"),
                    self.t("common.tokens"), self.t("common.cost"),
                    self.t("common.measurement"), "id",
                ],
                [
                    [
                        row["skill"],
                        self.t(f"enum.ExecutionStatus.{row['status']}"),
                        self.t(f"enum.Trigger.{row['trigger']}"),
                        row["started_at"],
                        row["duration_ms"],
                        row["input_tokens"] + row["output_tokens"],
                        round(row["cost_usd"], 6),
                        self.t(f"enum.Measurement.{row['cost_measurement']}"),
                        row["id"],
                    ]
                    for row in data["executions"]
                ],
            )
        )
        workbook.add_sheet(
            Sheet(
                self.t("export.sheet_steps"),
                [
                    "id", self.t("common.step"), self.t("common.screens"),
                    self.t("common.actions"), self.t("common.status"),
                    self.t("common.attempts"), self.t("common.selector"),
                    self.t("execution.validation_layers"),
                    self.t("execution.evidence_before"), self.t("execution.evidence_after"),
                ],
                [
                    [
                        row["execution_id"], row["position"], row["screen_index"] + 1,
                        self.t(f"enum.ActionType.{row['action']}") if row["action"] else "",
                        self.t(f"enum.StepStatus.{row['status']}"),
                        row["attempts"], row["selector_tier"], row["layers"],
                        row["screenshot_before"], row["screenshot_after"],
                    ]
                    for row in data["steps"]
                ],
            )
        )
        workbook.add_sheet(
            Sheet(
                self.t("export.sheet_interventions"),
                [
                    self.t("common.created_at"), self.t("common.skill"),
                    self.t("common.reason"), self.t("common.detail"),
                    self.t("action.review"), "id",
                ],
                [
                    [
                        row["created_at"], row["skill"],
                        self.t(f"enum.InterventionReason.{row['reason']}"),
                        row["detail"],
                        self.t("common.yes") if row["marked_for_review"] else self.t("common.no"),
                        row["execution_id"],
                    ]
                    for row in data["interventions"]
                ],
            )
        )
        return workbook.save(path)

    def to_pdf(self, path: str) -> str:
        data = self.dataset()
        summary = data["summary"]
        organization = self.repos.organizations.get(self.repos.tenant.organization_id)

        document = Document(self.t("export.report_title"))
        document.heading(self.t("export.report_title"))
        document.paragraph(
            f"{organization['name'] if organization else ''} · "
            f"{self.t('export.generated_at', date=utcnow())}"
        )
        document.spacer(6)

        document.heading(self.t("export.summary"), size=12)
        document.key_value(self.t("health.executions"), summary["total"])
        document.key_value(self.t("health.success_rate"), f"{summary['success_rate']:.0%}")
        document.key_value(self.t("dashboard.intervention_rate"), f"{summary['intervention_rate']:.0%}")
        document.key_value(
            self.t("common.cost"),
            f"{summary['total_cost_usd']} USD "
            f"({self.t('enum.Measurement.' + summary['cost_measurement'])})",
        )
        document.spacer(10)

        document.heading(self.t("export.sheet_executions"), size=12)
        document.table(
            [self.t("common.skill"), self.t("common.status"), self.t("common.started_at"),
             self.t("common.cost")],
            [
                [
                    row["skill"],
                    self.t(f"enum.ExecutionStatus.{row['status']}"),
                    row["started_at"][:19].replace("T", " "),
                    f"{row['cost_usd']:.4f}",
                ]
                for row in data["executions"]
            ],
            widths=[190, 110, 130, 70],
        )

        if data["interventions"]:
            document.spacer(8)
            document.heading(self.t("export.sheet_interventions"), size=12)
            document.table(
                [self.t("common.created_at"), self.t("common.skill"), self.t("common.reason")],
                [
                    [
                        row["created_at"][:19].replace("T", " "),
                        row["skill"],
                        self.t(f"enum.InterventionReason.{row['reason']}"),
                    ]
                    for row in data["interventions"]
                ],
                widths=[150, 190, 160],
            )

        document.spacer(10)
        document.paragraph(self.t("app.footer"))
        return document.save(path)
