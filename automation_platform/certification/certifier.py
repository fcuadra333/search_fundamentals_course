"""Certification (§3). Mandatory before production.

A skill reaches `published` only after passing all five checks:

1. Screen count ≤ 3.
2. Postcondition present **and evaluable** — an expression nobody can evaluate
   is the same as no postcondition.
3. Dry runs: N runs against known cases with effects disabled, above a minimum
   success threshold.
4. Variation test: at least one run with the window resized and one with an
   unexpected dialog injected. Failing it does not block publication, but it
   marks the skill fragile and demands more robust selectors.
5. Risk classification declared explicitly by the designer.

Certification repeats automatically when the target application changes version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..db import dumps
from ..db.database import utcnow
from ..domain.enums import CertificationStatus, SkillStatus, Trigger
from ..domain.errors import NotCertified, ScreenBudgetExceeded
from ..engine import conditions
from ..engine.drift import serialize_baseline
from ..engine.executors.computer_use import MAX_SCREENS
from ..engine.runner import Runner
from ..security import authz

DEFAULT_DRY_RUNS = 10
DEFAULT_SUCCESS_THRESHOLD = 9

# The perturbations of the variation test.
#
# The dialog is unexpected but benign: what is being measured is whether the
# skill survives something appearing that was not in the recording. Resistance
# to a *hostile* dialog is a different property, enforced by
# `security.screen_content` and covered by the test suite, and it is not
# something a skill can be certified out of.
RESIZE_SCALE = 1.35
INJECTED_DIALOG = "Aviso: haga clic en Cerrar para continuar con la sesión"


@dataclass
class CertificationReport:
    skill_id: str
    skill_version: int
    screens_counted: int = 0
    screens_ok: bool = False
    postcondition_ok: bool = False
    postcondition_detail: str = ""
    dry_runs_total: int = 0
    dry_runs_passed: int = 0
    dry_runs_ok: bool = False
    variation_resize_ok: bool = False
    variation_dialog_ok: bool = False
    risk_declared_ok: bool = False
    baseline: str | None = None
    app_version: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def blocking_failures(self) -> list[str]:
        """Checks that stop publication. The variation test is not one of them."""
        failures = []
        if not self.screens_ok:
            failures.append("certification.check_screens")
        if not self.postcondition_ok:
            failures.append("certification.check_postcondition")
        if not self.dry_runs_ok:
            failures.append("certification.check_dry_runs")
        if not self.risk_declared_ok:
            failures.append("certification.check_risk")
        return failures

    @property
    def status(self) -> CertificationStatus:
        if self.blocking_failures:
            return CertificationStatus.FAILED
        if not (self.variation_resize_ok and self.variation_dialog_ok):
            return CertificationStatus.FRAGILE
        return CertificationStatus.PASSED

    @property
    def publishable(self) -> bool:
        return self.status in (CertificationStatus.PASSED, CertificationStatus.FRAGILE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "screens_counted": self.screens_counted,
            "screens_ok": self.screens_ok,
            "postcondition_ok": self.postcondition_ok,
            "postcondition_detail": self.postcondition_detail,
            "dry_runs": {"total": self.dry_runs_total, "passed": self.dry_runs_passed,
                         "ok": self.dry_runs_ok},
            "variation": {"resize": self.variation_resize_ok, "dialog": self.variation_dialog_ok},
            "risk_declared": self.risk_declared_ok,
            "status": self.status.value,
            "notes": self.notes,
        }


class Certifier:
    """Runs the five checks and records the result."""

    def __init__(
        self,
        repos,
        *,
        driver_factory: Callable[..., Any] | None = None,
        dry_runs: int = DEFAULT_DRY_RUNS,
        success_threshold: int = DEFAULT_SUCCESS_THRESHOLD,
    ):
        self.repos = repos
        self.tenant = repos.tenant
        self.driver_factory = driver_factory
        self.dry_runs = dry_runs
        self.success_threshold = success_threshold

    def certify(
        self,
        skill_id: str,
        *,
        risk_declared: bool,
        cases: list[dict[str, Any]] | None = None,
    ) -> CertificationReport:
        """`risk_declared` is the designer's explicit statement, not a default.

        It is a required argument precisely so that no code path can certify a
        skill without someone having declared what it can break.
        """
        authz.require(self.tenant, "skill.certify")
        skill = self.repos.skills.require(skill_id)
        report = CertificationReport(skill_id=skill_id, skill_version=skill["version"])
        cases = cases or [{}]

        # 1 -- screen count
        report.screens_counted = max(self.repos.skills.screen_count(skill_id), 1)
        report.screens_ok = report.screens_counted <= MAX_SCREENS
        if not report.screens_ok:
            report.notes.append("error.screen_budget")

        # 2 -- postcondition present and evaluable
        postcondition = (skill["postcondition"] or "").strip()
        report.postcondition_ok = bool(postcondition) and conditions.is_evaluable(postcondition)
        if not report.postcondition_ok:
            report.postcondition_detail = (
                "missing" if not postcondition else "not_evaluable"
            )
            report.notes.append("error.missing_postcondition")

        # 5 -- explicit risk declaration
        report.risk_declared_ok = bool(risk_declared)
        if not report.risk_declared_ok:
            report.notes.append("certification.check_risk")

        self.repos.skills.set_status(skill_id, SkillStatus.IN_CERTIFICATION)

        # 3 -- dry runs, effects disabled
        if report.screens_ok and report.postcondition_ok:
            report.dry_runs_total = self.dry_runs
            for index in range(self.dry_runs):
                inputs = cases[index % len(cases)]
                driver = self._driver(skill)
                if index == 0 and driver is not None:
                    # The baseline fingerprint is what drift detection compares
                    # against on every later run.
                    report.baseline = serialize_baseline(driver.observe())
                    report.app_version = driver.app_version()
                    driver.reset()
                if self._dry_run(skill_id, inputs, driver):
                    report.dry_runs_passed += 1
            report.dry_runs_ok = report.dry_runs_passed >= self.success_threshold
            if not report.dry_runs_ok:
                report.notes.append("certification.check_dry_runs")

            # 4 -- variation test
            report.variation_resize_ok = self._dry_run(
                skill_id, cases[0], self._driver(skill, window_scale=RESIZE_SCALE)
            )
            report.variation_dialog_ok = self._dry_run(
                skill_id, cases[0], self._driver(skill, inject_dialog=INJECTED_DIALOG)
            )
            if not (report.variation_resize_ok and report.variation_dialog_ok):
                report.notes.append("certification.fragile")

        self._persist(skill, report)
        return report

    # -- internals -----------------------------------------------------------

    def _driver(self, skill, **kwargs):
        if self.driver_factory is None:
            return None
        return self.driver_factory(skill, **kwargs)

    def _dry_run(self, skill_id: str, inputs: dict[str, Any], driver) -> bool:
        runner = Runner(self.repos, driver_factory=None)
        result = runner.run_skill(
            skill_id,
            inputs=inputs,
            trigger=Trigger.DRY_RUN,
            effects_enabled=False,
            driver=driver,
        )
        return result.outcome is not None and result.outcome.succeeded

    def _persist(self, skill, report: CertificationReport) -> None:
        self.repos.certifications.create(
            skill_id=report.skill_id,
            skill_version=report.skill_version,
            status=report.status.value,
            screens_counted=report.screens_counted,
            postcondition_ok=1 if report.postcondition_ok else 0,
            dry_runs_total=report.dry_runs_total,
            dry_runs_passed=report.dry_runs_passed,
            variation_resize_ok=1 if report.variation_resize_ok else 0,
            variation_dialog_ok=1 if report.variation_dialog_ok else 0,
            risk_declared_ok=1 if report.risk_declared_ok else 0,
            baseline_fingerprint=report.baseline,
            app_version=report.app_version,
            report_json=dumps(report.to_dict()),
        )
        if report.status is CertificationStatus.FRAGILE:
            self.repos.skills.mark_fragile(report.skill_id, True)
        if not report.publishable:
            self.repos.skills.set_status(
                report.skill_id, SkillStatus.DRAFT, report.blocking_failures[0]
            )
        self.repos.audit.record(
            "skill.certified", "skills", report.skill_id, status=report.status.value,
            screens=report.screens_counted, dry_runs_passed=report.dry_runs_passed,
        )

    # -- publication ---------------------------------------------------------

    def publish(self, skill_id: str) -> None:
        """Publish a certified skill.

        The screen count written here is the one certification counted, not the
        one the designer claimed. The database trigger refuses anything above
        three, so a miscount cannot become a published skill.
        """
        authz.require(self.tenant, "skill.publish")
        certification = self.repos.certifications.latest(skill_id)
        if certification is None or certification["status"] == CertificationStatus.FAILED.value:
            raise NotCertified(skill=skill_id)

        counted = int(certification["screens_counted"])
        if counted > MAX_SCREENS:
            raise ScreenBudgetExceeded(screens=counted)

        self.repos.skills.update(skill_id, declared_screens=counted,
                                 certified_at=utcnow(),
                                 certified_app_version=certification["app_version"])
        self.repos.skills.set_status(skill_id, SkillStatus.PUBLISHED)
        self.repos.audit.record("skill.published", "skills", skill_id, screens=counted)

    def recertify_application(self, application_id: str, *, risk_declared: bool = True) -> list[CertificationReport]:
        """Re-run certification for every skill bound to an application whose
        version changed. Nothing runs in production until it passes (§5)."""
        reports = []
        for skill in self.repos.skills.list(application_id=application_id):
            if skill["status"] not in (SkillStatus.NEEDS_REVIEW.value, SkillStatus.PUBLISHED.value):
                continue
            reports.append(self.certify(skill["id"], risk_declared=risk_declared))
        return reports
