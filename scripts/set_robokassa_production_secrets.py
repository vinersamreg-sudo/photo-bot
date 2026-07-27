"""Install production Robokassa credentials from stdin without exposing values."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.set_robokassa_test_secrets import NAMES, update_env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    values = tuple(sys.stdin.readline().rstrip("\r\n") for _ in NAMES)
    if len(values) != len(NAMES) or any(not value for value in values):
        raise ValueError("All three Robokassa production credentials are required")
    if any("\n" in value or "\r" in value for value in values):
        raise ValueError("Credential values must be single-line")
    update_env(args.env_file, values)  # type: ignore[arg-type]
    print("robokassa_production_credentials=configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
