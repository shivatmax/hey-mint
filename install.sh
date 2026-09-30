#!/bin/bash
# Build Mint.app and install it to ~/Applications.
#
#   ./install.sh            build and install
#   ./install.sh --login    also start Mint automatically at login
#   ./install.sh --remove   quit, remove the app and the login item
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

APP_NAME="Mint"
BUILD="$ROOT/build/$APP_NAME.app"
INSTALL="$HOME/Applications/$APP_NAME.app"
AGENT="$HOME/Library/LaunchAgents/local.mint.plist"
IDENTIFIER="local.jarvis"   # unchanged on purpose: macOS keeps permissions by bundle id
# The app runs from here, not from this checkout. Folders like Downloads and
# Documents are privacy-protected: a new app reading them stalls at launch while
# macOS asks permission. Application Support is not protected.
RUNTIME="$HOME/Library/Application Support/Mint"

remove() {
  pkill -x "$APP_NAME" 2>/dev/null || true
  pkill -x MintEngine 2>/dev/null || true
  pkill -x MintRecorder 2>/dev/null || true
  pkill -x MintScreen 2>/dev/null || true
  pkill -f "python -m mint" 2>/dev/null || true
  if [[ -f "$AGENT" ]]; then
    launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
    rm -f "$AGENT"
  fi
  rm -rf "$INSTALL" "$RUNTIME"
  echo "Removed $INSTALL, its runtime, and the login item."
}

if [[ "${1:-}" == "--remove" ]]; then
  remove
  exit 0
fi

# --- one-time move from the assistant's old name -------------------------------------
# Everything it learned comes along: voiceprint, memory, skills, settings, history.
OLD="Jar""vis"
OLD_RUNTIME="$HOME/Library/Application Support/$OLD"
RELOGIN=0
if [[ -d "$OLD_RUNTIME" && ! -d "$RUNTIME" ]]; then
  echo "Moving your data from $OLD_RUNTIME to ${RUNTIME}…"
  pkill -x "$OLD" 2>/dev/null || true
  pkill -f "python -m $(echo "$OLD" | tr '[:upper:]' '[:lower:]')" 2>/dev/null || true
  sleep 2
  mv "$OLD_RUNTIME" "$RUNTIME"
  rm -rf "$RUNTIME/$(echo "$OLD" | tr '[:upper:]' '[:lower:]')"
  "$RUNTIME/.venv/bin/python" - "$RUNTIME" "$OLD" <<'PY' || true
import pathlib, sys
root, old = pathlib.Path(sys.argv[1]), sys.argv[2]
files = [root / "summary.md", root / "memories.md", root / "history.jsonl"]
files += list((root / "memory").rglob("*")) + list((root / "skills").rglob("*.md"))
for path in files:
    if path.is_file():
        text = path.read_text(errors="ignore")
        new = text.replace(old, "Mint").replace(old.lower(), "mint")
        if new != text:
            path.write_text(new)
PY
fi
OLD_LOGS="$HOME/Library/Logs/$OLD"
if [[ -d "$OLD_LOGS" && ! -d "$HOME/Library/Logs/Mint" ]]; then
  mv "$OLD_LOGS" "$HOME/Library/Logs/Mint"
  [[ -f "$HOME/Library/Logs/Mint/$(echo "$OLD" | tr '[:upper:]' '[:lower:]').log" ]] && \
    mv "$HOME/Library/Logs/Mint/$(echo "$OLD" | tr '[:upper:]' '[:lower:]').log" "$HOME/Library/Logs/Mint/mint.log"
fi
OLD_AGENT="$HOME/Library/LaunchAgents/local.$(echo "$OLD" | tr '[:upper:]' '[:lower:]').plist"
if [[ -f "$OLD_AGENT" ]]; then
  launchctl bootout "gui/$(id -u)" "$OLD_AGENT" 2>/dev/null || true
  rm -f "$OLD_AGENT"
  RELOGIN=1                      # it started at login before; keep doing that as Mint
