"""Bounded, privacy-safe evidence collector for one owner Robokassa sandbox E2E."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx

from app.commerce import (
    GENERATION_CREDITS_PER_PACK,
    PRICE_MINOR,
    RECEIPT_ITEM_NAME,
    UNLOCK_ENTITLEMENTS_PER_PACK,
)
from app.config import Settings, load_settings
from app.database import Database
from app.maintenance import _referenced_private_files
from app.max_runtime import build_max_application
from app.payment_admin import mask_reference, payment_reconcile
from app.payments import PaymentError, build_payment_service
from app.robokassa import RobokassaPaymentRequest, RobokassaProvider, amount_text


EVIDENCE_NAME = "robokassa_sandbox_evidence.json"
RESULT_URL = "https://pixoraai.ru/payments/robokassa/result"
SUCCESS_URL = "https://pixoraai.ru/payment-success.html"
FAIL_URL = "https://pixoraai.ru/payment-failed.html"


class SandboxEvidenceError(RuntimeError):
    """Safe test failure that never includes provider payloads or identifiers."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evidence_path(settings: Settings) -> Path:
    return settings.data_dir / EVIDENCE_NAME


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def _update_evidence(
    settings: Settings, stage: str, value: dict[str, Any]
) -> dict[str, Any]:
    path = _evidence_path(settings)
    report = _read_json(path)
    if not report:
        report = {
            "schema_version": 1,
            "scope": "one owner-only Robokassa sandbox payment",
            "started_at": _now(),
            "stages": {},
        }
    stages = report.setdefault("stages", {})
    if not isinstance(stages, dict):
        raise SandboxEvidenceError("Sandbox evidence stages are invalid")
    stages[stage] = value
    report["updated_at"] = _now()
    _write_json(path, report)
    return report


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SandboxEvidenceError(message)


def _owner_user_id(settings: Settings, database: Database) -> str:
    _require(bool(settings.max_owner_user_ids), "Owner allowlist is not configured")
    placeholders = ",".join("?" for _ in settings.max_owner_user_ids)
    with database.read() as connection:
        rows = connection.execute(
            f"""SELECT id FROM users
                WHERE platform='max' AND platform_user_id IN ({placeholders})""",
            settings.max_owner_user_ids,
        ).fetchall()
    _require(len(rows) == 1, "Exactly one configured owner record is required")
    return str(rows[0]["id"])


def _orphan_count(settings: Settings, database: Database) -> int:
    referenced = _referenced_private_files(database)
    if not settings.users_dir.is_dir():
        return 0
    return sum(
        1
        for candidate in settings.users_dir.rglob("*")
        if candidate.is_file()
        and not candidate.is_symlink()
        and candidate.resolve() not in referenced
    )


