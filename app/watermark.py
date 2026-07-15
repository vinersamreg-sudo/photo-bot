"""Generate reduced, visibly watermarked demo previews."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "DejaVuSans-Bold.ttf",
)


def _font(size: int, sample: str) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(candidate, size=size)
            if font.getmask(sample).getbbox():
                return font
        except (OSError, UnicodeError):
            continue
    raise RuntimeError("No installed font with Cyrillic support is available")


class WatermarkService:
    def __init__(self, text: str, max_dimension: int, output_format: str, jpeg_quality: int) -> None:
        self.text = text
        self.max_dimension = max_dimension
        self.output_format = output_format
        self.jpeg_quality = jpeg_quality

    @property
    def extension(self) -> str:
        return ".jpg" if self.output_format == "JPEG" else ".webp"

    def create_preview(self, original_path: Path, preview_path: Path, technical_id: str) -> None:
        with Image.open(original_path) as opened:
            image = opened.convert("RGB")
        image.thumbnail((self.max_dimension, self.max_dimension), Image.Resampling.LANCZOS)

        font_size = max(30, min(image.size) // 7)
        font = _font(font_size, self.text)
        tile = Image.new("RGBA", (max(image.size) * 2, max(image.size)), (0, 0, 0, 0))
        draw = ImageDraw.Draw(tile)
        bbox = draw.textbbox((0, 0), self.text, font=font, stroke_width=2)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        step_x = max(text_width + font_size, image.width // 2)
        step_y = max(text_height + font_size * 2, image.height // 3)
        for y in range(-tile.height, tile.height * 2, step_y):
            for x in range(-tile.width, tile.width * 2, step_x):
                draw.text(
                    (x, y),
                    self.text,
                    font=font,
                    fill=(255, 255, 255, 105),
                    stroke_width=2,
                    stroke_fill=(20, 20, 20, 90),
                )
        tile = tile.rotate(28, expand=False, resample=Image.Resampling.BICUBIC)
        overlay = tile.crop((0, 0, image.width, image.height))
        composed = Image.alpha_composite(image.convert("RGBA"), overlay)

        small_font = _font(max(14, min(image.size) // 35), "ДЕМО")
        bottom = ImageDraw.Draw(composed)
        label = f"ДЕМО · {technical_id[:8]}"
        label_box = bottom.textbbox((0, 0), label, font=small_font)
        bottom.rectangle(
            (8, image.height - (label_box[3] - label_box[1]) - 18, label_box[2] + 18, image.height - 8),
            fill=(0, 0, 0, 150),
        )
        bottom.text((13, image.height - (label_box[3] - label_box[1]) - 14), label, font=small_font, fill="white")

        output = io.BytesIO()
        composed.convert("RGB").save(
            output,
            format=self.output_format,
            quality=self.jpeg_quality,
            optimize=True,
        )
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(output.getvalue())
        try:
            preview_path.chmod(0o600)
        except OSError:
            pass
