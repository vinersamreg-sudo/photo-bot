"""Paginated watermarked contact sheets for MAX work and version galleries."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.database import Database


PAGE_SIZE = 6


@dataclass(frozen=True)
class GalleryThumbnail:
    id: str
    preview_path: Optional[Path]
    title: str
    created_at: str
    status: str


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
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM gallery_items WHERE user_id=? AND deleted=0",
                    (user_id,),
                ).fetchone()[0]
            )
            pages = max(1, math.ceil(total / PAGE_SIZE))
            selected_page = min(max(page, 1), pages)
            rows = connection.execute(
                """SELECT id,title,cover_preview_path,created_at,
                          CASE WHEN generation_count>0 THEN 'Готово' ELSE 'В работе' END AS status
                   FROM gallery_items WHERE user_id=? AND deleted=0
                   ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?""",
                (user_id, PAGE_SIZE, (selected_page - 1) * PAGE_SIZE),
            ).fetchall()
        return GalleryPage(
            tuple(
                GalleryThumbnail(
                    row["id"],
                    Path(row["cover_preview_path"]) if row["cover_preview_path"] else None,
                    row["title"],
                    row["created_at"],
                    row["status"],
                )
                for row in rows
            ),
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
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM gallery_versions WHERE gallery_item_id=?",
                    (item_id,),
                ).fetchone()[0]
            )
            pages = max(1, math.ceil(total / PAGE_SIZE))
            selected_page = min(max(page, 1), pages)
            rows = connection.execute(
                """SELECT id,version_number,preview_watermarked_path,created_at,status
                   FROM gallery_versions WHERE gallery_item_id=?
                   ORDER BY version_number DESC LIMIT ? OFFSET ?""",
                (item_id, PAGE_SIZE, (selected_page - 1) * PAGE_SIZE),
            ).fetchall()
        return GalleryPage(
            tuple(
                GalleryThumbnail(
                    row["id"],
                    Path(row["preview_watermarked_path"])
                    if row["preview_watermarked_path"]
                    else None,
                    f"Версия {row['version_number']}",
                    row["created_at"],
                    row["status"],
                )
                for row in rows
            ),
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
        signature = hashlib.sha256(
            "|".join(
                f"{entry.id}:{self._stamp(entry.preview_path)}"
                for entry in page.entries
            ).encode("utf-8")
        ).hexdigest()[:12]
        destination = self.cache_dir / (
            f"{prefix}-p{page.page}-r{revision}-{signature}.jpg"
        )
        if destination.is_file():
            return destination

        canvas = Image.new("RGB", (960, 640), "#eef1f5")
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default()
        cell_width, cell_height = 320, 320
        for index in range(PAGE_SIZE):
            left = (index % 3) * cell_width
            top = (index // 3) * cell_height
            entry = page.entries[index] if index < len(page.entries) else None
            tile = self._thumbnail(entry.preview_path if entry else None, (304, 304))
            canvas.paste(tile, (left + 8, top + 8))
            if entry:
                draw.rounded_rectangle(
                    (left + 18, top + 18, left + 70, top + 70),
                    radius=12,
                    fill="#111827",
                )
                label = str(index + 1)
                draw.text(
                    (left + 39, top + 44),
                    label,
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
    def _thumbnail(path: Optional[Path], size: tuple[int, int]) -> Image.Image:
        if path and path.is_file():
            try:
                with Image.open(path) as source:
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
            except (OSError, ValueError):
                pass
        placeholder = Image.new("RGB", size, "#d8dee8")
        draw = ImageDraw.Draw(placeholder)
        draw.rectangle((24, 24, size[0] - 24, size[1] - 24), outline="#9aa5b5", width=4)
        draw.line((40, size[1] - 50, size[0] // 2, size[1] // 2), fill="#9aa5b5", width=5)
        draw.line((size[0] // 2, size[1] // 2, size[0] - 40, size[1] - 50), fill="#9aa5b5", width=5)
        return placeholder

    @staticmethod
    def _stamp(path: Optional[Path]) -> str:
        if not path or not path.is_file():
            return "missing"
        stat = path.stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    def _cleanup(self, prefix: str, keep: int) -> None:
        files = sorted(
            self.cache_dir.glob(f"{prefix}-*.jpg"),
            key=lambda value: value.stat().st_mtime_ns,
            reverse=True,
        )
        for path in files[keep:]:
            path.unlink(missing_ok=True)
