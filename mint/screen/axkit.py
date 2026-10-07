"""Accessibility helpers shared by typing, clicking and the desktop tool.

The most important thing here is `unlock`. Electron and other Chromium-based
apps - Slack, ChatGPT/Codex, ZCode, VS Code, Claude, Discord, Notion - build
their accessibility tree only when they believe an assistive technology is
running. Until then they expose a few dozen controls: in testing ZCode showed
31, and Slack had no channel list at all, so the desktop tool had nothing to
choose from and Mint told the user it could not do the task. Setting the
app's AXManualAccessibility attribute is Chromium's documented switch for
this; once set, the full tree appears for every Accessibility client,
Desktop Voice's included.
"""

from __future__ import annotations

import collections
import ctypes
import logging
import struct
import threading
import time
from pathlib import Path

import AppKit
import ApplicationServices as AX

log = logging.getLogger("mint.screen.axkit")

_unlocked: dict[int, dict] = {}       # pid -> {stamp, done, attempts, at}

# Browsers built on Chromium take AXEnhancedUserInterface instead; it is not
# set on them - it slows window animation and was not needed for web pages.
_BROWSERS = {"com.google.Chrome", "com.brave.Browser", "com.microsoft.edgemac", "company.thebrowser.Browser"}


def attr(element, name: str):
    try:
        err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
        return value if err == 0 else None
    except Exception:
        return None


def frame(element):
    """(x, y, w, h) in Quartz screen points, or None."""
    pos, size = attr(element, "AXPosition"), attr(element, "AXSize")
    if pos is None or size is None:
        return None
    try:
        p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)[1]
        s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)[1]
        return (p.x, p.y, s.width, s.height)
    except Exception:
        return None


def is_chromium_app(app) -> bool:
    """Electron/Chromium app (not a browser): needs `unlock` to be usable."""
    if app is None or app.bundleIdentifier() in _BROWSERS:
        return False
    url = app.bundleURL()
    if url is None:
        return False
    root = Path(url.path()) / "Contents"
    frameworks = root / "Frameworks"
    try:
        names = {p.name for p in frameworks.iterdir()} if frameworks.exists() else set()
    except OSError:
        names = set()
    return ("Electron Framework.framework" in names
            or (root / "Resources" / "app.asar").exists()
            or any("Chromium" in n or "CEF" in n for n in names))


# --- turning the tree on, once per process lifetime ----------------------------------------------
#
# The tree is built asynchronously after the switch: a fixed 0.6 s wait was sometimes too short
# (VS Code listed 4 controls, then its search box 4 s later) and always paid in full. So poll for the
# web content (an AXWebArea, or a tree too big to be the bare window frame), re-assert the switch
# halfway, and settle briefly once it is there (cua-driver's enablement.rs). The cache is keyed by
# the process's start time too: pids are reused, and a relaunched app must be switched on again.

_AX_UNSUPPORTED = -25205              # kAXErrorAttributeUnsupported
MATERIALIZE_TIMEOUT = 4.0
MATERIALIZE_POLL = 0.1
SETTLE = 0.5
RETRY_AFTER = 30.0                    # a tree that never came: try again after this long...
MAX_ATTEMPTS = 3                      # ...at most this many times per process lifetime
_PROBE_NODES, _PROBE_DEPTH = 400, 10
_state_lock = threading.Lock()
_pid_locks: dict[int, object] = {}


