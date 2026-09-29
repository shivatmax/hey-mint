"""Screenshots and the clipboard.

* screenshot - the whole screen, the front window (even if covered), "this image"
  or any element named (found in the accessibility tree, e.g. the biggest
  picture on the page), an exact region or size, or a box the user drags.
  Saved where macOS saves screenshots (the Desktop unless changed), optionally
  resized to an exact size, and copied to the clipboard.
* clipboard - what is on it (text, an image, files), copy text / a file / an
  image / the path or address of "this", paste into the front app, clear, and
  a history of recent copies to bring one back.

The history (text, pictures, files - the last 150, a week) is kept on this Mac only, in
Application Support/Mint/clipboard (mode 700), so it survives a restart. Anything a
password manager marks as concealed, and text that looks like a password, key or card
number, is never kept there. Screenshots taken by Mint are numbered in it, so "paste the
last three screenshots into the chat" pastes them one after another.

Named clips: "pin this as the invoice template", then "paste the invoice template".
Secrets: "this is my OpenAI key" / "save this password as bank" puts what is on the
clipboard into the macOS Keychain (never a file, never the model); "copy my OpenAI key"
puts it back - hidden from clipboard managers, cleared after a minute. Mint never reads a
secret out and never pastes into a password field. Mint's own windows are excluded from
capture (see effects.SHARING), so the orb never appears in a screenshot.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from google.genai import types

log = logging.getLogger("mint.clip")

STRING = {"type": types.Type.STRING}
BOOLEAN = {"type": types.Type.BOOLEAN}
INTEGER = {"type": types.Type.INTEGER}


def _fn(name, description, properties, required=None):
    return types.FunctionDeclaration(
        name=name, description=description,
        parameters=types.Schema(type=types.Type.OBJECT,
                                properties={k: types.Schema(**v) for k, v in properties.items()},
                                required=required or []))


def _enum(values, description):
    return {"type": types.Type.STRING, "enum": list(values), "description": description}


def declarations() -> list[types.FunctionDeclaration]:
    return [
        _fn("screenshot",
            "Take a screenshot and save it as a file (and copy it to the clipboard). what: screen (the whole "
            "display), window (the front window, or the app named in target; works even if covered), element "
            "('this image', 'the chart', 'the sidebar' - target describes it; with no target, the biggest "
            "picture in the front window), region (target = 'x,y,width,height' in screen points, or just "
            "`size` for a box of that size in the middle of the front window), or select (the user drags a box "
            "themselves). size='1280x720' resizes the result to exactly that many pixels (cropping evenly if "
            "the shape differs); size='800' makes it 800 wide. Use this, not look, when the user wants a "
            "screenshot.",
            {"what": _enum(("screen", "window", "element", "region", "select"), "what to capture"),
             "target": STRING,
             "size": {**STRING, "description": "optional output size: 'WIDTHxHEIGHT' or 'WIDTH'"},
             "copy": {**BOOLEAN, "description": "also put the image on the clipboard (default true)"},
             "save": {**BOOLEAN, "description": "save a file (default true)"},
             "name": {**STRING, "description": "optional file name"},
             "format": _enum(("png", "jpg"), "default png")},
            ["what"]),
        _fn("clipboard",
            "Manage the clipboard. get (what is on it: text, an image, or files); copy (put `text` on it); "
            "copy_file (put the file at `path` on it, so pasting in Finder or a chat attaches the file); "
            "copy_image (the image file at `path`, as a picture); copy_path (copy the path of `path`; with no "
            "path: the file selected in Finder, else the address of the page open in the browser, else the "
            "file open in the front window); copy_selection (copy what is selected in the front app); paste "
            "(paste into the front app - first putting `text` on the clipboard if given); open (the clipboard "
            "window: everything copied and screenshotted, with filters and multi-select); history (recent "
            "copies, shown as a card; kind filters: text, image, screenshot, files); restore (put history item "
            "`index` back - 1 is the newest copy, so 'the one before the last two' is 3; call history first "
            "unless you already have the numbered list); to_file (write copied texts into a text file exactly as copied - `path`, the newest `count` or `indexes`, oldest first, "
            "added below what is there; use it instead of retyping them); paste_many (paste several history items into the front "
            "app one after another: the last `count` of `kind`, e.g. the last 5 screenshots, or `indexes`); "
            "pin (keep what is on the clipboard, or history item `index`, under `label`); pins; paste_pin / "
            "copy_pin (`label`); unpin; "
            "save_secret (put what is ON THE CLIPBOARD NOW into the macOS Keychain as `label` - a password, API "
            "key or token; the value never reaches you); copy_secret (`label`: back on the clipboard, hidden, "
            "cleared after a minute); secrets (their names only); forget_secret; clear.",
            {"action": _enum(("get", "copy", "copy_file", "copy_image", "copy_path", "copy_selection", "paste", "to_file",
                              "open", "history", "restore", "paste_many", "pin", "pins", "paste_pin", "copy_pin", "unpin",
                              "save_secret", "copy_secret", "secrets", "forget_secret", "clear"), "what to do"),
             "text": STRING, "path": STRING,
             "index": {**INTEGER, "description": "for restore / pin: the number from history (1 = newest)"},
             "count": {**INTEGER, "description": "for paste_many: how many (newest ones, pasted oldest first)"},
             "kind": _enum(("any", "text", "image", "screenshot", "files"), "for history / paste_many"),
             "indexes": {"type": types.Type.ARRAY, "items": types.Schema(type=types.Type.INTEGER),
                         "description": "for paste_many: history numbers"},
             "label": {**STRING, "description": "for pins and secrets: the name ('invoice template', 'OpenAI key')"}},
            ["action"]),
    ]


NAMES = ("screenshot", "clipboard")

PROMPT = """
# Screenshots and the clipboard
- "Take a screenshot" = screenshot (never look, which only shows YOU the screen). "Screenshot this image" / "that
  chart" = what=element with target. "Of this window" = window. "1280 by 720" = size. "Let me pick" = select.
  Say where it was saved, and that it is on the clipboard.
- "Copy X" = clipboard copy; "copy the path / link of this" = copy_path (no path: Finder selection, browser page,
  or the open document); "copy this file" = copy_file; "copy what I selected" = copy_selection; "paste it
  (here)" = paste; "what did I copy before" / "show my clipboard" = history, then restore. Numbers count from the
  newest (1): "the one I copied before the last two" = 3. Never paste into a password field.
