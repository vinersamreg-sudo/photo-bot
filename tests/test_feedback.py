from __future__ import annotations

import hashlib
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from app.database import CURRENT_SCHEMA_VERSION, Database, schema_version
from app.domain import InvalidInputError
from app.feedback import MAX_FEEDBACK_MESSAGE_LENGTH, FeedbackService


class FeedbackTests(TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Database(Path(temporary.name) / "feedback.sqlite3")
        self.now = datetime(2026, 10, 4, 12, 30, tzinfo=timezone(timedelta(hours=4)))
        self.feedback = FeedbackService(self.database, clock=lambda: self.now)
        with self.database.transaction() as connection:
            for user_id in ("owner", "other"):
                connection.execute(
                    "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,?,?,?)",
                    (user_id, "test", user_id, self.now.isoformat()),
                )

    def _context(self, user_id: str = "owner", suffix: str = "one") -> tuple[str, str]:
        gallery_id = f"gallery-{user_id}"
        item_id = f"item-{suffix}"
        version_id = f"version-{suffix}"
        now = self.now.isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO galleries(id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
                (gallery_id, user_id, now, now),
            )
            connection.execute(
                """INSERT INTO gallery_items(
                       id,gallery_id,user_id,title,created_at,updated_at,
                       original_source_path,storage_root_path,retention_until
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (item_id, gallery_id, user_id, "Synthetic work", now, now,
                 "synthetic-source", "synthetic-root", now),
            )
            connection.execute(
                """INSERT INTO gallery_versions(
                       id,gallery_item_id,version_number,source_path,prompt,
                       effective_prompt,provider,model,created_at,status,rating
                   ) VALUES(?,?,1,?,?,?,?,?,?,?,4)""",
                (version_id, item_id, "synthetic-source", "synthetic prompt",
                 "synthetic prompt", "test", "test", now, "succeeded"),
            )
        return item_id, version_id

    def _rows(self) -> list[dict[str, object]]:
        with self.database.read() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM service_feedback")]

    def test_feedback_needs_no_photo_and_preserves_exact_unicode_and_whitespace(self) -> None:
        message = " \tОтзыв 💜\nе\u0301 — ё 中文\r\n\u2003"
        feedback_id = self.feedback.record_message(
            "owner", message, "main_menu", event_key="synthetic-max-message-id"
        )
        row = self._rows()[0]
        self.assertEqual(row["id"], feedback_id)
        self.assertEqual(row["user_id"], "owner")
        self.assertEqual(row["message"], message)
        self.assertEqual(row["created_at"], "2026-10-04T08:30:00+00:00")
        self.assertEqual(row["source_screen"], "main_menu")
        self.assertIsNone(row["gallery_item_id"])
        self.assertIsNone(row["version_id"])
        self.assertEqual(
            row["event_key"], hashlib.sha256(b"synthetic-max-message-id").hexdigest()
        )
        self.assertNotIn("synthetic-max-message-id", tuple(row.values()))
        with self.database.read() as connection:
            for table in ("gallery_items", "gallery_versions", "user_feedback", "version_feedback"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_exact_length_limit_is_accepted_and_overlong_text_is_not_truncated(self) -> None:
        self.feedback.record_message(
            "owner", "я" * MAX_FEEDBACK_MESSAGE_LENGTH, "main_menu", event_key="at-limit"
        )
        for message in ("", " \t\n\r\u2003", "я" * (MAX_FEEDBACK_MESSAGE_LENGTH + 1)):
            with self.subTest(length=len(message)), self.assertRaises(InvalidInputError):
                self.feedback.record_message("owner", message, "main_menu", event_key="invalid")
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(len(self._rows()[0]["message"]), MAX_FEEDBACK_MESSAGE_LENGTH)

    def test_replay_is_idempotent_and_conflicting_user_or_exact_body_fails_closed(self) -> None:
        feedback_id = self.feedback.record_message("owner", "  Текст\n", "gallery", event_key="one")
        original = self._rows()[0]
        self.now += timedelta(days=1)
        replay_id = self.feedback.record_message("owner", "  Текст\n", "main_menu", event_key="one")
        self.assertEqual(feedback_id, replay_id)
        self.assertEqual(self._rows(), [original])
        for user_id, body in (("other", "  Текст\n"), ("owner", "Текст"), ("owner", "changed")):
            with self.subTest(user=user_id, body=body), self.assertRaises(InvalidInputError):
                self.feedback.record_message(user_id, body, "main_menu", event_key="one")
        self.assertEqual(self._rows(), [original])

    def test_distinct_events_with_the_same_body_are_separate_feedback(self) -> None:
        first = self.feedback.record_message("owner", "Отзыв", "main_menu", event_key="first")
        second = self.feedback.record_message("owner", "Отзыв", "main_menu", event_key="second")
        self.assertNotEqual(first, second)
        self.assertEqual(len(self._rows()), 2)

    def test_has_event_is_read_only_and_scoped_to_exact_key_and_user(self) -> None:
        self.assertFalse(self.feedback.has_event("owner", "event"))
        self.feedback.record_message("owner", "Отзыв", "main_menu", event_key="event")
        original = self._rows()
        self.assertTrue(self.feedback.has_event("owner", "event"))
        self.assertFalse(self.feedback.has_event("other", "event"))
        self.assertFalse(self.feedback.has_event("missing-user", "event"))
        self.assertFalse(self.feedback.has_event("owner", "different-event"))
        self.assertEqual(self._rows(), original)
        with self.assertRaises(InvalidInputError):
            self.feedback.has_event("owner", " \t")

    def test_concurrent_replay_creates_one_row(self) -> None:
        def submit(_: int) -> str:
            return self.feedback.record_message("owner", "Отзыв", "main_menu", event_key="parallel")

        with ThreadPoolExecutor(max_workers=4) as executor:
            ids = list(executor.map(submit, range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self._rows()), 1)

    def test_optional_owned_context_is_consistent_and_version_only_infers_item(self) -> None:
        item_id, version_id = self._context()
        for key, context in (
            ("item-only", {"gallery_item_id": item_id}),
            ("version-only", {"version_id": version_id}),
            ("both", {"gallery_item_id": item_id, "version_id": version_id}),
        ):
            self.feedback.record_message("owner", "Отзыв", "result_ready", event_key=key, **context)
        rows = self._rows()
        self.assertTrue(all(row["gallery_item_id"] == item_id for row in rows))
        self.assertIsNone(rows[0]["version_id"])
        self.assertEqual([row["version_id"] for row in rows[1:]], [version_id, version_id])

    def test_unknown_owner_foreign_context_and_mismatched_context_write_nothing(self) -> None:
        owner_item, _ = self._context()
        _, owner_other_version = self._context(suffix="second")
        foreign_item, foreign_version = self._context("other", "foreign")
        cases = (
            ("missing-user", {}),
            ("owner", {"gallery_item_id": "missing-item"}),
            ("owner", {"version_id": "missing-version"}),
            ("owner", {"gallery_item_id": foreign_item}),
            ("owner", {"version_id": foreign_version}),
            ("owner", {"gallery_item_id": owner_item, "version_id": foreign_version}),
            ("owner", {"gallery_item_id": owner_item, "version_id": owner_other_version}),
        )
        for number, (user_id, context) in enumerate(cases):
            with self.subTest(number=number), self.assertRaises(InvalidInputError):
                self.feedback.record_message(
                    user_id, "Отзыв", "gallery", event_key=f"invalid-{number}", **context
                )
        self.assertEqual(self._rows(), [])

    def test_screen_and_event_key_must_be_valid_technical_values(self) -> None:
        for screen in ("", "Result", "main menu", "provider/prompt", "x" * 65):
            with self.subTest(screen=screen), self.assertRaises(InvalidInputError):
                self.feedback.record_message("owner", "Отзыв", screen, event_key="event")
        with self.assertRaises(InvalidInputError):
            self.feedback.record_message("owner", "Отзыв", "main_menu", event_key=" \t")
        self.assertEqual(self._rows(), [])

    def test_gallery_retention_nulls_context_but_preserves_feedback_and_replay(self) -> None:
        item_id, version_id = self._context()
        feedback_id = self.feedback.record_message(
            "owner", "  Отзыв 💜  ", "result_ready", event_key="retained",
            gallery_item_id=item_id, version_id=version_id,
        )
        before = self._rows()[0]
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM gallery_items WHERE id=?", (item_id,))
        after = self._rows()[0]
        self.assertEqual(after, {**before, "gallery_item_id": None, "version_id": None})
        self.assertEqual(
            self.feedback.record_message(
                "owner", "  Отзыв 💜  ", "result_ready", event_key="retained",
                gallery_item_id=item_id, version_id=version_id,
            ),
            feedback_id,
        )
        with self.database.read() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_version_retention_preserves_item_reference_and_feedback(self) -> None:
        item_id, version_id = self._context()
        self.feedback.record_message(
            "owner", "Отзыв", "result_ready", event_key="version-retained", version_id=version_id
        )
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM gallery_versions WHERE id=?", (version_id,))
        self.assertEqual(self._rows()[0]["gallery_item_id"], item_id)
        self.assertIsNone(self._rows()[0]["version_id"])

    def test_fresh_schema_contains_only_minimal_feedback_fields_and_foreign_keys(self) -> None:
        with self.database.read() as connection:
            self.assertEqual(schema_version(connection), CURRENT_SCHEMA_VERSION)
            self.assertEqual(CURRENT_SCHEMA_VERSION, 15)
            fields = {row[1] for row in connection.execute("PRAGMA table_info(service_feedback)")}
            foreign_keys = {
                row[3]: (row[2], row[4], row[6])
                for row in connection.execute("PRAGMA foreign_key_list(service_feedback)")
            }
        self.assertEqual(fields, {
            "id", "user_id", "message", "created_at", "source_screen",
            "gallery_item_id", "version_id", "event_key",
        })
        self.assertEqual(foreign_keys, {
            "user_id": ("users", "id", "CASCADE"),
            "gallery_item_id": ("gallery_items", "id", "SET NULL"),
            "version_id": ("gallery_versions", "id", "SET NULL"),
        })

    def test_v14_migration_is_additive_lossless_and_repeatable(self) -> None:
        _, version_id = self._context()
        now = self.now.isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO user_feedback(id,user_id,version_id,feedback_type,rating,created_at)
                   VALUES('legacy-rating','owner',?,'rating',2,?)""", (version_id, now),
            )
            connection.execute(
                """INSERT INTO user_feedback(id,user_id,version_id,feedback_type,message,created_at)
                   VALUES('legacy-comment','owner',?,'comment',?,?)""",
                (version_id, "  Старый отзыв 💜\n", now),
            )
            connection.execute(
                """INSERT INTO version_feedback(
                       version_id,user_id,sentiment,reason_category,created_at,updated_at
                   ) VALUES(?,'owner','negative','synthetic-reason',?,?)""", (version_id, now, now),
            )
            connection.execute("DROP TABLE service_feedback")
            connection.execute("DELETE FROM schema_migrations WHERE version=15")
        tables = ("users", "gallery_items", "gallery_versions", "user_feedback", "version_feedback")
        with self.database.read() as connection:
            self.assertEqual(schema_version(connection), 14)
            before = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in tables
            }
            definitions = {
                row["name"]: row["sql"] for row in connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE name IN ('user_feedback','version_feedback')"
                )
            }
        for _ in range(2):
            Database(self.database.path)
            with self.database.read() as connection:
                self.assertEqual(schema_version(connection), 15)
                for table in tables:
                    self.assertEqual(
                        [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")], before[table]
                    )
                self.assertEqual({
                    row["name"]: row["sql"] for row in connection.execute(
                        "SELECT name,sql FROM sqlite_master WHERE name IN ('user_feedback','version_feedback')"
                    )
                }, definitions)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM service_feedback").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.feedback.record_message("owner", "Новый отзыв", "main_menu", event_key="after-migration")
        Database(self.database.path)
        self.assertEqual(len(self._rows()), 1)