fi
rm -rf "$HOME/Applications/$OLD.app"

# --- runtime -----------------------------------------------------------------------
echo "Installing the runtime to ${RUNTIME}…"
mkdir -p "$RUNTIME"
rm -rf "$RUNTIME/mint"
ditto "$ROOT/mint" "$RUNTIME/mint"
find "$RUNTIME/mint" -name '__pycache__' -type d -prune -exec rm -rf {} +
# The welcome tour's clips (the rest stream from the guide site).
if [[ -d "$ROOT/guide/media" ]]; then
  mkdir -p "$RUNTIME/media/tour"
  for clip in doing island-schedule island-meeting dictation clipboard-window convert video-edit translate drop \
              agent island-teach island-trackers image-card apple-shortcuts music connectors settings; do
    for ext in mp4 jpg; do
      [[ -f "$ROOT/guide/media/$clip.$ext" ]] && cp "$ROOT/guide/media/$clip.$ext" "$RUNTIME/media/tour/"
    done
  done
fi
cp "$ROOT/requirements.txt" "$RUNTIME/"
if [[ -f "$ROOT/.env" ]]; then
  # Merge, never overwrite: keys saved in Settings (a Telegram bot token, a changed API key) live only in the
  # installed copy, and copying the checkout's .env over it wiped them on every install.
  /usr/bin/python3 - "$ROOT/.env" "$RUNTIME/.env" <<'PY'
import os, sys
def read(path):
    keys, lines = {}, []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8").read().splitlines():
            name = line.strip().removeprefix("export ").split("=", 1)[0].strip()
            if "=" in line and name and not line.lstrip().startswith("#"):
                keys[name] = line
            lines.append(line)
    return keys, lines
repo, _ = read(sys.argv[1])
installed, lines = read(sys.argv[2])
lines += [line for name, line in repo.items() if name not in installed]
with open(sys.argv[2], "w", encoding="utf-8") as out:
    out.write("\n".join(lines).strip("\n") + "\n")
PY
  chmod 600 "$RUNTIME/.env"
fi
cp "$ROOT/custom.example.json" "$RUNTIME/custom.example.json"
# Your customisation is yours: seed it once, never overwrite it.
if [[ ! -f "$RUNTIME/custom.json" && -f "$ROOT/custom.json" ]]; then
  cp "$ROOT/custom.json" "$RUNTIME/custom.json"
fi
# Sub-agents: seeded once from the checkout; the user's own copy is never overwritten.
if [[ ! -f "$RUNTIME/agents.json" && -f "$ROOT/agents.json" ]]; then
  cp "$ROOT/agents.json" "$RUNTIME/agents.json"
fi
# The voice lock's speaker model (CAM++, 28 MB). Your voiceprint lives beside
# the runtime (voiceprint.json) and is never overwritten here.
mkdir -p "$RUNTIME/models"
CAMPP_SHA=aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2
# "Hey Mint": the shipped detector and the data "Train my voice" retrains it with.
cp "$ROOT/models/hey_mint.json" "$ROOT/models/hey_mint_data.npz" "$RUNTIME/models/"
if [[ -f "$ROOT/models/campplus.onnx" ]]; then
  cp "$ROOT/models/campplus.onnx" "$RUNTIME/models/campplus.onnx"
elif [[ ! -f "$RUNTIME/models/campplus.onnx" ]]; then
  echo "Downloading the voice lock model (28 MB)…"
  curl -fsSL --max-time 300 -o "$RUNTIME/models/campplus.onnx" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx" \
    || echo "Warning: voice lock model not downloaded; the voice lock stays off." >&2
fi
if [[ -f "$RUNTIME/models/campplus.onnx" ]] && \
   [[ "$(shasum -a 256 "$RUNTIME/models/campplus.onnx" | cut -d' ' -f1)" != "$CAMPP_SHA" ]]; then
  echo "Warning: the voice lock model does not match its checksum; removing it." >&2
  rm -f "$RUNTIME/models/campplus.onnx"
