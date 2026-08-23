"""Deterministic Ravuna growth content-plan generator."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from .models import ContentCategory, ContentPlanEntry


CONTENT_ROTATION: tuple[tuple[str, ContentCategory | None], ...] = (
    ("before_after", ContentCategory.PORTRAIT),
    ("one_prompt_result", None),
    ("replace_background", ContentCategory.REPLACE_BACKGROUND),
    ("replace_clothes", ContentCategory.REPLACE_CLOTHES),
    ("remove_objects", ContentCategory.REMOVE_OBJECT),
    ("old_photo_improvement", ContentCategory.OLD_PHOTO),
    ("restoration", ContentCategory.RESTORE),
    ("quality_improvement", ContentCategory.UPSCALE),
    ("useful_prompts", None),
    ("common_user_mistakes", None),
)


class ContentPlanGenerator:
    def generate(self, start_date: date, days: int = 7) -> list[ContentPlanEntry]:
        if days <= 0 or days > 366:
            raise ValueError("days must be between 1 and 366")
        entries: list[ContentPlanEntry] = []
        for offset in range(days):
            planned = start_date + timedelta(days=offset)
            content_type, category = CONTENT_ROTATION[offset % len(CONTENT_ROTATION)]
            entries.append(
                ContentPlanEntry(
                    id=str(uuid4()),
                    planned_date=planned,
                    content_type=content_type,
                    category=category,
                )
            )
        return entries
