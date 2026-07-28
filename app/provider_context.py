"""Optional provider memory derived from, never replacing, Ravuna lineage."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol
from uuid import uuid4

from openai import NotFoundError, OpenAIError

from app.config import Settings
from app.database import Database
from app.domain import InvalidInputError, ProviderContextRequest, ProviderResult


UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class ProviderContextGateway(Protocol):
    def create_conversation(self) -> str: ...

    def delete_response(self, response_id: str) -> None: ...

    def delete_conversation(self, conversation_id: str) -> None: ...


class OpenAIProviderContextGateway:
    """Small remote lifecycle boundary; SDK retry policy remains authoritative."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def create_conversation(self) -> str:
        conversation = self.client.conversations.create()
        conversation_id = getattr(conversation, "id", None)
        if not conversation_id:
            raise RuntimeError("OpenAI returned no conversation id")
        return str(conversation_id)

    def delete_response(self, response_id: str) -> None:
        try:
            self.client.responses.delete(response_id)
        except NotFoundError:
            return

    def delete_conversation(self, conversation_id: str) -> None:
        try:
            self.client.conversations.delete(conversation_id)
        except NotFoundError:
            return


@dataclass(frozen=True)
class ContextPlan:
    mode: str
    request: Optional[ProviderContextRequest]
    reason: str
    fallback: bool = False