fi
if [[ ! -x "$RUNTIME/.venv/bin/python" ]]; then
  echo "Creating the Python environment (first install only)…"
  python3 -m venv "$RUNTIME/.venv"
fi
"$RUNTIME/.venv/bin/python" -m pip install -q --upgrade pip
"$RUNTIME/.venv/bin/python" -m pip install -q -r "$RUNTIME/requirements.txt"
# Wake word models. Copy them from this checkout's environment when it has them:
# downloading stalled mid-file during testing and hung the installer for ten
# minutes. Otherwise download, with a hard timeout.
MODELS_REL="lib/python3.14/site-packages/openwakeword/resources/models"
MODELS_DST=$("$RUNTIME/.venv/bin/python" -c "import openwakeword, os; print(os.path.join(os.path.dirname(openwakeword.__file__), 'resources', 'models'))")
mkdir -p "$MODELS_DST"
if [[ -d "$ROOT/.venv/$MODELS_REL" ]]; then
  cp "$ROOT/.venv/$MODELS_REL"/* "$MODELS_DST"/ 2>/dev/null || true
fi
if [[ ! -f "$MODELS_DST/melspectrogram.onnx" || ! -f "$MODELS_DST/embedding_model.onnx" ]]; then
  echo "Downloading wake word models…"
  "$RUNTIME/.venv/bin/python" - <<'PY' || echo "Warning: wake word models incomplete; hands-free mode will fail until they download." >&2
import socket, sys, threading
socket.setdefaulttimeout(30)
import openwakeword.utils as u
done = threading.Event()
def fetch():
    u.download_models()
    done.set()
threading.Thread(target=fetch, daemon=True).start()
sys.exit(0 if done.wait(180) else 1)
PY
fi

# --- bundle --------------------------------------------------------------------------
echo "Building $APP_NAME.app…"
rm -rf "$BUILD"
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"

xcrun swiftc -O launcher/ear/*.swift -o "$BUILD/Contents/MacOS/$APP_NAME"   # Mint Ear + launcher (launcher/ear/main.swift)
# The screen-control engine behind the `desktop` tool (launcher/engine, from jev-use).
# Mint starts it on demand; without it (or without a TypeSafe key) Mint uses ui_act.
echo "Building the screen-control engine…"
if engine_log=$(cd launcher/engine && xcrun swift build -c release 2>&1); then
  cp "$(cd launcher/engine && xcrun swift build -c release --show-bin-path)/JevDesktop" "$BUILD/Contents/MacOS/MintEngine"
else
  printf '%s\n' "$engine_log" | tail -15 >&2
  echo "Warning: the screen-control engine did not build; Mint works without the desktop tool." >&2
fi
# Meeting notes: records the call's audio (a Core Audio tap) and the mic as two tracks (launcher/recorder).
echo "Building the meeting recorder…"
if ! recorder_log=$(xcrun swiftc -O launcher/recorder/*.swift -o "$BUILD/Contents/MacOS/MintRecorder" \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker launcher/recorder/Info.plist 2>&1); then
  printf '%s\n' "$recorder_log" | tail -15 >&2
  echo "Warning: the meeting recorder did not build; Mint works without meeting notes." >&2
fi
# Screen recordings: ScreenCaptureKit into an .mp4, a window or an area (launcher/screenrec).
echo "Building the screen recorder…"
if ! screen_log=$(xcrun swiftc -O launcher/screenrec/*.swift -o "$BUILD/Contents/MacOS/MintScreen" \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker launcher/screenrec/Info.plist 2>&1); then
  printf '%s\n' "$screen_log" | tail -15 >&2
  echo "Warning: the screen recorder did not build; Mint works without screen recordings." >&2
fi
"$RUNTIME/.venv/bin/python" launcher/make_icon.py "$BUILD/Contents/Resources/$APP_NAME.icns" >/dev/null
rm -f "$BUILD/Contents/Resources/$APP_NAME.png"

cat > "$BUILD/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>          <string>$IDENTIFIER</string>
  <key>CFBundleName</key>                <string>$APP_NAME</string>
  <key>CFBundleDisplayName</key>         <string>$APP_NAME</string>
  <key>CFBundleExecutable</key>          <string>$APP_NAME</string>
  <key>CFBundleIconFile</key>            <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>         <string>APPL</string>
  <key>CFBundleShortVersionString</key>  <string>0.2.0</string>
  <key>CFBundleVersion</key>             <string>2</string>
  <key>LSMinimumSystemVersion</key>      <string>14.2</string>
  <key>LSUIElement</key>                 <true/>
  <key>NSHighResolutionCapable</key>     <true/>

  <key>MintProjectRoot</key>           <string>$RUNTIME</string>
  <key>MintArguments</key>             <array><string>--hands-free</string></array>

  <key>NSMicrophoneUsageDescription</key>
  <string>Mint listens for its wake word on this Mac, and streams your voice to Gemini only after you say it.</string>
  <key>NSAudioCaptureUsageDescription</key>
  <string>Mint records a call's audio only when you click Record meeting, to write its transcript and notes.</string>
  <key>NSAppleEventsUsageDescription</key>
  <string>Mint controls apps such as Notes, Mail and System Events when you ask it to.</string>
  <key>NSCalendarsFullAccessUsageDescription</key>
  <string>Mint reads your calendar when you ask what is on your schedule.</string>
  <key>NSCalendarsUsageDescription</key>
  <string>Mint reads your calendar when you ask what is on your schedule.</string>
  <key>NSRemindersFullAccessUsageDescription</key>
  <string>Mint creates reminders when you ask it to.</string>
  <key>NSRemindersUsageDescription</key>
  <string>Mint creates reminders when you ask it to.</string>
  <key>NSDesktopFolderUsageDescription</key>
  <string>Mint finds and opens the files and folders you ask for.</string>
  <key>NSDocumentsFolderUsageDescription</key>
  <string>Mint finds and opens the files and folders you ask for.</string>
  <key>NSDownloadsFolderUsageDescription</key>
  <string>Mint finds and opens the files and folders you ask for.</string>
</dict>
</plist>
PLIST

# --- signing -------------------------------------------------------------------------
# A stable signature keeps macOS permissions across rebuilds; an ad-hoc one
# changes every build, and macOS then forgets Microphone and Accessibility.
python3 launcher/sign.py "$BUILD" "$IDENTIFIER"
codesign --verify --strict "$BUILD"

# --- install -------------------------------------------------------------------------
pkill -x "$APP_NAME" 2>/dev/null || true
pkill -x MintEngine 2>/dev/null || true
pkill -x MintRecorder 2>/dev/null || true
pkill -x MintScreen 2>/dev/null || true
mkdir -p "$HOME/Applications"
rm -rf "$INSTALL"
ditto "$BUILD" "$INSTALL"
echo "Installed: $INSTALL"

if [[ "${1:-}" == "--login" || "$RELOGIN" == 1 ]]; then
  mkdir -p "$(dirname "$AGENT")"
  cat > "$AGENT" <<AGENTPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>local.mint</string>
  <!-- Through LaunchServices, so macOS treats it as Mint.app for permissions. -->
  <key>ProgramArguments</key>  <array><string>/usr/bin/open</string><string>-g</string><string>$INSTALL</string></array>
  <key>RunAtLoad</key>         <true/>
</dict>
</plist>
AGENTPLIST
  launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENT"
  echo "Mint will start at login. (Undo: ./install.sh --remove)"
fi

echo
echo "Start it:  open ~/Applications/Mint.app"
