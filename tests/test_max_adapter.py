import tempfile
from pathlib import Path
from unittest import TestCase

from PIL import Image

from app.config import Settings
from app.database import Database
from app.domain import InvalidInputError
from app.image_service import build_demo_service
from app.max_adapter import (
    MaxDemoAdapter,
    WELCOME_TEXT,
    delete_confirmation_view,
    gallery_item_actions,
    legal_details_view,
    legal_view,
    main_menu,
    photoshoot_catalog,
    result_actions,
    scenario_catalog,
    settings_view,
    studio_menu_contract,
    version_history_actions,
)
from app.scenarios import SCENARIO_CATEGORIES


class Transport:
    def __init__(self) -> None:
        self.sent = []

    def send_image(self, platform_user_id, image, caption, buttons):
        self.sent.append((platform_user_id, image, caption, buttons))
        return True


class MaxAdapterTests(TestCase):
    def test_menu_order_catalog_and_required_copy(self) -> None:
        menu = main_menu()
        self.assertEqual(
            [button.text for button in menu.buttons],
            ["Подробнее"],
        )
        self.assertNotIn("GPT", menu.text + " ".join(button.text for button in menu.buttons))
        self.assertGreaterEqual(len(scenario_catalog().buttons), 12)
        self.assertEqual(len(SCENARIO_CATEGORIES), 12)
        self.assertNotIn("Продолжить", [button.text for button in legal_view().buttons])
        self.assertEqual(len(legal_details_view().buttons), 5)
        self.assertEqual(len(photoshoot_catalog().buttons), 4)
        self.assertEqual(len(settings_view().buttons), 5)
        self.assertEqual(
            [button.text for button in studio_menu_contract().buttons],
            ["📂 Мои работы", "⭐ Избранное", "Последние", "Коллекции", "🗑 Корзина"],
        )
        self.assertIn("Просто отправьте фотографию", WELCOME_TEXT)
        self.assertIn("соглашаетесь с обработкой фотографии", WELCOME_TEXT)
        self.assertEqual(result_actions(4).text, "Это демо с водяным знаком.")
        self.assertNotIn("4", result_actions(4).text)
        self.assertIn("Бесплатные варианты закончились", result_actions(0).text)
        self.assertEqual(result_actions(4).buttons[0].text, "⬇ Получить оригинал")
        self.assertEqual(len(gallery_item_actions()), 10)
        self.assertIn("👍 Получилось", [button.text for button in gallery_item_actions()])
        self.assertIn("👎 Не то", [button.text for button in gallery_item_actions()])
        self.assertEqual(len(version_history_actions()), 4)

    def test_user_screens_are_short_and_hide_internal_vocabulary(self) -> None:
        views = (
            main_menu(),
            legal_view(),
            legal_details_view(),
            settings_view(),
            photoshoot_catalog(),
            result_actions(4),
            result_actions(0),
            delete_confirmation_view(),
        )
        forbidden = (
            "demo session",
            "source",
            "storage",
            "gallery version",
            "preview",
            "originals",
            "gpt",
        )
        for view in views:
            normalized = view.text.lower()
            for term in forbidden:
                self.assertNotIn(term, normalized)
            self.assertLessEqual(len(view.text), 300)

    def test_consent_is_required_and_only_preview_is_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ("data", "logs", "temp"):
                (base / name).mkdir()
            settings = Settings("", "fake-image-edit-v1", "test", base, demo_min_request_interval_seconds=1)
            database = Database(settings.database_path)
            service = build_demo_service(settings, provider_name="fake")
            transport = Transport()
            adapter = MaxDemoAdapter(service, database, transport)
            source = base / "source.png"
            Image.new("RGB", (320, 240), "white").save(source)
            with self.assertRaises(InvalidInputError):
                adapter.start_demo("max-user", source)
            adapter.record_consent(
                "max-user",
                offer=True,
                personal_data=True,
                image_rights=True,
                external_ai=True,
                appearance_change=True,
            )
            session = adapter.start_demo("max-user", source)
            result = adapter.generate("max-user", session.session_id, "Светлый фон", "max-event")
            self.assertEqual(len(transport.sent), 1)
            delivered = Path(transport.sent[0][1])
            self.assertEqual(delivered, result.preview_path)
            with database.read() as connection:
                original = Path(connection.execute(
                    "SELECT original_result_path FROM generation_attempts WHERE id=?", (result.attempt_id,)
                ).fetchone()[0])
            self.assertNotEqual(delivered, original)
            self.assertNotIn(str(original), transport.sent[0][2])
            intent = adapter.unlock(result.attempt_id, "unlock-event")
            self.assertTrue(intent)
            root = service.storage.session_root(session.user_id, session.session_id)
            adapter.delete(session.session_id)
            self.assertTrue(root.exists())

    def test_implicit_consent_is_recorded_only_after_valid_photo_is_stored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ("data", "logs", "temp"):
                (base / name).mkdir()
            settings = Settings("", "fake-image-edit-v1", "test", base)
            database = Database(settings.database_path)
            service = build_demo_service(settings, provider_name="fake")
            adapter = MaxDemoAdapter(service, database, Transport())
            invalid = base / "invalid.bin"
            invalid.write_text("not an image", encoding="utf-8")
            with self.assertRaises(InvalidInputError):
                adapter.start_demo_with_implicit_consent("implicit-user", invalid)
            with database.read() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM legal_consents").fetchone()[0],
                    0,
                )

            source = base / "source.png"
            Image.new("RGB", (320, 240), "white").save(source)
            session = adapter.start_demo_with_implicit_consent("implicit-user", source)
            self.assertTrue(session.source_path.is_file())
            with database.read() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM legal_consents").fetchone()[0],
                    1,
                )
                self.assertGreater(
                    connection.execute("SELECT COUNT(*) FROM max_legal_acceptances").fetchone()[0],
                    0,
                )
