"""MAX runtime composition and fail-closed production polling loop."""

from __future__ import annotations

import logging
import threading

from app.config import Settings
from app.database import Database
from app.image_service import build_demo_service
from app.max_application import MaxApplication
from app.max_conversation import MaxConversationStore
from app.max_transport import (
    MaxApiClient,
    MaxTransportError,
    SingleInstanceLock,
    parse_update,
)
from app.payments import build_payment_service
from app.payment_webhook import PaymentWebhookServer


LOGGER = logging.getLogger(__name__)
OBSERVE_ONLY_TEXT = (
    "Ravuna временно недоступна.\n\n"
    "Попробуйте немного позже."
)

_RECIPIENT_DELIVERY_STAGES = {
    "message_send",
    "image_message_send",
    "file_message_send",
}


def _is_permanent_recipient_failure(exc: MaxTransportError) -> bool:
    """Return true only when retrying the same recipient cannot succeed."""

    if exc.kind == "bot_not_active":
        return True
    return (
        exc.stage in _RECIPIENT_DELIVERY_STAGES
        and exc.http_status in {403, 404}
    )


def build_max_application(
    settings: Settings, client: MaxApiClient | None = None
) -> tuple[MaxApplication, MaxApiClient, MaxConversationStore]:
    if not settings.max_owner_user_ids:
        raise MaxTransportError(
            "MAX owner allowlist is required when user handlers are enabled",
            kind="configuration_missing",
        )
    database = Database(settings.database_path)
    transport = client or MaxApiClient(
        settings.max_bot_token,
        settings.max_api_base_url,
        timeout_seconds=max(30, settings.max_poll_timeout_seconds + 5),
        ca_bundle=settings.max_ca_bundle_path,
        media_host_suffixes=settings.max_media_host_suffixes,
    )
    demo = build_demo_service(settings, provider_name=settings.image_provider)
    store = MaxConversationStore(database)
    payments = build_payment_service(settings, database)
    return MaxApplication(
        settings, database, demo, transport, store, payment_service=payments
    ), transport, store


