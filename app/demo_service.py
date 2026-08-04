"""Transactional free-demo policy and image workflow."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

from app.config import Settings
from app.commerce import CommerceService
from app.database import Database
from app.direct_prompt import (
    DIRECT_PROMPT_VERSION,
    build_direct_edit_plan,
    build_direct_prompt,
)
from app.domain import (
    AssetUnavailableError,
    ConcurrentGenerationError,
    CooldownError,
    DailyBudgetError,
    DeliveryError,
    DemoExpiredError,
    DemoGenerationResult,
    DemoLimitError,
    DemoSessionInfo,
    InvalidInputError,
    IntentAmbiguityError,
    PaymentRequiredError,
    PolicyRejectedError,
    ProviderUnavailableError,
    SourceReplacementError,
    StorageFailureError,
)
from app.edit_intent import EditPlan, merge_edit_plans, parse_edit_intent, repeat_edit_plan
from app.image_provider import ImageProvider
from app.gallery import GalleryService
from app.prompt_builder import PROMPT_BUILDER_VERSION, build_provider_prompt
from app.processing_modes import ProcessingMode, ProcessingPlan, legacy_processing_plan
from app.processing_pipeline import ProcessingExecutor
from app.processing_router import ModeRouter
from app.provider_context import ContextPlan, ProviderContextService
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
        processing_router: Optional[ModeRouter] = None,
        processing_executor: Optional[ProcessingExecutor] = None,
        provider_context_service: Optional[ProviderContextService] = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.storage = storage
        self.watermarker = watermarker
        self.provider = provider
        self.commerce = CommerceService(
            database, clock, paid_retention_days=settings.paid_retention_days
        )
        self.provider_context_service = provider_context_service
        self.gallery = GalleryService(
            database,
            storage,
            settings,
            clock,
            provider_context_service=provider_context_service,
        )
        self.deliver_preview = deliver_preview or (lambda _path, _attempt: True)
        self.clock = clock
        self.processing_router = processing_router
        self.processing_executor = processing_executor
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
            self.commerce.ensure_initial_grant(connection, user_id)
            existing = connection.execute(
                "SELECT * FROM demo_sessions WHERE user_id=?", (user_id,)
            ).fetchone()
            if existing:
                stored_source, stored_digest, _stored_size = self.storage.create_session(
                    user_id, existing["id"], source
                )
                current_item = connection.execute(
                    "SELECT * FROM gallery_items WHERE id=?",
                    (existing["gallery_item_id"],),
                ).fetchone()
                if (
                    existing["source_sha256"] != digest
                    or current_item is None
                    or current_item["deleted"]
                ):
                    self.gallery.create_linked_demo_item(
                        connection, user_id, existing["id"], stored_source, now
                    )
                expires = now + timedelta(minutes=self.settings.demo_session_ttl_minutes)
                connection.execute(
                    """UPDATE demo_sessions
                       SET source_file_path=?,source_sha256=?,status='active',expires_at=?,
                           completed_at=NULL,updated_at=? WHERE id=?""",
                    (
                        str(stored_source),
                        stored_digest,
                        iso(expires),
                        iso(now),
                        existing["id"],
                    ),
                )
                refreshed = connection.execute(
                    "SELECT * FROM demo_sessions WHERE id=?", (existing["id"],)
                ).fetchone()
                return self._session_info(refreshed)

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
            self.gallery.create_linked_demo_item(
                connection, user_id, session_id, stored_source, now
            )
            connection.execute("UPDATE users SET demo_used=1 WHERE id=?", (user_id,))
            self.storage.write_metadata(
                user_id,
                session_id,
                {"session_id": session_id, "created_at": iso(now), "source_sha256": stored_digest},
            )
            row = connection.execute("SELECT * FROM demo_sessions WHERE id=?", (session_id,)).fetchone()
            return self._session_info(row)

    def resume_session(
        self, platform: str, platform_user_id: str
    ) -> Optional[DemoSessionInfo]:
        """Resume the stored demo source without replacing it or granting new quota."""

        if not platform.strip() or not platform_user_id.strip():
            raise InvalidInputError("Platform identity is required")
        now = self.clock()
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT s.* FROM demo_sessions s
                   JOIN users u ON u.id=s.user_id
                   WHERE u.platform=? AND u.platform_user_id=?""",
                (platform, platform_user_id),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "deleted":
                return None
            item = connection.execute(
                "SELECT deleted FROM gallery_items WHERE id=?", (row["gallery_item_id"],)
            ).fetchone()
            if item is None or item["deleted"]:
                return None
            source_path = Path(row["source_file_path"])
            if not source_path.is_file():
                return None

            expires = now + timedelta(minutes=self.settings.demo_session_ttl_minutes)
            connection.execute(
                """UPDATE demo_sessions
                   SET status='active',expires_at=?,completed_at=NULL,updated_at=?
                   WHERE id=?""",
                (iso(expires), iso(now), row["id"]),
            )
            refreshed = connection.execute(
                "SELECT * FROM demo_sessions WHERE id=?", (row["id"],)
            ).fetchone()
            return self._session_info(refreshed)

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
        repeat: bool = False,
        delivery_override: Optional[DeliverPreview] = None,
        parent_version_id: Optional[str] = None,
    ) -> DemoGenerationResult:
        clean_prompt = prompt.strip()
        if not clean_prompt or len(clean_prompt) > self.settings.max_prompt_length:
            raise InvalidInputError("Prompt is empty or exceeds the configured limit")
        confirmed_balance_is_critical = bool(
            self.settings.image_provider == "openai"
            and self.settings.openai_balance_usd is not None
            and self.settings.openai_balance_usd
            <= self.settings.openai_balance_critical_usd
        )
        if (
            not self.settings.openai_image_requests_enabled
            or confirmed_balance_is_critical
        ):
            raise ProviderUnavailableError(
                "Image processing is paused by the operational budget guard"
            )
        scenario = get_scenario(scenario_id)
        if scenario_id and scenario is None:
            raise InvalidInputError("Unknown or inactive scenario")
        direct_prompt_active = self.settings.image_direct_prompt_enabled
        now = self.clock()
        attempt_id = uuid4().hex
        context_plan = ContextPlan("stateless", None, "context_service_unavailable")

        with self.database.transaction() as connection:
            replay = connection.execute(
                "SELECT * FROM generation_attempts WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if replay:
                if replay["status"] == "succeeded":
                    balance = self.commerce.ensure_initial_grant(
                        connection, replay["user_id"]
                    )
                    return DemoGenerationResult(
                        replay["id"],
                        Path(replay["demo_result_path"]),
                        balance.available,
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
            parent = None
            parent_processing_plan: Optional[ProcessingPlan] = None
            if parent_version_id:
                parent = connection.execute(
                    """SELECT * FROM gallery_versions
                       WHERE id=? AND gallery_item_id=? AND status='succeeded'""",
                    (parent_version_id, session["gallery_item_id"]),
                ).fetchone()
                if parent is None:
                    raise InvalidInputError(
                        "The selected parent version is missing or was not successful"
                    )
                parent_processing_plan = (
                    ProcessingPlan.from_json(parent["processing_plan_json"])
                    if parent["processing_plan_json"]
                    else legacy_processing_plan(parent["provider"], parent["model"])
                )
            elif correction or repeat:
                parent = connection.execute(
                    """SELECT * FROM gallery_versions
                       WHERE gallery_item_id=? AND status='succeeded'
                       ORDER BY version_number DESC LIMIT 1""",
                    (session["gallery_item_id"],),
                ).fetchone()
                if parent is None:
                    raise InvalidInputError("Correction or repeat requires a successful version")
                parent_version_id = parent["id"]
                parent_processing_plan = (
                    ProcessingPlan.from_json(parent["processing_plan_json"])
                    if parent["processing_plan_json"]
                    else legacy_processing_plan(parent["provider"], parent["model"])
                )

            if parent is None:
                if direct_prompt_active:
                    edit_plan = build_direct_edit_plan(
                        clean_prompt,
                        mode="scenario" if scenario else "initial_edit",
                    )
                else:
                    edit_plan = parse_edit_intent(
                        clean_prompt,
                        mode="scenario" if scenario else "initial_edit",
                        scenario_id=scenario_id,
                    )
                source_path = Path(session["source_file_path"])
                source_version_id = None
            else:
                parent_plan = (
                    EditPlan.from_json(parent["edit_plan_json"])
                    if parent["edit_plan_json"]
                    else EditPlan.from_legacy(
                        parent["correction_prompt"] or parent["prompt"],
                        correction=bool(parent["correction_prompt"]),
                        correction_target_version_id=parent["parent_version_id"],
                    )
                )
                if correction:
                    if direct_prompt_active:
                        edit_plan = build_direct_edit_plan(
                            clean_prompt,
                            mode="correction",
                            parent=parent_plan,
                            correction_target_version_id=parent["id"],
                        )
                    else:
                        correction_plan = parse_edit_intent(
                            clean_prompt,
                            mode="correction",
                            correction_target_version_id=parent["id"],
                        )
                        edit_plan = merge_edit_plans(parent_plan, correction_plan)
                    if not parent["original_path"]:
                        raise InvalidInputError("The selected parent original is unavailable")
                    source_path = Path(parent["original_path"])
                    source_version_id = parent["id"]
                else:
                    edit_plan = (
                        build_direct_edit_plan(
                            clean_prompt,
                            mode="repeat",
                            parent=parent_plan,
                            correction_target_version_id=parent["id"],
                        )
                        if direct_prompt_active
                        else repeat_edit_plan(parent_plan)
                    )
                    source_path = Path(parent["source_path"])
                    source_version_id = parent["source_version_id"]
            if edit_plan.unresolved_ambiguities and not direct_prompt_active:
                raise IntentAmbiguityError(edit_plan.unresolved_ambiguities)
            if not source_path.is_file():
                raise InvalidInputError("The selected edit source file is unavailable")
            if self.processing_router is None or direct_prompt_active:
                processing_plan = legacy_processing_plan(
                    self.provider.name, self.provider.model
                )
            else:
                from PIL import Image

                with Image.open(source_path) as opened:
                    width, height = opened.size
                orientation = (
                    "square" if width == height
                    else "landscape" if width > height else "portrait"
                )
                processing_plan = self.processing_router.route(
                    edit_plan,
                    source_orientation=orientation,
                    source_aspect_ratio=width / height,
                    parent=parent_processing_plan,
                )
            if processing_plan.requires_user_confirmation:
                raise AssetUnavailableError(
                    "No approved real background is available; AI fallback requires explicit consent"
                )
            prompt_builder_version = (
                DIRECT_PROMPT_VERSION if direct_prompt_active else PROMPT_BUILDER_VERSION
            )
            if direct_prompt_active:
                provider_prompt = build_direct_prompt(
                    prompt,
                    preservation_guard_enabled=(
                        self.settings.image_subject_preserve_guard_enabled
                    ),
                )
            else:
                provider_prompt = build_provider_prompt(edit_plan, processing_plan)
            contextual_modes = {
                ProcessingMode.AI_GENERATION,
                ProcessingMode.LOCAL_AI_EDIT,
                ProcessingMode.RESTORATION,
            }
            if (
                self.provider_context_service is not None
                and processing_plan.selected_mode in contextual_modes
                and hasattr(self.provider, "edit_with_context")
            ):
                context_plan = self.provider_context_service.prepare(
                    connection,
                    gallery_item_id=session["gallery_item_id"],
                    user_id=session["user_id"],
                    parent=parent,
                    correction=correction,
                    repeat=repeat,
                )
            elif processing_plan.selected_mode not in contextual_modes:
                context_plan = ContextPlan("stateless", None, "local_processing_mode")
            elif not hasattr(self.provider, "edit_with_context"):
                context_plan = ContextPlan("stateless", None, "provider_has_no_context_adapter")
            correction_prompt = clean_prompt if correction else None
            effective_user_prompt = edit_plan.effective_user_text
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
            planned_cost = (
                0.0
                if processing_plan.selected_mode in {
                    ProcessingMode.ENHANCEMENT,
                    ProcessingMode.REAL_BACKGROUND_COMPOSITE,
                }
                else self.settings.demo_estimated_cost_rub_per_generation
            )
            projected = float(daily[1]) + planned_cost
            if daily[0] >= self.settings.demo_daily_generation_limit or projected > self.settings.demo_daily_cost_limit_rub:
                raise DailyBudgetError(
                    "Сегодня бесплатные демонстрации временно закончились. "
                    "Можно вернуться позже или получить результат через платную операцию"
                )

            requested_size = "source"
            if not processing_plan.provider.startswith("local-"):
                resolver = getattr(self.provider, "resolve_size", None)
                requested_size = (
                    resolver(source_path)
                    if resolver is not None
                    else getattr(self.provider, "size", None)
                )

            connection.execute(
                """INSERT INTO generation_attempts(
                       id,idempotency_key,session_id,user_id,prompt,scenario_id,status,started_at,
                       provider,model,source_path,input_size_bytes,requested_size,
                       requested_quality,output_format,correction,parent_version_id,
                       correction_prompt,effective_prompt,edit_plan_json,provider_prompt,
                       source_version_id,prompt_builder_version,created_at
                       ,selected_mode,mode_reason,mode_confidence,fallback_mode,
                       asset_source_type,asset_id,asset_checksum,mask_strategy,processing_provider,
                       processing_provider_model,processing_pipeline_version,processing_plan_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    idempotency_key,
                    session_id,
                    session["user_id"],
                    clean_prompt,
                    scenario_id,
                    "processing",
                    iso(now),
                    processing_plan.provider,
                    processing_plan.provider_model,
                    str(source_path),
                    source_path.stat().st_size,
                    requested_size,
                    (
                        "local"
                        if processing_plan.provider.startswith("local-")
                        else getattr(self.provider, "quality", None)
                    ),
                    getattr(self.provider, "output_format", None),
                    int(correction),
                    parent_version_id,
                    correction_prompt,
                    effective_user_prompt,
                    edit_plan.to_json(),
                    provider_prompt,
                    source_version_id,
                    prompt_builder_version,
                    iso(now),
                    processing_plan.selected_mode.value,
                    processing_plan.mode_reason,
                    processing_plan.confidence,
                    processing_plan.fallback_mode.value if processing_plan.fallback_mode else None,
                    processing_plan.asset_source_type.value,
                    processing_plan.asset_id,
                    processing_plan.asset_checksum,
                    processing_plan.mask_strategy.value,
                    processing_plan.provider,
                    processing_plan.provider_model,
                    processing_plan.processing_pipeline_version,
                    processing_plan.to_json(),
                ),
            )
            reservation_id = self.commerce.reserve_generation(
                connection,
                user_id=session["user_id"],
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
            )
            connection.execute(
                "UPDATE generation_attempts SET credit_reservation_id=? WHERE id=?",
                (reservation_id, attempt_id),
            )
            user_id = session["user_id"]
            gallery_item_id = session["gallery_item_id"]

        started = time.monotonic()
        original_path = self.storage.original_path(user_id, session_id, attempt_id)
        preview_path = self.storage.preview_path(
            user_id, session_id, attempt_id, self.watermarker.extension
        )
        try:
            with self._global_semaphore:
                if context_plan.request is not None:
                    provider_result = self.provider.edit_with_context(
                        source_path, provider_prompt, context_plan.request
                    )
                else:
                    provider_result = (
                        self.processing_executor.execute(
                            source_path, provider_prompt, processing_plan
                        )
                        if self.processing_executor is not None
                        else self.provider.edit(source_path, provider_prompt)
                    )
            if context_plan.request is not None and provider_result.context_fallback_used:
                provider_result = replace(provider_result, context_depth=0)
            elif context_plan.request is None and context_plan.fallback:
                provider_result = replace(
                    provider_result,
                    context_fallback_used=True,
                    context_fallback_reason=context_plan.reason,
                    context_depth=0,
                )
            self._stage_provider_result(attempt_id, context_plan, provider_result)
            self.storage.write_private(original_path, provider_result.image_bytes)
            self.watermarker.create_preview(original_path, preview_path, attempt_id)
            delivery = delivery_override or self.deliver_preview
            if not delivery(preview_path, attempt_id):
                raise DeliveryError("Demo preview delivery failed")
        except PolicyRejectedError as exc:
            self._record_context_failure(context_plan, attempt_id, gallery_item_id, exc)
            self._fail_attempt(attempt_id, "rejected_policy", "policy_rejected", str(exc), False)
            raise
        except DeliveryError as exc:
            self._record_context_failure(context_plan, attempt_id, gallery_item_id, exc)
            self._fail_attempt(attempt_id, "delivery_failed", "delivery_failed", str(exc), True)
            raise
        except OSError as exc:
            self._record_context_failure(context_plan, attempt_id, gallery_item_id, exc)
            self._fail_attempt(
                attempt_id,
                "failed_technical",
                "storage_failure",
                "Private storage operation failed",
                True,
            )
            raise StorageFailureError("Private storage operation failed") from exc
        except Exception as exc:
            self._record_context_failure(context_plan, attempt_id, gallery_item_id, exc)
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
                       retries=?, usage_json=?,provider=?,model=?,provider_mode=?,
                       provider_response_id=?,provider_conversation_id=?,provider_context_id=?,
                       context_parent_response_id=?,context_depth=?,context_fallback_reason=?,
                       provider_http_status=?,provider_duration_ms=?
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
                    provider_result.provider_name or processing_plan.provider,
                    provider_result.provider_model or processing_plan.provider_model,
                    provider_result.provider_mode,
                    provider_result.provider_response_id,
                    provider_result.provider_conversation_id,
                    context_plan.request.context_id if context_plan.request else None,
                    (
                        context_plan.request.previous_response_id
                        if context_plan.request else None
                    ),
                    provider_result.context_depth,
                    provider_result.context_fallback_reason,
                    provider_result.http_status,
                    provider_result.provider_duration_ms,
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
            balance = self.commerce.consume_generation(connection, reservation_id)
            remaining = balance.available
            if self.provider_context_service is not None:
                self.provider_context_service.finalize(
                    connection,
                    plan=context_plan,
                    result=provider_result,
                    attempt_id=attempt_id,
                    gallery_item_id=gallery_item_id,
                )
            self.gallery.record_attempt_version(connection, attempt_id, gallery_item_id)
        self.storage.append_attempt_metadata(
            user_id,
            session_id,
            {
                "attempt_id": attempt_id,
                "status": "succeeded",
                "completed_at": iso(completed),
                "provider": self.provider.name,
                "model": self.provider.model,
                "requested_size": requested_size,
                "requested_quality": getattr(self.provider, "quality", None),
                "intent_category": edit_plan.primary_action,
                "edit_mode": edit_plan.mode,
                "parser_version": edit_plan.parser_version,
                "prompt_builder_version": prompt_builder_version,
                "selected_mode": processing_plan.selected_mode.value,
                "processing_pipeline_version": processing_plan.processing_pipeline_version,
                "asset_source_type": processing_plan.asset_source_type.value,
                "asset_id": processing_plan.asset_id,
                "estimated_cost_rub": estimated_cost,
                "original_file": original_path.name,
                "preview_file": preview_path.name,
            },
        )
        return DemoGenerationResult(attempt_id, preview_path, remaining)

    def _stage_provider_result(
        self,
        attempt_id: str,
        plan: ContextPlan,
        result,
    ) -> None:
        """Persist remote ids before storage/delivery so cleanup cannot lose them."""

        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE generation_attempts SET external_request_id=?,provider=?,model=?,
                          provider_mode=?,provider_response_id=?,provider_conversation_id=?,
                          provider_context_id=?,context_parent_response_id=?,context_depth=?,
                          context_fallback_reason=?,provider_http_status=?,provider_duration_ms=?,
                          retries=?,usage_json=? WHERE id=? AND status='processing'""",
                (
                    result.request_id,
                    result.provider_name or self.provider.name,
                    result.provider_model or self.provider.model,
                    result.provider_mode,
                    result.provider_response_id,
                    result.provider_conversation_id,
                    plan.request.context_id if plan.request else None,
                    plan.request.previous_response_id if plan.request else None,
                    result.context_depth,
                    result.context_fallback_reason,
                    result.http_status,
                    result.provider_duration_ms,
                    result.retries,
                    json.dumps(result.usage, ensure_ascii=False),
                    attempt_id,
                ),
            )

    def _record_context_failure(
        self,
        plan: ContextPlan,
        attempt_id: str,
        gallery_item_id: str,
        error: Exception,
    ) -> None:
        if self.provider_context_service is not None:
            self.provider_context_service.record_failure(
                plan,
                attempt_id=attempt_id,
                gallery_item_id=gallery_item_id,
                error=error,
            )

    def _fail_attempt(self, attempt_id: str, status: str, error_type: str, message: str, refund: bool) -> None:
        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE generation_attempts SET status=?, completed_at=?, error_type=?,
                   error_message_safe=?, technical_refund=? WHERE id=? AND status='processing'""",
                (status, iso(self.clock()), error_type, message[:300], int(refund), attempt_id),
            ).rowcount
            if updated:
                reservation = connection.execute(
                    """SELECT id FROM generation_credit_reservations
                       WHERE attempt_id=? AND status='reserved'""",
                    (attempt_id,),
                ).fetchone()
                if reservation:
                    self.commerce.release_generation(
                        connection, reservation["id"], error_type
                    )

    def delete_session(self, session_id: str) -> None:
        with self.database.read() as connection:
            session = connection.execute(
                "SELECT * FROM demo_sessions WHERE id=?", (session_id,)
            ).fetchone()
        if not session:
            return
        self.gallery.soft_delete(session["user_id"], session["gallery_item_id"])
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE demo_sessions SET status='deleted', updated_at=? WHERE id=?",
                (iso(self.clock()), session_id),
            )

    def unlock_original(self, attempt_id: str, idempotency_key: str) -> str:
        """Legacy test seam; production MAX uses the version-scoped PaymentService."""
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
            version = connection.execute(
                "SELECT id FROM gallery_versions WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            expires_at = self.clock() + timedelta(minutes=self.settings.payment_order_ttl_minutes)
            connection.execute(
                """INSERT INTO payment_intents(
                       id,attempt_id,idempotency_key,amount_rub,status,created_at,
                       version_id,user_id,provider,currency,updated_at,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    intent_id, attempt_id, idempotency_key,
                    self.settings.unlock_original_price_rub, "pending", now,
                    version["id"] if version else None, attempt["user_id"], "legacy",
                    "RUB", now, iso(expires_at),
                ),
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
                """UPDATE payment_intents SET status='paid',confirmed_at=?,updated_at=?
                   WHERE id=? AND status='pending'""",
                (now, now, payment_intent_id),
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
            version = connection.execute(
                "SELECT gallery_item_id FROM gallery_versions WHERE attempt_id=?",
                (intent["attempt_id"],),
            ).fetchone()
            if version:
                retention = self.clock() + timedelta(days=self.settings.paid_retention_days)
                connection.execute(
                    """UPDATE gallery_versions SET unlock_status='unlocked',unlocked_at=?
                       WHERE attempt_id=?""",
                    (now, intent["attempt_id"]),
                )
                # Retention remains item-scoped for storage lifecycle, while access
                # stays scoped to this exact GalleryVersion.
                connection.execute(
                    """UPDATE gallery_items SET retention_until=?,updated_at=? WHERE id=?""",
                    (iso(retention), now, version["gallery_item_id"]),
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
