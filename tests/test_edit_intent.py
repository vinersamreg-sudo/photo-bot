from unittest import TestCase

from app.edit_intent import (
    EditPlan,
    merge_edit_plans,
    parse_edit_intent,
    repeat_edit_plan,
)
from app.prompt_builder import build_provider_prompt, safe_prompt_inspection


class EditIntentTests(TestCase):
    def test_rocks_replace_background(self) -> None:
        plan = parse_edit_intent("Фон на скалы")
        self.assertEqual(plan.primary_action, "replace_background")
        self.assertIn("background", plan.target_regions)
        self.assertTrue(any("rocky mountains" in value for value in plan.requested_changes))

    def test_blurry_rock_correction_preserves_and_sharpens_background(self) -> None:
        parent = parse_edit_intent("Фон на скалы")
        correction = parse_edit_intent("Фон всё равно размыт", mode="correction")
        merged = merge_edit_plans(parent, correction)
        self.assertEqual(merged.primary_action, "sharpen_background")
        self.assertTrue(any("sharp" in value for value in merged.requested_changes))
        self.assertTrue(any("Preserve" in value for value in merged.preservation_rules))
        self.assertTrue(any("replace" in value for value in merged.forbidden_changes))

    def test_negative_background_instruction_overrides_keyword_collision(self) -> None:
        plan = parse_edit_intent("Не меняй фон, только сделай его чётче", mode="correction")
        self.assertEqual(plan.primary_action, "sharpen_background")
        self.assertFalse(any("Replace the background" in value for value in plan.requested_changes))
        self.assertTrue(any("Do not replace" in value for value in plan.forbidden_changes))
        self.assertEqual(plan.unresolved_ambiguities, ())

    def test_only_clothing_preserves_subject_pose_and_background(self) -> None:
        plan = parse_edit_intent("Поменяй только одежду")
        self.assertIn("clothing", plan.target_regions)
        joined = " ".join(plan.preservation_rules + plan.forbidden_changes).lower()
        self.assertIn("pose", joined)
        self.assertIn("background", joined)
        self.assertIn("recognizable", joined)

    def test_face_not_changed_adds_identity_preservation(self) -> None:
        plan = parse_edit_intent("Лицо не меняй")
        self.assertTrue(any("recognizable person" in value for value in plan.preservation_rules))
        self.assertTrue(any("face" in value.lower() for value in plan.forbidden_changes))

    def test_pose_change_allows_pose_target(self) -> None:
        plan = parse_edit_intent("Смени позу")
        self.assertIn("pose", plan.target_regions)
        prompt = build_provider_prompt(plan)
        self.assertNotIn("Preserve the pose and camera viewpoint.", prompt)

    def test_restore_previous_background_keeps_later_clothing(self) -> None:
        plan = parse_edit_intent(
            "Верни прошлый фон, но оставь одежду", mode="correction"
        )
        self.assertIn("background", plan.target_regions)
        self.assertTrue(any("previous successful" in value for value in plan.requested_changes))
        self.assertTrue(any("later successful edits" in value for value in plan.continuity_requirements))

    def test_multiple_corrections_accumulate_intent_without_string_concatenation(self) -> None:
        first = parse_edit_intent("Фон на скалы")
        second = merge_edit_plans(
            first,
            parse_edit_intent(
                "Переодень для хайкинга, смени позу, фон не должен быть размыт",
                mode="correction",
            ),
        )
        third = merge_edit_plans(
            second,
            parse_edit_intent("Фон всё равно размыт", mode="correction"),
        )
        inherited = " ".join(third.inherited_constraints).lower()
        self.assertIn("hiking clothing", inherited)
        self.assertIn("pose", inherited)
        self.assertTrue(any("background" in value.lower() for value in third.continuity_requirements))

    def test_repeat_preserves_effective_intent(self) -> None:
        plan = merge_edit_plans(
            parse_edit_intent("Фон на скалы"),
            parse_edit_intent("Убери размытие фона", mode="correction"),
        )
        repeated = repeat_edit_plan(plan)
        self.assertEqual(repeated.mode, "repeat")
        self.assertEqual(repeated.requested_changes, plan.requested_changes)
        self.assertEqual(repeated.forbidden_changes, plan.forbidden_changes)
        self.assertEqual(repeated.inherited_constraints, plan.inherited_constraints)

    def test_russian_punctuation_and_common_stem_variants(self) -> None:
        plan = parse_edit_intent("ФОН, всё-равно размытый!!! Сделай задний план чётче.", mode="correction")
        self.assertEqual(plan.primary_action, "sharpen_background")

    def test_do_not_replace_rocks_has_priority_over_replace_vocabulary(self) -> None:
        plan = parse_edit_intent(
            "Не надо менять скалы на другие размытые, сделай их детальными и чёткими",
            mode="correction",
        )
        self.assertNotEqual(plan.primary_action, "replace_background")
        self.assertTrue(any("different" in value for value in plan.forbidden_changes))

    def test_technical_prompt_contains_contextual_preservation(self) -> None:
        prompt = build_provider_prompt(parse_edit_intent("Фон на скалы"))
        self.assertIn("PRESERVE", prompt)
        self.assertIn("recognizable identity", prompt)
        self.assertIn("Preserve the pose", prompt)
        self.assertIn("Preserve the current clothing", prompt)

    def test_technical_prompt_redacts_paths_tokens_and_ids(self) -> None:
        fake_credential = "s" + "k-" + ("a" * 26)
        plan = parse_edit_intent(
            f"Сделай красиво {fake_credential} C:\\Users\\secret\\photo.png "
            + "0123456789abcdef0123456789abcdef"
        )
        prompt = build_provider_prompt(plan)
        self.assertNotIn("sk-", prompt)
        self.assertNotIn("C:\\Users", prompt)
        self.assertNotIn("0123456789abcdef", prompt)
        inspected = safe_prompt_inspection(plan, prompt)
        self.assertNotIn("source_user_text", inspected)

    def test_true_background_contradiction_requires_clarification(self) -> None:
        plan = parse_edit_intent("Поменяй фон, но фон не меняй")
        self.assertIn(
            "replace_background_conflicts_with_preserve_background",
            plan.unresolved_ambiguities,
        )
        self.assertLess(plan.confidence, 0.5)

    def test_explicit_clarification_resolves_background_conflict(self) -> None:
        plan = parse_edit_intent(
            "Поменяй фон, но фон не меняй. Итоговое решение: сохранить текущий фон и только улучшить его."
        )
        self.assertEqual(plan.unresolved_ambiguities, ())
        self.assertNotEqual(plan.primary_action, "replace_background")

    def test_scenario_adds_deterministic_change(self) -> None:
        plan = parse_edit_intent(
            "Сделай аккуратно", mode="scenario", scenario_id="business-look"
        )
        self.assertEqual(plan.primary_action, "change_clothes")
        self.assertIn("clothing", plan.target_regions)

    def test_hair_change_disables_default_hair_preservation(self) -> None:
        plan = parse_edit_intent("Смени прическу")
        self.assertIn("hair", plan.target_regions)
        self.assertNotIn("Preserve the hairstyle", build_provider_prompt(plan))

    def test_edit_plan_json_round_trip_is_stable(self) -> None:
        plan = parse_edit_intent("Поменяй только одежду")
        restored = EditPlan.from_json(plan.to_json())
        self.assertEqual(restored, plan)
        self.assertEqual(restored.parser_version, "rules-ru-v1")
