# Architecture

Version 4.0 turned a single 3.8k-line `main.py` into a layered application with
one dependency rule: **dependencies point inwards**. Nothing in `domain/` knows
about Telegram, SSH, sqlite or subprocesses.

```
src/
├── domain/                 # entities, policies, ports — pure Python
│   ├── targets.py          # ServerTarget, TargetKind, LOCAL_ALIAS
│   ├── policies.py         # who may use which target (fail-closed)
│   ├── ports.py            # ServerRepository / CommandTransport / TransportRouter
│   └── errors.py           # domain errors carrying i18n keys
├── application/            # use cases over ports
│   ├── catalog.py          # TargetCatalog: list/resolve/remove targets
│   ├── commands.py         # CommandService: policy → transport → stream
│   └── dto.py              # Principal, TargetView
├── infrastructure/         # adapters (the only place with I/O)
│   ├── persistence.py      # sqlite rows  → domain targets
│   ├── ssh_transport.py    # SSHManager    → CommandTransport
│   ├── local_transport.py  # /bin/bash     → CommandTransport
│   ├── local_shell.py      # persistent local shell for the terminal feature
│   ├── routing_manager.py  # strangler adapter for legacy handlers
│   └── router.py           # target kind → transport
├── presentation/           # Telegram-facing, still no business rules
│   ├── screens.py          # one function per screen → View
│   ├── views.py            # View + renderer (parse-mode aware)
│   ├── keyboards.py        # button-grid helpers, callback parsing
│   ├── handlers.py         # menu handlers over an injected UiContext
│   └── i18n/               # JSON catalogs, one file per language
├── composition.py          # builds the wired Container
└── main.py                 # wiring, legacy commands, lifecycle
```

## The self-host target

`LOCAL_ALIAS = "self-host"` is a **virtual** target: it is never stored in
sqlite, never counted against a plan limit, and cannot be removed. It exists
only for an administrator — resolved through
`PermissionManager.is_admin`, which `_configure_security` fills with the first
whitelisted user (the installer).

Three independent guards make it fail-closed:

1. `TargetAccessPolicy.ensure_can_use` raises `TargetAccessDenied` for any
   non-admin and `TargetDisabled` when `local_target_enabled` is false.
2. `TargetCatalog.find` resolves the reserved alias itself; it never queries
   persistence for it, and no SSH row may be stored under that alias.
3. `RoutingCommandManager` re-checks the policy before touching the local
   transport, so a legacy handler that bypasses the catalog still cannot reach
   the host.

Local commands run in a **new session per command** (`start_new_session=True`)
with a hard timeout and an output cap, and are killed by process group. The
persistent shell used by the terminal feature terminates a command with a
per-command random marker, so command output can never forge the boundary, and
a timed-out command tears the session down instead of leaking output into the
next one.

## Why `RoutingCommandManager` exists

Rewriting ~25 legacy call sites (`ssh_manager.run_command(user_id, alias, …)`)
in one release is a big-bang risk. `RoutingCommandManager` implements the same
surface and dispatches on the target kind, enforcing the same policy. New code
calls `CommandService` (which speaks `ServerTarget`, not `(user_id, alias)`);
the adapter is deleted once the last legacy handler migrates.

## i18n

`src/presentation/i18n/catalog/<lang>.json` is the single source of truth;
`en.json` is canonical. Guarantees enforced by tests
(`tests/test_i18n_catalog_parity.py`):

- identical key sets in all 15 languages,
- identical `{placeholder}` sets per key,
- every screen key non-empty, and never rendered as a raw key,
- plain-text menu strings (markup is applied by the renderer, so one catalog
  cannot break another language's parse mode).

`src/localization.py` remains as a facade so historical imports keep working.
Adding a language = one JSON file + one entry in `languages.json`.

Adding a string: add it to `en.json`, then to the other catalogs; the parity
test fails until they match, which is the point.

## Packaging

The Docker image installs a **wheel**, so subpackages and catalog JSON must be
declared in `pyproject.toml`:

- `[tool.setuptools.packages.find] include = ["src*"]`
- `[tool.setuptools.package-data] "src.presentation.i18n" = [...]`

`scripts/verify_wheel.py` asserts both in CI, so a packaging regression fails
the build instead of shipping a bot that renders raw keys.

## Adding a target kind

1. Add a member to `TargetKind` and a factory next to `local_target`.
2. Write an adapter satisfying `CommandTransport`.
3. Register it in `DefaultTransportRouter` (one line in `composition.py`).
4. Extend `TargetAccessPolicy` if the new kind needs privilege.

No menu, screen or i18n key changes are required — the panel is built from the
view model, not from a kind-specific branch.
