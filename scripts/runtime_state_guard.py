"""Snapshot and exactly restore safety-sensitive production runtime settings."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path


PROTECTED_KEYS = (
    "MAX_PUBLIC_ACCESS_ENABLED",
    "MAX_POLL_OBSERVE_ONLY",
    "MAX_TRANSPORT_MODE",
    "PAYMENTS_ENABLED",
    "PAYMENT_PROVIDER",
    "PAYMENT_WEBHOOK_ENABLED",
    "PAYMENT_REFUNDS_ENABLED",
    "ROBOKASSA_MODE",
    "ROBOKASSA_PRODUCTION_APPROVED",
    "PILOT_USER_LIMIT",
    "MAX_PILOT_USER_IDS",
    "OPENAI_IMAGE_REQUESTS_ENABLED",
    "IMAGE_PROVIDER",
    "GEMINI_IMAGE_MODEL",
    "IMAGE_DIRECT_PROMPT_ENABLED",
    "ROBOKASSA_SANDBOX_DUPLICATE_PROBE",
    "ROBOKASSA_SANDBOX_ORDER_BASELINE",
)


def _protected_values(path: Path) -> dict[str, str | None]:
    values: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value
    return {key: values.get(key) for key in PROTECTED_KEYS}


def snapshot(env_path: Path, snapshot_path: Path) -> dict[str, object]:
    if not env_path.is_file():
        raise FileNotFoundError(f"Runtime env is missing: {env_path}")
    if snapshot_path.exists():
        raise FileExistsError(f"Snapshot already exists: {snapshot_path}")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(env_path, snapshot_path)
    os.chmod(snapshot_path, 0o600)
    return {
        "action": "snapshot",
        "saved": True,
        "protected_key_count": len(PROTECTED_KEYS),
    }


def restore(env_path: Path, snapshot_path: Path) -> dict[str, object]:
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"Runtime snapshot is missing: {snapshot_path}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".env.restore-", dir=env_path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(snapshot_path, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, env_path)
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(env_path, 0o600)
    return {
        "action": "restore",
        "restored": True,
        "protected_key_count": len(PROTECTED_KEYS),
    }


def verify(
    env_path: Path, snapshot_path: Path, *, delete_on_success: bool = False
) -> dict[str, object]:
    expected = _protected_values(snapshot_path)
    actual = _protected_values(env_path)
    mismatched_keys = [
        key for key in PROTECTED_KEYS if expected[key] != actual[key]
    ]
    if not mismatched_keys and delete_on_success:
        snapshot_path.unlink(missing_ok=True)
    return {
        "action": "verify",
        "matches": not mismatched_keys,
        "mismatched_keys": mismatched_keys,
        "snapshot_deleted": bool(delete_on_success and not mismatched_keys),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runtime-state-guard")
    parser.add_argument("action", choices=("snapshot", "restore", "verify"))
    parser.add_argument("--env", dest="env_path", required=True, type=Path)
    parser.add_argument("--snapshot", dest="snapshot_path", required=True, type=Path)
    parser.add_argument("--delete-on-success", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "snapshot":
            result = snapshot(args.env_path, args.snapshot_path)
        elif args.action == "restore":
            result = restore(args.env_path, args.snapshot_path)
        else:
            result = verify(
                args.env_path,
                args.snapshot_path,
                delete_on_success=args.delete_on_success,
            )
    except (OSError, UnicodeError) as exc:
        print(
            json.dumps(
                {
                    "action": args.action,
                    "ok": False,
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("matches", True) else 3


if __name__ == "__main__":
    raise SystemExit(main())
