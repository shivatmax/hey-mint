"""A glowing border round the window Mint is working in (pref window_glow).

    window_glow.glow(pid=None, window_id=None, point=None, seconds=2.5)
    window_glow.stop()

While Mint clicks, types, scrolls or drags in an app, that app's window gets an animated iridescent
border - the Apple Intelligence colours slowly turning round its edge, a soft glow outside and a faint
light just inside. Every glow() is a ping: the border stays while pings keep coming and fades out
`seconds` after the last one.

Which window: `window_id`, else the window under `point` (Quartz screen points, top-left origin, the
same as effects.fx.click), else the front window of `pid`, else the front window of the frontmost
app. Mint's own windows never glow.

The border is a click-through panel laid exactly over the window (ABOVE_TARGET: ordered
just above the window, so windows in front still cover it; else floating), never key, on every space, and out of screen capture like every Mint
window (sharing.py). It follows the window as it moves or resizes (one cheap window-server query,
10 times a second) and goes away when the window is minimised, closed or on another space.
Callable from any thread; never raises.
"""

from __future__ import annotations

import logging
import math
import os
import platform
import threading
import time

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import kinetics
from mint.core import prefs

log = logging.getLogger("mint.ui.window_glow")

PREF = "window_glow"
PAD = 28.0              # room round the window for the soft outer glow
LINE = 3.0              # the bright border's width
FADE_IN = 0.25
FADE_OUT = 0.6
TRACK = 0.1             # seconds between follow-the-window checks
TURN = 7.0              # seconds for the colours to go once round
MIN_W, MIN_H = 120, 80  # smaller than this is a popup or a tooltip, not "the window"
# Order the panel just above the target window at the normal level, so windows in front of the
# target keep covering the glow (tested: works across apps). At the normal level the panel is a
# layer-0 Mint window, so ground.owner_at() and macos.list_windows() skip Mint's own pid.
ABOVE_TARGET = True


def _radius() -> float:
    # No public API says how round a window's corners are. macOS 26+ windows are rounder (16-26 pt
    # depending on the toolbar); a middling radius lets the 3 pt stroke and its glow cover the rest.
    try:
        major = int(platform.mac_ver()[0].split(".")[0])
    except Exception:
        major = 15
    return 16.0 if major >= 26 else 10.0


RADIUS = _radius()

# Blue -> violet -> pink -> orange -> mint and back to blue, so the conic seam never shows.
PALETTE = ((0.26, 0.56, 1.00), (0.60, 0.38, 1.00), (1.00, 0.38, 0.72),
           (1.00, 0.62, 0.28), (0.32, 0.92, 0.72), (0.26, 0.56, 1.00))


def enabled() -> bool:
    try:
        if not prefs.get(PREF):
            return False
        app = AppKit.NSApp()              # never create the app here: this may be a worker thread
        return app is not None and bool(app.isRunning())   # the --tool harness has no run loop
    except Exception:
        return False


# --- finding the window (any thread) ----------------------------------------------------------------

_OWN = os.getpid()


def _displays() -> list:
    try:
        err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
        return [Quartz.CGDisplayBounds(d) for d in (ids or [])[:count]]
    except Exception:
        return []


def _rect(info) -> tuple | None:
    b = info.get("kCGWindowBounds")
    if not b:
        return None
    return (float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"]))


def _usable(info, displays) -> bool:
    if info.get("kCGWindowLayer", 0) != 0 or int(info.get("kCGWindowOwnerPID", 0)) == _OWN:
        return False
    if float(info.get("kCGWindowAlpha", 1.0)) < 0.05:
        return False
    r = _rect(info)
    if r is None or r[2] < MIN_W or r[3] < MIN_H:
        return False
    if not displays:
        return True
    # Some apps keep invisible helper windows parked off screen (Brave has a few above y=0).
    return any(Quartz.CGRectIntersectsRect(Quartz.CGRectMake(*r), d) for d in displays)


def _on_screen() -> list:
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    return list(Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or [])


def _one(window_id: int):
    found = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, int(window_id))
    return found[0] if found else None


def _front_pid() -> int | None:
    try:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app is not None else None
    except Exception:
        return None


