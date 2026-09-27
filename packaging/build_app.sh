#!/bin/bash
# Build "Hey Mint.app" for Apple silicon with everything inside it (Python, libraries,
# models, Mint's code), and a DMG to share. No Xcode or Homebrew needed by the people
# who download it.
#
#   packaging/build_app.sh                 # ad-hoc signed (users approve it once in System Settings)
#   SIGN_IDENTITY="Developer ID Application: …" packaging/build_app.sh
#   … plus NOTARY_PROFILE=<keychain profile from `xcrun notarytool store-credentials`> to notarize
#
# Needs on the build Mac: Xcode command line tools, python3 (3.11+), and Homebrew's
# portaudio (PyAudio is compiled against it; the library is copied into the app).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

VERSION="${VERSION:-$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')}"
PY_RELEASE=20260924
PY_VERSION=3.12.14
PY_FILE="cpython-${PY_VERSION}+${PY_RELEASE}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/${PY_FILE/+/%2B}"
CAMPP_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
CAMPP_SHA=aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2
IDENTIFIER="io.github.shivatmax.heymint"

CACHE="$ROOT/build/cache"
DIST="$ROOT/dist"
APP="$DIST/Hey Mint.app"
RES="$APP/Contents/Resources"
mkdir -p "$CACHE" "$DIST"

[[ "$(uname -m)" == "arm64" ]] || { echo "Build on an Apple silicon Mac (the app is arm64)." >&2; exit 1; }
brew --prefix portaudio >/dev/null 2>&1 || { echo "Install portaudio first: brew install portaudio" >&2; exit 1; }

# --- a self-contained Python --------------------------------------------------------------
if [[ ! -f "$CACHE/$PY_FILE" ]]; then
  echo "Downloading Python $PY_VERSION…"
  curl -fsSL -o "$CACHE/$PY_FILE.part" "$PY_URL"
  curl -fsSL -o "$CACHE/SHA256SUMS-$PY_RELEASE" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/SHA256SUMS"
  want=$(grep " ${PY_FILE}\$" "$CACHE/SHA256SUMS-$PY_RELEASE" | cut -d' ' -f1)
  have=$(shasum -a 256 "$CACHE/$PY_FILE.part" | cut -d' ' -f1)
  [[ -n "$want" && "$want" == "$have" ]] || { echo "Python download failed its checksum" >&2; exit 1; }
  mv "$CACHE/$PY_FILE.part" "$CACHE/$PY_FILE"
fi

echo "Assembling Hey Mint.app $VERSION…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES/runtime" "$RES/app/models"
tar -xzf "$CACHE/$PY_FILE" -C "$RES/runtime"          # -> runtime/python
PYHOME="$RES/runtime/python"
PY="$PYHOME/bin/python3"
ln -sf python3 "$PYHOME/bin/python"                   # the launcher runs .venv/bin/python

echo "Installing Mint's libraries…"
"$PY" -m pip install --quiet --no-cache-dir --disable-pip-version-check -r requirements.txt

# Libraries linked from outside the app (portaudio) are copied in and relinked.
TOOLS="$ROOT/build/tools"
[[ -x "$TOOLS/bin/delocate-path" ]] || { python3 -m venv "$TOOLS" && "$TOOLS/bin/pip" install --quiet delocate; }
SITE=$("$PY" -c 'import site; print(site.getsitepackages()[0])')
"$TOOLS/bin/delocate-path" -L .dylibs "$SITE/pyaudio" >/dev/null

echo "Fetching models…"
"$PY" - <<'PY'
import socket
socket.setdefaulttimeout(60)
import os
import openwakeword, openwakeword.utils as u
# Only the two shared feature models Mint's own detector uses. openWakeWord's prebuilt
# wake word models carry a non-commercial licence and are not needed.
target = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
os.makedirs(target, exist_ok=True)
for model in openwakeword.FEATURE_MODELS.values():
    for url in (model["download_url"], model["download_url"].replace(".tflite", ".onnx")):
        if url.endswith(".onnx") and not os.path.exists(os.path.join(target, url.split("/")[-1])):
            u.download_file(url, target)
PY
if [[ ! -f "$CACHE/campplus.onnx" ]]; then
  curl -fsSL -o "$CACHE/campplus.onnx" "$CAMPP_URL"
fi
[[ "$(shasum -a 256 "$CACHE/campplus.onnx" | cut -d' ' -f1)" == "$CAMPP_SHA" ]] || { echo "voice lock model checksum mismatch" >&2; exit 1; }
cp "$CACHE/campplus.onnx" models/hey_mint.json models/hey_mint_data.npz "$RES/app/models/"

