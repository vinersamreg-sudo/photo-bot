from __future__ import annotations

import hashlib
import http.client
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from app.admin_journal import AdminJournal, AdminJournalServer, mask_client_id
from app.database import Database


class AdminJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.users_root = self.root / "data" / "users"
        self.users_root.mkdir(parents=True)
        self.database_path = self.root / "data" / "photo_bot.sqlite3"
        self.database = Database(self.database_path)
        self.user_id = "internal-user-00000001"
        self.platform_id = "full-platform-id-must-not-leak"
        self.session_id = "session-1"
        now = datetime(2026, 8, 30, 7, 15, tzinfo=timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,?,?,?)",
                (self.user_id, "max", self.platform_id, now),
            )
            connection.execute(
                """INSERT INTO demo_sessions(
                       id,user_id,source_file_path,source_sha256,status,started_at,expires_at,
                       successful_generations,max_generations,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.session_id,
                    self.user_id,
                    "fixture",
                    "0" * 64,
                    "active",
                    now,
                    now,
                    0,
                    2,
                    now,
                    now,
                ),
            )
        self.journal = AdminJournal(self.database_path, self.users_root)
        self.server = AdminJournalServer(self.journal, "127.0.0.1", 0)

    def tearDown(self) -> None:
        self.server.stop()
        self.temporary.cleanup()

    @staticmethod
    def _image(path: Path, color: tuple[int, int, int]) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 18), color).save(path, format="JPEG")
        return path.read_bytes()

    def _attempt(
        self,
        attempt_id: str,
        *,
        status: str = "succeeded",
        source: Path | None = None,
        secondary: Path | None = None,
        preview: Path | None = None,
        original: Path | None = None,
        prompt: str = "Сделай фотографию чуть темнее",
        error_type: str | None = None,
        correction: bool = False,
        source_version_id: str | None = None,
    ) -> None:
        source_path = source or self.users_root / self.user_id / f"{attempt_id}-source.jpg"
        now = datetime(2026, 8, 30, 7, 15, tzinfo=timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO generation_attempts(
                       id,idempotency_key,session_id,user_id,prompt,status,started_at,completed_at,
                       provider,model,source_path,secondary_source_path,original_result_path,
                       demo_result_path,error_type,duration_ms,provider_http_status,correction,
                       source_version_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    f"idem-{attempt_id}",
                    self.session_id,
                    self.user_id,
                    prompt,
                    status,
                    now,
                    now,
                    "gemini",
                    "gemini-3-pro-image",
                    str(source_path),
                    str(secondary) if secondary else None,
                    str(original) if original else None,
                    str(preview) if preview else None,
                    error_type,
                    1350,
                    200 if status == "succeeded" else 500,
                    int(correction),
                    source_version_id,
                    now,
                ),
            )

    def _request(self, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
        if self.server._server is None:
            self.server.start()
        host, port = self.server.address
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, headers, body

    def test_database_is_query_only_and_reads_do_not_change_database_hash(self) -> None:
        self._attempt("attempt-read-only")
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        with self.journal.database.read() as connection:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM generation_attempts")
        self.journal.latest()
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(after, before)

    def test_missing_database_fails_closed_without_creating_it(self) -> None:
        missing = self.root / "absent" / "journal.sqlite3"
        isolated = AdminJournalServer(AdminJournal(missing, self.users_root), "127.0.0.1", 0)
        try:
            isolated.start()
            host, port = isolated.address
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 503)
            self.assertEqual(response.getheader("Cache-Control"), "no-store, max-age=0")
            connection.close()
            self.assertFalse(missing.exists())
        finally:
            isolated.stop()

    def test_masks_client_ids_and_does_not_render_provider_private_fields(self) -> None:
        source = self.users_root / self.user_id / "source.jpg"
        preview = self.users_root / self.user_id / "preview.jpg"
        original = self.users_root / self.user_id / "secret-original.png"
        self._image(source, (20, 30, 40))
        self._image(preview, (50, 60, 70))
        self._image(original, (80, 90, 100))
        self._attempt("attempt-mask", source=source, preview=preview, original=original)
        status, _, body = self._request("GET", "/")
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(mask_client_id(self.user_id), page)
        self.assertIn("30.08.2026 11:15:00", page)
        self.assertNotIn(self.user_id, page)
        self.assertNotIn(self.platform_id, page)
        self.assertNotIn("secret-original", page)
        detail = self._request("GET", "/attempt/attempt-mask")[2].decode("utf-8")
        self.assertIn("Provider prompt, request ID, usage JSON", detail)
        self.assertNotIn("secret-original", detail)

    def test_path_traversal_and_symlink_are_denied(self) -> None:
        outside = self.root / "outside.jpg"
        self._image(outside, (1, 2, 3))
        self._attempt("attempt-outside", source=outside)
        self.assertIsNone(self.journal.media("attempt-outside", "source"))
        self.assertEqual(self._request("GET", "/media/attempt-outside/source")[0], 404)

        link = self.users_root / self.user_id / "escape.jpg"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Symlink creation is unavailable on this platform")
        self._attempt("attempt-symlink", source=link)
        self.assertIsNone(self.journal.media("attempt-symlink", "source"))

    def test_missing_historical_files_render_without_failure(self) -> None:
        missing = self.users_root / self.user_id / "already-retained.jpg"
        self._attempt("attempt-missing", source=missing, preview=missing)
        status, _, body = self._request("GET", "/attempt/attempt-missing")
        self.assertEqual(status, 200)
        self.assertIn("Исторические media уже недоступны", body.decode("utf-8"))
        self.assertEqual(self._request("GET", "/media/attempt-missing/source")[0], 404)

    def test_failed_attempt_has_safe_category_and_no_result(self) -> None:
        source = self.users_root / self.user_id / "failed-source.jpg"
        preview = self.users_root / self.user_id / "must-not-serve.jpg"
        self._image(source, (10, 20, 30))
        self._image(preview, (30, 20, 10))
        self._attempt(
            "attempt-failed",
            status="failed_technical",
            source=source,
            preview=preview,
            error_type="ProviderTimeoutError",
        )
        attempt = self.journal.get("attempt-failed")
        self.assertEqual(attempt["status"], "ERROR")
        self.assertEqual(attempt["error_category"], "ProviderTimeoutError")
        self.assertFalse(attempt["preview_available"])
        self.assertEqual(self._request("GET", "/media/attempt-failed/preview")[0], 404)

    def test_second_source_is_visible_when_present(self) -> None:
        source = self.users_root / self.user_id / "source-1.jpg"
        secondary = self.users_root / self.user_id / "source-2.jpg"
        preview = self.users_root / self.user_id / "result.jpg"
        self._image(source, (10, 10, 10))
        expected = self._image(secondary, (20, 20, 20))
        self._image(preview, (30, 30, 30))
        self._attempt(
            "attempt-two-source",
            source=source,
            secondary=secondary,
            preview=preview,
        )
        attempt = self.journal.get("attempt-two-source")
        self.assertTrue(attempt["secondary_source_available"])
        status, headers, body = self._request(
            "GET", "/media/attempt-two-source/source-2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/jpeg")
        self.assertEqual(body, expected)

    def test_correction_source_uses_parent_watermarked_preview_not_original(self) -> None:
        original = self.users_root / self.user_id / "parent-original.png"
        watermarked = self.users_root / self.user_id / "parent-watermarked.jpg"
        original_bytes = self._image(original, (111, 10, 10))
        watermarked_bytes = self._image(watermarked, (10, 111, 10))
        now = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO galleries(id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
                ("gallery-1", self.user_id, now, now),
            )
            connection.execute(
                """INSERT INTO gallery_items(
                       id,gallery_id,user_id,title,created_at,updated_at,original_source_path,
                       storage_root_path,retention_until)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "item-1",
                    "gallery-1",
                    self.user_id,
                    "Synthetic work",
                    now,
                    now,
                    str(original),
                    str(self.users_root / self.user_id / "gallery" / "item-1"),
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO gallery_versions(
                       id,gallery_item_id,version_number,source_path,prompt,effective_prompt,
                       provider,model,preview_watermarked_path,original_path,created_at,status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "version-parent",
                    "item-1",
                    1,
                    str(original),
                    "Synthetic",
                    "Synthetic",
                    "gemini",
                    "gemini-3-pro-image",
                    str(watermarked),
                    str(original),
                    now,
                    "succeeded",
                ),
            )
        self._attempt(
            "attempt-correction",
            source=original,
            correction=True,
            source_version_id="version-parent",
        )
        media = self.journal.media("attempt-correction", "source")
        self.assertIsNotNone(media)
        self.assertEqual(media.path.read_bytes(), watermarked_bytes)
        self.assertNotEqual(media.path.read_bytes(), original_bytes)
        attempt = self.journal.get("attempt-correction")
        self.assertEqual(attempt["source_label"], "Предыдущая версия (watermarked)")

    def test_all_responses_are_no_store(self) -> None:
        self._attempt("attempt-cache")
        for path in ("/", "/attempt/attempt-cache", "/missing"):
            _, headers, _ = self._request("GET", path)
            self.assertEqual(headers["cache-control"], "no-store, max-age=0")
            self.assertEqual(headers["pragma"], "no-cache")

    def test_admin_binds_only_loopback_and_unit_has_loopback_exec(self) -> None:
        with self.assertRaises(ValueError):
            AdminJournalServer(self.journal, "0.0.0.0", 8092)
        unit = (Path(__file__).parents[1] / "ops" / "ravuna-admin-journal.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("--host 127.0.0.1", unit)
        self.assertIn("IPAddressDeny=any", unit)
        self.assertIn("IPAddressAllow=localhost", unit)
        self.assertNotIn("0.0.0.0", unit)
        script = (Path(__file__).parents[1] / "scripts" / "run_admin_journal.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("load_settings", script)
        self.assertNotIn(".env", script)
        for nginx_candidate in (Path(__file__).parents[1] / "ops").glob("*.conf"):
            nginx_text = nginx_candidate.read_text(encoding="utf-8")
            self.assertNotIn("8092", nginx_text)
            self.assertNotIn("ravuna-admin-journal", nginx_text)

    def test_mutation_methods_are_rejected_without_database_changes(self) -> None:
        self._attempt("attempt-no-mutation")
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, headers, _ = self._request(method, "/attempt/attempt-no-mutation")
            self.assertEqual(status, 405)
            self.assertEqual(headers["cache-control"], "no-store, max-age=0")
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(after, before)

    def test_list_is_capped_at_latest_one_hundred(self) -> None:
        for index in range(105):
            self._attempt(f"attempt-{index:03d}", prompt=f"Synthetic prompt {index}")
        attempts = self.journal.latest(500)
        self.assertEqual(len(attempts), 100)
        self.assertEqual(attempts[0]["id"], "attempt-104")


if __name__ == "__main__":
    unittest.main()
