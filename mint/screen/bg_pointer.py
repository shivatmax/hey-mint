"""Mint's own pointer: clicks, drags and wheel turns posted straight to one app's window.

The window server hands these events to that one process (SkyLight's SLEventPostToPid - the route
refs/cua/libs/cua-driver uses on macOS), stamped with the exact window they are for, so the user's
pointer never moves and the window is not raised. For apps that show nothing to Accessibility
(Telegram for Mac, canvases, some Qt and Java apps) this is the only way to click without borrowing
the user's mouse.

Every check fails closed (cua's rules): no SkyLight, a window that is gone, changed owner, no longer
holds the point, was minimised or went to another Space -> nothing is sent and the caller uses the
real pointer instead, and says so. This module never posts to the global HID stream itself.

A click that changes nothing in the window is reported as not landed, so the caller can try the real
pointer; when the real pointer then does change it, that app is remembered as one Mint's pointer
does not reach (for this run).
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass

import Quartz

BACKGROUND = "background_pointer"      # effect.ROUTES: the click went by Mint's own pointer
REAL = "global_input"                  # ... by the user's real pointer

_SKYLIGHT = "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight"

# Raw CGEvent fields (the ones cua-driver stamps; numbers, as several have no public name).
F_NUMBER = 0            # kCGMouseEventNumber - used as a gesture-phase marker
F_CLICK_STATE = 1       # kCGMouseEventClickState
F_BUTTON = 3            # kCGMouseEventButtonNumber: 0 left, 1 right
F_SUBTYPE = 7           # kCGMouseEventSubtype: 3 = NSEventSubtypeTouch for clicks
F_TARGET_PID = 40       # kCGEventTargetUnixProcessID (Chromium drops synthetic events without it)
F_WINDOW = 51           # the window number, as an NSEvent carries it
F_GROUP = 58            # click group: one id for every event of one gesture
F_UNDER = 91            # kCGMouseEventWindowUnderMousePointer
F_UNDER_HANDLER = 92    # kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent

LOOK_FOR = 0.6          # seconds to wait for the window to show the click
# Taking the keyboard focus without raising (cua's recipe for Chromium) is off: with the window-local point a window
# behind takes the events anyway (tried 9 Oct, macOS 27, an inactive app's non-key window), and while it is on, the
# user's own window loses the keyboard for a moment. A window that ignores Mint's pointer gets the real one instead.
FOCUS_BEHIND = False
BEHIND_LOOK_FOR = 0.35  # ... when the app is behind: its window holds the keyboard until then
_lock = threading.Lock()


# --- SkyLight, loaded once -----------------------------------------------------------------------

class _Spi:
    """The private functions, resolved once; any of them may be None on another macOS."""

    def __init__(self) -> None:
        self.post = self.window_location = self.set_field = None
        self.connection = self.window_owner = self.connection_psn = None
        self.post_record = self.front_process = None
        try:
            lib = ctypes.CDLL(_SKYLIGHT, mode=ctypes.RTLD_GLOBAL)
        except OSError:
            return

        def sym(name, restype, *argtypes):
            fn = getattr(lib, name, None)
            if fn is not None:
                fn.restype, fn.argtypes = restype, list(argtypes)
            return fn

        vp, u32 = ctypes.c_void_p, ctypes.c_uint32
        self.post = sym("SLEventPostToPid", None, ctypes.c_int32, vp)
        self.window_location = sym("CGEventSetWindowLocation", None, vp, ctypes.c_double, ctypes.c_double)
        self.set_field = sym("SLEventSetIntegerValueField", None, vp, u32, ctypes.c_int64)
        self.connection = sym("CGSMainConnectionID", u32)
        self.window_owner = sym("SLSGetWindowOwner", ctypes.c_int32, u32, u32, ctypes.POINTER(u32))
        self.connection_psn = sym("SLSGetConnectionPSN", ctypes.c_int32, u32, vp)
        self.post_record = sym("SLPSPostEventRecordTo", ctypes.c_int32, vp, vp)
        self.front_process = sym("_SLPSGetFrontProcess", ctypes.c_int32, vp)

    @property
    def can_post(self) -> bool:
        return self.post is not None and self.window_location is not None

    @property
    def can_focus(self) -> bool:
        return None not in (self.connection, self.window_owner, self.connection_psn, self.post_record,
                            self.front_process)


_spi: _Spi | None = None


def _sl() -> _Spi:
    global _spi
    if _spi is None:
        _spi = _Spi()
    return _spi


# What this run has learned: Mint's pointer is off everywhere (it moved the real one), or for some apps.
_off = {"why": ""}
_no_reach: dict[int, str] = {}          # pid -> why Mint's pointer does not reach that app
_reached: set[int] = set()              # apps where a gesture of Mint's pointer changed the window


def available() -> bool:
    return not _off["why"] and os.environ.get("MINT_REAL_POINTER", "") != "1" and _sl().can_post


def why_off() -> str:
    if os.environ.get("MINT_REAL_POINTER", "") == "1":
        return "MINT_REAL_POINTER=1 is set"
    if _off["why"]:
        return _off["why"]
    return "" if _sl().can_post else "this macOS has no SkyLight event posting"


def mark_no_reach(pid: int | None, why: str = "its window did not change, the real pointer's click did") -> None:
    if pid:
        _no_reach[int(pid)] = why


def reaches(pid: int | None) -> bool:
    return bool(pid) and int(pid) not in _no_reach


def proven(pid: int | None) -> bool:
    """Mint's pointer has changed this app's window before: when a wheel turn now moves nothing, the pane is at its
    end - the real wheel would move nothing either, and would only take the user's pointer there and back."""
    return bool(pid) and int(pid) in _reached and reaches(pid)


