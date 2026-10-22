"""PURPOSE: Telegram operator surface for the 3.4 alerts lane — the admin-only
/alerts command (server menu -> rules pane -> add flow -> confirm), the
toggle/delete row actions, and the recent-events summary.

OWNER: lane `alerts` of wave 3.4 (branch feat/3.4-alerts).
CONSUMES: src.bot.state (language preference cache), src.bot.support (safe
  send/edit + identity extraction), src.bot.ui_kit (menu/kv_panel builders),
  src.alerts.storage (rule CRUD + the audit-backed event read model),
  src.alerts.models (ALERT_KINDS, kind labels), src.database (the caller's
  own server list), src.metrics.METRICS.
PROVIDES: alerts_command, alerts_menu_callback, alerts_add_callback,
  alerts_confirm_callback, alerts_toggle_callback, alerts_delete_callback,
  plus the callback prefixes for src/bot/application.py to register
  ('^alertsmenu_', '^alertsadd_', '^alertsconfirm_', '^alertstoggle_',
  '^alertsdel_' — all matched before the bare alerts_menu entry).
INVARIANTS:
  * Every user-visible string comes from translate() (contract law L4);
    the only literals here are icons and separators.
  * Nothing raises into the bot loop (law L5): a missing table, a duplicate
    rule and a failed edit all log + count a metric and render an
    explanatory message instead.
  * Ownership is re-checked here (rule.owner_id == pressing user) even though
    storage already scopes by owner: a keyboard outlives a rule, and a stale
    button must not act on someone else's row.
  * Every callback is answered BEFORE the message is edited.
  * Deterministic rendering: the pane is built by one pair of pure helpers
    (_pane_text/_pane_markup) so a refresh after a mutation cannot drift.
EXTEND: a new button is a pure builder plus a handler here, and a
  registration line in src/bot/application.py (orchestrator-owned).
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton
from telegram import InlineKeyboardMarkup
from telegram import Update
from telegram.ext import ContextTypes

from ..alerts.models import ALERT_KINDS, AlertRule, alert_kind_label
from ..alerts.storage import add_rule, delete_rule, events, list_rules, set_enabled
from ..database import get_all_servers
from ..localization import translate
from ..metrics import METRICS
from ..bot import state
from ..bot import ui_kit
from ..bot.support import _edit_message_safely, _extract_user_id, _send_message_safely

logger = logging.getLogger(__name__)

#: Callback-data prefixes owned by this module (application.py matches these).
MENU_CALLBACK = "alerts_menu"
MENU_PREFIX = "alertsmenu_"
ADD_PREFIX = "alertsadd_"
CONFIRM_PREFIX = "alertsconfirm_"
TOGGLE_PREFIX = "alertstoggle_"
DEL_PREFIX = "alertsdel_"

#: The contracted inline threshold row (50..90 step 10).
THRESHOLDS: tuple[int, ...] = (50, 60, 70, 80, 90)

#: /alerts renders this many recent events below the server menu (contract: 10).
EVENTS_LIMIT = 10

#: A pane never shows more rules than the keyboard clamp accommodates.
PANE_RULE_LIMIT = 8


# -- small helpers ---------------------------------------------------------------
def _is_int(text: str) -> bool:
    """True when ``text`` is a plain integer (no sign tricks, no floats)."""
    try:
        int(text)
    except ValueError:
        return False
    return True


def _split_kind_threshold(text: str) -> tuple[str | None, int | None]:
    """Splits ``<kind>`` / ``<kind>_<int>``; the kind may be unknown."""
    head, _, tail = text.rpartition("_")
    if head and _is_int(tail):
        return head, int(tail)
    return (text or None), None


def _parse_add_payload(
    payload: str, owned_aliases: tuple[str, ...] = ()
) -> tuple[str, str | None, int | None]:
    """Splits ``<alias>`` / ``<alias>_<kind>`` / ``<alias>_<kind>_<thr>``.

    Aliases and kinds both contain underscores (``web-1``, ``mem_high``), so a
    plain ``split("_")`` cannot work and no fixed token count is safe.  The
    caller's own aliases are therefore matched first, longest first, so
    ``web-1_prod_mem_high_80`` resolves to alias ``web-1_prod`` while
    ``web-1_gpu_high`` resolves to alias ``web-1`` plus an *unknown* kind
    (degraded, never silently saved).
    """
    text = payload.strip()
    if not text:
        return "", None, None

    for alias in sorted((a for a in owned_aliases if a), key=len, reverse=True):
        if text == alias:
            return alias, None, None
        prefix = f"{alias}_"
        if text.startswith(prefix):
            kind, threshold = _split_kind_threshold(text[len(prefix) :])
            return alias, kind, threshold

    # No owned alias matched: best-effort split so the caller can deny
    # ownership against a real alias instead of the whole payload.
    head, threshold = text, None
    candidate, _, tail = text.rpartition("_")
    if candidate and _is_int(tail):
        head, threshold = candidate, int(tail)
    for kind in ALERT_KINDS:
        suffix = f"_{kind}"
        if head.endswith(suffix):
            alias = head[: -len(suffix)]
            if alias:
                return alias, kind, threshold
    return head, None, threshold


def _degraded(reason: str) -> None:
    """Counts one degraded path so a broken panel is visible in metrics."""
    METRICS.inc("alerts_panel_degraded_total", reason=reason)


def _back_markup(language: str) -> InlineKeyboardMarkup:
    """A single localized Back row pointing at the server menu."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    translate("ui_back", language), callback_data=MENU_CALLBACK
                )
            ]
        ]
    )


