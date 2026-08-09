import json
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import TestCase

from app.config import Settings
from app.database import Database
from app.main import (
    _print_operator,
    build_parser,
    run_payment_reconciliation_report,
    run_payment_show,
)
from app.payment_admin import (
    payment_expiration_reconcile,
    pilot_report,
    robokassa_health,
)


class PaymentAdminTests(TestCase):
    def test_dangerous_commands_are_dry_run_until_apply_is_explicit(self) -> None:
        parser = build_parser()
        refund = parser.parse_args([
            "refund-create", "--invoice", "42", "--amount-rub", "49",
            "--reason", "customer_request", "--idempotency-key", "case-1",
        ])
        resend = parser.parse_args(["payment-resend-original", "--invoice", "42"])
        retry = parser.parse_args(["payment-mark-delivery-retry", "--invoice", "42"])
        submit = parser.parse_args(["refund-submit", "--refund-id", "opaque"])
        expiration = parser.parse_args(["payment-expiration-reconcile"])
        self.assertFalse(refund.apply)
        self.assertFalse(resend.apply)
        self.assertFalse(retry.apply)
        self.assertFalse(submit.apply)
        self.assertFalse(expiration.apply)

    def test_payment_expiration_reconcile_is_targeted_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings("", "fake", "test", Path(directory))
            database = Database(settings.database_path)
            now = datetime.now(timezone.utc).isoformat()
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES('u','max','private',?)",
                    (now,),
                )
                connection.execute(
                    """INSERT INTO demo_sessions(
                           id,user_id,source_file_path,source_sha256,status,started_at,
                           expires_at,successful_generations,max_generations,created_at,updated_at
                       ) VALUES('s','u','source.png','sha','active',?,?,0,2,?,?)""",
                    (now, now, now, now),
                )
                for index, (intent_status, order_status) in enumerate(
                    (
                        ("pending", "expired"),
                        ("paid", "expired"),
                        ("pending", "delivered"),
                        ("pending", "pending"),
                    ),
                    start=1,
                ):
                    attempt_id = f"a{index}"
                    intent_id = f"intent-{index}"
                    order_id = f"order-{index}"
                    connection.execute(
                        """INSERT INTO generation_attempts(
                               id,idempotency_key,session_id,user_id,prompt,status,
                               started_at,provider,model,source_path,created_at
                           ) VALUES(?,?,?,?,?,'succeeded',?,'fake','fake','source.png',?)""",
                        (
                            attempt_id,
                            f"attempt-key-{index}",
                            "s",
                            "u",
                            "prompt",
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO payment_intents(
                               id,attempt_id,idempotency_key,amount_rub,status,
                               created_at,updated_at,version_id,user_id,provider,currency
                           ) VALUES(?,?,?,?,?,?,?,?,?,'robokassa','RUB')""",
                        (
                            intent_id,
                            attempt_id,
                            f"intent-key-{index}",
                            49,
                            intent_status,
                            now,
                            now,
                            f"version-{index}",
                            "u",
                        ),
                    )
                    connection.execute(
                        """INSERT INTO payment_orders(
                               id,public_token,intent_id,attempt_id,version_id,user_id,
                               provider,merchant_hash,provider_invoice_id,amount_minor,
                               currency,status,description,created_at,updated_at,expires_at
                           ) VALUES(?,?,?,?,?,?,'robokassa','hash',?,4900,'RUB',?,
                                    'package',?,?,?)""",
                        (
                            order_id,
                            f"token-{index}",
                            intent_id,
                            attempt_id,
                            f"version-{index}",
                            "u",
                            index,
                            order_status,
                            now,
                            now,
                            now,
                        ),
                    )

            dry_run = payment_expiration_reconcile(database)
            self.assertEqual(dry_run["candidate_count"], 1)
            self.assertEqual(dry_run["applied_count"], 0)
            self.assertFalse(dry_run["database_mutated"])

            applied = payment_expiration_reconcile(database, apply=True)
            repeated = payment_expiration_reconcile(database, apply=True)
            self.assertEqual(applied["applied_count"], 1)
            self.assertEqual(repeated["applied_count"], 0)
            with database.read() as connection:
                statuses = {
                    row["id"]: row["status"]
                    for row in connection.execute(
                        "SELECT id,status FROM payment_intents ORDER BY id"
                    ).fetchall()
                }
                audit_count = connection.execute(
                    """SELECT COUNT(*) FROM payment_audit
                       WHERE event_type='payment_intent_expiration_reconciled'"""
                ).fetchone()[0]
            self.assertEqual(statuses["intent-1"], "expired")
            self.assertEqual(statuses["intent-2"], "paid")
            self.assertEqual(statuses["intent-3"], "pending")
            self.assertEqual(statuses["intent-4"], "pending")
            self.assertEqual(audit_count, 1)

    def test_pilot_report_is_cohort_scoped_and_contains_no_identifiers_or_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            pilot_ids = tuple(f"private-pilot-{index}" for index in range(5))
            settings = Settings(
                "", "fake", "test", base,
                max_pilot_user_ids=pilot_ids,
                pilot_user_limit=5,
                robokassa_commission_percent=5.0,
            )
            database = Database(settings.database_path)
            now = datetime.now(timezone.utc).isoformat()
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES('u','max',?,?)",
                    (pilot_ids[0], now),
                )
                connection.execute(
                    """INSERT INTO demo_sessions(
                           id,user_id,source_file_path,source_sha256,status,started_at,expires_at,
                           successful_generations,max_generations,created_at,updated_at
                       ) VALUES('s','u','private/source.png','sha','active',?,?,1,5,?,?)""",
                    (now, now, now, now),
                )
                connection.execute(
                    """INSERT INTO generation_attempts(
                           id,idempotency_key,session_id,user_id,prompt,status,started_at,completed_at,
                           provider,model,source_path,estimated_cost,duration_ms,correction,created_at
                       ) VALUES('a','k','s','u','private personal prompt','succeeded',?,?,
                                'fake','fake','private/source.png',10,90000,0,?)""",
                    (now, now, now),
                )
                for event_type in (
                    "start", "photo_uploaded", "prompt_submitted", "processing_started",
                    "result_delivered", "unlock_clicked",
                ):
                    connection.execute(
                        """INSERT INTO product_events(event_type,created_at,session_id,attempt_id)
                           VALUES(?,?,?,?)""",
                        (event_type, now, "s", "a"),
                    )
            report = pilot_report(settings, database)
            rendered = json.dumps(report, ensure_ascii=False)
            self.assertEqual(report["invited_users"], 5)
            self.assertEqual(report["activated_users"], 1)
            self.assertEqual(report["funnel"]["result"], 1)
            self.assertEqual(report["latency_ms"]["p95"], 90000)
            self.assertNotIn("private personal prompt", rendered)
            self.assertNotIn("private/source.png", rendered)
            for value in pilot_ids:
                self.assertNotIn(value, rendered)

    def test_robokassa_health_never_outputs_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                "", "fake", "test", Path(directory),
                payment_provider="robokassa",
                robokassa_merchant_login="private-shop",
                robokassa_password1="private-one",
                robokassa_password2="private-two",
                robokassa_password3="private-three",
                payment_result_url="https://pixoraai.ru/payments/robokassa/result",
                payment_success_url="https://ravuna.ru/payment-success.html",
                payment_fail_url="https://ravuna.ru/payment-failed.html",
            )
            report = robokassa_health(settings, Database(settings.database_path))
            rendered = json.dumps(report)
            self.assertTrue(report["checks"]["credentials_present"])
            self.assertTrue(report["checks"]["success_url_exact"])
            self.assertTrue(report["checks"]["fail_url_exact"])
            self.assertTrue(report["checks"]["return_urls_without_query"])
            for secret in (
                settings.robokassa_merchant_login,
                settings.robokassa_password1,
                settings.robokassa_password2,
                settings.robokassa_password3,
            ):
                self.assertNotIn(secret, rendered)

    def test_operator_output_supports_json_human_and_missing_invoice_is_nonzero(self) -> None:
        json_output = StringIO()
        with redirect_stdout(json_output):
            _print_operator({"status": "ok", "count": 0}, "json")
        self.assertEqual(json.loads(json_output.getvalue())["status"], "ok")
        human_output = StringIO()
        with redirect_stdout(human_output):
            _print_operator({"status": "ok", "count": 0}, "human")
        self.assertIn("status: ok", human_output.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings("", "fake", "test", Path(directory))
            self.assertNotEqual(run_payment_show(settings, 999, "json"), 0)

    def test_missing_database_reconciliation_fails_without_traceback_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings("", "fake", "test", Path(directory))
            with self.assertLogs("app.main", level="ERROR") as captured:
                result = run_payment_reconciliation_report(settings, "json")
            self.assertEqual(result, 2)
            self.assertFalse(settings.database_path.exists())
            rendered = "\n".join(captured.output)
            self.assertIn("OperationalError", rendered)
            self.assertNotIn(str(settings.database_path), rendered)