# Slim the runtime: tests, headers, pip and the parts of the standard library Mint never uses.
rm -rf "$PYHOME/include" "$PYHOME/share" "$PYHOME/lib/pkgconfig"
STD="$PYHOME/lib/python3.12"
rm -rf "$STD/test" "$STD/idlelib" "$STD/tkinter" "$STD/turtledemo" "$STD/ensurepip" "$STD/lib2to3"
find "$SITE" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} +
rm -rf "$SITE/PyObjCTest" "$PYHOME"/lib/libtcl* "$PYHOME"/lib/libtk* "$PYHOME"/lib/tcl* "$PYHOME"/lib/tk* "$PYHOME"/lib/itcl* "$PYHOME"/lib/thread*
"$PY" -m pip uninstall --quiet --yes pip >/dev/null 2>&1 || true
find "$PYHOME" -name '__pycache__' -type d -prune -exec rm -rf {} +
"$PY" -m compileall -q -j 0 "$STD" "$SITE" >/dev/null || true

# --- Mint's code ----------------------------------------------------------------------------
rsync -a --exclude '__pycache__' mint "$RES/app/"
cp custom.example.json .env.example "$RES/app/"
echo "$VERSION-$(git rev-parse --short HEAD 2>/dev/null || date +%s)" > "$RES/app/BUILD_ID"

# --- the launcher, icon and Info.plist ------------------------------------------------------
xcrun swiftc -O -target arm64-apple-macos14.2 launcher/ear/*.swift -o "$APP/Contents/MacOS/Mint"
"$PY" launcher/make_icon.py "$RES/Mint.icns" >/dev/null
BUILD_NUMBER="${BUILD_NUMBER:-$(git rev-list --count HEAD 2>/dev/null || echo 1)}"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>          <string>$IDENTIFIER</string>
  <key>CFBundleName</key>                <string>Hey Mint</string>
  <key>CFBundleDisplayName</key>         <string>Hey Mint</string>
  <key>CFBundleExecutable</key>          <string>Mint</string>
  <key>CFBundleIconFile</key>            <string>Mint</string>
  <key>CFBundlePackageType</key>         <string>APPL</string>
  <key>CFBundleShortVersionString</key>  <string>$VERSION</string>
  <key>CFBundleVersion</key>             <string>$BUILD_NUMBER</string>
  <key>LSMinimumSystemVersion</key>      <string>14.2</string>
  <key>LSArchitecturePriority</key>      <array><string>arm64</string></array>
  <key>LSUIElement</key>                 <true/>
  <key>NSHighResolutionCapable</key>     <true/>
  <key>NSHumanReadableCopyright</key>    <string>Hey Mint contributors, GPL-3.0</string>
  <key>MintArguments</key>               <array><string>--hands-free</string></array>

  <key>NSMicrophoneUsageDescription</key>
  <string>Mint listens for its wake word on this Mac, and streams your voice to Gemini only after you say it.</string>
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
</dict>
</plist>
PLIST

# --- signing ------------------------------------------------------------------------------
# Every executable and library inside is signed first, then the app. With a Developer ID
# the hardened runtime is on, with the exceptions an embedded Python needs.
ENTITLEMENTS="$ROOT/packaging/entitlements.plist"
IDENTITY="${SIGN_IDENTITY:--}"
sign_flags=(--force --sign "$IDENTITY")
[[ "$IDENTITY" != "-" ]] && sign_flags+=(--options runtime --timestamp --entitlements "$ENTITLEMENTS")
echo "Signing ($([[ "$IDENTITY" == "-" ]] && echo ad-hoc || echo "$IDENTITY"))…"
while IFS= read -r -d '' file; do
  if file -b "$file" | grep -q "Mach-O"; then codesign "${sign_flags[@]}" "$file" 2>/dev/null; fi
done < <(find "$RES" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0)
codesign "${sign_flags[@]}" "$APP"
codesign --verify --deep --strict "$APP"

# --- the DMG --------------------------------------------------------------------------------
DMG="$DIST/Hey-Mint-$VERSION-arm64.dmg"
STAGE="$ROOT/build/dmg"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Hey Mint.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "Hey Mint" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
[[ "$IDENTITY" != "-" ]] && codesign --force --sign "$IDENTITY" --timestamp "$DMG"
if [[ -n "${NOTARY_PROFILE:-}" && "$IDENTITY" != "-" ]]; then
  echo "Notarizing…"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
fi
shasum -a 256 "$DMG" | tee "$DMG.sha256"
du -sh "$APP" "$DMG"