# -- pure renderers ---------------------------------------------------------------
def _pane_text(
    alias: str, rules: list[AlertRule], language: str, toast: str = ""
) -> str:
    """Title + one line per rule (state icon, kind label, threshold)."""
    if not rules:
        return translate("alerts_pane_empty", language, alias=alias)
    title = translate("alerts_pane_title", language, alias=alias, count=len(rules))
    labels = [alert_kind_label(rule.kind, language) for rule in rules]
    width = max(len(label) for label in labels)
    lines = [title]
    for rule, label in zip(rules, labels, strict=True):
        icon = "🟢" if rule.enabled else "⏸"
        lines.append(f"{icon} {label.ljust(width)} ≥ {rule.threshold}%")
    body = "\n".join(lines)
    return f"{toast}\n\n{body}" if toast else body


def _pane_markup(
    alias: str, rules: list[AlertRule], user_id: int, language: str
) -> InlineKeyboardMarkup:
    """Rule rows (toggle + delete) + Add row + Back, for the owning user only."""
    keyboard: list[list[InlineKeyboardButton]] = []
    for rule in rules[:PANE_RULE_LIMIT]:
        if rule.owner_id != user_id:
            continue  # a foreign row never gets action buttons
        icon = "🟢" if rule.enabled else "⏸"
        keyboard.append(
            [
                InlineKeyboardButton(
                    translate(
                        "alerts_toggle_button",
                        language,
                        state_icon=icon,
                        kind=alert_kind_label(rule.kind, language),
                        threshold=rule.threshold,
                    ),
                    callback_data=f"{TOGGLE_PREFIX}{rule.id}",
                ),
                InlineKeyboardButton(
                    f"🗑 {translate('alerts_delete_button', language)}",
                    callback_data=f"{DEL_PREFIX}{rule.id}",
                ),
            ]
        )
    keyboard.append(
        [
            InlineKeyboardButton(
                f"➕ {translate('alerts_add_rule_button', language, alias=alias)}",
                callback_data=f"{ADD_PREFIX}{alias}",
            )
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                translate("ui_back", language), callback_data=MENU_CALLBACK
            )
        ]
    )
    return InlineKeyboardMarkup(keyboard)


