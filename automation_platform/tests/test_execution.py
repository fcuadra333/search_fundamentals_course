"""The step cycle, the three-screen ceiling, approvals and the kill switch."""

import unittest

from automation_platform.certification import Certifier
from automation_platform.domain.enums import ExecutionStatus, InterventionReason, SkillStatus
from automation_platform.domain.errors import (
    ExecutionStopped,
    NotCertified,
    ProhibitedAction,
    RateLimitExceeded,
)
from automation_platform.engine.drivers import DriverMode
from automation_platform.engine.executors.computer_use import MAX_SCREENS
from automation_platform.engine.runner import Runner
from automation_platform.security import risk as risk_policy
from automation_platform.simulation import erp_application, make_driver_factory

from .helpers import DEFAULT_INPUTS, erp_driver, erp_skill, fresh_database, make_organization


class RunTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.repos = make_organization(self.conn, "Acme", "acme")
        self.skill_id, self.application_id = erp_skill(self.repos)
        self.factory = make_driver_factory(self.repos)
        self.certifier = Certifier(self.repos, driver_factory=self.factory)

    def certify_and_publish(self, skill_id=None):
        skill_id = skill_id or self.skill_id
        report = self.certifier.certify(skill_id, risk_declared=True, cases=[DEFAULT_INPUTS])
        if report.publishable:
            self.certifier.publish(skill_id)
        return report

    def run_skill(self, skill_id=None, **kwargs):
        runner = Runner(self.repos, driver_factory=self.factory)
        kwargs.setdefault("inputs", DEFAULT_INPUTS)
        return runner.run_skill(skill_id or self.skill_id, **kwargs)


class StepCycleTests(RunTestCase):
    def test_a_published_skill_runs_and_verifies_every_layer(self):
        self.certify_and_publish()
        result = self.run_skill()
        self.assertEqual(result.status, ExecutionStatus.SUCCEEDED)
        layers = {entry["layer"] for entry in result.outcome.trace}
        self.assertIn("l1_precondition", layers)
        self.assertIn("l3_postcondition", layers)
        self.assertIn("l4_semantic", layers)

    def test_every_step_keeps_before_and_after_evidence(self):
        self.certify_and_publish()
        result = self.run_skill()
        steps = self.repos.executions.steps(result.execution_id)
        self.assertEqual(len(steps), 4)
        for step in steps:
            self.assertTrue(step["screenshot_before"])
            self.assertTrue(step["screenshot_after"])

    def test_the_run_ends_back_on_its_anchor(self):
        self.certify_and_publish()
        result = self.run_skill()
        self.assertEqual(result.outcome.outputs["screens"][0], "erp.main")
        self.assertEqual(result.outcome.screens_observed, 3)

    def test_an_unpublished_skill_does_not_run_in_production(self):
        with self.assertRaises(NotCertified):
            self.run_skill()

    def test_a_failing_action_is_retried_once_and_then_escalates(self):
        self.certify_and_publish()
        runner = Runner(self.repos, driver_factory=None)
        driver = erp_driver(failure_hook=lambda action, ref: ref == "btn.export")
        result = runner.run_skill(self.skill_id, inputs=DEFAULT_INPUTS, driver=driver)
        self.assertEqual(result.status, ExecutionStatus.ESCALATED)
        failed = [s for s in self.repos.executions.steps(result.execution_id) if s["status"] == "failed"]
        self.assertEqual(failed[0]["attempts"], 2)

    def test_a_missing_element_stops_instead_of_guessing(self):
        self.certify_and_publish()
        self.repos.skills.add_step(
            self.skill_id, position=5, screen_index=0, action_type="click",
            selectors=[{"tier": 1, "value": "noExiste"}],
        )
        result = self.run_skill()
        self.assertEqual(result.status, ExecutionStatus.ESCALATED)
        self.assertEqual(result.outcome.error_key, "error.selector_not_found")

    def test_a_wrong_starting_screen_aborts_before_touching_anything(self):
        self.certify_and_publish()
        runner = Runner(self.repos, driver_factory=None)
        driver = erp_driver()
        driver.current_screen = "erp.reports"
        result = runner.run_skill(self.skill_id, inputs=DEFAULT_INPUTS, driver=driver)
        self.assertEqual(result.status, ExecutionStatus.ESCALATED)
        self.assertEqual(result.outcome.error_key, "error.precondition_failed")
        self.assertEqual(len(self.repos.executions.steps(result.execution_id)), 0)

    def test_an_intervention_is_recorded_when_a_run_escalates(self):
        self.certify_and_publish()
        runner = Runner(self.repos, driver_factory=None)
        driver = erp_driver()
        driver.current_screen = "erp.reports"
        runner.run_skill(self.skill_id, inputs=DEFAULT_INPUTS, driver=driver)
        interventions = self.repos.interventions.list(skill_id=self.skill_id)
        self.assertEqual(interventions[0]["reason"], InterventionReason.PRECONDITION_FAILED.value)


