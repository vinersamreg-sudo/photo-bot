"""Operator CLI exposed as ``ravuna content ...``."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import ContentStudioSettings
from .models import (
    ContentCategory,
    PostStatus,
    PublicationMode,
    QualityIssue,
    TransformationType,
)
from .service import ContentStudioService
from .video import VerticalVideoGenerator, VerticalVideoSpec, command_preview


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ravuna")
    commands = parser.add_subparsers(dest="command", required=True)
    content = commands.add_parser("content", help="Ravuna Content Studio")
    actions = content.add_subparsers(dest="content_command", required=True)

    status = actions.add_parser("status")
    _add_format(status)

    auto_run = actions.add_parser("auto-run")
    auto_run.add_argument("--apply", action="store_true")
    _add_format(auto_run)

    dashboard = actions.add_parser("dashboard")
    dashboard.add_argument("--days", type=int, default=7)
    _add_format(dashboard)

    permissions = actions.add_parser("permissions")
    _add_format(permissions)

    generate = actions.add_parser("generate")
    source = generate.add_mutually_exclusive_group(required=True)
    source.add_argument("--asset-id")
    source.add_argument("--source-image", type=Path)
    generate.add_argument("--after-image", type=Path, required=True)
    generate.add_argument("--title")
    generate.add_argument("--description", default="")
    generate.add_argument("--category", choices=_values(ContentCategory))
    generate.add_argument("--tags", default="")
    generate.add_argument("--transformation", choices=_values(TransformationType), required=True)
    generate.add_argument("--prompt-en", required=True)
    generate.add_argument("--provider", default="manual")
    generate.add_argument("--platform", default="max")
    generate.add_argument("--template", choices=("square", "vertical", "stories"), action="append")
    generate.add_argument("--quality-issue", choices=_values(QualityIssue), action="append", default=[])
    generate.add_argument("--scene-intent-json", default="{}")
    generate.add_argument("--edit-plan-json", default="{}")
    _add_format(generate)

    queue = actions.add_parser("queue")
    queue.add_argument("--status", choices=_values(PostStatus))
    queue.add_argument("--limit", type=int, default=100)
    _add_format(queue)

    approve = actions.add_parser("approve")
    approve.add_argument("--post-id", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--apply", action="store_true")
    _add_format(approve)

    schedule = actions.add_parser("schedule")
    schedule.add_argument("--post-id")
    schedule.add_argument("--at")
    schedule.add_argument("--plan-start", type=date.fromisoformat)
    schedule.add_argument("--days", type=int, default=7)
    schedule.add_argument("--apply", action="store_true")
    _add_format(schedule)

    publish = actions.add_parser("publish")
    publish.add_argument("--post-id", required=True)
    publish.add_argument("--mode", choices=_values(PublicationMode), default="dry_run")
    publish.add_argument("--external-id")
    publish.add_argument("--apply", action="store_true")
    _add_format(publish)

    publish_due = actions.add_parser("publish-due")
    publish_due.add_argument("--limit", type=int, default=10)
    publish_due.add_argument("--apply", action="store_true")
    _add_format(publish_due)

    reconcile_queue = actions.add_parser("reconcile-queue")
    reconcile_queue.add_argument("--apply", action="store_true")
    _add_format(reconcile_queue)

    analytics = actions.add_parser("analytics")
    analytics.add_argument("--post-id")
    analytics.add_argument("--record", action="store_true")
    analytics.add_argument("--views", type=int, default=0)
    analytics.add_argument("--clicks", type=int, default=0)
    analytics.add_argument("--reactions", type=int, default=0)
    analytics.add_argument("--comments", type=int, default=0)
    analytics.add_argument("--conversion-to-bot", type=int, default=0)
    analytics.add_argument("--starts", type=int, default=0)
    analytics.add_argument("--first-photos", type=int, default=0)
    analytics.add_argument("--generations", type=int, default=0)
    analytics.add_argument("--payments", type=int, default=0)
    analytics.add_argument("--apply", action="store_true")
    _add_format(analytics)

    video = actions.add_parser("video")
    video.add_argument("--before", type=Path, required=True)
    video.add_argument("--after", type=Path, required=True)
    video.add_argument("--output", type=Path, required=True)
    video.add_argument("--hook", required=True)
    video.add_argument("--cta", default="Попробуйте Ravuna в MAX")
    video.add_argument("--apply", action="store_true")
    _add_format(video)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    try:
        settings = ContentStudioSettings.from_environment(PROJECT_ROOT)
        service = ContentStudioService(settings)
        result = _run_content(service, args)
    except (ValueError, LookupError, OSError, RuntimeError, sqlite3.Error) as error:
        LOGGER.error("Content Studio command failed: %s", error)
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    _print(result, args.format)
    if args.content_command == "publish-due" and result.get("failed"):
        return 1
    if args.content_command == "auto-run" and result.get("publication", {}).get("failed"):
        return 1
    return 0


def _run_content(service: ContentStudioService, args: argparse.Namespace) -> Any:
    command = args.content_command
    if command == "status":
        return service.status()
    if command == "auto-run":
        return service.full_auto_run(apply=args.apply)
    if command == "dashboard":
        if not 1 <= args.days <= 90:
            raise ValueError("dashboard days must be between 1 and 90")
        return service.growth_dashboard(days=args.days)
    if command == "permissions":
        return service.publishing_permissions()
    if command == "generate":
        asset_id = args.asset_id
        if args.source_image:
            if not args.title or not args.category:
                raise ValueError("--title and --category are required with --source-image")
            asset = service.add_asset(
                args.source_image,
                title=args.title,
                description=args.description,
                category=ContentCategory(args.category),
                tags=tuple(part.strip() for part in args.tags.split(",") if part.strip()),
            )
            asset_id = asset.id
        return service.generate(
            asset_id=asset_id,
            after_image=args.after_image,
            transformation_type=TransformationType(args.transformation),
            prompt_en=args.prompt_en,
            scene_intent=_json_object(args.scene_intent_json, "scene intent"),
            edit_plan=_json_object(args.edit_plan_json, "edit plan"),
            provider=args.provider,
            platform=args.platform,
            templates=tuple(args.template or ("square", "vertical", "stories")),
            reported_issues=tuple(QualityIssue(value) for value in args.quality_issue),
        )
    if command == "queue":
        return {"posts": service.repository.queue(args.status, args.limit)}
    if command == "approve":
        return service.approve(
            args.post_id,
            reviewer=args.reviewer,
            reason=args.reason,
            apply=args.apply,
        )
    if command == "schedule":
        if args.plan_start:
            if args.post_id or args.at:
                raise ValueError("--plan-start cannot be combined with --post-id/--at")
            return service.create_plan(args.plan_start, args.days, apply=args.apply)
        if not args.post_id or not args.at:
            raise ValueError("schedule requires --post-id and --at, or --plan-start")
        return service.schedule(args.post_id, args.at, apply=args.apply)
    if command == "publish":
        outcome = service.publication(
            args.post_id,
            mode=PublicationMode(args.mode),
            external_id=args.external_id,
            apply=args.apply,
        )
        return {
            "mode": outcome.mode.value,
            "status": outcome.status,
            "external_id": outcome.external_id,
            "payload": outcome.payload,
            "network_publication": outcome.mode in {PublicationMode.PUBLISH, PublicationMode.RETRY},
        }
    if command == "publish-due":
        return service.publish_due(limit=args.limit, apply=args.apply)
    if command == "reconcile-queue":
        return service.reconcile_full_auto_queue(apply=args.apply)
    if command == "analytics":
        if args.record:
            if not args.apply or not args.post_id:
                raise ValueError("recording analytics requires --post-id and --apply")
            return service.record_analytics(
                args.post_id,
                views=args.views,
                clicks=args.clicks,
                reactions=args.reactions,
                comments=args.comments,
                conversion_to_bot=args.conversion_to_bot,
                starts=args.starts,
                first_photos=args.first_photos,
                generations=args.generations,
                payments=args.payments,
            )
        return service.repository.analytics_summary(args.post_id)
    if command == "video":
        generator = VerticalVideoGenerator(
            service.settings.approved_assets_dir,
            service.settings.storage_dir,
        )
        spec = VerticalVideoSpec(
            before=args.before,
            after=args.after,
            output=args.output,
            hook=args.hook,
            cta=args.cta,
        )
        command_line = generator.command(spec)
        if not args.apply:
            return {
                "apply": False,
                "resolution": "1080x1920",
                "duration_seconds": spec.duration_seconds,
                "command": command_preview(command_line),
            }
        return {
            "apply": True,
            "output": str(generator.render(spec)),
            "resolution": "1080x1920",
            "duration_seconds": spec.duration_seconds,
        }
    raise ValueError("unsupported content command")


def _json_object(raw: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} must be valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _add_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("json", "human"), default="json")


def _values(enum_type: type[Any]) -> tuple[str, ...]:
    return tuple(item.value for item in enum_type)


def _print(value: Any, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
    else:
        print(value)


if __name__ == "__main__":
    raise SystemExit(main())
