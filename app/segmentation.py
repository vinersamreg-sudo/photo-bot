"""Fail-closed local foreground segmentation adapters."""

from __future__ import annotations

import io
import hashlib
import os
import threading
from pathlib import Path
from typing import Protocol

from PIL import Image, UnidentifiedImageError

from app.domain import SegmentationFailedError


class ForegroundSegmenter(Protocol):
    name: str
    model: str

    def segment(self, source_path: Path) -> Image.Image: ...


def validate_mask(mask: Image.Image, expected_size: tuple[int, int]) -> Image.Image:
    normalized = mask.convert("L")
    if normalized.size != expected_size:
        raise SegmentationFailedError("Segmentation mask size does not match the source")
    low, high = normalized.getextrema()
    if high == 0:
        raise SegmentationFailedError("Segmentation mask is empty")
    if low == 255:
        raise SegmentationFailedError("Segmentation mask contains no background")
    return normalized


class RembgSegmenter:
    """CPU rembg adapter that never downloads model weights at request time."""

    name = "rembg"

    def __init__(
        self,
        model_dir: Path,
        model: str = "u2net_human_seg",
        expected_sha256: str = "",
    ) -> None:
        self.model_dir = model_dir.resolve()
        self.model = model
        self.model_path = self.model_dir / f"{model}.onnx"
        self.expected_sha256 = expected_sha256.lower()
        self._session = None
        self._lock = threading.Lock()

    def _require_model(self) -> None:
        if (
            not self.model_path.is_file()
            or self.model_path.is_symlink()
            or self.model_dir not in self.model_path.resolve().parents
        ):
            raise SegmentationFailedError(
                "The approved segmentation model is not provisioned"
            )
        digest = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        if not self.expected_sha256 or digest != self.expected_sha256:
            raise SegmentationFailedError(
                "The segmentation model checksum is not approved"
            )

    def _get_session(self):
        self._require_model()
        with self._lock:
            if self._session is None:
                try:
                    from rembg import new_session
                except ImportError as exc:
                    raise SegmentationFailedError(
                        "The optional local segmentation backend is not installed"
                    ) from exc
                # rembg resolves pre-provisioned models through U2NET_HOME. This is
                # configured once before its lazy session is created; no remote URL
                # or per-request user data is involved.
                os.environ["U2NET_HOME"] = str(self.model_dir)
                os.environ.pop("MODEL_CHECKSUM_DISABLED", None)
                self._session = new_session(
                    self.model, providers=["CPUExecutionProvider"]
                )
        return self._session

    def segment(self, source_path: Path) -> Image.Image:
        if not source_path.is_file() or source_path.is_symlink():
            raise SegmentationFailedError("Unsafe or missing segmentation source")
        try:
            with Image.open(source_path) as opened:
                expected_size = opened.size
            from rembg import remove
            output = remove(
                source_path.read_bytes(),
                session=self._get_session(),
                only_mask=True,
                post_process_mask=True,
                force_return_bytes=True,
            )
            with Image.open(io.BytesIO(output)) as opened_mask:
                mask = opened_mask.copy()
        except SegmentationFailedError:
            raise
        except (ImportError, OSError, RuntimeError, ValueError, UnidentifiedImageError) as exc:
            raise SegmentationFailedError("Local foreground segmentation failed") from exc
        return validate_mask(mask, expected_size)


class FixedMaskSegmenter:
    """Deterministic injected segmenter for tests and offline pipeline benchmarks."""

    name = "fixed-mask"
    model = "test-mask-v1"

    def __init__(self, mask: Image.Image, fail: bool = False) -> None:
        self.mask = mask.copy()
        self.fail = fail
        self.calls = 0

    def segment(self, source_path: Path) -> Image.Image:
        self.calls += 1
        if self.fail:
            raise SegmentationFailedError("Injected segmentation failure")
        with Image.open(source_path) as source:
            return validate_mask(self.mask, source.size)
