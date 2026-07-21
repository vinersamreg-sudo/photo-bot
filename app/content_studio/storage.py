"""Private, content-addressed image storage for demonstration materials."""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


ALLOWED_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


@dataclass(frozen=True)
class StoredImage:
    relative_path: str
    checksum: str
    width: int
    height: int
    image_format: str


class ContentStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def import_image(
        self,
        source: Path,
        *,
        namespace: str,
        item_id: str,
        stem: str,
    ) -> StoredImage:
        source = source.expanduser().resolve()
        if not source.is_file() or source.is_symlink():
            raise ValueError("image source must be a regular file")
        with Image.open(source) as image:
            image.verify()
        with Image.open(source) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
        if image_format not in ALLOWED_FORMATS:
            raise ValueError("Content Studio accepts JPEG, PNG or WEBP images")
        if width < 256 or height < 256:
            raise ValueError("demonstration images must be at least 256x256")
        checksum = _sha256(source)
        relative = Path(namespace) / item_id / f"{stem}-{checksum[:12]}{ALLOWED_FORMATS[image_format]}"
        destination = self.resolve(relative.as_posix())
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, destination)
            try:
                destination.chmod(0o600)
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return StoredImage(
            relative_path=relative.as_posix(),
            checksum=checksum,
            width=width,
            height=height,
            image_format=image_format,
        )

    def output_path(self, namespace: str, item_id: str, filename: str) -> tuple[str, Path]:
        relative = (Path(namespace) / item_id / filename).as_posix()
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        return relative, path

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ValueError("content storage path escapes its root") from error
        return candidate

    def remove_tree(self, namespace: str, item_id: str) -> None:
        target = self.resolve((Path(namespace) / item_id).as_posix())
        if target.exists():
            shutil.rmtree(target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
