import json
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import TestCase

from app.config import Settings
from app.database import Database
from app.main import _print_operator, build_parser, run_payment_show
from app.payment_admin import pilot_report, robokassa_health


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
        self.assertFalse(refund.apply)
        self.assertFalse(resend.apply)
        self.assertFalse(retry.apply)
        self.assertFalse(submit.apply)

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
                payment_success_url="https://pixoraai.ru/payment/success",
                payment_fail_url="https://pixoraai.ru/payment/fail",
            )
            report = robokassa_health(settings, Database(settings.database_path))
            rendered = json.dumps(report)
            self.assertTrue(report["checks"]["credentials_present"])
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
