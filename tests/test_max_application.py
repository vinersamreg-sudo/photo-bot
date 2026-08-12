import shutil
import tempfile
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from app.config import Settings
from app.database import Database
from app.demo_service import DemoService
from app.domain import (
    DailyBudgetError,
    DeliveryError,
    ImageTooLargeError,
    PolicyRejectedError,
    ProviderInvalidRequestError,
    ProviderQuotaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.edit_intent import EditPlan
from app.image_provider import FakeImageProvider
from app.max_adapter import Button, result_actions
from app.max_application import (
    CORRECTION_REQUEST_TEXT,
    MaxApplication,
    NAV_HISTORY,
    NAV_WORK,
    NAV_WORKS,
    OWNER_ONLY_TEXT,
    PENDING_EDIT_PAYMENT_TEXT,
    PAYMENT_LINK_TEXT,
    PAYMENT_OFFER_TEXT,
    PHOTO_ACCEPTED_TEXT,
    PROCESSING_TEXT,
)
from app.max_conversation import MaxConversationStore
from app.max_transport import MaxIncomingEvent, MaxTransportError, parse_update
from app.max_ui_shell import parse_versioned_action
from app.storage import PrivateStorage
from app.watermark import WatermarkService
from app.payments import build_payment_service


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeMaxTransport:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.messages = []
        self.images = []
        self.files = []
        self.edits = []
        self.image_edits = []
        self.deletes = []
        self.callbacks = []
        self.image_delivery = True
        self.file_delivery = True
        self.fail_next_message = False
        self.fail_next_edit = False
        self.on_send_message = None
        self.timeline = []
        self.sources_by_url = {}
        self.failed_download_urls = set()
        self.downloaded_urls = []

    def send_message(self, user_id, text, buttons=(), **kwargs):
        if self.fail_next_message:
            self.fail_next_message = False
            raise MaxTransportError("fake send failure")
        if self.on_send_message:
            self.on_send_message(text)
        message_id = f"sent-{len(self.messages) + 1}"
        self.messages.append((user_id, text, tuple(buttons), kwargs, message_id))
        self.timeline.append(("send_message", message_id))
        return message_id

    def edit_message(self, message_id, text, buttons=(), **_kwargs):
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise MaxTransportError("fake edit failure")
        self.edits.append((message_id, text, tuple(buttons)))
        self.timeline.append(("edit_message", message_id))

    def edit_image(self, message_id, image, caption, buttons, **_kwargs):
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise MaxTransportError("fake edit failure")
        self.image_edits.append(
            (message_id, Path(image), caption, tuple(buttons))
        )
        self.timeline.append(("edit_image", message_id))

    def delete_message(self, message_id):
        self.deletes.append(message_id)
        self.timeline.append(("delete_message", message_id))

    def answer_callback(self, callback_id, notification):
        self.callbacks.append((callback_id, notification))

    def download_image(self, url, destination, _max_bytes):
        self.downloaded_urls.append(url)
        if url in self.failed_download_urls:
            raise MaxTransportError("fake download failure")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.sources_by_url.get(url, self.source), destination)
        return destination

    def send_image(self, user_id, image, caption, buttons, **_kwargs):
        self.images.append((user_id, Path(image), caption, tuple(buttons)))
        if not self.image_delivery:
            return None
        message_id = f"image-{len(self.images)}"
        self.timeline.append(("send_image", message_id))
        return message_id

    def send_file(self, user_id, file_path, caption, buttons):
        self.files.append((user_id, Path(file_path), caption, tuple(buttons)))
        if not self.file_delivery:
            return None
        message_id = f"file-{len(self.files)}"
        self.timeline.append(("send_file", message_id))
        return message_id


class MaxApplicationTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        for name in ("data", "logs", "temp"):
            (self.base / name).mkdir()
        self.source = self.base / "synthetic.png"
        Image.new("RGB", (320, 240), "#dde7f2").save(self.source)
        self.settings = Settings(
            "", "fake-image-edit-v1", "test", self.base,
            demo_min_request_interval_seconds=1,
            max_owner_user_ids=("u1",),
            image_direct_prompt_enabled=False,
            max_single_screen_ui_enabled=False,
        )
        self.clock = Clock()
        self.database = Database(self.settings.database_path)
        self.storage = PrivateStorage(
            self.settings.users_dir,
            self.settings.max_source_file_size_mb * 1024 * 1024,
        )
        self.provider = FakeImageProvider()
        self.provider.prompts = []
        self.provider.source_batches = []
        original_edit_many = self.provider.edit_many

        def record_edit_many(source_paths, prompt):
            self.provider.source_batches.append(tuple(source_paths))
            self.provider.prompts.append(prompt)
            return original_edit_many(source_paths, prompt)

        self.provider.edit_many = record_edit_many
        self.demo = DemoService(
            self.settings,
            self.database,
            self.storage,
            WatermarkService("ОБРАЗЕЦ", 1024, "JPEG", 82),
            self.provider,
            clock=self.clock,
        )
        self.transport = FakeMaxTransport(self.source)
        self.store = MaxConversationStore(self.database, self.clock)
        self.app = MaxApplication(
            self.settings, self.database, self.demo, self.transport, self.store
        )
        self.sequence = 0

    def event(
        self, event_type, *, text=None, image_url=None, image_urls=(),
        image_attachment_count=0, action=None,
    ):
        self.sequence += 1
        key = (
            f"callback:cb-{self.sequence}"
            if event_type == "message_callback"
            else f"message:mid-{self.sequence}"
            if event_type == "message_created"
            else f"start:c1:u1:{self.sequence}"
        )
        message_id = f"mid-{self.sequence}" if event_type != "bot_started" else None
        callback_text = "Сообщение с кнопками"
        if event_type == "message_callback":
            active = self.store.active_keyboards("u1")
            if active:
                message_id, callback_text = active[-1]
        return MaxIncomingEvent(
            event_type, key, "u1", "c1", self.sequence,
            message_id=message_id,
            text=(
                text
                if text is not None
                else callback_text
                if event_type == "message_callback"
                else None
            ),
            image_url=image_url,
            image_urls=tuple(image_urls),
            image_attachment_count=image_attachment_count,
            callback_id=f"cb-{self.sequence}" if event_type == "message_callback" else None,
            callback_payload=action,
        )

    def callback(self, action):
        event = self.event("message_callback", action=action)
        self.app.handle(event)
        return event

    def test_non_owner_gets_closed_testing_message_without_dialog_or_generation(self) -> None:
        event = MaxIncomingEvent(
            "message_created", "message:outsider", "outsider", "c2", 1,
            message_id="outsider-message", text="/start",
        )
        self.assertTrue(self.app.handle(event))
        self.assertEqual(self.transport.messages[-1][0:2], ("outsider", OWNER_ONLY_TEXT))
        self.assertIsNone(self.store.get("outsider"))
        self.assertEqual(self.provider.calls, 0)
        self.assertFalse(self.app.handle(event))

    def test_non_owner_callback_is_acknowledged_and_closed(self) -> None:
        event = MaxIncomingEvent(
            "message_callback", "callback:outsider", "outsider", "c2", 1,
            callback_id="callback-id", callback_payload="custom",
        )
        self.assertTrue(self.app.handle(event))
        self.assertEqual(self.transport.callbacks[-1][0], "callback-id")
        self.assertEqual(self.transport.messages[-1][0:2], ("outsider", OWNER_ONLY_TEXT))
        self.assertIsNone(self.store.get("outsider"))

    def test_public_access_allows_non_owner_start_without_generation(self) -> None:
        self.app.settings = replace(
            self.settings,
            max_public_access_enabled=True,
        )
        event = MaxIncomingEvent(
            "message_created", "message:public", "public-user", "c2", 1,
            message_id="public-message", text="/start",
        )

        self.assertTrue(self.app.handle(event))

        self.assertIsNotNone(self.store.get("public-user"))
        self.assertNotEqual(self.transport.messages[-1][1], OWNER_ONLY_TEXT)
        self.assertEqual(self.provider.calls, 0)

    def test_start_deactivates_keyboard_from_previous_state(self) -> None:
        self.app.handle(self.event("bot_started"))
        previous_message_id = self.transport.messages[-1][4]
        self.assertEqual(
            self.store.active_keyboards("u1"),
            [(previous_message_id, self.transport.messages[-1][1])],
        )

        self.app.handle(self.event("message_created", text="/start"))

        self.assertIn(
            (previous_message_id, self.transport.messages[0][1], ()),
            self.transport.edits,
        )
        active = self.store.active_keyboards("u1")
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active[0][0], previous_message_id)

    def test_all_keyboards_survive_restart_and_are_deactivated_together(self) -> None:
        self.store.get_or_create("u1", "c1")
        first_id = self.app._send_message(
            "u1", "РљР°СЂС‚РѕС‡РєР° 1", (Button("РћС‚РєСЂС‹С‚СЊ", "work:1"),)
        )
        second_id = self.app._send_message(
            "u1", "РљР°СЂС‚РѕС‡РєР° 2", (Button("РћС‚РєСЂС‹С‚СЊ", "work:2"),)
        )
        restarted_store = MaxConversationStore(self.database, self.clock)
        restarted = MaxApplication(
            self.settings,
            self.database,
            self.demo,
            self.transport,
            restarted_store,
        )

        restarted.handle(self.event("message_created", text="/start"))

        edited_ids = {
            message_id
            for message_id, _text, buttons in self.transport.edits
            if not buttons
        }
        self.assertTrue({first_id, second_id}.issubset(edited_ids))
        active = restarted_store.active_keyboards("u1")
        self.assertEqual(len(active), 1)
        self.assertNotIn(active[0][0], {first_id, second_id})

    def test_callback_source_keyboard_is_deactivated_once(self) -> None:
        self.store.get_or_create("u1", "c1")
        source_id = self.app._send_message(
            "u1", "Р”РµР№СЃС‚РІРёРµ", (Button("Р’ РјРµРЅСЋ", "menu"),)
        )
        event = replace(
            self.event("message_callback", action="menu"),
            message_id=source_id,
            text="Р”РµР№СЃС‚РІРёРµ",
        )

        self.app.handle(event)

        source_edits = [
            edit for edit in self.transport.edits if edit[0] == source_id
        ]
        self.assertEqual(source_edits, [(source_id, "Р”РµР№СЃС‚РІРёРµ", ())])

    def onboard_to_prompt(self) -> None:
        self.app.handle(self.event("bot_started"))
        self.assertEqual(self.store.get("u1").state, "main_menu")
        menu = self.transport.messages[-1]
        self.assertIn("Что хотите сделать с фотографией?", menu[1])
        self.assertEqual(
            [button.text for button in menu[2]],
            [
                "📁 Мои работы",
                "📄 Публичная оферта",
                "🔐 Обработка персональных данных",
            ],
        )
        self.callback("upload:ready")
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        upload = self.transport.messages[-1]
        self.assertIn("Прикрепите фотографию через скрепку 📎", upload[1])
        self.assertIn("до 2 фотографий для одной обработки", upload[1])
        self.assertIn("вправе его использовать", upload[1])
        self.assertEqual(
            [(button.text, button.action) for button in upload[2]],
            [
                ("📄 Публичная оферта", "https://ravuna.ru/legal/offer.html"),
                (
                    "🔐 Обработка персональных данных",
                    "https://ravuna.ru/legal/personal-data.html",
                ),
                ("← Назад", "nav:back:main"),
            ],
        )
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/source")
        )
        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")
        self.assertTrue(self.store.legal_is_current("u1"))

    def generate_first(self) -> None:
        self.app.handle(self.event("bot_started"))
        self.app.handle(
            self.event(
                "message_created",
                text="Сделай светлый фон",
                image_url="https://iu.oneme.ru/source",
            )
        )
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.store.get("u1").state, "result_ready")
        self.assertFalse(
            any(message[1] == PHOTO_ACCEPTED_TEXT for message in self.transport.messages)
        )
        self.assertFalse(any("Я понял задачу" in message[1] for message in self.transport.messages))
        self.assertFalse(any("Бесплатных вариантов доступно" in message[1] for message in self.transport.messages))

    def paid_application(self):
        paid_settings = replace(
            self.settings,
            payments_enabled=True,
            payment_provider="robokassa",
            payment_webhook_listener_enabled=True,
            payment_webhook_enabled=True,
            payment_result_url="https://example.test/payments/robokassa/result",
            robokassa_merchant_login="ravuna-test",
            robokassa_password1="one",
            robokassa_password2="two",
        )
        payments = build_payment_service(
            paid_settings, self.database, clock=self.clock
        )
        return (
            MaxApplication(
                paid_settings,
                self.database,
                self.demo,
                self.transport,
                self.store,
                payment_service=payments,
            ),
            payments,
        )

    def enable_single_screen(self) -> None:
        self.settings = replace(
            self.settings,
            max_single_screen_ui_enabled=True,
        )
        self.app.settings = self.settings

    def test_single_screen_callback_edits_current_message_without_provider_call(self) -> None:
        self.enable_single_screen()
        self.app.handle(self.event("bot_started"))
        active = self.app.ui.current("u1")
        message_count = len(self.transport.messages)
        provider_calls = self.provider.calls

        self.callback("studio:works")

        updated = self.app.ui.current("u1")
        self.assertEqual(len(self.transport.messages), message_count)
        self.assertEqual(updated.message_id, active.message_id)
        self.assertGreater(updated.revision, active.revision)
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertIn("Здесь пока пусто", self.transport.edits[-1][1])

    def test_single_screen_repeat_start_sends_new_complete_active_menu(self) -> None:
        self.enable_single_screen()
        self.app.handle(self.event("bot_started"))
        active = self.app.ui.current("u1")
        message_count = len(self.transport.messages)

        self.app.handle(self.event("message_created", text="/start"))
        after_command = self.app.ui.current("u1")
        self.app.handle(self.event("message_created", text="Старт"))

        current = self.app.ui.current("u1")
        self.assertEqual(self.store.get("u1").state, "main_menu")
        self.assertNotEqual(after_command.message_id, active.message_id)
        self.assertNotEqual(current.message_id, after_command.message_id)
        self.assertEqual(len(self.transport.messages), message_count + 2)
        self.assertEqual(
            self.transport.deletes,
            [active.message_id, after_command.message_id],
        )
        self.assertEqual(current.message_id, self.transport.messages[-1][4])
        self.assertIn(
            "Что хотите сделать с фотографией?",
            self.transport.messages[-1][1],
        )
        self.assertIn(
            "📁 Мои работы",
            [button.text for button in self.transport.messages[-1][2]],
        )
        message_count = len(self.transport.messages)
        self.callback("studio:works")

        updated = self.app.ui.current("u1")
        self.assertEqual(updated.message_id, current.message_id)
        self.assertEqual(len(self.transport.messages), message_count)
        self.assertEqual(self.transport.edits[-1][0], current.message_id)
        self.assertEqual(self.provider.calls, 0)

    def test_single_screen_finished_preview_is_new_native_image_message(self) -> None:
        self.enable_single_screen()
        self.generate_first()

        self.assertEqual(len(self.transport.messages), 2)
        self.assertEqual(len(self.transport.images), 1)
        self.assertEqual(len(self.transport.image_edits), 0)
        active = self.app.ui.current("u1")
        processing_message_id = self.transport.messages[-1][4]
        self.assertEqual(self.transport.messages[-1][1], PROCESSING_TEXT)
        self.assertIn(
            "Обрабатываю фотографию", self.transport.messages[-1][1]
        )
        self.assertIn("около 1 минуты", self.transport.messages[-1][1])
        self.assertEqual(
            self.transport.deletes,
            [self.transport.messages[0][4], processing_message_id],
        )
        self.assertNotIn("mid-2", self.transport.deletes)
        self.assertEqual(active.message_id, "image-1")
        self.assertNotEqual(processing_message_id, active.message_id)
        self.assertEqual(self.transport.images[-1][2], result_actions(1).text)
        self.assertEqual(
            self.demo.commerce.balance(self.store.get("u1").user_id).available,
            1,
        )

    def test_single_screen_failed_fresh_preview_keeps_processing_and_credit(self) -> None:
        self.enable_single_screen()
        self.transport.image_delivery = False

        self.app.handle(self.event("bot_started"))
        self.app.handle(
            self.event(
                "message_created",
                text="Сделай светлый фон",
                image_url="https://iu.oneme.ru/source",
            )
        )

        processing_message_id = self.transport.messages[-1][4]
        active = self.app.ui.current("u1")
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT status FROM generation_attempts"
            ).fetchone()
        self.assertEqual(attempt[0], "delivery_failed")
        self.assertEqual(
            self.demo.commerce.balance(self.store.get("u1").user_id).available,
            2,
        )
        self.assertEqual(active.message_id, processing_message_id)
        self.assertNotIn(processing_message_id, self.transport.deletes)
        self.assertEqual(len(self.transport.image_edits), 0)
        self.assertEqual(
            self.transport.edits[-1][1],
            "Изображение создано, но не удалось отправить его в MAX. Попытка не списана.",
        )

    def test_single_screen_edit_fallback_sends_once_and_deletes_old_bot_message(self) -> None:
        self.enable_single_screen()
        self.app.handle(self.event("bot_started"))
        previous = self.app.ui.current("u1").message_id
        self.transport.fail_next_edit = True

        self.callback("studio:works")

        self.assertEqual(len(self.transport.messages), 2)
        self.assertEqual(self.transport.deletes, [previous])

    def test_single_screen_stale_callback_has_no_side_effects(self) -> None:
        self.enable_single_screen()
        self.app.handle(self.event("bot_started"))
        self.callback("studio:works")
        message_count = len(self.transport.messages)
        edit_count = len(self.transport.edits)
        provider_calls = self.provider.calls
        stale = replace(
            self.event("message_callback", action="custom"),
            message_id="obsolete-bot-message",
        )

        self.app.handle(stale)

        self.assertEqual(len(self.transport.messages), message_count)
        self.assertEqual(len(self.transport.edits), edit_count)
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertEqual(self.transport.callbacks[-1][1], "Экран уже изменился")

    def test_share_screen_uses_clipboard_payloads_and_keeps_result_in_place(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        active_message = self.app.ui.current("u1").message_id
        message_count = len(self.transport.messages)
        provider_calls = self.provider.calls
        balance = self.demo.commerce.balance(self.store.get("u1").user_id).available
        with self.database.read() as connection:
            preview = Path(
                connection.execute(
                    "SELECT demo_result_path FROM generation_attempts"
                ).fetchone()[0]
            )

        self.callback("result:share")

        edit = self.transport.image_edits[-1]
        self.assertEqual(edit[1], preview)
        self.assertEqual(edit[0], active_message)
        self.assertEqual(len(self.transport.messages), message_count)
        clipboard = [button for button in edit[3] if button.kind == "clipboard"]
        self.assertEqual(
            [button.text for button in clipboard],
            ["📋 Скопировать приглашение", "📋 Скопировать только ссылку"],
        )
        self.assertIn("Я обработал фотографию", clipboard[0].action)
        self.assertIn("Попробуйте:\nhttps://max.ru/", clipboard[0].action)
        self.assertTrue(clipboard[1].action.startswith("https://max.ru/"))
        self.assertIn("?start=ref_", clipboard[1].action)
        self.assertNotIn("/original", "\n".join(button.action for button in clipboard))
        self.assertFalse(
            any("https://max.ru/:share" in button.action for button in edit[3])
        )
        self.assertFalse(any(button.text == "Открыть ссылку" for button in edit[3]))
        with self.database.read() as connection:
            code = connection.execute("SELECT referral_code FROM referral_codes").fetchone()[0]
            self.assertLessEqual(len(f"ref_{code}"), 128)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM attribution_events WHERE event_type='share_opened'"
                ).fetchone()[0],
                1,
            )
        self.callback("nav:back:work")
        self.assertEqual(self.app.ui.current("u1").message_id, active_message)
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertEqual(
            self.demo.commerce.balance(self.store.get("u1").user_id).available,
            balance,
        )

    def test_referral_rewards_two_edits_after_invitee_first_delivered_preview(self) -> None:
        self.enable_single_screen()
        self.app.settings = replace(
            self.settings,
            max_public_access_enabled=True,
        )
        self.generate_first()
        inviter_dialog = self.store.get("u1")
        inviter_active_message = self.app.ui.current("u1").message_id
        code = self.app.referrals.get_or_create_code(inviter_dialog.user_id)
        balance_before = self.demo.commerce.balance(inviter_dialog.user_id).available
        entitlements_before = self.demo.commerce.entitlement_balance(
            inviter_dialog.user_id
        ).available

        self.app.handle(
            MaxIncomingEvent(
                "bot_started",
                "start:c2:u2:1",
                "u2",
                "c2",
                1,
                start_payload=f"ref_{code}",
            )
        )
        self.app.handle(
            MaxIncomingEvent(
                "message_created",
                "message:u2-photo",
                "u2",
                "c2",
                2,
                message_id="u2-photo",
                text="Улучшить фон",
                image_url="https://iu.oneme.ru/u2-source",
            )
        )

        self.assertEqual(
            self.demo.commerce.balance(inviter_dialog.user_id).available,
            balance_before + 2,
        )
        self.assertEqual(
            self.demo.commerce.entitlement_balance(inviter_dialog.user_id).available,
            entitlements_before,
        )
        self.assertEqual(self.app.ui.current("u1").message_id, inviter_active_message)
        self.assertTrue(
            any(
                message[0] == "u1" and "начислено 2 бонусные обработки" in message[1]
                for message in self.transport.messages
            )
        )
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM bonus_credit_transactions").fetchone()[0],
                1,
            )

    def test_single_screen_gallery_number_opens_correct_work_in_place(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        result_message = self.app.ui.current("u1").message_id
        deletes_before = list(self.transport.deletes)

        self.callback("studio:works")

        works_message = self.app.ui.current("u1").message_id
        sheet = self.transport.images[-1]
        self.assertNotEqual(works_message, result_message)
        self.assertEqual(self.transport.deletes, deletes_before)
        number_button = next(
            button for button in sheet[3] if button.text == "Открыть 1"
        )
        _revision, action = parse_versioned_action(number_button.action)
        expected_item = action.rsplit(":", 1)[1]
        self.callback(action)

        dialog = self.store.get("u1")
        self.assertEqual(dialog.current_gallery_item_id, expected_item)
        self.assertEqual(dialog.pending_action, NAV_WORK)
        self.assertEqual(self.app.ui.current("u1").message_id, works_message)
        self.assertEqual(len(self.transport.messages), 2)

    def test_single_screen_correction_creates_new_progress_after_user_text(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        result_message = self.app.ui.current("u1").message_id
        deletes_before = list(self.transport.deletes)
        self.callback("result:correct")
        old_active = self.app.ui.current("u1").message_id
        self.assertNotEqual(old_active, result_message)
        self.assertEqual(self.transport.deletes, deletes_before)
        messages_before = len(self.transport.messages)
        image_edits_before = len(self.transport.image_edits)
        self.clock.advance(2)

        correction_event = self.event(
            "message_created", text="Сделай лицо естественнее"
        )
        self.app.handle(correction_event)

        active = self.app.ui.current("u1")
        processing_message_id = self.transport.messages[-1][4]
        self.assertEqual(len(self.transport.messages), messages_before + 1)
        self.assertEqual(self.transport.messages[-1][1], PROCESSING_TEXT)
        self.assertEqual(active.message_id, f"image-{len(self.transport.images)}")
        self.assertNotEqual(processing_message_id, active.message_id)
        self.assertNotEqual(active.message_id, old_active)
        self.assertIn(old_active, self.transport.deletes)
        self.assertNotIn(result_message, self.transport.deletes)
        self.assertIn(processing_message_id, self.transport.deletes)
        self.assertNotIn(correction_event.message_id, self.transport.deletes)
        self.assertEqual(len(self.transport.image_edits), image_edits_before)
        self.assertEqual(self.transport.images[-1][2], result_actions(0).text)

    def test_single_screen_gallery_page_switch_changes_sheet_and_mapping(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        dialog = self.store.get("u1")
        now = self.clock().isoformat()
        with self.database.read() as connection:
            gallery_id = connection.execute(
                "SELECT gallery_id FROM gallery_items WHERE user_id=? LIMIT 1",
                (dialog.user_id,),
            ).fetchone()[0]
        with self.database.transaction() as connection:
            for index in range(1, 20):
                preview = self.base / f"gallery-page-preview-{index}.jpg"
                Image.new("RGB", (80, 60), (index * 9 % 255, 70, 140)).save(preview)
                work_id = f"gallery-page-work-{index:02d}"
                version_id = f"gallery-page-version-{index:02d}"
                connection.execute(
                    """INSERT INTO gallery_items(
                           id,gallery_id,user_id,title,created_at,updated_at,
                           original_source_path,storage_root_path,cover_preview_path,
                           generation_count,current_best_version_id,retention_until
                       ) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (
                        work_id, gallery_id, dialog.user_id, f"Работа {index}",
                        now, f"{now}-{index:02d}", "private-source", "private-root",
                        str(preview), version_id, now,
                    ),
                )
                connection.execute(
                    """INSERT INTO gallery_versions(
                           id,gallery_item_id,version_number,source_path,prompt,
                           effective_prompt,provider,model,preview_watermarked_path,
                           original_path,created_at,status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'succeeded')""",
                    (
                        version_id, work_id, 1, "private-source", "prompt", "prompt",
                        "fake", "fake", str(preview), "private-original", now,
                    ),
                )

        self.callback("studio:works")
        first_send = self.transport.images[-1]
        first_sheet = first_send[1].read_bytes()
        first_ids = {
            parse_versioned_action(button.action)[1].rsplit(":", 1)[1]
            for button in first_send[3]
            if button.text.startswith("Открыть ")
        }
        self.callback("works:page:2")
        second_edit = self.transport.image_edits[-1]
        second_ids = {
            parse_versioned_action(button.action)[1].rsplit(":", 1)[1]
            for button in second_edit[3]
            if button.text.startswith("Открыть ")
        }

        self.assertNotEqual(first_send[1], second_edit[1])
        self.assertNotEqual(first_sheet, second_edit[1].read_bytes())
        self.assertTrue(first_ids.isdisjoint(second_ids))
        open_second = next(
            parse_versioned_action(button.action)[1]
            for button in second_edit[3]
            if button.text == "Открыть 1"
        )
        expected = open_second.rsplit(":", 1)[1]
        self.callback(open_second)
        self.assertEqual(self.store.get("u1").current_gallery_item_id, expected)
        self.callback("nav:back:works")
        self.assertIn("Страница 2 из 4", self.transport.image_edits[-1][2])
        self.callback("works:page:4")
        last_buttons = [
            button.text
            for button in self.transport.image_edits[-1][3]
            if button.text.startswith("Открыть ")
        ]
        self.assertEqual(last_buttons, ["Открыть 1", "Открыть 2"])

    def test_start_direct_upload_details_and_implicit_consent(self) -> None:
        self.app.handle(self.event("message_created", text="/start"))
        self.assertEqual(self.store.get("u1").state, "main_menu")
        start_message = self.transport.messages[-1]
        self.assertIn("Что хотите сделать с фотографией?", start_message[1])
        self.assertIn("Примеры:", start_message[1])
        self.assertIn("прикрепите фотографию через скрепку внизу чата", start_message[1])
        self.assertIn("до 2 фотографий для одной обработки", start_message[1])
        self.assertNotIn("Фото принято", start_message[1])
        self.assertEqual(
            [(button.text, button.action) for button in start_message[2]],
            [
                ("📁 Мои работы", "studio:works"),
                (
                    "📄 Публичная оферта",
                    "https://ravuna.ru/legal/offer.html",
                ),
                (
                    "🔐 Обработка персональных данных",
                    "https://ravuna.ru/legal/personal-data.html",
                ),
            ],
        )
        self.assertFalse(
            any(button.action == "upload:ready" for button in start_message[2])
        )
        self.assertFalse(
            any(button.action == "legal:accept_all" for button in start_message[2])
        )
        self.assertEqual(self.provider.calls, 0)

        balance_before = self.demo.commerce.balance(
            self.store.get("u1").user_id
        ).available
        provider_calls = self.provider.calls
        self.callback("upload:ready")
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        self.assertIn(
            "Прикрепите фотографию через скрепку 📎",
            self.transport.messages[-1][1],
        )
        self.assertEqual(
            [(button.text, button.action) for button in self.transport.messages[-1][2]],
            [
                ("📄 Публичная оферта", "https://ravuna.ru/legal/offer.html"),
                (
                    "🔐 Обработка персональных данных",
                    "https://ravuna.ru/legal/personal-data.html",
                ),
                ("← Назад", "nav:back:main"),
            ],
        )
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertEqual(
            self.demo.commerce.balance(self.store.get("u1").user_id).available,
            balance_before,
        )

        invalid = self.base / "invalid.bin"
        invalid.write_text("not image", encoding="utf-8")
        self.transport.source = invalid
        accepted_after_persistence = []

        def verify_photo_is_persisted_before_acceptance(text):
            if text == PHOTO_ACCEPTED_TEXT:
                with self.database.read() as connection:
                    row = connection.execute(
                        "SELECT source_file_path FROM demo_sessions"
                    ).fetchone()
                accepted_after_persistence.append(
                    bool(row and Path(row["source_file_path"]).is_file())
                )

        self.transport.on_send_message = verify_photo_is_persisted_before_acceptance
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/invalid")
        )
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(accepted_after_persistence, [])
        with self.database.read() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM legal_consents").fetchone()[0], 0)

        self.transport.source = self.source
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/source")
        )
        self.assertEqual(accepted_after_persistence, [True])
        self.assertEqual(self.transport.messages[-1][1], PHOTO_ACCEPTED_TEXT)
        self.assertTrue(self.store.legal_is_current("u1"))

    def test_two_photo_flow_rejects_third_and_generates_once(self) -> None:
        self.onboard_to_prompt()
        first_buttons = self.transport.messages[-1][2]
        self.assertIn(
            ("➕ Добавить второе фото", "source:add-second"),
            [(button.text, button.action) for button in first_buttons],
        )
        self.callback("source:add-second")
        self.assertEqual(self.store.get("u1").pending_action, "second_source")
        second = self.base / "second-max.png"
        Image.new("RGB", (240, 320), "red").save(second)
        self.transport.source = second

        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/second")
        )

        dialog = self.store.get("u1")
        self.assertEqual(dialog.state, "waiting_for_prompt")
        self.assertEqual(dialog.pending_action, "two_sources")
        self.assertIn("Фото 1 и Фото 2 приняты", self.transport.messages[-1][1])
        provider_calls = self.provider.calls
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/third")
        )
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertEqual(self.transport.messages[-1][1], "Можно использовать максимум 2 фотографии")

        prompt = "Одень меня как на втором фото"
        self.app.handle(self.event("message_created", text=prompt))

        self.assertEqual(self.provider.calls, provider_calls + 1)
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT * FROM generation_attempts ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            version = connection.execute(
                "SELECT * FROM gallery_versions WHERE attempt_id=?", (attempt["id"],)
            ).fetchone()
        self.assertEqual(attempt["prompt"], prompt)
        self.assertIsNotNone(attempt["secondary_source_path"])
        self.assertEqual(version["secondary_source_path"], attempt["secondary_source_path"])

    def test_raw_max_update_with_two_images_reaches_edit_many_once(self) -> None:
        prompt = "На фото 1 добавь мужчину из фото 2"
        direct_settings = replace(
            self.settings,
            image_direct_prompt_enabled=True,
            image_subject_preserve_guard_enabled=False,
        )
        self.app.settings = direct_settings
        self.demo.settings = direct_settings
        first_url = "https://iu.oneme.ru/one-message-first"
        second_url = "https://iu.oneme.ru/one-message-second"
        second = self.base / "one-message-second.png"
        Image.new("RGB", (240, 320), "red").save(second)
        self.transport.sources_by_url = {
            first_url: self.source,
            second_url: second,
        }
        event = parse_update(
            {
                "update_type": "message_created",
                "timestamp": 10,
                "message": {
                    "sender": {"user_id": "u1"},
                    "recipient": {"chat_id": "c1"},
                    "body": {
                        "mid": "raw-two-images",
                        "text": prompt,
                        "attachments": [
                            {"type": "image", "payload": {"url": first_url}},
                            {"type": "image", "payload": {"url": second_url}},
                        ],
                    },
                },
            }
        )

        self.assertTrue(self.app.handle(event))

        self.assertEqual(self.transport.downloaded_urls, [first_url, second_url])
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.provider.prompts, [prompt])
        self.assertEqual(len(self.provider.source_batches), 1)
        self.assertEqual(len(self.provider.source_batches[0]), 2)
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT * FROM generation_attempts ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(attempt["prompt"], prompt)
        self.assertIsNotNone(attempt["secondary_source_path"])
        self.assertEqual(
            self.demo.commerce.balance(self.store.get("u1").user_id).available,
            1,
        )

    def test_raw_max_update_with_one_image_keeps_existing_flow(self) -> None:
        image_url = "https://iu.oneme.ru/one-message-only"
        event = parse_update(
            {
                "update_type": "message_created",
                "timestamp": 10,
                "message": {
                    "sender": {"user_id": "u1"},
                    "recipient": {"chat_id": "c1"},
                    "body": {
                        "mid": "raw-one-image",
                        "text": "Улучши фото",
                        "attachments": [
                            {"type": "image", "payload": {"url": image_url}},
                        ],
                    },
                },
            }
        )

        self.assertTrue(self.app.handle(event))

        self.assertEqual(self.transport.downloaded_urls, [image_url])
        self.assertEqual(self.provider.calls, 1)
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT prompt,secondary_source_path FROM generation_attempts"
            ).fetchone()
        self.assertEqual(attempt["prompt"], "Улучши фото")
        self.assertIsNone(attempt["secondary_source_path"])

    def test_raw_max_update_with_more_than_two_images_is_rejected(self) -> None:
        event = parse_update(
            {
                "update_type": "message_created",
                "timestamp": 11,
                "message": {
                    "sender": {"user_id": "u1"},
                    "recipient": {"chat_id": "c1"},
                    "body": {
                        "mid": "raw-three-images",
                        "text": "prompt",
                        "attachments": [
                            {"type": "image", "payload": {"url": f"https://iu.oneme.ru/{index}"}}
                            for index in range(3)
                        ],
                    },
                },
            }
        )

        self.assertTrue(self.app.handle(event))

        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.transport.downloaded_urls, [])
        self.assertEqual(
            self.transport.messages[-1][1],
            "Можно использовать максимум 2 фотографии",
        )

    def test_second_image_download_failure_never_generates_or_debits(self) -> None:
        prompt = "На фото 1 добавь мужчину из фото 2"
        first_url = "https://iu.oneme.ru/download-first"
        second_url = "https://iu.oneme.ru/download-second"
        with self.database.read() as connection:
            ledger_before = connection.execute(
                "SELECT COUNT(*) FROM credit_ledger"
            ).fetchone()[0]
        self.transport.failed_download_urls.add(second_url)
        event = self.event(
            "message_created",
            text=prompt,
            image_url=first_url,
            image_urls=(first_url, second_url),
            image_attachment_count=2,
        )

        self.assertTrue(self.app.handle(event))

        self.assertEqual(self.provider.calls, 0)
        dialog = self.store.get("u1")
        self.assertIsNone(dialog.user_id if dialog else None)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM generation_attempts").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM demo_sessions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0],
                ledger_before,
            )
        self.assertIn("Попытка не списана", self.transport.messages[-1][1])

    def test_two_photo_pending_payment_resumes_exact_sources_and_prompt(self) -> None:
        self.onboard_to_prompt()
        self.callback("source:add-second")
        second = self.base / "second-pending.png"
        Image.new("RGB", (240, 320), "red").save(second)
        self.transport.source = second
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/second")
        )
        dialog = self.store.get("u1")
        with self.database.transaction() as connection:
            self.demo.commerce.adjust_generation_credits(
                connection,
                user_id=dialog.user_id,
                delta=-2,
                reason="test_exhaustion",
                idempotency_key="test:two-source-exhaustion",
            )
        paid_app, payments = self.paid_application()
        prompt = "Муж меня обнимает"
        provider_calls = self.provider.calls

        paid_app.handle(self.event("message_created", text=prompt))

        self.assertEqual(self.provider.calls, provider_calls)
        pending_dialog = self.store.get("u1")
        with self.database.read() as connection:
            pending = connection.execute(
                "SELECT * FROM pending_edit_requests WHERE id=?",
                (pending_dialog.pending_request_id,),
            ).fetchone()
            order = connection.execute(
                "SELECT * FROM payment_orders WHERE pending_request_id=?",
                (pending["id"],),
            ).fetchone()
        self.assertEqual(pending["prompt"], prompt)
        self.assertTrue(Path(pending["primary_source_path"]).is_file())
        self.assertTrue(Path(pending["secondary_source_path"]).is_file())
        self.assertEqual(order["payment_purpose"], "processing_request")
        pay_button = self.transport.images[-1][3][0]
        self.assertRegex(pay_button.action, r"^https://ravuna\.ru/p/[0-9a-f]{32}$")

        amount = "49.00"
        signature = hashlib.sha256(
            (
                f"{amount}:{order['provider_invoice_id']}:two:"
                f"Shp_order={order['public_token']}"
            ).encode()
        ).hexdigest()
        webhook = payments.process_webhook(
            {
                "OutSum": amount,
                "InvId": str(order["provider_invoice_id"]),
                "Shp_order": order["public_token"],
                "SignatureValue": signature,
            },
            method="POST",
            path="/payments/robokassa/result",
        )
        self.assertTrue(webhook.accepted)
        paid_app.notify_continuation_pack_paid(order["id"])
        paid_app.handle(self.event("message_callback", action="pending:process"))

        self.assertEqual(self.provider.calls, provider_calls + 1)
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT * FROM generation_attempts ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(attempt["prompt"], prompt)
        self.assertEqual(
            attempt["secondary_source_path"], pending["secondary_source_path"]
        )

    def test_start_works_button_opens_clear_empty_history(self) -> None:
        self.app.handle(self.event("message_created", text="/start"))
        provider_calls = self.provider.calls

        self.callback("studio:works")

        self.assertEqual(self.store.get("u1").state, "gallery")
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertEqual(
            self.transport.messages[-1][1],
            "Здесь пока пусто.\n\nСоздайте первую фотографию.",
        )
        self.assertEqual(
            [(button.text, button.action) for button in self.transport.messages[-1][2]],
            [("← Назад", "nav:back:main")],
        )

    def test_ready_scenario_is_optional_and_runs_without_prompt_confirmation(self) -> None:
        self.app.handle(self.event("bot_started"))
        self.callback("start:details")
        self.callback("catalog:ideas")
        self.callback("scenario:documents")
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/documents")
        )
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.store.get("u1").state, "result_ready")
        self.assertTrue(self.store.legal_is_current("u1"))
        self.assertFalse(any("Я понял задачу" in row[1] for row in self.transport.messages))

    def test_start_message_can_include_the_first_photo(self) -> None:
        self.app.handle(
            self.event(
                "message_created", text="/start",
                image_url="https://iu.oneme.ru/first-photo",
            )
        )
        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")
        self.assertEqual(self.transport.messages[-1][1], PHOTO_ACCEPTED_TEXT)
        self.assertTrue(self.store.legal_is_current("u1"))

    def test_photo_without_caption_asks_one_short_question(self) -> None:
        self.app.handle(self.event("message_created", text="/start"))
        before = len(self.transport.messages)

        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/source")
        )

        new_messages = self.transport.messages[before:]
        self.assertEqual(
            [message[1] for message in new_messages],
            [PHOTO_ACCEPTED_TEXT],
        )
        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")
        self.assertEqual(self.provider.calls, 0)

    def test_prompt_before_photo_is_saved_and_runs_when_photo_arrives(self) -> None:
        self.app.handle(self.event("message_created", text="/start"))
        self.callback("upload:ready")
        self.app.handle(
            self.event("message_created", text="Замени фон на однотонный")
        )
        waiting = self.store.get("u1")
        self.assertEqual(waiting.state, "waiting_for_source")
        self.assertEqual(waiting.pending_prompt, "Замени фон на однотонный")
        self.assertEqual(
            self.transport.messages[-1][1],
            "Теперь прикрепите фотографию через скрепку 📎.",
        )

        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/source")
        )
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.store.get("u1").state, "result_ready")
        self.assertFalse(
            any(message[1] == PHOTO_ACCEPTED_TEXT for message in self.transport.messages)
        )

    def test_start_clears_session_binding_and_returns_to_first_screen(self) -> None:
        self.onboard_to_prompt()
        before_restart = self.store.get("u1")
        self.assertIsNotNone(before_restart.session_id)

        message_count = len(self.transport.messages)
        self.app.handle(self.event("message_created", text="/start"))
        self.assertEqual(len(self.transport.messages), message_count + 1)
        restarted = self.store.get("u1")
        self.assertEqual(restarted.state, "main_menu")
        self.assertIsNone(restarted.session_id)
        self.assertIsNone(restarted.current_gallery_item_id)
        self.assertIsNone(restarted.current_version_id)
        self.assertEqual(self.provider.calls, 0)
        self.assertIn(
            "Что хотите сделать с фотографией?",
            self.transport.messages[-1][1],
        )
        self.assertFalse(any(message[1] == PROCESSING_TEXT for message in self.transport.messages))

    def test_custom_requires_upload_when_the_stored_source_is_missing(self) -> None:
        self.onboard_to_prompt()
        with self.database.read() as connection:
            source = Path(connection.execute(
                "SELECT source_file_path FROM demo_sessions"
            ).fetchone()[0])
        self.app.handle(self.event("message_created", text="/start"))
        source.unlink()
        self.app.handle(self.event("message_created", text="/start"))
        self.assertEqual(self.store.get("u1").state, "main_menu")
        self.callback("upload:ready")
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        self.assertIn("Прикрепите фотографию через скрепку 📎", self.transport.messages[-1][1])
        self.assertEqual(self.provider.calls, 0)

    def test_photoshoot_catalog_and_new_source_share_global_balance(self) -> None:
        self.generate_first()
        calls_before = self.provider.calls
        self.app.handle(self.event("message_created", text="/start"))
        self.callback("catalog:ideas")
        self.assertIn("выберите категорию", self.transport.messages[-1][1].lower())
        self.clock.advance(2)
        self.callback("scenario:cafe")
        self.assertEqual(self.store.get("u1").state, "demo_exhausted")
        self.assertEqual(self.provider.calls, calls_before + 1)

        other = self.base / "other.png"
        Image.new("RGB", (320, 240), "#aa7755").save(other)
        self.transport.source = other
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/other")
        )
        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")
        self.assertEqual(self.transport.messages[-1][1], PHOTO_ACCEPTED_TEXT)
        self.assertEqual(self.demo.commerce.balance(self.store.get("u1").user_id).available, 0)
        self.assertEqual(self.provider.calls, calls_before + 1)

    def test_image_sent_directly_after_start_is_saved_without_upload_callback(self) -> None:
        self.app.handle(self.event("message_created", text="/start"))
        self.assertEqual(self.store.get("u1").state, "main_menu")
        self.assertNotIn(
            "upload:ready",
            [button.action for button in self.transport.messages[-1][2]],
        )

        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/preloaded")
        )
        preloaded = self.store.get("u1")
        self.assertEqual(preloaded.state, "waiting_for_prompt")
        self.assertIsNotNone(preloaded.session_id)
        self.assertEqual(self.transport.messages[-1][1], PHOTO_ACCEPTED_TEXT)
        with self.database.read() as connection:
            source = Path(connection.execute(
                "SELECT source_file_path FROM demo_sessions WHERE id=?",
                (preloaded.session_id,),
            ).fetchone()[0])
        self.assertTrue(source.is_file())

        self.assertTrue(self.store.legal_is_current("u1"))

    def test_expired_source_never_reaches_processing_and_same_reupload_recovers(self) -> None:
        self.onboard_to_prompt()
        self.clock.advance(self.settings.demo_session_ttl_minutes * 60 + 1)

        self.app.handle(self.event("message_created", text="Замени фон"))
        expired = self.store.get("u1")
        self.assertEqual(expired.state, "waiting_for_source")
        self.assertIsNone(expired.session_id)
        self.assertEqual(self.provider.calls, 0)
        self.assertFalse(any(message[1] == PROCESSING_TEXT for message in self.transport.messages))
        self.assertIn("Сессия завершилась", self.transport.messages[-1][1])
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM demo_sessions").fetchone()[0],
                "expired",
            )

        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/source-again")
        )
        refreshed = self.store.get("u1")
        self.assertEqual(refreshed.state, "waiting_for_prompt")
        self.assertIsNotNone(refreshed.session_id)
        self.app.handle(self.event("message_created", text="Замени фон"))
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.store.get("u1").state, "result_ready")

    def test_processing_preview_original_guard_duplicate_and_unlock_placeholder(self) -> None:
        self.generate_first()
        self.assertTrue(any(row[1] == PROCESSING_TEXT for row in self.transport.messages))
        self.assertTrue(self.transport.edits)
        delivered_path = self.transport.images[-1][1]
        self.assertTrue(delivered_path.is_file())
        self.assertEqual(
            self.transport.images[-1][2],
            "Готово ✨\n"
            "Хотите ещё 2 обработки бесплатно?\n"
            "Пригласите друга — бонус начислится после его первой обработки.",
        )
        self.assertEqual(
            [button.text for button in self.transport.images[-1][3]],
            [
                "⬇️ Получить оригинал",
                "✏️ Исправить",
                "📷 Другое фото",
                "🎁 Пригласить друга — получить +2 обработки",
                "⭐ Оценить",
                "💬 Отзыв о Ravuna",
                "📁 Мои работы",
                "← Назад",
            ],
        )
        self.assertEqual(
            [button.row for button in self.transport.images[-1][3]],
            [0, 1, 1, 2, 3, 3, 4, 5],
        )
        self.assertEqual(self.transport.edits[-1][1], "✨ Готово")
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT original_result_path,demo_result_path FROM generation_attempts"
            ).fetchone()
        self.assertEqual(delivered_path, Path(attempt["demo_result_path"]))
        self.assertNotEqual(delivered_path, Path(attempt["original_result_path"]))

        unlock_event = self.callback("result:unlock")
        self.assertIn(
            "Пакет Ravuna — 49 ₽",
            self.transport.messages[-1][1],
        )
        self.assertNotIn(str(attempt["original_result_path"]), self.transport.messages[-1][1])
        callback_message_count = len(self.transport.messages)
        self.assertFalse(self.app.handle(unlock_event))
        self.assertEqual(len(self.transport.messages), callback_message_count)

    def test_result_other_photo_posts_new_active_message(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        result_message = self.app.ui.current("u1").message_id
        provider_calls = self.provider.calls
        message_count = len(self.transport.messages)
        edits_before = len(self.transport.edits)
        image_edits_before = len(self.transport.image_edits)
        deletes_before = list(self.transport.deletes)

        self.callback("new:source")

        active = self.app.ui.current("u1")
        self.assertEqual(len(self.transport.messages), message_count + 1)
        self.assertEqual(active.message_id, self.transport.messages[-1][4])
        self.assertNotEqual(active.message_id, result_message)
        self.assertEqual(len(self.transport.edits), edits_before)
        self.assertEqual(len(self.transport.image_edits), image_edits_before)
        self.assertEqual(self.transport.deletes, deletes_before)
        self.assertNotIn(result_message, self.transport.deletes)
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        self.assertIn("Загрузите другое фото", self.transport.messages[-1][1])
        self.assertEqual(self.provider.calls, provider_calls)

        self.callback("nav:back:main")
        self.assertEqual(self.app.ui.current("u1").message_id, active.message_id)
        self.assertEqual(self.transport.edits[-1][0], active.message_id)

    def test_result_original_then_other_photo_posts_new_active_message(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        dialog = self.store.get("u1")
        result_message = self.app.ui.current("u1").message_id
        message_count_before_unlock = len(self.transport.messages)
        edits_before_unlock = len(self.transport.edits)
        image_edits_before_unlock = len(self.transport.image_edits)
        deletes_before_unlock = list(self.transport.deletes)
        timeline_before_unlock = len(self.transport.timeline)
        with self.database.transaction() as connection:
            self.demo.commerce.grant_continuation_pack(
                connection,
                user_id=dialog.user_id,
                payment_order_id="test-result-history-order",
                payment_intent_id="test-result-history-intent",
            )
        self.callback("result:unlock")
        self.assertTrue(self.transport.files)
        original_message = f"file-{len(self.transport.files)}"
        confirmation_message = self.app.ui.current("u1").message_id
        self.assertNotEqual(confirmation_message, result_message)
        self.assertEqual(len(self.transport.messages), message_count_before_unlock + 1)
        self.assertEqual(len(self.transport.edits), edits_before_unlock)
        self.assertEqual(len(self.transport.image_edits), image_edits_before_unlock)
        self.assertEqual(self.transport.deletes, deletes_before_unlock)
        self.assertEqual(
            self.transport.timeline[timeline_before_unlock:],
            [
                ("send_file", original_message),
                ("send_message", confirmation_message),
            ],
        )

        provider_calls = self.provider.calls
        message_count = len(self.transport.messages)
        edits_before = len(self.transport.edits)
        image_edits_before = len(self.transport.image_edits)
        deletes_before = list(self.transport.deletes)

        self.callback("new:source")

        dialog = self.store.get("u1")
        active = self.app.ui.current("u1")
        self.assertEqual(len(self.transport.messages), message_count + 1)
        self.assertEqual(active.message_id, self.transport.messages[-1][4])
        self.assertNotEqual(active.message_id, confirmation_message)
        self.assertEqual(len(self.transport.edits), edits_before)
        self.assertEqual(len(self.transport.image_edits), image_edits_before)
        self.assertEqual(self.transport.deletes, deletes_before)
        self.assertNotIn(result_message, self.transport.deletes)
        self.assertNotIn(original_message, self.transport.deletes)
        self.assertEqual(self.transport.timeline[-1], ("send_message", active.message_id))
        self.assertEqual(dialog.state, "waiting_for_source")
        self.assertEqual(dialog.pending_action, "initial")
        self.assertIsNone(dialog.session_id)
        self.assertIsNone(dialog.current_version_id)
        upload_message = self.transport.messages[-1]
        self.assertIn("Загрузите другое фото", upload_message[1])
        self.assertIn("Прикрепите фотографию через скрепку 📎", upload_message[1])
        self.assertIn("до 2 фотографий", upload_message[1])
        self.assertIn("вправе его использовать", upload_message[1])
        self.assertEqual(
            [
                (button.text, parse_versioned_action(button.action)[1])
                for button in upload_message[2]
            ],
            [
                ("📄 Публичная оферта", "https://ravuna.ru/legal/offer.html"),
                (
                    "🔐 Обработка персональных данных",
                    "https://ravuna.ru/legal/personal-data.html",
                ),
                ("← Назад", "nav:back:main"),
            ],
        )
        self.assertEqual(self.provider.calls, provider_calls)

        self.callback("nav:back:main")
        self.assertEqual(self.app.ui.current("u1").message_id, active.message_id)
        self.assertEqual(self.transport.edits[-1][0], active.message_id)
        mutated_history_ids = {
            message_id
            for operation, message_id in self.transport.timeline
            if operation in {"edit_message", "edit_image", "delete_message"}
        }
        self.assertNotIn(result_message, mutated_history_ids)
        self.assertNotIn(original_message, mutated_history_ids)

    def test_start_and_direct_reupload_after_result_preserve_result_message(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        result_message = self.app.ui.current("u1").message_id
        deletes_before = list(self.transport.deletes)
        image_edits_before = len(self.transport.image_edits)

        self.app.handle(self.event("message_created", text="/start"))

        start_message = self.app.ui.current("u1").message_id
        self.assertNotEqual(start_message, result_message)
        self.assertNotIn(result_message, self.transport.deletes)
        self.assertEqual(self.transport.deletes, deletes_before)
        self.assertEqual(len(self.transport.image_edits), image_edits_before)

        second = self.base / "direct-reupload.png"
        Image.new("RGB", (240, 320), "#7697a8").save(second)
        self.transport.source = second
        self.app.handle(
            self.event("message_created", image_url="https://iu.oneme.ru/direct-reupload")
        )

        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")
        self.assertNotEqual(self.app.ui.current("u1").message_id, start_message)
        self.assertNotIn(result_message, self.transport.deletes)

    def test_repeat_from_result_preserves_previous_result_message(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        result_message = self.app.ui.current("u1").message_id
        deletes_before = list(self.transport.deletes)
        image_edits_before = len(self.transport.image_edits)
        self.clock.advance(2)

        self.callback("result:repeat")

        self.assertEqual(self.provider.calls, 2)
        self.assertNotEqual(self.app.ui.current("u1").message_id, result_message)
        self.assertNotIn(result_message, self.transport.deletes)
        self.assertEqual(
            [message_id for message_id in self.transport.deletes if message_id == result_message],
            [],
        )
        self.assertEqual(len(self.transport.image_edits), image_edits_before)
        self.assertGreater(len(self.transport.deletes), len(deletes_before))

    def test_zero_remaining_edits_replaces_edit_actions_with_purchase_offer(self) -> None:
        self.generate_first()

        self.clock.advance(2)
        self.callback("result:repeat")

        self.assertEqual(self.provider.calls, 2)
        self.assertEqual(self.demo.commerce.balance(self.store.get("u1").user_id).available, 0)
        buttons = self.transport.images[-1][3]
        button_texts = [button.text for button in buttons]
        self.assertNotIn("Исправить", button_texts)
        self.assertNotIn("📷 Другое фото", button_texts)
        self.assertIn("💳 Купить 2 обработки — 49 ₽", button_texts)
        self.assertIn("⬇️ Получить оригинал", button_texts)

    def test_zero_balance_start_shows_direct_purchase_and_blocks_stale_upload(self) -> None:
        self.app, _payments = self.paid_application()
        self.generate_first()
        self.clock.advance(2)
        self.callback("result:repeat")
        provider_calls = self.provider.calls

        self.app.handle(self.event("message_created", text="/start"))

        menu = self.transport.messages[-1]
        self.assertIn("У вас закончились обработки.", menu[1])
        self.assertIn("приобретите пакет Ravuna — 49 ₽", menu[1])
        buy = menu[2][0]
        self.assertEqual(buy.text, "Купить пакет — 49 ₽")
        self.assertRegex(buy.action, r"^https://ravuna\.ru/p/[0-9a-f]{32}$")
        self.callback("upload:ready")
        self.assertEqual(self.store.get("u1").state, "main_menu")
        self.assertEqual(self.provider.calls, provider_calls)

    def test_stale_correction_callback_with_zero_edits_opens_checkout_without_prompt(self) -> None:
        self.generate_first()
        self.clock.advance(2)
        self.callback("result:repeat")
        provider_calls = self.provider.calls
        correction_requests = sum(
            message[1] == CORRECTION_REQUEST_TEXT
            for message in self.transport.messages
        )

        self.callback("result:correct")

        dialog = self.store.get("u1")
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertEqual(dialog.state, "result_ready")
        self.assertEqual(dialog.pending_action, "checkout")
        self.assertNotEqual(dialog.state, "waiting_for_correction")
        self.assertEqual(
            sum(
                message[1] == CORRECTION_REQUEST_TEXT
                for message in self.transport.messages
            ),
            correction_requests,
        )
        self.assertEqual(self.transport.messages[-1][1], PAYMENT_OFFER_TEXT)

    def test_zero_balance_checkout_targets_new_pending_photo_not_old_work(self) -> None:
        self.enable_single_screen()
        self.app, payments = self.paid_application()
        self.generate_first()
        self.clock.advance(2)
        self.callback("result:repeat")
        old_dialog = self.store.get("u1")
        old_item_id = old_dialog.current_gallery_item_id
        user_id = old_dialog.user_id
        self.assertEqual(self.demo.commerce.balance(user_id).available, 0)

        self.app.handle(self.event("message_created", text="/start"))
        self.assertEqual(self.store.get("u1").state, "main_menu")
        self.callback("upload:ready")
        self.assertEqual(self.store.get("u1").state, "main_menu")
        new_source = self.base / "new-source.png"
        Image.new("RGB", (320, 240), "#b42318").save(new_source)
        self.transport.source = new_source
        provider_calls = self.provider.calls
        with self.database.read() as connection:
            attempts_before = connection.execute(
                "SELECT COUNT(*) FROM generation_attempts"
            ).fetchone()[0]
            error_events_before = connection.execute(
                "SELECT COUNT(*) FROM product_events WHERE event_type='error'"
            ).fetchone()[0]

        self.app.handle(
            self.event(
                "message_created",
                text="Поменять одежду. Улучшить фон.",
                image_url="https://iu.oneme.ru/new-source",
            )
        )

        dialog = self.store.get("u1")
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertEqual(dialog.state, "result_ready")
        self.assertEqual(dialog.pending_action, "checkout")
        self.assertIsNone(dialog.current_version_id)
        self.assertIsNotNone(dialog.pending_request_id)
        self.assertNotEqual(dialog.current_gallery_item_id, old_item_id)
        pending_cards = [
            (entry[1], entry[2])
            for entry in [*self.transport.images, *self.transport.image_edits]
            if entry[2] == PENDING_EDIT_PAYMENT_TEXT
        ]
        self.assertEqual(len(pending_cards), 1)
        with Image.open(pending_cards[0][0]) as displayed:
            self.assertEqual(displayed.convert("RGB").getpixel((0, 0)), (180, 35, 24))
        self.assertIn("У вас закончились обработки.", PENDING_EDIT_PAYMENT_TEXT)
        visible_work_ids = {
            entry.id for entry in self.app.work_gallery.works(user_id, 1).entries
        }
        self.assertIn(old_item_id, visible_work_ids)
        self.assertNotIn(dialog.current_gallery_item_id, visible_work_ids)
        with self.database.read() as connection:
            pending = connection.execute(
                "SELECT * FROM pending_edit_requests WHERE id=?",
                (dialog.pending_request_id,),
            ).fetchone()
            self.assertEqual(pending["gallery_item_id"], dialog.current_gallery_item_id)
            self.assertEqual(pending["prompt"], "Поменять одежду. Улучшить фон.")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM generation_attempts").fetchone()[0],
                attempts_before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM gallery_versions WHERE gallery_item_id=?",
                    (dialog.current_gallery_item_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM product_events WHERE event_type='error'"
                ).fetchone()[0],
                error_events_before,
            )
        self.assertEqual(self.demo.commerce.balance(user_id).available, 0)

        with self.database.read() as connection:
            intent = connection.execute(
                "SELECT * FROM payment_intents WHERE pending_request_id=?",
                (dialog.pending_request_id,),
            ).fetchone()
            order = connection.execute(
                "SELECT * FROM payment_orders WHERE pending_request_id=?",
                (dialog.pending_request_id,),
            ).fetchone()
            self.assertIsNotNone(intent)
            self.assertEqual(intent["pending_request_id"], dialog.pending_request_id)
            self.assertIsNone(intent["version_id"])
            self.assertIsNone(intent["attempt_id"])
            self.assertEqual(order["pending_request_id"], dialog.pending_request_id)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                2,
            )

        amount = "49.00"
        signature = hashlib.sha256(
            (
                f"{amount}:{order['provider_invoice_id']}:two:"
                f"Shp_order={order['public_token']}"
            ).encode()
        ).hexdigest()
        webhook = payments.process_webhook(
            {
                "OutSum": amount,
                "InvId": str(order["provider_invoice_id"]),
                "Shp_order": order["public_token"],
                "SignatureValue": signature,
            },
            method="POST",
            path="/payments/robokassa/result",
        )
        self.assertTrue(webhook.accepted)
        self.assertTrue(self.app.notify_continuation_pack_paid(order["id"]))
        paid_dialog = self.store.get("u1")
        self.assertEqual(paid_dialog.pending_action, "pending_paid")
        self.assertEqual(paid_dialog.current_gallery_item_id, dialog.current_gallery_item_id)
        self.assertEqual(self.provider.calls, provider_calls)

        self.clock.advance(2)
        process_event = self.event("message_callback", action="pending:process")
        process_event = replace(
            process_event,
            message_id=self.app.ui.current("u1").message_id,
        )
        self.assertTrue(self.app.handle(process_event))
        completed = self.store.get("u1")
        self.assertEqual(self.provider.calls, provider_calls + 1)
        self.assertEqual(self.demo.commerce.balance(user_id).available, 1)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM pending_edit_requests WHERE id=?",
                    (dialog.pending_request_id,),
                ).fetchone()[0],
                "completed",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT gallery_item_id FROM gallery_versions WHERE id=?",
                    (completed.current_version_id,),
                ).fetchone()[0],
                dialog.current_gallery_item_id,
            )

    def test_gallery_with_zero_edits_does_not_offer_correction_or_repeat(self) -> None:
        self.generate_first()
        self.clock.advance(2)
        self.callback("result:repeat")

        self.callback("studio:works")
        item_id = self.store.get("u1").current_gallery_item_id
        self.callback(f"works:open:{item_id}")

        button_texts = [button.text for button in self.transport.images[-1][3]]
        self.assertNotIn("✏️ Исправить", button_texts)
        self.assertNotIn("🎲 Другой вариант", button_texts)
        self.assertIn("💳 Купить ещё 2 обработки — 49 ₽", button_texts)

    def test_repeated_payment_click_sends_one_link_without_duplicate_message(self) -> None:
        self.generate_first()
        paid_app, _payments = self.paid_application()

        paid_app.handle(self.event("message_callback", action="result:unlock"))
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_orders").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_receipts").fetchone()[0],
                1,
            )
        offer_message_id = self.transport.messages[-1][4]
        paid_app.handle(self.event("message_callback", action="package:buy"))
        message_count = len(self.transport.messages)
        stale = replace(
            self.event("message_callback", action="package:buy"),
            message_id=offer_message_id,
            text=PAYMENT_OFFER_TEXT,
        )
        paid_app.handle(stale)

        payment_cards = [
            message for message in self.transport.messages
            if message[1] == PAYMENT_OFFER_TEXT
            and message[2]
            and message[2][0].text == "Оплатить 49 ₽"
            and message[2][0].action.startswith("https://ravuna.ru/p/")
        ]
        self.assertEqual(len(payment_cards), 1)
        self.assertFalse(
            any(message[1] == PAYMENT_LINK_TEXT for message in self.transport.messages)
        )
        self.assertEqual(len(self.transport.messages), message_count)
        self.assertEqual(self.transport.callbacks[-1][1], "Экран уже изменился")
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_orders").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_receipts").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM payment_attempts
                       WHERE purpose='max_checkout_card'"""
                ).fetchone()[0],
                0,
            )

    def test_payment_return_waits_for_resulturl_then_delivers_exact_original(self) -> None:
        self.generate_first()
        paid_app, payments = self.paid_application()
        selected_version = self.store.get("u1").current_version_id
        paid_app.handle(self.event("message_callback", action="result:unlock"))
        with self.database.read() as connection:
            order = connection.execute("SELECT * FROM payment_orders").fetchone()
            original = Path(connection.execute(
                "SELECT original_path FROM gallery_versions WHERE id=?",
                (selected_version,),
            ).fetchone()[0])
        pending_return = replace(
            self.event("message_created", text="/start"),
            start_payload=f"pay_{order['public_token']}",
        )
        paid_app.handle(pending_return)
        self.assertEqual(self.transport.messages[-1][1], "Проверяем оплату…")
        self.assertEqual(
            self.demo.commerce.entitlement_balance(order["user_id"]).available, 0
        )

        amount = "49.00"
        signature = hashlib.sha256(
            (
                f"{amount}:{order['provider_invoice_id']}:two:"
                f"Shp_order={order['public_token']}"
            ).encode()
        ).hexdigest()
        self.assertTrue(payments.process_webhook(
            {
                "OutSum": amount,
                "InvId": str(order["provider_invoice_id"]),
                "Shp_order": order["public_token"],
                "SignatureValue": signature,
            },
            method="POST",
            path="/payments/robokassa/result",
        ).accepted)
        file_count = len(self.transport.files)
        confirmed_return = replace(
            self.event("message_created", text="/start"),
            start_payload=f"pay_{order['public_token']}",
        )
        paid_app.handle(confirmed_return)

        self.assertEqual(len(self.transport.files), file_count + 1)
        self.assertEqual(self.transport.files[-1][1], original)
        self.assertEqual(self.store.get("u1").current_version_id, selected_version)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM payment_orders WHERE id=?", (order["id"],)).fetchone()[0],
                "delivered",
            )

    def test_unlock_offer_reuses_selected_preview_without_creating_payment(self) -> None:
        self.generate_first()
        paid_app, _payments = self.paid_application()
        selected_version_id = self.store.get("u1").current_version_id
        with self.database.read() as connection:
            selected_preview = Path(connection.execute(
                """SELECT preview_watermarked_path FROM gallery_versions
                   WHERE id=?""",
                (selected_version_id,),
            ).fetchone()[0])
        image_count = len(self.transport.images)
        message_count = len(self.transport.messages)

        unlock_events = [
            self.event("message_callback", action="result:unlock"),
            self.event("message_callback", action="result:unlock"),
        ]
        paid_app.handle(unlock_events[0])
        paid_app.handle(unlock_events[1])

        self.assertEqual(len(self.transport.images), image_count + 1)
        self.assertEqual(self.transport.images[-1][1], selected_preview)
        self.assertEqual(self.transport.images[-1][2], "Выбранная версия")
        self.assertEqual(self.transport.images[-1][3], ())
        offers = [
            message
            for message in self.transport.messages[message_count:]
            if message[1] == PAYMENT_OFFER_TEXT
        ]
        self.assertEqual(len(offers), 1)
        self.assertEqual(
            [(button.text, button.action) for button in offers[0][2]],
            [
                ("Оплатить 49 ₽", offers[0][2][0].action),
                ("← Назад", "nav:back:work"),
            ],
        )
        with self.database.read() as connection:
            self.assertRegex(offers[0][2][0].action, r"^https://ravuna\.ru/p/[0-9a-f]{32}$")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                1,
            )

    def test_back_from_payment_restores_exact_selected_work_without_side_effects(self) -> None:
        self.generate_first()
        paid_app, _payments = self.paid_application()
        selected_version_id = self.store.get("u1").current_version_id
        with self.database.read() as connection:
            selected_preview = Path(
                connection.execute(
                    "SELECT preview_watermarked_path FROM gallery_versions WHERE id=?",
                    (selected_version_id,),
                ).fetchone()[0]
            )
        provider_calls = self.provider.calls
        balance = self.demo.commerce.balance(self.store.get("u1").user_id)

        paid_app.handle(self.event("message_callback", action="result:unlock"))
        back_events = [
            self.event("message_callback", action="nav:back:work"),
            self.event("message_callback", action="nav:back:work"),
        ]
        paid_app.handle(back_events[0])

        restored = self.store.get("u1")
        self.assertEqual(restored.state, "gallery")
        self.assertEqual(restored.pending_action, NAV_WORK)
        self.assertEqual(restored.current_version_id, selected_version_id)
        self.assertEqual(self.transport.images[-1][1], selected_preview)
        self.assertEqual(self.transport.images[-1][3][-1].text, "← Назад")
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertEqual(
            self.demo.commerce.balance(restored.user_id),
            balance,
        )
        image_count = len(self.transport.images)
        message_count = len(self.transport.messages)
        paid_app.handle(back_events[1])
        self.assertEqual(len(self.transport.images), image_count)
        self.assertEqual(len(self.transport.messages), message_count)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                1,
            )

    def test_back_from_correction_clears_transient_state_without_generation_or_debit(self) -> None:
        self.generate_first()
        selected_version_id = self.store.get("u1").current_version_id
        provider_calls = self.provider.calls
        balance = self.demo.commerce.balance(self.store.get("u1").user_id)

        self.callback("result:correct")
        self.assertEqual(self.store.get("u1").state, "waiting_for_correction")
        self.callback("nav:back:work")

        restored = self.store.get("u1")
        self.assertEqual(restored.state, "gallery")
        self.assertIsNone(restored.pending_prompt)
        self.assertEqual(restored.pending_action, NAV_WORK)
        self.assertEqual(restored.current_version_id, selected_version_id)
        self.assertEqual(self.transport.images[-1][3][-1].text, "← Назад")
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertEqual(self.demo.commerce.balance(restored.user_id), balance)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                0,
            )

    def test_back_from_waiting_for_photo_returns_main_without_provider_call(self) -> None:
        self.app.handle(self.event("bot_started"))
        self.callback("upload:ready")
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        self.assertEqual(self.store.get("u1").pending_action, "initial")
        provider_calls = self.provider.calls

        self.callback("nav:back:main")

        restored = self.store.get("u1")
        self.assertEqual(restored.state, "main_menu")
        self.assertIsNone(restored.pending_prompt)
        self.assertIsNone(restored.pending_action)
        self.assertIsNone(restored.current_gallery_item_id)
        self.assertEqual(self.provider.calls, provider_calls)
        self.assertIn(
            "Что хотите сделать с фотографией?",
            self.transport.messages[-1][1],
        )
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                0,
            )

    def test_parallel_payment_clicks_send_one_card_atomically(self) -> None:
        self.generate_first()
        paid_app, _payments = self.paid_application()
        paid_app.handle(self.event("message_callback", action="result:unlock"))
        events = [
            self.event("message_callback", action="package:buy"),
            self.event("message_callback", action="package:buy"),
        ]

        with ThreadPoolExecutor(max_workers=2) as executor:
            handled = list(executor.map(paid_app.handle, events))

        self.assertEqual(handled, [True, True])
        payment_cards = [
            message for message in self.transport.messages
            if message[1] == PAYMENT_OFFER_TEXT
            and message[2]
            and message[2][0].text == "Оплатить 49 ₽"
            and message[2][0].action.startswith("https://ravuna.ru/p/")
        ]
        self.assertEqual(len(payment_cards), 1)
        self.assertEqual(
            sum(
                message[1] == "Ссылка на оплату уже создана."
                for message in self.transport.messages
            ),
            0,
        )
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_orders").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_receipts").fetchone()[0],
                1,
            )

    def test_stale_paid_purchase_callback_does_not_repeat_delivery(self) -> None:
        self.generate_first()
        paid_app, payments = self.paid_application()
        paid_app.handle(self.event("message_callback", action="result:unlock"))
        paid_app.handle(self.event("message_callback", action="package:buy"))
        with self.database.read() as connection:
            order = connection.execute("SELECT * FROM payment_orders").fetchone()
            original = Path(connection.execute(
                "SELECT original_path FROM gallery_versions WHERE id=?",
                (order["version_id"],),
            ).fetchone()[0])
        amount = "49.00"
        signature = hashlib.sha256(
            (
                f"{amount}:{order['provider_invoice_id']}:two:"
                f"Shp_order={order['public_token']}"
            ).encode()
        ).hexdigest()
        webhook = payments.process_webhook({
            "OutSum": amount,
            "InvId": str(order["provider_invoice_id"]),
            "Shp_order": order["public_token"],
            "SignatureValue": signature,
        }, method="POST", path="/payments/robokassa/result")
        self.assertTrue(webhook.accepted)
        message_count = len(self.transport.messages)

        file_count = len(self.transport.files)
        paid_app.handle(self.event("message_callback", action="package:buy"))

        self.assertEqual(len(self.transport.files), file_count)
        self.assertEqual(len(self.transport.messages), message_count)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_orders").fetchone()[0],
                1,
            )

    def test_owner_sandbox_payment_grants_pack_then_delivers_exact_original(self) -> None:
        self.generate_first()
        paid_app, payments = self.paid_application()
        event = self.event("message_callback", action="result:unlock")
        paid_app.handle(event)
        self.assertEqual(self.transport.messages[-1][1], PAYMENT_OFFER_TEXT)
        pay_button = self.transport.messages[-1][2][0]
        self.assertEqual(pay_button.text, "Оплатить 49 ₽")
        self.assertRegex(pay_button.action, r"^https://ravuna\.ru/p/[0-9a-f]{32}$")
        self.assertEqual(self.transport.messages[-1][2][-1].text, "← Назад")
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                1,
            )
        with self.database.read() as connection:
            order = connection.execute("SELECT * FROM payment_orders").fetchone()
            original = Path(connection.execute(
                "SELECT original_path FROM gallery_versions WHERE id=?",
                (order["version_id"],),
            ).fetchone()[0])
        self.assertEqual(order["payment_purpose"], "original_download")
        amount = "49.00"
        base = f"{amount}:{order['provider_invoice_id']}:two:Shp_order={order['public_token']}"
        signature = hashlib.sha256(base.encode()).hexdigest()
        webhook = payments.process_webhook({
            "OutSum": amount,
            "InvId": str(order["provider_invoice_id"]),
            "Shp_order": order["public_token"],
            "SignatureValue": signature,
        }, method="POST", path="/payments/robokassa/result")
        self.assertTrue(webhook.accepted)
        with self.database.transaction() as connection:
            intent_version_id = connection.execute(
                "SELECT version_id FROM payment_intents WHERE id=?",
                (order["intent_id"],),
            ).fetchone()[0]
            gallery_item_id = connection.execute(
                "SELECT gallery_item_id FROM gallery_versions WHERE id=?",
                (intent_version_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE payment_orders SET version_id='decoy-version' WHERE id=?",
                (order["id"],),
            )
        self.store.update(
            "u1",
            current_gallery_item_id=None,
            current_version_id=None,
        )
        file_count = len(self.transport.files)
        self.assertTrue(paid_app.notify_continuation_pack_paid(order["id"]))
        self.assertEqual(len(self.transport.files), file_count + 1)
        self.assertEqual(self.transport.files[-1][1], original)
        paid_dialog = self.store.get("u1")
        self.assertEqual(paid_dialog.current_version_id, intent_version_id)
        self.assertEqual(paid_dialog.current_gallery_item_id, gallery_item_id)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM payment_orders WHERE id=?", (order["id"],)).fetchone()[0],
                "delivered",
            )

    def test_true_intent_conflict_is_resolved_without_an_extra_question(self) -> None:
        direct_settings = replace(self.settings, image_direct_prompt_enabled=True)
        self.settings = direct_settings
        self.demo.settings = direct_settings
        self.app.settings = direct_settings
        self.onboard_to_prompt()
        user_text = "Поменяй фон, но фон не меняй"
        with patch(
            "app.max_application.parse_edit_intent",
            side_effect=AssertionError("legacy intent parser must be bypassed"),
        ), patch(
            "app.demo_service.build_provider_prompt",
            side_effect=AssertionError("technical prompt builder must be bypassed"),
        ):
            self.app.handle(
                self.event("message_created", text=user_text)
            )
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.store.get("u1").state, "result_ready")
        self.assertFalse(any("Оставить текущий фон" in row[1] for row in self.transport.messages))
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT prompt,provider_prompt FROM generation_attempts"
            ).fetchone()
        self.assertEqual(attempt["prompt"], user_text)
        self.assertTrue(attempt["provider_prompt"].startswith(user_text + "\n\n"))

    def test_text_after_result_continues_as_field_level_correction(self) -> None:
        self.onboard_to_prompt()
        with self.database.transaction() as connection:
            self.demo.commerce.adjust_generation_credits(
                connection,
                user_id=self.store.get("u1").user_id,
                delta=1,
                reason="test third lineage version",
                idempotency_key="test-lineage-credit",
            )
        phrases = (
            "Замени фон на Альпы",
            "Добавь куртку",
            "Сделай закат",
        )
        for index, phrase in enumerate(phrases):
            if index:
                self.clock.advance(2)
            self.app.handle(self.event("message_created", text=phrase))
            self.assertEqual(
                self.store.get("u1").state,
                "demo_exhausted" if index == 2 else "result_ready",
            )
        self.assertEqual(self.provider.calls, 3)
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT id,parent_version_id,edit_plan_json
                   FROM gallery_versions ORDER BY version_number"""
            ).fetchall()
        plans = [EditPlan.from_json(row["edit_plan_json"]) for row in rows]
        self.assertEqual(plans[-1].scene.background.setting, "realistic alpine mountains")
        self.assertEqual(plans[-1].scene.outfit.style, "realistic jacket")
        self.assertEqual(plans[-1].scene.lighting.style, "realistic warm sunset light")
        self.assertEqual(rows[1]["parent_version_id"], rows[0]["id"])
        self.assertEqual(rows[2]["parent_version_id"], rows[1]["id"])
        self.assertFalse(any("Что исправить?" in row[1] for row in self.transport.messages))

    def test_result_feedback_is_optional_and_technical_only(self) -> None:
        self.generate_first()
        message_count = len(self.transport.messages)
        self.callback("result:feedback:positive")
        self.assertEqual(len(self.transport.messages), message_count)
        self.callback("result:feedback:negative")
        self.assertEqual(len(self.transport.messages), message_count)
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM version_feedback").fetchone()
        self.assertEqual(row["sentiment"], "positive")
        self.assertIsNone(row["reason_category"])
        self.assertEqual(self.transport.callbacks[-1][1], "Экран уже изменился")

    def test_result_rating_saves_five_stars_without_provider_call(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        provider_calls = self.provider.calls
        current = self.store.get("u1")

        self.callback("result:rate")

        rating_screen = self.transport.image_edits[-1]
        self.assertEqual(rating_screen[2], "Как вам результат?")
        self.assertEqual(
            [button.text for button in rating_screen[3]],
            ["1 ⭐", "2 ⭐", "3 ⭐", "4 ⭐", "5 ⭐", "← Назад"],
        )

        self.callback("result:rating:5")

        self.assertEqual(
            self.transport.image_edits[-1][2],
            "Спасибо! Рады, что вам понравилось 😊",
        )
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM user_feedback WHERE feedback_type='rating'"
            ).fetchone()
        self.assertEqual(row["user_id"], current.user_id)
        self.assertEqual(row["version_id"], current.current_version_id)
        self.assertEqual(row["rating"], 5)
        self.assertIsNotNone(row["created_at"])
        self.assertEqual(self.provider.calls, provider_calls)

    def test_low_rating_comment_and_general_feedback_are_saved_safely(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        provider_calls = self.provider.calls
        current = self.store.get("u1")

        self.callback("result:rate")
        self.callback("result:rating:2")

        low_result = self.transport.image_edits[-1]
        self.assertIn("Спасибо за честную оценку", low_result[2])
        self.assertIn(
            "💬 Написать комментарий",
            [button.text for button in low_result[3]],
        )

        self.callback("result:feedback:comment")
        self.assertIn(
            "мы читаем все предложения", self.transport.image_edits[-1][2]
        )
        self.app.handle(
            self.event(
                "message_created",
                text="Добавьте более понятную кнопку возврата.",
            )
        )

        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM user_feedback"
            ).fetchall()
        feedback = {row["feedback_type"]: row for row in rows}
        self.assertEqual(set(feedback), {"rating", "comment"})
        self.assertEqual(feedback["rating"]["rating"], 2)
        self.assertEqual(
            feedback["comment"]["message"],
            "Добавьте более понятную кнопку возврата.",
        )
        self.assertEqual(feedback["comment"]["user_id"], current.user_id)
        self.assertEqual(
            feedback["comment"]["version_id"], current.current_version_id
        )
        self.assertIsNotNone(feedback["comment"]["created_at"])
        self.assertEqual(self.store.get("u1").pending_action, "navigation:feedback")
        self.assertEqual(
            self.transport.images[-1][2],
            "Спасибо! Мы получили ваше сообщение 🙏",
        )
        self.assertEqual(self.provider.calls, provider_calls)

    def test_result_general_feedback_entry_uses_same_safe_storage(self) -> None:
        self.enable_single_screen()
        self.generate_first()
        provider_calls = self.provider.calls

        self.callback("result:feedback")

        self.assertIn(
            "Есть идея, проблема", self.transport.image_edits[-1][2]
        )
        self.assertEqual(
            [button.text for button in self.transport.image_edits[-1][3]],
            ["← Назад"],
        )
        self.app.handle(
            self.event("message_created", text="Хочу больше примеров обработки.")
        )

        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM user_feedback WHERE feedback_type='comment'"
            ).fetchone()
        self.assertEqual(row["message"], "Хочу больше примеров обработки.")
        self.assertEqual(self.provider.calls, provider_calls)

    def test_gallery_navigation_clears_abandoned_correction_state(self) -> None:
        self.generate_first()
        item_id = self.store.get("u1").current_gallery_item_id

        self.callback("result:correct")
        correction = self.store.get("u1")
        self.assertEqual(correction.state, "waiting_for_correction")
        self.assertEqual(correction.pending_action, "correction")

        self.callback("studio:works")
        gallery = self.store.get("u1")
        self.assertEqual(gallery.state, "gallery")
        self.assertIsNone(gallery.pending_prompt)
        self.assertEqual(gallery.pending_action, NAV_WORKS)
        self.assertIsNone(gallery.status_message_id)

        self.callback(f"works:open:{item_id}")
        opened = self.store.get("u1")
        self.assertEqual(opened.state, "gallery")
        self.assertIsNone(opened.pending_prompt)
        self.assertEqual(opened.pending_action, NAV_WORK)

        self.callback("work:history")
        history = self.store.get("u1")
        self.assertEqual(history.state, "gallery")
        self.assertIsNone(history.pending_prompt)
        self.assertEqual(history.pending_action, NAV_HISTORY)

    def test_back_navigation_returns_history_to_work_and_work_to_list(self) -> None:
        self.generate_first()
        item_id = self.store.get("u1").current_gallery_item_id
        selected_version_id = self.store.get("u1").current_version_id
        provider_calls = self.provider.calls

        self.callback("studio:works")
        self.callback(f"works:open:{item_id}")
        self.callback("work:history")
        self.assertEqual(self.store.get("u1").pending_action, NAV_HISTORY)

        self.callback("nav:back:work")
        work = self.store.get("u1")
        self.assertEqual(work.pending_action, NAV_WORK)
        self.assertEqual(work.current_version_id, selected_version_id)
        self.assertEqual(self.transport.images[-1][3][-1].text, "← Назад")

        self.callback("nav:back:works")
        works = self.store.get("u1")
        self.assertEqual(works.pending_action, NAV_WORKS)
        self.assertTrue(
            any(
                message[1] == "📂 Мои работы\n\nВыберите работу."
                and message[2][-1].text == "← Назад"
                for message in self.transport.messages
            )
        )
        self.assertEqual(self.provider.calls, provider_calls)

    def test_correction_repeat_gallery_navigation_favorite_and_physical_delete(self) -> None:
        self.generate_first()
        first = self.store.get("u1").current_version_id
        with self.database.read() as connection:
            selected_preview = Path(connection.execute(
                "SELECT preview_watermarked_path FROM gallery_versions WHERE id=?",
                (first,),
            ).fetchone()[0])
        image_count = len(self.transport.images)
        self.callback("result:correct")
        self.assertEqual(self.store.get("u1").state, "waiting_for_correction")
        self.assertEqual(len(self.transport.images), image_count + 1)
        self.assertEqual(self.transport.images[-1][1], selected_preview)
        self.assertEqual(self.transport.images[-1][2], "Текущая версия")
        self.assertEqual(self.transport.images[-1][3], ())
        self.assertEqual(self.transport.messages[-1][1], CORRECTION_REQUEST_TEXT)
        self.assertEqual(
            self.transport.edits[-1], ("image-1", result_actions(1).text, ())
        )
        self.clock.advance(2)
        self.app.handle(self.event("message_created", text="Сделай лицо естественнее"))
        second = self.store.get("u1").current_version_id
        self.clock.advance(2)
        with self.database.transaction() as connection:
            self.demo.commerce.adjust_generation_credits(
                connection,
                user_id=self.store.get("u1").user_id,
                delta=1,
                reason="test repeat lineage version",
                idempotency_key="test-repeat-credit",
            )
        self.callback("result:repeat")
        dialog = self.store.get("u1")
        third = dialog.current_version_id
        with self.database.read() as connection:
            versions = connection.execute(
                """SELECT id,parent_version_id,correction_prompt,effective_prompt
                   FROM gallery_versions ORDER BY version_number"""
            ).fetchall()
            session = connection.execute("SELECT successful_generations FROM demo_sessions").fetchone()
            root = Path(connection.execute(
                "SELECT storage_root_path FROM gallery_items WHERE id=?",
                (dialog.current_gallery_item_id,),
            ).fetchone()[0])
        self.assertEqual([row["id"] for row in versions], [first, second, third])
        self.assertEqual(versions[1]["parent_version_id"], first)
        self.assertEqual(versions[1]["correction_prompt"], "Сделай лицо естественнее")
        self.assertEqual(versions[2]["parent_version_id"], second)
        self.assertEqual(versions[2]["effective_prompt"], versions[1]["effective_prompt"])
        self.assertEqual(session["successful_generations"], 3)

        self.callback("result:favorite")
        self.callback("studio:works")
        self.assertTrue(
            any("📂 Мои работы" in message[1] for message in self.transport.messages[-3:])
        )
        self.callback(f"works:open:{dialog.current_gallery_item_id}")
        self.callback("work:history")
        self.callback("work:previous")
        self.assertEqual(self.store.get("u1").current_version_id, second)
        self.callback("work:main")
        self.assertTrue(self.transport.images)

        self.callback("result:delete")
        self.assertIn("Переместить работу", self.transport.messages[-1][1])
        self.callback("delete:confirm")
        self.assertTrue(root.exists())
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT deleted FROM gallery_items").fetchone()[0], 1
            )
        self.assertEqual(self.store.get("u1").state, "deleted")
        self.callback("delete:restore")
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT deleted FROM gallery_items").fetchone()[0], 0
            )
        self.assertEqual(self.store.get("u1").state, "gallery")

    def test_send_failure_and_technical_failure_do_not_debit(self) -> None:
        self.onboard_to_prompt()
        self.transport.image_delivery = False
        self.app.handle(self.event("message_created", text="Измени фон"))
        with self.database.read() as connection:
            session = connection.execute(
                "SELECT successful_generations FROM demo_sessions"
            ).fetchone()
            attempt = connection.execute(
                "SELECT status FROM generation_attempts"
            ).fetchone()
        self.assertEqual(session[0], 0)
        self.assertEqual(attempt[0], "delivery_failed")
        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")
        self.assertEqual(
            self.transport.edits[-1][1],
            "Изображение создано, но не удалось отправить его в MAX. Попытка не списана.",
        )

        self.provider.fail = RuntimeError("provider unavailable")
        self.transport.image_delivery = True
        self.clock.advance(2)
        technical = self.event("message_created", text="Измени фон")
        self.assertTrue(self.app.handle(technical))
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT successful_generations FROM demo_sessions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM generation_attempts ORDER BY created_at DESC LIMIT 1"
                ).fetchone()[0],
                "failed_technical",
            )
        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")
        self.assertEqual(
            self.transport.edits[-1][1],
            "Сервис временно недоступен. Попытка не списана.",
        )

    def test_transport_error_marks_event_retryable(self) -> None:
        self.app.handle(self.event("bot_started"))
        self.callback("legal:accept_all")
        self.transport.fail_next_message = True
        event = self.event("message_callback", action="custom")
        with self.assertRaises(MaxTransportError):
            self.app.handle(event)
        self.assertTrue(self.store.begin_event(event.event_key, event.event_type))

    def test_product_errors_have_distinct_safe_copy(self) -> None:
        cases = (
            (
                ProviderTimeoutError("technical"),
                "Обработка заняла слишком много времени. Попытка не списана — попробуйте ещё раз.",
            ),
            (
                ProviderUnavailableError("technical"),
                "Сервис временно недоступен. Попытка не списана.",
            ),
            (
                ProviderQuotaError("technical"),
                "Сервис временно недоступен. Попытка не списана.",
            ),
            (
                PolicyRejectedError("technical"),
                "Это изображение или запрос нельзя обработать. Попробуйте изменить описание.",
            ),
            (
                ProviderInvalidRequestError("technical"),
                "Не удалось выполнить обработку. Попробуйте переформулировать запрос проще.",
            ),
            (
                DeliveryError("technical"),
                "Изображение создано, но не удалось отправить его в MAX. Попытка не списана.",
            ),
            (
                DailyBudgetError("technical"),
                "Сегодня бесплатная обработка временно недоступна. Попробуйте позже.",
            ),
            (
                ImageTooLargeError("technical"),
                "Файл слишком большой. Отправьте изображение до 15 МБ.",
            ),
        )
        event = self.event("message_created", text="request")
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.app._show_demo_error(event, error)
                self.assertEqual(self.transport.messages[-1][1], expected)

    def test_restart_recovery_finishes_stale_processing_status(self) -> None:
        self.store.get_or_create("u1", "c1")
        self.store.transition(
            "u1", "processing", force=True, status_message_id="status-old"
        )
        self.assertEqual(self.app.recover_interrupted_processing(), 1)
        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")
        self.assertIsNone(self.store.get("u1").status_message_id)
        self.assertIn("Обработка прервалась", self.transport.edits[-1][1])

    def test_restart_recovery_does_not_block_service_when_max_is_unavailable(self) -> None:
        self.store.get_or_create("u1", "c1")
        self.store.transition(
            "u1", "processing", force=True, status_message_id="status-old"
        )
        self.transport.fail_next_edit = True
        self.transport.fail_next_message = True
        self.assertEqual(self.app.recover_interrupted_processing(), 1)
        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")
        self.assertIsNone(self.store.get("u1").status_message_id)

    def test_telemetry_contains_events_but_never_prompt_text(self) -> None:
        self.generate_first()
        with self.database.read() as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(product_events)")
            }
            events = {
                row[0] for row in connection.execute("SELECT event_type FROM product_events")
            }
        self.assertNotIn("prompt", columns)
        self.assertTrue(
            {"start", "photo_uploaded", "prompt_submitted", "processing_started", "result_delivered"}
            <= events
        )