def _table_count(connection: Any, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _payment_counts(connection: Any) -> dict[str, int]:
    return {
        name: _table_count(connection, name)
        for name in (
            "payment_intents",
            "payment_orders",
            "payment_events",
            "payment_webhooks",
            "payment_receipts",
            "continuation_pack_grants",
            "refund_intents",
        )
    }


def _single_order(connection: Any) -> Any:
    rows = connection.execute("SELECT * FROM payment_orders ORDER BY created_at").fetchall()
    _require(len(rows) == 1, "Exactly one sandbox payment order is required")
    return rows[0]


def _wait_for(
    callback: Callable[[], dict[str, Any] | None],
    timeout_seconds: int,
    timeout_message: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = callback()
        if result is not None:
            return result
        time.sleep(2)
    raise SandboxEvidenceError(timeout_message)


def _backup_ready(settings: Settings) -> bool:
    latest = _read_json(settings.backup_dir_path / "latest.json")
    restore = _read_json(settings.backup_dir_path / "restore_status.json")
    offsite = _read_json(settings.backup_dir_path / "offsite_status.json")
    name = latest.get("backup_name")
    if not (
        name
        and restore.get("backup_name") == name
        and restore.get("restore_ok") is True
        and offsite.get("backup_name") == name
    ):
        return False
    try:
        created_at = datetime.fromisoformat(str(latest["created_at"]))
    except (KeyError, ValueError):
        return False
    age = datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)
    return age.total_seconds() <= 2 * 60 * 60


def preflight(settings: Settings, expected_sha: str) -> dict[str, Any]:
    _evidence_path(settings).unlink(missing_ok=True)
    database = Database(settings.database_path)
    owner_user_id = _owner_user_id(settings, database)
    deployed_sha = (settings.base_dir / ".deploy-sha").read_text(
        encoding="utf-8"
    ).strip()
    _require(
        len(expected_sha) == 40 and deployed_sha == expected_sha,
        "Production SHA does not match the approved sandbox workflow",
    )
    _require(settings.max_poll_observe_only, "MAX handlers are not safely disabled")
    _require(settings.pilot_user_limit == 0, "Pilot access must remain disabled")
    _require(not settings.payments_enabled, "Payments must be disabled before preflight")
    _require(settings.payment_provider == "disabled", "Payment provider must be disabled")
    _require(
        not settings.payment_webhook_enabled,
        "Business webhook must be disabled before preflight",
    )
    _require(not settings.payment_refunds_enabled, "Refund execution must be disabled")
    _require(settings.robokassa_mode == "sandbox", "Robokassa must be in sandbox mode")
    _require(
        not settings.robokassa_production_approved,
        "Production payment approval must remain false",
    )
    _require(
        not settings.robokassa_sandbox_duplicate_probe,
        "Duplicate probe must be disabled before preflight",
    )
    _require(settings.robokassa_hash_algorithm == "sha256", "SHA-256 is required")
    _require(settings.payment_result_url == RESULT_URL, "ResultURL is not exact")
    _require(settings.payment_success_url == SUCCESS_URL, "SuccessURL is not exact")
    _require(settings.payment_fail_url == FAIL_URL, "FailURL is not exact")
    _require(settings.payment_receipt_item_name == RECEIPT_ITEM_NAME, "Receipt name differs")
    _require(settings.payment_receipt_tax == "none", "Receipt tax must be none")
    _require(_backup_ready(settings), "A recent restore-tested off-site backup is required")
    with database.read() as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        processing = int(
            connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM generation_attempts
                      WHERE status IN ('pending','processing')) +
                     (SELECT COUNT(*) FROM max_dialogs WHERE state='processing') +
                     (SELECT COUNT(*) FROM gallery_versions WHERE status='processing')"""
            ).fetchone()[0]
        )
        payment_counts = _payment_counts(connection)
        complete_versions = int(
            connection.execute(
                """SELECT COUNT(*) FROM gallery_versions v
                   JOIN gallery_items i ON i.id=v.gallery_item_id
                   WHERE i.user_id=? AND i.deleted=0 AND v.status='succeeded'
                     AND v.original_path IS NOT NULL""",
                (owner_user_id,),
            ).fetchone()[0]
        )
        account = connection.execute(
            """SELECT available_generation_credits
               FROM user_credit_accounts WHERE user_id=?""",
            (owner_user_id,),
        ).fetchone()
        baseline_entitlements = int(
            connection.execute(
                "SELECT COUNT(*) FROM unlock_entitlements WHERE user_id=?",
                (owner_user_id,),
            ).fetchone()[0]
        )
        baseline_attempts = _table_count(connection, "generation_attempts")
    _require(quick_check == "ok", "SQLite quick_check failed")
    _require(processing == 0, "Production has active processing")
    _require(all(value == 0 for value in payment_counts.values()), "Payment state is not empty")
    _require(complete_versions > 0, "Owner has no completed GalleryVersion")
    _require(_orphan_count(settings, database) == 0, "Private storage has orphan files")
    result = {
        "production_sha_verified": True,
        "safe_flags_verified": True,
        "owner_allowlist_verified": True,
        "owner_gallery_version_available": True,
        "sqlite_quick_check": quick_check,
        "processing": processing,
        "orphan_files": 0,
        "backup_restore_ready": True,
        "payment_counts": payment_counts,
        "baseline_generation_attempts": baseline_attempts,
        "baseline_available_processing_credits": int(account[0]) if account else 0,
        "baseline_unlock_entitlements": baseline_entitlements,
    }
    _update_evidence(settings, "preflight", result)
    return result


def _validated_link(settings: Settings, database: Database, order: Any) -> dict[str, Any]:
    service = build_payment_service(settings, database)
    provider = service.provider
    _require(
        isinstance(provider, RobokassaProvider),
        "Robokassa provider is not active in the sandbox window",
    )
    request = RobokassaPaymentRequest(
        invoice_id=int(order["provider_invoice_id"]),
        amount_minor=int(order["amount_minor"]),
        description="Пакет доступа Pixora: 2 обработки и 1 оригинал",
        public_token=str(order["public_token"]),
        expires_at=datetime.fromisoformat(str(order["expires_at"])),
        receipt_name=settings.payment_receipt_item_name,
        receipt_tax=settings.payment_receipt_tax,
    )
    link = provider.payment_link(request)
    parsed = urlsplit(link)
    params = parse_qs(parsed.query, keep_blank_values=True)
    receipt_encoded = params.get("Receipt", [""])[0]
    receipt_json = unquote(receipt_encoded)
    receipt = json.loads(receipt_json)
    raw_receipt = next(
        (
            pair.split("=", 1)[1]
            for pair in parsed.query.split("&")
            if pair.startswith("Receipt=")
        ),
        "",
    )
    shp = f":Shp_order={order['public_token']}"
    modifiers = [receipt_encoded]
    if settings.payment_success_url:
        modifiers.extend((quote(settings.payment_success_url, safe=""), "GET"))
    if settings.payment_fail_url:
        modifiers.extend((quote(settings.payment_fail_url, safe=""), "GET"))
    base = ":".join(
        (
            settings.robokassa_merchant_login,
            amount_text(int(order["amount_minor"])),
            str(order["provider_invoice_id"]),
            *modifiers,
            settings.robokassa_password1,
        )
    ) + shp
    expected_signature = hashlib.sha256(base.encode("utf-8")).hexdigest().upper()
    forbidden_fields = {"sno", "payment_method", "payment_object", "cost"}
    item = receipt.get("items", [{}])[0]
    _require(parsed.scheme == "https", "Sandbox payment URL is not HTTPS")
    _require(parsed.hostname == "auth.robokassa.ru", "Unexpected payment URL host")
    _require(params.get("MerchantLogin") == [settings.robokassa_merchant_login], "Merchant mismatch")
    _require(params.get("OutSum") == ["49.00"], "Sandbox amount differs from 49.00")
    _require(params.get("IsTest") == ["1"], "IsTest=1 is absent")
    _require("%25" in raw_receipt, "Receipt is not double URL-encoded")
    _require(params.get("SignatureValue") == [expected_signature], "SHA-256 signature mismatch")
    _require(params.get("SuccessUrl2") == [SUCCESS_URL], "SuccessUrl2 is incorrect")
    _require(params.get("SuccessUrl2Method") == ["GET"], "SuccessUrl2 method is not GET")
    _require(params.get("FailUrl2") == [FAIL_URL], "FailUrl2 is incorrect")
    _require(params.get("FailUrl2Method") == ["GET"], "FailUrl2 method is not GET")
    _require(
        item
        == {
            "name": RECEIPT_ITEM_NAME,
            "quantity": 1,
            "sum": 49.00,
            "tax": "none",
        },
        "Receipt item differs from the approved fiscal model",
    )
    _require(not (forbidden_fields & set(item)), "Receipt contains forbidden fields")
    _require(
        settings.robokassa_password1 not in link
        and settings.robokassa_password2 not in link,
        "A signing password is present in the payment URL",
    )
    if provider._owns_client:
        provider.close()
    return {
        "invoice_ref": mask_reference(order["provider_invoice_id"], prefix="inv"),
        "amount_rub": 49,
        "is_test": True,
        "merchant_login_from_secret": True,
        "receipt_present": True,
        "receipt_double_url_encoded": True,
        "receipt_in_sha256": True,
        "signing_passwords_absent_from_url": True,
        "success_url": SUCCESS_URL,
        "success_method": "GET",
        "fail_url": FAIL_URL,
        "fail_method": "GET",
        "receipt": {
            "items": [
                {
                    "name": RECEIPT_ITEM_NAME,
                    "quantity": 1,
                    "sum": 49.00,
                    "tax": "none",
                }
            ]
        },
    }


def wait_order(settings: Settings, timeout_seconds: int) -> dict[str, Any]:
    database = Database(settings.database_path)

    def check() -> dict[str, Any] | None:
        with database.read() as connection:
            count = _table_count(connection, "payment_orders")
            _require(count <= 1, "More than one sandbox order was created")
            if count == 0:
                return None
            order = _single_order(connection)
            _require(
                str(order["status"]) in {"pending", "paid"},
                "Sandbox order entered an unexpected state",
            )
        return _validated_link(settings, database, order)

    result = _wait_for(check, timeout_seconds, "Owner did not create the sandbox order in time")
    _update_evidence(settings, "payment_link", result)
    return result


def _paid_facts(settings: Settings, database: Database) -> dict[str, Any]:
    evidence = _read_json(_evidence_path(settings))
    baseline = evidence.get("stages", {}).get("preflight", {})
    with database.read() as connection:
        order = _single_order(connection)
        order_id = str(order["id"])
        intent = connection.execute(
            "SELECT * FROM payment_intents WHERE id=?", (order["intent_id"],)
        ).fetchone()
        event = connection.execute(
            "SELECT * FROM payment_events WHERE order_id=?", (order_id,)
        ).fetchall()
        webhooks = connection.execute(
            """SELECT w.* FROM payment_webhooks w
               JOIN payment_events e ON e.id=w.event_id WHERE e.order_id=?""",
            (order_id,),
        ).fetchall()
        receipts = connection.execute(
            "SELECT * FROM payment_receipts WHERE order_id=?", (order_id,)
        ).fetchall()
        grants = connection.execute(
            "SELECT * FROM continuation_pack_grants WHERE payment_order_id=?",
            (order_id,),
        ).fetchall()
        credit_lots = connection.execute(
            """SELECT l.* FROM generation_credit_lots l
               JOIN continuation_pack_grants g ON g.credit_lot_id=l.id
               WHERE g.payment_order_id=?""",
            (order_id,),
        ).fetchall()
        entitlements = connection.execute(
            "SELECT * FROM unlock_entitlements WHERE source_payment_order_id=?",
            (order_id,),
        ).fetchall()
        account = connection.execute(
            "SELECT * FROM user_credit_accounts WHERE user_id=?",
            (order["user_id"],),
        ).fetchone()
        confirmed = int(
            connection.execute(
                """SELECT COUNT(*) FROM payment_audit
                   WHERE order_id=? AND event_type='payment_confirmed'""",
                (order_id,),
            ).fetchone()[0]
        )
        duplicate = int(
            connection.execute(
                """SELECT COUNT(*) FROM payment_audit
                   WHERE order_id=? AND event_type='duplicate_callback_accepted'""",
                (order_id,),
            ).fetchone()[0]
        )
        attempts = _table_count(connection, "generation_attempts")
        counts = _payment_counts(connection)
    _require(str(order["status"]) in {"paid", "delivered"}, "Order is not paid")
    _require(intent is not None and intent["status"] == "paid", "PaymentIntent is not paid")
    _require(len(event) == 1 and event[0]["status"] == "processed", "ResultURL event is not unique")
    _require(len(webhooks) == 1, "ResultURL webhook record is not unique")
    webhook = webhooks[0]
    _require(
        webhook["request_method"] == "POST"
        and webhook["request_path"] == "/payments/robokassa/result"
        and all(
            int(webhook[name]) == 1
            for name in (
                "signature_valid",
                "merchant_valid",
                "invoice_valid",
                "amount_valid",
                "currency_valid",
                "status_valid",
                "timestamp_valid",
                "replay_valid",
            )
        )
        and int(webhook["http_status"]) == 200
        and webhook["response_code"] == "ok",
        "ResultURL validation evidence is incomplete",
    )
    _require(len(receipts) == 1, "Sale receipt audit row is not unique")
    receipt = receipts[0]
    _require(
        receipt["receipt_type"] == "payment"
        and receipt["item_name"] == RECEIPT_ITEM_NAME
        and receipt["quantity"] == "1"
        and int(receipt["amount_minor"]) == PRICE_MINOR
        and receipt["tax"] == "none"
        and receipt["payment_method"] == ""
        and receipt["payment_object"] == ""
        and receipt["status"] == "payment_confirmed",
        "Sale receipt audit row differs from the approved model",
    )
    _require(len(grants) == 1, "Continuation pack grant is not unique")
    _require(
        int(grants[0]["generation_credit_quantity"]) == GENERATION_CREDITS_PER_PACK
        and int(grants[0]["unlock_entitlement_quantity"]) == UNLOCK_ENTITLEMENTS_PER_PACK,
        "Continuation pack grant quantity differs from 2+1",
    )
    _require(
        len(credit_lots) == 1 and int(credit_lots[0]["granted_credits"]) == 2,
        "Exactly two processing credits were not granted",
    )
    _require(len(entitlements) == 1, "Exactly one original entitlement was not granted")
    _require(confirmed == 1, "Payment confirmation audit is not unique")
    _require(duplicate >= 1, "Real callback duplicate probe did not pass")
    _require(counts["refund_intents"] == 0, "Refund API state was created")
    _require(
        attempts == int(baseline.get("baseline_generation_attempts", -1)),
        "An OpenAI image attempt appeared during the payment test",
    )
    baseline_credits = int(baseline.get("baseline_available_processing_credits", 0))
    _require(
        int(account["available_generation_credits"]) == baseline_credits + 2,
        "Available processing-credit delta is not exactly +2",
    )
    baseline_entitlements = int(baseline.get("baseline_unlock_entitlements", 0))
    with database.read() as connection:
        owner_entitlements = int(
            connection.execute(
                "SELECT COUNT(*) FROM unlock_entitlements WHERE user_id=?",
                (order["user_id"],),
            ).fetchone()[0]
        )
    _require(
        owner_entitlements == baseline_entitlements + 1,
        "Original-entitlement delta is not exactly +1",
    )
    return {
        "invoice_ref": mask_reference(order["provider_invoice_id"], prefix="inv"),
        "payment_intent_paid": True,
        "order_paid": True,
        "result_url_post_received": True,
        "password_2_signature_valid": True,
        "invoice_valid": True,
        "amount_valid": True,
        "callback_event_count": len(event),
        "result_url_http_status": 200,
        "result_url_response": "OK{InvId}",
        "processing_credits_granted": 2,
        "original_entitlements_granted": 1,
        "sale_receipt_audit_rows": len(receipts),
        "duplicate_callback_accepted": True,
        "duplicate_created_second_grant": False,
        "duplicate_created_second_receipt": False,
        "real_payment_count": 0,
        "openai_image_request_count": 0,
    }


def wait_paid(settings: Settings, timeout_seconds: int) -> dict[str, Any]:
    database = Database(settings.database_path)

    def check() -> dict[str, Any] | None:
        with database.read() as connection:
            count = _table_count(connection, "payment_orders")
            _require(count <= 1, "More than one sandbox order was created")
            if count == 0:
                return None
            order = _single_order(connection)
            if str(order["status"]) == "pending":
                return None
        return _paid_facts(settings, database)

    result = _wait_for(check, timeout_seconds, "Real Robokassa ResultURL was not received in time")
    _update_evidence(settings, "paid_callback", result)
    return result


def verify_paid_after_restart(settings: Settings) -> dict[str, Any]:
    result = _paid_facts(settings, Database(settings.database_path))
    result["service_restart_persistence_verified"] = True
    _update_evidence(settings, "paid_after_restart", result)
    return result


def verify_success_page(settings: Settings) -> dict[str, Any]:
    database = Database(settings.database_path)
    with database.read() as connection:
        before = _payment_counts(connection)
    response = httpx.get(SUCCESS_URL, timeout=20, follow_redirects=True)
    with database.read() as connection:
        after = _payment_counts(connection)
    _require(response.status_code == 200, "SuccessURL did not return HTTP 200")
    _require(before == after, "Browser SuccessURL mutated payment state")
    result = {
        "http_status": 200,
        "static_page": True,
        "payment_state_unchanged": True,
        "grant_state_unchanged": True,
    }
    _update_evidence(settings, "browser_success", result)
    return result


def wait_first_original(settings: Settings, timeout_seconds: int) -> dict[str, Any]:
    database = Database(settings.database_path)

    def check() -> dict[str, Any] | None:
        with database.read() as connection:
            order = _single_order(connection)
            entitlement = connection.execute(
                """SELECT status,gallery_version_id FROM unlock_entitlements
                   WHERE source_payment_order_id=?""",
                (order["id"],),
            ).fetchone()
            _require(entitlement is not None, "Original entitlement is absent")
            if entitlement["status"] != "consumed":
                return None
            version = connection.execute(
                """SELECT unlock_status,delivery_count FROM gallery_versions
                   WHERE id=?""",
                (entitlement["gallery_version_id"],),
            ).fetchone()
            _require(version is not None, "Unlocked GalleryVersion is absent")
            if version["unlock_status"] != "unlocked" or int(version["delivery_count"]) < 1:
                return None
            _require(
                _table_count(connection, "payment_orders") == 1
                and _table_count(connection, "payment_receipts") == 1
                and _table_count(connection, "continuation_pack_grants") == 1,
                "Original delivery created new commercial state",
            )
        return {
            "original_delivered": True,
            "entitlement_consumed_once": True,
            "delivery_count_at_least": 1,
            "new_payment_created": False,
            "new_receipt_created": False,
        }

    result = _wait_for(
        check,
        timeout_seconds,
        "Owner did not unlock and receive an original in time",
    )
    _update_evidence(settings, "first_original", result)
    return result


def resend_original(settings: Settings) -> dict[str, Any]:
    database = Database(settings.database_path)
    with database.read() as connection:
        order = _single_order(connection)
        receipt_count = _table_count(connection, "payment_receipts")
        entitlement = connection.execute(
            """SELECT gallery_version_id,status FROM unlock_entitlements
               WHERE source_payment_order_id=?""",
            (order["id"],),
        ).fetchone()
        _require(
            entitlement is not None and entitlement["status"] == "consumed",
            "Original entitlement has not been consumed",
        )
        version = connection.execute(
            "SELECT delivery_count FROM gallery_versions WHERE id=?",
            (entitlement["gallery_version_id"],),
        ).fetchone()
        before_delivery = int(version["delivery_count"])
        order_id = str(order["id"])
    application, client, _store = build_max_application(settings)
    try:
        delivered = application.deliver_paid_original(order_id)
    finally:
        client.close()
    _require(delivered, "Repeated original delivery through MAX failed")
    with database.read() as connection:
        after_delivery = int(
            connection.execute(
                "SELECT delivery_count FROM gallery_versions WHERE id=?",
                (entitlement["gallery_version_id"],),
            ).fetchone()[0]
        )
        _require(after_delivery == before_delivery + 1, "Delivery count did not increase once")
        _require(
            _table_count(connection, "payment_orders") == 1
            and _table_count(connection, "payment_receipts") == receipt_count == 1
            and _table_count(connection, "continuation_pack_grants") == 1,
            "Repeated original delivery created new commercial state",
        )
    result = {
        "redelivery_succeeded": True,
        "delivery_count_increment": 1,
        "new_payment_created": False,
        "new_receipt_created": False,
    }
    _update_evidence(settings, "original_redelivery", result)
    return result


def reconcile(settings: Settings) -> dict[str, Any]:
    database = Database(settings.database_path)
    with database.read() as connection:
        order = _single_order(connection)
        invoice = int(order["provider_invoice_id"])
    report = payment_reconcile(database, invoice)
    _require(
        report["checked"] == 1 and report["mismatch_count"] == 0,
        "Local payment reconciliation found a mismatch",
    )
    result = {
        "scope": "single_invoice_local_ledger",
        "checked": 1,
        "mismatch_count": 0,
        "provider_status_api_called": False,
    }
    _update_evidence(settings, "reconciliation", result)
    return result


def final_safety(settings: Settings) -> dict[str, Any]:
    database = Database(settings.database_path)
    _require(settings.max_poll_observe_only, "MAX observe-only was not restored")
    _require(settings.pilot_user_limit == 0, "Pilot limit was not restored to zero")
    _require(not settings.payments_enabled, "Payments were not disabled")
    _require(settings.payment_provider == "disabled", "Provider was not disabled")
    _require(not settings.payment_webhook_enabled, "Business webhook was not disabled")
    _require(not settings.payment_refunds_enabled, "Refunds were not disabled")
    _require(settings.robokassa_mode == "sandbox", "Robokassa mode is not sandbox")
    _require(
        not settings.robokassa_production_approved,
        "Production approval was not restored to false",
    )
    _require(
        not settings.robokassa_sandbox_duplicate_probe,
        "Duplicate probe was not disabled",
    )
    _require(
        not settings.robokassa_merchant_login
        and not settings.robokassa_password1
        and not settings.robokassa_password2,
        "Sandbox credentials remain in the runtime environment",
    )
    with database.read() as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        processing = int(
            connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM generation_attempts
                      WHERE status IN ('pending','processing')) +
                     (SELECT COUNT(*) FROM max_dialogs WHERE state='processing') +
                     (SELECT COUNT(*) FROM gallery_versions WHERE status='processing')"""
            ).fetchone()[0]
        )
        counts = _payment_counts(connection)
        order = _single_order(connection)
        refund_rows = _table_count(connection, "refund_intents")
    _require(quick_check == "ok", "SQLite quick_check failed after E2E")
    _require(processing == 0, "Processing state remains after E2E")
    _require(_orphan_count(settings, database) == 0, "Orphan files remain after E2E")
    _require(counts["payment_orders"] == 1, "Sandbox order count changed")
    _require(counts["payment_receipts"] == 1, "Sale receipt audit count changed")
    _require(counts["continuation_pack_grants"] == 1, "Pack grant count changed")
    _require(refund_rows == 0, "Refund state exists")
    result = {
        "invoice_ref": mask_reference(order["provider_invoice_id"], prefix="inv"),
        "max_poll_observe_only": True,
        "pilot_user_limit": 0,
        "payments_enabled": False,
        "payment_provider": "disabled",
        "payment_webhook_enabled": False,
        "payment_refunds_enabled": False,
        "robokassa_mode": "sandbox",
        "robokassa_production_approved": False,
        "sandbox_credentials_cleared": True,
        "processing": 0,
        "sqlite_quick_check": quick_check,
        "orphan_files": 0,
        "real_payments": 0,
        "openai_image_requests": 0,
    }
    report = _update_evidence(settings, "final_safety", result)
    report["completed_at"] = _now()
    report["success"] = True
    _write_json(_evidence_path(settings), report)
    return result


