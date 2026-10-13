import hashlib
import json
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.commerce import CommerceService, SMALL_PACKAGE, LARGE_PACKAGE, MAX_ACCOUNT_BALANCE
from app.database import Database, schema_version
from app.feedback_report import ReportUnavailable
from app.max_adapter import REFERRAL_V2_BUTTON, main_menu, result_actions, gallery_item_actions
from app.max_application import MaxApplication
from app.max_transport import MaxIncomingEvent
from app.referrals import ReferralService
from app.referral_report import build_report


class ReferralV2Tests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture.sqlite3"
        self.db = Database(self.path)
        self.now = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
        self.commerce = CommerceService(self.db, lambda: self.now)
        with self.db.transaction() as c:
            for user in ("inviter", "owner"):
                c.execute("INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,'max',?,?)", (user, user + "-max", self.now.isoformat()))
                self.commerce.ensure_initial_grant(c, user)
        self.referrals = ReferralService(self.db, self.commerce, v2_enabled=True, clock=lambda: self.now)
        self.code = self.referrals.get_or_create_code("inviter")

    def register(self, platform="friend"):
        relationship = self.referrals.register_start(platform, self.code, event_key="start:" + platform)
        with self.db.read() as c:
            row = c.execute("SELECT * FROM referral_relationships WHERE id=?", (relationship,)).fetchone()
        return relationship, row["invitee_user_id"]

    def success(self, user, *, delivered=True, cost=0.1):
        now = self.now.isoformat()
        with self.db.transaction() as c:
            c.execute("""INSERT INTO demo_sessions(id,user_id,source_file_path,source_sha256,status,started_at,expires_at,max_generations,created_at,updated_at)
                VALUES(?,?,'synthetic','hash','active',?,?,2,?,?)""", ("session-" + user, user, now, now, now, now))
            c.execute("""INSERT INTO generation_attempts(id,idempotency_key,session_id,user_id,prompt,status,started_at,completed_at,provider,model,source_path,correction,created_at,estimated_cost)
                VALUES(?,?,?,?,'synthetic task','succeeded',?,?,'fake','fake','synthetic',0,?,?)""",
                ("attempt-" + user, "attempt-key-" + user, "session-" + user, user, now, now, now, cost))
            if delivered:
                reservation = self.commerce.reserve_generation(c, user_id=user, attempt_id="attempt-" + user, idempotency_key="consume-" + user)
                self.commerce.consume_generation(c, reservation)

    def test_ordinary_user_stays_two_edits_zero_originals(self):
        self.assertEqual(self.commerce.balance("owner").available, 2)
        self.assertEqual(self.commerce.entitlement_balance("owner").available, 0)

    def test_registration_is_two_total_and_one_original_once(self):
        relation, user = self.register()
        self.assertEqual(self.commerce.balance(user).available, 2)
        self.assertEqual(self.commerce.entitlement_balance(user).available, 1)
        self.assertEqual(self.referrals.register_start("friend", self.code, event_key="repeat"), relation)
        self.referrals.link_user("friend", user)
        self.assertEqual(self.commerce.balance(user).available, 2)
        self.assertEqual(self.commerce.entitlement_balance(user).available, 1)

    def test_concurrent_registration_grants_only_one_initial_original(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            relations = list(pool.map(lambda i: self.referrals.register_start("racing", self.code, event_key=f"race-{i}"), range(8)))
        self.assertEqual(len(set(relations)), 1)
        with self.db.read() as c:
            user = c.execute("SELECT id FROM users WHERE platform_user_id='racing'").fetchone()[0]
            self.assertEqual(c.execute("SELECT COUNT(*) FROM referral_bonus_grants").fetchone()[0], 1)
        self.assertEqual(self.commerce.balance(user).available, 2)
        self.assertEqual(self.commerce.entitlement_balance(user).available, 1)

    def test_self_existing_wrong_identity_invalid_and_first_binding(self):
        self.assertIsNone(self.referrals.register_start("inviter-max", self.code))
        self.assertIsNone(self.referrals.register_start("owner-max", self.code))
        self.assertIsNone(self.referrals.register_start("x", "unrecognized_code"))
        relation, user = self.register()
        other = self.referrals.get_or_create_code("owner")
        self.assertIsNone(self.referrals.register_start("friend", other, event_key="second-code"))
        self.referrals.link_user("friend", "owner")
        with self.db.read() as c:
            row = c.execute("SELECT * FROM referral_relationships WHERE id=?", (relation,)).fetchone()
        self.assertEqual(row["invitee_user_id"], user)
        self.assertEqual(row["inviter_user_id"], "inviter")

    def test_previous_start_without_user_cannot_be_claimed_as_new(self):
        with self.db.transaction() as c:
            c.execute("""INSERT INTO attribution_profiles(platform_user_id,first_source,first_started_at,last_source,last_started_at,created_at,updated_at)
                VALUES('old-no-photo','direct',?,'direct',?,?,?)""", (self.now.isoformat(),) * 4)
        self.assertIsNone(self.referrals.register_start("old-no-photo", self.code))

    def test_provider_success_without_confirmed_delivery_does_not_reward(self):
        _, user = self.register()
        self.success(user, delivered=False)
        self.assertIsNone(self.referrals.reward_first_success(user))
        self.assertEqual(self.commerce.balance("inviter").available, 2)

    def test_inviter_four_plus_two_atomic_concurrent_and_restart(self):
        _, user = self.register()
        self.success(user)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.referrals.reward_first_success(user), range(4)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.commerce.balance("inviter").available, 6)
        self.assertEqual(self.commerce.entitlement_balance("inviter").available, 2)
        ReferralService(self.db, self.commerce, v2_enabled=True, clock=lambda: self.now)
        self.assertEqual(self.commerce.balance("inviter").available, 6)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM referral_bonus_grants").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bonus_credit_transactions").fetchone()[0], 0)

    def test_failure_between_two_bonus_types_rolls_back_and_restart_recovers(self):
        _, user = self.register()
        self.success(user)
        with patch.object(self.commerce, "adjust_unlock_entitlements", side_effect=RuntimeError("synthetic crash")):
            with self.assertRaises(RuntimeError): self.referrals.reward_first_success(user)
        self.assertEqual(self.commerce.balance("inviter").available, 2)
        self.assertEqual(self.commerce.entitlement_balance("inviter").available, 0)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM referral_bonus_grants WHERE beneficiary='inviter'").fetchone()[0], 0)
        ReferralService(self.db, self.commerce, v2_enabled=True, clock=lambda: self.now)
        self.assertEqual(self.commerce.balance("inviter").available, 6)
        self.assertEqual(self.commerce.entitlement_balance("inviter").available, 2)

    def test_hundred_independent_successful_friends_no_business_cap(self):
        with self.assertLogs("app.referrals", level="WARNING"):
            for index in range(100):
                _, user = self.register(f"friend-{index}")
                self.success(user)
                self.assertIsNotNone(self.referrals.reward_first_success(user))
        self.assertEqual(self.commerce.balance("inviter").available, 402)
        self.assertEqual(self.commerce.entitlement_balance("inviter").available, 200)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM referral_relationships WHERE status='rewarded'").fetchone()[0], 100)

    def test_promo_not_limited_by_existing_paid_admin_balance_ceiling(self):
        with self.db.transaction() as c:
            self.commerce.adjust_generation_credits(c, user_id="inviter", delta=MAX_ACCOUNT_BALANCE - 2, reason="fixture", idempotency_key="large-fixture")
        _, user = self.register()
        self.success(user)
        self.referrals.reward_first_success(user)
        self.assertEqual(self.commerce.balance("inviter").available, MAX_ACCOUNT_BALANCE + 4)

    def test_kill_switch_preserves_issued_rights_and_can_recover_pending_v2(self):
        _, user = self.register()
        self.success(user)
        paused = ReferralService(self.db, self.commerce, v2_enabled=True, promo_grants_enabled=False)
        self.assertIsNone(paused.register_start("new-paused", self.code))
        self.assertIsNone(paused.reward_first_success(user))
        self.assertEqual(self.commerce.entitlement_balance(user).available, 1)
        ReferralService(self.db, self.commerce, v2_enabled=True, clock=lambda: self.now)
        self.assertEqual(self.commerce.balance("inviter").available, 6)

    def test_legacy_pending_stays_v1_after_enablement(self):
        legacy = ReferralService(self.db, self.commerce)
        relation = legacy.register_start("legacy", self.code)
        with self.db.transaction() as c:
            c.execute("INSERT INTO users(id,platform,platform_user_id,created_at) VALUES('legacy-user','max','legacy',?)", (self.now.isoformat(),))
            self.commerce.ensure_initial_grant(c, "legacy-user")
        legacy.link_user("legacy", "legacy-user")
        self.success("legacy-user")
        self.referrals.reward_first_success("legacy-user")
        self.assertEqual(self.commerce.balance("inviter").available, 4)
        self.assertEqual(self.commerce.entitlement_balance("inviter").available, 0)
        with self.db.read() as c:
            self.assertEqual(c.execute("SELECT quantity FROM bonus_credit_transactions WHERE relationship_id=?", (relation,)).fetchone()[0], 2)

    def test_readonly_report_owner_filter_bonus_provenance_retention_and_privacy(self):
        _, user = self.register()
        self.success(user)
        self.referrals.reward_first_success(user)
        self.now += timedelta(days=1)
        self.referrals.record_activity("friend", "return-day1")
        self.referrals.record_activity("friend", "return-day1")
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        report = build_report(self.path, days=20, owner_platform_ids=("owner-max",), at=self.now + timedelta(days=15))
        self.assertEqual(report["registrations_by_version"]["v2"], 1)
        self.assertEqual(report["v2_bonus"]["granted"], {"invitee_originals": 1, "inviter_edits": 4, "inviter_originals": 2})
        self.assertEqual(report["repeat_users"]["D+1"], {"eligible": 1, "returned": 1})
        self.assertEqual(report["repeat_users"]["D+7"], {"eligible": 1, "returned": 0})
        self.assertEqual(report["invitations_actually_sent"], "NOT MEASURABLE")
        self.assertEqual(report["first_delivered_success_users"], 1)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        for value in ("owner-max", "friend", user, str(self.path)):
            self.assertNotIn(value, json.dumps(report))
        with self.assertRaises(ReportUnavailable): build_report(self.path, owner_platform_ids=())

    def test_unaged_retention_denominators_exclude_recent_cohorts(self):
        self.register()
        report = build_report(self.path, days=7, owner_platform_ids=("owner-max",), at=self.now + timedelta(hours=2))
        self.assertEqual(report["repeat_users"]["D+1"]["eligible"], 0)
        self.assertEqual(report["repeat_users"]["D+7"]["eligible"], 0)

    def test_schema15_copy_migration_keeps_history_balances_and_legacy_check(self):
        legacy = ReferralService(self.db, self.commerce, clock=lambda: self.now)
        legacy.register_start("legacy", self.code)
        with self.db.transaction() as c:
            c.execute("INSERT INTO users(id,platform,platform_user_id,created_at) VALUES('legacy-user','max','legacy',?)", (self.now.isoformat(),))
            self.commerce.ensure_initial_grant(c, "legacy-user")
        legacy.link_user("legacy", "legacy-user")
        self.success("legacy-user")
        legacy.reward_first_success("legacy-user")
        with self.db.read() as c:
            balances = [tuple(r) for r in c.execute("SELECT * FROM user_credit_accounts ORDER BY user_id")]
            legacy_sql = c.execute("SELECT sql FROM sqlite_master WHERE name='bonus_credit_transactions'").fetchone()[0]
        with self.db.transaction() as c:
            for table in ("referral_activity", "referral_diagnostics", "referral_bonus_entitlements", "referral_bonus_grants", "referral_program_metadata"):
                c.execute(f"DROP TABLE {table}")
            for column in ("first_success_attempt_id", "first_success_at", "registered_at", "program_version"):
                c.execute(f"ALTER TABLE referral_relationships DROP COLUMN {column}")
            c.execute("DELETE FROM schema_migrations WHERE version=16")
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        copy = Path(self.temp.name) / "migration-copy.sqlite3"
        from scripts.check_migration_copy import verify_migration_copy
        proof = verify_migration_copy(self.path, copy)
        self.assertEqual((proof["status"], proof["source_schema"], proof["target_schema"]), ("PASS", 15, 16))
        migrated = Database(copy)
        for _ in range(2):
            migrated.initialize()
        with migrated.read() as c:
            self.assertEqual(schema_version(c), 16)
            self.assertEqual(c.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual([tuple(r) for r in c.execute("SELECT * FROM user_credit_accounts ORDER BY user_id")], balances)
            self.assertEqual(c.execute("SELECT sql FROM sqlite_master WHERE name='bonus_credit_transactions'").fetchone()[0], legacy_sql)
            self.assertEqual(c.execute("SELECT program_version,registered_at FROM referral_relationships").fetchone()[:], (1, None))
            self.assertEqual(c.execute("SELECT quantity FROM bonus_credit_transactions").fetchone()[0], 2)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_exact_ui_copy_and_unchanged_paid_products(self):
        menu = main_menu(0, 1, referral_v2_enabled=True)
        self.assertIn(REFERRAL_V2_BUTTON, [b.text for b in menu.buttons])
        self.assertIn("ему — 2 бесплатные обработки и 1 оригинал", menu.text)
        self.assertIn(REFERRAL_V2_BUTTON, [b.text for b in result_actions(1, referral_v2_enabled=True).buttons])
        self.assertIn(REFERRAL_V2_BUTTON, [b.text for b in gallery_item_actions(0, referral_v2_enabled=True)])
        self.assertEqual((SMALL_PACKAGE.price_minor, SMALL_PACKAGE.generation_credits, SMALL_PACKAGE.unlock_entitlements), (4900, 2, 1))
        self.assertEqual((LARGE_PACKAGE.price_minor, LARGE_PACKAGE.generation_credits, LARGE_PACKAGE.unlock_entitlements), (199000, 100, 50))

    def test_emergency_pause_does_not_advertise_rewards_for_either_version(self):
        for v2 in (False, True):
            view = result_actions(1, referral_v2_enabled=v2, referral_promos_enabled=False)
            self.assertIn("приостановлены", view.text)
            self.assertNotIn("ещё 2", view.text)
            self.assertNotIn("получите 4", view.text)
        from app.config import Settings
        default = Settings("", "fake", "test", Path(self.temp.name))
        self.assertFalse(default.referral_v2_enabled)
        self.assertTrue(default.referral_promo_grants_enabled)

    def test_unknown_cost_is_not_fabricated_and_legacy_retention_not_inferred(self):
        _, user = self.register()
        self.success(user, cost=None)
        legacy = ReferralService(self.db, self.commerce, clock=lambda: self.now)
        legacy.register_start("v1-no-activity", self.code)
        with self.db.transaction() as c:
            c.execute("INSERT INTO users(id,platform,platform_user_id,created_at) VALUES('v1-user','max','v1-no-activity',?)", (self.now.isoformat(),))
            self.commerce.ensure_initial_grant(c, "v1-user")
        legacy.link_user("v1-no-activity", "v1-user")
        report = build_report(self.path, days=20, owner_platform_ids=("owner-max",), at=self.now + timedelta(days=15))
        self.assertEqual(report["free_edit_cost"]["total_estimate_usd"], "NOT MEASURABLE")
        self.assertEqual(report["repeat_users"]["D+1"]["eligible"], 1)

    def test_confirmed_payments_are_deduped_and_owner_excluded(self):
        from app.config import Settings
        from app.payments import build_payment_service
        _, user = self.register()
        settings = Settings("", "fake", "test", Path(self.temp.name), payments_enabled=True, payment_provider="robokassa",
            robokassa_merchant_login="fixture", robokassa_password1="fixture-one", robokassa_password2="fixture-two")
        payment = build_payment_service(settings, self.db, clock=lambda: self.now)
        for account in (user, user, "owner"):
            order = payment.create_order(account, None, "order-" + str(self.now.timestamp()), account_purchase=True)
            fields = {"OutSum": "49.00", "InvId": str(order.provider_invoice_id), "Shp_order": order.public_token}
            fields["SignatureValue"] = hashlib.sha256(f"49.00:{order.provider_invoice_id}:fixture-two:Shp_order={order.public_token}".encode()).hexdigest()
            self.assertTrue(payment.process_webhook(fields, method="POST", path="/result").accepted)
            self.assertTrue(payment.process_webhook(fields, method="POST", path="/result").accepted)
            self.now += timedelta(seconds=1)
        report = build_report(self.path, days=7, owner_platform_ids=("owner-max",), at=self.now + timedelta(seconds=1))
        self.assertEqual(report["confirmed_gross_revenue_minor"], 9800)
        self.assertEqual(report["first_purchases"], 1)
        self.assertEqual(report["repeat_purchases"], 1)

    def test_promotional_usage_provenance_and_estimated_cost_alerts(self):
        _, user = self.register()
        self.success(user, cost=5.1)
        with self.assertLogs("app.referrals", level="WARNING"):
            self.referrals.observe_free_edit_cost("friend")
        report = build_report(self.path, owner_platform_ids=("owner-max",), at=self.now + timedelta(hours=1))
        self.assertEqual(report["free_edit_cost"]["known_estimate_usd"], 5.1)
        self.assertTrue(report["warnings"])
        self.referrals.reward_first_success(user)
        # Consume one exact promotional lot, not an unrelated initial/paid lot.
        with self.db.transaction() as c:
            c.execute("UPDATE generation_credit_lots SET available_credits=0,consumed_credits=granted_credits WHERE user_id='inviter' AND source_type='initial_free'")
            c.execute("UPDATE user_credit_accounts SET available_generation_credits=available_generation_credits-2,total_generation_credits_consumed=2 WHERE user_id='inviter'")
        self.success("inviter", cost=0.2)
        report = build_report(self.path, owner_platform_ids=("owner-max",), at=self.now + timedelta(hours=1))
        self.assertEqual(report["v2_bonus"]["consumed_edits"], 1)

    def test_v2_flag_off_cannot_reward_v2_relationship_but_preserves_original(self):
        _, user = self.register()
        self.success(user)
        off = ReferralService(self.db, self.commerce, v2_enabled=False)
        self.assertIsNone(off.reward_first_success(user))
        self.assertEqual(self.commerce.entitlement_balance(user).available, 1)


class ReferralV2UiTests(TestCase):
    def setUp(self):
        from tests.test_max_application import MaxApplicationTests
        self.h = MaxApplicationTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        h = self.h
        h.settings = replace(h.settings, referral_v2_enabled=True, max_public_access_enabled=True, max_single_screen_ui_enabled=True)
        h.app = MaxApplication(h.settings, h.database, h.demo, h.transport, h.store)
        h.app.handle(h.event("bot_started"))

    def friend(self):
        h = self.h
        code = h.app.referrals.get_or_create_code(h.store.get("u1").user_id)
        event = MaxIncomingEvent("bot_started", "friend-start", "u2", "c2", 1, start_payload="ref_" + code)
        h.app.handle(event)
        return h.store.get("u2").user_id

    def generate_friend(self):
        h = self.h
        h.app.handle(MaxIncomingEvent("message_created", "friend-photo", "u2", "c2", 2,
            message_id="friend-photo", image_url="https://iu.oneme.ru/synthetic", text="Улучшить фон"))

    def callback_friend(self, key):
        h = self.h
        current = h.app.ui.current("u2")
        from app.max_ui_shell import versioned_action
        h.app.handle(MaxIncomingEvent("message_callback", "unlock-" + key, "u2", "c2", 3,
            callback_id="callback-" + key, callback_payload=versioned_action("result:unlock", current.revision), message_id=current.message_id))

    def test_invitation_from_main_without_photo_and_back_no_provider_or_debit(self):
        h = self.h
        user = h.store.get("u1").user_id
        self.assertIn(REFERRAL_V2_BUTTON, [b.text for b in h.transport.messages[-1][2]])
        h.callback("referral:open")
        current = h.app.ui.current("u1")
        self.assertEqual(current.screen, "share")
        self.assertIsNone(h.store.get("u1").session_id)
        invitation = [b for b in h.transport.edits[-1][2] if b.kind == "clipboard"][0].action
        self.assertIn("2 бесплатные обработки и 1 оригинал", invitation)
        self.assertIn("4 обработки и 2 оригинала", invitation)
        h.callback("nav:back:main")
        self.assertEqual(h.app.ui.current("u1").screen, "main_menu")
        self.assertEqual(h.provider.calls, 0)
        self.assertEqual(h.demo.commerce.balance(user).available, 2)

    def test_registered_original_is_free_delivery_failure_releases_retry_idempotent(self):
        h = self.h
        friend = self.friend()
        self.assertEqual(h.demo.commerce.balance(friend).available, 2)
        self.assertEqual(h.demo.commerce.entitlement_balance(friend).available, 1)
        self.generate_friend()
        inviter = h.store.get("u1").user_id
        self.assertEqual(h.demo.commerce.balance(inviter).available, 6)
        self.assertEqual(h.demo.commerce.entitlement_balance(inviter).available, 2)
        h.transport.file_delivery = False
        self.callback_friend("fail")
        self.assertEqual(h.demo.commerce.entitlement_balance(friend).available, 1)
        h.transport.file_delivery = True
        self.callback_friend("retry")
        self.assertEqual(h.demo.commerce.entitlement_balance(friend).available, 0)
        self.callback_friend("redelivery")
        self.assertEqual(h.demo.commerce.entitlement_balance(friend).available, 0)
        self.assertEqual(h.provider.calls, 1)
        with h.database.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM payment_orders").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM referral_bonus_grants").fetchone()[0], 3)

    def test_failed_preview_never_rewards_inviter(self):
        h = self.h
        friend = self.friend()
        h.transport.image_delivery = False
        self.generate_friend()
        self.assertEqual(h.demo.commerce.balance(h.store.get("u1").user_id).available, 2)
        self.assertEqual(h.demo.commerce.entitlement_balance(friend).available, 1)

    def test_post_delivery_promotion_failure_does_not_regenerate_and_restart_recovers(self):
        h = self.h
        self.friend()
        with patch.object(h.app.referrals, "reward_first_success", side_effect=RuntimeError("fixture promotion crash")):
            self.generate_friend()
        self.assertEqual(h.provider.calls, 1)
        self.assertEqual(h.store.get("u2").state, "result_ready")
        h.app = MaxApplication(h.settings, h.database, h.demo, h.transport, h.store)
        self.assertEqual(h.provider.calls, 1)
        self.assertEqual(h.demo.commerce.balance(h.store.get("u1").user_id).available, 6)
        self.assertEqual(h.demo.commerce.entitlement_balance(h.store.get("u1").user_id).available, 2)
