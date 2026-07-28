"""Build a local, side-effect-free Robokassa SignatureValue bisect.

The command only writes signed sandbox URLs to a local ignored directory. It
does not open the URLs, call Robokassa, create a PaymentIntent, or touch the
application database.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from dotenv import dotenv_values


PAYMENT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
OUT_SUM = "49.00"
SUCCESS_URL = "https://ravuna.ru/payment-success.html"
FAIL_URL = "https://ravuna.ru/payment-failed.html"
MOSCOW_TIMEZONE = timezone(timedelta(hours=3))
RECEIPT_JSON = (
    '{"items":[{"name":"Пакет доступа Ravuna","quantity":1,'
    '"sum":49.00,"tax":"none"}]}'
)
VARIANT_NAMES = ("A", "B", "C", "D", "E")


@dataclass(frozen=True)
class BisectVariant:
    name: str
    invoice_id: int
    parameters: tuple[tuple[str, str], ...]
    signature_base_redacted: str
    receipt_once_encoded: str | None
    receipt_query_value: str | None
    sha256_digest: str
    url: str

    def public_dict(self) -> dict[str, object]:
        return {
            "variant": self.name,
            "invoice_id": self.invoice_id,
            "parameters": [
                {"name": name, "value": value}
                for name, value in self.parameters
            ],
            "signature_base_redacted": self.signature_base_redacted,
            "receipt_once_encoded": self.receipt_once_encoded,
            "receipt_query_value": self.receipt_query_value,
            "sha256_digest": self.sha256_digest,
            "url": self.url,
        }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().upper()


def _query_value(query: str, name: str) -> str | None:
    prefix = f"{name}="
    for pair in query.split("&"):
        if pair.startswith(prefix):
            return pair[len(prefix):]
    return None


def _signature_bases(
    *,
    merchant_login: str,
    invoice_id: int,
    password1: str,
    receipt_once_encoded: str | None,
    shp_order: str | None,
    include_return_urls: bool,
) -> tuple[str, str]:
    actual_parts = [merchant_login, OUT_SUM, str(invoice_id)]
    redacted_parts = [merchant_login, OUT_SUM, str(invoice_id)]
    if receipt_once_encoded is not None:
        actual_parts.append(receipt_once_encoded)
        redacted_parts.append(receipt_once_encoded)
    # Robokassa documents that optional modifiers are added only when present.
    # Missing StepByStep and ResultUrl2 therefore do not produce empty slots.
    if include_return_urls:
        return_parts = [
            quote(SUCCESS_URL, safe=""),
            "GET",
            quote(FAIL_URL, safe=""),
            "GET",
        ]
        actual_parts.extend(return_parts)
        redacted_parts.extend(return_parts)
    actual_parts.append(password1)
    redacted_parts.append("[REDACTED]")
    actual = ":".join(actual_parts)
    redacted = ":".join(redacted_parts)
    if shp_order is not None:
        suffix = f":Shp_order={shp_order}"
        actual += suffix
        redacted += suffix
    return actual, redacted


def _variant(
    *,
    name: str,
    merchant_login: str,
    password1: str,
    invoice_id: int,
    expiration_date: str,
) -> BisectVariant:
    if name not in VARIANT_NAMES:
        raise ValueError(f"Unknown bisect variant: {name}")
    include_receipt = name in {"B", "C", "D", "E"}
    include_shp = name in {"C", "D", "E"}
    include_return_urls = name in {"D", "E"}
    include_expiration = name == "E"

    receipt_once_encoded = quote(RECEIPT_JSON, safe="") if include_receipt else None
    shp_order = f"bisect_{invoice_id}" if include_shp else None
    actual_base, redacted_base = _signature_bases(
        merchant_login=merchant_login,
        invoice_id=invoice_id,
        password1=password1,
        receipt_once_encoded=receipt_once_encoded,
        shp_order=shp_order,
        include_return_urls=include_return_urls,
    )
    digest = _digest(actual_base)
    params: list[tuple[str, str]] = [
        ("MerchantLogin", merchant_login),
        ("OutSum", OUT_SUM),
        ("InvId", str(invoice_id)),
        ("Description", f"Ravuna signature bisect {name}"),
        ("IsTest", "1"),
        ("SignatureValue", digest),
    ]
    if receipt_once_encoded is not None:
        params.append(("Receipt", receipt_once_encoded))
    if shp_order is not None:
        params.append(("Shp_order", shp_order))
    if include_return_urls:
        params.extend((
            ("SuccessUrl2", SUCCESS_URL),
            ("SuccessUrl2Method", "GET"),
            ("FailUrl2", FAIL_URL),
            ("FailUrl2Method", "GET"),
        ))
    if include_expiration:
        params.append(("ExpirationDate", expiration_date))
    query = urlencode(params)
    return BisectVariant(
        name=name,
        invoice_id=invoice_id,
        parameters=tuple(params),
        signature_base_redacted=redacted_base,
        receipt_once_encoded=receipt_once_encoded,
        receipt_query_value=_query_value(query, "Receipt"),
        sha256_digest=digest,
        url=f"{PAYMENT_URL}?{query}",
    )


def build_variants(
    *,
    merchant_login: str,
    password1: str,
    base_invoice_id: int | None = None,
    now: datetime | None = None,
) -> tuple[BisectVariant, ...]:
    merchant_login = merchant_login.strip()
    if not merchant_login:
        raise ValueError("ROBOKASSA_MERCHANT_LOGIN is required")
    if not password1:
        raise ValueError("ROBOKASSA_TEST_PASSWORD_1 is required")
    if base_invoice_id is None:
        # Millisecond Unix time with one final variant digit stays well below
        # Robokassa's documented signed 64-bit InvId ceiling.
        base_invoice_id = (time.time_ns() // 1_000_000) * 10
    invoice_ids = tuple(base_invoice_id + index for index in range(1, 6))
    if len(set(invoice_ids)) != 5:
        raise AssertionError("Bisect InvId values are not unique")
    if not all(1 <= invoice <= 9_223_372_036_854_775_807 for invoice in invoice_ids):
        raise ValueError("Generated InvId is outside Robokassa's documented range")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    expiration_date = (
        current.astimezone(MOSCOW_TIMEZONE) + timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M")
    return tuple(
        _variant(
            name=name,
            merchant_login=merchant_login,
            password1=password1,
            invoice_id=invoice_id,
            expiration_date=expiration_date,
        )
        for name, invoice_id in zip(VARIANT_NAMES, invoice_ids, strict=True)
    )


def _markdown(variants: Sequence[BisectVariant]) -> str:
    lines = [
        "# Robokassa Signature Bisect",
        "",
        "This artifact was generated locally. No URL was opened and no request "
        "was sent to Robokassa.",
        "",
        "Official modifier rule: absent optional modifiers are omitted. Missing "
        "`StepByStep` and `ResultUrl2` do **not** leave empty `::` positions. "
        "Only a missing `InvId` requires an empty slot; every bisect variant "
        "uses an explicit unique `InvId`.",
        "",
        "Open A through E manually and stop at the first variant returning "
        "Robokassa error 29.",
        "",
    ]
    for variant in variants:
        lines.extend((
            f"## Variant {variant.name}",
            "",
            f"- InvId: `{variant.invoice_id}`",
            f"- Parameters: `{', '.join(name for name, _ in variant.parameters)}`",
            f"- Signature base: `{variant.signature_base_redacted}`",
            f"- Receipt once encoded: `{variant.receipt_once_encoded or '[ABSENT]'}`",
            f"- Receipt query value: `{variant.receipt_query_value or '[ABSENT]'}`",
            f"- SHA-256: `{variant.sha256_digest}`",
            "",
            f"[Open sandbox variant {variant.name}]({variant.url})",
            "",
        ))
    return "\n".join(lines)


def write_report(
    variants: Sequence[BisectVariant],
    output_dir: Path,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "robokassa-signature-bisect.json"
    markdown_path = output_dir / "robokassa-signature-bisect.md"
    payload = {
        "network_requests_performed": 0,
        "database_changes": 0,
        "payment_intents_created": 0,
        "password_in_report": False,
        "missing_optional_modifier_slots": False,
        "variants": [variant.public_dict() for variant in variants],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(variants) + "\n", encoding="utf-8")
    return json_path, markdown_path


def _configuration(
    environ: Mapping[str, str],
    env_file: Path | None,
    *,
    prompt_missing: bool = False,
) -> tuple[str, str]:
    values: dict[str, str] = {}
    if env_file is not None:
        if not env_file.is_file():
            raise ValueError(f"Secrets env file does not exist: {env_file}")
        values.update({
            str(key): str(value)
            for key, value in dotenv_values(env_file).items()
            if value is not None
        })
    values.update({
        key: value
        for key, value in environ.items()
        if key in {"ROBOKASSA_MERCHANT_LOGIN", "ROBOKASSA_TEST_PASSWORD_1"}
    })
    merchant_login = values.get("ROBOKASSA_MERCHANT_LOGIN", "").strip()
    password1 = values.get("ROBOKASSA_TEST_PASSWORD_1", "")
    if prompt_missing and not merchant_login:
        merchant_login = input("Robokassa test MerchantLogin: ").strip()
    if prompt_missing and not password1:
        password1 = getpass.getpass(
            "Robokassa test Password #1 (input is hidden): "
        )
    if not merchant_login or not password1:
        missing = [
            name for name, value in (
                ("ROBOKASSA_MERCHANT_LOGIN", merchant_login),
                ("ROBOKASSA_TEST_PASSWORD_1", password1),
            )
            if not value
        ]
        raise ValueError(
            "Missing required local variables: " + ", ".join(missing)
        )
    return merchant_login, password1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate five local Robokassa sandbox signature bisect URLs."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional untracked dotenv file containing the existing test secrets.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("temp/robokassa-signature-bisect"),
        help="Local report directory (default: ignored temp directory).",
    )
    parser.add_argument(
        "--prompt",
        action="store_true",
        help="Prompt locally for missing values; Password #1 input is hidden.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        merchant_login, password1 = _configuration(
            os.environ,
            args.env_file,
            prompt_missing=args.prompt,
        )
        variants = build_variants(
            merchant_login=merchant_login,
            password1=password1,
        )
        json_path, markdown_path = write_report(variants, args.output_dir)
    except ValueError as error:
        print(f"Robokassa signature bisect was not generated: {error}", file=sys.stderr)
        return 2
    print(f"Generated {len(variants)} local variants without network access.")
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