class ScreenBudgetTests(RunTestCase):
    def test_the_ceiling_is_three(self):
        self.assertEqual(MAX_SCREENS, 3)

    def test_a_run_that_crosses_a_fourth_screen_stops_and_flags_the_skill(self):
        self.certify_and_publish()
        # A step that walks into a fourth screen: the skill in production is no
        # longer the skill that was certified.
        application = erp_application()
        application.screens["erp.main"].transitions[("click", "menu.entries")] = "erp.extra"
        from automation_platform.engine.drivers import ScriptedScreen, UiElement

        application.screens["erp.extra"] = ScriptedScreen(
            key="erp.extra", title="Extra",
            elements=(UiElement(ref="lbl", role="text", name="Extra", automation_id="lblExtra"),),
        )
        self.repos.skills.add_step(
            self.skill_id, position=5, screen_index=2, action_type="click",
            selectors=[{"tier": 1, "value": "menuAsientos"}],
        )
        from automation_platform.engine.drivers import SimulatedDesktopDriver

        runner = Runner(self.repos, driver_factory=None)
        result = runner.run_skill(
            self.skill_id, inputs=DEFAULT_INPUTS,
            driver=SimulatedDesktopDriver(application, mode=DriverMode.PRODUCTION),
        )
        self.assertEqual(result.outcome.error_key, "error.screen_budget")
        self.assertEqual(self.repos.skills.get(self.skill_id)["status"], SkillStatus.NEEDS_REVIEW.value)


class ApprovalFlowTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.repos.skills.update(self.skill_id, risk_classification="irreversible_write")
        self.certify_and_publish()

    def test_an_irreversible_run_parks_for_approval_without_acting(self):
        result = self.run_skill()
        self.assertTrue(result.waiting_approval)
        self.assertIsNotNone(result.approval_id)
        execution = self.repos.executions.get(result.execution_id)
        self.assertEqual(execution["status"], ExecutionStatus.WAITING_APPROVAL.value)

    def test_the_approver_sees_what_the_agent_intends_to_do(self):
        from automation_platform.db import loads

        result = self.run_skill()
        approval = self.repos.approvals.get(result.approval_id)
        evidence = loads(approval["evidence_json"], {})
        self.assertTrue(evidence["proposed_action"])
        self.assertEqual(evidence["proposed_action"][0]["element"], "btn.export")

    def test_approval_runs_the_skill_for_real(self):
        result = self.run_skill()
        runner = Runner(self.repos, driver_factory=self.factory)
        decided = runner.decide_approval(result.approval_id, approved=True)
        self.assertEqual(decided.status, ExecutionStatus.SUCCEEDED)

    def test_rejection_stops_the_run_and_records_the_intervention(self):
        result = self.run_skill()
        runner = Runner(self.repos, driver_factory=self.factory)
        decided = runner.decide_approval(result.approval_id, approved=False, comment="no")
        self.assertEqual(decided.status, ExecutionStatus.STOPPED)
        reasons = [row["reason"] for row in self.repos.interventions.list()]
        self.assertIn(InterventionReason.APPROVAL_REJECTED.value, reasons)


