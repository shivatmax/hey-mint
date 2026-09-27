"""Screenshots and the clipboard.

* screenshot - the whole screen, the front window (even if covered), "this image"
  or any element named (found in the accessibility tree, e.g. the biggest
  picture on the page), an exact region or size, or a box the user drags.
  Saved where macOS saves screenshots (the Desktop unless changed), optionally
  resized to an exact size, and copied to the clipboard.
* clipboard - what is on it (text, an image, files), copy text / a file / an
  image / the path or address of "this", paste into the front app, clear, and
  a history of recent copies to bring one back.

The history lives in memory only. Anything a password manager marks as
concealed, and text that looks like a password, key or card number, is never
kept. Mint's own windows are excluded from capture (see effects.SHARING), so
the orb never appears in a screenshot.
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
            "(paste into the front app - first putting `text` on the clipboard if given); history (recent "
            "copies); restore (put history item `index` back); clear.",
            {"action": _enum(("get", "copy", "copy_file", "copy_image", "copy_path", "copy_selection", "paste",
                              "history", "restore", "clear"), "what to do"),
             "text": STRING, "path": STRING,
             "index": {**INTEGER, "description": "for restore: the number from history"}},
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
  (here)" = paste; "what did I copy before" = history, then restore. Never paste into a password field.
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
    from .skillbook import has_secret
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

HISTORY: list[dict] = []           # newest first: {at, kind, text?, files?, png?, label}
KEEP = 30
_watch_started = False


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
    png = board.dataForType_("public.png") or board.dataForType_("public.tiff")
    if png is not None:
        image = _image(board)
        rep = image.representations()[0] if image is not None and image.representations() else None
        size = f"{rep.pixelsWide()}x{rep.pixelsHigh()}" if rep else "?"
        return {"kind": "image", "png": bytes(png), "label": f"image {size}"}
    return None


def _remember(entry: dict) -> None:
    entry["at"] = time.time()
    same = [h for h in HISTORY if h.get("label") == entry.get("label") and h.get("kind") == entry.get("kind")]
    for h in same:
        HISTORY.remove(h)
    HISTORY.insert(0, entry)
    images = [h for h in HISTORY if h["kind"] == "image"]
    for old in images[5:]:                      # keep the pictures of only the last few
        old.pop("png", None)
    del HISTORY[KEEP:]


def start_watching() -> None:
    """Note each new clipboard content (once a second), for history/restore."""
    global _watch_started
    if _watch_started:
        return
    _watch_started = True

    def watch():
        last = -1
        while True:
            from .skills import BOARD_LOCK
            if BOARD_LOCK.acquire(blocking=False):      # busy: someone is using the clipboard - next beat
                try:
                    board = _board()
                    count = board.changeCount()
                    if count != last:
                        last = count
                        entry = _snapshot(board)
                        if entry:
                            _remember(entry)
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
        from . import extra_tools
        target = extra_tools._target.get("app")
        if target is not None and not target.isTerminated():
            return target
    return front


def _finder_selection() -> list[str]:
    from .harness_tools import _osascript
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
    from .harness_tools import _BROWSERS, _tab_info
    if bundle in _BROWSERS:
        url, title = _tab_info(front)
        if url:
            return url, f"the address of '{title or url}'"
    if front is not None:
        import ApplicationServices as AX
        from .axkit import attr
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
    """Record what is on the clipboard now in the history (the watcher would, a second later)."""
    from .skills import BOARD_LOCK
    with BOARD_LOCK:
        entry = _snapshot(_board())
        if entry:
            _remember(entry)


def _put_text(text: str) -> None:
    from .skills import BOARD_LOCK
    with BOARD_LOCK:
        board = _board()
        board.clearContents()
        board.setString_forType_(text, "public.utf8-plain-text")
        _note()


def _put_png(data: bytes) -> None:
    from .skills import BOARD_LOCK
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
    from .skills import BOARD_LOCK
    with BOARD_LOCK:
        from Foundation import NSURL
        board = _board()
        board.clearContents()
        board.writeObjects_([NSURL.fileURLWithPath_(p) for p in paths])
        _note()


def _path_arg(raw: str) -> tuple[Path | None, str]:
    from .harness_tools import _resolve
    return _resolve(raw)


def clipboard(args: dict) -> str:
    start_watching()
    from .skills import BOARD_LOCK
    with BOARD_LOCK:
        return _clipboard(args)


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
        from .fastinput import press_key
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
        from .harness_tools import _password_field
        if _password_field():
            return "REFUSED: the focused field is a password field. Mint never pastes into password fields."
        if text is not None and str(text) != "":
            _put_text(str(text))
        from .fastinput import press_key
        board = _board()
        if not _types(board):
            return "FAILED: the clipboard is empty."
        front = _front_app()
        press_key("v", ["command"])
        where = front.localizedName() if front is not None else "the front app"
        return f"Pasted {_describe(board)[:120]} into {where}."

    if action == "history":
        if not HISTORY:
            return "No clipboard history yet (Mint starts noting copies from when it starts; hidden items and secrets are never kept)."
        rows = []
        for i, h in enumerate(HISTORY[:15], 1):
            age = int(time.time() - h["at"])
            when = f"{age // 60} min ago" if age >= 60 else "just now"
            rows.append(f"{i}. [{h['kind']}] {h['label']}  ({when})")
        return "Recent copies, newest first:\n" + "\n".join(rows)

    if action == "restore":
        index = int(args.get("index") or 0)
        if not 1 <= index <= len(HISTORY):
            return f"FAILED: history has {len(HISTORY)} items; give index 1-{len(HISTORY)}."
        h = HISTORY[index - 1]
        if h["kind"] == "text":
            _put_text(h["text"])
        elif h["kind"] == "files":
            _put_files(h["files"])
        elif h.get("png"):
            _put_png(h["png"])
        else:
            return "FAILED: that picture is too old to bring back (only the last few images are kept)."
        return f"Put back on the clipboard: {h['label']}"

    if action == "clear":
        _board().clearContents()
        return "Cleared the clipboard."

    return "FAILED: unknown clipboard action."


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
    """(window id, (x, y, w, h), app name) of the front window, or of `app_name`'s."""
    import Quartz
    front = _front_app()
    pid = None
    name = ""
    if app_name:
        import AppKit
        from .appfinder import resolve
        resolved, _ = resolve(app_name)
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            if (app.localizedName() or "").lower() == (resolved or app_name).lower():
                pid, name = app.processIdentifier(), app.localizedName()
                break
        if pid is None:
            return None
    elif front is not None:
        pid, name = front.processIdentifier(), front.localizedName()
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    mine = []
    for window in windows:
        if window.get("kCGWindowOwnerPID") != pid or window.get("kCGWindowLayer", 0) != 0:
            continue
        b = window.get("kCGWindowBounds") or {}
        if b.get("Width", 0) < 60 or b.get("Height", 0) < 60:
            continue
        mine.append((int(window["kCGWindowNumber"]), (b["X"], b["Y"], b["Width"], b["Height"])))
    if not mine:
        return None
    # The window the app calls focused (its AX frame), not a bubble or popup listed first.
    try:
        import ApplicationServices as AX
        from .axkit import attr, frame
        app = AX.AXUIElementCreateApplication(pid)
        focused = frame(attr(app, "AXFocusedWindow") or attr(app, "AXMainWindow"))
    except Exception:
        focused = None
    if focused:
        best = min(mine, key=lambda m: sum(abs(a - b) for a, b in zip(m[1], focused)))
    else:
        best = max(mine, key=lambda m: m[1][2] * m[1][3])
    return best[0], best[1], name


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
    from .harness_tools import _attr, _frame, _front, _label, _search, _walk_limited
    front, app, window = _front()
    if window is None:
        return None
    from .axkit import unlock
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
            from .harness_tools import _walk_find
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
    copy = args.get("copy") is not False
    save = args.get("save") is not False
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
            return f"FAILED: no window of {target or 'the front app'} is on screen."
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
        _put_png(_as_png(path))
    if box:
        try:
            from .effects import fx
            fx.highlight(*box, seconds=0.9)
        except Exception:
            pass
    parts = [f"Took a screenshot of {described} ({pixels} pixels){note}."]
    if save:
        parts.append(f"Saved as {_short(str(path))}.")
    else:
        path.unlink(missing_ok=True)
    if copy:
        parts.append("It is on the clipboard, ready to paste.")
    return " ".join(parts)


HANDLERS = {"screenshot": screenshot, "clipboard": clipboard}
