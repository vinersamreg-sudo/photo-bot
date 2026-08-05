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

    def seed_works(
        self,
        quantity: int,
        *,
        status: str = "succeeded",
        missing_preview_indexes: tuple[int, ...] = (),
        start_index: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            for index in range(start_index, start_index + quantity):
                preview = self.base / f"preview-{index}.jpg"
                if index not in missing_preview_indexes:
                    Image.new("RGB", (80, 60), (index % 255, 80, 120)).save(preview)
                version_id = f"work-version-{index}"
                connection.execute(
                    """INSERT INTO gallery_items(
                           id,gallery_id,user_id,title,created_at,updated_at,
                           original_source_path,storage_root_path,cover_preview_path,
                           generation_count,current_best_version_id,retention_until
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                        1 if status == "succeeded" else 0,
                        version_id,
                        now,
                    ),
                )
                connection.execute(
                    """INSERT INTO gallery_versions(
                           id,gallery_item_id,version_number,source_path,prompt,
                           effective_prompt,provider,model,preview_watermarked_path,
                           original_path,created_at,status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        version_id,
                        f"work-{index}",
                        1,
                        "private-source",
                        "prompt",
                        "prompt",
                        "fake",
                        "fake",
                        str(preview),
                        f"private-original-{index}",
                        now,
                        status,
                    ),
                )

    def test_zero_one_six_seven_and_twenty_work_pagination(self) -> None:
        self.assertEqual(self.gallery.works("u", 1).total, 0)
        for quantity, pages, last_size in ((1, 1, 1), (6, 1, 6), (7, 2, 1), (20, 4, 2)):
            with self.database.transaction() as connection:
                connection.execute("DELETE FROM gallery_versions")
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

    def test_only_ready_works_with_existing_preview_are_paginated(self) -> None:
        self.seed_works(7, missing_preview_indexes=(1,))
        self.seed_works(1, status="processing", start_index=7)
        self.seed_works(1, status="failed", start_index=8)
        self.seed_works(1, status="rejected", start_index=9)
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO gallery_items(
                       id,gallery_id,user_id,title,created_at,updated_at,
                       original_source_path,storage_root_path,generation_count,
                       retention_until
                   ) VALUES('source-only','g','u','Исходник',?,?,?,?,0,?)""",
                (now, now, "private-source", "private-root", now),
            )

        first = self.gallery.works("u", 1)
        second = self.gallery.works("u", 2)

        self.assertEqual(first.total, 6)
        self.assertEqual(first.pages, 1)
        self.assertEqual(second.page, 1)
        self.assertNotIn("work-1", {entry.id for entry in first.entries})
        self.assertTrue(all(entry.status == "succeeded" for entry in first.entries))

    def test_dynamic_layout_has_no_placeholder_cells(self) -> None:
        for count in range(1, 7):
            self.assertEqual(len(self.gallery._layout_boxes(count)), count)
        self.assertEqual(len(self.gallery._layout_boxes(5)), 5)

    def test_corrupt_preview_is_skipped_without_a_slot(self) -> None:
        self.seed_works(1)
        (self.base / "preview-0.jpg").write_bytes(b"not-an-image")

        page = self.gallery.works("u", 1)

        self.assertEqual(page.total, 0)
        self.assertEqual(page.entries, ())

    def test_pages_have_distinct_ids_cache_keys_and_twenty_split(self) -> None:
        self.seed_works(20)
        pages = [self.gallery.works("u", page) for page in range(1, 5)]
        self.assertEqual([len(page.entries) for page in pages], [6, 6, 6, 2])
        self.assertTrue(
            set(entry.id for entry in pages[0].entries).isdisjoint(
                entry.id for entry in pages[1].entries
            )
        )
        first_path = self.gallery.contact_sheet("works:u:chat", pages[0], 1)
        second_path = self.gallery.contact_sheet("works:u:chat", pages[1], 2)
        self.assertNotEqual(first_path, second_path)
        self.assertNotEqual(first_path.read_bytes(), second_path.read_bytes())

    def test_version_gallery_is_paginated_from_watermarked_previews(self) -> None:
        self.seed_works(1)
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM gallery_versions WHERE gallery_item_id='work-0'")
            connection.execute(
                "UPDATE gallery_items SET current_best_version_id=NULL WHERE id='work-0'"
            )
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
        self.assertTrue(
            {entry.id for entry in first.entries}.isdisjoint(
                entry.id for entry in second.entries
            )
        )
        first_sheet = self.gallery.contact_sheet("versions:u:chat:work-0", first, 1)
        second_sheet = self.gallery.contact_sheet("versions:u:chat:work-0", second, 2)
        self.assertNotEqual(first_sheet, second_sheet)
        self.assertNotEqual(first_sheet.read_bytes(), second_sheet.read_bytes())

    def test_version_gallery_skips_failed_and_missing_previews(self) -> None:
        self.seed_works(1)
        now = datetime.now(timezone.utc).isoformat()
        missing = self.base / "missing-version.jpg"
        with self.database.transaction() as connection:
            for index, status in ((2, "failed"), (3, "rejected"), (4, "succeeded")):
                connection.execute(
                    """INSERT INTO gallery_versions(
                           id,gallery_item_id,version_number,source_path,prompt,
                           effective_prompt,provider,model,preview_watermarked_path,
                           original_path,created_at,status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"bad-version-{index}", "work-0", index, "private-source",
                        "prompt", "prompt", "fake", "fake", str(missing),
                        "private-original", now, status,
                    ),
                )

        page = self.gallery.versions("u", "work-0", 1)

        self.assertEqual(page.total, 1)
        self.assertEqual([entry.id for entry in page.entries], ["work-version-0"])
