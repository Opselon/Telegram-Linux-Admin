"""Localization facade: the legacy import path now resolves to the i18n layer.

All catalogs live in :mod:`src.presentation.i18n.catalog` (one JSON file per
language); this module stays so imports like ``from src.localization import
translate`` keep working for handlers and tests.
"""

from __future__ import annotations

from src.presentation.i18n import (  # noqa: F401
    DEFAULT_LANGUAGE,
    LANGUAGE_LABELS,
    SUPPORTED_LANGUAGES,
    SUPPORTED_LANGUAGE_SET,
    get_language_label,
    is_rtl,
    supported_languages,
    translate,
)

__all__ = [
    "DEFAULT_LANGUAGE",
    "LANGUAGE_LABELS",
    "SUPPORTED_LANGUAGES",
    "SUPPORTED_LANGUAGE_SET",
    "get_language_label",
    "is_rtl",
    "supported_languages",
    "translate",
]
