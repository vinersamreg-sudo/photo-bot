import tempfile
from pathlib import Path
from unittest import TestCase

from PIL import Image

from app.config import Settings
from app.database import Database
from app.domain import InvalidInputError
from app.image_service import build_demo_service
from app.max_adapter import MaxDemoAdapter, WELCOME_TEXT, legal_view, main_menu, result_actions, scenario_catalog, studio_menu_contract
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
        self.assertEqual(menu.buttons[0].text, "💬 Своя идея")
        self.assertEqual(len(menu.buttons), 9)
        self.assertNotIn("GPT", menu.text + " ".join(button.text for button in menu.buttons))
        self.assertGreaterEqual(len(scenario_catalog().buttons), 9)
        self.assertEqual(len(SCENARIO_CATEGORIES), 12)
        self.assertEqual(len(legal_view().buttons), 3)
        self.assertEqual(
            [button.text for button in studio_menu_contract().buttons],
            ["📁 Мои работы", "⭐ Избранное", "🕒 Последние", "📂 Коллекции", "🗑 Корзина"],
        )
        self.assertIn("одной исходной фотографии", WELCOME_TEXT)
        self.assertIn("Бесплатных правок осталось: 4", result_actions(4).text)
        self.assertIn("демонстрация завершена", result_actions(0).text)

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
