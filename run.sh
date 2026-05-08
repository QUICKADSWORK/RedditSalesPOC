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

# --- 4. Make sure .env exists with at least an OPENAI_API_KEY -------------
ENV_FILE="$SCRIPT_DIR/backend/.env"
ENV_EXAMPLE="$SCRIPT_DIR/backend/.env.example"
if [ ! -f "$ENV_FILE" ]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  warn "created backend/.env from the example template"
fi

# Read existing key (if any) without sourcing the file.
get_env() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- | sed 's/^"//;s/"$//' || true
}

CURRENT_KEY="$(get_env OPENAI_API_KEY)"
if [ -z "$CURRENT_KEY" ] || [ "$CURRENT_KEY" = "sk-..." ]; then
  echo
  printf "%sOpenAI API key needed.%s Get one at https://platform.openai.com/api-keys\n" "$BOLD" "$RESET_"
  if [ -t 0 ]; then
    printf "Paste it now (or press Enter to skip and add it later to backend/.env): "
    read -r INPUT_KEY
    if [ -n "$INPUT_KEY" ]; then
      # Replace or append.
      if grep -qE "^OPENAI_API_KEY=" "$ENV_FILE"; then
        # macOS sed needs '' after -i; GNU sed doesn't. Detect.
        if sed --version >/dev/null 2>&1; then
          sed -i "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=${INPUT_KEY}|" "$ENV_FILE"
        else
          sed -i '' "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=${INPUT_KEY}|" "$ENV_FILE"
        fi
      else
        printf "\nOPENAI_API_KEY=%s\n" "$INPUT_KEY" >> "$ENV_FILE"
      fi
      ok "saved OPENAI_API_KEY to backend/.env"
    else
      warn "no key entered — the UI will load but Analyze will fail until you set OPENAI_API_KEY in backend/.env"
    fi
  else
    warn "non-interactive shell; edit backend/.env and add your OPENAI_API_KEY"
  fi
fi

# --- 5. Start the server ---------------------------------------------------
URL="http://localhost:${PORT}"
say "starting server on ${BOLD}${URL}${RESET_}"
echo "${DIM}press Ctrl-C to stop${RESET_}"

# Try to open a browser shortly after the server is up.
if [ "$OPEN_BROWSER" = "1" ]; then
  (
    sleep 1.5
    if command -v open >/dev/null 2>&1; then
      open "$URL" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$URL" >/dev/null 2>&1 || true
    elif command -v wslview >/dev/null 2>&1; then
      wslview "$URL" >/dev/null 2>&1 || true
    fi
  ) &
fi

cd "$SCRIPT_DIR/backend"
exec env PORT="$PORT" python main.py
