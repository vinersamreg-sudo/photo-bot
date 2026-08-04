from unittest import TestCase

from app.direct_prompt import (
    build_direct_edit_plan,
    build_direct_prompt,
)


class DirectPromptTests(TestCase):
    def test_preserves_exact_unicode_without_hidden_instructions(self) -> None:
        user_text = "  Замени девушку на фото на Монику Беллуччи ✨\n"

        prompt = build_direct_prompt(user_text)

        self.assertEqual(prompt, user_text)
        self.assertNotIn("PRIORITY ORDER", prompt)
        self.assertNotIn("Сохрани лицо", prompt)

    def test_rejects_only_semantically_empty_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            build_direct_prompt(" \n\t ")

    def test_direct_plan_contains_no_interpreted_constraints(self) -> None:
        plan = build_direct_edit_plan("замени фон", mode="initial_edit")
        self.assertEqual(plan.source_user_text, "замени фон")
        self.assertEqual(plan.requested_changes, ("замени фон",))
        self.assertEqual(plan.preservation_rules, ())
        self.assertEqual(plan.forbidden_changes, ())
        self.assertEqual(plan.parser_version, "direct-unicode-v3")
