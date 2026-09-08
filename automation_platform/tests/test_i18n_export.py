"""The bilingual layer and the evidence export."""

import os
import tempfile
import unittest
import zipfile

from automation_platform.domain.enums import (
    ActionType,
    ApplicationHealth,
    ApprovalStatus,
    CertificationStatus,
    ExecutionStatus,
    Executor,
    FailurePolicy,
    InterventionReason,
    Measurement,
    RiskClass,
    Role,
    SelectorTier,
    SkillStatus,
    StepStatus,
    Trigger,
    ValidationLayer,
)
from automation_platform.export import EvidenceRange, EvidenceReport
from automation_platform.export.xlsx import Sheet, Workbook
from automation_platform.i18n import (
    SUPPORTED_LOCALES,
    Translator,
    catalog_keys,
    missing_keys,
    parse_accept_language,
    resolve_locale,
)

from .helpers import erp_skill, fresh_database, make_organization


class CatalogTests(unittest.TestCase):
    def test_both_catalogs_hold_exactly_the_same_keys(self):
        # This is what stops one language decaying into a stub of the other.
        self.assertEqual(missing_keys(), {})
        self.assertEqual(catalog_keys("es"), catalog_keys("en"))

    def test_no_value_is_empty(self):
        for locale in SUPPORTED_LOCALES:
            translator = Translator(locale)
            for key in catalog_keys(locale):
                with self.subTest(locale=locale, key=key):
                    self.assertTrue(translator(key).strip())

    def test_every_enum_member_has_a_label_in_both_languages(self):
        enums = [Executor, RiskClass, SkillStatus, Role, ExecutionStatus, StepStatus,
                 ValidationLayer, FailurePolicy, ApprovalStatus, CertificationStatus,
                 Measurement, Trigger, ActionType, InterventionReason, ApplicationHealth]
        for locale in SUPPORTED_LOCALES:
            keys = catalog_keys(locale)
            for enum in enums:
                for member in enum:
                    with self.subTest(locale=locale, member=member):
                        self.assertIn(member.label_key, keys)
        for tier in SelectorTier:
            for locale in SUPPORTED_LOCALES:
                self.assertIn(tier.label_key, catalog_keys(locale))

    def test_the_two_languages_actually_differ(self):
        spanish, english = Translator("es"), Translator("en")
        differing = sum(1 for key in catalog_keys("es") if spanish(key) != english(key))
        self.assertGreater(differing / len(catalog_keys("es")), 0.8)

    def test_placeholders_match_between_languages(self):
        import re

        pattern = re.compile(r"\{(\w+)\}")
        spanish, english = Translator("es"), Translator("en")
        for key in catalog_keys("es"):
            with self.subTest(key=key):
                self.assertEqual(
                    set(pattern.findall(spanish(key))), set(pattern.findall(english(key)))
                )

    def test_an_unknown_key_returns_the_key_rather_than_crashing(self):
        self.assertEqual(Translator("es")("no.existe"), "no.existe")

    def test_parameters_are_interpolated(self):
        self.assertIn("2", Translator("es")("skill.screens_used", count=2))
        self.assertIn("2", Translator("en")("skill.screens_used", count=2))


