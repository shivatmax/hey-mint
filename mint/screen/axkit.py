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

import logging
import time
from pathlib import Path

import AppKit
import ApplicationServices as AX

log = logging.getLogger("mint.screen.axkit")

_unlocked: dict[int, float] = {}

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


def unlock(app=None, wait: float = 0.6) -> bool:
    """Make a Chromium-based app expose its full accessibility tree.

    Cheap to call repeatedly: once per process, then a no-op. Returns True if
    the switch was newly thrown (the caller may want to give the app a moment).
    """
    if app is None:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return False
    pid = app.processIdentifier()
    if pid in _unlocked or not is_chromium_app(app):
        return False
    element = AX.AXUIElementCreateApplication(pid)
    err = AX.AXUIElementSetAttributeValue(element, "AXManualAccessibility", True)
    _unlocked[pid] = time.monotonic()
    log.info("AXManualAccessibility on %s (%s): %s", app.localizedName(), pid, err)
    if err == 0 and wait:
        time.sleep(wait)          # the tree is built asynchronously
    return err == 0


def focused_window(pid: int):
    app = AX.AXUIElementCreateApplication(pid)
    return attr(app, "AXFocusedWindow") or next(iter(attr(app, "AXWindows") or []), None)


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
    queue, seen = [(element, 0)], 0
    while queue and seen < limit:
        node, level = queue.pop(0)
        seen += 1
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
