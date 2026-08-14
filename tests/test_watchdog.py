import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.config import Settings
from app.database import Database
from app.watchdog import (
    alert_transport_from_environment,
    apply_auto_heal,
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
        self.database = Database(self.settings.database_path)
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
            "database": {"quick_check": "ok", "migration": 13},
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
            robokassa_available=True,
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
        broken = {**self.launch, "database": {"quick_check": "ok", "migration": 12}}
        critical = build_watchdog_report(
            self.settings,
            online=True,
            application_errors=[],
            launch=broken,
            payment_routes=self.routes,
            robokassa_available=True,
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

    def test_external_workflow_uses_safe_runtime_entrypoint(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "production-watchdog.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("*/10 * * * *", workflow)
        self.assertIn("scripts.production_watchdog", workflow)
        self.assertIn("--format human", workflow)
        self.assertNotIn("systemctl restart", workflow)
        self.assertNotIn("deploy", workflow.lower())

    def _report(
        self,
        *,
        launch: dict | None = None,
        robokassa_available: bool = True,
        application_errors: list[str] | None = None,
    ) -> dict:
        return build_watchdog_report(
            self.settings,
            online=True,
            application_errors=application_errors or [],
            launch=launch or self.launch,
            payment_routes=self.routes,
            robokassa_available=robokassa_available,
        )

    def _auto_heal(
        self,
        report: dict,
        *,
        restart,
        refresh=None,
        now: datetime | None = None,
    ) -> dict:
        current = now or datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        return apply_auto_heal(
            report,
            state_path=self.base / "watchdog-autoheal.json",
            restart_service=restart,
            refresh_report=refresh or self._report,
            sleep=lambda _seconds: None,
            now=lambda: current,
            readiness_attempts=1,
            readiness_delay_seconds=0,
        )

    def test_auto_heal_healthy_service_does_not_restart(self) -> None:
        restarts: list[str] = []
        result = self._auto_heal(
            self._report(),
            restart=lambda: restarts.append("restart") or True,
        )
        self.assertEqual(restarts, [])
        self.assertEqual(result["auto_heal"]["state"], "NOT_ELIGIBLE")

    def test_auto_heal_failed_service_restarts_once_and_recovers(self) -> None:
        broken = {**self.launch, "runtime": {"service_active": False}}
        restarts: list[str] = []
        result = self._auto_heal(
            self._report(launch=broken),
            restart=lambda: restarts.append("restart") or True,
        )
        self.assertEqual(restarts, ["restart"])
        self.assertEqual(result["status"], "RECOVERED")
        self.assertEqual(result["auto_heal"]["state"], "RECOVERED")

    def test_auto_heal_failed_restart_is_critical_without_second_attempt(self) -> None:
        broken = {**self.launch, "runtime": {"service_active": False}}
        restarts: list[str] = []
        result = self._auto_heal(
            self._report(launch=broken),
            restart=lambda: restarts.append("restart") or False,
        )
        self.assertEqual(restarts, ["restart"])
        self.assertEqual(result["status"], "CRITICAL")
        self.assertEqual(result["auto_heal"]["state"], "FAILED")

    def test_auto_heal_max_external_outage_never_restarts(self) -> None:
        broken = {
            **self.launch,
            "max": {"last_poll_age_hours": 1, "connected": False},
        }
        restarts: list[str] = []
        result = self._auto_heal(
            self._report(launch=broken),
            restart=lambda: restarts.append("restart") or True,
        )
        self.assertEqual(restarts, [])
        self.assertEqual(result["auto_heal"]["state"], "NOT_ELIGIBLE")

    def test_auto_heal_robokassa_outage_never_restarts(self) -> None:
        restarts: list[str] = []
        result = self._auto_heal(
            self._report(robokassa_available=False),
            restart=lambda: restarts.append("restart") or True,
        )
        self.assertEqual(restarts, [])
        self.assertEqual(result["auto_heal"]["state"], "NOT_ELIGIBLE")

    def test_auto_heal_payment_preparation_failure_never_restarts(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO product_events(event_type,created_at,error_type)
                   VALUES('payment_preparation_failed',?,?)""",
                (datetime.now(timezone.utc).isoformat(), "payment_prepare_version_missing"),
            )
        report = self._report()
        payment_check = next(
            check for check in report["checks"]
            if check["name"] == "payment_preparation"
        )
        self.assertEqual(payment_check["state"], "WARN")
        self.assertEqual(payment_check["detail"], "recent_failures=1")
        restarts: list[str] = []
        result = self._auto_heal(
            report,
            restart=lambda: restarts.append("restart") or True,
        )
        self.assertEqual(restarts, [])
        self.assertEqual(result["auto_heal"]["state"], "NOT_ELIGIBLE")

    def test_auto_heal_stale_polling_with_available_max_restarts_once(self) -> None:
        broken = {
            **self.launch,
            "max": {"last_poll_age_hours": 1, "connected": True},
        }
        restarts: list[str] = []
        result = self._auto_heal(
            self._report(launch=broken),
            restart=lambda: restarts.append("restart") or True,
        )
        self.assertEqual(restarts, ["restart"])
        self.assertEqual(result["status"], "RECOVERED")
        self.assertEqual(result["auto_heal"]["reason"], "max_polling_stale")

    def test_auto_heal_cooldown_prevents_restart_loop(self) -> None:
        current = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
        state_path = self.base / "watchdog-autoheal.json"
        state_path.write_text(
            json.dumps({
                "attempted_at": (current - timedelta(minutes=5)).isoformat(),
                "reason": "service_inactive",
                "result": "restart_failed",
            }),
            encoding="utf-8",
        )
        broken = {**self.launch, "runtime": {"service_active": False}}
        restarts: list[str] = []
        result = self._auto_heal(
            self._report(launch=broken),
            restart=lambda: restarts.append("restart") or True,
            now=current,
        )
        self.assertEqual(restarts, [])
        self.assertEqual(result["auto_heal"]["state"], "COOLDOWN")
        self.assertGreater(result["auto_heal"]["remaining_seconds"], 0)
