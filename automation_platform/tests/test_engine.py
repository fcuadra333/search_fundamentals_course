"""Selector cascade, drift detection, conditions and layered validation."""

import unittest

from automation_platform.domain.enums import SelectorTier, ValidationLayer, FailurePolicy, RiskClass
from automation_platform.domain.errors import SelectorNotFound
from automation_platform.engine import conditions, drift, selectors, validation
from automation_platform.engine.conditions import ConditionContext, ConditionError
from automation_platform.engine.drivers import ScreenObservation, UiElement

from .helpers import erp_driver


def observation(**overrides):
    base = dict(
        screen_key="erp.reports",
        elements=(
            UiElement(ref="txt.period", role="edit", name="Período",
                      automation_id="txtPeriodo", label="Período", bounds=(40, 120, 200, 28)),
            UiElement(ref="btn.export", role="button", name="Exportar",
                      automation_id="btnExportar", label="Exportar", bounds=(40, 180, 140, 36)),
        ),
        text=("Seleccione el período",),
    )
    base.update(overrides)
    return ScreenObservation(**base)


class SelectorCascadeTests(unittest.TestCase):
    def test_prefers_the_automation_identifier(self):
        cascade = selectors.parse_selectors([
            {"tier": 3, "value": "Exportar"},
            {"tier": 1, "value": "btnExportar"},
        ])
        self.assertEqual(cascade[0].tier, SelectorTier.AUTOMATION_ID)
        resolution = selectors.resolve(observation(), cascade)
        self.assertEqual(resolution.tier_used, SelectorTier.AUTOMATION_ID)
        self.assertFalse(resolution.degraded)

    def test_falls_through_and_records_the_degradation(self):
        cascade = selectors.parse_selectors([
            {"tier": 1, "value": "noSuchId"},
            {"tier": 2, "value": "Exportar", "role": "button"},
        ])
        resolution = selectors.resolve(observation(), cascade)
        self.assertEqual(resolution.tier_used, SelectorTier.ACCESSIBLE_ROLE_NAME)
        self.assertTrue(resolution.degraded)
        self.assertFalse(resolution.marks_fragile)

    def test_pixel_coordinates_mark_the_skill_fragile(self):
        cascade = selectors.parse_selectors([
            {"tier": 1, "value": "gone"},
            {"tier": 5, "value": "110,198"},
        ])
        resolution = selectors.resolve(observation(), cascade)
        self.assertEqual(resolution.tier_used, SelectorTier.PIXEL_COORDINATES)
        self.assertTrue(resolution.marks_fragile)
        self.assertEqual(resolution.element.ref, "btn.export")

    def test_relative_to_anchor(self):
        cascade = selectors.parse_selectors([
            {"tier": 4, "value": "txt.period", "anchor_ref": "txt.period", "offset": [0, 60]},
        ])
        resolution = selectors.resolve(observation(), cascade)
        self.assertEqual(resolution.element.ref, "btn.export")

    def test_no_match_fails_cleanly_rather_than_guessing(self):
        cascade = selectors.parse_selectors([{"tier": 1, "value": "gone"}])
        with self.assertRaises(SelectorNotFound):
            selectors.resolve(observation(), cascade)

    def test_declaring_no_selectors_is_an_error(self):
        with self.assertRaises(SelectorNotFound):
            selectors.resolve(observation(), [])

    def test_malformed_entries_are_dropped_not_guessed(self):
        self.assertEqual(selectors.parse_selectors([{"tier": 99, "value": "x"}, {"value": "y"}]), [])


class DriftTests(unittest.TestCase):
    def test_identical_screen_has_no_drift(self):
        baseline = drift.serialize_baseline(observation())
        reading = drift.compare(observation(), baseline)
        self.assertEqual(reading.distance, 0.0)
        self.assertFalse(reading.exceeded)

    def test_a_changed_screen_exceeds_the_threshold(self):
        baseline = drift.serialize_baseline(observation())
        changed = observation(elements=(
            UiElement(ref="a", role="button", name="Otro", automation_id="btnOtro"),
            UiElement(ref="b", role="button", name="Mas", automation_id="btnMas"),
        ))
        reading = drift.compare(changed, baseline)
        self.assertEqual(reading.distance, 1.0)
        self.assertTrue(reading.exceeded)

    def test_one_added_element_stays_under_the_threshold(self):
        many = observation(elements=tuple(
            UiElement(ref=f"e{index}", role="button", name=f"B{index}",
                      automation_id=f"btn{index}")
            for index in range(20)
        ))
        baseline = drift.serialize_baseline(many)
        plus_one = observation(elements=many.elements + (
            UiElement(ref="extra", role="text", name="Nuevo", automation_id="lblNuevo"),
        ))
        self.assertFalse(drift.compare(plus_one, baseline).exceeded)

    def test_without_a_baseline_there_is_nothing_to_compare(self):
        self.assertEqual(drift.compare(observation(), None).distance, 0.0)

    def test_fingerprint_ignores_coordinates(self):
        moved = observation(elements=tuple(
            UiElement(ref=el.ref, role=el.role, name=el.name, automation_id=el.automation_id,
                      label=el.label, bounds=(el.bounds[0] + 200, el.bounds[1], 10, 10))
            for el in observation().elements
        ))
        self.assertEqual(observation().fingerprint(), moved.fingerprint())


