from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from PIL import Image, ImageDraw

from app.commerce import CommerceService
from app.config import Settings
from app.database import Database, ReadOnlyDatabase
from app.payments import build_payment_service
from app.content_studio.config import ContentStudioSettings
from app.content_studio.growth import FullAutoGrowthEngine
from app.content_studio.publisher import PlatformPublisher
from app.content_studio.service import ContentStudioService
from app.content_studio.transports import ContentPublishingError, ContentPublicationUncertainError, VkCommunityTransport, build_publishers
from app.content_studio.vk_report import _bonuses, build_vk_report
from app.content_studio.vk_rewards import ExistingLedgerRewardGrant, VkIdentityApi, VkRewardError, VkRewardSettings, VkSubscriptionRewards
from app.content_studio.video import VerticalPairVideoSpec, VerticalVideoGenerator
from tests.test_content_studio_transports import MemoryDeliveries

ROOT = Path(__file__).resolve().parents[1]


class VkChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = ContentStudioSettings(
            base_dir=self.root, database_path=self.root / "content.sqlite3", storage_dir=self.root / "storage",
            approved_assets_dir=ROOT / "marketing/assets/approved", content_library_path=ROOT / "marketing/content/library.json",
            production_database_path=self.root / "missing.sqlite3", full_auto_enabled=True,
            publishing_enabled=True, vk_publishing_enabled=True,
        )

    def engine(self):
        service = ContentStudioService(self.settings, publishers={
            "max": PlatformPublisher(platform="max", publishing_enabled=False),
            "vk": PlatformPublisher(platform="vk", publishing_enabled=True),
        })
        engine = FullAutoGrowthEngine(service)
        def render(spec):
            spec.output.parent.mkdir(parents=True, exist_ok=True)
            spec.output.write_bytes(b"synthetic-video")
            return spec.output
        engine.video.render_pair = render
        engine.quality.assess_video = lambda *_a, **_k: dict(decoded=True, codec="h264", width=1080, height=1920, duration_seconds=13)
        return engine

    def test_queue_daily_photos_three_videos_separate_and_no_backfill(self):
        engine = self.engine()
        now = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
        value = engine.maintain_queue(now=now, apply=True)
        self.assertEqual(value["health"]["status"], "HEALTHY")
        posts = engine.repository.queue(limit=100)
        week = [p for p in posts if p["scheduled_time"] < "2026-10-19"]
        self.assertEqual(sum(p["id"].startswith("vk-post-") for p in week), 7)
        self.assertEqual(sum(p["id"].startswith("vk-video-") for p in week), 3)
        self.assertEqual(len(engine.repository.reserved_asset_checksums("vk")), len(posts))
        self.assertTrue(all(p["platform"] == "vk" and "2 фотографии бесплатно" in p["cta"] for p in posts))
        self.assertTrue(all(p["scheduled_time"] > now.isoformat() for p in posts))
        self.assertEqual(engine.maintain_queue(now=now, apply=True)["created_posts"], [])

    def test_durable_vk_outbox_survives_restart_and_claim_lease_is_vk_only(self):
        engine = self.engine()
        post_id = engine._ensure_post(engine.library[0], platform="vk", media_kind="image",
            planned=datetime(2026, 10, 12).date(), clock="18:30", apply=True)
        repo = engine.repository
        repo.prepare_vk_delivery(post_id, "123", "photo-123_9")
        self.assertTrue(repo.begin_vk_send(post_id))
        self.assertFalse(repo.begin_vk_send(post_id))
        restarted = type(repo)(repo.path)
        self.assertEqual(restarted.vk_delivery(post_id)["state"], "sending")
        self.assertTrue(repo.claim_publication(post_id, "old"))
        self.assertFalse(repo.claim_publication(post_id, "concurrent"))
        with repo.transaction() as c:
            c.execute("UPDATE content_publication_slots SET claimed_at='2020-01-01T00:00:00+00:00' WHERE post_id=?", (post_id,))
        self.assertTrue(repo.claim_publication(post_id, "recovery"))
        repo.confirm_vk_send(post_id, "wall:77")
        self.assertEqual(restarted.vk_delivery(post_id)["external_id"], "wall:77")

    def test_invalid_vk_configuration_does_not_disable_max(self):
        settings = replace(self.settings, max_publishing_enabled=True, max_channel_id="test-channel", max_bot_token="test-token")
        publishers = build_publishers(settings)
        self.assertIsNone(publishers["vk"].transport)
        self.assertIsNotNone(publishers["max"].transport)
        self.assertTrue(publishers["max"].publishing_enabled)

    def test_vk_publication_error_does_not_abort_following_max_post(self):
        engine = self.engine()
        with patch.object(engine.repository, "due_posts", return_value=[{"id": "vk-fail"}, {"id": "max-ok"}]), \
             patch.object(engine, "_validate_staged_post"), \
             patch.object(engine.service, "publication", side_effect=[ContentPublishingError("VK unavailable"), SimpleNamespace(external_id="max-confirmed")]):
            result = engine.publish_due(apply=True)
        self.assertEqual(result["failed"][0]["post_id"], "vk-fail")
        self.assertEqual(result["published"], [{"post_id": "max-ok", "external_id": "max-confirmed"}])

    def test_vk_timeout_is_held_then_reconciled_without_resending(self):
        payload = dict(platform="vk", post_id="test-post", text="Пример\nhttps://max.ru/bot?start=src_vk-test",
            media_path=str(ROOT / "marketing/assets/approved" / json.loads((ROOT / "marketing/content/library.json").read_text("utf-8"))["assets"][0]["file"]),
            source_code="src_vk-test")
        # A local media fixture, not a user photo.
        media = self.root / "photo.png"
        media.write_bytes(b"synthetic-placeholder")
        payload["media_path"] = str(media)
        repo = MemoryDeliveries()
        repo.prepare_vk_delivery("test-post", "123", "photo-123_9")
        calls, published = [], []
        def handler(request):
            method = request.url.path.rsplit("/", 1)[-1]
            calls.append(method)
            if method == "account.getAppPermissions":
                return httpx.Response(200, json={"response": 8212})
            if method == "groups.getById":
                return httpx.Response(200, json={"response": [{"id": 123, "can_post": 1, "can_upload_video": 1}]})
            if method == "wall.get":
                return httpx.Response(200, json={"response": {"items": published}})
            if method == "wall.post":
                raise httpx.ReadTimeout("private-token-must-not-leak", request=request)
            return httpx.Response(500)
        transport = VkCommunityTransport("test-token", "123", self.root, delivery_repository=repo,
            api_client=httpx.Client(base_url="https://api.vk.com/method/", transport=httpx.MockTransport(handler)))
        with self.assertRaises(ContentPublishingError) as failed:
            transport.publish(payload)
        self.assertNotIn("private-token", str(failed.exception))
        self.assertEqual(repo.vk_delivery("test-post")["state"], "sending")
        with self.assertRaises(ContentPublicationUncertainError):
            transport.publish(payload)
        published.append(dict(id=77, from_id=-123, text=payload["text"]))
        self.assertEqual(transport.publish(payload), "wall:77")
        self.assertEqual(transport.publish(payload), "wall:77")
        self.assertEqual(calls.count("wall.post"), 1)

    def test_structured_api_rejection_is_retryable_and_unknown_is_not(self):
        transport = VkCommunityTransport("test-token", "123", self.root,
            api_client=httpx.Client(base_url="https://api.vk.com/method/", transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"error": {"error_code": 6, "error_msg": "secret"}}))))
        with self.assertRaises(ContentPublishingError) as failure:
            transport._call("wall.post", {})
        self.assertTrue(failure.exception.confirmed_rejection)
        self.assertEqual(failure.exception.api_code, 6)
        self.assertNotIn("secret", failure.exception.diagnostic())
        transport.api_client = httpx.Client(base_url="https://api.vk.com/method/", transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"error": "malformed private response"})))
        with self.assertRaises(ContentPublishingError) as failure:
            transport._call("wall.post", {})
        self.assertFalse(failure.exception.confirmed_rejection)
        transport.api_client = httpx.Client(base_url="https://api.vk.com/method/", transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"error": {"error_code": 10}})))
        with self.assertRaises(ContentPublishingError) as failure:
            transport._call("wall.post", {})
        self.assertFalse(failure.exception.confirmed_rejection)

    def test_permissions_and_upload_destination_fail_closed(self):
        for url in ("http://upload.vk.com/x", "https://secret@upload.vk.com/x", "https://upload.vk.com:444/x", "https://vk.com.evil.test/x"):
            with self.assertRaises(ContentPublishingError):
                VkCommunityTransport._validate_upload_url(url)
        settings = ContentStudioSettings.from_environment(self.root, {})
        self.assertFalse(settings.vk_publishing_enabled)
        self.assertEqual(settings.vk_video_weekdays, (0, 2, 4))

    def test_long_task_video_caption_is_bounded_without_changing_post(self):
        generator = VerticalVideoGenerator(self.root, self.root)
        media = self.root / "pair.png"
        media.write_bytes(b"fixture")
        command = generator.pair_command(VerticalPairVideoSpec(media, self.root / "video.mp4",
            "Пример восстановления", "Восстанови фотографию: " + "сохрани естественные детали " * 12))
        self.assertIn("1080:1920", command[command.index("-filter_complex") + 1])
        self.assertIn("…", command[command.index("-filter_complex") + 1])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg runtime not installed locally")
    def test_real_ffmpeg_output_is_1080x1920_13sec_before_then_after(self):
        media = self.root / "pair.png"
        image = Image.new("RGB", (320, 160), "blue")
        ImageDraw.Draw(image).rectangle((160, 0, 319, 159), fill="yellow")
        image.save(media)
        generator = VerticalVideoGenerator(self.root, self.root)
        spec = VerticalPairVideoSpec(media, self.root / "render.mp4", "До и после",
            "Замени фон: сохрани объект и естественный свет.", cta="Обработайте 2 фото бесплатно")
        output = generator.render_pair(spec)
        proof = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)]))
        video = proof["streams"][0]
        self.assertEqual((video["width"], video["height"], video["codec_name"]), (1080, 1920, "h264"))
        self.assertAlmostEqual(float(proof["format"]["duration"]), 13, delta=0.1)
        pixels = []
        for second in (1, 5):
            frame = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", str(second), "-i", str(output),
                "-frames:v", "1", "-vf", "crop=2:2:540:960", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
            pixels.append(tuple(frame[:3]))
        self.assertGreater(pixels[0][2], pixels[0][0])
        self.assertGreater(pixels[1][0], pixels[1][2])


class VkRewardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = Database(root / "main.sqlite3")
        self.database.initialize()
        with self.database.transaction() as c:
            for user in ("alice", "bob"):
                c.execute("INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,'max',?,?)", (user, user, "2026-10-11T00:00:00+00:00"))
        self.settings = VkRewardSettings(root / "bridge" / "vk_rewards.sqlite3", enabled=True, group_id="123",
            client_id="456", redirect_uri="https://example.test/vk/callback", handoff_key="x" * 32, membership_token="synthetic-token")
        class Api:
            user, member = "100", True
            def verified_identity(self, **_k): return self.user
            def is_member(self, _user): return self.member
        self.api = Api()
        self.commerce = CommerceService(self.database)
        self.bridge = VkSubscriptionRewards(self.settings, identity_api=self.api,
            grant=ExistingLedgerRewardGrant(self.database, self.commerce), clock=lambda: 1000)

    def authorize(self, user="alice"):
        handoff = self.bridge.issue_max_handoff(user)
        url = self.bridge.begin(handoff)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["code_challenge_method"], ["s256"])
        self.assertNotIn(user, url)
        return query["state"][0]

    def complete(self, state):
        return self.bridge.complete(state=state, code="synthetic-code", device_id="synthetic-device")

    def test_disabled_means_no_database_and_no_api_or_grant(self):
        self.bridge.settings = replace(self.settings, enabled=False)
        with self.assertRaises(VkRewardError): self.bridge.issue_max_handoff("alice")
        with self.assertRaises(VkRewardError): self.complete("state")
        self.assertFalse(self.settings.database_path.exists())
        self.assertFalse(VkRewardSettings.from_environment(self.settings.database_path.parent, {}).enabled)

    def test_authentic_handoff_replay_and_expiration(self):
        handoff = self.bridge.issue_max_handoff("alice")
        handoff["user"] = "bob"
        with self.assertRaises(VkRewardError): self.bridge.begin(handoff)
        handoff = self.bridge.issue_max_handoff("alice")
        self.bridge.begin(handoff)
        with self.assertRaises(VkRewardError): self.bridge.begin(handoff)
        state = self.authorize()
        self.bridge.clock = lambda: 1601
        with self.assertRaises(VkRewardError): self.complete(state)

    def test_one_max_and_one_vk_once_preserves_two_free_and_distinct_audit(self):
        state = self.authorize()
        self.assertEqual(self.complete(state), "GRANTED")
        with self.assertRaises(VkRewardError): self.complete(state)
        self.assertEqual(self.complete(self.authorize()), "ALREADY_GRANTED")
        self.assertEqual(self.complete(self.authorize("bob")), "ALREADY_CLAIMED")
        self.api.user = "101"
        self.assertEqual(self.complete(self.authorize()), "ALREADY_CLAIMED")
        with self.database.read() as c:
            self.assertEqual(c.execute("SELECT available_generation_credits FROM user_credit_accounts WHERE user_id='alice'").fetchone()[0], 3)
            rows = c.execute("SELECT delta,reason FROM commerce_admin_audit").fetchall()
            self.assertEqual([(r[0], r[1]) for r in rows], [(1, "vk_subscription_verified")])
        with closing(sqlite3.connect(self.settings.database_path)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM vk_subscription_rewards").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM vk_reward_sessions WHERE verifier<>'' AND claimed=1").fetchone()[0], 0)

    def test_unsubscribed_or_api_error_never_grants(self):
        self.api.member = False
        self.assertEqual(self.complete(self.authorize()), "NOT_SUBSCRIBED")
        self.api.member = True
        self.api.is_member = lambda _u: (_ for _ in ()).throw(VkRewardError("API unavailable"))
        with self.assertRaises(VkRewardError): self.complete(self.authorize())
        with self.database.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM commerce_admin_audit").fetchone()[0], 0)

    def test_crash_after_ledger_commit_retries_same_key_without_double_grant(self):
        actual = self.bridge.grant
        def crash(**kwargs):
            actual(**kwargs)
            raise RuntimeError("simulated crash")
        self.bridge.grant = crash
        with self.assertRaises(RuntimeError): self.complete(self.authorize())
        self.bridge.grant = actual
        self.assertEqual(self.complete(self.authorize()), "GRANTED")
        with self.database.read() as c:
            self.assertEqual(c.execute("SELECT available_generation_credits FROM user_credit_accounts WHERE user_id='alice'").fetchone()[0], 3)

    def test_bonus_report_counts_verified_receipt_not_referral_or_account_balance(self):
        self.assertEqual(self.complete(self.authorize()), "GRANTED")
        with self.database.transaction() as c:
            c.execute("""INSERT INTO attribution_profiles(platform_user_id,user_id,first_source,first_campaign,
                first_started_at,last_source,last_started_at,created_at,updated_at)
                VALUES('alice','alice','campaign','vk-test','2026-10-11','campaign','2026-10-11','2026-10-11','2026-10-11')""")
        output = {"vk-post-test": {}}
        with ReadOnlyDatabase(self.database.path).read() as c:
            _bonuses(c, self.settings.database_path, [{"id": "vk-post-test", "source_code": "src_vk-test"}], set(), output,
                datetime.fromtimestamp(900, timezone.utc), datetime.fromtimestamp(1100, timezone.utc))
        self.assertEqual(output["vk-post-test"], dict(verified_subscriptions=1, bonus_granted=1, bonus_consumed=0))

    def test_identity_key_rotation_fails_closed_instead_of_resetting_vk_uniqueness(self):
        self.assertEqual(self.complete(self.authorize()), "GRANTED")
        self.bridge.settings = replace(self.settings, handoff_key="y" * 32)
        with self.assertRaises(VkRewardError):
            self.authorize()
        with self.database.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM commerce_admin_audit WHERE reason='vk_subscription_verified'").fetchone()[0], 1)

    def test_vk_id_exchange_checks_state_identity_and_uses_post_body(self):
        requests = []
        def handler(r):
            requests.append(r)
            if r.url.path.endswith("/auth"):
                return httpx.Response(200, json=dict(state="state", user_id=100, access_token="synthetic-access"))
            return httpx.Response(200, json=dict(user=dict(user_id="100")))
        api = VkIdentityApi(self.settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual(api.verified_identity(code="code", state="state", device_id="device", verifier="verifier"), "100")
        self.assertTrue(all("synthetic-access" not in str(r.url) for r in requests))
        with self.assertRaises(VkRewardError):
            api.verified_identity(code="code", state="wrong", device_id="device", verifier="verifier")


class VkReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        main = Settings("", "fake", "test", self.root, payments_enabled=True, payment_provider="robokassa",
            robokassa_merchant_login="synthetic", robokassa_password1="synthetic-one", robokassa_password2="synthetic-two")
        self.database = Database(main.database_path)
        self.database.initialize()
        self.now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        with self.database.transaction() as c:
            for user in ("owner", "buyer"):
                c.execute("INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,'max',?,?)", (user, "platform-" + user, self.now.isoformat()))
                c.execute("""INSERT INTO attribution_profiles(platform_user_id,user_id,first_source,first_campaign,first_started_at,last_source,last_started_at,created_at,updated_at)
                    VALUES(?,?,'campaign','vk-test',?,'campaign',?,?,?)""", ("platform-" + user, user, *([self.now.isoformat()] * 4)))
        service = build_payment_service(main, self.database, clock=lambda: self.now)
        for user in ("owner", "buyer"):
            order = service.create_order(user, None, user, account_purchase=True)
            signature = hashlib.sha256(f"49.00:{order.provider_invoice_id}:synthetic-two:Shp_order={order.public_token}".encode()).hexdigest()
            response = service.process_webhook({"OutSum": "49.00", "InvId": str(order.provider_invoice_id), "Shp_order": order.public_token,
                "SignatureValue": signature}, method="POST", path="/result")
            self.assertTrue(response.accepted)
        self.settings = ContentStudioSettings(self.root, self.root / "content.sqlite3", self.root / "storage", ROOT / "marketing/assets/approved",
            production_database_path=main.database_path, excluded_max_user_ids=("platform-owner",))
        studio = ContentStudioService(self.settings)
        # Only synthetic reporting rows; fixtures never touch production.
        with closing(sqlite3.connect(self.settings.database_path)) as c, c:
            c.execute("""INSERT INTO demo_posts(id,result_id,title,body,hashtags_json,cta,disclosure,publish_status,published_time,platform,utm_url,source_code,generator_prompt_en,created_at,updated_at,published_external_id)
                VALUES('vk-post-test','fixture','title','body','[]','cta','','published',?,'vk','https://max.ru/test','src_vk-test','',?,?,'wall:77')""", (self.now.isoformat(), self.now.isoformat(), self.now.isoformat()))
        studio.repository.record_platform_metrics("vk-post-test", {"views": 100, "reactions": 2})

    def test_readonly_ledger_dedup_owner_exclusion_and_no_pii(self):
        before = hashlib.sha256(self.database.path.read_bytes()).hexdigest()
        content_before = hashlib.sha256(self.settings.database_path.read_bytes()).hexdigest()
        report = build_vk_report(self.settings, None, days=7, at=datetime(2026, 10, 12, tzinfo=timezone.utc))
        row = report["posts"][0]
        self.assertEqual(row["new_max_users"], 1)
        self.assertEqual(row["first_purchases"], 1)
        self.assertEqual(row["confirmed_gross_revenue_minor"], 4900)
        self.assertEqual(row["clicks"], "NOT MEASURABLE")
        self.assertEqual(row["views"], 100)
        self.assertEqual(row["bonus_consumed"], "NOT MEASURABLE")
        rendered = json.dumps(report)
        for private in ("platform-owner", "buyer", "synthetic-two", str(self.root)):
            self.assertNotIn(private, rendered)
        self.assertEqual(hashlib.sha256(self.database.path.read_bytes()).hexdigest(), before)
        self.assertEqual(hashlib.sha256(self.settings.database_path.read_bytes()).hexdigest(), content_before)
        with ReadOnlyDatabase(self.database.path).read() as c:
            self.assertEqual(c.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError): c.execute("DELETE FROM users")

    def test_unknown_owner_mapping_fails_closed_commercial_metrics(self):
        report = build_vk_report(replace(self.settings, excluded_max_user_ids=()), None, at=datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(report["posts"][0]["confirmed_gross_revenue_minor"], "NOT MEASURABLE")

    def test_payment_before_campaign_touch_is_not_vk_revenue(self):
        with self.database.transaction() as c:
            c.execute("UPDATE attribution_profiles SET first_started_at='2026-10-11T13:00:00+00:00' WHERE user_id='buyer'")
        report = build_vk_report(self.settings, None, at=datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(report["posts"][0]["confirmed_gross_revenue_minor"], 0)
        self.assertEqual(report["posts"][0]["first_purchases"], 0)

    def test_touch_before_post_publication_is_not_attributed(self):
        with self.database.transaction() as c:
            c.execute("UPDATE attribution_profiles SET first_started_at='2026-10-10T13:00:00+00:00' WHERE user_id='buyer'")
        report = build_vk_report(self.settings, None, at=datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(report["posts"][0]["new_max_users"], 0)
        self.assertEqual(report["posts"][0]["confirmed_gross_revenue_minor"], 0)

    def test_vk_report_cli_does_not_initialize_service(self):
        from app.content_studio.cli import main
        with patch("app.content_studio.cli.ContentStudioSettings.from_environment", return_value=self.settings), \
             patch("app.content_studio.cli.ContentStudioService", side_effect=AssertionError("must remain read-only")), \
             patch("app.content_studio.cli._print"):
            self.assertEqual(main(["content", "vk-report", "--days", "7"]), 0)


if __name__ == "__main__":
    unittest.main()