def _events_text(user_id: int | None, language: str) -> str:
    """Recent-events block (contract: last 10) as aligned plain lines."""
    if user_id is None:
        return translate("alerts_events_empty", language)
    fired = events(user_id, limit=EVENTS_LIMIT)
    if not fired:
        return translate("alerts_events_empty", language)
    pairs = [
        (
            f"{event.alias} {alert_kind_label(event.kind, language)}",
            translate(
                "alerts_event_kv_value",
                language,
                value=f"{event.value:.1f}",
                threshold=event.threshold,
            ),
        )
        for event in fired
    ]
    width = max(len(label) for label, _ in pairs)
    lines = [translate("alerts_events_title", language, count=len(pairs))]
    lines.extend(f"{label.ljust(width)} : {value}" for label, value in pairs)
    return "\n".join(lines)


def _owned_aliases(user_id: int | None) -> tuple[str, ...]:
    """The aliases this user may alert on: own servers plus the local host.

    The local host ("self") is always alertable -- the probe reads it
    directly -- while SSH servers come from the owner's server list.
    """
    if user_id is None:
        return ()
    # imported here: src.runtime pulls the bot package, which imports this
    # module's handlers -> module-level import would be circular
    from ..runtime.resolver import LOCAL_ALIAS

    aliases: list[str] = [LOCAL_ALIAS]
    try:
        servers = get_all_servers(user_id)
    except Exception as exc:  # noqa: BLE001 - the panel must render regardless
        _degraded("servers")
        logger.warning("alerts: server list unavailable: %s", exc)
        return tuple(aliases)
    aliases.extend(str(server.get("alias") or "") for server in servers)
    return tuple(dict.fromkeys(aliases))  # de-dup, keep order


def _server_rows(user_id: int | None) -> list[tuple[str, str]]:
    """``(label, alertsmenu_<alias>)`` per server the caller owns."""
    return [
        (f"🖥 {alias}", f"{MENU_PREFIX}{alias}")
        for alias in _owned_aliases(user_id)
        if alias
    ]


def _alias_owned(alias: str, user_id: int | None) -> bool:
    """True only when ``alias`` is one of this user's own servers.

    Callback data is client-supplied: without this check a crafted
    ``alertsadd_<someone-elses-alias>`` would let a user conjure rules for a
    host they do not own.  An unreadable store denies (fail closed).
    """
    if user_id is None or not alias:
        return False
    return alias in _owned_aliases(user_id)



async def _render(
    update: Update,
    text: str,
    markup: InlineKeyboardMarkup,
    user_id: int | None,
) -> None:
    """Edits when pressed from a button, sends when invoked by /alerts."""
    query = update.callback_query
    try:
        if query is not None:
            await _edit_message_safely(query, text, user_id, reply_markup=markup)
        elif update.effective_chat is not None:
            await _send_message_safely(
                update.effective_chat,
                text,
                user_id,
                reply_markup=markup,
                preformatted=True,
            )
    except Exception:  # pragma: no cover - stale message: never raise
        _degraded("render")
        logger.warning("alerts: panel render failed", exc_info=True)


# -- /alerts ----------------------------------------------------------------------
async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only ``/alerts``: the caller's servers plus a recent-alerts summary."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    query = update.callback_query
    if query is not None:
        await query.answer()

    rows = _server_rows(user_id)
    events_block = _events_text(user_id, language)

    try:
        if not rows:
            _degraded("no_servers")
            text = f"{translate('alerts_menu_empty', language)}\n\n{events_block}"
            await _render(update, text, _back_markup(language), user_id)
            return

        title, markup = ui_kit.menu(
            translate("alerts_menu_title", language), rows, back=MENU_CALLBACK
        )
        METRICS.inc("alerts_panel_views_total")
        await _render(update, f"{title}\n\n{events_block}", markup, user_id)
    except Exception:  # noqa: BLE001 - the panel must never kill the loop
        _degraded("menu")
        logger.warning("alerts: menu render failed", exc_info=True)


