# Database Audit — Telegram Linux Admin

**Audited commit:** `07fd561`
**Method:** live schema inspection (`PRAGMA table_info`, `PRAGMA journal_mode`,
`PRAGMA busy_timeout`) plus source read of `src/database.py` (666 lines).

## Verified schema (live)

```
servers: id, owner_id, alias, hostname, user, password, key_path,
         created_at, host_key        ← NO port column
journal_mode: wal
busy_timeout: 5000                   ← Python sqlite3 default, NOT configured
foreign_keys: ON (set per-connection in _connect)
```

## Findings

### P1-01 — No SSH port support (verified: no column, no code path)
- **File/line:** `src/database.py:194-207` (schema), `src/database.py:223-261`
  (`add_server` signature), `src/bot/servers.py:56-148` (conversation flow)
- **Repro:** `PRAGMA table_info(servers)` on a live DB → no `port` column.
  `grep -rn "port" src/bot/servers.py src/validators.py` → zero hits in the
  add-server path. `src/ssh_manager.py:164-166` reads `config.get("port")` and
  passes it to `asyncssh.connect` — but nothing can ever populate it.
- **Root cause:** the schema and conversation flow were written port-less;
  `ssh_manager` alone kept a vestigial `port` read.
- **Impact:** any server on a non-22 port can never be added. This is a
  straight functional gap against the required Add Server flow.
- **Fix:** versioned migration adding `port INTEGER NOT NULL DEFAULT 22`,
  `add_server(..., port=22)`, a `PORT` conversation state with validation
  (1-65535, default 22), and `update_server` support.
- **Regression test:** add a server on port 2222, restart the process, and
  assert `asyncssh.connect` receives `port=2222`.

### P1-02 — `busy_timeout` is the unconfigured 5000 ms default
- **File/line:** `src/database.py:57-71` (`_connect`)
- **Repro:** `PRAGMA busy_timeout` on the live connection → `5000`, which is
  the Python `sqlite3` default; `grep -n "busy_timeout" src/` → no hits.
- **Root cause:** never set explicitly.
- **Impact:** under SQLite write contention (WAL helps, but the single shared
  connection serializes writes through `_conn_lock` anyway) a 5 s wait can
  still surface as `database is locked` to a user mid-operation.
- **Fix:** `conn.execute("PRAGMA busy_timeout = 10000")` in `_connect`, made
  configurable via `TLA_DB_BUSY_TIMEOUT`.
- **Test:** two concurrent transactions; assert no `OperationalError`.

### P1-03 — No schema versioning / migration framework
- **File/line:** `src/database.py:176-220` (`initialize_database`)
- **Repro:** `grep -rn "user_version\|schema_version" src/` → none.
- **Root cause:** `initialize_database` is a `CREATE TABLE IF NOT EXISTS` +
  `_ensure_column` set; there is no version number anywhere.
- **Impact:** migrations are not idempotent-by-version, cannot be tested
  against "an old DB", and nothing detects a partially-applied migration.
- **Fix:** `PRAGMA user_version` ladder; each migration its own function,
  wrapped in a transaction, with `user_version` bumped inside it. Test against
  a fixture DB built at the previous version.
- **Test:** build a DB at V(n-1), run migrations, assert V(n) and data intact.

### P1-04 — `seed_users` deletes every server row in the database
- **File/line:** `src/database.py:476-483`
- **Repro:**
  ```python
  db.add_server(111, alias='a', hostname='h', user='u', password='p')
  db.seed_users([222])
  assert db.get_all_servers(111) == []   # silently destroyed
  ```
- **Root cause:** `seed_users` runs `DELETE FROM servers` (and `DELETE FROM
  users`) to rebuild the whitelist, but `servers.owner_id` references
  `users.telegram_id` — so seeding a new whitelist destroys every registered
  server for every user, not just the removed ones.
- **Impact:** one admin call wipes all server registrations bot-wide. This is
  a data-integrity landmine hidden behind a "seed the whitelist" API.
- **Fix:** seed only the *diff* — delete users not in the new list (FK
  `ON DELETE CASCADE` then removes *their* servers), and insert users that are
  new. Never `DELETE FROM servers` wholesale.
