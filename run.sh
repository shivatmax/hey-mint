#!/bin/bash
# Start Mint. Creates the virtualenv and installs dependencies on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

if [[ ! -d .venv ]]; then
  echo "Setting up (first run only)…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

# --grant only asks macOS for Accessibility; it needs no key and no running app.
if [[ "${1:-}" == "--grant" ]]; then
  exec .venv/bin/python -m mint --grant
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "GEMINI_API_KEY is not set." >&2
  echo "Get one at https://aistudio.google.com/apikey then either:" >&2
  echo "  export GEMINI_API_KEY=..." >&2
  echo "  or copy .env.example to .env and fill it in" >&2
  exit 1
fi

if ! pgrep -x JevDesktop >/dev/null; then
  echo "Starting Desktop Voice…"
  open "$HOME/Applications/Desktop Voice.app"
  sleep 4
fi

exec .venv/bin/python -m mint "$@"