# --- the window a point belongs to ------------------------------------------------------------------

@dataclass
class Target:
    pid: int
    window_id: int
    bounds: tuple[float, float, float, float]       # x, y, w, h in screen points
    app: str = ""

    def holds(self, x: float, y: float) -> bool:
        bx, by, bw, bh = self.bounds
        return bx <= x <= bx + bw and by <= y <= by + bh


def _click_through() -> set[int]:
    try:
        from mint.screen.ocr import _click_through
        return _click_through()
    except Exception:
        return set()


def _windows() -> list[dict]:
    return list(Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or [])


def _target_of(w: dict) -> Target:
    b = w.get("kCGWindowBounds") or {}
    return Target(int(w.get("kCGWindowOwnerPID", 0)), int(w.get("kCGWindowNumber", 0)),
                  (float(b.get("X", 0)), float(b.get("Y", 0)), float(b.get("Width", 0)), float(b.get("Height", 0))),
                  str(w.get("kCGWindowOwnerName", "") or ""))


def target_at(x: float, y: float, pid: int | None = None, windows: list[dict] | None = None) -> Target | None:
    """The ordinary window (layer 0, on this Space, not minimised) on top at (x, y) - what the real pointer
    would click - skipping Mint's click-through overlays. With `pid`, that app's top window holding the
    point instead, even under another app's window. None when there is no such window, or it is Mint's."""
    through = _click_through()
    for w in windows if windows is not None else _windows():
        if w.get("kCGWindowLayer", 0) != 0 or float(w.get("kCGWindowAlpha", 1) or 0) <= 0:
            continue
        if int(w.get("kCGWindowNumber", 0)) in through:
            continue
        found = _target_of(w)
        if not found.holds(x, y) or found.bounds[2] < 2 or found.bounds[3] < 2:
            continue
        if pid is not None and found.pid != int(pid):
            continue
        return None if found.pid == os.getpid() else found
    return None


def still(target: Target, x: float, y: float) -> str:
    """'' when the window is still `target`'s, on screen and holding the point - checked right before
    sending; else why not."""
    rows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, target.window_id) or []
    row = next((w for w in rows if int(w.get("kCGWindowNumber", 0)) == target.window_id), None)
    if row is None:
        return "the window is gone"
    now = _target_of(row)
    if now.pid != target.pid:
        return "the window changed owner"
    if not row.get("kCGWindowIsOnscreen"):
        return "the window is minimised, hidden or on another Space"
    if not now.holds(x, y):
        return "the window no longer holds that point"
    target.bounds = now.bounds
    return ""


# --- looking at the window -----------------------------------------------------------------------