- "Open my clipboard" / "show the clipboard window" = open.
- "Paste the last 5 screenshots (into the chat)" = paste_many kind=screenshot count=5 (items 2 and 4 = indexes).
  "Whenever I say screenshot, just put it on the clipboard" = set_preference screenshot_to=clipboard (or file,
  both).
- "Pin this as X" / "paste X" = pin / paste_pin. "This is my OpenAI key" / "save this password as bank" (it is on
  the clipboard) = save_secret label=...; "copy my OpenAI key" = copy_secret. Never ask the user to say or type a
  secret to you, never repeat one.
"""


# --- the pasteboard ---------------------------------------------------------------------------

_CONCEALED = {"org.nspasteboard.ConcealedType", "org.nspasteboard.TransientType",
              "org.nspasteboard.AutoGeneratedType", "com.agilebits.onepassword"}


def _board():
    import AppKit
    return AppKit.NSPasteboard.generalPasteboard()


def _types(board) -> list[str]:
    return [str(t) for t in (board.types() or [])]


def _files(board) -> list[str]:
    import AppKit
    urls = board.readObjectsForClasses_options_([AppKit.NSURL], {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True})
    return [str(u.path()) for u in (urls or [])]


def _image(board):
    import AppKit
    images = board.readObjectsForClasses_options_([AppKit.NSImage], None)
    return images[0] if images else None


def _secret(text: str) -> bool:
    from mint.knowledge.skills import has_secret
    return has_secret(text)


def _describe(board) -> str:
    kinds = set(_types(board))
    if kinds & _CONCEALED:
        return "a password or other hidden item (Mint does not read it)"
    files = _files(board)
    if files:
        shown = ", ".join(_short(f) for f in files[:5])
        return f"{len(files)} file{'s' if len(files) > 1 else ''}: {shown}"
    text = board.stringForType_("public.utf8-plain-text")
    if text:
        text = str(text)
        return f"text ({len(text)} characters): {text[:1500]}" + (" …" if len(text) > 1500 else "")
    image = _image(board)
    if image is not None:
        rep = image.representations()[0] if image.representations() else None
        size = f"{rep.pixelsWide()}x{rep.pixelsHigh()} " if rep else ""
        return f"an image ({size}pixels)"
    return "nothing" if not kinds else f"something Mint cannot read ({', '.join(sorted(kinds))[:120]})"


def _short(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


# --- history ------------------------------------------------------------------------------------

HISTORY: list[dict] = []           # newest first: {at, kind, text?, files?, image?, label, source?}
KEEP = 150
KEEP_IMAGES = 80
KEEP_DAYS = 7
STORE = Path.home() / "Library" / "Application Support" / "Mint" / "clipboard"
_watch_started = False
_loaded = False
_screenshot_count = {"n": 0}
VERSION = [0]                      # bumped on every change (the clipboard window redraws)
_own_counts: set[int] = set()      # pasteboard changes that were Mint putting a clip back: not new copies
_restoring = [False]


def _store() -> Path:
    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / "images").mkdir(exist_ok=True)
    os.chmod(STORE, 0o700)
    return STORE


def _load() -> None:
    """The history saved by an earlier run (text, files, and pictures on disk)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    import json
    try:
        rows = json.loads((_store() / "history.json").read_text())
    except (OSError, ValueError):
        rows = []
    cutoff = time.time() - KEEP_DAYS * 86400
    HISTORY[:] = [r for r in rows if r.get("at", 0) >= cutoff and (r["kind"] != "image" or Path(r.get("image", "")).exists())]
    _screenshot_count["n"] = max([r.get("shot", 0) for r in HISTORY] + [0])


