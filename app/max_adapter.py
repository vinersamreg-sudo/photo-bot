"""MAX-facing interaction contract, independent of a concrete MAX transport SDK."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from app.database import Database
from app.demo_service import DeliverPreview, DemoService, iso, utc_now
from app.domain import DemoGenerationResult, DemoSessionInfo, InvalidInputError
from app.scenarios import SCENARIOS


WELCOME_TEXT = (
    "ИИ-редактор фото\n\n"
    "Попробуйте обработку бесплатно.\n\n"
    "Загрузите одну фотографию и получите до пяти вариантов с водяным знаком «ОБРАЗЕЦ».\n\n"
    "Если результат понравится, позже сможете получить оригинал без водяного знака.\n\n"
    "Бесплатная демонстрация действует для одной исходной фотографии."
)

LEGAL_TEXT = (
    "Перед загрузкой подтвердите оферту и согласие на обработку персональных данных. "
    "Также подтвердите права на изображение. Для обработки используется внешний AI-провайдер; "
    "результат может изменить внешность и детали."
)


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
        "Что хотите сделать с фотографией?",
        (
            Button("💬 Своя идея", "custom"),
            Button("👔 Фото для работы и резюме", "scenario:resume"),
            Button("🌆 Сменить фон", "scenario:light-background"),
            Button("👕 Одежда и образ", "scenario:business-look"),
            Button("🕰 Восстановить старое фото", "scenario:restore"),
            Button("✨ Улучшить качество", "scenario:enhance"),
            Button("📸 Готовые фотосессии", "catalog:photoshoot"),
            Button("📁 Мои работы", "studio:works"),
            Button("ℹ️ Правила и приватность", "legal:show"),
        ),
    )


def legal_view() -> View:
    return View(
        LEGAL_TEXT,
        (
            Button("📄 Оферта", "legal:offer"),
            Button("🔐 Обработка данных", "legal:privacy"),
            Button("☑️ Принимаю обязательные документы", "legal:accept_all"),
        ),
    )


def scenario_catalog() -> View:
    buttons = tuple(
        Button(f"{scenario.emoji} {scenario.title}", f"scenario:{scenario.id}")
        for scenario in sorted(SCENARIOS, key=lambda value: value.sort_order)
        if scenario.active
    ) + (Button("🏠 Главное меню", "menu"),)
    return View("Выберите готовый сценарий:", buttons)


def studio_menu_contract() -> View:
    """Future navigation contract; no MAX routing is attached yet."""
    return View(
        "Ваша личная AI-фотостудия",
        (
            Button("📁 Мои работы", "studio:works"),
            Button("⭐ Избранное", "studio:favorites"),
            Button("🕒 Последние", "studio:recent"),
            Button("📂 Коллекции", "studio:collections"),
            Button("🗑 Корзина", "studio:trash"),
        ),
    )


def result_actions(remaining: int) -> View:
    if remaining > 0:
        text = (
            "Это демо-результат с водяным знаком.\n\n"
            f"Бесплатных правок осталось: {remaining}.\n\nЧто сделать дальше?"
        )
    else:
        text = (
            "Бесплатная демонстрация завершена.\n\n"
            "Вы уже увидели, как сервис обрабатывает вашу фотографию.\n\n"
            "Получить оригинал без водяного знака или продолжить правки можно после оплаты."
        )
    return View(
        text,
        (
            Button("✅ Получить оригинал", "result:unlock"),
            Button("✏️ Исправить", "result:correct"),
            Button("🔄 Другой вариант", "result:repeat"),
            Button("⭐ В избранное", "result:favorite"),
            Button("📁 Мои работы", "studio:works"),
            Button("🗑 Удалить", "result:delete"),
            Button("🏠 Главное меню", "menu"),
        ),
    )


def confirmation_view(prompt: str, remaining: int) -> View:
    safe_prompt = " ".join(prompt.split())[:500]
    return View(
        "Я понял задачу так:\n\n"
        f"{safe_prompt}\n\n"
        "Будет использована ваша текущая фотография.\n"
        "Результат придёт с водяным знаком «ОБРАЗЕЦ».\n"
        f"Бесплатных вариантов доступно: {remaining}.\n\n"
        "Начать обработку?",
        (
            Button("✅ Начать", "prompt:start"),
            Button("✏️ Изменить описание", "prompt:edit"),
            Button("🗑 Отменить", "prompt:cancel"),
        ),
    )


def delete_confirmation_view() -> View:
    return View(
        "Удалить эту работу и все её версии? Исходник, preview и originals будут удалены без восстановления.",
        (
            Button("🗑 Да, удалить", "delete:confirm"),
            Button("↩️ Не удалять", "delete:cancel"),
        ),
    )


def gallery_item_actions() -> tuple[Button, ...]:
    return (
        Button("⬅️ Предыдущая", "work:previous"),
        Button("➡️ Следующая", "work:next"),
        Button("🏆 Сделать главной", "work:main"),
        Button("✏️ Исправить", "result:correct"),
        Button("🔄 Другой вариант", "result:repeat"),
        Button("⭐ В избранное", "result:favorite"),
        Button("🗑 Удалить", "result:delete"),
        Button("📁 К списку работ", "studio:works"),
        Button("🏠 Главное меню", "menu"),
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
                "Демо-результат. Оригинал хранится отдельно и станет доступен только после оплаты.",
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