def look(target: Target | None):
    """A small grey picture of the window alone (other windows over it left out), or None when it can't be
    taken (no Screen Recording permission)."""
    if target is None:
        return None
    try:
        from PIL import Image
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, target.window_id,
            Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution)
        if image is None:
            return None
        w, h = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
        if not w or not h or Quartz.CGImageGetBitsPerPixel(image) != 32:
            return None
        data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
        pic = Image.frombuffer("RGBA", (w, h), bytes(data), "raw", "BGRA", Quartz.CGImageGetBytesPerRow(image), 1)
        if pic.getchannel("A").getbbox() is None:
            return None             # a blank picture: what a window we may not record looks like
        return pic.convert("L").resize((max(1, w // 3), max(1, h // 3)))
    except Exception:
        return None


def changed(before, after, at_least: int = 6) -> bool | None:
    """Whether the window's picture changed (a caret blink is a few pixels; a highlight, focus ring or new
    view is more). None when either picture is missing."""
    if before is None or after is None:
        return None
    if before.size != after.size:
        return True
    from PIL import ImageChops
    diff = ImageChops.difference(before, after).point(lambda v: 255 if v > 24 else 0)
    return diff.histogram()[255] >= at_least


def wait_for_change(target: Target, before, seconds: float = LOOK_FOR) -> bool | None:
    deadline = time.monotonic() + seconds
    result = None
    while time.monotonic() < deadline:
        time.sleep(0.12)
        result = changed(before, look(target))
        if result:
            return True
    return result


# --- keyboard focus without raising -----------------------------------------------------------------

def _psn_of(window_id: int):
    spi = _sl()
    owner = ctypes.c_uint32(0)
    if spi.window_owner(spi.connection(), int(window_id), ctypes.byref(owner)) != 0 or not owner.value:
        return None
    psn = ctypes.create_string_buffer(8)
    return psn if spi.connection_psn(owner.value, psn) == 0 else None


def _record(window_id: int, focus: bool):
    buf = (ctypes.c_uint8 * 0xF8)()
    buf[0x04], buf[0x08] = 0xF8, 0x0D
    for i, byte in enumerate(int(window_id).to_bytes(4, "little")):
        buf[0x3C + i] = byte
    buf[0x8A] = 0x01 if focus else 0x02
    return buf


def focus_without_raise(target: Target) -> object | None:
    """Make the target's window take events as the active one, without raising it or moving Spaces (the
    focus/defocus event records yabai and cua-driver use). -> the front process's PSN to give focus back
    to, or None when it could not be done."""
    spi = _sl()
    if not spi.can_focus:
        return None
    front = ctypes.create_string_buffer(8)
    if spi.front_process(front) != 0:
        return None
    psn = _psn_of(target.window_id)
    if psn is None:
        return None
    spi.post_record(front, _record(target.window_id, False))
    spi.post_record(psn, _record(target.window_id, True))
    time.sleep(0.05)              # let AppKit update its key-window routing first
    return front


def give_focus_back(target: Target, front_pid: int | None) -> None:
    """Undo focus_without_raise: the target's window lets go, the front app's key window takes the keyboard
    again (else the user's window would stay deaf until they click it)."""
    spi = _sl()
    if not spi.can_focus or not front_pid:
        return
    try:
        from mint.screen import axkit
        wid = axkit.window_id(axkit.focused_window(int(front_pid)))
    except Exception:
        wid = None
    if not wid:
        return
    psn, back = _psn_of(target.window_id), _psn_of(wid)
    if psn is not None:
        spi.post_record(psn, _record(target.window_id, False))
    if back is not None:
        spi.post_record(back, _record(wid, True))


# --- sending ---------------------------------------------------------------------------------------

def _ptr(event) -> ctypes.c_void_p:
    return event.__c_void_p__()


def _stamp(event, target: Target, x: float, y: float, fields: dict[int, int]) -> None:
    spi, ptr = _sl(), _ptr(event)
    every = {F_TARGET_PID: target.pid, F_WINDOW: target.window_id, F_UNDER: target.window_id,
             F_UNDER_HANDLER: target.window_id, **fields}
    for field, value in every.items():
        if spi.set_field is not None:
            spi.set_field(ptr, field, int(value))
        else:
            Quartz.CGEventSetIntegerValueField(event, field, int(value))
    # The point inside the window, from its top-left corner. On macOS 27 an event stamped with the screen point (what
    # cua-driver does) or not stamped at all is dropped without a word; this one arrives, even in a window behind.
    bx, by = target.bounds[0], target.bounds[1]
    spi.window_location(ptr, float(x) - bx, float(y) - by)


def _send(event, target: Target) -> None:
    _sl().post(int(target.pid), _ptr(event))


def _source():
    return Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)


def _mouse(kind, x: float, y: float, button=Quartz.kCGMouseButtonLeft):
    return Quartz.CGEventCreateMouseEvent(_source(), kind, Quartz.CGPointMake(x, y), button)


def _group() -> int:
    return time.time_ns() % 1_000_000_000


@dataclass
class Sent:
    """What one try with Mint's pointer came to."""
    landed: bool = False            # sent, and the window changed (or could not be looked at: `checked` False)
    sent: bool = False              # events went out
    checked: bool = False           # before/after pictures of the window were compared
    why: str = ""                   # why it was not sent, or did not land
    target: Target | None = None
    before: object = None           # the window's picture before, for the caller's own check

    @property
    def route(self) -> str:
        return BACKGROUND if self.landed else REAL


class _Gesture:
    """Common to every gesture: the target window, checked; focus taken without raising when the app is
    behind, and given back after; the user's pointer and front app watched, and Mint's pointer switched off
    for the run if either moved because of it."""

    def __init__(self, x: float, y: float, pid: int | None) -> None:
        self.x, self.y, self.pid = x, y, pid
        self.result = Sent()
        self.target: Target | None = None
        self.front_pid: int | None = None
        self.focus = None
        self.pointer = None
        self.windows: set[int] = set()

    def open(self) -> bool:
        from mint.app import control
        from mint.screen import effect
        r = self.result
        if not available():
            r.why = why_off()
            return False
        if control.stopped():
            r.why = "stopped by the user"
            return False
        self.target = target_at(self.x, self.y, self.pid)
        r.target = self.target
        if self.target is None:
            r.why = "no ordinary app window at that point" + (" for that app" if self.pid else "")
            return False
        if not reaches(self.target.pid):
            r.why = f"Mint's pointer does not reach {self.target.app or 'that app'} ({_no_reach[self.target.pid]})"
            return False
        problem = still(self.target, self.x, self.y)
        if problem:
            r.why = problem
            return False
        self.pointer = effect.pointer_at()
        self.front_pid = _front_pid()
        if FOCUS_BEHIND and self.front_pid != self.target.pid:
            self.focus = focus_without_raise(self.target)
        # After focus is taken, and judged before it is given back: the window's active look is in both pictures.
        r.before = look(self.target)
        self.windows = _pid_windows(self.target.pid)
        return True

    def close(self, end: tuple[float, float]) -> None:
        from mint.screen import effect
        if self.focus is not None:
            give_focus_back(self.target, self.front_pid)
        now = effect.pointer_at()
        if self.pointer and now and abs(now[0] - self.pointer[0]) + abs(now[1] - self.pointer[1]) > 2 and \
                abs(now[0] - end[0]) <= 2 and abs(now[1] - end[1]) <= 2:
            # cua's rule: a route that moved the user's pointer is not a background route. Off for this run.
            _off["why"] = "it moved the real pointer once"
        if self.focus is not None and self.front_pid and _front_pid() == self.target.pid:
            mark_no_reach(self.target.pid, "it brought the app to the front")

    def verdict(self, look_for: float = LOOK_FOR) -> Sent:
        r = self.result
        r.sent = True
        if self.focus is not None:
            look_for = min(look_for, BEHIND_LOOK_FOR)   # the user's window is without the keyboard meanwhile
        seen = wait_for_change(self.target, r.before, look_for) if r.before is not None else None
        if not seen and _pid_windows(self.target.pid) - self.windows:
            seen = True             # a menu or popover opened: a window of its own, not in the picture
        r.checked = seen is not None
        r.landed = seen is not False
        if seen:
            _reached.add(self.target.pid)
        if not r.landed:
            r.why = "the window did not change"
        return r


def _pid_windows(pid: int) -> set[int]:
    """The app's on-screen windows at any level (its menus and popovers too)."""
    return {int(w.get("kCGWindowNumber", 0)) for w in _windows() if int(w.get("kCGWindowOwnerPID", 0)) == pid}


def _front_pid() -> int | None:
    try:
        import AppKit
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(front.processIdentifier()) if front is not None else None
    except Exception:
        return None


def click(x: float, y: float, button: str = "left", count: int = 1, pid: int | None = None,
          look_for: float = LOOK_FOR) -> Sent:
    """Click at screen point (x, y) in the window there (or in app `pid`'s window there) with Mint's
    pointer. cua-driver's background recipe: a move to the spot, an off-screen press-release that opens
    Chromium's user-activation gate without touching anything, then the press(es) at the spot."""
    from mint.app import control
    with _lock:
        g = _Gesture(x, y, pid)
        if not g.open():
            return g.result
        target, group = g.target, _group()
        right = button == "right"
        down, up = ((Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp) if right
                    else (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp))
        which = Quartz.kCGMouseButtonRight if right else Quartz.kCGMouseButtonLeft
        number = 1 if right else 0
        try:
            move = _mouse(Quartz.kCGEventMouseMoved, x, y)
            _stamp(move, target, x, y, {F_NUMBER: 2, F_CLICK_STATE: 0, F_BUTTON: 0, F_SUBTYPE: 3, F_GROUP: group})
            _send(move, target)
            time.sleep(0.015)
            if not right:
                for kind, phase, pause in ((down, 1, 0.001), (up, 2, 0.1)):
                    primer = _mouse(kind, -1, -1, which)
                    _stamp(primer, target, -1, -1, {F_NUMBER: phase, F_CLICK_STATE: 1, F_BUTTON: number,
                                                    F_SUBTYPE: 3, F_GROUP: group})
                    _send(primer, target)
                    time.sleep(pause)
            for n in range(1, max(1, min(2, count)) + 1):
                if control.stopped():
                    break
                for kind in (down, up):
                    event = _mouse(kind, x, y, which)
                    _stamp(event, target, x, y, {F_NUMBER: 3, F_CLICK_STATE: n, F_BUTTON: number, F_SUBTYPE: 3,
                                                 F_GROUP: group})
                    _send(event, target)
                    time.sleep(0.03)          # an NSButton's tracking loop polls for the release
                if n < count:
                    time.sleep(0.08)
            return g.verdict(look_for)
        finally:
            g.close((x, y))


def drag(x0: float, y0: float, x1: float, y1: float, seconds: float = 0.6, pid: int | None = None,
         look_for: float = LOOK_FOR) -> Sent:
    """Press at (x0, y0), move to (x1, y1) over `seconds`, release - all inside the window at the start, with
    Mint's pointer. Both ends must be in that one window: a drop on another app needs the real pointer."""
    from mint.app import control
    with _lock:
        g = _Gesture(x0, y0, pid)
        if not g.open():
            return g.result
        target, group = g.target, _group()
        if not target.holds(x1, y1):
            g.result.why = "the drop point is outside that window"
            g.close((x0, y0))
            return g.result
        try:
            move = _mouse(Quartz.kCGEventMouseMoved, x0, y0)
            _stamp(move, target, x0, y0, {F_CLICK_STATE: 0, F_BUTTON: 0, F_SUBTYPE: 0, F_GROUP: group})
            _send(move, target)
            time.sleep(0.03)
            press = _mouse(Quartz.kCGEventLeftMouseDown, x0, y0)
            _stamp(press, target, x0, y0, {F_CLICK_STATE: 1, F_BUTTON: 0, F_SUBTYPE: 0, F_GROUP: group})
            _send(press, target)
            time.sleep(0.12)                    # a held press: lists and canvases tell a drag from a click by it
            steps = max(12, int(seconds * 60))
            for i in range(1, steps + 1):
                if control.stopped():
                    break
                t = i / steps
                t = t * t * (3 - 2 * t)
                px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
                step = _mouse(Quartz.kCGEventLeftMouseDragged, px, py)
                _stamp(step, target, px, py, {F_CLICK_STATE: 1, F_BUTTON: 0, F_SUBTYPE: 0, F_GROUP: group})
                _send(step, target)
                time.sleep(seconds / steps)
            time.sleep(0.05)
            release = _mouse(Quartz.kCGEventLeftMouseUp, x1, y1)
            _stamp(release, target, x1, y1, {F_CLICK_STATE: 1, F_BUTTON: 0, F_SUBTYPE: 0, F_GROUP: group})
            _send(release, target)
            time.sleep(0.1)
            return g.verdict(look_for)
        finally:
            g.close((x1, y1))


def scroll(x: float, y: float, dy: int = 0, dx: int = 0, ticks: int = 1, pid: int | None = None,
           look_for: float = LOOK_FOR) -> Sent:
    """Turn the wheel over (x, y) in the window there, `ticks` notches of (dy, dx) lines each (positive dy
    shows what is above), with Mint's pointer."""
    from mint.app import control
    with _lock:
        g = _Gesture(x, y, pid)
        if not g.open():
            return g.result
        target = g.target
        try:
            move = _mouse(Quartz.kCGEventMouseMoved, x, y)
            _stamp(move, target, x, y, {F_CLICK_STATE: 0, F_BUTTON: 0, F_SUBTYPE: 3, F_GROUP: _group()})
            _send(move, target)
            time.sleep(0.012)
            for _ in range(max(1, ticks)):
                if control.stopped():
                    break
                wheel = Quartz.CGEventCreateScrollWheelEvent(_source(), Quartz.kCGScrollEventUnitLine, 2, int(dy),
                                                             int(dx))
                Quartz.CGEventSetLocation(wheel, Quartz.CGPointMake(x, y))
                _stamp(wheel, target, x, y, {})
                _send(wheel, target)
                time.sleep(0.03)
            return g.verdict(look_for)
        finally:
            g.close((x, y))