def run_polling(settings: Settings, stop_event: threading.Event) -> int:
    """Run the single production polling instance for observe-only or allowlisted use."""

    database = Database(settings.database_path)
    store = MaxConversationStore(database)
    client = MaxApiClient(
        settings.max_bot_token,
        settings.max_api_base_url,
        timeout_seconds=max(30, settings.max_poll_timeout_seconds + 5),
        ca_bundle=settings.max_ca_bundle_path,
        media_host_suffixes=settings.max_media_host_suffixes,
    )
    application = None
    payment_webhook = None
    if not settings.max_poll_observe_only:
        if not settings.max_owner_user_ids:
            client.close()
            raise MaxTransportError(
                "MAX owner allowlist is required when observe-only mode is disabled",
                kind="configuration_missing",
            )
        application, client, store = build_max_application(settings, client)
    try:
        with SingleInstanceLock(settings.max_poll_lock_path):
            if settings.payment_webhook_listener_enabled:
                payment_service = (
                    application.payments
                    if application is not None
                    else build_payment_service(settings, database)
                )
                payment_webhook = PaymentWebhookServer(
                    payment_service,
                    settings.payment_webhook_host,
                    settings.payment_webhook_port,
                    settings.payment_webhook_path,
                    accepting_callbacks=settings.payment_webhook_enabled,
                    verify_duplicate_callback=(
                        settings.robokassa_sandbox_duplicate_probe
                    ),
                    on_paid=(
                        application.notify_continuation_pack_paid
                        if application is not None else None
                    ),
                )
                payment_webhook.start()
            runtime_database = application.database if application is not None else database
            crash_recovery = runtime_database.recover_interrupted_runtime()
            if any(crash_recovery.values()):
                LOGGER.warning(
                    "Recovered interrupted runtime state "
                    "(completed_events=%s,retryable_events=%s,attempts=%s)",
                    crash_recovery["completed_events"],
                    crash_recovery["retryable_events"],
                    crash_recovery["interrupted_attempts"],
                )
            if application is not None:
                recovered = application.recover_interrupted_processing()
                if recovered:
                    LOGGER.warning(
                        "Recovered interrupted MAX processing dialogs (count=%s)",
                        recovered,
                    )
            marker = store.get_marker()
            LOGGER.info(
                "MAX closed-test polling started (observe_only=%s)",
                settings.max_poll_observe_only,
            )
            while not stop_event.is_set():
                try:
                    updates, next_marker = client.get_updates(
                        marker, timeout=settings.max_poll_timeout_seconds
                    )
                    store.touch_poll_success()
                    if settings.max_poll_observe_only:
                        batch_ok = True
                        if updates:
                            event_types = sorted(
                                {
                                    str(update.get("update_type") or "unknown")
                                    for update in updates
                                    if isinstance(update, dict)
                                }
                            )
                            LOGGER.info(
                                "MAX transport-only batch observed (count=%s,event_types=%s)",
                                len(updates),
                                ",".join(event_types),
                            )
                        for raw_update in updates:
                            event = parse_update(raw_update)
                            if event is None or not store.begin_event(
                                event.event_key, event.event_type
                            ):
                                continue
                            try:
                                if event.callback_id:
                                    client.answer_callback(
                                        event.callback_id,
                                        "Сервис временно недоступен",
                                    )
                                active_keyboards = store.active_keyboards(event.user_id)
                                active_ids = {
                                    message_id
                                    for message_id, _message_text in active_keyboards
                                }
                                for message_id, message_text in active_keyboards:
                                    try:
                                        client.edit_message(
                                            message_id, message_text, ()
                                        )
                                    except MaxTransportError:
                                        LOGGER.info(
                                            "Observe-only stale keyboard "
                                            "could not be deactivated"
                                        )
                                    else:
                                        store.clear_keyboard(
                                            event.user_id, message_id
                                        )
                                if (
                                    event.message_id
                                    and event.event_type == "message_callback"
                                    and event.message_id not in active_ids
                                ):
                                    try:
                                        client.edit_message(
                                            event.message_id,
                                            event.text
                                            or "Сервис временно недоступен",
                                            (),
                                        )
                                    except MaxTransportError:
                                        LOGGER.info(
                                            "Observe-only callback keyboard "
                                            "could not be deactivated"
                                        )
                                client.send_message(event.user_id, OBSERVE_ONLY_TEXT)
                                store.finish_event(event.event_key, True)
                            except MaxTransportError as exc:
                                store.finish_event(event.event_key, False)
                                if _is_permanent_recipient_failure(exc):
                                    LOGGER.warning(
                                        "MAX event skipped after permanent recipient "
                                        "failure (event_type=%s,kind=%s,stage=%s,"
                                        "http_status=%s)",
                                        event.event_type,
                                        exc.kind,
                                        exc.stage,
                                        exc.http_status,
                                    )
                                    continue
                                batch_ok = False
                                break
                        if batch_ok and next_marker is not None:
                            marker = next_marker
                        store.set_marker(marker)
                        if not batch_ok:
                            stop_event.wait(settings.max_poll_retry_seconds)
                            continue
                        if not updates:
                            stop_event.wait(settings.max_poll_idle_seconds)
                        continue
                    batch_ok = True
                    for raw_update in updates:
                        event = parse_update(raw_update)
                        if event is None:
                            continue
                        try:
                            application.handle(event)
                        except MaxTransportError as exc:
                            if _is_permanent_recipient_failure(exc):
                                LOGGER.warning(
                                    "MAX event skipped after permanent recipient "
                                    "failure (event_type=%s,kind=%s,stage=%s,"
                                    "http_status=%s)",
                                    event.event_type,
                                    exc.kind,
                                    exc.stage,
                                    exc.http_status,
                                )
                                continue
                            batch_ok = False
                            LOGGER.error(
                                "MAX event transport failed safely "
                                "(event_type=%s,kind=%s,stage=%s,http_status=%s)",
                                event.event_type,
                                exc.kind,
                                exc.stage,
                                exc.http_status,
                            )
                            break
                        except Exception as exc:
                            batch_ok = False
                            LOGGER.error(
                                "MAX event processing failed safely (event_type=%s,error_type=%s)",
                                event.event_type, type(exc).__name__,
                            )
                            break
                    if batch_ok and next_marker is not None:
                        marker = next_marker
                        store.set_marker(marker)
                    elif not batch_ok:
                        stop_event.wait(settings.max_poll_retry_seconds)
                except MaxTransportError as exc:
                    LOGGER.error(
                        "MAX polling transport failure (kind=%s,http_status=%s): %s",
                        exc.kind,
                        exc.http_status,
                        exc,
                    )
                    stop_event.wait(settings.max_poll_retry_seconds)
    finally:
        if payment_webhook is not None:
            payment_webhook.stop()
        client.close()
    LOGGER.info("MAX polling stopped")
    return 0
