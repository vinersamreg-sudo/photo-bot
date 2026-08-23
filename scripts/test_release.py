"""Run the complete local release gate once with concise output."""

from __future__ import annotations

import compileall
import re
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TAIL_LINES = 100


def _run(command: list[str]) -> tuple[int, str, float]:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return completed.returncode, completed.stdout, time.perf_counter() - started


def _safe_print(value: str) -> None:
    try:
        print(value)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(value.encode(encoding, errors="backslashreplace").decode(encoding))


def _failed(label: str, command: list[str], output: str) -> int:
    print(f"STATUS=FAIL step={label}")
    print("COMMAND=" + " ".join(command))
    lines = output.rstrip().splitlines()
    if lines:
        print("OUTPUT_TAIL:")
        _safe_print("\n".join(lines[-TAIL_LINES:]))
    print("RERUN=" + " ".join(command))
    return 1


def _count_tests(output: str) -> int | None:
    match = re.search(r"Ran (\d+) tests? in ([0-9.]+)s", output)
    return int(match.group(1)) if match else None


def main() -> int:
    started = time.perf_counter()
    steps: list[tuple[str, list[str]]] = [
        (
            "backend_tests",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        ),
        (
            "site_tests",
            [sys.executable, "-m", "unittest", "discover", "-s", "site/tests", "-q"],
        ),
        ("secret_scan", [sys.executable, "scripts/scan_secrets.py"]),
        ("diff_check", ["git", "diff", "--check"]),
    ]
    timings: dict[str, float] = {}
    test_counts: dict[str, int | None] = {}
    for label, command in steps:
        code, output, seconds = _run(command)
        timings[label] = seconds
        if label.endswith("_tests"):
            test_counts[label] = _count_tests(output)
        if code:
            return _failed(label, command, output)

    compile_started = time.perf_counter()
    compile_ok = all(
        compileall.compile_dir(
            ROOT / directory,
            quiet=2,
            force=False,
        )
        for directory in ("app", "scripts", "tests", "site/tests")
    )
    timings["compile"] = time.perf_counter() - compile_started
    if not compile_ok:
        return _failed("compile", ["compileall", "app", "scripts", "tests", "site/tests"], "")

    print("STATUS=OK")
    print(f"BACKEND_TESTS={test_counts['backend_tests'] or 'unknown'}")
    print(f"SITE_TESTS={test_counts['site_tests'] or 'unknown'}")
    print(f"BACKEND_SECONDS={timings['backend_tests']:.2f}")
    print(f"SITE_SECONDS={timings['site_tests']:.2f}")
    print(f"SECRET_SCAN=OK seconds={timings['secret_scan']:.2f}")
    print(f"PY_COMPILE=OK seconds={timings['compile']:.2f}")
    print(f"DIFF_CHECK=OK seconds={timings['diff_check']:.2f}")
    print(f"TOTAL_SECONDS={time.perf_counter() - started:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
