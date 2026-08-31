import io
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest import TestCase

from scripts.build_content_studio_release import REQUIRED_MEMBERS, build_release
from scripts.main_bot_production_preflight import (
    MAIN_BOT_PRODUCTION_TEST_MODULES,
    run_preflight,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SITE_CI_WORKFLOW = ROOT / ".github" / "workflows" / "site.yml"
SITE_DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "site-deploy.yml"
ENV_EXAMPLE = ROOT / ".env.example"
SERVICE = ROOT / "ops" / "photo-bot.service"
RETENTION_SERVICE = ROOT / "ops" / "ravuna-retention-cleanup.service"
RETENTION_TIMER = ROOT / "ops" / "ravuna-retention-cleanup.timer"
DEPLOY_SUDOERS = ROOT / "ops" / "photo-bot-deploy.sudoers"
NGINX_RESULTURL_DEPLOY = ROOT / "ops" / "deploy_nginx_resulturl.sh"
NGINX_RAVUNA_CONFIG = ROOT / "site" / "nginx" / "ravuna.ru.conf"


class DeployPolicyTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.ci_workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        cls.site_ci_workflow = SITE_CI_WORKFLOW.read_text(encoding="utf-8")
        cls.site_deploy_workflow = SITE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        cls.nginx_resulturl_deploy = NGINX_RESULTURL_DEPLOY.read_text(encoding="utf-8")

    def test_supports_manual_deploy(self) -> None:
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("target_sha:", self.workflow)
        self.assertIn("--target-sha \"$TARGET_SHA\"", self.workflow)
        self.assertIn("--canonical-ref origin/main", self.workflow)
        self.assertIn("Canonical release gate", self.workflow)

    def test_ci_and_deployment_triggers_are_separated(self) -> None:
        self.assertIn("pull_request:", self.ci_workflow)
        self.assertIn("push:", self.ci_workflow)
        self.assertIn("branches: [main]", self.ci_workflow)
        self.assertIn("Canonical release gate", self.ci_workflow)
        self.assertIn("python scripts/test_fast.py payments", self.ci_workflow)
        self.assertIn("Run full release suite", self.ci_workflow)
        deploy_header = self.workflow.split("permissions:", 1)[0]
        self.assertIn("workflow_dispatch:", deploy_header)
        self.assertNotIn("pull_request:", deploy_header)
        self.assertNotIn("push:", deploy_header)

    def test_ci_has_no_production_authority(self) -> None:
        self.assertNotIn("environment: production", self.ci_workflow)
        self.assertNotIn("${{ secrets.", self.ci_workflow)
        self.assertNotIn("ssh ", self.ci_workflow)
        self.assertNotIn("rsync", self.ci_workflow)
        self.assertNotIn("systemctl", self.ci_workflow)

    def test_shell_scripts_and_committed_release_archive_are_lf_only(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", attributes)
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        shell_paths = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", revision],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        for relative in (path for path in shell_paths if path.endswith(".sh")):
            content = subprocess.run(
                ["git", "cat-file", "blob", f"{revision}:{relative}"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            self.assertNotIn(b"\r\n", content, relative)

    def test_release_builder_uses_committed_lf_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Ravuna CI")
            self._git(repository, "config", "user.email", "ci@invalid.example")
            self._git(repository, "config", "core.autocrlf", "true")
            files = {member: b"synthetic\n" for member in REQUIRED_MEMBERS}
            files[".gitattributes"] = b"*.sh text eol=lf\n"
            for relative, content in files.items():
                target = repository / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            self._git(repository, "add", ".")
            self._git(repository, "commit", "--quiet", "-m", "synthetic release")
            revision = self._git(repository, "rev-parse", "HEAD").strip()
            dirty_script = repository / "ops" / "deploy_ravuna_content_studio.sh"
            dirty_script.write_bytes(b"#!/bin/sh\r\nexit 0\r\n")

            archive = Path(directory) / "release.tar.gz"
            report = build_release(repository, revision, archive)
            self.assertEqual(report["source"], "committed_git_content")
            with tarfile.open(archive, "r:gz") as bundle:
                archived = bundle.extractfile("ops/deploy_ravuna_content_studio.sh")
                self.assertIsNotNone(archived)
                self.assertNotIn(b"\r\n", archived.read())

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_site_ci_and_manual_deploy_are_separated(self) -> None:
        self.assertIn("pull_request:", self.site_ci_workflow)
        self.assertIn("push:", self.site_ci_workflow)
        site_ci_header = self.site_ci_workflow.split("permissions:", 1)[0]
        self.assertNotIn("paths:", site_ci_header)
        self.assertNotIn("environment: production", self.site_ci_workflow)
        self.assertNotIn("secrets.", self.site_ci_workflow)
        self.assertNotIn("  deploy:", self.site_ci_workflow)
        site_deploy_header = self.site_deploy_workflow.split("permissions:", 1)[0]
        self.assertIn("workflow_dispatch:", site_deploy_header)
        self.assertNotIn("pull_request:", site_deploy_header)
        self.assertNotIn("push:", site_deploy_header)
        self.assertIn("target_sha:", self.site_deploy_workflow)
        self.assertIn("environment: production", self.site_deploy_workflow)
        self.assertIn("--canonical-ref origin/main", self.site_deploy_workflow)

    def test_preserves_runtime_state(self) -> None:
        for path in (".env", "venv/", "data/", "logs/", "temp/", "/site/", "/app/content_studio/", "/marketing/", "/app/admin_journal.py", "/scripts/run_admin_journal.py", "/ops/ravuna-admin-journal.service", "/ops/ravuna-retention-cleanup.service", "/ops/ravuna-retention-cleanup.timer"):
            self.assertIn(f"--exclude='{path}'", self.workflow)
        self.assertNotIn("--delete-excluded", self.workflow)
        self.assertIn('"$RUNNER_TEMP/ravuna-release/source/" "$SSH_USER@$SSH_HOST:/opt/photo-bot/"', self.workflow)

    def test_ravuna_payment_nginx_is_root_installed_and_preflighted(self) -> None:
        script = (ROOT / "ops" / "deploy_nginx_ravuna_payment.sh").read_text(
            encoding="utf-8"
        )
        nginx_config = NGINX_RAVUNA_CONFIG.read_text(encoding="utf-8")
        self.assertIn('if [ "$(id -u)" -ne 0 ]', script)
        self.assertIn('install -o root -g root -m 644 "$CANDIDATE" "$ACTIVE"', script)
        self.assertIn("nginx -t", script)
        self.assertIn("systemctl reload nginx", script)
        self.assertIn("trap rollback ERR", script)
        self.assertIn("Require preinstalled Ravuna payment routes", self.workflow)
        self.assertIn("https://ravuna.ru/p/00000000000000000000000000000000", self.workflow)
        self.assertIn("X-Ravuna-Payment-Route: active", self.workflow)
        self.assertIn("Validate Ravuna nginx candidate with nginx", self.ci_workflow)
        self.assertIn("nginx:1.27-alpine nginx -t", self.ci_workflow)
        self.assertIn(
            'location ~ "^/(?:p|payment/(?:success|fail))/[0-9a-f]{32}/?$" {',
            script,
        )
        self.assertIn(
            'add_header X-Ravuna-Payment-Route "active" always;',
            script,
        )
        self.assertIn(
            'add_header X-Ravuna-Payment-Route "active" always;',
            nginx_config,
        )

    def test_provisioning_is_separate_and_example_remains_fail_closed(self) -> None:
        self.assertNotIn("ensure_env", self.workflow)
        self.assertNotIn("set_env", self.workflow)
        self.assertNotIn('install -m 600 /dev/null "$ROOT/.env"', self.workflow)
        for line in ("MAX_PUBLIC_ACCESS_ENABLED=false", "MAX_POLL_OBSERVE_ONLY=true", "PAYMENTS_ENABLED=false", "PAYMENT_PROVIDER=disabled", "PAYMENT_WEBHOOK_ENABLED=false", "ROBOKASSA_MODE=sandbox", "ROBOKASSA_PRODUCTION_APPROVED=false"):
            self.assertIn(line, ENV_EXAMPLE.read_text(encoding="utf-8"))

    def test_uses_scoped_safe_operations(self) -> None:
        self.assertNotIn("pkill", self.workflow)
        self.assertIn("/opt/photo-bot", self.workflow)
        self.assertNotIn("REG_RU_", self.workflow)
        self.assertNotIn("trip-day", self.workflow.lower())
        self.assertIn("secrets.HETZNER_HOST", self.workflow)
        self.assertIn('cd "$ROOT"', self.workflow)
        self.assertNotIn('pip install', self.workflow)
        self.assertIn('cmp -s /opt/photo-bot/requirements.txt requirements.txt', self.workflow)
        self.assertIn('"$ROOT"/venv/bin/pip check', self.workflow)
        self.assertIn("Database(path)", self.workflow)
        self.assertIn('"$ROOT"/scripts/healthcheck.sh', self.workflow)
        self.assertIn("sudo -n systemctl", self.workflow)
        self.assertNotIn("sudo", SERVICE.read_text(encoding="utf-8"))

    def test_main_bot_production_preflight_is_explicit_and_git_independent(
        self,
    ) -> None:
        install_block = self.workflow.split(
            "- name: Install and verify production", 1
        )[1]
        self.assertIn(
            "python -m scripts.main_bot_production_preflight",
            self.workflow,
        )
        self.assertLess(self.workflow.index("python -m scripts.main_bot_production_preflight"), self.workflow.index("systemctl stop photo-bot.service"))
        self.assertNotIn("python -m unittest discover", install_block)
        self.assertNotIn("tests.test_deploy_policy", MAIN_BOT_PRODUCTION_TEST_MODULES)
        self.assertNotIn("tests.test_efficiency_tooling", MAIN_BOT_PRODUCTION_TEST_MODULES)
        self.assertNotIn("tests.test_release_lineage", MAIN_BOT_PRODUCTION_TEST_MODULES)
        self.assertFalse(
            any(
                module.startswith("tests.test_content_studio")
                for module in MAIN_BOT_PRODUCTION_TEST_MODULES
            )
        )

    def test_main_bot_preflight_passes_for_artifact_without_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            package = artifact / "artifact_tests"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "test_smoke.py").write_text(
                "import unittest\n\n"
                "class SmokeTest(unittest.TestCase):\n"
                "    def test_runtime(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            self.assertFalse((artifact / ".git").exists())
            result = run_preflight(
                ("artifact_tests.test_smoke",),
                artifact_root=artifact,
                stream=io.StringIO(),
            )
            self.assertTrue(result.wasSuccessful())

    def test_verifies_log_safety_and_tripday_isolation(self) -> None:
        self.assertIn("scripts.check_runtime_secrets", self.workflow)
        self.assertNotIn('chmod 600 "$ROOT/logs/app.log"', self.workflow)
        self.assertIn("python -m app.main max-check", self.workflow)
        self.assertNotIn("TripDay", self.workflow)

    def test_uses_python_312_and_scans_for_secrets(self) -> None:
        self.assertIn('python-version: "3.12"', self.ci_workflow)
        self.assertIn("python scripts/scan_secrets.py", self.ci_workflow)
        self.assertIn("python scripts/check_asset_licenses.py", self.ci_workflow)

    def test_deployment_cannot_activate_or_switch_providers(self) -> None:
        self.assertNotIn("activate_gemini:", self.workflow)
        self.assertNotIn("ACTIVATE_GEMINI", self.workflow)
        self.assertNotIn("secrets.GEMINI_API_KEY", self.workflow)
        self.assertIn("Capture pre-deploy production snapshot", self.workflow)
        self.assertIn("gemini_api_key_configured", self.workflow)

    def test_schema_and_commerce_gates_remain_enforced(self) -> None:
        for contract in ("CURRENT_SCHEMA_VERSION", "assert current_schema == CURRENT_SCHEMA_VERSION", "source.backup(backup)", "sqlite_backup=created", "assert credit_accounts == users", "assert negative_credit_accounts == 0", "assert stale_processing_orphans == 0", "_assert_commerce_invariants(after)", "_commerce_snapshot(after) == commerce", "payment-reconciliation"):
            self.assertIn(contract, self.workflow)
        for version in range(6, 13):
            self.assertIn(f"assert migration_v{version}", self.workflow)

    def test_records_deployed_commit_after_healthcheck(self) -> None:
        health_position = self.workflow.rindex('"$ROOT"/scripts/healthcheck.sh')
        marker_position = self.workflow.rindex("data/deployed_commit.txt")
        self.assertGreater(marker_position, health_position)

    def test_migration_copy_gate_runs_before_service_stop(self) -> None:
        gate = self.workflow.index(
            "Verify migration on isolated production database copy"
        )
        stop = self.workflow.index("systemctl stop photo-bot.service")
        self.assertLess(gate, stop)
        self.assertIn("scripts/check_migration_copy.py", self.workflow)
        self.assertIn("--source /opt/photo-bot/data/photo_bot.sqlite3", self.workflow)
        self.assertIn("--copy \"$STAGE_ROOT/photo_bot.migrated.sqlite3\"", self.workflow)
        self.assertIn("CURRENT_SCHEMA_VERSION", self.workflow)
        self.assertIn("assert current_schema == CURRENT_SCHEMA_VERSION", self.workflow)

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

    def test_env_is_captured_and_verified_not_rewritten(self) -> None:
        self.assertIn("deploy_env_guard.py", self.workflow)
        self.assertIn("capture", self.workflow)
        self.assertGreaterEqual(self.workflow.count("deploy_env_guard.py"), 4)
        self.assertIn("steps.snapshot.outcome == 'success'", self.workflow)
        snapshot = self.workflow.split("- name: Capture pre-deploy production snapshot", 1)[1].split("- name: Store pre-deploy production snapshot", 1)[0]
        self.assertIn("run: |\n          set -euo pipefail", snapshot)
        for forbidden in ('> "$ROOT/.env"', '>> "$ROOT/.env"', '"$ROOT/.env.tmp"', '"$ROOT/.env.runtime.tmp"', 'chmod 600 "$ROOT/.env"', "set_robokassa_production_secrets"):
            self.assertNotIn(forbidden, self.workflow)
        self.assertIn('assert settings.payment_provider == "robokassa"', self.workflow)
        self.assertIn('assert settings.robokassa_mode == "production"', self.workflow)
        self.assertIn("Verify ResultURL transport", self.workflow)

    def test_nginx_resulturl_deploy_is_root_scoped_and_rolls_back(self) -> None:
        script = self.nginx_resulturl_deploy
        self.assertIn('if [ "$(id -u)" -ne 0 ]', script)
        self.assertIn('CANDIDATE="${1:-}"', script)
        self.assertIn('! -f "$CANDIDATE"', script)
        self.assertIn('! -f "$ACTIVE"', script)
        self.assertIn("trap rollback ERR", script)
        self.assertIn('install -o root -g root -m 644 "$BACKUP" "$ACTIVE"', script)
        self.assertIn("nginx -t", script)
        self.assertIn('test "$GET_STATUS" = 405', script)
        self.assertIn('test "$POST_STATUS" = 503', script)

    def test_no_provider_secrets_or_billable_validation_in_deploy(self) -> None:
        self.assertNotIn("secrets.OPENAI_API_KEY", self.workflow)
        self.assertNotIn("secrets.GEMINI_API_KEY", self.workflow)
        self.assertNotIn("python -m app.main openai-check", self.workflow)
        self.assertIn("external API check intentionally skipped", self.workflow)

    def test_payment_secrets_are_not_transferred_by_deploy(self) -> None:
        self.assertNotIn("secrets.ROBOKASSA", self.workflow)
        self.assertNotIn("Configure production Robokassa credentials", self.workflow)
        self.assertNotIn("--password", self.workflow)

    def test_existing_max_configuration_is_verified_without_rewriting(self) -> None:
        self.assertNotIn("secrets.MAX_BOT_TOKEN", self.workflow)
        self.assertNotIn("Configure MAX credential", self.workflow)
        self.assertIn("python -m app.main max-check", self.workflow)
        self.assertIn("python -m app.main health", self.workflow)

    def test_deploy_does_not_modify_owner_or_pilot_flags(self) -> None:
        for forbidden in ("enable_owner_handlers:", "pilot_user_limit:", "secrets.MAX_OWNER_USER_ID", "secrets.MAX_PILOT_USER_IDS", "MAX_OWNER_HANDLERS_ENABLED"):
            self.assertNotIn(forbidden, self.workflow)

    def test_deploy_cannot_reset_dialogs(self) -> None:
        self.assertNotIn("reset_owner_dialog:", self.workflow)
        self.assertNotIn("MaxConversationStore", self.workflow)

    def test_deploy_cannot_grant_credits(self) -> None:
        self.assertNotIn("grant_owner_e2e_attempts:", self.workflow)
        self.assertNotIn("adjust_generation_credits", self.workflow)

    def test_successful_deploy_restarts_exactly_once_without_a_second_runtime(self) -> None:
        self.assertEqual(self.workflow.count("systemctl restart photo-bot.service"), 1)
        self.assertIn('test "$MAIN_PID" != "$OLD_PID"', self.workflow)
        self.assertIn("--property=NRestarts --value)", self.workflow)
        self.assertIn("pgrep -u photoapp", self.workflow)
        self.assertIn("RESTART_EPOCH=$(date +%s)", self.workflow)
        self.assertIn("WHERE name='poll_last_success'", self.workflow)
        for forbidden in ("DUPLICATE_LOG", "duplicate_polling_instance", "timeout 10", "nohup", "crontab", "pkill"):
            self.assertNotIn(forbidden, self.workflow)

    def test_deploy_refuses_to_interrupt_active_processing(self) -> None:
        self.assertIn("Require idle production before deployment", self.workflow)
        self.assertIn("Deployment paused: active processing is present", self.workflow)
        idle_check = self.workflow.index("Require idle production before deployment")
        stop = self.workflow.index("systemctl stop photo-bot.service")
        self.assertLess(idle_check, stop)

    def test_deploy_records_sha_without_modifying_sandbox_settings(self) -> None:
        self.assertNotIn("set_env ROBOKASSA_SANDBOX", self.workflow)
        self.assertIn('printf \'%s\\n\' "$DEPLOY_SHA" > "$ROOT/.deploy-sha"', self.workflow)
        self.assertIn('chmod 600 "$ROOT/.deploy-sha"', self.workflow)

    def test_systemd_template_uses_least_privilege_and_restart_safety(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")
        self.assertIn("User=photoapp", service)
        self.assertIn("Group=photoapp", service)
        self.assertIn("EnvironmentFile=/opt/photo-bot/.env", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn("KillSignal=SIGTERM", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ReadWritePaths=/opt/photo-bot/data /opt/photo-bot/logs /opt/photo-bot/temp", service)

    def test_retention_cleanup_is_an_independent_daily_oneshot(self) -> None:
        service = RETENTION_SERVICE.read_text(encoding="utf-8")
        timer = RETENTION_TIMER.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("User=photoapp", service)
        self.assertIn("maintenance-cleanup --execute", service)
        self.assertIn("retention-cleanup.lock", service)
        self.assertNotIn("photo-bot.service", service)
        self.assertIn("OnCalendar=*-*-* 02:30:00 UTC", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=ravuna-retention-cleanup.service", timer)

    def test_deploy_cannot_install_or_operate_retention_units(self) -> None:
        for unit in ("ravuna-retention-cleanup.service", "ravuna-retention-cleanup.timer", "ravuna-admin-journal.service", "ravuna-content-publisher.timer", "ravuna-content-publisher.service"):
            self.assertNotIn(f"/etc/systemd/system/{unit}", self.workflow)
            for action in ("start", "stop", "restart", "enable", "disable"):
                self.assertNotIn(f"systemctl {action} {unit}", self.workflow)

    def test_deploy_sudoers_allows_only_exact_retention_unit_install_commands(self) -> None:
        sudoers = DEPLOY_SUDOERS.read_text(encoding="utf-8")
        self.assertIn(
            "/usr/bin/install -o root -g root -m 644 "
            "/opt/photo-bot/ops/ravuna-retention-cleanup.service "
            "/etc/systemd/system/ravuna-retention-cleanup.service",
            sudoers,
        )
        self.assertIn(
            "/usr/bin/install -o root -g root -m 644 "
            "/opt/photo-bot/ops/ravuna-retention-cleanup.timer "
            "/etc/systemd/system/ravuna-retention-cleanup.timer",
            sudoers,
        )
        command_alias = sudoers.splitlines()[0]
        self.assertNotIn("*", command_alias)
        self.assertNotIn("ravuna-retention-cleanup.timer,", command_alias)
