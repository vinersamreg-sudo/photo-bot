"""Orchestration for one durable first response per Avito chat."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .api import AvitoApiClient, AvitoApiError
from .config import AvitoResponderSettings
from .models import ChatMessage, ClaimedChat, IncomingEvent, ReplyContext
from .repository import AvitoRepository
from .responder import AvitoReplyGenerator


LOGGER = logging.getLogger(__name__)


class AvitoResponderService:
    def __init__(
        self,
        settings: AvitoResponderSettings,
        repository: AvitoRepository,
        api: AvitoApiClient,
        generator: AvitoReplyGenerator,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.api = api
        self.generator = generator

    def accept_webhook(self, payload: dict[str, Any]) -> dict[str, object]:
        event = IncomingEvent.from_webhook(payload)
        direction_allowed = event.direction != "out"
        author_allowed = event.author_id > 0 and event.author_id != self.settings.account_user_id
        account_allowed = event.account_user_id in {0, self.settings.account_user_id}
        type_allowed = event.message_type not in {"system", "call"}
        item_allowed = event.item_id == 0 or event.item_id in self.settings.allowed_item_ids
        eligible = bool(
            self.settings.processing_enabled
            and direction_allowed and author_allowed and account_allowed
            and type_allowed and item_allowed
        )
        inserted, scheduled = self.repository.record_event(
            event,
            eligible=eligible,
            inbound_valid=direction_allowed and author_allowed and account_allowed and type_allowed,
            allowlist_pass=item_allowed,
            reopen_observed=self.settings.mode == "live",
        )
        return {"accepted": True, "duplicate": not inserted, "scheduled": scheduled}

    def process_one_due(self, *, now: datetime | None = None) -> str:
        if not self.settings.processing_enabled:
            return "idle"
        claim = self.repository.claim_due(now=now)
        if claim is None:
            return "idle"
        try:
            chat = self.api.get_chat(self.settings.account_user_id, claim.chat_id)
            if not self.repository.renew_processing_lease(claim):
                self.repository.release_for_newer_revision(claim)
                return "superseded"
            item_id = _chat_item_id(chat)
            if item_id not in self.settings.allowed_item_ids:
                self._finish_claim(claim, "ignored_item")
                return "ignored_item"
            messages = self.api.get_messages(
                self.settings.account_user_id,
                claim.chat_id,
                limit=max(20, self.settings.context_messages),
            )
            if not self.repository.renew_processing_lease(claim):
                self.repository.release_for_newer_revision(claim)
                return "superseded"
            if claim.last_message_id not in {message.message_id for message in messages}:
                self._finish_claim(claim, "unverified_event", "message_not_in_api")
                return "unverified_event"
            if _has_prior_seller_reply(messages, self.settings.account_user_id):
                self._finish_claim(claim, "existing_outbound")
                return "existing_outbound"
            context = _reply_context(
                claim.chat_id,
                item_id,
                messages,
                message_limit=self.settings.context_messages,
                char_limit=self.settings.max_context_chars,
            )
            if not self.repository.renew_processing_lease(claim):
                self.repository.release_for_newer_revision(claim)
                return "superseded"
            generated = self.generator.generate(context)
            if not self.repository.revision_is_current(claim):
                self.repository.release_for_newer_revision(claim)
                return "superseded"
            if not self.repository.prepare_reply(
                claim,
                generated.text,
                self.settings.reply_model,
                generated.response_id,
                bundle_message_count=claim.revision,
                attachment_count=context.image_count,
            ):
                return "duplicate_reply"
            if self.settings.mode == "observe":
                if not self.repository.mark_observed(claim):
                    return "duplicate_reply"
                return "observed"
            if self.settings.mode != "live":
                self._finish_claim(claim, "failed", "send_barrier")
                return "failed"
            if not self.repository.mark_sending(claim):
                return "duplicate_reply"
            try:
                sent_id = self.api.send_message(
                    self.settings.account_user_id, claim.chat_id, generated.text
                )
            except AvitoApiError as error:
                if error.uncertain:
                    sent_id = self._reconcile_uncertain_send(claim.chat_id, generated.text)
                    if sent_id:
                        self.repository.mark_sent(claim.chat_id, sent_id)
                        return "sent_reconciled"
                    self._finish_claim(claim, "uncertain", "delivery_outcome_unknown")
                    return "uncertain"
                self._finish_claim(
                    claim, "delivery_failed", f"avito_{error.status or 'transport'}"
                )
                return "delivery_failed"
            self.repository.mark_sent(claim.chat_id, sent_id)
            return "sent"
        except AvitoApiError as error:
            self._finish_claim(
                claim, "api_failed", f"avito_{error.status or 'transport'}"
            )
            return "api_failed"
        except Exception as error:  # fail one isolated chat; never terminate photo-bot
            LOGGER.exception("Avito first response failed safely (error=%s)", type(error).__name__)
            self._finish_claim(claim, "failed", type(error).__name__)
            return "failed"

    def run_worker(self, stop_event: threading.Event) -> None:
        next_cleanup = datetime.now(timezone.utc)
        while not stop_event.wait(0.25):
            now = datetime.now(timezone.utc)
            if now >= next_cleanup:
                self.repository.cleanup(
                    now - timedelta(days=self.settings.diagnostic_retention_days)
                )
                next_cleanup = now + timedelta(hours=1)
            if not self.settings.processing_enabled:
                continue
            if self.settings.auto_reply_enabled:
                recovery = self.recover_one_sending(now=now)
                if recovery != "idle":
                    LOGGER.info("Avito send recovery completed (result=%s)", recovery)
                    continue
            result = self.process_one_due()
            if result != "idle":
                LOGGER.info("Avito first-response job completed (result=%s)", result)

    def recover_one_sending(self, *, now: datetime | None = None) -> str:
        if self.settings.mode != "live":
            return "idle"
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        attempt = self.repository.recoverable_sending(now=current)
        if attempt is None:
            return "idle"
        try:
            messages = self.api.get_messages(
                self.settings.account_user_id, attempt.chat_id, limit=100
            )
        except AvitoApiError:
            self.repository.mark_terminal(
                attempt.chat_id, "uncertain", "reconciliation_unavailable", now=current
            )
            return "uncertain"
        sent_id = _matching_outbound(
            messages, self.settings.account_user_id, attempt.reply_text
        )
        if sent_id:
            self.repository.mark_sent(attempt.chat_id, sent_id, now=current)
            return "sent_reconciled"
        if _has_prior_seller_reply(messages, self.settings.account_user_id):
            self.repository.mark_terminal(
                attempt.chat_id, "uncertain", "different_outbound_present", now=current
            )
            return "uncertain"
        if attempt.current_revision != attempt.source_revision:
            self.repository.mark_terminal(
                attempt.chat_id, "uncertain", "newer_inbound_after_send", now=current
            )
            return "uncertain"
        if attempt.last_message_id not in {message.message_id for message in messages}:
            self.repository.mark_terminal(
                attempt.chat_id, "uncertain", "source_not_visible", now=current
            )
            return "uncertain"
        age = (current - attempt.updated_at).total_seconds()
        if age < self.settings.send_reconcile_grace_seconds:
            return "reconciliation_wait"
        if attempt.send_attempts != 1:
            self.repository.mark_terminal(
                attempt.chat_id, "uncertain", "retry_limit_reached", now=current
            )
            return "uncertain"
        if not self.repository.claim_reconciled_retry(attempt, now=current):
            return "idle"
        try:
            sent_id = self.api.send_message(
                self.settings.account_user_id, attempt.chat_id, attempt.reply_text
            )
        except AvitoApiError as error:
            if error.uncertain:
                sent_id = self._reconcile_uncertain_send(
                    attempt.chat_id, attempt.reply_text
                )
                if sent_id:
                    self.repository.mark_sent(attempt.chat_id, sent_id)
                    return "sent_reconciled"
            self.repository.mark_terminal(
                attempt.chat_id, "uncertain", "recovery_send_failed"
            )
            return "uncertain"
        self.repository.mark_sent(attempt.chat_id, sent_id)
        return "sent_retried"

    def _reconcile_uncertain_send(self, chat_id: str, reply_text: str) -> str | None:
        try:
            messages = self.api.get_messages(
                self.settings.account_user_id, chat_id, limit=20
            )
        except AvitoApiError:
            return None
        return _matching_outbound(messages, self.settings.account_user_id, reply_text)

    def _finish_claim(
        self, claim: ClaimedChat, status: str, error_class: str | None = None
    ) -> None:
        if not self.repository.mark_claim_terminal(claim, status, error_class):
            self.repository.release_for_newer_revision(claim)


def _chat_item_id(chat: dict[str, Any]) -> int:
    context = chat.get("context") or {}
    value = context.get("value") or {}
    return int(value.get("id") or chat.get("item_id") or 0)


def _has_prior_seller_reply(messages: tuple[ChatMessage, ...], account_user_id: int) -> bool:
    return any(
        message.message_type != "system"
        and (message.direction == "out" or message.author_id == account_user_id)
        for message in messages
    )


def _matching_outbound(
    messages: tuple[ChatMessage, ...], account_user_id: int, reply_text: str
) -> str | None:
    normalized = " ".join(reply_text.split())
    for message in reversed(messages):
        if (
            (message.direction == "out" or message.author_id == account_user_id)
            and " ".join(message.text.split()) == normalized
        ):
            return message.message_id
    return None


def _reply_context(
    chat_id: str,
    item_id: int,
    messages: tuple[ChatMessage, ...],
    *,
    message_limit: int,
    char_limit: int,
) -> ReplyContext:
    customer = tuple(
        message for message in messages
        if message.direction != "out" and message.message_type != "system"
    )[-message_limit:]
    selected: list[ChatMessage] = []
    used = 0
    for message in reversed(customer):
        remaining = max(0, char_limit - used)
        text = message.text[-remaining:] if remaining else ""
        selected.append(
            ChatMessage(
                message.message_id, message.author_id, message.direction,
                message.message_type, text, message.image_count, message.created,
            )
        )
        used += len(text)
        if used >= char_limit:
            break
    selected.reverse()
    texts = [message.text for message in selected if message.text]
    return ReplyContext(
        chat_id=chat_id,
        item_id=item_id,
        messages=tuple(selected),
        image_count=sum(message.image_count for message in selected),
        customer_text="\n".join(texts),
    )
