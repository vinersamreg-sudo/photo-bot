"""Fingerprint an operator-provisioned environment without parsing or rewriting it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path


def fingerprint(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("environment must be a regular, non-symlink file")
    contents = path.read_bytes()
    if not contents.strip():
        raise ValueError("environment must be provisioned separately before deployment")
    return {
        "sha256": hashlib.sha256(contents).hexdigest(),
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }


def capture(path: Path, snapshot: Path) -> None:
    value = fingerprint(path)
    # No credentials or parsed values are stored in the snapshot.
    descriptor = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)


def verify(path: Path, snapshot: Path) -> None:
    expected = json.loads(snapshot.read_text(encoding="utf-8"))
    if fingerprint(path) != expected:
        raise ValueError("environment bytes, permissions or ownership changed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("capture", "verify"))
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    try:
        {"capture": capture, "verify": verify}[args.operation](args.env_file, args.snapshot)
    except (OSError, ValueError):
        print("environment_preservation=FAIL (requires operator review; no rewrite performed)")
        return 1
    print("environment_preservation=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
