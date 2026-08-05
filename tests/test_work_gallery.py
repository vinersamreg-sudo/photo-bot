import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase

from PIL import Image

from app.database import Database
from app.work_gallery import WorkGallery


class WorkGalleryTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.database = Database(self.base / "gallery.sqlite3")
        self.gallery = WorkGallery(self.database, self.base / "temp")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES('u','max','p',?)",
                (now,),
            )
            connection.execute(
                "INSERT INTO galleries(id,user_id,created_at,updated_at) VALUES('g','u',?,?)",
                (now, now),
            )

    def seed_works(self, quantity: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            for index in range(quantity):
                preview = self.base / f"preview-{index}.jpg"
                Image.new("RGB", (80, 60), (index % 255, 80, 120)).save(preview)
                connection.execute(
                    """INSERT INTO gallery_items(
                           id,gallery_id,user_id,title,created_at,updated_at,
                           original_source_path,storage_root_path,cover_preview_path,
                           generation_count,retention_until
                       ) VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
                    (
                        f"work-{index}",
                        "g",
                        "u",
                        f"Работа {index}",
                        now,
                        f"{now}-{index:02d}",
                        "private-source",
                        "private-root",
                        str(preview),
                        now,
                    ),
                )

    def test_zero_one_six_seven_and_twenty_work_pagination(self) -> None:
        self.assertEqual(self.gallery.works("u", 1).total, 0)
        for quantity, pages, last_size in ((1, 1, 1), (6, 1, 6), (7, 2, 1), (20, 4, 2)):
            with self.database.transaction() as connection:
                connection.execute("DELETE FROM gallery_items")
            self.seed_works(quantity)
            first = self.gallery.works("u", 1)
            last = self.gallery.works("u", pages)
            self.assertEqual(first.pages, pages)
            self.assertEqual(len(last.entries), last_size)
            self.assertLessEqual(len(first.entries), 6)

    def test_contact_sheet_uses_current_page_previews_and_is_cached(self) -> None:
        self.seed_works(7)
        page = self.gallery.works("u", 2)
        first = self.gallery.contact_sheet("works:u", page, 4)
        second = self.gallery.contact_sheet("works:u", page, 4)
        self.assertEqual(first, second)
        self.assertTrue(first.is_file())
        with Image.open(first) as image:
            self.assertEqual(image.size, (960, 640))

    def test_version_gallery_is_paginated_from_watermarked_previews(self) -> None:
        self.seed_works(1)
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            for index in range(7):
                preview = self.base / f"version-preview-{index}.jpg"
                Image.new("RGB", (60, 80), (80, index, 120)).save(preview)
                connection.execute(
                    """INSERT INTO gallery_versions(
                           id,gallery_item_id,version_number,source_path,prompt,
                           effective_prompt,provider,model,preview_watermarked_path,
                           original_path,created_at,status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'succeeded')""",
                    (
                        f"version-{index}",
                        "work-0",
                        index + 1,
                        "private-source",
                        "prompt",
                        "prompt",
                        "fake",
                        "fake",
                        str(preview),
                        f"private-original-{index}",
                        now,
                    ),
                )
        first = self.gallery.versions("u", "work-0", 1)
        second = self.gallery.versions("u", "work-0", 2)
        self.assertEqual((first.pages, len(first.entries)), (2, 6))
        self.assertEqual(len(second.entries), 1)
        self.assertTrue(all("version-preview" in str(entry.preview_path) for entry in first.entries))
