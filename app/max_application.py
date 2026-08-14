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

from app.attribution import AttributionService, parse_start_payload
from app.config import Settings
from app.commerce import CommerceService
from app.database import Database
from app.demo_service import DemoService
from app.direct_prompt import build_direct_edit_plan
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
    ProviderInvalidRequestError,
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
    main_menu,
    new_source_view,
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
from app.max_ui_shell import MaxUiShell, parse_versioned_action
from app.max_transport import MaxIncomingEvent, MaxTransportError
from app.referrals import ReferralService
from app.telemetry import TelemetryRecorder
from app.work_gallery import GalleryPage, WorkGallery
from app.payments import (
    PaymentError,
    PaymentService,
    PaymentStatus,
    PaymentUnavailable,
    build_payment_service,
)


LOGGER = logging.getLogger(__name__)

PHOTO_ACCEPTED_TEXT = (
    "Фото 1 принято.\n\nЧто хотите изменить?"
)
PHOTO_REUSED_TEXT = (
    "Что хотите изменить?"
)
PROCESSING_TEXT = (
    "⏳ Обрабатываю фотографию… Обычно это занимает около 1 минуты. "
    "Пожалуйста, не закрывайте чат."
)
RESULT_HISTORY_SCREENS = frozenset(
    {
        "result_ready",
        "demo_exhausted",
        "original_ready",
        "original_delivery_failed",
    }
)
RESULT_EXIT_ACTIONS = frozenset(
    {"new:source", "result:correct", "result:repeat", "studio:works"}
)
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
PAYMENT_OFFER_TEXT = (
    "Пакет Ravuna — 49 ₽\n\n"
    "В пакет входит:\n"
    "• 2 обработки фотографий\n"
    "• оригинал этой фотографии без водяного знака\n\n"
    "Пакет начислится сразу после оплаты."
)
PENDING_EDIT_PAYMENT_TEXT = (
    "У вас закончились обработки.\n\n"
    "Чтобы обработать эту фотографию, приобретите пакет Ravuna.\n\n"
    + PAYMENT_OFFER_TEXT
)
# Kept as a compatibility symbol for older integrations; this screen is no
# longer rendered in the one-step checkout flow.
PAYMENT_LINK_TEXT = "Ссылка на оплату готова."
NAV_WORKS = "navigation:works"
NAV_WORK = "navigation:work"
NAV_HISTORY = "navigation:history"
NAV_MORE = "navigation:more"
NAV_SETTINGS = "navigation:settings"
NAV_LEGAL_DETAIL = "navigation:legal-detail"
NAV_IDEAS = "navigation:ideas"
NAV_IDEA_CATEGORY = "navigation:idea-category"
NAV_PAYMENT_LINK = "checkout:link"
NAV_SHARE = "navigation:share"
NAV_RATING = "navigation:rating"
NAV_FEEDBACK = "navigation:feedback"
NAV_FEEDBACK_COMMENT = "feedback:comment"

