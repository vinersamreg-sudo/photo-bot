"""MAX-facing interaction contract, independent of a concrete MAX transport SDK."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from app.database import Database
from app.demo_service import DeliverPreview, DemoService, iso, utc_now
from app.domain import DemoGenerationResult, DemoSessionInfo, InvalidInputError
from app.scenarios import SCENARIOS


WELCOME_TEXT = "✨ Pixora\n\nЧто хотите сделать?"

LEGAL_TEXT = "✨ Pixora\n\nПродолжая, вы принимаете условия использования."


@dataclass(frozen=True)
class Button:
    text: str
    action: str


@dataclass(frozen=True)
class View:
    text: str
    buttons: tuple[Button, ...]


class MaxTransport(Protocol):
    def send_image(self, platform_user_id: str, image: Path, caption: str, buttons: Sequence[Button]) -> bool: ...


def main_menu() -> View:
    return View(
        WELCOME_TEXT,
        (
            Button("💬 Своя идея", "custom"),
            Button("📸 Сменить фон", "scenario:light-background"),
            Button("👔 Фото для работы", "scenario:resume"),
            Button("✨ Улучшить качество", "scenario:enhance"),
            Button("🧥 Одежда и образ", "scenario:business-look"),
            Button("🪄 Восстановить старое фото", "scenario:restore"),
            Button("🎭 Готовые фотосессии", "catalog:photoshoot"),
            Button("📂 Мои работы", "studio:works"),
            Button("⚙️ Настройки", "settings"),
        ),
    )


def legal_view() -> View:
    return View(
        LEGAL_TEXT,
        (
            Button("Продолжить", "legal:accept_all"),
            Button("Подробнее →", "legal:details"),
        ),
    )


def legal_details_view() -> View:
    return View(
        "Фото обрабатывается с помощью внешнего AI-сервиса. Загружая фото, вы подтверждаете право на его использование.",
        (
            Button("Продолжить", "legal:accept_all"),
            Button("← Назад", "legal:back"),
        ),
    )


def settings_view() -> View:
    return View(
        "⚙️ Настройки\n\nУсловия использования и приватность.",
        (
            Button("Условия", "legal:offer"),
            Button("Приватность", "legal:privacy"),
            Button("← Назад", "menu"),
        ),
    )


def photoshoot_catalog() -> View:
    return View(
        "🎭 Готовые фотосессии\n\nВыберите образ.",
        (
            Button("В уютном кафе", "scenario:cafe"),
            Button("На пляже", "scenario:beach"),
            Button("С питомцем", "scenario:cat"),
            Button("← Назад", "menu"),
        ),
    )


def scenario_catalog() -> View:
    buttons = tuple(
        Button(f"{scenario.emoji} {scenario.title}", f"scenario:{scenario.id}")
        for scenario in sorted(SCENARIOS, key=lambda value: value.sort_order)
        if scenario.active
    ) + (Button("← Назад", "menu"),)
    return View("Выберите образ.", buttons)


def studio_menu_contract() -> View:
    """Future navigation contract; no MAX routing is attached yet."""
    return View(
        "📂 Мои работы",
        (
            Button("📂 Мои работы", "studio:works"),
            Button("⭐ Избранное", "studio:favorites"),
            Button("Последние", "studio:recent"),
            Button("Коллекции", "studio:collections"),
            Button("🗑 Корзина", "studio:trash"),
        ),
    )


def result_actions(remaining: int) -> View:
    if remaining > 0:
        text = "Это демо с водяным знаком."
        buttons = (
            Button("⬇ Получить оригинал", "result:unlock"),
            Button("✨ Исправить", "result:correct"),
            Button("🎲 Другой вариант", "result:repeat"),
            Button("⭐ В избранное", "result:favorite"),
            Button("📂 Мои работы", "studio:works"),
            Button("🗑 Удалить", "result:delete"),
        )
    else:
        text = (
            "Бесплатные варианты закончились.\n\n"
            "Получите оригинал или продолжите после оплаты."
        )
        buttons = (
            Button("⬇ Получить оригинал", "result:unlock"),
            Button("📂 Мои работы", "studio:works"),
            Button("🗑 Удалить", "result:delete"),
        )
    return View(text, buttons)


def delete_confirmation_view() -> View:
    return View(
        "Удалить работу со всеми версиями?\n\nЭто нельзя отменить.",
        (
            Button("🗑 Удалить", "delete:confirm"),
            Button("Отмена", "delete:cancel"),
        ),
    )


def gallery_item_actions() -> tuple[Button, ...]:
    return (
        Button("⬇ Получить оригинал", "result:unlock"),
        Button("✨ Исправить", "result:correct"),
        Button("🎲 Другой вариант", "result:repeat"),
        Button("⭐ В избранное", "result:favorite"),
        Button("История версий", "work:history"),
        Button("🗑 Удалить", "result:delete"),
        Button("📂 К работам", "studio:works"),
    )


def version_history_actions() -> tuple[Button, ...]:
    return (
        Button("← Предыдущая", "work:previous"),
        Button("Следующая →", "work:next"),
        Button("Сделать основной", "work:main"),
        Button("← К работе", "work:open"),
    )


class MaxDemoAdapter:
    """Maps MAX identities and actions to the tested domain service."""

    platform = "max"

    def __init__(self, service: DemoService, database: Database, transport: MaxTransport) -> None:
        self.service = service
        self.database = database
        self.transport = transport

    def record_consent(
        self,
        platform_user_id: str,
        *,
        offer: bool,
        personal_data: bool,
        image_rights: bool,
        external_ai: bool,
        appearance_change: bool,
    ) -> None:
        if not all((offer, personal_data, image_rights, external_ai, appearance_change)):
            raise InvalidInputError("All required confirmations must be accepted before upload")
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO legal_consents(
                       platform,platform_user_id,offer_accepted,personal_data_accepted,
                       image_rights_confirmed,external_ai_acknowledged,
                       appearance_change_acknowledged,accepted_at
                   ) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(platform,platform_user_id) DO UPDATE SET
                       offer_accepted=excluded.offer_accepted,
                       personal_data_accepted=excluded.personal_data_accepted,
                       image_rights_confirmed=excluded.image_rights_confirmed,
                       external_ai_acknowledged=excluded.external_ai_acknowledged,
                       appearance_change_acknowledged=excluded.appearance_change_acknowledged,
                       accepted_at=excluded.accepted_at""",
                (self.platform, platform_user_id, 1, 1, 1, 1, 1, iso(utc_now())),
            )

    def _require_consent(self, platform_user_id: str) -> None:
        with self.database.read() as connection:
            consent = connection.execute(
                """SELECT * FROM legal_consents WHERE platform=? AND platform_user_id=?
                   AND offer_accepted=1 AND personal_data_accepted=1
                   AND image_rights_confirmed=1 AND external_ai_acknowledged=1
                   AND appearance_change_acknowledged=1""",
                (self.platform, platform_user_id),
            ).fetchone()
        if consent is None:
            raise InvalidInputError("Legal confirmation is required before photo upload")

    def start_demo(self, platform_user_id: str, source: Path) -> DemoSessionInfo:
        self._require_consent(platform_user_id)
        return self.service.start_session(self.platform, platform_user_id, source)

    def generate(
        self,
        platform_user_id: str,
        session_id: str,
        prompt: str,
        event_id: str,
        scenario_id: str | None = None,
        correction: bool = False,
        parent_version_id: str | None = None,
        delivery_override: DeliverPreview | None = None,
    ) -> DemoGenerationResult:
        self._require_consent(platform_user_id)
        def deliver(preview: Path, _attempt_id: str) -> bool:
            return self.transport.send_image(
                platform_user_id,
                preview,
                "Это демо с водяным знаком.",
                (),
            )

        return self.service.generate(
            session_id,
            prompt,
            event_id,
            scenario_id=scenario_id,
            correction=correction,
            delivery_override=delivery_override or deliver,
            parent_version_id=parent_version_id,
        )

    def delete(self, session_id: str) -> None:
        self.service.delete_session(session_id)

    def unlock(self, attempt_id: str, event_id: str) -> str:
        return self.service.unlock_original(attempt_id, f"max:{event_id}")
