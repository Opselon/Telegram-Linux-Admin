# Bale adapter implementation notes — mini-app research + SDK evidence

Agent: BALE-INTEGRATION (platform 4.0). SDK: **python-bale-bot 2.5.0**
(installed into `.venv` via `uv pip install -p .venv/Scripts/python.exe python-bale-bot`;
PyPI resolved 2.5.0, not the 2.4.5 the docs site references — verify before pinning).
All file references below are relative to `.venv/Lib/site-packages/bale/`.

## 1. What the SDK verifiably supports (evidence)

| Capability | Evidence (installed source) |
|---|---|
| Bot factory | `bale/bot.py` L70 — `Bot(token)`. Raises `InvalidToken` only for a non-string token; **server-side** validity is checked by `await bot.get_me()` (L323), which raises `bale.InvalidToken` for a rejected token (`bale/error.py` L51 + response parser). |
| Send / edit / delete | `bot.send_message(chat_id, text, *, components, reply_to_message_id, delete_after)` (`bot.py` L381), `bot.edit_message(chat_id, message_id, text, *, components)` (L1252), `bot.delete_message` (L1301). |
| **Message length limit** | Docstrings: `send_message` — "Max **4096** characters after entities parsing"; `edit_message` — "1- 4096 characters". Enforced in the adapter via `utils.truncate` (no parse-mode entities are used, so 4096 chars is safe). |
| Inline keyboards | `bale/ui/inline_keyboard_button.py` L14-49 — `InlineKeyboardButton(text, *, callback_data, url, switch_inline_query, switch_inline_query_current_chat)`. `bale/ui/inline_keyboard_markup.py` — `add(button, row=N)` with **1-based** rows (`bale/ui/reply_markup_item.py` warns rows must be natural ≥ 1). |
| Callback queries | `bale/callbackquery.py` — `id, from_user, data, message`. **There is no `answerCallbackQuery` method on `Bot`** (checked the full method list of `bale/bot.py`); the adapter acknowledges presses by editing the message instead. |
| Updates / polling | `bale/update.py` (`update_id`, `callback_query`, `message`, `edited_message`); `bale/updater.py` — `Updater.start()` runs a retry loop dispatching `message`/`callback` events; `Updater.stop()` clears `running`; `interval` slot sleeps between polls (default `None` = **hot loop** — the factory always sets it, default 1.0 s, env `TLA_BALE_POLL_INTERVAL`). |
| Events | `bot.event` / `bot.listen` (`bot.py` L119-157); handler exceptions are routed to `on_error` (L272-319) and never kill the polling loop. |
| Errors | `bale/error.py` — `BaleError`, `InvalidToken`, `NetworkError`, `APIError`, `NotFound`, `Forbidden`, `RateLimited`, `TimeOut`. |

Not verified against a live Bale server (no test bot token available): actual
callback-data size limit (kept ≤ ~64 bytes by design), whether `getUpdates`
long-polls server-side, and rate-limit behavior. These are **assumptions**,
documented as such.

### Deltas: docs (v2.4.5 contract) vs installed package (2.5.0)

Where the API-reference contract (`docs/upgrade/bale-api-contract.md`) and the
installed source differ, the adapter follows the **installed source**:

- `CallbackQuery`: docs say `callback_id`; installed 2.5.0 exposes `.id`
  (`bale/callbackquery.py` L40-55) plus a `user` alias — identical semantics.
  The adapter only reads `data` / `from_user` / `message`, which match both.
- Event registration: docs list `add_event(event, function)` / `listen(name)`;
  installed 2.5.0 has the `bot.event` decorator + `bot.listen(event_name)`
  (`bale/bot.py` L119-157) and the dispatcher calls `on_message`/`on_callback`
  (`bale/updater.py` L122-134). Both spellings exist in the installed package;
  the adapter uses `bot.event`.
- Keyboard class: docs' `Components(inline_keyboards=[...])` corresponds to
  installed `bale.InlineKeyboardMarkup` (`bale/ui/inline_keyboard_markup.py`)
  which serializes to the same `{"inline_keyboard": [...]}` payload.

## 2. Mini-app research conclusion: URL-button fallback shipped

Research steps performed on the installed source:

- `grep -rn "web_app\|webapp\|miniapp\|mini_app\|init_data\|WebApp" .venv/Lib/site-packages/bale/` → **zero matches** in 2.5.0.
- `bale/ui/inline_keyboard_button.py` defines exactly the five fields listed
  above — there is **no `web_app` / mini-app launch field**.
- No signed init-data / WebApp user payload mechanism exists anywhere in the
  package, so there is nothing verifiable to implement server-side (security
  model §12 requires *verified* init-data before trusting it — we will not
  implement unverified signature validation).

