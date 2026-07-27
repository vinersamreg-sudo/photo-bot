from __future__ import annotations

import shutil
from pathlib import Path


SITE = Path(__file__).resolve().parents[1]
SOURCE = SITE / "public"
TARGET = SITE / "ravuna-public"
TEXT_SUFFIXES = {".css", ".html", ".json", ".svg", ".txt", ".webmanifest", ".xml"}
REPLACEMENTS = (
    ("https://pixoraai.ru", "https://ravuna.ru"),
    ("PIXORA", "RAVUNA"),
    ("Pixora", "Ravuna"),
    ("pixora", "ravuna"),
)


def transform(text: str) -> str:
    for source, target in REPLACEMENTS:
        text = text.replace(source, target)
    return text


def build() -> None:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)

    for source in SOURCE.rglob("*"):
        relative = source.relative_to(SOURCE)
        target = TARGET / relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() in TEXT_SUFFIXES:
            target.write_text(
                transform(source.read_text(encoding="utf-8")),
                encoding="utf-8",
                newline="\n",
            )
        else:
            shutil.copy2(source, target)

    leftovers: list[str] = []
    for target in TARGET.rglob("*"):
        if target.is_file() and target.suffix.lower() in TEXT_SUFFIXES:
            text = target.read_text(encoding="utf-8")
            if "Pixora" in text or "pixoraai.ru" in text:
                leftovers.append(str(target.relative_to(TARGET)))
    if leftovers:
        raise RuntimeError(f"Ravuna build contains Pixora references: {leftovers}")


if __name__ == "__main__":
    build()
