"""HTTP API package (FastAPI). See ``app.create_app`` and ``routes_*.py``."""

from .app import API_PREFIX, create_app

__all__ = ["API_PREFIX", "create_app"]
