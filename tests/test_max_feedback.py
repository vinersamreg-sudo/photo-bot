"""Feedback must stay independent of provider/credits and survive redelivery."""

from unittest import TestCase

from app.max_adapter import Button
from app.max_application import FEEDBACK_PROMPT_TEXT, FEEDBACK_SAVED_TEXT, MaxApplication
from app.max_transport import MaxApiClient, MaxTransportError
from app.max_ui_shell import FEEDBACK_BUTTON_TEXT, FEEDBACK_SCREENS, with_feedback_button
from tests import test_max_application as fixtures


class FeedbackFooterTests(TestCase):
    def test_all_opted_in_screens_put_feedback_before_final_back_row(self):
        original = (Button("Первое", "first", 0), Button("Второе", "second", 0),
                    Button("← Назад", "back", 1))
        for screen in FEEDBACK_SCREENS:
            with self.subTest(screen=screen):
                result = with_feedback_button(original, screen)
                self.assertEqual(result[:-2], original[:-1])
                self.assertEqual(result[-2].text, FEEDBACK_BUTTON_TEXT)
                self.assertEqual(result[-1], original[-1])
                rows = MaxApiClient._keyboard(result)[0]["payload"]["buttons"]
                self.assertEqual(len(rows[-1]), 1)
                self.assertEqual(len(rows[-2]), 1)
                self.assertEqual(rows[-2][0]["text"], FEEDBACK_BUTTON_TEXT)
                self.assertEqual(rows[-1][0]["text"], "← Назад")
                self.assertEqual(with_feedback_button(result, screen), result)

    def test_screen_without_back_keeps_feedback_last(self):
        original = (Button("Загрузить фото", "upload"),)
        result = with_feedback_button(original, "main_menu")
        self.assertEqual(result[:-1], original)
        self.assertEqual(result[-1].text, FEEDBACK_BUTTON_TEXT)

    def test_back_is_last_even_if_input_order_or_row_is_shared(self):
        result = with_feedback_button(
            (Button("← Назад", "back", 0), Button("Действие", "action", 0)),
            "navigation:work",
        )
        rows = MaxApiClient._keyboard(result)[0]["payload"]["buttons"]
        self.assertEqual([row[0]["text"] for row in rows],
                         ["Действие", FEEDBACK_BUTTON_TEXT, "← Назад"])
        self.assertEqual([len(row) for row in rows], [1, 1, 1])

    def test_processing_errors_and_feedback_forms_have_no_footer(self):
        for screen in ("processing", "processing_interrupted", "error", "work_not_ready",
                       "original_delivery_failed", "feedback_prompt", "feedback_saved",
                       "rating", "rating_result", "payment_status_invalid",
                       "version_history", "version_history_empty",
                       "navigation:history", "navigation:more"):
            with self.subTest(screen=screen):
                self.assertEqual(with_feedback_button((), screen), ())


