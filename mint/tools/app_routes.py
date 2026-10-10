"""An app's own ways in, before clicking pixels: what Mint can know about an app from its bundle, plus routes that are
known to work for apps people use. Given to the model when it opens an app (extra_tools, after open_app/switch_to).

Seen 9-10 Oct: Telegram shows Accessibility nothing, and Mint clicked, typed and guessed for minutes - while
`tg://resolve?domain=BotFather` opens the chat in one step; GenOffice opens .xlsx files, so a spreadsheet is best made
as a file and opened; an app with an Export menu does a bulk job in one go. The rule: links, files, scripts and menus
first; clicking through the window last; and when there is no direct way, say so and offer one (a script that repeats
the clicks, the Mac's own tools) - never claim progress that isn't there.

card(name) never raises; '' when there is nothing useful to say.
"""

from __future__ import annotations

import logging
import plistlib
from pathlib import Path

log = logging.getLogger("mint.tools.app_routes")

# Routes known to work, by bundle id: one or two lines each, plain.
KNOWN = {
    "ru.keepcoder.Telegram": (
        "Telegram hides its controls from Accessibility: ui_elements is empty - work by the words on screen. A chat "
        "with an @name (bots, channels, public groups): open_url tg://resolve?domain=NAME opens it at once. Any other "
        "chat: ⌘K, type the name once, click_text the result, then read_window to check the header and messages. "
        "Media in bulk: right-click a message for Save As, or the chat's ⋯ menu for Export chat history if offered."),
    "org.telegram.desktop": (
        "Telegram Desktop: open_url tg://resolve?domain=NAME opens a chat by @name; Settings ▸ Advanced ▸ Export "
        "Telegram data, or a chat's ⋯ ▸ Export chat history, saves its media in bulk."),
    "com.genoffice.app": (
        "GenOffice opens .xlsx, .docx and .pptx files. A spreadsheet: make the .xlsx with edit_spreadsheet (a new path "
        "makes a new file; rows as 'Item | Price', total=true for sums), then file_action open it with to=GenOffice - exact, no typing "
        "into the grid. It is an Electron app: its grid may hide cells from Accessibility."),
    "name:Paper Studio": (
        "Paper Studio has no links or scripting: work in its window - the sidebar's Image Studio / Compress Images / "
        "Resize, then its Export and the save sheet (type the folder path with ⌘⇧G). If its controls don't respond, "
        "say so and offer the Mac's own tool instead: a script with `sips` compresses or resizes an image exactly."),
    "com.firecore.infuse": (
        "Infuse plays a file you open with it: file_action open with the video's path and to=Infuse - no clicking "
        "through its library"),
    "com.apple.systempreferences": (
        "System Settings panes open directly: open_url x-apple.systempreferences:com.apple.preference.<pane> "
        "(e.g. com.apple.Bluetooth-Settings.extension, com.apple.preference.security?Privacy_ScreenCapture). Change "
        "a setting only when the user asked for that change."),
    "com.apple.finder": (
        "Files: file_action (open, reveal, move, copy, rename, make_folder, trash) and find_files do it exactly - "
        "no dragging in Finder windows."),
    "com.tinyspeck.slackmacgap": "Slack: open_slack opens a workspace, channel or DM by name.",
    "com.apple.Notes": "Notes: create_note and the notes tool add and read notes directly - no typing into the app.",
}

_cache: dict[str, str] = {}


def _bundle(name: str) -> Path | None:
    try:
        from mint.tools import appfinder
        path = appfinder.installed().get(name)
        if path:
            return Path(path)
        resolved, _ = appfinder.resolve(name)
        path = appfinder.installed().get(resolved or "")
        return Path(path) if path else None
    except Exception:
        return None


def probe(bundle: Path) -> dict:
    """What the bundle says about itself: id, URL schemes, scripting, document types, Electron."""
    info: dict = {}
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as fh:
            info = plistlib.load(fh)
    except Exception:
        return {}
    schemes = sorted({s for t in info.get("CFBundleURLTypes", []) or [] for s in t.get("CFBundleURLSchemes", []) or []
                      if s and not s.startswith(("fb", "com.googleusercontent"))})
    kinds = sorted({e.lower() for t in info.get("CFBundleDocumentTypes", []) or []
                    for e in t.get("CFBundleTypeExtensions", []) or [] if e and e != "*"})
    scriptable = bool(info.get("NSAppleScriptEnabled")) or bool(info.get("OSAScriptingDefinition"))
    electron = (bundle / "Contents" / "Frameworks" / "Electron Framework.framework").exists()
    return {"id": info.get("CFBundleIdentifier", ""), "name": info.get("CFBundleName") or bundle.stem,
            "schemes": schemes[:6], "files": kinds[:10], "scriptable": scriptable, "electron": electron}


def card(name: str) -> str:
    """'[Ways into X: ...]' for the model, or ''."""
    if not name:
        return ""
    if name in _cache:
        return _cache[name]
    text = ""
    try:
        bundle = _bundle(name)
        facts = probe(bundle) if bundle else {}
        lines = []
        known = KNOWN.get(facts.get("id", "")) or KNOWN.get(f"name:{facts.get('name') or name}")
        if known:
            lines.append(known.rstrip("."))
        else:
            if facts.get("schemes"):
                lines.append("its links: " + ", ".join(f"{s}://" for s in facts["schemes"]) + " (open_url)")
            if facts.get("scriptable"):
                lines.append("it has an AppleScript dictionary (run_applescript can drive it)")
            if facts.get("files"):
                lines.append("it opens " + ", ".join("." + f for f in facts["files"]) + " files - making the file and "
                             "opening it can beat typing into the app")
            if facts.get("electron"):
                lines.append("an Electron app: some of its content may be hidden from Accessibility")
        if lines:
            text = (f"\n[Ways into {facts.get('name') or name}: " + "; ".join(lines) + ". Prefer these over clicking "
                    "through the window; if none fits and clicking doesn't work, say so and offer another way (its "
                    "menus, a script that repeats the steps) - never say it is progressing when it isn't.]")
    except Exception:
        log.debug("app card for %s", name, exc_info=True)
    _cache[name] = text
    return text
