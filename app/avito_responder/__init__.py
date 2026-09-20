"""Isolated first-response bridge for Avito Messenger."""

from .config import AvitoResponderSettings
from .service import AvitoResponderService

__all__ = ["AvitoResponderService", "AvitoResponderSettings"]
