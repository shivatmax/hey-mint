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
# Options (environment): HEYMINT_DEST=<folder>   where to install (default /Applications, or ~/Applications)
#                        HEYMINT_NO_OPEN=1      don't open it afterwards
#                        HEYMINT_DMG=<file>     install from this DMG instead of downloading
set -euo pipefail

REPO="shivatmax/hey-mint"
NAME="Hey Mint"
say()  { printf '\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die()  { printf '\033[31mCould not install %s: %s\033[0m\n' "$NAME" "$*" >&2; exit 1; }

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
SRC=""
if [[ -n "${BASH_SOURCE[0]:-}" && -d "$(dirname "${BASH_SOURCE[0]}")/$NAME.app" ]]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$NAME.app"          # run from the opened DMG
  note "Using the app next to this script."
else
  DMG="${HEYMINT_DMG:-}"
  if [[ -z "$DMG" ]]; then
    note "Finding the latest release…"
    curl -fsSL "https://github.com/$REPO/releases/latest/download/latest.json" -o "$WORK/latest.json" \
      || die "could not reach GitHub. Check the internet connection, or download the DMG from https://github.com/$REPO/releases"
    file="$(plutil -extract dmg raw -o - "$WORK/latest.json")"
    want="$(plutil -extract sha256 raw -o - "$WORK/latest.json")"
    version="$(plutil -extract version raw -o - "$WORK/latest.json")"
    note "Downloading $NAME ${version}…"
    curl -fL --progress-bar "https://github.com/$REPO/releases/download/v$version/$file" -o "$WORK/$file" \
      || die "the download failed."
    have="$(shasum -a 256 "$WORK/$file" | cut -d' ' -f1)"
    [[ "$have" == "$want" ]] || die "the download is corrupt (its checksum doesn't match). Try again."
    DMG="$WORK/$file"
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
if pgrep -f "$DEST/Contents/MacOS/" >/dev/null 2>&1; then
  note "$NAME is running: asking it to quit…"
  osascript -e "tell application \"$NAME\" to quit" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "$DEST/Contents/MacOS/" >/dev/null 2>&1 || break; sleep 1; done
  pgrep -f "$DEST/Contents/MacOS/" >/dev/null 2>&1 && die "$NAME is still running. Quit it (menu bar icon ▸ Quit) and run this again."
fi

note "Copying to ${DEST_DIR}…"
rm -rf "$DEST"
ditto "$SRC" "$DEST" || die "could not copy the app to $DEST_DIR."

# --- approving it ---------------------------------------------------------------------------------------
note "Approving it with macOS (it is not notarized: Apple charges developers for that)…"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
xattr -dr com.apple.provenance "$DEST" 2>/dev/null || true
codesign --verify --deep --strict "$DEST" >/dev/null 2>&1 || die "the app's signature didn't check out, so it was not approved."

say "Installed: $DEST"
if [[ "${HEYMINT_NO_OPEN:-}" != "1" ]]; then
  note "Opening it. It will ask for a Gemini key (free) and walk you through the rest."
  open "$DEST"
fi
