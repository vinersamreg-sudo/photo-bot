from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.parse import quote, urlsplit

from scripts.robokassa_signature_bisect import (
    FAIL_URL,
    OUT_SUM,
    RECEIPT_JSON,
    SUCCESS_URL,
    build_variants,
    write_report,
)


class RobokassaSignatureBisectTests(TestCase):
    def setUp(self) -> None:
        self.password = "fixture-password-one"
        self.base_invoice = 1_800_000_000_000_000
        self.variants = build_variants(
            merchant_login="ravuna-fixture",
            password1=self.password,
            base_invoice_id=self.base_invoice,
            now=datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc),
        )

    def test_variants_have_unique_invoice_ids_and_exact_parameter_growth(self) -> None:
        self.assertEqual([variant.name for variant in self.variants], list("ABCDE"))
        self.assertEqual(
            [variant.invoice_id for variant in self.variants],
            [self.base_invoice + index for index in range(1, 6)],
        )
        expected = {
            "A": {
                "MerchantLogin", "OutSum", "InvId", "Description", "IsTest",
                "SignatureValue",
            },
            "B": {
                "MerchantLogin", "OutSum", "InvId", "Description", "IsTest",
                "SignatureValue", "Receipt",
            },
            "C": {
                "MerchantLogin", "OutSum", "InvId", "Description", "IsTest",
                "SignatureValue", "Receipt", "Shp_order",
            },
            "D": {
                "MerchantLogin", "OutSum", "InvId", "Description", "IsTest",
                "SignatureValue", "Receipt", "Shp_order", "SuccessUrl2",
                "SuccessUrl2Method", "FailUrl2", "FailUrl2Method",
            },
            "E": {
                "MerchantLogin", "OutSum", "InvId", "Description", "IsTest",
                "SignatureValue", "Receipt", "Shp_order", "SuccessUrl2",
                "SuccessUrl2Method", "FailUrl2", "FailUrl2Method",
                "ExpirationDate",
            },
        }
        for variant in self.variants:
            self.assertEqual({name for name, _ in variant.parameters}, expected[variant.name])

    def test_signature_bases_match_documented_modifier_contract(self) -> None:
        receipt = quote(RECEIPT_JSON, safe="")
        success = quote(SUCCESS_URL, safe="")
        fail = quote(FAIL_URL, safe="")
        expected = {
            "A": (
                f"ravuna-fixture:{OUT_SUM}:{self.base_invoice + 1}:[REDACTED]"
            ),
            "B": (
                f"ravuna-fixture:{OUT_SUM}:{self.base_invoice + 2}:"
                f"{receipt}:[REDACTED]"
            ),
            "C": (
                f"ravuna-fixture:{OUT_SUM}:{self.base_invoice + 3}:"
                f"{receipt}:[REDACTED]:Shp_order=bisect_{self.base_invoice + 3}"
            ),
            "D": (
                f"ravuna-fixture:{OUT_SUM}:{self.base_invoice + 4}:"
                f"{receipt}:{success}:GET:{fail}:GET:[REDACTED]:"
                f"Shp_order=bisect_{self.base_invoice + 4}"
            ),
            "E": (
                f"ravuna-fixture:{OUT_SUM}:{self.base_invoice + 5}:"
                f"{receipt}:{success}:GET:{fail}:GET:[REDACTED]:"
                f"Shp_order=bisect_{self.base_invoice + 5}"
            ),
        }
        for variant in self.variants:
            self.assertEqual(variant.signature_base_redacted, expected[variant.name])
            self.assertNotIn("::", variant.signature_base_redacted)
            self.assertNotIn("StepByStep", variant.signature_base_redacted)
            self.assertNotIn("ResultUrl2", variant.signature_base_redacted)
            self.assertNotIn("ExpirationDate", variant.signature_base_redacted)

    def test_digest_uses_actual_password_but_report_never_contains_it(self) -> None:
        for variant in self.variants:
            actual_base = variant.signature_base_redacted.replace(
                "[REDACTED]", self.password
            )
            expected = hashlib.sha256(actual_base.encode("utf-8")).hexdigest().upper()
            self.assertEqual(variant.sha256_digest, expected)
            self.assertNotIn(self.password, variant.url)
            self.assertNotIn(self.password, variant.signature_base_redacted)

    def test_receipt_records_once_encoded_and_raw_query_value(self) -> None:
        receipt_once = quote(RECEIPT_JSON, safe="")
        self.assertIsNone(self.variants[0].receipt_once_encoded)
        self.assertIsNone(self.variants[0].receipt_query_value)
        for variant in self.variants[1:]:
            self.assertEqual(variant.receipt_once_encoded, receipt_once)
            self.assertEqual(
                variant.receipt_query_value,
                quote(receipt_once, safe=""),
            )
            self.assertIn("%25", variant.receipt_query_value or "")

    def test_expiration_is_query_only_and_moscow_wall_clock(self) -> None:
        for variant in self.variants[:-1]:
            self.assertNotIn("ExpirationDate", dict(variant.parameters))
        self.assertEqual(
            dict(self.variants[-1].parameters)["ExpirationDate"],
            "2026-07-26T14:00",
        )

    def test_report_is_redacted_and_side_effect_manifest_is_explicit(self) -> None:
        with TemporaryDirectory() as directory:
            json_path, markdown_path = write_report(
                self.variants,
                Path(directory),
            )
            json_text = json_path.read_text(encoding="utf-8")
            markdown_text = markdown_path.read_text(encoding="utf-8")
            self.assertNotIn(self.password, json_text)
            self.assertNotIn(self.password, markdown_text)
            payload = json.loads(json_text)
            self.assertEqual(payload["network_requests_performed"], 0)
            self.assertEqual(payload["database_changes"], 0)
            self.assertEqual(payload["payment_intents_created"], 0)
            self.assertFalse(payload["missing_optional_modifier_slots"])
            self.assertEqual(len(payload["variants"]), 5)
            for variant in payload["variants"]:
                self.assertEqual(
                    urlsplit(variant["url"]).hostname,
                    "auth.robokassa.ru",
                )