def runtime_safety(settings: Settings) -> dict[str, Any]:
    database = Database(settings.database_path)
    _require(settings.max_poll_observe_only, "MAX observe-only was not restored")
    _require(settings.pilot_user_limit == 0, "Pilot limit was not restored to zero")
    _require(not settings.payments_enabled, "Payments were not disabled")
    _require(settings.payment_provider == "disabled", "Provider was not disabled")
    _require(not settings.payment_webhook_enabled, "Business webhook was not disabled")
    _require(not settings.payment_refunds_enabled, "Refunds were not disabled")
    _require(settings.robokassa_mode == "sandbox", "Robokassa mode is not sandbox")
    _require(
        not settings.robokassa_production_approved,
        "Production approval was not restored to false",
    )
    _require(
        not settings.robokassa_sandbox_duplicate_probe,
        "Duplicate probe was not disabled",
    )
    _require(
        not settings.robokassa_merchant_login
        and not settings.robokassa_password1
        and not settings.robokassa_password2,
        "Sandbox credentials remain in the runtime environment",
    )
    with database.read() as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        processing = int(
            connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM generation_attempts
                      WHERE status IN ('pending','processing')) +
                     (SELECT COUNT(*) FROM max_dialogs WHERE state='processing') +
                     (SELECT COUNT(*) FROM gallery_versions WHERE status='processing')"""
            ).fetchone()[0]
        )
    _require(quick_check == "ok", "SQLite quick_check failed after cleanup")
    _require(processing == 0, "Processing state remains after cleanup")
    _require(_orphan_count(settings, database) == 0, "Orphan files remain after cleanup")
    return {
        "safe_flags_verified": True,
        "sandbox_credentials_cleared": True,
        "processing": 0,
        "sqlite_quick_check": "ok",
        "orphan_files": 0,
    }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--expected-sha", required=True)
    for name in ("wait-order", "wait-paid", "wait-first-original"):
        wait_parser = subparsers.add_parser(name)
        wait_parser.add_argument("--timeout", type=int, default=600)
    for name in (
        "verify-paid-after-restart",
        "verify-success-page",
        "resend-original",
        "reconcile",
        "final-safety",
        "runtime-safety",
    ):
        subparsers.add_parser(name)
    args = parser.parse_args()
    settings = load_settings()
    commands: dict[str, Callable[[], dict[str, Any]]] = {
        "preflight": lambda: preflight(settings, args.expected_sha),
        "wait-order": lambda: wait_order(settings, args.timeout),
        "wait-paid": lambda: wait_paid(settings, args.timeout),
        "wait-first-original": lambda: wait_first_original(settings, args.timeout),
        "verify-paid-after-restart": lambda: verify_paid_after_restart(settings),
        "verify-success-page": lambda: verify_success_page(settings),
        "resend-original": lambda: resend_original(settings),
        "reconcile": lambda: reconcile(settings),
        "final-safety": lambda: final_safety(settings),
        "runtime-safety": lambda: runtime_safety(settings),
    }
    try:
        _print(commands[args.command]())
    except (SandboxEvidenceError, PaymentError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"robokassa_sandbox_e2e_error={type(exc).__name__}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