def process_start(pid: int):
    """The kernel's start time of `pid` ((sec, usec)), or None when it can't be read."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        buf = ctypes.create_string_buffer(136)            # struct proc_bsdinfo
        filled = libc.proc_pidinfo(int(pid), 3, ctypes.c_uint64(0), buf, 136)   # PROC_PIDTBSDINFO
        if filled != 136:
            return None
        sec, usec = struct.unpack_from("<QQ", buf.raw, 120)
        return (sec, usec) if sec else None
    except Exception:
        return None


def _stamp(app, pid: int):
    stamp = process_start(pid)
    if stamp is None and app is not None:
        try:
            date = app.launchDate()
            stamp = ("launch", round(date.timeIntervalSince1970(), 3)) if date is not None else None
        except Exception:
            stamp = None
    return stamp


def _next_attempt(cached: dict | None, stamp, now: float):
    """None = skip (done, or waiting out a backoff), else how many earlier tries timed out."""
    if cached is None or stamp is None or cached["stamp"] != stamp:
        return 0
    if cached["done"]:
        return None
    if cached["attempts"] < MAX_ATTEMPTS and now - cached["at"] >= RETRY_AFTER:
        return cached["attempts"]
    return None


def _switch_on(element) -> str:
    """'manual', 'enhanced' (the older switch some Electron builds take instead) or ''."""
    try:
        err = AX.AXUIElementSetAttributeValue(element, "AXManualAccessibility", True)
    except Exception:
        return ""
    if err == 0:
        return "manual"
    if err != _AX_UNSUPPORTED:          # busy or timed out: not a "no such switch", don't claim it
        return ""
    try:
        return "enhanced" if AX.AXUIElementSetAttributeValue(element, "AXEnhancedUserInterface", True) == 0 else ""
    except Exception:
        return ""


def has_web_content(element, nodes: int = _PROBE_NODES, depth: int = _PROBE_DEPTH) -> bool:
    """An AXWebArea in the app's windows within a bounded look, or windows too big to be just their
    frames. (Windows only: the native menu bar alone can run to hundreds of items.)"""
    queue, seen = collections.deque((w, 0) for w in attr(element, "AXWindows") or []), 0
    while queue:
        node, level = queue.popleft()
        if seen >= nodes:
            return True
        seen += 1
        if attr(node, "AXRole") == "AXWebArea":
            return True
        if level < depth:
            queue.extend((c, level + 1) for c in children(node))
    return False


def _await_tree(probe, reassert, sleep) -> bool:
    steps = round(MATERIALIZE_TIMEOUT / MATERIALIZE_POLL)
    for step in range(steps + 1):
        if probe():
            reassert()
            sleep(SETTLE)
            return True
        if step == steps // 2:
            reassert()
        if step < steps:
            sleep(MATERIALIZE_POLL)
    return False


def unlock(app=None, wait: float = 0.6) -> bool:
    """Make a Chromium-based app expose its full accessibility tree.

    Cheap to call repeatedly: once per process lifetime, then a no-op. With `wait` (any non-zero
    value; kept for older callers) it returns once the tree is there - polled, up to 4 s - instead
    of sleeping a fixed time. Returns True if the switch was newly thrown.
    """
    if app is None:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return False
    pid = int(app.processIdentifier())
    if not is_chromium_app(app):
        return False
    with _state_lock:
        pid_lock = _pid_locks.setdefault(pid, threading.Lock())
    with pid_lock:                     # a second caller waits for the first one's poll, then skips
        stamp = _stamp(app, pid)
        now = time.monotonic()
        tries = _next_attempt(_unlocked.get(pid), stamp, now)
        if tries is None:
            return False
        element = AX.AXUIElementCreateApplication(pid)
        bound(element)
        how = _switch_on(element)
        if not how:
            log.debug("accessibility switch refused by %s (%s)", app.localizedName(), pid)
            return False
        log.info("accessibility switch on %s (%s): %s", app.localizedName(), pid, how)
        done = True
        if how == "manual" and wait:
            done = _await_tree(lambda: has_web_content(element), lambda: _switch_on(element), time.sleep)
            if not done:
                log.info("no web content from %s after %.0f s", app.localizedName(), MATERIALIZE_TIMEOUT)
        if stamp is not None:
            _unlocked[pid] = {"stamp": stamp, "done": done, "attempts": tries + (0 if done else 1), "at": now}
        return True


# --- bounded reads, the app's windows, and their window-server ids ----------------------------

AX_TIMEOUT = 2.0      # seconds per AX message: a hung app must not freeze Mint's walk


def bound(element, seconds: float = AX_TIMEOUT):
    """Give `element` its own messaging timeout (they are per element, not inherited)."""
    try:
        AX.AXUIElementSetMessagingTimeout(element, seconds)
    except Exception:
        pass
    return element


def children(element) -> list:
    return list(attr(element, "AXChildren") or [])


def top_level(pid: int) -> list:
    """The app's AXChildren plus AXWindows: a background app's children leave out its windows."""
    app = bound(AX.AXUIElementCreateApplication(pid))
    items = children(app)
    for window in attr(app, "AXWindows") or []:
        if not any(window == item for item in items):      # CFEqual: same element, other proxy
            items.append(window)
    return items


_get_window = None


def window_id(element) -> int | None:
    """The CGWindowID of an AX window (HIServices' private _AXUIElementGetWindow), or None."""
    global _get_window
    if element is None:
        return None
    try:
        if _get_window is None:
            lib = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            fn = getattr(lib, "_AXUIElementGetWindow", None)
            if fn is None:
                _get_window = False
                return None
            fn.restype, fn.argtypes = ctypes.c_int32, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            _get_window = fn
        if _get_window is False:
            return None
        number = ctypes.c_uint32(0)
        err = _get_window(element.__c_void_p__(), ctypes.byref(number))
        return int(number.value) if err == 0 and number.value else None
    except Exception:
        return None


def window_by_id(pid: int, wid: int, probe: bool = True):
    """The AX window of app `pid` whose window-server id is `wid` - or None, never a sibling window.
    A window the window server shows for `pid` but AXWindows leaves out (an app that was never
    active, a window on another Space) is looked for by its element token (see _token_window)."""
    for item in top_level(pid):
        if attr(item, "AXRole") == "AXWindow" and window_id(item) == int(wid):
            return item
    if probe and server_owner(wid) == int(pid):
        return _token_window(int(pid), int(wid))
    return None


def server_owner(wid: int) -> int | None:
    """The pid the window server says owns window `wid`, or None."""
    try:
        import Quartz
        rows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, int(wid)) or []
    except Exception:
        return None
    if not rows:
        return None
    try:
        return int(rows[0].get("kCGWindowOwnerPID", 0)) or None
    except Exception:
        return None


