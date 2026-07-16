"""Deterministic selection of the safest image-processing technology."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from app.background_assets import BackgroundCatalog
from app.edit_intent import EditPlan
from app.processing_modes import (
    AssetSourceType,
    MaskStrategy,
    ProcessingMode,
    ProcessingPlan,
)


REAL_BACKGROUND_CATEGORIES = {
    "rocky_mountains",
    "alpine_mountains",
    "forest",
    "beach",
    "sea",
    "modern_office",
    "light_photo_studio",
    "cafe",
    "city_business",
    "neutral_resume_background",
    "park",
    "street",
    "interior",
    "natural_landscape",
}


class ModeRouter:
    def __init__(
        self,
        catalog: BackgroundCatalog,
        *,
        provider_name: str,
        provider_model: str,
        real_background_enabled: bool = False,
        allow_ai_background_fallback: bool = False,
    ) -> None:
        self.catalog = catalog
        self.provider_name = provider_name
        self.provider_model = provider_model
        self.real_background_enabled = real_background_enabled
        self.allow_ai_background_fallback = allow_ai_background_fallback

    @staticmethod
    def _category(plan: EditPlan) -> Optional[str]:
        category = plan.scene.background.category
        if category:
            return category
        setting = (plan.scene.background.setting or "").lower()
        text = plan.source_user_text.casefold().replace("ё", "е")
        russian_categories = {
            "rocky_mountains": ("скал", "каменн", "скалист"),
            "alpine_mountains": ("альп",),
            "forest": ("лес",),
            "beach": ("пляж",),
            "sea": ("море", "морск"),
            "modern_office": ("офис",),
            "light_photo_studio": ("фотостуди", "светлую студи", "светлая студи"),
            "cafe": ("кафе",),
            "city_business": ("москва-сити", "деловой город", "бизнес квартал"),
            "neutral_resume_background": ("для резюме", "нейтральн"),
            "park": ("парк",),
            "street": ("улиц",),
            "interior": ("интерьер",),
            "natural_landscape": ("пейзаж", "природ"),
        }
        for value, hints in russian_categories.items():
            if any(hint in text for hint in hints):
                return value
        for value, hints in {
            "rocky_mountains": ("rocky mountain", "rock formation"),
            "alpine_mountains": ("alpine",),
            "light_photo_studio": ("light modern photo studio", "light studio"),
            "modern_office": ("modern office",),
            "cafe": ("cafe",),
            "beach": ("beach",),
            "forest": ("forest",),
            "sea": ("sea",),
        }.items():
            if any(hint in setting for hint in hints):
                return value
        return None

    @staticmethod
    def _is_fantasy(plan: EditPlan) -> bool:
        text = plan.source_user_text.casefold().replace("ё", "е")
        return any(
            word in text
            for word in (
                "фэнтез", "сказоч", "парящ", "магич", "космос", "киберпанк",
                "сюрреал", "аниме", "несуществ", "инопланет",
            )
        )

    def route(
        self,
        plan: EditPlan,
        *,
        source_orientation: Optional[str] = None,
        source_aspect_ratio: Optional[float] = None,
        parent: Optional[ProcessingPlan] = None,
    ) -> ProcessingPlan:
        if plan.mode == "repeat" and parent is not None:
            return replace(
                parent,
                mode_reason="Repeat preserves the parent processing plan and asset",
                confidence=min(parent.confidence, plan.confidence),
                requires_user_confirmation=False,
            )

        if self._is_fantasy(plan):
            return self._provider_plan(
                ProcessingMode.AI_GENERATION,
                "The requested scene is fictional or generative by nature",
                plan.confidence,
                parent,
            )

        if plan.primary_action == "restore_photo":
            return self._provider_plan(
                ProcessingMode.RESTORATION,
                "Damage, fading or historical restoration was requested",
                plan.confidence,
                parent,
            )

        if plan.primary_action in {"improve_quality", "sharpen_background"}:
            return ProcessingPlan(
                selected_mode=ProcessingMode.ENHANCEMENT,
                mode_reason="The request can be attempted with conservative local enhancement",
                confidence=plan.confidence,
                fallback_mode=ProcessingMode.LOCAL_AI_EDIT,
                asset_source_type=(parent.asset_source_type if parent else AssetSourceType.NONE),
                asset_id=parent.asset_id if parent else None,
                asset_checksum=parent.asset_checksum if parent else None,
                mask_strategy=MaskStrategy.NONE,
                provider="local-pillow",
                provider_model="pillow-enhancement-v1",
                background_category=(parent.background_category if parent else self._category(plan)),
                background_blur="forbidden" if plan.primary_action == "sharpen_background" else "preserve",
            )

        if plan.primary_action == "replace_background":
            if self._is_fantasy(plan) or plan.scene.background.source == "ai":
                return self._provider_plan(
                    ProcessingMode.AI_GENERATION,
                    "The requested scene is fictional or generative by nature",
                    plan.confidence,
                    parent,
                )
            category = self._category(plan)
            if category in REAL_BACKGROUND_CATEGORIES:
                asset = self.catalog.match(
                    category,
                    orientation=source_orientation,
                    aspect_ratio=source_aspect_ratio,
                )
                executable = self.real_background_enabled and asset is not None
                fallback = ProcessingMode.AI_GENERATION
                return ProcessingPlan(
                    selected_mode=ProcessingMode.REAL_BACKGROUND_COMPOSITE,
                    mode_reason=(
                        "A photographic real-world scene should use a licensed catalog asset"
                    ),
                    confidence=plan.confidence,
                    fallback_mode=fallback,
                    asset_source_type=(asset.source_type if asset else AssetSourceType.NONE),
                    asset_id=asset.id if asset else None,
                    asset_checksum=asset.file_checksum if asset else None,
                    mask_strategy=MaskStrategy.LOCAL_REMBG_U2NET_HUMAN,
                    provider="local-composite",
                    provider_model="rembg-u2net-human+pillow-v1",
                    background_category=category,
                    background_blur="forbidden",
                    preserve_subject=True,
                    preserve_face=True,
                    preserve_clothing=True,
                    preserve_pose=True,
                    requires_user_confirmation=not executable,
                    ai_finishing=False,
                )
            return self._provider_plan(
                ProcessingMode.AI_GENERATION,
                "No licensed real-scene category was identified",
                min(plan.confidence, 0.7),
                parent,
            )

        return self._provider_plan(
            ProcessingMode.LOCAL_AI_EDIT,
            "The request targets clothing, pose, objects, lighting or another local region",
            plan.confidence,
            parent,
        )

    def _provider_plan(
        self,
        mode: ProcessingMode,
        reason: str,
        confidence: float,
        parent: Optional[ProcessingPlan],
    ) -> ProcessingPlan:
        return ProcessingPlan(
            selected_mode=mode,
            mode_reason=reason,
            confidence=confidence,
            fallback_mode=None,
            asset_source_type=parent.asset_source_type if parent else AssetSourceType.NONE,
            asset_id=parent.asset_id if parent else None,
            asset_checksum=parent.asset_checksum if parent else None,
            mask_strategy=MaskStrategy.NONE,
            provider=self.provider_name,
            provider_model=self.provider_model,
            background_category=parent.background_category if parent else None,
            background_blur=parent.background_blur if parent else "preserve",
        )
