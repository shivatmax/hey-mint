#!/bin/bash
# Put a new Gemini API key into .env and check that it actually works.
# Usage: ./set-key.sh AIzaSy...        (or run with no argument to be prompted)
set -euo pipefail
cd "$(dirname "$0")"

KEY="${1:-}"
if [[ -z "$KEY" ]]; then
  read -r -p "Paste your Gemini API key: " KEY
fi

KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"

if [[ -z "$KEY" ]]; then
  echo "No key given." >&2
  exit 1
fi
if (( ${#KEY} < 20 )); then
  echo "That is too short to be an API key." >&2
  exit 1
fi
# Google has used several key formats ("AIza…", "AQ.…"). Rather than guess at a
# prefix and reject a valid new one, only note the unfamiliar case and let the
# live check below be the real test.
if [[ "$KEY" != AIza* && "$KEY" != AQ.* ]]; then
  echo "Note: unfamiliar key prefix. Trying it against the API anyway."
fi

[[ -f .env ]] || cp .env.example .env

# Replace the key line, leaving every other setting alone.
python3 - "$KEY" <<'PY'
import pathlib, re, sys

key = sys.argv[1]
path = pathlib.Path(".env")
text = path.read_text()
line = f"export GEMINI_API_KEY={key}"

if re.search(r"(?m)^\s*(export\s+)?GEMINI_API_KEY=.*$", text):
    text = re.sub(r"(?m)^\s*(export\s+)?GEMINI_API_KEY=.*$", line, text, count=1)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"

path.write_text(text)
path.chmod(0o600)   # the key is a secret; keep it to this user
print(f"Wrote {key[:12]}...{key[-4:]} to .env")

# The installed app keeps its own copy; update it too so it does not go stale.
runtime = pathlib.Path.home() / "Library/Application Support/Mint/.env"
if runtime.parent.exists():
    runtime.write_text(text)
    runtime.chmod(0o600)
    print("Updated the installed Mint.app as well (restart it to use the new key).")
PY

echo
echo "Checking the key against the API…"

set -a; source .env; set +a

.venv/bin/python - <<'PY'
import os
import sys

from google import genai

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
try:
    # Listing models checks the credential itself, without depending on any one
    # model still being available - retired models return 404 on a good key.
    names = [model.name for model in client.models.list()]
    print(f"\n  Key works. {len(names)} models available.")

    wanted = os.environ.get("MINT_MODEL", "models/gemini-3.8-live")
    if wanted in names:
        print(f"  {wanted} is available.")
    else:
        live = [name for name in names if "live" in name.lower()]
        print(f"  WARNING: {wanted} is not in your model list.")
        if live:
            print("  Live models you do have:")
            for name in live:
                print(f"    {name}")
            print("  Set MINT_MODEL in .env to one of these.")
    print("\n  Start it with:  ./run.sh --hands-free\n")
except Exception as error:
    message = str(error)
    print(f"\n  Key rejected: {type(error).__name__}")
    if "suspended" in message:
        print("  This project is suspended. Create the key in a NEW project at")
        print("  https://aistudio.google.com/apikey, or use a different Google account.")
    elif "API_KEY_INVALID" in message or "API key not valid" in message:
        print("  The key itself is not valid. Check for a missing or extra character.")
    elif "UNAUTHENTICATED" in message or "401" in message:
        print("  The key was not accepted. Most often this means it was copied")
        print("  incompletely, or it is an OAuth credential rather than an API key.")
        print("  Take it from https://aistudio.google.com/apikey, not the Cloud console.")
    else:
        print(f"  {message[:300]}")
    sys.exit(1)
PY
