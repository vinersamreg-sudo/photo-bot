import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from app.commerce import (
    GENERATION_CREDITS_PER_PACK,
    INITIAL_GENERATION_CREDITS,
    MAX_ACCOUNT_BALANCE,
    PRICE_MINOR,
    PRODUCT_CODE,
    UNLOCK_ENTITLEMENTS_PER_PACK,
    CommerceService,
    migrate_legacy_credit_accounts,
)
from app.database import Database
from app.domain import DemoLimitError, InvalidInputError, PaymentRequiredError


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int = 1) -> None:
        self.value += timedelta(seconds=seconds)


class CommerceLedgerTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "pixora.sqlite3")
        self.clock = Clock()
        self.service = CommerceService(self.database, self.clock)
        self.user_id = self._insert_user("owner")

    def _insert_user(self, platform_id: str) -> str:
        user_id = f"user-{platform_id}"
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?, 'max', ?, ?)",
                (user_id, platform_id, self.clock().isoformat()),
            )
            connection.execute(
                """INSERT INTO demo_sessions(
                       id,user_id,source_file_path,source_sha256,status,started_at,expires_at,
                       successful_generations,max_generations,created_at,updated_at
                   ) VALUES(?,?,?,'source-hash','active',?,?,0,2,?,?)""",
                (
                    f"session-{platform_id}",
                    user_id,
                    str(self.root / f"{platform_id}.png"),
                    self.clock().isoformat(),
                    (self.clock() + timedelta(hours=1)).isoformat(),
                    self.clock().isoformat(),
                    self.clock().isoformat(),
                ),
            )
        return user_id

    def _attempt(self, user_id: str, suffix: str, status: str = "processing") -> str:
        platform_id = user_id.removeprefix("user-")
        attempt_id = f"attempt-{platform_id}-{suffix}"
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO generation_attempts(
                       id,idempotency_key,session_id,user_id,prompt,status,started_at,
                       provider,model,source_path,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    f"idem-{platform_id}-{suffix}",
                    f"session-{platform_id}",
                    user_id,
                    "safe test prompt",
                    status,
                    self.clock().isoformat(),
                    "fake",
                    "fake",
                    str(self.root / f"{platform_id}.png"),
                    self.clock().isoformat(),
                ),
            )
        return attempt_id

    def _reserve(self, user_id: str, suffix: str) -> str:
        attempt_id = self._attempt(user_id, suffix)
        with self.database.transaction() as connection:
            return self.service.reserve_generation(
                connection,
                user_id=user_id,
                attempt_id=attempt_id,
                idempotency_key=f"reserve-{user_id}-{suffix}",
            )

    def _gallery_version(self, user_id: str, suffix: str, *, original: bool = True) -> str:
        platform_id = user_id.removeprefix("user-")
        attempt_id = self._attempt(user_id, f"version-{suffix}", status="succeeded")
        gallery_id = f"gallery-{platform_id}"
        item_id = f"item-{platform_id}-{suffix}"
        version_id = f"version-{platform_id}-{suffix}"
        original_path = self.root / f"original-{platform_id}-{suffix}.png"
        if original:
            original_path.write_bytes(b"safe-image-placeholder")
        now = self.clock().isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO galleries(id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
                (gallery_id, user_id, now, now),
            )
            connection.execute(
                """INSERT INTO gallery_items(
                       id,gallery_id,user_id,title,created_at,updated_at,original_source_path,
                       storage_root_path,retention_until
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    item_id,
                    gallery_id,
                    user_id,
                    "Test work",
                    now,
                    now,
                    str(self.root / "source.png"),
                    str(self.root),
                    (self.clock() + timedelta(days=30)).isoformat(),
                ),
            )
            connection.execute(
                """INSERT INTO gallery_versions(
                       id,gallery_item_id,attempt_id,version_number,source_path,prompt,
                       effective_prompt,provider,model,original_path,created_at,status
                   ) VALUES(?,?,?,1,?,'safe','safe','fake','fake',?,?,'succeeded')""",
                (
                    version_id,
                    item_id,
                    attempt_id,
                    str(self.root / "source.png"),
                    str(original_path) if original else None,
                    now,
                ),
            )
        return version_id

    def _grant_pack(self, user_id: str, order_id: str) -> None:
        with self.database.transaction() as connection:
            self.service.grant_continuation_pack(
                connection,
                user_id=user_id,
                payment_order_id=order_id,
                payment_intent_id=f"intent-{order_id}",
            )

    def test_product_constants_are_the_permanent_two_plus_one_model(self) -> None:
        self.assertEqual(PRODUCT_CODE, "continuation_pack_2_plus_1")
        self.assertEqual(PRICE_MINOR, 4_900)
        self.assertEqual(INITIAL_GENERATION_CREDITS, 2)
        self.assertEqual(GENERATION_CREDITS_PER_PACK, 2)
        self.assertEqual(UNLOCK_ENTITLEMENTS_PER_PACK, 1)

    def test_initial_grant_is_exactly_two_and_lifetime_idempotent(self) -> None:
        first = self.service.balance(self.user_id)
        second = CommerceService(self.database, self.clock).balance(self.user_id)
        self.assertEqual(first.available, 2)
        self.assertEqual(second.available, 2)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM generation_credit_lots WHERE user_id=?",
                    (self.user_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM credit_ledger WHERE event_type='initial_free_grant'"
                ).fetchone()[0],
                1,
            )

    def test_reservation_consumption_and_idempotency(self) -> None:
        reservation = self._reserve(self.user_id, "one")
        self.assertEqual(self.service.balance(self.user_id).available, 1)
        self.assertEqual(self.service.balance(self.user_id).reserved, 1)
        with self.database.transaction() as connection:
            consumed = self.service.consume_generation(connection, reservation)
            replay = self.service.consume_generation(connection, reservation)
        self.assertEqual(consumed.available, 1)
        self.assertEqual(replay.total_consumed, 1)
        self.assertEqual(replay.reserved, 0)

    def test_release_matrix_never_consumes_a_credit(self) -> None:
        failure_reasons = (
            "provider_error",
            "network_error",
            "timeout",
            "policy_rejection",
            "storage_failure",
            "internal_error",
            "invalid_input",
            "max_delivery_failure",
            "cancelled_processing",
            "result_not_delivered",
        )
        for index, reason in enumerate(failure_reasons):
            with self.subTest(reason=reason):
                reservation = self._reserve(self.user_id, f"failure-{index}")
                with self.database.transaction() as connection:
                    released = self.service.release_generation(
                        connection, reservation, reason
                    )
                    replay = self.service.release_generation(
                        connection, reservation, reason
                    )
                self.assertEqual(released.available, 2)
                self.assertEqual(replay.available, 2)
                self.assertEqual(replay.total_consumed, 0)

    def test_third_generation_is_blocked_and_balance_never_negative(self) -> None:
        for suffix in ("first", "second"):
            reservation = self._reserve(self.user_id, suffix)
            with self.database.transaction() as connection:
                self.service.consume_generation(connection, reservation)
        attempt = self._attempt(self.user_id, "third")
        with self.database.transaction() as connection:
            with self.assertRaises(DemoLimitError):
                self.service.reserve_generation(
                    connection,
                    user_id=self.user_id,
                    attempt_id=attempt,
                    idempotency_key="third-blocked",
                )
        balance = self.service.balance(self.user_id)
        self.assertEqual((balance.available, balance.reserved), (0, 0))

    def test_parallel_reservations_cannot_exceed_balance(self) -> None:
        first = self._reserve(self.user_id, "parallel-1")
        second = self._reserve(self.user_id, "parallel-2")
        third_attempt = self._attempt(self.user_id, "parallel-3")
        with self.database.transaction() as connection:
            with self.assertRaises(DemoLimitError):
                self.service.reserve_generation(
                    connection,
                    user_id=self.user_id,
                    attempt_id=third_attempt,
                    idempotency_key="parallel-third",
                )
            self.service.release_generation(connection, first, "cancelled")
            self.service.consume_generation(connection, second)
        self.assertEqual(self.service.balance(self.user_id).available, 1)

    def test_stale_restart_recovery_releases_only_non_processing_reservations(self) -> None:
        stale = self._reserve(self.user_id, "stale")
        live = self._reserve(self.user_id, "live")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE generation_attempts SET status='failed_technical' WHERE id=?",
                ("attempt-owner-stale",),
            )
            released = CommerceService.recover_stale_reservations(
                connection, self.clock().isoformat()
            )
        self.assertEqual(released, 1)
        with self.database.read() as connection:
            states = {
                row["id"]: row["status"]
                for row in connection.execute(
                    "SELECT id,status FROM generation_credit_reservations"
                )
            }
        self.assertEqual(states[stale], "released")
        self.assertEqual(states[live], "reserved")

    def test_each_paid_pack_grants_exactly_two_credits_and_one_entitlement(self) -> None:
        self.service.balance(self.user_id)
        self._grant_pack(self.user_id, "order-one")
        self.assertEqual(self.service.balance(self.user_id).available, 4)
        self.assertEqual(self.service.entitlement_balance(self.user_id).available, 1)
        self._grant_pack(self.user_id, "order-one")
        self.assertEqual(self.service.balance(self.user_id).available, 4)
        self._grant_pack(self.user_id, "order-two")
        self._grant_pack(self.user_id, "order-three")
        self.assertEqual(self.service.balance(self.user_id).available, 8)
        self.assertEqual(self.service.entitlement_balance(self.user_id).available, 3)

    def test_pack_does_not_auto_unlock_and_can_unlock_old_or_new_owned_version(self) -> None:
        old_version = self._gallery_version(self.user_id, "old")
        self._grant_pack(self.user_id, "order-old")
        new_version = self._gallery_version(self.user_id, "new")
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM gallery_versions WHERE unlock_status='unlocked'"
                ).fetchone()[0],
                0,
            )
        result = self.service.unlock_version(self.user_id, new_version)
        self.assertTrue(result.consumed_now)
        self.assertEqual(self.service.entitlement_balance(self.user_id).available, 0)
        self._grant_pack(self.user_id, "order-second-original")
        self.assertTrue(self.service.unlock_version(self.user_id, old_version).consumed_now)

    def test_original_redelivery_is_free_and_missing_file_does_not_consume(self) -> None:
        version = self._gallery_version(self.user_id, "retry")
        self._grant_pack(self.user_id, "order-retry")
        first = self.service.unlock_version(self.user_id, version)
        second = self.service.unlock_version(self.user_id, version)
        self.assertTrue(first.consumed_now)
        self.assertFalse(second.consumed_now)
        self.assertEqual(first.entitlement_id, second.entitlement_id)

        missing = self._gallery_version(self.user_id, "missing", original=False)
        self._grant_pack(self.user_id, "order-missing")
        before = self.service.entitlement_balance(self.user_id).available
        with self.assertRaises(InvalidInputError):
            self.service.unlock_version(self.user_id, missing)
        self.assertEqual(self.service.entitlement_balance(self.user_id).available, before)

    def test_original_delivery_reservation_is_released_on_failure_and_restart(self) -> None:
        version = self._gallery_version(self.user_id, "delivery-reservation")
        self._grant_pack(self.user_id, "order-delivery-reservation")
        reservation = self.service.reserve_unlock_delivery(self.user_id, version)
        self.assertFalse(reservation.already_unlocked)
        self.assertEqual(self.service.entitlement_balance(self.user_id).available, 0)
        self.assertTrue(
            self.service.release_unlock_delivery(
                self.user_id, version, reservation.entitlement_id
            )
        )
        self.assertEqual(self.service.entitlement_balance(self.user_id).available, 1)

        reservation = self.service.reserve_unlock_delivery(self.user_id, version)
        with self.database.transaction() as connection:
            recovered = CommerceService.recover_stale_unlock_deliveries(
                connection, self.clock().isoformat()
            )
        self.assertEqual(recovered, 1)
        self.assertEqual(self.service.entitlement_balance(self.user_id).available, 1)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT status,gallery_version_id FROM unlock_entitlements WHERE id=?",
                (reservation.entitlement_id,),
            ).fetchone()
        self.assertEqual(row["status"], "available")
        self.assertIsNone(row["gallery_version_id"])

    def test_entitlement_rejects_foreign_deleted_and_second_version(self) -> None:
        owned = self._gallery_version(self.user_id, "owned")
        foreign_user = self._insert_user("foreign")
        foreign = self._gallery_version(foreign_user, "foreign")
        self._grant_pack(self.user_id, "order-security")
        with self.assertRaises(InvalidInputError):
            self.service.unlock_version(self.user_id, foreign)
        self.assertTrue(self.service.unlock_version(self.user_id, owned).consumed_now)
        another = self._gallery_version(self.user_id, "another")
        with self.assertRaises(PaymentRequiredError):
            self.service.unlock_version(self.user_id, another)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE gallery_items SET deleted=1 WHERE id=(SELECT gallery_item_id FROM gallery_versions WHERE id=?)",
                (another,),
            )
        with self.assertRaises(InvalidInputError):
            self.service.unlock_version(self.user_id, another)

    def test_unused_pack_refund_rolls_back_only_that_pack(self) -> None:
        self.service.balance(self.user_id)
        self._grant_pack(self.user_id, "order-a")
        self._grant_pack(self.user_id, "order-b")
        self.assertTrue(self.service.refund_eligibility("order-b").eligible)
        with self.database.transaction() as connection:
            changed = self.service.rollback_unused_pack(connection, "order-b")
            replay = self.service.rollback_unused_pack(connection, "order-b")
        self.assertTrue(changed)
        self.assertFalse(replay)
        self.assertEqual(self.service.balance(self.user_id).available, 4)
        self.assertEqual(self.service.entitlement_balance(self.user_id).available, 1)

    def test_used_credit_or_entitlement_requires_manual_refund_review(self) -> None:
        self.service.balance(self.user_id)
        self._grant_pack(self.user_id, "order-credit-used")
        first = self._reserve(self.user_id, "consume-initial-first")
        second = self._reserve(self.user_id, "consume-initial-second")
        paid = self._reserve(self.user_id, "consume-paid")
        with self.database.transaction() as connection:
            self.service.consume_generation(connection, first)
            self.service.consume_generation(connection, second)
            self.service.consume_generation(connection, paid)
        self.assertFalse(self.service.refund_eligibility("order-credit-used").eligible)

        entitlement_user = self._insert_user("entitlement-refund")
        self._grant_pack(entitlement_user, "order-entitlement-used")
        version = self._gallery_version(entitlement_user, "refund-used")
        self.service.unlock_version(entitlement_user, version)
        self.assertFalse(
            self.service.refund_eligibility("order-entitlement-used").eligible
        )

    def test_refund_hold_prevents_spend_and_can_be_released_or_rolled_back(self) -> None:
        self.service.balance(self.user_id)
        first = self._reserve(self.user_id, "free-1")
        second = self._reserve(self.user_id, "free-2")
        with self.database.transaction() as connection:
            self.service.consume_generation(connection, first)
            self.service.consume_generation(connection, second)
        self._grant_pack(self.user_id, "order-held")
        with self.database.transaction() as connection:
            self.service.hold_pack_for_refund(connection, "order-held")
        attempt = self._attempt(self.user_id, "held-spend")
        with self.database.transaction() as connection:
            with self.assertRaises(DemoLimitError):
                self.service.reserve_generation(
                    connection,
                    user_id=self.user_id,
                    attempt_id=attempt,
                    idempotency_key="held-spend",
                )
            self.service.release_refund_hold(connection, "order-held")
        available = self._reserve(self.user_id, "after-release")
        with self.database.transaction() as connection:
            self.service.release_generation(connection, available, "test")
            self.service.hold_pack_for_refund(connection, "order-held")
            self.service.rollback_unused_pack(connection, "order-held")
        self.assertEqual(self.service.balance(self.user_id).available, 0)

    def test_admin_adjustments_are_idempotent_audited_and_never_negative(self) -> None:
        self.service.balance(self.user_id)
        with self.database.transaction() as connection:
            raised = self.service.adjust_generation_credits(
                connection,
                user_id=self.user_id,
                delta=3,
                reason="owner support correction",
                idempotency_key="admin-credit-1",
            )
            replay = self.service.adjust_generation_credits(
                connection,
                user_id=self.user_id,
                delta=3,
                reason="owner support correction",
                idempotency_key="admin-credit-1",
            )
        self.assertEqual(raised.available, 5)
        self.assertEqual(replay.available, 5)
        with self.database.transaction() as connection:
            with self.assertRaises(InvalidInputError):
                self.service.adjust_generation_credits(
                    connection,
                    user_id=self.user_id,
                    delta=-6,
                    reason="invalid negative",
                    idempotency_key="admin-credit-negative",
                )
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM commerce_admin_audit").fetchone()[0],
                1,
            )

    def test_balance_overflow_is_rejected(self) -> None:
        self.service.balance(self.user_id)
        with self.database.transaction() as connection:
            with self.assertRaises(InvalidInputError):
                self.service.adjust_generation_credits(
                    connection,
                    user_id=self.user_id,
                    delta=MAX_ACCOUNT_BALANCE,
                    reason="overflow guard",
                    idempotency_key="overflow",
                )

    def test_legacy_migration_explicitly_maps_zero_one_and_many_successes(self) -> None:
        users = []
        for successes in (0, 1, 7):
            user_id = self._insert_user(f"legacy-{successes}")
            users.append(user_id)
            for index in range(successes):
                self._attempt(user_id, f"legacy-{index}", status="succeeded")
        with self.database.transaction() as connection:
            report = migrate_legacy_credit_accounts(
                connection, self.clock().isoformat()
            )
        self.assertGreaterEqual(report["created"], 3)
        self.assertEqual(
            [self.service.balance(user_id).available for user_id in users],
            [2, 1, 0],
        )

    def test_commerce_telemetry_is_hashed_and_contains_no_raw_prompt_or_image(self) -> None:
        self.service.balance(self.user_id)
        self._grant_pack(self.user_id, "order-private")
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT event_type,subject_hash FROM product_events WHERE subject_hash IS NOT NULL"
            ).fetchall()
        rendered = repr([tuple(row) for row in rows])
        self.assertNotIn(self.user_id, rendered)
        self.assertNotIn("safe test prompt", rendered)
        self.assertNotIn(str(self.root), rendered)