def server_windows(pid: int) -> list[int]:
    """`pid`'s ordinary on-screen windows (layer 0, not tiny), front to back, by window-server id."""
    try:
        import Quartz
        rows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
    except Exception:
        return []
    out = []
    for r in rows:
        b = r.get("kCGWindowBounds") or {}
        if int(r.get("kCGWindowOwnerPID", -1)) == int(pid) and r.get("kCGWindowLayer", 0) == 0 \
                and b.get("Width", 0) > 40 and b.get("Height", 0) > 40:
            out.append(int(r["kCGWindowNumber"]))
    return out


# An app's AX elements can be made from a 20-byte token (pid, 'coco', element number) - how
# cua-driver (MIT, ax/bindings.rs) reaches windows that AXWindows leaves out. Only an element that
# is an AXWindow AND has exactly the asked-for window id is returned, so it is as strong a match as
# AXWindows; the look is bounded (0.3 s, 2000 numbers, 0.05 s per element).
TOKEN_PROBE_SECONDS, TOKEN_PROBE_IDS = 0.3, 2000
_token_fns = None


def _token_window(pid: int, wid: int):
    global _token_fns
    try:
        if _token_fns is None:
            ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            make = getattr(ax, "_AXUIElementCreateWithRemoteToken", None)
            if make is None:
                _token_fns = False
                return None
            make.restype, make.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
            cf.CFDataCreate.restype = ctypes.c_void_p
            cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long]
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            _token_fns = (make, cf)
        if _token_fns is False:
            return None
        import objc
        make, cf = _token_fns
        deadline = time.monotonic() + TOKEN_PROBE_SECONDS
        for number in range(TOKEN_PROBE_IDS):
            if time.monotonic() > deadline:
                return None
            token = struct.pack("=i4xiQ", int(pid), 0x636F636F, number)
            data = cf.CFDataCreate(None, token, len(token))
            if not data:
                continue
            pointer = make(data)
            cf.CFRelease(data)
            if not pointer:
                continue
            element = objc.objc_object(c_void_p=pointer)     # retains; drop the create's own count
            cf.CFRelease(pointer)
            bound(element, 0.05)
            if attr(element, "AXRole") == "AXWindow" and window_id(element) == int(wid):
                return bound(element)
    except Exception:
        log.debug("token window probe", exc_info=True)
    return None