**Shipped path (``src/adapters/bale/miniapp.py``):** a plain **URL button**
("📱 Open Management App") using the standard `InlineKeyboardButton.url`
field, opening the responsive web app in Bale's in-app browser. The URL comes
from env `TLA_PUBLIC_URL` (default `http://localhost:8080`). The button is
attached to the main menu, help, and secondary view keyboards
(`keyboards.py`).

**Assumptions (unverifiable from the SDK):**
1. Bale's client opens `url` buttons in an in-app browser — standard behavior
   for the `url` field per the SDK docstring ("HTTP url to be opened when the
   button is pressed"), but the exact WebView class is client-side and cannot
   be verified from Python.
2. `TLA_PUBLIC_URL` must be reachable from the user's device; loopback default
   is for local development only.

Account linking for the web app stays on the existing one-time-code flow
(6-char code, 5-min TTL, max attempts, `store.create_link_code`/
`consume_link_code`) — no client-asserted identity is trusted.

## 3. Adapter structure

- `client.py` — `create_bot(token)`, env token reader, poll interval.
- `lifecycle.py` — `async def run_bale(stop) -> dict[str, bool]` (exact
  signature the runtime calls). Missing/invalid token → logs ERROR, returns
  `{"bale": False}`; never raises. Mid-run updater death → `{"bale": False}`.
- `authorization.py` — `LinkSessions` (per-`bale_user_id`, TTL 300 s, lazy
  sweep — no unkeyed global state), `resolve_actor` (re-resolved from the
  store on **every** message and callback), `try_bind_code` (consumes via
  `store.consume_link_code`, binds via `store.bind_bale_user`).
- `handlers.py` — commands: `/start /help /servers /status /services /jobs
  /security /link /unlink /run <alias> <command>`. Unbound strangers are
  rejected on every command except `/start`//`/help`. `/run` goes **only**
  through `services.commands.request` (SAFE → job + brief polling; CONFIRM →
  inline [Confirm]/[Cancel] carrying the one-shot token; BLOCKED → denial).
- `callbacks.py` — routes `srv:*`, `svc:<action>:<sid>:<name>`,
  `cmd:confirm|cancel:<token>`, `jobs:*`, `sec:view`. Every callback
  re-resolves the Actor, re-checks the permission (explicit or inside the
  service), and rejects unknown routes / foreign or replayed tokens
  (`ConfirmationError`).
- `notifications.py` — subscribes to `services.notifications`; filters
  recipients by `recipient_filter` (disabled categories) and fans out one
  message per bound Bale user; per-send errors swallowed.
- `keyboards.py` / `utils.py` — 1-based rows, permission-gated buttons,
  4096-char truncation.

## 4. Dependency requests (orchestrator must apply — not done by this agent)

1. **pyproject**: add `python-bale-bot==2.5.0` (a platform-4.0 optional extra
   or a pinned dependency — it is required for `src.adapters.bale` to import;
   the runtime already degrades gracefully when `BALE_BOT_TOKEN` is unset;
   `lifecycle.run_bale` additionally treats a missing package as a disabled
   adapter via its `ImportError` guard).
2. **`.env.example`**: document `BALE_BOT_TOKEN`, `TLA_PUBLIC_URL`,
   `TLA_BALE_POLL_INTERVAL` (optional, default 1.0).

## 5. Service-layer change requests (orchestrator may apply)

1. `src/services/store.py`: add a reverse binding lookup, e.g.
   `get_bale_user_ids(account_id) -> list[str]`. Until then
   `src/adapters/bale/notifications.py` queries the `bale_bindings` table via
   `src.database` directly (same table the store owns; the only place the
   adapter touches the DB layer).
2. Observation (no change requested): `store.consume_link_code` checks
   `attempts >= LINK_CODE_MAX_ATTEMPTS` but nothing ever increments
   `attempts` (a wrong code hashes to a different row). The max-attempt cap is
   therefore currently inert; worth a look by the services owner.

## 6. Known limitations

- No `answerCallbackQuery` in the SDK — presses are acknowledged by editing
  the originating message (`BaleAdapter.edit`, falls back to send on failure).
- No native mini-app: see §2. If Bale ships a verifiable init-data contract in
  a future SDK release, `miniapp.py` is the single swap point.
- Job result *output* text is not fetched by the Bale adapter (the job model
  exposes `state`/`exit_ok`; output delivery stays with the web/Telegram
  surfaces) — the Bale `/run` flow reports state and exit status, with a
  cancel button for `jobs.cancel` holders.
- Linking sessions are in-memory; a process restart drops the "awaiting code"
  flag only (the code itself remains valid server-side).
