# Feature Inventory — Telegram Linux Admin

**Audited commit:** `07fd561` · v3.5.0
**Method:** handler-graph trace, source read, live execution. Every entry below
was verified against actual code, not inferred from a docstring.

## 0. Legend

✅ verified working · ❌ broken/missing · ⚠️ partial

## 1. Commands (11)

| Command | Handler | Status | Evidence |
| --- | --- | --- | --- |
| `/start` | `navigation.start` | ✅ | registered `application.py:259` |
| `/add_server` | `servers.add_server_start` | ⚠️ | works but no port step, no back, aborts on validation error |
| `/cancel` | conversation fallbacks | ✅ | registered as fallback in all 9 ConversationHandlers |
| `/language` | `navigation.language_menu` | ✅ | |
| `/security` | `security_center.security_menu` | ✅ | admin-gated |
| `/status` | `host_status.quick_status` | ✅ | local host only |
| `/dashboard` | `admin.dashboard_command` | ✅ | admin-gated |
| `/jobs` | `jobs_ui.jobs_status_menu` | ✅ | |
| `/debug` | `support.toggle_debug_mode` | ✅ | admin-gated |
| `/exit_shell` | `shell.exit_shell` | ✅ | |
| `/update_bot` | `admin.update_bot_command` | ✅ | admin-gated |

## 2. Server management

| Feature | Status | Evidence |
| --- | --- | --- |
| Add server | ⚠️ | `servers.py:56-148`. **No port step** — `servers` table has no `port` column (`PRAGMA table_info` verified). Validation failure **ends the conversation** and leaves `user_data` dirty (`save_server` has no `user_data.clear()` on the error path). |
| List servers | ✅ | `connect_server_menu`, `remove_server_menu` |
| Connect | ✅ | `handle_server_connection` with TOFU host-key check, destination policy, 10 s timeout |
| Disconnect | ✅ | `disconnect` |
| Remove server | ⚠️ | `remove_server_confirm` deletes **on the first tap** — no confirmation step (§4/§5 require confirmation for destructive ops). |
| **Rename server** | ❌ | `grep -rn "rename" src/bot/` → no handler, no callback, no button. **Does not exist.** |
| **Edit connection config** | ❌ | `update_server()` exists in `database.py:264` but **no handler or keyboard invokes it**. Unreachable from Telegram. |
| **Test connection** | ❌ | No standalone "test connection" callback; connection only happens implicitly via `handle_server_connection`. |
| **Reconnect** | ❌ | Not exposed as an operation (pool reconnects implicitly). |
| Server details view | ⚠️ | `server_status_menu` shows live metrics, not the stored connection config. |
| Navigate between servers | ⚠️ | Each menu returns to Main Menu; no server-to-server navigation. |

## 3. Operations on a connected server

| Feature | Status | Notes |
| --- | --- | --- |
| Run command | ✅ | `run_command_start` → `execute_command`, policy-gated |
| **Confirm a CONFIRM-band command** | ❌ | **P0 — see §3.1 below** |
| **Cancel a CONFIRM-band command** | ❌ | same |
| Cancel a running command | ✅ | `cancel_command_callback` → `kill_process(pid)` |
| Interactive shell | ✅ | `start_shell_session` / `exit_shell` |
| Services: start/stop/restart/check | ✅ | `services.py` |
| Packages: update/upgrade/install | ✅ | `packages.py` |
| Docker: ps/logs/start/stop | ✅ | `docker.py` |
| Processes: ps aux/kill | ✅ | `processes.py` |
| Firewall: allow/deny/delete/status | ✅ | `firewall.py` |
| Files: ls/download/upload | ✅ | `files.py` |
| Status: static/resource/disk/network/ports | ✅ | `monitoring.py` |
| Live monitoring | ✅ | `monitoring.py` with stop button |
| System commands: reboot/shutdown | ✅ | `system.py` with `execute_` confirmation |

### 3.1 P0 — the CONFIRM-band command buttons are DEAD

`src/bot/commands.py:289-306` renders `confirm_cmd_{token}` / `cancel_cmd_{token}`
buttons for commands the policy engine classes as `Risk.CONFIRM`. The handlers
`confirm_command_callback` and `cancel_command_confirmation` **exist**
(`commands.py:328`, `commands.py:363`) and **are unit-tested**
(`tests/test_bot_guards.py:131-155`) — but they are **never registered**:

