"""Purpose: Process-wide PTB error handler.

OWNER: src/bot/<this module>.
CONSUMES: src/bot/state.py (mutable singletons, conversation states,
  leaf helpers) and the sibling handler modules listed below.
PROVIDES: python-telegram-bot handlers registered by
  src/bot/application.py.
INVARIANTS: handler logic and user-visible behavior are unchanged from
  3.1; only the import graph was made explicit. Mutable runtime state
  is read/written through ``state`` so a patched singleton is seen here.
EXTEND: add a handler in this module, then register it in
  src/bot/application.py.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from telegram import Update
from telegram.error import BadRequest
from telegram.error import NetworkError
from telegram.error import TimedOut
from telegram.ext import ContextTypes

# Optional transport-level timeout exceptions: httpx and httpcore are PTB's
# default HTTP transport but are not guaranteed present, so resolve them
# dynamically and fall back to an empty tuple.
#: Each name is either the real exception class or None. Annotated as ``Any``
#: deliberately: the ignore comments that a ``type: ignore`` would need are
#: themselves an error when the transport IS installed (``warn_unused_ignores``),
#: so the optional import must type-check the same with httpx present or absent.
HTTPCoreReadTimeout: Any = None
HTTPXReadTimeout: Any = None
try:
    from httpcore import ReadTimeout as _HTTPCoreReadTimeout

    HTTPCoreReadTimeout = _HTTPCoreReadTimeout
except ImportError:  # pragma: no cover - depends on installed transport
    pass
try:
    from httpx import ReadTimeout as _HTTPXReadTimeout

    HTTPXReadTimeout = _HTTPXReadTimeout
except ImportError:  # pragma: no cover - depends on installed transport
    pass

from .support import _extract_user_id, _send_message_safely

logger = logging.getLogger(__name__)


# --- Error Handling ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a detailed message to the user."""
    error = context.error

    # Handle specific error types gracefully
    if isinstance(error, BadRequest) and "Message is not modified" in str(error):
        # This is a harmless error - message content hasn't changed
        logger.debug("Message not modified error (ignored): %s", error)
        return

    # Handle timeout errors (from various sources)
    timeout_errors = [TimedOut, NetworkError]
    if HTTPCoreReadTimeout is not None:
        timeout_errors.append(HTTPCoreReadTimeout)
    if HTTPXReadTimeout is not None:
        timeout_errors.append(HTTPXReadTimeout)
    
    # Check if it's a timeout error by type, cause, or message content
    is_timeout = isinstance(error, tuple(timeout_errors))
    if not is_timeout and hasattr(error, '__cause__') and error.__cause__:
        is_timeout = isinstance(error.__cause__, tuple(timeout_errors))
    if not is_timeout:
        error_str = str(error).lower()
        error_type_str = str(type(error)).lower()
        is_timeout = 'readtimeout' in error_type_str or 'timeout' in error_str
    
    if is_timeout:
        # Network/timeout errors - log but don't spam user with technical details
        logger.warning("Network/timeout error occurred: %s", error)
        if isinstance(update, Update) and update.effective_chat:
            user_id = _extract_user_id(update)
            try:
                await _send_message_safely(
                    update.effective_chat,
                    "⚠️ **Connection timeout**\n\nThe request took too long to complete. Please try again.",
                    user_id,
                    preformatted=True
                )
            except Exception as e:
                logger.error(f"Failed to send timeout error message: {e}")
        return
    
    # Log other errors
    logger.error("Exception while handling an update:", exc_info=error)

    if isinstance(update, Update) and update.effective_chat:
        user_id = _extract_user_id(update)
        # Format the traceback
        tb_list = traceback.format_exception(None, error, error.__traceback__)
        tb_string = "".join(tb_list)

        # Sanitize and prepare the message
        error_message = f"❌ **An unexpected error occurred:**\n\n```\n{tb_string}\n```"

        # Ensure the message is not too long
        if len(error_message) > 4096:
            error_message = error_message[:4090] + "\n...```"

        try:
            await _send_message_safely(update.effective_chat, error_message, user_id, preformatted=True)
        except Exception as e:
            logger.error(f"Failed to send detailed error message to user: {e}", exc_info=True)
            # Fallback to a simpler message if the detailed one fails
            try:
                await _send_message_safely(
                    update.effective_chat,
                    "❌ **An unexpected error occurred.**\nThe technical details have been logged.",
                    user_id,
                    preformatted=True
                )
            except Exception as fallback_e:
                logger.error(f"Failed to send even the fallback error message: {fallback_e}", exc_info=True)