def _save() -> None:
    import json
    VERSION[0] += 1
    keep = {r.get("image") for r in HISTORY if r.get("image")} | {r.get("image") for r in _pins().values()}
    try:
        tmp = _store() / "history.tmp"
        tmp.write_text(json.dumps(HISTORY, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        tmp.replace(STORE / "history.json")
        for old in (STORE / "images").glob("*.png"):          # pictures nothing points to any more
            if str(old) not in keep:
                old.unlink(missing_ok=True)
    except OSError as error:
        log.info("clipboard history: %s", error)


def _png_file(data: bytes) -> str:
    import hashlib
    path = _store() / "images" / f"{hashlib.sha1(data).hexdigest()[:16]}.png"
    if not path.exists():
        path.write_bytes(data)
        os.chmod(path, 0o600)
    return str(path)


def _snapshot(board) -> dict | None:
    kinds = set(_types(board))
    if not kinds or kinds & _CONCEALED:
        return None
    files = _files(board)
    if files:
        return {"kind": "files", "files": files, "label": ", ".join(Path(f).name for f in files[:3])}
    text = board.stringForType_("public.utf8-plain-text")
    if text:
        text = str(text)
        if _secret(text):
            return None
        return {"kind": "text", "text": text, "label": " ".join(text.split())[:80]}
    png = board.dataForType_("public.png")
    if png is None and board.dataForType_("public.tiff") is not None:
        image = _image(board)
        rep = _bitmap(image) if image is not None else None
        png = rep.representationUsingType_properties_(4, None) if rep is not None else None   # 4 = PNG
    if png is not None:
        image = _image(board)
        rep = image.representations()[0] if image is not None and image.representations() else None
        size = f"{rep.pixelsWide()}x{rep.pixelsHigh()}" if rep else "?"
        return {"kind": "image", "png": bytes(png), "label": f"image {size}"}
    return None


def _bitmap(image):
    """The image as one bitmap (to turn a TIFF-only copy into PNG)."""
    import AppKit
    data = image.TIFFRepresentation()
    return AppKit.NSBitmapImageRep.imageRepWithData_(data) if data is not None else None


def _remember(entry: dict) -> None:
    _load()
    import uuid
    entry["at"] = time.time()
    entry.setdefault("id", uuid.uuid4().hex[:10])
    entry.setdefault("source", "you")
    if entry.get("png") is not None:
        entry["image"] = _png_file(entry.pop("png"))
    if _pending_label:
        entry.update(_pending_label)
        _pending_label.clear()
    same = [h for h in HISTORY if (h.get("image") and h.get("image") == entry.get("image"))
            or (h.get("label") == entry.get("label") and h.get("kind") == entry.get("kind") and not h.get("image"))]
    for h in same:
        if h.get("source") == "screenshot" and entry.get("source") != "screenshot":
            entry.update(source="screenshot", shot=h.get("shot"), label=h.get("label"))
        entry["id"] = h.get("id", entry["id"])
        HISTORY.remove(h)
    HISTORY.insert(0, entry)
    images = [h for h in HISTORY if h["kind"] == "image"]
    for old in images[KEEP_IMAGES:]:
        HISTORY.remove(old)
    del HISTORY[KEEP:]
    _save()


def delete(ids: list[str]) -> int:
    """Remove clips from the history (the window's Delete)."""
    _load()
    before = len(HISTORY)
    HISTORY[:] = [h for h in HISTORY if h.get("id") not in set(ids)]
    _save()
    return before - len(HISTORY)


def entry(clip_id: str) -> dict | None:
    _load()
    return next((h for h in HISTORY if h.get("id") == clip_id), None)


_pending_label: dict = {}


_folder_seen = {"since": time.time(), "names": set(), "checked": 0.0}


def _screenshots_saved() -> None:
    """Screenshots macOS saved as files (⇧⌘3, ⇧⌘4) go into the history too, as screenshots."""
    now = time.time()
    if now - _folder_seen["checked"] < 3.0:
        return
    _folder_seen["checked"] = now
    try:
        folder = _save_folder()
        for path in folder.iterdir():
            name = path.name
            if name in _folder_seen["names"] or not name.lower().endswith((".png", ".jpg", ".jpeg", ".heic")):
                continue
            if not re.match(r"(Screenshot|Screen Shot|CleanShot|Shottr)", name):
                continue
            if path.stat().st_mtime < _folder_seen["since"]:
                _folder_seen["names"].add(name)
                continue
            _folder_seen["names"].add(name)
            data = _as_png(path)
            _load()
            _screenshot_count["n"] += 1
            _remember({"kind": "image", "png": data, "source": "screenshot", "shot": _screenshot_count["n"],
                       "file": str(path), "label": f"screenshot {_screenshot_count['n']} · {name[:40]}"})
    except Exception as error:
        log.debug("screenshot folder: %s", error)


def start_watching() -> None:
    """Note each new clipboard content (once a second), for history/restore."""
    global _watch_started
    if _watch_started:
        return
    _watch_started = True

    def watch():
        last = -1
        while True:
            from mint.tools.everyday import BOARD_LOCK
            if BOARD_LOCK.acquire(blocking=False):      # busy: someone is using the clipboard - next beat
                try:
                    board = _board()
                    count = board.changeCount()
                    if count != last:
                        last = count
                        entry = _snapshot(board) if count not in _own_counts else None
                        if entry:
                            _remember(entry)
                    _screenshots_saved()
                except Exception as error:
                    log.debug("clipboard watch: %s", error)
                finally:
                    BOARD_LOCK.release()
            time.sleep(1.0)
    threading.Thread(target=watch, name="clipboard-watch", daemon=True).start()


# --- clipboard actions ------------------------------------------------------------------------

def _front_app():
    import AppKit
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is not None and front.processIdentifier() in {os.getpid(), os.getppid()}:
        from mint.tools import extra as extra_tools
        target = extra_tools._target.get("app")
        if target is not None and not target.isTerminated():
            return target
    return front


def _finder_selection() -> list[str]:
    from mint.tools.harness import _osascript
    ok, out = _osascript('tell application "Finder"\nset out to ""\nrepeat with i in (get selection)\n'
                         'set out to out & POSIX path of (i as alias) & linefeed\nend repeat\nreturn out\nend tell', 6)
    return [line for line in out.splitlines() if line.strip()] if ok else []


def _this_path() -> tuple[str, str]:
    """(text to copy, what it is) for "copy the path of this"."""
    front = _front_app()
    bundle = front.bundleIdentifier() if front is not None else ""
    if bundle == "com.apple.finder":
        chosen = _finder_selection()
        if chosen:
            return "\n".join(p.rstrip("/") if len(p) > 1 else p for p in chosen), \
                f"the path{'s' if len(chosen) > 1 else ''} of what is selected in Finder"
    from mint.tools.harness import _BROWSERS, _tab_info
    if bundle in _BROWSERS:
        url, title = _tab_info(front)
        if url:
            return url, f"the address of '{title or url}'"
    if front is not None:
        import ApplicationServices as AX
        from mint.screen.axkit import attr
        app = AX.AXUIElementCreateApplication(front.processIdentifier())
        window = attr(app, "AXFocusedWindow") or attr(app, "AXMainWindow")
        document = attr(window, "AXDocument") if window is not None else None
        if document:
            from urllib.parse import unquote, urlparse
            text = str(document)
            path = unquote(urlparse(text).path) if text.startswith("file://") else text
            return path, f"the path of the file open in {front.localizedName()}"
    chosen = _finder_selection()
    if chosen:
        return "\n".join(chosen), "the path of what is selected in Finder"
    return "", ""


def _note() -> None:
    """Record what Mint just put on the clipboard in the history (as Mint's). A clip being put back
    from the history is not recorded again: the history keeps its order, so numbers stay put."""
    from mint.tools.everyday import BOARD_LOCK
    with BOARD_LOCK:
        board = _board()
        if _restoring[0]:
            _own_counts.add(board.changeCount())
            return
        entry = _snapshot(board)
        if entry:
            entry.setdefault("source", "mint")
            _remember(entry)


def _put_text(text: str) -> None:
    from mint.tools.everyday import BOARD_LOCK
    with BOARD_LOCK:
        board = _board()
        board.clearContents()
        board.setString_forType_(text, "public.utf8-plain-text")
        _note()


def _put_png(data: bytes, screenshot: str = "") -> None:
    """Put a picture on the clipboard. `screenshot` (what it shows) numbers it as a screenshot in the
    history, so it can be pasted back by number."""
    from mint.tools.everyday import BOARD_LOCK
    if screenshot:
        _load()
        _screenshot_count["n"] += 1
        n = _screenshot_count["n"]
        _pending_label.update(source="screenshot", shot=n,
                              label=f"screenshot {n} · {screenshot} · {time.strftime('%H:%M')}")
    with BOARD_LOCK:
        import AppKit
        from Foundation import NSData
        board = _board()
        board.clearContents()
        blob = NSData.dataWithBytes_length_(data, len(data))
        board.setData_forType_(blob, "public.png")
        image = AppKit.NSImage.alloc().initWithData_(blob)
        if image is not None and image.TIFFRepresentation() is not None:
            board.setData_forType_(image.TIFFRepresentation(), "public.tiff")   # for apps that only take TIFF
        _note()


def _put_files(paths: list[str]) -> None:
    from mint.tools.everyday import BOARD_LOCK
    with BOARD_LOCK:
        from Foundation import NSURL
        board = _board()
        board.clearContents()
        board.writeObjects_([NSURL.fileURLWithPath_(p) for p in paths])
        _note()


def _path_arg(raw: str) -> tuple[Path | None, str]:
    from mint.tools.harness import _resolve
    return _resolve(raw)


# Clipboard actions that replace what is on it: each can be undone (the old content goes back).
_CHANGES = {"copy", "copy_file", "copy_image", "copy_path", "copy_selection", "restore", "copy_pin", "clear"}


def clipboard(args: dict) -> str:
    start_watching()
    from mint.tools.everyday import BOARD_LOCK
    with BOARD_LOCK:
        action = str(args.get("action") or "get").lower()
        if action not in _CHANGES:
            return _clipboard(args)
        before = undo_snapshot()
        result = _clipboard(args)
        if not result.startswith(("FAILED", "REFUSED", "No ")):
            from mint.tools import undo
            undo.record("clipboard", f"the clipboard change ({action.replace('_', ' ')})",
                        {"kind": "clipboard", "was": before} if before else None,
                        "" if before else "what was on it before was a password or hidden item, which Mint never keeps.")
        return result


def undo_snapshot() -> dict | None:
    """What is on the clipboard now, as data that can put it back; None for a hidden item or a secret."""
    from mint.tools.everyday import BOARD_LOCK
    with BOARD_LOCK:
        board = _board()
        if not _types(board):
            return {"type": "empty"}
        entry = _snapshot(board)
    if entry is None:
        return None
    if entry["kind"] == "image":
        from mint.tools import undo
        return {"type": "image", "image": undo.clip_image(entry["png"])}
    return {"type": entry["kind"], **({"text": entry["text"]} if entry["kind"] == "text" else {"files": entry["files"]})}


def undo_put(was: dict) -> str:
    """Put a snapshot back on the clipboard (not a new copy in the history)."""
    kind = was.get("type")
    _restoring[0] = True
    try:
        if kind == "text":
            _put_text(was["text"])
            text = " ".join(was["text"].split())
            return f"the clipboard holds \"{text[:50]}{'…' if len(text) > 50 else ''}\" again."
        if kind == "files":
            missing = [f for f in was["files"] if not Path(f).exists()]
            if missing:
                return f"FAILED: {Path(missing[0]).name}, which was on the clipboard, is gone."
            _put_files(was["files"])
            return f"the clipboard holds {', '.join(Path(f).name for f in was['files'][:3])} again."
        if kind == "image":
            if not Path(was["image"]).exists():
                return "FAILED: the picture that was on the clipboard is no longer kept."
            _put_png(Path(was["image"]).read_bytes())
            return "the picture is back on the clipboard."
        from mint.tools.everyday import BOARD_LOCK
        with BOARD_LOCK:
            _board().clearContents()
        return "the clipboard is empty again, as it was."
    finally:
        _restoring[0] = False


def _clipboard(args: dict) -> str:
    action = str(args.get("action") or "get").lower()
    text = args.get("text")
    raw = str(args.get("path") or "").strip()

    if action == "get":
        return "The clipboard holds " + _describe(_board()) + "."

    if action == "copy":
        if text is None or str(text) == "":
            return "FAILED: put the text to copy in `text`."
        _put_text(str(text))
        return f"Copied {len(str(text))} characters to the clipboard."

    if action in {"copy_file", "copy_image"}:
        if not raw:
            chosen = _finder_selection()
            if not chosen:
                return f"FAILED: say which file (path), or select it in Finder first."
            paths = chosen
        else:
            path, why = _path_arg(raw)
            if path is None:
                return f"FAILED: {why}"
            paths = [str(path)]
        if action == "copy_file":
            _put_files(paths)
            names = ", ".join(Path(p).name for p in paths)
            return f"Copied {names} as {'files' if len(paths) > 1 else 'a file'} - paste in Finder, Mail or a chat to attach it."
        path = Path(paths[0])
        try:
            data = _as_png(path)
        except Exception as error:
            return f"FAILED: {path.name} is not an image Mint can read ({error})."
        _put_png(data)
        return f"Copied the picture in {path.name} to the clipboard."

    if action == "copy_path":
        if raw:
            path, why = _path_arg(raw)
            if path is None:
                return f"FAILED: {why}"
            copied, what = str(path), f"the path of {path.name}"
        else:
            copied, what = _this_path()
            if not copied:
                return ("FAILED: nothing to take a path from - select a file in Finder, or open the page or document "
                        "first, or give the path.")
        _put_text(copied)
        return f"Copied {what}: {copied}"

    if action == "copy_selection":
        from mint.tools.fastinput import press_key
        board = _board()
        before = board.changeCount()
        press_key("c", ["command"])
        for _ in range(12):
            time.sleep(0.1)
            if board.changeCount() != before:
                break
        if board.changeCount() == before:
            return "FAILED: nothing was copied - nothing may be selected in the front app."
        _note()
        return "Copied the selection. The clipboard now holds " + _describe(board) + "."

    if action == "paste":
        from mint.tools.harness import _password_field
        if _password_field():
            return "REFUSED: the focused field is a password field. Mint never pastes into password fields."
        if text is not None and str(text) != "":
            _put_text(str(text))
        from mint.tools.fastinput import press_key
        board = _board()
        if not _types(board):
            return "FAILED: the clipboard is empty."
        front = _front_app()
        press_key("v", ["command"])
        where = front.localizedName() if front is not None else "the front app"
        return f"Pasted {_describe(board)[:120]} into {where}."

    if action == "history":
        _load()
        kind = str(args.get("kind") or "any")
        rows = [(i, h) for i, h in enumerate(HISTORY, 1) if _is_kind(h, kind)][:15]
        if not rows:
            return ("No clipboard history yet (hidden items and secrets are never kept)." if kind == "any"
                    else f"No {kind} in the clipboard history.")
        _history_card(rows, kind)
        return ("Recent copies, newest first (1 = the newest copy; shown on screen as a card):\n"
                + _history_lines(rows))

    if action == "restore":
        _load()
        index = int(args.get("index") or 0)
        if not 1 <= index <= len(HISTORY):
            return f"FAILED: history has {len(HISTORY)} items; give index 1-{len(HISTORY)}."
        chosen = HISTORY[index - 1]
        problem = _put_back(chosen)
        if problem:
            return problem
        # Say which item went back (not the newest), with its neighbours, so a wrong number shows at once.
        rows = list(enumerate(HISTORY, 1))[:max(5, index + 1)]
        return (f"Put back on the clipboard: #{index} {chosen['label']}. The history, newest first (the order "
                f"does not change):\n{_history_lines(rows, mark=index)}\nIf that is not the one the user meant, "
                "restore the right number.")

    if action == "open":
        from mint.ui import clipboard_window
        clipboard_window.show()
        _load()
        rows = list(enumerate(HISTORY, 1))[:6]
        return ("Opened the clipboard window: everything copied (by the user or you), every screenshot, pinned clips; "
                "filters, multi-select, paste, copy, pin, delete, drag out."
                + (f" The newest copies (1 = the newest):\n{_history_lines(rows)}" if rows else ""))

    if action == "to_file":
        return _to_file(args)

    if action == "paste_many":
        return _paste_many(args)

    if action in ("pin", "pins", "paste_pin", "copy_pin", "unpin"):
        return _pin_action(action, args)

    if action in ("save_secret", "copy_secret", "secrets", "forget_secret"):
        return _secret_action(action, str(args.get("label") or "").strip())

    if action == "clear":
        _board().clearContents()
        return "Cleared the clipboard."

    return "FAILED: unknown clipboard action."


def _history_lines(rows: list, mark: int = 0) -> str:
    lines = []
    for i, h in rows:
        age = int(time.time() - h["at"])
        when = f"{age // 3600} h ago" if age >= 3600 else f"{age // 60} min ago" if age >= 60 else "just now"
        lines.append(f"{i}. [{h.get('source') or h['kind']}] {h['label']}  ({when})"
                     + ("  <- put back now" if i == mark else ""))
    return "\n".join(lines)


def _is_kind(h: dict, kind: str) -> bool:
    if kind in ("", "any"):
        return True
    if kind == "screenshot":
        return h.get("source") == "screenshot"
    return h["kind"] == kind


def _put_back(h: dict) -> str:
    """Put a history (or pinned) item back on the clipboard; "" or why not. The history keeps its order."""
    _restoring[0] = True
    try:
        if h["kind"] == "text":
            _put_text(h["text"])
        elif h["kind"] == "files":
            _put_files(h["files"])
        elif h.get("image") and Path(h["image"]).exists():
            _put_png(Path(h["image"]).read_bytes())
        else:
            return "FAILED: that picture is no longer kept."
    finally:
        _restoring[0] = False
    return ""


def _history_card(rows: list, kind: str) -> None:
    try:
        from mint.tools import cards
        items = []
        for i, h in rows[:7]:
            item = {"title": h["label"][:60], "trailing": f"#{i}"}
            if h.get("image"):
                item["path"] = h["image"]
                item["detail"] = "screenshot" if h.get("source") == "screenshot" else "picture"
            elif h["kind"] == "files":
                item["path"] = h["files"][0]
                item["detail"] = f"{len(h['files'])} file(s)"
            else:
                item.update(icon="text.alignleft", detail=f"{len(h.get('text', ''))} characters")
            items.append(item)
        title = {"screenshot": "Screenshots", "image": "Pictures copied", "text": "Text copied",
                 "files": "Files copied"}.get(kind, "Clipboard")
        cards.show(title, subtitle="newest first · say “paste number 2”", items=items, icon="doc.on.clipboard.fill",
                   tint="teal", more=max(0, len(rows) - 7))
    except Exception:
        pass


def _to_file(args: dict) -> str:
    """Write copied texts into a file exactly as copied (the model retyping them changed "MintBench" to "Mint",
    bench hard-clip-three): the newest `count` (or `indexes`), oldest first, one per line, added below what is
    there."""
    _load()
    indexes = [int(i) for i in (args.get("indexes") or []) if str(i).lstrip("-").isdigit()]
    if indexes:
        picked = [HISTORY[i - 1] for i in indexes if 1 <= i <= len(HISTORY)]
    else:
        count = max(1, min(int(args.get("count") or 1), 20))
        picked = [h for h in HISTORY if h.get("kind") == "text"][:count]
        picked.reverse()                      # oldest first, the order they were copied in
    texts = [h.get("text", "") for h in picked if h.get("kind") == "text" and h.get("text")]
    if not texts:
        return "FAILED: no copied text to write (pictures and files can't go into a text file)."
    path = str(args.get("path") or "")
    if not path:
        return "FAILED: to_file needs `path`."
    from mint.tools import harness as harness_tools
    result = harness_tools.write_file({"path": path, "content": "\n".join(texts) + "\n", "mode": "append"})
    return f"{result} Wrote {len(texts)} copied item(s), exactly as copied, oldest first."


def _paste_many(args: dict) -> str:
    """Paste several history items one after another (e.g. the last five screenshots into a chat)."""
    from mint.tools.fastinput import press_key
    from mint.tools.harness import _password_field
    _load()
    if _password_field():
        return "REFUSED: the focused field is a password field."
    indexes = [int(i) for i in (args.get("indexes") or []) if str(i).lstrip("-").isdigit()]
    if indexes:
        picked = [HISTORY[i - 1] for i in indexes if 1 <= i <= len(HISTORY)]
    else:
        kind = str(args.get("kind") or "any")
        count = max(1, min(int(args.get("count") or 1), 20))
        picked = [h for h in HISTORY if _is_kind(h, kind)][:count]
        picked.reverse()                      # oldest first, the order they were taken in
    if not picked:
        return "Nothing in the clipboard history matches that."
    front = _front_app()
    board = _board()
    saved = board.stringForType_("public.utf8-plain-text")
    done = 0
    for h in picked:
        if _put_back(h):
            continue
        press_key("v", ["command"])
        done += 1
        time.sleep(0.45 if h["kind"] == "image" else 0.2)   # let a chat take in each picture
    if saved is not None and picked[-1]["kind"] != "text":
        time.sleep(0.4)
        _restoring[0] = True
        try:
            _put_text(str(saved))             # the user's own clipboard back (not a new copy)
        finally:
            _restoring[0] = False
    where = front.localizedName() if front is not None else "the front app"
    return (f"Pasted {done} item(s) into {where}, oldest first: " + "; ".join(h["label"][:40] for h in picked)
            + ". Check the chat box before sending - Mint never sends by itself.")


# --- pinned clips -----------------------------------------------------------------------------

def _pins() -> dict:
    import json
    try:
        return json.loads((_store() / "pins.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_pins(pins: dict) -> None:
    import json
    path = _store() / "pins.json"
    path.write_text(json.dumps(pins, ensure_ascii=False))
    os.chmod(path, 0o600)


def _pin_action(action: str, args: dict) -> str:
    _load()
    pins = _pins()
    label = str(args.get("label") or "").strip()
    key = label.lower()
    if action == "pins":
        if not pins:
            return "No pinned clips. Say 'pin this as …' to keep one."
        try:
            from mint.tools import cards
            cards.show("Pinned clips", len(pins), "pinned", icon="pin.fill", tint="orange",
                       items=[{"title": p["name"], "detail": p["label"][:50], "path": p.get("image", ""),
                               "icon": "pin.fill"} for p in pins.values()])
        except Exception:
            pass
        return "Pinned: " + "; ".join(f"{p['name']} ({p['kind']})" for p in pins.values())
    if not label:
        return "FAILED: give the pin a name (label)."
    if action == "pin":
        index = int(args.get("index") or 0)
        if index:
            if not 1 <= index <= len(HISTORY):
                return f"FAILED: history has {len(HISTORY)} items."
            entry = dict(HISTORY[index - 1])
        else:
            entry = _snapshot(_board())
            if entry is None:
                return "FAILED: the clipboard is empty, hidden (a password) or looks like a secret - use save_secret."
            if entry.get("png") is not None:
                entry["image"] = _png_file(entry.pop("png"))
        if key not in pins and len(pins) >= 20:
            return "FAILED: 20 clips are pinned already - unpin one first."
        entry.update(name=label, at=time.time())
        pins[key] = entry
        _save_pins(pins)
        return f"Pinned as '{label}': {entry['label'][:60]}. Say 'paste {label}' any time."
    found = pins.get(key) or next((p for k, p in pins.items() if key in k), None)
    if found is None:
        return f"No pinned clip called '{label}'. Pinned: {', '.join(p['name'] for p in pins.values()) or 'none'}."
    if action == "unpin":
        pins.pop(found["name"].lower(), None)
        _save_pins(pins)
        return f"Unpinned '{found['name']}'."
    problem = _put_back(found)
    if problem:
        return problem
    if action == "copy_pin":
        return f"'{found['name']}' is on the clipboard."
    from mint.tools.fastinput import press_key
    from mint.tools.harness import _password_field
    if _password_field():
        return "REFUSED: the focused field is a password field."
    press_key("v", ["command"])
    return f"Pasted '{found['name']}'."


# --- secrets (the macOS Keychain) -------------------------------------------------------------

KEYCHAIN = "Mint clipboard"


def _secret_names() -> list[str]:
    import json
    try:
        return json.loads((_store() / "secrets.json").read_text())
    except (OSError, ValueError):
        return []


def _secret_action(action: str, label: str) -> str:
    """Secrets live in the Keychain only; their values never reach the model or a file."""
    names = _secret_names()
    if action == "secrets":
        return ("Saved secrets (names only): " + ", ".join(names)) if names else "No saved secrets."
    if not label:
        return "FAILED: give the secret a name (label), e.g. 'OpenAI key'."
    import json
    if action == "save_secret":
        board = _board()
        value = board.stringForType_("public.utf8-plain-text")
        if not value:
            return "FAILED: there is no text on the clipboard - copy the password or key first, then ask again."
        done = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN, "-a", label, "-w"],
                              input=f"{value}\n{value}\n", capture_output=True, text=True, timeout=10)
        if done.returncode != 0:
            return "FAILED: the Keychain did not take it."
        if label not in names:
            names.append(label)
            (_store() / "secrets.json").write_text(json.dumps(names))
        # It was on the clipboard as plain text: take it out of the history too.
        HISTORY[:] = [h for h in HISTORY if h.get("text") != str(value)]
        _save()
        board.clearContents()
        return (f"Saved '{label}' in the macOS Keychain and cleared it from the clipboard. Say 'copy my {label}' "
                "when you need it (do not repeat or describe the value).")
    match = next((n for n in names if n.lower() == label.lower()), None) or \
        next((n for n in names if label.lower() in n.lower()), None)
    if match is None:
        return f"No saved secret called '{label}'. Saved: {', '.join(names) or 'none'}."
    if action == "forget_secret":
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN, "-a", match], capture_output=True,
                       timeout=10)
        names.remove(match)
        (_store() / "secrets.json").write_text(json.dumps(names))
        return f"Forgot '{match}' (removed from the Keychain)."
    done = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN, "-a", match, "-w"],
                          capture_output=True, text=True, timeout=10)
    if done.returncode != 0:
        return f"FAILED: '{match}' is not in the Keychain any more."
    value = done.stdout.rstrip("\n")
    from mint.tools.everyday import BOARD_LOCK
    with BOARD_LOCK:
        board = _board()
        board.clearContents()
        board.setString_forType_(value, "public.utf8-plain-text")
        board.setString_forType_("", "org.nspasteboard.ConcealedType")   # clipboard managers skip it
        count = board.changeCount()

    def clear_later():
        with BOARD_LOCK:
            if _board().changeCount() == count:
                _board().clearContents()
    threading.Timer(60.0, clear_later).start()
    return (f"'{match}' is on the clipboard (hidden from clipboard history, cleared in a minute). Tell the user to "
            "paste it with ⌘V - never read it out.")