- **Test:** two users with servers; seed a whitelist containing both; assert
  both users' servers survive. Seed a whitelist dropping one user; assert only
  that user's servers cascade away.

### P2-01 — Backup copies `database.db` while the bot holds an open WAL connection
- **File/line:** `src/bot/backup.py:46-118` (`backup`)
- **Repro:** start the bot (connection open, WAL mode), call `backup()`, unzip
  the archive → `database.db` present, `-wal` and `-shm` absent. Restore it on
  a clean machine → the DB may be missing the last committed transactions
  because the WAL had not been checkpointed into the main file.
- **Root cause:** `zipfile.ZipFile.write("database.db")` is a raw file copy.
  SQLite does not guarantee the main DB file is up to date while a WAL
  connection is open.
- **Fix:** use `sqlite3.Connection.backup()` (the online backup API), which
  reads through the WAL and produces a self-consistent snapshot; or
  `PRAGMA wal_checkpoint(TRUNCATE)` before copying. Then verify with
  `PRAGMA integrity_check` on the *backup*, not just a checksum of the file.
- **Test:** insert a row, call the new backup, restore into a temp path, assert
  the row is present and `integrity_check` = ok.

### P2-02 — Restore is a `shutil.copy2` overwrite of the live file
- **File/line:** `src/bot/backup.py:455-474`
- **Repro:** read `restore_file`. Current `config.json`/`database.db` are
  copied to a safety dir, then `shutil.copy2(source, target)` overwrites the
  live files. No `os.replace` atomicity, no rename-temp-then-swap, and the
  process keeps using the *old* sqlite connection object (now pointing at a
  replaced file).
- **Root cause:** file-copy restore, not a connection-aware swap.
- **Impact:** a crash mid-copy leaves a half-written `database.db`; after
  restore the cached `_conn` may operate on a stale inode (on Linux
  `copy2` truncates the same inode, so this is mostly benign, but a concurrent
  writer can see a torn page).
- **Fix:** write to `database.db.restore-tmp`, run `PRAGMA integrity_check` on
  it, `close_db_connection()`, `os.replace()` (atomic on the same filesystem),
  re-open. Verify after re-open. On any failure, roll back to the safety copy.
- **Test:** corrupt the `database.db` inside the zip → assert restore refuses
  and the live DB is untouched; interrupt restore mid-copy → assert safety
  backup restores cleanly.

### P3-01 — `get_users_joined_today` is a guess, not a metric
- **File/line:** `src/database.py:494-505`
- **Repro:** read the SQL — it counts `user_preferences` rows with no servers,
  i.e. "has a language preference but never added a server". The docstring
  even hedges ("likely joined today").
- ** **Fix:** add a real `first_seen_at` column or drop the metric.

### P3-02 — `servers` table has no index on `created_at` for the "recent servers" query
- **File/line:** `src/database.py:545-557` (`get_recent_servers`)
- **Repro:** `EXPLAIN QUERY PLAN SELECT ... FROM servers ORDER BY created_at
  DESC LIMIT 10` → `SCAN servers` (full scan) — acceptable at current scale,
  but it is the only un-indexed ORDER BY.
- **Fix:** optional `idx_servers_created_at`; only justified if the dashboard
  is measurably slow. Row counts here are tiny (plan-limited to ≤10/user).

## What is already correct (verified)

- **WAL is enabled** (`PRAGMA journal_mode = wal`, `src/database.py:69`)
- **Foreign keys are ON** (`src/database.py:68`), FK `ON DELETE CASCADE` exists
- **All queries are parameterized** — no f-string SQL interpolation except the
  `ALTER TABLE` / `PRAGMA table_info` identifiers in `_ensure_column`, which
  are internal, never user-supplied
- **Explicit transactions** (`transaction()`, `isolation_level=None`,
  BEGIN/COMMIT/ROLLBACK at `src/database.py:98-109`)
- **Secrets are Fernet-encrypted at rest** with a versioned key file,
  base64-wrapped for storage — see `docs/audit/SECURITY_AUDIT.md`
- **`check_same_thread=False` + `_conn_lock`**: writes are serialized by the
  RLock; reads are concurrent-safe under WAL. A generic connection pool would
  add nothing here — the lock already serializes writers.
