"""Fail if configured API secret values are present in the runtime log."""

from __future__ import annotations

from app.config import load_settings


def main() -> int:
    settings = load_settings()
    text = (
        settings.log_file.read_text(encoding="utf-8", errors="replace")
        if settings.log_file.exists()
        else ""
    )
    if any(
        secret and secret in text
        for secret in (settings.openai_api_key, settings.max_bot_token)
    ):
        print("Potential configured secret value detected in app.log")
        return 1
    print("Runtime log secret-value scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
