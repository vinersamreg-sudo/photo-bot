"""Commercial-license-aware local background asset catalog."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from app.processing_modes import AssetSourceType


ALLOWED_LICENSE_TYPES = {
    "pixora_owned",
    "commercial_license",
    "purchased",
    "user_provided",
    "synthetic_test",
}


class AssetPolicyError(ValueError):
    """Raised when an asset cannot prove safe commercial provenance."""


@dataclass(frozen=True)
class BackgroundAsset:
    id: str
    title: str
    category: str
    tags: tuple[str, ...]
    location_type: str
    orientation: str
    aspect_ratio: float
    dominant_lighting: str
    time_of_day: str
    weather: str
    horizon_position: float
    source_type: AssetSourceType
    source_reference: str
    license_type: str
    license_record: str
    commercial_use_allowed: bool
    attribution_required: bool
    file_checksum: str
    file_name: str
    active: bool
    created_at: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BackgroundAsset":
        return cls(
            id=str(value.get("id") or "").strip(),
            title=str(value.get("title") or "").strip(),
            category=str(value.get("category") or "").strip(),
            tags=tuple(str(item).strip() for item in value.get("tags") or ()),
            location_type=str(value.get("location_type") or "").strip(),
            orientation=str(value.get("orientation") or "").strip(),
            aspect_ratio=float(value.get("aspect_ratio") or 0),
            dominant_lighting=str(value.get("dominant_lighting") or "").strip(),
            time_of_day=str(value.get("time_of_day") or "").strip(),
            weather=str(value.get("weather") or "").strip(),
            horizon_position=float(value.get("horizon_position", 0.5)),
            source_type=AssetSourceType(str(value.get("source_type") or "none")),
            source_reference=str(value.get("source_reference") or "").strip(),
            license_type=str(value.get("license_type") or "").strip(),
            license_record=str(value.get("license_record") or "").strip(),
            commercial_use_allowed=bool(value.get("commercial_use_allowed", False)),
            attribution_required=bool(value.get("attribution_required", False)),
            file_checksum=str(value.get("file_checksum") or "").strip().lower(),
            file_name=str(value.get("file_name") or "").strip(),
            active=bool(value.get("active", False)),
            created_at=str(value.get("created_at") or "").strip(),
        )

    def validate_metadata(self) -> None:
        if not self.id or not self.category or not self.file_name:
            raise AssetPolicyError("Asset id, category and file_name are required")
        if self.license_type not in ALLOWED_LICENSE_TYPES:
            raise AssetPolicyError("Asset license type is not approved")
        if not self.commercial_use_allowed or not self.license_record:
            raise AssetPolicyError("Asset lacks a commercial-use license record")
        if self.source_type not in {
            AssetSourceType.PIXORA_OWNED,
            AssetSourceType.USER_UPLOADED,
            AssetSourceType.LICENSED_STOCK,
            AssetSourceType.PURCHASED,
            AssetSourceType.SYNTHETIC_TEST,
        }:
            raise AssetPolicyError("Asset source type is not approved")
        if self.orientation not in {"portrait", "landscape", "square"}:
            raise AssetPolicyError("Asset orientation is invalid")
        if self.aspect_ratio <= 0 or not 0 <= self.horizon_position <= 1:
            raise AssetPolicyError("Asset geometry metadata is invalid")
        if len(self.file_checksum) != 64 or any(
            char not in "0123456789abcdef" for char in self.file_checksum
        ):
            raise AssetPolicyError("Asset SHA-256 checksum is invalid")


class BackgroundCatalog:
    def __init__(self, manifest_path: Path, assets: Iterable[BackgroundAsset]) -> None:
        self.manifest_path = manifest_path.resolve()
        self.root = self.manifest_path.parent.resolve()
        self._assets = {asset.id: asset for asset in assets}

    @classmethod
    def empty(cls, manifest_path: Path) -> "BackgroundCatalog":
        return cls(manifest_path, ())

    @classmethod
    def load(cls, manifest_path: Path) -> "BackgroundCatalog":
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise AssetPolicyError("Background catalog is missing or is a symlink")
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or int(value.get("schema_version", 0)) != 1:
            raise AssetPolicyError("Unsupported background catalog schema")
        raw_assets = value.get("assets") or []
        if not isinstance(raw_assets, list):
            raise AssetPolicyError("Background catalog assets must be a list")
        assets = [BackgroundAsset.from_dict(item) for item in raw_assets]
        catalog = cls(manifest_path, assets)
        for asset in assets:
            asset.validate_metadata()
            catalog.verify_file(asset)
        return catalog

    def list_active(self) -> tuple[BackgroundAsset, ...]:
        return tuple(asset for asset in self._assets.values() if asset.active)

    def get(self, asset_id: str) -> BackgroundAsset:
        try:
            asset = self._assets[asset_id]
        except KeyError as exc:
            raise AssetPolicyError("Unknown background asset") from exc
        if not asset.active:
            raise AssetPolicyError("Background asset is inactive")
        asset.validate_metadata()
        self.verify_file(asset)
        return asset

    def asset_path(self, asset: BackgroundAsset) -> Path:
        relative = Path(asset.file_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise AssetPolicyError("Asset path traversal is forbidden")
        path = (self.root / relative).resolve()
        if self.root not in path.parents or path.is_symlink() or not path.is_file():
            raise AssetPolicyError("Asset file is outside the catalog or unsafe")
        return path

    def verify_file(self, asset: BackgroundAsset) -> Path:
        path = self.asset_path(asset)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != asset.file_checksum:
            raise AssetPolicyError("Background asset checksum mismatch")
        return path

    def match(
        self,
        category: str,
        *,
        orientation: Optional[str] = None,
        aspect_ratio: Optional[float] = None,
        dominant_lighting: Optional[str] = None,
        horizon_position: Optional[float] = None,
    ) -> Optional[BackgroundAsset]:
        candidates = [
            asset for asset in self.list_active()
            if asset.category == category and asset.commercial_use_allowed
        ]
        if not candidates:
            return None

        def score(asset: BackgroundAsset) -> tuple[float, str]:
            orientation_penalty = 0.0 if not orientation or asset.orientation == orientation else 1.0
            ratio_penalty = (
                abs(asset.aspect_ratio - aspect_ratio) if aspect_ratio else 0.0
            )
            lighting_penalty = (
                0.0
                if not dominant_lighting or asset.dominant_lighting == dominant_lighting
                else 0.35
            )
            horizon_penalty = (
                abs(asset.horizon_position - horizon_position)
                if horizon_position is not None else 0.0
            )
            return (
                orientation_penalty + ratio_penalty + lighting_penalty + horizon_penalty,
                asset.id,
            )

        selected = sorted(candidates, key=score)[0]
        selected.validate_metadata()
        self.verify_file(selected)
        return selected
