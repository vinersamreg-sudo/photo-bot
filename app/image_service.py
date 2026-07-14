"""Future image-processing service boundary.

Image editing is intentionally not implemented at the infrastructure-bootstrap
stage. This module only reserves a stable place for that functionality.
"""

from __future__ import annotations

from pathlib import Path


class ImageService:
    """Placeholder service with no commercial workflow or fake processing."""

    def __init__(self, temp_dir: Path) -> None:
        self.temp_dir = temp_dir
