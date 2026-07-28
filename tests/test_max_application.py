import shutil
import tempfile
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from PIL import Image

from app.config import Settings
from app.database import Database
from app.demo_service import DemoService
from app.domain import (
    DailyBudgetError,
    DeliveryError,
    ImageTooLargeError,
    PolicyRejectedError,
    ProviderQuotaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.edit_intent import EditPlan
from app.image_provider import FakeImageProvider
from app.max_application import (
    MaxApplication,
    OWNER_ONLY_TEXT,
    PHOTO_ACCEPTED_TEXT,
    PROCESSING_TEXT,
)
from app.max_conversation import MaxConversationStore
from app.max_transport import MaxIncomingEvent, MaxTransportError
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
        self.edits = []
        self.callbacks = []
        self.image_delivery = True
        self.fail_next_message = False
        self.fail_next_edit = False
        self.on_send_message = None

    def send_message(self, user_id, text, buttons=(), **kwargs):
        if self.fail_next_message:
            self.fail_next_message = False
            raise MaxTransportError("fake send failure")
        if self.on_send_message:
            self.on_send_message(text)
        message_id = f"sent-{len(self.messages) + 1}"
        self.messages.append((user_id, text, tuple(buttons), kwargs, message_id))
        return message_id

    def edit_message(self, message_id, text, buttons=()):
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise MaxTransportError("fake edit failure")
        self.edits.append((message_id, text, tuple(buttons)))

    def answer_callback(self, callback_id, notification):
        self.callbacks.append((callback_id, notification))

    def download_image(self, _url, destination, _max_bytes):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source, destination)
        return destination

    def send_image(self, user_id, image, caption, buttons):
        self.images.append((user_id, Path(image), caption, tuple(buttons)))
        return self.image_delivery


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
        )
        self.clock = Clock()
        self.database = Database(self.settings.database_path)
        self.storage = PrivateStorage(
            self.settings.users_dir,
            self.settings.max_source_file_size_mb * 1024 * 1024,
        )
        self.provider = FakeImageProvider()
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

    def event(self, event_type, *, text=None, image_url=None, action=None):
        self.sequence += 1
        key = (
            f"callback:cb-{self.sequence}"
            if event_type == "message_callback"
            else f"message:mid-{self.sequence}"
            if event_type == "message_created"
            else f"start:c1:u1:{self.sequence}"
        )
        return MaxIncomingEvent(
            event_type, key, "u1", "c1", self.sequence,
            message_id=f"mid-{self.sequence}" if event_type != "bot_started" else None,
            text=text,
            image_url=image_url,
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

    def onboard_to_prompt(self) -> None:
        self.app.handle(self.event("bot_started"))
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        menu = self.transport.messages[-1]
        self.assertIn("Прикрепите фотографию через скрепку 📎", menu[1])
        self.assertEqual(
            [button.text for button in menu[2]],
            ["Публичная оферта", "Обработка персональных данных"],
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

    def test_start_direct_upload_details_and_implicit_consent(self) -> None:
        self.app.handle(self.event("message_created", text="/start"))
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")
        start_message = self.transport.messages[-1]
        self.assertIn("Прикрепите фотографию через скрепку 📎", start_message[1])
        self.assertNotIn("Фото принято", start_message[1])
        self.assertEqual(
            [(button.text, button.action) for button in start_message[2]],
            [
                (
                    "Публичная оферта",
                    "https://ravuna.ru/legal/offer.html",
                ),
                (
                    "Обработка персональных данных",
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
            ["Что хотите изменить?"],
        )
        self.assertEqual(self.store.get("u1").state, "waiting_for_prompt")
        self.assertEqual(self.provider.calls, 0)

    def test_prompt_before_photo_is_saved_and_runs_when_photo_arrives(self) -> None:
        self.app.handle(self.event("message_created", text="/start"))
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
        self.assertEqual(restarted.state, "waiting_for_source")
        self.assertIsNone(restarted.session_id)
        self.assertIsNone(restarted.current_gallery_item_id)
        self.assertIsNone(restarted.current_version_id)
        self.assertEqual(self.provider.calls, 0)
        self.assertIn(
            "Прикрепите фотографию через скрепку 📎",
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

    def test_image_outside_waiting_for_source_is_saved_and_not_silently_ignored(self) -> None:
        self.app.handle(self.event("bot_started"))
        self.assertEqual(self.store.get("u1").state, "waiting_for_source")

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
            "Готово",
        )
        self.assertEqual(
            [button.text for button in self.transport.images[-1][3]],
            [
                "Получить оригинал",
                "Исправить",
                "Другой вариант",
                "История версий",
                "Мои работы",
            ],
        )
        self.assertEqual(self.transport.edits[-1][1], "✨ Готово")
        with self.database.read() as connection:
            attempt = connection.execute(
                "SELECT original_result_path,demo_result_path FROM generation_attempts"
            ).fetchone()
        self.assertEqual(delivered_path, Path(attempt["demo_result_path"]))
        self.assertNotEqual(delivered_path, Path(attempt["original_result_path"]))

        duplicate = self.event("message_created", text="ignored")
        self.assertTrue(self.app.handle(duplicate))
        message_count = len(self.transport.messages)
        self.assertFalse(self.app.handle(duplicate))
        self.assertEqual(len(self.transport.messages), message_count)

        unlock_event = self.callback("result:unlock")
        self.assertIn(
            "Пакет доступа Ravuna — 49 ₽",
            self.transport.messages[-1][1],
        )
        self.assertNotIn(str(attempt["original_result_path"]), self.transport.messages[-1][1])
        callback_message_count = len(self.transport.messages)
        self.assertFalse(self.app.handle(unlock_event))
        self.assertEqual(len(self.transport.messages), callback_message_count)

    def test_repeated_payment_click_sends_one_card_and_one_short_notice(self) -> None:
        self.generate_first()
        paid_app, _payments = self.paid_application()

        paid_app.handle(self.event("message_callback", action="result:unlock"))
        paid_app.handle(self.event("message_callback", action="result:unlock"))

        payment_cards = [
            message for message in self.transport.messages
            if message[1].startswith("Пакет доступа Ravuna — 49 ₽")
            and message[2]
            and message[2][0].text == "Оплатить 49 ₽"
        ]
        self.assertEqual(len(payment_cards), 1)
        self.assertEqual(
            self.transport.messages[-1][1],
            "Ссылка на оплату уже создана.",
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
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM payment_attempts
                       WHERE purpose='max_checkout_card'"""
                ).fetchone()[0],
                1,
            )

    def test_parallel_payment_clicks_send_one_card_atomically(self) -> None:
        self.generate_first()
        paid_app, _payments = self.paid_application()
        events = [
            self.event("message_callback", action="result:unlock"),
            self.event("message_callback", action="result:unlock"),
        ]

        with ThreadPoolExecutor(max_workers=2) as executor:
            handled = list(executor.map(paid_app.handle, events))

        self.assertEqual(handled, [True, True])
        payment_cards = [
            message for message in self.transport.messages
            if message[1].startswith("Пакет доступа Ravuna — 49 ₽")
            and message[2]
            and message[2][0].text == "Оплатить 49 ₽"
        ]
        self.assertEqual(len(payment_cards), 1)
        self.assertEqual(
            sum(
                message[1] == "Ссылка на оплату уже создана."
                for message in self.transport.messages
            ),
            1,
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

    def test_repeated_paid_purchase_goes_to_selected_original(self) -> None:
        self.generate_first()
        paid_app, payments = self.paid_application()
        paid_app.handle(self.event("message_callback", action="result:unlock"))
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

        paid_app.handle(self.event("message_callback", action="package:buy"))

        self.assertEqual(self.transport.images[-1][1], original)
        self.assertEqual(len(self.transport.messages), message_count + 1)
        self.assertEqual(self.transport.messages[-1][1], "Оригинал готов ✅")
        self.assertEqual(
            [button.text for button in self.transport.messages[-1][2]],
            [
                "📷 Обработать другую фотографию",
                "📁 Мои работы",
            ],
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

    def test_owner_sandbox_payment_grants_pack_then_user_selects_original(self) -> None:
        self.generate_first()
        paid_app, payments = self.paid_application()
        event = self.event("message_callback", action="result:unlock")
        paid_app.handle(event)
        self.assertEqual(
            self.transport.messages[-1][1],
            "Пакет доступа Ravuna — 49 ₽\n\n"
            "После оплаты начисляется:\n"
            "• 2 обработки\n"
            "• 1 оригинал\n\n"
            "Пакет начисляется сразу после оплаты.",
        )
        pay_button = self.transport.messages[-1][2][0]
        self.assertEqual(pay_button.text, "Оплатить 49 ₽")
        self.assertTrue(pay_button.action.startswith("https://auth.robokassa.ru/"))
        with self.database.read() as connection:
            order = connection.execute("SELECT * FROM payment_orders").fetchone()
            original = Path(connection.execute(
                "SELECT original_path FROM gallery_versions WHERE id=?",
                (order["version_id"],),
            ).fetchone()[0])
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
        paid_message_start = len(self.transport.messages)
        self.assertTrue(paid_app.notify_continuation_pack_paid(order["id"]))
        paid_messages = self.transport.messages[paid_message_start:]
        self.assertEqual(len(paid_messages), 2)
        self.assertEqual(
            paid_messages[0][1],
            "✅ Оплата прошла успешно\n\n"
            "Ваш оригинал готов к скачиванию.",
        )
        self.assertEqual(
            [button.text for button in paid_messages[0][2]],
            ["📥 Скачать оригинал"],
        )
        self.assertEqual(
            paid_messages[1][1],
            "Осталось:\n"
            "• обработок — 3;\n"
            "• оригиналов — 1.",
        )
        self.assertEqual(
            [button.text for button in paid_messages[1][2]],
            [
                "📷 Другая фотография",
                "📁 Мои работы",
            ],
        )
        self.assertTrue(all(len(message[2]) <= 3 for message in paid_messages))
        paid_copy = "\n".join(message[1] for message in paid_messages)
        self.assertNotIn("После оплаты начисляется", paid_copy)
        self.assertNotIn("Пакет доступа Ravuna", paid_copy)
        self.assertNotIn("Что дальше?", paid_copy)
        paid_dialog = self.store.get("u1")
        self.assertEqual(paid_dialog.current_version_id, intent_version_id)
        self.assertEqual(paid_dialog.current_gallery_item_id, gallery_item_id)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT unlock_status FROM gallery_versions WHERE id=?",
                    (intent_version_id,),
                ).fetchone()[0],
                "demo",
            )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE payment_orders SET version_id=? WHERE id=?",
                (intent_version_id, order["id"]),
            )
        self.transport.image_delivery = False
        paid_app.handle(self.event("message_callback", action="result:unlock"))
        failed_delivery = self.transport.messages[-1]
        self.assertEqual(
            failed_delivery[1],
            "Не удалось отправить оригинал. Право на скачивание сохранено.",
        )
        self.assertEqual(
            [button.text for button in failed_delivery[2]],
            [
                "🔁 Повторить скачивание",
                "📷 Другая фотография",
                "📁 Мои работы",
            ],
        )
        with self.database.read() as connection:
            entitlement = connection.execute(
                """SELECT status,gallery_version_id FROM unlock_entitlements
                   WHERE source_payment_order_id=?""",
                (order["id"],),
            ).fetchone()
            self.assertEqual(entitlement["status"], "available")
            self.assertIsNone(entitlement["gallery_version_id"])
            self.assertEqual(
                connection.execute(
                    "SELECT unlock_status FROM gallery_versions WHERE id=?",
                    (intent_version_id,),
                ).fetchone()[0],
                "demo",
            )
        self.transport.image_delivery = True
        paid_app.handle(self.event("message_callback", action="result:unlock"))
        self.assertEqual(self.transport.images[-1][1], original)
        delivered_message = self.transport.messages[-1]
        self.assertEqual(delivered_message[1], "Оригинал готов ✅")
        self.assertEqual(
            [button.text for button in delivered_message[2]],
            [
                "📷 Обработать другую фотографию",
                "📁 Мои работы",
            ],
        )
        self.assertNotIn("Что дальше?", delivered_message[1])
        self.assertNotIn("Оплат", delivered_message[1])
        self.assertLessEqual(len(delivered_message[2]), 3)
        paid_app.handle(self.event("message_callback", action="result:unlock"))
        self.assertEqual(self.transport.images[-1][1], original)
        self.assertEqual(self.transport.messages[-1][1], "Оригинал готов ✅")
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT delivery_count FROM gallery_versions WHERE id=?",
                    (intent_version_id,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_orders").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_receipts").fetchone()[0],
                1,
            )
            work_count = connection.execute(
                "SELECT COUNT(*) FROM gallery_items WHERE user_id=?",
                (order["user_id"],),
            ).fetchone()[0]
        balance_before_new_source = self.demo.commerce.balance(order["user_id"])
        entitlements_before_new_source = self.demo.commerce.entitlement_balance(
            order["user_id"]
        )
        paid_app.handle(self.event("message_callback", action="new:source"))
        restarted = self.store.get("u1")
        self.assertEqual(restarted.state, "waiting_for_source")
        self.assertIsNone(restarted.current_gallery_item_id)
        self.assertIsNone(restarted.current_version_id)
        self.assertIn(
            "Прикрепите фотографию через скрепку 📎",
            self.transport.messages[-1][1],
        )
        self.assertEqual(
            self.demo.commerce.balance(order["user_id"]),
            balance_before_new_source,
        )
        self.assertEqual(
            self.demo.commerce.entitlement_balance(order["user_id"]),
            entitlements_before_new_source,
        )
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM gallery_items WHERE user_id=?",
                    (order["user_id"],),
                ).fetchone()[0],
                work_count,
            )

    def test_true_intent_conflict_is_resolved_without_an_extra_question(self) -> None:
        self.onboard_to_prompt()
        self.app.handle(
            self.event("message_created", text="Поменяй фон, но фон не меняй")
        )
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.store.get("u1").state, "result_ready")
        self.assertFalse(any("Оставить текущий фон" in row[1] for row in self.transport.messages))

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
        self.assertEqual(row["sentiment"], "negative")
        self.assertIsNone(row["reason_category"])

    def test_correction_repeat_gallery_navigation_favorite_and_physical_delete(self) -> None:
        self.generate_first()
        first = self.store.get("u1").current_version_id
        self.callback("result:correct")
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