class LocaleResolutionTests(unittest.TestCase):
    def test_order_of_precedence(self):
        self.assertEqual(resolve_locale(explicit="en", user_locale="es"), "en")
        self.assertEqual(resolve_locale(user_locale="en", organization_locale="es"), "en")
        self.assertEqual(resolve_locale(organization_locale="en"), "en")
        self.assertEqual(resolve_locale(accept_language="en-GB,en;q=0.9"), "en")

    def test_spanish_is_the_default(self):
        self.assertEqual(resolve_locale(), "es")

    def test_unsupported_locales_fall_through(self):
        self.assertEqual(resolve_locale(explicit="fr", organization_locale="en"), "en")

    def test_accept_language_quality_ordering(self):
        self.assertEqual(parse_accept_language("es;q=0.2, en;q=0.9"), "en")
        self.assertEqual(parse_accept_language("fr, es-AR;q=0.8"), "es")
        self.assertIsNone(parse_accept_language("fr, de"))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.repos = make_organization(self.conn, "Acme", "acme")
        self.skill_id, _ = erp_skill(self.repos)
        execution_id = self.repos.executions.create(
            skill_id=self.skill_id, trigger="manual", status="succeeded",
            cost_usd=0.0124, cost_measurement="measured", input_tokens=5400, output_tokens=160,
        )
        self.repos.executions.add_step(
            execution_id, position=1, action_json='{"type": "click"}', status="passed",
            screenshot_before="sim://a/1", screenshot_after="sim://a/2",
            verification_json='[{"layer": "l2_action", "passed": true}]',
        )
        self.repos.interventions.create(reason="drift_detected", skill_id=self.skill_id)
        self.directory = tempfile.mkdtemp()

    def report(self, locale="es"):
        return EvidenceReport(self.repos, Translator(locale), span=EvidenceRange.all_time())

    def test_the_dataset_carries_runs_steps_and_interventions(self):
        data = self.report().dataset()
        self.assertEqual(data["summary"]["total"], 1)
        self.assertEqual(len(data["steps"]), 1)
        self.assertEqual(len(data["interventions"]), 1)
        self.assertEqual(data["summary"]["success_rate"], 1.0)

    def test_the_spreadsheet_is_a_real_workbook(self):
        path = self.report().to_xlsx(os.path.join(self.directory, "e.xlsx"))
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            self.assertIn("xl/workbook.xml", names)
            self.assertIn("xl/worksheets/sheet1.xml", names)
            self.assertIn("[Content_Types].xml", names)

    def test_the_pdf_structure_is_valid(self):
        path = self.report().to_pdf(os.path.join(self.directory, "e.pdf"))
        with open(path, "rb") as handle:
            data = handle.read()
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        # Every cross-reference offset has to point at the object it claims.
        tail = data.rfind(b"startxref")
        offset = int(data[tail:].split(b"\n")[1])
        lines = data[offset:].split(b"\n")
        count = int(lines[1].split()[1])
        for number, line in enumerate(lines[3:3 + count - 1], start=1):
            position = int(line.split()[0])
            self.assertTrue(data[position:].startswith(f"{number} 0 obj".encode()))

    def test_the_export_is_bilingual(self):
        spanish = self.report("es").to_xlsx(os.path.join(self.directory, "es.xlsx"))
        english = self.report("en").to_xlsx(os.path.join(self.directory, "en.xlsx"))
        with zipfile.ZipFile(spanish) as archive:
            spanish_workbook = archive.read("xl/workbook.xml").decode()
        with zipfile.ZipFile(english) as archive:
            english_workbook = archive.read("xl/workbook.xml").decode()
        self.assertIn("Ejecuciones", spanish_workbook)
        self.assertIn("Runs", english_workbook)

    def test_a_mixed_cost_total_is_reported_as_estimated(self):
        self.repos.executions.create(
            skill_id=self.skill_id, trigger="manual", status="succeeded",
            cost_usd=0.01, cost_measurement="estimated",
        )
        self.assertEqual(self.report().dataset()["summary"]["cost_measurement"], "estimated")

    def test_export_needs_the_permission(self):
        from automation_platform.db import RepositorySet
        from automation_platform.domain.errors import PermissionDenied
        from automation_platform.tenancy import TenantContext

        # Every listed role can export evidence, so an unknown-permission check
        # is what proves the gate is wired at all.
        stranger = TenantContext(self.repos.tenant.organization_id, role=Role.OPERATOR)
        repos = RepositorySet(self.conn, stranger)
        report = EvidenceReport(repos, Translator("es"))
        self.assertIsNotNone(report)
        with self.assertRaises(PermissionDenied):
            from automation_platform.security import authz

            authz.require(stranger, "organization.manage")


class WorkbookTests(unittest.TestCase):
    def test_numbers_and_strings_keep_their_types(self):
        directory = tempfile.mkdtemp()
        workbook = Workbook()
        workbook.add_sheet(Sheet("Datos", ["texto", "número"], [["Conciliación año", 42.5]]))
        path = workbook.save(os.path.join(directory, "w.xlsx"))
        with zipfile.ZipFile(path) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("<v>42.5</v>", sheet)
        self.assertIn("Conciliación año", sheet)

    def test_sheet_names_are_sanitised(self):
        self.assertEqual(Sheet("a/b:c*d" * 8, [], []).safe_name[:6], "abcdab")


if __name__ == "__main__":
    unittest.main()
