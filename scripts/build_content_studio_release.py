from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path


REQUIRED_MEMBERS = {
    "app/content_studio/cli.py",
    "marketing/assets/approved/manifest.json",
    "marketing/content/library.json",
    "ops/deploy_ravuna_content_studio.sh",
    "ops/ravuna-content-publisher.service",
    "ops/ravuna-content-publisher.timer",
    "requirements.txt",
}


def build_release(repository: Path, revision: str, output: Path) -> dict[str, object]:
    """Build and validate a release from committed Git content only."""

    repository = repository.resolve()
    output = output.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be a full lowercase commit SHA")
    resolved = _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if resolved.strip() != revision:
        raise ValueError("revision does not resolve to the requested commit")

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "archive",
                "--format=tar.gz",
                f"--output={temporary}",
                revision,
            ],
            check=True,
            capture_output=True,
        )
        shell_count = _validate_archive(temporary)
        checksum = hashlib.sha256(temporary.read_bytes()).hexdigest()
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "revision": revision,
        "sha256": checksum,
        "shell_scripts": shell_count,
        "source": "committed_git_content",
    }


def _validate_archive(archive: Path) -> int:
    with tarfile.open(archive, "r:gz") as bundle:
        members = {member.name: member for member in bundle.getmembers()}
        missing = sorted(REQUIRED_MEMBERS - members.keys())
        if missing:
            raise ValueError("release archive is missing required members")
        shell_members = [member for member in members.values() if member.name.endswith(".sh")]
        for member in shell_members:
            extracted = bundle.extractfile(member)
            if extracted is None or b"\r\n" in extracted.read():
                raise ValueError(f"release shell script is not LF-only: {member.name}")
    return len(shell_members)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            build_release(args.repository, args.revision, args.output),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