# --- screenshots ------------------------------------------------------------------------------

def _save_folder() -> Path:
    try:
        done = subprocess.run(["defaults", "read", "com.apple.screencapture", "location"], capture_output=True,
                              text=True, timeout=3, check=False)
        where = done.stdout.strip()
        if done.returncode == 0 and where:
            folder = Path(os.path.expanduser(where))
            if folder.is_dir():
                return folder
    except Exception:
        pass
    return Path.home() / "Desktop"


def _as_png(path: Path) -> bytes:
    from io import BytesIO
    from PIL import Image
    with Image.open(path) as image:
        out = BytesIO()
        image.save(out, "PNG")
        return out.getvalue()


def _front_window_info(app_name: str = ""):
    """(window id, (x, y, w, h), app name) of the front window, or of `app_name`'s. `app_name` may also be
    words from a window's title ("Poster": the browser window showing that page). A window on another
    Space is used when it is the one meant (screencapture -l takes it from there too)."""
    import Quartz
    front = _front_app()
    pid = None
    name = ""
    if app_name:
        import AppKit
        from mint.tools.appfinder import resolve
        resolved, _ = resolve(app_name)
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            if (app.localizedName() or "").lower() == (resolved or app_name).lower():
                pid, name = app.processIdentifier(), app.localizedName()
                break
    elif front is not None:
        pid, name = front.processIdentifier(), front.localizedName()
    options = Quartz.kCGWindowListExcludeDesktopElements
    on_screen = _windows(Quartz.CGWindowListCopyWindowInfo(options | Quartz.kCGWindowListOptionOnScreenOnly,
                                                           Quartz.kCGNullWindowID))
    everywhere = _windows(Quartz.CGWindowListCopyWindowInfo(options | Quartz.kCGWindowListOptionAll,
                                                            Quartz.kCGNullWindowID))
    if app_name and pid is None:
        # Not an app: words from a window title (on this Space first).
        front_pid = front.processIdentifier() if front is not None else None
        return _titled_window(on_screen, app_name, front_pid) or _titled_window(everywhere, app_name, front_pid)
    mine = [w for w in on_screen if w["pid"] == pid]
    # The window the app calls focused (its AX frame and title), not a bubble or popup listed first.
    try:
        import ApplicationServices as AX
        from mint.screen.axkit import attr, frame
        app = AX.AXUIElementCreateApplication(pid)
        window = attr(app, "AXFocusedWindow") or attr(app, "AXMainWindow")
        focused, title = frame(window), str(attr(window, "AXTitle") or "") if window is not None else ""
    except Exception:
        focused, title = None, ""

    def same_title(w) -> bool:
        # Chrome's AX title is "Page - Google Chrome - Profile", its window-list name just "Page".
        return bool(title and w["title"] and (title.startswith(w["title"]) or w["title"].startswith(title)))

    # Same title first: several browser windows often share one frame, and the focused one may be on
    # another Space, where the nearest on-screen frame would be a different window.
    pool = [w for w in mine if same_title(w)]
    if not pool and title:
        pool = [w for w in everywhere if w["pid"] == pid and same_title(w)]
    if not pool:
        pool = mine or [w for w in everywhere if w["pid"] == pid and w["title"]]
    if not pool:
        return None
    if focused:
        best = min(pool, key=lambda w: sum(abs(a - b) for a, b in zip(w["box"], focused)))
    else:
        best = max(pool, key=lambda w: w["box"][2] * w["box"][3])
    return best["id"], best["box"], name


