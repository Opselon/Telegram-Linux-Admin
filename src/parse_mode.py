"""
Professional Parse Mode System for Telegram Linux Admin Bot
Supports all languages with proper escaping and formatting
"""

from __future__ import annotations
from typing import Optional, Literal, TypedDict
from dataclasses import dataclass, field
from functools import cache
import re

# Telegram parse modes
PARSE_MODE_MARKDOWN = "Markdown"
PARSE_MODE_MARKDOWN_V2 = "MarkdownV2"
PARSE_MODE_HTML = "HTML"
PARSE_MODE_NONE = None

# Language-specific parse mode preferences
# MarkdownV2 is better for most languages but requires proper escaping
# HTML is better for RTL languages and complex formatting
# Markdown (V1) is legacy but simpler for basic cases
LANGUAGE_PARSE_MODE: dict[str, Optional[str]] = {
    "en": PARSE_MODE_MARKDOWN_V2,  # English - MarkdownV2 for better formatting
    "ar": PARSE_MODE_HTML,  # Arabic - HTML for better RTL support
    "fa": PARSE_MODE_HTML,  # Persian - HTML for better RTL support
    "fr": PARSE_MODE_MARKDOWN_V2,  # French
    "de": PARSE_MODE_MARKDOWN_V2,  # German
    "es": PARSE_MODE_MARKDOWN_V2,  # Spanish
    "pt": PARSE_MODE_MARKDOWN_V2,  # Portuguese
    "it": PARSE_MODE_MARKDOWN_V2,  # Italian
    "ru": PARSE_MODE_MARKDOWN_V2,  # Russian
    "tr": PARSE_MODE_MARKDOWN_V2,  # Turkish
    "zh": PARSE_MODE_HTML,  # Chinese - HTML for better CJK support
    "ja": PARSE_MODE_HTML,  # Japanese - HTML for better CJK support
    "ko": PARSE_MODE_HTML,  # Korean - HTML for better CJK support
    "hi": PARSE_MODE_HTML,  # Hindi - HTML for better Devanagari support
    "ur": PARSE_MODE_HTML,  # Urdu - HTML for better RTL support
}

# Default parse mode
DEFAULT_PARSE_MODE = PARSE_MODE_MARKDOWN_V2


@cache
def get_parse_mode(language: str | None = None) -> str | None:
    """
    Returns the best parse mode for the given language.
    
    Args:
        language: Language code (e.g., 'en', 'ar', 'fa')
        
    Returns:
        Parse mode string or None
    """
    if not language:
        return DEFAULT_PARSE_MODE
    return LANGUAGE_PARSE_MODE.get(language, DEFAULT_PARSE_MODE)


# --- MarkdownV2 Escaping ---
# Characters that need escaping in MarkdownV2
MARKDOWN_V2_SPECIAL = r'_*[]()~`>#+-=|{}.!'

def escape_markdown_v2(text: str) -> str:
    """
    Escapes special characters for MarkdownV2 parse mode.
    
    Args:
        text: Text to escape
        
    Returns:
        Escaped text safe for MarkdownV2
    """
    if not text:
        return text
    
    # Escape all special characters
    escaped = ""
    for char in text:
        if char in MARKDOWN_V2_SPECIAL:
            escaped += "\\" + char
        else:
            escaped += char
    return escaped


def escape_markdown_v2_code(text: str) -> str:
    """
    Escapes text for use inside code blocks in MarkdownV2.
    Code blocks need less escaping, but backticks still need escaping.
    
    Args:
        text: Text to escape for code block
        
    Returns:
        Escaped text safe for code blocks
    """
    if not text:
        return text
    return text.replace("\\", "\\\\").replace("`", "\\`")


# --- Markdown (V1) Escaping ---
# Markdown V1 has fewer special characters
MARKDOWN_V1_SPECIAL = r'_*[]()~`>#+-=|{}.!'

def escape_markdown(text: str) -> str:
    """
    Escapes special characters for Markdown (V1) parse mode.
    
    Args:
        text: Text to escape
        
    Returns:
        Escaped text safe for Markdown
    """
    if not text:
        return text
    
    # Markdown V1 is more lenient, but we still escape common issues
    escaped = ""
    for char in text:
        if char in MARKDOWN_V1_SPECIAL:
            escaped += "\\" + char
        else:
            escaped += char
    return escaped


# --- HTML Escaping ---
def escape_html(text: str) -> str:
    """
    Escapes special characters for HTML parse mode.
    
    Args:
        text: Text to escape
        
    Returns:
        Escaped text safe for HTML
    """
    if not text:
        return text
    
    # HTML entities
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


# --- Universal Escaping Function ---
def escape_text(text: str, parse_mode: str | None = None) -> str:
    """
    Escapes text based on the specified parse mode (2026 standards with match/case).
    
    Args:
        text: Text to escape
        parse_mode: Parse mode to use (Markdown, MarkdownV2, HTML, or None)
        
    Returns:
        Escaped text safe for the specified parse mode
    """
    if not text or not parse_mode:
        return text or ""
    
    # Modern match/case statement (Python 3.10+)
    match parse_mode:
        case "MarkdownV2":
            return escape_markdown_v2(text)
        case "Markdown":
            return escape_markdown(text)
        case "HTML":
            return escape_html(text)
        case _:
            # No escaping needed for None or unknown modes
            return text


# --- Smart Formatting Functions ---
def format_bold(text: str, parse_mode: str | None = None) -> str:
    """
    Formats text as bold based on parse mode (2026 standards with match/case).
    
    Args:
        text: Text to make bold
        parse_mode: Parse mode to use
        
    Returns:
        Formatted bold text
    """
    if not parse_mode:
        return text
    
    match parse_mode:
        case "MarkdownV2":
            return f"*{escape_markdown_v2(text)}*"
        case "Markdown":
            return f"*{escape_markdown(text)}*"
        case "HTML":
            return f"<b>{escape_html(text)}</b>"
        case _:
            return text


