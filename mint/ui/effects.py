"""On-screen effects, kept minimal: the orb itself shows what Mint is doing
(it morphs into the task - see orb.set_badge), so the screen only gets

* click     - a small soft ripple where a click lands (the orb's eyes glance there);
* highlight - a thin outline round the field being typed into.

Scrolling only turns the orb's eyes. The overlays are excluded from screen
capture (NSWindowSharingNone), ignore the mouse, and do nothing until build()
has run - the `--tool` test harness has no UI and gets no effects.
"""

from __future__ import annotations

import math
import os

import AppKit
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx
from mint.core import prefs


# MINT_CAPTURE=1 lets screenshots see the HUD and effects - for demos and
# visual tests only; normally they must stay out of what Mint reads.
SHARING = (AppKit.NSWindowSharingReadOnly if os.environ.get("MINT_CAPTURE")
           else AppKit.NSWindowSharingNone)


def _ease(name=Quartz.kCAMediaTimingFunctionEaseInEaseOut):
    return Quartz.CAMediaTimingFunction.functionWithName_(name)


def _primary_height() -> float:
    screens = AppKit.NSScreen.screens()
    return screens[0].frame().size.height if screens else 0.0


class Effects:
    def __init__(self) -> None:
        self._overlays: list[tuple] = []   # (frame, window, root layer, scale)
        self._observer = None
        self.origin = None                 # callable -> (x, y) Cocoa global, the orb
        self.on_target = None              # callable((x, y) Cocoa global): the orb looks there
        self.built = False

    # --- construction (main thread) ---------------------------------------------

    def build(self) -> None:
        self._rebuild()
        center = AppKit.NSNotificationCenter.defaultCenter()
        self._observer = center.addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationDidChangeScreenParametersNotification, None, None,
            lambda note: self._rebuild())
        self.built = True

    def _rebuild(self) -> None:
        for _, window, _, _ in self._overlays:
            window.orderOut_(None)
        self._overlays = []
        for screen in AppKit.NSScreen.screens():
            frame = screen.frame()
            window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
            window.setOpaque_(False)
            window.setBackgroundColor_(AppKit.NSColor.clearColor())
            window.setIgnoresMouseEvents_(True)
            window.setHasShadow_(False)
            window.setLevel_(AppKit.NSPopUpMenuWindowLevel)
            window.setReleasedWhenClosed_(False)
            window.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorStationary
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
            # Never in a screenshot: text recognition must read the app, not a ripple.
            window.setSharingType_(SHARING)
            view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, frame.size.width, frame.size.height))
            view.setWantsLayer_(True)
            window.setContentView_(view)
            window.orderFrontRegardless()
            self._overlays.append((frame, window, view.layer(), screen.backingScaleFactor()))

    # --- public API (any thread) ---------------------------------------------------

    def enabled(self) -> bool:
        return self.built and bool(prefs.get("cursor_effects"))

    def click(self, x: float, y: float, label: str = "") -> float:
        """A click is about to land at (x, y), in screen points from the top
        left (Quartz). Returns how long the caller should wait so the spark
        arrives as the click does."""
        if not self.enabled():
            return 0.0
        AppHelper.callAfter(self._click, x, y, label)
        return 0.08

    def highlight(self, x: float, y: float, w: float, h: float, seconds: float = 2.2,
                  label: str = "") -> None:
        if self.enabled() and w > 3 and h > 3:
            AppHelper.callAfter(self._highlight, x, y, w, h, seconds, label)

    def highlight_focused(self, seconds: float = 2.2, label: str = "") -> None:
        """Outline whatever has keyboard focus: the field about to be typed into."""
        if not self.enabled():
            return
        frame = focused_frame()
        if frame is not None:
            self.highlight(*frame, seconds=seconds, label=label)

    def scroll(self, x: float, y: float, direction: str) -> None:
        """Nothing drawn: the orb's eyes glance at the pane being scrolled."""
        if self.enabled():
            AppHelper.callAfter(self._locate, x, y)

    # --- drawing (main thread) -------------------------------------------------------

    def _locate(self, x: float, y: float):
        """Quartz global point -> (root layer, local point, frame, scale)."""
        if not self._overlays:
            return None
        cx, cy = x, _primary_height() - y
        if self.on_target is not None:
            self.on_target((cx, cy))
        for frame, _, root, scale in self._overlays:
            if AppKit.NSPointInRect((cx, cy), frame):
                return root, (cx - frame.origin.x, cy - frame.origin.y), frame, scale
        frame, _, root, scale = self._overlays[0]
        return root, (cx - frame.origin.x, cy - frame.origin.y), frame, scale

    def _dot(self, size, rgb, alpha=1.0):
        dot = Quartz.CALayer.layer()
        dot.setBounds_(Quartz.CGRectMake(0, 0, size, size))
        dot.setCornerRadius_(size / 2)
        dot.setBackgroundColor_(gfx.cg(rgb, alpha))
        dot.setShadowColor_(gfx.cg(rgb))
        dot.setShadowOpacity_(0.9)
        dot.setShadowRadius_(size * 0.8)
        dot.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        dot.setOpacity_(0)
        return dot

    def _later(self, seconds, layers):
        AppHelper.callLater(seconds, lambda: [layer.removeFromSuperlayer() for layer in layers])

    def _ripple(self, root, point, rgb, delay=0.0, radius=16, rings=3):
        x, y = point
        now = Quartz.CACurrentMediaTime() + delay
        layers = []
        for k in range(rings):
            ring = Quartz.CAShapeLayer.layer()
            bounds = Quartz.CGRectMake(0, 0, radius * 2, radius * 2)
            ring.setBounds_(bounds)
            ring.setPosition_(Quartz.CGPointMake(x, y))
            ring.setPath_(Quartz.CGPathCreateWithEllipseInRect(bounds, None))
            ring.setFillColor_(None)
            ring.setStrokeColor_(gfx.cg(gfx.light(rgb) if k == 0 else rgb))
            ring.setLineWidth_(2.5 if k == 0 else 1.5)
            ring.setShadowColor_(gfx.cg(rgb))
            ring.setShadowOpacity_(0.8)
            ring.setShadowRadius_(6)
            ring.setShadowOffset_(Quartz.CGSizeMake(0, 0))
            ring.setOpacity_(0)
            grow = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
            grow.setFromValue_(0.25)
            grow.setToValue_(1.7 + k * 0.55)
            fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            fade.setFromValue_(0.95)
            fade.setToValue_(0.0)
            group = Quartz.CAAnimationGroup.animation()
            group.setAnimations_([grow, fade])
            group.setDuration_(0.7)
            group.setBeginTime_(now + k * 0.11)
            group.setTimingFunction_(_ease(Quartz.kCAMediaTimingFunctionEaseOut))
            ring.addAnimation_forKey_(group, "ripple")
            root.addSublayer_(ring)
            layers.append(ring)

        flash = self._dot(16, gfx.light(rgb))
        flash.setPosition_(Quartz.CGPointMake(x, y))
        shrink = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
        shrink.setFromValue_(1.5)
        shrink.setToValue_(0.2)
        fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
        fade.setFromValue_(1.0)
        fade.setToValue_(0.0)
        group = Quartz.CAAnimationGroup.animation()
        group.setAnimations_([shrink, fade])
        group.setDuration_(0.4)
        group.setBeginTime_(now)
        flash.addAnimation_forKey_(group, "flash")
        root.addSublayer_(flash)
        layers.append(flash)
        self._later(delay + 1.2, layers)

    def _click(self, x, y, label):
        found = self._locate(x, y)
        if found is None:
            return
        root, end, _, _ = found
        self._ripple(root, end, gfx.accent(), radius=9, rings=1)

    def _highlight(self, x, y, w, h, seconds, label):
        found = self._locate(x, y + h)          # bottom-left corner in Cocoa terms
        if found is None:
            return
        root, (lx, ly), _, scale = found
        rgb = gfx.accent()
        rect = Quartz.CGRectMake(lx - 5, ly - 5, w + 10, h + 10)
        box = Quartz.CAShapeLayer.layer()
        box.setPath_(Quartz.CGPathCreateWithRoundedRect(rect, 9, 9, None))
        box.setFillColor_(None)
        box.setStrokeColor_(gfx.cg(gfx.light(rgb)))
        box.setLineWidth_(1.5)
        box.setShadowColor_(gfx.cg(rgb))
        box.setShadowOpacity_(0.5)
        box.setShadowRadius_(4)
        box.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        box.setOpacity_(0)
        pulse = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        beats = max(2, int(seconds / 0.45))
        values = [0.0] + [1.0 if i % 2 == 0 else 0.55 for i in range(beats)] + [0.0]
        pulse.setValues_(values)
        pulse.setDuration_(seconds)
        # Draw the outline in as well as fading it in.
        draw = Quartz.CABasicAnimation.animationWithKeyPath_("strokeEnd")
        draw.setFromValue_(0.0)
        draw.setToValue_(1.0)
        draw.setDuration_(0.35)
        draw.setTimingFunction_(_ease(Quartz.kCAMediaTimingFunctionEaseOut))
        box.addAnimation_forKey_(pulse, "pulse")
        box.addAnimation_forKey_(draw, "draw")
        root.addSublayer_(box)
        layers = [box]
        self._later(seconds + 0.1, layers)


def focused_frame():
    """(x, y, w, h) in Quartz screen points of the focused UI element, or None."""
    try:
        import ApplicationServices as AX

        from mint.screen import axkit
        # Never the system-wide focused element: when Mint itself has the keyboard, that is answered inside
        # Mint, off the main thread, and AppKit's accessibility code crashed (the teach crash, 1 Oct).
        element = axkit.focused_element()
        if element is None:
            return None
        e1, pos = AX.AXUIElementCopyAttributeValue(element, "AXPosition", None)
        e2, size = AX.AXUIElementCopyAttributeValue(element, "AXSize", None)
        if e1 != 0 or e2 != 0 or pos is None or size is None:
            return None
        p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)[1]
        s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)[1]
        if s.width < 4 or s.height < 4:
            return None
        # A whole-page editor (Docs, Notion) is focused as one huge area; an
        # outline around the entire window says nothing, so trim it to a band.
        height = min(s.height, 160)
        return (p.x, p.y, s.width, height)
    except Exception:
        return None


fx = Effects()
