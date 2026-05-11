#!/usr/bin/env bash
# One-command local launcher for the Reddit Sales POC.
#
#   ./run.sh                # set up + start on http://localhost:8000
#   PORT=9000 ./run.sh      # custom port
#   ./run.sh --no-open      # don't try to open the browser
#   ./run.sh --reset        # wipe the venv and reinstall

set -euo pipefail

# Resolve repo root (script may be invoked from anywhere).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${PORT:-8000}"
OPEN_BROWSER=1
RESET=0
for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN_BROWSER=0 ;;
    --reset)   RESET=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown flag: $arg"; exit 2 ;;
  esac
done

# --- pretty print ----------------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; CYAN=$'\033[36m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET_=$'\033[0m'
else
  BOLD=""; DIM=""; CYAN=""; GREEN=""; YELLOW=""; RED=""; RESET_=""
fi
say()   { printf "%s\n" "${CYAN}>${RESET_} $*"; }
ok()    { printf "%s\n" "${GREEN}✓${RESET_} $*"; }
warn()  { printf "%s\n" "${YELLOW}!${RESET_} $*"; }
fail()  { printf "%s\n" "${RED}✗${RESET_} $*" >&2; }

# --- 1. Find Python --------------------------------------------------------
PYTHON=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import sys; assert sys.version_info >= (3,9)" 2>/dev/null; then
      PYTHON="$cand"
      break
    fi
  fi
done
if [ -z "$PYTHON" ]; then
  fail "Python 3.9+ is required. Install from https://www.python.org/downloads/ and re-run."
  exit 1
fi
ok "using $($PYTHON --version) at $(command -v $PYTHON)"

# --- 2. Create / refresh venv ---------------------------------------------
VENV_DIR="$SCRIPT_DIR/.venv"
if [ "$RESET" = "1" ] && [ -d "$VENV_DIR" ]; then
  say "removing existing virtualenv"
  rm -rf "$VENV_DIR"
fi
if [ ! -d "$VENV_DIR" ]; then
  say "creating virtualenv in .venv"
  VENV_LOG="$(mktemp)"
  if ! "$PYTHON" -m venv "$VENV_DIR" >"$VENV_LOG" 2>&1; then
    if grep -q "ensurepip" "$VENV_LOG" 2>/dev/null; then
      warn "system venv lacks ensurepip — bootstrapping pip manually"
      rm -rf "$VENV_DIR"
      "$PYTHON" -m venv --without-pip "$VENV_DIR"
      "$VENV_DIR/bin/python" -m ensurepip --upgrade 2>/dev/null || \
        ( curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && \
          "$VENV_DIR/bin/python" /tmp/get-pip.py )
    else
      cat "$VENV_LOG" >&2
      fail "could not create virtualenv"
      exit 1
    fi
  fi
  rm -f "$VENV_LOG"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
ok "virtualenv ready"

# --- 3. Install deps -------------------------------------------------------
REQ="$SCRIPT_DIR/backend/requirements.txt"
STAMP="$VENV_DIR/.requirements.sha256"
NEW_HASH="$(sha256sum "$REQ" | cut -d' ' -f1)"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$NEW_HASH" ]; then
  say "installing python dependencies (first run takes a minute)"
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r "$REQ"
  echo "$NEW_HASH" > "$STAMP"
  ok "dependencies installed"
else
  ok "dependencies already up to date"
fi

# --- 4. Make sure .env exists with at least one LLM key -------------------
ENV_FILE="$SCRIPT_DIR/backend/.env"
ENV_EXAMPLE="$SCRIPT_DIR/backend/.env.example"
if [ ! -f "$ENV_FILE" ]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  warn "created backend/.env from the example template"
fi

get_env() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- | sed 's/^"//;s/"$//' || true
}

set_env() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    if sed --version >/dev/null 2>&1; then
      sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
    else
      sed -i '' "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
    fi
  else
    printf "\n%s=%s\n" "$key" "$val" >> "$ENV_FILE"
  fi
}

ANTHROPIC_KEY="$(get_env ANTHROPIC_API_KEY)"
OPENAI_KEY="$(get_env OPENAI_API_KEY)"
if [ -z "$ANTHROPIC_KEY" ] && { [ -z "$OPENAI_KEY" ] || [ "$OPENAI_KEY" = "sk-..." ]; }; then
  echo
  printf "%sLLM API key needed.%s\n" "$BOLD" "$RESET_"
  printf "  Anthropic (preferred): https://console.anthropic.com/settings/keys\n"
  printf "  OpenAI fallback:       https://platform.openai.com/api-keys\n"
  if [ -t 0 ]; then
    printf "Paste a key now (sk-ant-... or sk-..., Enter to skip): "
    read -r INPUT_KEY
    if [ -n "$INPUT_KEY" ]; then
      case "$INPUT_KEY" in
        sk-ant-*) set_env ANTHROPIC_API_KEY "$INPUT_KEY"
                  ok "saved ANTHROPIC_API_KEY to backend/.env" ;;
        sk-*)     set_env OPENAI_API_KEY "$INPUT_KEY"
                  ok "saved OPENAI_API_KEY to backend/.env" ;;
        *)        warn "unrecognized key prefix; saving as ANTHROPIC_API_KEY"
                  set_env ANTHROPIC_API_KEY "$INPUT_KEY" ;;
      esac
    else
      warn "no key entered — the UI will load but Analyze will fail until you add one to backend/.env"
    fi
  else
    warn "non-interactive shell; edit backend/.env and add ANTHROPIC_API_KEY (or OPENAI_API_KEY)"
  fi
