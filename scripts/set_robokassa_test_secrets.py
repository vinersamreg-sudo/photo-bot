"""Install or clear Robokassa sandbox credentials without command-line secrets."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path


NAMES = (
    "ROBOKASSA_MERCHANT_LOGIN",
    "ROBOKASSA_PASSWORD1",
    "ROBOKASSA_PASSWORD2",
)


def _read_values() -> tuple[str, str, str]:
    values = tuple(sys.stdin.readline().rstrip("\r\n") for _ in NAMES)
    if len(values) != len(NAMES) or any(not value for value in values):
        raise ValueError("All three Robokassa sandbox credentials are required")
    if any("\n" in value or "\r" in value for value in values):
        raise ValueError("Credential values must be single-line")
    return values  # type: ignore[return-value]


def update_env(path: Path, values: tuple[str, str, str] | None) -> None:
    """Atomically replace only the three sandbox credential entries."""

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefixes = tuple(f"{name}=" for name in NAMES)
    lines = [line for line in existing if not line.startswith(prefixes)]
    if values is not None:
        lines.extend(f"{name}={value}" for name, value in zip(NAMES, values))
    payload = "\n".join(lines) + ("\n" if lines else "")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".env.robokassa.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    update_env(args.env_file, None if args.clear else _read_values())
    print("robokassa_test_credentials=" + ("cleared" if args.clear else "configured"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
