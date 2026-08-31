import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from scripts.build_deploy_artifact import build_release, verify_release
from scripts.deploy_env_guard import capture, verify
from scripts.main_bot_production_preflight import run_preflight


ROOT = Path(__file__).resolve().parents[1]


class EnvironmentPreservationTests(TestCase):
    def test_preserves_all_bytes_and_metadata_even_unknown_or_missing_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            env, snapshot = Path(directory) / ".env", Path(directory) / "fingerprint.json"
            contents = "# Комментарий\r\nIMAGE_PROVIDER=openai\r\nTOKEN='synthetic value'\r\nUNKNOWN=  spaces  \r\nUNKNOWN=second\r\n\r\n".encode()
            env.write_bytes(contents)
            env.chmod(0o600)
            before = env.stat()
            capture(env, snapshot)
            verify(env, snapshot)
            verify(env, snapshot)
            self.assertEqual(env.read_bytes(), contents)
            after = env.stat()
            self.assertEqual((before.st_mode, before.st_uid, before.st_gid, before.st_mtime_ns, before.st_ino),
                             (after.st_mode, after.st_uid, after.st_gid, after.st_mtime_ns, after.st_ino))
            self.assertNotIn("synthetic value", snapshot.read_text())
            self.assertEqual(set(json.loads(snapshot.read_text())), {"sha256", "mode", "uid", "gid"})

    def test_missing_or_empty_env_fails_without_initializing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            env, snapshot = Path(directory) / ".env", Path(directory) / "fingerprint.json"
            with self.assertRaises(FileNotFoundError):
                capture(env, snapshot)
            self.assertFalse(env.exists())
            env.write_bytes(b"")
            with self.assertRaises(ValueError):
                capture(env, snapshot)
            self.assertEqual(env.read_bytes(), b"")
            self.assertFalse(snapshot.exists())

    def test_one_byte_or_line_ending_change_is_rejected_not_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            env, snapshot = Path(directory) / ".env", Path(directory) / "fingerprint.json"
            env.write_bytes(b"MODE=unchanged\r\n")
            capture(env, snapshot)
            for changed in (b"MODE=unchanged\n", b"MODE=changed\r\n"):
                env.write_bytes(changed)
                with self.assertRaises(ValueError):
                    verify(env, snapshot)
                self.assertEqual(env.read_bytes(), changed)

    @skipUnless(os.name == "posix", "POSIX permissions")
    def test_permission_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            env, snapshot = Path(directory) / ".env", Path(directory) / "fingerprint.json"
            env.write_bytes(b"MODE=unchanged\n")
            env.chmod(0o600)
            capture(env, snapshot)
            env.chmod(0o644)
            with self.assertRaises(ValueError):
                verify(env, snapshot)
            self.assertEqual(env.stat().st_mode & 0o777, 0o644)

    def test_env_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            target, env = Path(directory) / "private", Path(directory) / ".env"
            target.write_bytes(b"MODE=synthetic\n")
            try:
                env.symlink_to(target)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaises(ValueError):
                capture(env, Path(directory) / "fingerprint.json")