# -- alertsmenu_<alias> -------------------------------------------------------------
async def alerts_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``alertsmenu_<alias>``: the rules pane for one server."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    alias = (query.data or "")[len(MENU_PREFIX) :].strip()

    if not alias or user_id is None or not _alias_owned(alias, user_id):
        _degraded("menu_payload")
        await _render(
            update,
            translate("alerts_not_found_toast", language),
            _back_markup(language),
            user_id,
        )
        return

    rules = list_rules(user_id, alias)
    METRICS.inc("alerts_pane_views_total")
    await _render(
        update,
        _pane_text(alias, rules, language),
        _pane_markup(alias, rules, user_id, language),
        user_id,
    )


# -- add flow: kind -> threshold -> confirm -----------------------------------------
async def alerts_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``alertsadd_<alias>`` / ``..._<kind>`` / ``..._<kind>_<thr>`` steps."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    payload = (query.data or "")[len(ADD_PREFIX) :]
    alias, kind, threshold = _parse_add_payload(payload, _owned_aliases(user_id))

    if not alias or user_id is None:
        _degraded("add_payload")
        await _render(
            update,
            translate("alerts_not_found_toast", language),
            _back_markup(language),
            user_id,
        )
        return

    if not _alias_owned(alias, user_id):
        # Callback data is client-supplied: without this check a crafted
        # ``alertsadd_<someone-elses-alias>`` would create a rule on a host
        # the pressing user does not own.
        _degraded("add_alias")
        await _render(
            update,
            translate("alerts_not_found_toast", language),
            _back_markup(language),
            user_id,
        )
        return

    # step 1 — which metric to watch
    if kind is None:
        items = [
            (alert_kind_label(kind_name, language), f"{ADD_PREFIX}{alias}_{kind_name}")
            for kind_name in ALERT_KINDS
        ]
        title, markup = ui_kit.menu(
            translate("alerts_pick_kind_title", language, alias=alias),
            items,
            back=MENU_CALLBACK,
        )
        await _render(update, title, markup, user_id)
        return

    if kind not in ALERT_KINDS:
        _degraded("add_kind")
        await _render(
            update,
            translate("alerts_not_found_toast", language),
            _back_markup(language),
            user_id,
        )
        return

    # step 2 — the contracted inline 50/60/70/80/90 row
    if threshold is None:
        items = [
            (f"{value}%", f"{ADD_PREFIX}{alias}_{kind}_{value}") for value in THRESHOLDS
        ]
        title, markup = ui_kit.menu(
            translate(
                "alerts_pick_threshold_title",
                language,
                alias=alias,
                kind=alert_kind_label(kind, language),
            ),
            items,
            back=MENU_CALLBACK,
        )
        await _render(update, title, markup, user_id)
        return

    # step 3 — confirm
    if threshold not in THRESHOLDS:
        _degraded("add_threshold")
        await _render(
            update,
            translate("alerts_not_found_toast", language),
            _back_markup(language),
            user_id,
        )
        return

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"✅ {translate('alerts_confirm_yes', language)}",
                    callback_data=f"{CONFIRM_PREFIX}{alias}_{kind}_{threshold}",
                ),
                InlineKeyboardButton(
                    f"❌ {translate('alerts_confirm_no', language)}",
                    callback_data=f"{MENU_PREFIX}{alias}",
                ),
            ]
        ]
    )
    await _render(
        update,
        translate(
            "alerts_confirm_title",
            language,
            alias=alias,
            kind=alert_kind_label(kind, language),
            threshold=threshold,
        ),
        markup,
        user_id,
    )


