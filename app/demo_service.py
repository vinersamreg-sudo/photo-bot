"""Transactional free-demo policy and image workflow."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

from app.config import Settings
from app.database import Database
from app.domain import (
    ConcurrentGenerationError,
    CooldownError,
    DailyBudgetError,
    DeliveryError,
    DemoExpiredError,
    DemoGenerationResult,
    DemoLimitError,
    DemoSessionInfo,
    InvalidInputError,
    PaymentRequiredError,
    PolicyRejectedError,
    SourceReplacementError,
)
from app.image_provider import ImageProvider
from app.scenarios import get_scenario
from app.storage import PrivateStorage
from app.watermark import WatermarkService


UTC = timezone.utc
DeliverPreview = Callable[[Path, str], bool]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class DemoService:
    _semaphores: dict[int, threading.BoundedSemaphore] = {}
    _semaphore_guard = threading.Lock()

    def __init__(
        self,
        settings: Settings,
        database: Database,
        storage: PrivateStorage,
        watermarker: WatermarkService,
        provider: ImageProvider,
        deliver_preview: Optional[DeliverPreview] = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self.database = database
        self.storage = storage
        self.watermarker = watermarker
        self.provider = provider
        self.deliver_preview = deliver_preview or (lambda _path, _attempt: True)
        self.clock = clock
        with self._semaphore_guard:
            self._global_semaphore = self._semaphores.setdefault(
                settings.global_max_concurrent_generations,
                threading.BoundedSemaphore(settings.global_max_concurrent_generations),
            )

    def start_session(self, platform: str, platform_user_id: str, source: Path) -> DemoSessionInfo:
        if not platform.strip() or not platform_user_id.strip():
            raise InvalidInputError("Platform identity is required")
        digest, _extension, _size = self.storage.validate_source(source)
        now = self.clock()
        with self.database.transaction() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE platform=? AND platform_user_id=?",
                (platform, platform_user_id),
            ).fetchone()
            if user is None:
                user_id = uuid4().hex
                connection.execute(
                    "INSERT INTO users(id, platform, platform_user_id, created_at) VALUES(?,?,?,?)",
                    (user_id, platform, platform_user_id, iso(now)),
                )
            else:
                user_id = user["id"]
                existing = connection.execute(
                    "SELECT * FROM demo_sessions WHERE user_id=?", (user_id,)
                ).fetchone()
                if existing:
                    if existing["source_sha256"] != digest:
                        connection.execute(
                            "UPDATE users SET risk_score=risk_score+1 WHERE id=?", (user_id,)
                        )
                        connection.commit()
                        raise SourceReplacementError(
                            "Бесплатная демонстрация действует для одной исходной фотографии. "
                            "Для обработки нового фото потребуется платная операция или новый пакет"
                        )
                    return self._session_info(existing)
                if user["demo_used"]:
                    raise DemoLimitError("The free demo has already been used")

            session_id = uuid4().hex
            stored_source, stored_digest, _stored_size = self.storage.create_session(
                user_id, session_id, source
            )
            expires = now + timedelta(minutes=self.settings.demo_session_ttl_minutes)
            connection.execute(
                """INSERT INTO demo_sessions(
                       id,user_id,source_file_path,source_sha256,status,started_at,expires_at,
                       max_generations,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id,
                    user_id,
                    str(stored_source),
                    stored_digest,
                    "active",
                    iso(now),
                    iso(expires),
                    self.settings.demo_max_successful_generations,
                    iso(now),
                    iso(now),
                ),
            )
            connection.execute("UPDATE users SET demo_used=1 WHERE id=?", (user_id,))
            self.storage.write_metadata(
                user_id,
                session_id,
                {"session_id": session_id, "created_at": iso(now), "source_sha256": stored_digest},
            )
            row = connection.execute("SELECT * FROM demo_sessions WHERE id=?", (session_id,)).fetchone()
            return self._session_info(row)

    @staticmethod
    def _session_info(row: sqlite3.Row) -> DemoSessionInfo:
        return DemoSessionInfo(
            session_id=row["id"],
            user_id=row["user_id"],
            source_path=Path(row["source_file_path"]),
            expires_at=row["expires_at"],
            successful_generations=row["successful_generations"],
            max_generations=row["max_generations"],
        )

    def generate(
        self,
        session_id: str,
        prompt: str,
        idempotency_key: str,
        scenario_id: Optional[str] = None,
        correction: bool = False,
        delivery_override: Optional[DeliverPreview] = None,
    ) -> DemoGenerationResult:
        clean_prompt = prompt.strip()
        if not clean_prompt or len(clean_prompt) > self.settings.max_prompt_length:
            raise InvalidInputError("Prompt is empty or exceeds the configured limit")
        scenario = get_scenario(scenario_id)
        if scenario_id and scenario is None:
            raise InvalidInputError("Unknown or inactive scenario")
        provider_prompt = (
            scenario.prompt_template.format(instruction=clean_prompt) if scenario else clean_prompt
        )
        now = self.clock()
        attempt_id = uuid4().hex

        with self.database.transaction() as connection:
            replay = connection.execute(
                "SELECT * FROM generation_attempts WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if replay:
                if replay["status"] == "succeeded":
                    session = connection.execute(
                        "SELECT * FROM demo_sessions WHERE id=?", (replay["session_id"],)
                    ).fetchone()
                    return DemoGenerationResult(
                        replay["id"],
                        Path(replay["demo_result_path"]),
                        session["max_generations"] - session["successful_generations"],
                        True,
                    )
                raise ConcurrentGenerationError("The same event is already being or was processed")

            session = connection.execute(
                "SELECT * FROM demo_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not session or session["status"] != "active":
                raise DemoLimitError("Demo session is not active")
            if now >= datetime.fromisoformat(session["expires_at"]):
                connection.execute(
                    "UPDATE demo_sessions SET status='expired', updated_at=? WHERE id=?",
                    (iso(now), session_id),
                )
                raise DemoExpiredError("Demo session has expired")
            if session["successful_generations"] >= session["max_generations"]:
                raise DemoLimitError("Demo successful-generation limit is exhausted")
            user = connection.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
            if user["blocked_until"] and now < datetime.fromisoformat(user["blocked_until"]):
                raise CooldownError("User is temporarily blocked")
            active = connection.execute(
                "SELECT COUNT(*) FROM generation_attempts WHERE user_id=? AND status IN ('pending','processing')",
                (session["user_id"],),
            ).fetchone()[0]
            if active >= self.settings.demo_max_concurrent_per_user:
                raise ConcurrentGenerationError("Another generation is already active")

            hour_start = iso(now - timedelta(hours=1))
            recent = connection.execute(
                "SELECT COUNT(*), MAX(started_at) FROM generation_attempts WHERE user_id=? AND started_at>=?",
                (session["user_id"], hour_start),
            ).fetchone()
            if recent[0] >= self.settings.demo_max_attempts_per_hour:
                connection.execute(
                    "UPDATE users SET risk_score=risk_score+1, blocked_until=? WHERE id=?",
                    (iso(now + timedelta(minutes=15)), session["user_id"]),
                )
                connection.commit()
                raise CooldownError("Too many attempts; temporary cooldown applied")
            if recent[1]:
                seconds = (now - datetime.fromisoformat(recent[1])).total_seconds()
                if seconds < self.settings.demo_min_request_interval_seconds:
                    raise CooldownError("Please wait before the next generation")

            day_start = iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
            daily = connection.execute(
                """SELECT COUNT(*), COALESCE(SUM(estimated_cost),0)
                   FROM generation_attempts WHERE status='succeeded' AND completed_at>=?""",
                (day_start,),
            ).fetchone()
            projected = float(daily[1]) + self.settings.demo_estimated_cost_rub_per_generation
            if daily[0] >= self.settings.demo_daily_generation_limit or projected > self.settings.demo_daily_cost_limit_rub:
                raise DailyBudgetError(
                    "Сегодня бесплатные демонстрации временно закончились. "
                    "Можно вернуться позже или получить результат через платную операцию"
                )

            connection.execute(
                """INSERT INTO generation_attempts(
                       id,idempotency_key,session_id,user_id,prompt,scenario_id,status,started_at,
                       provider,model,source_path,input_size_bytes,requested_size,
                       requested_quality,output_format,correction,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    idempotency_key,
                    session_id,
                    session["user_id"],
                    clean_prompt,
                    scenario_id,
                    "processing",
                    iso(now),
                    self.provider.name,
                    self.provider.model,
                    session["source_file_path"],
                    Path(session["source_file_path"]).stat().st_size,
                    getattr(self.provider, "size", None),
                    getattr(self.provider, "quality", None),
                    getattr(self.provider, "output_format", None),
                    int(correction),
                    iso(now),
                ),
            )
            user_id = session["user_id"]
            source_path = Path(session["source_file_path"])

        started = time.monotonic()
        original_path = self.storage.original_path(user_id, session_id, attempt_id)
        preview_path = self.storage.preview_path(
            user_id, session_id, attempt_id, self.watermarker.extension
        )
        try:
            with self._global_semaphore:
                provider_result = self.provider.edit(source_path, provider_prompt)
            self.storage.write_private(original_path, provider_result.image_bytes)
            self.watermarker.create_preview(original_path, preview_path, attempt_id)
            delivery = delivery_override or self.deliver_preview
            if not delivery(preview_path, attempt_id):
                raise DeliveryError("Demo preview delivery failed")
        except PolicyRejectedError as exc:
            self._fail_attempt(attempt_id, "rejected_policy", "policy_rejected", str(exc), False)
            raise
        except DeliveryError as exc:
            self._fail_attempt(attempt_id, "delivery_failed", "delivery_failed", str(exc), True)
            raise
        except Exception as exc:
            self._fail_attempt(attempt_id, "failed_technical", type(exc).__name__, "Technical generation failure", True)
            raise

        completed = self.clock()
        duration_ms = int((time.monotonic() - started) * 1000)
        estimated_cost = (
            provider_result.estimated_cost_rub
            if provider_result.estimated_cost_rub is not None
            else self.settings.demo_estimated_cost_rub_per_generation
        )
        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE generation_attempts SET
                       status='succeeded', completed_at=?, original_result_path=?, demo_result_path=?,
                       estimated_cost=?, external_request_id=?, duration_ms=?, output_size_bytes=?,
                       retries=?, usage_json=?
                   WHERE id=? AND status='processing'""",
                (
                    iso(completed),
                    str(original_path),
                    str(preview_path),
                    estimated_cost,
                    provider_result.request_id,
                    duration_ms,
                    len(provider_result.image_bytes),
                    provider_result.retries,
                    json.dumps(provider_result.usage, ensure_ascii=False),
                    attempt_id,
                ),
            ).rowcount
            if updated != 1:
                raise ConcurrentGenerationError("Attempt was already finalized")
            connection.execute(
                """UPDATE demo_sessions
                   SET successful_generations=successful_generations+1, updated_at=?
                   WHERE id=?""",
                (iso(completed), session_id),
            )
            session = connection.execute(
                "SELECT successful_generations,max_generations FROM demo_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            remaining = session["max_generations"] - session["successful_generations"]
            if remaining == 0:
                connection.execute(
                    "UPDATE demo_sessions SET status='completed', completed_at=?, updated_at=? WHERE id=?",
                    (iso(completed), iso(completed), session_id),
                )
        self.storage.append_attempt_metadata(
            user_id,
            session_id,
            {
                "attempt_id": attempt_id,
                "status": "succeeded",
                "completed_at": iso(completed),
                "provider": self.provider.name,
                "model": self.provider.model,
                "requested_size": getattr(self.provider, "size", None),
                "requested_quality": getattr(self.provider, "quality", None),
                "estimated_cost_rub": estimated_cost,
                "original_file": original_path.name,
                "preview_file": preview_path.name,
            },
        )
        return DemoGenerationResult(attempt_id, preview_path, remaining)

    def _fail_attempt(self, attempt_id: str, status: str, error_type: str, message: str, refund: bool) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE generation_attempts SET status=?, completed_at=?, error_type=?,
                   error_message_safe=?, technical_refund=? WHERE id=? AND status='processing'""",
                (status, iso(self.clock()), error_type, message[:300], int(refund), attempt_id),
            )

    def delete_session(self, session_id: str) -> None:
        with self.database.transaction() as connection:
            session = connection.execute(
                "SELECT * FROM demo_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not session:
                return
            self.storage.delete_session(session["user_id"], session_id)
            connection.execute(
                """UPDATE generation_attempts SET source_path='', original_result_path=NULL,
                   demo_result_path=NULL WHERE session_id=?""",
                (session_id,),
            )
            connection.execute(
                "UPDATE demo_sessions SET status='deleted', source_file_path='', updated_at=? WHERE id=?",
                (iso(self.clock()), session_id),
            )

    def unlock_original(self, attempt_id: str, idempotency_key: str) -> str:
        now = iso(self.clock())
        with self.database.transaction() as connection:
            attempt = connection.execute(
                "SELECT * FROM generation_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not attempt or attempt["status"] != "succeeded":
                raise PaymentRequiredError("Only a successful result can be unlocked")
            existing = connection.execute(
                "SELECT * FROM payment_intents WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if existing:
                return existing["id"]
            intent_id = uuid4().hex
            connection.execute(
                """INSERT INTO payment_intents(id,attempt_id,idempotency_key,amount_rub,status,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (intent_id, attempt_id, idempotency_key, self.settings.unlock_original_price_rub, "pending", now),
            )
            return intent_id

    def confirm_payment(self, payment_intent_id: str) -> None:
        """Idempotently unlock after a future payment adapter verifies paid status."""
        now = iso(self.clock())
        with self.database.transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM payment_intents WHERE id=?", (payment_intent_id,)
            ).fetchone()
            if not intent:
                raise PaymentRequiredError("Unknown payment intent")
            if intent["status"] == "paid":
                return
            if intent["status"] != "pending":
                raise PaymentRequiredError("Payment intent cannot be confirmed")
            connection.execute(
                "UPDATE payment_intents SET status='paid', confirmed_at=? WHERE id=? AND status='pending'",
                (now, payment_intent_id),
            )
            connection.execute(
                "UPDATE generation_attempts SET result_unlocked=1 WHERE id=?",
                (intent["attempt_id"],),
            )
            connection.execute(
                """UPDATE demo_sessions SET converted_to_paid=1, updated_at=? WHERE id=(
                       SELECT session_id FROM generation_attempts WHERE id=?
                   )""",
                (now, intent["attempt_id"]),
            )

    def original_for_paid_intent(self, payment_intent_id: str) -> Path:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT p.status, a.original_result_path
                   FROM payment_intents p JOIN generation_attempts a ON a.id=p.attempt_id
                   WHERE p.id=?""",
                (payment_intent_id,),
            ).fetchone()
        if not row or row["status"] != "paid":
            raise PaymentRequiredError("Original is unavailable until payment is confirmed")
        return Path(row["original_result_path"])
