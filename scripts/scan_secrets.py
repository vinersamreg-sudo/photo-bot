"""Fail when a tracked repository file appears to contain a private key."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    re.compile("sk-" + r"[A-Za-z0-9_-]{24,}"),
    re.compile("-----BEGIN " + r"(?:OPENSSH|RSA|EC|DSA) PRIVATE KEY-----"),
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in PATTERNS):
                findings.append(f"{path.relative_to(ROOT)}:{line_number}")
    if findings:
        print("Potential secret material detected in tracked files:")
        print("\n".join(findings))
        return 1
    print("Tracked-file secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
