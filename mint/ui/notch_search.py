"""Search, for the notch: Spotlight in the notch - find files, folders and apps, then open, drag, preview them.

    compact (~616 x 150): the field and one row that scrolls sideways
    ┌───────────────────────────────────────┐ ┌──────────────────────┐ ┌──────────┐ ┌──┐
    │ ⌕ invoice                  12 results │ │ All Apps Files Folders│ │✦ Ask Mint│ │⤢ │
    └───────────────────────────────────────┘ └──────────────────────┘ └──────────┘ └──┘
     ‹ ┌────┐    ┌────┐    ┌────┐    ┌────┐    ┌────┐    ┌────┐ ›
       │ 📄 │    │ 🖼 │    │ 📁 │    │ 📄 │    │ 🅰 │    │ 🎞 │         drag out, double-click to open,
       └────┘    └────┘    └────┘    └────┘    └────┘    └────┘         Space = Quick Look, right-click
       Invoice   scan.png  Taxes     inv-03…   Numbers   call.mov
       PDF·1 MB  PNG·2 MB  Folder    PDF·80 KB Application

    expanded (~736 x 430): the field, a layout switcher (3×1, 3×2, 3×3, list) and a grid that scrolls down.

* Typing searches (0.25 s after the last key): the installed apps first (fuzzy: "photsop" finds
  Photoshop), then file names in the home folder (Spotlight, `mdfind -onlyin ~`), then - for three or
  more letters - what is inside files. Exact names and apps rank first, then Desktop/Documents/Downloads
  and recent files. At most 120 results. Everything runs off the main thread; nothing leaves the Mac.
* Mint's own tools push results with show(paths, query, title) ("3 PDFs about the lease").
* Tiles: QuickLook thumbnails (only for tiles on screen, cached by path + mtime) or the app's icon, the
  name cut in the middle, kind · size · date. Click selects (⌘ toggles, ⇧ extends), double-click or
  Return opens (an app launches), Space previews, drag copies the file (or the selection) anywhere.
  Right-click: Open, Open With ▸, Show in Finder, Quick Look, Copy, Copy Path, Add to Shelf, Share…,
  AirDrop. Nothing here deletes, moves or renames anything.
* "Ask Mint" sends the typed words to Mint as a request (the same notification `mint --say` posts).
* Keys in the field: ↓ or Tab goes into the results, Return opens the selection (or the first result),
  Esc clears the field. In the results: arrows move (⇧ extends), Return opens, Space previews, ⌘A/⌘C/
  ⌘O/⌘R, Esc or Tab or ↑ from the top row goes back to the field, typing goes on searching.

The API the notch uses (notch.py, another session's file):

    view, update = view(width, height)   # compact below 250 tall (made for 616 x 150), grid above (736 x 430)
    resize(view, width, height)          # re-lay an existing view out (compact <-> grid) in place
    focus_field(view) -> bool            # the field takes the keyboard (the panel must be able to be key)
    search(query, scope="all", now=False)   # scope: all | files | apps | folders; debounced unless now
    show(paths=None, query="", title="")    # results pushed by Mint's tools
    on_change(fn) -> unsubscribe         # fn() on the main thread: items, searching, title or expanded changed
    items(), has_results(), title(), query(), searching(), clear(), available()
    want_expanded(), set_expanded(bool)  # "Show more" / the layout switcher ask the notch to open bigger
    grid(), set_grid("3x1" | "3x2" | "3x3" | "list")
    dragging()                           # a drag out of a result is under way (don't treat it as a drag in)
    ask(text) -> bool                    # send a request to Mint

Views are built and painted on the main thread only; every view shows the same (shared) results.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx
from mint.ui import notch_shelf
from mint.core import prefs

log = logging.getLogger("mint.ui.notch_search")

HOME = str(Path.home())
SAY_NOTIFICATION = "local.mint.say"          # mint.main.SAY_NOTIFICATION (main is not imported: it is the app)
GRID_PREF = "notch_search_grid"
SCOPES = ("all", "apps", "files", "folders")
LAYOUTS = {"3x1": (3, 1), "3x2": (3, 2), "3x3": (3, 3), "list": (1, 0)}
LIMIT = 120
DEBOUNCE = 0.25
MDFIND_TIMEOUT = 5.0
COMPACT_BELOW = 250.0                       # views shorter than this are the compact row
BAR_H = 28.0
GAP = 6.0
COMPACT_TILE_W = 104.0
ROW_H = 44.0
DRAG_START = 3.0
INK = (1.0, 1.0, 1.0)
DIM = (0.62, 0.62, 0.64)
FAINT = (0.45, 0.45, 0.47)


def _cg(rgb, alpha=1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _ns(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _accent() -> tuple:
    return tuple(gfx.accent())


def _font(size, weight=AppKit.NSFontWeightRegular):
    return AppKit.NSFont.systemFontOfSize_weight_(size, weight)


HOW_TO = "Double-click opens · Space previews · drag to copy"


def _label(size, weight=AppKit.NSFontWeightRegular, rgb=INK, alpha=1.0, lines=1):
    field = AppKit.NSTextField.wrappingLabelWithString_("")
    field.setFont_(_font(size, weight))
    field.setTextColor_(_ns(rgb, alpha))
    field.setMaximumNumberOfLines_(lines)
    field.setSelectable_(False)
    field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    field.cell().setTruncatesLastVisibleLine_(True)
    return field


def _on_main(fn) -> None:
    if threading.current_thread() is threading.main_thread():
        fn()
    else:
        AppHelper.callAfter(fn)


# --- ranking (pure; any thread) -------------------------------------------------------------------------

_SPLIT = re.compile(r"[\s_\-.,()\[\]{}+&'’]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def _words(text: str) -> list[str]:
    """"MyInvoice_2024 (final).pdf" -> my, invoice, 2024, final, pdf."""
    return [w for w in _SPLIT.split(_CAMEL.sub(" ", text).lower()) if w]


def _subsequence(q: str, text: str) -> float:
    """How tightly q's letters appear in order in text (1.0 = together), 0 if they don't."""
    first, at = -1, 0
    for ch in q:
        at = text.find(ch, at)
        if at < 0:
            return 0.0
        if first < 0:
            first = at
        at += 1
    return len(q) / max(1, at - first)


def score(name: str, query: str, fuzzy: bool = False) -> float:
    """How well `name` matches `query`: 1000 exact, 800 prefix, 650 word prefixes, 600 initials, 500
    substring, 450 all words; with `fuzzy` (apps) 200-400 for typos ("photsop" -> Photoshop). 0 = no."""
    q = " ".join(query.lower().split())
    if not q or not name:
        return 0.0
    n = name.lower()
    stem = n.rsplit(".", 1)[0] if "." in n[1:] else n
    if q in (n, stem):
        return 1000.0
    closeness = len(q) / max(1, len(stem))
    if stem.startswith(q):
        return 800.0 + 100 * closeness
    words = _words(name)
    tokens = _words(query) or [q]
    if all(any(w.startswith(t) for w in words) for t in tokens):
        return 650.0 + 50 * closeness
    squashed = q.replace(" ", "")
    if len(squashed) >= 2 and len(words) >= 2 and "".join(w[0] for w in words).startswith(squashed):
        return 600.0
    if q in n:
        return 500.0 + 50 * closeness
    if all(t in n for t in tokens):
        return 450.0
    if not fuzzy or len(squashed) < 3:
        return 0.0
    # Typos keep the first letter (people rarely miss it): only words starting with it are compared,
    # so "invoice" does not find Voice Memos.
    lead = squashed[0]
    candidates = [w for w in words if w[0] == lead]
    joined = "".join(words)
    at = joined.find(lead)
    if at >= 0:
        candidates.append(joined[at:])
    if not candidates:
        return 0.0
    tight = max(_subsequence(squashed, w) for w in candidates) * 0.9
    similar = max(difflib.SequenceMatcher(None, squashed, w).ratio() for w in candidates)
    prefix = max(difflib.SequenceMatcher(None, squashed, w[:len(squashed) + 1]).ratio() for w in candidates)
    best = max(tight, similar, prefix * 0.95)
    return 200.0 + 200.0 * best if best >= 0.75 else 0.0


def _bonus(path: str, mtime: float, kind: str, now: float) -> float:
    """Where and how fresh: Desktop/Documents/Downloads and recent files first, deep folders later."""
    bonus = 0.0
    rel = path[len(HOME) + 1:] if path.startswith(HOME + "/") else path.lstrip("/")
    top = rel.split("/", 1)[0]
    if top in ("Desktop", "Documents", "Downloads"):
        bonus += 40
    elif rel.startswith("Library/Mobile Documents/com~apple~CloudDocs"):
        bonus += 30
    bonus -= 4 * max(0, rel.count("/") - 3)
    age = now - mtime if mtime else 1e12
    if age < 3 * 86400:
        bonus += 40
    elif age < 30 * 86400:
        bonus += 20
    elif age > 2 * 365 * 86400:
        bonus -= 10
    if kind == "folder":
        bonus += 10
    return bonus


def rank(entries) -> list[dict]:
    """Apps whose name holds the words, and exact names, first; then by score + bonus, then by name."""
    def key(e):
        s = e.get("score", 0.0)
        tier = 0 if s >= 1000 or (e.get("kind") == "app" and s >= 450) else 1     # typo'd apps rank by score
        return (tier, -(s + e.get("bonus", 0.0)), e.get("name", "").lower())
    return sorted(entries, key=key)


# --- what is on disk (any thread) -----------------------------------------------------------------------

_JUNK = re.compile(r"/(\.[^/]*|node_modules|__pycache__|site-packages|venv|DerivedData|Caches|Pods)(/|$)")
_INSIDE_PACKAGE = re.compile(r"\.(app|photoslibrary|bundle|framework|pkg|xcodeproj|xcworkspace|rtfd|pages|numbers|"
                             r"key|logicx|fcpbundle|imovielibrary|musiclibrary|tvlibrary|plugin|kext|appex|xpc|"
                             r"lproj|nib|storyboardc|momd|band|playground|sparsebundle)/", re.IGNORECASE)
_PACKAGES = {".app", ".photoslibrary", ".bundle", ".framework", ".pkg", ".xcodeproj", ".xcworkspace", ".rtfd",
             ".pages", ".numbers", ".key", ".logicx", ".fcpbundle", ".imovielibrary", ".band", ".playground",
             ".sparsebundle", ".musiclibrary", ".tvlibrary"}
_APP_DIRS = ["/Applications", "/Applications/Utilities", "/System/Applications", "/System/Applications/Utilities",
             os.path.join(HOME, "Applications"), "/System/Library/CoreServices/Applications"]
_EXTRA_APPS = ["/System/Library/CoreServices/Finder.app"]
_apps_cache: list = [0.0, {}]
_meta: dict = {}                             # (path, mtime) -> (type, size)


def _junk(path: str) -> bool:
    if _JUNK.search(path) or _INSIDE_PACKAGE.search(path):
        return True
    rel = path[len(HOME):] if path.startswith(HOME) else ""
    return rel.startswith("/Library/") and not rel.startswith("/Library/Mobile Documents/")


def _kind(path: str, is_dir: bool) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".app":
        return "app"
    if is_dir and ext not in _PACKAGES:
        return "folder"
    return "file"


def _app_name(path: str) -> str:
    try:
        name = str(AppKit.NSFileManager.defaultManager().displayNameAtPath_(path) or "")
    except Exception:
        name = ""
    name = name or os.path.basename(path)
    return name[:-4] if name.lower().endswith(".app") else name


def installed_apps() -> dict[str, str]:
    """{path: display name} of the .app bundles in the usual folders (one level into sub-folders such as
    /Applications/Adobe Photoshop 2025). Cached 2 minutes."""
    if time.monotonic() - _apps_cache[0] < 120 and _apps_cache[1]:
        return _apps_cache[1]
    found: dict[str, str] = {}

    def scan(folder: str, depth: int) -> None:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.name.endswith(".app"):
                found.setdefault(entry.path, _app_name(entry.path))
            elif depth and entry.is_dir(follow_symlinks=False) and entry.name != "Utilities":
                scan(entry.path, depth - 1)
    for folder in _APP_DIRS:
        scan(folder, 1 if folder in ("/Applications", os.path.join(HOME, "Applications")) else 0)
    for path in _EXTRA_APPS:
        if os.path.exists(path):
            found.setdefault(path, _app_name(path))
    _apps_cache[:] = [time.monotonic(), found]
    return found


def _stat(path: str):
    try:
        return os.stat(path)
    except OSError:
        return None


_day_fmt: list = []


def _date_text(ts: float) -> str:
    """"Today 2:02 PM", "Yesterday 9:10 AM", "12 Sep 2026" (the user's locale)."""
    if not ts:
        return ""
    if not _day_fmt:
        day = AppKit.NSDateFormatter.alloc().init()
        day.setDateStyle_(AppKit.NSDateFormatterMediumStyle)
        day.setTimeStyle_(AppKit.NSDateFormatterNoStyle)
        day.setDoesRelativeDateFormatting_(True)
        clock = AppKit.NSDateFormatter.alloc().init()
        clock.setDateStyle_(AppKit.NSDateFormatterNoStyle)
        clock.setTimeStyle_(AppKit.NSDateFormatterShortStyle)
        _day_fmt[:] = [day, clock]
    day, clock = _day_fmt
    date = AppKit.NSDate.dateWithTimeIntervalSince1970_(ts)
    text = str(day.stringFromDate_(date))
    if time.time() - ts < 2 * 86400:
        text += " " + str(clock.stringFromDate_(date))
    return text


def _where(path: str) -> str:
    parent = os.path.dirname(path)
    cloud = os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs")
    if parent.startswith(cloud):
        return "iCloud Drive" + parent[len(cloud):]
    return "~" + parent[len(HOME):] if parent.startswith(HOME) else parent


def _entry(path: str, kind: str | None = None, name: str | None = None, value: float = 0.0,
           match: str = "name", now: float | None = None) -> dict | None:
    st = _stat(path)
    if st is None:
        return None
    is_dir = os.path.isdir(path)
    kind = kind or _kind(path, is_dir)
    name = name or (_app_name(path) if kind == "app" else os.path.basename(path.rstrip("/")) or path)
    return {"path": path, "name": name, "kind": kind, "score": value, "match": match,
            "mtime": st.st_mtime, "bytes": 0 if is_dir else st.st_size,
            "bonus": 0.0 if kind == "app" else _bonus(path, st.st_mtime, kind, now or time.time())}


def _enrich(entry: dict) -> dict:
    """Add the words the tiles show: type ("PDF document"), size, date, folder."""
    path, kind = entry["path"], entry["kind"]
    if kind == "app":
        entry.update(type="Application", size="", date="", where=_where(path))
        return entry
    key = (path, entry["mtime"])
    if key not in _meta:
        kind_text = ""
        with contextlib.suppress(Exception):
            values, _error = AppKit.NSURL.fileURLWithPath_(path).resourceValuesForKeys_error_(
                [AppKit.NSURLLocalizedTypeDescriptionKey], None)
            if values is not None:
                kind_text = str(values.objectForKey_(AppKit.NSURLLocalizedTypeDescriptionKey) or "")
        size = "" if kind == "folder" else str(AppKit.NSByteCountFormatter.stringFromByteCount_countStyle_(
            entry["bytes"], AppKit.NSByteCountFormatterCountStyleFile))
        if len(_meta) > 3000:
            _meta.clear()
        _meta[key] = (kind_text or ("Folder" if kind == "folder" else "Document"), size)
    entry["type"], entry["size"] = _meta[key]
    entry["date"] = _date_text(entry["mtime"])
    entry["where"] = _where(path)
    return entry


def _short(entry: dict) -> str:
    """The compact tile's line: "PDF · 1.2 MB", "Folder", "Application"."""
    if entry["kind"] != "file":
        return entry.get("type", "")
    ext = os.path.splitext(entry["path"])[1][1:].upper()
    head = ext if 0 < len(ext) <= 5 else entry.get("type", "")
    return " · ".join(p for p in (head, entry.get("size", "")) if p)


def _long(entry: dict) -> str:
    return " · ".join(p for p in (entry.get("type", ""), entry.get("size", ""), entry.get("date", "")) if p)


# --- the shared results (any thread) --------------------------------------------------------------------

_lock = threading.RLock()
_state = {"items": [], "title": "", "query": "", "scope": "all", "searching": False, "expanded": False,
          "gen": 0, "version": 0}
_listeners: list = []
_debounce: list = [None]
_procs: list = []
_grid: list = [None]
_dragging = [False]


def _changed() -> None:
    def tell():
        for fn in _listeners[:]:
            try:
                fn()
            except Exception:
                log.exception("search listener")
        for item in _views[:]:
            try:
                item.reload()
            except Exception:
                log.exception("search view reload")
    _on_main(tell)


def _kill_procs() -> None:
    for proc in _procs[:]:
        with contextlib.suppress(Exception):
            proc.kill()


def _mdfind(gen: int, args: list, limit: int) -> list[str]:
    """Spotlight, streamed: stops at `limit` paths, after MDFIND_TIMEOUT, or when a newer search starts."""
    try:
        proc = subprocess.Popen(["mdfind", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return []
    _procs.append(proc)
    killer = threading.Timer(MDFIND_TIMEOUT, proc.kill)
    killer.daemon = True
    killer.start()
    out: list[str] = []
    try:
        for line in proc.stdout:
            if _state["gen"] != gen:
                break
            path = line.rstrip("\n")
            if path and not _junk(path):
                out.append(path)
                if len(out) >= limit:
                    break
    except Exception:
        log.debug("mdfind", exc_info=True)
    finally:
        killer.cancel()
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=1)
        if proc in _procs:
            _procs.remove(proc)
    return out


def _name_query(query: str) -> str:
    """Every word somewhere in the file name, any case, any accents."""
    def esc(word):
        return word.replace("\\", "\\\\").replace('"', '\\"').replace("*", "\\*")
    return " && ".join(f'kMDItemFSName == "*{esc(w)}*"cd' for w in query.split())


def _run(gen: int, query: str, scope: str) -> None:
    """Background: apps, then file names, then file contents; publishes after each step."""
    found: dict[str, dict] = {}
    now = time.time()

    def publish(final: bool) -> bool:
        ranked = [_enrich(dict(e)) for e in rank(found.values())[:LIMIT]]
        with _lock:
            if _state["gen"] != gen:
                return False
            _state["items"] = ranked
            _state["version"] += 1
            if final:
                _state["searching"] = False
        _changed()
        return True

    try:
        if scope in ("all", "apps"):
            for path, name in installed_apps().items():
                value = max(score(name, query, fuzzy=True),
                            score(os.path.basename(path)[:-4], query, fuzzy=True))
                if value > 0:
                    found[path] = {"path": path, "name": name, "kind": "app", "score": value, "match": "name",
                                   "mtime": 0.0, "bytes": 0, "bonus": 0.0}
            if scope == "apps" or len(query) < 2:
                publish(True)
                return
            if found and not publish(False):
                return
        if _state["gen"] != gen:
            return
        for path in _mdfind(gen, ["-onlyin", HOME, _name_query(query)], 1500):
            if path in found:
                continue
            e = _entry(path, value=0.0, now=now)
            if e is None or (scope == "files" and e["kind"] != "file") \
                    or (scope == "folders" and e["kind"] != "folder"):
                continue
            e["score"] = score(e["name"], query, fuzzy=e["kind"] == "app") or 400.0
            found[path] = e
        content = len(query) >= 3 and scope in ("all", "files")
        if not publish(not content) or not content:
            return
        for path in _mdfind(gen, ["-onlyin", HOME, query], 600):
            if path in found:
                continue
            e = _entry(path, match="content", now=now)
            if e is None or e["kind"] == "folder" or (scope == "files" and e["kind"] != "file"):
                continue
            e["score"] = 150.0
            found[path] = e
        publish(True)
    except Exception:
        log.exception("search %r", query)
        with _lock:
            if _state["gen"] == gen:
                _state["searching"] = False
        _changed()


def search(query: str, scope: str = "all", now: bool = False, title: str = "") -> None:
    """Search apps and files (any thread). Debounced by 0.25 s unless `now`; an empty query clears."""
    q = " ".join(str(query or "").split())
    scope = scope if scope in SCOPES else "all"
    with _lock:
        _state["gen"] += 1
        gen = _state["gen"]
        _state.update(query=q, scope=scope, title=str(title or ""))
        if _debounce[0] is not None:
            _debounce[0].cancel()
            _debounce[0] = None
        _kill_procs()
        if q:
            _state["searching"] = True
        else:
            _state.update(items=[], searching=False)
            _state["version"] += 1
    _changed()
    if not q:
        return

    def start():
        if _state["gen"] == gen:
            threading.Thread(target=_run, args=(gen, q, scope), daemon=True, name="notch-search").start()
    if now:
        start()
    else:
        timer = threading.Timer(DEBOUNCE, start)
        timer.daemon = True
        _debounce[0] = timer
        timer.start()


def show(paths=None, query: str = "", title: str = "") -> None:
    """Results pushed by Mint (after its find-files tool): these paths, in this order, with a title.
    Without paths, searches `query` now (titled `title`)."""
    q = " ".join(str(query or "").split())
    if paths is None:
        if q:
            search(q, _state["scope"], now=True, title=title)
        return
    with _lock:
        _state["gen"] += 1
        gen = _state["gen"]
        if _debounce[0] is not None:
            _debounce[0].cancel()
            _debounce[0] = None
        _kill_procs()
        _state["searching"] = True
    wanted = [os.path.abspath(os.path.expanduser(str(p))) for p in paths if p]

    def work():
        seen, entries = set(), []
        for path in wanted:
            if path in seen:
                continue
            seen.add(path)
            e = _entry(path)
            if e is not None:
                entries.append(_enrich(e))
            if len(entries) >= LIMIT:
                break
        with _lock:
            if _state["gen"] != gen:
                return
            n = len(entries)
            _state.update(items=entries, query=q, searching=False, scope="all",
                          title=str(title or "") or (f"{n} item{'s' if n != 1 else ''}" if n else ""))
            _state["version"] += 1
        _changed()
    if threading.current_thread() is threading.main_thread():
        threading.Thread(target=work, daemon=True, name="notch-search-show").start()
    else:
        work()


def clear() -> None:
    with _lock:
        _state["gen"] += 1
        if _debounce[0] is not None:
            _debounce[0].cancel()
            _debounce[0] = None
        _kill_procs()
        _state.update(items=[], title="", query="", searching=False)
        _state["version"] += 1
    _changed()


def items() -> list[dict]:
    with _lock:
        return [dict(e) for e in _state["items"]]


def has_results() -> bool:
    return bool(_state["items"])


def title() -> str:
    return _state["title"]


def query() -> str:
    return _state["query"]


def searching() -> bool:
    return bool(_state["searching"])


def available() -> bool:
    return True


def want_expanded() -> bool:
    return bool(_state["expanded"])


def set_expanded(expanded: bool) -> None:
    expanded = bool(expanded)
    if _state["expanded"] != expanded:
        _state["expanded"] = expanded
        _changed()


def dragging() -> bool:
    """A result is being dragged out of the notch (the notch should not open its shelf for it)."""
    return _dragging[0]


def grid() -> str:
    if _grid[0] is None:
        value = None
        with contextlib.suppress(Exception):
            value = prefs.get(GRID_PREF)
        _grid[0] = value if value in LAYOUTS else "3x2"
    return _grid[0]


def set_grid(layout: str) -> None:
    """3x1 / 3x2 / 3x3 (columns x rows on screen) or list; remembered in settings; asks for the big notch."""
    if layout not in LAYOUTS:
        return
    if grid() != layout:
        _grid[0] = layout
        try:
            prefs.set(GRID_PREF, layout)
        except Exception:
            log.debug("saving %s", GRID_PREF, exc_info=True)
    _state["expanded"] = True
    _changed()


def on_change(callback):
    """callback() on the main thread when the results, the searching state, the title or the wish to be
    expanded change. Returns an unsubscribe."""
    _listeners.append(callback)

    def unsubscribe():
        if callback in _listeners:
            _listeners.remove(callback)
    return unsubscribe


# --- doing things with results (main thread) ------------------------------------------------------------

_VERBS = ("find", "show", "search", "look", "where", "open", "get", "list", "locate", "which", "what", "any",
          "can you", "could you", "please", "give", "bring", "pull")


def _request(text: str) -> str:
    """A bare search ("lease pdf") becomes a request Mint can act on; a sentence goes as typed."""
    if text.lower().startswith(_VERBS) or len(text.split()) > 4:
        return text
    return f"Find my files matching “{text}” and show them in the notch"


def _post_say(text: str) -> None:
    """How `mint --say` reaches the running app (mint.main); tests replace this."""
    AppKit.NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
        SAY_NOTIFICATION, text, None, True)


def ask(text: str) -> bool:
    """Send a request to Mint ("find more like these"). False for empty text."""
    text = " ".join(str(text or "").split())
    if not text:
        return False
    _post_say(_request(text))
    return True


def open_paths(paths_in) -> None:
    notch_shelf.open_paths(paths_in)             # openURL: files open in their app, an app launches


def open_with(paths_in, app_path: str) -> None:
    urls = [AppKit.NSURL.fileURLWithPath_(p) for p in paths_in if os.path.exists(p)]
    if not urls:
        return
    AppKit.NSWorkspace.sharedWorkspace().openURLs_withApplicationAtURL_configuration_completionHandler_(
        urls, AppKit.NSURL.fileURLWithPath_(app_path), AppKit.NSWorkspaceOpenConfiguration.configuration(), None)


def copy_paths_text(paths_in) -> None:
    if paths_in:
        board = AppKit.NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_("\n".join(paths_in), AppKit.NSPasteboardTypeString)


def _apps_for(path: str) -> list[tuple[str, str, bool]]:
    """[(name, app path, is default)] that can open `path`, the default first."""
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    url = AppKit.NSURL.fileURLWithPath_(path)
    try:
        default = workspace.URLForApplicationToOpenURL_(url)
        urls = list(workspace.URLsForApplicationsToOpenURL_(url) or [])
    except Exception:
        return []
    default_path = str(default.path()) if default is not None else ""
    seen, rows = set(), []
    for app in ([default] if default is not None else []) + urls:
        app_path = str(app.path())
        if app_path in seen:
            continue
        seen.add(app_path)
        rows.append((_app_name(app_path), app_path, app_path == default_path))
    head, rest = rows[:1] if default_path else [], rows[1:] if default_path else rows
    return head + sorted(rest, key=lambda r: r[0].lower())[:24]


# Quick Look: QLPreviewPanel, fed by this object (and the view offers to control the panel).

class _NotchSearchQuickLook(AppKit.NSObject):
    def init(self):
        self = objc.super(_NotchSearchQuickLook, self).init()
        if self is None:
            return None
        self.urls = []
        self.owner = None
        return self

    @objc.typedSelector(b"q@:@")
    def numberOfPreviewItemsInPreviewPanel_(self, panel):
        return len(self.urls)

    @objc.typedSelector(b"@@:@q")
    def previewPanel_previewItemAtIndex_(self, panel, index):
        return self.urls[index] if 0 <= index < len(self.urls) else None

    @objc.typedSelector(b"Z@:@@")
    def previewPanel_handleEvent_(self, panel, event):
        # Arrows in the preview move through the results, like Finder.
        arrows = (123, 124, 125, 126)
        if self.owner is not None and event.type() == AppKit.NSEventTypeKeyDown and event.keyCode() in arrows:
            self.owner.key_move(event.keyCode(), 0)
            self.owner.quick_look(refresh=True)
            return True
        return False

    @objc.typedSelector(b"{CGRect={CGPoint=dd}{CGSize=dd}}@:@@")
    def previewPanel_sourceFrameOnScreenForPreviewItem_(self, panel, item):
        if self.owner is not None:
            rect = self.owner.screen_rect(str(item.path()) if item is not None else "")
            if rect is not None:
                return rect
        return AppKit.NSZeroRect


_ql_source: list = []


def _ql():
    if not _ql_source:
        _ql_source.append(_NotchSearchQuickLook.alloc().init())
    return _ql_source[0]


def quick_look(paths_in, owner=None, refresh: bool = False) -> None:
    """Preview files in Quick Look (toggles when already showing); qlmanage if the panel is unavailable."""
    paths_in = [p for p in paths_in if os.path.exists(p)]
    panel_class = getattr(Quartz, "QLPreviewPanel", None)
    if panel_class is None:
        if paths_in:
            subprocess.Popen(["qlmanage", "-p", *paths_in], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    source = _ql()
    panel = panel_class.sharedPreviewPanel()
    if panel.isVisible() and not refresh:
        panel.orderOut_(None)
        return
    if not paths_in:
        return
    source.urls = [AppKit.NSURL.fileURLWithPath_(p) for p in paths_in]
    source.owner = owner
    panel.setDataSource_(source)
    panel.setDelegate_(source)
    panel.reloadData()
    if refresh:
        return
    panel.setCurrentPreviewItemIndex_(0)
    AppKit.NSApp.activateIgnoringOtherApps_(True)          # the panel needs Mint active to take keys
    panel.makeKeyAndOrderFront_(None)


# --- thumbnails (QuickLook, in the background; cached by path + mtime + size) ---------------------------

_thumbs: dict = {}


def _thumbnail(path: str, px: int, done) -> None:
    """done(NSImage) on the main thread with a QuickLook thumbnail about px points square."""
    key = (path, notch_shelf._mtime(path), px)
    if key in _thumbs:
        done(_thumbs[key])
        return
    classes = notch_shelf._generator()
    if classes is None:
        return
    generator, request_class = classes

    def finished(rep, error):
        image = rep.NSImage() if rep is not None else None
        if image is None:
            return

        def give():
            if len(_thumbs) > 400:
                _thumbs.clear()
            _thumbs[key] = image
            done(image)
        AppHelper.callAfter(give)
    try:
        request = request_class.alloc().initWithFileAtURL_size_scale_representationTypes_(
            AppKit.NSURL.fileURLWithPath_(path), (px, px), 2.0, 0xFFFFFFFF)
        generator.sharedGenerator().generateBestRepresentationForRequest_completionHandler_(request, finished)
    except Exception:
        log.debug("thumbnail %s", path, exc_info=True)


_names: dict = {}


def _text_height(text: str, font, width: float) -> float:
    return math.ceil(AppKit.NSAttributedString.alloc().initWithString_attributes_(
        text, {AppKit.NSFontAttributeName: font}).boundingRectWithSize_options_(
        AppKit.NSMakeSize(width, 1000), AppKit.NSStringDrawingUsesLineFragmentOrigin).size.height)


def _two_lines(name: str, font, width: float) -> str:
    key = (name, font.pointSize(), round(width))
    if key not in _names:
        if len(_names) > 2000:
            _names.clear()
        _names[key] = notch_shelf._two_lines(name, font, width)
    return _names[key]


# --- AppKit pieces ----------------------------------------------------------------------------------------

class _NotchSearchAct(AppKit.NSObject):
    def initWithFn_(self, fn):
        self = objc.super(_NotchSearchAct, self).init()
        if self is not None:
            self.fn = fn
        return self

    def fire_(self, sender):
        try:
            self.fn()
        except Exception:
            log.exception("search button")


class _NotchSearchButton(AppKit.NSButton):
    def acceptsFirstMouse_(self, event):
        return True                     # one click, even while the notch panel is not key


class _NotchSearchField(AppKit.NSTextField):
    def acceptsFirstMouse_(self, event):
        return True

    def becomeFirstResponder(self):
        ok = objc.super(_NotchSearchField, self).becomeFirstResponder()
        if ok and getattr(self, "owner", None) is not None:
            self.owner.field_focus(True)
        return ok


class _NotchSearchRoot(AppKit.NSView):
    """The whole view; takes the keyboard for the results, and offers to drive Quick Look."""

    def acceptsFirstMouse_(self, event):
        return True

    def acceptsFirstResponder(self):
        return True

    def keyDown_(self, event):
        if self.owner is None or not self.owner.key_down(event):
            objc.super(_NotchSearchRoot, self).keyDown_(event)

    def performKeyEquivalent_(self, event):
        window = self.window()
        if self.owner is not None and window is not None and window.firstResponder() is self \
                and self.owner.key_equivalent(event):
            return True
        return objc.super(_NotchSearchRoot, self).performKeyEquivalent_(event)

    @objc.typedSelector(b"Z@:@")
    def acceptsPreviewPanelControl_(self, panel):
        return True

    @objc.typedSelector(b"v@:@")
    def beginPreviewPanelControl_(self, panel):
        panel.setDataSource_(_ql())
        panel.setDelegate_(_ql())

    @objc.typedSelector(b"v@:@")
    def endPreviewPanelControl_(self, panel):
        pass


class _NotchSearchDoc(AppKit.NSView):
    def isFlipped(self):
        return True                     # rows run top-down

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        if self.owner is not None:
            self.owner.focus_results()
            self.owner.select_none()


class _NotchSearchScroll(AppKit.NSScrollView):
    """The compact row scrolls sideways, also for a plain (vertical) mouse wheel."""

    def scrollWheel_(self, event):
        owner = getattr(self, "owner", None)
        dx, dy = event.scrollingDeltaX(), event.scrollingDeltaY()
        if owner is not None and owner.mode == "compact" and abs(dy) > abs(dx):
            clip = self.contentView()
            step = dy if event.hasPreciseScrollingDeltas() else dy * 12
            doc_w = self.documentView().frame().size.width
            x = min(max(0.0, clip.bounds().origin.x - step), max(0.0, doc_w - clip.bounds().size.width))
            clip.scrollToPoint_(AppKit.NSMakePoint(x, 0))
            self.reflectScrolledClipView_(clip)
            return
        objc.super(_NotchSearchScroll, self).scrollWheel_(event)


class _NotchSearchTile(AppKit.NSView):
    """One result: select, open, drag out (NSDraggingSource), right-click menu, hover."""

    def acceptsFirstMouse_(self, event):
        return True

    def hitTest_(self, point):
        # The whole tile answers clicks: its thumbnail (an NSImageView) would refuse the first click into
        # the non-activating notch, so a drag started on the picture never began.
        found = objc.super(_NotchSearchTile, self).hitTest_(point)
        return self if found is not None else None

    def updateTrackingAreas(self):
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        options = (AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways
                   | AppKit.NSTrackingInVisibleRect)
        self.addTrackingArea_(AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None))
        objc.super(_NotchSearchTile, self).updateTrackingAreas()

    def mouseEntered_(self, event):
        self.owner.hover(self, True)

    def mouseExited_(self, event):
        self.owner.hover(self, False)

    def mouseDown_(self, event):
        self.down_at = event.locationInWindow()
        self.owner.focus_results()
        if event.clickCount() >= 2:
            self.down_at = None
            self.owner.open_tile(self)
            return
        self.owner.click(self, int(event.modifierFlags()))

    def mouseDragged_(self, event):
        start = getattr(self, "down_at", None)
        if start is None:
            return
        where = event.locationInWindow()
        if abs(where.x - start.x) + abs(where.y - start.y) <= DRAG_START:
            return
        self.down_at = None
        self.owner.drag_out(self, event)

    def mouseUp_(self, event):
        self.down_at = None

    def menuForEvent_(self, event):
        return self.owner.tile_menu(self)

    # NSDraggingSource: a copy by default (⌘⌥ makes an alias); never a move.
    def draggingSession_sourceOperationMaskForDraggingContext_(self, session, context):
        if context == AppKit.NSDraggingContextOutsideApplication:
            return AppKit.NSDragOperationCopy | AppKit.NSDragOperationLink
        return AppKit.NSDragOperationCopy

    def draggingSession_endedAtPoint_operation_(self, session, point, operation):
        self.owner.drag_ended()

    def ignoreModifierKeysForDraggingSession_(self, session):
        return False


class _NotchSearchTarget(AppKit.NSObject):
    """The field's delegate, the menus' target, scroll notifications, and the shared refresh timer."""

    def initWithOwner_(self, owner):
        self = objc.super(_NotchSearchTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def controlTextDidChange_(self, note):
        self.owner.typed()

    def controlTextDidEndEditing_(self, note):
        self.owner.field_focus(False)

    def control_textView_doCommandBySelector_(self, control, view, selector):
        name = selector.decode() if isinstance(selector, bytes) else str(selector)
        return bool(self.owner.field_command(name))

    def menu_(self, sender):
        self.owner.menu_action(str(sender.representedObject() or ""))

    def scrolled_(self, note):
        if self.owner is not None:
            self.owner.scrolled()

    def tick_(self, timer):
        try:
            _tick()
        except Exception:
            log.debug("search tick", exc_info=True)


# --- the view ---------------------------------------------------------------------------------------------

class SearchView:
    def __init__(self, width: float, height: float) -> None:
        self.w, self.h = float(width), float(height)
        self.mode = "compact"
        self.acts: list = []
        self.tiles: dict = {}                    # path -> tile
        self.order: list[str] = []
        self.selected: set[str] = set()
        self.anchor: str | None = None
        self.cursor: int | None = None
        self.hovered = None
        self.cols = 1
        self.layout = ""
        self.version = -1
        self.gen = -1
        self.was_visible = False
        self.in_window = False
        self.hint_until = 0.0
        self.target = _NotchSearchTarget.alloc().initWithOwner_(self)
        root = _NotchSearchRoot.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.w, self.h))
        root.owner = self
        root.setWantsLayer_(True)
        root.layer().setMasksToBounds_(True)
        root.layer().setBackgroundColor_(_cg((0, 0, 0), 0.0))
        root.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        self.view = root
        self._build()
        self._layout()
        self.reload()

    # building
    def _button(self, symbol: str, tip: str, fn, point: float = 11, title: str = ""):
        act = _NotchSearchAct.alloc().initWithFn_(fn)
        self.acts.append(act)
        if title:
            button = _NotchSearchButton.buttonWithTitle_target_action_(title, act, "fire:")
        else:
            button = _NotchSearchButton.buttonWithImage_target_action_(gfx.symbol(symbol, point), act, "fire:")
        button.setBordered_(False)
        button.setToolTip_(tip)
        button.setContentTintColor_(_ns(INK, 0.8))
        button.setWantsLayer_(True)
        button.layer().setCornerRadius_(8)
        button.layer().setCornerCurve_(Quartz.kCACornerCurveContinuous)
        return button

    def _group(self, entries, symbols: bool):
        """A pill of small buttons (scope chips or layout icons). entries: [(key, label/symbol, tip, fn)]."""
        box = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, BAR_H))
        box.setWantsLayer_(True)
        box.layer().setCornerRadius_(9)
        box.layer().setCornerCurve_(Quartz.kCACornerCurveContinuous)
        box.layer().setBackgroundColor_(_cg(INK, 0.07))
        buttons, x = {}, 3.0
        font = _font(11, AppKit.NSFontWeightSemibold)
        for key, face, tip, fn in entries:
            if symbols:
                button = self._button(face, tip, fn, 11)
                width = 26.0
            else:
                button = self._button("", tip, fn, title=face)
                width = math.ceil(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    face, {AppKit.NSFontAttributeName: font}).size().width) + 14
            button.layer().setCornerRadius_(7)
            button.setFrame_(AppKit.NSMakeRect(x, 3, width, BAR_H - 6))
            box.addSubview_(button)
            buttons[key] = (button, face)
            x += width + 1
        box.setFrameSize_(AppKit.NSMakeSize(x + 2, BAR_H))
        return box, buttons

    def _build(self) -> None:
        root = self.view
        # the field, in a pill: magnifier, text, clear ×, count or spinner
        pill = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 200, BAR_H))
        pill.setWantsLayer_(True)
        pill.layer().setCornerRadius_(9)
        pill.layer().setCornerCurve_(Quartz.kCACornerCurveContinuous)
        pill.layer().setBackgroundColor_(_cg(INK, 0.1))
        pill.layer().setBorderWidth_(1)
        pill.layer().setBorderColor_(_cg(INK, 0.06))
        self.pill = pill
        glass = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("magnifyingglass", 12, "medium"))
        glass.setContentTintColor_(_ns(DIM))
        glass.setFrame_(AppKit.NSMakeRect(8, (BAR_H - 16) / 2, 16, 16))
        pill.addSubview_(glass)
        field = _NotchSearchField.alloc().initWithFrame_(AppKit.NSMakeRect(28, 5, 100, 18))
        field.owner = self
        field.setBezeled_(False)
        field.setBordered_(False)
        field.setDrawsBackground_(False)
        field.setFocusRingType_(AppKit.NSFocusRingTypeNone)
        field.setFont_(_font(13))
        field.setTextColor_(_ns(INK))
        field.cell().setScrollable_(True)
        field.cell().setWraps_(False)
        field.cell().setUsesSingleLineMode_(True)
        field.setDelegate_(self.target)
        pill.addSubview_(field)
        self.field = field
        self.clear_btn = self._button("xmark.circle.fill", "Clear", self.clear_field, 11)
        self.clear_btn.setContentTintColor_(_ns(DIM))
        pill.addSubview_(self.clear_btn)
        self.count = _label(10.5, AppKit.NSFontWeightMedium, DIM)
        self.count.setAlignment_(AppKit.NSTextAlignmentRight)
        pill.addSubview_(self.count)
        spinner = AppKit.NSProgressIndicator.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 16, 16))
        spinner.setStyle_(AppKit.NSProgressIndicatorStyleSpinning)
        spinner.setControlSize_(AppKit.NSControlSizeSmall)
        spinner.setDisplayedWhenStopped_(False)
        pill.addSubview_(spinner)
        self.spinner = spinner
        root.addSubview_(pill)

        self.scopes, self.scope_buttons = self._group(
            [(key, label, tip, (lambda k=key: self.set_scope(k))) for key, label, tip in (
                ("all", "All", "Apps, files and folders"), ("apps", "Apps", "Only apps"),
                ("files", "Files", "Only files"), ("folders", "Folders", "Only folders"))], symbols=False)
        root.addSubview_(self.scopes)
        self.layouts, self.layout_buttons = self._group(
            [(key, symbol, tip, (lambda k=key: set_grid(k))) for key, symbol, tip in (
                ("3x1", "rectangle.split.3x1", "3 × 1: big previews"), ("3x2", "square.grid.3x2", "3 × 2"),
                ("3x3", "square.grid.3x3", "3 × 3"), ("list", "list.bullet", "List"))], symbols=True)
        root.addSubview_(self.layouts)

        ask = self._button("sparkles", "Ask Mint to find it (sends what you typed to Mint)", self.ask_clicked,
                           title=" Ask Mint")
        ask.setImage_(gfx.symbol("sparkles", 10, "semibold"))
        ask.setImagePosition_(AppKit.NSImageLeft)
        ask.setImageHugsTitle_(True)
        self.ask_btn = ask
        self.ask_w = math.ceil(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            " Ask Mint", {AppKit.NSFontAttributeName: _font(11.5, AppKit.NSFontWeightSemibold)}).size().width) + 36
        root.addSubview_(ask)
        self.size_btn = self._button("arrow.up.left.and.arrow.down.right", "Show more", self.size_clicked, 11)
        self.size_btn.layer().setBackgroundColor_(_cg(INK, 0.07))
        root.addSubview_(self.size_btn)

        # grid header: the title on the left, how to use it on the right
        self.head = _label(12.5, AppKit.NSFontWeightSemibold, INK, 0.95)
        self.hint = _label(10.5, AppKit.NSFontWeightMedium, FAINT)
        self.hint.setAlignment_(AppKit.NSTextAlignmentRight)
        self.hint.setStringValue_(HOW_TO)
        root.addSubview_(self.head)
        root.addSubview_(self.hint)

        scroll = _NotchSearchScroll.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.w, 100))
        scroll.owner = self
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(AppKit.NSNoBorder)
        scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
        scroll.setScrollerKnobStyle_(AppKit.NSScrollerKnobStyleLight)
        scroll.setAutohidesScrollers_(True)
        scroll.setWantsLayer_(True)
        document = _NotchSearchDoc.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.w, 100))
        document.owner = self
        scroll.setDocumentView_(document)
        clip = scroll.contentView()
        clip.setPostsBoundsChangedNotifications_(True)
        AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self.target, "scrolled:", AppKit.NSViewBoundsDidChangeNotification, clip)
        self.fade = Quartz.CAGradientLayer.layer()          # more to scroll to: that edge fades
        self.scroll, self.document = scroll, document
        root.addSubview_(scroll)

        # empty states
        self.empty = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, 10))
        self.empty_icon = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, 26))
        self.empty_icon.setContentTintColor_(_ns(DIM))
        self.empty_text = _label(13, AppKit.NSFontWeightMedium, DIM, lines=2)
        self.empty_text.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.empty_spin = AppKit.NSProgressIndicator.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 20, 20))
        self.empty_spin.setStyle_(AppKit.NSProgressIndicatorStyleSpinning)
        self.empty_spin.setControlSize_(AppKit.NSControlSizeRegular)
        self.empty_spin.setDisplayedWhenStopped_(False)
        for part in (self.empty_icon, self.empty_text, self.empty_spin):
            self.empty.addSubview_(part)
        root.addSubview_(self.empty)

        # ‹ › for the compact row
        self.prev_btn = self._button("chevron.left", "Scroll left", lambda: self.page(-1), 10)
        self.next_btn = self._button("chevron.right", "Scroll right", lambda: self.page(1), 10)
        for button in (self.prev_btn, self.next_btn):
            button.layer().setCornerRadius_(11)
            button.layer().setBackgroundColor_(_cg((0.12, 0.12, 0.13), 0.92))
            button.layer().setBorderWidth_(1)
            button.layer().setBorderColor_(_cg(INK, 0.14))
            button.setContentTintColor_(_ns(INK, 0.9))
            root.addSubview_(button)

    # layout
    def _layout(self) -> None:
        w, h = self.w, self.h
        self.mode = "compact" if h < COMPACT_BELOW else "grid"
        grid_mode = self.mode == "grid"
        self.view.setFrameSize_(AppKit.NSMakeSize(w, h))
        y = h - BAR_H
        right = w
        self.size_btn.setImage_(gfx.symbol("arrow.down.right.and.arrow.up.left" if grid_mode
                                           else "arrow.up.left.and.arrow.down.right", 11))
        self.size_btn.setToolTip_("Show less" if grid_mode else "Show more (a bigger grid)")
        self.size_btn.setFrame_(AppKit.NSMakeRect(right - 28, y, 28, BAR_H))
        right -= 28 + GAP
        self.ask_btn.setFrame_(AppKit.NSMakeRect(right - self.ask_w, y, self.ask_w, BAR_H))
        right -= self.ask_w + GAP
        lw = self.layouts.frame().size.width
        self.layouts.setHidden_(not grid_mode)
        if grid_mode:
            self.layouts.setFrameOrigin_(AppKit.NSMakePoint(right - lw, y))
            right -= lw + GAP
        sw = self.scopes.frame().size.width
        room = right - sw - GAP >= 180
        self.scopes.setHidden_(not room)
        if room:
            self.scopes.setFrameOrigin_(AppKit.NSMakePoint(right - sw, y))
            right -= sw + GAP
        pw = max(120.0, right)
        self.pill.setFrame_(AppKit.NSMakeRect(0, y, pw, BAR_H))
        self.spinner.setFrameOrigin_(AppKit.NSMakePoint(pw - 24, (BAR_H - 16) / 2))
        self.count.setFrame_(AppKit.NSMakeRect(pw - 118, (BAR_H - 14) / 2, 110, 14))
        self._field_width()
        self.head.setHidden_(not grid_mode)
        self.hint.setHidden_(not grid_mode)
        if grid_mode:
            self.head.setFrame_(AppKit.NSMakeRect(4, y - 23, w * 0.55, 17))
            self.hint.setFrame_(AppKit.NSMakeRect(w * 0.45, y - 22, w * 0.55 - 4, 15))
            ch = y - 28
        else:
            ch = y - GAP
        self.scroll.setFrame_(AppKit.NSMakeRect(0, 0, w, ch))
        self.scroll.setHasVerticalScroller_(grid_mode)
        self.scroll.setHasHorizontalScroller_(False)
        self.scroll.setVerticalScrollElasticity_(AppKit.NSScrollElasticityAllowed if grid_mode
                                                 else AppKit.NSScrollElasticityNone)
        self.scroll.setHorizontalScrollElasticity_(AppKit.NSScrollElasticityNone if grid_mode
                                                   else AppKit.NSScrollElasticityAllowed)
        self.fade.setFrame_(Quartz.CGRectMake(0, 0, w, ch))
        self.empty.setFrame_(AppKit.NSMakeRect(0, 0, w, ch))
        mid = ch / 2
        self.empty_icon.setFrame_(AppKit.NSMakeRect(0, mid + 4, w, 26))
        self.empty_spin.setFrame_(AppKit.NSMakeRect(w / 2 - 10, mid + 7, 20, 20))
        self.empty_text.setFrame_(AppKit.NSMakeRect(20, mid - 38, w - 40, 36))
        row_mid = ch / 2 + 14
        self.prev_btn.setFrame_(AppKit.NSMakeRect(2, row_mid - 11, 22, 22))
        self.next_btn.setFrame_(AppKit.NSMakeRect(w - 24, row_mid - 11, 22, 22))
        self._arrange(keep=True)

    def _field_width(self) -> None:
        pw = self.pill.frame().size.width
        busy = searching()
        tail = 28 if busy else (min(110.0, self.count.attributedStringValue().size().width + 14) if
                                self.count.stringValue() else 8)
        clear = 20 if self.field.stringValue() else 0
        self.clear_btn.setHidden_(not clear)
        self.clear_btn.setFrame_(AppKit.NSMakeRect(pw - tail - 20, (BAR_H - 18) / 2, 18, 18))
        self.field.setFrame_(AppKit.NSMakeRect(28, (BAR_H - 18) / 2, max(40.0, pw - 28 - tail - clear - 4), 18))

    def _geometry(self):
        """(tile w, tile h, style, cols, gap) for the current mode and layout."""
        cw = self.scroll.frame().size.width
        ch = self.scroll.frame().size.height
        if self.mode == "compact":
            return COMPACT_TILE_W, ch, "v", max(1, len(self.order)), 6.0
        layout = grid()
        if layout == "list":
            return cw - 10, ROW_H, "row", 1, 2.0
        cols, rows = LAYOUTS[layout]
        gap = 8.0
        tw = math.floor((cw - (cols - 1) * gap) / cols)
        th = math.floor((ch - (rows - 1) * gap) / rows)
        return tw, th, ("h" if tw / max(1, th) > 1.55 else "v"), cols, gap

    def _arrange(self, keep: bool = True) -> None:
        tw, th, style, cols, gap = self._geometry()
        self.cols = cols
        self.layout = grid()
        cw = self.scroll.frame().size.width
        ch = self.scroll.frame().size.height
        n = len(self.order)
        if self.mode == "compact":
            doc = (max(cw, n * tw + max(0, n - 1) * gap), ch)
        else:
            rows = math.ceil(n / cols) if n else 0
            doc = (cw, max(ch, rows * th + max(0, rows - 1) * gap))
        self.document.setFrameSize_(AppKit.NSMakeSize(*doc))
        for i, path in enumerate(self.order):
            tile = self.tiles[path]
            if self.mode == "compact":
                x, y = i * (tw + gap), 0.0
            else:
                x, y = (i % cols) * (tw + gap), (i // cols) * (th + gap)
            tile.setFrame_(AppKit.NSMakeRect(x, y, tw, th))
            self._shape(tile, tw, th, style)
        if not keep:
            self.scroll.contentView().scrollToPoint_(AppKit.NSMakePoint(0, 0))
        self.scroll.reflectScrolledClipView_(self.scroll.contentView())
        self.scrolled()

    # tiles
    def _make_tile(self, entry: dict):
        tile = _NotchSearchTile.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, 10))
        tile.owner = self
        tile.path = entry["path"]
        tile.entry = entry
        tile.shaped = None
        tile.icon_ok = False
        tile.thumb_px = 0
        tile.image = None
        tile.box = 0.0
        tile.setWantsLayer_(True)
        tile.layer().setCornerRadius_(10)
        tile.layer().setCornerCurve_(Quartz.kCACornerCurveContinuous)
        tile.setToolTip_(_where(entry["path"]) + "/" + os.path.basename(entry["path"].rstrip("/")))
        holder = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, 10))
        holder.setWantsLayer_(True)
        shadow = AppKit.NSShadow.alloc().init()
        shadow.setShadowColor_(_ns((0, 0, 0), 0.25))
        shadow.setShadowBlurRadius_(3)
        shadow.setShadowOffset_(AppKit.NSMakeSize(0, -2))
        holder.setShadow_(shadow)
        image = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, 10))
        image.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        image.setWantsLayer_(True)
        image.layer().setMasksToBounds_(True)
        holder.addSubview_(image)
        tile.addSubview_(holder)
        name = _label(12, AppKit.NSFontWeightMedium, INK, lines=2)
        sub = _label(10, AppKit.NSFontWeightRegular, DIM)
        tile.addSubview_(name)
        tile.addSubview_(sub)
        tile.parts = {"holder": holder, "image": image, "name": name, "sub": sub}
        return tile

    def _shape(self, tile, tw: float, th: float, style: str) -> None:
        entry = tile.entry
        key = (tw, th, style, self.mode, entry.get("date"), entry.get("size"), entry.get("type"))
        if tile.shaped == key:
            return
        tile.shaped = key
        p = tile.parts
        name, sub, holder = p["name"], p["sub"], p["holder"]
        compact = self.mode == "compact"
        if style == "v":
            name_font = _font(11.5 if compact else 12.5, AppKit.NSFontWeightMedium)
            big = th > 250
            sub_lines = 1 if compact else (2 if big else 1)
            sub_h = 13.0 * sub_lines
            name_h = 30.0 if compact else 32.0
            box = max(24.0, min(tw - 16, th - 8 - 4 - name_h - sub_h - 6))
            name.setFont_(name_font)
            name.setAlignment_(AppKit.NSTextAlignmentCenter)
            name.setMaximumNumberOfLines_(2)
            name.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
            text = _two_lines(entry["name"], name_font, tw - 18)      # a label draws ~4 pt narrower than its frame
            name.setStringValue_(text)
            used = min(name_h, _text_height(text, name_font, tw - 18) + 3)
            block = box + 4 + used + 1 + sub_h
            top = 7.0 if compact else max(7.0, (th - block) / 2)      # tall tiles: the whole block centred
            holder.setFrame_(AppKit.NSMakeRect(round((tw - box) / 2), round(th - top - box), box, box))
            name_y = round(th - top - box - 4 - used)
            name.setFrame_(AppKit.NSMakeRect(5, name_y, tw - 10, used))
            sub.setFont_(_font(10 if compact else 10.5))
            sub.setAlignment_(AppKit.NSTextAlignmentCenter)
            sub.setMaximumNumberOfLines_(sub_lines)
            text = _short(entry) if compact else _long(entry)
            if big and entry.get("where"):
                text += "\n" + entry["where"]
            sub.setStringValue_(text)
            sub.setFrame_(AppKit.NSMakeRect(5, max(4, name_y - 1 - sub_h), tw - 10, sub_h))
        elif style == "h":
            box = th - 16
            holder.setFrame_(AppKit.NSMakeRect(8, 8, box, box))
            x = 8 + box + 10
            name_font = _font(13, AppKit.NSFontWeightSemibold)
            name.setFont_(name_font)
            name.setAlignment_(AppKit.NSTextAlignmentLeft)
            name.setMaximumNumberOfLines_(2)
            name.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
            text = _two_lines(entry["name"], name_font, tw - x - 16)
            name.setStringValue_(text)
            used = min(34.0, _text_height(text, name_font, tw - x - 16) + 3)
            name.setFrame_(AppKit.NSMakeRect(x, th - 10 - used, tw - x - 8, used))
            sub.setFont_(_font(10.5))
            sub.setAlignment_(AppKit.NSTextAlignmentLeft)
            sub.setMaximumNumberOfLines_(3)
            lines = [" · ".join(q for q in (entry.get("type", ""), entry.get("size", "")) if q),
                     entry.get("date", ""), entry.get("where", "")]
            sub.setStringValue_("\n".join(q for q in lines if q))
            sub.setFrame_(AppKit.NSMakeRect(x, 8, tw - x - 8, th - 10 - used - 4 - 8))
        else:                                  # a list row
            box = 30.0
            holder.setFrame_(AppKit.NSMakeRect(10, (th - box) / 2, box, box))
            x = 10 + box + 10
            name.setFont_(_font(13, AppKit.NSFontWeightMedium))
            name.setAlignment_(AppKit.NSTextAlignmentLeft)
            name.setMaximumNumberOfLines_(1)
            name.setLineBreakMode_(AppKit.NSLineBreakByTruncatingMiddle)
            name.setStringValue_(entry["name"])
            name.setFrame_(AppKit.NSMakeRect(x, th / 2 - 1, tw - x - 10, 18))
            sub.setFont_(_font(10.5))
            sub.setAlignment_(AppKit.NSTextAlignmentLeft)
            sub.setMaximumNumberOfLines_(1)
            sub.setStringValue_(" · ".join(q for q in (_long(entry), entry.get("where", "")) if q))
            sub.setFrame_(AppKit.NSMakeRect(x, th / 2 - 17, tw - x - 10, 15))
        tile.box = box
        tile.style = style
        self._fit_image(tile)

    def _fit_image(self, tile) -> None:
        image_view, box = tile.parts["image"], tile.box
        image = tile.image
        if image is None:
            image_view.setFrame_(AppKit.NSMakeRect(0, 0, box, box))
            return
        fx, fy, fw, fh = notch_shelf._fit(image.size(), box)
        if getattr(tile, "style", "") == "v":
            fy = 0.0                            # a wide picture sits on its name, not floating above it
        image_view.setFrame_(AppKit.NSMakeRect(round(fx), round(fy), round(fw), round(fh)))
        thumb = tile.thumb_px > 0
        image_view.layer().setCornerRadius_(min(10.0, fw / 6, fh / 6) if thumb else 0.0)
        image_view.setImage_(image)

    def _load_visible(self) -> None:
        """Icons and QuickLook thumbnails for the tiles on screen (and one tile beyond) only."""
        if not self._visible() or not self.order:
            return
        seen = self.scroll.contentView().bounds()
        tw, th, _style, _cols, _gap = self._geometry()
        wide = AppKit.NSInsetRect(seen, -tw, -th)
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        for path in self.order:
            tile = self.tiles[path]
            if not AppKit.NSIntersectsRect(tile.frame(), wide):
                continue
            if not tile.icon_ok:
                tile.icon_ok = True
                tile.image = workspace.iconForFile_(path)
                self._fit_image(tile)
            if tile.entry["kind"] != "file":
                continue                       # an app's or folder's icon is its best picture
            px = 64 if tile.box <= 64 else 128 if tile.box <= 128 else 256
            if tile.thumb_px >= px:
                continue
            tile.thumb_px = px

            def got(image, tile=tile, px=px):
                if self.tiles.get(tile.path) is not tile:
                    return
                tile.image = image
                tile.thumb_px = px
                self._fit_image(tile)
            _thumbnail(path, px, got)

    def _visible(self) -> bool:
        window = self.view.window()
        return bool(window is not None and window.isVisible() and not self.view.isHiddenOrHasHiddenAncestor())

    # painting
    def reload(self) -> None:
        """Take the shared results (main thread)."""
        with _lock:
            version, gen = _state["version"], _state["gen"]
            entries = [dict(e) for e in _state["items"]] if version != self.version else None
        if entries is not None:
            self.version = version
            paths = [e["path"] for e in entries]
            keep = set(paths)
            for path in [p for p in self.tiles if p not in keep]:
                tile = self.tiles.pop(path)
                if tile is self.hovered:
                    self.hovered = None
                tile.removeFromSuperview()
            for entry in entries:
                tile = self.tiles.get(entry["path"])
                if tile is None:
                    tile = self._make_tile(entry)
                    self.tiles[entry["path"]] = tile
                    self.document.addSubview_(tile)
                else:
                    tile.entry = entry
            fresh = paths != self.order
            self.order = paths
            self.selected &= keep
            if self.cursor is not None and self.cursor >= len(paths):
                self.cursor = len(paths) - 1 if paths else None
            if self.anchor not in keep:
                self.anchor = None
            new_search = gen != self.gen
            self.gen = gen
            self._arrange(keep=not (fresh and new_search))
        elif self.mode == "grid" and self.layout != grid():
            self._arrange(keep=False)                   # the layout switcher: new tile sizes
            cursor = self.order[self.cursor] if self.cursor is not None and self.cursor < len(self.order) else None
            if cursor is not None:
                self.document.scrollRectToVisible_(self.tiles[cursor].frame())
        self._paint()

    def _paint(self) -> None:
        q, busy, head = query(), searching(), title()
        n = len(self.order)
        editing = self.field.currentEditor() is not None
        if not editing and str(self.field.stringValue()) != q:
            self.field.setStringValue_(q)
        placeholder = ("Type what to find, then Ask Mint" if time.monotonic() < self.hint_until
                       else (head if head and not q else "Search files and apps"))
        self.field.setPlaceholderAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            placeholder, {AppKit.NSForegroundColorAttributeName: _ns(DIM, 0.9), AppKit.NSFontAttributeName: _font(13)}))
        if busy:
            self.spinner.startAnimation_(None)
            self.count.setStringValue_("")
        else:
            self.spinner.stopAnimation_(None)
            self.count.setStringValue_(f"{n} result{'s' if n != 1 else ''}" if n and (q or head) else "")
        self.count.setHidden_(busy)
        self._field_width()
        # empty states
        empty = n == 0
        self.empty.setHidden_(not empty)
        self.scroll.setHidden_(empty)
        if empty:
            if busy:
                self.empty_spin.startAnimation_(None)
                self.empty_icon.setImage_(None)
                self.empty_text.setStringValue_("Searching…")
            else:
                self.empty_spin.stopAnimation_(None)
                self.empty_icon.setImage_(gfx.symbol("doc.questionmark" if q else "magnifyingglass", 20, "medium"))
                self.empty_text.setStringValue_(f"No results for “{q}”" if q else "Type to search files and apps")
        else:
            self.empty_spin.stopAnimation_(None)
        # header, chips, layout icons
        if self.mode == "grid":
            self.head.setStringValue_(head or (f"Results for “{q}”" if q else "Search"))
        scope = _state["scope"]
        for key, (button, face) in self.scope_buttons.items():
            on = key == scope
            button.layer().setBackgroundColor_(_cg(INK, 0.16 if on else 0.0))
            button.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                face, {AppKit.NSFontAttributeName: _font(11, AppKit.NSFontWeightSemibold),
                       AppKit.NSForegroundColorAttributeName: _ns(INK, 0.95 if on else 0.55)}))
        layout = grid()
        for key, (button, _face) in self.layout_buttons.items():
            on = key == layout
            button.layer().setBackgroundColor_(_cg(INK, 0.16 if on else 0.0))
            button.setContentTintColor_(_ns(INK, 0.95 if on else 0.55))
        accent = _accent()
        self.ask_btn.layer().setBackgroundColor_(_cg(accent, 0.18))
        self.ask_btn.setContentTintColor_(_ns(accent))
        self.ask_btn.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            " Ask Mint", {AppKit.NSFontAttributeName: _font(11.5, AppKit.NSFontWeightSemibold),
                          AppKit.NSForegroundColorAttributeName: _ns(accent)}))
        self._paint_tiles()

    def _paint_tiles(self) -> None:
        accent = _accent()
        for path in self.order:
            tile = self.tiles[path]
            chosen = path in self.selected
            hover = 0.07 if tile is self.hovered else 0.0
            layer = tile.layer()
            layer.setBackgroundColor_(_cg(accent, 0.18) if chosen else _cg(INK, hover))
            layer.setBorderColor_(_cg(accent, 0.8) if chosen else _cg(INK, 0.0))
            layer.setBorderWidth_(1.5 if chosen else 0.0)

    def scrolled(self) -> None:
        """Edges fade where there is more; ‹ › show in the compact row; thumbnails for what came into view."""
        clip = self.scroll.contentView().bounds()
        size = self.document.frame().size
        if self.mode == "compact":
            before, after = clip.origin.x > 1, clip.origin.x + clip.size.width < size.width - 1
            start, end = Quartz.CGPointMake(0, 0.5), Quartz.CGPointMake(1, 0.5)
            span = clip.size.width
        else:
            before, after = clip.origin.y > 1, clip.origin.y + clip.size.height < size.height - 1
            start, end = Quartz.CGPointMake(0.5, 1), Quartz.CGPointMake(0.5, 0)     # layer y runs upwards
            span = clip.size.height
        compact = self.mode == "compact" and bool(self.order)
        self.prev_btn.setHidden_(not (compact and before))
        self.next_btn.setHidden_(not (compact and after))
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        if before or after:
            edge = min(0.2, 18.0 / max(1.0, span))
            self.fade.setStartPoint_(start)
            self.fade.setEndPoint_(end)
            self.fade.setColors_([_cg((0, 0, 0), 0.0 if before else 1.0), _cg((0, 0, 0), 1.0),
                                  _cg((0, 0, 0), 1.0), _cg((0, 0, 0), 0.0 if after else 1.0)])
            self.fade.setLocations_([0.0, edge, 1.0 - edge, 1.0])
            self.scroll.layer().setMask_(self.fade)
        else:
            self.scroll.layer().setMask_(None)
        Quartz.CATransaction.commit()
        self._load_visible()

    def page(self, direction: int) -> None:
        clip = self.scroll.contentView()
        width = clip.bounds().size.width
        doc_w = self.document.frame().size.width
        x = min(max(0.0, clip.bounds().origin.x + direction * width * 0.8), max(0.0, doc_w - width))
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.25)
        clip.animator().setBoundsOrigin_(AppKit.NSMakePoint(x, 0))
        AppKit.NSAnimationContext.endGrouping()
        self.scroll.reflectScrolledClipView_(clip)

    # the field
    def typed(self) -> None:
        self._field_width()
        search(str(self.field.stringValue()), _state["scope"])

    def field_focus(self, focused: bool) -> None:
        self.pill.layer().setBorderColor_(_cg(_accent(), 0.55) if focused else _cg(INK, 0.06))

    def field_command(self, name: str) -> bool:
        if name in ("moveDown:", "insertTab:"):
            self.focus_results(select=True)
            return True
        if name == "insertNewline:":
            chosen = self._chosen() or self.order[:1]
            if chosen:
                open_paths(chosen)
            return True
        if name == "cancelOperation:":
            if self.field.stringValue():
                self.clear_field()
            return True                          # Esc never closes anything else
        return False

    def clear_field(self) -> None:
        self.field.setStringValue_("")
        search("", _state["scope"])
        self._field_width()

    def focus_field(self) -> bool:
        window = self.view.window()
        if window is None:
            return False
        ok = bool(window.makeFirstResponder_(self.field))
        editor = self.field.currentEditor()
        if editor is not None:
            editor.setSelectedRange_((len(self.field.stringValue()), 0))
        return ok

    def focus_results(self, select: bool = False) -> None:
        window = self.view.window()
        if window is not None and window.firstResponder() is not self.view:
            window.makeFirstResponder_(self.view)
        if select and self.order and not self.selected:
            self._select_index(0)

    def set_scope(self, scope: str) -> None:
        text = str(self.field.stringValue()) or query()
        search(text, scope, now=True)

    def ask_clicked(self) -> None:
        text = str(self.field.stringValue()).strip()
        if not text:
            self.hint_until = time.monotonic() + 3.0
            self._paint()
            self.focus_field()
            AppHelper.callLater(3.1, self._paint)
            return
        ask(text)
        self.count.setStringValue_("Asked Mint")

    def size_clicked(self) -> None:
        set_expanded(self.mode != "grid")

    # selection and keys
    def _chosen(self) -> list[str]:
        return [p for p in self.order if p in self.selected]

    def _targets(self, tile) -> list[str]:
        if tile is not None and tile.path not in self.selected:
            return [tile.path]
        return self._chosen() or ([tile.path] if tile is not None else [])

    def _select_index(self, i: int, extend: bool = False) -> None:
        if not self.order:
            return
        i = max(0, min(len(self.order) - 1, i))
        path = self.order[i]
        if extend and self.anchor in self.order:
            a = self.order.index(self.anchor)
            self.selected = set(self.order[min(a, i):max(a, i) + 1])
        else:
            self.selected = {path}
            self.anchor = path
        self.cursor = i
        self._paint_tiles()
        tile = self.tiles[path]
        self.document.scrollRectToVisible_(AppKit.NSInsetRect(tile.frame(), -4, -4))

    def click(self, tile, flags: int) -> None:
        path = tile.path
        i = self.order.index(path)
        if flags & AppKit.NSEventModifierFlagCommand:
            self.selected ^= {path}
            self.anchor, self.cursor = path, i
        elif flags & AppKit.NSEventModifierFlagShift and self.anchor in self.order:
            a = self.order.index(self.anchor)
            self.selected = set(self.order[min(a, i):max(a, i) + 1])
            self.cursor = i
        else:
            self.selected = {path}
            self.anchor, self.cursor = path, i
        self._paint_tiles()

    def select_none(self) -> None:
        if self.selected:
            self.selected = set()
            self._paint_tiles()

    def select_all(self) -> None:
        self.selected = set(self.order)
        self._paint_tiles()

    def key_move(self, code: int, flags: int) -> None:
        extend = bool(flags & AppKit.NSEventModifierFlagShift)
        if not self.order:
            return
        i = self.cursor if self.cursor is not None else -1
        cols = 1 if self.mode == "compact" else self.cols
        if code == 123:
            i -= 1
        elif code == 124:
            i += 1
        elif code == 125:
            i = i + (cols if self.mode != "compact" else 0) if i >= 0 else 0
        elif code == 126:
            if self.mode == "compact" or i < cols:
                self.focus_field()
                return
            i -= cols
        self._select_index(max(0, i), extend)

    def key_down(self, event) -> bool:
        code = event.keyCode()
        flags = int(event.modifierFlags())
        if flags & AppKit.NSEventModifierFlagCommand:
            return self.key_equivalent(event)
        if code in (123, 124, 125, 126):
            self.key_move(code, flags)
            return True
        if code in (36, 76):
            if self._chosen():
                open_paths(self._chosen())
            return True
        if code == 49:
            self.quick_look()
            return True
        if code in (53, 48):
            self.focus_field()
            return True
        chars = str(event.characters() or "")
        if chars and chars.isprintable() and not flags & AppKit.NSEventModifierFlagControl:
            self.field.setStringValue_(str(self.field.stringValue()) + chars)     # typing goes on searching
            self.focus_field()
            self.typed()
            return True
        return False

    def key_equivalent(self, event) -> bool:
        key = str(event.charactersIgnoringModifiers() or "").lower()
        chosen = self._chosen()
        if key == "a":
            self.select_all()
        elif key == "c" and chosen:
            notch_shelf.copy_to_pasteboard(chosen)
        elif key == "o" and chosen:
            open_paths(chosen)
        elif key == "r" and chosen:
            notch_shelf.reveal(chosen)
        elif key == "y":
            self.quick_look()
        elif key == "f":
            self.focus_field()
        else:
            return False
        return True

    def hover(self, tile, inside: bool) -> None:
        before = self.hovered
        if inside:
            self.hovered = tile
        elif self.hovered is tile:
            self.hovered = None
        if before is not self.hovered:
            if before is not None:
                self._where_line(before, False)
            if self.hovered is not None:
                self._where_line(self.hovered, True)
        self._paint_tiles()

    def _where_line(self, tile, on: bool) -> None:
        """Hovering a result shows where it is: its folder under the name, the full path in the grid's
        header and in the tooltip."""
        entry, sub = tile.entry, tile.parts["sub"]
        full = _where(entry["path"]) + "/" + os.path.basename(entry["path"].rstrip("/"))
        tall = bool(tile.shaped) and tile.shaped[2] == "v"     # side-by-side tiles already say where
        if on and not tall:
            self.hint.setLineBreakMode_(AppKit.NSLineBreakByTruncatingMiddle)
            self.hint.setStringValue_(full)
        elif on:
            if getattr(tile, "plain_sub", None) is None:
                tile.plain_sub = (sub.stringValue(), sub.lineBreakMode(), sub.maximumNumberOfLines())
            sub.setMaximumNumberOfLines_(1)
            sub.setLineBreakMode_(AppKit.NSLineBreakByTruncatingMiddle)
            sub.setStringValue_(entry.get("where") or _where(entry["path"]))
            sub.setTextColor_(_ns(INK, 0.78))
            self.hint.setLineBreakMode_(AppKit.NSLineBreakByTruncatingMiddle)
            self.hint.setStringValue_(full)
        else:
            plain = getattr(tile, "plain_sub", None)
            if plain is not None:
                sub.setStringValue_(plain[0])
                sub.setLineBreakMode_(plain[1])
                sub.setMaximumNumberOfLines_(plain[2])
                tile.plain_sub = None
            sub.setTextColor_(_ns(DIM))
            self.hint.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
            self.hint.setStringValue_(HOW_TO)

    def open_tile(self, tile) -> None:
        open_paths(self._targets(tile))

    def quick_look(self, refresh: bool = False) -> None:
        chosen = self._chosen()
        if not chosen and self.order and not refresh:
            self._select_index(0)
            chosen = self._chosen()
        quick_look(chosen, owner=self, refresh=refresh)

    def screen_rect(self, path: str):
        tile = self.tiles.get(path)
        window = self.view.window()
        if tile is None or window is None or not self._visible():
            return None
        holder = tile.parts["holder"]
        return window.convertRectToScreen_(holder.convertRect_toView_(holder.bounds(), None))

    # menus
    def _item(self, menu, title_text: str, key: str, symbol: str | None = None, enabled: bool = True):
        item = menu.addItemWithTitle_action_keyEquivalent_(title_text, "menu:", "")
        item.setTarget_(self.target)
        item.setRepresentedObject_(key)
        item.setEnabled_(enabled)
        if symbol:
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
            if image is not None:
                item.setImage_(image)
        return item

    def tile_menu(self, tile):
        if tile.path not in self.selected:
            self.selected = {tile.path}
            self.anchor, self.cursor = tile.path, self.order.index(tile.path)
            self._paint_tiles()
        self._menu_tile = tile
        chosen = self._targets(tile)
        many = len(chosen) > 1
        menu = AppKit.NSMenu.alloc().initWithTitle_("Search")
        menu.setAutoenablesItems_(False)
        self._item(menu, f"Open {len(chosen)} Items" if many else "Open", "open", "arrow.up.forward.app")
        if tile.entry["kind"] != "app":
            apps = _apps_for(tile.path)
            if apps:
                sub = AppKit.NSMenu.alloc().initWithTitle_("Open With")
                sub.setAutoenablesItems_(False)
                for i, (name, app_path, default) in enumerate(apps):
                    item = self._item(sub, f"{name} (default)" if default else name, "with:" + app_path)
                    icon = AppKit.NSWorkspace.sharedWorkspace().iconForFile_(app_path).copy()
                    icon.setSize_(AppKit.NSMakeSize(16, 16))
                    item.setImage_(icon)
                    if default and i == 0 and len(apps) > 1:
                        sub.addItem_(AppKit.NSMenuItem.separatorItem())
                holder = menu.addItemWithTitle_action_keyEquivalent_("Open With", None, "")
                holder.setSubmenu_(sub)
        self._item(menu, "Show in Finder", "reveal", "folder")
        self._item(menu, "Quick Look", "quicklook", "eye")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._item(menu, "Copy", "copy", "doc.on.doc")
        self._item(menu, "Copy Paths" if many else "Copy Path", "copypath", "link")
        self._item(menu, "Add to Shelf", "shelf", "tray.and.arrow.down")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._item(menu, "Share…", "share", "square.and.arrow.up")
        airdrop_item = self._item(menu, "AirDrop", "airdrop", "wifi", enabled=notch_shelf.can_airdrop(chosen))
        service = notch_shelf.airdrop_service()
        if service is not None and service.image() is not None:
            picture = service.image().copy()
            picture.setSize_(AppKit.NSMakeSize(16, 16))
            airdrop_item.setImage_(picture)
        return menu

    def menu_action(self, key: str) -> None:
        tile = getattr(self, "_menu_tile", None)
        chosen = self._targets(tile) if tile is not None else self._chosen()
        if not chosen:
            return
        if key == "open":
            open_paths(chosen)
        elif key.startswith("with:"):
            open_with(chosen, key[5:])
        elif key == "reveal":
            notch_shelf.reveal(chosen)
        elif key == "quicklook":
            quick_look(chosen, owner=self)
        elif key == "copy":
            notch_shelf.copy_to_pasteboard(chosen)
        elif key == "copypath":
            copy_paths_text(chosen)
        elif key == "shelf":
            notch_shelf.add_paths(chosen)
        elif key == "share":
            notch_shelf.share(chosen, tile or self.view)
        elif key == "airdrop":
            AppHelper.callAfter(notch_shelf.airdrop, chosen)

    # drag out
    def drag_out(self, tile, event) -> None:
        chosen = self._targets(tile)
        where = tile.convertPoint_fromView_(event.locationInWindow(), None)
        items_out = []
        for i, path in enumerate(chosen):
            if not os.path.exists(path):
                continue
            other = self.tiles.get(path)
            picture = notch_shelf._drag_image(other.image if other is not None else None,
                                              other.entry["name"] if other is not None else os.path.basename(path))
            size = picture.size()
            item = AppKit.NSDraggingItem.alloc().initWithPasteboardWriter_(AppKit.NSURL.fileURLWithPath_(path))
            item.setDraggingFrame_contents_(AppKit.NSMakeRect(where.x - size.width / 2 + i * 6,
                                                              where.y - size.height / 2 - i * 6,
                                                              size.width, size.height), picture)
            items_out.append(item)
        if not items_out:
            return
        _dragging[0] = True
        session = tile.beginDraggingSessionWithItems_event_source_(items_out, event, tile)
        session.setAnimatesToStartingPositionsOnCancelOrFail_(True)
        session.setDraggingFormation_(AppKit.NSDraggingFormationStack if len(items_out) > 1
                                      else AppKit.NSDraggingFormationNone)

    def drag_ended(self) -> None:
        _dragging[0] = False

    # upkeep
    def tick(self, visible: bool) -> None:
        if visible and not self.was_visible:
            self.update()
        self.was_visible = visible

    def update(self, *_ignored) -> None:
        """Repaint now (after being shown)."""
        self.version = -1
        self.reload()
        self._load_visible()

    def resize(self, width: float, height: float) -> None:
        self.w, self.h = float(width), float(height)
        for tile in self.tiles.values():
            tile.shaped = None
        self._layout()
        self._paint()


