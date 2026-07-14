"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Runtime settings and resolved application paths."""

    openai_api_key: str
    openai_image_model: str
    app_env: str
    base_dir: Path

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def temp_dir(self) -> Path:
        return self.base_dir / "temp"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "app.log"


def load_settings(
    env_file: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Load settings from an optional .env file and the process environment."""

    if environ is None:
        load_dotenv(dotenv_path=env_file or PROJECT_ROOT / ".env", override=False)
        values: Mapping[str, str] = os.environ
    else:
        values = environ

    base_dir_value = values.get("BASE_DIR", "").strip()
    base_dir = Path(base_dir_value).expanduser() if base_dir_value else PROJECT_ROOT

    return Settings(
        openai_api_key=values.get("OPENAI_API_KEY", "").strip(),
        openai_image_model=values.get("OPENAI_IMAGE_MODEL", "").strip(),
        app_env=values.get("APP_ENV", "production").strip() or "production",
        base_dir=base_dir.resolve(),
    )
