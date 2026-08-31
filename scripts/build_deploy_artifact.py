"""Build a byte-exact Git-object release; verify it without Git on the VPS."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


def _git(repository: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), "-c", "core.autocrlf=false", "-c", "core.eol=lf", *arguments],
        check=True, capture_output=True,
    ).stdout


def build_release(repository: Path, revision: str, archive: Path, manifest: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("an exact commit SHA is required")
    if _git(repository, "rev-parse", f"{revision}^{{commit}}").decode().strip() != revision:
        raise ValueError("commit does not match the requested SHA")
    entries = {}
    for entry in _git(repository, "ls-tree", "-rz", revision).split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        name = name.decode("utf-8")
        path = PurePosixPath(name)
        if kind != "blob" or mode not in {"100644", "100755"} or path.is_absolute() or ".." in path.parts:
            raise ValueError("unsupported release member")
        if path.parts[0] in {".env", ".git", "venv", "data", "logs", "temp"} and name not in {"data/.gitkeep", "logs/.gitkeep", "temp/.gitkeep"}:
            raise ValueError("mutable or private content must not enter a release")
        entries[name] = {"blob": oid, "executable": mode == "100755"}
    archive.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "archive", "--format=tar.gz", f"--output={archive.resolve()}", revision)
    with tarfile.open(archive, "r:gz") as bundle:
        members = {item.name: item for item in bundle.getmembers() if not item.isdir()}
        if set(members) != set(entries):
            raise ValueError("archive members differ from Git tree")
        for name, member in members.items():
            if not member.isfile():
                raise ValueError("release symlinks are forbidden")
            contents = bundle.extractfile(member).read()
            blob = hashlib.sha1(b"blob " + str(len(contents)).encode() + b"\0" + contents).hexdigest()
            if blob != entries[name]["blob"]:
                raise ValueError("archive bytes differ from committed Git blob")
            if bool(member.mode & 0o111) != entries[name]["executable"]:
                raise ValueError("archive executable bit differs from Git tree")
            if name.endswith(".sh") and b"\r\n" in contents:
                raise ValueError("committed shell script contains CRLF")
            entries[name]["sha256"] = hashlib.sha256(contents).hexdigest()
    report = {"revision": revision, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "files": entries}
    manifest.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    return report


def verify_release(directory: Path, archive: Path, manifest: Path) -> dict:
    report = json.loads(manifest.read_text(encoding="utf-8"))
    if hashlib.sha256(archive.read_bytes()).hexdigest() != report["archive_sha256"]:
        raise ValueError("archive checksum mismatch")
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file() or p.is_symlink()}
    if actual != set(report["files"]):
        raise ValueError("unexpected or missing staged file")
    for name, entry in report["files"].items():
        path = directory / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("staged path escapes artifact")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("staged bytes differ from committed artifact")
        if os.name == "posix" and bool(path.stat().st_mode & 0o111) != entry["executable"]:
            raise ValueError("staged executable bit differs from committed artifact")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--revision")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--verify-directory", type=Path)
    args = parser.parse_args()
    try:
        report = (verify_release(args.verify_directory, args.archive, args.manifest)
                  if args.verify_directory else build_release(args.repository, args.revision or "", args.archive, args.manifest))
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError):
        print("committed_artifact=FAIL")
        return 1
    print(f"committed_artifact=PASS revision={report['revision']} files={len(report['files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