def focused_window(pid: int):
    """The window to read for app `pid`: its focused window, else its main window, else its first
    window (AXWindows is front to back) - an app opened in the background and never made active has
    neither of the first two - else its front-most on-screen window from the window server, found
    by id. Only `pid`'s own windows, never another app's."""
    import os
    app = bound(AX.AXUIElementCreateApplication(pid))
    if int(pid) == os.getpid():            # Mint itself is answered in process: ask no more than before
        return attr(app, "AXFocusedWindow") or next(iter(attr(app, "AXWindows") or []), None)
    for name in ("AXFocusedWindow", "AXMainWindow"):
        window = attr(app, name)
        if window is not None:
            return window
    windows = list(attr(app, "AXWindows") or [])
    for item in children(app):
        if not any(item == w for w in windows):
            windows.append(item)
    windows = [w for w in windows if attr(w, "AXRole") == "AXWindow"]
    shown = [w for w in windows if attr(w, "AXMinimized") is not True]
    if shown:
        return shown[0]
    for wid in server_windows(pid)[:3]:
        window = window_by_id(pid, wid)
        if window is not None:
            return window
    return windows[0] if windows else None


# --- asking about a screen point without asking Mint itself ------------------------------------
#
# AXUIElementCopyElementAtPosition on the system-wide element is answered by whichever app owns the
# window under the point. When that app is Mint, HIServices does not send a message: it calls
# AppKit's accessibility code IN PROCESS, on the calling thread. From teach's worker thread that
# raced the main thread inside AppKit and Mint died with SIGSEGV (1 Oct 21:09, crash report
# Python-2026-10-01-211001: teach-worker in _AXUIElementCopyElementAtPositionIncludeIgnored ->
# AppKit CopyElementAtPosition, main thread in objc_msgSend from the same AppKit routine). The same
# goes for the system-wide AXFocusedUIElement when Mint has the keyboard. So: never ask when the
# point (or the keyboard) belongs to Mint.

def _main_height() -> float:
    import Quartz
    return float(Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.height)


def own_window_at(x: float, y: float) -> bool:
    """Would a click at this Quartz point (top-left origin) land in one of Mint's own windows?
    Mint's click-through overlays (effects, glow, the folded notch) don't count: the window
    server skips windows that ignore the mouse, exactly as a real click does."""
    import os

    import Quartz
    me = os.getpid()
    try:
        number = AppKit.NSWindow.windowNumberAtPoint_belowWindowWithWindowNumber_(
            AppKit.NSMakePoint(float(x), _main_height() - float(y)), 0)
        if not number:
            return False
        info = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, number) or []
        return any(int(w.get("kCGWindowOwnerPID", 0)) == me for w in info)
    except Exception:
        log.debug("own_window_at", exc_info=True)
    # Could not ask the window server: any small Mint window (not a full-screen overlay) at the
    # point counts, which errs on the side of not asking.
    try:
        screen = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size
        for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                                   Quartz.kCGNullWindowID) or []:
            if int(w.get("kCGWindowOwnerPID", 0)) != me:
                continue
            b = w.get("kCGWindowBounds") or {}
            if b.get("Width", 0) >= screen.width and b.get("Height", 0) >= screen.height - 2:
                continue
            if b and b["X"] <= x <= b["X"] + b["Width"] and b["Y"] <= y <= b["Y"] + b["Height"]:
                return True
    except Exception:
        return True
    return False


