"""Exec an Avito operator command with a private dotenv file, without shell parsing."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        raise SystemExit("usage: run_ravuna_avito_env.py ENV_FILE -- COMMAND [ARGS...]")
    env_path = Path(sys.argv[1])
    values = dotenv_values(env_path)
    environment = os.environ.copy()
    for name, value in values.items():
        if value is None:
            raise SystemExit(f"invalid environment entry: {name}")
        environment[name] = value
    os.execvpe(sys.argv[3], sys.argv[3:], environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
