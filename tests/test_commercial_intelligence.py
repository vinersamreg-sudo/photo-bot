from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.commercial_intelligence import (
    AuditWindow,
    CommercialIntelligence,
    parse_boundary,
    render_json,
    render_markdown,
)


SCHEMA = """
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
INSERT INTO schema_migrations VALUES(14);
CREATE TABLE users(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE demo_sessions(id TEXT PRIMARY KEY,user_id TEXT,started_at TEXT,created_at TEXT);
CREATE TABLE generation_attempts(
 id TEXT PRIMARY KEY,user_id TEXT,status TEXT,started_at TEXT,completed_at TEXT,
 duration_ms INTEGER,error_type TEXT,provider TEXT,model TEXT,
 secondary_source_path TEXT,prompt TEXT,provider_usage_json TEXT
);
CREATE TABLE payment_orders(
 id TEXT PRIMARY KEY,user_id TEXT,status TEXT,paid_at TEXT,created_at TEXT,
 amount_minor INTEGER,product_code TEXT,refunded_amount_minor INTEGER DEFAULT 0
);
CREATE TABLE payment_attempts(id TEXT PRIMARY KEY,order_id TEXT,status TEXT,started_at TEXT);
CREATE TABLE payment_events(
 id INTEGER PRIMARY KEY,order_id TEXT,event_type TEXT,status TEXT,received_at TEXT
);
CREATE TABLE continuation_pack_grants(
 id TEXT PRIMARY KEY,user_id TEXT,payment_order_id TEXT,credit_lot_id TEXT,
 generation_credit_quantity INTEGER,unlock_entitlement_quantity INTEGER,created_at TEXT
);
CREATE TABLE generation_credit_lots(id TEXT PRIMARY KEY,user_id TEXT);
CREATE TABLE generation_credit_reservations(
 id TEXT PRIMARY KEY,user_id TEXT,lot_id TEXT,status TEXT,consumed_at TEXT
);
CREATE TABLE credit_ledger(
 id INTEGER PRIMARY KEY,user_id TEXT,balance_after INTEGER,created_at TEXT
);
CREATE TABLE unlock_entitlements(
 id TEXT PRIMARY KEY,user_id TEXT,status TEXT,created_at TEXT,consumed_at TEXT
);
CREATE TABLE product_events(
 id INTEGER PRIMARY KEY,event_type TEXT,created_at TEXT,session_id TEXT
);
CREATE TABLE attribution_profiles(
 platform_user_id TEXT,user_id TEXT,first_source TEXT
);
"""


class CommercialIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "snapshot.sqlite3"
        connection = sqlite3.connect(self.db_path)
        connection.executescript(SCHEMA)
        users = [
            ("heavy-secret-id", "2025-12-01T08:00:00+00:00"),
            ("user-one-secret", "2026-01-01T08:00:00+00:00"),
            ("user-two-secret", "2026-01-03T08:00:00+00:00"),
            ("user-three-secret", "2026-01-04T08:00:00+00:00"),
        ]
        connection.executemany("INSERT INTO users VALUES(?,?)", users)
        connection.executemany(
            "INSERT INTO demo_sessions VALUES(?,?,?,?)",
            [
                ("s1", "user-one-secret", "2026-01-01T08:00:00+00:00", "2026-01-01T08:00:00+00:00"),
                ("s2", "user-two-secret", "2026-01-03T08:00:00+00:00", "2026-01-03T08:00:00+00:00"),
            ],
        )
        connection.executemany(
            "INSERT INTO product_events VALUES(NULL,'photo_uploaded',?,?)",
            [
                ("2026-01-01T08:05:00+00:00", "s1"),
                ("2026-01-03T08:05:00+00:00", "s2"),
            ],
        )
        attempts = [
            ("a1", "user-one-secret", "succeeded", "2026-01-01T09:00:00+00:00", "2026-01-01T09:00:10+00:00", 10000, None, "openai", "model", None, "Секретный товарный prompt", None),
            ("a2", "user-one-secret", "succeeded", "2026-01-02T09:00:00+00:00", "2026-01-02T09:00:20+00:00", 20000, None, "openai", "model", "second.jpg", "Замени фон 🔐", None),
            ("a3", "user-two-secret", "succeeded", "2026-01-03T09:00:00+00:00", "2026-01-03T09:00:30+00:00", 30000, None, "openai", "model", None, "Ретушь лица", None),
            ("a4", "user-three-secret", "failed_technical", "2026-01-04T09:00:00+00:00", "2026-01-04T09:00:01+00:00", 1000, "timeout", "gemini", "model-g", None, "private", None),
        ]
        connection.executemany(
            "INSERT INTO generation_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", attempts
        )
        self._payment(connection, "old-heavy", "heavy-secret-id", "2025-12-15T08:00:00+00:00", 999000, "large", 0.2)
        self._payment(connection, "p1", "user-one-secret", "2026-01-01T12:00:00+00:00", 4900, "small", 0.5)
        self._payment(connection, "p2", "user-one-secret", "2026-01-05T12:00:00+00:00", 4900, "small", 1.5)
        self._payment(connection, "p3", "user-two-secret", "2026-01-09T12:00:00+00:00", 199000, "large", 2.5)
        self._payment(connection, "p4", "user-two-secret", "2026-01-18T12:00:00+00:00", 4900, "small", 3.5)
        connection.execute(
            "INSERT INTO payment_orders VALUES('pending','user-three-secret','pending',NULL,'2026-01-04T12:00:00+00:00',4900,'small',0)"
        )
        connection.executemany(
            "INSERT INTO credit_ledger VALUES(NULL,?,?,?)",
            [
                ("user-one-secret", 3, "2026-01-05T12:00:02+00:00"),
                ("user-two-secret", 100, "2026-01-09T12:00:03+00:00"),
                ("heavy-secret-id", 50, "2025-12-15T08:00:01+00:00"),
            ],
        )
        connection.executemany(
            "INSERT INTO attribution_profiles VALUES(?,?,?)",
            [
                ("platform-1", "user-one-secret", "campaign"),
                ("platform-2", "user-two-secret", "direct"),
            ],
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _payment(
        connection: sqlite3.Connection,
        order_id: str,
        user_id: str,
        paid_at: str,
        amount: int,
        package: str,
        latency: float,
    ) -> None:
        connection.execute(
            "INSERT INTO payment_orders VALUES(?,?, 'delivered',?,?,?, ?,0)",
            (order_id, user_id, paid_at, paid_at, amount, package),
        )
        received = paid_at
        grant_time = (
            __import__("datetime").datetime.fromisoformat(paid_at)
            + __import__("datetime").timedelta(seconds=latency)
        ).isoformat()
        connection.execute(
            "INSERT INTO payment_events VALUES(NULL,?,'result_url','processed',?)",
            (order_id, received),
        )
        connection.execute(
            "INSERT INTO continuation_pack_grants VALUES(?,?,?,?,?,?,?)",
            (
                f"g-{order_id}",
                user_id,
                order_id,
                f"lot-{order_id}",
                100 if amount == 199000 else 2,
                50 if amount == 199000 else 1,
                grant_time,
            ),
        )
        connection.execute(
            "INSERT INTO generation_credit_lots VALUES(?,?)", (f"lot-{order_id}", user_id)
        )

    def _window(self, label: str = "jan", start: str = "2026-01-01", end: str = "2026-02-01") -> AuditWindow:
        return AuditWindow(label, parse_boundary(start, "Europe/Samara"), parse_boundary(end, "Europe/Samara"), "Europe/Samara")

    def _report(self, *windows: AuditWindow) -> dict:
        return CommercialIntelligence(self.db_path).calculate(list(windows or (self._window(),)))

    def test_cohort_aging_denominators_and_repeat_payment_windows(self) -> None:
        retention = self._report()["windows"][0]["retention"]
        repeat = retention["first_payment_to_second_payment_within_days"]
        self.assertEqual(repeat["7"]["eligible_users"], 2)
        self.assertEqual(repeat["7"]["matched_users"], 1)
        self.assertEqual(repeat["14"]["eligible_users"], 2)
        self.assertEqual(repeat["14"]["matched_users"], 2)
        self.assertEqual(repeat["30"]["eligible_users"], 1)

    def test_second_success_timing(self) -> None:
        retention = self._report()["windows"][0]["retention"]
        self.assertEqual(retention["first_success_to_second_success"]["cohort_users"], 2)
        self.assertEqual(retention["first_success_to_second_success"]["second_success_users"], 1)
        self.assertEqual(retention["median_seconds_to_second_success"], 86410.0)

    def test_concentration(self) -> None:
        revenue = self._report()["windows"][0]["revenue"]
        self.assertEqual(revenue["confirmed_orders"], 4)
        self.assertAlmostEqual(revenue["top_1_concentration_percent"], 95.41, places=2)
        self.assertEqual(revenue["historic_heavy_user_concentration_percent"], 0.0)
        self.assertEqual(revenue["packages"]["small"]["orders"], 3)
        self.assertEqual(revenue["packages"]["large"]["orders"], 1)

    def test_resulturl_grant_latency(self) -> None:
        latency = self._report()["windows"][0]["payments"]["resulturl_to_grant_latency_seconds"]
        self.assertEqual(latency["samples"], 4)
        self.assertEqual(latency["median"], 2.0)
        self.assertEqual(latency["max"], 3.5)

    def test_adjacent_comparison_windows_do_not_double_count(self) -> None:
        first = self._window("first", "2026-01-01", "2026-01-05")
        second = self._window("second", "2026-01-05", "2026-01-10")
        windows = self._report(first, second)["windows"]
        self.assertEqual(windows[0]["revenue"]["confirmed_orders"], 1)
        self.assertEqual(windows[1]["revenue"]["confirmed_orders"], 2)
        self.assertEqual(
            sum(window["revenue"]["confirmed_orders"] for window in windows), 3
        )

    def test_outputs_mask_ids_and_never_emit_prompts(self) -> None:
        report = self._report()
        outputs = render_json(report) + render_markdown(report)
        for secret in (
            "user-one-secret",
            "user-two-secret",
            "heavy-secret-id",
            "Секретный товарный prompt",
            "Замени фон",
            "platform-1",
        ):
            self.assertNotIn(secret, outputs)
        parsed = json.loads(render_json(report))
        ids = parsed["windows"][0]["interview_candidates"]["repeat_payers"]["masked_ids"]
        self.assertTrue(all(value.startswith("u_") and len(value) == 12 for value in ids))

    def test_database_is_opened_read_only(self) -> None:
        before = self.db_path.read_bytes()
        report = self._report()
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual(render_json(report), render_json(report))


if __name__ == "__main__":
    unittest.main()
