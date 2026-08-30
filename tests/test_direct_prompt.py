from inspect import signature
from unittest import TestCase

from app.direct_prompt import (
    build_direct_edit_plan,
    build_direct_prompt,
)


class DirectPromptTests(TestCase):
    def test_direct_prompt_has_only_the_user_text_input(self) -> None:
        self.assertEqual(list(signature(build_direct_prompt).parameters), ["user_text"])

    def test_clothing_and_background_have_no_hidden_guard(self) -> None:
        user_text = "Поменять одежду. Улучшить фон"

        prompt = build_direct_prompt(user_text)

        self.assertEqual(prompt, user_text)
        self.assertNotIn("PRIORITY ORDER", prompt)

    def test_exact_unicode_whitespace(self) -> None:
        texts = (
            "  Надень очки ✨\nВторая строка 👨‍👩‍👧\r\n\t",
            "Изменить размер для загрузки на сотовый телефон",
            "Муж меня обнимает",
        )
        for user_text in texts:
            with self.subTest(text=user_text):
                prompt = build_direct_prompt(user_text)
                self.assertEqual(prompt, user_text)
                self.assertEqual(prompt.encode("utf-8"), user_text.encode("utf-8"))
                self.assertNotIn("Сохрани", prompt)
                self.assertNotIn("Измени только", prompt)

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
