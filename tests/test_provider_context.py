from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.config import Settings
from app.database import Database
from app.domain import InvalidInputError, ProviderResult
from app.provider_context import ProviderContextService


UTC = timezone.utc


class FakeGateway:
    def __init__(self, *, fail_delete: bool = False) -> None:
        self.fail_delete = fail_delete
        self.created = 0
        self.responses: list[str] = []
        self.conversations: list[str] = []

    def create_conversation(self) -> str:
        self.created += 1
        return f"conversation-{self.created}"

    def delete_response(self, response_id: str) -> None:
        if self.fail_delete:
            raise RuntimeError("temporary provider failure")
        self.responses.append(response_id)

    def delete_conversation(self, conversation_id: str) -> None:
        if self.fail_delete:
            raise RuntimeError("temporary provider failure")
        self.conversations.append(conversation_id)


class ProviderContextServiceTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
        self.settings = Settings(
            "test-key",
            "gpt-image-2",
            "test",
            self.root,
            openai_conversation_memory_enabled=True,
            openai_responses_image_enabled=True,
            openai_conversation_retention_enabled=True,
            openai_context_max_idle_days=14,
            openai_context_retention_days=30,
            openai_context_max_depth=3,
        )
        self.database = Database(self.settings.database_path)
        self.gateway = FakeGateway()
        self.service = ProviderContextService(
            self.settings, self.database, self.gateway, clock=lambda: self.now
        )
        self._insert_work("u1", "g1", "item1", "v1", response_id=None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _insert_work(
        self,
        user_id: str,
        gallery_id: str,
        item_id: str,
        version_id: str,
        *,
        response_id: str | None,
        depth: int = 0,
    ) -> None:
        stamp = self.now.isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO users(id,platform,platform_user_id,created_at) VALUES(?,?,?,?)",
                (user_id, "test", user_id, stamp),
            )
            connection.execute(
                "INSERT OR IGNORE INTO galleries(id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
                (gallery_id, user_id, stamp, stamp),
            )
            connection.execute(
                """INSERT OR IGNORE INTO gallery_items(
                       id,gallery_id,user_id,title,created_at,updated_at,original_source_path,
                       storage_root_path,retention_until
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    item_id,
                    gallery_id,
                    user_id,
                    "work",
                    stamp,
                    stamp,
                    str(self.root / f"{item_id}.png"),
                    str(self.root / item_id),
                    (self.now + timedelta(days=30)).isoformat(),
                ),
            )
            connection.execute(
                """INSERT OR IGNORE INTO gallery_versions(
                       id,gallery_item_id,version_number,source_path,prompt,effective_prompt,
                       provider,model,created_at,status,provider_response_id,context_depth
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    item_id,
                    1,
                    str(self.root / f"{item_id}.png"),
                    "prompt",
                    "prompt",
                    "openai",
                    "gpt-image-2",
                    stamp,
                    "succeeded",
                    response_id,
                    depth,
                ),
            )

    def _parent(self, version_id: str = "v1"):
        with self.database.read() as connection:
            return connection.execute(
                "SELECT * FROM gallery_versions WHERE id=?", (version_id,)
            ).fetchone()

    def _prepare(self, *, parent=None, correction=False, repeat=False, service=None):
        with self.database.transaction() as connection:
            return (service or self.service).prepare(
                connection,
                gallery_item_id="item1",
                user_id="u1",
                parent=parent,
                correction=correction,
                repeat=repeat,
            )

    def test_01_flags_off_preserve_stateless_behavior(self) -> None:
        service = ProviderContextService(
            replace(self.settings, openai_conversation_memory_enabled=False),
            self.database,
            self.gateway,
        )
        plan = self._prepare(service=service)
        self.assertEqual((plan.mode, plan.reason), ("stateless", "memory_disabled"))
        self.assertIsNone(self.service.get_context("item1"))

    def test_02_responses_flag_off_is_stateless(self) -> None:
        service = ProviderContextService(
            replace(self.settings, openai_responses_image_enabled=False), self.database
        )
        self.assertEqual(self._prepare(service=service).reason, "responses_disabled")

    def test_03_retention_flag_off_is_stateless(self) -> None:
        service = ProviderContextService(
            replace(self.settings, openai_conversation_retention_enabled=False), self.database
        )
        self.assertEqual(self._prepare(service=service).reason, "retention_disabled")

    def test_04_enabled_initial_edit_creates_local_context(self) -> None:
        plan = self._prepare()
        self.assertEqual((plan.mode, plan.request.depth), ("responses", 1))
        self.assertEqual(plan.reason, "new_context")
        self.assertIsNotNone(self.service.get_context("item1", user_id="u1"))

    def test_05_correction_without_parent_response_is_stateless(self) -> None:
        plan = self._prepare(parent=self._parent(), correction=True)
        self.assertEqual(plan.reason, "missing_parent_response_id")
        self.assertIsNone(plan.request)

    def test_06_correction_uses_selected_parent_response(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE gallery_versions SET provider_response_id='resp-parent',context_depth=1 WHERE id='v1'"
            )
        self._prepare()  # creates the logical context
        plan = self._prepare(parent=self._parent(), correction=True)
        self.assertEqual(plan.request.previous_response_id, "resp-parent")
        self.assertEqual(plan.request.depth, 2)

    def test_07_repeat_never_uses_context(self) -> None:
        plan = self._prepare(parent=self._parent(), repeat=True)
        self.assertEqual((plan.mode, plan.reason), ("stateless", "repeat_is_stateless"))

    def test_08_prepare_reuses_one_context_per_gallery_item(self) -> None:
        first = self._prepare().request.context_id
        second = self._prepare().request.context_id
        self.assertEqual(first, second)

    def test_09_branch_uses_parent_not_global_last_response(self) -> None:
        self._prepare()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_contexts SET last_response_id='global-latest' WHERE gallery_item_id='item1'"
            )
            connection.execute(
                "UPDATE gallery_versions SET provider_response_id='branch-parent',context_depth=1 WHERE id='v1'"
            )
        plan = self._prepare(parent=self._parent(), correction=True)
        self.assertEqual(plan.request.previous_response_id, "branch-parent")

    def test_10_depth_limit_resets_chain(self) -> None:
        self._prepare()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE gallery_versions SET provider_response_id='old',context_depth=3 WHERE id='v1'"
            )
        plan = self._prepare(parent=self._parent(), correction=True)
        self.assertIsNone(plan.request.previous_response_id)
        self.assertEqual((plan.request.depth, plan.reason), (1, "max_depth_reset"))

    def test_11_idle_context_falls_back_to_stateless(self) -> None:
        self._prepare()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_contexts SET updated_at=? WHERE gallery_item_id='item1'",
                ((self.now - timedelta(days=15)).isoformat(),),
            )
            connection.execute(
                "UPDATE gallery_versions SET provider_response_id='old' WHERE id='v1'"
            )
        plan = self._prepare(parent=self._parent(), correction=True)
        self.assertEqual(plan.reason, "context_idle_expired")

    def test_12_deleted_context_falls_back_to_stateless(self) -> None:
        self._prepare()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_contexts SET status='deleted' WHERE gallery_item_id='item1'"
            )
            connection.execute(
                "UPDATE gallery_versions SET provider_response_id='old' WHERE id='v1'"
            )
        self.assertEqual(
            self._prepare(parent=self._parent(), correction=True).reason,
            "context_not_active",
        )

    def test_13_explicit_reset_clears_response_and_depth(self) -> None:
        self._prepare()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_contexts SET last_response_id='x',depth=2 WHERE gallery_item_id='item1'"
            )
        self.service.reset_context("item1", user_id="u1")
        row = self.service.get_context("item1", user_id="u1")
        self.assertEqual((row["last_response_id"], row["depth"], row["reset_count"]), (None, 0, 1))

    def test_14_cross_user_context_read_is_rejected(self) -> None:
        self._prepare()
        with self.assertRaises(InvalidInputError):
            self.service.get_context("item1", user_id="u2")

    def test_15_cross_user_context_reset_is_rejected(self) -> None:
        self._prepare()
        with self.assertRaises(InvalidInputError):
            self.service.reset_context("item1", user_id="u2")

    def test_16_finalize_responses_saves_response_id_and_depth(self) -> None:
        plan = self._prepare()
        result = ProviderResult(
            b"image", provider_mode="responses", provider_response_id="resp-1", context_depth=1
        )
        with self.database.transaction() as connection:
            self.service.finalize(
                connection, plan=plan, result=result, attempt_id="a1", gallery_item_id="item1"
            )
        row = self.service.get_context("item1")
        self.assertEqual((row["last_response_id"], row["depth"]), ("resp-1", 1))

    def test_17_finalize_fallback_increments_counter(self) -> None:
        plan = self._prepare()
        result = ProviderResult(
            b"image",
            context_fallback_used=True,
            context_fallback_reason="ProviderTimeoutError",
        )
        with self.database.transaction() as connection:
            self.service.finalize(
                connection, plan=plan, result=result, attempt_id="a1", gallery_item_id="item1"
            )
        self.assertEqual(self.service.get_context("item1")["fallback_count"], 1)

    def test_18_record_failure_contains_only_error_class(self) -> None:
        plan = self._prepare()
        self.service.record_failure(
            plan, attempt_id="a1", gallery_item_id="item1", error=RuntimeError("private text")
        )
        with self.database.read() as connection:
            event = connection.execute(
                "SELECT * FROM provider_context_events WHERE event_type='context_failed'"
            ).fetchone()
        self.assertEqual(event["error_class"], "RuntimeError")
        self.assertNotIn("private text", tuple(event))

    def test_19_remote_conversation_id_is_stored_without_payload(self) -> None:
        context_id = self._prepare().request.context_id
        conversation_id = self.service.create_remote_conversation(context_id)
        self.assertEqual(conversation_id, "conversation-1")
        self.assertEqual(self.service.get_context("item1")["provider_conversation_id"], conversation_id)

    def test_20_remote_conversation_requires_gateway(self) -> None:
        context_id = self._prepare().request.context_id
        service = ProviderContextService(self.settings, self.database, None)
        with self.assertRaises(RuntimeError):
            service.create_remote_conversation(context_id)

    def test_21_gallery_delete_removes_all_remote_ids(self) -> None:
        plan = self._prepare()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE gallery_versions SET provider_response_id='resp-v1' WHERE id='v1'"
            )
            connection.execute(
                """UPDATE provider_contexts SET last_response_id='resp-last',
                          provider_conversation_id='conv-1' WHERE id=?""",
                (plan.request.context_id,),
            )
        self.assertTrue(self.service.delete_for_gallery("item1"))
        self.assertEqual(set(self.gateway.responses), {"resp-v1", "resp-last"})
        self.assertEqual(self.gateway.conversations, ["conv-1"])

    def test_22_delete_without_remote_ids_completes_locally(self) -> None:
        self._prepare()
        self.assertTrue(self.service.delete_for_gallery("item1"))
        self.assertEqual(self.service.get_context("item1")["status"], "deleted")

    def test_23_delete_failure_leaves_retryable_tombstone(self) -> None:
        plan = self._prepare()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_contexts SET last_response_id='resp' WHERE id=?",
                (plan.request.context_id,),
            )
        service = ProviderContextService(
            self.settings, self.database, FakeGateway(fail_delete=True), clock=lambda: self.now
        )
        self.assertFalse(service.delete_for_gallery("item1"))
        self.assertEqual(service.get_context("item1")["status"], "delete_pending")
        with self.database.read() as connection:
            events = connection.execute(
                "SELECT COUNT(*) FROM provider_context_events WHERE event_type='context_delete_failure'"
            ).fetchone()[0]
        self.assertEqual(events, 1)

    def test_24_no_gateway_with_remote_ids_leaves_pending(self) -> None:
        plan = self._prepare()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_contexts SET last_response_id='resp' WHERE id=?",
                (plan.request.context_id,),
            )
        service = ProviderContextService(self.settings, self.database, None, clock=lambda: self.now)
        self.assertFalse(service.delete_for_gallery("item1"))
        self.assertEqual(service.get_context("item1")["last_error_class"], "gateway_unavailable")

    def test_25_cleanup_dry_run_does_not_delete(self) -> None:
        self._prepare()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_contexts SET expires_at=? WHERE gallery_item_id='item1'",
                ((self.now - timedelta(days=1)).isoformat(),),
            )
        self.assertEqual(self.service.cleanup_due(execute=False), ["item1"])
        self.assertEqual(self.service.get_context("item1")["status"], "active")

    def test_26_cleanup_apply_deletes_due_context(self) -> None:
        self._prepare()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_contexts SET expires_at=? WHERE gallery_item_id='item1'",
                ((self.now - timedelta(days=1)).isoformat(),),
            )
        self.service.cleanup_due(execute=True)
        self.assertEqual(self.service.get_context("item1")["status"], "deleted")

    def test_27_retention_keeps_fresh_context(self) -> None:
        self._prepare()
        self.assertEqual(self.service.cleanup_due(execute=False), [])

    def test_28_delete_for_user_isolated_to_owned_contexts(self) -> None:
        self._insert_work("u2", "g2", "item2", "v2", response_id=None)
        self._prepare()
        with self.database.transaction() as connection:
            self.service.prepare(
                connection,
                gallery_item_id="item2",
                user_id="u2",
                parent=None,
                correction=False,
                repeat=False,
            )
        result = self.service.delete_for_user("u1")
        self.assertEqual(result, {"item1": True})
        self.assertEqual(self.service.get_context("item2")["status"], "active")

    def test_29_fallback_plan_never_carries_provider_ids(self) -> None:
        plan = self.service.fallback_to_stateless("broken_context")
        self.assertEqual((plan.mode, plan.reason, plan.request), ("stateless", "broken_context", None))
        self.assertTrue(plan.fallback)

    def test_30_context_table_stores_no_key_prompt_or_image_columns(self) -> None:
        with self.database.read() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_contexts)")}
        self.assertFalse(columns & {"api_key", "prompt", "image", "signed_url", "raw_response"})

    def test_31_schema_migration_seven_is_recorded(self) -> None:
        with self.database.read() as connection:
            row = connection.execute("SELECT name FROM schema_migrations WHERE version=7").fetchone()
        self.assertEqual(row[0], "optional_openai_provider_context")

    def test_32_context_event_telemetry_has_no_free_text_column(self) -> None:
        with self.database.read() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_context_events)")}
        self.assertNotIn("prompt", columns)
        self.assertNotIn("payload", columns)

    def test_33_reinitializing_version_six_database_applies_context_migration(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=7")
            connection.execute("DROP TABLE provider_context_events")
            connection.execute("DROP TABLE provider_contexts")
        migrated = Database(self.settings.database_path)
        with migrated.read() as connection:
            migration = connection.execute(
                "SELECT name FROM schema_migrations WHERE version=7"
            ).fetchone()
            legacy = connection.execute(
                "SELECT id,provider_mode FROM gallery_versions WHERE id='v1'"
            ).fetchone()
            context_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'provider_context%'"
                )
            }
        self.assertEqual(migration[0], "optional_openai_provider_context")
        self.assertEqual((legacy["id"], legacy["provider_mode"]), ("v1", "stateless"))
        self.assertEqual(
            context_tables,
            {"provider_contexts", "provider_context_events"},
        )