fi

# --- 5. Pick a free port ---------------------------------------------------
port_is_free() {
  python - <<PY 2>/dev/null
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("0.0.0.0", $1))
    sys.exit(0)
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

ORIGINAL_PORT="$PORT"
TRIES=0
while ! port_is_free "$PORT"; do
  TRIES=$((TRIES + 1))
  if [ "$TRIES" -gt 25 ]; then
    fail "couldn't find a free port near ${ORIGINAL_PORT}"
    exit 1
  fi
  PORT=$((PORT + 1))
done
if [ "$PORT" != "$ORIGINAL_PORT" ]; then
  warn "port ${ORIGINAL_PORT} was busy — using ${PORT} instead"
fi

# --- 6. Start the server ---------------------------------------------------
URL_LOCAL="http://localhost:${PORT}"
URL_LOOPBACK="http://127.0.0.1:${PORT}"
LOG_FILE="$(mktemp)"

USER_QUIT=0
cleanup() {
  USER_QUIT=1
  if [ -n "${TAIL_PID:-}" ] && kill -0 "$TAIL_PID" 2>/dev/null; then
    kill "$TAIL_PID" 2>/dev/null || true
  fi
  if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -f "$LOG_FILE"
}
trap cleanup INT TERM EXIT

# Stream the log to the user in the background.
: > "$LOG_FILE"
tail -n +1 -f "$LOG_FILE" &
TAIL_PID=$!

ATTEMPT=0
LAST_START=0
RESTART_FAILS=0

say "starting server"

while true; do
  ATTEMPT=$((ATTEMPT + 1))
  LAST_START=$(date +%s)
  ( cd "$SCRIPT_DIR/backend" && PORT="$PORT" python main.py ) \
    >>"$LOG_FILE" 2>&1 &
  SERVER_PID=$!

  # Poll until /api/health responds (or the server dies).
  READY=0
  for _ in $(seq 1 60); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      break
    fi
    if curl -sf "${URL_LOOPBACK}/api/health" >/dev/null 2>&1; then
      READY=1
      break
    fi
    sleep 0.5
  done

  if [ "$READY" = "1" ]; then
    if [ "$ATTEMPT" = "1" ]; then
      echo
      ok "site is live!"
      printf "   %s%s%s\n" "$BOLD" "$URL_LOCAL" "$RESET_"
      printf "   %s%s%s\n" "$DIM" "$URL_LOOPBACK" "$RESET_"
      echo
      echo "${DIM}press Ctrl-C to stop${RESET_}"
      echo
      if [ "$OPEN_BROWSER" = "1" ]; then
        if command -v open >/dev/null 2>&1; then
          open "$URL_LOCAL" >/dev/null 2>&1 || true
        elif command -v xdg-open >/dev/null 2>&1; then
          xdg-open "$URL_LOCAL" >/dev/null 2>&1 || true
        elif command -v wslview >/dev/null 2>&1; then
          wslview "$URL_LOCAL" >/dev/null 2>&1 || true
        elif command -v powershell.exe >/dev/null 2>&1; then
          powershell.exe -NoProfile start "$URL_LOCAL" >/dev/null 2>&1 || true
        fi
      fi
    else
      ok "server back up at $URL_LOCAL (restart #$((ATTEMPT - 1)))"
    fi
    RESTART_FAILS=0
  else
    if [ "$ATTEMPT" = "1" ]; then
      fail "server did not start. Last 25 log lines:"
      echo "${DIM}---${RESET_}"
      tail -n 25 "$LOG_FILE" >&2 || true
      echo "${DIM}---${RESET_}"
      if grep -q "address already in use" "$LOG_FILE" 2>/dev/null; then
        warn "port ${PORT} is in use. Try: PORT=$((PORT + 1)) ./run.sh"
      fi
      USER_QUIT=1
      break
    fi
    RESTART_FAILS=$((RESTART_FAILS + 1))
    warn "server failed to come up on restart attempt #$((ATTEMPT - 1))"
  fi

  # `wait` returns the server's exit code, which can be non-zero when
  # killed; that's expected here and must not trigger `set -e`.
  SERVER_EXIT=0
  wait "$SERVER_PID" || SERVER_EXIT=$?

  if [ "$USER_QUIT" = "1" ]; then
    break
  fi

  # If the server stayed up <5 s and this is our 4th-in-a-row quick crash,
  # give up so we don't loop forever in a broken state.
  NOW=$(date +%s)
  UPTIME=$((NOW - LAST_START))
  if [ "$UPTIME" -lt 5 ]; then
    RESTART_FAILS=$((RESTART_FAILS + 1))
    if [ "$RESTART_FAILS" -ge 4 ]; then
      fail "server keeps crashing within ${UPTIME}s of startup; giving up. Last log lines:"
      echo "${DIM}---${RESET_}"
      tail -n 30 "$LOG_FILE" >&2 || true
      echo "${DIM}---${RESET_}"
      exit "$SERVER_EXIT"
    fi
  else
    RESTART_FAILS=0
  fi

  warn "server exited (code ${SERVER_EXIT}) after ${UPTIME}s — restarting in 1 s…"
  sleep 1
done

exit 0
