import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase

from app.database import Database
from app.domain import InvalidInputError
from app.max_conversation import MaxConversationStore


class MaxConversationTests(TestCase):
    def test_legal_versions_dedup_marker_and_state_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.sqlite3")
            store = MaxConversationStore(database)
            dialog = store.get_or_create("u1", "c1")
            self.assertEqual(dialog.state, "new_user")
            store.transition("u1", "legal_required", event_key="start")
            self.assertFalse(store.legal_is_current("u1"))
            store.accept_required_documents("u1")
            self.assertTrue(store.legal_is_current("u1"))
            store.transition("u1", "main_menu", event_key="legal")
            with self.assertRaises(InvalidInputError):
                store.transition("u1", "processing")

            self.assertTrue(store.begin_event("message:1", "message_created"))
            self.assertFalse(store.begin_event("message:1", "message_created"))
            store.finish_event("message:1", True)
            self.assertFalse(store.begin_event("message:1", "message_created"))
            self.assertTrue(store.begin_event("callback:1", "message_callback"))
            store.finish_event("callback:1", False)
            self.assertTrue(store.begin_event("callback:1", "message_callback"))

            store.set_marker(123)
            self.assertEqual(store.get_marker(), 123)
            store.touch_poll_success()
            self.assertEqual(store.transport_state("poll_last_success")[0], "ok")
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE max_legal_documents SET active=0 WHERE document_type='offer'"
                )
                connection.execute(
                    """INSERT INTO max_legal_documents(
                           document_type,version,required,draft,active,created_at
                       ) VALUES('offer','2026-08-draft-2',1,1,1,?)""",
                    (datetime.now(timezone.utc).isoformat(),),
                )
            self.assertFalse(store.legal_is_current("u1"))

    def test_restart_recovers_processing_dialog_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path)
            store = MaxConversationStore(database)
            store.get_or_create("u1", "c1")
            store.transition("u1", "processing", force=True)
            store.begin_event("message:processing", "message_created")
            restarted = Database(path)
            recovered = MaxConversationStore(restarted)
            self.assertEqual(recovered.get("u1").state, "confirmation")
            self.assertTrue(
                recovered.begin_event("message:processing", "message_created")
            )
