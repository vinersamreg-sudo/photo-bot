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


class ImageTooLargeError(InvalidInputError):
    pass


class IntentAmbiguityError(InvalidInputError):
    """Raised before provider work when deterministic rules find a contradiction."""

    def __init__(self, ambiguities: tuple[str, ...]) -> None:
        super().__init__("The edit request contains contradictory instructions")
        self.ambiguities = ambiguities


class PolicyRejectedError(DemoError):
    pass


class DeliveryError(DemoError):
    pass


class ProviderTimeoutError(DemoError):
    pass


class ProviderUnavailableError(DemoError):
    pass


class ProviderQuotaError(DemoError):
    pass


class StorageFailureError(DemoError):
    pass


class PaymentRequiredError(DemoError):
    pass


class AssetUnavailableError(DemoError):
    """A licensed resource was required but no approved asset was available."""


class SegmentationFailedError(DemoError):
    """Local foreground extraction failed its technical quality gate."""


@dataclass(frozen=True)
class ProviderResult:
    image_bytes: bytes
    request_id: Optional[str] = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    estimated_cost_rub: Optional[float] = None
    retries: int = 0
    provider_name: Optional[str] = None
    provider_model: Optional[str] = None
    image_model: Optional[str] = None
    provider_response_id: Optional[str] = None
    provider_conversation_id: Optional[str] = None
    provider_mode: str = "stateless"
    context_depth: int = 0
    context_fallback_used: bool = False
    context_fallback_reason: Optional[str] = None
    http_status: Optional[int] = None
    provider_duration_ms: Optional[int] = None


@dataclass(frozen=True)
class ProviderContextRequest:
    """Non-authoritative provider context selected from Pixora lineage."""

    context_id: str
    previous_response_id: Optional[str]
    conversation_id: Optional[str]
    depth: int
    reason: str


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
