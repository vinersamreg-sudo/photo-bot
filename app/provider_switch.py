"""Explicit, bounded provider operations. No automatic fallback or DB writes."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream


MODELS = {"gemini": "gemini-3-pro-image", "openai": "gpt-image-2"}
MODEL_KEYS = {"gemini": "GEMINI_IMAGE_MODEL", "openai": "OPENAI_IMAGE_MODEL"}
PROVIDER_KEYS = {"IMAGE_PROVIDER", "IMAGE_DIRECT_PROMPT_ENABLED", *MODEL_KEYS.values()}


class SwitchBlocked(RuntimeError):
    """Messages are fixed local reason codes, never raw external errors."""


@dataclass(frozen=True)
class Environment:
    contents: bytes = field(repr=False)
    mode: int
    uid: int
    gid: int

    @classmethod
    def read(cls, path: Path) -> Environment:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SwitchBlocked("unsafe_environment_file")
            return cls(stream.read(), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)

    def values(self) -> dict[str, str]:
        return dict(dotenv_values(stream=io.StringIO(self.contents.decode("utf-8")), interpolate=False))

    def fingerprint(self) -> dict:
        return dict(sha256=hashlib.sha256(self.contents).hexdigest(), mode=self.mode,
                    uid=self.uid, gid=self.gid)

    def target(self, provider: str) -> Environment:
        updates = {"IMAGE_PROVIDER": provider, MODEL_KEYS[provider]: MODELS[provider],
                   "IMAGE_DIRECT_PROMPT_ENABLED": "true"}
        pieces, seen = [], set()
        text = self.contents.decode("utf-8")
        for binding in parse_stream(io.StringIO(text)):
            if binding.error:
                raise SwitchBlocked("ambiguous_environment_syntax")
            if binding.key in PROVIDER_KEYS:
                if binding.key in seen:
                    raise SwitchBlocked("duplicate_provider_key")
                seen.add(binding.key)
            original = binding.original.string
            if binding.key in updates:
                # Keep leading blank lines. Only the selected assignment changes.
                leading = original[:len(original) - len(original.lstrip("\r\n"))]
                ending = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else ""
                pieces.append(f"{leading}{binding.key}={updates[binding.key]}{ending}")
            else:
                pieces.append(original)
        result = "".join(pieces)
        ending = "\r\n" if "\r\n" in text else "\n"
        for key, value in updates.items():
            if key not in seen:
                if result and not result.endswith("\n"):
                    result += ending
                result += f"{key}={value}{ending}"
        changed = Environment(result.encode("utf-8"), self.mode, self.uid, self.gid)
        before, after = self.values(), changed.values()
        if any(before.get(k) != after.get(k) for k in before.keys() | after.keys() if k not in updates):
            raise SwitchBlocked("unapproved_environment_change")
        if any(after.get(k) != v for k, v in updates.items()):
            raise SwitchBlocked("invalid_target_environment")
        return changed


def atomic_write(path: Path, contents: bytes, *, mode: int = 0o600,
                 uid: int | None = None, gid: int | None = None) -> None:
    """Same-directory private temporary file; no visible partial document."""
    descriptor, name = tempfile.mkstemp(prefix=".provider-switch-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.name == "posix":
                if uid is not None:
                    os.fchown(stream.fileno(), uid, gid)
                os.fchmod(stream.fileno(), mode)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def replace_environment(path: Path, expected: Environment, replacement: Environment) -> None:
    if Environment.read(path) != expected:
        raise SwitchBlocked("concurrent_environment_change")
    atomic_write(path, replacement.contents, mode=replacement.mode,
                 uid=replacement.uid, gid=replacement.gid)
    if Environment.read(path) != replacement:
        raise SwitchBlocked("environment_verification_failed")


class History:
    """Root-private, bounded history and persistent interruption sentinel."""
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "history.json"

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        if self.directory.is_symlink() or self.path.is_symlink() or self.path.stat().st_size > 131072:
            raise SwitchBlocked("unsafe_history")
        entries = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(entries, list) or len(entries) > 100:
            raise SwitchBlocked("invalid_history")
        return entries

    def last(self) -> dict | None:
        entries = self.read()
        if not entries:
            return None
        last = entries[-1]
        # Never echo arbitrary fields from a history file.
        timestamp = datetime.fromisoformat(last["timestamp"]).isoformat()
        result = last.get("result")
        if result not in {"SWITCHED", "BLOCKED", "ROLLED_BACK", "IN_PROGRESS", "RECOVERY_REQUIRED"}:
            raise SwitchBlocked("invalid_history_result")
        return {"timestamp": timestamp, "result": result}

    def record(self, entry: dict, *, finish: bool = False) -> None:
        entries = self.read()
        if finish:
            if not entries or entries[-1].get("result") != "IN_PROGRESS":
                raise SwitchBlocked("missing_switch_journal")
            entries.pop()
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        entries = [e for e in entries if datetime.fromisoformat(e["timestamp"]) >= cutoff]
        entries.append(entry)
        atomic_write(self.path, json.dumps(entries[-100:], sort_keys=True).encode())

    @contextmanager
    def lock(self):
        import fcntl  # Switching is a Linux/systemd operation; status is portable.
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise SwitchBlocked("unsafe_history_directory")
        descriptor = os.open(self.directory / "switch.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SwitchBlocked("switch_already_running") from None
            last = self.last()
            if last and last["result"] in {"IN_PROGRESS", "RECOVERY_REQUIRED"}:
                raise SwitchBlocked("interrupted_switch_requires_operator")
            yield
        finally:
            os.close(descriptor)


def run_switch(runtime, history: History, target: str) -> dict:
    """At most two image calls and one normal + one recovery restart. No loops."""
    before = runtime.environment()
    pre = runtime.snapshot(online=True)
    if pre["provider"] == target and pre["model"] == MODELS[target] and pre["direct_prompt"]:
        if pre["healthy"] and pre["config_matches_runtime"]:
            return {"result": "ALREADY_ACTIVE", "restarts": 0}
    entry = dict(timestamp=datetime.now(timezone.utc).isoformat(), source="operator_cli",
                 from_provider=pre["provider"], from_model=pre["model"],
                 to_provider=target, to_model=MODELS[target], result="BLOCKED")

    def blocked(reason):
        entry["reason"] = reason
        history.record(entry)
        return {**entry, "restarts": 0}

    if not pre["healthy"] or not pre["idle"] or not pre["config_matches_runtime"]:
        return blocked("busy_or_unhealthy")
    if os.name == "posix" and before.mode & 0o037:
        return blocked("unsafe_environment_permissions")
    desired = before.target(target)
    entry["pre_smoke"] = runtime.smoke(desired)
    if not entry["pre_smoke"]["ok"]:
        return blocked("TARGET_UNHEALTHY")
    # A smoke can take minutes. Recheck the actual runtime and queues, not a cache.
    ready = runtime.snapshot(online=True)
    if runtime.environment() != before or ready != pre:
        # Polling age can naturally change, but identity/health must not.
        stable_keys = ("pid", "restarts", "provider", "model", "direct_prompt")
        if (runtime.environment() != before or not ready["healthy"] or not ready["idle"]
                or not ready["config_matches_runtime"]
                or any(ready[k] != pre[k] for k in stable_keys)):
            return blocked("state_changed_during_smoke")
    entry.update(result="IN_PROGRESS", environment_before=before.fingerprint())
    history.record(entry)  # Durable BEFORE any configuration mutation.
    restarts = 0
    try:
        replace_environment(runtime.env_path, before, desired)
        # Last DB read immediately before the only normal restart.
        if not runtime.idle():
            replace_environment(runtime.env_path, desired, before)
            entry.update(result="BLOCKED", reason="became_busy_before_restart")
            history.record(entry, finish=True)
            return {**entry, "restarts": 0}
        restarts += 1
        runtime.restart()
        if not runtime.wait_healthy(target, MODELS[target], previous_pid=pre["pid"]):
            raise SwitchBlocked("post_switch_health_failed")
        entry["post_smoke"] = runtime.smoke(desired)
        if not entry["post_smoke"]["ok"]:
            raise SwitchBlocked("post_switch_smoke_failed")
        final = runtime.snapshot(online=True)
        if (not final["healthy"] or not final["idle"] or not final["config_matches_runtime"]
                or final["restarts"] != 0):
            raise SwitchBlocked("post_smoke_health_failed")
        if runtime.environment() != desired:
            raise SwitchBlocked("concurrent_environment_change")
        entry["result"] = "SWITCHED"
    except (Exception, KeyboardInterrupt):
        # Never overwrite concurrent credential edits or repeatedly restart.
        try:
            current = runtime.environment()
            if current == desired:
                replace_environment(runtime.env_path, desired, before)
            elif current != before:
                raise SwitchBlocked("rollback_environment_conflict")
            if restarts:
                restarts += 1
                runtime.restart()
                if not runtime.wait_healthy(pre["provider"], pre["model"]):
                    raise SwitchBlocked("rollback_health_failed")
            entry["result"] = "ROLLED_BACK" if restarts else "BLOCKED"
            entry["reason"] = "switch_failed_restored_previous_configuration"
        except (Exception, KeyboardInterrupt):
            entry.update(result="RECOVERY_REQUIRED", reason="rollback_not_verified_manual_review")
    history.record(entry, finish=True)
    return {**entry, "restarts": restarts}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Explicit provider switch (two billable synthetic edits); status is read-only.")
    parser.add_argument("action", choices=("status", *MODELS))
    parser.add_argument("--root", type=Path, default=Path("/opt/photo-bot"))
    args = parser.parse_args(argv)
    # SDK/provider error loggers may contain raw upstream diagnostics. Output only our allowlisted summary.
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    from app.provider_switch_runtime import Runtime
    runtime = Runtime(args.root)
    # Outside photoapp-writable data/: a compromised app must not replace the
    # root ops lock/history directory. No customer data or credentials live here.
    history = History(Path("/var/lib/ravuna-provider-switch"))
    try:
        if args.action == "status":
            report = runtime.snapshot(online=False)
            report["last_switch"] = history.last()
        else:
            if os.name != "posix" or os.geteuid() != 0:
                raise SwitchBlocked("switch_requires_root_on_production_host")
            with history.lock():
                report = run_switch(runtime, history, args.action)
    except (Exception, KeyboardInterrupt):
        # No raw exceptions, arbitrary settings, provider responses or paths on stderr.
        report = {"result": "BLOCKED", "reason": "precondition_or_operation_failed_review_private_state"}
        try:
            last = history.last()
            if last and last["result"] in {"IN_PROGRESS", "RECOVERY_REQUIRED"}:
                report = {"result": "RECOVERY_REQUIRED", "reason": "unfinished_switch_requires_operator"}
        except Exception:
            pass
    finally:
        logging.disable(previous_logging_disable)
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("result") in {"ALREADY_ACTIVE", "SWITCHED"} or (
        args.action == "status" and report.get("healthy")) else 1


if __name__ == "__main__":
    # Runtime imports our Environment type. Use its canonical module instance
    # under `python -m` too, otherwise dataclass equality fails across two types.
    from app.provider_switch import main as cli_main
    raise SystemExit(cli_main())
