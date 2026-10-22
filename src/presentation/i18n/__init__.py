"""i18n: JSON-backed catalogs, one file per language under ``catalog/``.

``en.json`` is the source of truth; every other language must expose the
identical key set (enforced by tests).  Catalogs load lazily and are cached
for the lifetime of the process; a language line wraps transparently to the
default catalog when a key is somehow missing, and finally to the key itself
so a typo never renders a blank screen in production.

Files are read through :mod:`importlib.resources` (with a filesystem
fallback) so the catalogs also resolve inside a PyInstaller ``--onefile``
bundle and from a wheel installed into ``site-packages``.
"""

from __future__ import annotations

import json
import threading
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Tuple

_RESOURCE_ERRORS = (FileNotFoundError, NotADirectoryError, ModuleNotFoundError, OSError, TypeError)

try:
    _pkg: Any = resources.files(__package__)
except _RESOURCE_ERRORS:  # pragma: no cover - exotic importers fall back to disk
    _pkg = Path(__file__).resolve().parent

CATALOG_DIR: Path = Path(__file__).resolve().parent / "catalog"

_lock = threading.Lock()
_catalogs: Dict[str, Dict[str, str]] = {}


def _read_text(name: str) -> str:
    """Reads a bundled JSON file through resources, falling back to disk."""
    try:
        return (_pkg / name).read_text(encoding="utf-8")
    except _RESOURCE_ERRORS:  # pragma: no cover - frozen/zip fallback
        return (Path(__file__).resolve().parent / name).read_text(encoding="utf-8")


def _cast_json(raw: str) -> Any:
    return json.loads(raw)


_config: dict[str, Any] = _cast_json(_read_text("languages.json"))

DEFAULT_LANGUAGE: str = _config.get("default", "en")
RTL_LANGUAGES: frozenset[str] = frozenset(_config.get("rtl", ()))
LANGUAGE_LABELS: Dict[str, str] = dict(_config["languages"])


def _load(language: str) -> Dict[str, str]:
    with _lock:
        cached = _catalogs.get(language)
        if cached is None:
            cached = dict(_cast_json(_read_text(f"catalog/{language}.json")))
            _catalogs[language] = cached
        return cached


def supported_languages() -> Tuple[str, ...]:
    """All language codes that ship a catalog file."""
    return tuple(sorted(LANGUAGE_LABELS))


SUPPORTED_LANGUAGES: Tuple[str, ...] = supported_languages()
SUPPORTED_LANGUAGE_SET = frozenset(SUPPORTED_LANGUAGES)


def translate(key: str, language: str | None = None, **kwargs: str) -> str:
    """Returns *key* rendered for *language*, degrading gracefully to English.

    A catalog can be edited by translators at any time, so a template whose
    placeholders do not match the caller's kwargs must never crash a handler:
    we fall back to the English template and, failing that, to the raw string.
    """
    lang = language if language in SUPPORTED_LANGUAGE_SET else DEFAULT_LANGUAGE
    template = _load(lang).get(key)
    if template is None:
        template = _load(DEFAULT_LANGUAGE).get(key, key)
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        fallback = _load(DEFAULT_LANGUAGE).get(key)
        if fallback is not None and fallback != template:
            try:
                return fallback.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return fallback
        return template


def get_language_label(language: str) -> str:
    """Returns a user-facing label for a language code."""
    return LANGUAGE_LABELS.get(language, language)


def is_rtl(language: str) -> bool:
    return language in RTL_LANGUAGES
