"""Application controller connecting normalized MAX events to domain services."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol, Sequence

from app.config import Settings
from app.database import Database
from app.demo_service import DemoService
from app.domain import (
    CooldownError,
    DailyBudgetError,
    DeliveryError,
    DemoError,
    DemoExpiredError,
    DemoLimitError,
    InvalidInputError,
    PolicyRejectedError,
    SourceReplacementError,
)
from app.gallery import GalleryService, GalleryVersion
from app.max_adapter import (
    Button,
    MaxDemoAdapter,
    View,
    delete_confirmation_view,
    gallery_item_actions,
    legal_details_view,
    legal_view,
    main_menu,
    photoshoot_catalog,
    result_actions,
    settings_view,
    version_history_actions,
)
from app.max_conversation import MaxConversationStore, MaxDialog
from app.max_transport import MaxIncomingEvent, MaxTransportError


LOGGER = logging.getLogger(__name__)

PHOTO_ACCEPTED_TEXT = (
    "Фото получено ✅\n\n"
    "Что изменить?"
)
PROCESSING_TEXT = (
    "⏳ Обрабатываю фотографию…\n\n"
    "Обычно это занимает около минуты."
)
UNLOCK_PLACEHOLDER = (
    "Получение оригинала пока недоступно — идёт закрытое тестирование.\n\n"
    "Работа сохранена в «Моих работах»."
)
OWNER_ONLY_TEXT = (
    "Pixora пока в закрытом тестировании.\n\n"
    "Скоро откроем доступ."
)


class LiveMaxTransport(Protocol):
    def send_message(
        self, user_id: str, text: str, buttons: Sequence[Button] = (), **kwargs
    ) -> str: ...
    def edit_message(
        self, message_id: str, text: str, buttons: Sequence[Button] = ()
    ) -> None: ...
    def answer_callback(self, callback_id: str, notification: str) -> None: ...
    def download_image(self, url: str, destination: Path, max_bytes: int) -> Path: ...
    def send_image(
        self, platform_user_id: str, image: Path, caption: str,
        buttons: Sequence[Button],
    ) -> bool: ...


class MaxApplication:
    """Minimal restart-safe MAX vertical slice; no payment implementation."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        demo_service: DemoService,
        transport: LiveMaxTransport,
        store: Optional[MaxConversationStore] = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.demo = demo_service
        self.transport = transport
        self.store = store or MaxConversationStore(database)
        self.adapter = MaxDemoAdapter(demo_service, database, transport)
        self.gallery: GalleryService = demo_service.gallery

    def handle(self, event: MaxIncomingEvent) -> bool:
        """Handle one deduplicated event. Returns false for a known duplicate."""

        if not self.store.begin_event(event.event_key, event.event_type):
            return False
        try:
            if event.user_id not in self.settings.max_owner_user_ids:
                if event.callback_id:
                    self.transport.answer_callback(
                        event.callback_id, "Сервис находится в закрытом тестировании"
                    )
                self.transport.send_message(event.user_id, OWNER_ONLY_TEXT)
                self.store.finish_event(event.event_key, True)
                return True
            self._dispatch(event)
        except MaxTransportError:
            self.store.finish_event(event.event_key, False)
            raise
        except DemoExpiredError:
            LOGGER.info("MAX demo session expired (event_type=%s)", event.event_type)
            self._reset_dialog_to_main(event.user_id, event.event_key)
            self.transport.send_message(
                event.user_id,
                "Сессия завершилась.\n\nВыберите действие и загрузите фото снова.",
                main_menu().buttons,
            )
            self.store.finish_event(event.event_key, True)
            return True
        except DemoError as exc:
            LOGGER.info(
                "MAX domain request rejected (event_type=%s,error_type=%s)",
                event.event_type, type(exc).__name__,
            )
            self._show_demo_error(event, exc)
            self.store.finish_event(event.event_key, True)
            return True
        except Exception:
            self.store.finish_event(event.event_key, False)
            raise
        self.store.finish_event(event.event_key, True)
        return True

    def _show_demo_error(self, event: MaxIncomingEvent, exc: DemoError) -> None:
        if isinstance(exc, SourceReplacementError):
            self.transport.send_message(
                event.user_id,
                "Бесплатное демо уже связано с первой фотографией.\n\nПродолжите в «Моих работах».",
                (
                    Button("📂 Мои работы", "studio:works"),
                    Button("← В меню", "menu"),
                ),
            )
            return
        if isinstance(exc, DemoLimitError):
            self.transport.send_message(
                event.user_id,
                "Бесплатные варианты закончились.",
                (
                    Button("⬇ Получить оригинал", "result:unlock"),
                    Button("📂 Мои работы", "studio:works"),
                ),
            )
            return
        if isinstance(exc, CooldownError):
            self.transport.send_message(
                event.user_id,
                "Слишком быстро. Попробуйте ещё раз через минуту.",
                (Button("Попробовать снова", "prompt:start"),),
            )
            return
        if isinstance(exc, DailyBudgetError):
            self.transport.send_message(
                event.user_id,
                "Сегодня бесплатные обработки закончились.\n\nПопробуйте позже.",
                (Button("← В меню", "menu"),),
            )
            return
        if isinstance(exc, PolicyRejectedError):
            self.transport.send_message(
                event.user_id,
                "Не могу выполнить этот запрос. Попробуйте описать его иначе.",
                (Button("Изменить запрос", "prompt:edit"),),
            )
            return
        if isinstance(exc, DeliveryError):
            self.transport.send_message(
                event.user_id,
                "Не получилось отправить результат. Попробуйте немного позже.",
                (Button("← В меню", "menu"),),
            )
            return
        dialog = self.store.get(event.user_id)
        if isinstance(exc, InvalidInputError) and dialog and dialog.state == "waiting_for_source":
            text = "Не получилось прочитать фото. Отправьте другое изображение 📷"
            buttons: tuple[Button, ...] = ()
        elif isinstance(exc, InvalidInputError) and dialog and dialog.state in {
            "waiting_for_prompt", "waiting_for_correction"
        }:
            text = "Не получилось прочитать запрос. Напишите короче."
            buttons = ()
        else:
            text = "Что-то пошло не так. Попробуйте ещё раз."
            buttons = (Button("← В меню", "menu"),)
        self.transport.send_message(event.user_id, text, buttons)

    def _dispatch(self, event: MaxIncomingEvent) -> None:
        dialog = self.store.get_or_create(event.user_id, event.chat_id)
        text = (event.text or "").strip()
        if event.event_type == "bot_started" or text.lower() == "/start":
            self._start(event, dialog)
            return
        if event.event_type == "message_callback":
            if event.callback_id:
                self.transport.answer_callback(event.callback_id, "Готово")
            self._callback(event, dialog)
            return
        if event.event_type != "message_created":
            return
        if not self.store.legal_is_current(event.user_id):
            self._show_legal(event.user_id, dialog, event.event_key)
            return
        if event.image_url:
            self._receive_source(
                event,
                dialog,
                choose_scenario=dialog.state != "waiting_for_source",
            )
            return
        if dialog.state == "waiting_for_source":
            self._receive_source(event, dialog)
        elif dialog.state in {"waiting_for_prompt", "waiting_for_correction"}:
            self._receive_prompt(event, dialog)
        else:
            self._show_main(event.user_id, dialog, event.event_key)

    def _start(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        if not self.store.legal_is_current(event.user_id):
            self._show_legal(event.user_id, dialog, event.event_key)
            return
        self._show_main(event.user_id, dialog, event.event_key)

    def _send_view(self, user_id: str, view: View) -> str:
        return self.transport.send_message(user_id, view.text, view.buttons)

    def _show_legal(self, user_id: str, dialog: MaxDialog, event_key: str) -> None:
        self.store.transition(
            user_id, "legal_required", event_key=event_key, force=True,
            pending_prompt=None, pending_action=None,
        )
        self._send_view(user_id, legal_view())

    def _show_main(self, user_id: str, dialog: MaxDialog, event_key: str) -> None:
        self._reset_dialog_to_main(user_id, event_key)
        self._send_view(user_id, main_menu())

    def _reset_dialog_to_main(self, user_id: str, event_key: str) -> None:
        self.store.transition(
            user_id, "main_menu", event_key=event_key, force=True,
            selected_scenario_id=None,
            session_id=None,
            pending_prompt=None,
            pending_action=None,
            current_gallery_item_id=None,
            current_version_id=None,
            gallery_cursor=0,
            status_message_id=None,
        )

    def _callback(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        action = event.callback_payload or ""
        if action == "legal:offer":
            self.transport.send_message(
                event.user_id,
                "Условия использования\n\nСервис создаёт демо-обработку. Оплата оригинала пока недоступна.",
                (Button("← Назад", "settings"),),
            )
            return
        if action == "legal:privacy":
            self.transport.send_message(
                event.user_id,
                "Приватность\n\nФото используется для обработки и передаётся AI-провайдеру.",
                (Button("← Назад", "settings"),),
            )
            return
        if action == "legal:details":
            self._send_view(event.user_id, legal_details_view())
            return
        if action == "legal:back":
            self._send_view(event.user_id, legal_view())
            return
        if action == "legal:accept_all":
            self.store.accept_required_documents(event.user_id)
            self.adapter.record_consent(
                event.user_id,
                offer=True, personal_data=True, image_rights=True,
                external_ai=True, appearance_change=True,
            )
            self._show_main(event.user_id, dialog, event.event_key)
            return
        if action == "legal:show":
            view = settings_view() if self.store.legal_is_current(event.user_id) else legal_view()
            self._send_view(event.user_id, view)
            return
        if action == "menu":
            if self.store.legal_is_current(event.user_id):
                self._show_main(event.user_id, dialog, event.event_key)
            else:
                self._show_legal(event.user_id, dialog, event.event_key)
            return
        if action == "settings":
            if self.store.legal_is_current(event.user_id):
                self._send_view(event.user_id, settings_view())
            else:
                self._show_legal(event.user_id, dialog, event.event_key)
            return
        if not self.store.legal_is_current(event.user_id):
            self._show_legal(event.user_id, dialog, event.event_key)
            return
        if action == "custom" or action.startswith("scenario:"):
            scenario = action.split(":", 1)[1] if action.startswith("scenario:") else None
            self._begin_work(event, dialog, scenario)
        elif action == "prompt:edit":
            target = "waiting_for_correction" if dialog.pending_action == "correction" else "waiting_for_prompt"
            self.store.transition(event.user_id, target, event_key=event.event_key)
            self.transport.send_message(event.user_id, "Что изменить?")
        elif action == "prompt:cancel":
            self._show_main(event.user_id, dialog, event.event_key)
        elif action == "prompt:start":
            self._generate(event, dialog, correction=dialog.pending_action == "correction")
        elif action == "result:unlock":
            self.transport.send_message(event.user_id, UNLOCK_PLACEHOLDER)
        elif action == "result:correct":
            self.store.transition(
                event.user_id, "waiting_for_correction", event_key=event.event_key,
                pending_prompt=None, pending_action="correction",
            )
            self.transport.send_message(
                event.user_id,
                "Что исправить?",
            )
        elif action == "result:repeat":
            self._generate(event, dialog, correction=False, repeat=True)
        elif action == "result:favorite":
            self._favorite(event.user_id, dialog)
        elif action == "studio:works":
            self._show_works(event, dialog)
        elif action.startswith("works:open:"):
            self._open_work(event, dialog, action.rsplit(":", 1)[1])
        elif action == "work:open":
            if not dialog.current_gallery_item_id:
                raise InvalidInputError("No gallery work selected")
            self._open_work(event, dialog, dialog.current_gallery_item_id)
        elif action == "work:history":
            self._show_version_history(event, dialog)
        elif action in {"work:previous", "work:next"}:
            self._navigate_version(event, dialog, -1 if action.endswith("previous") else 1)
        elif action == "work:main":
            self._make_current_best(event.user_id, dialog)
        elif action == "result:delete":
            self._send_view(event.user_id, delete_confirmation_view())
        elif action == "delete:cancel":
            if dialog.current_gallery_item_id:
                self._open_work(event, dialog, dialog.current_gallery_item_id)
            else:
                self._show_main(event.user_id, dialog, event.event_key)
        elif action == "delete:confirm":
            self._delete_current(event, dialog)
        elif action == "catalog:photoshoot":
            self._send_view(event.user_id, photoshoot_catalog())

    def _begin_work(
        self, event: MaxIncomingEvent, dialog: MaxDialog, scenario_id: Optional[str]
    ) -> None:
        reusable_source = bool(
            dialog.session_id and self._session_is_usable(dialog.session_id)
        )
        target = "waiting_for_prompt" if reusable_source else "waiting_for_source"
        self.store.transition(
            event.user_id, target, event_key=event.event_key,
            selected_scenario_id=scenario_id, pending_prompt=None,
            pending_action="initial",
            session_id=dialog.session_id if reusable_source else None,
            current_gallery_item_id=(
                dialog.current_gallery_item_id if reusable_source else None
            ),
            current_version_id=None,
        )
        if target == "waiting_for_source":
            self.transport.send_message(
                event.user_id,
                "Пришлите фотографию 📷",
            )
        else:
            self.transport.send_message(event.user_id, PHOTO_ACCEPTED_TEXT)

    def _receive_source(
        self,
        event: MaxIncomingEvent,
        dialog: MaxDialog,
        *,
        choose_scenario: bool = False,
    ) -> None:
        if not event.image_url:
            self.transport.send_message(event.user_id, "Пришлите фотографию 📷")
            return
        destination = self.settings.temp_dir / f"max-{event.message_id or event.event_key}.upload"
        try:
            self.transport.download_image(
                event.image_url,
                destination,
                self.settings.max_source_file_size_mb * 1024 * 1024,
            )
            session = self.adapter.start_demo(event.user_id, destination)
        finally:
            destination.unlink(missing_ok=True)
        with self.database.read() as connection:
            item_id = connection.execute(
                "SELECT gallery_item_id FROM demo_sessions WHERE id=?", (session.session_id,)
            ).fetchone()[0]
        if choose_scenario:
            self.store.transition(
                event.user_id, "main_menu", event_key=event.event_key, force=True,
                user_id=session.user_id,
                session_id=session.session_id,
                selected_scenario_id=None,
                current_gallery_item_id=item_id,
                current_version_id=None,
                pending_prompt=None,
                pending_action=None,
                status_message_id=None,
            )
            self.transport.send_message(
                event.user_id,
                "Фото получено ✅\n\nЧто хотите сделать?",
                main_menu().buttons,
            )
        else:
            self.store.transition(
                event.user_id, "waiting_for_prompt", event_key=event.event_key,
                user_id=session.user_id, session_id=session.session_id,
                current_gallery_item_id=item_id, current_version_id=None,
                pending_action="initial",
            )
            self.transport.send_message(event.user_id, PHOTO_ACCEPTED_TEXT)

    def _session_is_usable(self, session_id: str) -> bool:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT source_file_path,status,expires_at FROM demo_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None or row["status"] != "active" or not row["source_file_path"]:
            return False
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return False
        if self.demo.clock() >= expires_at:
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE demo_sessions SET status='expired',updated_at=?
                       WHERE id=? AND status='active'""",
                    (self.demo.clock().isoformat(), session_id),
                )
            return False
        return Path(row["source_file_path"]).is_file()

    def _remaining(self, session_id: str) -> int:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT successful_generations,max_generations FROM demo_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise InvalidInputError("Demo session is missing")
        return max(0, row["max_generations"] - row["successful_generations"])

    def _receive_prompt(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        prompt = (event.text or "").strip()
        if not prompt:
            self.transport.send_message(event.user_id, "Что изменить?")
            return
        if len(prompt) > self.settings.max_prompt_length:
            raise InvalidInputError("Prompt is too long")
        if not dialog.session_id or not self._session_is_usable(dialog.session_id):
            raise DemoExpiredError("The current source image is no longer available")
        mode = "correction" if dialog.state == "waiting_for_correction" else "initial"
        updated = self.store.transition(
            event.user_id, "confirmation", event_key=event.event_key,
            pending_prompt=prompt, pending_action=mode,
        )
        self._generate(event, updated, correction=mode == "correction")

    def _generate(
        self,
        event: MaxIncomingEvent,
        dialog: MaxDialog,
        *,
        correction: bool,
        repeat: bool = False,
    ) -> None:
        dialog = self.store.get(event.user_id) or dialog
        if not dialog.session_id or not self._session_is_usable(dialog.session_id):
            raise DemoExpiredError("The current source image is no longer available")
        if repeat and not dialog.current_version_id:
            raise InvalidInputError("Repeat requires an existing version")
        prompt = dialog.pending_prompt or ("Другой вариант" if repeat else "")
        if not prompt:
            raise InvalidInputError("Prompt confirmation is missing")
        available = self._remaining(dialog.session_id)
        if available == 0:
            self.store.transition(
                event.user_id, "demo_exhausted", event_key=event.event_key, force=True
            )
            self._send_view(event.user_id, result_actions(0))
            return
        remaining_after = available - 1
        status_id = self.transport.send_message(event.user_id, PROCESSING_TEXT)
        self.store.transition(
            event.user_id, "processing", event_key=event.event_key,
            status_message_id=status_id,
        )

        def deliver(preview: Path, _attempt_id: str) -> bool:
            caption = result_actions(remaining_after).text
            return self.transport.send_image(
                event.user_id, preview, caption, result_actions(remaining_after).buttons
            )

        try:
            result = self.adapter.generate(
                event.user_id,
                dialog.session_id,
                prompt,
                event.event_key,
                scenario_id=dialog.selected_scenario_id,
                correction=correction,
                parent_version_id=dialog.current_version_id if (correction or repeat) else None,
                delivery_override=deliver,
            )
        except Exception:
            recovery_state = "result_ready" if dialog.current_version_id else "confirmation"
            self.store.transition(
                event.user_id, recovery_state, event_key=event.event_key,
                status_message_id=None, force=True,
            )
            try:
                self.transport.edit_message(status_id, "Не получилось завершить обработку.")
            except MaxTransportError:
                LOGGER.info("MAX status message could not be edited after failed generation")
            raise
        with self.database.read() as connection:
            version = connection.execute(
                "SELECT id,gallery_item_id FROM gallery_versions WHERE attempt_id=?",
                (result.attempt_id,),
            ).fetchone()
        next_state = "demo_exhausted" if result.remaining_generations == 0 else "result_ready"
        self.store.transition(
            event.user_id, next_state, event_key=event.event_key,
            current_gallery_item_id=version["gallery_item_id"],
            current_version_id=version["id"], status_message_id=None,
            pending_prompt=None, pending_action=None,
        )
        try:
            self.transport.edit_message(status_id, "✨ Готово")
        except MaxTransportError:
            LOGGER.info("MAX status message could not be edited after successful delivery")

    def _favorite(self, platform_user_id: str, dialog: MaxDialog) -> None:
        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No current version")
        self.gallery.set_version_favorite(dialog.user_id, dialog.current_version_id, True)
        self.transport.send_message(platform_user_id, "Добавлено в избранное ⭐")

    def _show_works(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        if not dialog.user_id:
            self.transport.send_message(
                event.user_id,
                "Здесь пока пусто.\n\nСоздайте первую фотографию.",
                (Button("← В меню", "menu"),),
            )
            return
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT id,title,scenario_id,generation_count,favorite,created_at
                   FROM gallery_items WHERE user_id=? AND deleted=0
                   ORDER BY updated_at DESC LIMIT 10""",
                (dialog.user_id,),
            ).fetchall()
        self.store.transition(event.user_id, "gallery", event_key=event.event_key, force=True)
        if not rows:
            self.transport.send_message(
                event.user_id,
                "Здесь пока пусто.\n\nСоздайте первую фотографию.",
                (Button("← В меню", "menu"),),
            )
            return
        lines = ["📂 Мои работы", "Выберите работу."]
        buttons: list[Button] = []
        for index, row in enumerate(rows, start=1):
            favorite = " ⭐" if row["favorite"] else ""
            count = row["generation_count"]
            version_word = "версия" if count == 1 else "версии"
            buttons.append(
                Button(
                    f"{index}. {row['title'][:28]}{favorite} · {count} {version_word}",
                    f"works:open:{row['id']}",
                )
            )
        buttons.append(Button("← В меню", "menu"))
        self.transport.send_message(event.user_id, "\n\n".join(lines), tuple(buttons))

    def _open_work(
        self, event: MaxIncomingEvent, dialog: MaxDialog, item_id: str
    ) -> None:
        if not dialog.user_id:
            raise InvalidInputError("Gallery owner is missing")
        item, best = self.gallery.open_item(dialog.user_id, item_id)
        versions = self.gallery.list_versions(dialog.user_id, item_id)
        if not best and versions:
            best = versions[-1]
        self.store.transition(
            event.user_id, "gallery", event_key=event.event_key, force=True,
            current_gallery_item_id=item.id,
            current_version_id=best.id if best else None,
        )
        self._send_work(event.user_id, item.title, item.favorite, versions, best)

    def _send_work(
        self,
        platform_user_id: str,
        title: str,
        favorite: bool,
        versions: list[GalleryVersion],
        current: Optional[GalleryVersion],
        *,
        history: bool = False,
    ) -> None:
        if current is None or current.preview_path is None:
            self.transport.send_message(platform_user_id, "Результат ещё не готов.")
            return
        heading = "История версий" if history else title
        caption = f"{heading}{' ⭐' if favorite or current.favorite else ''}\nВерсия {current.version_number} из {len(versions)}"
        buttons = version_history_actions() if history else gallery_item_actions()
        if not self.transport.send_image(
            platform_user_id, current.preview_path, caption, buttons
        ):
            raise MaxTransportError("MAX gallery preview delivery failed")

    def _show_version_history(
        self, event: MaxIncomingEvent, dialog: MaxDialog
    ) -> None:
        if not dialog.user_id or not dialog.current_gallery_item_id:
            raise InvalidInputError("No gallery work selected")
        item, best = self.gallery.open_item(dialog.user_id, dialog.current_gallery_item_id)
        versions = self.gallery.list_versions(dialog.user_id, item.id)
        current = next(
            (version for version in versions if version.id == dialog.current_version_id),
            best or (versions[-1] if versions else None),
        )
        self._send_work(
            event.user_id, item.title, item.favorite, versions, current, history=True
        )

    def _navigate_version(
        self, event: MaxIncomingEvent, dialog: MaxDialog, direction: int
    ) -> None:
        if not dialog.user_id or not dialog.current_gallery_item_id:
            raise InvalidInputError("No gallery work selected")
        item, _best = self.gallery.open_item(dialog.user_id, dialog.current_gallery_item_id)
        versions = self.gallery.list_versions(dialog.user_id, item.id)
        if not versions:
            raise InvalidInputError("Work has no versions")
        current_index = next(
            (index for index, version in enumerate(versions) if version.id == dialog.current_version_id),
            len(versions) - 1,
        )
        selected = versions[(current_index + direction) % len(versions)]
        self.store.update(event.user_id, current_version_id=selected.id)
        self._send_work(
            event.user_id, item.title, item.favorite, versions, selected, history=True
        )

    def _make_current_best(self, platform_user_id: str, dialog: MaxDialog) -> None:
        if not dialog.user_id or not dialog.current_gallery_item_id or not dialog.current_version_id:
            raise InvalidInputError("No current gallery version")
        self.gallery.set_current_best(
            dialog.user_id, dialog.current_gallery_item_id, dialog.current_version_id
        )
        self.transport.send_message(platform_user_id, "Выбрано как основное.")

    def _delete_current(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        if not dialog.user_id or not dialog.current_gallery_item_id:
            raise InvalidInputError("No current gallery work")
        # Ownership is verified before the irreversible purge.
        self.gallery.open_item(dialog.user_id, dialog.current_gallery_item_id)
        self.gallery.soft_delete(dialog.user_id, dialog.current_gallery_item_id)
        self.gallery.purge_item(dialog.current_gallery_item_id)
        self.store.transition(
            event.user_id, "deleted", event_key=event.event_key, force=True,
            session_id=None, current_gallery_item_id=None, current_version_id=None,
            pending_prompt=None, pending_action=None, status_message_id=None,
        )
        self.transport.send_message(
            event.user_id,
            "Работа удалена.",
            (Button("← В меню", "menu"),),
        )