def element_at(x: float, y: float):
    """The accessibility element at a Quartz screen point, or None - and None, without asking,
    when the point is on one of Mint's own windows (see above). Safe from any thread."""
    if own_window_at(x, y):
        return None
    try:
        err, element = AX.AXUIElementCopyElementAtPosition(AX.AXUIElementCreateSystemWide(),
                                                           float(x), float(y), None)
    except Exception:
        return None
    return element if err == 0 else None


def focused_element(pid: int | None = None):
    """The focused control of app `pid` (default: the frontmost app) - never Mint's own, which
    could only be read in process. Safe from any thread."""
    import os
    if not pid:
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        pid = int(front.processIdentifier()) if front is not None else 0
    if not pid or pid == os.getpid():
        return None
    return attr(AX.AXUIElementCreateApplication(pid), "AXFocusedUIElement")


# Where Mint itself last clicked (click_text, click_at, ui_act): typing right after a click into a
# box that Accessibility can't see (Electron/Monaco inputs are 1x1 hidden textareas) goes there.
_last_click: dict = {}


def note_click(pid: int | None, x: float, y: float, label: str = "") -> None:
    _last_click.update(pid=int(pid or 0), x=float(x), y=float(y), label=label, at=time.monotonic())


def recent_click(pid: int | None, seconds: float = 30.0) -> dict | None:
    """Mint's last click, if it was in app `pid` within `seconds`."""
    if not _last_click or not pid or _last_click.get("pid") != int(pid):
        return None
    return dict(_last_click) if time.monotonic() - _last_click["at"] <= seconds else None


def walk(element, limit: int = 4000, depth: int = 60):
    """Every element under `element`, breadth first, up to `limit`."""
    queue, seen = collections.deque([(element, 0)]), 0
    while queue and seen < limit:
        node, level = queue.popleft()
        seen += 1
        bound(node)
        yield node
        if level >= depth:
            continue
        for child in attr(node, "AXChildren") or []:
            queue.append((child, level + 1))


_INPUT_ROLES = {"AXTextArea", "AXTextField", "AXComboBox", "AXSearchField"}


def is_editable(element) -> bool:
    """Takes typing: a text input of any size (Electron's are 1x1 hidden textareas) or anything
    inside an editable web area."""
    if element is None:
        return False
    return attr(element, "AXRole") in _INPUT_ROLES or attr(element, "AXEditableAncestor") is not None


def text_inputs(window) -> list[dict]:
    """Editable text fields in a window, described for choosing between them."""
    found = []
    for node in walk(window):
        role = attr(node, "AXRole")
        subrole = attr(node, "AXSubrole") or ""
        # Chromium exposes contenteditable composers (ProseMirror, Lexical) as
        # AXTextArea; a plain-group heuristic caught whole pages in testing.
        editable = role in _INPUT_ROLES
        if not editable:
            continue
        box = frame(node)
        if box is None or box[2] < 20 or box[3] < 8:
            continue
        if attr(node, "AXEnabled") is False:
            continue
        label = " ".join(str(x) for x in (
            attr(node, "AXPlaceholderValue"), attr(node, "AXTitle"),
            attr(node, "AXDescription"), attr(node, "AXHelp")) if x)
        value = attr(node, "AXValue")
        found.append({"element": node, "role": role, "subrole": subrole, "label": label[:80],
                      "value": (str(value)[:60] if isinstance(value, str) else ""),
                      "frame": box, "focused": bool(attr(node, "AXFocused"))})
    return found


