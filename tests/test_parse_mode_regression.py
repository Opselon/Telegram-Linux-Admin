"""Regression tests for the parse-mode helpers (inline code crashed on use).

``format_code`` referenced an undefined helper, so every MarkdownV2 message
that embedded a command — the "Running `cmd`" banner, for example — raised
``NameError``.  These tests pin the behaviour so it cannot regress.
"""

from __future__ import annotations

import pytest

from src.parse_mode import (
    PARSE_MODE_HTML,
    PARSE_MODE_MARKDOWN,
    PARSE_MODE_MARKDOWN_V2,
    MessageBuilder,
    escape_markdown_v2_code,
    format_code,
    format_code_block,
)


class TestFormatCode:
    def test_markdown_v2_wraps_command(self):
        assert format_code("reboot", PARSE_MODE_MARKDOWN_V2) == "`reboot`"

    def test_markdown_v2_escapes_backticks_and_backslashes(self):
        assert format_code("echo `id`", PARSE_MODE_MARKDOWN_V2) == "`echo \\`id\\``"
        assert format_code("a\\b", PARSE_MODE_MARKDOWN_V2) == "`a\\\\b`"

    def test_html_uses_code_tag(self):
        assert format_code("rm -rf /tmp", PARSE_MODE_HTML) == "<code>rm -rf /tmp</code>"

    def test_no_parse_mode_returns_raw(self):
        assert format_code("reboot", None) == "reboot"

    @pytest.mark.parametrize("mode", [PARSE_MODE_MARKDOWN_V2, PARSE_MODE_MARKDOWN])
    def test_code_block(self, mode):
        assert format_code_block("line1\nline2", "", mode) == "```\nline1\nline2\n```"


def test_escape_markdown_v2_code_edge_cases():
    assert escape_markdown_v2_code("") == ""
    assert escape_markdown_v2_code("plain") == "plain"


def test_message_builder_does_not_raise_on_markdown_v2():
    builder = MessageBuilder(PARSE_MODE_MARKDOWN_V2)
    builder.add_text("Running ")
    builder.add_code("systemctl status nginx")
    builder.add_line()
    builder.add_code_block("active")
    assert "systemctl status nginx" in builder.build()


def test_escape_html_quote_flag_and_attribute_safety():
    from src.parse_mode import escape_html, format_link

    assert escape_html('a"b') == "a\"b"
    assert escape_html('a"b', quote=True) == "a&quot;b"
    link = format_link('name "x"', "https://example.com/?a=1&b=2", "HTML")
    assert link == '<a href="https://example.com/?a=1&amp;b=2">name "x"</a>'


def test_module_defines_each_formatter_once():
    """Duplicate definitions silently shadow bug fixes (F811)."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("src/parse_mode.py").read_text(encoding="utf-8"))
    names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    duplicates = {name for name in names if names.count(name) > 1}
    assert not duplicates, f"duplicated definitions: {sorted(duplicates)}"