def _windows(listed) -> list[dict]:
    """Ordinary windows (layer 0, not tiny, not Mint's own) from a CGWindowList, front to back."""
    out = []
    for window in listed or []:
        b = window.get("kCGWindowBounds") or {}
        if window.get("kCGWindowLayer", 0) != 0 or b.get("Width", 0) < 60 or b.get("Height", 0) < 60:
            continue
        if window.get("kCGWindowOwnerPID") == os.getpid():
            continue
        out.append({"id": int(window["kCGWindowNumber"]), "pid": window.get("kCGWindowOwnerPID"),
                    "box": (b["X"], b["Y"], b["Width"], b["Height"]),
                    "title": str(window.get("kCGWindowName") or ""),
                    "app": str(window.get("kCGWindowOwnerName") or "app")})
    return out


def _titled_window(windows: list[dict], title: str, front_pid):
    """(window id, box, app name) of the window whose title holds `title` (the front app's first, then the
    biggest), or None."""
    want = " ".join(title.lower().split())
    hits = [w for w in windows if want and want in " ".join(w["title"].lower().split())]
    if not hits:
        return None
    best = max(hits, key=lambda w: (w["pid"] == front_pid, w["box"][2] * w["box"][3]))
    return best["id"], best["box"], best["app"]


def _display_of(point) -> int:
    """screencapture's -D number (1-based, CGGetActiveDisplayList order) of the display holding `point`."""
    import Quartz
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    for i, display in enumerate(ids[:count] if ids else []):
        r = Quartz.CGDisplayBounds(display)
        if r.origin.x <= point[0] < r.origin.x + r.size.width and r.origin.y <= point[1] < r.origin.y + r.size.height:
            return i + 1
    return 1


