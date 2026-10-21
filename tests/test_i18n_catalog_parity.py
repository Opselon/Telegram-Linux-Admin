"""Catalog parity and rendering guarantees for all shipped languages.

The project promises 1:1 parity: every language must expose exactly the same
keys and the same placeholders as English, so a translation can never silently
fall back to English (or to a raw key) at runtime.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.presentation.i18n import (
    CATALOG_DIR,
    DEFAULT_LANGUAGE,
    RTL_LANGUAGES,
    SUPPORTED_LANGUAGES,
    get_language_label,
    is_rtl,
    translate,
)

EXPECTED_LANGUAGES = (
    "ar", "de", "en", "es", "fa", "fr", "hi", "it", "ja", "ko", "pt", "ru", "tr", "ur", "zh",
)

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

#: Keys the 4.0 UI relies on; a missing one breaks a screen, not just a string.
REQUIRED_SCREEN_KEYS = (
    "app_title",
    "main_menu_intro",
    "menu_servers",
    "menu_tools",
    "menu_settings",
    "menu_admin",
    "menu_help",
    "button_self_host",
    "button_main_menu",
    "button_back",
    "button_refresh",
    "button_run_command",
    "button_terminal",
    "button_status",
    "button_disconnect",
    "servers_title",
    "servers_pick",
    "servers_summary",
    "servers_empty",
    "servers_self_host_line",
    "dashboard_title",
    "dashboard_kind_ssh",
    "dashboard_kind_local",
    "settings_title",
    "settings_language",
    "settings_target",
    "settings_no_target",
    "settings_access",
    "settings_account",
    "help_title",
    "help_body",
    "help_self_host",
    "help_security",
    "language_pick",
    "language_active",
    "target_required",
    "admin_only",
)


def catalog(language: str) -> dict[str, str]:
    return json.loads((CATALOG_DIR / f"{language}.json").read_text(encoding="utf-8"))


def test_every_expected_language_ships_a_catalog():
    assert set(SUPPORTED_LANGUAGES) == set(EXPECTED_LANGUAGES)
    for language in EXPECTED_LANGUAGES:
        assert (CATALOG_DIR / f"{language}.json").exists()


def test_all_languages_have_identical_key_sets():
    base = set(catalog(DEFAULT_LANGUAGE))
    problems = {}
    for language in EXPECTED_LANGUAGES:
        keys = set(catalog(language))
        if keys != base:
            problems[language] = {
                "missing": sorted(base - keys),
                "extra": sorted(keys - base),
            }
    assert not problems, f"catalog key parity broken: {problems}"


@pytest.mark.parametrize("language", EXPECTED_LANGUAGES)
def test_placeholders_match_english(language):
    english = catalog(DEFAULT_LANGUAGE)
    translated = catalog(language)
    mismatches = {}
    for key, template in english.items():
        expected = set(PLACEHOLDER.findall(template))
        actual = set(PLACEHOLDER.findall(translated.get(key, "")))
        if expected != actual:
            mismatches[key] = {"expected": sorted(expected), "actual": sorted(actual)}
    assert not mismatches, f"{language}: placeholder drift in {mismatches}"


@pytest.mark.parametrize("language", EXPECTED_LANGUAGES)
def test_required_screen_keys_exist_and_are_non_empty(language):
    data = catalog(language)
    for key in REQUIRED_SCREEN_KEYS:
        assert key in data, f"{language} missing {key}"
        assert data[key].strip(), f"{language} has an empty value for {key}"


@pytest.mark.parametrize("language", EXPECTED_LANGUAGES)
def test_translate_never_returns_a_raw_key(language):
    for key in REQUIRED_SCREEN_KEYS:
        value = translate(key, language)
        assert value != key


#: Keys whose catalogs intentionally carry markup (rendered preformatted).
MARKUP_KEYS = {"admin_only", "audit_title", "host_key_mismatch"}


def test_new_ui_keys_are_plain_text_not_markup():
    """Menu strings are escaped by the renderer; markup markers would show up."""
    english = catalog(DEFAULT_LANGUAGE)
    for key in REQUIRED_SCREEN_KEYS:
        if key in MARKUP_KEYS:
            continue
        value = english[key]
        assert "**" not in value, f"{key} contains markdown bold"
        assert "`" not in value, f"{key} contains a code span"


def test_rtl_languages_are_declared():
    for language in ("ar", "fa", "ur"):
        assert is_rtl(language)
        assert language in RTL_LANGUAGES
    assert not is_rtl("en")


def test_language_labels_are_self_describing():
    for language in EXPECTED_LANGUAGES:
        label = get_language_label(language)
        assert label.strip()
        assert any(ord(char) > 127 for char in label), "label should carry a flag or native name"


def test_unknown_language_falls_back_to_english():
    assert translate("app_title", "xx") == translate("app_title", DEFAULT_LANGUAGE)


def test_missing_key_returns_the_key_instead_of_raising():
    assert translate("definitely_not_a_key", "fa") == "definitely_not_a_key"


def test_catalog_files_are_utf8_json_objects():
    for path in sorted(Path(CATALOG_DIR).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict) and payload