class FeedbackFlowTests(TestCase):
    def setUp(self):
        self.c = fixtures.MaxApplicationTests(methodName="runTest")
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)

    def rows(self):
        with self.c.database.read() as connection:
            return connection.execute("SELECT * FROM service_feedback").fetchall()

    def test_no_photo_exact_text_and_no_credit_or_provider_change_both_ui_modes(self):
        for single_screen in (False, True):
            with self.subTest(single_screen=single_screen):
                if single_screen:
                    self.c.enable_single_screen()
                self.c.app.handle(self.c.event("bot_started"))
                account = self.c.store.get("u1").user_id
                before = self.c.demo.commerce.balance(account)
                self.c.callback("feedback:open")
                active = self.c.app.ui.current("u1")
                self.assertEqual(active.screen, "feedback_prompt")
                text = "  Очень удобно 😊\nХочу новые стили!\n "
                self.c.app.handle(self.c.event("message_created", text=text))
                row = self.rows()[-1]
                self.assertEqual(row["message"], text)
                self.assertEqual(row["source_screen"], "main")
                self.assertEqual(row["user_id"], account)
                self.assertIsNone(row["gallery_item_id"])
                self.assertIsNone(row["version_id"])
                self.assertEqual(self.c.transport.messages[-1][1], FEEDBACK_SAVED_TEXT)
                self.assertEqual(self.c.demo.commerce.balance(account), before)
                self.assertEqual(self.c.provider.calls, 0)
                self.assertEqual(self.c.transport.images, [])

    def test_selected_work_context_and_legacy_entry_preserved(self):
        self.c.enable_single_screen()
        self.c.generate_first()
        before = self.c.store.get("u1")
        calls = self.c.provider.calls
        self.c.callback("result:feedback")
        self.assertEqual(self.c.transport.edits[-1][1], FEEDBACK_PROMPT_TEXT)
        self.c.app.handle(self.c.event("message_created", text="Не могу найти оригинал"))
        row = self.rows()[0]
        self.assertEqual(row["source_screen"], "result")
        self.assertEqual(row["gallery_item_id"], before.current_gallery_item_id)
        self.assertEqual(row["version_id"], before.current_version_id)
        self.assertEqual(self.c.provider.calls, calls)

    def test_restart_while_awaiting_message_retains_source_screen(self):
        self.c.app.handle(self.c.event("bot_started"))
        self.c.callback("catalog:ideas")
        self.c.callback("feedback:open")
        self.c.app = MaxApplication(self.c.settings, self.c.database, self.c.demo,
                                    self.c.transport, self.c.store)
        self.c.app.handle(self.c.event("message_created", text="Добавьте стили"))
        self.assertEqual(self.rows()[0]["source_screen"], "ideas")
        self.assertEqual(self.c.provider.calls, 0)

    def test_photo_inside_feedback_is_not_downloaded_or_stored(self):
        self.c.app.handle(self.c.event("bot_started"))
        self.c.callback("feedback:open")
        self.c.app.handle(self.c.event("message_created", text="Мнение", image_url="https://example.test/photo"))
        self.assertEqual(self.c.transport.downloaded_urls, [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.c.store.get("u1").pending_action, "feedback:comment")
        self.assertEqual(self.c.provider.calls, 0)

    def test_empty_and_overlong_text_remain_in_feedback_without_generation(self):
        self.c.app.handle(self.c.event("bot_started"))
        self.c.callback("feedback:open")
        for text in (" \n ", "я" * 4001):
            self.c.app.handle(self.c.event("message_created", text=text))
            self.assertEqual(self.rows(), [])
            self.assertEqual(self.c.store.get("u1").pending_action, "feedback:comment")
        self.assertEqual(self.c.provider.calls, 0)

    def test_failed_acknowledgement_replay_has_one_record_no_generation(self):
        self.c.enable_single_screen()
        self.c.generate_first()
        calls = self.c.provider.calls
        self.c.callback("feedback:open")
        event = self.c.event("message_created", text="Спасибо 😊")
        self.c.transport.fail_next_message = True
        with self.assertRaises(MaxTransportError):
            self.c.app.handle(event)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.c.store.get("u1").pending_action, "feedback:comment")
        self.assertTrue(self.c.app.handle(event))
        self.assertFalse(self.c.app.handle(event))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.c.provider.calls, calls)

    def test_saved_event_after_crash_before_finish_never_becomes_correction(self):
        self.c.generate_first()
        self.c.callback("feedback:open")
        event = self.c.event("message_created", text="Спасибо")
        self.c.app.handle(event)
        self.c.store.finish_event(event.event_key, False)
        self.c.store.update("u1", pending_action=None)
        calls = self.c.provider.calls
        self.assertTrue(self.c.app.handle(event))
        self.assertEqual(self.c.provider.calls, calls)
        self.assertEqual(len(self.rows()), 1)

    def test_feedback_back_keeps_uploaded_sources_and_pending_instruction(self):
        self.c.app.handle(self.c.event("bot_started"))
        self.c.app.handle(self.c.event("message_created", image_url="https://example.test/source"))
        before = self.c.store.get("u1")
        self.c.callback("feedback:open")
        self.c.callback("feedback:back")
        after = self.c.store.get("u1")
        self.assertEqual(after.state, before.state)
        self.assertEqual(after.session_id, before.session_id)
        self.assertEqual(after.pending_action, before.pending_action)
        self.assertEqual(after.pending_prompt, before.pending_prompt)
        self.assertEqual(self.c.provider.calls, 0)

    def test_error_does_not_inherit_footer_from_result(self):
        self.c.generate_first()
        self.c.app._send_error("u1", "Техническая ошибка")
        self.assertNotIn(FEEDBACK_BUTTON_TEXT, [b.text for b in self.c.transport.messages[-1][2]])

    def test_followup_text_after_thanks_is_feedback_never_paid_correction(self):
        self.c.enable_single_screen()
        self.c.generate_first()
        calls = self.c.provider.calls
        self.c.callback("feedback:open")
        self.c.app.handle(self.c.event("message_created", text="Спасибо"))
        self.c.app.handle(self.c.event("message_created", text="Ещё хочу больше стилей"))
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(row["source_screen"] == "result" for row in self.rows()))
        self.assertEqual(self.c.provider.calls, calls)

    def test_feedback_back_does_not_create_checkout_or_discard_pending_request(self):
        self.c.app.handle(self.c.event("bot_started"))
        self.c.store.update("u1", pending_action="checkout", pending_request_id="synthetic-pending",
                            pending_prompt="Точное ожидающее изменение")
        self.c.app._send_message("u1", "Выберите вариант", screen="pending_payment_offer")
        self.c.callback("feedback:open")
        self.c.callback("feedback:back")
        after = self.c.store.get("u1")
        self.assertEqual(after.pending_request_id, "synthetic-pending")
        self.assertEqual(after.pending_prompt, "Точное ожидающее изменение")
        self.assertEqual(after.pending_action, "checkout")
        with self.c.database.read() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM payment_orders").fetchone()[0], 0)
        self.assertEqual(self.c.provider.calls, 0)

    def test_real_navigation_main_upload_result_works_work_history_more_and_ideas(self):
        self.c.enable_single_screen()

        def check_footer(expected=True):
            action = next(kind for kind, _ in reversed(self.c.transport.timeline)
                          if kind in {"send_message", "edit_message", "send_image", "edit_image"})
            if action == "send_message":
                buttons = self.c.transport.messages[-1][2]
            elif action == "edit_message":
                buttons = self.c.transport.edits[-1][2]
            elif action == "send_image":
                buttons = self.c.transport.images[-1][3]
            else:
                buttons = self.c.transport.image_edits[-1][3]
            footers = [button for button in buttons if button.text == FEEDBACK_BUTTON_TEXT]
            self.assertEqual(len(footers), int(expected))
            if expected:
                index = -2 if buttons[-1].text == "← Назад" else -1
                self.assertEqual(buttons[index].text, FEEDBACK_BUTTON_TEXT)
                self.assertIsNone(buttons[index].row)
            if any(button.text == "← Назад" for button in buttons):
                self.assertEqual(buttons[-1].text, "← Назад")

        self.c.app.handle(self.c.event("bot_started"))
        check_footer()
        self.c.callback("upload:ready")
        check_footer()
        self.c.generate_first()
        check_footer()
        item = self.c.store.get("u1").current_gallery_item_id
        for action in ("studio:works", f"works:open:{item}", "work:history",
                       "nav:back:work", "work:more", "catalog:ideas", "ideas:backgrounds"):
            self.c.callback(action)
            check_footer(expected=action != "work:more")
