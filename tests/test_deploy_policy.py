from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
SERVICE = ROOT / "ops" / "photo-bot.service"


class DeployPolicyTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_supports_manual_deploy(self) -> None:
        self.assertIn("workflow_dispatch:", self.workflow)

    def test_preserves_runtime_state(self) -> None:
        for protected_path in (".env", "venv/", "data/", "logs/", "temp/"):
            self.assertIn(f"--exclude='{protected_path}'", self.workflow)
        self.assertIn("--exclude='site/'", self.workflow)

    def test_uses_scoped_safe_operations(self) -> None:
        self.assertNotIn("pkill", self.workflow)
        self.assertIn("/opt/photo-bot", self.workflow)
        self.assertNotIn("REG_RU_", self.workflow)
        self.assertNotIn("trip-day", self.workflow.lower())
        self.assertIn("secrets.HETZNER_HOST", self.workflow)
        self.assertIn('chmod +x "$ROOT"/scripts/*.sh', self.workflow)
        self.assertIn('cd "$ROOT"', self.workflow)
        self.assertIn('"$ROOT"/venv/bin/pip install', self.workflow)
        self.assertIn('"$ROOT"/venv/bin/pip check', self.workflow)
        self.assertIn("Database(load_settings().database_path)", self.workflow)
        self.assertIn('"$ROOT"/scripts/healthcheck.sh', self.workflow)
        self.assertIn("sudo -n systemctl", self.workflow)
        self.assertNotIn("sudo", SERVICE.read_text(encoding="utf-8"))

    def test_verifies_log_safety_and_tripday_isolation(self) -> None:
        self.assertIn("scripts.check_runtime_secrets", self.workflow)
        self.assertIn("MAX_BOT_TOKEN", self.workflow)
        self.assertNotIn("TripDay", self.workflow)

    def test_uses_python_312_and_scans_for_secrets(self) -> None:
        self.assertIn('python-version: "3.12"', self.workflow)
        self.assertIn("python scripts/scan_secrets.py", self.workflow)

    def test_records_deployed_commit_after_healthcheck(self) -> None:
        health_position = self.workflow.rindex('"$ROOT"/scripts/healthcheck.sh')
        marker_position = self.workflow.rindex("data/deployed_commit.txt")
        openai_position = self.workflow.rindex("python -m app.main openai-check")
        self.assertGreater(marker_position, health_position)
        self.assertGreater(marker_position, openai_position)

    def test_creates_but_does_not_overwrite_production_env(self) -> None:
        self.assertIn('if [ ! -f "$ROOT/.env" ]', self.workflow)
        self.assertIn('install -m 600 /dev/null "$ROOT/.env"', self.workflow)
        self.assertNotIn("'OPENAI_API_KEY=", self.workflow)
        self.assertIn("OPENAI_IMAGE_MODEL=gpt-image-2", self.workflow)
        self.assertIn("APP_ENV=production", self.workflow)
        self.assertIn("ensure_env DEMO_MAX_SUCCESSFUL_GENERATIONS 5", self.workflow)
        self.assertIn("ensure_env GLOBAL_MAX_CONCURRENT_GENERATIONS 2", self.workflow)
        self.assertIn("ensure_env DEMO_RETENTION_DAYS 30", self.workflow)
        self.assertIn("ensure_env PAID_RETENTION_DAYS 180", self.workflow)
        self.assertIn("ensure_env TRASH_RETENTION_DAYS 30", self.workflow)

    def test_transfers_openai_key_via_stdin_and_checks_authorization(self) -> None:
        self.assertIn("Validate OpenAI credential from GitHub runner", self.workflow)
        self.assertIn("Configure OpenAI credential", self.workflow)
        self.assertIn("secrets.OPENAI_API_KEY", self.workflow)
        self.assertIn('printf \'%s\' "$OPENAI_API_KEY" |', self.workflow)
        self.assertIn("python -m app.main openai-check", self.workflow)

    def test_configures_max_without_exposing_the_token_as_an_argument(self) -> None:
        self.assertIn("Configure MAX credential", self.workflow)
        self.assertIn("secrets.MAX_BOT_TOKEN", self.workflow)
        self.assertIn('printf \'%s\' "$MAX_BOT_TOKEN" |', self.workflow)
        self.assertIn("set_env MAX_TRANSPORT_MODE polling", self.workflow)
        self.assertIn("set_env MAX_POLL_OBSERVE_ONLY true", self.workflow)
        self.assertIn("ensure_env MAX_API_BASE_URL https://platform-api2.max.ru", self.workflow)
        self.assertIn("ensure_env MAX_CA_BUNDLE ops/certs/russian_trusted_root_ca_pem.crt", self.workflow)
        self.assertIn("/opt/photo-bot/scripts/stop_bot.sh", self.workflow)
        self.assertIn('if [ "$MODE" = polling ]', self.workflow)
        self.assertIn("python -m app.main max-check", self.workflow)

    def test_deploy_uses_one_systemd_service_without_background_watchdogs(self) -> None:
        self.assertIn("install -o root -g root -m 644", self.workflow)
        self.assertIn("systemctl enable photo-bot.service", self.workflow)
        self.assertIn("systemctl restart photo-bot.service", self.workflow)
        self.assertIn("duplicate_polling_instance", self.workflow)
        self.assertNotIn("nohup", self.workflow)
        self.assertNotIn("crontab", self.workflow)
        self.assertNotIn("pkill", self.workflow)

    def test_systemd_template_uses_least_privilege_and_restart_safety(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")
        self.assertIn("User=photoapp", service)
        self.assertIn("Group=photoapp", service)
        self.assertIn("EnvironmentFile=/opt/photo-bot/.env", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("KillSignal=SIGTERM", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ReadWritePaths=/opt/photo-bot/data /opt/photo-bot/logs /opt/photo-bot/temp", service)
