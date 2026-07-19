import hashlib
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from urllib.parse import parse_qs, urlparse

import httpx
from PIL import Image

from app.config import Settings
from app.commercial_operations import cost_status, payment_status
from app.database import Database
from app.demo_service import DemoService
from app.image_provider import FakeImageProvider
from app.payments import (
    PaymentError,
    PaymentStatus,
    PaymentUnavailable,
    build_payment_service,
)
from app.payment_webhook import PaymentWebhookServer
from app.robokassa import RobokassaProvider, RobokassaRefundResult
from app.storage import PrivateStorage
from app.watermark import WatermarkService


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **values) -> None:
        self.value += timedelta(**values)


class PaymentTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        for name in ("data", "logs", "temp"):
            (self.base / name).mkdir()
        self.settings = Settings(
            "", "fake", "test", self.base,
            demo_min_request_interval_seconds=1,
            payments_enabled=True,
            payment_provider="robokassa",
            payment_webhook_enabled=True,
            payment_result_url="https://example.test/payments/robokassa/result",
            robokassa_merchant_login="pixora-test",
            robokassa_password1="password-one",
            robokassa_password2="password-two",
            robokassa_password3="password-three",
        )
        self.clock = Clock()
        self.database = Database(self.settings.database_path)
        source = self.base / "source.png"
        Image.new("RGB", (320, 240), "#aabbcc").save(source)
        self.demo = DemoService(
            self.settings,
            self.database,
            PrivateStorage(self.settings.users_dir, 15 * 1024 * 1024),
            WatermarkService("ОБРАЗЕЦ", 1024, "JPEG", 82),
            FakeImageProvider(),
            clock=self.clock,
        )
        session = self.demo.start_session("max", "owner", source)
        first = self.demo.generate(session.session_id, "Замени фон", "first")
        self.clock.advance(seconds=2)
        second = self.demo.generate(
            session.session_id, "Поменяй куртку", "second", correction=True
        )
        with self.database.read() as connection:
            self.versions = connection.execute(
                "SELECT id,attempt_id FROM gallery_versions ORDER BY version_number"
            ).fetchall()
        self.user_id = session.user_id
        self.service = build_payment_service(
            self.settings, self.database, clock=self.clock
        )

    def signed_callback(self, order, *, amount="149.00", token=None):
        token = order.public_token if token is None else token
        base = (
            f"{amount}:{order.provider_invoice_id}:password-two:Shp_order={token}"
        )
        signature = hashlib.sha256(base.encode("utf-8")).hexdigest().upper()
        return {
            "OutSum": amount,
            "InvId": str(order.provider_invoice_id),
            "SignatureValue": signature,
            "Shp_order": token,
            "PaymentMethod": "BankCard",
        }

    def test_payment_link_uses_test_mode_receipt_and_bound_order_token(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        query = parse_qs(urlparse(order.payment_url).query)
        self.assertEqual(query["MerchantLogin"], ["pixora-test"])
        self.assertEqual(query["OutSum"], ["149.00"])
        self.assertEqual(query["InvId"], [str(order.provider_invoice_id)])
        self.assertEqual(query["IsTest"], ["1"])
        self.assertEqual(query["Shp_order"], [order.public_token])
        self.assertIn("Receipt", query)
        self.assertNotIn("password-one", order.payment_url)
        again = self.service.create_order(self.user_id, self.versions[0]["id"], "event-2")
        self.assertEqual(again.id, order.id)

    def test_paid_webhook_unlocks_only_exact_version_and_is_idempotent(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        result = self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result", source="127.0.0.1",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.response_text, f"OK{order.provider_invoice_id}")
        with self.database.read() as connection:
            versions = connection.execute(
                "SELECT id,unlock_status,payment_order_id FROM gallery_versions ORDER BY version_number"
            ).fetchall()
            item_status = connection.execute(
                "SELECT unlock_status,retention_until FROM gallery_items"
            ).fetchone()
            events = connection.execute(
                "SELECT COUNT(*) FROM product_events WHERE event_type='payment_confirmed'"
            ).fetchone()[0]
        self.assertEqual(versions[0]["unlock_status"], "unlocked")
        self.assertEqual(versions[0]["payment_order_id"], order.id)
        self.assertEqual(versions[1]["unlock_status"], "demo")
        self.assertEqual(item_status["unlock_status"], "demo")
        self.assertGreater(
            datetime.fromisoformat(item_status["retention_until"]),
            self.clock.value + timedelta(days=179),
        )
        self.assertEqual(events, 1)
        duplicate = self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result", source="127.0.0.1",
        )
        self.assertTrue(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(
            self.service.history(order.id).order.status, PaymentStatus.PAID
        )

    def test_forged_wrong_amount_token_unknown_and_expired_callbacks_fail_closed(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        wrong_amount = self.service.process_webhook(
            self.signed_callback(order, amount="148.00"), method="POST",
            path="/payments/robokassa/result",
        )
        self.assertFalse(wrong_amount.accepted)
        self.assertEqual(wrong_amount.reason, "amount_mismatch")
        wrong_token = self.service.process_webhook(
            self.signed_callback(order, token="forged"), method="POST",
            path="/payments/robokassa/result",
        )
        self.assertFalse(wrong_token.accepted)
        self.assertEqual(wrong_token.reason, "order_token_mismatch")
        self.clock.advance(minutes=31)
        expired = self.service.process_webhook(
            self.signed_callback(order, amount="149.000000"), method="POST",
            path="/payments/robokassa/result",
        )
        self.assertFalse(expired.accepted)
        self.assertEqual(expired.reason, "expired_invoice")
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT unlock_status FROM gallery_versions WHERE id=?",
                    (self.versions[0]["id"],),
                ).fetchone()[0],
                "demo",
            )

    def test_invalid_signature_unknown_invoice_and_replay_are_rejected(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        invalid = self.signed_callback(order)
        invalid["SignatureValue"] = "0" * 64
        rejected = self.service.process_webhook(
            invalid, method="POST", path="/payments/robokassa/result",
            source="203.0.113.10",
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "invalid_signature")
        replay = self.service.process_webhook(
            invalid, method="POST", path="/payments/robokassa/result",
            source="203.0.113.10",
        )
        self.assertFalse(replay.accepted)
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.http_status, 409)

        original_provider = self.service.provider
        mismatched_provider = RobokassaProvider(
            merchant_login="another-shop", password1="password-one",
            password2="password-two", mode="sandbox",
        )
        self.addCleanup(mismatched_provider.close)
        self.service.provider = mismatched_provider
        merchant_mismatch = self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result",
        )
        self.assertFalse(merchant_mismatch.accepted)
        self.assertEqual(merchant_mismatch.reason, "merchant_mismatch")
        self.service.provider = original_provider

        invoice = order.provider_invoice_id + 1000
        amount = "149.00"
        token = "unknown-order"
        base = f"{amount}:{invoice}:password-two:Shp_order={token}"
        unknown = self.service.process_webhook(
            {
                "OutSum": amount,
                "InvId": str(invoice),
                "SignatureValue": hashlib.sha256(base.encode()).hexdigest(),
                "Shp_order": token,
            },
            method="POST",
            path="/payments/robokassa/result",
        )
        self.assertFalse(unknown.accepted)
        self.assertEqual(unknown.reason, "unknown_invoice")
        with self.database.read() as connection:
            safe_payloads = [row[0] for row in connection.execute(
                "SELECT payload_safe_json FROM payment_events"
            )]
            source_hash = connection.execute(
                "SELECT source_hash FROM payment_webhooks WHERE response_code='invalid_signature'"
            ).fetchone()[0]
        self.assertTrue(all("SignatureValue" not in payload for payload in safe_payloads))
        self.assertNotEqual(source_hash, "203.0.113.10")

    def test_migration_v8_and_commercial_metrics_are_privacy_safe(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result",
        )
        metrics = cost_status(self.settings, self.database)
        status = payment_status(self.settings, self.database)
        self.assertEqual(metrics["paid_orders"], 1)
        self.assertEqual(metrics["recognized_revenue_rub"], 149.0)
        self.assertNotIn(self.user_id, str(metrics))
        self.assertEqual(status["orders"]["paid"]["count"], 1)
        with self.database.read() as connection:
            migration = connection.execute(
                "SELECT name FROM schema_migrations WHERE version=8"
            ).fetchone()
        self.assertEqual(migration[0], "version_scoped_commercial_payments")

    def test_delivery_failure_does_not_undo_payment_and_redelivery_works(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result",
        )
        original = self.service.original_for_order(order.id, self.user_id)
        self.assertTrue(original.is_file())
        self.service.mark_delivery(order.id, delivered=False, error_code="max_failed")
        self.assertEqual(
            self.service.history(order.id).order.status, PaymentStatus.DELIVERY_PENDING
        )
        self.assertEqual(self.service.original_for_order(order.id, self.user_id), original)
        self.service.mark_delivery(order.id, delivered=True)
        self.service.mark_delivery(order.id, delivered=True)
        with self.database.read() as connection:
            count = connection.execute(
                "SELECT delivery_count FROM gallery_versions WHERE id=?",
                (self.versions[0]["id"],),
            ).fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual(
            self.service.history(order.id).order.status, PaymentStatus.DELIVERED
        )

    def test_refund_is_prepared_but_not_sent_while_execution_flag_is_off(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result",
        )
        refund = self.service.prepare_refund(order.id, 14900, "customer_request", "refund-1")
        self.assertEqual(refund.status.value, "draft")
        history = self.service.refund_history(refund.id)
        self.assertEqual(history.refund.reason, "customer_request")
        self.assertEqual(history.audit[0].event_type, "refund_prepared")
        with self.assertRaisesRegex(PaymentError, "idempotency key conflict"):
            self.service.prepare_refund(
                order.id, 100, "customer_request", "refund-1"
            )
        with self.assertRaises(PaymentUnavailable):
            self.service.submit_refund(refund.id)
        with self.assertRaisesRegex(PaymentError, "not supported"):
            self.service.prepare_refund(order.id, 100, "private free text", "refund-2")

    def test_refund_execution_and_status_update_are_idempotently_audited(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        callback = self.signed_callback(order)
        callback["OpKey"] = "operation-key"
        self.service.process_webhook(
            callback, method="POST", path="/payments/robokassa/result"
        )

        class FakeRefundProvider:
            name = "robokassa"
            merchant_login = "pixora-test"

            def create_refund(self, request):
                self.request = request
                return RobokassaRefundResult(True, "refund-request", None, 200)

            def refund_status(self, request_id):
                self.request_id = request_id
                return "finished", 200

        refund_provider = FakeRefundProvider()
        self.service.provider = refund_provider
        object.__setattr__(self.settings, "payment_refunds_enabled", True)
        refund = self.service.prepare_refund(order.id, 14900, "customer_request", "refund-1")
        submitted = self.service.submit_refund(refund.id)
        self.assertEqual(submitted.status.value, "pending")
        finished = self.service.refresh_refund(refund.id)
        self.assertEqual(finished.status.value, "succeeded")
        with self.database.read() as connection:
            paid = connection.execute(
                "SELECT status,refunded_amount_minor FROM payment_orders WHERE id=?",
                (order.id,),
            ).fetchone()
            receipt_types = [row[0] for row in connection.execute(
                "SELECT receipt_type FROM payment_receipts WHERE order_id=? ORDER BY created_at",
                (order.id,),
            )]
            version_unlock = connection.execute(
                "SELECT unlock_status FROM gallery_versions WHERE id=?",
                (self.versions[0]["id"],),
            ).fetchone()[0]
        self.assertEqual(paid["status"], "refunded")
        self.assertEqual(paid["refunded_amount_minor"], 14900)
        self.assertEqual(receipt_types, ["payment", "refund"])
        self.assertEqual(version_unlock, "refunded")
        with self.assertRaisesRegex(PaymentError, "not paid"):
            self.service.original_for_order(order.id, self.user_id)

    def test_http_webhook_accepts_valid_post_and_invokes_delivery_once(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[1]["id"], "event-http")
        delivered = []
        server = PaymentWebhookServer(
            self.service, "127.0.0.1", 0, "/payments/robokassa/result",
            on_paid=delivered.append,
        )
        server.start()
        self.addCleanup(server.stop)
        response = httpx.post(
            f"http://127.0.0.1:{server.bound_port}/payments/robokassa/result",
            data=self.signed_callback(order),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, f"OK{order.provider_invoice_id}")
        self.assertEqual(delivered, [order.id])
        duplicate = httpx.post(
            f"http://127.0.0.1:{server.bound_port}/payments/robokassa/result",
            data=self.signed_callback(order),
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(delivered, [order.id])
        missing = httpx.get(f"http://127.0.0.1:{server.bound_port}/wrong")
        self.assertEqual(missing.status_code, 404)


class RobokassaSignatureTests(TestCase):
    def test_result_signature_accepts_six_decimal_production_amount(self) -> None:
        provider = RobokassaProvider(
            merchant_login="shop", password1="one", password2="two",
            hash_algorithm="sha256", mode="production",
        )
        self.addCleanup(provider.close)
        raw = "149.000000:42:two:Shp_order=opaque"
        values = {
            "OutSum": "149.000000", "InvId": "42", "Shp_order": "opaque",
            "SignatureValue": hashlib.sha256(raw.encode()).hexdigest(),
        }
        notification = provider.parse_notification(values)
        self.assertTrue(notification.signature_valid)
        self.assertEqual(notification.amount_minor, 14900)
