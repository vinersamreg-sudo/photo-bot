"""Conservative quality gate with pluggable semantic issue input."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps, ImageStat

from .models import QualityAssessment, QualityIssue


class ContentQualityGate:
    """Rejects objective failures and records semantic reviewer/detector flags.

    Finger, eye and face defects cannot be claimed as machine-detected without a
    vision model. They enter through ``reported_issues`` and still block approval.
    Every new post remains ``needs_review`` even when this assessment is clean.
    """

    def assess(
        self,
        before_path: Path,
        after_path: Path,
        reported_issues: Iterable[QualityIssue] = (),
    ) -> QualityAssessment:
        issues = set(reported_issues)
        checks: dict[str, object] = {}
        try:
            before = _open_small(before_path)
            after = _open_small(after_path)
        except (OSError, ValueError):
            issues.add(QualityIssue.INVALID_IMAGE)
            return QualityAssessment(tuple(sorted(issues, key=str)), {"decoded": False})
        checks["decoded"] = True
        checks["before_size"] = list(before.size)
        checks["after_size"] = list(after.size)
        if min(after.size) < 512:
            issues.add(QualityIssue.TOO_SMALL)
        extrema = ImageStat.Stat(after.convert("L")).extrema[0]
        checks["luminance_range"] = int(extrema[1] - extrema[0])
        if extrema[1] - extrema[0] < 8:
            issues.add(QualityIssue.NEAR_UNIFORM)
        before_hash = _visual_hash(before)
        after_hash = _visual_hash(after)
        checks["visual_change_detected"] = before_hash != after_hash
        if before_hash == after_hash:
            issues.add(QualityIssue.UNCHANGED_RESULT)
        return QualityAssessment(tuple(sorted(issues, key=str)), checks)


def _open_small(path: Path) -> Image.Image:
    with Image.open(path) as image:
        result = ImageOps.exif_transpose(image).convert("RGB")
        result.thumbnail((768, 768), Image.Resampling.LANCZOS)
        return result.copy()


def _visual_hash(image: Image.Image) -> str:
    normalized = ImageOps.fit(image.convert("L"), (32, 32), method=Image.Resampling.LANCZOS)
    return hashlib.sha256(normalized.tobytes()).hexdigest()
