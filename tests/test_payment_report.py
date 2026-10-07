import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from unittest import TestCase
from unittest.mock import patch

from app.config import Settings
from app.database import Database
from app.feedback_report import ReportUnavailable
from app.payment_report import build_report, render_markdown
from app.payments import build_payment_service


class PaymentReportTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings("", "fake", "test", self.root, payments_enabled=True,
            payment_provider="robokassa", robokassa_merchant_login="synthetic",
            robokassa_password1="synthetic-one", robokassa_password2="synthetic-two")
        self.database = Database(self.settings.database_path)
        self.now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        with self.database.transaction() as connection:
            for user in ("owner", "alice", "bob", "carol"):
                connection.execute("INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,'max',?,?)",
                    (user, "platform-" + user, self.now.isoformat()))
        self.payments = build_payment_service(self.settings, self.database, clock=lambda: self.now)

    def create(self, user, key):
        return self.payments.create_order(user, None, key, account_purchase=True)

    def pay(self, order, **extra):
        signature = hashlib.sha256(f"49.00:{order.provider_invoice_id}:synthetic-two:Shp_order={order.public_token}".encode()).hexdigest()
        result = self.payments.process_webhook({"OutSum": "49.00", "InvId": str(order.provider_invoice_id),
            "Shp_order": order.public_token, "SignatureValue": signature, **extra}, method="POST", path="/result")
        self.assertTrue(result.accepted)

    def report(self):
        return build_report(self.settings.database_path, days=7, at=self.now + timedelta(seconds=1), owner_platform_ids=("platform-owner",))

    def test_report_counts_invoices_not_reopens_and_excludes_owner_everywhere(self):
        owner = self.create("owner", "owner-checkout")
        alice = self.create("alice", "alice-checkout")
        bob = self.create("bob", "bob-checkout")
        for order in (owner, alice, bob):
            for _ in range(3):
                self.payments.record_journey(order, "payment_link_opened")
            self.payments.record_journey(order, "fail_url_return")
            self.payments.record_journey(order, "max_payfail_return", event_key="same-event")
            self.payments.record_journey(order, "max_payfail_return", event_key="same-event")
        self.now += timedelta(minutes=2)
        self.pay(owner)
        self.pay(alice)
        self.pay(alice)  # duplicate ResultURL cannot duplicate report purchases
        self.payments.record_journey(alice, "success_url_return")
        report = self.report()
        self.assertEqual(report["stages"]["checkout_created"], {"users": 2, "invoices": 2})
        self.assertEqual(report["stages"]["paid"], {"users": 1, "invoices": 1})
        self.assertEqual(report["stages"]["result_url"], {"users": 1, "invoices": 1})
        self.assertEqual(report["stages"]["fail_return"], {"users": 2, "invoices": 2})
        self.assertEqual(report["fail_url_invoices"], 2)
        self.assertEqual(report["repeat_link_open_requests"], 4)
        self.assertEqual(report["reopened_invoices"], 2)
        self.assertEqual(report["paid_after_fail_same_invoice"], 1)
        self.assertEqual(report["unpaid_after_fail_invoices"], 1)
        self.assertEqual(report["users_without_payment_after_fail"], 1)
        self.assertEqual(report["median_checkout_to_paid_seconds"], 120)
        output = json.dumps(report) + render_markdown(report)
        for private in ("alice", "bob", "platform-owner", alice.id, alice.public_token, str(alice.provider_invoice_id), str(self.root)):
            self.assertNotIn(private, output)

    def test_recovery_via_new_invoice_is_not_lost_user_or_duplicate_purchase(self):
        old = self.create("carol", "carol-old")
        self.payments.record_journey(old, "fail_url_return")
        self.now += timedelta(minutes=31)
        new = self.payments.refresh_order_for_platform_user(old.public_token, "platform-carol")
        self.now += timedelta(minutes=1)
        self.pay(new)
        report = self.report()
        self.assertEqual(report["new_invoices_after_failure"], 1)
        self.assertEqual(report["paid_after_fail_same_invoice"], 0)
        self.assertEqual(report["users_paid_after_fail_any_invoice"], 1)
        self.assertEqual(report["users_without_payment_after_fail"], 0)
        self.assertEqual(report["stages"]["paid"]["invoices"], 1)

    def test_browser_success_without_resulturl_and_unverified_payment_method_never_count_as_payment(self):
        order = self.create("alice", "unsigned")
        self.payments.record_journey(order, "success_url_return")
        self.assertEqual(self.report()["stages"]["paid"]["invoices"], 0)
        self.pay(order, PaymentMethod="4111111111111111 private@example.test secret")
        with self.database.read() as connection:
            payload = connection.execute("SELECT payload_safe_json FROM payment_events WHERE order_id=?", (order.id,)).fetchone()[0]
        self.assertNotIn("4111111111111111", payload)
        self.assertNotIn("private@example.test", payload)
        self.assertEqual(self.report()["payment_methods"], "NOT MEASURABLE")

    def test_cutoff_and_creation_cohort_half_open_and_no_payment_after_cutoff(self):
        created = self.now
        order = self.create("alice", "boundary")
        at_creation = build_report(self.settings.database_path, days=7, at=created, owner_platform_ids=("platform-owner",))
        self.assertEqual(at_creation["stages"]["checkout_created"]["invoices"], 0)
        self.now += timedelta(days=1)
        self.pay(order)
        before = build_report(self.settings.database_path, days=7, at=created + timedelta(hours=1), owner_platform_ids=("platform-owner",))
        self.assertEqual(before["stages"]["paid"]["invoices"], 0)
        self.assertEqual(before["stages"]["checkout_created"]["invoices"], 1)

    def test_readonly_uri_query_only_hash_unchanged_and_fail_closed_owner(self):
        self.create("alice", "readonly")
        before = hashlib.sha256(self.settings.database_path.read_bytes()).hexdigest()
        traces = []
        connect = sqlite3.connect
        def traced(database_uri, **kwargs):
            self.assertTrue(database_uri.endswith("?mode=ro"))
            self.assertTrue(kwargs["uri"])
            connection = connect(database_uri, **kwargs)
            connection.set_trace_callback(traces.append)
            return connection
        with patch("app.payment_report.sqlite3.connect", side_effect=traced), patch.object(Database, "initialize", side_effect=AssertionError("must not initialize")):
            self.report()
        self.assertEqual(traces[:2], ["PRAGMA query_only=ON", "BEGIN"])
        self.assertEqual(hashlib.sha256(self.settings.database_path.read_bytes()).hexdigest(), before)
        with self.assertRaises(ReportUnavailable):
            build_report(self.settings.database_path, days=7, owner_platform_ids=())
        with self.assertRaises(ReportUnavailable):
            build_report(self.settings.database_path, days=7, owner_platform_ids=("unproven",))

    def test_duplicate_result_with_changed_advisory_fields_does_not_inflate_latency(self):
        order = self.create("alice", "duplicate")
        self.now += timedelta(minutes=1)
        self.pay(order)
        self.now += timedelta(minutes=10)
        self.pay(order, PaymentMethod="BankCard")
        self.assertEqual(self.report()["median_checkout_to_paid_seconds"], 60)

    def test_missing_legacy_journey_telemetry_is_explicit_not_inferred_abandonment(self):
        self.create("alice", "legacy")
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM payment_audit WHERE actor_type='journey'")
        report = self.report()
        self.assertEqual(report["journey_instrumented_invoices"], 0)
        self.assertTrue(any("Historical" in caveat for caveat in report["caveats"]))

    def test_late_failure_return_after_paid_is_not_reported_as_lost_purchase(self):
        order = self.create("alice", "late-fail")
        self.now += timedelta(minutes=1)
        self.pay(order)
        self.now += timedelta(minutes=1)
        self.payments.record_journey(order, "fail_url_return")
        report = self.report()
        self.assertEqual(report["late_fail_returns_after_paid"], 1)
        self.assertEqual(report["unpaid_after_fail_invoices"], 0)
        self.assertEqual(report["users_without_payment_after_fail"], 0)
        self.assertEqual(report["users_paid_after_fail_any_invoice"], 0)