def _element_box(target: str):
    """(x, y, w, h, label) of the element `target` names in the front window; with no target, the
    biggest picture. Uses the accessibility tree (web pages included)."""
    from mint.tools.harness import _attr, _frame, _front, _label, _search, _walk_limited
    front, app, window = _front()
    if window is None:
        return None
    from mint.screen.axkit import unlock
    try:
        unlock(front)
    except Exception:
        pass
    win = _frame(window)
    want = (target or "").lower().strip()
    picture_words = {"image", "picture", "photo", "pic", "img", "screenshot", "chart", "graph", "diagram", "figure",
                     "logo", "icon", "thumbnail", "this", "that", "the"}
    generic = not want or set(re.findall(r"[a-z]+", want)) <= picture_words

    def visible(box):
        if not box or box[2] < 24 or box[3] < 24:
            return None
        if not win:
            return box
        x0, y0 = max(box[0], win[0]), max(box[1], win[1])
        x1, y1 = min(box[0] + box[2], win[0] + win[2]), min(box[1] + box[3], win[1] + win[3])
        return (x0, y0, x1 - x0, y1 - y0) if x1 - x0 > 24 and y1 - y0 > 24 else None

    candidates = []
    if generic:
        kinds = {"AXImage"}
        if want and set(re.findall(r"[a-z]+", want)) & {"chart", "graph", "diagram", "figure"}:
            kinds |= {"AXGroup"}
        for node in _walk_limited(window, 6000):
            if _attr(node, "AXRole") in {"AXImage"} or (
                    "AXGroup" in kinds and "chart" in str(_attr(node, "AXRoleDescription") or "").lower()):
                box = visible(_frame(node))
                if box:
                    candidates.append((box[2] * box[3], box, _label(node) or "image"))
        # A web page's <canvas> or <video> counts as a picture too.
        for node in _walk_limited(window, 6000):
            if str(_attr(node, "AXRoleDescription") or "").lower() in {"canvas", "video"}:
                box = visible(_frame(node))
                if box:
                    candidates.append((box[2] * box[3], box, str(_attr(node, "AXRoleDescription"))))
    else:
        hits = _search(window, target, limit=30) or []
        if not hits:
            from mint.tools.harness import _walk_find
            hits = _walk_find(window, target, limit=5000, seconds=2.0)
        for node in hits:
            box = visible(_frame(node))
            if box:
                label = _label(node)
                exact = label.lower().strip() == want
                candidates.append(((2 if exact else 1) * 1e9 + box[2] * box[3], box, label or target))
    if not candidates:
        return None
    _, box, label = max(candidates, key=lambda c: c[0])
    return (*box, label)


