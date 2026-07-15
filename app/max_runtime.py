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
        timeout_seconds=settings.generation_timeout_seconds,
        media_host_suffixes=settings.max_media_host_suffixes,
    )
    demo = build_demo_service(settings, provider_name="openai")
    store = MaxConversationStore(database)
    return MaxApplication(settings, database, demo, transport, store), transport, store


def run_polling(settings: Settings, stop_event: threading.Event) -> int:
    """Development/closed-smoke mode. Official production mode remains Webhook."""

    application, client, store = build_max_application(settings)
    try:
        with SingleInstanceLock(settings.max_poll_lock_path):
            marker = store.get_marker()
            LOGGER.info("MAX closed-test polling started")
            while not stop_event.is_set():
                try:
                    updates, next_marker = client.get_updates(
                        marker, timeout=settings.max_poll_timeout_seconds
                    )
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
                        stop_event.wait(2)
                except MaxTransportError as exc:
                    LOGGER.error("MAX polling transport failure: %s", exc)
                    stop_event.wait(5)
    finally:
        client.close()
    LOGGER.info("MAX polling stopped")
    return 0