def resolve(pid=None, window_id=None, point=None) -> tuple | None:
    """-> (window id, (x, y, w, h) Quartz) of the window meant, or None."""
    displays = _displays()
    if window_id:
        info = _one(window_id)
        if info is None or int(info.get("kCGWindowOwnerPID", 0)) == _OWN or not _rect(info):
            return None
        return int(info["kCGWindowNumber"]), _rect(info)
    windows = [w for w in _on_screen() if _usable(w, displays)]   # front to back
    if point is not None:
        px, py = float(point[0]), float(point[1])
        for w in windows:
            x, y, ww, hh = _rect(w)
            if x <= px <= x + ww and y <= py <= y + hh:
                return int(w["kCGWindowNumber"]), _rect(w)
        return None
    if pid is None:
        pid = _front_pid()
    if pid is None or int(pid) == _OWN:
        return None
    mine = [w for w in windows if int(w.get("kCGWindowOwnerPID", 0)) == int(pid)]
    if not mine:
        return None
    # The frontmost window that is a real window: a find bar or a hover card in front of the main
    # window is a fraction of its size.
    biggest = max(_rect(w)[2] * _rect(w)[3] for w in mine)
    for w in mine:
        r = _rect(w)
        if r[2] * r[3] >= biggest * 0.25:
            return int(w["kCGWindowNumber"]), r
    return int(mine[0]["kCGWindowNumber"]), _rect(mine[0])


# --- drawing (main thread) --------------------------------------------------------------------------

def _primary_height() -> float:
    screens = AppKit.NSScreen.screens()
    return screens[0].frame().size.height if screens else 0.0


def _cocoa(rect) -> AppKit.NSRect:
    """Quartz window bounds -> the panel's Cocoa frame, PAD bigger on every side."""
    x, y, w, h = rect
    return AppKit.NSMakeRect(x - PAD, _primary_height() - (y + h) - PAD, w + 2 * PAD, h + 2 * PAD)


def _rounded(w, h, inset=0.0, radius=None):
    r = max(0.0, (RADIUS if radius is None else radius) - inset)
    rect = Quartz.CGRectMake(PAD + inset, PAD + inset, max(1.0, w - 2 * PAD - 2 * inset),
                             max(1.0, h - 2 * PAD - 2 * inset))
    r = min(r, rect.size.width / 2, rect.size.height / 2)
    return Quartz.CGPathCreateWithRoundedRect(rect, r, r, None)


def _ring(w, h, width, inset=0.0):
    """The outline of a stroke `width` wide along the window's edge, as a fillable path."""
    return Quartz.CGPathCreateCopyByStrokingPath(_rounded(w, h, inset), None, width,
                                                 Quartz.kCGLineCapRound, Quartz.kCGLineJoinRound, 10)


def _conic():
    g = Quartz.CAGradientLayer.layer()
    g.setType_(Quartz.kCAGradientLayerConic)
    g.setColors_([Quartz.CGColorCreateSRGB(r, gg, b, 1.0) for r, gg, b in PALETTE])
    g.setStartPoint_(Quartz.CGPointMake(0.5, 0.5))
    g.setEndPoint_(Quartz.CGPointMake(1.0, 0.5))
    return g


def _turn(g, phase: float = 0.0) -> None:
    """Turn the conic gradient forever by walking its end point round the centre."""
    g.removeAnimationForKey_("turn")
    if kinetics.reduce_motion():
        return                            # still colours: no movement
    steps = 48
    values = []
    for i in range(steps + 1):
        a = phase + 2 * math.pi * i / steps
        values.append(AppKit.NSValue.valueWithPoint_(AppKit.NSMakePoint(0.5 + 0.5 * math.cos(a),
                                                                        0.5 + 0.5 * math.sin(a))))
    anim = Quartz.CAKeyframeAnimation.animationWithKeyPath_("endPoint")
    anim.setValues_(values)
    anim.setDuration_(TURN)
    anim.setRepeatCount_(float("inf"))
    anim.setCalculationMode_(Quartz.kCAAnimationLinear)
    g.addAnimation_forKey_(anim, "turn")


def _masked(parent, opacity: float):
    """A gradient seen through a mask; returns (holder, gradient, mask). The mask is a plain layer whose
    sublayers (bands, _bands) say how much of the gradient shows where."""
    holder = Quartz.CALayer.layer()
    holder.setOpacity_(opacity)
    mask = Quartz.CALayer.layer()
    holder.setMask_(mask)
    g = _conic()
    holder.addSublayer_(g)
    parent.addSublayer_(holder)
    return holder, g, mask


