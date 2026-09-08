"""Multi-tenant isolation: the rule that every query filters by organization."""

import unittest

from automation_platform.db.database import statement_needs_scope
from automation_platform.domain.errors import CrossTenantAccessError, PermissionDenied, TenantScopeError
from automation_platform.domain.enums import Role
from automation_platform.tenancy import TenantContext

from .helpers import erp_skill, fresh_database, make_organization


class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.acme = make_organization(self.conn, "Acme", "acme")
        self.beta = make_organization(self.conn, "Beta", "beta", locale="en")
        self.skill_id, self.application_id = erp_skill(self.acme)

    def test_other_tenant_cannot_read_the_skill(self):
        self.assertIsNone(self.beta.skills.get(self.skill_id))
        self.assertEqual(self.beta.skills.list(), [])

    def test_other_tenant_gets_a_not_found_not_a_permission_error(self):
        # Telling a tenant "that exists but is not yours" leaks the id itself.
        with self.assertRaises(CrossTenantAccessError):
            self.beta.skills.require(self.skill_id)

    def test_steps_are_scoped_too(self):
        self.assertEqual(len(self.acme.skills.steps(self.skill_id)), 4)
        self.assertEqual(self.beta.skills.steps(self.skill_id), [])

    def test_executions_do_not_cross(self):
        self.acme.executions.create(skill_id=self.skill_id, trigger="manual", status="queued")
        self.assertEqual(len(self.acme.executions.list()), 1)
        self.assertEqual(len(self.beta.executions.list()), 0)

    def test_health_counters_are_scoped(self):
        self.acme.interventions.create(reason="drift_detected", skill_id=self.skill_id)
        self.assertEqual(self.acme.interventions.count_for_skill(self.skill_id), 1)
        self.assertEqual(self.beta.interventions.count_for_skill(self.skill_id), 0)


class ScopeGuardTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()

    def test_unscoped_read_is_refused(self):
        with self.assertRaises(TenantScopeError):
            self.conn.execute("SELECT * FROM skills")

    def test_unscoped_write_is_refused(self):
        with self.assertRaises(TenantScopeError):
            self.conn.execute("UPDATE skills SET name = 'x'")

    def test_scoped_statement_passes(self):
        self.conn.execute("SELECT * FROM skills WHERE organization_id = ?", ("org_1",))

    def test_organizations_table_is_exempt(self):
        self.conn.execute("SELECT * FROM organizations")

    def test_scope_detection(self):
        self.assertTrue(statement_needs_scope("SELECT * FROM executions"))
        self.assertTrue(statement_needs_scope("INSERT INTO approvals (id) VALUES (1)"))
        self.assertFalse(statement_needs_scope("SELECT * FROM organizations"))
        self.assertFalse(statement_needs_scope("PRAGMA foreign_keys = ON"))


class RoleTests(unittest.TestCase):
    def test_role_requirement(self):
        operator = TenantContext("org", role=Role.OPERATOR)
        operator.require_role(Role.OPERATOR)
        with self.assertRaises(PermissionDenied):
            operator.require_role(Role.APPROVER)

    def test_admin_passes_every_role_check(self):
        TenantContext("org", role=Role.ADMIN).require_role(Role.APPROVER, Role.DESIGNER)


if __name__ == "__main__":
    unittest.main()