RATING_PROMPT_TEXT = "Как вам результат?"
FEEDBACK_PROMPT_TEXT = (
    "Есть идея, проблема или что-то можно сделать удобнее?\n\n"
    "Напишите сообщение — мы читаем все предложения и улучшаем Ravuna."
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
        self, message_id: str, text: str, buttons: Sequence[Button] = (), **kwargs
    ) -> None: ...
    def answer_callback(self, callback_id: str, notification: str) -> None: ...
    def download_image(self, url: str, destination: Path, max_bytes: int) -> Path: ...
    def send_image(
        self, platform_user_id: str, image: Path, caption: str,
        buttons: Sequence[Button], **kwargs,
    ) -> Optional[str]: ...
    def edit_image(
        self, message_id: str, image: Path, caption: str,
        buttons: Sequence[Button], **kwargs,
    ) -> None: ...
    def delete_message(self, message_id: str) -> None: ...
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
        self.ui = MaxUiShell(database, transport, self.store)
        self.work_gallery = WorkGallery(database, settings.temp_dir)
        self.attribution = AttributionService(database)
        commerce = getattr(demo_service, "commerce", None) or CommerceService(database)
        self.referrals = ReferralService(database, commerce)
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

    def _record_payment_preparation_failure(self, exc: PaymentError) -> None:
        if isinstance(exc, PaymentUnavailable):
            reason = "payment_prepare_provider_unavailable"
        else:
            reason = {
                "Continuation pack price must be exactly 49 RUB": "payment_prepare_config_invalid",
                "Pending edit request was not found": "payment_prepare_pending_request_missing",
                "Gallery version was not found": "payment_prepare_version_missing",
                "Only a completed version can start a package purchase": "payment_prepare_version_invalid",
                "The paid original file is unavailable": "payment_prepare_original_missing",
            }.get(str(exc), "payment_prepare_internal_validation")
        LOGGER.warning("Payment preparation failed safely (reason=%s)", reason)
        self._track("payment_preparation_failed", error_type=reason)

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
                if self.settings.max_single_screen_ui_enabled:
                    self._send_message(
                        row["platform_user_id"], text, screen="processing_interrupted"
                    )
                elif row["status_message_id"]:
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
            if self._deactivate_active_keyboards(event):
                if event.callback_id:
                    self.transport.answer_callback(
                        event.callback_id, "Экран уже изменился"
                    )
                self.store.finish_event(event.event_key, True)
                return True
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
            self._reset_dialog_to_upload(event.user_id, event.event_key)
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
                    Button(
                        "← Назад",
                        "nav:back:work" if dialog and dialog.current_version_id
                        else "nav:back:main",
                    ),
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
                    Button("Пакет Ravuna — 49 ₽", "package:offer"),
                    Button("📂 Мои работы", "studio:works"),
                    Button("← Назад", "nav:back:work"),
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
        if isinstance(exc, ProviderInvalidRequestError):
            self._send_error(
                event.user_id,
                "Не удалось выполнить обработку. Попробуйте переформулировать запрос проще.",
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
            buttons: tuple[Button, ...] = (
                Button("← Назад", "nav:back:main"),
            )
        elif isinstance(exc, InvalidInputError) and dialog and dialog.state in {
            "waiting_for_prompt", "waiting_for_correction"
        }:
            text = "Не получилось прочитать запрос. Напишите короче."
            buttons = (
                Button(
                    "← Назад",
                    "nav:back:work"
                    if dialog.state == "waiting_for_correction"
                    else "nav:back:main",
                ),
            )
        else:
            text = "Что-то пошло не так. Попробуйте ещё раз."
            buttons = (Button("← Назад", "nav:back:main"),)
        self._send_error(event.user_id, text, buttons)

    def _send_error(
        self, user_id: str, text: str, buttons: Sequence[Button] = ()
    ) -> None:
        """Finish a processing status in place; send one fallback if editing fails."""

        dialog = self.store.get(user_id)
        if self.settings.max_single_screen_ui_enabled and not buttons:
            buttons = (
                Button(
                    "← Назад",
                    "nav:back:work"
                    if dialog and dialog.current_version_id
                    else "nav:back:main",
                ),
            )
        status_id = dialog.status_message_id if dialog else None
        if status_id:
            if self.settings.max_single_screen_ui_enabled:
                self._send_message(user_id, text, buttons, screen="error")
                self.store.update(user_id, status_message_id=None)
                return
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
        is_start = (
            event.event_type == "bot_started"
            or text.casefold() in {"/start", "старт"}
        )
        if (
            self.settings.max_single_screen_ui_enabled
            and event.event_type in {"message_created", "bot_started"}
        ):
            active_ui = self.ui.current(event.user_id)
            if active_ui and active_ui.screen in RESULT_HISTORY_SCREENS:
                self.ui.begin_new_message(event.user_id, chat_id=event.chat_id)
            else:
                self.ui.begin_user_input(event.user_id, chat_id=event.chat_id)
        if is_start:
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
                _revision, current_action = parse_versioned_action(
                    event.callback_payload or ""
                )
                notifications = {
                    "prompt:edit": "Напишите изменение",
                    "result:correct": "Напишите изменение",
                }
                notification = notifications.get(current_action, "Готово")
                self.transport.answer_callback(event.callback_id, notification)
            self._callback(event, dialog)
            return
        if event.event_type != "message_created":
            return
        if event.image_url:
            self._receive_source(event, dialog)
            return
        if text and dialog.pending_action == NAV_FEEDBACK_COMMENT:
            self._receive_feedback_comment(event, dialog, text)
            return
        if dialog.state == "waiting_for_source":
            if dialog.pending_action == "second_source":
                self._send_message(
                    event.user_id,
                    "Прикрепите вторую фотографию через скрепку 📎.",
                    (Button("← Назад", "source:one"),),
                )
                return
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
        return_token = (event.start_payload or "").strip()
        if return_token.startswith(("pay_", "payfail_")):
            self._show_payment_return(event, dialog, return_token)
            return
        relationship_id = None
        referral_code = None
        parsed = parse_start_payload(event.start_payload)
        if parsed.referral_code:
            relationship_id = self.referrals.register_start(
                event.user_id, parsed.referral_code
            )
            referral_code = parsed.referral_code if relationship_id else None
        inviter_id = self.referrals.inviter_for_relationship(relationship_id)
        effective_payload = event.start_payload
        if parsed.referral_code and not relationship_id:
            effective_payload = None
        self.attribution.record_start(
            event.user_id,
            f"ref_{referral_code}" if referral_code else effective_payload,
            event_key=f"attribution:{event.event_key}",
            first_referrer_user_id=inviter_id,
        )
        if relationship_id:
            self.attribution.record_event(
                event.user_id,
                "referral_started",
                idempotency_key=f"referral-start:{relationship_id}",
            )
        self._track("start", session_id=dialog.session_id)
        self._show_main(event.user_id, dialog, event.event_key)

    def _show_payment_return(
        self, event: MaxIncomingEvent, dialog: MaxDialog, payload: str
    ) -> None:
        failed = payload.startswith("payfail_")
        token = payload.split("_", 1)[1] if "_" in payload else ""
        try:
            order = self.payments.order_for_platform_user(token, event.user_id)
        except (PaymentError, PaymentUnavailable):
            self._send_message(
                event.user_id,
                "Не удалось найти эту оплату.",
                (Button("← Назад", "nav:back:main"),),
                screen="payment_return_invalid",
            )
            return
        if failed:
            self._send_message(
                event.user_id,
                "Оплата отменена или не завершена. Фотография и запрос сохранены.",
                ((Button("Повторить оплату", self.payments.short_payment_url(order)),)
                 if order.status is PaymentStatus.PENDING else ())
                + (Button("← Назад", "nav:back:main"),),
                screen="payment_return_failed",
            )
            return
        if order.status is PaymentStatus.PENDING:
            self._send_message(
                event.user_id,
                "Проверяем оплату…",
                (
                    Button("Обновить состояние", f"payment:refresh:{token}"),
                    Button("← Назад", "nav:back:main"),
                ),
                screen="payment_return_pending",
            )
            return
        if order.status in {
            PaymentStatus.PAID,
            PaymentStatus.DELIVERY_PENDING,
            PaymentStatus.DELIVERED,
            PaymentStatus.PARTIALLY_REFUNDED,
        }:
            self.notify_continuation_pack_paid(order.id)
            return
        self._send_message(
            event.user_id,
            "Оплата не завершена. Начислений не было.",
            (Button("← Назад", "nav:back:main"),),
            screen="payment_return_failed",
        )

    def _send_view(self, user_id: str, view: View) -> str:
        return self._send_message(user_id, view.text, view.buttons)

    def _send_message(
        self, user_id: str, text: str, buttons: Sequence[Button] = (), **kwargs
    ) -> str:
        if self.settings.max_single_screen_ui_enabled:
            dialog = self.store.get(user_id)
            result = self.ui.render(
                user_id,
                text=text,
                buttons=buttons,
                screen=str(kwargs.pop("screen", self._screen_name(dialog))),
                context=kwargs.pop("context", self._screen_context(dialog)),
                chat_id=dialog.chat_id if dialog else None,
                expected_revision=kwargs.pop("expected_revision", None),
                notify=bool(kwargs.pop("notify", False)),
            )
            if not result.applied or not result.message_id:
                raise MaxTransportError("MAX UI screen was superseded")
            return result.message_id
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
        *,
        screen: Optional[str] = None,
        context: Optional[dict[str, object]] = None,
        expected_revision: Optional[int] = None,
    ) -> Optional[str]:
        if self.settings.max_single_screen_ui_enabled:
            dialog = self.store.get(user_id)
            result = self.ui.render(
                user_id,
                text=caption,
                buttons=buttons,
                screen=screen or self._screen_name(dialog),
                context=context or self._screen_context(dialog),
                chat_id=dialog.chat_id if dialog else None,
                image=image,
                expected_revision=expected_revision,
                notify=False,
            )
            return result.message_id if result.applied else None
        message_id = self.transport.send_image(user_id, image, caption, buttons)
        if message_id and buttons:
            self.store.register_keyboard(user_id, message_id, caption)
        return message_id

    @staticmethod
    def _screen_name(dialog: Optional[MaxDialog]) -> str:
        if dialog is None:
            return "unknown"
        return dialog.pending_action or dialog.state

    @staticmethod
    def _screen_context(dialog: Optional[MaxDialog]) -> dict[str, object]:
        if dialog is None:
            return {}
        return {
            "gallery_item_id": dialog.current_gallery_item_id,
            "version_id": dialog.current_version_id,
            "cursor": dialog.gallery_cursor,
        }

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

        preview = self._selected_preview_path(dialog)
        message_id = self._send_image(platform_user_id, preview, caption, ())
        if not message_id:
            raise MaxTransportError("MAX selected preview delivery failed")
        return message_id

    def _selected_preview_path(self, dialog: MaxDialog) -> Path:
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
        return preview

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

    def _deactivate_active_keyboards(self, event: MaxIncomingEvent) -> bool:
        """Deactivate the source keyboard and identify stale callbacks."""

        if self.settings.max_single_screen_ui_enabled:
            if event.event_type != "message_callback":
                return False
            revision, _action = parse_versioned_action(event.callback_payload or "")
            return not self.ui.callback_is_current(
                event.user_id, event.message_id, revision
            )

        active = self.store.active_keyboards(event.user_id)
        if event.event_type == "message_callback":
            source = next(
                (
                    (message_id, message_text)
                    for message_id, message_text in active
                    if message_id == event.message_id
                ),
                None,
            )
            if source is None:
                self._deactivate_callback_keyboard(event)
                return True
            message_id, message_text = source
            try:
                self.transport.edit_message(message_id, message_text, ())
            except MaxTransportError:
                LOGGER.info(
                    "MAX callback keyboard could not be deactivated "
                    "(message_id_present=true)"
                )
            else:
                self.store.clear_keyboard(event.user_id, message_id)
            return False
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
        return False

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
        account_id = self.adapter.ensure_account(user_id)
        balance = self.demo.commerce.balance(account_id).available
        self._reset_dialog_to_main(user_id, event_key)
        self.store.update(user_id, user_id=account_id)
        payment_url = None
        if balance == 0 and self.settings.payments_enabled:
            try:
                order = self.payments.create_order(
                    account_id,
                    None,
                    f"max-main:{event_key}",
                    account_purchase=True,
                )
                payment_url = self.payments.short_payment_url(order)
            except (PaymentError, PaymentUnavailable) as exc:
                self._record_payment_preparation_failure(exc)
                LOGGER.warning("Start payment offer is temporarily unavailable")
        self._send_view(user_id, main_menu(balance, payment_url))

    def _reset_dialog_to_main(self, user_id: str, event_key: str) -> None:
        self.store.transition(
            user_id, "main_menu", event_key=event_key, force=True,
            selected_scenario_id=None,
            session_id=None,
            pending_prompt=None,
            pending_action=None,
            pending_request_id=None,
            current_gallery_item_id=None,
            current_version_id=None,
            gallery_cursor=0,
            status_message_id=None,
        )

    def _show_upload(
        self, user_id: str, event_key: str, *, view: Optional[View] = None
    ) -> None:
        account_id = self.adapter.ensure_account(user_id)
        if self.demo.commerce.balance(account_id).available <= 0:
            dialog = self.store.get(user_id)
            if dialog is not None:
                self._show_main(user_id, dialog, event_key)
            return
        self._reset_dialog_to_upload(user_id, event_key)
        self._send_view(user_id, view or upload_view())

    def _reset_dialog_to_upload(self, user_id: str, event_key: str) -> None:
        self.store.transition(
            user_id, "waiting_for_source", event_key=event_key, force=True,
            selected_scenario_id=None,
            session_id=None,
            pending_prompt=None,
            pending_action="initial",
            pending_request_id=None,
            current_gallery_item_id=None,
            current_version_id=None,
            gallery_cursor=0,
            status_message_id=None,
        )

    def _mark_navigation(
        self, event: MaxIncomingEvent, dialog: MaxDialog, marker: str
    ) -> MaxDialog:
        return self.store.transition(
            event.user_id,
            dialog.state,
            event_key=event.event_key,
            force=True,
            pending_prompt=None,
            pending_action=marker,
            status_message_id=None,
        )

    def _show_navigation_view(
        self, event: MaxIncomingEvent, dialog: MaxDialog, view: View, marker: str
    ) -> None:
        self._mark_navigation(event, dialog, marker)
        self._send_view(event.user_id, view)

    def _callback(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        _revision, action = parse_versioned_action(event.callback_payload or "")
        if (
            self.settings.max_single_screen_ui_enabled
            and action in RESULT_EXIT_ACTIONS
        ):
            active_ui = self.ui.current(event.user_id)
            if active_ui and active_ui.screen in RESULT_HISTORY_SCREENS:
                self.ui.begin_new_message(event.user_id, chat_id=event.chat_id)
        if action in {"start:details", "legal:details"}:
            self._show_navigation_view(
                event, dialog, legal_details_view(), NAV_SETTINGS
            )
            return
        if action == "legal:offer":
            self._mark_navigation(event, dialog, NAV_LEGAL_DETAIL)
            self._send_message(
                event.user_id,
                "Условия использования\n\n"
                "Сервис создаёт демо-обработку. Пакет доступа Ravuna включает "
                "две обработки и один оригинал. Оплата пока недоступна.",
                (Button("← Назад", "settings"),),
            )
            return
        if action == "legal:privacy":
            self._mark_navigation(event, dialog, NAV_LEGAL_DETAIL)
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
        if action == "upload:ready":
            self._show_upload(event.user_id, event.event_key)
            return
        if action == "new:source":
            self._show_upload(
                event.user_id, event.event_key, view=new_source_view()
            )
            return
        if action == "settings":
            if dialog.pending_action != NAV_SETTINGS:
                self._show_navigation_view(
                    event, dialog, settings_view(), NAV_SETTINGS
                )
            return
        if action == "catalog:ideas":
            if dialog.pending_action != NAV_IDEAS:
                self._show_navigation_view(
                    event, dialog, ideas_catalog(), NAV_IDEAS
                )
            return
        if action.startswith("ideas:"):
            self._show_navigation_view(
                event,
                dialog,
                scenario_catalog(action.split(":", 1)[1]),
                NAV_IDEA_CATEGORY,
            )
            return
        if action == "nav:back:main":
            if dialog.pending_request_id:
                self._show_main(event.user_id, dialog, event.event_key)
                return
            if dialog.pending_action in {
                NAV_WORK,
                NAV_HISTORY,
                NAV_MORE,
                NAV_PAYMENT_LINK,
                "checkout",
                "correction",
            }:
                return
            self._show_main(event.user_id, dialog, event.event_key)
            return
        if action == "nav:back:works":
            if dialog.pending_action == NAV_WORK:
                self._show_works(event, dialog, max(dialog.gallery_cursor, 1))
            return
        if action == "nav:back:work":
            if (
                dialog.pending_action != NAV_WORK
                and dialog.current_gallery_item_id
            ):
                self._show_selected_work(event, dialog)
            return
        if action == "nav:back:prompt":
            if dialog.state != "confirmation":
                return
            correction = dialog.pending_action == "correction"
            target = "waiting_for_correction" if correction else "waiting_for_prompt"
            self.store.transition(
                event.user_id,
                target,
                event_key=event.event_key,
                force=True,
                pending_prompt=None,
                pending_action="correction" if correction else "initial",
            )
            self._send_message(
                event.user_id,
                CORRECTION_REQUEST_TEXT if correction else PHOTO_ACCEPTED_TEXT,
                (
                    Button(
                        "← Назад",
                        "nav:back:work" if correction else "nav:back:main",
                    ),
                ),
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
        elif action == "result:share":
            self._show_share(event, dialog)
        elif action == "result:rate":
            self._show_rating(event, dialog)
        elif action.startswith("result:rating:"):
            try:
                rating = int(action.rsplit(":", 1)[1])
            except ValueError:
                return
            self._record_rating(event, dialog, rating)
        elif action in {"result:feedback", "result:feedback:comment"}:
            self._show_feedback_prompt(event, dialog)
        elif action == "package:buy":
            self._buy_continuation_pack(event, dialog)
        elif action.startswith("payment:refresh:"):
            self._show_payment_return(
                event,
                dialog,
                f"pay_{action.rsplit(':', 1)[-1]}",
            )
        elif action == "source:one":
            updated = self.store.transition(
                event.user_id,
                "waiting_for_prompt",
                event_key=event.event_key,
                force=True,
                pending_action="initial",
            )
            self._send_message(
                event.user_id,
                "Фото 1 принято. Теперь напишите, что хотите изменить.",
                (Button("← Назад", "nav:back:main"),),
                screen="waiting_for_prompt",
            )
        elif action == "source:add-second":
            if not dialog.session_id:
                raise InvalidInputError("The first source image is unavailable")
            self.store.transition(
                event.user_id,
                "waiting_for_source",
                event_key=event.event_key,
                force=True,
                pending_action="second_source",
            )
            self._send_message(
                event.user_id,
                "Прикрепите вторую фотографию через скрепку 📎.",
                (Button("← Назад", "source:one"),),
                screen="waiting_for_second_source",
            )
        elif action == "pending:process":
            self._process_paid_pending_request(event, dialog)
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
            if self.settings.max_single_screen_ui_enabled:
                self._send_image(
                    event.user_id,
                    self._selected_preview_path(dialog),
                    CORRECTION_REQUEST_TEXT,
                    (Button("← Назад", "nav:back:work"),),
                    screen="waiting_for_correction",
                )
            else:
                self._send_selected_preview(
                    event.user_id, dialog, "Текущая версия"
                )
                self._send_message(
                    event.user_id,
                    CORRECTION_REQUEST_TEXT,
                    (Button("← Назад", "nav:back:work"),),
                )
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
            self._favorite(event, dialog)
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
        elif action.startswith("works:page:"):
            try:
                page = int(action.rsplit(":", 1)[1])
            except ValueError:
                return
            self._show_works(event, dialog, page)
        elif action.startswith("works:open:"):
            self._open_work(event, dialog, action.rsplit(":", 1)[1])
        elif action == "work:open":
            if not dialog.current_gallery_item_id:
                raise InvalidInputError("No gallery work selected")
            self._open_work(event, dialog, dialog.current_gallery_item_id)
        elif action == "work:history":
            self._show_version_history(
                event,
                dialog,
                max(dialog.gallery_cursor, 1)
                if dialog.pending_action == NAV_HISTORY
                else 1,
            )
        elif action.startswith("versions:page:"):
            try:
                page = int(action.rsplit(":", 1)[1])
            except ValueError:
                return
            self._show_version_history(
                event, dialog, page
            )
        elif action.startswith("versions:open:"):
            self._open_version(event, dialog, action.rsplit(":", 1)[1])
        elif action == "work:more":
            self._mark_navigation(event, dialog, NAV_MORE)
            self._send_view(event.user_id, gallery_more_actions())
        elif action in {"work:previous", "work:next"}:
            self._navigate_version(event, dialog, -1 if action.endswith("previous") else 1)
        elif action == "work:main":
            self._make_current_best(event, dialog)
        elif action == "result:delete":
            self._send_view(event.user_id, delete_confirmation_view())
        elif action == "delete:cancel":
            if dialog.current_gallery_item_id:
                self._show_selected_work(event, dialog)
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
                pending_request_id=None,
            )
            self._generate(event, updated, correction=False)
            return
        target = "waiting_for_prompt" if reusable_source else "waiting_for_source"
        self.store.transition(
            event.user_id, target, event_key=event.event_key, force=True,
            user_id=user_id,
            selected_scenario_id=scenario_id, pending_prompt=None,
            pending_action="initial",
            pending_request_id=None,
            session_id=session_id,
            current_gallery_item_id=item_id if reusable_source else None,
            current_version_id=None,
        )
        if target == "waiting_for_source":
            self._send_message(
                event.user_id,
                "Пришлите фотографию 📷",
                (Button("← Назад", "nav:back:main"),),
            )
        else:
            self._send_message(
                event.user_id,
                PHOTO_REUSED_TEXT,
                (Button("← Назад", "nav:back:main"),),
            )

    def _receive_source(
        self,
        event: MaxIncomingEvent,
        dialog: MaxDialog,
    ) -> None:
        image_urls = event.image_urls or ((event.image_url,) if event.image_url else ())
        if event.image_attachment_count > 2 or len(image_urls) > 2:
            self._send_message(
                event.user_id,
                "Можно использовать максимум 2 фотографии",
                (Button("← Назад", "nav:back:main"),),
            )
            return
        if event.image_attachment_count and len(image_urls) != event.image_attachment_count:
            raise StorageFailureError("Source image download URL is unavailable")
        if not event.image_url:
            self._send_message(event.user_id, "Пришлите фотографию 📷")
            return
        if dialog.pending_action == "two_sources":
            self._send_message(
                event.user_id,
                "Можно использовать максимум 2 фотографии",
                (Button("← Назад", "nav:back:main"),),
            )
            return
        if dialog.pending_action == "second_source":
            self._receive_secondary_source(event, dialog)
            return
        destination = self.settings.temp_dir / f"max-{event.message_id or event.event_key}.upload"
        secondary_destination = (
            self.settings.temp_dir
            / f"max-secondary-{event.message_id or event.event_key}.upload"
        )
        try:
            try:
                self.transport.download_image(
                    image_urls[0],
                    destination,
                    self.settings.max_source_file_size_mb * 1024 * 1024,
                )
                if len(image_urls) == 2:
                    self.transport.download_image(
                        image_urls[1],
                        secondary_destination,
                        self.settings.max_source_file_size_mb * 1024 * 1024,
                    )
            except MaxTransportError as exc:
                if exc.kind == "media_too_large":
                    raise ImageTooLargeError("Source image exceeds the allowed limit") from exc
                if len(image_urls) == 2:
                    raise StorageFailureError("Source image download failed") from exc
                raise
            try:
                session = self.adapter.start_demo_with_implicit_consent(
                    event.user_id, destination
                )
                if len(image_urls) == 2:
                    self.adapter.add_secondary_source(
                        session.session_id, secondary_destination
                    )
            except OSError as exc:
                raise StorageFailureError("Source image storage failed") from exc
        finally:
            destination.unlink(missing_ok=True)
            secondary_destination.unlink(missing_ok=True)
        with self.database.read() as connection:
            item_id = connection.execute(
                "SELECT gallery_item_id FROM demo_sessions WHERE id=?", (session.session_id,)
            ).fetchone()[0]
        self._track(
            "photo_uploaded",
            session_id=session.session_id,
            gallery_item_id=item_id,
        )
        self.attribution.link_user(event.user_id, session.user_id)
        self.referrals.link_user(event.user_id, session.user_id)
        self.attribution.record_event(
            event.user_id,
            "photo_uploaded",
            idempotency_key=f"photo:{event.event_key}",
            user_id=session.user_id,
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
                pending_request_id=None,
                status_message_id=None,
            )
            self._generate(event, updated, correction=False)
        else:
            updated = self.store.transition(
                event.user_id, "waiting_for_prompt", event_key=event.event_key,
                force=True,
                user_id=session.user_id, session_id=session.session_id,
                current_gallery_item_id=item_id, current_version_id=None,
                pending_request_id=None,
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
                        image_urls=event.image_urls,
                        image_attachment_count=event.image_attachment_count,
                        callback_id=event.callback_id,
                        callback_payload=event.callback_payload,
                    )
                self._receive_prompt(prompt_event, updated)
            else:
                self._send_message(
                    event.user_id,
                    PHOTO_ACCEPTED_TEXT,
                    (
                        Button("Продолжить с одним фото", "source:one"),
                        Button("➕ Добавить второе фото", "source:add-second"),
                        Button("← Назад", "nav:back:main"),
                    ),
                )

    def _receive_secondary_source(
        self, event: MaxIncomingEvent, dialog: MaxDialog
    ) -> None:
        if not event.image_url or not dialog.session_id:
            raise InvalidInputError("The second source image is unavailable")
        destination = (
            self.settings.temp_dir
            / f"max-secondary-{event.message_id or event.event_key}.upload"
        )
        try:
            self.transport.download_image(
                event.image_url,
                destination,
                self.settings.max_source_file_size_mb * 1024 * 1024,
            )
            self.adapter.add_secondary_source(dialog.session_id, destination)
        except MaxTransportError as exc:
            if exc.kind == "media_too_large":
                raise ImageTooLargeError(
                    "Source image exceeds the allowed limit"
                ) from exc
            raise
        finally:
            destination.unlink(missing_ok=True)
        self.store.transition(
            event.user_id,
            "waiting_for_prompt",
            event_key=event.event_key,
            force=True,
            pending_action="two_sources",
        )
        self._send_message(
            event.user_id,
            "Фото 1 и Фото 2 приняты.\n\nТеперь напишите один запрос для обработки.",
            (Button("← Назад", "nav:back:main"),),
            screen="waiting_for_prompt",
        )

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
            self._send_message(
                event.user_id,
                "Что изменить?",
                (
                    Button(
                        "← Назад",
                        "nav:back:work"
                        if dialog.state == "waiting_for_correction"
                        else "nav:back:main",
                    ),
                ),
            )
            return
        if len(prompt) > self.settings.max_prompt_length:
            raise InvalidInputError("Prompt is too long")
        if not dialog.session_id or not self._session_is_usable(dialog.session_id):
            raise DemoExpiredError("The current source image is no longer available")
        mode = "correction" if dialog.state == "waiting_for_correction" else "initial"
        edit_mode = "correction" if mode == "correction" else (
            "scenario" if dialog.selected_scenario_id else "initial_edit"
        )
        if self.settings.image_direct_prompt_enabled:
            preflight = build_direct_edit_plan(
                prompt,
                mode=edit_mode,
                correction_target_version_id=(
                    dialog.current_version_id if mode == "correction" else None
                ),
            )
        else:
            preflight = parse_edit_intent(
                prompt,
                mode=edit_mode,
                scenario_id=dialog.selected_scenario_id,
                correction_target_version_id=(
                    dialog.current_version_id if mode == "correction" else None
                ),
            )
        self._track(
            "prompt_submitted",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
            parser_fallback=(
                not self.settings.image_direct_prompt_enabled
                and preflight.primary_action == "custom"
            ),
        )
        updated = self.store.transition(
            event.user_id, "confirmation", event_key=event.event_key,
            pending_prompt=prompt, pending_action=mode,
        )
        if (
            preflight.unresolved_ambiguities
            and not self.settings.image_direct_prompt_enabled
        ):
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
                Button("← Назад", "nav:back:prompt"),
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

    def _show_rating(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No current version for rating")
        self.store.update(
            event.user_id,
            pending_prompt=None,
            pending_action=NAV_RATING,
        )
        self._send_image(
            event.user_id,
            self._selected_preview_path(dialog),
            RATING_PROMPT_TEXT,
            tuple(
                Button(f"{rating} ⭐", f"result:rating:{rating}", 0)
                for rating in range(1, 6)
            )
            + (Button("← Назад", "nav:back:work", 1),),
            screen="rating",
            context={
                "gallery_item_id": dialog.current_gallery_item_id,
                "version_id": dialog.current_version_id,
            },
        )

    def _record_rating(
        self, event: MaxIncomingEvent, dialog: MaxDialog, rating: int
    ) -> None:
        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No current version for rating")
        self.gallery.record_rating(dialog.user_id, dialog.current_version_id, rating)
        sentiment = "positive" if rating >= 4 else "negative"
        self._track(
            f"feedback_{sentiment}",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
            value_integer=rating,
        )
        self.store.update(
            event.user_id,
            pending_prompt=None,
            pending_action=NAV_FEEDBACK,
        )
        if rating >= 4:
            text = "Спасибо! Рады, что вам понравилось 😊"
            buttons = (Button("← Назад", "nav:back:work"),)
        else:
            text = (
                "Спасибо за честную оценку 🙏\n"
                "Расскажите, что можно улучшить."
            )
            buttons = (
                Button("💬 Написать комментарий", "result:feedback:comment"),
                Button("← Назад", "nav:back:work"),
            )
        self._send_image(
            event.user_id,
            self._selected_preview_path(dialog),
            text,
            buttons,
            screen="rating_result",
            context={
                "gallery_item_id": dialog.current_gallery_item_id,
                "version_id": dialog.current_version_id,
                "rating": rating,
            },
        )

    def _show_feedback_prompt(
        self, event: MaxIncomingEvent, dialog: MaxDialog
    ) -> None:
        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No current version for feedback")
        self.store.update(
            event.user_id,
            pending_prompt=None,
            pending_action=NAV_FEEDBACK_COMMENT,
        )
        self._send_image(
            event.user_id,
            self._selected_preview_path(dialog),
            FEEDBACK_PROMPT_TEXT,
            (Button("← Назад", "nav:back:work"),),
            screen="feedback_prompt",
            context={
                "gallery_item_id": dialog.current_gallery_item_id,
                "version_id": dialog.current_version_id,
            },
        )

    def _receive_feedback_comment(
        self, event: MaxIncomingEvent, dialog: MaxDialog, text: str
    ) -> None:
        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No current version for feedback")
        self.gallery.record_feedback_message(
            dialog.user_id, dialog.current_version_id, text
        )
        self.store.update(
            event.user_id,
            pending_prompt=None,
            pending_action=NAV_FEEDBACK,
        )
        self._send_image(
            event.user_id,
            self._selected_preview_path(dialog),
            "Спасибо! Мы получили ваше сообщение 🙏",
            (Button("← Назад", "nav:back:work"),),
            screen="feedback_saved",
            context={
                "gallery_item_id": dialog.current_gallery_item_id,
                "version_id": dialog.current_version_id,
            },
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
            if not correction and not repeat and not dialog.current_version_id:
                self._show_pending_edit_offer(event, dialog, prompt)
            else:
                self._show_continuation_pack_offer(event, dialog)
            return
        remaining_after = available - 1
        status_id = self._send_message(
            event.user_id, PROCESSING_TEXT, screen="processing"
        )
        processing_ui = self.ui.current(event.user_id)
        processing_revision = processing_ui.revision if processing_ui else None
        self.store.transition(
            event.user_id, "processing", event_key=event.event_key,
            status_message_id=status_id,
        )
        self._track(
            "processing_started",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        preview_delivered = False

        def deliver(preview: Path, _attempt_id: str) -> bool:
            nonlocal preview_delivered
            caption = result_actions(remaining_after).text
            if self.settings.max_single_screen_ui_enabled:
                try:
                    rendered = self.ui.render(
                        event.user_id,
                        text=caption,
                        buttons=result_actions(remaining_after).buttons,
                        screen="result_ready",
                        context={
                            "gallery_item_id": dialog.current_gallery_item_id,
                            "version_id": dialog.current_version_id,
                        },
                        chat_id=event.chat_id,
                        image=preview,
                        expected_revision=processing_revision,
                        notify=False,
                        force_new_image_message=True,
                    )
                except MaxTransportError:
                    LOGGER.info("MAX fresh preview delivery failed safely")
                    return False
                if not rendered.applied:
                    try:
                        self.transport.send_message(
                            event.user_id,
                            "Обработка завершена. Результат доступен в «Моих работах».",
                            (),
                            notify=False,
                        )
                    except MaxTransportError:
                        pass
                else:
                    preview_delivered = True
                return True
            preview_delivered = bool(
                self._send_image(
                    event.user_id,
                    preview,
                    caption,
                    result_actions(remaining_after).buttons,
                )
            )
            return preview_delivered

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
                status_message_id=status_id,
                pending_prompt=prompt if dialog.pending_request_id else None,
                force=True,
            )
            if dialog.pending_request_id:
                with self.database.transaction() as connection:
                    connection.execute(
                        """UPDATE pending_edit_requests SET status='paid',updated_at=?
                           WHERE id=? AND status='processing'""",
                        (self.demo.clock().isoformat(), dialog.pending_request_id),
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
            pending_request_id=None,
        )
        if dialog.pending_request_id:
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE pending_edit_requests SET status='completed',updated_at=?
                       WHERE id=?""",
                    (self.demo.clock().isoformat(), dialog.pending_request_id),
                )
        if self.settings.max_single_screen_ui_enabled:
            active_result = self.ui.current(event.user_id)
            if active_result and active_result.screen == "result_ready":
                self.ui.update_context(
                    event.user_id,
                    {
                        "gallery_item_id": version["gallery_item_id"],
                        "version_id": version["id"],
                    },
                    expected_revision=active_result.revision,
                )
        if not self.settings.max_single_screen_ui_enabled:
            try:
                self.transport.edit_message(status_id, "✨ Готово")
            except MaxTransportError:
                LOGGER.info("MAX status message could not be edited after successful delivery")
        with self.database.read() as connection:
            successful_count = int(
                connection.execute(
                    """SELECT COUNT(*) FROM generation_attempts
                       WHERE user_id=? AND status='succeeded'""",
                    (dialog.user_id,),
                ).fetchone()[0]
            )
        if successful_count == 1 and dialog.user_id:
            self.attribution.record_event(
                event.user_id,
                "first_generation_success",
                idempotency_key=f"first-success:{result.attempt_id}",
                user_id=dialog.user_id,
            )
        if successful_count == 1 and dialog.user_id and preview_delivered:
            reward = self.referrals.reward_first_success(dialog.user_id)
            if reward:
                self.attribution.record_event(
                    event.user_id,
                    "referral_rewarded",
                    idempotency_key=f"referral-reward-event:{reward.relationship_id}",
                    user_id=dialog.user_id,
                )
                try:
                    self.transport.send_message(
                        reward.inviter_platform_user_id,
                        "🎁 Друг воспользовался вашей ссылкой.\n"
                        "Вам начислено 2 бонусные обработки.",
                        (),
                    )
                except MaxTransportError:
                    LOGGER.info("Referral reward notification was not delivered")
        self._track(
            "result_delivered",
            session_id=dialog.session_id,
            attempt_id=result.attempt_id,
            gallery_item_id=version["gallery_item_id"],
            duration_ms=attempt["duration_ms"] if attempt else None,
            estimated_cost=attempt["estimated_cost"] if attempt else None,
        )

    def _favorite(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No current version")
        self.gallery.set_version_favorite(dialog.user_id, dialog.current_version_id, True)
        self._track(
            "favorite_changed",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        self._show_selected_work(event, self.store.get(event.user_id) or dialog)

    def _show_share(self, event: MaxIncomingEvent, dialog: MaxDialog) -> None:
        if not dialog.user_id or not dialog.current_version_id:
            raise InvalidInputError("No result is selected for sharing")
        code = self.referrals.get_or_create_code(dialog.user_id)
        separator = "&" if "?" in self.settings.max_bot_url else "?"
        referral_url = f"{self.settings.max_bot_url}{separator}start=ref_{code}"
        share_text = (
            "Я обработал фотографию в Ravuna прямо в MAX.\n\n"
            "Здесь можно менять фон и одежду, убирать лишних людей и предметы\n"
            "и создавать новые образы.\n\n"
            f"Попробуйте:\n{referral_url}"
        )
        self.attribution.record_event(
            event.user_id,
            "share_opened",
            idempotency_key=f"share:{event.event_key}",
            user_id=dialog.user_id,
        )
        self.store.update(
            event.user_id,
            pending_prompt=None,
            pending_action=NAV_SHARE,
        )
        self._send_image(
            event.user_id,
            self._selected_preview_path(dialog),
            "📤 Поделиться Ravuna\n\n"
            "Скопируйте приглашение и отправьте его другу в любом чате MAX.\n"
            "После его первой успешной обработки вы получите "
            "2 бесплатные обработки.\n\n"
            "Чтобы отправить и фотографию, используйте стандартную функцию "
            "«Переслать» у сообщения с результатом.",
            (
                Button(
                    "📋 Скопировать приглашение",
                    share_text,
                    0,
                    "clipboard",
                ),
                Button(
                    "📋 Скопировать только ссылку",
                    referral_url,
                    1,
                    "clipboard",
                ),
                Button("← Назад", "nav:back:work", 2),
            ),
            screen="share",
            context={
                "gallery_item_id": dialog.current_gallery_item_id,
                "version_id": dialog.current_version_id,
            },
        )

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
        if self.settings.max_single_screen_ui_enabled:
            self.ui.begin_new_message(event.user_id, chat_id=event.chat_id)
        if delivered:
            self._send_message(
                event.user_id,
                "Оригинал готов ✅",
                delivered_actions(),
                screen="original_ready",
            )
        else:
            self._send_message(
                event.user_id,
                "Не удалось отправить оригинал. Право на скачивание сохранено.",
                retry_delivery_actions(),
                screen="original_delivery_failed",
            )

    def _show_pending_edit_offer(
        self, event: MaxIncomingEvent, dialog: MaxDialog, prompt: str
    ) -> None:
        if (
            not dialog.user_id
            or not dialog.session_id
            or not dialog.current_gallery_item_id
            or not prompt
        ):
            raise InvalidInputError("A complete pending edit request is required")
        now = self.demo.clock().isoformat()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id,status FROM pending_edit_requests WHERE session_id=?",
                (dialog.session_id,),
            ).fetchone()
            request_id = existing["id"] if existing else uuid4().hex
            item = connection.execute(
                """SELECT original_source_path,secondary_source_path
                   FROM gallery_items WHERE id=? AND user_id=?""",
                (dialog.current_gallery_item_id, dialog.user_id),
            ).fetchone()
            if item is None:
                raise AssetUnavailableError("Pending edit source is unavailable")
            if existing is None:
                connection.execute(
                    """INSERT INTO pending_edit_requests(
                           id,platform_user_id,user_id,session_id,gallery_item_id,
                           prompt,primary_source_path,secondary_source_path,
                           status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?, 'awaiting_payment',?,?)""",
                    (
                        request_id, event.user_id, dialog.user_id,
                        dialog.session_id, dialog.current_gallery_item_id,
                        prompt, item["original_source_path"],
                        item["secondary_source_path"], now, now,
                    ),
                )
            elif existing["status"] not in {"paid", "processing", "completed"}:
                connection.execute(
                    """UPDATE pending_edit_requests
                       SET prompt=?,primary_source_path=?,secondary_source_path=?,
                           status='awaiting_payment',updated_at=? WHERE id=?""",
                    (
                        prompt, item["original_source_path"],
                        item["secondary_source_path"], now, request_id,
                    ),
                )
            source = connection.execute(
                """SELECT i.original_source_path
                   FROM pending_edit_requests r
                   JOIN gallery_items i ON i.id=r.gallery_item_id
                   WHERE r.id=? AND i.user_id=r.user_id""",
                (request_id,),
            ).fetchone()
        source_path = Path(source["original_source_path"]) if source else None
        if source_path is None or not source_path.is_file():
            raise AssetUnavailableError("Pending edit source is unavailable")
        updated = self.store.transition(
            event.user_id,
            "result_ready",
            event_key=event.event_key,
            force=True,
            pending_prompt=prompt,
            pending_action="checkout",
            pending_request_id=request_id,
            current_gallery_item_id=dialog.current_gallery_item_id,
            current_version_id=None,
            status_message_id=None,
        )
        try:
            order = self.payments.create_order(
                dialog.user_id,
                None,
                f"max:{event.event_key}",
                pending_request_id=request_id,
            )
            payment_url = self.payments.short_payment_url(order)
        except (PaymentError, PaymentUnavailable) as exc:
            self._record_payment_preparation_failure(exc)
            payment_url = "package:buy"
        self._send_image(
            event.user_id,
            source_path,
            PENDING_EDIT_PAYMENT_TEXT,
            (
                Button("Оплатить 49 ₽", payment_url),
                Button("← Назад", "nav:back:main"),
            ),
            screen="pending_payment_offer",
            context={
                "gallery_item_id": updated.current_gallery_item_id,
                "version_id": None,
            },
        )
        self.attribution.record_event(
            event.user_id,
            "payment_offer_opened",
            idempotency_key=f"payment-offer:{event.event_key}",
            user_id=dialog.user_id,
        )

    def _process_paid_pending_request(
        self, event: MaxIncomingEvent, dialog: MaxDialog
    ) -> None:
        if not dialog.pending_request_id or dialog.pending_action != "pending_paid":
            return
        with self.database.transaction() as connection:
            pending = connection.execute(
                """SELECT * FROM pending_edit_requests
                   WHERE id=? AND status IN ('paid','processing')""",
                (dialog.pending_request_id,),
            ).fetchone()
            if pending is None or pending["platform_user_id"] != event.user_id:
                raise InvalidInputError("Pending edit request is unavailable")
            connection.execute(
                "UPDATE pending_edit_requests SET status='processing',updated_at=? WHERE id=?",
                (self.demo.clock().isoformat(), dialog.pending_request_id),
            )
            connection.execute(
                """UPDATE demo_sessions
                   SET source_file_path=?,secondary_source_file_path=?,gallery_item_id=?,
                       updated_at=?
                   WHERE id=? AND user_id=?""",
                (
                    pending["primary_source_path"],
                    pending["secondary_source_path"],
                    pending["gallery_item_id"],
                    self.demo.clock().isoformat(),
                    pending["session_id"],
                    pending["user_id"],
                ),
            )
        updated = self.store.transition(
            event.user_id,
            "confirmation",
            event_key=event.event_key,
            force=True,
            user_id=pending["user_id"],
            session_id=pending["session_id"],
            current_gallery_item_id=pending["gallery_item_id"],
            current_version_id=None,
            pending_prompt=pending["prompt"],
            pending_action="initial",
            pending_request_id=pending["id"],
        )
        self._generate(event, updated, correction=False)

    def _show_continuation_pack_offer(
        self, event: MaxIncomingEvent, dialog: MaxDialog
    ) -> None:
        """Show the selected result with its direct, idempotent checkout link."""

        current = self.store.get(event.user_id) or dialog
        if current.pending_action == "checkout":
            return
        dialog = current
        if not dialog.current_version_id:
            self._send_message(
                event.user_id,
                "Не удалось определить фотографию для оплаты.",
                (Button("← Назад", "nav:back:main"),),
            )
            return
        if dialog.current_version_id and not self.settings.max_single_screen_ui_enabled:
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
        try:
            order = self.payments.create_order(
                dialog.user_id,
                dialog.current_version_id,
                f"max:{event.event_key}",
            )
            payment_url = self.payments.short_payment_url(order)
        except (PaymentError, PaymentUnavailable) as exc:
            self._record_payment_preparation_failure(exc)
            payment_url = "package:buy"
        offer_buttons = (
            Button("Оплатить 49 ₽", payment_url),
            Button("← Назад", "nav:back:work"),
        )
        if self.settings.max_single_screen_ui_enabled and dialog.current_version_id:
            self._send_image(
                event.user_id,
                self._selected_preview_path(dialog),
                PAYMENT_OFFER_TEXT,
                offer_buttons,
                screen="payment_offer",
            )
        else:
            self._send_message(
                event.user_id,
                PAYMENT_OFFER_TEXT,
                offer_buttons,
            )
        self.attribution.record_event(
            event.user_id,
            "payment_offer_opened",
            idempotency_key=f"payment-offer:{event.event_key}",
            user_id=dialog.user_id,
        )

    def _buy_continuation_pack(
        self, event: MaxIncomingEvent, dialog: MaxDialog
    ) -> None:
        self._track(
            "continuation_pack_clicked",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        with self._checkout_lock:
            current = self.store.get(event.user_id) or dialog
            if current.pending_action != "checkout":
                self._show_main(event.user_id, current, event.event_key)
                return
            if not self.settings.payments_enabled:
                self._send_message(
                    event.user_id,
                    "Оплата временно недоступна.",
                    (Button("← Назад", "nav:back:work"),),
                )
                return
            if not current.user_id:
                raise InvalidInputError("No Ravuna account is selected")
            version_id = current.current_version_id
            pending_request_id = current.pending_request_id
            if not version_id and not pending_request_id:
                raise InvalidInputError(
                    "A current Ravuna payment target is required for checkout"
                )
            if version_id and self._paid_order_exists(current.user_id, version_id):
                current = self.store.update(
                    event.user_id,
                    current_version_id=version_id,
                )
                self._unlock_or_deliver(event, current)
                return
            try:
                order = self.payments.create_order(
                    current.user_id,
                    version_id,
                    f"max:{event.event_key}",
                    pending_request_id=pending_request_id,
                )
            except PaymentUnavailable as exc:
                self._record_payment_preparation_failure(exc)
                self._send_message(
                    event.user_id,
                    UNLOCK_PLACEHOLDER,
                    (
                        Button(
                            "← Назад",
                            "nav:back:main" if pending_request_id else "nav:back:work",
                        ),
                    ),
                )
                return
            except PaymentError as exc:
                self._record_payment_preparation_failure(exc)
                self._send_message(
                    event.user_id,
                    "Не удалось подготовить оплату. Попробуйте позже.",
                    (
                        Button(
                            "← Назад",
                            "nav:back:main" if pending_request_id else "nav:back:work",
                        ),
                    ),
                )
                return
            self.attribution.record_event(
                event.user_id,
                "payment_started",
                idempotency_key=f"payment-start:{order.id}",
                user_id=current.user_id,
            )
            # Compatibility path for an old callback: render the same paywall,
            # never a second "link ready" screen.
            if pending_request_id:
                self._show_pending_edit_offer(
                    event, current, current.pending_prompt or ""
                )
            else:
                self._show_continuation_pack_offer(event, current)

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
                """SELECT o.user_id,o.status,o.payment_purpose,u.platform_user_id,
                          COALESCE(i.version_id,o.version_id) AS target_version_id,
                          v.gallery_item_id,
                          COALESCE(i.pending_request_id,o.pending_request_id) AS pending_request_id,
                          r.session_id AS pending_session_id,
                          r.gallery_item_id AS pending_gallery_item_id,
                          r.prompt AS pending_prompt
                   FROM payment_orders o
                   LEFT JOIN payment_intents i ON i.id=o.intent_id
                   LEFT JOIN gallery_versions v ON v.id=COALESCE(i.version_id,o.version_id)
                   LEFT JOIN pending_edit_requests r
                     ON r.id=COALESCE(i.pending_request_id,o.pending_request_id)
                   JOIN users u ON u.id=o.user_id
                   WHERE o.id=?""",
                (order_id,),
            ).fetchone()
        if row is None:
            raise PaymentError("Payment order was not found")
        if not self.settings.max_single_screen_ui_enabled:
            self._deactivate_payment_card(order_id)
        self.store.get_or_create(row["platform_user_id"], None)
        balance = self.demo.commerce.balance(row["user_id"])
        entitlements = self.demo.commerce.entitlement_balance(row["user_id"])
        if row["pending_request_id"]:
            if (
                not row["pending_session_id"]
                or not row["pending_gallery_item_id"]
                or not row["pending_prompt"]
            ):
                raise PaymentError("Paid pending edit request is incomplete")
            self.store.transition(
                row["platform_user_id"],
                "result_ready",
                force=True,
                user_id=row["user_id"],
                session_id=row["pending_session_id"],
                current_gallery_item_id=row["pending_gallery_item_id"],
                current_version_id=None,
                pending_prompt=row["pending_prompt"],
                pending_action="pending_paid",
                pending_request_id=row["pending_request_id"],
                status_message_id=None,
            )
            self._send_message(
                row["platform_user_id"],
                "✅ Оплата прошла успешно\n\n"
                "Фотография и запрос сохранены. Можно начать обработку.\n\n"
                "Осталось:\n"
                f"• обработок — {balance.available};\n"
                f"• оригиналов — {entitlements.available}.",
                (
                    Button("Обработать эту фотографию", "pending:process"),
                    Button("← Назад", "nav:back:main"),
                ),
                screen="pending_payment_success",
            )
            self.attribution.record_event(
                row["platform_user_id"],
                "payment_success",
                idempotency_key=f"payment-success:{order_id}",
                user_id=row["user_id"],
            )
            return True
        if row["payment_purpose"] == "account_topup":
            self.store.transition(
                row["platform_user_id"],
                "main_menu",
                force=True,
                user_id=row["user_id"],
                session_id=None,
                current_gallery_item_id=None,
                current_version_id=None,
                pending_prompt=None,
                pending_action=None,
                pending_request_id=None,
                status_message_id=None,
            )
            menu = main_menu(balance.available)
            self._send_view(
                row["platform_user_id"],
                View(
                    "✅ Оплата прошла успешно\n\n"
                    f"Доступно обработок: {balance.available}\n\n"
                    + menu.text,
                    menu.buttons,
                ),
            )
            return True
        if not row["target_version_id"] or not row["gallery_item_id"]:
            raise PaymentError("Paid version target is incomplete")
        self.store.update(
            row["platform_user_id"],
            user_id=row["user_id"],
            current_gallery_item_id=row["gallery_item_id"],
            current_version_id=row["target_version_id"],
        )
        if row["status"] != PaymentStatus.DELIVERED.value:
            self.attribution.record_event(
                row["platform_user_id"],
                "payment_success",
                idempotency_key=f"payment-success:{order_id}",
                user_id=row["user_id"],
            )
            return self.deliver_paid_original(order_id)
        actions = paid_actions()
        if self.settings.max_single_screen_ui_enabled:
            self._send_message(
                row["platform_user_id"],
                "✅ Оплата прошла успешно\n\n"
                "Ваш оригинал готов к скачиванию.\n\n"
                "Осталось:\n"
                f"• обработок — {balance.available};\n"
                f"• оригиналов — {entitlements.available}.",
                actions,
                screen="payment_success",
            )
        else:
            self._send_message(
                row["platform_user_id"],
                "✅ Оплата прошла успешно\n\n"
                "Ваш оригинал готов к скачиванию.",
                (actions[0], actions[-1]),
            )
            self._send_message(
                row["platform_user_id"],
                "Осталось:\n"
                f"• обработок — {balance.available};\n"
                f"• оригиналов — {entitlements.available}.",
                actions[1:],
            )
        self.attribution.record_event(
            row["platform_user_id"],
            "payment_success",
            idempotency_key=f"payment-success:{order_id}",
            user_id=row["user_id"],
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
        reservation = self.demo.commerce.reserve_unlock_delivery(
            row["user_id"], row["target_version_id"]
        )
        try:
            delivered = bool(self._send_file(
                row["platform_user_id"],
                reservation.original_path,
                "Оригинал без водяного знака.",
                (),
            ))
        except MaxTransportError:
            delivered = False
        if not delivered and not reservation.already_unlocked and reservation.entitlement_id:
            self.demo.commerce.release_unlock_delivery(
                row["user_id"], row["target_version_id"], reservation.entitlement_id
            )
        if delivered and not reservation.already_unlocked and reservation.entitlement_id:
            self.demo.commerce.commit_unlock_delivery(
                row["user_id"], row["target_version_id"], reservation.entitlement_id
            )
        if delivered:
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE gallery_versions SET delivery_count=delivery_count+1,
                       last_delivered_at=? WHERE id=?""",
                    (self.demo.clock().isoformat(), row["target_version_id"]),
                )
        self.payments.mark_delivery(
            order_id,
            delivered=delivered,
            error_code=None if delivered else "max_delivery_failed",
        )
        if self.settings.max_single_screen_ui_enabled:
            self.ui.begin_new_message(row["platform_user_id"])
        if delivered:
            try:
                balance = self.demo.commerce.balance(row["user_id"])
                self._send_message(
                    row["platform_user_id"],
                    "✅ Оплата прошла успешно\n\n"
                    "Оригинал отправлен.\n\n"
                    f"Доступно обработок: {balance.available}",
                    delivered_actions(),
                    screen="original_ready",
                )
            except MaxTransportError:
                LOGGER.info("Paid original delivered but follow-up actions were not sent")
        else:
            try:
                self._send_message(
                    row["platform_user_id"],
                    "Не удалось отправить оригинал. Право на скачивание сохранено.",
                    retry_delivery_actions(),
                    screen="original_delivery_failed",
                )
            except MaxTransportError:
                LOGGER.warning("Paid original delivery and fallback message both failed")
        return delivered

    def _show_works(
        self, event: MaxIncomingEvent, dialog: MaxDialog, page: int = 1
    ) -> None:
        self._track(
            "gallery_opened",
            session_id=dialog.session_id,
            gallery_item_id=dialog.current_gallery_item_id,
        )
        self.store.transition(
            event.user_id,
            "gallery",
            event_key=event.event_key,
            force=True,
            pending_prompt=None,
            pending_action=NAV_WORKS,
            gallery_cursor=max(page, 1),
            status_message_id=None,
        )
        if self.settings.max_single_screen_ui_enabled:
            self._show_works_page(event, dialog, page)
            return
        if not dialog.user_id:
            self._send_message(
                event.user_id,
                "Здесь пока пусто.\n\nСоздайте первую фотографию.",
                (Button("← Назад", "nav:back:main"),),
            )
            return
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT id,title,generation_count,favorite,created_at,cover_preview_path
                   FROM gallery_items WHERE user_id=? AND deleted=0
                   ORDER BY updated_at DESC LIMIT 5""",
                (dialog.user_id,),
            ).fetchall()
        if not rows:
            self._send_message(
                event.user_id,
                "Здесь пока пусто.\n\nСоздайте первую фотографию.",
                (Button("← Назад", "nav:back:main"),),
            )
            return
        self._send_message(
            event.user_id,
            "📂 Мои работы\n\nВыберите работу.",
            (Button("← Назад", "nav:back:main"),),
        )
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
                event.user_id,
                "Работы без доступного превью.",
                tuple(fallback_buttons)
                + (Button("← Назад", "nav:back:main"),),
            )

    def _show_works_page(
        self, event: MaxIncomingEvent, dialog: MaxDialog, page: int
    ) -> None:
        if not dialog.user_id:
            self._send_message(
                event.user_id,
                "Здесь пока пусто.\n\nСоздайте первую фотографию.",
                (Button("← Назад", "nav:back:main"),),
                screen="works_empty",
            )
            return
        gallery_page = self.work_gallery.works(dialog.user_id, page)
        self.store.update(event.user_id, gallery_cursor=gallery_page.page)
        if not gallery_page.entries:
            self._send_message(
                event.user_id,
                "Здесь пока пусто.\n\nСоздайте первую фотографию.",
                (Button("← Назад", "nav:back:main"),),
                screen="works_empty",
            )
            return
        current = self.ui.current(event.user_id)
        sheet = self.work_gallery.contact_sheet(
            f"works:{dialog.user_id}:{event.chat_id or event.user_id}",
            gallery_page,
            (current.revision if current else 0) + 1,
        )
        buttons = self._gallery_page_buttons(gallery_page, "works")
        self._send_image(
            event.user_id,
            sheet,
            f"📂 Мои работы\n\nСтраница {gallery_page.page} из {gallery_page.pages}",
            buttons,
            screen="works_gallery",
            context={"page": gallery_page.page},
        )

    @staticmethod
    def _gallery_page_buttons(
        page: GalleryPage, kind: str
    ) -> tuple[Button, ...]:
        item_prefix = "works:open" if kind == "works" else "versions:open"
        buttons = tuple(
            Button(
                f"Открыть {index}",
                f"{item_prefix}:{entry.id}",
                (index - 1) // 3,
            )
            for index, entry in enumerate(page.entries, 1)
        )
        navigation_row = 2
        previous_page = max(1, page.page - 1)
        next_page = min(page.pages, page.page + 1)
        navigation = (
            Button("◀", f"{kind}:page:{previous_page}", navigation_row),
            Button(
                f"{page.page}/{page.pages}",
                f"{kind}:page:{page.page}",
                navigation_row,
            ),
            Button("▶", f"{kind}:page:{next_page}", navigation_row),
        )
        back = Button(
            "← Назад",
            "nav:back:main" if kind == "works" else "nav:back:work",
            3,
        )
        return buttons + navigation + (back,)

    def _open_work(
        self, event: MaxIncomingEvent, dialog: MaxDialog, item_id: str
    ) -> None:
        if not dialog.user_id:
            raise InvalidInputError("Gallery owner is missing")
        item, best = self.gallery.open_item(dialog.user_id, item_id)
        versions = self._ready_gallery_versions(
            self.gallery.list_versions(dialog.user_id, item_id)
        )
        if best not in versions:
            best = None
        if not best and versions:
            best = versions[-1]
        self.store.transition(
            event.user_id, "gallery", event_key=event.event_key, force=True,
            current_gallery_item_id=item.id,
            current_version_id=best.id if best else None,
            pending_prompt=None,
            pending_action=NAV_WORK,
            status_message_id=None,
        )
        self._send_work(event.user_id, item.title, item.favorite, versions, best)

    def _show_selected_work(
        self, event: MaxIncomingEvent, dialog: MaxDialog
    ) -> None:
        if not dialog.user_id or not dialog.current_gallery_item_id:
            self._show_works(event, dialog)
            return
        item, best = self.gallery.open_item(
            dialog.user_id, dialog.current_gallery_item_id
        )
        versions = self._ready_gallery_versions(
            self.gallery.list_versions(dialog.user_id, item.id)
        )
        selected = next(
            (
                version
                for version in versions
                if version.id == dialog.current_version_id
            ),
            best or (versions[-1] if versions else None),
        )
        self.store.transition(
            event.user_id,
            "gallery",
            event_key=event.event_key,
            force=True,
            current_gallery_item_id=item.id,
            current_version_id=selected.id if selected else None,
            pending_prompt=None,
            pending_action=NAV_WORK,
            status_message_id=None,
        )
        self._send_work(
            event.user_id, item.title, item.favorite, versions, selected
        )

    def _send_work(
        self,
        platform_user_id: str,
        title: str,
        favorite: bool,
        versions: list[GalleryVersion],
        current: Optional[GalleryVersion],
        *,
        history: bool = False,
        version_detail: bool = False,
    ) -> None:
        if current is None or current.preview_path is None:
            self._send_message(
                platform_user_id,
                "Результат ещё не готов.",
                (
                    Button(
                        "← Назад",
                        "nav:back:work" if history else "nav:back:works",
                    ),
                ),
            )
            return
        heading = "История версий" if history else title
        with self.database.read() as connection:
            metadata = connection.execute(
                "SELECT created_at,status FROM gallery_versions WHERE id=?",
                (current.id,),
            ).fetchone()
        try:
            created = datetime.fromisoformat(metadata["created_at"]).strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            created = ""
        status = "Готово" if metadata and metadata["status"] == "succeeded" else "В работе"
        caption = (
            f"{heading}{' ⭐' if favorite or current.favorite else ''}\n"
            f"Версия {current.version_number} из {len(versions)}"
            f"{f' · {created}' if created else ''} · {status}"
        )
        dialog = self.store.get(platform_user_id)
        remaining = (
            self.demo.commerce.balance(dialog.user_id).available
            if dialog and dialog.user_id
            else 0
        )
        if version_detail:
            buttons = (
                Button("← Предыдущая", "work:previous"),
                Button("Следующая →", "work:next"),
                Button("Сделать основной", "work:main"),
                Button("← Назад", "work:history"),
            )
        else:
            buttons = (
                version_history_actions()
                if history
                else gallery_item_actions(remaining)
            )
        if not self._send_image(
            platform_user_id, current.preview_path, caption, buttons
        ):
            raise MaxTransportError("MAX gallery preview delivery failed")

    @staticmethod
    def _ready_gallery_versions(
        versions: Sequence[GalleryVersion],
    ) -> list[GalleryVersion]:
        ready: list[GalleryVersion] = []
        for version in versions:
            preview = version.preview_path
            if version.status != "succeeded" or preview is None:
                continue
            try:
                if preview.is_symlink() or not preview.is_file():
                    continue
            except OSError:
                continue
            ready.append(version)
        return ready

    def _show_version_history(
        self, event: MaxIncomingEvent, dialog: MaxDialog, page: int = 1
    ) -> None:
        if not dialog.user_id or not dialog.current_gallery_item_id:
            raise InvalidInputError("No gallery work selected")
        item, best = self.gallery.open_item(dialog.user_id, dialog.current_gallery_item_id)
        versions = self._ready_gallery_versions(
            self.gallery.list_versions(dialog.user_id, item.id)
        )
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
            pending_action=NAV_HISTORY,
            gallery_cursor=max(page, 1),
            status_message_id=None,
        )
        if self.settings.max_single_screen_ui_enabled:
            gallery_page = self.work_gallery.versions(
                dialog.user_id, dialog.current_gallery_item_id, page
            )
            self.store.update(event.user_id, gallery_cursor=gallery_page.page)
            if not gallery_page.entries:
                self._send_message(
                    event.user_id,
                    "У этой работы пока нет готовых версий.",
                    (Button("← Назад", "nav:back:work"),),
                    screen="version_history_empty",
                )
                return
            active = self.ui.current(event.user_id)
            sheet = self.work_gallery.contact_sheet(
                "versions:"
                f"{dialog.user_id}:{event.chat_id or event.user_id}:"
                f"{dialog.current_gallery_item_id}",
                gallery_page,
                (active.revision if active else 0) + 1,
            )
            self._send_image(
                event.user_id,
                sheet,
                f"История версий\n\nСтраница {gallery_page.page} из {gallery_page.pages}",
                self._gallery_page_buttons(gallery_page, "versions"),
                screen="version_history",
                context={
                    "gallery_item_id": dialog.current_gallery_item_id,
                    "page": gallery_page.page,
                },
            )
            return
        self._send_work(
            event.user_id, item.title, item.favorite, versions, current, history=True
        )

    def _open_version(
        self, event: MaxIncomingEvent, dialog: MaxDialog, version_id: str
    ) -> None:
        if not dialog.user_id or not dialog.current_gallery_item_id:
            raise InvalidInputError("No gallery work selected")
        item, _best = self.gallery.open_item(
            dialog.user_id, dialog.current_gallery_item_id
        )
        versions = self._ready_gallery_versions(
            self.gallery.list_versions(dialog.user_id, item.id)
        )
        selected = next((version for version in versions if version.id == version_id), None)
        if selected is None:
            raise InvalidInputError("Gallery version is not available")
        self.store.update(
            event.user_id,
            current_version_id=selected.id,
            pending_action=NAV_HISTORY,
        )
        self._send_work(
            event.user_id,
            item.title,
            item.favorite,
            versions,
            selected,
            history=True,
            version_detail=True,
        )

    def _navigate_version(
        self, event: MaxIncomingEvent, dialog: MaxDialog, direction: int
    ) -> None:
        if not dialog.user_id or not dialog.current_gallery_item_id:
            raise InvalidInputError("No gallery work selected")
        item, _best = self.gallery.open_item(dialog.user_id, dialog.current_gallery_item_id)
        versions = self._ready_gallery_versions(
            self.gallery.list_versions(dialog.user_id, item.id)
        )
        if not versions:
            raise InvalidInputError("Work has no versions")
        current_index = next(
            (index for index, version in enumerate(versions) if version.id == dialog.current_version_id),
            len(versions) - 1,
        )
        selected = versions[(current_index + direction) % len(versions)]
        self.store.update(
            event.user_id,
            current_version_id=selected.id,
            pending_action=NAV_HISTORY,
        )
        self._send_work(
            event.user_id, item.title, item.favorite, versions, selected, history=True
        )

    def _make_current_best(
        self, event: MaxIncomingEvent, dialog: MaxDialog
    ) -> None:
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
        if dialog.pending_action == NAV_HISTORY:
            self._show_version_history(event, dialog)
        else:
            self._show_selected_work(event, dialog)

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
                Button("← Назад", "studio:works"),
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
