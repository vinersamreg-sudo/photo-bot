"""Paginated watermarked contact sheets for MAX work and version galleries."""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.database import Database


PAGE_SIZE = 6
CANVAS_SIZE = (960, 640)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GalleryThumbnail:
    id: str
    preview_path: Path
    title: str
    created_at: str
    status: str
    preview_revision: str


@dataclass(frozen=True)
class GalleryPage:
    entries: tuple[GalleryThumbnail, ...]
    page: int
    pages: int
    total: int


class WorkGallery:
    def __init__(self, database: Database, cache_dir: Path) -> None:
        self.database = database
        self.cache_dir = cache_dir / "max-contact-sheets"

    def works(self, user_id: str, page: int) -> GalleryPage:
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT i.id,i.title,i.created_at,i.updated_at,
                          v.id AS version_id,v.preview_watermarked_path,
                          v.created_at AS version_created_at,v.status
                   FROM gallery_items AS i
                   JOIN gallery_versions AS v ON v.id=i.current_best_version_id
                   LEFT JOIN generation_attempts AS a ON a.id=v.attempt_id
                   WHERE i.user_id=? AND i.deleted=0
                     AND v.status='succeeded'
                     AND (a.id IS NULL OR a.status='succeeded')
                   ORDER BY i.updated_at DESC,i.id DESC""",
                (user_id,),
            ).fetchall()
        entries = tuple(
            GalleryThumbnail(
                row["id"],
                Path(row["preview_watermarked_path"]),
                row["title"],
                row["created_at"],
                row["status"],
                f"{row['version_id']}:{row['version_created_at']}:{row['updated_at']}",
            )
            for row in rows
            if self._preview_is_available(row["preview_watermarked_path"])
        )
        self._record_skipped("works", len(rows) - len(entries))
        total = len(entries)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        selected_page = min(max(page, 1), pages)
        offset = (selected_page - 1) * PAGE_SIZE
        return GalleryPage(
            entries[offset : offset + PAGE_SIZE],
            selected_page,
            pages,
            total,
        )

    def versions(self, user_id: str, item_id: str, page: int) -> GalleryPage:
        with self.database.read() as connection:
            owner = connection.execute(
                "SELECT 1 FROM gallery_items WHERE id=? AND user_id=? AND deleted=0",
                (item_id, user_id),
            ).fetchone()
            if owner is None:
                return GalleryPage((), 1, 1, 0)
            rows = connection.execute(
                """SELECT v.id,v.version_number,v.preview_watermarked_path,
                          v.created_at,v.status,v.attempt_id
                   FROM gallery_versions AS v
                   LEFT JOIN generation_attempts AS a ON a.id=v.attempt_id
                   WHERE v.gallery_item_id=? AND v.status='succeeded'
                     AND (a.id IS NULL OR a.status='succeeded')
                   ORDER BY v.version_number DESC""",
                (item_id,),
            ).fetchall()
        entries = tuple(
            GalleryThumbnail(
                row["id"],
                Path(row["preview_watermarked_path"]),
                f"Версия {row['version_number']}",
                row["created_at"],
                row["status"],
                f"{row['id']}:{row['created_at']}",
            )
            for row in rows
            if self._preview_is_available(row["preview_watermarked_path"])
        )
        self._record_skipped("versions", len(rows) - len(entries))
        total = len(entries)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        selected_page = min(max(page, 1), pages)
        offset = (selected_page - 1) * PAGE_SIZE
        return GalleryPage(
            entries[offset : offset + PAGE_SIZE],
            selected_page,
            pages,
            total,
        )

    def contact_sheet(
        self,
        scope: str,
        page: GalleryPage,
        revision: int,
    ) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        prefix = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
        layout = self._layout_boxes(len(page.entries))
        signature_source = "|".join(
            (
                scope,
                f"page={page.page}/{page.pages}",
                f"canvas={CANVAS_SIZE[0]}x{CANVAS_SIZE[1]}",
                f"layout={layout}",
                *(f"{entry.id}:{entry.preview_revision}:{self._stamp(entry.preview_path)}"
                  for entry in page.entries),
            )
        )
        signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()[:16]
        destination = self.cache_dir / (
            f"{prefix}-p{page.page}-r{revision}-{signature}.jpg"
        )
        if destination.is_file():
            return destination

        canvas = Image.new("RGB", CANVAS_SIZE, "#eef1f5")
        draw = ImageDraw.Draw(canvas)
        font = self._badge_font()
        for index, (entry, box) in enumerate(zip(page.entries, layout), 1):
            left, top, right, bottom = box
            tile = self._thumbnail(
                entry.preview_path,
                (right - left - 16, bottom - top - 16),
            )
            canvas.paste(tile, (left + 8, top + 8))
            badge_size = 76
            draw.rounded_rectangle(
                (left + 20, top + 20, left + 20 + badge_size, top + 20 + badge_size),
                radius=18,
                fill="#111827",
                outline="white",
                width=3,
            )
            draw.text(
                (left + 20 + badge_size // 2, top + 20 + badge_size // 2),
                str(index),
                anchor="mm",
                fill="white",
                font=font,
            )
        temporary = destination.with_suffix(".tmp.jpg")
        canvas.save(temporary, "JPEG", quality=84, optimize=True)
        temporary.replace(destination)
        self._cleanup(prefix, keep=12)
        return destination

    @staticmethod
    def _thumbnail(path: Path, size: tuple[int, int]) -> Image.Image:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)

    @staticmethod
    def _layout_boxes(count: int) -> tuple[tuple[int, int, int, int], ...]:
        if count == 1:
            return ((0, 0, 960, 640),)
        if count == 2:
            return ((0, 0, 480, 640), (480, 0, 960, 640))
        if count == 3:
            return tuple((index * 320, 0, (index + 1) * 320, 640) for index in range(3))
        if count == 4:
            return tuple(
                ((index % 2) * 480, (index // 2) * 320,
                 (index % 2 + 1) * 480, (index // 2 + 1) * 320)
                for index in range(4)
            )
        if count == 5:
            return (
                (0, 0, 320, 320), (320, 0, 640, 320), (640, 0, 960, 320),
                (0, 320, 480, 640), (480, 320, 960, 640),
            )
        if count == 6:
            return tuple(
                ((index % 3) * 320, (index // 3) * 320,
                 (index % 3 + 1) * 320, (index // 3 + 1) * 320)
                for index in range(6)
            )
        raise ValueError("Contact sheet requires between one and six previews")

    @staticmethod
    def _badge_font() -> ImageFont.ImageFont:
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
        except OSError:
            return ImageFont.load_default()

    @staticmethod
    def _preview_is_available(value: object) -> bool:
        if not value:
            return False
        path = Path(str(value))
        try:
            if path.is_symlink() or not path.is_file():
                return False
            with Image.open(path) as preview:
                preview.verify()
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _record_skipped(kind: str, count: int) -> None:
        if count:
            LOGGER.warning(
                "MAX gallery skipped unavailable previews (kind=%s,count=%s)",
                kind,
                count,
            )

    @staticmethod
    def _stamp(path: Optional[Path]) -> str:
        try:
            if not path or path.is_symlink() or not path.is_file():
                return "missing"
            stat = path.stat()
            return f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            return "missing"

    def _cleanup(self, prefix: str, keep: int) -> None:
        files = sorted(
            self.cache_dir.glob(f"{prefix}-*.jpg"),
            key=lambda value: value.stat().st_mtime_ns,
            reverse=True,
        )
        for path in files[keep:]:
            path.unlink(missing_ok=True)
