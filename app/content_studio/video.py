"""Deterministic 1080x1920 before/after video rendering with FFmpeg."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class VerticalVideoSpec:
    before: Path
    after: Path
    output: Path
    hook: str
    cta: str = "Попробуйте Ravuna в MAX"
    duration_seconds: int = 13


@dataclass(frozen=True)
class VerticalPairVideoSpec:
    pair_card: Path
    output: Path
    hook: str
    prompt_text: str
    cta: str = "Попробуйте Ravuna в MAX"
    duration_seconds: int = 13


class VerticalVideoGenerator:
    def __init__(
        self,
        approved_assets_root: Path,
        output_root: Path,
        *,
        ffmpeg_binary: str = "ffmpeg",
        font_file: Path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ) -> None:
        self.approved_assets_root = approved_assets_root.resolve()
        self.output_root = output_root.resolve()
        self.ffmpeg_binary = ffmpeg_binary
        self.font_file = font_file

    def command(self, spec: VerticalVideoSpec) -> list[str]:
        before = self._approved_input(spec.before)
        after = self._approved_input(spec.after)
        output = spec.output.resolve()
        if not _inside(output, self.output_root):
            raise ValueError("vertical video output must stay inside Content Studio storage")
        if not 8 <= spec.duration_seconds <= 15:
            raise ValueError("vertical video duration must be between 8 and 15 seconds")
        hook = _drawtext(spec.hook, 90)
        cta = _drawtext(spec.cta, 90)
        font = str(self.font_file).replace("\\", "/").replace(":", "\\:")
        filter_graph = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,zoompan=z='min(zoom+0.00025,1.04)':d=1:"
            "s=1080x1920:fps=30,setpts=PTS-STARTPTS[b];"
            "[1:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,zoompan=z='min(zoom+0.00025,1.04)':d=1:"
            "s=1080x1920:fps=30,setpts=PTS-STARTPTS[a];"
            "[b][a]xfade=transition=fade:duration=0.5:offset=4[v];"
            f"[v]drawtext=fontfile='{font}':text='ДО':fontsize=86:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=28:x=(w-text_w)/2:y=120:"
            "enable='between(t,0,2)',"
            f"drawtext=fontfile='{font}':text='{hook}':fontsize=54:fontcolor=white:"
            "box=1:boxcolor=black@0.60:boxborderw=30:x=(w-text_w)/2:y=h*0.72:"
            "enable='between(t,2,4)',"
            f"drawtext=fontfile='{font}':text='ПОСЛЕ':fontsize=86:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=28:x=(w-text_w)/2:y=120:"
            "enable='between(t,4,10)',"
            f"drawtext=fontfile='{font}':text='{cta}':fontsize=58:fontcolor=white:"
            "box=1:boxcolor=black@0.68:boxborderw=32:x=(w-text_w)/2:y=h*0.78:"
            "enable='between(t,10,13)'[out]"
        )
        return [
            self.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-t",
            "4.5",
            "-i",
            str(before),
            "-loop",
            "1",
            "-t",
            "9",
            "-i",
            str(after),
            "-filter_complex",
            filter_graph,
            "-map",
            "[out]",
            "-t",
            str(spec.duration_seconds),
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            str(output),
        ]

    def render(self, spec: VerticalVideoSpec) -> Path:
        if shutil.which(self.ffmpeg_binary) is None:
            raise RuntimeError("FFmpeg is not installed; no video was generated")
        output = spec.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            self.command(spec),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0 or not output.is_file():
            output.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg could not generate the vertical video")
        return output

    def pair_command(self, spec: VerticalPairVideoSpec) -> list[str]:
        pair = self._approved_input(spec.pair_card)
        output = spec.output.resolve()
        if not _inside(output, self.output_root):
            raise ValueError("vertical video output must stay inside Content Studio storage")
        if not 8 <= spec.duration_seconds <= 15:
            raise ValueError("vertical video duration must be between 8 and 15 seconds")
        hook = _drawtext(spec.hook, 90)
        prompt = _drawtext(spec.prompt_text, 110)
        cta = _drawtext(spec.cta, 90)
        font = str(self.font_file).replace("\\", "/").replace(":", "\\:")
        filter_graph = (
            "[0:v]crop=iw/2:ih:x='if(lt(t,4),0,iw/2)':y=0,"
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,fps=30,"
            f"drawtext=fontfile='{font}':text='ДО':fontsize=86:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=28:x=(w-text_w)/2:y=120:"
            "enable='between(t,0,2)',"
            f"drawtext=fontfile='{font}':text='{hook}':fontsize=48:fontcolor=white:"
            "box=1:boxcolor=black@0.65:boxborderw=26:x=(w-text_w)/2:y=h*0.70:"
            "enable='between(t,0,2)',"
            f"drawtext=fontfile='{font}':text='{prompt}':fontsize=42:fontcolor=white:"
            "box=1:boxcolor=black@0.68:boxborderw=25:x=(w-text_w)/2:y=h*0.72:"
            "enable='between(t,2,4)',"
            f"drawtext=fontfile='{font}':text='ПОСЛЕ':fontsize=86:fontcolor=white:"
            "box=1:boxcolor=black@0.55:boxborderw=28:x=(w-text_w)/2:y=120:"
            "enable='between(t,4,10)',"
            f"drawtext=fontfile='{font}':text='{cta}':fontsize=54:fontcolor=white:"
            "box=1:boxcolor=black@0.70:boxborderw=30:x=(w-text_w)/2:y=h*0.78:"
            "enable='between(t,10,13)'[out]"
        )
        return [
            self.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-t",
            str(spec.duration_seconds),
            "-i",
            str(pair),
            "-filter_complex",
            filter_graph,
            "-map",
            "[out]",
            "-t",
            str(spec.duration_seconds),
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-threads",
            "1",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            str(output),
        ]

    def render_pair(self, spec: VerticalPairVideoSpec) -> Path:
        if shutil.which(self.ffmpeg_binary) is None:
            raise RuntimeError("FFmpeg is not installed; no video was generated")
        output = spec.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            self.pair_command(spec),
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0 or not output.is_file():
            output.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg could not generate the vertical video")
        return output

    def _approved_input(self, value: Path) -> Path:
        candidate = value.expanduser().resolve()
        if not candidate.is_file() or not _inside(candidate, self.approved_assets_root):
            raise ValueError("video inputs must be approved marketing assets")
        return candidate


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _drawtext(value: str, maximum: int) -> str:
    clean = " ".join(value.strip().split())
    if not clean or len(clean) > maximum:
        raise ValueError(f"video caption must contain 1..{maximum} characters")
    return (
        clean.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
        .replace("%", "\\%")
    )


def command_preview(command: Sequence[str]) -> str:
    """Privacy-safe command summary for owner review; omits absolute paths."""

    return " ".join(
        "<asset>" if Path(part).is_absolute() else part for part in command
    )
