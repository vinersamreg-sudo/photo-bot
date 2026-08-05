"""Print a privacy-safe, read-only Ravuna production status summary."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


FLAG_KEYS = (
    "MAX_PUBLIC_ACCESS_ENABLED",
    "MAX_SINGLE_SCREEN_UI_ENABLED",
    "MAX_POLL_OBSERVE_ONLY",
    "MAX_TRANSPORT_MODE",
    "PAYMENTS_ENABLED",
    "PAYMENT_PROVIDER",
    "PAYMENT_WEBHOOK_ENABLED",
    "PAYMENT_REFUNDS_ENABLED",
    "ROBOKASSA_MODE",
    "ROBOKASSA_PRODUCTION_APPROVED",
    "OPENAI_IMAGE_REQUESTS_ENABLED",
    "IMAGE_PROVIDER",
    "GEMINI_IMAGE_MODEL",
    "IMAGE_DIRECT_PROMPT_ENABLED",
    "IMAGE_SUBJECT_PRESERVE_GUARD_ENABLED",
    "IMAGE_FACE_PRESERVE_GUARD_ENABLED",
    "PILOT_USER_LIMIT",
)


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in FLAG_KEYS or key.strip() == "PAYMENT_RESULT_URL":
            values[key.strip()] = value.strip()
    return values


def _secret_is_configured(path: Path, key: str) -> bool:
    if not path.is_file():
        return False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return bool(value.strip())
    return False


def _run(command: list[str], *, cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return completed.returncode, completed.stdout


def _python(root: Path) -> Path:
    candidates = (
        root / "venv" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
        root / "venv" / "Scripts" / "python.exe",
    )
    return next((path for path in candidates if path.is_file()), Path(sys.executable))


def _systemd_property(root: Path, name: str) -> str:
    if os.name == "nt":
        return "unavailable"
    code, output = _run(
        ["systemctl", "show", "photo-bot.service", f"--property={name}", "--value"],
        cwd=root,
    )
    return output.strip() if code == 0 and output.strip() else "unavailable"


def _launch_report(root: Path) -> dict[str, Any]:
    code, output = _run(
        [str(_python(root)), "-m", "app.main", "launch-status"],
        cwd=root,
        timeout=60,
    )
    if code:
        return {}
    start = output.find("{")
    if start < 0:
        return {}
    try:
        value = json.loads(output[start:])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _resulturl_status(url: str) -> str:
    if not url.startswith("https://"):
        return "invalid"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return str(response.status)
    except urllib.error.HTTPError as exc:
        return str(exc.code)
    except (OSError, urllib.error.URLError):
        return "unavailable"


def _sha(root: Path) -> str:
    for path in (root / ".deploy-sha", root / "data" / "deployed_commit.txt"):
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value[:12]
    code, output = _run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root)
    return output.strip() if code == 0 else "unavailable"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--online", action="store_true", help="perform safe ResultURL GET")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    env = _read_env(root / ".env")
    report = _launch_report(root)
    database = report.get("database", {})
    processing = report.get("processing", {})
    cleanup = report.get("cleanup", {})
    health_code, _ = _run([str(_python(root)), "-m", "app.main", "health"], cwd=root)

    active_state = _systemd_property(root, "ActiveState")
    main_pid = _systemd_property(root, "MainPID")
    restarts = _systemd_property(root, "NRestarts")
    runtime_count = "1" if main_pid not in {"0", "unavailable", ""} else "unavailable"
    orphan = cleanup.get("remaining_orphan_count")
    if orphan is None:
        orphan = cleanup.get("orphan_candidate_count", "unavailable")

    lines = [
        f"SHA={_sha(root)}",
        f"SYSTEMD={active_state}",
        f"RUNTIME_COUNT={runtime_count}",
        f"RESTARTS={restarts}",
        f"HEALTH={'OK' if health_code == 0 else 'FAIL'}",
        f"SQLITE={database.get('quick_check', 'unavailable')}",
        f"PROCESSING={processing.get('active_attempts', 'unavailable')}",
        f"ORPHAN={orphan}",
    ]
    for key in FLAG_KEYS:
        lines.append(f"{key}={env.get(key, 'missing')}")
    lines.append(
        "GEMINI_API_KEY_CONFIGURED="
        + ("yes" if _secret_is_configured(root / ".env", "GEMINI_API_KEY") else "no")
    )
    result_url = env.get("PAYMENT_RESULT_URL", "")
    lines.append(f"RESULT_URL_CONFIGURED={'yes' if result_url else 'no'}")
    lines.append(
        f"RESULTURL_GET={_resulturl_status(result_url) if args.online and result_url else 'skipped'}"
    )
    print("\n".join(lines))
    return 0 if health_code == 0 and database.get("quick_check") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
