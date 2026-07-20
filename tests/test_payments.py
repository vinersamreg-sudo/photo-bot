import hashlib
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
from PIL import Image

from app.config import Settings
from app.commercial_operations import cost_status, payment_status
from app.payment_admin import payment_reconcile, payment_show
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

    def signed_callback(self, order, *, amount="49.00", token=None):
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
        self.assertEqual(query["OutSum"], ["49.00"])
        self.assertEqual(query["InvId"], [str(order.provider_invoice_id)])
        self.assertEqual(query["IsTest"], ["1"])
        self.assertEqual(query["Shp_order"], [order.public_token])
        self.assertIn("Receipt", query)
        self.assertNotIn("password-one", order.payment_url)
        again = self.service.create_order(self.user_id, self.versions[0]["id"], "event-2")
        self.assertEqual(again.id, order.id)

    def test_paid_webhook_grants_pack_without_auto_unlock_and_is_idempotent(self) -> None:
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
                "SELECT COUNT(*) FROM product_events WHERE event_type='continuation_pack_paid'"
            ).fetchone()[0]
        self.assertEqual(versions[0]["unlock_status"], "demo")
        self.assertIsNone(versions[0]["payment_order_id"])
        self.assertEqual(versions[1]["unlock_status"], "demo")
        self.assertEqual(item_status["unlock_status"], "demo")
        self.assertEqual(events, 1)
        self.assertEqual(self.demo.commerce.balance(self.user_id).available, 2)
        self.assertEqual(self.demo.commerce.entitlement_balance(self.user_id).available, 1)
        unlocked = self.demo.commerce.unlock_version(
            self.user_id, self.versions[1]["id"]
        )
        self.assertTrue(unlocked.consumed_now)
        with self.database.read() as connection:
            statuses = connection.execute(
                "SELECT unlock_status FROM gallery_versions ORDER BY version_number"
            ).fetchall()
        self.assertEqual([row[0] for row in statuses], ["demo", "unlocked"])
        duplicate = self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result", source="127.0.0.1",
        )
        self.assertTrue(duplicate.accepted)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(
            self.service.history(order.id).order.status, PaymentStatus.PAID
        )

    def test_forged_wrong_amount_and_token_fail_closed_but_late_signed_payment_is_honored(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        wrong_amount = self.service.process_webhook(
            self.signed_callback(order, amount="48.00"), method="POST",
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
            self.signed_callback(order, amount="49.000000"), method="POST",
            path="/payments/robokassa/result",
        )
        self.assertTrue(expired.accepted)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT unlock_status FROM gallery_versions WHERE id=?",
                    (self.versions[0]["id"],),
                ).fetchone()[0],
                "demo",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM continuation_pack_grants WHERE payment_order_id=?",
                    (order.id,),
                ).fetchone()[0],
                1,
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
        amount = "49.00"
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
        self.assertEqual(metrics["recognized_revenue_rub"], 49.0)
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
        self.demo.commerce.unlock_version(self.user_id, self.versions[0]["id"])
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

    def test_refund_preview_and_delivery_retry_are_safe_and_audited(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        with self.assertRaisesRegex(PaymentError, "not paid"):
            self.service.original_for_order(order.id, self.user_id)
        self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result",
        )
        preview = self.service.preview_refund(
            order.id, 4900, "customer_request", "refund-preview"
        )
        self.assertEqual(preview.available_minor, 4900)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM refund_intents").fetchone()[0], 0
            )
        self.demo.commerce.unlock_version(self.user_id, self.versions[0]["id"])
        self.service.mark_delivery(order.id, delivered=False, error_code="max_failed")
        scheduled = self.service.schedule_delivery_retry(order.id)
        self.assertEqual(scheduled.status, PaymentStatus.DELIVERY_PENDING)
        with self.database.read() as connection:
            audit = connection.execute(
                "SELECT event_type FROM payment_audit WHERE order_id=? ORDER BY id DESC LIMIT 1",
                (order.id,),
            ).fetchone()[0]
        self.assertEqual(audit, "delivery_retry_scheduled")
        with self.assertRaisesRegex(PaymentError, "delivery-pending"):
            self.service.schedule_delivery_retry(
                self.service.create_order(self.user_id, self.versions[1]["id"], "event-2").id
            )

    def test_operator_payment_view_is_reconciled_and_privacy_safe(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result",
        )
        report = payment_show(self.database, order.provider_invoice_id)
        reconciliation = payment_reconcile(self.database, order.provider_invoice_id)
        rendered = str(report)
        self.assertTrue(report["consistent"])
        self.assertEqual(reconciliation["mismatch_count"], 0)
        self.assertNotIn(order.id, rendered)
        self.assertNotIn(order.user_id, rendered)
        self.assertNotIn(order.public_token, rendered)
        self.assertNotIn(str(self.base), rendered)

    def test_callback_database_failure_returns_no_ack_and_preserves_pending_state(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-db")
        with patch.object(
            self.database,
            "transaction",
            side_effect=sqlite3.OperationalError("simulated commit boundary failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.process_webhook(
                    self.signed_callback(order),
                    method="POST",
                    path="/payments/robokassa/result",
                )
        with self.database.read() as connection:
            status = connection.execute(
                "SELECT status FROM payment_orders WHERE id=?", (order.id,)
            ).fetchone()[0]
            unlock = connection.execute(
                "SELECT unlock_status FROM gallery_versions WHERE id=?",
                (self.versions[0]["id"],),
            ).fetchone()[0]
        self.assertEqual(status, "pending")
        self.assertEqual(unlock, "demo")

    def test_paid_delivery_pending_state_survives_service_restart(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-restart")
        self.service.process_webhook(
            self.signed_callback(order), method="POST", path="/payments/robokassa/result"
        )
        self.demo.commerce.unlock_version(self.user_id, self.versions[0]["id"])
        self.service.mark_delivery(order.id, delivered=False, error_code="max_unavailable")
        restarted = build_payment_service(self.settings, Database(self.settings.database_path))
        self.addCleanup(restarted.provider.close)
        history = restarted.history(order.id)
        self.assertEqual(history.order.status, PaymentStatus.DELIVERY_PENDING)
        original = restarted.original_for_order(order.id, self.user_id)
        self.assertTrue(original.is_file())

    def test_reconciliation_detects_receipt_mismatch(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-reconcile")
        self.service.process_webhook(
            self.signed_callback(order), method="POST", path="/payments/robokassa/result"
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE payment_receipts SET amount_minor=4800 WHERE order_id=?",
                (order.id,),
            )
        report = payment_reconcile(self.database, order.provider_invoice_id)
        self.assertEqual(report["mismatch_count"], 1)
        self.assertFalse(report["orders"][0]["checks"]["receipt_consistent"])

    def test_refund_is_prepared_but_not_sent_while_execution_flag_is_off(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result",
        )
        refund = self.service.prepare_refund(order.id, 4900, "customer_request", "refund-1")
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
        refund = self.service.prepare_refund(order.id, 4900, "customer_request", "refund-1")
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
            grant = connection.execute(
                "SELECT status FROM continuation_pack_grants WHERE payment_order_id=?",
                (order.id,),
            ).fetchone()[0]
            entitlement = connection.execute(
                "SELECT status FROM unlock_entitlements WHERE source_payment_order_id=?",
                (order.id,),
            ).fetchone()[0]
        self.assertEqual(paid["status"], "refunded")
        self.assertEqual(paid["refunded_amount_minor"], 4900)
        self.assertEqual(receipt_types, ["payment", "refund"])
        self.assertEqual(version_unlock, "demo")
        self.assertEqual(grant, "refunded")
        self.assertEqual(entitlement, "refunded")
        self.assertEqual(self.demo.commerce.balance(self.user_id).available, 0)
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

    def test_http_webhook_rejects_wrong_method_oversize_and_malformed_encoding(self) -> None:
        server = PaymentWebhookServer(
            self.service, "127.0.0.1", 0, "/payments/robokassa/result"
        )
        server.start()
        self.addCleanup(server.stop)
        url = f"http://127.0.0.1:{server.bound_port}/payments/robokassa/result"
        wrong_method = httpx.get(url)
        self.assertEqual(wrong_method.status_code, 405)
        self.assertEqual(wrong_method.headers.get("allow"), "POST")
        oversize = httpx.post(url, content=b"x" * (64 * 1024 + 1))
        self.assertEqual(oversize.status_code, 413)
        malformed = httpx.post(url, content=b"OutSum=49.00&bad=\xff")
        self.assertEqual(malformed.status_code, 400)


class RobokassaSignatureTests(TestCase):
    def test_result_signature_accepts_six_decimal_production_amount(self) -> None:
        provider = RobokassaProvider(
            merchant_login="shop", password1="one", password2="two",
            hash_algorithm="sha256", mode="production",
        )
        self.addCleanup(provider.close)
        raw = "49.000000:42:two:Shp_order=opaque"
        values = {
            "OutSum": "49.000000", "InvId": "42", "Shp_order": "opaque",
            "SignatureValue": hashlib.sha256(raw.encode()).hexdigest(),
        }
        notification = provider.parse_notification(values)
        self.assertTrue(notification.signature_valid)
        self.assertEqual(notification.amount_minor, 4900)
