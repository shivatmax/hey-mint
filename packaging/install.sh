#!/bin/bash
# Installs Hey Mint and approves it with macOS, in one go.
#
#   curl -fsSL https://hey-mint.pages.dev/install.sh | bash     # downloads the latest release
#   Install Hey Mint.command                                     # the same, from the DMG (uses the app beside it)
#
# Why it exists: Hey Mint is free and open source and not notarized by Apple (that needs a paid developer account),
# so macOS puts a "downloaded from the internet" mark on it and, the first time, says it can't check it. This
# installs the app, removes that mark from the copy it installs (the same thing System Settings > Privacy &
# Security > Open Anyway does, without the hunt) and opens it. Nothing else is changed; it reads no files
# of yours. Read it first if you like: it is this file.
#
# In a terminal it then asks two things, Enter keeping the default: your Gemini API key (typed hidden, checked
# with Google, saved only to Hey Mint's .env, mode 600; Enter skips: the welcome window asks for it) and where
# Mint lives (in the notch, or floating on screen). Nothing is asked, and nothing more is changed, with --yes
# or without a terminal (curl ... | bash): the welcome window asks both.
#
#   curl -fsSL https://hey-mint.pages.dev/install.sh | bash -s -- --yes
#
# Options: --yes, -y               ask nothing, keep the defaults
# (environment)  HEYMINT_DEST=<folder>   where to install (default /Applications, or ~/Applications)
#                HEYMINT_NO_OPEN=1      don't open it afterwards
#                HEYMINT_DMG=<file>     install from this DMG instead of downloading
#                MINT_ASK=1             ask even without a terminal (answers piped in, for tests)
set -euo pipefail

