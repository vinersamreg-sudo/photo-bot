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
        self.assertIn('chmod +x "$ROOT"/scripts/*.sh "$ROOT"/scripts/pixora', self.workflow)
        self.assertIn('cd "$ROOT"', self.workflow)
        self.assertIn('"$ROOT"/venv/bin/pip install', self.workflow)
        self.assertIn('"$ROOT"/venv/bin/pip check', self.workflow)
        self.assertIn("Database(load_settings().database_path)", self.workflow)
        self.assertIn('"$ROOT"/scripts/healthcheck.sh', self.workflow)
        self.assertIn("sudo -n systemctl", self.workflow)
        self.assertNotIn("sudo", SERVICE.read_text(encoding="utf-8"))

    def test_verifies_log_safety_and_tripday_isolation(self) -> None:
        self.assertIn("scripts.check_runtime_secrets", self.workflow)
        self.assertIn('chmod 600 "$ROOT/logs/app.log"', self.workflow)
        self.assertIn("MAX_BOT_TOKEN", self.workflow)
        self.assertNotIn("TripDay", self.workflow)

    def test_uses_python_312_and_scans_for_secrets(self) -> None:
        self.assertIn('python-version: "3.12"', self.workflow)
        self.assertIn("python scripts/scan_secrets.py", self.workflow)
        self.assertIn("python scripts/check_asset_licenses.py", self.workflow)

    def test_v1_provider_path_is_single_and_experiments_are_fail_closed(self) -> None:
        self.assertIn("set_env PROCESSING_MODE_ROUTER_ENABLED false", self.workflow)
        self.assertIn("set_env REAL_BACKGROUND_COMPOSITE_ENABLED false", self.workflow)
        self.assertIn("set_env ALLOW_AI_BACKGROUND_FALLBACK false", self.workflow)
        self.assertIn("set_env SEGMENTATION_BACKEND disabled", self.workflow)
        self.assertIn("set_env LOCAL_AI_FINISHING_ENABLED false", self.workflow)
        self.assertIn("scripts/benchmark_processing.py", self.workflow)
        self.assertIn("WHERE version=6", self.workflow)
        self.assertIn('"migration_v6": migration_v6', self.workflow)
        self.assertIn("WHERE version=7", self.workflow)
        self.assertIn('"migration_v7": migration_v7', self.workflow)
        self.assertIn("WHERE version=8", self.workflow)
        self.assertIn('"migration_v8": migration_v8', self.workflow)
        self.assertIn("assert migration_v8", self.workflow)
        self.assertIn("WHERE version=9", self.workflow)
        self.assertIn('"migration_v9": migration_v9', self.workflow)
        self.assertIn("assert migration_v9", self.workflow)
        self.assertIn('"credit_accounts": credit_accounts', self.workflow)
        self.assertIn("assert credit_accounts == users", self.workflow)
        self.assertIn("assert negative_credit_accounts == 0", self.workflow)
        self.assertIn('"stale_processing_orphans": stale_processing_orphans', self.workflow)
        self.assertIn("assert stale_processing_orphans == 0", self.workflow)

    def test_records_deployed_commit_after_healthcheck(self) -> None:
        health_position = self.workflow.rindex('"$ROOT"/scripts/healthcheck.sh')
        marker_position = self.workflow.rindex("data/deployed_commit.txt")
        self.assertGreater(marker_position, health_position)

    def test_runtime_audit_python_heredoc_terminates_at_remote_column(self) -> None:
        lines = self.workflow.splitlines()
        start = next(
            index
            for index, line in enumerate(lines)
            if "venv/bin/python - <<'PY'" in line
        )
        py_end = next(
            index for index in range(start + 1, len(lines)) if lines[index].strip() == "PY"
        )
        remote_end = next(
            index
            for index in range(py_end + 1, len(lines))
            if lines[index].strip() == "REMOTE"
        )
        leading = lambda value: len(value) - len(value.lstrip())
        self.assertEqual(leading(lines[py_end]), leading(lines[remote_end]))
        self.assertEqual(leading(lines[start + 1]), leading(lines[remote_end]))

    def test_creates_but_does_not_overwrite_production_env(self) -> None:
        self.assertIn('if [ ! -f "$ROOT/.env" ]', self.workflow)
        self.assertIn('install -m 600 /dev/null "$ROOT/.env"', self.workflow)
        self.assertNotIn("'OPENAI_API_KEY=", self.workflow)
        self.assertIn("set_env OPENAI_IMAGE_MODEL gpt-image-2", self.workflow)
        self.assertIn("set_env OPENAI_CONVERSATION_MEMORY_ENABLED false", self.workflow)
        self.assertIn("set_env OPENAI_RESPONSES_IMAGE_ENABLED false", self.workflow)
        self.assertIn("set_env OPENAI_CONVERSATION_RETENTION_ENABLED false", self.workflow)
        self.assertIn("set_env APP_ENV production", self.workflow)
        self.assertIn("set_env BASE_DIR /opt/photo-bot", self.workflow)
        self.assertIn("set_env DEMO_MAX_SUCCESSFUL_GENERATIONS 2", self.workflow)
        self.assertIn("set_env CONTINUATION_PACK_PRICE_RUB 49", self.workflow)
        self.assertIn("ensure_env GLOBAL_MAX_CONCURRENT_GENERATIONS 2", self.workflow)
        self.assertIn("ensure_env DEMO_RETENTION_DAYS 30", self.workflow)
        self.assertIn("ensure_env PAID_RETENTION_DAYS 180", self.workflow)
        self.assertIn("ensure_env TRASH_RETENTION_DAYS 30", self.workflow)
        self.assertIn("set_env PAYMENTS_ENABLED false", self.workflow)
        self.assertIn("set_env PAYMENT_PROVIDER disabled", self.workflow)
        self.assertIn("set_env PAYMENT_WEBHOOK_ENABLED false", self.workflow)
        self.assertIn("set_env PAYMENT_REFUNDS_ENABLED false", self.workflow)
        self.assertIn("set_env CONTENT_STUDIO_PUBLISHING_ENABLED false", self.workflow)
        self.assertIn('status["publishing_enabled"] is False', self.workflow)
        self.assertIn('status["published_posts"] == 0', self.workflow)
        self.assertIn("set_env ROBOKASSA_MODE sandbox", self.workflow)
        self.assertIn("set_env ROBOKASSA_PRODUCTION_APPROVED false", self.workflow)

    def test_transfers_openai_key_via_stdin_without_external_validation(self) -> None:
        self.assertIn("Confirm OpenAI credential is configured without an API request", self.workflow)
        self.assertIn("Configure OpenAI credential", self.workflow)
        self.assertIn("secrets.OPENAI_API_KEY", self.workflow)
        self.assertIn('printf \'%s\' "$OPENAI_API_KEY" |', self.workflow)
        self.assertNotIn("python -m app.main openai-check", self.workflow)
        self.assertIn("external API check intentionally skipped", self.workflow)

    def test_configures_max_without_exposing_the_token_as_an_argument(self) -> None:
        self.assertIn("Configure MAX credential", self.workflow)
        self.assertIn("secrets.MAX_BOT_TOKEN", self.workflow)
        self.assertIn('printf \'%s\' "$MAX_BOT_TOKEN" |', self.workflow)
        self.assertIn("set_env MAX_TRANSPORT_MODE polling", self.workflow)
        self.assertIn("set_env MAX_POLL_OBSERVE_ONLY true", self.workflow)
        self.assertIn("set_env MAX_POLL_OBSERVE_ONLY false", self.workflow)
        self.assertIn("ensure_env MAX_API_BASE_URL https://platform-api2.max.ru", self.workflow)
        self.assertIn("ensure_env MAX_CA_BUNDLE ops/certs/russian_trusted_root_ca_pem.crt", self.workflow)
        self.assertIn("/opt/photo-bot/scripts/stop_bot.sh", self.workflow)
        self.assertIn('if [ "$MODE" = polling ]', self.workflow)
        self.assertIn("python -m app.main max-check", self.workflow)

    def test_owner_allowlist_is_secret_and_fail_closed(self) -> None:
        self.assertGreaterEqual(self.workflow.count("MAX_OWNER_CONFIGURED:"), 2)
        self.assertIn("secrets.MAX_OWNER_USER_ID", self.workflow)
        self.assertIn('printf \'%s\' "$MAX_OWNER_USER_ID" |', self.workflow)
        self.assertIn("MAX_OWNER_USER_IDS=$OWNER_ID", self.workflow)
        self.assertIn("Disable MAX handlers without owner secret", self.workflow)
        self.assertIn("enable_owner_handlers:", self.workflow)
        self.assertIn('MAX_OWNER_HANDLERS_ENABLED', self.workflow)
        self.assertIn('[ "$MAX_OWNER_HANDLERS_ENABLED" = true ]', self.workflow)
        self.assertIn('"$GITHUB_SHA" "$MAX_OWNER_HANDLERS_ENABLED" "$PILOT_USER_LIMIT"', self.workflow)
        self.assertIn('MAX_OWNER_HANDLERS_ENABLED="${2:-false}"', self.workflow)
        self.assertNotIn(
            "MAX_OWNER_HANDLERS_ENABLED: ${{ env.MAX_OWNER_HANDLERS_ENABLED }}",
            self.workflow,
        )
        self.assertGreaterEqual(
            self.workflow.count(
                "MAX_OWNER_HANDLERS_ENABLED: ${{ github.event_name == 'workflow_dispatch' "
                "&& inputs.enable_owner_handlers == true }}"
            ),
            2,
        )

    def test_manual_deploy_can_reset_owner_dialog_without_logging_identifiers(self) -> None:
        self.assertIn("reset_owner_dialog:", self.workflow)
        self.assertIn("Reset owner dialog to a clean main-menu state", self.workflow)
        self.assertIn("inputs.reset_owner_dialog == true", self.workflow)
        self.assertIn('event_key="ops:owner-dialog-reset"', self.workflow)
        self.assertIn("owner_dialogs_reset=", self.workflow)
        self.assertNotIn("print(owner_id)", self.workflow)

    def test_manual_deploy_can_grant_only_bounded_owner_e2e_attempts(self) -> None:
        self.assertIn("grant_owner_e2e_attempts:", self.workflow)
        self.assertIn("options: ['0', '5']", self.workflow)
        self.assertIn("Grant bounded owner E2E attempts", self.workflow)
        self.assertIn("inputs.grant_owner_e2e_attempts == '5'", self.workflow)
        self.assertIn("CommerceService(database).adjust_generation_credits", self.workflow)
        self.assertIn("delta=5", self.workflow)
        self.assertIn("owner_e2e_generation_credits_adjusted=5", self.workflow)
        self.assertNotIn("print(owner_id)", self.workflow)

    def test_deploy_uses_one_systemd_service_without_background_watchdogs(self) -> None:
        self.assertIn("install -o root -g root -m 644", self.workflow)
        self.assertIn("systemctl enable photo-bot.service", self.workflow)
        self.assertIn("systemctl restart photo-bot.service", self.workflow)
        self.assertIn("duplicate_polling_instance", self.workflow)
        self.assertNotIn("nohup", self.workflow)
        self.assertNotIn("crontab", self.workflow)
        self.assertNotIn("pkill", self.workflow)

    def test_deploy_refuses_to_interrupt_active_processing(self) -> None:
        self.assertIn("Require idle production before deployment", self.workflow)
        self.assertIn("Deployment paused: active processing is present", self.workflow)
        idle_check = self.workflow.index("Require idle production before deployment")
        stop = self.workflow.index("systemctl stop photo-bot.service")
        self.assertLess(idle_check, stop)

    def test_systemd_template_uses_least_privilege_and_restart_safety(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")
        self.assertIn("User=photoapp", service)
        self.assertIn("Group=photoapp", service)
        self.assertIn("EnvironmentFile=/opt/photo-bot/.env", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("KillSignal=SIGTERM", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ReadWritePaths=/opt/photo-bot/data /opt/photo-bot/logs /opt/photo-bot/temp", service)