class CommittedArtifactTests(TestCase):
    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.repo), *arguments], check=True, capture_output=True).stdout

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Synthetic CI")
        self.git("config", "user.email", "ci@invalid.example")
        self.git("config", "core.autocrlf", "true")
        self.git("config", "core.eol", "crlf")
        for name, content in {"app.py": b"print('committed')\n", ".env.example": b"MODE=disabled\n", "run.sh": b"#!/bin/sh\nexit 0\n", "image.bin": b"\x00\r\nbinary", ".gitattributes": b"*.sh text eol=lf\n"}.items():
            (self.repo / name).write_bytes(content)
        self.git("add", ".")
        self.git("update-index", "--chmod=+x", "run.sh")
        self.git("commit", "--quiet", "-m", "synthetic committed release")
        self.revision = self.git("rev-parse", "HEAD").decode().strip()
        self.archive, self.manifest = self.base / "release.tar.gz", self.base / "manifest.json"

    def test_archive_ignores_dirty_windows_bytes_and_untracked_private_files(self):
        (self.repo / "app.py").write_bytes(b"dirty checkout\r\n")
        (self.repo / ".env.example").write_bytes(b"dirty template\r\n")
        (self.repo / ".env").write_bytes(b"SYNTHETIC=not-for-release\r\n")
        report = build_release(self.repo, self.revision, self.archive, self.manifest)
        extracted = self.base / "extracted"
        extracted.mkdir()
        with tarfile.open(self.archive) as bundle:
            for name in report["files"]:
                self.assertEqual(bundle.extractfile(name).read(), self.git("cat-file", "blob", f"{self.revision}:{name}"))
            self.assertTrue(bundle.getmember("run.sh").mode & 0o111)
            bundle.extractall(extracted, filter="data")
        self.assertFalse((extracted / ".git").exists())
        self.assertFalse((extracted / ".env").exists())
        self.assertEqual(verify_release(extracted, self.archive, self.manifest)["revision"], self.revision)
        (extracted / "app.py").write_bytes(b"changed\r\n")
        with self.assertRaises(ValueError):
            verify_release(extracted, self.archive, self.manifest)

    def test_requires_exact_sha(self):
        with self.assertRaises(ValueError):
            build_release(self.repo, "main", self.archive, self.manifest)

    def test_tracked_private_data_fails_closed(self):
        (self.repo / ".env").write_bytes(b"SYNTHETIC=private\n")
        self.git("add", ".env")
        self.git("commit", "--quiet", "-m", "synthetic forbidden member")
        with self.assertRaises(ValueError):
            build_release(self.repo, self.git("rev-parse", "HEAD").decode().strip(), self.archive, self.manifest)

    def test_archive_export_substitution_cannot_silently_change_blobs(self):
        (self.repo / "revision.txt").write_bytes(b"$Format:%H$\n")
        (self.repo / ".gitattributes").write_bytes(b"*.sh text eol=lf\nrevision.txt export-subst\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "synthetic export substitution")
        with self.assertRaises(ValueError):
            build_release(self.repo, self.git("rev-parse", "HEAD").decode().strip(), self.archive, self.manifest)


class DeployDryRunTests(TestCase):
    def test_health_preflight_never_contacts_real_host_systemd(self):
        with patch("app.main.subprocess.run", side_effect=AssertionError("test leaked into real systemd")):
            result = run_preflight(("tests.test_health",), stream=io.StringIO())
        self.assertTrue(result.wasSuccessful())
        self.assertGreaterEqual(result.testsRun, 6)

    @skipUnless(shutil.which("rsync"), "rsync execution proof runs on Linux CI")
    def test_real_rsync_filters_preserve_mutable_and_separate_runtime_files(self):
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
        block = workflow.split("- name: Synchronize application files", 1)[1].split("- name: Install and verify production", 1)[0]
        filters = ["--exclude=" + value for value in re.findall(r"--exclude='([^']+)'", block)]
        protected = (".env", "data/db.sqlite3", "logs/app.log", "temp/source.png", "venv/bin/python", "app/content_studio/service.py", "app/admin_journal.py", "scripts/run_admin_journal.py", "site/public/index.html", "ops/ravuna-retention-cleanup.timer", "ops/ravuna-retention-cleanup.service", "ops/ravuna-admin-journal.service", "marketing/assets/example.png")
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source", Path(directory) / "target"
            for name in (*protected, "app/main.py"):
                for root, data in ((source, b"new"), (target, b"existing")):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            # --delete must not remove protected paths absent from the new artifact.
            (source / "scripts/run_admin_journal.py").unlink()
            subprocess.run(["rsync", "-a", "--delete", *filters, str(source) + "/", str(target) + "/"], check=True)
            for name in protected:
                self.assertEqual((target / name).read_bytes(), b"existing", name)
            self.assertEqual((target / "app/main.py").read_bytes(), b"new")

    def test_embedded_shell_and_python_are_syntax_valid_without_execution(self):
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
        if not bash:
            self.skipTest("bash not available")
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
        blocks = re.findall(r"        run: \|\n((?:          .*\n|\n)+)", workflow)
        self.assertGreater(len(blocks), 5)
        for block in blocks:
            script = "\n".join(line[10:] if line.startswith("          ") else line for line in block.splitlines()) + "\n"
            subprocess.run([bash, "-n"], input=script, text=True, check=True, capture_output=True)
            for remote in re.findall(r"<<'REMOTE'\n(.*?)\nREMOTE", script, re.S):
                subprocess.run([bash, "-n"], input=remote + "\n", text=True, check=True, capture_output=True)
            for python in re.findall(r"<<'PY'\n(.*?)\nPY", script, re.S):
                compile(python, "workflow-heredoc", "exec")