class OperationalControlTests(RunTestCase):
    def test_the_organization_kill_switch_stops_every_run(self):
        self.certify_and_publish()
        self.repos.organizations.set_stopped(self.repos.tenant.organization_id, True, "auditoría")
        with self.assertRaises(ExecutionStopped):
            self.run_skill()

    def test_the_skill_kill_switch_stops_only_that_skill(self):
        self.certify_and_publish()
        self.repos.skills.set_stopped(self.skill_id, True)
        with self.assertRaises(ExecutionStopped):
            self.run_skill()

    def test_the_hourly_ceiling_is_enforced(self):
        self.certify_and_publish()
        self.repos.skills.update(self.skill_id, max_runs_per_hour=2)
        self.run_skill()
        self.run_skill()
        with self.assertRaises(RateLimitExceeded):
            self.run_skill()

    def test_dry_runs_do_not_consume_the_ceiling(self):
        self.repos.skills.update(self.skill_id, max_runs_per_hour=1)
        report = self.certify_and_publish()
        self.assertEqual(report.dry_runs_passed, 10)

    def test_prohibited_actions_are_refused_before_reaching_the_machine(self):
        with self.assertRaises(ProhibitedAction):
            risk_policy.assert_allowed("click", value="Pagar factura")
        with self.assertRaises(ProhibitedAction):
            risk_policy.assert_allowed("delete_record")
        risk_policy.assert_allowed("click", value="Exportar")

    def test_a_version_change_sends_bound_skills_to_recertification(self):
        self.certify_and_publish()
        runner = Runner(self.repos, driver_factory=None)
        from automation_platform.engine.drivers import SimulatedDesktopDriver

        upgraded = SimulatedDesktopDriver(erp_application(version="13.0.0"),
                                          mode=DriverMode.PRODUCTION)
        runner.run_skill(self.skill_id, inputs=DEFAULT_INPUTS, driver=upgraded)
        skill = self.repos.skills.get(self.skill_id)
        self.assertEqual(skill["status"], SkillStatus.NEEDS_REVIEW.value)
        self.assertEqual(
            self.repos.applications.get(self.application_id)["health_status"], "version_changed"
        )


class DataTransformTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.repos = make_organization(self.conn, "Acme", "acme")
        self.skill_id = self.repos.skills.create(
            key="conciliar", name="Conciliar", executor="data_transform",
            precondition="len(erp_rows) > 0",
            postcondition="left_count > 0 and right_count > 0",
            semantic_check=("matched + differences + only_left == left_count "
                            "and matched + differences + only_right == right_count"),
            risk_classification="read_only", declared_screens=1,
        )
        self.repos.skills.add_step(
            self.skill_id, position=1, action_type="reconcile",
            value_template=('{{"left": "erp_rows", "right": "banco_rows", '
                            '"key": "id", "amount": "importe"}}'),
        )
        self.repos.certifications.create(
            skill_id=self.skill_id, skill_version=1, status="passed", screens_counted=1,
            postcondition_ok=1, dry_runs_total=10, dry_runs_passed=10,
            variation_resize_ok=1, variation_dialog_ok=1, risk_declared_ok=1,
        )
        self.repos.skills.set_status(self.skill_id, "published")

    def test_reconciliation_arithmetic_is_the_layer_four_check(self):
        runner = Runner(self.repos)
        result = runner.run_skill(self.skill_id, inputs={
            "erp_rows": [{"id": "A", "importe": 10}, {"id": "B", "importe": 20}],
            "banco_rows": [{"id": "A", "importe": 10}, {"id": "C", "importe": 5}],
        })
        self.assertEqual(result.status, ExecutionStatus.SUCCEEDED)
        summary = result.outcome.outputs["summary"]
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["only_left"], 1)
        self.assertEqual(summary["only_right"], 1)

    def test_amount_differences_are_reported_not_hidden(self):
        runner = Runner(self.repos)
        result = runner.run_skill(self.skill_id, inputs={
            "erp_rows": [{"id": "A", "importe": 100}],
            "banco_rows": [{"id": "A", "importe": 90}],
        })
        self.assertEqual(result.outcome.outputs["summary"]["differences"], 1)
        self.assertEqual(result.outcome.outputs["rows"][0]["difference"], 10)


if __name__ == "__main__":
    unittest.main()
