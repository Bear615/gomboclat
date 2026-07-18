#!/usr/bin/env bash
# One-shot setup + launcher for the AI-Moderator Discord bot (Linux).
#
#   ./setup.sh            # install everything, then launch the TUI
#   ./setup.sh --headless # install everything, then run without the TUI
#   ./setup.sh --install   # install everything and stop (don't launch)
#   ./setup.sh --reinstall # force-reinstall all dependencies
#   ./setup.sh --test      # install everything, then run the unit tests
#
# It creates a local virtualenv (.venv), installs dependencies, ensures a .env
# exists, and runs the bot. Re-running is safe and fast.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m warn:\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m error:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. Python check --------------------------------------------------------
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "python3 not found. Install Python 3.11+ and retry."

PY_VER="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_OK="$("$PYTHON_BIN" -c 'import sys; print(1 if sys.version_info[:2] >= (3, 11) else 0)')"
[ "$PY_OK" = "1" ] || die "Python 3.11+ required (found $PY_VER)."
info "Using Python $PY_VER"

# --- 2. Virtualenv ----------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
  info "Creating virtualenv in $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR" || die "Failed to create venv (is python3-venv installed?)."
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# --- 3. Dependencies --------------------------------------------------------
info "Upgrading pip and installing dependencies"
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

# --- 4. .env ----------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  warn "Created .env from .env.example — set DISCORD_TOKEN and your OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL (or do it in the TUI's Configure tab)."
fi

# --- 5. Dispatch ------------------------------------------------------------
MODE="${1:-tui}"
case "$MODE" in
  --install|install)
    info "Install complete. Edit .env, then run: ./setup.sh"
    ;;
  --reinstall|reinstall)
    info "Force-reinstalling dependencies"
    python -m pip install --force-reinstall --no-cache-dir -r requirements.txt
    ;;
  --test|test)
    info "Running unit tests"
    python -m pytest -q
    ;;
  --headless|headless)
    info "Launching bot (headless)"
    exec python run.py --headless
    ;;
  *)
    if grep -q "your-discord-bot-token-here" .env 2>/dev/null; then
      die "Fill in DISCORD_TOKEN and your OpenAI-compatible API key/endpoint/model in .env (or use the TUI's Configure tab) before launching."
    fi
    info "Launching TUI dashboard (press q to quit)"
    exec python run.py
    ;;
esac