class ProviderContextService:
    """Owns optional provider ids while Ravuna DB remains the source of truth."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        gateway: Optional[ProviderContextGateway] = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.settings = settings
        self.database = database
        self.gateway = gateway
        self.clock = clock

    @property
    def operationally_enabled(self) -> bool:
        return bool(
            self.settings.openai_conversation_memory_enabled
            and self.settings.openai_responses_image_enabled
            and self.settings.openai_conversation_retention_enabled
        )

    def prepare(
        self,
        connection: sqlite3.Connection,
        *,
        gallery_item_id: str,
        user_id: str,
        parent: Optional[sqlite3.Row],
        correction: bool,
        repeat: bool,
    ) -> ContextPlan:
        if repeat:
            return ContextPlan("stateless", None, "repeat_is_stateless")
        if not self.settings.openai_conversation_memory_enabled:
            return ContextPlan("stateless", None, "memory_disabled")
        if not self.settings.openai_responses_image_enabled:
            return ContextPlan("stateless", None, "responses_disabled")
        if not self.settings.openai_conversation_retention_enabled:
            return ContextPlan("stateless", None, "retention_disabled")
        if correction and parent is None:
            return ContextPlan("stateless", None, "missing_parent", fallback=True)
        if correction and not parent["provider_response_id"]:
            return ContextPlan(
                "stateless", None, "missing_parent_response_id", fallback=True
            )

        now = self.clock()
        row = connection.execute(
            "SELECT * FROM provider_contexts WHERE gallery_item_id=?",
            (gallery_item_id,),
        ).fetchone()
        if row is None:
            context_id = uuid4().hex
            connection.execute(
                """INSERT INTO provider_contexts(
                       id,gallery_item_id,user_id,provider_name,provider_model,image_model,
                       status,created_at,updated_at,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    context_id,
                    gallery_item_id,
                    user_id,
                    "openai-responses",
                    self.settings.openai_responses_model,
                    self.settings.openai_image_model,
                    "active",
                    _iso(now),
                    _iso(now),
                    _iso(now + timedelta(days=self.settings.openai_context_retention_days)),
                ),
            )
            row = connection.execute(
                "SELECT * FROM provider_contexts WHERE id=?", (context_id,)
            ).fetchone()
        else:
            context_id = row["id"]

        previous_response_id = parent["provider_response_id"] if parent is not None else None
        parent_depth = int(parent["context_depth"] or 0) if parent is not None else 0
        last_used = row["last_used_at"] or row["updated_at"]
        idle = now - datetime.fromisoformat(last_used)
        if row["status"] != "active":
            return ContextPlan("stateless", None, "context_not_active", fallback=True)
        if idle >= timedelta(days=self.settings.openai_context_max_idle_days):
            connection.execute(
                """UPDATE provider_contexts SET last_response_id=NULL,depth=0,
                          reset_count=reset_count+1,updated_at=? WHERE id=?""",
                (_iso(now), context_id),
            )
            return ContextPlan("stateless", None, "context_idle_expired", fallback=True)
        reset_reason = None
        if parent_depth >= self.settings.openai_context_max_depth:
            reset_reason = "max_depth_reset"
        if reset_reason:
            previous_response_id = None
            parent_depth = 0
            connection.execute(
                """UPDATE provider_contexts SET status='active',last_response_id=NULL,
                          depth=0,reset_count=reset_count+1,deletion_requested_at=NULL,
                          deleted_at=NULL,last_error_class=NULL WHERE id=?""",
                (context_id,),
            )

        depth = parent_depth + 1 if previous_response_id else 1
        reason = reset_reason or (
            "branch_from_parent_response"
            if previous_response_id
            else "new_chain_from_parent_image" if parent is not None else "new_context"
        )
        expires = now + timedelta(days=self.settings.openai_context_retention_days)
        connection.execute(
            "UPDATE provider_contexts SET updated_at=?,expires_at=? WHERE id=?",
            (_iso(now), _iso(expires), context_id),
        )
        request = ProviderContextRequest(
            context_id=context_id,
            previous_response_id=previous_response_id,
            conversation_id=row["provider_conversation_id"],
            depth=depth,
            reason=reason,
        )
        self._event(
            connection,
            context_id=context_id,
            gallery_item_id=gallery_item_id,
            event_type="context_prepared",
            provider_mode="responses",
            depth=depth,
        )
        return ContextPlan("responses", request, reason)

    def finalize(
        self,
        connection: sqlite3.Connection,
        *,
        plan: ContextPlan,
        result: ProviderResult,
        attempt_id: str,
        gallery_item_id: str,
    ) -> None:
        request = plan.request
        if request is None:
            self._event(
                connection,
                context_id=None,
                attempt_id=attempt_id,
                gallery_item_id=gallery_item_id,
                event_type="context_fallback" if plan.fallback else "stateless_completed",
                provider_mode=result.provider_mode,
                depth=0,
                fallback_reason=plan.reason if plan.fallback else None,
                duration_ms=result.provider_duration_ms,
            )
            return
        now = self.clock()
        if result.provider_mode == "responses" and result.provider_response_id:
            connection.execute(
                """UPDATE provider_contexts SET last_response_id=?,depth=?,last_used_at=?,
                          updated_at=?,last_error_class=NULL WHERE id=?""",
                (
                    result.provider_response_id,
                    request.depth,
                    _iso(now),
                    _iso(now),
                    request.context_id,
                ),
            )
            event_type = "context_completed"
        else:
            connection.execute(
                """UPDATE provider_contexts SET fallback_count=fallback_count+1,
                          updated_at=?,last_error_class=? WHERE id=?""",
                (
                    _iso(now),
                    result.context_fallback_reason,
                    request.context_id,
                ),
            )
            event_type = "context_fallback"
        self._event(
            connection,
            context_id=request.context_id,
            attempt_id=attempt_id,
            gallery_item_id=gallery_item_id,
            event_type=event_type,
            provider_mode=result.provider_mode,
            depth=request.depth,
            fallback_reason=result.context_fallback_reason,
            duration_ms=result.provider_duration_ms,
        )

    def record_failure(
        self,
        plan: ContextPlan,
        *,
        attempt_id: str,
        gallery_item_id: str,
        error: Exception,
    ) -> None:
        if plan.request is None:
            return
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE provider_contexts SET updated_at=?,last_error_class=? WHERE id=?",
                (_iso(self.clock()), type(error).__name__, plan.request.context_id),
            )
            self._event(
                connection,
                context_id=plan.request.context_id,
                attempt_id=attempt_id,
                gallery_item_id=gallery_item_id,
                event_type="context_failed",
                provider_mode="responses",
                depth=plan.request.depth,
                error_class=type(error).__name__,
            )

    def create_remote_conversation(self, context_id: str) -> str:
        if self.gateway is None:
            raise RuntimeError("Provider context gateway is unavailable")
        conversation_id = self.gateway.create_conversation()
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE provider_contexts SET provider_conversation_id=?,updated_at=?
                   WHERE id=? AND status='active'""",
                (conversation_id, _iso(self.clock()), context_id),
            )
        return conversation_id

    def get_context(
        self, gallery_item_id: str, *, user_id: Optional[str] = None
    ) -> Optional[sqlite3.Row]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM provider_contexts WHERE gallery_item_id=?",
                (gallery_item_id,),
            ).fetchone()
            if row is not None and user_id is not None and row["user_id"] != user_id:
                raise InvalidInputError("Provider context does not belong to this user")
            return row

    def reset_context(self, gallery_item_id: str, *, user_id: str) -> None:
        now = self.clock()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM provider_contexts WHERE gallery_item_id=?",
                (gallery_item_id,),
            ).fetchone()
            if row is None:
                return
            if row["user_id"] != user_id:
                raise InvalidInputError("Provider context does not belong to this user")
            connection.execute(
                """UPDATE provider_contexts SET status='active',last_response_id=NULL,
                          depth=0,reset_count=reset_count+1,updated_at=?,last_used_at=NULL,
                          last_error_class=NULL WHERE id=?""",
                (_iso(now), row["id"]),
            )

    @staticmethod
    def fallback_to_stateless(reason: str) -> ContextPlan:
        return ContextPlan("stateless", None, reason, fallback=True)

    def delete_for_gallery(self, gallery_item_id: str) -> bool:
        now = self.clock()
        with self.database.transaction() as connection:
            context = connection.execute(
                "SELECT * FROM provider_contexts WHERE gallery_item_id=?",
                (gallery_item_id,),
            ).fetchone()
            if context is None:
                return True
            responses = connection.execute(
                """SELECT DISTINCT provider_response_id FROM gallery_versions
                   WHERE gallery_item_id=? AND provider_response_id IS NOT NULL""",
                (gallery_item_id,),
            ).fetchall()
            response_ids = [row[0] for row in responses]
            attempt_responses = connection.execute(
                """SELECT DISTINCT a.provider_response_id
                   FROM generation_attempts a
                   JOIN demo_sessions s ON s.id=a.session_id
                   WHERE s.gallery_item_id=? AND a.provider_response_id IS NOT NULL""",
                (gallery_item_id,),
            ).fetchall()
            response_ids.extend(row[0] for row in attempt_responses)
            if context["last_response_id"]:
                response_ids.append(context["last_response_id"])
            response_ids = list(dict.fromkeys(response_ids))
            connection.execute(
                """UPDATE provider_contexts SET status='delete_pending',
                          deletion_requested_at=COALESCE(deletion_requested_at,?),
                          delete_attempts=delete_attempts+1,updated_at=? WHERE id=?""",
                (_iso(now), _iso(now), context["id"]),
            )

        if self.gateway is None and (response_ids or context["provider_conversation_id"]):
            self._mark_delete_error(context["id"], "gateway_unavailable")
            return False
        try:
            if self.gateway is not None:
                for response_id in response_ids:
                    self.gateway.delete_response(response_id)
                if context["provider_conversation_id"]:
                    self.gateway.delete_conversation(context["provider_conversation_id"])
        except (OpenAIError, RuntimeError) as exc:
            self._mark_delete_error(context["id"], type(exc).__name__)
            return False
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE gallery_versions SET provider_response_id=NULL,
                          provider_conversation_id=NULL,provider_context_id=NULL,
                          context_parent_response_id=NULL,provider_parent_response_id=NULL
                   WHERE gallery_item_id=?""",
                (gallery_item_id,),
            )
            connection.execute(
                """UPDATE generation_attempts SET provider_response_id=NULL,
                          provider_conversation_id=NULL,provider_context_id=NULL,
                          context_parent_response_id=NULL,provider_parent_response_id=NULL
                   WHERE session_id IN (
                       SELECT id FROM demo_sessions WHERE gallery_item_id=?
                   )""",
                (gallery_item_id,),
            )
            connection.execute(
                """UPDATE provider_contexts SET status='deleted',deleted_at=?,updated_at=?,
                          last_response_id=NULL,provider_conversation_id=NULL,last_error_class=NULL
                   WHERE id=?""",
                (_iso(now), _iso(now), context["id"]),
            )
            self._event(
                connection,
                context_id=context["id"],
                gallery_item_id=gallery_item_id,
                event_type="context_delete_success",
                provider_mode="lifecycle",
                depth=int(context["depth"] or 0),
            )
        return True

    def delete_for_user(self, user_id: str) -> dict[str, bool]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT gallery_item_id FROM provider_contexts WHERE user_id=? AND status!='deleted'",
                (user_id,),
            ).fetchall()
        return {row[0]: self.delete_for_gallery(row[0]) for row in rows}

    def cleanup_due(self, *, execute: bool) -> list[str]:
        now = self.clock()
        idle_cutoff = _iso(
            now - timedelta(days=self.settings.openai_context_max_idle_days)
        )
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT gallery_item_id FROM provider_contexts
                   WHERE status='delete_pending' OR
                         (status='active' AND (expires_at<=? OR updated_at<=?))""",
                (_iso(now), idle_cutoff),
            ).fetchall()
        gallery_ids = [row[0] for row in rows]
        if execute:
            for gallery_item_id in gallery_ids:
                self.delete_for_gallery(gallery_item_id)
        return gallery_ids

    def _mark_delete_error(self, context_id: str, error_class: str) -> None:
        with self.database.transaction() as connection:
            context = connection.execute(
                "SELECT gallery_item_id,depth FROM provider_contexts WHERE id=?",
                (context_id,),
            ).fetchone()
            connection.execute(
                "UPDATE provider_contexts SET last_error_class=?,updated_at=? WHERE id=?",
                (error_class, _iso(self.clock()), context_id),
            )
            if context is not None:
                self._event(
                    connection,
                    context_id=context_id,
                    gallery_item_id=context["gallery_item_id"],
                    event_type="context_delete_failure",
                    provider_mode="lifecycle",
                    depth=int(context["depth"] or 0),
                    error_class=error_class,
                )

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        context_id: Optional[str],
        event_type: str,
        provider_mode: str,
        depth: int,
        attempt_id: Optional[str] = None,
        gallery_item_id: Optional[str] = None,
        fallback_reason: Optional[str] = None,
        error_class: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        connection.execute(
            """INSERT INTO provider_context_events(
                   context_id,attempt_id,gallery_item_id,event_type,provider_mode,
                   context_depth,fallback_reason,error_class,duration_ms,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                context_id,
                attempt_id,
                gallery_item_id,
                event_type,
                provider_mode,
                depth,
                fallback_reason,
                error_class,
                duration_ms,
                _iso(datetime.now(UTC)),
            ),
        )