REPO="shivatmax/hey-mint"
NAME="Hey Mint"
DATA="$HOME/Library/Application Support/$NAME"     # where the app keeps its .env and settings (Packaged.swift)
say()  { printf '\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die()  { printf '\033[31mCould not install %s: %s\033[0m\n' "$NAME" "$*" >&2; exit 1; }

YES=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    *) die "unknown option $arg (the only one is --yes)" ;;
  esac
done
# Ask only a person at a terminal; MINT_ASK=1 asks anyway (answers piped in). --yes always wins.
ASK=0
if (( YES == 0 )) && { [[ "${MINT_ASK:-}" == "1" ]] || [[ -t 0 && -t 1 ]]; }; then ASK=1; fi

[[ "$(uname -s)" == "Darwin" ]] || die "this is a Mac app."
[[ "$(uname -m)" == "arm64" ]] || die "Hey Mint needs a Mac with Apple silicon (M1 or newer)."
macos="$(sw_vers -productVersion)"
IFS=. read -r major minor _ <<<"$macos"
if (( major < 14 || (major == 14 && ${minor:-0} < 2) )); then die "it needs macOS 14.2 or newer (this Mac has $macos)."; fi

say "Installing $NAME"
WORK="$(mktemp -d)"
MOUNT=""
cleanup() {
  [[ -n "$MOUNT" ]] && hdiutil detach "$MOUNT" -quiet -force >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# --- where the app comes from -------------------------------------------------------------------------
# Always the newest release. Run from a downloaded DMG ("Install Hey Mint.command"), the app beside the script is
# used only if it is already the newest (or GitHub can't be reached): an old DMG used to install its old version.
LATEST="" FILE="" WANT=""
newest() {        # sets LATEST, FILE, WANT (empty if GitHub can't be reached); never fails
  local api
  if curl -fsSL -H 'Cache-Control: no-cache' "https://github.com/$REPO/releases/latest/download/latest.json" \
       -o "$WORK/latest.json" 2>/dev/null; then
    LATEST="$(plutil -extract version raw -o - "$WORK/latest.json" 2>/dev/null || true)"
    FILE="$(plutil -extract dmg raw -o - "$WORK/latest.json" 2>/dev/null || true)"
    WANT="$(plutil -extract sha256 raw -o - "$WORK/latest.json" 2>/dev/null || true)"
  fi
  if [[ -z "$LATEST" || -z "$FILE" ]]; then
    # The newest release may still be building (no latest.json yet): ask GitHub's API for its DMG instead.
    api="$(curl -fsSL -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null)" || return 0
    LATEST="$(printf '%s' "$api" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("vV"))' 2>/dev/null || true)"
    FILE="$(printf '%s' "$api" | /usr/bin/python3 -c 'import json,sys
for a in json.load(sys.stdin).get("assets", []):
    if a["name"].endswith("-arm64.dmg"): print(a["name"]); break' 2>/dev/null || true)"
    WANT="$(printf '%s' "$api" | /usr/bin/python3 -c 'import json,sys
for a in json.load(sys.stdin).get("assets", []):
    if a["name"].endswith("-arm64.dmg") and str(a.get("digest","")).startswith("sha256:"): print(a["digest"][7:]); break' 2>/dev/null || true)"
  fi
  [[ -n "$LATEST" && -n "$FILE" ]] || { LATEST=""; FILE=""; WANT=""; }
}
version_of() { plutil -extract CFBundleShortVersionString raw -o - "$1/Contents/Info.plist" 2>/dev/null || echo 0; }
older() {         # older A B: is version A older than B?
  [[ "$1" != "$2" ]] && [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" == "$1" ]]
}

SRC=""
DMG="${HEYMINT_DMG:-}"
if [[ -z "$DMG" && -n "${BASH_SOURCE[0]:-}" && -d "$(dirname "${BASH_SOURCE[0]}")/$NAME.app" ]]; then
  BESIDE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$NAME.app"
  note "Checking for a newer version than the one in this disk image…"
  newest
  here="$(version_of "$BESIDE")"
  if [[ -n "$LATEST" ]] && older "$here" "$LATEST"; then
    note "This disk image has $NAME $here; $LATEST is out - installing that instead."
  else
    SRC="$BESIDE"
    note "Using the app next to this script ($here)."
  fi
fi
if [[ -z "$SRC" ]]; then
  if [[ -z "$DMG" ]]; then
    [[ -n "$LATEST" ]] || { note "Finding the latest release…"; newest; }
    [[ -n "$LATEST" ]] \
      || die "could not reach GitHub. Check the internet connection, or download the DMG from https://github.com/$REPO/releases/latest"
    note "Downloading $NAME ${LATEST}…"
    curl -fL --progress-bar "https://github.com/$REPO/releases/download/v$LATEST/$FILE" -o "$WORK/$FILE" \
      || die "the download failed."
    if [[ -n "$WANT" ]]; then
      have="$(shasum -a 256 "$WORK/$FILE" | cut -d' ' -f1)"
      [[ "$have" == "$WANT" ]] || die "the download is corrupt (its checksum doesn't match). Try again."
    fi
    DMG="$WORK/$FILE"
  fi
  MOUNT="$WORK/mount"
  mkdir -p "$MOUNT"
  hdiutil attach "$DMG" -nobrowse -readonly -noverify -mountpoint "$MOUNT" -quiet || die "could not open the disk image."
  SRC="$MOUNT/$NAME.app"
  [[ -d "$SRC" ]] || die "the disk image has no $NAME.app."
fi

# --- where it goes ------------------------------------------------------------------------------------
DEST_DIR="${HEYMINT_DEST:-}"
if [[ -z "$DEST_DIR" ]]; then
  if [[ -w /Applications ]]; then DEST_DIR=/Applications; else DEST_DIR="$HOME/Applications"; fi
fi
mkdir -p "$DEST_DIR"
DEST="$DEST_DIR/$NAME.app"

# A running copy is asked to quit first (its settings, voice and memory live in ~/Library and stay).
running() { ps -axo comm= | grep -Fx "$DEST/Contents/MacOS/Mint" >/dev/null; }
if running; then
  note "${NAME} is running: asking it to quit…"
  osascript -e "tell application \"$NAME\" to quit" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do running || break; sleep 1; done
  running && die "$NAME is still running. Quit it (menu bar icon ▸ Quit) and run this again."
fi

note "Copying to ${DEST_DIR}…"
rm -rf "$DEST"
ditto "$SRC" "$DEST" || die "could not copy the app to $DEST_DIR."

# --- approving it ---------------------------------------------------------------------------------------
note "Approving it with macOS (it is not notarized: Apple charges developers for that)…"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
xattr -dr com.apple.provenance "$DEST" 2>/dev/null || true
codesign --verify --deep --strict "$DEST" >/dev/null 2>&1 || die "the app's signature didn't check out, so it was not approved."

say "Installed: $DEST ($NAME $(version_of "$DEST"))"

# --- a few choices (in a terminal; Enter keeps the default) -----------------------------------------------
# One line of input; piped answers are echoed so the transcript reads right. No more input: the default.
answer() {
  local line=""
  IFS= read -r line || true
  [[ -t 0 ]] || printf '%s\n' "$line"
  REPLY="$line"
}
hidden() {
  local line=""
  IFS= read -rs line || true
  printf '\n'
  REPLY="$line"
}
# Google's own answer to listing one model with this key: ok | bad | offline. The key goes in a header read
# from stdin, never in the URL or on a command line.
check_key() {
  local code
  code="$(printf 'x-goog-api-key: %s\n' "$1" | curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H @- \
    "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1" 2>/dev/null)" || true
  case "$code" in
    200|429) echo ok ;;
    ""|000) echo offline ;;
    *) echo bad ;;
  esac
}
# Into the app's .env (mode 600), keeping every other line, as the app's own key window does.
save_key() {
  local env="$DATA/.env" tmp
  mkdir -p "$DATA"
  tmp="$(umask 077 && mktemp "$DATA/.env.XXXXXX")"
  if [[ -f "$env" ]]; then grep -vE '^[[:space:]]*(export[[:space:]]+)?GEMINI_API_KEY=' "$env" >"$tmp" || true; fi
  printf 'export GEMINI_API_KEY=%s\n' "$1" >>"$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$env"
}
ask_key() {
  local have key verdict
  have="$(grep -E '^[[:space:]]*(export[[:space:]]+)?GEMINI_API_KEY=' "$DATA/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d "\"' ")" || have=""
  printf '\n'
  if [[ -n "$have" ]]; then
    note "A Gemini key is already saved (••••${have: -4})."
  else
    note "Mint talks through Google's Gemini, with your own API key (free: https://aistudio.google.com/apikey)."
  fi
  for _ in 1 2 3; do
    if [[ -n "$have" ]]; then printf '  Paste a new Gemini API key (hidden), or Enter to keep it: '
    else printf '  Paste your Gemini API key (hidden), or Enter to add it later: '; fi
    hidden
    key="${REPLY//[[:space:]]/}"
    if [[ -z "$key" ]]; then
      if [[ -n "$have" ]]; then note "Kept the key you had."; else note "Skipped: you can add it in the welcome window."; fi
      return 0
    fi
    if (( ${#key} < 20 )); then note "That doesn't look like a whole key. Copy it again from AI Studio."; continue; fi
    note "Checking it with Google..."
    verdict="$(check_key "$key")"
    if [[ "$verdict" == "bad" ]]; then note "Google didn't accept that key. Copy it again from AI Studio (no spaces)."; continue; fi
    save_key "$key"
    if [[ "$verdict" == "ok" ]]; then note "Gemini key saved (••••${key: -4}): Google accepted it."
    else note "Gemini key saved (••••${key: -4}). Couldn't reach Google to check it: Mint will when you're online."; fi
    return 0
  done
  note "Skipped: you can add the key in the welcome window."
}
# Where Mint lives: settings.json's notch_mode, written before Mint opens (so nothing else is writing it).
ask_home() {
  local py="$DEST/Contents/Resources/runtime/python/bin/python3" settings="$DATA/settings.json" now pick
  [[ -x "$py" ]] || return 0
  if ps -axo comm= | grep -E '/Hey Mint\.app/Contents/MacOS/Mint$' >/dev/null; then
    note "Hey Mint is running elsewhere: choose where it lives in its welcome window or Settings."
    return 0
  fi
  now="$("$py" -I -B -c 'import json, sys
try:
    print("float" if json.load(open(sys.argv[1])).get("notch_mode") is False else "notch")
except Exception:
    print("notch")' "$settings" 2>/dev/null)" || now="notch"
  [[ "$now" == "float" ]] && pick=2 || pick=1
  printf '\n  Where should Mint live?\n    1. In the notch, at the top of the screen\n    2. Floating on screen\n'
  printf '  Choose [%s] ' "$pick"
  answer
  case "${REPLY// /}" in
    1|n|notch) pick=1 ;;
    2|f|float|floating) pick=2 ;;
    "") ;;
    *) note "Kept $([[ $pick == 1 ]] && echo 'the notch' || echo 'floating')." ;;
  esac
  mkdir -p "$DATA"
  if "$py" -I -B - "$settings" "$pick" <<'PY'
import json, os, sys
path, notch = sys.argv[1], sys.argv[2] == "1"
try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
if not isinstance(data, dict):
    sys.exit(1)
data["notch_mode"] = notch
tmp = path + ".install"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
os.replace(tmp, path)
PY
  then note "Mint will live $([[ $pick == 1 ]] && echo 'in the notch' || echo 'floating on screen') (change it any time in Settings)."
  else note "Couldn't save that (the settings file didn't read as JSON): choose in the welcome window."; fi
}

if (( ASK )); then
  ask_key
  ask_home
else
  note "Kept the defaults: the welcome window asks for the Gemini key and the rest."
fi

if [[ "${HEYMINT_NO_OPEN:-}" != "1" ]]; then
  note "Opening it. The welcome window walks you through the rest."
  open "$DEST"
fi
