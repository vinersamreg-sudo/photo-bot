import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

from scripts.production_status import (
    _canonical_lineage,
    _content_studio_sha,
    _deployed_sha,
)
from scripts.validate_deploy_target import validate_target


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


class ReleaseLineageTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "release-test@example.invalid")
        _git(self.root, "config", "user.name", "Release Test")
        _git(self.root, "commit", "--allow-empty", "-q", "-m", "base")
        self.base = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "branch", "-M", "main")
        _git(self.root, "commit", "--allow-empty", "-q", "-m", "canonical")
        self.canonical = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "--orphan", "outside", "-q")
        _git(self.root, "commit", "--allow-empty", "-q", "-m", "outside")
        self.outside = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "checkout", "main", "-q")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_canonical_sha_passes_without_deploying(self) -> None:
        valid, reason, ahead = validate_target(self.root, self.base, "main")
        self.assertTrue(valid)
        self.assertEqual(reason, "ok")
        self.assertEqual(ahead, 1)

    def test_sha_outside_canonical_history_is_rejected(self) -> None:
        valid, reason, ahead = validate_target(self.root, self.outside, "main")
        self.assertFalse(valid)
        self.assertEqual(reason, "target_outside_canonical_history")
        self.assertIsNone(ahead)

    def test_invalid_or_missing_sha_fails_closed(self) -> None:
        self.assertEqual(
            validate_target(self.root, "not-a-sha", "main")[:2],
            (False, "target_sha_invalid"),
        )
        self.assertEqual(
            validate_target(self.root, "0" * 40, "main")[:2],
            (False, "target_sha_missing"),
        )

    def test_status_distinguishes_runtimes_and_canonical_ancestry(self) -> None:
        report = _canonical_lineage(
            self.root,
            "main",
            {"main_bot": self.base, "content_studio": self.canonical},
        )
        self.assertEqual(report["origin_main_sha"], self.canonical)
        self.assertEqual(report["main_bot_in_canonical_main"], "PASS")
        self.assertEqual(report["content_studio_in_canonical_main"], "PASS")
        self.assertEqual(report["main_ahead_of_main_bot"], "1")
        self.assertEqual(report["main_ahead_of_content_studio"], "0")

    def test_status_marks_missing_deployed_commit_as_error(self) -> None:
        report = _canonical_lineage(
            self.root,
            "main",
            {"main_bot": "0" * 40, "content_studio": self.canonical},
        )
        self.assertEqual(report["main_bot_in_canonical_main"], "FAIL")

    def test_status_reads_separate_deployment_markers(self) -> None:
        application = self.root / "application"
        content = self.root / "content"
        (content / "current").mkdir(parents=True)
        application.mkdir()
        (application / ".deploy-sha").write_text(self.base + "\n", encoding="utf-8")
        (content / "current" / "REVISION").write_text(
            self.canonical + "\n", encoding="utf-8"
        )
        self.assertEqual(_deployed_sha(application), self.base)
        self.assertEqual(_content_studio_sha(content), self.canonical)
