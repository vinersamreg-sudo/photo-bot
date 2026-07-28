"""Offline benchmark for Ravuna's local composite and enhancement stages.

The benchmark creates procedural fixtures in a temporary directory. It never
downloads assets, loads user data, or invokes an external image provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw

from app.background_assets import BackgroundCatalog
from app.processing_modes import (
    AssetSourceType,
    MaskStrategy,
    ProcessingMode,
    ProcessingPlan,
)
from app.processing_pipeline import HybridProcessingExecutor
from app.segmentation import FixedMaskSegmenter


class ForbiddenProvider:
    name = "forbidden"
    model = "forbidden"

    def edit(self, source_path: Path, prompt: str):  # pragma: no cover - safety tripwire
        raise AssertionError("offline benchmark attempted an external provider call")


def _write_fixtures(root: Path, size: int) -> tuple[Path, Path, Path]:
    source_path = root / "source.png"
    background_path = root / "background.png"
    mask_path = root / "mask.png"

    source = Image.new("RGB", (size, size), "#d9c0a2")
    draw = ImageDraw.Draw(source)
    draw.ellipse((size * .31, size * .08, size * .69, size * .46), fill="#b97e61")
    draw.rounded_rectangle(
        (size * .23, size * .38, size * .77, size * .98),
        radius=size * .08,
        fill="#385166",
    )
    source.save(source_path)

    background = Image.new("RGB", (size * 3 // 2, size), "#87b8dc")
    draw = ImageDraw.Draw(background)
    draw.polygon(
        [(0, size), (size * .3, size * .32), (size * .55, size), (size, size * .2),
         (size * 1.3, size), (size * 1.5, size * .42), (size * 1.5, size)],
        fill="#52685b",
    )
    background.save(background_path)

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((size * .29, size * .06, size * .71, size * .49), fill=255)
    draw.rounded_rectangle(
        (size * .2, size * .35, size * .8, size), radius=size * .1, fill=255
    )
    mask.save(mask_path)
    return source_path, background_path, mask_path


def run(iterations: int, size: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ravuna-benchmark-") as temporary:
        root = Path(temporary)
        source_path, background_path, mask_path = _write_fixtures(root, size)
        checksum = hashlib.sha256(background_path.read_bytes()).hexdigest()
        manifest = root / "catalog.json"
        manifest.write_text(json.dumps({
            "schema_version": 1,
            "assets": [{
                "id": "procedural-owned-benchmark",
                "title": "Procedural benchmark background",
                "category": "rocky_mountains",
                "tags": ["benchmark"],
                "location_type": "procedural",
                "orientation": "landscape",
                "aspect_ratio": 1.5,
                "dominant_lighting": "daylight",
                "time_of_day": "day",
                "weather": "clear",
                "horizon_position": 0.5,
                "source_type": "synthetic_test",
                "source_reference": "generated-in-process",
                "license_type": "synthetic_test",
                "license_record": "Ravuna procedural benchmark fixture",
                "commercial_use_allowed": True,
                "attribution_required": False,
                "file_checksum": checksum,
                "file_name": background_path.name,
                "active": True,
                "created_at": "2026-07-16T00:00:00Z"
            }]
        }), encoding="utf-8")
        catalog = BackgroundCatalog.load(manifest)
        with Image.open(mask_path) as opened:
            fixed_mask = opened.copy()
        executor = HybridProcessingExecutor(
            ForbiddenProvider(), catalog, FixedMaskSegmenter(fixed_mask), root / "temp"
        )
        composite_plan = ProcessingPlan(
            selected_mode=ProcessingMode.REAL_BACKGROUND_COMPOSITE,
            mode_reason="offline resource benchmark",
            confidence=1,
            fallback_mode=None,
            asset_source_type=AssetSourceType.SYNTHETIC_TEST,
            asset_id="procedural-owned-benchmark",
            asset_checksum=checksum,
            mask_strategy=MaskStrategy.LOCAL_REMBG_U2NET_HUMAN,
            provider="local-composite",
            provider_model="fixed-mask+pillow-benchmark",
        )
        enhancement_plan = ProcessingPlan(
            selected_mode=ProcessingMode.ENHANCEMENT,
            mode_reason="offline resource benchmark",
            confidence=1,
            fallback_mode=None,
            asset_source_type=AssetSourceType.NONE,
            asset_id=None,
            asset_checksum=None,
            mask_strategy=MaskStrategy.NONE,
            provider="local-pillow",
            provider_model="pillow-enhancement-v1",
        )

        timings: dict[str, list[float]] = {"composite": [], "enhancement": []}
        output_sizes: dict[str, int] = {}
        tracemalloc.start()
        for name, plan in (("composite", composite_plan), ("enhancement", enhancement_plan)):
            for _ in range(iterations):
                started = time.perf_counter()
                result = executor.execute(source_path, "offline benchmark", plan)
                timings[name].append((time.perf_counter() - started) * 1000)
                output_sizes[name] = len(result.image_bytes)
                assert result.usage.get("external_calls") == 0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        return {
            "status": "ok",
            "fixture": f"procedural-{size}x{size}",
            "iterations": iterations,
            "external_provider_calls": 0,
            "segmentation_backend": "fixed-mask (rembg not benchmarked)",
            "mean_ms": {key: round(statistics.mean(value), 2) for key, value in timings.items()},
            "p95_ms": {
                key: round(sorted(value)[max(0, math.ceil(len(value) * .95) - 1)], 2)
                for key, value in timings.items()
            },
            "output_bytes": output_sizes,
            "python_peak_memory_mb": round(peak / 1024 / 1024, 2),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--size", type=int, default=1024)
    args = parser.parse_args()
    if args.iterations < 1 or args.size < 256:
        raise SystemExit("iterations must be positive and size must be at least 256")
    print("processing_benchmark=" + json.dumps(run(args.iterations, args.size), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
