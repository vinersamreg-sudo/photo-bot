"""Typed execution plans between SceneIntent and image processing backends."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping, Optional


PROCESSING_PIPELINE_VERSION = "hybrid-processing-v1"


class ProcessingMode(str, Enum):
    AI_GENERATION = "AI_GENERATION"
    REAL_BACKGROUND_COMPOSITE = "REAL_BACKGROUND_COMPOSITE"
    LOCAL_AI_EDIT = "LOCAL_AI_EDIT"
    ENHANCEMENT = "ENHANCEMENT"
    RESTORATION = "RESTORATION"


class AssetSourceType(str, Enum):
    NONE = "none"
    # Immutable persisted identifier retained for existing asset metadata.
    PIXORA_OWNED = "pixora_owned"
    USER_UPLOADED = "user_uploaded"
    LICENSED_STOCK = "licensed_stock"
    PURCHASED = "purchased"
    SYNTHETIC_TEST = "synthetic_test"


class MaskStrategy(str, Enum):
    NONE = "none"
    LOCAL_REMBG_U2NET_HUMAN = "local_rembg_u2net_human"
    USER_SUPPLIED_ALPHA = "user_supplied_alpha"


@dataclass(frozen=True)
class ProcessingPlan:
    """Serializable, provider-neutral decision made before paid processing."""

    selected_mode: ProcessingMode
    mode_reason: str
    confidence: float
    fallback_mode: Optional[ProcessingMode]
    asset_source_type: AssetSourceType
    asset_id: Optional[str]
    asset_checksum: Optional[str]
    mask_strategy: MaskStrategy
    provider: str
    provider_model: str
    processing_pipeline_version: str = PROCESSING_PIPELINE_VERSION
    background_category: Optional[str] = None
    background_blur: str = "preserve"
    preserve_subject: bool = True
    preserve_face: bool = True
    preserve_clothing: bool = True
    preserve_pose: bool = True
    requires_user_confirmation: bool = False
    ai_finishing: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["selected_mode"] = self.selected_mode.value
        value["fallback_mode"] = self.fallback_mode.value if self.fallback_mode else None
        value["asset_source_type"] = self.asset_source_type.value
        value["mask_strategy"] = self.mask_strategy.value
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProcessingPlan":
        fallback = value.get("fallback_mode")
        return cls(
            selected_mode=ProcessingMode(str(value["selected_mode"])),
            mode_reason=str(value.get("mode_reason") or "legacy processing plan"),
            confidence=max(0.0, min(1.0, float(value.get("confidence", 0.5)))),
            fallback_mode=ProcessingMode(str(fallback)) if fallback else None,
            asset_source_type=AssetSourceType(str(value.get("asset_source_type") or "none")),
            asset_id=str(value["asset_id"]) if value.get("asset_id") else None,
            asset_checksum=(
                str(value["asset_checksum"]) if value.get("asset_checksum") else None
            ),
            mask_strategy=MaskStrategy(str(value.get("mask_strategy") or "none")),
            provider=str(value.get("provider") or "unknown"),
            provider_model=str(value.get("provider_model") or "unknown"),
            processing_pipeline_version=str(
                value.get("processing_pipeline_version") or "legacy-v0"
            ),
            background_category=(
                str(value["background_category"])
                if value.get("background_category") else None
            ),
            background_blur=str(value.get("background_blur") or "preserve"),
            preserve_subject=bool(value.get("preserve_subject", True)),
            preserve_face=bool(value.get("preserve_face", True)),
            preserve_clothing=bool(value.get("preserve_clothing", True)),
            preserve_pose=bool(value.get("preserve_pose", True)),
            requires_user_confirmation=bool(
                value.get("requires_user_confirmation", False)
            ),
            ai_finishing=bool(value.get("ai_finishing", False)),
        )

    @classmethod
    def from_json(cls, raw: str) -> "ProcessingPlan":
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("ProcessingPlan JSON must contain an object")
        return cls.from_dict(value)


def legacy_processing_plan(provider: str, model: str) -> ProcessingPlan:
    """Compatibility plan for directly constructed services in older integrations."""

    return ProcessingPlan(
        selected_mode=ProcessingMode.LOCAL_AI_EDIT,
        mode_reason="Legacy caller did not configure the mode router",
        confidence=0.5,
        fallback_mode=None,
        asset_source_type=AssetSourceType.NONE,
        asset_id=None,
        asset_checksum=None,
        mask_strategy=MaskStrategy.NONE,
        provider=provider,
        provider_model=model,
    )