class ConditionTests(unittest.TestCase):
    def setUp(self):
        self.context = ConditionContext(
            screen_key="erp.main",
            artifacts={"files": {"mayor": [1, 2, 3]}},
            rows=[{"importe": 10}, {"importe": 32}],
            variables={"periodo": "2026-01", "matched": 4, "left_count": 4},
        )

    def test_predicates(self):
        self.assertTrue(conditions.evaluate("screen_is('erp.main')", self.context))
        self.assertTrue(conditions.evaluate("file_exists('mayor')", self.context))
        self.assertTrue(conditions.evaluate("file_rows('mayor') == 3", self.context))
        self.assertTrue(conditions.evaluate("column_sum('importe') == 42", self.context))
        self.assertTrue(conditions.evaluate("matched == left_count", self.context))
        self.assertFalse(conditions.evaluate("screen_is('otra')", self.context))

    def test_unknown_names_are_falsy_rather_than_errors(self):
        self.assertFalse(conditions.evaluate("nunca_definido == 1", self.context))

    def test_dangerous_expressions_are_refused(self):
        for expression in (
            "__import__('os').system('ls')",
            "open('/etc/passwd').read()",
            "screen_is.__class__",
            "(lambda: 1)()",
            "[x for x in ()]",
            "globals()",
        ):
            with self.subTest(expression=expression):
                with self.assertRaises(ConditionError):
                    conditions.validate(expression)

    def test_empty_condition_is_not_evaluable(self):
        self.assertFalse(conditions.is_evaluable(""))
        self.assertFalse(conditions.is_evaluable(None))


class ValidationLayerTests(unittest.TestCase):
    def test_policies_match_the_specification_table(self):
        self.assertEqual(validation.LAYER_POLICY[ValidationLayer.L1_PRECONDITION], FailurePolicy.ABORT)
        self.assertEqual(validation.LAYER_POLICY[ValidationLayer.L2_ACTION], FailurePolicy.RETRY_ONCE)
        self.assertEqual(validation.LAYER_POLICY[ValidationLayer.L3_POSTCONDITION], FailurePolicy.ESCALATE)
        self.assertEqual(validation.LAYER_POLICY[ValidationLayer.L4_SEMANTIC], FailurePolicy.REVIEW_QUEUE)
        self.assertEqual(validation.LAYER_POLICY[ValidationLayer.L5_HUMAN], FailurePolicy.APPROVAL_QUEUE)

    def test_a_missing_postcondition_fails_layer_three(self):
        result = validation.check_postcondition("", ConditionContext())
        self.assertFalse(result.passed)

    def test_a_missing_precondition_passes_layer_one(self):
        self.assertTrue(validation.check_precondition("", ConditionContext()).passed)

    def test_a_missing_semantic_check_passes_but_is_marked_skipped(self):
        result = validation.check_semantic(None, ConditionContext())
        self.assertTrue(result.passed)
        self.assertTrue(result.skipped)

    def test_an_unevaluable_condition_fails_closed(self):
        result = validation.check_postcondition("open('x')", ConditionContext())
        self.assertFalse(result.passed)
        self.assertEqual(result.detail_key, "validation.not_evaluable")

    def test_layer_five_requires_approval_for_irreversible_writes(self):
        self.assertFalse(validation.check_human(RiskClass.IRREVERSIBLE_WRITE, approved=False).passed)
        self.assertTrue(validation.check_human(RiskClass.IRREVERSIBLE_WRITE, approved=True).passed)
        self.assertTrue(validation.check_human(RiskClass.READ_ONLY, approved=False).skipped)


class DriverTests(unittest.TestCase):
    def test_window_resize_does_not_change_the_structure(self):
        normal = erp_driver().observe()
        resized = erp_driver(window_scale=1.35).observe()
        self.assertEqual(normal.structural_signature(), resized.structural_signature())
        self.assertNotEqual(normal.elements[0].bounds, resized.elements[0].bounds)

    def test_dry_run_effects_stay_inside_the_session(self):
        from automation_platform.engine.drivers import DriverMode

        driver = erp_driver(mode=DriverMode.SANDBOX)
        driver.act("click", driver.observe().element("menu.reports"), None)
        driver.act("click", driver.observe().element("btn.export"), None)
        self.assertIn("mayor", driver.artifacts().get("files", {}))

    def test_preview_stops_before_an_effectful_action(self):
        from automation_platform.engine.drivers import PREVIEW_HALT, DriverMode

        driver = erp_driver(mode=DriverMode.PREVIEW)
        driver.act("click", driver.observe().element("menu.reports"), None)
        result = driver.act("click", driver.observe().element("btn.export"), None)
        self.assertEqual(result.detail, PREVIEW_HALT)
        self.assertEqual(driver.artifacts().get("files", {}), {})
        self.assertEqual(driver.proposed[0]["element"], "btn.export")


if __name__ == "__main__":
    unittest.main()
