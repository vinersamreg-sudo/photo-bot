from datetime import datetime, timezone
from unittest import TestCase
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.config import load_settings
from app.payment_methods import parse_payment_methods
from app.robokassa import RobokassaPaymentRequest, RobokassaProvider


class PaymentMethodsTests(TestCase):
    def provider(self, methods=()):
        provider = RobokassaProvider(
            merchant_login="fixture-shop", password1="fixture-one",
            password2="fixture-two", mode="production", payment_methods=methods,
        )
        self.addCleanup(provider.close)
        return provider

    def request(self, amount):
        return RobokassaPaymentRequest(
            invoice_id=123, amount_minor=amount, description="Обработка & оригинал",
            public_token="fixture-order", expires_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
            receipt_name="Пакет доступа Ravuna", receipt_tax="none",
            success_url="https://example.test/success?a=1&b=два",
            fail_url="https://example.test/fail?a=1&b=два",
        )

    def test_config_default_blank_and_preserved_order(self):
        for raw in (None, "", "  "):
            env = {"APP_ENV": "test"}
            if raw is not None:
                env["ROBOKASSA_PAYMENT_METHODS"] = raw
            self.assertEqual(load_settings(environ=env).robokassa_payment_methods, ())
        settings = load_settings(environ={
            "APP_ENV": "test", "ROBOKASSA_PAYMENT_METHODS": " SberPay, BankCard,SBP "})
        self.assertEqual(settings.robokassa_payment_methods, ("SberPay", "BankCard", "SBP"))

    def test_invalid_config_fails_closed_without_echoing_value(self):
        for raw in ("BankCard,", ",SBP", "BankCard,,SBP", "Unknown-secret",
                    "bankcard", "BankCard,BankCard", "BankCard&secret=value"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    load_settings(environ={"APP_ENV": "test", "ROBOKASSA_PAYMENT_METHODS": raw})
                self.assertIn("ROBOKASSA_PAYMENT_METHODS", str(caught.exception))
                self.assertNotIn(raw, str(caught.exception))

    def test_provider_rejects_direct_invalid_configuration(self):
        for methods in (("Unknown",), ("BankCard", ""), ("SBP", "SBP"), "SBP"):
            with self.subTest(methods=methods):
                with self.assertRaises(ValueError):
                    self.provider(methods)

    def test_both_amounts_have_three_repeated_fields_with_unchanged_signed_fields(self):
        unrestricted = self.provider()
        selected = self.provider(parse_payment_methods("BankCard,SBP,SberPay"))
        for amount, expected_sum in ((4900, "49.00"), (199000, "1990.00")):
            with self.subTest(amount=amount):
                request = self.request(amount)
                before = unrestricted.payment_form(request)
                after = selected.payment_form(request)
                repeated = (("PaymentMethods", "BankCard"), ("PaymentMethods", "SBP"),
                            ("PaymentMethods", "SberPay"))
                self.assertEqual(after.fields, before.fields + repeated)
                self.assertEqual(dict(after.fields)["OutSum"], expected_sum)
                self.assertEqual(after.signature_base_redacted, before.signature_base_redacted)
                self.assertEqual(dict(after.fields)["SignatureValue"], dict(before.fields)["SignatureValue"])
                self.assertEqual(dict(after.fields)["Receipt"], dict(before.fields)["Receipt"])
                for field in ("SuccessUrl2", "FailUrl2", "SuccessUrl2Method", "FailUrl2Method", "Shp_order"):
                    self.assertEqual(dict(after.fields)[field], dict(before.fields)[field])
                self.assertEqual(tuple(parse_qsl(urlsplit(after.as_url()).query)), after.fields)
                self.assertEqual(tuple(parse_qsl(urlencode(after.fields))), after.fields)

    def test_empty_config_preserves_entire_legacy_form_and_url(self):
        before = self.provider()
        after = self.provider(parse_payment_methods(" "))
        for amount in (4900, 199000):
            request = self.request(amount)
            self.assertEqual(before.payment_form(request), after.payment_form(request))
            self.assertEqual(before.payment_link(request), after.payment_link(request))
            self.assertNotIn("PaymentMethods", before.payment_link(request))

    def test_configured_subset_order_is_preserved(self):
        provider = self.provider(parse_payment_methods("SberPay,BankCard"))
        fields = provider.payment_form(self.request(4900)).fields
        self.assertEqual([value for key, value in fields if key == "PaymentMethods"], ["SberPay", "BankCard"])