# -- alertsconfirm_<alias>_<kind>_<thr> ----------------------------------------------
async def alerts_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``alertsconfirm_...``: persist the rule, toast, then show the pane."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)
    payload = (query.data or "")[len(CONFIRM_PREFIX) :]
    alias, kind, threshold = _parse_add_payload(payload, _owned_aliases(user_id))

    valid = (
        user_id is not None
        and bool(alias)
        and kind is not None
        and threshold is not None
        and threshold in THRESHOLDS
        and _alias_owned(alias, user_id)
    )
    if not valid:
        _degraded("confirm_payload")
        await _render(
            update,
            translate("alerts_not_found_toast", language),
            _back_markup(language),
            user_id,
        )
        return

    assert user_id is not None and kind is not None and threshold is not None
    try:
        add_rule(user_id, alias, kind, threshold)
    except ValueError:
        # Duplicate (owner, alias, kind) — a second identical alert would fire
        # the same event twice, so the store refuses it.
        _degraded("duplicate")
        METRICS.inc("alerts_rules_saved_total", outcome="duplicate")
        await _render(
            update,
            translate(
                "alerts_duplicate", language, kind=alert_kind_label(kind, language)
            ),
            _back_markup(language),
            user_id,
        )
        return
    except Exception as exc:  # noqa: BLE001 - store down: degrade, never crash
        _degraded("store")
        logger.warning("alerts: rule save failed: %s", exc)
        await _render(
            update, translate("alerts_degraded", language), _back_markup(language), user_id
        )
        return

    METRICS.inc("alerts_rules_saved_total", outcome="ok")
    rules = list_rules(user_id, alias)
    await _render(
        update,
        _pane_text(
            alias, rules, language, toast=translate("alerts_saved_toast", language)
        ),
        _pane_markup(alias, rules, user_id, language),
        user_id,
    )


# -- alertstoggle_<id> / alertsdel_<id> ------------------------------------------------
async def alerts_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``alertstoggle_<id>``: pause/resume one of the caller's own rules."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)

    rule = _owned_rule((query.data or "")[len(TOGGLE_PREFIX) :], user_id)
    if rule is None or rule.id is None or user_id is None:
        _degraded("toggle_missing")
        await _render(
            update,
            translate("alerts_not_found_toast", language),
            _back_markup(language),
            user_id,
        )
        return

    if not set_enabled(rule.id, user_id, not rule.enabled):
        _degraded("toggle_write")
        await _render(
            update,
            translate("alerts_not_found_toast", language),
            _back_markup(language),
            user_id,
        )
        return

    toast = translate(
        "alerts_enabled_toast" if not rule.enabled else "alerts_disabled_toast",
        language,
    )
    rules = list_rules(user_id, rule.alias)
    await _render(
        update,
        _pane_text(rule.alias, rules, language, toast=toast),
        _pane_markup(rule.alias, rules, user_id, language),
        user_id,
    )


async def alerts_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``alertsdel_<id>``: delete one of the caller's own rules."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id)

    rule = _owned_rule((query.data or "")[len(DEL_PREFIX) :], user_id)
    if rule is None or rule.id is None or user_id is None:
        _degraded("delete_missing")
        await _render(
            update,
            translate("alerts_not_found_toast", language),
            _back_markup(language),
            user_id,
        )
        return

    if not delete_rule(rule.id, user_id):
        _degraded("delete_write")
        await _render(
            update,
            translate("alerts_not_found_toast", language),
            _back_markup(language),
            user_id,
        )
        return

    rules = list_rules(user_id, rule.alias)
    await _render(
        update,
        _pane_text(
            rule.alias, rules, language, toast=translate("alerts_deleted_toast", language)
        ),
        _pane_markup(rule.alias, rules, user_id, language),
        user_id,
    )


# -- ownership -----------------------------------------------------------------------
def _owned_rule(raw_id: str, user_id: int | None) -> AlertRule | None:
    """Resolves a rule id from callback data, only when this user owns it.

    Storage already scopes reads by owner; this re-check exists because the
    keyboard outlives the rule — a stale button press must be answered, never
    acted on.
    """
    if user_id is None or not _is_int(raw_id):
        return None
    try:
        rule_id = int(raw_id)
    except ValueError:  # pragma: no cover - _is_int already guarded
        return None
    for rule in list_rules(user_id):
        if rule.id == rule_id and rule.owner_id == user_id:
            return rule
    return None
