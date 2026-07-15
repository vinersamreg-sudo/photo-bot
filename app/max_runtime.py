"""MAX runtime composition and temporary closed-test polling loop."""

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


LOGGER = logging.getLogger(__name__)


def build_max_application(
    settings: Settings, client: MaxApiClient | None = None
) -> tuple[MaxApplication, MaxApiClient, MaxConversationStore]:
    database = Database(settings.database_path)
    transport = client or MaxApiClient(
        settings.max_bot_token,
        settings.max_api_base_url,
        timeout_seconds=max(30, settings.max_poll_timeout_seconds + 5),
        ca_bundle=settings.max_ca_bundle_path,
        media_host_suffixes=settings.max_media_host_suffixes,
    )
    demo = build_demo_service(settings, provider_name="openai")
    store = MaxConversationStore(database)
    return MaxApplication(settings, database, demo, transport, store), transport, store


def run_polling(settings: Settings, stop_event: threading.Event) -> int:
    """Development/closed-smoke mode. Official production mode remains Webhook."""

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
    if not settings.max_poll_observe_only:
        application, client, store = build_max_application(settings, client)
    try:
        with SingleInstanceLock(settings.max_poll_lock_path):
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
                        if next_marker is not None:
                            marker = next_marker
                        store.set_marker(marker)
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
        client.close()
    LOGGER.info("MAX polling stopped")
    return 0