# --- keeping them fresh ---------------------------------------------------------------------------------

_views: list = []
_timer: list = []


def _tick() -> None:
    for item in list(_views):
        window = item.view.window()
        if window is None:
            if item.in_window:                   # taken out of its window for good: forget it
                _views.remove(item)
            continue
        item.in_window = True
        item.tick(item._visible())


def _attach() -> None:
    if _timer:
        return
    target = _NotchSearchTarget.alloc().initWithOwner_(None)
    timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.5, target, "tick:", None, True)
    AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, AppKit.NSRunLoopCommonModes)
    _timer.extend([target, timer])


def _owner(view_in):
    for item in _views:
        if item.view is view_in:
            return item
    return None


def view(width: float, height: float):
    """For the notch: (NSView, update). Main thread. Below 250 tall it is the compact row (made for
    616 x 150), above it the grid (made for 736 x 430). Every view shows the same results."""
    _attach()
    item = SearchView(width, height)
    _views.append(item)
    return item.view, item.update


def resize(view_in, width: float, height: float) -> None:
    """Re-lay out a view from view() at a new size (compact <-> grid), keeping its field and selection."""
    item = _owner(view_in)
    if item is not None:
        item.resize(width, height)


def focus_field(view_in) -> bool:
    """Put the keyboard in the view's search field. Needs the view's window to be able to become key."""
    item = _owner(view_in)
    return bool(item is not None and item.focus_field())
