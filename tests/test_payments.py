import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, quote_plus, unquote, unquote_plus, urlparse

import httpx
from PIL import Image

from app.config import Settings
from app.commercial_operations import cost_status, payment_status
from app.commerce import RECEIPT_ITEM_NAME, USER_PRODUCT_NAME
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
from app.robokassa import (
    RobokassaPaymentRequest,
    RobokassaProvider,
    RobokassaRefundResult,
)
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
    def test_public_and_fiscal_product_name_are_the_same_single_item(self) -> None:
        self.assertEqual(USER_PRODUCT_NAME, "Пакет доступа Ravuna")
        self.assertEqual(RECEIPT_ITEM_NAME, USER_PRODUCT_NAME)

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
            payment_webhook_listener_enabled=True,
            payment_webhook_enabled=True,
            payment_result_url="https://example.test/payments/robokassa/result",
            payment_success_url="https://ravuna.ru/payment-success.html",
            payment_fail_url="https://ravuna.ru/payment-failed.html",
            robokassa_merchant_login="ravuna-test",
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
        self.session = self.demo.start_session("max", "owner", source)
        first = self.demo.generate(self.session.session_id, "Замени фон", "first")
        self.clock.advance(seconds=2)
        second = self.demo.generate(
            self.session.session_id, "Поменяй куртку", "second", correction=True
        )
        with self.database.read() as connection:
            self.versions = connection.execute(
                "SELECT id,attempt_id FROM gallery_versions ORDER BY version_number"
            ).fetchall()
        self.user_id = self.session.user_id
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
        raw_query = urlparse(order.payment_url).query
        query = parse_qs(raw_query)
        self.assertEqual(query["MerchantLogin"], ["ravuna-test"])
        self.assertEqual(query["OutSum"], ["49.00"])
        self.assertEqual(query["InvId"], [str(order.provider_invoice_id)])
        self.assertEqual(query["IsTest"], ["1"])
        self.assertEqual(query["ExpirationDate"], ["2026-07-19T11:30"])
        self.assertEqual(query["Shp_order"], [order.public_token])
        self.assertEqual(query["Description"], ["Пакет доступа Ravuna"])
        success_url = f"https://ravuna.ru/payment/success/{order.public_token}"
        fail_url = f"https://ravuna.ru/payment/fail/{order.public_token}"
        self.assertEqual(query["SuccessUrl2"], [success_url])
        self.assertEqual(query["SuccessUrl2Method"], ["GET"])
        self.assertEqual(query["FailUrl2"], [fail_url])
        self.assertEqual(query["FailUrl2Method"], ["GET"])
        self.assertIn("Receipt", query)
        receipt_once_encoded = query["Receipt"][0]
        receipt_text = unquote_plus(receipt_once_encoded)
        self.assertIn('"sum":49.00', receipt_text)
        self.assertNotIn("149", receipt_text)
        receipt = json.loads(receipt_text)
        self.assertEqual(receipt, {
            "items": [{
                "name": "Пакет доступа Ravuna",
                "quantity": 1,
                "sum": 49.0,
                "tax": "none",
            }]
        })
        self.assertNotIn("sno", receipt)
        self.assertNotIn("cost", receipt["items"][0])
        self.assertNotIn("payment_method", receipt["items"][0])
        self.assertNotIn("payment_object", receipt["items"][0])
        self.assertEqual(
            sum(int(round(item["sum"] * 100)) for item in receipt["items"]),
            int(round(float(query["OutSum"][0]) * 100)),
        )
        raw_receipt = next(
            part.split("=", 1)[1]
            for part in raw_query.split("&")
            if part.startswith("Receipt=")
        )
        self.assertIn("%25", raw_receipt)
        self.assertEqual(unquote(raw_receipt), receipt_once_encoded)
        self.assertEqual(unquote_plus(unquote(raw_receipt)), receipt_text)
        self.assertEqual(receipt["items"][0]["name"], "Пакет доступа Ravuna")
        expected_signature_base = (
            f"ravuna-test:49.00:{order.provider_invoice_id}:"
            f"{receipt_once_encoded}:{quote_plus(success_url, safe='')}:GET:"
            f"{quote_plus(fail_url, safe='')}:GET:"
            f"password-one:Shp_order={order.public_token}"
        )
        self.assertEqual(
            query["SignatureValue"][0],
            hashlib.sha256(expected_signature_base.encode("utf-8")).hexdigest().upper(),
        )
        wrongly_double_encoded_signature_base = (
            f"ravuna-test:49.00:{order.provider_invoice_id}:"
            f"{raw_receipt}:"
            f"password-one:Shp_order={order.public_token}"
        )
        self.assertNotEqual(
            query["SignatureValue"][0],
            hashlib.sha256(
                wrongly_double_encoded_signature_base.encode("utf-8")
            ).hexdigest().upper(),
        )
        tampered_receipt = receipt_once_encoded.replace("Ravuna", "Ravunb", 1)
        tampered_signature_base = (
            f"ravuna-test:49.00:{order.provider_invoice_id}:"
            f"{tampered_receipt}:"
            f"password-one:Shp_order={order.public_token}"
        )
        self.assertNotEqual(
            query["SignatureValue"][0],
            hashlib.sha256(tampered_signature_base.encode("utf-8")).hexdigest().upper(),
        )
        self.assertNotIn("password-one", order.payment_url)
        again = self.service.create_order(self.user_id, self.versions[0]["id"], "event-2")
        self.assertEqual(again.id, order.id)

    def test_account_checkout_is_purpose_scoped_and_uses_only_opaque_short_url(self) -> None:
        order = self.service.create_order(
            self.user_id,
            None,
            "account-topup",
            account_purchase=True,
        )
        replay = self.service.create_order(
            self.user_id,
            None,
            "account-topup-reopen",
            account_purchase=True,
        )

        self.assertEqual(replay.id, order.id)
        self.assertEqual(order.purpose, "account_topup")
        short = self.service.short_payment_url(order)
        self.assertEqual(short, f"https://ravuna.ru/p/{order.public_token}")
        self.assertNotIn(order.id, short)
        self.assertNotIn(self.user_id, short)
        self.assertNotIn("SignatureValue", short)
        with self.database.read() as connection:
            intent = connection.execute(
                "SELECT * FROM payment_intents WHERE id=?", (order.intent_id,)
            ).fetchone()
        self.assertEqual(intent["payment_purpose"], "account_topup")
        self.assertIsNone(intent["version_id"])
        self.assertIsNone(intent["pending_request_id"])

    def test_payment_link_rejects_timezone_naive_expiration(self) -> None:
        provider = RobokassaProvider(
            merchant_login="ravuna-test",
            password1="password-one",
            password2="password-two",
        )
        request = RobokassaPaymentRequest(
            invoice_id=1,
            amount_minor=4900,
            description="Ravuna",
            public_token="token",
            expires_at=datetime(2026, 7, 19, 8, 30),
            receipt_name="Ravuna",
            receipt_tax="none",
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            provider.payment_link(request)

    def test_invoice_id_uses_merchant_wide_timestamp_namespace(self) -> None:
        order = self.service.create_order(
            self.user_id, self.versions[0]["id"], "global-invoice-1"
        )

        expected_floor = int(
            self.clock().astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
        ) * 1000
        self.assertEqual(order.provider_invoice_id, expected_floor)
        self.assertNotEqual(order.provider_invoice_id, 1)

        self.clock.advance(minutes=31)
        next_order = self.service.create_order(
            self.user_id, self.versions[0]["id"], "global-invoice-2"
        )

        self.assertGreater(
            next_order.provider_invoice_id, order.provider_invoice_id
        )

    def test_invoice_id_stays_monotonic_after_sequence_restore(self) -> None:
        restored_high_watermark = 880000000000000000
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO payment_invoice_sequence(id,created_at) VALUES(?,?)",
                (restored_high_watermark, self.clock().isoformat()),
            )

        order = self.service.create_order(
            self.user_id, self.versions[0]["id"], "restored-invoice"
        )

        self.assertEqual(
            order.provider_invoice_id, restored_high_watermark + 1
        )

    def test_receipt_json_is_canonical_stable_and_nonempty(self) -> None:
        request = RobokassaPaymentRequest(
            invoice_id=1,
            amount_minor=4900,
            description="Пакет доступа Ravuna",
            public_token="token",
            expires_at=self.clock(),
            receipt_name="Пакет доступа Ravuna",
            receipt_tax="none",
        )
        expected = (
            '{"items":[{"name":"Пакет доступа Ravuna",'
            '"quantity":1,"sum":49.00,"tax":"none"}]}'
        )
        self.assertEqual(RobokassaProvider._receipt(request), expected)
        self.assertEqual(RobokassaProvider._receipt(request), expected)
        self.assertEqual(expected.encode("utf-8").decode("utf-8"), expected)
        for forbidden in (
            '"cost"',
            '"sno"',
            '"payment_method"',
            '"payment_object"',
            "prepayment",
            "full_prepayment",
            "advance",
            "full_payment",
        ):
            self.assertNotIn(forbidden, expected)
        empty = RobokassaPaymentRequest(
            invoice_id=1,
            amount_minor=4900,
            description="Пакет доступа Ravuna",
            public_token="token",
            expires_at=self.clock(),
            receipt_name=" ",
            receipt_tax="none",
        )
        with self.assertRaisesRegex(ValueError, "item name"):
            RobokassaProvider._receipt(empty)

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
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM payment_receipts
                       WHERE order_id=? AND receipt_type='payment'""",
                    (order.id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM payment_audit
                       WHERE order_id=? AND event_type='payment_confirmed'""",
                    (order.id,),
                ).fetchone()[0],
                1,
            )
        self.assertEqual(
            self.service.history(order.id).order.status, PaymentStatus.PAID
        )

    def test_using_package_and_redelivering_original_create_no_new_sale_receipts(self) -> None:
        order = self.service.create_order(
            self.user_id, self.versions[0]["id"], "event-single-receipt"
        )
        self.service.process_webhook(
            self.signed_callback(order),
            method="POST",
            path="/payments/robokassa/result",
        )
        self.clock.advance(seconds=2)
        self.demo.generate(
            self.session.session_id,
            "Сделай фон светлее",
            "paid-processing",
            correction=True,
        )
        self.demo.commerce.unlock_version(self.user_id, self.versions[0]["id"])
        original = self.service.original_for_order(order.id, self.user_id)
        self.assertTrue(original.is_file())
        self.assertEqual(self.service.original_for_order(order.id, self.user_id), original)
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM payment_receipts WHERE order_id=?",
                    (order.id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM payment_receipts
                       WHERE order_id=? AND receipt_type='payment'""",
                    (order.id,),
                ).fetchone()[0],
                1,
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

    def test_current_payment_migrations_and_metrics_are_privacy_safe(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-1")
        self.service.process_webhook(
            self.signed_callback(order), method="POST",
            path="/payments/robokassa/result",
        )
        metrics = cost_status(self.settings, self.database)
        status = payment_status(self.settings, self.database)
        self.assertEqual(metrics["paid_orders"], 1)
        self.assertEqual(metrics["recognized_revenue_rub"], 49.0)
        self.assertEqual(
            metrics["measured_unit_economics"]["robokassa_commission_rub"], 1.91
        )
        self.assertEqual(
            metrics["measured_unit_economics"]["estimated_contribution_rub"], 37.09
        )
        self.assertEqual(len(metrics["pilot_scenarios"]), 7)
        repurchase = metrics["pilot_scenarios"][-1]
        self.assertEqual(repurchase["orders"], 2)
        self.assertEqual(
            repurchase["contribution_before_unknown_variable_costs_rub"], -5.82
        )
        self.assertNotIn(self.user_id, str(metrics))
        self.assertEqual(status["orders"]["paid"]["count"], 1)
        with self.database.read() as connection:
            migration = connection.execute(
                "SELECT name FROM schema_migrations WHERE version=8"
            ).fetchone()
            context_migration = connection.execute(
                "SELECT name FROM schema_migrations WHERE version=12"
            ).fetchone()
            intent_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(payment_intents)")
            }
            order_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(payment_orders)")
            }
        self.assertEqual(migration[0], "version_scoped_commercial_payments")
        self.assertEqual(
            context_migration[0], "multi_source_edits_and_payment_return_context"
        )
        self.assertIn("payment_purpose", intent_columns)
        self.assertIn("payment_purpose", order_columns)

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
            merchant_login = "ravuna-test"

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
        self.assertEqual(receipt_types.count("payment"), 1)
        self.assertEqual(receipt_types.count("refund"), 1)
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
        returned = httpx.get(
            f"http://127.0.0.1:{server.bound_port}/payment/success/{order.public_token}"
        )
        self.assertEqual(returned.status_code, 303)
        self.assertIn(f"pay_{order.public_token}", returned.headers["location"])
        duplicate = httpx.post(
            f"http://127.0.0.1:{server.bound_port}/payments/robokassa/result",
            data=self.signed_callback(order),
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(delivered, [order.id])
        missing = httpx.get(f"http://127.0.0.1:{server.bound_port}/wrong")
        self.assertEqual(missing.status_code, 404)

    def test_http_webhook_runs_one_bounded_duplicate_probe_without_double_grant(self) -> None:
        order = self.service.create_order(
            self.user_id, self.versions[0]["id"], "event-http-probe"
        )
        delivered = []
        server = PaymentWebhookServer(
            self.service,
            "127.0.0.1",
            0,
            "/payments/robokassa/result",
            verify_duplicate_callback=True,
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
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM payment_events WHERE order_id=?",
                    (order.id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM payment_webhooks WHERE event_id IN "
                    "(SELECT id FROM payment_events WHERE order_id=?)",
                    (order.id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM continuation_pack_grants "
                    "WHERE payment_order_id=?",
                    (order.id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM payment_receipts "
                    "WHERE order_id=? AND receipt_type='payment'",
                    (order.id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM payment_audit "
                    "WHERE order_id=? AND event_type='duplicate_callback_accepted'",
                    (order.id,),
                ).fetchone()[0],
                1,
            )

    def test_sandbox_probe_caps_runtime_at_one_order(self) -> None:
        object.__setattr__(
            self.settings, "robokassa_sandbox_duplicate_probe", True
        )
        order = self.service.create_order(
            self.user_id, self.versions[0]["id"], "event-capped"
        )
        self.service.process_webhook(
            self.signed_callback(order),
            method="POST",
            path="/payments/robokassa/result",
        )
        with self.assertRaisesRegex(PaymentError, "sandbox order cap"):
            self.service.create_order(
                self.user_id, self.versions[1]["id"], "event-capped-second"
            )
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_orders").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM payment_intents").fetchone()[0],
                1,
            )

    def test_sandbox_probe_allows_one_new_order_after_historical_baseline(self) -> None:
        historical = self.service.create_order(
            self.user_id, self.versions[0]["id"], "historical-sandbox-order"
        )
        self.clock.advance(minutes=31)
        object.__setattr__(
            self.settings, "robokassa_sandbox_duplicate_probe", True
        )
        object.__setattr__(
            self.settings, "robokassa_sandbox_order_baseline", 1
        )

        current = self.service.create_order(
            self.user_id, self.versions[1]["id"], "current-sandbox-order"
        )
        self.service.process_webhook(
            self.signed_callback(current),
            method="POST",
            path="/payments/robokassa/result",
        )

        self.assertNotEqual(
            current.provider_invoice_id, historical.provider_invoice_id
        )
        with self.assertRaisesRegex(PaymentError, "sandbox order cap"):
            self.service.create_order(
                self.user_id, self.versions[0]["id"], "extra-sandbox-order"
            )
        with self.database.read() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM payment_orders"
                ).fetchone()[0],
                2,
            )

    def test_original_delivery_follows_consumed_entitlement_version(self) -> None:
        order = self.service.create_order(
            self.user_id, self.versions[0]["id"], "event-selected-version"
        )
        self.service.process_webhook(
            self.signed_callback(order),
            method="POST",
            path="/payments/robokassa/result",
        )
        selected_version_id = self.versions[1]["id"]
        selected = self.demo.commerce.unlock_version(
            self.user_id, selected_version_id
        )
        self.assertTrue(selected.consumed_now)
        with self.database.read() as connection:
            before = {
                row["id"]: row["delivery_count"]
                for row in connection.execute(
                    "SELECT id,delivery_count FROM gallery_versions"
                )
            }
            selected_path = Path(
                connection.execute(
                    "SELECT original_path FROM gallery_versions WHERE id=?",
                    (selected_version_id,),
                ).fetchone()[0]
            )
        self.assertEqual(
            self.service.original_for_order(order.id, self.user_id),
            selected_path,
        )
        self.service.mark_delivery(order.id, delivered=True)
        with self.database.read() as connection:
            after = {
                row["id"]: row["delivery_count"]
                for row in connection.execute(
                    "SELECT id,delivery_count FROM gallery_versions"
                )
            }
        self.assertEqual(
            after[selected_version_id], before[selected_version_id] + 1
        )
        self.assertEqual(
            after[self.versions[0]["id"]], before[self.versions[0]["id"]]
        )

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

    def test_http_webhook_transport_is_fail_closed_when_callbacks_are_disabled(self) -> None:
        order = self.service.create_order(self.user_id, self.versions[0]["id"], "event-off")
        server = PaymentWebhookServer(
            self.service,
            "127.0.0.1",
            0,
            "/payments/robokassa/result",
            accepting_callbacks=False,
        )
        server.start()
        self.addCleanup(server.stop)
        url = f"http://127.0.0.1:{server.bound_port}/payments/robokassa/result"
        response = httpx.post(url, data=self.signed_callback(order))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("retry-after"), "300")
        self.assertEqual(httpx.get(url).status_code, 405)
        with self.database.read() as connection:
            status = connection.execute(
                "SELECT status FROM payment_orders WHERE id=?", (order.id,)
            ).fetchone()[0]
            webhooks = connection.execute(
                "SELECT COUNT(*) FROM payment_webhooks"
            ).fetchone()[0]
        self.assertEqual(status, "pending")
        self.assertEqual(webhooks, 0)

    def test_browser_success_and_fail_redirects_never_confirm_payment(self) -> None:
        order = self.service.create_order(
            self.user_id, self.versions[0]["id"], "event-browser-redirect"
        )
        server = PaymentWebhookServer(
            self.service, "127.0.0.1", 0, "/payments/robokassa/result"
        )
        server.start()
        self.addCleanup(server.stop)
        base = f"http://127.0.0.1:{server.bound_port}"
        short = httpx.get(f"{base}/p/{order.public_token}")
        self.assertEqual(short.status_code, 200)
        self.assertNotIn("location", short.headers)
        self.assertIn('method="post"', short.text)
        self.assertIn(
            'action="https://auth.robokassa.ru/Merchant/Index.aspx"',
            short.text,
        )
        self.assertNotIn("Merchant/Index.aspx?", short.text)
        self.assertIn('name="SignatureValue"', short.text)
        self.assertIn('name="Receipt"', short.text)
        self.assertIn('name="SuccessUrl2"', short.text)
        self.assertIn('name="FailUrl2"', short.text)
        self.assertNotIn("password-one", short.text)
        self.assertIn("form-action https://auth.robokassa.ru", short.headers["content-security-policy"])
        pending_return = httpx.get(
            f"{base}/payment/success/{order.public_token}"
        )
        self.assertEqual(pending_return.status_code, 200)
        self.assertIn("Проверяем оплату", pending_return.text)
        failed_return = httpx.get(f"{base}/payment/fail/{order.public_token}")
        self.assertEqual(failed_return.status_code, 303)
        self.assertIn(f"payfail_{order.public_token}", failed_return.headers["location"])
        self.assertEqual(httpx.get(f"{base}/p/{'0' * 32}").status_code, 404)
        self.assertEqual(
            httpx.get(f"{base}/payment-success.html").status_code,
            404,
        )
        self.assertEqual(
            httpx.get(f"{base}/payment-failed.html").status_code,
            404,
        )
        with self.database.read() as connection:
            status = connection.execute(
                "SELECT status FROM payment_orders WHERE id=?", (order.id,)
            ).fetchone()[0]
            webhooks = connection.execute(
                "SELECT COUNT(*) FROM payment_webhooks"
            ).fetchone()[0]
        self.assertEqual(status, "pending")
        self.assertEqual(webhooks, 0)

    def test_receipt_omits_self_employed_receipt_fields(self) -> None:
        request = RobokassaPaymentRequest(
            invoice_id=1,
            amount_minor=4900,
            description="description",
            public_token="token",
            expires_at=self.clock(),
            receipt_name="Пакет доступа Ravuna",
            receipt_tax="none",
        )
        receipt = json.loads(RobokassaProvider._receipt(request))
        self.assertEqual(len(receipt["items"]), 1)
        self.assertNotIn("sno", receipt)
        self.assertNotIn("payment_method", receipt["items"][0])
        self.assertNotIn("payment_object", receipt["items"][0])


class RobokassaSignatureTests(TestCase):
    def test_production_form_uses_official_receipt_return_url_and_shp_signature(self) -> None:
        provider = RobokassaProvider(
            merchant_login="ravuna-production",
            password1="fixture-password-one",
            password2="fixture-password-two",
            hash_algorithm="sha256",
            mode="production",
        )
        self.addCleanup(provider.close)
        request = RobokassaPaymentRequest(
            invoice_id=9_223_372_036_854_775_000,
            amount_minor=4900,
            description="Пакет доступа Ravuna",
            public_token="0123456789abcdef0123456789abcdef",
            expires_at=datetime(2026, 8, 8, 12, 30, tzinfo=timezone.utc),
            receipt_name="Пакет доступа Ravuna",
            receipt_tax="none",
            success_url=(
                "https://ravuna.ru/payment/success/"
                "0123456789abcdef0123456789abcdef"
            ),
            fail_url=(
                "https://ravuna.ru/payment/fail/"
                "0123456789abcdef0123456789abcdef"
            ),
        )

        form = provider.payment_form(request)
        fields = dict(form.fields)
        receipt = quote_plus(provider._receipt(request), safe="")
        success = quote_plus(request.success_url, safe="")
        failure = quote_plus(request.fail_url, safe="")
        expected_redacted = (
            "ravuna-production:49.00:9223372036854775000:"
            f"{receipt}:{success}:GET:{failure}:GET:[PASSWORD1]:"
            "Shp_order=0123456789abcdef0123456789abcdef"
        )
        expected_actual = expected_redacted.replace(
            "[PASSWORD1]", "fixture-password-one"
        )

        self.assertEqual(form.signature_base_redacted, expected_redacted)
        self.assertIn("+", receipt)
        self.assertNotIn("%20", receipt)
        self.assertEqual(fields["Receipt"], receipt)
        self.assertEqual(fields["SuccessUrl2"], request.success_url)
        self.assertEqual(fields["SuccessUrl2Method"], "GET")
        self.assertEqual(fields["FailUrl2"], request.fail_url)
        self.assertEqual(fields["FailUrl2Method"], "GET")
        self.assertEqual(fields["Shp_order"], request.public_token)
        self.assertNotIn("IsTest", fields)
        self.assertNotIn("StepByStep", fields)
        self.assertNotIn("ResultUrl2", fields)
        self.assertEqual(
            fields["SignatureValue"],
            hashlib.sha256(expected_actual.encode("utf-8")).hexdigest().upper(),
        )
        self.assertNotIn("fixture-password-one", form.signature_base_redacted)

    def test_provider_rejects_non_sha256_algorithms(self) -> None:
        for rejected in ("md5", "sha512"):
            with self.subTest(rejected=rejected):
                with self.assertRaisesRegex(ValueError, "must be sha256"):
                    RobokassaProvider(
                        merchant_login="shop",
                        password1="one",
                        password2="two",
                        hash_algorithm=rejected,
                    )

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
