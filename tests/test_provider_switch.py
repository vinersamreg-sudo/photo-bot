import io
import json
import os
import runpy
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from app.provider_switch import (
    Environment, History, MODELS, SwitchBlocked, main, replace_environment, run_switch,
)


ENV = (b"# private configuration\r\nIMAGE_PROVIDER=openai\r\nOPENAI_IMAGE_MODEL=gpt-image-2\r\n"
       b"GEMINI_IMAGE_MODEL=gemini-3-pro-image\r\nIMAGE_DIRECT_PROMPT_ENABLED=true\r\n"
       b"OPENAI_API_KEY='synthetic-secret'\r\nPAYMENT_PASSWORD=unchanged\r\nUNKNOWN=  old  \r\n")
SMOKE = dict(ok=True, category="ok", latency_seconds=1.2, requests=1, http=200,
             exact_prompt=True, ordered_images=True)


class FakeRuntime:
    def __init__(self, root):
        self.env_path = root / ".env"
        self.env_path.write_bytes(ENV)
        self.env_path.chmod(0o600)
        self.provider, self.model, self.pid = "openai", "gpt-image-2", 10
        self.restarts = 0
        self.smokes = []
        self.smoke_results = [SMOKE.copy(), SMOKE.copy()]
        self.health_results = [True, True]
        self.busy = False
        self.healthy = True
        self.conflict = False

    def environment(self):
        return Environment.read(self.env_path)

    def snapshot(self, *, online):
        return dict(provider=self.provider, model=self.model, pid=self.pid, restarts=0,
                    direct_prompt=True, healthy=self.healthy, idle=not self.busy,
                    config_matches_runtime=True)

    def idle(self):
        return not self.busy

    def restart(self):
        self.restarts += 1
        self.pid += 1
        self.provider = self.environment().values()["IMAGE_PROVIDER"]
        self.model = MODELS[self.provider]

    def wait_healthy(self, *args, **kwargs):
        if self.conflict:
            self.env_path.write_bytes(self.env_path.read_bytes() + b"NEW_SECRET=concurrent\r\n")
        return self.health_results.pop(0)

    def smoke(self, env):
        self.smokes.append(env)
        return self.smoke_results.pop(0)


class ProviderSwitchTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = FakeRuntime(self.root)
        self.history = History(self.root / "ops")
        self.history.directory.mkdir(mode=0o700)

    def switch(self):
        return run_switch(self.runtime, self.history, "gemini")

    def test_same_provider_no_restart_no_smoke_no_history(self):
        before = self.runtime.environment()
        report = run_switch(self.runtime, self.history, "openai")
        self.assertEqual(report["result"], "ALREADY_ACTIVE")
        self.assertEqual(self.runtime.restarts, 0)
        self.assertFalse(self.runtime.smokes)
        self.assertFalse(self.history.path.exists())
        self.assertEqual(self.runtime.environment(), before)

    def test_status_read_only_no_history_directory_or_api_smoke(self):
        history = self.root / "data" / "ops" / "provider-switch"
        before = self.runtime.env_path.stat()
        with patch("app.provider_switch_runtime.Runtime", return_value=self.runtime), \
                patch("app.provider_switch.History", return_value=History(history)), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["status", "--root", str(self.root)]), 0)
        self.assertFalse(history.exists())
        self.assertEqual(before, self.runtime.env_path.stat())
        self.assertEqual(self.runtime.restarts, 0)
        self.assertFalse(self.runtime.smokes)
        self.assertIsNone(json.loads(output.getvalue())["last_switch"])
        self.assertNotIn("synthetic-secret", output.getvalue())

    def test_unhealthy_target_no_mutation_or_restart(self):
        self.runtime.smoke_results[0] = dict(SMOKE, ok=False, category="timeout")
        before = self.runtime.environment()
        result = self.switch()
        self.assertEqual(result["reason"], "TARGET_UNHEALTHY")
        self.assertEqual(self.runtime.environment(), before)
        self.assertEqual(self.runtime.restarts, 0)
        self.assertEqual(len(self.runtime.smokes), 1)

    def test_busy_or_unhealthy_no_smoke_mutation_restart(self):
        for busy, healthy in ((True, True), (False, False)):
            with self.subTest(busy=busy):
                self.runtime.busy, self.runtime.healthy = busy, healthy
                self.assertEqual(self.switch()["result"], "BLOCKED")
                self.assertEqual(self.runtime.env_path.read_bytes(), ENV)
                self.assertFalse(self.runtime.smokes)
                self.assertEqual(self.runtime.restarts, 0)

    def test_success_exactly_one_restart_two_smokes_and_private_audit(self):
        before = self.runtime.environment()
        report = self.switch()
        self.assertEqual(report["result"], "SWITCHED")
        self.assertEqual(report["restarts"], 1)
        self.assertEqual(self.runtime.restarts, 1)
        self.assertEqual(len(self.runtime.smokes), 2)
        self.assertEqual(self.runtime.environment(), before.target("gemini"))
        audit = self.history.path.read_text()
        self.assertNotIn("synthetic-secret", audit)
        self.assertNotIn("PAYMENT_PASSWORD", audit)
        self.assertNotIn('"prompt":', audit)
        self.assertEqual(len(json.loads(audit)), 1)
        self.assertEqual(self.history.last()["result"], "SWITCHED")
        self.assertEqual(report["environment_before"], before.fingerprint())

    def test_failed_post_health_exact_rollback_one_recovery_restart(self):
        before = self.runtime.environment()
        self.runtime.health_results = [False, True]
        result = self.switch()
        self.assertEqual(result["result"], "ROLLED_BACK")
        self.assertEqual(self.runtime.restarts, 2)
        self.assertEqual(self.runtime.environment(), before)
        self.assertEqual(len(self.runtime.smokes), 1)

    def test_failed_post_smoke_exact_rollback_without_third_smoke(self):
        self.runtime.smoke_results[1] = dict(SMOKE, ok=False)
        self.assertEqual(self.switch()["result"], "ROLLED_BACK")
        self.assertEqual(self.runtime.env_path.read_bytes(), ENV)
        self.assertEqual(self.runtime.restarts, 2)
        self.assertEqual(len(self.runtime.smokes), 2)

    def test_failed_recovery_stops_without_loop(self):
        self.runtime.health_results = [False, False]
        result = self.switch()
        self.assertEqual(result["result"], "RECOVERY_REQUIRED")
        self.assertEqual(self.runtime.restarts, 2)
        self.assertEqual(len(self.runtime.smokes), 1)

    def test_concurrent_env_edit_after_smoke_blocks_without_mutation(self):
        def smoke(_):
            self.runtime.env_path.write_bytes(ENV + b"UNCHANGED_BY_SWITCH=external\r\n")
            return SMOKE.copy()
        self.runtime.smoke = smoke
        self.assertEqual(self.switch()["result"], "BLOCKED")
        self.assertEqual(self.runtime.restarts, 0)
        self.assertIn(b"UNCHANGED_BY_SWITCH=external", self.runtime.env_path.read_bytes())

    def test_work_arriving_during_smoke_blocks(self):
        def smoke(_):
            self.runtime.busy = True
            return SMOKE.copy()
        self.runtime.smoke = smoke
        self.assertEqual(self.switch()["result"], "BLOCKED")
        self.assertEqual(self.runtime.env_path.read_bytes(), ENV)
        self.assertEqual(self.runtime.restarts, 0)

    def test_work_arriving_after_mutation_restores_env_without_restart(self):
        self.runtime.idle = lambda: False
        result = self.switch()
        self.assertEqual(result["result"], "BLOCKED")
        self.assertEqual(self.runtime.env_path.read_bytes(), ENV)
        self.assertEqual(self.runtime.restarts, 0)

    def test_concurrent_secret_edit_not_clobbered_by_rollback(self):
        self.runtime.conflict = True
        self.runtime.health_results = [False]
        result = self.switch()
        self.assertEqual(result["result"], "RECOVERY_REQUIRED")
        self.assertEqual(self.runtime.restarts, 1)
        self.assertIn(b"NEW_SECRET=concurrent", self.runtime.env_path.read_bytes())

    def test_atomic_update_failure_keeps_original_and_does_not_restart(self):
        with patch("app.provider_switch.os.replace", side_effect=OSError("synthetic-secret")):
            # The first atomic write is the audit sentinel: fail before config mutation.
            with self.assertRaises(OSError):
                self.switch()
        self.assertEqual(self.runtime.env_path.read_bytes(), ENV)
        self.assertEqual(self.runtime.restarts, 0)
        self.assertEqual(list(self.history.directory.iterdir()), [])

    def test_raw_error_secrets_never_reach_cli_output(self):
        with patch("app.provider_switch_runtime.Runtime.snapshot", side_effect=ValueError("synthetic-secret")), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["status", "--root", str(self.root)]), 1)
        self.assertNotIn("synthetic-secret", output.getvalue())

    def test_bounded_history_and_safe_last_projection(self):
        for _ in range(105):
            self.runtime.busy = True
            self.switch()
        self.assertEqual(len(self.history.read()), 100)
        self.assertEqual(set(self.history.last()), {"timestamp", "result"})

    @skipUnless(os.name == "posix", "Linux operation lock")
    def test_parallel_switch_blocked_and_interrupted_switch_stays_blocked(self):
        with self.history.lock():
            with self.assertRaisesRegex(SwitchBlocked, "already_running"):
                with self.history.lock():
                    self.fail("lock bypass")
        self.history.record(dict(timestamp="2026-08-31T00:00:00+00:00", result="IN_PROGRESS"))
        with self.assertRaisesRegex(SwitchBlocked, "interrupted_switch"):
            with self.history.lock():
                self.fail("interruption bypass")


