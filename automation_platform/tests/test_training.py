"""Certification and training: the two gates before production."""

import unittest

from automation_platform.certification import Certifier
from automation_platform.certification.certifier import INJECTED_DIALOG
from automation_platform.domain.enums import CertificationStatus, RiskClass, SkillStatus
from automation_platform.domain.errors import NotCertified, UnreviewedProposal
from automation_platform.engine.drivers import DriverMode, SimulatedDesktopDriver
from automation_platform.simulation import erp_application, make_driver_factory
from automation_platform.training import create_skill_from_proposal, generalize, split_plan
from automation_platform.training.recorder import replay_into_recorder

from .helpers import DEFAULT_INPUTS, erp_skill, fresh_database, make_organization

ERP_SCRIPT = [
    {"action": "click", "ref": "menu.reports"},
    {"action": "type_text", "ref": "txt.period", "value": "2026-01"},
    {"action": "click", "ref": "btn.export"},
    {"action": "click", "ref": "btn.home"},
]


class CertificationTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.repos = make_organization(self.conn, "Acme", "acme")
        self.skill_id, self.application_id = erp_skill(self.repos)
        self.certifier = Certifier(self.repos, driver_factory=make_driver_factory(self.repos))

    def certify(self, **kwargs):
        kwargs.setdefault("risk_declared", True)
        kwargs.setdefault("cases", [DEFAULT_INPUTS])
        return self.certifier.certify(self.skill_id, **kwargs)

    def test_a_sound_skill_passes_every_check(self):
        report = self.certify()
        self.assertEqual(report.status, CertificationStatus.PASSED)
        self.assertEqual(report.screens_counted, 3)
        self.assertEqual(report.dry_runs_passed, 10)
        self.assertTrue(report.variation_resize_ok)
        self.assertTrue(report.variation_dialog_ok)

    def test_an_undeclared_risk_classification_blocks_publication(self):
        report = self.certify(risk_declared=False)
        self.assertEqual(report.status, CertificationStatus.FAILED)
        self.assertIn("certification.check_risk", report.blocking_failures)
        with self.assertRaises(NotCertified):
            self.certifier.publish(self.skill_id)

    def test_a_missing_postcondition_blocks_publication(self):
        self.repos.skills.update(self.skill_id, postcondition="")
        report = self.certify()
        self.assertFalse(report.postcondition_ok)
        self.assertEqual(report.status, CertificationStatus.FAILED)

    def test_a_postcondition_nobody_can_evaluate_counts_as_missing(self):
        self.repos.skills.update(self.skill_id, postcondition="open('/etc/passwd')")
        report = self.certify()
        self.assertFalse(report.postcondition_ok)
        self.assertEqual(report.postcondition_detail, "not_evaluable")

    def test_publication_writes_the_counted_screens_not_the_declared_ones(self):
        self.repos.skills.update(self.skill_id, declared_screens=1)
        self.certify()
        self.certifier.publish(self.skill_id)
        skill = self.repos.skills.get(self.skill_id)
        self.assertEqual(skill["declared_screens"], 3)
        self.assertEqual(skill["status"], SkillStatus.PUBLISHED.value)

    def test_certification_records_a_baseline_for_drift_detection(self):
        self.certify()
        certification = self.repos.certifications.latest(self.skill_id)
        self.assertTrue(certification["baseline_fingerprint"])
        self.assertEqual(certification["app_version"], "12.4.1")

    def test_failing_the_variation_test_marks_the_skill_fragile(self):
        # A skill that cannot survive an unexpected dialog is publishable but
        # flagged: the product demands more robust selectors rather than lying.
        class BlockingFactory:
            def __call__(self, skill, **kwargs):
                if kwargs.get("inject_dialog"):
                    kwargs["dialog_dismissible"] = False
                return SimulatedDesktopDriver(erp_application(), mode=DriverMode.PRODUCTION, **kwargs)

        certifier = Certifier(self.repos, driver_factory=BlockingFactory())
        report = certifier.certify(self.skill_id, risk_declared=True, cases=[DEFAULT_INPUTS])
        self.assertEqual(report.status, CertificationStatus.FRAGILE)
        self.assertFalse(report.variation_dialog_ok)
        self.assertTrue(report.publishable)
        self.assertTrue(self.repos.skills.get(self.skill_id)["fragile"])

    def test_the_injected_dialog_is_unexpected_but_not_an_attack(self):
        # Resistance to a hostile dialog is a security property, not something a
        # skill can be certified out of, so the variation test uses a benign one.
        from automation_platform.security.screen_content import quarantine

        self.assertFalse(quarantine([INJECTED_DIALOG]).hostile)

    def test_recertification_after_a_version_change(self):
        self.certify()
        self.certifier.publish(self.skill_id)
        self.repos.applications.set_version(self.application_id, "13.0.0")
        self.repos.skills.flag_application_for_review(self.application_id, "test")
        reports = self.certifier.recertify_application(self.application_id, risk_declared=True)
        self.assertEqual(len(reports), 1)


class GeneralizerTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.repos = make_organization(self.conn, "Acme", "acme")
        driver = SimulatedDesktopDriver(erp_application(), mode=DriverMode.SANDBOX)
        self.recorder = replay_into_recorder(driver, ERP_SCRIPT, title="Exportar mayor")
        self.payload = self.recorder.payload()

    def test_the_recording_keeps_the_accessibility_tree_not_just_pixels(self):
        event = self.payload["events"][0]
        self.assertTrue(event["accessibility"])
        self.assertEqual(event["automation_id"], "menuReportes")

    def test_a_date_like_value_becomes_a_parameter(self):
        proposal = generalize(self.payload)
        # `txtPeriodo` becomes `periodo`: the widget prefix is noise to a person
        # reviewing the proposal.
        self.assertIn("periodo", proposal.parameters)
        self.assertEqual(proposal.parameters["periodo"], "2026-01")
        step = [s for s in proposal.steps if s.action_type == "type_text"][0]
        self.assertEqual(step.value_template, "{periodo}")

    def test_the_cascade_prefers_the_automation_identifier(self):
        proposal = generalize(self.payload)
        self.assertEqual(proposal.steps[0].selectors[0]["tier"], 1)
        self.assertEqual(proposal.steps[0].selectors[0]["value"], "menuReportes")

    def test_the_anchor_is_the_first_screen(self):
        proposal = generalize(self.payload)
        self.assertEqual(proposal.start_anchor, "erp.main")
        self.assertEqual(proposal.precondition, "screen_is('erp.main')")

    def test_no_postcondition_is_invented_when_none_can_be_verified(self):
        proposal = generalize(self.payload)
        # It may propose one, but whatever it proposes has to be evaluable.
        from automation_platform.engine import conditions

        if proposal.postcondition:
            self.assertTrue(conditions.is_evaluable(proposal.postcondition))

    def test_an_oversized_recording_gets_a_split_plan(self):
        payload = dict(self.payload)
        payload["screens"] = ["s1", "s2", "s3", "s4", "s5"]
        payload["events"] = [
            {**self.payload["events"][0], "index": index + 1, "screen_key": f"s{index + 1}"}
            for index in range(5)
        ]
        proposal = generalize(payload)
        self.assertTrue(proposal.exceeds_screen_budget)
        self.assertEqual(proposal.suggested_parts, 2)
        parts = split_plan(proposal)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]["anchor"], "s1")
        self.assertEqual(parts[1]["screens"], ["s4", "s5"])

    def test_suspicious_recorded_text_is_flagged_for_the_reviewer(self):
        payload = dict(self.payload)
        payload["events"] = [{**self.payload["events"][0], "suspicious_text": True}]
        proposal = generalize(payload)
        self.assertTrue(any(note["key"] == "execution.suspicious_content"
                            for note in proposal.notes))


class ProposalReviewTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_database()
        self.repos = make_organization(self.conn, "Acme", "acme")
        driver = SimulatedDesktopDriver(erp_application(), mode=DriverMode.SANDBOX)
        recorder = replay_into_recorder(driver, ERP_SCRIPT, title="Exportar mayor")
        self.demonstration_id = recorder.save(self.repos)
        proposal = generalize(recorder.payload(), name="Exportar mayor")
        self.proposal_id = self.repos.proposals.create(
            demonstration_id=self.demonstration_id, proposal=proposal.to_dict()
        )

    def test_an_unreviewed_proposal_cannot_become_a_skill(self):
        with self.assertRaises(UnreviewedProposal):
            create_skill_from_proposal(self.repos, self.proposal_id)

    def test_a_reviewed_proposal_becomes_a_draft_skill(self):
        from automation_platform.db import loads

        row = self.repos.proposals.get(self.proposal_id)
        accepted = loads(row["proposal_json"], {})
        accepted["postcondition"] = "file_exists('mayor')"
        self.repos.proposals.accept(
            self.proposal_id, reviewed_by=self.repos.tenant.user_id, accepted=accepted
        )
        skill_id = create_skill_from_proposal(self.repos, self.proposal_id)
        skill = self.repos.skills.get(skill_id)
        self.assertEqual(skill["status"], SkillStatus.DRAFT.value)
        self.assertEqual(skill["postcondition"], "file_exists('mayor')")
        self.assertEqual(len(self.repos.skills.steps(skill_id)), 4)

    def test_the_reviewer_edit_wins_over_the_proposal(self):
        from automation_platform.db import loads

        row = self.repos.proposals.get(self.proposal_id)
        accepted = loads(row["proposal_json"], {})
        accepted["risk_classification"] = RiskClass.IRREVERSIBLE_WRITE.value
        accepted["postcondition"] = "screen_is('erp.main')"
        self.repos.proposals.accept(
            self.proposal_id, reviewed_by=self.repos.tenant.user_id, accepted=accepted
        )
        skill_id = create_skill_from_proposal(self.repos, self.proposal_id)
        self.assertEqual(
            self.repos.skills.get(skill_id)["risk_classification"],
            RiskClass.IRREVERSIBLE_WRITE.value,
        )

    def test_the_raw_recording_is_kept_as_evidence(self):
        self.assertIsNotNone(self.repos.demonstrations.get(self.demonstration_id))


if __name__ == "__main__":
    unittest.main()