def format_italic(text: str, parse_mode: str | None = None) -> str:
    """
    Formats text as italic based on parse mode (2026 standards with match/case).
    
    Args:
        text: Text to make italic
        parse_mode: Parse mode to use
        
    Returns:
        Formatted italic text
    """
    if not parse_mode:
        return text
    
    match parse_mode:
        case "MarkdownV2":
            return f"_{escape_markdown_v2(text)}_"
        case "Markdown":
            return f"_{escape_markdown(text)}_"
        case "HTML":
            return f"<i>{escape_html(text)}</i>"
        case _:
            return text


def format_code(text: str, parse_mode: str | None = None) -> str:
    """
    Formats text as inline code based on parse mode (2026 standards with match/case).
    
    Args:
        text: Text to format as code
        parse_mode: Parse mode to use
        
    Returns:
        Formatted code text
    """
    if not parse_mode:
        return text
    
    match parse_mode:
        case "MarkdownV2" | "Markdown":
            return f"`{escape_markdown_v2_code(text)}`"
        case "HTML":
            return f"<code>{escape_html(text)}</code>"
        case _:
            return text


def format_code_block(text: str, language: str = "", parse_mode: Optional[str] = None) -> str:
    """
    Formats text as a code block based on parse mode.
    
    Args:
        text: Text to format as code block
        language: Optional language identifier for syntax highlighting
        parse_mode: Parse mode to use
        
    Returns:
        Formatted code block text
    """
    if not parse_mode:
        return f"```{language}\n{text}\n```"
    
    if parse_mode == PARSE_MODE_MARKDOWN_V2:
        # Code blocks in MarkdownV2 don't need escaping inside
        return f"```{language}\n{text}\n```"
    elif parse_mode == PARSE_MODE_MARKDOWN:
        # Code blocks in Markdown don't need escaping inside
        return f"```{language}\n{text}\n```"
    elif parse_mode == PARSE_MODE_HTML:
        return f"<pre><code>{escape_html(text)}</code></pre>"
    else:
        return f"```{language}\n{text}\n```"


def format_link(text: str, url: str, parse_mode: Optional[str] = None) -> str:
    """
    Formats text as a link based on parse mode.
    
    Args:
        text: Link text
        url: URL
        parse_mode: Parse mode to use
        
    Returns:
        Formatted link text
    """
    if not parse_mode:
        return f"[{text}]({url})"
    
    if parse_mode == PARSE_MODE_MARKDOWN_V2:
        return f"[{escape_markdown_v2(text)}]({escape_markdown_v2(url)})"
    elif parse_mode == PARSE_MODE_MARKDOWN:
        return f"[{escape_markdown(text)}]({url})"
    elif parse_mode == PARSE_MODE_HTML:
        return f'<a href="{escape_html(url)}">{escape_html(text)}</a>'
    else:
        return f"[{text}]({url})"


# --- Safe Message Formatting ---
def safe_format_message(
    template: str,
    parse_mode: Optional[str] = None,
    **kwargs: str
) -> str:
    """
    Safely formats a message template with escaping based on parse mode.
    
    This function handles placeholders in templates and escapes them properly.
    
    Args:
        template: Message template with {placeholders}
        parse_mode: Parse mode to use
        **kwargs: Values to substitute in template
        
    Returns:
        Formatted and escaped message
    """
    if not parse_mode:
        return template.format(**kwargs)
    
    # Escape all substitution values
    escaped_kwargs = {}
    for key, value in kwargs.items():
        if value:
            escaped_kwargs[key] = escape_text(str(value), parse_mode)
        else:
            escaped_kwargs[key] = ""
    
    # Format the template
    try:
        return template.format(**escaped_kwargs)
    except KeyError as e:
        # If a placeholder is missing, return template as-is
        return template


# --- Pro Message Builder ---
class MessageBuilder:
    """
    Professional message builder that handles formatting based on parse mode.
    """
    def __init__(self, parse_mode: Optional[str] = None):
        self.parse_mode = parse_mode
        self._parts: list[str] = []

    def add_text(self, text: str, escape: bool = True) -> "MessageBuilder":
        """
        Add plain text to the message (2026 standards).
        
        Args:
            text: Text to add
            escape: Whether to escape the text
            
        Returns:
            Self for chaining
        """
        if escape and self.parse_mode:
            self._parts.append(escape_text(text, self.parse_mode))
        else:
            self._parts.append(text)
        return self
    
    def add_bold(self, text: str) -> "MessageBuilder":
        """Add bold text."""
        self._parts.append(format_bold(text, self.parse_mode))
        return self
    
    def add_italic(self, text: str) -> "MessageBuilder":
        """Add italic text."""
        self._parts.append(format_italic(text, self.parse_mode))
        return self
    
    def add_code(self, text: str) -> "MessageBuilder":
        """Add inline code."""
        self._parts.append(format_code(text, self.parse_mode))
        return self
    
    def add_code_block(self, text: str, language: str = "") -> "MessageBuilder":
        """Add code block."""
        self._parts.append(format_code_block(text, language, self.parse_mode))
        return self
    
    def add_line(self, text: str = "") -> "MessageBuilder":
        """Add a line break."""
        self._parts.append("\n" + text if text else "\n")
        return self
    
    def build(self) -> str:
        """Build and return the final message."""
        return "".join(self._parts)
    
    def clear(self) -> "MessageBuilder":
        """Clear all parts."""
        self._parts.clear()
        return self

