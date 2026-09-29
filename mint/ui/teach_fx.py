"""While Mint is learning a task by watching (teach), the pointer and the clicks look recorded.

* A mint ring (Mint's own colour) glows round the pointer and follows it, trailing a touch, the
  whole time Mint is watching - so it is plain that this is being recorded. macOS does not let an
  app recolour the system pointer inside other apps, so the pointer itself stays; the ring is the
  colour.
* Each click: the ring presses in, a ripple rolls out, a camera-like frame snaps round the spot
  ("captured"), and the step's number floats up from it.
* Clicks on Mint itself (the island's pause / done / cancel) get no effect - a global mouse monitor
  never sees them - and teach.py does not record them either (their window is Mint's).
* Paused: the ring dims and stops reacting. It all goes away when the recording ends.

Nothing here is in screenshots or screen shares (the overlay is kept out of captures), so it never
ends up in the skill's own screenshots either.
"""

from __future__ import annotations

import logging
import math
import time

import AppKit
import objc
import Quartz

from mint.ui import gfx

log = logging.getLogger("mint.ui.teach_fx")

RING = 34          # the pointer ring's diameter


def _cg(rgb, alpha=1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


class _FxTicker(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(_FxTicker, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def tick_(self, timer):
        try:
            self.owner.tick()
        except Exception:
            log.debug("teach fx tick", exc_info=True)


class TeachFX:
    def __init__(self) -> None:
        self.active = False
        self.paused = False
        self.pos = None
        self.monitor = None
        self.last_check = 0.0
        self.clicks = 0

    def attach(self, hud) -> None:
        from mint.ui.hud import _panel
        self.hud = hud
        screen = AppKit.NSScreen.mainScreen().frame()
        self.origin = (screen.origin.x, screen.origin.y)
        self.panel = _panel(screen, click_through=True)
        root = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, screen.size.width, screen.size.height))
        root.setWantsLayer_(True)
        self.panel.setContentView_(root)
        self.root = root.layer()
        self.rgb = tuple(gfx.accent())

        ring = Quartz.CAShapeLayer.layer()
        ring.setBounds_(Quartz.CGRectMake(0, 0, RING, RING))
        ring.setPath_(Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(2, 2, RING - 4, RING - 4), None))
        ring.setFillColor_(_cg(self.rgb, 0.16))
        ring.setStrokeColor_(_cg(self.rgb, 0.95))
        ring.setLineWidth_(2.5)
        ring.setShadowColor_(_cg(self.rgb))
        ring.setShadowRadius_(8)
        ring.setShadowOpacity_(0.9)
        ring.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        ring.setOpacity_(0.0)
        self.root.addSublayer_(ring)
        self.ring = ring

        self.ticker = _FxTicker.alloc().initWithOwner_(self)
        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 60, self.ticker, "tick:", None, True)

    # --- state ---------------------------------------------------------------------------------

    def _state(self) -> tuple[bool, bool, int]:
        from mint.knowledge import teach
        snap = teach.snapshot()
        live = bool(snap) and "seconds" in snap
        return live, bool(snap.get("paused")), int(snap.get("clicks", 0))

    def tick(self) -> None:
        now = time.monotonic()
        if now - self.last_check > 0.2:
            self.last_check = now
            active, paused, clicks = self._state()
            self.clicks = clicks
            if active != self.active:
                self._set_active(active)
            if paused != self.paused:
                self.paused = paused
                Quartz.CATransaction.begin()
                Quartz.CATransaction.setAnimationDuration_(0.3)
                self.ring.setStrokeColor_(_cg((0.6, 0.6, 0.65) if paused else self.rgb, 0.8))
                self.ring.setShadowOpacity_(0.0 if paused else 0.9)
                Quartz.CATransaction.commit()
        if not self.active:
            return
        mouse = AppKit.NSEvent.mouseLocation()
        target = (mouse.x - self.origin[0], mouse.y - self.origin[1])
        if self.pos is None:
            self.pos = target
        # Trails the pointer a touch, like something following it rather than glued on.
        self.pos = (self.pos[0] + (target[0] - self.pos[0]) * 0.45, self.pos[1] + (target[1] - self.pos[1]) * 0.45)
        breath = 1.0 + 0.05 * math.sin(now * 3.2)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.ring.setPosition_(Quartz.CGPointMake(*self.pos))
        if not self.ring.animationForKey_("press"):
            self.ring.setAffineTransform_(Quartz.CGAffineTransformMakeScale(breath, breath))
        Quartz.CATransaction.commit()

    def _set_active(self, active: bool) -> None:
        self.active = active
        if active:
            self.pos = None
            self.panel.orderFrontRegardless()
            if self.monitor is None:
                mask = AppKit.NSEventMaskLeftMouseDown | AppKit.NSEventMaskRightMouseDown
                self.monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, self._clicked)
        else:
            if self.monitor is not None:
                AppKit.NSEvent.removeMonitor_(self.monitor)
                self.monitor = None
            self.panel.orderOut_(None)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.35)
        self.ring.setOpacity_(1.0 if active else 0.0)
        Quartz.CATransaction.commit()

    # --- a click --------------------------------------------------------------------------------

    def _clicked(self, event) -> None:
        """A click in another app (the monitor never sees clicks on Mint's own windows)."""
        if not self.active or self.paused:
            return
        try:
            mouse = AppKit.NSEvent.mouseLocation()
            self.burst((mouse.x - self.origin[0], mouse.y - self.origin[1]), self.clicks + 1)
        except Exception:
            log.debug("teach fx click", exc_info=True)

    def burst(self, point, step: int) -> None:
        x, y = point
        rgb = self.rgb
        # The ring presses in and springs back.
        press = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
        press.setFromValue_(0.62)
        press.setToValue_(1.0)
        press.setDamping_(9)
        press.setStiffness_(260)
        press.setDuration_(press.settlingDuration())
        self.ring.addAnimation_forKey_(press, "press")

        # A ripple rolling out.
        ripple = Quartz.CAShapeLayer.layer()
        ripple.setBounds_(Quartz.CGRectMake(0, 0, 40, 40))
        ripple.setPosition_(Quartz.CGPointMake(x, y))
        ripple.setPath_(Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(0, 0, 40, 40), None))
        ripple.setFillColor_(None)
        ripple.setStrokeColor_(_cg(rgb, 0.9))
        ripple.setLineWidth_(3)
        ripple.setOpacity_(0.0)
        self.root.addSublayer_(ripple)
        grow = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
        grow.setFromValue_(0.4)
        grow.setToValue_(2.6)
        fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
        fade.setFromValue_(0.95)
        fade.setToValue_(0.0)
        self._group(ripple, [grow, fade], 0.6)

        # The "captured" frame: four corner brackets snapping in round the spot.
        frame = Quartz.CAShapeLayer.layer()
        w, h, c = 70.0, 48.0, 12.0
        frame.setBounds_(Quartz.CGRectMake(0, 0, w, h))
        frame.setPosition_(Quartz.CGPointMake(x, y))
        path = Quartz.CGPathCreateMutable()
        for (px, py, dx, dy) in ((0, 0, 1, 1), (w, 0, -1, 1), (0, h, 1, -1), (w, h, -1, -1)):
            Quartz.CGPathMoveToPoint(path, None, px, py + dy * c)
            Quartz.CGPathAddLineToPoint(path, None, px, py)
            Quartz.CGPathAddLineToPoint(path, None, px + dx * c, py)
        frame.setPath_(path)
        frame.setFillColor_(None)
        frame.setStrokeColor_(_cg((1, 1, 1), 0.95))
        frame.setLineWidth_(2.5)
        frame.setLineCap_(Quartz.kCALineCapRound)
        frame.setShadowColor_(_cg(rgb))
        frame.setShadowRadius_(6)
        frame.setShadowOpacity_(1.0)
        frame.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        frame.setOpacity_(0.0)
        self.root.addSublayer_(frame)
        snap = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
        snap.setValues_([1.35, 0.96, 1.0, 1.0])
        snap.setKeyTimes_([0.0, 0.3, 0.45, 1.0])
        show = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        show.setValues_([0.0, 1.0, 1.0, 0.0])
        show.setKeyTimes_([0.0, 0.12, 0.6, 1.0])
        self._group(frame, [snap, show], 0.75)

        # The step number floating up.
        badge = gfx.number_badge(str(step), 22, _cg(rgb), _cg((0.04, 0.1, 0.08)), 12)
        badge.setPosition_(Quartz.CGPointMake(x + 30, y + 22))
        badge.setOpacity_(0.0)
        self.root.addSublayer_(badge)
        rise = Quartz.CABasicAnimation.animationWithKeyPath_("position.y")
        rise.setFromValue_(y + 14)
        rise.setToValue_(y + 40)
        pop = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
        pop.setValues_([0.3, 1.2, 1.0, 1.0])
        pop.setKeyTimes_([0.0, 0.2, 0.35, 1.0])
        glow = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        glow.setValues_([0.0, 1.0, 1.0, 0.0])
        glow.setKeyTimes_([0.0, 0.15, 0.7, 1.0])
        self._group(badge, [rise, pop, glow], 1.0)

    def _group(self, layer, animations, seconds: float) -> None:
        group = Quartz.CAAnimationGroup.animation()
        group.setAnimations_(animations)
        group.setDuration_(seconds)
        group.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseOut))
        layer.addAnimation_forKey_(group, "fx")
        from PyObjCTools import AppHelper
        AppHelper.callLater(seconds + 0.05, layer.removeFromSuperlayer)


fx = TeachFX()


def attach(hud) -> None:
    try:
        fx.attach(hud)
    except Exception:
        log.exception("teach effects failed to build")