```
$ grep -n "confirm_command_callback\|cancel_command_confirmation" src/bot/application.py
(no matches)
```

The only commands.py callback registered is
`application.py:265: CallbackQueryHandler(cancel_command_callback, pattern='^cancel_command_')`
— which is a *different* handler, for `cancel_command_{alias}_{pid}`.

**Consequence:** when a user runs any `CONFIRM`-band command, the bot shows
"✅ Confirm / ❌ Cancel" and then **ignores both taps**. Because the
conversation has already ended (`execute_command` returns END at line 311),
the pending command sits in `CONFIRMATIONS` until its 300 s TTL expires. A
confirmed-by-design dangerous command can never be executed at all — the
feature is 100% broken in production while being 100% green in CI.

**Why CI missed it:** the tests call `main.confirm_command_callback(update,
ctx)` directly, never through the PTB handler graph.

**Root cause:** the 3.2 split of `src/main.py` into `src/bot/*` dropped the two
`add_handler` lines when moving them. This is exactly the "handler exists ≠
feature works" trap the brief warns about.

**Fix + regression test:** register both handlers and add a wiring test that
builds the real Application and drives a `confirm_cmd_…` callback through it
(see `tests/test_handler_graph_wiring.py`).

## 4. Backup & restore

| Feature | Status | Notes |
| --- | --- | --- |
| Create backup | ⚠️ | ZIP of `config.json` + `database.db` + encryption keys; SHA-256 per file; `zipf.testzip()`; file size cap 100 MB |
| Integrity verification | ⚠️ | Verifies the **ZIP**, not the **database** — no `PRAGMA integrity_check` anywhere (`grep` verified) |
| Backup listing | ❌ | Only sends the file into chat; no list/manage UI |
| Backup deletion | ❌ | Temp dir cleaned; no retention UI |
| Restore | ⚠️ | Validates JSON + SHA-256 of extracted files, takes a safety backup, then `shutil.copy2` overwrite (non-atomic) |
| Restore atomicity | ❌ | `backup.py:455-474` — no `os.replace`, no connection swap, live SQLite connection not reopened |
| Restart-during-restore | ❌ | No recovery path; safety backup is left in a temp dir the user cannot reach |

##  atomicity race in backup

`backup.py:46-118` zips `database.db` with `zipfile.write()` while the bot's
SQLite connection is open in **WAL mode**. The main `.db` file can lag the
WAL; a backup taken mid-stream can silently miss the last committed
transactions. **Fix:** use `sqlite3.Connection.backup()` or
`PRAGMA wal_checkpoint(TRUNCATE)` before copying.

## 5. Security center (admin)

Whitelist add/remove, access mode (admin/whitelist/open), config summary,
audit tail, setup-PIN enrollment — all ✅, all fail-closed.

## 6. UX patterns

| Pattern | Status |
| --- | --- |
| Every callback answers | ✅ verified exhaustively |
| Back buttons | ✅ via `ui_kit` `nav_keyboard` |
| Cancel in multi-step flows | ✅ 11 cancel handlers, all clear `user_data` |
| Destructive confirmation | ❌ remove-server is one-tap; **CONFIRM-band buttons are dead (§3.1)** |
| English-only hardcoded strings | ⚠️ many: `backup.py` ("Backup Complete!", "Restore Successful!"), `servers.py:245` ("Select a server to remove:"), `servers.py:387-397` (entire post-connect keyboard), `commands.py:302-305` ("✅ Confirm"), `disconnect` message |
| 52 keys missing × 14 languages | ❌ verified by key-set diff |
| Stale keyboards | ⚠️ old messages keep working buttons (acceptable) |

## 7. What already works well

- **SSH layer is genuinely hardened**: TOFU host-key pinning with
  `secrets.compare_digest`, base64 command envelope, pooled connections,
  bounded output (2 MiB), tenacity retry limited to transient classes only
  (auth/host-key/cancel are never retried).
- **Jobs queue** has dedupe keys, bounded concurrency, priority ordering,
  cancellation, and drain-with-timeout shutdown.
- **Policy engine** classifies command risk and requires confirmation for
  dangerous commands — the *logic* is correct; only the wiring is broken (§3.1).
- **Every callback answers** its query (exhaustive scan, verified).
- **No secrets leak**: Fernet at rest, redaction in logs, audit records
  aliases/targets not credentials.
