"""Autonomous, rights-bounded Ravuna marketing pipeline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.database import ReadOnlyDatabase

from .novelty import MAX_CANDIDATE_ATTEMPTS
from .repository import PublicationSlotConflictError
from .models import (
    ContentCategory,
    ContentPlanEntry,
    DemoPost,
    DemoResult,
    DemoTransformation,
    PostStatus,
    PublicationMode,
    ResultStatus,
    TransformationType,
    utc_now,
)
from .video import VerticalPairVideoSpec, VerticalVideoGenerator

if TYPE_CHECKING:
    from .service import ContentStudioService


@dataclass(frozen=True)
class LibraryAsset:
    id: str
    file: str
    sha256: str
    title: str
    theme: str
    category: ContentCategory
    transformation: TransformationType
    hook: str
    prompt_example: str
    hashtags: tuple[str, ...]
    score: float


class FullAutoGrowthEngine:
    """Keeps an eight-day queue and publishes only machine-approved demo media."""

    def __init__(self, service: ContentStudioService) -> None:
        self.service = service
        self.settings = service.settings
        self.repository = service.repository
        self.quality = service.quality
        self.timezone = _publish_timezone(self.settings.daily_publish_timezone)
        self.video = VerticalVideoGenerator(
            self.settings.approved_assets_dir,
            self.settings.storage_dir,
            ffmpeg_binary=self.settings.ffmpeg_binary,
        )
        self.library = self._load_library()

    def run(self, *, now: datetime | None = None, apply: bool = False) -> dict[str, object]:
        if apply and not self.settings.full_auto_enabled:
            raise ValueError("Content Studio full-auto mode is disabled")
        if apply and not self.settings.publishing_enabled:
            raise ValueError("Content Studio publishing is disabled")
        moment = now or datetime.now(timezone.utc)
        permissions = self.audit_permissions()
        queue = self.maintain_queue(now=moment, apply=apply)
        analytics = self.collect_analytics(apply=apply)
        publication = self.publish_due(now=moment, apply=apply)
        return {
            "mode": "full_auto",
            "apply": apply,
            "permissions": permissions,
            "queue": queue,
            "publication": publication,
            "analytics": analytics,
            "dashboard": self.dashboard(days=7),
        }

    def audit_permissions(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for platform, publisher in self.service.publishers.items():
            transport = getattr(publisher, "transport", None)
            audit = getattr(transport, "audit_permissions", None)
            if callable(audit):
                try:
                    result[platform] = audit()
                except Exception as error:
                    result[platform] = {"ready": False, "error": type(error).__name__}
            else:
                enabled = {
                    "max": self.settings.max_publishing_enabled,
                    "telegram": self.settings.telegram_publishing_enabled,
                    "vk": self.settings.vk_publishing_enabled,
                }.get(platform, False)
                result[platform] = {"ready": False, "configured": bool(enabled)}
        return result

    def maintain_queue(
        self, *, now: datetime | None = None, apply: bool = False
    ) -> dict[str, object]:
        moment = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        today = moment.date()
        horizon = today + timedelta(days=self.settings.minimum_queue_days)
        optimization = self.optimization()
        ideas = self._idea_pool(today, optimization)
        selected = self._select_assets(
            today,
            len(self.library),
            optimization,
            ideas=ideas,
        )
        if apply:
            self.service.novelty.ensure_published_history()
        created: list[str] = []
        skipped: list[dict[str, str]] = []
        plan_entries: list[ContentPlanEntry] = []
        queue_platforms = self._queue_platforms()
        reservations = {
            platform: self.repository.reserved_asset_checksums(platform)
            for platform in queue_platforms
        }
        recent_categories = {
            platform: list(
                reversed(
                    self.repository.recent_publication_categories(platform, limit=3)
                )
            )
            for platform in queue_platforms
        }
        for offset in range(self.settings.minimum_queue_days + 1):
            planned = today + timedelta(days=offset)
            specs = []
            if "max" in queue_platforms:
                specs.append(
                    ("max", "image", self.settings.max_daily_publish_time)
                )
            if "vk" in queue_platforms:
                specs.append(("vk", "video", self.settings.vk_video_publish_time))
            if "vk" in queue_platforms and planned.weekday() in {0, 2, 4}:
                specs.append(("vk", "image", self.settings.vk_wall_publish_time))
            for platform, media_kind, clock in specs:
                scheduled = datetime.combine(
                    planned,
                    datetime.strptime(clock, "%H:%M").time(),
                    tzinfo=self.timezone,
                ).astimezone(timezone.utc).isoformat()
                if self.repository.slot_has_post(platform, scheduled):
                    continue
                rotated = selected[offset % len(selected) :] + selected[: offset % len(selected)]
                available = [
                    item for item in rotated if item.sha256 not in reservations[platform]
                ]
                asset = None
                category_saturated = False
                for candidate in rotated[:MAX_CANDIDATE_ATTEMPTS]:
                    if candidate.sha256 in reservations[platform]:
                        continue
                    alternatives = {
                        item.category.value
                        for item in available
                        if item.sha256 != candidate.sha256
                    }
                    if (
                        len(recent_categories[platform][-3:]) == 3
                        and all(
                            category == candidate.category.value
                            for category in recent_categories[platform][-3:]
                        )
                        and alternatives - {candidate.category.value}
                    ):
                        category_saturated = True
                        continue
                    asset = candidate
                    break
                if asset is None:
                    rejection_reason = (
                        "recent_category_saturation"
                        if available and category_saturated
                        else "candidate_pool_exhausted"
                    )
                    skipped.append(
                        {
                            "asset": "",
                            "platform": platform,
                            "error": rejection_reason,
                        }
                    )
                    if apply:
                        self.repository.record_novelty_event(
                            _stable_id(
                                "novelty-skip",
                                f"{platform}:{scheduled}:{rejection_reason}",
                            ),
                            platform=platform,
                            scheduled_time=scheduled,
                            reason=rejection_reason,
                        )
                    continue
                try:
                    post_id = self._ensure_post(
                        asset,
                        platform=platform,
                        media_kind=media_kind,
                        planned=planned,
                        clock=clock,
                        apply=apply,
                    )
                except Exception as error:
                    skipped.append(
                        {
                            "asset": asset.id,
                            "platform": platform,
                            "error": type(error).__name__,
                        }
                    )
                    continue
                if post_id:
                    created.append(post_id)
                    reservations[platform].add(asset.sha256)
                    recent_categories[platform].append(asset.category.value)
                    plan_entries.append(
                        ContentPlanEntry(
                            id=_stable_id("plan", post_id),
                            planned_date=planned,
                            content_type=(
                                "vk_vertical_video"
                                if platform == "vk" and media_kind == "video"
                                else f"{platform}_before_after"
                            ),
                            category=asset.category,
                            status="assigned",
                            post_id=post_id if apply else None,
                        )
                    )
        if apply and plan_entries:
            self.repository.create_plan(plan_entries)
        return {
            "ideas_generated": len(ideas),
            "idea_types": sorted({str(idea["angle"]) for idea in ideas}),
            "selected_concepts": len(selected),
            "created_posts": created,
            "skipped": skipped,
            "queue_through": horizon.isoformat(),
            "days_queued": self._days_queued(today),
            "exploration_rate": self.settings.exploration_rate,
        }

    def publish_due(
        self, *, now: datetime | None = None, apply: bool = False, limit: int = 10
    ) -> dict[str, object]:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        due = self.repository.due_posts(
            moment,
            limit,
            platforms=self._publishing_platforms(),
        )
        if not apply:
            return {"due": [post["id"] for post in due], "published": [], "failed": []}
        published: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        for post in due:
            try:
                self._validate_staged_post(post["id"])
                outcome = self.service.publication(
                    post["id"], mode=PublicationMode.PUBLISH, apply=True
                )
            except Exception as error:
                failed.append({"post_id": post["id"], "error": type(error).__name__})
            else:
                published.append(
                    {"post_id": post["id"], "external_id": outcome.external_id or ""}
                )
        return {
            "due": [post["id"] for post in due],
            "published": published,
            "deferred": [],
            "failed": failed,
        }

    def reconcile_queue(
        self, *, now: datetime | None = None, apply: bool = False
    ) -> dict[str, object]:
        """Archive stale/disabled queue rows and rebuild from today's slot."""

        moment = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        keep_from = datetime.combine(
            moment.date(),
            datetime.min.time(),
            tzinfo=self.timezone,
        ).astimezone(timezone.utc).isoformat()
        result = self.repository.reconcile_scheduled_queue(
            enabled_platforms=self._queue_platforms(),
            keep_from=keep_from,
            apply=apply,
        )
        result["queue"] = (
            self.maintain_queue(now=moment, apply=True) if apply else None
        )
        return result

    def _queue_platforms(self) -> tuple[str, ...]:
        configured = {
            "max": self.settings.max_publishing_enabled,
            "telegram": self.settings.telegram_publishing_enabled,
            "vk": self.settings.vk_publishing_enabled,
        }
        return tuple(
            platform
            for platform in ("max", "telegram", "vk")
            if configured[platform]
            or bool(
                getattr(
                    self.service.publishers.get(platform),
                    "publishing_enabled",
                    False,
                )
            )
        )

    def _publishing_platforms(self) -> tuple[str, ...]:
        return tuple(
            platform
            for platform, publisher in self.service.publishers.items()
            if bool(getattr(publisher, "publishing_enabled", False))
        )

    def collect_analytics(self, *, apply: bool = False) -> dict[str, object]:
        posts = self.repository.published_posts()
        if not posts:
            return {"posts": 0, "snapshots": 0, "production_db": "read_only"}
        campaigns = [str(post["source_code"])[4:] for post in posts if post["source_code"]]
        funnel = self._funnel_by_campaign(campaigns)
        snapshots = 0
        failures = 0
        for post in posts:
            engagement = {"views": 0, "reactions": 0, "comments": 0}
            publisher = self.service.publishers.get(post["platform"])
            transport = getattr(publisher, "transport", None)
            metrics = getattr(transport, "metrics", None)
            if callable(metrics) and post.get("published_external_id"):
                try:
                    engagement = metrics(post["published_external_id"])
                except Exception:
                    failures += 1
            counts = funnel.get(str(post["source_code"])[4:], {})
            if apply:
                self.service.record_analytics(
                    post["id"],
                    views=int(engagement.get("views", 0)),
                    clicks=0,
                    reactions=int(engagement.get("reactions", 0)),
                    comments=int(engagement.get("comments", 0)),
                    conversion_to_bot=int(counts.get("bot_started", 0)),
                    starts=int(counts.get("bot_started", 0)),
                    first_photos=int(counts.get("photo_uploaded", 0)),
                    generations=int(counts.get("first_generation_success", 0)),
                    payments=int(counts.get("payment_success", 0)),
                )
                snapshots += 1
        return {
            "posts": len(posts),
            "snapshots": snapshots,
            "engagement_failures": failures,
            "production_db": "read_only",
        }

    def optimization(self) -> dict[str, object]:
        themes: dict[str, dict[str, float]] = {}
        for row in self.repository.performance_rows():
            try:
                theme = str(json.loads(row["edit_plan_json"]).get("theme") or "unknown")
            except (TypeError, ValueError):
                theme = "unknown"
            values = themes.setdefault(
                theme,
                {"starts": 0, "photos": 0, "generations": 0, "payments": 0, "score": 0},
            )
            values["starts"] += int(row["starts"])
            values["photos"] += int(row["first_photos"])
            values["generations"] += int(row["generations"])
            values["payments"] += int(row["payments"])
        for values in themes.values():
            starts = max(values["starts"], 1)
            values["score"] = round(
                (values["payments"] * 20 + values["generations"] * 3 + values["photos"])
                / starts,
                4,
            )
        ordered = sorted(themes, key=lambda key: (-themes[key]["score"], key))
        return {
            "top_themes": ordered[:3],
            "bottom_themes": list(reversed(ordered[-3:])),
            "exploration_rate": self.settings.exploration_rate,
            "theme_metrics": themes,
        }

    def dashboard(self, *, days: int = 7) -> dict[str, object]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = self.repository.performance_rows()
        recent = []
        for row in rows:
            post = self.repository.get_post(row["id"])
            published = datetime.fromisoformat(post["published_time"]) if post["published_time"] else None
            if published and published >= since:
                recent.append(row)
        totals = {
            "starts": sum(int(row["starts"]) for row in recent),
            "photos": sum(int(row["first_photos"]) for row in recent),
            "results": sum(int(row["generations"]) for row in recent),
            "payments": sum(int(row["payments"]) for row in recent),
        }
        top = sorted(
            recent,
            key=lambda row: (
                -(int(row["payments"]) / max(int(row["starts"]), 1)),
                -int(row["generations"]),
                row["id"],
            ),
        )[:5]
        connection = self.repository.connect()
        try:
            failed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM content_publication_attempts WHERE status='failed' AND created_at>=?",
                    (since.isoformat(),),
                ).fetchone()[0]
            )
        finally:
            connection.close()
        return {
            "days": days,
            "posts_max": sum(row["platform"] == "max" for row in recent),
            "posts_vk": sum(row["platform"] == "vk" for row in recent),
            "video_count": sum(str(row["id"]).startswith("vk-clip-") for row in recent),
            "bot_starts": totals["starts"],
            "photo_uploads": totals["photos"],
            "results": totals["results"],
            "payments": totals["payments"],
            "payment_per_start": round(totals["payments"] / totals["starts"], 4)
            if totals["starts"]
            else 0.0,
            "photo_per_start": round(totals["photos"] / totals["starts"], 4)
            if totals["starts"]
            else 0.0,
            "result_per_photo": round(totals["results"] / totals["photos"], 4)
            if totals["photos"]
            else 0.0,
            "top_content": [row["id"] for row in top],
            "failed_publications": failed,
            "privacy": "aggregate_only",
        }

    def _ensure_post(
        self,
        asset: LibraryAsset,
        *,
        platform: str,
        media_kind: str,
        planned: date,
        clock: str,
        apply: bool,
    ) -> str | None:
        prefix = "vk-clip" if platform == "vk" and media_kind == "video" else "vk-post" if platform == "vk" else "max"
        post_id = f"{prefix}-{planned.isoformat()}-{asset.id}"
        if self.repository.post_exists(post_id):
            return None
        pair_path = self._validated_source(asset)
        assessment = self.quality.assess_pair_card(pair_path)
        if assessment.blocked:
            raise ValueError("approved pair failed the machine quality gate")
        if not apply:
            return post_id
        stored_asset = self.repository.find_asset_by_checksum(asset.sha256)
        if stored_asset is None:
            stored = self.service.add_asset(
                pair_path,
                title=asset.title,
                description="Synthetic rights-cleared side-by-side Ravuna demonstration.",
                category=asset.category,
                tags=(asset.theme, "synthetic", "before-after"),
            )
            stored_asset = self.repository.get_asset(stored.id)
        media_relative = stored_asset["storage_path"]
        video_checks = None
        if media_kind == "video":
            media_relative, output = self.service.storage.output_path(
                "videos", post_id, "vertical.mp4"
            )
            if output.is_file():
                try:
                    video_checks = self.quality.assess_video(
                        output, ffprobe_binary=self.settings.ffprobe_binary
                    )
                except (OSError, RuntimeError, ValueError):
                    output.unlink(missing_ok=True)
            if video_checks is None:
                self.video.render_pair(
                    VerticalPairVideoSpec(
                        pair_card=pair_path,
                        output=output,
                        hook=asset.hook,
                        prompt_text=f"Запрос: {asset.prompt_example}",
                    )
                )
                video_checks = self.quality.assess_video(
                    output, ffprobe_binary=self.settings.ffprobe_binary
                )
        scheduled = datetime.combine(
            planned,
            datetime.strptime(clock, "%H:%M").time(),
            tzinfo=self.timezone,
        ).astimezone(timezone.utc)
        copy = self.service.generator.generate_demo_case(
            asset.transformation,
            post_id=post_id,
            platform=platform,
            title=asset.title,
            hook=asset.hook,
            prompt_example=asset.prompt_example,
            hashtags=asset.hashtags,
        )
        transformation_id = _stable_id("transformation", post_id)
        result_id = _stable_id("result", post_id)
        now = utc_now()
        edit_plan = {
            "theme": asset.theme,
            "asset_file": asset.file,
            "asset_sha256": asset.sha256,
            "media_kind": media_kind,
            "quality_checks": assessment.checks,
            "video_checks": video_checks,
            "customer_data": False,
            "automatic_quality_gate": True,
        }
        transformation = DemoTransformation(
            id=transformation_id,
            asset_id=stored_asset["id"],
            transformation_type=asset.transformation,
            instructions_en=f"Synthetic marketing demonstration for {asset.theme}.",
            scene_intent={"theme": asset.theme, "synthetic": True},
            edit_plan=edit_plan,
            created_at=now,
        )
        result = DemoResult(
            id=result_id,
            asset_id=stored_asset["id"],
            transformation_id=transformation_id,
            provider="synthetic-approved",
            scene_intent={"theme": asset.theme, "synthetic": True},
            edit_plan=edit_plan,
            prompt_en=f"Synthetic marketing demonstration for {asset.theme}.",
            before_path=stored_asset["storage_path"],
            after_path=stored_asset["storage_path"],
            thumbnail_path=stored_asset["storage_path"],
            watermark_preview_path=media_relative,
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
        self.repository.approve_post(
            post_id,
            "automatic_quality_gate",
            "Rights manifest, image difference, CTA and attribution validation passed.",
        )
        try:
            self.repository.transition_post(
                post_id, PostStatus.SCHEDULED, scheduled_time=scheduled.isoformat()
            )
        except PublicationSlotConflictError:
            self.repository.transition_post(
                post_id,
                PostStatus.ARCHIVED,
                last_error="novelty_publication_slot_conflict",
            )
            self.repository.record_novelty_event(
                _stable_id("novelty-slot", post_id),
                post_id=post_id,
                platform=platform,
                asset_checksum=asset.sha256,
                scheduled_time=scheduled.isoformat(),
                reason="publication_slot_conflict",
            )
            return None
        return post_id

    def _validate_staged_post(self, post_id: str) -> None:
        post = self.repository.post_with_result(post_id)
        if json.loads(post["quality_issues_json"]):
            raise ValueError("post has unresolved quality issues")
        plan = json.loads(post["edit_plan_json"])
        asset = next((item for item in self.library if item.file == plan.get("asset_file")), None)
        if asset is None:
            raise ValueError("post asset is absent from the approved library")
        pair = self._validated_source(asset)
        if self.quality.assess_pair_card(pair).blocked:
            raise ValueError("post source no longer passes the quality gate")
        media = self.service.storage.resolve(post["watermark_preview_path"])
        if plan.get("media_kind") == "video":
            self.quality.assess_video(media, ffprobe_binary=self.settings.ffprobe_binary)
        elif media.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("post image format is invalid")
        if (
            not post["cta"]
            or not post["source_code"]
            or post["source_code"] not in post["cta"]
            or "utm_source=" not in post["cta"]
        ):
            raise ValueError("post CTA or attribution is missing")

    def _validated_source(self, asset: LibraryAsset) -> Path:
        path = (self.settings.approved_assets_dir / asset.file).resolve()
        try:
            path.relative_to(self.settings.approved_assets_dir)
        except ValueError as error:
            raise ValueError("marketing source is outside approved assets") from error
        if not path.is_file():
            raise ValueError("approved marketing source is missing")
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        if checksum != asset.sha256:
            raise ValueError("approved marketing source checksum changed")
        return path

    def _load_library(self) -> tuple[LibraryAsset, ...]:
        try:
            value = json.loads(self.settings.content_library_path.read_text("utf-8"))
            rights = json.loads(
                (self.settings.approved_assets_dir / "manifest.json").read_text("utf-8")
            )
        except (OSError, ValueError) as error:
            raise ValueError("Content Studio library is unavailable") from error
        if value.get("version") != 1 or value.get("customer_data") is not False:
            raise ValueError("Content Studio library policy is invalid")
        rights_by_file = {
            str(item.get("file")): item
            for item in rights.get("assets", [])
            if isinstance(item, dict)
        }
        assets = []
        for raw in value.get("assets", []):
            if raw.get("commercial_use") is not True or raw.get("customer_data") is not False:
                raise ValueError("Content Studio library contains an unapproved asset")
            right = rights_by_file.get(str(raw.get("file")))
            if (
                not right
                or right.get("commercial_use") is not True
                or right.get("customer_data") is not False
                or right.get("sha256") != raw.get("sha256")
            ):
                raise ValueError("Content Studio rights manifest does not approve an asset")
            assets.append(
                LibraryAsset(
                    id=str(raw["id"]),
                    file=str(raw["file"]),
                    sha256=str(raw["sha256"]),
                    title=str(raw["title"]),
                    theme=str(raw["theme"]),
                    category=ContentCategory(str(raw["category"])),
                    transformation=TransformationType(str(raw["transformation"])),
                    hook=str(raw["hook"]),
                    prompt_example=str(raw["prompt_example"]),
                    hashtags=tuple(str(tag) for tag in raw["hashtags"]),
                    score=float(raw.get("score", 1.0)),
                )
            )
        if len(assets) < 7:
            raise ValueError("Content Studio library must contain at least seven approved assets")
        return tuple(assets)

    def _idea_pool(
        self, start: date, optimization: dict[str, object]
    ) -> list[dict[str, object]]:
        metrics = optimization.get("theme_metrics", {})
        angles = ("before_after", "prompt_example", "practical_tip")
        rotation = start.toordinal() % len(self.library)
        ideas: list[dict[str, object]] = []
        for index in range(18):
            asset = self.library[(rotation + index) % len(self.library)]
            angle = angles[(start.toordinal() + index) % len(angles)]
            measured = float((metrics.get(asset.theme) or {}).get("score", 0))
            ideas.append(
                {
                    "id": f"{start.isoformat()}:{asset.id}:{angle}:{index}",
                    "asset": asset,
                    "angle": angle,
                    "score": asset.score + measured - (index * 0.0001),
                    "unseen": not bool((metrics.get(asset.theme) or {}).get("starts")),
                }
            )
        return ideas

    def _select_assets(
        self,
        start: date,
        count: int,
        optimization: dict[str, object],
        *,
        ideas: list[dict[str, object]] | None = None,
    ) -> list[LibraryAsset]:
        metrics = optimization.get("theme_metrics", {})
        candidate_ideas = ideas or self._idea_pool(start, optimization)
        ranked = sorted(candidate_ideas, key=lambda item: (-float(item["score"]), str(item["id"])))
        ordered = []
        seen = set()
        for idea in ranked:
            asset = idea["asset"]
            if asset.id not in seen:
                seen.add(asset.id)
                ordered.append(asset)
        minimum_exploration = max(1, int(round(count * self.settings.exploration_rate)))
        unseen = [item for item in ordered if not (metrics.get(item.theme) or {}).get("starts")]
        selected = list(ordered[:count])
        selected_ids = {item.id for item in selected}
        unseen_ids = {item.id for item in unseen}
        selected_unseen = sum(item.id in unseen_ids for item in selected)
        missing_exploration = max(0, minimum_exploration - selected_unseen)
        candidates = [item for item in unseen if item.id not in selected_ids]
        for index, item in enumerate(candidates[:missing_exploration]):
            selected[-(index + 1)] = item
        return selected

    def _funnel_by_campaign(self, campaigns: list[str]) -> dict[str, dict[str, int]]:
        if not campaigns or not self.settings.production_database_path.is_file():
            return {}
        placeholders = ",".join("?" for _ in campaigns)
        database = ReadOnlyDatabase(self.settings.production_database_path)
        with database.read() as connection:
            rows = connection.execute(
                f"""SELECT campaign,event_type,COUNT(*) count
                    FROM attribution_events
                    WHERE campaign IN ({placeholders})
                      AND event_type IN ('bot_started','photo_uploaded',
                                         'first_generation_success','payment_success')
                    GROUP BY campaign,event_type""",
                tuple(campaigns),
            ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result.setdefault(str(row["campaign"]), {})[str(row["event_type"])] = int(
                row["count"]
            )
        return result

    def _days_queued(self, today: date) -> int:
        value = self.repository.scheduled_through()
        if not value:
            return 0
        return max(0, (datetime.fromisoformat(value).astimezone(self.timezone).date() - today).days)


def _stable_id(namespace: str, value: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"ravuna-content:{namespace}:{value}"))


def _publish_timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Samara":
            return timezone(timedelta(hours=4), name)
        raise
