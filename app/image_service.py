"""Composition root for the demo image workflow."""

from __future__ import annotations

from typing import Any, Optional

from app.config import Settings
from app.background_assets import BackgroundCatalog
from app.database import Database
from app.demo_service import DemoService, DeliverPreview
from app.image_provider import FakeImageProvider, OpenAIImageProvider
from app.processing_pipeline import HybridProcessingExecutor
from app.processing_router import ModeRouter
from app.segmentation import RembgSegmenter
from app.openai_client import create_openai_client
from app.storage import PrivateStorage
from app.watermark import WatermarkService


def build_demo_service(
    settings: Settings,
    provider_name: str = "openai",
    deliver_preview: Optional[DeliverPreview] = None,
    client: Any = None,
) -> DemoService:
    if provider_name == "fake":
        provider = FakeImageProvider()
    elif provider_name == "openai":
        provider = OpenAIImageProvider(
            client or create_openai_client(settings),
            settings.openai_image_model,
            quality=settings.image_edit_quality,
            size=settings.image_edit_size,
            input_fidelity=settings.image_edit_input_fidelity,
            output_format=settings.image_edit_output_format,
        )
    else:
        raise ValueError("provider must be 'openai' or 'fake'")
    processing_router = None
    processing_executor = None
    if settings.processing_mode_router_enabled and provider_name == "openai":
        catalog = BackgroundCatalog.load(settings.background_asset_catalog_path)
        segmenter = (
            RembgSegmenter(
                settings.segmentation_model_dir_path,
                settings.segmentation_model,
                settings.segmentation_model_sha256,
            )
            if settings.segmentation_backend == "rembg"
            else None
        )
        processing_router = ModeRouter(
            catalog,
            provider_name=provider.name,
            provider_model=provider.model,
            real_background_enabled=(
                settings.real_background_composite_enabled and segmenter is not None
            ),
            allow_ai_background_fallback=settings.allow_ai_background_fallback,
        )
        processing_executor = HybridProcessingExecutor(
            provider, catalog, segmenter, settings.temp_dir
        )
    return DemoService(
        settings,
        Database(settings.database_path),
        PrivateStorage(
            settings.users_dir,
            settings.max_source_file_size_mb * 1024 * 1024,
        ),
        WatermarkService(
            settings.demo_watermark_text,
            settings.demo_max_dimension,
            settings.demo_output_format,
            settings.demo_jpeg_quality,
        ),
        provider,
        deliver_preview=deliver_preview,
        processing_router=processing_router,
        processing_executor=processing_executor,
    )
