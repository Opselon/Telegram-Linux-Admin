"""Tests for localization coverage and fallbacks."""

from src.localization import SUPPORTED_LANGUAGES, translate


PROMPT_KEYS = [
    "prompt_hostname",
    "prompt_username",
    "prompt_auth_method",
    "prompt_password",
    "prompt_key_path",
    "button_auth_key",
    "button_auth_password",
    "server_added",
]


def test_add_server_prompts_available_for_all_languages():
    """Ensure add-server prompts never fall back to raw keys in any language."""

    for language in SUPPORTED_LANGUAGES:
        for key in PROMPT_KEYS:
            value = translate(key, language, alias="example")
            assert value
            # We should never get the raw key back even if a translation is missing
            assert value != key

