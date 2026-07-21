"""Deterministic weekly content-plan generator."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from .models import ContentCategory, ContentPlanEntry


WEEKLY_ROTATION: tuple[tuple[str, ContentCategory | None], ...] = (
    ("before_after", ContentCategory.PORTRAIT),
    ("photo_tip", None),
    ("remove_objects", ContentCategory.REMOVE_OBJECT),
    ("replace_clothes", ContentCategory.REPLACE_CLOTHES),
    ("replace_background", ContentCategory.REPLACE_BACKGROUND),
    ("restoration", ContentCategory.RESTORE),
    ("best_case_of_week", None),
)


class ContentPlanGenerator:
    def generate(self, start_date: date, days: int = 7) -> list[ContentPlanEntry]:
        if days <= 0 or days > 366:
            raise ValueError("days must be between 1 and 366")
        entries: list[ContentPlanEntry] = []
        for offset in range(days):
            planned = start_date + timedelta(days=offset)
            content_type, category = WEEKLY_ROTATION[planned.weekday()]
            entries.append(
                ContentPlanEntry(
                    id=str(uuid4()),
                    planned_date=planned,
                    content_type=content_type,
                    category=category,
                )
            )
        return entries