def describe_input(item: dict, window_frame=None) -> str:
    x, y, w, h = item["frame"]
    where = ""
    if window_frame:
        wx, wy, ww, wh = window_frame
        vertical = "top" if y + h / 2 < wy + wh / 3 else ("bottom" if y + h / 2 > wy + 2 * wh / 3 else "middle")
        horizontal = "left" if x + w / 2 < wx + ww / 3 else ("right" if x + w / 2 > wx + 2 * ww / 3 else "centre")
        where = f", {vertical} {horizontal}"
    kind = {"AXSearchField": "search field", "AXTextArea": "text area", "AXComboBox": "combo box",
            "AXGroup": "rich text editor"}.get(item["role"], "text field")
    label = f" '{item['label']}'" if item["label"] else ""
    value = f", contains '{item['value']}'" if item["value"] else ""
    return f"{kind}{label} ({int(w)}x{int(h)}{where}{value})"


def focus(element) -> bool:
    err = AX.AXUIElementSetAttributeValue(element, "AXFocused", True)
    return err == 0


def dump(name: str) -> str:
    """Diagnostics for `--ax NAME`: control counts before and after unlock."""
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    app = next((a for a in workspace.runningApplications()
                if (a.localizedName() or "").lower() == name.lower()), None)
    if app is None:
        return f"{name} is not running."
    pid = app.processIdentifier()

    def census():
        window = focused_window(pid)
        if window is None:
            return 0, {}, None
        counts: dict[str, int] = {}
        total = 0
        for node in walk(window):
            total += 1
            role = attr(node, "AXRole") or "?"
            counts[role] = counts.get(role, 0) + 1
        return total, counts, window

    before, _, _ = census()
    unlocked = unlock(app, wait=1.5)
    after, counts, window = census()
    lines = [f"{name} (pid {pid}) chromium={is_chromium_app(app)} unlocked={unlocked}",
             f"elements before={before} after={after}",
             "roles: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:14])]
    if window is not None:
        wf = frame(window)
        for item in text_inputs(window)[:12]:
            lines.append("input: " + describe_input(item, wf) + (" [focused]" if item["focused"] else ""))
        buttons = []
        for node in walk(window):
            if attr(node, "AXRole") in ("AXButton", "AXLink", "AXMenuButton", "AXRadioButton"):
                label = attr(node, "AXTitle") or attr(node, "AXDescription") or ""
                if label:
                    buttons.append(str(label)[:40])
        lines.append(f"buttons/links ({len(buttons)}): " + " | ".join(buttons[:70]))
    return "\n".join(lines)


def bring_to_front(app, timeout: float = 2.5) -> bool:
    """Make `app` the frontmost app, and check that it really is.

    `open -a` from a background process launches or unhides the app but macOS's
    cooperative activation can leave the previous app in front: in testing,
    ChatGPT opened behind the Claude app and the text meant for ChatGPT was
    typed into Claude. So ask politely, then through Accessibility, and verify.
    """
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    pid = app.processIdentifier()

    def front() -> bool:
        current = workspace.frontmostApplication()
        return current is not None and current.processIdentifier() == pid

    if front():
        return True
    app.unhide()
    app.activateWithOptions_(AppKit.NSApplicationActivateAllWindows)
    deadline = time.monotonic() + timeout
    tried_ax = False
    while time.monotonic() < deadline:
        time.sleep(0.12)
        if front():
            return True
        if not tried_ax and time.monotonic() > deadline - timeout + 0.35:
            tried_ax = True
            element = AX.AXUIElementCreateApplication(pid)
            AX.AXUIElementSetAttributeValue(element, "AXFrontmost", True)
            window = focused_window(pid)
            if window is not None:
                AX.AXUIElementPerformAction(window, "AXRaise")
    return front()


def windows_front_to_back(pid: int) -> list:
    """The app's windows, popups and dialogs first (AXWindows is front to back)."""
    app = AX.AXUIElementCreateApplication(pid)
    wins = list(attr(app, "AXWindows") or [])
    focused = attr(app, "AXFocusedWindow")
    if focused is not None and focused not in wins:
        wins.append(focused)
    return wins
