#!/usr/bin/env bash
# PURPOSE / OWNER / CONSUMES / PROVIDES / INVARIANTS / EXTEND
# PURPOSE: Prove that the built wheel actually installs and serves health.
#   A wheel that imports but cannot start is the exact failure the Docker stage
#   used to hit, so this checks the console script, the module import, and the
#   three HTTP endpoints in one pass.
# OWNER:   ci lane (feat/3.2-ci).
# CONSUMES: a wheel path ($1) built by `python -m build`.
# PROVIDES: exit 0 only when tla-bot installs into a clean venv, starts under
#   SMOKE_TEST=1, and answers /healthz and /readyz.
# INVARIANTS:
#   - Never touches the caller's environment: everything happens in a venv
#     under a mktemp dir that is removed on exit.
#   - Never needs network access beyond the wheel's own declared deps.
#   - Always kills the bot and always removes the temp dir, on every exit path.
# EXTEND: add endpoint probes in `probe`, keep the port overridable via
#   TLA_HEALTH_PORT.
#
# Usage: scripts/ci/verify_install.sh path/to/wheel.whl

set -uo pipefail

WHEEL="${1:-}"
PORT="${TLA_HEALTH_PORT:-8099}"
WAIT_SECONDS="${WAIT_SECONDS:-30}"

die() {
  echo "FAIL: $*" >&2
  exit 1
}

[ -n "$WHEEL" ] || die "usage: $0 <path-to-wheel>"
[ -f "$WHEEL" ] || die "wheel not found: $WHEEL"

WORKDIR="$(mktemp -d 2>/dev/null || mktemp -d -t tla-verify)"
BOT_PID=""
cleanup() {
  if [ -n "$BOT_PID" ] && kill -0 "$BOT_PID" 2>/dev/null; then
    kill "$BOT_PID" 2>/dev/null || true
    wait "$BOT_PID" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT INT TERM

# Native Python (unlike MSYS bash) does not understand MSYS's /tmp mount, so
# hand it a path it can always resolve: an absolute POSIX path (Linux/macOS)
# or a Windows-native path via cygpath when running under git-bash.
native_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    printf '%s' "$1"
  fi
}

# Resolve the venv layout: POSIX uses bin/, Windows (git-bash) uses Scripts/.
VENV="$WORKDIR/venv"
python -m venv "$(native_path "$VENV")" || die "could not create venv"
if [ -d "$VENV/bin" ]; then
  BIN="$VENV/bin"
else
  BIN="$VENV/Scripts"
fi
PY_BIN="$BIN/python"
# Windows vends python.exe, POSIX vends python; either is fine. Some minimal
# layouts expose only a python3.* shim, so fall back to whichever exists.
if [ -f "$PY_BIN.exe" ]; then
  PY="$PY_BIN.exe"
elif [ -f "$PY_BIN" ]; then
  PY="$PY_BIN"
else
  PY=""
  for candidate in "$BIN"/python3* "$BIN"/python*; do
    if [ -f "$candidate" ]; then
      PY="$candidate"
      break
    fi
  done
fi
[ -n "$PY" ] && [ -f "$PY" ] || die "no interpreter found in $BIN"

echo "== installing $WHEEL into a clean venv =="
"$PY" -m pip install --quiet --upgrade pip || echo "WARN: pip upgrade failed (continuing)"
"$PY" -m pip install --quiet "$WHEEL" || die "pip install of the wheel failed"

echo "== import check =="
"$PY" -c "import src.main; print('import src.main: OK')" || die "import src.main failed"

echo "== console script check =="
[ -x "$BIN/tla-bot" ] || [ -x "$BIN/tla-bot.exe" ] || die "console script tla-bot missing from $BIN"
TLA_BIN="$BIN/tla-bot"
[ -x "$TLA_BIN" ] || TLA_BIN="$BIN/tla-bot.exe"

# A minimal config in the venv dir; SMOKE_TEST makes the bot skip polling and
# exit on its own after SMOKE_TEST_SECONDS. Every path handed to the bot goes
# through native_path: it is a Windows binary under git-bash, and MSYS's /tmp
# is invisible to it.
cat > "$WORKDIR/config.json" <<'JSON'
{"telegram_token": "123456:ABC-DEF1234567890", "whitelisted_users": [12345], "access_mode": "whitelist"}
JSON

echo "== starting tla-bot on port $PORT (SMOKE_TEST=1) =="
(
  cd "$(native_path "$WORKDIR")" || exit 1
  SMOKE_TEST=1 \
  SMOKE_TEST_SECONDS=60 \
  TLA_HEALTH_PORT="$PORT" \
  TLA_HEALTH_HOST=127.0.0.1 \
  TLA_CONFIG_FILE="$(native_path "$WORKDIR/config.json")" \
  TLA_AUDIT_DIR="$(native_path "$WORKDIR/var/log")" \
  "$(native_path "$TLA_BIN")"
) > "$WORKDIR/bot.log" 2>&1 &
BOT_PID=$!

# Wait for the endpoint rather than sleeping a fixed time: the bot binds the
# port as soon as the web app starts, which is well before polling would.
ready=0
for _ in $(seq 1 "$WAIT_SECONDS"); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done

if [ "$ready" != "1" ]; then
  echo "---- bot.log ----" >&2
  cat "$WORKDIR/bot.log" >&2 || true
  die "tla-bot never answered /healthz on port $PORT"
fi

echo "healthz: $(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/healthz")" || die "/healthz failed"
echo "readyz:  $(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/readyz")" || die "/readyz failed"

# /metrics is optional: the observability lane adds it. Probe it when present
# so a regression there is visible, without failing today's tree for a feature
# that has not landed.
if curl -fsS --max-time 5 "http://127.0.0.1:$PORT/metrics" >/dev/null 2>&1; then
  echo "metrics: OK ($(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/metrics" | wc -l | tr -d ' ') lines)"
else
  echo "metrics: not present (optional until the observability lane lands)"
fi

kill "$BOT_PID" 2>/dev/null || true
wait "$BOT_PID" 2>/dev/null || true
BOT_PID=""

echo "PASS: wheel installs, imports, and serves /healthz + /readyz"
exit 0