def _bands(mask, w, h, start, end, peak, step=2.0) -> None:
    """Fill `mask` so the gradient shows from `start` to `end` pt off the window's edge (negative =
    inside), fading from `peak` to nothing: a soft glow with no blur filter to run every frame. Nested
    bands that all begin at `start`, each a little wider and faint, add up to a smooth falloff (side by
    side bands left visible seams)."""
    for layer in list(mask.sublayers() or []):
        layer.removeFromSuperlayer()
    span = abs(end - start)
    sign = 1.0 if end > start else -1.0
    n = max(1, int(math.ceil(span / step)))
    alpha = 1.0 - (1.0 - peak) ** (1.0 / n)
    for k in range(n):
        width = (k + 1) * step
        band = Quartz.CAShapeLayer.layer()
        band.setFrame_(Quartz.CGRectMake(0, 0, w, h))
        band.setPath_(_ring(w, h, width, inset=-(start + sign * width / 2)))
        band.setFillColor_(Quartz.CGColorCreateSRGB(0, 0, 0, alpha))
        mask.addSublayer_(band)


class MintGlowTicker(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(MintGlowTicker, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def tick_(self, timer):
        try:
            self.owner.tick()
        except Exception:
            log.debug("window glow tick failed", exc_info=True)


class Glow:
    def __init__(self) -> None:
        self.panel = None
        self.root = None
        self.wid = None
        self.rect = None              # Quartz bounds of the window glowing now
        self.deadline = 0.0
        self.fading_until = 0.0
        self.timer = None
        self.parts = []               # [(holder, gradient, mask, kind)]
        self.edge = None
        self.size = (0.0, 0.0)
        self.above = ABOVE_TARGET     # ordered just above the target; False = the floating level

    # --- the panel ---------------------------------------------------------------------------------
    def _build(self) -> None:
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, 400, 300),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)       # Mint is almost never the active app
        panel.setReleasedWhenClosed_(False)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setLevel_(AppKit.NSNormalWindowLevel if self.above else AppKit.NSFloatingWindowLevel)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        try:
            from mint.ui import sharing
            panel.setSharingType_(sharing._desired())
        except Exception:
            panel.setSharingType_(AppKit.NSWindowSharingNone)
        view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 400, 300))
        view.setWantsLayer_(True)
        panel.setContentView_(view)
        root = view.layer()
        root.setOpacity_(0.0)

        # Back to front: the soft glow outside, a tint washing in from the edge, the bright border,
        # and a hairline of light just inside it.
        outer = _masked(root, 0.9)
        inside = Quartz.CALayer.layer()               # clips the wash to the window itself
        clip = Quartz.CAShapeLayer.layer()
        clip.setFillColor_(Quartz.CGColorCreateSRGB(0, 0, 0, 1))
        inside.setMask_(clip)
        root.addSublayer_(inside)
        wash = _masked(inside, 0.35)
        border = _masked(root, 1.0)
        edge = Quartz.CAShapeLayer.layer()
        edge.setFillColor_(None)
        edge.setStrokeColor_(Quartz.CGColorCreateSRGB(1, 1, 1, 0.38))
        edge.setLineWidth_(1.0)
        root.addSublayer_(edge)
        self.parts = [(*outer, "outer"), (*wash, "wash"), (*border, "border")]
        self.inside, self.clip, self.edge = inside, clip, edge
        self.panel, self.root = panel, root
        for i, (_, g, _, _) in enumerate(self.parts):
            _turn(g, phase=0.0)
        if not kinetics.reduce_motion():
            kinetics.pulse(outer[0], key="breathe", seconds=2.6, low=0.7, high=1.0)
        self.ticker = MintGlowTicker.alloc().initWithOwner_(self)

    def _layout(self, w: float, h: float) -> None:
        if (w, h) == self.size:
            return
        self.size = (w, h)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        bounds = Quartz.CGRectMake(0, 0, w, h)
        for holder, g, mask, kind in self.parts:
            holder.setFrame_(bounds)
            g.setFrame_(bounds)
            mask.setFrame_(bounds)
            if kind == "outer":
                _bands(mask, w, h, 0.5, 20.0, 0.85)               # the halo outside the window
            elif kind == "wash":
                _bands(mask, w, h, -LINE, -34.0, 0.7, step=3.0)   # colour washing in from the edge
            else:
                border = Quartz.CAShapeLayer.layer()
                border.setFrame_(bounds)
                border.setPath_(_ring(w, h, LINE, inset=0.5))
                border.setFillColor_(Quartz.CGColorCreateSRGB(0, 0, 0, 1))
                for old in list(mask.sublayers() or []):
                    old.removeFromSuperlayer()
                mask.addSublayer_(border)
        self.inside.setFrame_(bounds)
        self.clip.setFrame_(bounds)
        self.clip.setPath_(_rounded(w, h))
        self.edge.setFrame_(bounds)
        self.edge.setPath_(_rounded(w, h, inset=LINE + 0.5))
        Quartz.CATransaction.commit()

    def _place(self, rect) -> None:
        frame = _cocoa(rect)
        self.rect = rect
        self._layout(frame.size.width, frame.size.height)
        if not AppKit.NSEqualRects(self.panel.frame(), frame):
            self.panel.setFrame_display_(frame, False)
        self._order()

    def _order(self) -> None:
        """Just above the target window (ABOVE_TARGET), else floating; never made key."""
        if self.wid is None:
            return
        if self.above:
            try:
                self.panel.orderWindow_relativeTo_(AppKit.NSWindowAbove, int(self.wid))
                return
            except Exception:
                self.above = False
                self.panel.setLevel_(AppKit.NSFloatingWindowLevel)
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()

    # --- public, main thread ---------------------------------------------------------------------
    def show(self, wid: int, rect, seconds: float) -> None:
        if not enabled():
            return
        if self.panel is None:
            self._build()
        now = time.monotonic()
        fresh = wid != self.wid or not self.panel.isVisible() or now < self.fading_until \
            or self.root.opacity() < 0.5
        self.wid = wid
        self.deadline = max(self.deadline if not fresh else 0.0, now + max(0.3, float(seconds)))
        self._place(rect)
        if fresh:
            self.fading_until = 0.0
            kinetics.fade(self.root, True, FADE_IN)
        if self.timer is None:
            self.timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                TRACK, self.ticker, "tick:", None, True)
            AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, AppKit.NSRunLoopCommonModes)

    def stop(self, seconds: float = FADE_OUT) -> None:
        if self.panel is None or self.wid is None:
            return
        self.wid = None
        self.fading_until = time.monotonic() + seconds
        kinetics.fade(self.root, False, seconds)

    def _finish(self) -> None:
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        if self.panel is not None:
            self.panel.orderOut_(None)
        self.fading_until = 0.0

    def tick(self) -> None:
        now = time.monotonic()
        if self.wid is None:
            if now >= self.fading_until:
                self._finish()
            return
        if now > self.deadline or not prefs.get(PREF):
            self.stop()
            return
        info = _one(self.wid)
        if info is None or not info.get("kCGWindowIsOnscreen"):
            self.stop(0.15)               # closed, minimised or on another space
            return
        rect = _rect(info)
        if rect and rect != self.rect:
            self._place(rect)
        else:
            self._order()


