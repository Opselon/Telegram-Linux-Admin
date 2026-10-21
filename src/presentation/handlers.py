"""Menu handlers: thin async functions over the screen builders.

They own no state: the caller injects a :class:`UiContext` once at startup
(``configure``), and the handlers read language, active target and catalog from
it.  That inversion is what lets the same handler run in tests with a fake
Telegram, and keeps ``main.py`` focused on wiring and the legacy commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.application.catalog import TargetCatalog
from src.application.dto import Principal
from src.domain.targets import LOCAL_ALIAS
from src.presentation import screens
from src.presentation.i18n import SUPPORTED_LANGUAGES, translate
from src.presentation.views import View, render


@dataclass(slots=True)
class UiContext:
    """Everything the menu handlers need from the composition root."""

    get_language: Callable[[int | None], str]
    get_parse_mode: Callable[[int | None], str | None]
    principal: Callable[[Update], Principal]
    catalog: TargetCatalog
    active_target: Callable[[int], str | None]
    server_limit: Callable[[int], int]
    access_mode: Callable[[], str]
    user_name: Callable[[Update], str]
    send: Callable[..., Awaitable[Any]]
    edit: Callable[..., Awaitable[Any]]
    answer: Callable[..., Awaitable[Any]]
    #: Persists a language choice (owned by main.py, which owns the cache).
    persist_language: Callable[[int, str], None] | None = None


_ctx: UiContext | None = None


def configure(ctx: UiContext) -> None:
    """Installs the runtime context (called once from ``main``)."""
    global _ctx
    _ctx = ctx


def get_context() -> UiContext:
    """Returns the configured context, or explains the wiring mistake.

    Named ``get_context`` (not ``context``) so it can never be shadowed by the
    ``context`` parameter every PTB handler receives.
    """
    if _ctx is None:  # pragma: no cover - wiring error, not a runtime path
        raise RuntimeError("UiContext is not configured; call handlers.configure() first.")
    return _ctx


def markup(view: View) -> InlineKeyboardMarkup | None:
    """Converts a view's button grid into a Telegram keyboard."""
    rows = [
        [InlineKeyboardButton(label, callback_data=payload) for label, payload in row]
        for row in view.buttons
        if row
    ]
    return InlineKeyboardMarkup(rows) if rows else None


async def _deliver(
    update: Update, view: View, *, toast: str | None = None, alert: bool = False
) -> None:
    """Answers the callback (optionally with a toast) and renders the view."""
    ctx = get_context()
    user_id = update.effective_user.id if update.effective_user else None
    language = ctx.get_language(user_id)
    parse_mode = ctx.get_parse_mode(user_id)
    text = render(view, parse_mode)
    query = update.callback_query
    if query is not None:
        await ctx.answer(query, toast, alert)
        if query.message is not None:
            await ctx.edit(query.message, text, user_id, reply_markup=markup(view), preformatted=True)
        return
    if update.effective_message is not None:
        await ctx.send(
            update.effective_message.chat,
            text,
            user_id,
            reply_markup=markup(view),
            preformatted=True,
        )


def _display_name(alias: str | None, language: str) -> str | None:
    if not alias:
        return None
    if alias == LOCAL_ALIAS:
        return translate("button_self_host", language)
    return alias


async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Root screen; the only entry point users need to remember."""
    ctx = get_context()
    principal = ctx.principal(update)
    language = ctx.get_language(principal.user_id)
    view = screens.main_menu(
        lang=language,
        user_name=ctx.user_name(update),
        is_admin=principal.is_admin,
        self_host_available=ctx.catalog.self_host_available(principal),
        active_target=_display_name(ctx.active_target(principal.user_id), language),
        server_count=ctx.catalog.count_remote(principal),
    )
    await _deliver(update, view)


async def servers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists every target the user may connect to (self-host first)."""
    ctx = get_context()
    principal = ctx.principal(update)
    language = ctx.get_language(principal.user_id)
    targets = ctx.catalog.list_views(principal)
    view = screens.servers_menu(
        lang=language,
        targets=targets,
        remote_count=ctx.catalog.count_remote(principal),
        limit=ctx.server_limit(principal.user_id),
    )
    await _deliver(update, view)


