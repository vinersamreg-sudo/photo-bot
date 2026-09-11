import base64
import io
import json
import sqlite3
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import httpx
from PIL import Image

from app.config import Settings
from app.database import Database, ReadOnlyDatabase
from app.provider_switch_runtime import Runtime, database_snapshot, synthetic_smoke
from app.provider_router import select_image_provider
from tests.test_provider_switch import ENV


class ProviderOperationalSmokeTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = Settings("synthetic-key", "gpt-image-2", "test", self.root,
                                 gemini_api_key="synthetic-gemini", openai_max_retries=5)
        buffer = io.BytesIO()
        Image.new("RGB", (32, 32), "yellow").save(buffer, format="PNG")
        self.encoded = base64.b64encode(buffer.getvalue()).decode()

    def run_smoke(self, provider, *, status=200, invalid_image=False, error=None):
        requests = []
        original_client = httpx.Client

        def handler(request):
            requests.append(request)
            if error:
                raise error(request)
            encoded = base64.b64encode(b"not an image").decode() if invalid_image else self.encoded
            if provider == "openai":
                payload = {"created": 1, "data": [{"b64_json": encoded}]}
            else:
                payload = {"outputs": [{"type": "image", "data": encoded}]}
            if status != 200:
                payload = {"error": {"message": "synthetic-secret upstream response", "code": "bad"}}
            return httpx.Response(status, json=payload)

        class MockClient(original_client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        with patch("httpx.Client", MockClient):
            report = synthetic_smoke(replace(self.settings, image_provider=provider), temporary_root=self.root)
        self.assertEqual(list(self.root.iterdir()), [])  # No DB, gallery, retained photos or prompt.
        return report, requests

    def test_real_adapters_two_images_exact_unicode_order_and_zero_sdk_retries(self):
        for provider in ("openai", "gemini"):
            with self.subTest(provider=provider):
                result, requests = self.run_smoke(provider)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["requests"], 1)
                self.assertEqual(len(requests), 1)
                self.assertTrue(result["ordered_images"])
                self.assertTrue(result["exact_prompt"])
                self.assertEqual(result["http"], 200)
                self.assertNotIn("synthetic-key", json.dumps(result))

    def test_500_429_timeout_no_retries_no_raw_error_output(self):
        for provider in ("openai", "gemini"):
            for code in (500, 429):
                with self.subTest(provider=provider, code=code):
                    result, requests = self.run_smoke(provider, status=code)
                    self.assertFalse(result["ok"])
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(result["http"], code)
                    self.assertNotIn("synthetic-secret", json.dumps(result))
            result, requests = self.run_smoke(provider, error=lambda req: httpx.ReadTimeout("synthetic-secret", request=req))
            self.assertFalse(result["ok"])
            self.assertEqual(len(requests), 1)
            self.assertEqual(result["category"], "timeout")

    def test_invalid_image_fails_with_no_second_request(self):
        for provider in ("openai", "gemini"):
            result, requests = self.run_smoke(provider, invalid_image=True)
            self.assertFalse(result["ok"])
            self.assertEqual(len(requests), 1)

    def test_no_customer_business_pipeline_or_database_is_called(self):
        with patch("app.database.Database.__init__", side_effect=AssertionError("no customer DB")), \
                patch("app.database.ReadOnlyDatabase.connect", side_effect=AssertionError("no customer DB")):
            result, _ = self.run_smoke("gemini")
        self.assertTrue(result["ok"])

    def test_existing_disabled_processing_guard_is_not_bypassed(self):
        self.settings = replace(self.settings, openai_image_requests_enabled=False)
        result, requests = self.run_smoke("openai")
        self.assertFalse(result["ok"])
        self.assertEqual(requests, [])

    def test_changed_prompt_or_reversed_files_block_before_network(self):
        for provider in ("openai", "gemini"):
            for corruption in ("prompt", "order"):
                def corrupt(*args, **kwargs):
                    selected = select_image_provider(*args, **kwargs)
                    original = selected.provider.edit_many
                    selected.provider.edit_many = lambda paths, prompt: original(
                        tuple(reversed(paths)) if corruption == "order" else paths,
                        prompt + " hidden guard" if corruption == "prompt" else prompt)
                    return selected
                with self.subTest(provider=provider, corruption=corruption), \
                        patch("app.provider_switch_runtime.select_image_provider", side_effect=corrupt):
                    result, requests = self.run_smoke(provider)
                self.assertFalse(result["ok"])
                self.assertEqual(requests, [])


class ProviderHostBoundaryTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = Database(self.root / "data" / "photo_bot.sqlite3")
        self.environment = ENV + b"MAX_TRANSPORT_MODE=polling\nMAX_BOT_TOKEN=synthetic-max\n"
        (self.root / ".env").write_bytes(self.environment)
        (self.root / ".deploy-sha").write_text("a" * 40)
        (self.root / "data" / "deployed_commit.txt").write_text("a" * 40)
        now = datetime.now(timezone.utc)
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO max_transport_state(name,value,updated_at) VALUES(?,?,?)",
                               ("poll_last_success", "ok", now.isoformat()))
        self.runtime = Runtime(self.root)
        self.host = dict(active=True, pid=123, restarts=0, runtime_count=1, started=now.timestamp() - 60)
        self.values = self.runtime.environment().values()

    def snapshot(self, **host_overrides):
        with patch.object(self.runtime, "_host", return_value=(dict(self.host, **host_overrides), self.values.copy())):
            return self.runtime.snapshot(online=False)

    def test_snapshot_reads_actual_runtime_and_sqlite_read_only(self):
        before = self.database.path.read_bytes()
        with patch("app.database.Database.initialize", side_effect=AssertionError("no init")), \
                patch("app.provider_switch_runtime.MaxApiClient", side_effect=AssertionError("status no API")):
            result = self.snapshot()
        self.assertTrue(result["healthy"], result)
        self.assertTrue(result["idle"])
        self.assertEqual(result["schema"], 14)
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["model"], "gpt-image-2")
        self.assertEqual(self.database.path.read_bytes(), before)
        with ReadOnlyDatabase(self.database.path).read() as connection:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM max_transport_state")

    def test_runtime_config_drift_and_partial_deploy_fail_closed(self):
        self.values["IMAGE_PROVIDER"] = "gemini"
        result = self.snapshot()
        self.assertEqual(result["provider"], "gemini")  # Not the still-openai .env value.
        self.assertFalse(result["healthy"])
        self.assertFalse(result["config_matches_runtime"])
        self.values = self.runtime.environment().values()
        (self.root / ".deploy-sha").write_text("b" * 40)
        self.assertFalse(self.snapshot()["complete_deploy"])

    def test_sunburst_model_is_supported_by_runtime_health(self):
        target = "gpt-image-2.5-sunburst-2026-09-08"
        updated = self.environment.replace(b"gpt-image-2\r\n", target.encode() + b"\r\n")
        self.runtime.env_path.write_bytes(updated)
        self.values = self.runtime.environment().values()
        result = self.snapshot()
        self.assertTrue(result["healthy"], result)
        self.assertEqual(result["model"], target)

    def test_pre_restart_poll_timestamp_cannot_pass_new_runtime_readiness(self):
        result = self.snapshot(started=datetime.now(timezone.utc).timestamp() + 1)
        self.assertFalse(result["max_polling_fresh"])
        self.assertFalse(result["healthy"])

    def test_two_runtimes_or_inactive_service_fail_closed(self):
        self.assertFalse(self.snapshot(runtime_count=2)["healthy"])
        self.assertFalse(self.snapshot(active=False)["healthy"])

    def test_schema_mismatch_fails_without_migration(self):
        with self.database.transaction() as connection:
            connection.execute("UPDATE schema_migrations SET version=99 WHERE version=14")
        self.assertFalse(self.snapshot()["healthy"])
        self.assertEqual(database_snapshot(self.database.path)["schema"], 99)

    def test_real_api_probe_uses_only_get_me_and_closes_client(self):
        with patch.object(self.runtime, "_host", return_value=(self.host, self.values)), \
                patch("app.provider_switch_runtime.MaxApiClient") as client:
            client.return_value.last_status_code = 200
            result = self.runtime.snapshot(online=True)
        self.assertTrue(result["healthy"])
        client.return_value.get_me.assert_called_once_with()
        client.return_value.get_updates.assert_not_called()
        client.return_value.close.assert_called_once_with()

    def test_restart_command_is_only_main_bot(self):
        with patch("app.provider_switch_runtime._command") as command:
            self.runtime.restart()
        command.assert_called_once_with("systemctl", "restart", "photo-bot.service", timeout=60)

    def test_readiness_requires_new_pid_and_never_restarts_or_generates(self):
        status = self.snapshot()
        with patch.object(self.runtime, "snapshot", return_value=status), \
                patch.object(self.runtime, "restart") as restart, \
                patch.object(self.runtime, "smoke") as smoke:
            self.assertTrue(self.runtime.wait_healthy("openai", "gpt-image-2", previous_pid=10))
        restart.assert_not_called()
        smoke.assert_not_called()

    def test_auto_restart_loop_fails_readiness_immediately(self):
        status = dict(self.snapshot(), restarts=1)
        with patch.object(self.runtime, "snapshot", return_value=status), \
                patch.object(self.runtime, "restart") as restart:
            self.assertFalse(self.runtime.wait_healthy("openai", "gpt-image-2"))
        restart.assert_not_called()