class ProviderEnvironmentTests(TestCase):
    def environment(self, contents):
        return Environment(contents, 0o600, 1001, 1001)

    def test_only_allowlisted_bytes_change_with_crlf_comments_and_multiline(self):
        original = ENV + b'OTHER="line one\r\nline two"\r\nDUP=1\r\nDUP=2\r\n'
        env = self.environment(original)
        updated = env.target("gemini")
        self.assertEqual(updated.contents, original.replace(b"IMAGE_PROVIDER=openai", b"IMAGE_PROVIDER=gemini"))
        self.assertEqual((env.mode, env.uid, env.gid), (updated.mode, updated.uid, updated.gid))

    def test_missing_keys_and_no_final_newline_preserve_existing_prefix(self):
        env = self.environment(b"# keep\r\nSECRET='untouched'")
        updated = env.target("openai")
        self.assertTrue(updated.contents.startswith(env.contents + b"\r\n"))
        self.assertEqual(updated.values()["OPENAI_IMAGE_MODEL"], "gpt-image-2")
        self.assertNotIn("GEMINI_IMAGE_MODEL", updated.values())

    def test_unused_provider_model_and_credentials_preserved(self):
        env = self.environment(ENV.replace(b"gemini-3-pro-image", b"older-target"))
        self.assertEqual(env.target("openai").contents, env.contents)
        self.assertIn(b"GEMINI_IMAGE_MODEL=gemini-3-pro-image", env.target("gemini").contents)

    def test_duplicate_and_malformed_provider_assignments_fail_closed(self):
        for contents in (ENV + b"IMAGE_PROVIDER=gemini\n", ENV + b"not an assignment\n"):
            with self.subTest(contents=contents[-24:]), self.assertRaises(SwitchBlocked):
                self.environment(contents).target("gemini")

    def test_quoted_export_and_blank_lines(self):
        env = self.environment(b'\n\nexport IMAGE_PROVIDER="openai" # old\nOTHER=untouched\n')
        updated = env.target("gemini")
        self.assertIn(b"\n\nIMAGE_PROVIDER=gemini\nOTHER=untouched\n", updated.contents)

    def test_atomic_compare_and_swap_and_exact_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_bytes(ENV)
            path.chmod(0o600)
            before = Environment.read(path)
            after = before.target("gemini")
            replace_environment(path, before, after)
            self.assertEqual(Environment.read(path), after)
            with self.assertRaises(SwitchBlocked):
                replace_environment(path, before, after)
            replace_environment(path, after, before)
            self.assertEqual(Environment.read(path), before)
            self.assertEqual([p.name for p in Path(directory).iterdir()], [".env"])

    @skipUnless(os.name == "posix", "POSIX links")
    def test_symlink_and_hardlink_env_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "actual"
            source.write_bytes(ENV)
            path = root / ".env"
            path.symlink_to(source)
            with self.assertRaises(OSError):
                Environment.read(path)
            path.unlink()
            os.link(source, path)
            with self.assertRaises(SwitchBlocked):
                Environment.read(path)

    def test_canonical_entrypoint_keeps_content_studio_separate(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "ravuna").read_text()
        self.assertIn('-m app.provider_switch "$@" --root "$ROOT"', script)
        self.assertIn('-m app.content_studio.cli "$@"', script)

    def test_python_module_entrypoint_uses_one_environment_type(self):
        import warnings
        with patch("app.provider_switch.main", return_value=0) as canonical, warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_module("app.provider_switch", run_name="__main__")
        canonical.assert_called_once_with()
        self.assertEqual(stopped.exception.code, 0)
