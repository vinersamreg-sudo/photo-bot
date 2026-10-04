"""Synthetic-only tests for the isolated, read-only feedback CLI."""

from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.feedback_report import ReportUnavailable, build_report, classify, main, render_markdown


AT = datetime(2026, 10, 4, 8, tzinfo=timezone.utc)
NOW = "2026-10-03T10:00:00+00:00"
EARLIER = "2026-09-25T10:00:00+00:00"
SCHEMA = """
CREATE TABLE users(id TEXT PRIMARY KEY,platform TEXT,platform_user_id TEXT);
CREATE TABLE service_feedback(id TEXT,user_id TEXT,message TEXT,created_at TEXT,source_screen TEXT);
CREATE TABLE user_feedback(id TEXT,user_id TEXT,version_id TEXT,feedback_type TEXT,rating INTEGER,message TEXT,created_at TEXT);
CREATE TABLE version_feedback(user_id TEXT,sentiment TEXT,updated_at TEXT);
CREATE TABLE gallery_items(id TEXT,user_id TEXT);
CREATE TABLE gallery_versions(id TEXT,gallery_item_id TEXT,rating INTEGER,created_at TEXT);
CREATE TABLE payment_orders(id TEXT,user_id TEXT,provider TEXT,paid_at TEXT);
CREATE TABLE payment_events(order_id TEXT,provider TEXT,event_type TEXT,status TEXT,processed_at TEXT);
CREATE TABLE continuation_pack_grants(payment_order_id TEXT,user_id TEXT,created_at TEXT);
"""


class FeedbackReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executescript(SCHEMA)
            connection.executemany("INSERT INTO users VALUES(?,?,?)", [
                ("owner-internal-private", "max", "owner-platform-private"),
                ("u1-private", "max", "user-one-platform-private"),
                ("u2-private", "max", "user-two-platform-private"),
                ("u3-private", "max", "user-three-platform-private"),
            ])

    def execute(self, sql, values=()):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(sql, values)

    def message(self, user="u1-private", text="непонятная формулировка", at=NOW, screen="main"):
        self.execute("INSERT INTO service_feedback VALUES('synthetic',?,?,?,?)", (user, text, at, screen))

    def rating(self, user="u1-private", value=4, version="v1", at=NOW):
        self.execute("INSERT INTO user_feedback VALUES('rating',?,?,'rating',?,NULL,?)", (user, version, value, at))

    def payment(self, user="u1-private", order="p1", at=EARLIER, event="result_url", processed="processed", grant=True, grant_at=None):
        self.execute("INSERT INTO payment_orders VALUES(?,?,'robokassa',?)", (order, user, at))
        self.execute("INSERT INTO payment_events VALUES(?,'robokassa',?,?,?)", (order, event, processed, at))
        if grant:
            self.execute("INSERT INTO continuation_pack_grants VALUES(?,?,?)", (order, user, grant_at or at))

    def report(self, **kwargs):
        values = {"days": 7, "at": AT, "owner_platform_ids": ("owner-platform-private",)}
        values.update(kwargs)
        return build_report(self.path, **values)

    def test_empty_report_has_no_invented_evidence_and_three_recommendations(self):
        report = self.report()
        self.assertEqual(report["current"]["messages"], 0)
        self.assertIsNone(report["current"]["ratings"]["average"])
        self.assertEqual(report["current"]["topics"], [])
        self.assertEqual(len(report["recommendations"]), 3)
        self.assertNotIn("разработ", " ".join(report["recommendations"]))
        self.assertIn("Что пока не стоит делать на основании этих данных", render_markdown(report))

    def test_owner_excluded_from_every_source_period_topic_and_payment_slice(self):
        for at in (NOW, EARLIER):
            self.message("owner-internal-private", "Добавьте пакетную обработку. Деньги списали, но результат не пришел", at)
            self.rating("owner-internal-private", value=1, at=at)
            self.execute("INSERT INTO user_feedback VALUES('c','owner-internal-private','ov','comment',NULL,'Очень медленно',?)", (at,))
            self.execute("INSERT INTO version_feedback VALUES('owner-internal-private','negative',?)", (at,))
        self.execute("INSERT INTO gallery_items VALUES('oi','owner-internal-private')")
        self.execute("INSERT INTO gallery_versions VALUES('old-owner','oi',1,?)", (NOW,))
        self.payment("owner-internal-private", "owner-p1")
        self.payment("owner-internal-private", "owner-p2")
        report = self.report()
        for period in (report["current"], report["previous"]):
            self.assertEqual(period["messages"], 0)
            self.assertEqual(period["respondents"], 0)
            self.assertEqual(period["ratings"]["count"], 0)
            self.assertEqual(period["legacy_reactions"]["negative"], 0)
            self.assertEqual(period["topics"], [])
            self.assertTrue(all(value["messages"] == 0 for value in period["cohorts"].values()))
        self.assertEqual(report["undated_legacy_ratings"]["count"], 0)

    def test_missing_owner_configuration_fails_closed(self):
        with self.assertRaisesRegex(ReportUnavailable, "MAX_OWNER_USER_IDS"):
            self.report(owner_platform_ids=())

    def test_owner_wrong_platform_unmapped_or_ambiguous_fails_closed(self):
        for ids in (("unmapped-private",), ("owner-platform-private", "missing-private")):
            with self.subTest(ids=ids), self.assertRaises(ReportUnavailable):
                self.report(owner_platform_ids=ids)
        self.execute("UPDATE users SET platform='telegram' WHERE id='owner-internal-private'")
        with self.assertRaises(ReportUnavailable):
            self.report()
        self.execute("UPDATE users SET platform='max' WHERE id='owner-internal-private'")
        self.execute("INSERT INTO users VALUES('duplicate-owner','MAX','owner-platform-private')")
        with self.assertRaises(ReportUnavailable):
            self.report()

    def test_owner_mapping_does_not_guess_from_internal_id_or_name(self):
        with self.assertRaises(ReportUnavailable):
            self.report(owner_platform_ids=("owner-internal-private",))

    def test_all_configured_owners_are_excluded(self):
        self.message("u1-private", "Отличное качество")
        report = self.report(owner_platform_ids=("owner-platform-private", "user-one-platform-private"))
        self.assertEqual(report["current"]["messages"], 0)

    def test_exact_samara_period_boundaries(self):
        for at in ("2026-09-20T11:59:59+04:00", "2026-09-20T12:00:00+04:00", "2026-09-27T11:59:59+04:00", "2026-09-27T12:00:00+04:00", "2026-10-04T11:59:59+04:00", "2026-10-04T12:00:00+04:00"):
            self.message(at=at)
        report = self.report()
        self.assertEqual(report["current"]["messages"], 2)
        self.assertEqual(report["previous"]["messages"], 2)
        self.assertEqual(report["current"]["start"], "2026-09-27T12:00:00+04:00")
        self.assertEqual(report["current"]["end_exclusive"], "2026-10-04T12:00:00+04:00")

    def test_historic_comments_ratings_reactions_and_undated_fallback_not_double_counted(self):
        self.message(text="Отличное качество", screen="result")
        self.execute("INSERT INTO user_feedback VALUES('comment','u2-private','v2','comment',NULL,'Очень медленно',?)", (NOW,))
        self.rating(value=5, version="v1")
        self.rating(value=3, version="v1")
        self.execute("INSERT INTO version_feedback VALUES('u1-private','positive',?)", (NOW,))
        self.execute("INSERT INTO gallery_items VALUES('i1','u1-private')")
        self.execute("INSERT INTO gallery_versions VALUES('v1','i1',3,?)", (NOW,))
        self.execute("INSERT INTO gallery_versions VALUES('v-old','i1',2,?)", (NOW,))
        report = self.report()
        self.assertEqual(report["current"]["messages"], 2)
        self.assertEqual(report["current"]["ratings"]["count"], 2)
        self.assertEqual(report["current"]["ratings"]["average"], 4.0)
        self.assertEqual(report["current"]["legacy_reactions"]["positive"], 1)
        self.assertEqual(report["undated_legacy_ratings"]["count"], 1)
        self.assertEqual(report["undated_legacy_ratings"]["distribution"]["2"], 1)
        self.assertEqual(report["current"]["source_screens"], {"result": 1, "unknown": 1})

    def test_authoritative_rating_outside_window_still_prevents_fallback(self):
        self.rating(at="2025-01-01T00:00:00Z")
        self.execute("INSERT INTO gallery_items VALUES('i1','u1-private')")
        self.execute("INSERT INTO gallery_versions VALUES('v1','i1',4,?)", (NOW,))
        self.assertEqual(self.report()["undated_legacy_ratings"]["count"], 0)

    def test_reactions_do_not_invent_numeric_ratings(self):
        self.execute("INSERT INTO version_feedback VALUES('u1-private','positive',?)", (NOW,))
        report = self.report()
        self.assertEqual(report["current"]["ratings"]["count"], 0)
        self.assertEqual(report["current"]["messages"], 0)

    def test_spam_does_not_increase_independent_support(self):
        for _ in range(12):
            self.message(text="Добавьте пакетную обработку")
        self.message("u2-private", "Хочу пакетную обработку")
        report = self.report()
        topic = report["current"]["topics"][0]
        self.assertEqual(topic["key"], "batch")
        self.assertEqual(topic["messages"], 13)
        self.assertEqual(topic["authors"], 2)
        self.assertEqual(report["current"]["repeat_feedback_authors"], 1)
        self.assertEqual(topic["cohorts"]["nonpayer"]["authors"], 2)

    def test_single_user_request_is_not_a_feature_recommendation(self):
        for _ in range(5):
            self.message(text="Добавьте пакетную обработку")
        recommendations = " ".join(self.report()["recommendations"])
        self.assertNotIn("Уточнить сценарий", recommendations)
        self.assertNotIn("разработ", recommendations)

    def test_negation_and_ambiguous_text_remain_unclassified(self):
        for message in ("Не нравится", "Не нужно больше стилей", "Нет проблем с оплатой", "Работает не медленно", "Не искажает лицо", "Неудобно", "Зебра на луне", "Ничего не нужно добавлять, новые стили не нужны", "Деньги не списали, ничего не получил"):
            with self.subTest(message=message):
                self.assertEqual(classify(message), ())
        self.message(text="Не нравится")
        self.assertEqual(self.report()["current"]["uncategorized"]["messages"], 1)

    def test_positive_and_negative_clauses_can_coexist(self):
        self.assertEqual(set(classify("Отличное качество, но очень медленно")), {"positive_quality", "speed"})
        self.assertIn("payment_loss", classify("Деньги списали, но результат не пришел"))
        self.assertIn("delivery", classify("Не могу скачать оригинал"))
        self.assertIn("navigation", classify("Непонятно где кнопка"))

    def test_payers_are_time_scoped_and_duplicate_callbacks_do_not_repeat_purchase(self):
        self.payment(at="2026-10-01T09:00:00Z")
        self.execute("INSERT INTO payment_events VALUES('p1','robokassa','result_url','processed','2026-10-01T09:00:01Z')")
        self.message(at="2026-09-30T10:00:00Z")
        self.message(at="2026-10-02T10:00:00Z")
        self.payment(order="p2", at="2026-10-03T09:00:00Z")
        self.message(at=NOW)
        report = self.report()
        self.assertEqual(report["current"]["cohorts"]["nonpayer"]["messages"], 1)
        self.assertEqual(report["current"]["cohorts"]["payer"]["messages"], 1)
        self.assertEqual(report["current"]["cohorts"]["repeat_payer"]["messages"], 1)

    def test_no_success_url_or_unprocessed_callback_or_grant_alone_can_prove_payment(self):
        self.payment(event="success_url")
        self.payment("u2-private", "p2", processed="received")
        self.execute("INSERT INTO continuation_pack_grants VALUES('manual','u3-private',?)", (EARLIER,))
        for user in ("u1-private", "u2-private", "u3-private"):
            self.message(user)
        cohorts = self.report()["current"]["cohorts"]
        self.assertEqual(cohorts["payer"]["messages"], 0)
        self.assertEqual(cohorts["unknown"]["messages"], 2)
        self.assertEqual(cohorts["nonpayer"]["messages"], 1)

    def test_missing_grant_and_delayed_grant_are_unknown_until_confirmation(self):
        self.payment(grant=False)
        self.payment("u2-private", "p2", at="2026-10-01T09:00:00Z", grant_at="2026-10-03T12:00:00Z")
        self.message()
        self.message("u2-private", at="2026-10-02T10:00:00Z")
        self.message("u2-private", at="2026-10-03T13:00:00Z")
        cohorts = self.report()["current"]["cohorts"]
        self.assertEqual(cohorts["unknown"]["messages"], 2)
        self.assertEqual(cohorts["payer"]["messages"], 1)

    def test_payment_timing_applies_to_topics_and_previous_period(self):
        self.payment(at="2026-09-26T10:00:00Z")
        self.message(text="Очень медленно", at=EARLIER)
        self.message(text="Очень медленно")
        report = self.report()
        self.assertEqual(report["previous"]["topics"][0]["cohorts"]["nonpayer"]["authors"], 1)
        self.assertEqual(report["current"]["topics"][0]["cohorts"]["payer"]["authors"], 1)

    def test_missing_authoritative_payment_table_is_unknown_not_nonpayer(self):
        self.execute("DROP TABLE payment_events")
        self.message()
        report = self.report()
        self.assertFalse(report["coverage"]["payment_evidence"])
        self.assertEqual(report["current"]["cohorts"]["unknown"]["messages"], 1)
        self.assertEqual(report["current"]["cohorts"]["nonpayer"]["messages"], 0)

    def test_missing_legacy_sources_are_reported_not_initialized(self):
        for table in ("user_feedback", "version_feedback", "gallery_versions"):
            self.execute(f"DROP TABLE {table}")
        self.message()
        report = self.report()
        self.assertFalse(report["coverage"]["user_feedback"])
        self.assertFalse(report["coverage"]["gallery_ratings"])
        self.assertIn("Недоступные источники", render_markdown(report))

    def test_unknown_screens_and_sensitive_text_never_appear_in_either_format(self):
        self.message(text="Отличное качество email-secret@example.test +79991234567 /private/customer.jpg private prompt", screen="email-secret@example.test")
        report = self.report()
        output = json.dumps(report, ensure_ascii=False) + render_markdown(report)
        for secret in ("email-secret", "+79991234567", "/private/customer.jpg", "private prompt", "u1-private", "owner-platform-private", "user-one-platform-private", str(self.path)):
            self.assertNotIn(secret, output)
        self.assertEqual(report["current"]["source_screens"], {"unknown": 1})

    def test_readonly_no_db_initialization_logging_or_file_mutation(self):
        self.message()
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.path.parent.iterdir()}
        with patch("app.database.Database.initialize", side_effect=AssertionError("must not initialize")), patch("logging.basicConfig", side_effect=AssertionError("must not log")):
            self.report()
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.path.parent.iterdir()}
        self.assertEqual(before, after)

    def test_connection_uses_ro_uri_query_only_and_consistent_transaction(self):
        traces = []
        real_connect = sqlite3.connect

        def connect(database_uri, **kwargs):
            self.assertTrue(database_uri.endswith("?mode=ro"))
            self.assertTrue(kwargs["uri"])
            connection = real_connect(database_uri, **kwargs)
            connection.set_trace_callback(traces.append)
            return connection

        with patch("app.feedback_report.sqlite3.connect", side_effect=connect):
            self.report()
        self.assertEqual(traces[:2], ["PRAGMA query_only=ON", "BEGIN"])
        self.assertFalse(any(query.startswith(("INSERT", "UPDATE", "CREATE", "DELETE", "ALTER")) for query in traces))

    def test_missing_database_not_created_and_error_does_not_expose_path(self):
        missing = self.path.parent / "private-missing-db.sqlite"
        with self.assertRaises(ReportUnavailable) as caught:
            build_report(missing, days=7, at=AT, owner_platform_ids=("owner-platform-private",))
        self.assertFalse(missing.exists())
        self.assertNotIn("private-missing", str(caught.exception))

    def test_invalid_date_or_unknown_author_is_counted_without_leaking(self):
        self.message(at="invalid-sensitive-date")
        self.message(user="nonexistent-sensitive-user")
        self.message(user="owner-internal-private", at="invalid-owner-date")
        report = self.report()
        self.assertEqual(report["invalid_records_excluded"], 2)
        self.assertEqual(report["current"]["messages"], 0)

    def test_comparison_and_json_human_use_same_calculations(self):
        self.message()
        self.message("u2-private")
        self.message(at=EARLIER)
        self.rating(value=5)
        self.rating(value=1, at=EARLIER)
        report = self.report()
        self.assertEqual(report["comparison"]["messages"], {"current": 2, "previous": 1, "delta": 1})
        self.assertEqual(report["comparison"]["rating_average"]["delta"], 4.0)
        self.assertIn("| Текстовые сообщения | 2 | 1 |", render_markdown(report))
        self.assertEqual(json.loads(json.dumps(report))["current"]["messages"], 2)

    def test_cli_loads_canonical_settings_and_does_not_build_application(self):
        self.message()
        settings = SimpleNamespace(database_path=self.path, max_owner_user_ids=("owner-platform-private",))
        with patch("app.config.load_settings", return_value=settings), patch("app.database.Database.initialize", side_effect=AssertionError("initialize")), redirect_stdout(io.StringIO()) as output:
            status = main(["--days", "7", "--at", AT.isoformat(), "--format", "json"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["current"]["messages"], 1)

    def test_cli_invalid_arguments_and_configuration_do_not_echo_values(self):
        with redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit):
            main(["--at", "private-invalid-value"])
        self.assertNotIn("private-invalid-value", output.getvalue())
        with patch("app.config.load_settings", side_effect=ValueError("private-secret-config")), redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main([]), 2)
        self.assertNotIn("private-secret", output.getvalue())

    def test_wrapper_routes_without_application_entrypoint(self):
        wrapper = Path(__file__).resolve().parents[1] / "scripts" / "ravuna"
        content = wrapper.read_text(encoding="utf-8")
        route = content.split('if [ "${1:-}" = "feedback-report" ]; then', 1)[1].split("fi", 1)[0]
        self.assertIn('exec "$PYTHON" -B -m app.feedback_report "$@"', route)
        self.assertNotIn("app.main", route)

    def test_weekly_wrapper_executes_real_cli_readonly_in_both_formats(self):
        bash = (str(Path("C:/Program Files/Git/bin/bash.exe"))
                if os.name == "nt" and Path("C:/Program Files/Git/bin/bash.exe").is_file()
                else shutil.which("bash"))
        if not bash:
            self.skipTest("Canonical Bash entrypoint requires Bash")
        self.message(text="Очень удобно")
        self.message("owner-internal-private", text="Очень медленно")
        self.message("u2-private", at=EARLIER)
        self.rating(value=5)
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.path.parent.iterdir()}
        wrapper = Path(__file__).resolve().parents[1] / "scripts" / "ravuna"
        environment = {
            key: value for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        environment.update({
            "RAVUNA_PYTHON": Path(sys.executable).as_posix(),
            "MAX_OWNER_USER_IDS": "owner-platform-private",
            "PYTHONIOENCODING": "utf-8", "PYTHON_DOTENV_DISABLED": "1",
        })
        outputs = {}
        for format_name in ("json", "human"):
            completed = subprocess.run(
                [bash, wrapper.as_posix(), "feedback-report", "--days", "7",
                 "--db", self.path.as_posix(), "--at", AT.isoformat(),
                 "--format", format_name], env=environment, capture_output=True,
                text=True, encoding="utf-8", timeout=30, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            outputs[format_name] = completed.stdout
        report = json.loads(outputs["json"])
        self.assertEqual(report, self.report())
        self.assertEqual(report["current"]["messages"], 1)
        self.assertEqual(report["previous"]["messages"], 1)
        self.assertEqual(outputs["human"], render_markdown(report))
        for secret in ("owner-internal-private", "owner-platform-private", "u1-private"):
            self.assertNotIn(secret, outputs["json"] + outputs["human"])
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.path.parent.iterdir()}
        self.assertEqual(before, after)

    def test_report_reads_real_canonical_schema_and_new_service_feedback(self):
        from app.database import Database
        from app.feedback import FeedbackService

        path = Path(self.temp.name) / "canonical-synthetic.sqlite3"
        database = Database(path)
        database.initialize()
        with database.transaction() as connection:
            connection.executemany(
                "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES(?,?,?,?)",
                [("synthetic-owner", "max", "test-owner", NOW),
                 ("synthetic-customer", "max", "test-customer", NOW)],
            )
        service = FeedbackService(database, clock=lambda: AT - timedelta(days=1))
        service.record_message("synthetic-owner", "Очень медленно", "main", event_key="owner-event")
        service.record_message("synthetic-customer", "Очень удобно", "main", event_key="customer-event")
        before = path.read_bytes()
        report = build_report(path, days=7, at=AT, owner_platform_ids=("test-owner",))
        self.assertEqual(report["current"]["messages"], 1)
        self.assertEqual(report["current"]["authors"], 1)
        self.assertEqual(report["current"]["cohorts"]["nonpayer"]["authors"], 1)
        self.assertTrue(all(report["coverage"].values()))
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
