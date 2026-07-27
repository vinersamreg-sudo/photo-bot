"""Record an operator-confirmed OpenAI balance without claiming live API data."""

from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


NAMES = ("OPENAI_BALANCE_USD", "OPENAI_BALANCE_CONFIRMED_AT")


def update_confirmation(path: Path, balance: str, confirmed_at: str) -> None:
    try:
        parsed_balance = Decimal(balance)
    except InvalidOperation as exc:
        raise ValueError("Balance must be a decimal number") from exc
    if parsed_balance < 0:
        raise ValueError("Balance must be non-negative")
    try:
        timestamp = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Confirmation time must be ISO-8601") from exc
    if timestamp.tzinfo is None:
        raise ValueError("Confirmation time must include a timezone")

    path.parent.mkdir(parents=True, exist_ok=True)
    original_stat = path.stat() if path.exists() else None
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefixes = tuple(f"{name}=" for name in NAMES)
    lines = [line for line in existing if not line.startswith(prefixes)]
    normalized_balance = format(parsed_balance, "f")
    normalized_timestamp = timestamp.astimezone(timezone.utc).isoformat()
    lines.extend(
        (
            f"OPENAI_BALANCE_USD={normalized_balance}",
            f"OPENAI_BALANCE_CONFIRMED_AT={normalized_timestamp}",
        )
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".env.openai-balance.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        if original_stat is not None and os.name != "nt":
            os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openai-balance-confirm")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--balance-usd", required=True)
    parser.add_argument("--confirmed-at")
    args = parser.parse_args(argv)
    confirmed_at = args.confirmed_at or datetime.now(timezone.utc).isoformat()
    update_confirmation(args.env_file, args.balance_usd, confirmed_at)
    print("openai_balance_confirmation=recorded source=manual")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
