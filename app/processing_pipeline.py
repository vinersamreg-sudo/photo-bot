"""Local enhancement and licensed-background compositing execution."""

from __future__ import annotations

import io
import shutil
import tempfile
import time
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat

from app.background_assets import BackgroundCatalog
from app.domain import AssetUnavailableError, ProviderResult, SegmentationFailedError
from app.image_provider import ImageProvider
from app.processing_modes import ProcessingMode, ProcessingPlan
from app.segmentation import ForegroundSegmenter, validate_mask


class ProcessingExecutor(Protocol):
    def execute(
        self, source_path: Path, prompt: str, processing_plan: ProcessingPlan
    ) -> ProviderResult: ...


def cleanup_processing_temp(
    temp_dir: Path, *, older_than_seconds: int = 3600, now: float | None = None
) -> tuple[Path, ...]:
    """Remove only known stale processing artifacts without following symlinks."""

    if not temp_dir.exists():
        return ()
    root = temp_dir.resolve()
    cutoff = (time.time() if now is None else now) - older_than_seconds
    removed: list[Path] = []
    for candidate in temp_dir.iterdir():
        if not candidate.name.startswith(("pixora-mask-", "pixora-composite-")):
            continue
        if candidate.is_symlink() or candidate.stat().st_mtime > cutoff:
            continue
        resolved = candidate.resolve()
        if root not in resolved.parents:
            continue
        if candidate.is_dir():
            shutil.rmtree(candidate)
        elif candidate.is_file():
            candidate.unlink()
        removed.append(candidate)
    return tuple(removed)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _orientation(size: tuple[int, int]) -> str:
    width, height = size
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"


class HybridProcessingExecutor:
    """Execute a routed plan without changing quota or delivery boundaries."""

    def __init__(
        self,
        provider: ImageProvider,
        catalog: BackgroundCatalog,
        segmenter: ForegroundSegmenter | None,
        temp_dir: Path,
    ) -> None:
        self.provider = provider
        self.catalog = catalog
        self.segmenter = segmenter
        self.temp_dir = temp_dir
        temp_dir.mkdir(parents=True, exist_ok=True)

    def execute(
        self, source_path: Path, prompt: str, processing_plan: ProcessingPlan
    ) -> ProviderResult:
        mode = processing_plan.selected_mode
        if mode in {
            ProcessingMode.AI_GENERATION,
            ProcessingMode.LOCAL_AI_EDIT,
            ProcessingMode.RESTORATION,
        }:
            return self.provider.edit(source_path, prompt)
        if mode == ProcessingMode.ENHANCEMENT:
            return self._enhance(source_path)
        if mode == ProcessingMode.REAL_BACKGROUND_COMPOSITE:
            return self._composite(source_path, processing_plan)
        raise ValueError("Unsupported processing mode")

    def _enhance(self, source_path: Path) -> ProviderResult:
        if not source_path.is_file() or source_path.is_symlink():
            raise SegmentationFailedError("Unsafe or missing enhancement source")
        with Image.open(source_path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        result = image.filter(
            ImageFilter.UnsharpMask(radius=1.1, percent=75, threshold=4)
        )
        result = ImageEnhance.Contrast(result).enhance(1.02)
        content = _png_bytes(result)
        return ProviderResult(
            image_bytes=content,
            request_id=f"local-enhance-{uuid4().hex}",
            usage={"backend": "local-pillow", "external_calls": 0},
            estimated_cost_rub=0.0,
        )

    def _composite(
        self, source_path: Path, processing_plan: ProcessingPlan
    ) -> ProviderResult:
        if processing_plan.ai_finishing:
            raise AssetUnavailableError(
                "AI finishing is disabled until masked preservation is visually approved"
            )
        if not processing_plan.asset_id or self.segmenter is None:
            raise AssetUnavailableError(
                "No approved background asset or segmentation backend is available"
            )
        asset = self.catalog.get(processing_plan.asset_id)
        background_path = self.catalog.verify_file(asset)
        mask = self.segmenter.segment(source_path)

        with Image.open(source_path) as opened_source:
            source = ImageOps.exif_transpose(opened_source).convert("RGBA")
        mask = validate_mask(mask, source.size)

        # Temporary masks are deliberately materialized so cleanup is testable;
        # TemporaryDirectory removes them after both success and exceptions.
        with tempfile.TemporaryDirectory(
            prefix="pixora-mask-", dir=self.temp_dir
        ) as temporary:
            mask_path = Path(temporary) / "foreground-mask.png"
            mask.save(mask_path, format="PNG")
            with Image.open(mask_path) as checked:
                checked_mask = validate_mask(checked, source.size)
            result = self._compose_images(source, checked_mask, background_path)

        content = _png_bytes(result)
        return ProviderResult(
            image_bytes=content,
            request_id=f"local-composite-{uuid4().hex}",
            usage={
                "backend": "local-composite",
                "external_calls": 0,
                "asset_id": asset.id,
                "mask_strategy": processing_plan.mask_strategy.value,
            },
            estimated_cost_rub=0.0,
        )

    @staticmethod
    def _compose_images(
        source: Image.Image, mask: Image.Image, background_path: Path
    ) -> Image.Image:
        with Image.open(background_path) as opened_background:
            background = ImageOps.exif_transpose(opened_background).convert("RGB")

        fitted = ImageOps.fit(
            background,
            source.size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        source_luma = ImageStat.Stat(source.convert("L")).mean[0]
        background_luma = max(1.0, ImageStat.Stat(fitted.convert("L")).mean[0])
        brightness = max(0.85, min(1.15, source_luma / background_luma))
        fitted = ImageEnhance.Brightness(fitted).enhance(brightness)

        feather_radius = max(0.8, min(source.size) / 900)
        feathered = mask.filter(ImageFilter.GaussianBlur(radius=feather_radius))
        subject = source.copy()
        subject.putalpha(feathered)
        canvas = fitted.convert("RGBA")
        canvas.alpha_composite(subject)
        result = canvas.convert("RGB")
        if result.size != source.size or _orientation(result.size) != _orientation(source.size):
            raise RuntimeError("Composite geometry changed unexpectedly")
        return result
