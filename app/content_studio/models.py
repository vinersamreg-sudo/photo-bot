"""Domain types for the platform-neutral Content Studio core."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AssetSourceType(StrEnum):
    CREATED_FOR_PIXORA = "created_for_pixora"


class LicenseStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ContentCategory(StrEnum):
    REPLACE_BACKGROUND = "replace_background"
    REMOVE_OBJECT = "remove_object"
    REMOVE_PERSON = "remove_person"
    PORTRAIT = "portrait"
    BUSINESS_PHOTO = "business_photo"
    OLD_PHOTO = "old_photo"
    RESTORE = "restore"
    UPSCALE = "upscale"
    COLORIZE = "colorize"
    REPLACE_CLOTHES = "replace_clothes"
    TRAVEL = "travel"
    CAR = "car"
    REAL_ESTATE = "real_estate"
    PRODUCTS = "products"
    FAMILY = "family"
    MEMORIAL_RESTORATION = "memorial_restoration"
    DOCUMENT_PHOTO = "document_photo"
    AVATAR = "avatar"
    WEDDING = "wedding"
    NATURE = "nature"


class TransformationType(StrEnum):
    REPLACE_BACKGROUND = "replace_background"
    REMOVE_OBJECT = "remove_object"
    REMOVE_PERSON = "remove_person"
    PORTRAIT = "portrait"
    BUSINESS_PHOTO = "business_photo"
    RESTORE_PHOTO = "restore_photo"
    UPSCALE = "upscale"
    COLORIZE = "colorize"
    REPLACE_CLOTHES = "replace_clothes"
    DOCUMENT_PHOTO = "document_photo"
    AVATAR = "avatar"
    OTHER = "other"


class ResultStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class PostStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class PublicationMode(StrEnum):
    PREVIEW = "preview"
    DRY_RUN = "dry_run"
    MANUAL_PUBLISH = "manual_publish"
    PUBLISH = "publish"
    RETRY = "retry"


class QualityIssue(StrEnum):
    SIX_FINGERS = "six_fingers"
    BROKEN_HANDS = "broken_hands"
    BAD_EYES = "bad_eyes"
    DOUBLE_TEETH = "double_teeth"
    SMEARED_SKIN = "smeared_skin"
    UNNATURAL_BACKGROUND = "unnatural_background"
    SEGMENTATION_ERROR = "segmentation_error"
    VISUAL_ARTIFACT = "visual_artifact"
    INVALID_IMAGE = "invalid_image"
    TOO_SMALL = "too_small"
    NEAR_UNIFORM = "near_uniform"
    UNCHANGED_RESULT = "unchanged_result"


@dataclass(frozen=True)
class DemoAsset:
    id: str
    title: str
    description: str
    source_type: AssetSourceType
    license_status: LicenseStatus
    commercial_allowed: bool
    created_at: str
    tags: tuple[str, ...]
    category: ContentCategory
    storage_path: str
    checksum: str


@dataclass(frozen=True)
class DemoTransformation:
    id: str
    asset_id: str
    transformation_type: TransformationType
    instructions_en: str
    scene_intent: dict[str, Any]
    edit_plan: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class DemoResult:
    id: str
    asset_id: str
    transformation_id: str
    provider: str
    scene_intent: dict[str, Any]
    edit_plan: dict[str, Any]
    prompt_en: str
    before_path: str
    after_path: str
    thumbnail_path: str
    watermark_preview_path: str
    status: ResultStatus
    quality_issues: tuple[QualityIssue, ...]
    created_at: str


@dataclass(frozen=True)
class DemoPost:
    id: str
    result_id: str
    title: str
    body: str
    hashtags: tuple[str, ...]
    cta: str
    disclosure: str
    publish_status: PostStatus
    scheduled_time: str | None
    published_time: str | None
    platform: str
    utm_url: str
    generator_prompt_en: str
    created_at: str
    updated_at: str
    published_external_id: str | None = None
    retry_count: int = 0
    last_error: str | None = None


@dataclass(frozen=True)
class ContentPlanEntry:
    id: str
    planned_date: date
    content_type: str
    category: ContentCategory | None
    status: str = "planned"
    post_id: str | None = None


@dataclass(frozen=True)
class QualityAssessment:
    issues: tuple[QualityIssue, ...] = field(default_factory=tuple)
    checks: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return bool(self.issues)
