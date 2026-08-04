"""Run a focused unittest profile with concise, failure-first output."""

from __future__ import annotations

import argparse
import os
import py_compile
import re
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILES: dict[str, tuple[str, ...]] = {
    "max": (
        "tests.test_max_adapter",
        "tests.test_max_conversation",
        "tests.test_max_runtime",
        "tests.test_max_transport",
    ),
    "payments": (
        "tests.test_commerce",
        "tests.test_payment_admin",
        "tests.test_config",
        "tests.test_robokassa_sandbox_e2e",
    ),
    "openai": (
        "tests.test_edit_intent",
        "tests.test_image_provider",
        "tests.test_openai_client",
    ),
    "providers": (
        "tests.test_config",
        "tests.test_direct_prompt",
        "tests.test_image_provider",
        "tests.test_gemini_image_provider",
        "tests.test_provider_router",
        "tests.test_image_service",
        "tests.test_demo_service",
        "tests.test_max_application",
    ),
    "site": ("site/tests/test_site.py",),
    "storage": (
        "tests.test_demo_service",
        "tests.test_gallery",
        "tests.test_backup_maintenance_operations",
    ),
}
TAIL_LINES = 80


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


def _changed_python_files() -> list[Path]:
    commands = (
        ["git", "diff", "--name-only", "--diff-filter=ACMR"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    names: set[str] = set()
    for command in commands:
        code, output, _ = _run(command)
        if code == 0:
            names.update(line.strip() for line in output.splitlines() if line.strip())
    return sorted(
        path
        for name in names
        if name.endswith(".py") and (path := ROOT / name).is_file()
    )


def _compile_changed(paths: list[Path]) -> tuple[bool, str]:
    try:
        for path in paths:
            py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        return False, str(exc)
    return True, ""


def _failure(label: str, command: list[str], output: str) -> int:
    print(f"STATUS=FAIL step={label}")
    print("COMMAND=" + " ".join(command))
    lines = output.rstrip().splitlines()
    if lines:
        print("OUTPUT_TAIL:")
        print("\n".join(lines[-TAIL_LINES:]))
    print("RERUN=" + " ".join(command))
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", choices=sorted(PROFILES))
    parser.add_argument(
        "--test",
        action="append",
        default=[],
        help="dotted unittest module/class/test; may be repeated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.profile and not args.test:
        build_parser().error("choose a profile or provide --test")
    targets = list(PROFILES.get(args.profile, ())) + list(args.test)
    if args.profile == "site" and not args.test:
        test_command = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "site/tests",
            "-p",
            "test_site.py",
            "-q",
        ]
    else:
        test_command = [sys.executable, "-m", "unittest", "-q", *targets]
    code, output, test_seconds = _run(test_command)
    if code:
        return _failure("tests", test_command, output)

    changed = _changed_python_files()
    compiled, compile_error = _compile_changed(changed)
    if not compiled:
        return _failure("compile", ["py_compile", "<changed-python>"], compile_error)

    diff_command = ["git", "diff", "--check"]
    code, diff_output, diff_seconds = _run(diff_command)
    if code:
        return _failure("diff", diff_command, diff_output)

    match = re.search(r"Ran (\d+) tests? in ([0-9.]+)s", output)
    test_count = match.group(1) if match else "unknown"
    profile = args.profile or "custom"
    print("STATUS=OK")
    print(f"PROFILE={profile}")
    print(f"TARGETS={len(targets)}")
    print(f"TESTS={test_count}")
    print(f"TEST_SECONDS={test_seconds:.2f}")
    print(f"PY_COMPILE=OK files={len(changed)}")
    print(f"DIFF_CHECK=OK seconds={diff_seconds:.2f}")
    print(f"TOTAL_SECONDS={test_seconds + diff_seconds:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
