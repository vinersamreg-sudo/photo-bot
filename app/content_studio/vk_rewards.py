"""Prepared VK ID/subscription bridge, OFF and not wired into photo-bot.

An authenticated MAX backend must issue the signed handoff. No public profile
link or client-supplied MAX identity is accepted. Tokens are never persisted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

REASON = "vk_subscription_verified"


class VkRewardError(RuntimeError):
    """Only static, privacy-safe error messages cross the bridge boundary."""


@dataclass(frozen=True)
class VkRewardSettings:
    database_path: Path
    enabled: bool = False
    group_id: str = ""
    client_id: str = ""
    redirect_uri: str = ""
    handoff_key: str = field(default="", repr=False)
    membership_token: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls, base_dir: Path, values) -> "VkRewardSettings":
        raw = values.get("VK_SUBSCRIPTION_REWARD_ENABLED", "false").strip().lower()
        if raw not in {"true", "false"}:
            raise ValueError("VK_SUBSCRIPTION_REWARD_ENABLED must be true or false")
        settings = cls(base_dir / "vk_rewards.sqlite3", raw == "true",
            values.get("CONTENT_STUDIO_VK_GROUP_ID", "").strip(),
            values.get("VK_ID_CLIENT_ID", "").strip(), values.get("VK_ID_REDIRECT_URI", "").strip(),
            values.get("VK_MAX_HANDOFF_KEY", "").strip(), values.get("VK_MEMBERSHIP_ACCESS_TOKEN", "").strip())
        if settings.enabled:
            settings.validate()
        return settings

    def validate(self):
        parsed = urlsplit(self.redirect_uri)
        if (self.database_path.name != "vk_rewards.sqlite3"
                or not self.group_id.isdigit() or int(self.group_id) <= 0 or not self.client_id.isdigit()
                or len(self.handoff_key) < 32 or not self.membership_token
                or parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.query or parsed.fragment):
            raise VkRewardError("VK reward configuration is incomplete or unsafe")


class VkIdentityApi:
    def __init__(self, settings: VkRewardSettings, *, client=None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)

    def _post(self, url, data):
        try:
            response = self.client.post(url, data=data)
            value = response.json()
            if response.status_code != 200 or not isinstance(value, dict) or value.get("error"):
                raise VkRewardError("VK verification API rejected the request")
            return value
        except (httpx.HTTPError, ValueError):
            raise VkRewardError("VK verification API unavailable") from None

    def verified_identity(self, *, code, state, device_id, verifier):
        token = self._post("https://id.vk.ru/oauth2/auth", {
            "grant_type": "authorization_code", "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri, "code": code,
            "code_verifier": verifier, "state": state, "device_id": device_id,
        })
        if token.get("state") != state or not isinstance(token.get("access_token"), str):
            raise VkRewardError("VK ID state verification failed")
        identity = self._post("https://id.vk.ru/oauth2/user_info", {
            "client_id": self.settings.client_id, "access_token": token["access_token"],
        }).get("user", {})
        user_id = str(identity.get("user_id", "")) if isinstance(identity, dict) else ""
        if not user_id.isdigit() or int(user_id) <= 0 or str(token.get("user_id")) != user_id:
            raise VkRewardError("VK ID identity verification failed")
        return user_id

    def is_member(self, user_id):
        value = self._post("https://api.vk.com/method/groups.isMember", {
            "group_id": self.settings.group_id, "user_id": user_id, "v": "5.199",
            "access_token": self.settings.membership_token,
        }).get("response")
        if type(value) is not int or value not in (0, 1):
            raise VkRewardError("VK membership response is not authoritative")
        return value == 1


class VkSubscriptionRewards:
    def __init__(self, settings: VkRewardSettings, *, identity_api, grant, clock=time.time):
        self.settings, self.api, self.grant, self.clock = settings, identity_api, grant, clock

    def _enabled(self):
        if not self.settings.enabled:
            raise VkRewardError("VK subscription reward is disabled")
        self.settings.validate()

    @contextmanager
    def _database(self):
        path = self.settings.database_path
        if path.is_symlink() or path.parent.is_symlink():
            raise VkRewardError("VK reward storage must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        conn = sqlite3.connect(path, timeout=15, isolation_level=None)
        path.chmod(0o600)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS vk_reward_sessions (
                state_hash TEXT PRIMARY KEY, handoff_nonce TEXT UNIQUE NOT NULL,
                user_id TEXT NOT NULL, verifier TEXT NOT NULL, expires_at REAL NOT NULL,
                claimed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS vk_subscription_rewards (
                user_id TEXT PRIMARY KEY, vk_subject TEXT UNIQUE NOT NULL,
                group_id TEXT NOT NULL, grant_key TEXT UNIQUE NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('verified','granted')),
                verified_at REAL NOT NULL, granted_at REAL, receipt_json TEXT);
            CREATE TABLE IF NOT EXISTS vk_reward_identity_key (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), fingerprint TEXT NOT NULL);
        """)
        try:
            conn.execute("BEGIN IMMEDIATE")
            # A changed HMAC key must not make an already rewarded VK identity
            # appear new. Rotation requires an explicit verified migration.
            fingerprint = hashlib.sha256(self.settings.handoff_key.encode()).hexdigest()
            previous = conn.execute("SELECT fingerprint FROM vk_reward_identity_key WHERE singleton=1").fetchone()
            if previous and not hmac.compare_digest(previous[0], fingerprint):
                raise VkRewardError("VK reward identity key changed; operator migration required")
            if not previous:
                if conn.execute("SELECT 1 FROM vk_subscription_rewards LIMIT 1").fetchone():
                    raise VkRewardError("Unbound VK reward identity receipts; operator review required")
                conn.execute("INSERT INTO vk_reward_identity_key VALUES(1,?)", (fingerprint,))
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _sign(self, data):
        return hmac.new(self.settings.handoff_key.encode(), data.encode(), hashlib.sha256).hexdigest()

    def issue_max_handoff(self, authenticated_user_id: str) -> dict:
        """Internal authenticated MAX adapter only; NEVER expose as public API."""
        self._enabled()
        body = {"user": authenticated_user_id, "expires": int(self.clock()) + 120,
                "nonce": secrets.token_urlsafe(24)}
        body["signature"] = self._sign(json.dumps(body, sort_keys=True))
        return body

    def begin(self, handoff: dict) -> str:
        self._enabled()
        body = {key: handoff.get(key) for key in ("user", "expires", "nonce")}
        valid_types = (isinstance(body["user"], str) and 0 < len(body["user"]) <= 200
            and type(body["expires"]) is int and isinstance(body["nonce"], str)
            and 20 <= len(body["nonce"]) <= 100)
        if (not valid_types or not self.clock() < body["expires"] <= self.clock() + 120
                or not hmac.compare_digest(self._sign(json.dumps(body, sort_keys=True)), str(handoff.get("signature", "")))):
            raise VkRewardError("Authenticated MAX handoff required")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        try:
            with self._database() as conn:
                conn.execute("INSERT INTO vk_reward_sessions VALUES(?,?,?,?,?,0)",
                    (self._sign(state), body["nonce"], body["user"], verifier, self.clock() + 600))
        except sqlite3.IntegrityError:
            raise VkRewardError("MAX handoff already used") from None
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return "https://id.vk.ru/authorize?" + urlencode({
            "response_type": "code", "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri, "scope": "vkid.personal_info",
            "state": state, "code_challenge": challenge, "code_challenge_method": "s256",
        })

    def complete(self, *, state: str, code: str, device_id: str) -> str:
        self._enabled()
        if any(not isinstance(v, str) or not 1 <= len(v) <= 4096 for v in (state, code, device_id)):
            raise VkRewardError("Invalid VK ID callback")
        with self._database() as conn:
            session = conn.execute("SELECT * FROM vk_reward_sessions WHERE state_hash=?", (self._sign(state),)).fetchone()
            if not session or session["claimed"] or session["expires_at"] <= self.clock():
                raise VkRewardError("Expired or replayed VK ID callback")
            conn.execute("UPDATE vk_reward_sessions SET claimed=1,verifier='' WHERE state_hash=?", (self._sign(state),))
        vk_user = self.api.verified_identity(code=code, state=state, device_id=device_id, verifier=session["verifier"])
        if not self.api.is_member(vk_user):
            return "NOT_SUBSCRIBED"
        user, vk_subject = session["user_id"], self._sign("vk:" + vk_user)
        with self._database() as conn:
            reward = conn.execute("SELECT * FROM vk_subscription_rewards WHERE user_id=? OR vk_subject=?", (user, vk_subject)).fetchone()
            if reward and reward["group_id"] != self.settings.group_id:
                raise VkRewardError("Reward community changed; operator review required")
            if reward and (reward["user_id"] != user or reward["vk_subject"] != vk_subject):
                return "ALREADY_CLAIMED"
            if reward and reward["state"] == "granted":
                return "ALREADY_GRANTED"
            key = reward["grant_key"] if reward else "vk-subscription:" + self._sign("max:" + user)
            if reward is None:
                conn.execute("INSERT INTO vk_subscription_rewards VALUES(?,?,?,?,?,?,NULL,NULL)",
                    (user, vk_subject, self.settings.group_id, key, "verified", self.clock()))
        # Durable verified outbox before grant. Reauthorization can retry this
        # exact key after a crash; the authoritative ledger is idempotent.
        receipt = self.grant(user_id=user, amount=1, reason=REASON, idempotency_key=key)
        with self._database() as conn:
            conn.execute("UPDATE vk_subscription_rewards SET state='granted',granted_at=?,receipt_json=? WHERE grant_key=?",
                (self.clock(), json.dumps(receipt, sort_keys=True), key))
        return "GRANTED"


class ExistingLedgerRewardGrant:
    """Explicit future integration adapter; never instantiated by Content Studio."""
    def __init__(self, database, commerce):
        self.database, self.commerce = database, commerce

    def __call__(self, *, user_id, amount, reason, idempotency_key):
        if amount != 1 or reason != REASON or not idempotency_key.startswith("vk-subscription:"):
            raise VkRewardError("Unexpected reward grant contract")
        with self.database.transaction() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id=? AND platform='max'", (user_id,)).fetchone():
                raise VkRewardError("Authenticated MAX account does not exist")
            previous = conn.execute("SELECT id,consumed_credits FROM generation_credit_lots WHERE user_id=?", (user_id,)).fetchall()
            before = {row["id"] for row in previous}
            self.commerce.adjust_generation_credits(conn, user_id=user_id, delta=1, reason=reason, idempotency_key=idempotency_key)
            lots = conn.execute("SELECT id FROM generation_credit_lots WHERE user_id=? AND source_type='admin'", (user_id,)).fetchall()
            created = [row["id"] for row in lots if row["id"] not in before]
            return {"ledger_key": idempotency_key, "credit_lot": created[0] if len(created) == 1 else None}
