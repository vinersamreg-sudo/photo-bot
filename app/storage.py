"""Private filesystem storage for source, original and preview images."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from app.domain import InvalidInputError


SUPPORTED_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


class PrivateStorage:
    def __init__(self, users_dir: Path, max_source_bytes: int) -> None:
        self.users_dir = users_dir
        self.max_source_bytes = max_source_bytes
        self._secure_directory(users_dir)

    @staticmethod
    def _secure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass

    def validate_source(self, source: Path) -> tuple[str, str, int]:
        if not source.is_file():
            raise InvalidInputError("Source image does not exist")
        size = source.stat().st_size
        if size <= 0 or size > self.max_source_bytes:
            raise InvalidInputError("Source image size is outside the allowed limit")
        try:
            with Image.open(source) as image:
                detected_format = str(image.format or "").upper()
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise InvalidInputError("Source file is not a valid supported image") from exc
        if detected_format not in SUPPORTED_FORMATS:
            raise InvalidInputError("Source image format is not supported")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return digest, SUPPORTED_FORMATS[detected_format], size

    def create_session(self, user_id: str, session_id: str, source: Path) -> tuple[Path, str, int]:
        digest, extension, size = self.validate_source(source)
        root = self.session_root(user_id, session_id)
        for directory in (
            self.users_dir / user_id,
            self.users_dir / user_id / "demo_sessions",
            root,
        ):
            self._secure_directory(directory)
        for name in ("source", "originals", "previews"):
            self._secure_directory(root / name)
        destination = root / "source" / f"source{extension}"
        shutil.copyfile(source, destination)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
        return destination, digest, size

    def session_root(self, user_id: str, session_id: str) -> Path:
        return self.users_dir / user_id / "demo_sessions" / session_id

    def gallery_item_root(self, user_id: str, gallery_item_id: str) -> Path:
        return self.users_dir / user_id / "gallery" / gallery_item_id

    def create_gallery_item_source(
        self, user_id: str, gallery_item_id: str, source: Path
    ) -> tuple[Path, str, int]:
        digest, extension, size = self.validate_source(source)
        root = self.gallery_item_root(user_id, gallery_item_id)
        for directory in (
            self.users_dir / user_id,
            self.users_dir / user_id / "gallery",
            root,
            root / "source",
            root / "versions",
            root / "metadata",
        ):
            self._secure_directory(directory)
        destination = root / "source" / f"source{extension}"
        shutil.copyfile(source, destination)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
        return destination, digest, size

    def gallery_version_paths(
        self, user_id: str, gallery_item_id: str, version_id: str
    ) -> dict[str, Path]:
        root = self.gallery_item_root(user_id, gallery_item_id) / "versions" / version_id
        for directory in (root, root / "original", root / "preview"):
            self._secure_directory(directory)
        return {
            "root": root,
            "original": root / "original" / "result.png",
            "preview_small": root / "preview" / "small.jpg",
            "preview_large": root / "preview" / "large.jpg",
            "metadata": root / "metadata.json",
        }

    def original_path(self, user_id: str, session_id: str, attempt_id: str, extension: str = ".png") -> Path:
        return self.session_root(user_id, session_id) / "originals" / f"{attempt_id}{extension}"

    def preview_path(self, user_id: str, session_id: str, attempt_id: str, extension: str) -> Path:
        return self.session_root(user_id, session_id) / "previews" / f"{attempt_id}{extension}"

    @staticmethod
    def write_private(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(content)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)

    def write_metadata(self, user_id: str, session_id: str, metadata: dict[str, Any]) -> Path:
        path = self.session_root(user_id, session_id) / "metadata.json"
        self.write_private(path, json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"))
        return path

    def append_attempt_metadata(self, user_id: str, session_id: str, attempt: dict[str, Any]) -> None:
        path = self.session_root(user_id, session_id) / "metadata.json"
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            current = {"session_id": session_id}
        current.setdefault("attempts", []).append(attempt)
        self.write_private(
            path,
            json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def delete_session(self, user_id: str, session_id: str) -> None:
        root = self.session_root(user_id, session_id).resolve()
        allowed = self.users_dir.resolve()
        if allowed not in root.parents:
            raise RuntimeError("Refusing to delete outside private users storage")
        if root.exists():
            shutil.rmtree(root)

    def delete_private_tree(self, root: Path) -> None:
        resolved = root.resolve()
        allowed = self.users_dir.resolve()
        if allowed not in resolved.parents:
            raise RuntimeError("Refusing to delete outside private users storage")
        if resolved.exists():
            shutil.rmtree(resolved)
