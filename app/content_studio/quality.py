"""Conservative quality gate with pluggable semantic issue input."""

from __future__ import annotations

import hashlib
import json
import subprocess
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

    def assess_pair_card(self, pair_path: Path) -> QualityAssessment:
        """Validate a synthetic side-by-side source without extracting customer media."""

        issues: set[QualityIssue] = set()
        checks: dict[str, object] = {"pair_card": True}
        try:
            with Image.open(pair_path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
        except (OSError, ValueError):
            return QualityAssessment((QualityIssue.INVALID_IMAGE,), {"decoded": False})
        checks["decoded"] = True
        checks["size"] = list(image.size)
        width, height = image.size
        if width < 1024 or height < 768:
            issues.add(QualityIssue.TOO_SMALL)
        if width < 2:
            issues.add(QualityIssue.INVALID_IMAGE)
            return QualityAssessment(tuple(sorted(issues, key=str)), checks)
        split = width // 2
        left = image.crop((0, 0, split, height))
        right = image.crop((width - split, 0, width, height))
        left.thumbnail((512, 512), Image.Resampling.LANCZOS)
        right.thumbnail((512, 512), Image.Resampling.LANCZOS)
        left_hash = _visual_hash(left)
        right_hash = _visual_hash(right)
        checks["visual_change_detected"] = left_hash != right_hash
        if left_hash == right_hash:
            issues.add(QualityIssue.UNCHANGED_RESULT)
        for key, half in (("before", left), ("after", right)):
            extrema = ImageStat.Stat(half.convert("L")).extrema[0]
            checks[f"{key}_luminance_range"] = int(extrema[1] - extrema[0])
            if extrema[1] - extrema[0] < 8:
                issues.add(QualityIssue.NEAR_UNIFORM)
        return QualityAssessment(tuple(sorted(issues, key=str)), checks)

    def assess_video(self, video_path: Path, *, ffprobe_binary: str = "ffprobe") -> dict[str, object]:
        """Decode-probe the rendered short; raises a safe error when invalid."""

        completed = subprocess.run(
            [
                ffprobe_binary,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height:format=duration",
                "-of",
                "json",
                str(video_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError("rendered video could not be decoded")
        try:
            value = json.loads(completed.stdout)
            stream = value["streams"][0]
            duration = float(value["format"]["duration"])
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("rendered video metadata is invalid") from error
        if stream.get("codec_name") != "h264":
            raise ValueError("rendered video must use H.264")
        if (int(stream.get("width", 0)), int(stream.get("height", 0))) != (1080, 1920):
            raise ValueError("rendered video must be 1080x1920")
        if not 8.0 <= duration <= 15.1:
            raise ValueError("rendered video duration must be 8..15 seconds")
        return {
            "decoded": True,
            "codec": "h264",
            "width": 1080,
            "height": 1920,
            "duration_seconds": round(duration, 3),
        }


def _open_small(path: Path) -> Image.Image:
    with Image.open(path) as image:
        result = ImageOps.exif_transpose(image).convert("RGB")
        result.thumbnail((768, 768), Image.Resampling.LANCZOS)
        return result.copy()


def _visual_hash(image: Image.Image) -> str:
    normalized = ImageOps.fit(image.convert("L"), (32, 32), method=Image.Resampling.LANCZOS)
    return hashlib.sha256(normalized.tobytes()).hexdigest()
