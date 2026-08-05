import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from app.attribution import AttributionService, parse_start_payload
from app.commerce import CommerceService
from app.database import Database
from app.referrals import ReferralService


class ReferralTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "referrals.sqlite3")
        self.commerce = CommerceService(self.database)
        self.referrals = ReferralService(self.database, self.commerce)
        self.attribution = AttributionService(self.database)
        self.now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES('inviter','max','owner-max',?)",
                (self.now,),
            )
            self.commerce.ensure_initial_grant(connection, "inviter")

    def create_invitee(self, platform_id: str = "new-max", user_id: str = "invitee") -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?, 'max', ?, ?)",
                (user_id, platform_id, self.now),
            )
            self.commerce.ensure_initial_grant(connection, user_id)
            connection.execute(
                """INSERT INTO demo_sessions(
                       id,user_id,source_file_path,source_sha256,status,started_at,
                       expires_at,max_generations,created_at,updated_at
                   ) VALUES(?,?,'source','hash','active',?,?,2,?,?)""",
                (f"session-{user_id}", user_id, self.now, self.now, self.now, self.now),
            )

    def add_success(self, user_id: str = "invitee", status: str = "succeeded") -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO generation_attempts(
                       id,idempotency_key,session_id,user_id,prompt,status,started_at,
                       provider,model,source_path,correction,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,0,?)""",
                (
                    f"attempt-{user_id}-{status}",
                    f"idem-{user_id}-{status}",
                    f"session-{user_id}",
                    user_id,
                    "not stored in attribution",
                    status,
                    self.now,
                    "fake",
                    "fake",
                    "source",
                    self.now,
                ),
            )

    def test_payload_parser_is_allowlisted_and_first_touch_is_immutable(self) -> None:
        self.assertEqual(parse_start_payload("src_site").source, "site")
        self.assertEqual(parse_start_payload("src_partner_alpha").campaign, "alpha")
        self.assertEqual(parse_start_payload("src_../../bad").source, "direct")
        self.attribution.record_start("new-max", "src_site", event_key="start:1")
        self.attribution.record_start("new-max", "src_vk", event_key="start:2")
        with self.database.read() as connection:
            profile = connection.execute(
                "SELECT * FROM attribution_profiles WHERE platform_user_id='new-max'"
            ).fetchone()
        self.assertEqual(profile["first_source"], "site")
        self.assertEqual(profile["last_source"], "vk")

    def test_referral_reward_is_opaque_additive_and_idempotent(self) -> None:
        code = self.referrals.get_or_create_code("inviter")
        self.assertNotIn("owner-max", code)
        self.assertGreaterEqual(len(code), 10)
        relationship = self.referrals.register_start("new-max", code)
        self.assertIsNotNone(relationship)
        self.create_invitee()
        self.referrals.link_user("new-max", "invitee")
        self.add_success()
        before = self.commerce.balance("inviter").available

        with ThreadPoolExecutor(max_workers=2) as pool:
            rewards = list(pool.map(lambda _value: self.referrals.reward_first_success("invitee"), range(2)))

        self.assertEqual(sum(reward is not None for reward in rewards), 1)
        self.assertEqual(self.commerce.balance("inviter").available, before + 2)
        self.assertEqual(self.commerce.entitlement_balance("inviter").available, 0)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM bonus_credit_transactions").fetchone()[0],
                1,
            )

    def test_self_existing_and_failed_generation_do_not_reward(self) -> None:
        code = self.referrals.get_or_create_code("inviter")
        self.assertIsNone(self.referrals.register_start("owner-max", code))
        self.create_invitee("existing-max", "existing")
        self.assertIsNone(self.referrals.register_start("existing-max", code))

        self.assertIsNotNone(self.referrals.register_start("failed-max", code))
        self.create_invitee("failed-max", "failed")
        self.referrals.link_user("failed-max", "failed")
        self.add_success("failed", "delivery_failed")
        before = self.commerce.balance("inviter")
        self.assertIsNone(self.referrals.reward_first_success("failed"))
        self.assertEqual(self.commerce.balance("inviter"), before)

    def test_owner_report_contains_source_and_referral_counters(self) -> None:
        self.attribution.record_start("new-max", "src_site", event_key="start:site")
        self.attribution.record_event(
            "new-max", "first_generation_success", idempotency_key="success:site"
        )
        report = self.attribution.report(30)
        site = next(row for row in report["sources"] if row["source"] == "site")
        self.assertEqual(site["starts"], 1)
        self.assertEqual(site["first_edits"], 1)