async def tools_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick actions for the active target; explains what to do without one."""
    ctx = get_context()
    principal = ctx.principal(update)
    language = ctx.get_language(principal.user_id)
    alias = ctx.active_target(principal.user_id)
    if not alias:
        await _deliver(
            update,
            screens.servers_menu(
                lang=language,
                targets=ctx.catalog.list_views(principal),
                remote_count=ctx.catalog.count_remote(principal),
                limit=ctx.server_limit(principal.user_id),
            ),
            toast=translate("target_required", language),
        )
        return
    await _deliver(
        update,
        screens.tools_menu(lang=language, alias=alias, is_local=alias == LOCAL_ALIAS),
    )


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_context()
    principal = ctx.principal(update)
    language = ctx.get_language(principal.user_id)
    await _deliver(
        update,
        screens.settings_menu(
            lang=language,
            user_name=ctx.user_name(update),
            active_target=_display_name(ctx.active_target(principal.user_id), language),
            access_mode=ctx.access_mode(),
        ),
    )


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Administration screen (the caller wraps this in ``admin_authorized``)."""
    ctx = get_context()
    principal = ctx.principal(update)
    language = ctx.get_language(principal.user_id)
    if not principal.is_admin:
        await _deliver(
            update,
            screens.main_menu(
                lang=language,
                user_name=ctx.user_name(update),
                is_admin=False,
                self_host_available=False,
                active_target=None,
                server_count=ctx.catalog.count_remote(principal),
            ),
            toast=translate("admin_only", language),
            alert=True,
        )
        return
    await _deliver(
        update,
        screens.admin_menu(
            lang=language, self_host_available=ctx.catalog.self_host_available(principal)
        ),
    )


async def help_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_context()
    user_id = update.effective_user.id if update.effective_user else None
    language = ctx.get_language(user_id)
    await _deliver(update, screens.help_menu(lang=language))


async def language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_context()
    user_id = update.effective_user.id if update.effective_user else None
    language = ctx.get_language(user_id)
    await _deliver(
        update,
        screens.language_menu(
            lang=language, active=language, languages=SUPPORTED_LANGUAGES
        ),
    )


async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persists the chosen language and re-renders the picker in it."""
    ctx = get_context()
    query = update.callback_query
    if query is None or not query.data:
        return
    user_id = query.from_user.id
    code = query.data.rsplit("_", 1)[-1]
    if code not in SUPPORTED_LANGUAGES:
        await ctx.answer(query, translate("language_unsupported", ctx.get_language(user_id)), alert=True)
        return
    if ctx.persist_language is not None:
        ctx.persist_language(user_id, code)
    await _deliver(
        update,
        screens.language_menu(lang=code, active=code, languages=SUPPORTED_LANGUAGES),
        toast=translate("language_saved_toast", code),
    )


async def remove_server_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_context()
    principal = ctx.principal(update)
    language = ctx.get_language(principal.user_id)
    await _deliver(
        update,
        screens.remove_server_menu(lang=language, targets=ctx.catalog.list_views(principal)),
    )


async def dashboard_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-opens the panel of the active target."""
    ctx = get_context()
    principal = ctx.principal(update)
    language = ctx.get_language(principal.user_id)
    alias = ctx.active_target(principal.user_id)
    if not alias:
        await servers_menu(update, context)
        return
    await _deliver(update, build_dashboard(language, alias, is_admin=principal.is_admin))


def build_dashboard(language: str, alias: str, *, is_admin: bool) -> View:
    """Single source of the connected panel, reused by the connect handler."""
    return screens.dashboard(lang=language, alias=alias, is_local=alias == LOCAL_ALIAS)


def dashboard_markup(
    language: str, alias: str, *, is_admin: bool
) -> InlineKeyboardMarkup | None:
    return markup(build_dashboard(language, alias, is_admin=is_admin))


def dashboard_text(language: str, alias: str, *, is_admin: bool, parse_mode: str | None) -> str:
    return render(build_dashboard(language, alias, is_admin=is_admin), parse_mode)


def supported_languages() -> Sequence[str]:
    return SUPPORTED_LANGUAGES
