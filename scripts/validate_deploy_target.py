"""Fail closed unless a full deployment SHA belongs to canonical main history."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(root: Path, *arguments: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def validate_target(root: Path, target_sha: str, canonical_ref: str) -> tuple[bool, str, int | None]:
    if not FULL_SHA.fullmatch(target_sha):
        return False, "target_sha_invalid", None
    target_code, _ = _git(root, "cat-file", "-e", f"{target_sha}^{{commit}}")
    canonical_code, canonical_sha = _git(root, "rev-parse", "--verify", f"{canonical_ref}^{{commit}}")
    if target_code:
        return False, "target_sha_missing", None
    if canonical_code or not FULL_SHA.fullmatch(canonical_sha):
        return False, "canonical_ref_missing", None
    ancestor_code, _ = _git(root, "merge-base", "--is-ancestor", target_sha, canonical_sha)
    if ancestor_code:
        return False, "target_outside_canonical_history", None
    count_code, count = _git(root, "rev-list", "--count", f"{target_sha}..{canonical_sha}")
    if count_code or not count.isdigit():
        return False, "canonical_distance_unavailable", None
    return True, "ok", int(count)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--canonical-ref", default="origin/main")
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    valid, reason, commits_ahead = validate_target(
        args.repository_root.resolve(), args.target_sha, args.canonical_ref
    )
    print(f"DEPLOY_TARGET_VALID={'yes' if valid else 'no'}")
    print(f"DEPLOY_TARGET_REASON={reason}")
    if commits_ahead is not None:
        print(f"CANONICAL_MAIN_AHEAD={commits_ahead}")
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
