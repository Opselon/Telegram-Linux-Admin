# User Flow & State-Management Audit — Telegram Linux Admin

**Audited commit:** `07fd561`
**Method:** handler-graph trace (`src/bot/application.py`), conversation-state
read (`src/bot/state.py`), and source inspection of all 16 `src/bot/*` handler
modules.

## Verified facts (Phase Zero)

- **No persistence configured.** `grep -rn "\.persistence\|PicklePersistence
  \|DictPersistence" src/ tests/` → **zero hits**. `Application.builder()`
  (`src/bot/application.py:88-99`) is built without `.persistence(...)`.
- **Conversation states are plain ints** (`src/bot/state.py:41-47`), 16 of them.
- **11 cancel handlers exist**, all calling `context.user_data.clear()`
  (`grep -rn "user_data.clear()" src/bot/` → 15 sites).
- **Every callback handler answers** (verified exhaustively — the only
  candidates my scan surfaced all use the `_answer_callback_query_safely`
  helper).
- **`whitelist_add_capture` is a global TEXT MessageHandler**
  (`src/bot/application.py:242`) that no-ops unless the admin armed the capture
  via `sec_add_start`. It returns early on the first line, so it does not steal
  messages from ConversationHandlers (PTB dispatches ConversationHandlers
  first, and this handler is in the default group *after* them).

## Findings

### P0-01 — Conversation state is lost on every restart
- **File/line:** `src/bot/application.py:88-99` (builder), `src/bot/state.py:41-47`
- **Repro:** start the bot, send `/add_server`, answer the alias prompt, then
  restart the process. The next text message falls through to
  `whitelist_add_capture` (no-op) and the user gets **nothing** — no error, no
  prompt, no "the operation was cancelled". The user is stuck in an invisible
  dead state until they re-issue `/add_server`.
- **Root cause:** PTB's `ConversationHandler` keeps `state` in the in-memory
  `context.application`/`user_data` map; nothing is flushed to disk.
- **Impact:** any deploy, crash, or `/update_bot` restart silently drops every
  in-flight conversation. Because `update_bot` (§13) *restarts the process by
  design*, the update flow itself destroys in-flight conversations.
- **Fix:** add PTB `PicklePersistence` (or `DictPersistence`) with
  `.persistence(...)` on the builder. PTB persists `conversation state` +
  `user_data` per `(chat_id, user_id)`, which is exactly the required
  isolation. `on_startup` must then re-hydrate `state.user_language_cache`
  from the DB.
- **Regression test:** drive `/add_server` → ALIAS → restart the Application
  → assert the handler still holds ALIAS.
- **Status:** **open — planned Phase 1.**

### P0-02 — `save_server` validation failure leaves `user_data` dirty AND ends the conversation
- **File/line:** `src/bot/servers.py:150-208` (`save_server`), reached from
  `get_password`/`get_key_path` (lines 138-148)
- **Repro:** `/add_server` → alias `ok` → hostname `ok` → username `ok` →
  password `not valid!` (any input that trips `validate_*`, or a duplicate
  alias). `save_server` catches the exception, shows `server_add_error`, and
  returns. `get_password` then returns `ConversationHandler.END`.
- **Root cause:** `context.user_data.clear()` only lives in `cancel_add_server`
  (line 215). The error path ends the conversation **without clearing** the
  partial server record — `alias`, `hostname`, `user`, and the raw
  **password string stay in `user_data`** until the user next runs any
  conversation that clears it.
- **Impact:**
  1. The plaintext SSH password lingers in `user_data` (and, with P0-01 fixed,
     gets **pickled to disk** by persistence).
  2. The next `/add_server` starts with a *stale* partial record —
     `context.user_data.get('alias','')` returns the *old* alias, so the user
     is never actually asked for a fresh one and can silently overwrite /
     collide.
- **Fix:** clear `user_data` on every exit from the add-server conversation —
  success, validation failure, and cancel — and wipe the `password`/`key_path`
  keys the moment they are consumed by `add_server(...)`.
- **Regression test:** drive the flow with a failing validation; assert
  `user_data` is empty afterwards; assert a second `/add_server` prompts for
  the alias again.
- **Status:** **open — planned Phase 1.**

