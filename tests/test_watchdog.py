import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.config import Settings
from app.database import Database
from app.watchdog import (
    alert_transport_from_environment,
    build_watchdog_report,
    render_watchdog,
)


class WatchdogTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        for name in ("data", "logs", "temp"):
            (self.base / name).mkdir()
        self.settings = Settings(
            "", "gpt-image-2", "test", self.base,
            image_provider="gemini",
            gemini_api_key="gemini-secret",
            max_poll_max_stale_seconds=90,
            disk_min_free_mb=1,
            payment_result_url="https://ravuna.ru/payments/robokassa/result",
        )
        Database(self.settings.database_path)
        now = datetime.now(timezone.utc).isoformat()
        self.settings.backup_dir_path.mkdir(parents=True)
        recovery_name = "ravuna-recovery-synthetic.tar.gz.enc"
        for name, value in (
            (
                "latest_recovery.json",
                {"recovery_bundle_name": recovery_name, "created_at": now},
            ),
            (
                "recovery_restore_status.json",
                {"backup_name": recovery_name, "overall": "PASS"},
            ),
            (
                "recovery_offsite_status.json",
                {"backup_name": recovery_name, "copied_at": now},
            ),
        ):
            (self.settings.backup_dir_path / name).write_text(
                json.dumps(value), encoding="utf-8"
            )
        self.launch = {
            "runtime": {"service_active": True},
            "database": {"quick_check": "ok", "migration": 12},
            "processing": {"active_attempts": 0, "active_dialogs": 0},
            "cleanup": {"remaining_orphan_count": 0},
            "backup": {
                "latest_age_hours": 1,
                "restore_tested": True,
                "offsite_copied": True,
            },
            "provider": {
                "name": "gemini",
                "model": "gemini-3-pro-image",
                "configured": True,
            },
            "max": {"last_poll_age_hours": 0.001, "connected": True},
            "site": {"required_routes_ok": True},
            "storage": {"free_mb": 4096, "minimum_free_mb": 1},
        }
        self.routes = {
            "short": 404,
            "success": 404,
            "fail": 404,
            "result": 405,
        }

    def test_watchdog_is_compact_privacy_safe_and_healthy(self) -> None:
        before_sha = hashlib.sha256(
            self.settings.database_path.read_bytes()
        ).hexdigest()
        report = build_watchdog_report(
            self.settings,
            online=True,
            application_errors=[],
            launch=self.launch,
            payment_routes=self.routes,
        )
        rendered = render_watchdog(report)
        after_sha = hashlib.sha256(
            self.settings.database_path.read_bytes()
        ).hexdigest()
        self.assertEqual(report["status"], "HEALTHY")
        self.assertEqual(after_sha, before_sha)
        self.assertEqual(report["exit_code"], 0)
        self.assertLessEqual(len(rendered.splitlines()), 20)
        self.assertNotIn("gemini-secret", json.dumps(report))
        self.assertIn("payment_routes=PASS", rendered)
        self.assertIn("provider_configuration=PASS", rendered)
        self.assertNotIn("provider_availability", rendered)

    def test_watchdog_maps_warning_and_critical_to_exit_codes(self) -> None:
        warning = build_watchdog_report(
            self.settings,
            online=False,
            application_errors=[],
            launch=self.launch,
        )
        self.assertEqual(warning["status"], "WARNING")
        self.assertEqual(warning["exit_code"], 1)
        broken = {**self.launch, "database": {"quick_check": "ok", "migration": 11}}
        critical = build_watchdog_report(
            self.settings,
            online=True,
            application_errors=[],
            launch=broken,
            payment_routes=self.routes,
        )
        self.assertEqual(critical["status"], "CRITICAL")
        self.assertEqual(critical["exit_code"], 2)

    def test_alert_transport_is_optional_and_requires_https(self) -> None:
        self.assertFalse(alert_transport_from_environment({}).configured)
        configured = alert_transport_from_environment(
            {"RAVUNA_ALERT_WEBHOOK_URL": "https://alerts.example.test/private"}
        )
        self.assertTrue(configured.configured)
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            alert_transport_from_environment(
                {"RAVUNA_ALERT_WEBHOOK_URL": "http://alerts.example.test/private"}
            )
        with patch("app.watchdog.httpx.post") as post:
            post.return_value.raise_for_status.return_value = None
            configured.send({
                "status": "CRITICAL",
                "checked_at": "2026-08-09T00:00:00+00:00",
                "checks": [
                    {"name": "sqlite", "state": "FAIL", "detail": "private"}
                ],
            })
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["attention"], ["sqlite"])
        self.assertNotIn("private", json.dumps(payload))

    def test_external_workflow_is_read_only_and_does_not_send_alerts_by_default(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "production-watchdog.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("*/10 * * * *", workflow)
        self.assertIn("scripts.production_watchdog --format human", workflow)
        self.assertNotIn("--notify", workflow)
        self.assertNotIn("systemctl restart", workflow)
        self.assertNotIn("deploy", workflow.lower())
