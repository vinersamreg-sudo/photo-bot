"""Application controller connecting normalized MAX events to domain services."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol, Sequence
from uuid import uuid4

from app.config import Settings
from app.database import Database
from app.demo_service import DemoService
from app.domain import (
    AssetUnavailableError,
    CooldownError,
    DailyBudgetError,
    DeliveryError,
    DemoError,
    DemoExpiredError,
    DemoLimitError,
    ImageTooLargeError,
    InvalidInputError,
    IntentAmbiguityError,
    PaymentRequiredError,
    PolicyRejectedError,
    ProviderQuotaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    SegmentationFailedError,
    SourceReplacementError,
    StorageFailureError,
)
from app.gallery import GalleryService, GalleryVersion
from app.edit_intent import parse_edit_intent
from app.max_adapter import (
    Button,
    MaxDemoAdapter,
    View,
    delete_confirmation_view,
    delivered_actions,
    gallery_item_actions,
    gallery_more_actions,
    ideas_catalog,
    legal_details_view,
    legal_view,
    paid_actions,
    photoshoot_catalog,
    result_actions,
    retry_delivery_actions,
    scenario_catalog,
    settings_view,
    upload_view,
    version_history_actions,
)
from app.max_conversation import MaxConversationStore, MaxDialog
from app.max_transport import MaxIncomingEvent, MaxTransportError
from app.telemetry import TelemetryRecorder
from app.payments import (
    PaymentError,
    PaymentService,
    PaymentStatus,
    PaymentUnavailable,
    build_payment_service,
)


LOGGER = logging.getLogger(__name__)

PHOTO_ACCEPTED_TEXT = (
    "Что хотите изменить?"
)
PHOTO_REUSED_TEXT = (
    "Что хотите изменить?"
)
PROCESSING_TEXT = "Обрабатываю фотографию…"
UNLOCK_PLACEHOLDER = (
    "Получение оригинала пока недоступно — идёт закрытое тестирование.\n\n"
    "Работа сохранена в «Моих работах»."
)
OWNER_ONLY_TEXT = (
    "Ravuna пока в закрытом тестировании.\n\n"
    "Скоро откроем доступ."
)
CORRECTION_REQUEST_TEXT = (
    "Напишите одним сообщением, что нужно изменить в этой фотографии.\n\n"
    "Например:\n"
    "• сделать фон светлее;\n"
    "• убрать лишний предмет;\n"
    "• изменить цвет одежды;\n"
    "• сохранить лицо без изменений."
)


def _version_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "версия"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return "версии"
    return "версий"


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
    ) -> Optional[str]: ...
    def send_file(
        self, platform_user_id: str, file_path: Path, caption: str,
        buttons: Sequence[Button],
    ) -> Optional[str]: ...


class MaxApplication:
    """Minimal restart-safe MAX vertical slice; no payment implementation."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        demo_service: DemoService,
        transport: LiveMaxTransport,
        store: Optional[MaxConversationStore] = None,
        payment_service: Optional[PaymentService] = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.demo = demo_service
        self.transport = transport
        self.store = store or MaxConversationStore(database)
        self.adapter = MaxDemoAdapter(demo_service, database, transport)
        self.gallery: GalleryService = demo_service.gallery
        self.telemetry = TelemetryRecorder(database)
        self.payments = payment_service or build_payment_service(settings, database)
        self._checkout_lock = threading.RLock()

    def _track(self, event_type: str, **values: object) -> None:
        try:
            self.telemetry.record(event_type, **values)
        except Exception as exc:
            LOGGER.warning(
                "Product telemetry write skipped (event_type=%s,error_type=%s)",
                event_type,
                type(exc).__name__,
            )

    def recover_interrupted_processing(self) -> int:
        """Close stale MAX status messages after a process restart."""

        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT platform_user_id,status_message_id,current_version_id
                   FROM max_dialogs WHERE state='processing'"""
            ).fetchall()
        for row in rows:
            text = "Обработка прервалась. Попытка не списана — попробуйте ещё раз."
            try:
                if row["status_message_id"]:
                    try:
                        self.transport.edit_message(row["status_message_id"], text)
                    except MaxTransportError:
                        self._send_message(row["platform_user_id"], text)
                else:
                    self._send_message(row["platform_user_id"], text)
            except MaxTransportError:
                LOGGER.warning(
                    "Interrupted-processing notice deferred because MAX is unavailable"
                )
            self.store.transition(
                row["platform_user_id"],
                "result_ready" if row["current_version_id"] else "waiting_for_prompt",
                event_key="ops:restart-recovery",
                force=True,
                status_message_id=None,
                pending_prompt=None,
            )
        return len(rows)

    def handle(self, event: MaxIncomingEvent) -> bool:
        """Handle one deduplicated event. Returns false for a known duplicate."""

        if not self.store.begin_event(event.event_key, event.event_type):
            self._deactivate_active_keyboards(event)
            return False
        try:
            if (
                not self.settings.max_public_access_enabled
                and event.user_id not in self.settings.max_allowed_user_ids
            ):
                if event.callback_id:
                    self.transport.answer_callback(
                        event.callback_id, "Сервис находится в закрытом тестировании"
                    )
                self._send_message(event.user_id, OWNER_ONLY_TEXT)
                self.store.finish_event(event.event_key, True)
                return True
            self._deactivate_active_keyboards(event)
            self._dispatch(event)
        except MaxTransportError:
            self.store.finish_event(event.event_key, False)
            raise
        except sqlite3.Error as exc:
            LOGGER.error(
                "MAX database operation failed safely (error_type=%s)",
                type(exc).__name__,
            )
            try:
                self._send_message(
                    event.user_id,
                    "Сервис временно недоступен. Попытка не списана.",
                )
            except MaxTransportError:
                pass
            raise
        except DemoExpiredError:
            LOGGER.info("MAX demo session expired (event_type=%s)", event.event_type)
            self._reset_dialog_to_main(event.user_id, event.event_key)
            self._send_message(
                event.user_id,
                "Сессия завершилась.\n\nОтправьте фотографию снова.",
                upload_view().buttons,
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
        except Exception as exc:
            LOGGER.error(
                "MAX application failure closed (event_type=%s,error_type=%s)",
                event.event_type,
                type(exc).__name__,
            )
            dialog = self.store.get(event.user_id)
            if dialog and dialog.status_message_id:
                self._send_error(
                    event.user_id, "Сервис временно недоступен. Попытка не списана."
                )
                self.store.transition(
                    event.user_id,
                    "result_ready" if dialog.current_version_id else "waiting_for_prompt",
                    event_key=event.event_key,
                    force=True,
                    status_message_id=None,
                    pending_prompt=None,
                )
                self.store.finish_event(event.event_key, True)
                return True
            self.store.finish_event(event.event_key, False)
            raise
        self.store.finish_event(event.event_key, True)
        return True

    def _show_demo_error(self, event: MaxIncomingEvent, exc: DemoError) -> None:
        dialog = self.store.get(event.user_id)
        self._track(
            "error",
            session_id=dialog.session_id if dialog else None,
            gallery_item_id=dialog.current_gallery_item_id if dialog else None,
            error_type=type(exc).__name__,
        )
        if isinstance(exc, AssetUnavailableError):
            self._send_error(
                event.user_id, "Не удалось выполнить обработку. Измените описание."
            )
            return
        if isinstance(exc, SegmentationFailedError):
            self._send_error(
                event.user_id, "Не удалось выполнить обработку. Попробуйте другое фото."
            )
            return
        if isinstance(exc, SourceReplacementError):
            self._send_error(
                event.user_id,
                "Бесплатное демо уже связано с первой фотографией.\n\n"
                "Продолжите с ней или откройте «Мои работы».",
                (
                    Button("Продолжить с фото", "custom"),
                    Button("📂 Мои работы", "studio:works"),
                ),
            )
            return
        if isinstance(exc, IntentAmbiguityError):
            self._show_intent_ambiguity(event.user_id, exc)
            return
        if isinstance(exc, DemoLimitError):
            self._send_error(
                event.user_id,
                "Бесплатные обработки закончились.",
                (
                    Button("⬇ Получить оригинал", "result:unlock"),
                    Button("Пакет доступа Ravuna — 49 ₽", "package:buy"),
                    Button("📂 Мои работы", "studio:works"),
                ),
            )
            return
        if isinstance(exc, CooldownError):
            self._send_error(
                event.user_id, "Слишком быстро. Попробуйте ещё раз через минуту."
            )
            return
        if isinstance(exc, DailyBudgetError):
            self._send_error(
                event.user_id,
                "Сегодня бесплатная обработка временно недоступна. Попробуйте позже.",
            )
            return
        if isinstance(exc, PolicyRejectedError):
            self._send_error(
                event.user_id,
                "Это изображение или запрос нельзя обработать. Попробуйте изменить описание.",
            )
            return
        if isinstance(exc, DeliveryError):
            self._send_error(
                event.user_id,
                "Изображение создано, но не удалось отправить его в MAX. Попытка не списана.",
            )
            return
        if isinstance(exc, ProviderTimeoutError):
            self._send_error(
                event.user_id,
                "Обработка заняла слишком много времени. Попытка не списана — попробуйте ещё раз.",
            )
            return
        if isinstance(exc, (ProviderUnavailableError, ProviderQuotaError, StorageFailureError)):
            self._send_error(
                event.user_id, "Сервис временно недоступен. Попытка не списана."
            )
            return
        if isinstance(exc, ImageTooLargeError):
            self._send_error(
                event.user_id,
                f"Файл слишком большой. Отправьте изображение до {self.settings.max_source_file_size_mb} МБ.",
            )
            return
        if isinstance(exc, InvalidInputError) and dialog and dialog.state == "waiting_for_source":
            text = "Не удалось открыть изображение. Отправьте JPG, PNG или WEBP."
            buttons: tuple[Button, ...] = ()
        elif isinstance(exc, InvalidInputError) and dialog and dialog.state in {
            "waiting_for_prompt", "waiting_for_correction"
        }:
            text = "Не получилось прочитать запрос. Напишите короче."
            buttons = ()
        else:
            text = "Что-то пошло не так. Попробуйте ещё раз."
            buttons = (Button("← В меню", "menu"),)
        self._send_error(event.user_id, text, buttons)

    def _send_error(
        self, user_id: str, text: str, buttons: Sequence[Button] = ()
    ) -> None:
        """Finish a processing status in place; send one fallback if editing fails."""

        dialog = self.store.get(user_id)
        status_id = dialog.status_message_id if dialog else None
        if status_id:
            try:
                self._edit_message(user_id, status_id, text, buttons)
            except MaxTransportError:
                LOGGER.info("MAX processing status edit failed; using one fallback message")
                self._send_message(user_id, text, buttons)
            finally:
                self.store.update(user_id, status_message_id=None)
            return
        self._send_message(user_id, text, buttons)

    def _dispatch(self, event: MaxIncomingEvent) -> None:
        dialog = self.store.get_or_create(event.user_id, event.chat_id)
        text = (event.text or "").strip()
        if event.event_type == "bot_started" or text.lower() == "/start":
            if event.image_url:
                self._reset_dialog_to_main(event.user_id, event.event_key)
                self._receive_source(
                    event, self.store.get(event.user_id) or dialog
                )
                return
            self._start(event, dialog)
            return
        if event.event_type == "message_callback":
            if event.callback_id:
                notifications = {
                    "prompt:edit": "Напишите изменение",
                    "result:correct": "Напишите изменение",
                }
                notification = notifications.get(event.callback_payload, "Готово")
                self.transport.answer_callback(event.callback_id, notification)
            self._callback(event, dialog)
            return
        if event.event_type != "message_created":
            return
        if event.image_url:
            self._receive_source(event, dialog)
            return
        if dialog.state == "waiting_for_source":
            if text:
                self.store.transition(
                    event.user_id,
                    "waiting_for_source",
                    event_key=event.event_key,
                    pending_prompt=text,
                    pending_action="initial",
                )
                self._send_message(
                    event.user_id,
                    "Теперь прикрепите фотографию через скрепку 📎.",
                )
            else:
                self._send_message(
                    event.user_id,
                    "Прикрепите фотографию через скрепку 📎.",
                )
        elif dialog.state in {"waiting_for_prompt", "waiting_for_correction"}:
            self._receive_prompt(event, dialog)
        elif dialog.state == "result_ready" and text:
            correction_dialog = self.store.transition(
                event.user_id, "waiting_for_correction", event_key=event.event_key,
                pending_prompt=None, pending_action="correction",
            )
            self._receive_prompt(event, correction_dialog)
        else:
            self._show_main(event.user_id, dialog, event.event_key)

    def _start(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        self._track("start", session_id=dialog.session_id)
        self._show_main(event.user_id, dialog, event.event_key)

    def _send_view(self, user_id: str, view: View) -> str:
        return self._send_message(user_id, view.text, view.buttons)

    def _send_message(
        self, user_id: str, text: str, buttons: Sequence[Button] = (), **kwargs
    ) -> str:
        message_id = self.transport.send_message(user_id, text, buttons, **kwargs)
        if buttons:
            self.store.register_keyboard(user_id, message_id, text)
        return message_id

    def _send_image(
        self,
        user_id: str,
        image: Path,
        caption: str,
        buttons: Sequence[Button],
    ) -> Optional[str]:
        message_id = self.transport.send_image(user_id, image, caption, buttons)
        if message_id and buttons:
            self.store.register_keyboard(user_id, message_id, caption)
        return message_id

    def _send_file(
        self,
        user_id: str,
        file_path: Path,
        caption: str,
        buttons: Sequence[Button],
    ) -> Optional[str]:
        message_id = self.transport.send_file(user_id, file_path, caption, buttons)
        if message_id and buttons:
            self.store.register_keyboard(user_id, message_id, caption)
        return message_id

    def _send_selected_preview(
        self,
        platform_user_id: str,
        dialog: MaxDialog,
        caption: str,
    ) -> str:
        """Send the exact selected version without silently switching lineage."""

        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No gallery version selected")
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT v.preview_watermarked_path AS preview_path
                   FROM gallery_versions AS v
                   JOIN gallery_items AS i ON i.id=v.gallery_item_id
                   WHERE v.id=? AND i.user_id=? AND i.deleted=0
                         AND v.status='succeeded'""",
                (dialog.current_version_id, dialog.user_id),
            ).fetchone()
        preview = Path(row["preview_path"]) if row and row["preview_path"] else None
        if preview is None or not preview.is_file():
            raise AssetUnavailableError("Selected preview is not available")
        message_id = self._send_image(platform_user_id, preview, caption, ())
        if not message_id:
            raise MaxTransportError("MAX selected preview delivery failed")
        return message_id

    def _edit_message(
        self,
        user_id: str,
        message_id: str,
        text: str,
        buttons: Sequence[Button] = (),
    ) -> None:
        self.transport.edit_message(message_id, text, buttons)
        if buttons:
            self.store.register_keyboard(user_id, message_id, text)
        else:
            self.store.clear_keyboard(user_id, message_id)

    def _deactivate_active_keyboards(self, event: MaxIncomingEvent) -> None:
        """Remove every keyboard left by earlier dialog states."""

        active = self.store.active_keyboards(event.user_id)
        active_ids = {message_id for message_id, _ in active}
        for message_id, message_text in active:
            try:
                self.transport.edit_message(message_id, message_text, ())
            except MaxTransportError:
                LOGGER.info(
                    "MAX stale keyboard could not be deactivated "
                    "(message_id_present=true)"
                )
            else:
                self.store.clear_keyboard(event.user_id, message_id)
        if (
            event.event_type == "message_callback"
            and event.message_id
            and event.message_id not in active_ids
        ):
            self._deactivate_callback_keyboard(event)

    def _deactivate_callback_keyboard(self, event: MaxIncomingEvent) -> None:
        """Make the keyboard that triggered a state transition single-use."""

        if not event.message_id:
            return
        try:
            self.transport.edit_message(
                event.message_id,
                event.text or "Действие выбрано ✅",
                (),
            )
        except MaxTransportError:
            LOGGER.info(
                "MAX source keyboard could not be deactivated "
                "(action=%s,message_id_present=true)",
                event.callback_payload or "unknown",
            )

    def _show_legal(self, user_id: str, dialog: MaxDialog, event_key: str) -> None:
        self._send_view(user_id, legal_view())

    def _show_main(self, user_id: str, dialog: MaxDialog, event_key: str) -> None:
        self._reset_dialog_to_main(user_id, event_key)
        self._send_view(user_id, upload_view())

    def _reset_dialog_to_main(self, user_id: str, event_key: str) -> None:
        self.store.transition(
            user_id, "waiting_for_source", event_key=event_key, force=True,
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
        if action in {"start:details", "legal:details"}:
            self._send_view(event.user_id, legal_details_view())
            return
        if action == "legal:offer":
            self._send_message(
                event.user_id,
                "Условия использования\n\n"
                "Сервис создаёт демо-обработку. Пакет доступа Ravuna включает "
                "две обработки и один оригинал. Оплата пока недоступна.",
                (Button("← Назад", "settings"),),
            )
            return
        if action == "legal:privacy":
            self._send_message(
                event.user_id,
                "Приватность\n\nФото используется для обработки и передаётся AI-провайдеру.",
                (Button("← Назад", "settings"),),
            )
            return
        if action == "legal:back":
            self._show_main(event.user_id, dialog, event.event_key)
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
            self._send_view(event.user_id, settings_view())
            return
        if action == "menu":
            self._show_main(event.user_id, dialog, event.event_key)
            return
        if action == "new:source":
            self._show_main(event.user_id, dialog, event.event_key)
            return
        if action == "settings":
            self._send_view(event.user_id, settings_view())
            return
        if action == "catalog:ideas":
            self._send_view(event.user_id, ideas_catalog())
            return
        if action.startswith("ideas:"):
            self._send_view(
                event.user_id, scenario_catalog(action.split(":", 1)[1])
            )
            return
        if action == "custom" or action.startswith("scenario:"):
            scenario = action.split(":", 1)[1] if action.startswith("scenario:") else None
            self._begin_work(event, dialog, scenario)
        elif action == "prompt:edit":
            target = "waiting_for_correction" if dialog.pending_action == "correction" else "waiting_for_prompt"
            self.store.transition(event.user_id, target, event_key=event.event_key)
        elif action == "prompt:cancel":
            self._show_main(event.user_id, dialog, event.event_key)
        elif action == "prompt:start":
            self._generate(event, dialog, correction=dialog.pending_action == "correction")
        elif action == "result:unlock":
            self._track(
                "unlock_clicked",
                session_id=dialog.session_id,
                gallery_item_id=dialog.current_gallery_item_id,
            )
            self._unlock_or_deliver(event, dialog)
        elif action == "package:buy":
            self._buy_continuation_pack(event, dialog)
        elif action == "package:offer":
            self._show_continuation_pack_offer(event, dialog)
        elif action == "result:correct":
            if not dialog.session_id or self._remaining(dialog.session_id) <= 0:
                self._show_continuation_pack_offer(event, dialog)
                return
            self._track(
                "correction_started",
                session_id=dialog.session_id,
                gallery_item_id=dialog.current_gallery_item_id,
            )
            self.store.transition(
                event.user_id, "waiting_for_correction", event_key=event.event_key,
                pending_prompt=None, pending_action="correction",
            )
            self._send_selected_preview(
                event.user_id, dialog, "Текущая версия"
            )
            self._send_message(event.user_id, CORRECTION_REQUEST_TEXT)
        elif action == "result:repeat":
            if not dialog.session_id or self._remaining(dialog.session_id) <= 0:
                self._show_continuation_pack_offer(event, dialog)
                return
            self._track(
                "repeat_started",
                session_id=dialog.session_id,
                gallery_item_id=dialog.current_gallery_item_id,
            )
            self._generate(event, dialog, correction=False, repeat=True)
        elif action == "result:favorite":
            self._favorite(event.user_id, dialog)
        elif action == "result:feedback:positive":
            self._record_feedback(event.user_id, dialog, "positive")
        elif action == "result:feedback:negative":
            self._record_feedback(event.user_id, dialog, "negative")
        elif action == "clarify:preserve-background":
            prompt = (
                (dialog.pending_prompt or "")
                + ". Итоговое решение: сохранить текущий фон и только улучшить его."
            )
            updated = self.store.transition(
                event.user_id, "confirmation", event_key=event.event_key, force=True,
                pending_prompt=prompt,
            )
            self._generate(
                event, updated, correction=updated.pending_action == "correction"
            )
        elif action == "clarify:replace-background":
            prompt = (
                (dialog.pending_prompt or "")
                + ". Итоговое решение: заменить текущий фон."
            )
            updated = self.store.transition(
                event.user_id, "confirmation", event_key=event.event_key, force=True,
                pending_prompt=prompt,
            )
            self._generate(
                event, updated, correction=updated.pending_action == "correction"
            )
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
        elif action == "work:more":
            self._send_view(event.user_id, gallery_more_actions())
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
        elif action == "delete:restore":
            self._restore_current(event, dialog)
        elif action == "catalog:photoshoot":
            self._send_view(event.user_id, photoshoot_catalog())

    def _begin_work(
        self, event: MaxIncomingEvent, dialog: MaxDialog, scenario_id: Optional[str]
    ) -> None:
        session_id = dialog.session_id if (
            dialog.session_id and self._session_is_usable(dialog.session_id)
        ) else None
        user_id = dialog.user_id
        item_id = dialog.current_gallery_item_id
        if session_id is None and self.store.legal_is_current(event.user_id):
            stored = self.adapter.resume_demo(event.user_id)
            if stored is not None:
                session_id = stored.session_id
                user_id = stored.user_id
                with self.database.read() as connection:
                    item_id = connection.execute(
                        "SELECT gallery_item_id FROM demo_sessions WHERE id=?",
                        (stored.session_id,),
                    ).fetchone()[0]
        reusable_source = session_id is not None
        if reusable_source and scenario_id:
            updated = self.store.transition(
                event.user_id, "confirmation", event_key=event.event_key, force=True,
                user_id=user_id,
                selected_scenario_id=scenario_id,
                pending_prompt="Применить выбранный сценарий",
                pending_action="initial",
                session_id=session_id,
                current_gallery_item_id=item_id,
                current_version_id=None,
            )
            self._generate(event, updated, correction=False)
            return
        target = "waiting_for_prompt" if reusable_source else "waiting_for_source"
        self.store.transition(
            event.user_id, target, event_key=event.event_key, force=True,
            user_id=user_id,
            selected_scenario_id=scenario_id, pending_prompt=None,
            pending_action="initial",
            session_id=session_id,
            current_gallery_item_id=item_id if reusable_source else None,
            current_version_id=None,
        )
        if target == "waiting_for_source":
            self._send_message(
                event.user_id,
                "Пришлите фотографию 📷",
            )
        else:
            self._send_message(event.user_id, PHOTO_REUSED_TEXT)

    def _receive_source(
        self,
        event: MaxIncomingEvent,
        dialog: MaxDialog,
    ) -> None:
        if not event.image_url:
            self._send_message(event.user_id, "Пришлите фотографию 📷")
            return
        destination = self.settings.temp_dir / f"max-{event.message_id or event.event_key}.upload"
        try:
            try:
                self.transport.download_image(
                    event.image_url,
                    destination,
                    self.settings.max_source_file_size_mb * 1024 * 1024,
                )
            except MaxTransportError as exc:
                if exc.kind == "media_too_large":
                    raise ImageTooLargeError("Source image exceeds the allowed limit") from exc
                raise
            try:
                session = self.adapter.start_demo_with_implicit_consent(
                    event.user_id, destination
                )
            except OSError as exc:
                raise StorageFailureError("Source image storage failed") from exc
        finally:
            destination.unlink(missing_ok=True)
        with self.database.read() as connection:
            item_id = connection.execute(
                "SELECT gallery_item_id FROM demo_sessions WHERE id=?", (session.session_id,)
            ).fetchone()[0]
        self._track(
            "photo_uploaded",
            session_id=session.session_id,
            gallery_item_id=item_id,
        )
        # An image sent from the exhausted result screen starts a fresh work.
        # It must not inherit and auto-run the scenario that produced the last
        # result; the user may upload a new source even with a zero balance.
        selected_scenario_id = (
            dialog.selected_scenario_id
            if dialog.state != "demo_exhausted"
            else None
        )
        if selected_scenario_id:
            updated = self.store.transition(
                event.user_id, "confirmation", event_key=event.event_key, force=True,
                user_id=session.user_id,
                session_id=session.session_id,
                selected_scenario_id=selected_scenario_id,
                current_gallery_item_id=item_id,
                current_version_id=None,
                pending_prompt="Применить выбранный сценарий",
                pending_action="initial",
                status_message_id=None,
            )
            self._generate(event, updated, correction=False)
        else:
            updated = self.store.transition(
                event.user_id, "waiting_for_prompt", event_key=event.event_key,
                force=True,
                user_id=session.user_id, session_id=session.session_id,
                current_gallery_item_id=item_id, current_version_id=None,
                selected_scenario_id=None, pending_action="initial",
            )
            inline_prompt = (event.text or "").strip()
            if inline_prompt.lower() == "/start":
                inline_prompt = ""
            prompt = inline_prompt or (dialog.pending_prompt or "").strip()
            if prompt:
                prompt_event = event
                if prompt != inline_prompt:
                    prompt_event = MaxIncomingEvent(
                        event.event_type,
                        event.event_key,
                        event.user_id,
                        event.chat_id,
                        event.timestamp_ms,
                        message_id=event.message_id,
                        text=prompt,
                        image_url=event.image_url,
                        callback_id=event.callback_id,
                        callback_payload=event.callback_payload,
                    )
                self._receive_prompt(prompt_event, updated)
            else:
                self._send_message(event.user_id, PHOTO_ACCEPTED_TEXT)

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
                "SELECT user_id FROM demo_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise InvalidInputError("Demo session is missing")
        return self.demo.commerce.balance(row["user_id"]).available

    def _receive_prompt(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        prompt = (event.text or "").strip()
        if not prompt:
            self._send_message(event.user_id, "Что изменить?")
            return
        if len(prompt) > self.settings.max_prompt_length:
            raise InvalidInputError("Prompt is too long")
        if not dialog.session_id or not self._session_is_usable(dialog.session_id):
            raise DemoExpiredError("The current source image is no longer available")
        mode = "correction" if dialog.state == "waiting_for_correction" else "initial"
        preflight = parse_edit_intent(
            prompt,
            mode="correction" if mode == "correction" else (
                "scenario" if dialog.selected_scenario_id else "initial_edit"
            ),
            scenario_id=dialog.selected_scenario_id,
            correction_target_version_id=(
                dialog.current_version_id if mode == "correction" else None
            ),
        )
        self._track(
            "prompt_submitted",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
            parser_fallback=preflight.primary_action == "custom",
        )
        updated = self.store.transition(
            event.user_id, "confirmation", event_key=event.event_key,
            pending_prompt=prompt, pending_action=mode,
        )
        if preflight.unresolved_ambiguities:
            self._show_intent_ambiguity(
                event.user_id,
                IntentAmbiguityError(preflight.unresolved_ambiguities),
            )
            return
        self._generate(event, updated, correction=mode == "correction")

    def _show_intent_ambiguity(
        self, platform_user_id: str, _exc: IntentAmbiguityError
    ) -> None:
        self._send_message(
            platform_user_id,
            "Оставить текущий фон и только улучшить его?",
            (
                Button("Да, только улучшить", "clarify:preserve-background"),
                Button("Нет, заменить фон", "clarify:replace-background"),
            ),
        )

    def _record_feedback(
        self, _platform_user_id: str, dialog: MaxDialog, sentiment: str
    ) -> None:
        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No current version for feedback")
        self.gallery.record_feedback(
            dialog.user_id, dialog.current_version_id, sentiment
        )
        self._track(
            f"feedback_{sentiment}",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )

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
            self._track(
                "demo_quota_exhausted",
                session_id=dialog.session_id,
                gallery_item_id=dialog.current_gallery_item_id,
            )
            self._show_continuation_pack_offer(event, dialog)
            return
        remaining_after = available - 1
        status_id = self._send_message(event.user_id, PROCESSING_TEXT)
        self.store.transition(
            event.user_id, "processing", event_key=event.event_key,
            status_message_id=status_id,
        )
        self._track(
            "processing_started",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )

        def deliver(preview: Path, _attempt_id: str) -> bool:
            caption = result_actions(remaining_after).text
            return bool(self._send_image(
                event.user_id, preview, caption, result_actions(remaining_after).buttons
            ))

        try:
            result = self.adapter.generate(
                event.user_id,
                dialog.session_id,
                prompt,
                event.event_key,
                scenario_id=dialog.selected_scenario_id,
                correction=correction,
                repeat=repeat,
                parent_version_id=dialog.current_version_id if (correction or repeat) else None,
                delivery_override=deliver,
            )
        except Exception:
            recovery_state = (
                "result_ready" if dialog.current_version_id else "waiting_for_prompt"
            )
            self.store.transition(
                event.user_id, recovery_state, event_key=event.event_key,
                status_message_id=status_id, pending_prompt=None, force=True,
            )
            raise
        with self.database.read() as connection:
            version = connection.execute(
                "SELECT id,gallery_item_id FROM gallery_versions WHERE attempt_id=?",
                (result.attempt_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT duration_ms,estimated_cost FROM generation_attempts WHERE id=?",
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
        self._track(
            "result_delivered",
            session_id=dialog.session_id,
            attempt_id=result.attempt_id,
            gallery_item_id=version["gallery_item_id"],
            duration_ms=attempt["duration_ms"] if attempt else None,
            estimated_cost=attempt["estimated_cost"] if attempt else None,
        )

    def _favorite(self, platform_user_id: str, dialog: MaxDialog) -> None:
        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No current version")
        self.gallery.set_version_favorite(dialog.user_id, dialog.current_version_id, True)
        self._track(
            "favorite_changed",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        self._send_message(platform_user_id, "Добавлено в избранное ⭐")

    def _unlock_or_deliver(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No gallery version selected")
        try:
            reservation = self.demo.commerce.reserve_unlock_delivery(
                dialog.user_id, dialog.current_version_id
            )
        except PaymentRequiredError:
            self._show_continuation_pack_offer(event, dialog)
            return
        try:
            delivered = self._send_file(
                event.user_id,
                reservation.original_path,
                "Оригинал без водяного знака.",
                (),
            )
        except MaxTransportError:
            delivered = False
        if not delivered and not reservation.already_unlocked and reservation.entitlement_id:
            self.demo.commerce.release_unlock_delivery(
                dialog.user_id,
                dialog.current_version_id,
                reservation.entitlement_id,
            )
        if delivered and not reservation.already_unlocked and reservation.entitlement_id:
            self.demo.commerce.commit_unlock_delivery(
                dialog.user_id,
                dialog.current_version_id,
                reservation.entitlement_id,
            )
        now = self.demo.clock().isoformat()
        with self.database.transaction() as connection:
            if delivered:
                connection.execute(
                    """UPDATE gallery_versions SET delivery_count=delivery_count+1,
                       last_delivered_at=? WHERE id=?""",
                    (now, dialog.current_version_id),
                )
        self._track(
            "original_delivered" if delivered else "original_delivery_failed",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        if delivered:
            self._send_message(
                event.user_id,
                "Оригинал готов ✅",
                delivered_actions(),
            )
        else:
            self._send_message(
                event.user_id,
                "Не удалось отправить оригинал. Право на скачивание сохранено.",
                retry_delivery_actions(),
            )

    def _show_continuation_pack_offer(
        self, event: MaxIncomingEvent, dialog: MaxDialog
    ) -> None:
        """Show the selected result before checkout without creating an order."""

        if dialog.pending_action != "checkout":
            self._send_selected_preview(
                event.user_id, dialog, "Выбранная версия"
            )
        self.store.transition(
            event.user_id,
            "result_ready",
            event_key=event.event_key,
            force=True,
            pending_prompt=None,
            pending_action="checkout",
        )
        self._send_message(
            event.user_id,
            "Пакет доступа Ravuna — 49 ₽\n\n"
            "После оплаты начисляется:\n"
            "• 2 обработки\n"
            "• 1 оригинал\n\n"
            "После оплаты вы сможете скачать оригинал этой фотографии "
            "без водяного знака.\n\n"
            "Пакет начисляется сразу после оплаты.",
            (Button("Оплатить 49 ₽", "package:buy"),),
        )

    def _buy_continuation_pack(
        self, event: MaxIncomingEvent, dialog: MaxDialog
    ) -> None:
        self._track(
            "continuation_pack_clicked",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        if not self.settings.payments_enabled:
            self._send_message(
                event.user_id,
                "Пакет доступа Ravuna — 49 ₽\n\n"
                "После оплаты начисляется:\n"
                "• 2 обработки\n"
                "• 1 оригинал\n\n"
                "Пакет начисляется сразу после оплаты.\n\n"
                "Оплата временно недоступна.",
                (Button("📁 Мои работы", "studio:works"),),
            )
            return
        if not dialog.user_id:
            raise InvalidInputError("No Ravuna account is selected")
        version_id = dialog.current_version_id
        if not version_id:
            with self.database.read() as connection:
                latest = connection.execute(
                    """SELECT v.id FROM gallery_versions v
                       JOIN gallery_items i ON i.id=v.gallery_item_id
                       WHERE i.user_id=? AND i.deleted=0 AND v.status='succeeded'
                       ORDER BY v.created_at DESC,v.version_number DESC LIMIT 1""",
                    (dialog.user_id,),
                ).fetchone()
            version_id = latest["id"] if latest else None
        if not version_id:
            raise InvalidInputError("A completed Ravuna version is required for checkout")
        with self._checkout_lock:
            if self._paid_order_exists(dialog.user_id, version_id):
                current = self.store.update(
                    event.user_id,
                    current_version_id=version_id,
                )
                self._unlock_or_deliver(event, current)
                return
            try:
                order = self.payments.create_order(
                    dialog.user_id, version_id, f"max:{event.event_key}"
                )
            except PaymentUnavailable:
                self._send_message(event.user_id, UNLOCK_PLACEHOLDER)
                return
            except PaymentError:
                self._send_message(
                    event.user_id, "Не удалось подготовить оплату. Попробуйте позже."
                )
                return
            if self._payment_card_was_sent(order.id):
                self._send_message(
                    event.user_id,
                    "Ссылка на оплату уже создана.",
                )
                return
            payment_message_id = self._send_message(
                event.user_id,
                "Пакет доступа Ravuna — 49 ₽\n\n"
                "После оплаты начисляется:\n"
                "• 2 обработки\n"
                "• 1 оригинал\n\n"
                "Пакет начисляется сразу после оплаты.",
                (Button("Оплатить 49 ₽", order.payment_url or "package:buy"),),
            )
            self._mark_payment_card_sent(
                order.id,
                order.payment_url or "",
                payment_message_id,
            )

    def _paid_order_exists(self, user_id: str, version_id: str) -> bool:
        with self.database.read() as connection:
            return connection.execute(
                """SELECT 1 FROM payment_orders
                   WHERE user_id=? AND version_id=? AND status IN
                   ('paid','delivery_pending','delivered','partially_refunded')
                   LIMIT 1""",
                (user_id, version_id),
            ).fetchone() is not None

    def _payment_card_was_sent(self, order_id: str) -> bool:
        with self.database.read() as connection:
            return connection.execute(
                """SELECT 1 FROM payment_attempts
                   WHERE order_id=? AND purpose='max_checkout_card'
                         AND status='succeeded'
                   LIMIT 1""",
                (order_id,),
            ).fetchone() is not None

    def _mark_payment_card_sent(
        self, order_id: str, payment_url: str, message_id: str
    ) -> None:
        now = self.demo.clock().isoformat()
        idempotency_key = hashlib.sha256(
            f"max-checkout-card:{order_id}".encode("utf-8")
        ).hexdigest()
        request_digest = (
            hashlib.sha256(payment_url.encode("utf-8")).hexdigest()
            if payment_url else None
        )
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO payment_attempts(
                       id,order_id,purpose,idempotency_key,request_digest,
                       provider_request_id,status,
                       started_at,completed_at
                   ) VALUES(?,?,?,?,?,?,'succeeded',?,?)""",
                (
                    uuid4().hex,
                    order_id,
                    "max_checkout_card",
                    idempotency_key,
                    request_digest,
                    message_id,
                    now,
                    now,
                ),
            )

    def _deactivate_payment_card(self, order_id: str) -> None:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT a.provider_request_id,u.platform_user_id
                   FROM payment_attempts AS a
                   JOIN payment_orders AS o ON o.id=a.order_id
                   JOIN users AS u ON u.id=o.user_id
                   WHERE a.order_id=? AND a.purpose='max_checkout_card'
                         AND a.status='succeeded'
                   ORDER BY a.started_at DESC LIMIT 1""",
                (order_id,),
            ).fetchone()
        message_id = row["provider_request_id"] if row else None
        if not message_id:
            return
        try:
            self._edit_message(
                row["platform_user_id"],
                message_id,
                "Оплата подтверждена ✅",
                (),
            )
        except MaxTransportError:
            LOGGER.info("MAX payment keyboard could not be deactivated")

    def notify_continuation_pack_paid(self, order_id: str) -> bool:
        """Notify after the atomic ledger grant; no original is auto-unlocked."""

        with self.database.read() as connection:
            row = connection.execute(
                """SELECT o.user_id,u.platform_user_id,
                          COALESCE(i.version_id,o.version_id) AS target_version_id,
                          v.gallery_item_id
                   FROM payment_orders o
                   LEFT JOIN payment_intents i ON i.id=o.intent_id
                   JOIN gallery_versions v ON v.id=COALESCE(i.version_id,o.version_id)
                   JOIN users u ON u.id=o.user_id
                   WHERE o.id=?""",
                (order_id,),
            ).fetchone()
        if row is None:
            raise PaymentError("Payment order was not found")
        self._deactivate_payment_card(order_id)
        self.store.get_or_create(row["platform_user_id"], None)
        self.store.update(
            row["platform_user_id"],
            user_id=row["user_id"],
            current_gallery_item_id=row["gallery_item_id"],
            current_version_id=row["target_version_id"],
        )
        balance = self.demo.commerce.balance(row["user_id"])
        entitlements = self.demo.commerce.entitlement_balance(row["user_id"])
        actions = paid_actions()
        self._send_message(
            row["platform_user_id"],
            "✅ Оплата прошла успешно\n\n"
            "Ваш оригинал готов к скачиванию.",
            actions[:1],
        )
        self._send_message(
            row["platform_user_id"],
            "Осталось:\n"
            f"• обработок — {balance.available};\n"
            f"• оригиналов — {entitlements.available}.",
            actions[1:],
        )
        return True

    def deliver_paid_original(self, order_id: str) -> bool:
        """Deliver an already-paid exact version; payment stays paid on MAX failure."""

        with self.database.read() as connection:
            row = connection.execute(
                """SELECT o.user_id,u.platform_user_id,
                          COALESCE(i.version_id,o.version_id) AS target_version_id,
                          v.gallery_item_id
                   FROM payment_orders o
                   LEFT JOIN payment_intents i ON i.id=o.intent_id
                   JOIN gallery_versions v ON v.id=COALESCE(i.version_id,o.version_id)
                   JOIN users u ON u.id=o.user_id
                   WHERE o.id=?""",
                (order_id,),
            ).fetchone()
        if row is None:
            raise PaymentError("Payment order was not found")
        self.store.get_or_create(row["platform_user_id"], None)
        self.store.update(
            row["platform_user_id"],
            user_id=row["user_id"],
            current_gallery_item_id=row["gallery_item_id"],
            current_version_id=row["target_version_id"],
        )
        original = self.payments.original_for_order(order_id, row["user_id"])
        try:
            delivered = self._send_file(
                row["platform_user_id"],
                original,
                "Оригинал без водяного знака.",
                (),
            )
        except MaxTransportError:
            delivered = False
        self.payments.mark_delivery(
            order_id,
            delivered=delivered,
            error_code=None if delivered else "max_delivery_failed",
        )
        if delivered:
            try:
                self._send_message(
                    row["platform_user_id"],
                    "Оригинал готов ✅",
                    delivered_actions(),
                )
            except MaxTransportError:
                LOGGER.info("Paid original delivered but follow-up actions were not sent")
        else:
            try:
                self._send_message(
                    row["platform_user_id"],
                    "Не удалось отправить оригинал. Право на скачивание сохранено.",
                    retry_delivery_actions(),
                )
            except MaxTransportError:
                LOGGER.warning("Paid original delivery and fallback message both failed")
        return delivered

    def _show_works(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        self._track(
            "gallery_opened",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        if not dialog.user_id:
            self._send_message(
                event.user_id,
                "Здесь пока пусто.\n\nСоздайте первую фотографию.",
                (Button("← В меню", "menu"),),
            )
            return
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT id,title,generation_count,favorite,created_at,cover_preview_path
                   FROM gallery_items WHERE user_id=? AND deleted=0
                   ORDER BY updated_at DESC LIMIT 5""",
                (dialog.user_id,),
            ).fetchall()
        self.store.transition(
            event.user_id,
            "gallery",
            event_key=event.event_key,
            force=True,
            pending_prompt=None,
            pending_action=None,
            status_message_id=None,
        )
        if not rows:
            self._send_message(
                event.user_id,
                "Здесь пока пусто.\n\nСоздайте первую фотографию.",
                (Button("← В меню", "menu"),),
            )
            return
        self._send_message(event.user_id, "📂 Мои работы\n\nВыберите работу.")
        fallback_buttons: list[Button] = []
        for row in rows:
            favorite = " ⭐" if row["favorite"] else ""
            count = row["generation_count"]
            version_word = _version_word(count)
            try:
                created = datetime.fromisoformat(row["created_at"]).strftime("%d.%m.%Y")
            except (TypeError, ValueError):
                created = ""
            caption = (
                f"{row['title'][:36]}{favorite}\n"
                f"{created} · {count} {version_word}"
            )
            preview = Path(row["cover_preview_path"]) if row["cover_preview_path"] else None
            if preview and preview.is_file() and self._send_image(
                event.user_id,
                preview,
                caption,
                (Button("Открыть", f"works:open:{row['id']}"),),
            ):
                continue
            fallback_buttons.append(
                Button(
                    f"{row['title'][:28]}{favorite} · {count} {version_word}",
                    f"works:open:{row['id']}",
                )
            )
        if fallback_buttons:
            self._send_message(
                event.user_id, "Работы без доступного превью.", tuple(fallback_buttons)
            )
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
            pending_prompt=None,
            pending_action=None,
            status_message_id=None,
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
            self._send_message(platform_user_id, "Результат ещё не готов.")
            return
        heading = "История версий" if history else title
        caption = f"{heading}{' ⭐' if favorite or current.favorite else ''}\nВерсия {current.version_number} из {len(versions)}"
        dialog = self.store.get(platform_user_id)
        remaining = (
            self.demo.commerce.balance(dialog.user_id).available
            if dialog and dialog.user_id
            else 0
        )
        buttons = (
            version_history_actions()
            if history
            else gallery_item_actions(remaining)
        )
        if not self._send_image(
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
        self.store.transition(
            event.user_id,
            "gallery",
            event_key=event.event_key,
            force=True,
            pending_prompt=None,
            pending_action=None,
            status_message_id=None,
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
        self._track(
            "current_best_changed",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        self._send_message(platform_user_id, "Выбрано как основное.")

    def _delete_current(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        if not dialog.user_id or not dialog.current_gallery_item_id:
            raise InvalidInputError("No current gallery work")
        # Ownership is verified before moving the work to the recoverable trash.
        self.gallery.open_item(dialog.user_id, dialog.current_gallery_item_id)
        self._track(
            "work_deleted",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        self.gallery.soft_delete(dialog.user_id, dialog.current_gallery_item_id)
        self.store.transition(
            event.user_id, "deleted", event_key=event.event_key, force=True,
            pending_prompt=None, pending_action=None, status_message_id=None,
        )
        self._send_message(
            event.user_id,
            "Работа перемещена в корзину.",
            (
                Button("Восстановить", "delete:restore"),
                Button("← В меню", "menu"),
            ),
        )

    def _restore_current(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        if not dialog.user_id or not dialog.current_gallery_item_id:
            raise InvalidInputError("No deleted gallery work")
        self.gallery.restore(dialog.user_id, dialog.current_gallery_item_id)
        self._track(
            "work_restored",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        self.store.transition(
            event.user_id, "gallery", event_key=event.event_key, force=True,
        )
        self._open_work(event, self.store.get(event.user_id) or dialog, dialog.current_gallery_item_id)
