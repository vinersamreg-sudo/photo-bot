"""Application service orchestrating the independent Content Studio pipeline."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from .before_after_renderer import BeforeAfterRenderer, TEMPLATES
from .config import ContentStudioSettings
from .content_generator import ContentGenerator
from .models import (
    AssetSourceType,
    ContentCategory,
    DemoAsset,
    DemoPost,
    DemoResult,
    DemoTransformation,
    LicenseStatus,
    PostStatus,
    PublicationMode,
    QualityIssue,
    ResultStatus,
    TransformationType,
    utc_now,
)
from .planner import ContentPlanGenerator
from .publisher import PublicationOutcome, PublisherAdapter
from .quality import ContentQualityGate
from .repository import ContentStudioRepository
from .storage import ContentStorage


class ContentStudioService:
    def __init__(
        self,
        settings: ContentStudioSettings,
        *,
        publisher: PublisherAdapter | None = None,
        publishers: dict[str, PublisherAdapter] | None = None,
    ) -> None:
        self.settings = settings
        self.repository = ContentStudioRepository(settings.database_path)
        self.storage = ContentStorage(
            settings.storage_dir, settings.approved_assets_dir
        )
        self.renderer = BeforeAfterRenderer()
        self.quality = ContentQualityGate()
        self.generator = ContentGenerator(settings.bot_url)
        self.planner = ContentPlanGenerator()
        if publisher is not None and publishers is not None:
            raise ValueError("provide either publisher or publishers")
        if publishers is None:
            if publisher is not None:
                publishers = {publisher.platform: publisher}
            else:
                from .transports import build_publishers

                publishers = build_publishers(settings)
        self.publishers = dict(publishers)

    def add_asset(
        self,
        image: Path,
        *,
        title: str,
        description: str,
        category: ContentCategory,
        tags: tuple[str, ...] = (),
        source_type: AssetSourceType = AssetSourceType.CREATED_FOR_PIXORA,
        license_status: LicenseStatus = LicenseStatus.VERIFIED,
        commercial_allowed: bool = True,
    ) -> DemoAsset:
        if source_type is not AssetSourceType.CREATED_FOR_PIXORA:
            raise ValueError(
                "v1 accepts only assets created specifically for Ravuna "
                "(stored under the legacy created_for_pixora enum)"
            )
        if license_status is not LicenseStatus.VERIFIED or not commercial_allowed:
            raise ValueError("asset must have verified commercial permission")
        title = title.strip()
        if not title or len(title) > 160:
            raise ValueError("asset title must contain 1..160 characters")
        clean_tags = tuple(dict.fromkeys(tag.strip().lower() for tag in tags if tag.strip()))
        asset_id = str(uuid4())
        stored = self.storage.import_image(
            image, namespace="assets", item_id=asset_id, stem="source"
        )
        asset = DemoAsset(
            id=asset_id,
            title=title,
            description=description.strip(),
            source_type=source_type,
            license_status=license_status,
            commercial_allowed=commercial_allowed,
            created_at=utc_now(),
            tags=clean_tags,
            category=category,
            storage_path=stored.relative_path,
            checksum=stored.checksum,
        )
        try:
            self.repository.create_asset(asset)
        except Exception:
            self.storage.remove_tree("assets", asset_id)
            raise
        return asset

    def generate(
        self,
        *,
        asset_id: str,
        after_image: Path,
        transformation_type: TransformationType,
        prompt_en: str,
        scene_intent: dict[str, object] | None = None,
        edit_plan: dict[str, object] | None = None,
        provider: str = "manual",
        platform: str = "max",
        templates: tuple[str, ...] = ("square", "vertical", "stories"),
        reported_issues: tuple[QualityIssue, ...] = (),
    ) -> dict[str, object]:
        asset = self.repository.get_asset(asset_id)
        if asset["source_type"] != AssetSourceType.CREATED_FOR_PIXORA.value:
            raise ValueError("asset is not marked created_for_pixora")
        if asset["license_status"] != LicenseStatus.VERIFIED.value or not asset["commercial_allowed"]:
            raise ValueError("asset is not commercially approved")
        prompt_en = prompt_en.strip()
        if not prompt_en or not prompt_en.isascii():
            raise ValueError("transformation prompt must be non-empty English/ASCII text")
        if not templates or any(template not in TEMPLATES for template in templates):
            raise ValueError("at least one known card template is required")
        if platform not in {"max", "telegram", "vk"}:
            raise ValueError("unsupported Content Studio platform")
        transformation_id = str(uuid4())
        result_id = str(uuid4())
        post_id = str(uuid4())
        stored_after = self.storage.import_image(
            after_image, namespace="results", item_id=result_id, stem="after"
        )
        before_path = self.storage.resolve(asset["storage_path"])
        after_path = self.storage.resolve(stored_after.relative_path)
        assessment = self.quality.assess(before_path, after_path, reported_issues)
        rendered_cards: dict[str, str] = {}
        try:
            for template in dict.fromkeys(templates):
                relative, output = self.storage.output_path(
                    "cards", result_id, f"before-after-{template}.png"
                )
                self.renderer.render(
                    before_path,
                    after_path,
                    output,
                    template=template,
                    title=asset["title"],
                )
                rendered_cards[template] = relative
            thumbnail_relative, thumbnail_path = self.storage.output_path(
                "thumbnails", result_id, "after.webp"
            )
            self.renderer.thumbnail(after_path, thumbnail_path)
            copy = self.generator.generate(
                transformation_type, post_id=post_id, platform=platform
            )
            now = utc_now()
            scene = scene_intent or {"transformation": transformation_type.value}
            plan = dict(edit_plan or {"change": transformation_type.value})
            plan["content_cards"] = rendered_cards
            plan["quality_checks"] = assessment.checks
            transformation = DemoTransformation(
                id=transformation_id,
                asset_id=asset_id,
                transformation_type=transformation_type,
                instructions_en=prompt_en,
                scene_intent=scene,
                edit_plan=plan,
                created_at=now,
            )
            result = DemoResult(
                id=result_id,
                asset_id=asset_id,
                transformation_id=transformation_id,
                provider=provider.strip() or "manual",
                scene_intent=scene,
                edit_plan=plan,
                prompt_en=prompt_en,
                before_path=asset["storage_path"],
                after_path=stored_after.relative_path,
                thumbnail_path=thumbnail_relative,
                watermark_preview_path=rendered_cards[templates[0]],
                status=ResultStatus.NEEDS_REVIEW,
                quality_issues=assessment.issues,
                created_at=now,
            )
            post = DemoPost(
                id=post_id,
                result_id=result_id,
                title=copy.title,
                body=copy.body,
                hashtags=copy.hashtags,
                cta=copy.cta,
                disclosure=copy.disclosure,
                publish_status=PostStatus.NEEDS_REVIEW,
                scheduled_time=None,
                published_time=None,
                platform=platform,
                utm_url=copy.utm_url,
                source_code=copy.source_code,
                generator_prompt_en=copy.internal_prompt_en,
                created_at=now,
                updated_at=now,
            )
            self.repository.create_bundle(transformation, result, post)
        except Exception:
            self.storage.remove_tree("results", result_id)
            self.storage.remove_tree("cards", result_id)
            self.storage.remove_tree("thumbnails", result_id)
            raise
        return {
            "asset_id": asset_id,
            "transformation_id": transformation_id,
            "result_id": result_id,
            "post_id": post_id,
            "status": PostStatus.NEEDS_REVIEW.value,
            "quality_issues": [issue.value for issue in assessment.issues],
            "cards": sorted(rendered_cards),
            "external_ai_requests": 0,
            "external_publications": 0,
        }

    def approve(
        self,
        post_id: str,
        *,
        reviewer: str,
        reason: str,
        apply: bool = False,
    ) -> dict[str, object]:
        post = self.repository.get_post(post_id)
        result = self.repository.get_result(post["result_id"])
        quality_issues = json.loads(result["quality_issues_json"])
        preview = {
            "post_id": post_id,
            "from": post["publish_status"],
            "to": PostStatus.APPROVED.value,
            "quality_issues": quality_issues,
            "apply": apply,
        }
        if quality_issues:
            raise ValueError("post cannot be approved while quality issues are unresolved")
        if post["publish_status"] != PostStatus.NEEDS_REVIEW.value:
            raise ValueError("only a needs_review post can be approved")
        if not reviewer.strip() or not reason.strip():
            raise ValueError("reviewer and reason are required")
        if apply:
            self.repository.approve_post(post_id, reviewer.strip(), reason.strip())
        return preview

    def schedule(
        self,
        post_id: str,
        scheduled_time: str,
        *,
        apply: bool = False,
    ) -> dict[str, object]:
        post = self.repository.get_post(post_id)
        if post["publish_status"] != PostStatus.APPROVED.value:
            raise ValueError("only an approved post can be scheduled")
        try:
            scheduled = datetime.fromisoformat(scheduled_time)
        except ValueError as error:
            raise ValueError("scheduled time must be ISO 8601") from error
        if scheduled.tzinfo is None:
            raise ValueError("scheduled time must include a timezone")
        scheduled_utc = scheduled.astimezone(timezone.utc).isoformat()
        result = {
            "post_id": post_id,
            "from": post["publish_status"],
            "to": PostStatus.SCHEDULED.value,
            "scheduled_time": scheduled_utc,
            "apply": apply,
        }
        if apply:
            self.repository.transition_post(
                post_id, PostStatus.SCHEDULED, scheduled_time=scheduled_utc
            )
        return result

    def create_plan(self, start_date: date, days: int, *, apply: bool = False) -> dict[str, object]:
        entries = self.planner.generate(start_date, days)
        created = self.repository.create_plan(entries) if apply else 0
        return {
            "apply": apply,
            "created": created,
            "entries": [
                {
                    "date": entry.planned_date.isoformat(),
                    "content_type": entry.content_type,
                    "category": entry.category.value if entry.category else None,
                }
                for entry in entries
            ],
        }

    def publication(
        self,
        post_id: str,
        *,
        mode: PublicationMode,
        external_id: str | None = None,
        apply: bool = False,
    ) -> PublicationOutcome:
        post = self.repository.get_post(post_id)
        result = self.repository.get_result(post["result_id"])
        media_path = str(self.storage.resolve(result["watermark_preview_path"]))
        publisher = self.publishers.get(post["platform"])
        if publisher is None:
            raise ValueError("Content Studio publisher is unavailable")
        if mode in {
            PublicationMode.MANUAL_PUBLISH,
            PublicationMode.PUBLISH,
            PublicationMode.RETRY,
        } and post["publish_status"] not in {
            PostStatus.APPROVED.value,
            PostStatus.SCHEDULED.value,
        }:
            raise ValueError("publication requires an approved or scheduled post")
        try:
            if mode is PublicationMode.PREVIEW:
                outcome = publisher.preview(post, media_path)
            elif mode is PublicationMode.DRY_RUN:
                outcome = publisher.dry_run(post, media_path)
            elif mode is PublicationMode.MANUAL_PUBLISH:
                if not apply:
                    raise ValueError("manual_publish requires --apply and an external id")
                outcome = publisher.manual_publish(post, media_path, external_id or "")
            elif mode is PublicationMode.PUBLISH:
                if not apply:
                    raise ValueError("publish requires --apply")
                outcome = publisher.publish(post, media_path)
            elif mode is PublicationMode.RETRY:
                if not apply:
                    raise ValueError("retry requires --apply")
                outcome = publisher.retry(post, media_path)
            else:  # pragma: no cover - enum exhaustiveness
                raise ValueError("unsupported publication mode")
        except Exception as error:
            if mode in {PublicationMode.PUBLISH, PublicationMode.RETRY} and apply:
                self.repository.add_publication_attempt(
                    str(uuid4()),
                    post_id,
                    post["platform"],
                    mode.value,
                    "failed",
                    {"post_id": post_id, "platform": post["platform"]},
                    error_safe=type(error).__name__,
                )
            raise
        if mode in {PublicationMode.PREVIEW, PublicationMode.DRY_RUN} or apply:
            self.repository.add_publication_attempt(
                str(uuid4()),
                post_id,
                post["platform"],
                mode.value,
                outcome.status,
                outcome.payload,
                external_id=outcome.external_id,
            )
        if outcome.status == "succeeded" and mode in {
            PublicationMode.MANUAL_PUBLISH,
            PublicationMode.PUBLISH,
            PublicationMode.RETRY,
        }:
            self.repository.transition_post(
                post_id,
                PostStatus.PUBLISHED,
                published_time=utc_now(),
                external_id=outcome.external_id,
                increment_retry=mode is PublicationMode.RETRY,
            )
        return outcome

    def publish_due(self, *, limit: int = 10, apply: bool = False) -> dict[str, object]:
        """Publish a bounded batch of already reviewed posts whose time has arrived."""

        due = self.repository.due_posts(utc_now(), limit)
        if not apply:
            return {
                "apply": False,
                "due": [post["id"] for post in due],
                "published": [],
                "failed": [],
            }
        published: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        for post in due:
            try:
                outcome = self.publication(
                    post["id"], mode=PublicationMode.PUBLISH, apply=True
                )
            except Exception as error:
                failed.append({"post_id": post["id"], "error": type(error).__name__})
            else:
                published.append(
                    {
                        "post_id": post["id"],
                        "external_id": outcome.external_id or "",
                    }
                )
        return {
            "apply": True,
            "due": [post["id"] for post in due],
            "published": published,
            "failed": failed,
        }

    def record_analytics(
        self,
        post_id: str,
        *,
        views: int,
        clicks: int,
        reactions: int,
        comments: int,
        conversion_to_bot: int,
        starts: int = 0,
        first_photos: int = 0,
        generations: int = 0,
        payments: int = 0,
    ) -> dict[str, object]:
        post = self.repository.get_post(post_id)
        if post["publish_status"] != PostStatus.PUBLISHED.value:
            raise ValueError("analytics can be recorded only for a published post")
        self.repository.add_analytics(
            str(uuid4()),
            post_id,
            views=views,
            clicks=clicks,
            reactions=reactions,
            comments=comments,
            conversion_to_bot=conversion_to_bot,
            starts=starts,
            first_photos=first_photos,
            generations=generations,
            payments=payments,
            utm={
                "utm_source": post["platform"],
                "utm_medium": "channel",
                "utm_campaign": "demo_posts",
                "utm_content": post_id,
                "source_code": post["source_code"],
            },
        )
        return self.repository.analytics_summary(post_id)

    def status(self) -> dict[str, object]:
        status = self.repository.status()
        status.update(
            {
                "publishing_enabled": self.settings.publishing_enabled,
                "external_ai_requests": 0,
                "approved_assets_configured": self.settings.approved_assets_dir.is_dir(),
                "daily_publish_time": self.settings.daily_publish_time,
                "daily_publish_timezone": self.settings.daily_publish_timezone,
                "platform_adapters": sorted(self.publishers),
                "platform_publishing_enabled": {
                    "max": self.settings.max_publishing_enabled,
                    "telegram": self.settings.telegram_publishing_enabled,
                    "vk": self.settings.vk_publishing_enabled,
                },
                "full_auto_enabled": self.settings.full_auto_enabled,
                "minimum_queue_days": self.settings.minimum_queue_days,
                "mode": "full_auto" if self.settings.full_auto_enabled else "review_first",
            }
        )
        return status

    def full_auto_run(
        self, *, apply: bool = False, now: datetime | None = None
    ) -> dict[str, object]:
        from .growth import FullAutoGrowthEngine

        return FullAutoGrowthEngine(self).run(now=now, apply=apply)

    def growth_dashboard(self, *, days: int = 7) -> dict[str, object]:
        from .growth import FullAutoGrowthEngine

        return FullAutoGrowthEngine(self).dashboard(days=days)

    def publishing_permissions(self) -> dict[str, object]:
        from .growth import FullAutoGrowthEngine

        return FullAutoGrowthEngine(self).audit_permissions()
