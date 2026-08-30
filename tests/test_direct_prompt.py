from unittest import TestCase
from unittest.mock import patch

from app.direct_prompt import (
    build_direct_edit_plan,
    build_direct_prompt,
    build_preservation_guard,
)


class DirectPromptTests(TestCase):
    def test_clothing_and_background_have_no_hidden_guard(self) -> None:
        user_text = "Поменять одежду. Улучшить фон"

        prompt = build_direct_prompt(user_text)

        self.assertEqual(prompt, user_text)
        self.assertNotIn("PRIORITY ORDER", prompt)

    def test_exact_unicode_whitespace_and_no_guard_even_when_flag_enabled(self) -> None:
        texts = (
            "  Надень очки ✨\nВторая строка 👨‍👩‍👧\r\n\t",
            "Изменить размер для загрузки на сотовый телефон",
            "Муж меня обнимает",
        )
        with patch(
            "app.direct_prompt.build_preservation_guard",
            side_effect=AssertionError("Direct mode must not interpret the prompt"),
        ):
            for user_text in texts:
                for enabled in (False, True):
                    with self.subTest(text=user_text, guard=enabled):
                        prompt = build_direct_prompt(
                            user_text, preservation_guard_enabled=enabled
                        )
                        self.assertEqual(prompt, user_text)
                        self.assertEqual(prompt.encode("utf-8"), user_text.encode("utf-8"))
                        self.assertNotIn("Сохрани", prompt)
                        self.assertNotIn("Измени только", prompt)

    def test_face_change_removes_only_identity_protection(self) -> None:
        guard = build_preservation_guard(
            "Замени девушку на фото на Монику Беллуччи"
        )

        self.assertNotIn("личности", guard)
        self.assertNotIn("лица всех людей", guard)
        self.assertIn("мимику", guard)
        self.assertIn("позы, положение тел", guard)
        self.assertIn("ракурс", guard)
        self.assertIn("композицию", guard)

    def test_pose_change_removes_only_pose_protection(self) -> None:
        guard = build_preservation_guard("Измени позу человека")

        self.assertIn("личности и узнаваемые лица", guard)
        self.assertNotIn("позы", guard)
        self.assertNotIn("положение тел", guard)
        self.assertIn("пропорции", guard)
        self.assertIn("композицию", guard)

    def test_hair_makeup_and_glasses_keep_identity_and_pose(self) -> None:
        for user_text in ("измени причёску", "добавь макияж", "надень очки"):
            with self.subTest(user_text=user_text):
                guard = build_preservation_guard(user_text)
                self.assertIn("личности и узнаваемые лица", guard)
                self.assertIn("позы, положение тел", guard)
                self.assertIn("композицию", guard)

    def test_expression_and_composition_protections_are_independent(self) -> None:
        expression_guard = build_preservation_guard("Добавь улыбку")
        composition_guard = build_preservation_guard("Измени кадрирование")

        self.assertNotIn("мимику", expression_guard)
        self.assertIn("композицию", expression_guard)
        self.assertIn("мимику", composition_guard)
        self.assertNotIn("композицию", composition_guard)
        self.assertIn("ракурс", composition_guard)

    def test_guard_can_be_disabled_for_exact_legacy_passthrough(self) -> None:
        user_text = "Замени фон"

        self.assertEqual(
            build_direct_prompt(user_text, preservation_guard_enabled=False),
            user_text,
        )

    def test_rejects_only_semantically_empty_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            build_direct_prompt(" \n\t ")

    def test_direct_plan_contains_no_interpreted_constraints(self) -> None:
        plan = build_direct_edit_plan("замени фон", mode="initial_edit")
        self.assertEqual(plan.source_user_text, "замени фон")
        self.assertEqual(plan.requested_changes, ("замени фон",))
        self.assertEqual(plan.preservation_rules, ())
        self.assertEqual(plan.forbidden_changes, ())
        self.assertEqual(plan.parser_version, "direct-unicode-v5")
