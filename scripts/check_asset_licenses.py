"""Fail-closed validation for the commercial background asset catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.background_assets import AssetPolicyError, BackgroundCatalog


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("assets/backgrounds/catalog.json"),
    )
    args = parser.parse_args()
    try:
        catalog = BackgroundCatalog.load(args.catalog)
        assets = catalog.list_active()
    except (AssetPolicyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"asset_catalog_status=failed kind={type(exc).__name__}")
        return 1

    summary = {
        "status": "ok",
        "active_assets": len(assets),
        "categories": sorted({asset.category for asset in assets}),
        "attribution_required": sum(asset.attribution_required for asset in assets),
    }
    print("asset_catalog_audit=" + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
