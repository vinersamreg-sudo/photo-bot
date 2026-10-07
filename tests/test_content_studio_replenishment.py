from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from unittest.mock import patch

from app.content_studio.config import ContentStudioSettings
from app.content_studio.content_generator import ContentGenerator
from app.content_studio.growth import FullAutoGrowthEngine
from app.content_studio.models import TransformationType
from app.content_studio.novelty import (
    PHASH_MAX_DISTANCE, NoveltyFingerprint, evaluate_fingerprint,
    normalize_caption, normalized_text_hash, perceptual_hash, phash_distance,
)
from app.content_studio.publisher import MaxPublisher, PlatformPublisher
from app.content_studio.quality import ContentQualityGate
from app.content_studio.service import ContentStudioService


ROOT = Path(__file__).resolve().parents[1]
APPROVED = ROOT / "marketing/assets/approved"


class ContentStudioReplenishmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = json.loads((ROOT / "marketing/content/library.json").read_text("utf-8"))["assets"]
        cls.new = [item for item in cls.assets if item["id"].startswith("october-")]

    def test_thirty_unique_checksum_bound_commercial_assets_pass_quality(self):
        self.assertEqual(len(self.new), 30)
        self.assertEqual(len(self.assets), 61)
        self.assertEqual(len({item["sha256"] for item in self.assets}), 61)
        self.assertEqual(set(Counter(item["category"] for item in self.new).values()), {3})
        self.assertEqual(len({item["category"] for item in self.new}), 10)
        rights = {r["file"]: r for r in json.loads((APPROVED / "manifest.json").read_text("utf-8"))["assets"]}
        for item in self.new:
            with self.subTest(asset=item["id"]):
                image = APPROVED / item["file"]
                self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), item["sha256"])
                self.assertEqual(rights[item["file"]]["sha256"], item["sha256"])
                self.assertTrue(rights[item["file"]]["commercial_use"])
                self.assertFalse(rights[item["file"]]["customer_data"])
                self.assertFalse(ContentQualityGate().assess_pair_card(image).blocked)

    def test_no_near_duplicates_against_old_pool_or_new_pool(self):
        hashes = {a["id"]: perceptual_hash(APPROVED / a["file"]) for a in self.assets}
        new_ids = {a["id"] for a in self.new}
        for left, right in combinations(self.assets, 2):
            if left["id"] not in new_ids and right["id"] not in new_ids:
                continue
            with self.subTest(left=left["id"], right=right["id"]):
                self.assertGreater(phash_distance(hashes[left["id"]], hashes[right["id"]]), PHASH_MAX_DISTANCE)

    def test_new_prompt_examples_reach_max_without_slugs_or_corruption(self):
        generator = ContentGenerator("https://max.ru/ravuna_bot")
        self.assertEqual(len({a["prompt_example"] for a in self.new}), 30)
        for asset in self.new:
            with self.subTest(asset=asset["id"]):
                copy = generator.generate_demo_case(
                    TransformationType(asset["transformation"]), post_id=asset["id"], platform="max",
                    title=asset["title"], hook=asset["hook"], prompt_example=asset["prompt_example"],
                    hashtags=tuple(asset["hashtags"]),
                )
                payload = MaxPublisher().dry_run(dict(id=asset["id"], title=copy.title, body=copy.body,
                    cta=copy.cta, hashtags_json=json.dumps(copy.hashtags), utm_url=copy.utm_url), "cards/synthetic.png").payload
                self.assertIn(asset["prompt_example"], payload["text"])
                self.assertNotIn("\ufffd", payload["text"])
                self.assertNotRegex(copy.title + copy.body, r"[a-z]+_[a-z]+")

    def test_replenished_pool_rebuilds_future_horizon_without_reusing_old_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = ContentStudioSettings(base_dir=root, database_path=root / "content.sqlite3",
                storage_dir=root / "storage", approved_assets_dir=APPROVED,
                content_library_path=ROOT / "marketing/content/library.json",
                production_database_path=root / "absent.sqlite3", publishing_enabled=True,
                full_auto_enabled=True, max_publishing_enabled=True)
            service = ContentStudioService(settings, publishers={"max": PlatformPublisher(platform="max", publishing_enabled=True)})
            engine = FullAutoGrowthEngine(service)
            used = {a["sha256"] for a in self.assets if not a["id"].startswith("october-")}
            now = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)
            with patch.object(service.repository, "reserved_asset_checksums", return_value=used):
                preview = engine.run(now=now, apply=False)
            self.assertEqual(preview["queue_health"]["status"], "HEALTHY")
            self.assertEqual(preview["queue_health"]["days_queued"], 7)
            self.assertEqual(preview["queue"]["skipped"], [])
            self.assertEqual(len(preview["queue"]["created_posts"]), 7)
            self.assertTrue(all("october-" in p for p in preview["queue"]["created_posts"]))
            self.assertTrue(preview["queue"]["created_posts"][0].startswith("max-2026-10-08-"))
            self.assertEqual(service.repository.status()["posts"], 0)

    def test_complete_new_pool_passes_existing_text_visual_and_rotation_gate(self):
        generator = ContentGenerator("https://max.ru/ravuna_bot")
        history = []
        for asset in self.new:
            copy = generator.generate_demo_case(
                TransformationType(asset["transformation"]), post_id=asset["id"],
                platform="max", title=asset["title"], hook=asset["hook"],
                prompt_example=asset["prompt_example"], hashtags=tuple(asset["hashtags"]),
            )
            text = normalize_caption(copy.title, copy.body)
            phash = perceptual_hash(APPROVED / asset["file"])
            candidate = NoveltyFingerprint(
                post_id=asset["id"], platform="max", asset_id=asset["id"],
                asset_checksum=asset["sha256"], transformation_type=asset["transformation"],
                category=asset["category"], before_phash=phash, after_phash=phash,
                card_phash=phash, normalized_text_hash=normalized_text_hash(text),
                normalized_text=text, scheduled_time=None,
            )
            decision = evaluate_fingerprint(candidate, history,
                alternative_categories={a["category"] for a in self.new})
            self.assertTrue(decision.allowed, decision.reason)
            if history:
                self.assertNotEqual(history[0]["category"], asset["category"])
            history.insert(0, candidate.record())


if __name__ == "__main__":
    unittest.main()
