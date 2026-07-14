from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


class DeployPolicyTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_supports_manual_deploy(self) -> None:
        self.assertIn("workflow_dispatch:", self.workflow)

    def test_preserves_runtime_state(self) -> None:
        for protected_path in (".env", "venv", "data", "logs", "temp"):
            self.assertIn(f"--exclude={protected_path}", self.workflow)

    def test_uses_scoped_safe_operations(self) -> None:
        self.assertNotIn("sudo", self.workflow)
        self.assertNotIn("pkill", self.workflow)
        self.assertIn("/var/www/u3546857/data/apps/photo-bot", self.workflow)
        self.assertIn('chmod +x "$ROOT"/scripts/*.sh', self.workflow)
        self.assertIn('cd "$ROOT"', self.workflow)
        self.assertIn('"$ROOT"/venv/bin/pip install', self.workflow)
        self.assertIn('"$ROOT"/venv/bin/pip check', self.workflow)
        self.assertIn('"$ROOT"/scripts/healthcheck.sh', self.workflow)

    def test_verifies_log_safety_and_tripday_isolation(self) -> None:
        self.assertIn("Potential secret material detected in app.log", self.workflow)
        self.assertIn("photo-bot files detected inside TripDay", self.workflow)

    def test_records_deployed_commit_after_healthcheck(self) -> None:
        health_position = self.workflow.index('"$ROOT"/scripts/healthcheck.sh')
        marker_position = self.workflow.index("data/deployed_commit.txt")
        self.assertGreater(marker_position, health_position)

    def test_creates_but_does_not_overwrite_production_env(self) -> None:
        self.assertIn('if [ ! -f "$ROOT/.env" ]', self.workflow)
        self.assertIn('install -m 600 /dev/null "$ROOT/.env"', self.workflow)
        self.assertNotIn("'OPENAI_API_KEY=", self.workflow)
        self.assertIn("OPENAI_IMAGE_MODEL=gpt-image-2", self.workflow)
        self.assertIn("APP_ENV=production", self.workflow)
