"""Domain types for the free demo workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional


ATTEMPT_STATUSES = {
    "pending",
    "processing",
    "succeeded",
    "failed_technical",
    "rejected_policy",
    "cancelled",
    "delivery_failed",
}


class DemoError(RuntimeError):
    """Base error safe to map to a user-facing demo message."""


class DemoLimitError(DemoError):
    pass


class DemoExpiredError(DemoError):
    pass


class SourceReplacementError(DemoError):
    pass


class ConcurrentGenerationError(DemoError):
    pass


class CooldownError(DemoError):
    pass


class DailyBudgetError(DemoError):
    pass


class InvalidInputError(DemoError):
    pass


class PolicyRejectedError(DemoError):
    pass


class DeliveryError(DemoError):
    pass


class PaymentRequiredError(DemoError):
    pass


@dataclass(frozen=True)
class ProviderResult:
    image_bytes: bytes
    request_id: Optional[str] = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    estimated_cost_rub: Optional[float] = None
    retries: int = 0


@dataclass(frozen=True)
class DemoGenerationResult:
    attempt_id: str
    preview_path: Path
    remaining_generations: int
    idempotent_replay: bool = False


@dataclass(frozen=True)
class DemoSessionInfo:
    session_id: str
    user_id: str
    source_path: Path
    expires_at: str
    successful_generations: int
    max_generations: int