def _resize(path: Path, size: str) -> str:
    """Resize the saved picture: 'WxH' exactly (cropping evenly if the shape differs) or 'W' keeping shape."""
    from PIL import Image
    match = re.fullmatch(r"\s*(\d{2,5})\s*(?:[x×*,\s]\s*(\d{2,5}))?\s*(?:px)?\s*", size or "")
    if not match:
        return f" (size '{size}' not understood - use like 1280x720 or 800)"
    w = int(match.group(1))
    h = int(match.group(2)) if match.group(2) else None
    with Image.open(path) as image:
        image.load()
        src_w, src_h = image.size
        if h is None:
            h = max(1, round(src_h * w / src_w))
            out = image.resize((w, h), Image.LANCZOS)
        else:
            scale = max(w / src_w, h / src_h)          # cover, then crop the middle
            big = image.resize((max(w, round(src_w * scale)), max(h, round(src_h * scale))), Image.LANCZOS)
            left, top = (big.width - w) // 2, (big.height - h) // 2
            out = big.crop((left, top, left + w, top + h))
        out.save(path)
    return ""


def screenshot(args: dict) -> str:
    what = str(args.get("what") or "screen").lower()
    target = str(args.get("target") or "").strip()
    size = str(args.get("size") or "").strip()
    fmt = "jpg" if str(args.get("format") or "").lower() in {"jpg", "jpeg"} else "png"
    from mint.core import prefs
    where = str(prefs.get("screenshot_to") or "both")      # "clipboard", "file" or "both"
    copy = args["copy"] if isinstance(args.get("copy"), bool) else where in ("both", "clipboard")
    save = args["save"] if isinstance(args.get("save"), bool) else where in ("both", "file")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d at %H.%M.%S")
    name = str(args.get("name") or "").strip()
    name = re.sub(r"[/:]", "-", name) if name else f"Mint Screenshot {stamp}"
    if not name.lower().endswith("." + fmt):
        name += "." + fmt
    folder = _save_folder() if save else Path("/tmp")
    path = folder / name
    number = 2
    while path.exists():                    # "name (2).png", like Finder
        path = folder / f"{Path(name).stem} ({number}){Path(name).suffix}"
        number += 1

    command = ["screencapture", "-x", "-t", fmt]
    described, box = "", None
    if what == "window":
        info = _front_window_info(target)
        if info is None:
            if not target:
                return "FAILED: no window of the front app is on screen."
            return (f"FAILED: no window of {target} is on screen ('{target}' is neither a running app nor in the "
                    "title of a window on this screen). Give the app's name as target (e.g. Google Chrome), or no "
                    "target for the front window.")
        window_id, box, app = info
        command += ["-o", "-l", str(window_id)]
        described = f"the {app} window"
    elif what == "element":
        found = _element_box(target)
        if found is None:
            return (f"FAILED: could not find {target or 'a picture'} in the front window. Try what=window, or "
                    "what=select to let the user drag a box.")
        x, y, w, h, label = found
        box = (x, y, w, h)
        command += ["-R", f"{x:.0f},{y:.0f},{w:.0f},{h:.0f}"]
        described = f"'{label[:60]}'"
    elif what == "region":
        numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", target)]
        if len(numbers) >= 4:
            box = tuple(numbers[:4])
        else:
            sized = re.fullmatch(r"\s*(\d{2,5})\s*[x×*,\s]\s*(\d{2,5})\s*", size or target)
            if not sized:
                return "FAILED: give the region as target='x,y,width,height', or a size like '800x600'."
            w, h = int(sized.group(1)), int(sized.group(2))
            info = _front_window_info()
            if info:
                cx, cy = info[1][0] + info[1][2] / 2, info[1][1] + info[1][3] / 2
            else:
                import AppKit
                frame = AppKit.NSScreen.mainScreen().frame()
                cx, cy = frame.size.width / 2, frame.size.height / 2
            # A size is in pixels; screen points are half that on a Retina display.
            import AppKit
            scale = AppKit.NSScreen.mainScreen().backingScaleFactor() or 1
            bw, bh = w / scale, h / scale
            screen = AppKit.NSScreen.screens()[0].frame()
            left = min(max(0.0, cx - bw / 2), max(0.0, screen.size.width - bw))
            top = min(max(0.0, cy - bh / 2), max(0.0, screen.size.height - bh))
            box = (left, top, bw, bh)
            if not re.fullmatch(r"\s*\d+\s*", size or "x"):
                size = f"{w}x{h}"
        command += ["-R", ",".join(f"{v:.0f}" for v in box)]
        described = "the region " + "x".join(f"{v:.0f}" for v in box[2:]) + f" at {box[0]:.0f},{box[1]:.0f}"
    elif what == "select":
        command += ["-i"]
        described = "the area you selected"
    else:
        info = _front_window_info()
        display = _display_of((info[1][0] + info[1][2] / 2, info[1][1] + info[1][3] / 2)) if info else 1
        command += ["-D", str(display)]
        described = "the whole screen" if display == 1 else f"display {display}"
    command.append(str(path))

    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=90 if what == "select" else 20,
                              check=False)
    except subprocess.TimeoutExpired:
        return "FAILED: the screenshot timed out (no area was selected)."
    if not path.exists() or path.stat().st_size == 0:
        if what == "select":
            return "No screenshot: the selection was cancelled."
        detail = (done.stderr or "").strip()[:200]
        return (f"FAILED: macOS did not take the screenshot{': ' + detail if detail else ''}. Mint may need Screen "
                "Recording permission (System Settings > Privacy & Security > Screen & System Audio Recording).")
    note = _resize(path, size) if size else ""
    from PIL import Image
    with Image.open(path) as image:
        pixels = f"{image.width}x{image.height}"
    if copy:
        _put_png(_as_png(path), screenshot=str(described)[:40])
    if box:
        try:
            from mint.ui.effects import fx
            fx.highlight(*box, seconds=0.9)
        except Exception:
            pass
    parts = [f"Took a screenshot of {described} ({pixels} pixels){note}."]
    if save:
        parts.append(f"Saved as {_short(str(path))}.")
    else:
        path.unlink(missing_ok=True)
    if copy:
        parts.append(f"It is on the clipboard, ready to paste (screenshot {_screenshot_count['n']} in the history).")
    return " ".join(parts)


HANDLERS = {"screenshot": screenshot, "clipboard": clipboard}
