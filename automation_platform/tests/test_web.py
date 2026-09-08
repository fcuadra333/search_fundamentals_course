"""The dashboard: every view renders in both languages, and no tenant leaks."""

import re
import tempfile
import unittest
from pathlib import Path

from automation_platform.config import Config
from automation_platform.db import RepositorySet, connect
from automation_platform.domain.enums import Role
from automation_platform.i18n import catalog_keys
from automation_platform.seed import seed
from automation_platform.tenancy import TenantContext
from automation_platform.web import create_app


class WebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        cls.database = Path(cls.directory) / "platform.sqlite3"
        connection = connect(cls.database)
        seed(connection)
        connection.close()

    def setUp(self):
        config = Config(database_path=self.database, export_dir=Path(self.directory) / "exports")
        self.app = create_app(config)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.conn = connect(self.database)
        bootstrap = RepositorySet(self.conn, TenantContext("bootstrap"))
        self.acme = bootstrap.organizations.by_slug("acme")
        self.beta = bootstrap.organizations.by_slug("beta")
        self.acme_repos = RepositorySet(
            self.conn, TenantContext(self.acme["id"], role=Role.ADMIN)
        )
        self.beta_repos = RepositorySet(
            self.conn, TenantContext(self.beta["id"], role=Role.ADMIN)
        )

    def tearDown(self):
        self.conn.close()

    def sign_in(self, organization, repos, role="admin"):
        users = {row["role"]: row["id"] for row in repos.users.list()}
        with self.client.session_transaction() as session:
            session["organization_id"] = organization["id"]
            session["user_id"] = users.get(role, next(iter(users.values())))

    def routes_for(self, repos):
        skill = [row for row in repos.skills.list() if row["executor"] == "computer_use"][0]
        execution = repos.executions.list(limit=1)[0]
        return [
            "/", "/queue", "/skills", f"/skills/{skill['id']}", "/processes", "/approvals",
            "/health", "/training", "/applications", "/job-roles",
            f"/executions/{execution['id']}",
        ]


class RenderingTests(WebTestCase):
    def test_every_view_renders_in_both_languages(self):
        self.sign_in(self.acme, self.acme_repos)
        for locale in ("es", "en"):
            self.client.get(f"/language/{locale}")
            for route in self.routes_for(self.acme_repos):
                with self.subTest(locale=locale, route=route):
                    response = self.client.get(route)
                    self.assertEqual(response.status_code, 200)

    def test_no_untranslated_key_reaches_the_page(self):
        self.sign_in(self.acme, self.acme_repos)
        keys = catalog_keys("es")
        for locale in ("es", "en"):
            self.client.get(f"/language/{locale}")
            for route in self.routes_for(self.acme_repos):
                text = re.sub(r"<[^>]+>", " ", self.client.get(route).get_data(as_text=True))
                leaked = sorted(key for key in keys if key in text)
                with self.subTest(locale=locale, route=route):
                    self.assertEqual(leaked, [])

    def test_the_language_switch_actually_changes_the_page(self):
        self.sign_in(self.acme, self.acme_repos)
        self.client.get("/language/es")
        spanish = self.client.get("/").get_data(as_text=True)
        self.client.get("/language/en")
        english = self.client.get("/").get_data(as_text=True)
        self.assertIn("Tablero", spanish)
        self.assertIn("Dashboard", english)
        self.assertNotEqual(spanish, english)

    def test_the_organization_default_locale_applies_before_any_choice(self):
        # Beta is an English tenant; Acme is a Spanish one.
        self.sign_in(self.beta, self.beta_repos)
        self.assertIn("Dashboard", self.client.get("/").get_data(as_text=True))

    def test_evidence_downloads_in_both_formats(self):
        self.sign_in(self.acme, self.acme_repos)
        for suffix, signature in (("xlsx", b"PK"), ("pdf", b"%PDF")):
            with self.subTest(format=suffix):
                response = self.client.get(f"/export.{suffix}")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.data.startswith(signature))
                response.close()


class AccessTests(WebTestCase):
    def test_an_anonymous_visitor_is_sent_to_the_sign_in_page(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_the_sign_in_page_renders_without_a_session(self):
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_one_tenant_cannot_open_another_tenants_skill(self):
        beta_skill = self.beta_repos.skills.list()[0]
        self.sign_in(self.acme, self.acme_repos)
        response = self.client.get(f"/skills/{beta_skill['id']}")
        self.assertEqual(response.status_code, 404)

    def test_one_tenant_cannot_open_another_tenants_execution(self):
        beta_execution = self.beta_repos.executions.list(limit=1)[0]
        self.sign_in(self.acme, self.acme_repos)
        self.assertEqual(
            self.client.get(f"/executions/{beta_execution['id']}").status_code, 404
        )

    def test_a_tenants_list_never_includes_another_tenants_rows(self):
        self.sign_in(self.acme, self.acme_repos)
        html = self.client.get("/skills").get_data(as_text=True)
        for row in self.beta_repos.skills.list():
            self.assertNotIn(row["name"], html)

    def test_an_operator_cannot_publish(self):
        self.sign_in(self.acme, self.acme_repos, role="operator")
        skill = [row for row in self.acme_repos.skills.list()
                 if row["executor"] == "computer_use"][0]
        response = self.client.post(f"/skills/{skill['id']}/publish")
        self.assertEqual(response.status_code, 403)

    def test_an_operator_cannot_decide_an_approval(self):
        self.sign_in(self.acme, self.acme_repos, role="operator")
        pending = self.acme_repos.approvals.pending()
        if not pending:
            self.skipTest("no pending approval in the demo data")
        response = self.client.post(
            f"/approvals/{pending[0]['id']}/decide", data={"decision": "approve"}
        )
        self.assertEqual(response.status_code, 403)


class OperationTests(WebTestCase):
    def test_running_an_irreversible_skill_parks_it_for_approval(self):
        self.sign_in(self.acme, self.acme_repos)
        skill = [row for row in self.acme_repos.skills.list()
                 if row["risk_classification"] == "irreversible_write"][0]
        response = self.client.post(f"/skills/{skill['id']}/run", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.acme_repos.approvals.pending())

    def test_the_kill_switch_shows_on_every_page_once_engaged(self):
        self.sign_in(self.acme, self.acme_repos)
        self.client.post("/organization/stop", data={"stopped": "on", "reason": "auditoría"},
                         follow_redirects=True)
        html = self.client.get("/skills").get_data(as_text=True)
        self.assertIn("auditoría", html)
        self.client.post("/organization/stop", data={"reason": ""}, follow_redirects=True)

    def test_recording_produces_a_reviewable_proposal(self):
        self.sign_in(self.acme, self.acme_repos)
        application = self.acme_repos.applications.list()[0]
        before = len(self.acme_repos.proposals.list())
        self.client.post("/training/record",
                         data={"application_id": application["id"], "name": "Demo"},
                         follow_redirects=True)
        self.assertEqual(len(self.acme_repos.proposals.list()), before + 1)


if __name__ == "__main__":
    unittest.main()
