"""The data-model invariants the specification calls non-negotiable (§2).

Each one is enforced by a database trigger, so these tests go through the
repositories but the refusal comes from the schema.
"""

import unittest

from automation_platform.domain.errors import InvariantViolation

from .helpers import erp_skill, fresh_database, make_organization


class PublicationInvariantTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.repos = make_organization(self.conn, "Acme", "acme")
        self.skill_id, _ = erp_skill(self.repos)

    def _certify(self, **overrides):
        fields = {
            "skill_id": self.skill_id, "skill_version": 1, "status": "passed",
            "screens_counted": 3, "postcondition_ok": 1, "dry_runs_total": 10,
            "dry_runs_passed": 10, "variation_resize_ok": 1, "variation_dialog_ok": 1,
            "risk_declared_ok": 1,
        }
        fields.update(overrides)
        return self.repos.certifications.create(**fields)

    def test_cannot_publish_without_certification(self):
        with self.assertRaises(InvariantViolation) as caught:
            self.repos.skills.set_status(self.skill_id, "published")
        self.assertEqual(caught.exception.message_key, "error.not_certified")

    def test_cannot_publish_without_postcondition(self):
        self._certify()
        self.repos.skills.update(self.skill_id, postcondition="   ")
        with self.assertRaises(InvariantViolation) as caught:
            self.repos.skills.set_status(self.skill_id, "published")
        self.assertEqual(caught.exception.message_key, "error.missing_postcondition")

    def test_cannot_publish_beyond_three_screens(self):
        self._certify()
        # The column check refuses the value outright; the publication trigger is
        # the second line of defence for a value written another way.
        with self.assertRaises(Exception):
            self.repos.skills.update(self.skill_id, declared_screens=4)

    def test_publishes_once_every_check_is_satisfied(self):
        self._certify()
        self.repos.skills.set_status(self.skill_id, "published")
        self.assertEqual(self.repos.skills.get(self.skill_id)["status"], "published")

    def test_certification_of_another_version_does_not_publish_this_one(self):
        self._certify(skill_version=2)
        with self.assertRaises(InvariantViolation):
            self.repos.skills.set_status(self.skill_id, "published")

    def test_failed_certification_does_not_publish(self):
        self._certify(status="failed")
        with self.assertRaises(InvariantViolation):
            self.repos.skills.set_status(self.skill_id, "published")

    def test_fragile_certification_still_publishes(self):
        # Failing the variation test marks a skill fragile and demands better
        # selectors; it does not block publication (§3).
        self._certify(status="fragile", variation_dialog_ok=0)
        self.repos.skills.set_status(self.skill_id, "published")
        self.assertEqual(self.repos.skills.get(self.skill_id)["status"], "published")


class ApprovalInvariantTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.repos = make_organization(self.conn, "Acme", "acme")
        self.skill_id, _ = erp_skill(self.repos, risk_classification="irreversible_write")
        self.execution_id = self.repos.executions.create(
            skill_id=self.skill_id, trigger="manual", status="running"
        )

    def test_irreversible_run_cannot_succeed_without_approval(self):
        with self.assertRaises(InvariantViolation) as caught:
            self.repos.executions.set_status(self.execution_id, "succeeded")
        self.assertEqual(caught.exception.message_key, "error.approval_required")

    def test_rejected_approval_is_not_an_approval(self):
        approval_id = self.repos.approvals.request(
            execution_id=self.execution_id, skill_id=self.skill_id, proposed_action="enviar"
        )
        self.repos.approvals.decide(approval_id, approved=False, decided_by=self.repos.tenant.user_id)
        with self.assertRaises(InvariantViolation):
            self.repos.executions.set_status(self.execution_id, "succeeded")

    def test_approved_run_may_succeed(self):
        approval_id = self.repos.approvals.request(
            execution_id=self.execution_id, skill_id=self.skill_id, proposed_action="enviar"
        )
        self.repos.approvals.decide(approval_id, approved=True, decided_by=self.repos.tenant.user_id)
        self.repos.executions.set_status(self.execution_id, "succeeded")
        self.assertEqual(self.repos.executions.get(self.execution_id)["status"], "succeeded")

    def test_dry_run_needs_no_approval(self):
        dry = self.repos.executions.create(
            skill_id=self.skill_id, trigger="dry_run", status="running", effects_enabled=0
        )
        self.repos.executions.set_status(dry, "succeeded")
        self.assertEqual(self.repos.executions.get(dry)["status"], "succeeded")


class AuditImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.repos = make_organization(self.conn, "Acme", "acme")
        self.entry_id = self.repos.audit.record("skill.publish", "skills", "skl_1")

    def test_entries_cannot_be_updated(self):
        with self.assertRaises(InvariantViolation):
            self.conn.execute(
                "UPDATE audit_log SET action = 'x' WHERE organization_id = ?",
                (self.repos.tenant.organization_id,),
            )

    def test_entries_cannot_be_deleted(self):
        with self.assertRaises(InvariantViolation):
            self.conn.execute(
                "DELETE FROM audit_log WHERE organization_id = ?",
                (self.repos.tenant.organization_id,),
            )

    def test_entries_can_be_read(self):
        self.assertEqual(len(self.repos.audit.list()), 1)


class SchemaGuardTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.repos = make_organization(self.conn, "Acme", "acme")

    def test_unknown_column_is_refused_rather_than_silently_dropped(self):
        with self.assertRaises(ValueError):
            self.repos.skills.create(
                key="k", name="n", executor="computer_use", risk_classification="read_only",
                postcondition="screen_is('x')", nonexistent_column="oops",
            )

    def test_child_rows_cannot_point_at_another_tenant(self):
        other = make_organization(self.conn, "Beta", "beta")
        skill_id, _ = erp_skill(other)
        with self.assertRaises(Exception):
            self.repos.skills.add_step(skill_id, position=9, action_type="click")


if __name__ == "__main__":
    unittest.main()