_glow = Glow()
_last = {"key": None, "at": 0.0, "wid": None, "rect": None}


def _send(found, seconds) -> None:
    wid, rect = found
    if AppKit.NSThread.isMainThread():
        _glow.show(wid, rect, seconds)
    else:
        AppHelper.callAfter(_glow.show, wid, rect, seconds)


def _work(pid, window_id, point, seconds) -> None:
    try:
        key = (pid, window_id, None if point is None else (round(point[0] / 40), round(point[1] / 40)))
        now = time.monotonic()
        if key == _last["key"] and now - _last["at"] < 0.4 and _last["wid"] is not None:
            found = (_last["wid"], _last["rect"])      # a burst of pings: no new window-server query
        else:
            found = resolve(pid, window_id, point)
            _last.update(key=key, at=now, wid=found[0] if found else None, rect=found[1] if found else None)
        if found:
            _send(found, seconds)
    except Exception:
        log.debug("window glow failed", exc_info=True)


def glow(pid=None, window_id=None, point=None, seconds: float = 2.5) -> None:
    """Mint is working in this window: light it up (or keep it lit) for `seconds` more."""
    try:
        if not enabled():
            return
        if AppKit.NSThread.isMainThread():
            # The window list query takes a few ms; the main thread draws instead.
            threading.Thread(target=_work, args=(pid, window_id, point, seconds), daemon=True,
                             name="mint-window-glow").start()
        else:
            _work(pid, window_id, point, seconds)
    except Exception:
        log.debug("window glow failed", exc_info=True)


def ping(*args, **kwargs) -> None:
    glow(*args, **kwargs)


def stop() -> None:
    """Fade the glow out now."""
    try:
        if AppKit.NSThread.isMainThread():
            _glow.stop()
        else:
            AppHelper.callAfter(_glow.stop)
    except Exception:
        log.debug("window glow stop failed", exc_info=True)
