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
    delivered_actions,
    gallery_item_actions,
    gallery_more_actions,
    ideas_catalog,
    legal_details_view,
    legal_view,
    main_menu,
    new_source_view,
    paid_actions,
    photoshoot_catalog,
    result_actions,
    retry_delivery_actions,
    scenario_catalog,
    settings_view,
    studio_menu_contract,
    upload_view,
    version_history_actions,
)
from app.scenarios import SCENARIO_CATEGORIES


class Transport:
    def __init__(self) -> None:
        self.sent = []

    def send_image(self, platform_user_id, image, caption, buttons):
        self.sent.append((platform_user_id, image, caption, buttons))
        return f"image-{len(self.sent)}"


class MaxAdapterTests(TestCase):
    def test_menu_order_catalog_and_required_copy(self) -> None:
        menu = main_menu(2, 0)
        self.assertEqual(
            [button.text for button in menu.buttons],
            [
                "📁 Мои работы",
                "📄 Публичная оферта",
                "🔐 Обработка персональных данных",
            ],
        )
        self.assertEqual(
            [button.action for button in menu.buttons[-2:]],
            [
                "https://ravuna.ru/legal/offer.html",
                "https://ravuna.ru/legal/personal-data.html",
            ],
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
            [
                "📂 Мои работы",
                "⭐ Избранное",
                "Последние",
                "Коллекции",
                "🗑 Корзина",
                "← Назад",
            ],
        )
        self.assertIn("Что хотите сделать с фотографией?", WELCOME_TEXT)
        self.assertIn("прикрепите фотографию через скрепку внизу чата", WELCOME_TEXT)
        self.assertIn("до 2 фотографий для одной обработки", WELCOME_TEXT)
        self.assertIn("• поменять одежду", WELCOME_TEXT)
        self.assertIn("• улучшить качество", WELCOME_TEXT)
        self.assertNotIn("upload:ready", [button.action for button in menu.buttons])
        upload = upload_view()
        self.assertIn("Прикрепите фотографию через скрепку 📎", upload.text)
        self.assertIn("до 2 фотографий для одной обработки", upload.text)
        self.assertIn("вправе его использовать", upload.text)
        self.assertIn("AI-провайдеру только для выполнения обработки", upload.text)
        self.assertEqual(
            [(button.text, button.action) for button in upload.buttons],
            [
                ("📄 Публичная оферта", "https://ravuna.ru/legal/offer.html"),
                (
                    "🔐 Обработка персональных данных",
                    "https://ravuna.ru/legal/personal-data.html",
                ),
                ("← Назад", "nav:back:main"),
            ],
        )
        self.assertEqual([button.row for button in upload.buttons], [0, 0, 1])
        new_source = new_source_view()
        self.assertIn("Загрузите другое фото", new_source.text)
        self.assertIn("Прикрепите фотографию через скрепку 📎", new_source.text)
        self.assertIn("до 2 фотографий", new_source.text)
        self.assertIn("вправе его использовать", new_source.text)
        self.assertEqual(
            [(button.text, button.action) for button in new_source.buttons],
            [(button.text, button.action) for button in upload.buttons],
        )
        self.assertIn("Хотите ещё 2 обработки бесплатно?", result_actions(4).text)
        self.assertIn("2 обработки бесплатно", result_actions(0).text)
        self.assertEqual(
            [button.text for button in result_actions(4).buttons],
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
            [button.row for button in result_actions(4).buttons],
            [0, 1, 1, 2, 3, 3, 4, 5],
        )
        self.assertEqual(
            [button.text for button in result_actions(0).buttons],
            [
                "⬇️ Получить оригинал",
                "💳 Купить 2 обработки — 49 ₽",
                "💳 100 обработок + 50 оригиналов — 1990 ₽",
                "🎁 Пригласить друга — получить +2 обработки",
                "⭐ Оценить",
                "💬 Отзыв о Ravuna",
                "📁 Мои работы",
                "← Назад",
            ],
        )
        self.assertEqual(
            [button.row for button in result_actions(0).buttons],
            [0, 1, 2, 3, 4, 4, 5, 6],
        )
        self.assertEqual(
            [button.text for button in paid_actions()],
            [
                "📥 Скачать оригинал",
                "📷 Другая фотография",
                "📁 Мои работы",
                "← Назад",
            ],
        )
        self.assertEqual(
            [button.text for button in delivered_actions()],
            [
                "📷 Обработать другую фотографию",
                "📁 Мои работы",
                "← Назад",
            ],
        )
        self.assertEqual(
            [button.text for button in retry_delivery_actions()],
            [
                "🔁 Повторить скачивание",
                "📷 Другая фотография",
                "📁 Мои работы",
                "← Назад",
            ],
        )
        self.assertEqual(len(gallery_item_actions()), 7)
        self.assertEqual(
            [button.text for button in gallery_item_actions(0)],
            [
                "⬇ Получить оригинал",
                "🎁 Поделиться и получить бонус",
                "💳 Купить ещё 2 обработки — 49 ₽",
                "💳 100 обработок + 50 оригиналов — 1990 ₽",
                "История версий",
                "Ещё",
                "← Назад",
            ],
        )
        self.assertEqual(len(gallery_more_actions().buttons), 4)
        self.assertNotIn("👍 Получилось", [button.text for button in gallery_item_actions()])
        self.assertNotIn("👎 Не то", [button.text for button in gallery_item_actions()])
        self.assertEqual(len(version_history_actions()), 4)

    def test_zero_balance_main_menu_offers_both_one_time_packages(self) -> None:
        menu = main_menu(0, 0, "https://ravuna.ru/p/" + "a" * 32)
        actions = {button.text: button.action for button in menu.buttons}
        self.assertEqual(
            actions["Купить пакет — 49 ₽"],
            "https://ravuna.ru/p/" + "a" * 32,
        )
        self.assertEqual(
            actions["Купить 100 обработок + 50 оригиналов — 1990 ₽"],
            "package:buy:large",
        )

    def test_main_menu_shows_processing_and_original_balances(self) -> None:
        for processing, originals in ((2, 0), (4, 1), (104, 51)):
            with self.subTest(processing=processing, originals=originals):
                self.assertIn(
                    "Ваш баланс:\n"
                    f"⚡ Обработки: {processing}\n"
                    f"🖼 Оригиналы без водяного знака: {originals}",
                    main_menu(processing, originals).text,
                )

    def test_every_nested_menu_ends_with_exact_back_button(self) -> None:
        views = (
            legal_view(),
            settings_view(),
            photoshoot_catalog(),
            ideas_catalog(),
            scenario_catalog(),
            studio_menu_contract(),
            new_source_view(),
            result_actions(1),
            result_actions(0),
            delete_confirmation_view(),
            gallery_more_actions(),
        )
        button_groups = (
            *(view.buttons for view in views),
            paid_actions(),
            delivered_actions(),
            retry_delivery_actions(),
            gallery_item_actions(1),
            gallery_item_actions(0),
            version_history_actions(),
        )
        for buttons in button_groups:
            self.assertTrue(buttons)
            self.assertEqual(buttons[-1].text, "← Назад")
        self.assertNotIn("← Назад", [button.text for button in main_menu(2, 0).buttons])

    def test_user_screens_are_short_and_hide_internal_vocabulary(self) -> None:
        views = (
            (main_menu(2, 0), 380),
            (legal_view(), 300),
            (legal_details_view(), 300),
            (settings_view(), 300),
            (photoshoot_catalog(), 300),
            (result_actions(4), 300),
            (result_actions(0), 300),
            (delete_confirmation_view(), 300),
        )
        forbidden = (
            "demo session",
            "source",
            "storage",
            "gallery version",
            "preview",
            "originals",
            "gpt",
            "генерац",
        )
        for view, max_length in views:
            normalized = view.text.lower()
            for term in forbidden:
                self.assertNotIn(term, normalized)
            self.assertLessEqual(len(view.text), max_length)

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
