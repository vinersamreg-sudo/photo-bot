"""Fail-closed parsing of reviewed Robokassa payment method aliases."""

from __future__ import annotations

from collections.abc import Sequence


# Boundary validation, not a default selection. Aliases are case-sensitive.
SUPPORTED_PAYMENT_METHODS = frozenset({"BankCard", "SBP", "SberPay"})
CONFIG_ERROR = "ROBOKASSA_PAYMENT_METHODS must be empty or contain unique supported aliases"


def validate_payment_methods(methods: Sequence[str]) -> tuple[str, ...]:
    if isinstance(methods, str):
        raise ValueError(CONFIG_ERROR)
    result = tuple(methods)
    if any(method not in SUPPORTED_PAYMENT_METHODS for method in result):
        raise ValueError(CONFIG_ERROR)
    if len(result) != len(set(result)):
        raise ValueError(CONFIG_ERROR)
    return result


def parse_payment_methods(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    return validate_payment_methods(tuple(value.strip() for value in raw.split(",")))
