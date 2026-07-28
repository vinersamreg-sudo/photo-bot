"""Pillow renderer for branded Ravuna before/after demonstration cards."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


@dataclass(frozen=True)
class CardTemplate:
    name: str
    width: int
    height: int
    layout: str


TEMPLATES = {
    "square": CardTemplate("square", 1080, 1080, "side_by_side"),
    "vertical": CardTemplate("vertical", 1080, 1350, "side_by_side"),
    "stories": CardTemplate("stories", 1080, 1920, "stacked"),
}


class BeforeAfterRenderer:
    background = "#F5F3EF"
    ink = "#171717"
    muted = "#6F6B65"
    accent = "#6257E8"
    panel = "#FFFFFF"

    def render(
        self,
        before_path: Path,
        after_path: Path,
        output_path: Path,
        *,
        template: str = "square",
        title: str = "Результат Ravuna",
    ) -> Path:
        if template not in TEMPLATES:
            raise ValueError(f"unknown renderer template: {template}")
        spec = TEMPLATES[template]
        before = _open_rgb(before_path)
        after = _open_rgb(after_path)
        canvas = Image.new("RGB", (spec.width, spec.height), self.background)
        draw = ImageDraw.Draw(canvas)
        title_font = _font(52)
        label_font = _font(34, bold=True)
        brand_font = _font(28, bold=True)
        small_font = _font(24)

        draw.text((64, 52), "RAVUNA AI", fill=self.accent, font=brand_font)
        draw.text((64, 102), title[:52], fill=self.ink, font=title_font)
        top = 190
        footer = 118
        gap = 22
        margin = 54
        if spec.layout == "side_by_side":
            panel_width = (spec.width - margin * 2 - gap) // 2
            panel_height = spec.height - top - footer
            boxes = (
                (margin, top, margin + panel_width, top + panel_height),
                (margin + panel_width + gap, top, spec.width - margin, top + panel_height),
            )
        else:
            panel_height = (spec.height - top - footer - gap) // 2
            boxes = (
                (margin, top, spec.width - margin, top + panel_height),
                (margin, top + panel_height + gap, spec.width - margin, spec.height - footer),
            )
        for image, label, box in zip((before, after), ("ДО", "ПОСЛЕ"), boxes, strict=True):
            draw.rounded_rectangle(box, radius=28, fill=self.panel)
            inner = (box[0] + 8, box[1] + 8, box[2] - 8, box[3] - 8)
            fitted = ImageOps.fit(
                image,
                (inner[2] - inner[0], inner[3] - inner[1]),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            mask = Image.new("L", fitted.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, fitted.width, fitted.height), radius=22, fill=255
            )
            canvas.paste(fitted, (inner[0], inner[1]), mask)
            label_box = draw.textbbox((0, 0), label, font=label_font)
            label_width = label_box[2] - label_box[0]
            pill = (box[0] + 22, box[1] + 22, box[0] + label_width + 66, box[1] + 74)
            draw.rounded_rectangle(pill, radius=18, fill=self.ink)
            draw.text((pill[0] + 22, pill[1] + 7), label, fill="white", font=label_font)

        footer_y = spec.height - 82
        draw.text((margin, footer_y), "ДЕМОНСТРАЦИОННЫЙ ПРИМЕР", fill=self.muted, font=small_font)
        marker = "RAVUNA • ДЕМО"
        marker_box = draw.textbbox((0, 0), marker, font=small_font)
        draw.text(
            (spec.width - margin - (marker_box[2] - marker_box[0]), footer_y),
            marker,
            fill=self.accent,
            font=small_font,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        canvas.save(temporary, format="PNG", optimize=True)
        temporary.replace(output_path)
        return output_path

    def thumbnail(self, source_path: Path, output_path: Path, size: int = 480) -> Path:
        image = _open_rgb(source_path)
        thumb = ImageOps.fit(image, (size, size), method=Image.Resampling.LANCZOS)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        thumb.save(temporary, format="WEBP", quality=82, method=6)
        temporary.replace(output_path)
        return output_path


def _open_rgb(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts") / ("arialbd.ttf" if bold else "arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu") / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/freefont") / ("FreeSansBold.ttf" if bold else "FreeSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default(size=size)