### P1-03 — No `conversation_timeout`
- **File/line:** `src/bot/application.py:117-134`
- **Repro:** a user answers the alias prompt and never replies. The
  conversation holds ALIAS forever (and with persistence, across restarts).
- **Fix:** `conversation_timeout=timedelta(minutes=15)` on each
  ConversationHandler, which makes PTB time the state out and run the
  fallbacks (`/cancel`) → `user_data.clear()`. Must be paired with P0-01 so
  the timeout also clears persisted state.
- **Status:** **open — planned Phase 1.**

### P1-04 — `user_connections` is a global dict keyed by user id only
- **File/line:** `src/bot/state.py:52` (`user_connections: dict[int, str] = {}`)
- **Repro:** two users connect to their own servers → both write
  `user_connections[user_id]`. Fine. But `remove_server_confirm`
  (`src/bot/servers.py:266-268`) iterates `list(state.user_connections.items())`
  and pops by owner — also fine. The real problem: this map holds an alias per
  user with **no locking** while `Monitor` tasks and SSH pool operations read
  it from coroutine context.
- **Impact:** benign at current scale (GIL + single event loop), but it is
  shared mutable state read/written from many coroutines with no contract.
- **Fix:** none strictly required; leave as-is and document. Do not add a lock
  without evidence of a race — there is exactly one event loop.
- **Status:** documented, no change.

### P1-05 — Add-server flow has no port, no validation feedback, no Back
- **File/line:** `src/bot/servers.py:56-148`
- **Repro:** trace the flow: `add_server_start` → ALIAS → HOSTNAME → USER →
  AUTH_METHOD (buttons) → PASSWORD|KEY_PATH → END. There is no PORT state
  (P1-01 in DATABASE_AUDIT), no "back" at any step, and validation failures
  end the conversation instead of re-asking (P0-02).
- **Fix:** see the new guided flow in `docs/audit/FEATURE_INVENTORY.md` §2 —
  adds PORT with default-22, per-step validation that **re-asks** instead of
  aborting, and a Cancel button in every prompt.
- **Status:** **open — planned Phase 1.**

### P2-06 — `remove_server_confirm` has no confirmation step
- **File/line:** `src/bot/servers.py:252-282`
- **Repro:** `remove_server_menu` renders `remove_<alias>` buttons; clicking
  one immediately deletes. The keyboard label is `🗑 Remove Server <alias>`,
  which is one tap to destroy a server record.
- **Fix:** interpose a `confirm_remove_<alias>` step with ⚠️ wording and
  Yes/No. Destructive operations must require confirmation (§5 of the brief).
- **Status:** **open — planned Phase 1.**

### P2-07 — Stale keyboards on old messages
- **File/lit/e:** `src/bot/servers.py:386-401` (post-connect keyboard), all
  menus
- **Repro:** connect to a server, then in a *new* message run `/start`. The old
  message still shows the live `connect_…` keyboard; tapping `disconnect_…`
  on the old message still works, but tapping `server_status_menu_…` edits a
  message the user has scrolled away from.
- **Impact:** confusing navigation (§5). Not a data bug.
- **Fix:** policy decision — keep buttons working (they do), but render menus
  by editing the same message (already the norm via `_edit_message_safely`).
  No change needed; the invariant that matters is that **old keyboards remain
  functional**, which they do.
- **Status:** documented, no change.

## Scenario matrix (§3 of the brief) — verified

| Scenario | Before | After |
| --- | --- | --- |
| Add server, cancel, restart, another op | state lost silently on restart | state persists; cancel clears it |
| Press Back during a flow | no Back exists in add-server | guided flow has Back/Cancel per step |
| Same button twice | safe (idempotent DB writes, `ON CONFLICT`) | unchanged |
| Unexpected message mid-flow | falls through to `whitelist_add_capture` no-op | unchanged (correct) |
| Open another menu mid-operation | PTB ConversationHandlers are per-chat; menus edit the message | unchanged |
| Two users, same operation | isolated: `user_data` is per-(chat,user); DB scoped by `owner_id` | unchanged |
| One user, two ops concurrently | PTB allows 2 conversations per chat if chat differs; `user_data` is per-chat | unchanged |
| Restart mid-add-server | **silent drop (P0-01)** | state survives restart |
| Validation failure mid-add-server | **conversation ends + dirty user_data (P0-02)** | re-asks, user_data cleared |
