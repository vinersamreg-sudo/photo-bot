"""SQLite persistence and transactional guards for demo operations."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import NAMESPACE_URL, uuid5


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    demo_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    context_version INTEGER NOT NULL DEFAULT 1,
    risk_score INTEGER NOT NULL DEFAULT 0,
    blocked_until TEXT,
    UNIQUE(platform, platform_user_id)
);
CREATE TABLE IF NOT EXISTS demo_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    source_file_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    successful_generations INTEGER NOT NULL DEFAULT 0,
    max_generations INTEGER NOT NULL,
    completed_at TEXT,
    converted_to_paid INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id)
);
CREATE TABLE IF NOT EXISTS generation_attempts (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES demo_sessions(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    prompt TEXT NOT NULL,
    scenario_id TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    source_path TEXT NOT NULL,
    original_result_path TEXT,
    demo_result_path TEXT,
    error_type TEXT,
    error_message_safe TEXT,
    estimated_cost REAL,
    external_request_id TEXT,
    duration_ms INTEGER,
    input_size_bytes INTEGER,
    output_size_bytes INTEGER,
    requested_size TEXT,
    requested_quality TEXT,
    output_format TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    technical_refund INTEGER NOT NULL DEFAULT 0,
    correction INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT,
    result_unlocked INTEGER NOT NULL DEFAULT 0,
    edit_plan_json TEXT,
    provider_prompt TEXT,
    source_version_id TEXT,
    prompt_builder_version TEXT,
    selected_mode TEXT,
    mode_reason TEXT,
    mode_confidence REAL,
    fallback_mode TEXT,
    asset_source_type TEXT,
    asset_id TEXT,
    asset_checksum TEXT,
    mask_strategy TEXT,
    processing_provider TEXT,
    processing_provider_model TEXT,
    processing_pipeline_version TEXT,
    processing_plan_json TEXT,
    provider_mode TEXT NOT NULL DEFAULT 'stateless',
    provider_response_id TEXT,
    provider_conversation_id TEXT,
    provider_context_id TEXT,
    context_parent_response_id TEXT,
    context_depth INTEGER NOT NULL DEFAULT 0,
    context_fallback_reason TEXT,
    provider_http_status INTEGER,
    provider_duration_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_user_started ON generation_attempts(user_id, started_at);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON generation_attempts(status);
CREATE TABLE IF NOT EXISTS payment_intents (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES generation_attempts(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    amount_rub INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(attempt_id)
);
CREATE TABLE IF NOT EXISTS legal_consents (
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    offer_accepted INTEGER NOT NULL,
    personal_data_accepted INTEGER NOT NULL,
    image_rights_confirmed INTEGER NOT NULL,
    external_ai_acknowledged INTEGER NOT NULL,
    appearance_change_acknowledged INTEGER NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY(platform, platform_user_id)
);
"""


GALLERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS galleries (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS gallery_items (
    id TEXT PRIMARY KEY,
    gallery_id TEXT NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    scenario_id TEXT,
    original_source_path TEXT NOT NULL,
    storage_root_path TEXT NOT NULL,
    current_best_version_id TEXT,
    favorite INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    purge_after TEXT,
    purged_at TEXT,
    cover_preview_path TEXT,
    preview_small_path TEXT,
    preview_large_path TEXT,
    generation_count INTEGER NOT NULL DEFAULT 0,
    folder_id TEXT REFERENCES collections(id) ON DELETE SET NULL,
    last_opened_at TEXT,
    unlock_status TEXT NOT NULL DEFAULT 'demo',
    retention_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gallery_user_updated ON gallery_items(user_id, deleted, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_gallery_search ON gallery_items(user_id, title, scenario_id, folder_id, favorite, deleted);
CREATE TABLE IF NOT EXISTS gallery_versions (
    id TEXT PRIMARY KEY,
    gallery_item_id TEXT NOT NULL REFERENCES gallery_items(id) ON DELETE CASCADE,
    attempt_id TEXT UNIQUE REFERENCES generation_attempts(id) ON DELETE SET NULL,
    version_number INTEGER NOT NULL,
    parent_version_id TEXT REFERENCES gallery_versions(id) ON DELETE SET NULL,
    source_path TEXT NOT NULL,
    prompt TEXT NOT NULL,
    correction_prompt TEXT,
    effective_prompt TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    preview_watermarked_path TEXT,
    original_path TEXT,
    created_at TEXT NOT NULL,
    processing_time_ms INTEGER,
    estimated_cost REAL,
    status TEXT NOT NULL,
    rating INTEGER,
    favorite INTEGER NOT NULL DEFAULT 0,
    unlock_status TEXT NOT NULL DEFAULT 'demo',
    edit_plan_json TEXT,
    provider_prompt TEXT,
    source_version_id TEXT,
    prompt_builder_version TEXT,
    selected_mode TEXT,
    mode_reason TEXT,
    mode_confidence REAL,
    fallback_mode TEXT,
    asset_source_type TEXT,
    asset_id TEXT,
    asset_checksum TEXT,
    mask_strategy TEXT,
    processing_provider TEXT,
    processing_provider_model TEXT,
    processing_pipeline_version TEXT,
    processing_plan_json TEXT,
    provider_mode TEXT NOT NULL DEFAULT 'stateless',
    provider_response_id TEXT,
    provider_conversation_id TEXT,
    provider_context_id TEXT,
    context_parent_response_id TEXT,
    context_depth INTEGER NOT NULL DEFAULT 0,
    context_fallback_reason TEXT,
    provider_http_status INTEGER,
    provider_duration_ms INTEGER,
    provider_parent_response_id TEXT,
    provider_context_used INTEGER NOT NULL DEFAULT 0,
    provider_context_fallback_reason TEXT,
    input_version_id TEXT,
    effective_prompt_hash TEXT,
    scene_intent_hash TEXT,
    provider_request_id TEXT,
    provider_usage_json TEXT,
    UNIQUE(gallery_item_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_versions_item_number ON gallery_versions(gallery_item_id, version_number DESC);
CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, normalized_name)
);
CREATE TABLE IF NOT EXISTS gallery_item_tags (
    gallery_item_id TEXT NOT NULL REFERENCES gallery_items(id) ON DELETE CASCADE,
    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY(gallery_item_id, tag_id)
);
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    favorite_style TEXT,
    favorite_background TEXT,
    favorite_clothing TEXT,
    favorite_format TEXT,
    favorite_quality TEXT,
    favorite_scenarios_json TEXT NOT NULL DEFAULT '[]',
    recent_scenarios_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS version_feedback (
    version_id TEXT NOT NULL REFERENCES gallery_versions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sentiment TEXT NOT NULL CHECK(sentiment IN ('positive','negative')),
    reason_category TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(version_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_user_updated
ON version_feedback(user_id, updated_at DESC);
"""


MAX_DIALOG_STATES = (
    "new_user", "legal_required", "main_menu", "waiting_for_source",
    "waiting_for_prompt", "confirmation", "processing", "result_ready",
    "waiting_for_correction", "demo_exhausted", "gallery", "deleted",
)

MAX_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS max_dialogs (
    platform_user_id TEXT PRIMARY KEY,
    chat_id TEXT,
    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    state TEXT NOT NULL CHECK(state IN ({','.join(repr(value) for value in MAX_DIALOG_STATES)})),
    selected_scenario_id TEXT,
    session_id TEXT REFERENCES demo_sessions(id) ON DELETE SET NULL,
    pending_prompt TEXT,
    pending_action TEXT,
    current_gallery_item_id TEXT REFERENCES gallery_items(id) ON DELETE SET NULL,
    current_version_id TEXT REFERENCES gallery_versions(id) ON DELETE SET NULL,
    gallery_cursor INTEGER NOT NULL DEFAULT 0,
    status_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS max_active_keyboards (
    platform_user_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    message_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(platform_user_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_max_active_keyboards_user
ON max_active_keyboards(platform_user_id, created_at);
CREATE TABLE IF NOT EXISTS max_legal_documents (
    document_type TEXT NOT NULL,
    version TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    draft INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY(document_type, version)
);
CREATE TABLE IF NOT EXISTS max_legal_acceptances (
    platform_user_id TEXT NOT NULL,
    document_type TEXT NOT NULL,
    document_version TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY(platform_user_id, document_type, document_version),
    FOREIGN KEY(document_type, document_version)
        REFERENCES max_legal_documents(document_type, version)
);
CREATE TABLE IF NOT EXISTS max_processed_events (
    event_key TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('processing','completed','failed')),
    attempts INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_max_events_status ON max_processed_events(status, updated_at);
CREATE TABLE IF NOT EXISTS max_transport_state (
    name TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS max_dialog_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_user_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    event_key TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_max_transitions_user ON max_dialog_transitions(platform_user_id, id DESC);
"""

TELEMETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    session_id TEXT REFERENCES demo_sessions(id) ON DELETE SET NULL,
    attempt_id TEXT REFERENCES generation_attempts(id) ON DELETE SET NULL,
    gallery_item_id TEXT REFERENCES gallery_items(id) ON DELETE SET NULL,
    error_type TEXT,
    duration_ms INTEGER,
    estimated_cost REAL,
    parser_fallback INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_product_events_type_time
ON product_events(event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_product_events_session
ON product_events(session_id, created_at DESC);
"""

PAYMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_invoice_sequence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_orders (
    id TEXT PRIMARY KEY,
    public_token TEXT NOT NULL UNIQUE,
    intent_id TEXT REFERENCES payment_intents(id) ON DELETE SET NULL,
    attempt_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    merchant_hash TEXT NOT NULL,
    provider_invoice_id INTEGER NOT NULL UNIQUE,
    provider_payment_id TEXT,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    currency TEXT NOT NULL CHECK(currency='RUB'),
    status TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    paid_at TEXT,
    delivered_at TEXT,
    refunded_amount_minor INTEGER NOT NULL DEFAULT 0,
    failure_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_payment_orders_status_time
ON payment_orders(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_payment_orders_user_time
ON payment_orders(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS payment_attempts (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES payment_orders(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_digest TEXT,
    provider_request_id TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_payment_attempts_order
ON payment_attempts(order_id, started_at DESC);
CREATE TABLE IF NOT EXISTS payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT REFERENCES payment_orders(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_digest TEXT NOT NULL UNIQUE,
    provider_event_id TEXT,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    payload_safe_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_payment_events_order
ON payment_events(order_id, received_at DESC);
CREATE TABLE IF NOT EXISTS payment_webhooks (
    id TEXT PRIMARY KEY,
    event_id INTEGER REFERENCES payment_events(id) ON DELETE SET NULL,
    request_method TEXT NOT NULL,
    request_path TEXT NOT NULL,
    source_hash TEXT,
    signature_valid INTEGER NOT NULL,
    merchant_valid INTEGER NOT NULL,
    invoice_valid INTEGER NOT NULL,
    amount_valid INTEGER NOT NULL,
    currency_valid INTEGER NOT NULL,
    status_valid INTEGER NOT NULL,
    timestamp_valid INTEGER NOT NULL,
    replay_valid INTEGER NOT NULL,
    body_hash TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    response_code TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_receipts (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES payment_orders(id) ON DELETE CASCADE,
    receipt_type TEXT NOT NULL,
    item_name TEXT NOT NULL,
    quantity TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    tax TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    payment_object TEXT NOT NULL,
    provider_receipt_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT REFERENCES payment_orders(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    reason TEXT,
    actor_type TEXT NOT NULL,
    actor_ref_hash TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payment_audit_order
ON payment_audit(order_id, created_at DESC);
CREATE TABLE IF NOT EXISTS refund_intents (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES payment_orders(id),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    currency TEXT NOT NULL CHECK(currency='RUB'),
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    provider_request_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_refund_intents_order
ON refund_intents(order_id, created_at DESC);
CREATE TABLE IF NOT EXISTS refund_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    refund_id TEXT NOT NULL REFERENCES refund_intents(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    reason TEXT,
    actor_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

PROVIDER_CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_contexts (
    id TEXT PRIMARY KEY,
    gallery_item_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    provider_model TEXT NOT NULL,
    image_model TEXT NOT NULL,
    provider_conversation_id TEXT,
    last_response_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    depth INTEGER NOT NULL DEFAULT 0,
    reset_count INTEGER NOT NULL DEFAULT 0,
    fallback_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    expires_at TEXT NOT NULL,
    deletion_requested_at TEXT,
    deleted_at TEXT,
    delete_attempts INTEGER NOT NULL DEFAULT 0,
    last_error_class TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_context_cleanup
ON provider_contexts(status, expires_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_provider_context_user
ON provider_contexts(user_id, status);
CREATE TABLE IF NOT EXISTS provider_context_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    context_id TEXT,
    attempt_id TEXT,
    gallery_item_id TEXT,
    event_type TEXT NOT NULL,
    provider_mode TEXT NOT NULL,
    context_depth INTEGER NOT NULL DEFAULT 0,
    fallback_reason TEXT,
    error_class TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_context_events_time
ON provider_context_events(event_type, created_at DESC);
"""


COMMERCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_credit_accounts (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    free_grant_applied INTEGER NOT NULL DEFAULT 0 CHECK(free_grant_applied IN (0,1)),
    available_generation_credits INTEGER NOT NULL DEFAULT 0
        CHECK(available_generation_credits >= 0),
    reserved_generation_credits INTEGER NOT NULL DEFAULT 0
        CHECK(reserved_generation_credits >= 0),
    total_generation_credits_granted INTEGER NOT NULL DEFAULT 0
        CHECK(total_generation_credits_granted >= 0),
    total_generation_credits_consumed INTEGER NOT NULL DEFAULT 0
        CHECK(total_generation_credits_consumed >= 0),
    total_generation_credits_refunded INTEGER NOT NULL DEFAULT 0
        CHECK(total_generation_credits_refunded >= 0),
    total_generation_credits_adjusted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)
);
CREATE TABLE IF NOT EXISTS generation_credit_lots (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK(source_type IN ('initial_free','continuation_pack','admin')),
    source_payment_order_id TEXT UNIQUE,
    granted_credits INTEGER NOT NULL CHECK(granted_credits > 0),
    available_credits INTEGER NOT NULL CHECK(available_credits >= 0),
    reserved_credits INTEGER NOT NULL DEFAULT 0 CHECK(reserved_credits >= 0),
    consumed_credits INTEGER NOT NULL DEFAULT 0 CHECK(consumed_credits >= 0),
    refunded_credits INTEGER NOT NULL DEFAULT 0 CHECK(refunded_credits >= 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','exhausted','refunded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(available_credits + reserved_credits + consumed_credits + refunded_credits = granted_credits)
);
CREATE INDEX IF NOT EXISTS idx_credit_lots_user_available
ON generation_credit_lots(user_id,status,created_at);
CREATE TABLE IF NOT EXISTS generation_credit_reservations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lot_id TEXT NOT NULL REFERENCES generation_credit_lots(id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES generation_attempts(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('reserved','consumed','released')),
    release_reason TEXT,
    reserved_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    consumed_at TEXT,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_credit_reservations_status
ON generation_credit_reservations(status,reserved_at);
CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    reference_type TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    balance_after INTEGER NOT NULL CHECK(balance_after >= 0),
    reserved_after INTEGER NOT NULL CHECK(reserved_after >= 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_time
ON credit_ledger(user_id,id DESC);
CREATE TABLE IF NOT EXISTS unlock_entitlements (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_payment_intent_id TEXT,
    source_payment_order_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('available','reserved','consumed','cancelled','refunded')),
    gallery_version_id TEXT REFERENCES gallery_versions(id) ON DELETE SET NULL,
    reserved_at TEXT,
    consumed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unlock_entitlements_user_status
ON unlock_entitlements(user_id,status,created_at);
CREATE TABLE IF NOT EXISTS continuation_pack_grants (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_order_id TEXT NOT NULL UNIQUE,
    payment_intent_id TEXT,
    credit_lot_id TEXT NOT NULL UNIQUE REFERENCES generation_credit_lots(id),
    entitlement_id TEXT NOT NULL UNIQUE REFERENCES unlock_entitlements(id),
    generation_credit_quantity INTEGER NOT NULL CHECK(generation_credit_quantity=2),
    unlock_entitlement_quantity INTEGER NOT NULL CHECK(unlock_entitlement_quantity=1),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','refunded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pack_grants_user_time
ON continuation_pack_grants(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS commerce_admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_hash TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('generation_credit','unlock_entitlement')),
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    applied INTEGER NOT NULL CHECK(applied IN (0,1)),
    created_at TEXT NOT NULL
);
"""


GROWTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_ui_sessions (
    platform_user_id TEXT PRIMARY KEY,
    chat_id TEXT,
    active_ui_message_id TEXT,
    active_screen TEXT NOT NULL DEFAULT 'none',
    active_screen_context TEXT NOT NULL DEFAULT '{}',
    ui_revision INTEGER NOT NULL DEFAULT 0 CHECK(ui_revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS referral_codes (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    referral_code TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS referral_relationships (
    id TEXT PRIMARY KEY,
    referral_code TEXT NOT NULL REFERENCES referral_codes(referral_code),
    inviter_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invitee_platform_user_id TEXT NOT NULL UNIQUE,
    invitee_user_id TEXT UNIQUE REFERENCES users(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','rewarded','disqualified')),
    started_at TEXT NOT NULL,
    rewarded_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(inviter_user_id <> COALESCE(invitee_user_id, ''))
);
CREATE INDEX IF NOT EXISTS idx_referral_relationships_inviter
ON referral_relationships(inviter_user_id,status,created_at);
CREATE TABLE IF NOT EXISTS bonus_credit_transactions (
    id TEXT PRIMARY KEY,
    relationship_id TEXT NOT NULL UNIQUE
        REFERENCES referral_relationships(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL CHECK(quantity=2),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attribution_profiles (
    platform_user_id TEXT PRIMARY KEY,
    user_id TEXT UNIQUE REFERENCES users(id) ON DELETE SET NULL,
    first_source TEXT NOT NULL,
    first_campaign TEXT,
    first_referrer_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    first_started_at TEXT NOT NULL,
    last_source TEXT NOT NULL,
    last_campaign TEXT,
    last_started_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attribution_profiles_first_source
ON attribution_profiles(first_source,first_started_at);
CREATE TABLE IF NOT EXISTS attribution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_user_id TEXT NOT NULL,
    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'bot_started','photo_uploaded','first_generation_success',
        'payment_offer_opened','payment_started','payment_success',
        'share_opened','referral_started','referral_rewarded'
    )),
    source TEXT,
    campaign TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attribution_events_type_time
ON attribution_events(event_type,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_attribution_events_source_time
ON attribution_events(source,created_at DESC);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        connection = self.connect()
        try:
            connection.executescript(SCHEMA)
            existing = {
                row[1] for row in connection.execute("PRAGMA table_info(generation_attempts)")
            }
            for name, declaration in (
                ("requested_size", "TEXT"),
                ("requested_quality", "TEXT"),
                ("output_format", "TEXT"),
                ("parent_version_id", "TEXT"),
                ("correction_prompt", "TEXT"),
                ("effective_prompt", "TEXT"),
                ("edit_plan_json", "TEXT"),
                ("provider_prompt", "TEXT"),
                ("source_version_id", "TEXT"),
                ("prompt_builder_version", "TEXT"),
                ("selected_mode", "TEXT"),
                ("mode_reason", "TEXT"),
                ("mode_confidence", "REAL"),
                ("fallback_mode", "TEXT"),
                ("asset_source_type", "TEXT"),
                ("asset_id", "TEXT"),
                ("asset_checksum", "TEXT"),
                ("mask_strategy", "TEXT"),
                ("processing_provider", "TEXT"),
                ("processing_provider_model", "TEXT"),
                ("processing_pipeline_version", "TEXT"),
                ("processing_plan_json", "TEXT"),
                ("provider_mode", "TEXT NOT NULL DEFAULT 'stateless'"),
                ("provider_response_id", "TEXT"),
                ("provider_conversation_id", "TEXT"),
                ("provider_context_id", "TEXT"),
                ("context_parent_response_id", "TEXT"),
                ("context_depth", "INTEGER NOT NULL DEFAULT 0"),
                ("context_fallback_reason", "TEXT"),
                ("provider_http_status", "INTEGER"),
                ("provider_duration_ms", "INTEGER"),
                ("provider_parent_response_id", "TEXT"),
                ("provider_context_used", "INTEGER NOT NULL DEFAULT 0"),
                ("provider_context_fallback_reason", "TEXT"),
                ("input_version_id", "TEXT"),
                ("effective_prompt_hash", "TEXT"),
                ("scene_intent_hash", "TEXT"),
                ("provider_request_id", "TEXT"),
                ("provider_usage_json", "TEXT"),
            ):
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE generation_attempts ADD COLUMN {name} {declaration}"
                    )
            session_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(demo_sessions)")
            }
            if "gallery_item_id" not in session_columns:
                connection.execute(
                    "ALTER TABLE demo_sessions ADD COLUMN gallery_item_id TEXT"
                )
            connection.executescript(GALLERY_SCHEMA)
            version_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(gallery_versions)")
            }
            for name, declaration in (
                ("edit_plan_json", "TEXT"),
                ("provider_prompt", "TEXT"),
                ("source_version_id", "TEXT"),
                ("prompt_builder_version", "TEXT"),
                ("selected_mode", "TEXT"),
                ("mode_reason", "TEXT"),
                ("mode_confidence", "REAL"),
                ("fallback_mode", "TEXT"),
                ("asset_source_type", "TEXT"),
                ("asset_id", "TEXT"),
                ("asset_checksum", "TEXT"),
                ("mask_strategy", "TEXT"),
                ("processing_provider", "TEXT"),
                ("processing_provider_model", "TEXT"),
                ("processing_pipeline_version", "TEXT"),
                ("processing_plan_json", "TEXT"),
                ("provider_mode", "TEXT NOT NULL DEFAULT 'stateless'"),
                ("provider_response_id", "TEXT"),
                ("provider_conversation_id", "TEXT"),
                ("provider_context_id", "TEXT"),
                ("context_parent_response_id", "TEXT"),
                ("context_depth", "INTEGER NOT NULL DEFAULT 0"),
                ("context_fallback_reason", "TEXT"),
                ("provider_http_status", "INTEGER"),
                ("provider_duration_ms", "INTEGER"),
                ("provider_parent_response_id", "TEXT"),
                ("provider_context_used", "INTEGER NOT NULL DEFAULT 0"),
                ("provider_context_fallback_reason", "TEXT"),
                ("input_version_id", "TEXT"),
                ("effective_prompt_hash", "TEXT"),
                ("scene_intent_hash", "TEXT"),
                ("provider_request_id", "TEXT"),
                ("provider_usage_json", "TEXT"),
            ):
                if name not in version_columns:
                    connection.execute(
                        f"ALTER TABLE gallery_versions ADD COLUMN {name} {declaration}"
                    )
            migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=2"
            ).fetchone()
            if migration is None:
                self._backfill_gallery(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(2,?,?)",
                    ("personal_ai_studio_gallery", datetime.now(timezone.utc).isoformat()),
                )
            connection.executescript(MAX_SCHEMA)
            connection.executescript(TELEMETRY_SCHEMA)
            connection.executescript(PAYMENT_SCHEMA)
            connection.executescript(PROVIDER_CONTEXT_SCHEMA)
            connection.executescript(COMMERCE_SCHEMA)
            connection.executescript(GROWTH_SCHEMA)
            account_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(user_credit_accounts)")
            }
            if "total_generation_credits_adjusted" not in account_columns:
                connection.execute(
                    """ALTER TABLE user_credit_accounts ADD COLUMN
                       total_generation_credits_adjusted INTEGER NOT NULL DEFAULT 0"""
                )
            payment_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(payment_intents)")
            }
            for name, declaration in (
                ("version_id", "TEXT"),
                ("user_id", "TEXT"),
                ("provider", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("currency", "TEXT NOT NULL DEFAULT 'RUB'"),
                ("updated_at", "TEXT"),
                ("expires_at", "TEXT"),
                ("product_code", "TEXT NOT NULL DEFAULT 'legacy_original_unlock'"),
            ):
                if name not in payment_columns:
                    connection.execute(
                        f"ALTER TABLE payment_intents ADD COLUMN {name} {declaration}"
                    )
            self._allow_repeat_payment_intents(connection)
            gallery_version_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(gallery_versions)")
            }
            for name, declaration in (
                ("payment_order_id", "TEXT"),
                ("unlocked_at", "TEXT"),
                ("delivery_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_delivered_at", "TEXT"),
                ("unlock_entitlement_id", "TEXT"),
            ):
                if name not in gallery_version_columns:
                    connection.execute(
                        f"ALTER TABLE gallery_versions ADD COLUMN {name} {declaration}"
                    )
            payment_order_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(payment_orders)")
            }
            for name, declaration in (
                ("product_code", "TEXT NOT NULL DEFAULT 'legacy_original_unlock'"),
                ("generation_credit_quantity", "INTEGER NOT NULL DEFAULT 0"),
                ("unlock_entitlement_quantity", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in payment_order_columns:
                    connection.execute(
                        f"ALTER TABLE payment_orders ADD COLUMN {name} {declaration}"
                    )
            telemetry_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(product_events)")
            }
            for name, declaration in (
                ("subject_hash", "TEXT"),
                ("value_integer", "INTEGER"),
                ("value_real", "REAL"),
            ):
                if name not in telemetry_columns:
                    connection.execute(
                        f"ALTER TABLE product_events ADD COLUMN {name} {declaration}"
                    )
            attempt_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(generation_attempts)")
            }
            if "credit_reservation_id" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE generation_attempts ADD COLUMN credit_reservation_id TEXT"
                )
            dialog_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(max_dialogs)")
            }
            if "pending_action" not in dialog_columns:
                connection.execute("ALTER TABLE max_dialogs ADD COLUMN pending_action TEXT")
            max_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=3"
            ).fetchone()
            if max_migration is None:
                now = datetime.now(timezone.utc).isoformat()
                for document_type in ("offer", "personal_data", "image_rights", "external_ai"):
                    connection.execute(
                        """INSERT OR IGNORE INTO max_legal_documents(
                               document_type,version,required,draft,active,created_at
                           ) VALUES(?,?,1,1,1,?)""",
                        (document_type, "2026-07-draft-1", now),
                    )
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(3,?,?)",
                    ("max_dialog_state_and_legal_versions", now),
                )
            brain_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=4"
            ).fetchone()
            if brain_migration is None:
                self._backfill_edit_plans(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(4,?,?)",
                    ("structured_edit_plan_and_feedback", datetime.now(timezone.utc).isoformat()),
                )
            processing_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=5"
            ).fetchone()
            if processing_migration is None:
                self._backfill_processing_plans(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(5,?,?)",
                    (
                        "hybrid_processing_modes_and_asset_lineage",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            telemetry_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=6"
            ).fetchone()
            if telemetry_migration is None:
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(6,?,?)",
                    (
                        "privacy_safe_product_events",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            context_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=7"
            ).fetchone()
            if context_migration is None:
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(7,?,?)",
                    (
                        "optional_openai_provider_context",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            payment_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=8"
            ).fetchone()
            if payment_migration is None:
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """UPDATE payment_intents
                       SET version_id=(SELECT id FROM gallery_versions
                                       WHERE gallery_versions.attempt_id=payment_intents.attempt_id),
                           user_id=(SELECT user_id FROM generation_attempts
                                    WHERE generation_attempts.id=payment_intents.attempt_id),
                           updated_at=COALESCE(updated_at,confirmed_at,created_at),
                           expires_at=COALESCE(expires_at,datetime(created_at,'+30 minutes'))
                       WHERE version_id IS NULL OR user_id IS NULL OR updated_at IS NULL
                          OR expires_at IS NULL"""
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(8,?,?)",
                    ("version_scoped_commercial_payments", now),
                )
            commerce_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=9"
            ).fetchone()
            if commerce_migration is None:
                from app.commerce import migrate_legacy_credit_accounts

                now = datetime.now(timezone.utc).isoformat()
                migrate_legacy_credit_accounts(connection, now)
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(9,?,?)",
                    ("continuation_pack_credit_and_entitlement_ledgers", now),
                )
            growth_migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=10"
            ).fetchone()
            if growth_migration is None:
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(10,?,?)",
                    (
                        "single_screen_ui_referrals_and_attribution",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        finally:
            connection.close()

    def recover_interrupted_runtime(self) -> dict[str, int]:
        """Recover crash state only from the primary runtime after it owns the lock."""

        with self.transaction() as connection:
            completed_events = connection.execute(
                """UPDATE max_processed_events SET status='completed',updated_at=datetime('now')
                   WHERE status='processing' AND event_key IN (
                       SELECT transition.event_key
                       FROM max_dialog_transitions AS transition
                       JOIN max_dialogs AS dialog
                         ON dialog.platform_user_id=transition.platform_user_id
                       WHERE dialog.state='processing'
                         AND transition.to_state='processing'
                         AND transition.event_key IS NOT NULL
                   )"""
            ).rowcount
            retryable_events = connection.execute(
                """UPDATE max_processed_events SET status='failed',updated_at=datetime('now')
                   WHERE status='processing'"""
            ).rowcount
            interrupted_attempts = connection.execute(
                """UPDATE generation_attempts
                   SET status='failed_technical', completed_at=datetime('now'),
                       error_type='process_restarted',
                       error_message_safe='Generation interrupted by process restart',
                       technical_refund=1
                   WHERE status IN ('pending', 'processing')"""
            ).rowcount
            from app.commerce import CommerceService

            recovery_now = datetime.now(timezone.utc).isoformat()
            released_credit_reservations = CommerceService.recover_stale_reservations(
                connection, recovery_now
            )
            released_unlock_reservations = CommerceService.recover_stale_unlock_deliveries(
                connection, recovery_now
            )
        return {
            "completed_events": completed_events,
            "retryable_events": retryable_events,
            "interrupted_attempts": interrupted_attempts,
            "released_credit_reservations": released_credit_reservations,
            "released_unlock_reservations": released_unlock_reservations,
        }

    @staticmethod
    def _allow_repeat_payment_intents(connection: sqlite3.Connection) -> None:
        """Remove the legacy one-payment-per-generation constraint safely."""

        unique_attempt_index = False
        for index in connection.execute("PRAGMA index_list(payment_intents)").fetchall():
            if not index[2]:
                continue
            columns = connection.execute(
                f"PRAGMA index_info('{index[1]}')"
            ).fetchall()
            if [row[2] for row in columns] == ["attempt_id"]:
                unique_attempt_index = True
                break
        if not unique_attempt_index:
            return
        connection.executescript(
            """
            PRAGMA foreign_keys=OFF;
            PRAGMA legacy_alter_table=ON;
            BEGIN IMMEDIATE;
            ALTER TABLE payment_intents RENAME TO payment_intents_legacy_unique_attempt;
            CREATE TABLE payment_intents (
                id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL REFERENCES generation_attempts(id),
                idempotency_key TEXT NOT NULL UNIQUE,
                amount_rub INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                version_id TEXT,
                user_id TEXT,
                provider TEXT NOT NULL DEFAULT 'legacy',
                currency TEXT NOT NULL DEFAULT 'RUB',
                updated_at TEXT,
                expires_at TEXT,
                product_code TEXT NOT NULL DEFAULT 'legacy_original_unlock'
            );
            INSERT INTO payment_intents(
                id,attempt_id,idempotency_key,amount_rub,status,created_at,confirmed_at,
                version_id,user_id,provider,currency,updated_at,expires_at,product_code
            )
            SELECT id,attempt_id,idempotency_key,amount_rub,status,created_at,confirmed_at,
                   version_id,user_id,provider,currency,updated_at,expires_at,product_code
            FROM payment_intents_legacy_unique_attempt;
            DROP TABLE payment_intents_legacy_unique_attempt;
            COMMIT;
            PRAGMA legacy_alter_table=OFF;
            PRAGMA foreign_keys=ON;
            """
        )

    @staticmethod
    def _backfill_gallery(connection: sqlite3.Connection) -> None:
        sessions = connection.execute(
            "SELECT * FROM demo_sessions WHERE gallery_item_id IS NULL"
        ).fetchall()
        for session in sessions:
            gallery_id = uuid5(
                NAMESPACE_URL, f"photo-bot:user-gallery:{session['user_id']}"
            ).hex
            connection.execute(
                """INSERT OR IGNORE INTO galleries(id,user_id,created_at,updated_at)
                   VALUES(?,?,?,?)""",
                (gallery_id, session["user_id"], session["created_at"], session["updated_at"]),
            )
            item_id = uuid5(NAMESPACE_URL, f"photo-bot:gallery:{session['id']}").hex
            root = str(Path(session["source_file_path"]).parent.parent)
            retention = (
                datetime.fromisoformat(session["started_at"])
                + timedelta(days=180 if session["converted_to_paid"] else 30)
            ).isoformat()
            attempts = connection.execute(
                """SELECT * FROM generation_attempts
                   WHERE session_id=? AND status='succeeded'
                   ORDER BY completed_at, created_at""",
                (session["id"],),
            ).fetchall()
            unlock_status = "unlocked" if any(row["result_unlocked"] for row in attempts) else "demo"
            connection.execute(
                """INSERT OR IGNORE INTO gallery_items(
                       id,gallery_id,user_id,title,created_at,updated_at,scenario_id,original_source_path,
                       storage_root_path,generation_count,last_opened_at,unlock_status,retention_until
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item_id,
                    gallery_id,
                    session["user_id"],
                    "Моя работа",
                    session["created_at"],
                    session["updated_at"],
                    attempts[-1]["scenario_id"] if attempts else None,
                    session["source_file_path"],
                    root,
                    len(attempts),
                    session["updated_at"],
                    unlock_status,
                    retention,
                ),
            )
            last_version_id = None
            for number, attempt in enumerate(attempts, start=1):
                version_id = uuid5(
                    NAMESPACE_URL, f"photo-bot:gallery-version:{attempt['id']}"
                ).hex
                effective_prompt = attempt["effective_prompt"] or attempt["prompt"]
                version_unlock = "unlocked" if attempt["result_unlocked"] else "demo"
                connection.execute(
                    """INSERT OR IGNORE INTO gallery_versions(
                           id,gallery_item_id,attempt_id,version_number,parent_version_id,
                           source_path,prompt,correction_prompt,effective_prompt,provider,model,
                           preview_watermarked_path,original_path,created_at,processing_time_ms,
                           estimated_cost,status,unlock_status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        version_id,
                        item_id,
                        attempt["id"],
                        number,
                        attempt["parent_version_id"],
                        attempt["source_path"],
                        attempt["prompt"],
                        attempt["correction_prompt"],
                        effective_prompt,
                        attempt["provider"],
                        attempt["model"],
                        attempt["demo_result_path"],
                        attempt["original_result_path"],
                        attempt["completed_at"] or attempt["created_at"],
                        attempt["duration_ms"],
                        attempt["estimated_cost"],
                        attempt["status"],
                        version_unlock,
                    ),
                )
                last_version_id = version_id
            if last_version_id:
                last = attempts[-1]
                connection.execute(
                    """UPDATE gallery_items SET current_best_version_id=?,cover_preview_path=?,
                       preview_small_path=?,preview_large_path=? WHERE id=?""",
                    (
                        last_version_id,
                        last["demo_result_path"],
                        last["demo_result_path"],
                        last["demo_result_path"],
                        item_id,
                    ),
                )
            connection.execute(
                "UPDATE demo_sessions SET gallery_item_id=? WHERE id=?",
                (item_id, session["id"]),
            )

    @staticmethod
    def _backfill_edit_plans(connection: sqlite3.Connection) -> None:
        """Make legacy rows readable without changing their historical behavior."""

        from app.edit_intent import EditPlan

        attempts = connection.execute(
            "SELECT * FROM generation_attempts WHERE edit_plan_json IS NULL"
        ).fetchall()
        for row in attempts:
            plan = EditPlan.from_legacy(
                row["prompt"],
                correction=bool(row["correction"]),
                correction_target_version_id=row["parent_version_id"],
            )
            connection.execute(
                """UPDATE generation_attempts
                   SET edit_plan_json=?,provider_prompt=COALESCE(provider_prompt,effective_prompt,prompt),
                       prompt_builder_version=COALESCE(prompt_builder_version,'legacy-concatenation')
                   WHERE id=?""",
                (plan.to_json(), row["id"]),
            )

        versions = connection.execute(
            "SELECT * FROM gallery_versions WHERE edit_plan_json IS NULL"
        ).fetchall()
        for row in versions:
            plan = EditPlan.from_legacy(
                row["correction_prompt"] or row["prompt"],
                correction=bool(row["correction_prompt"]),
                correction_target_version_id=row["parent_version_id"],
            )
            connection.execute(
                """UPDATE gallery_versions
                   SET edit_plan_json=?,provider_prompt=COALESCE(provider_prompt,effective_prompt,prompt),
                       prompt_builder_version=COALESCE(prompt_builder_version,'legacy-concatenation')
                   WHERE id=?""",
                (plan.to_json(), row["id"]),
            )

    @staticmethod
    def _backfill_processing_plans(connection: sqlite3.Connection) -> None:
        """Make historical versions explicit without changing their execution history."""

        from app.processing_modes import legacy_processing_plan

        attempts = connection.execute(
            "SELECT * FROM generation_attempts WHERE processing_plan_json IS NULL"
        ).fetchall()
        for row in attempts:
            plan = legacy_processing_plan(row["provider"], row["model"])
            connection.execute(
                """UPDATE generation_attempts SET selected_mode=?,mode_reason=?,mode_confidence=?,
                          fallback_mode=?,asset_source_type=?,asset_id=?,asset_checksum=?,mask_strategy=?,
                          processing_provider=?,processing_provider_model=?,
                          processing_pipeline_version=?,processing_plan_json=? WHERE id=?""",
                (
                    plan.selected_mode.value,
                    plan.mode_reason,
                    plan.confidence,
                    None,
                    plan.asset_source_type.value,
                    None,
                    None,
                    plan.mask_strategy.value,
                    plan.provider,
                    plan.provider_model,
                    plan.processing_pipeline_version,
                    plan.to_json(),
                    row["id"],
                ),
            )

        versions = connection.execute(
            "SELECT * FROM gallery_versions WHERE processing_plan_json IS NULL"
        ).fetchall()
        for row in versions:
            plan = legacy_processing_plan(row["provider"], row["model"])
            connection.execute(
                """UPDATE gallery_versions SET selected_mode=?,mode_reason=?,mode_confidence=?,
                          fallback_mode=?,asset_source_type=?,asset_id=?,asset_checksum=?,mask_strategy=?,
                          processing_provider=?,processing_provider_model=?,
                          processing_pipeline_version=?,processing_plan_json=? WHERE id=?""",
                (
                    plan.selected_mode.value,
                    plan.mode_reason,
                    plan.confidence,
                    None,
                    plan.asset_source_type.value,
                    None,
                    None,
                    plan.mask_strategy.value,
                    plan.provider,
                    plan.provider_model,
                    plan.processing_pipeline_version,
                    plan.to_json(),
                    row["id"],
                ),
            )

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
