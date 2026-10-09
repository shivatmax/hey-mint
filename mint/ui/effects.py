"""On-screen effects, kept minimal: the orb itself shows what Mint is doing
(it morphs into the task - see orb.set_badge), so the screen only gets

* click     - a small soft ripple where a click lands (the orb's eyes glance there);
* highlight - a thin outline round the field being typed into;
* the Mint cursor - Mint's own pointer, in its colour, that glides to each spot it acts on (click, type,
  scroll, drag) so you see what it is doing; it never moves your pointer, and fades when Mint stops.

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
        self._cursor = None                # (root, arrow layer, label layer) on the screen it is on
        self._at = None                    # where the Mint cursor is, Quartz global (x, y)
        self._token = 0                    # each action's number: only the last one hides the cursor

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
        left (Quartz). Returns how long the caller should wait so the Mint
        cursor arrives (and the ripple starts) as the click does."""
        _glow(point=(x, y))
        if not self.enabled():
            return 0.0
        glide = self.glide_time(x, y)
        AppHelper.callAfter(self._click, x, y, label, glide)
        return glide + 0.06

    def glide_time(self, x: float, y: float) -> float:
        """Seconds the Mint cursor takes to glide to (x, y): quick for a short hop, never slow."""
        if self._at is None or _still():
            return CURSOR_APPEAR
        return glide_seconds(self._at, (x, y))

    def drag(self, x0: float, y0: float, x1: float, y1: float, seconds: float = 0.6, label: str = "") -> float:
        """A drag from (x0, y0) to (x1, y1) (Quartz): the cursor goes to the start, presses, and draws a
        fading trail to the end. Returns how long until it has reached the start."""
        _glow(point=(x0, y0), seconds=max(2.5, seconds + 1.0))
        if not self.enabled():
            return 0.0
        glide = self.glide_time(x0, y0)
        AppHelper.callAfter(self._drag, x0, y0, x1, y1, seconds, label, glide)
        return glide + 0.06

    def highlight(self, x: float, y: float, w: float, h: float, seconds: float = 2.2,
                  label: str = "") -> None:
        _glow(point=(x + w / 2, y + h / 2), seconds=max(2.5, seconds))
        if self.enabled() and w > 3 and h > 3:
            AppHelper.callAfter(self._highlight, x, y, w, h, seconds, label)

    def highlight_focused(self, seconds: float = 2.2, label: str = "") -> None:
        """Outline whatever has keyboard focus: the field about to be typed into."""
        _glow(seconds=max(2.5, seconds))      # typing goes to the front app's window
        if not self.enabled():
            return
        frame = focused_frame()
        if frame is not None:
            self.highlight(*frame, seconds=seconds, label=label)
            AppHelper.callAfter(self._typing, frame[0] + min(frame[2] - 4, 18), frame[1] + frame[3] / 2,
                                seconds, label or "Typing")

    def scroll(self, x: float, y: float, direction: str) -> None:
        """The Mint cursor goes to the pane being scrolled and shows the way it goes."""
        _glow(point=(x, y))
        if self.enabled():
            AppHelper.callAfter(self._scroll, x, y, direction)

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

    def _click(self, x, y, label, glide=0.0):
        found = self._locate(x, y)
        if found is None:
            return
        root, end, _, _ = found
        self._move_cursor(found, x, y, glide, label)
        self._press(glide)
        self._ripple(root, end, gfx.accent(), delay=glide, radius=9, rings=2)

    # --- the Mint cursor (main thread) -----------------------------------------------------

    def _cursor_on(self, root):
        """The cursor's layers, on `root`'s screen (made, or moved there, as needed)."""
        if self._cursor is not None and self._cursor[0] is root:
            return self._cursor
        if self._cursor is not None:
            for layer in self._cursor[1:]:
                layer.removeFromSuperlayer()
        rgb = gfx.accent()
        arrow = Quartz.CAShapeLayer.layer()
        arrow.setBounds_(Quartz.CGRectMake(0, 0, 15 * ARROW, 22 * ARROW))
        arrow.setAnchorPoint_(Quartz.CGPointMake(0.0, 1.0))           # the tip is the point
        arrow.setPath_(_arrow_path())
        arrow.setFillColor_(gfx.cg(rgb))
        arrow.setStrokeColor_(gfx.cg((1.0, 1.0, 1.0), 0.95))
        arrow.setLineWidth_(1.4)
        arrow.setLineJoin_(Quartz.kCALineJoinRound)
        arrow.setShadowColor_(gfx.cg(rgb))
        arrow.setShadowOpacity_(0.55)
        arrow.setShadowRadius_(5)
        arrow.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        arrow.setOpacity_(0)
        arrow.setZPosition_(10)
        label = Quartz.CATextLayer.layer()
        label.setFontSize_(11)
        label.setFont_(AppKit.NSFont.systemFontOfSize_weight_(11, AppKit.NSFontWeightSemibold))
        label.setForegroundColor_(gfx.cg((1.0, 1.0, 1.0)))
        label.setBackgroundColor_(gfx.cg((0.08, 0.09, 0.1), 0.82))
        label.setBorderColor_(gfx.cg(rgb, 0.9))
        label.setBorderWidth_(1.0)
        label.setCornerRadius_(9)
        label.setAlignmentMode_(Quartz.kCAAlignmentCenter)
        label.setAnchorPoint_(Quartz.CGPointMake(0.0, 1.0))
        label.setOpacity_(0)
        label.setZPosition_(9)
        try:
            label.setContentsScale_(AppKit.NSScreen.mainScreen().backingScaleFactor())
        except Exception:
            label.setContentsScale_(2.0)
        root.addSublayer_(arrow)
        root.addSublayer_(label)
        self._cursor = (root, arrow, label)
        return self._cursor

    def _move_cursor(self, found, x, y, glide, label=""):
        """Glide the cursor to (x, y) over `glide` s (it fades in where it is first needed), show what it is
        doing in a small tag beside it, and fade it out a while after the last action."""
        root, (lx, ly), _, _ = found
        _, arrow, tag = self._cursor_on(root)
        self._token += 1
        token = self._token
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        start = arrow.position() if arrow.opacity() > 0 and self._at is not None else None
        arrow.setPosition_(Quartz.CGPointMake(lx, ly))
        arrow.setOpacity_(1.0)
        Quartz.CATransaction.commit()
        if start is not None and glide > 0.02 and not _still():
            move = Quartz.CABasicAnimation.animationWithKeyPath_("position")
            move.setFromValue_(AppKit.NSValue.valueWithPoint_(start))
            move.setToValue_(AppKit.NSValue.valueWithPoint_(Quartz.CGPointMake(lx, ly)))
            move.setDuration_(glide)
            move.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithControlPoints____(0.3, 0.0, 0.15, 1.0))
            arrow.addAnimation_forKey_(move, "glide")
        else:
            appear = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            appear.setFromValue_(0.0)
            appear.setToValue_(1.0)
            appear.setDuration_(min(glide, CURSOR_APPEAR) or 0.12)
            arrow.addAnimation_forKey_(appear, "appear")
            if not _still():
                grow = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
                grow.setFromValue_(0.4)
                grow.setToValue_(1.0)
                grow.setDuration_(CURSOR_APPEAR)
                grow.setTimingFunction_(_ease(Quartz.kCAMediaTimingFunctionEaseOut))
                arrow.addAnimation_forKey_(grow, "grow")
        self._at = (x, y)
        self._tag(tag, label, lx, ly, glide)
        AppHelper.callLater(glide + CURSOR_LINGER, lambda: self._hide_cursor(token))

    def _tag(self, tag, text, lx, ly, delay):
        text = " ".join(str(text or "").split())[:32]
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        if not text:
            tag.setOpacity_(0)
            Quartz.CATransaction.commit()
            return
        tag.setString_(text)
        width = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            text, {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(
                11, AppKit.NSFontWeightSemibold)}).size().width + 18
        tag.setBounds_(Quartz.CGRectMake(0, 0, width, 18))
        tag.setPosition_(Quartz.CGPointMake(lx + 19, ly - 22))
        tag.setOpacity_(0.0)
        Quartz.CATransaction.commit()
        show = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        show.setValues_([0.0, 1.0, 1.0, 0.0])
        show.setKeyTimes_([0.0, 0.15, 0.8, 1.0])
        show.setDuration_(1.6)
        show.setBeginTime_(Quartz.CACurrentMediaTime() + delay)
        show.setFillMode_(Quartz.kCAFillModeBackwards)
        tag.addAnimation_forKey_(show, "show")

    def _press(self, delay):
        """The cursor dips as it presses."""
        if self._cursor is None or _still():
            return
        arrow = self._cursor[1]
        press = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
        press.setValues_([1.0, 0.78, 1.06, 1.0])
        press.setKeyTimes_([0.0, 0.35, 0.7, 1.0])
        press.setDuration_(0.3)
        press.setBeginTime_(Quartz.CACurrentMediaTime() + delay)
        arrow.addAnimation_forKey_(press, "press")

    def _hide_cursor(self, token):
        if token != self._token or self._cursor is None:
            return                      # another action came since: it stays
        arrow, tag = self._cursor[1], self._cursor[2]
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.5)
        arrow.setOpacity_(0.0)
        tag.setOpacity_(0.0)
        Quartz.CATransaction.commit()
        self._at = None

    def _typing(self, x, y, seconds, label):
        """Typing: the cursor waits at the field, a caret blinks beside it and three dots ripple in its tag."""
        found = self._locate(x, y)
        if found is None:
            return
        glide = self.glide_time(x, y)
        self._move_cursor(found, x, y, glide, "")
        root, (lx, ly), _, _ = found
        rgb = gfx.accent()
        chip = Quartz.CALayer.layer()
        chip.setBounds_(Quartz.CGRectMake(0, 0, 40, 16))
        chip.setAnchorPoint_(Quartz.CGPointMake(0.0, 0.5))
        chip.setPosition_(Quartz.CGPointMake(lx + 19, ly - 30))
        chip.setCornerRadius_(8)
        chip.setBackgroundColor_(gfx.cg((0.08, 0.09, 0.1), 0.82))
        chip.setBorderColor_(gfx.cg(rgb, 0.9))
        chip.setBorderWidth_(1.0)
        chip.setOpacity_(0)
        layers = [chip]
        for i in range(3):
            dot = Quartz.CALayer.layer()
            dot.setBounds_(Quartz.CGRectMake(0, 0, 5, 5))
            dot.setCornerRadius_(2.5)
            dot.setBackgroundColor_(gfx.cg(gfx.light(rgb)))
            dot.setPosition_(Quartz.CGPointMake(11 + i * 9, 8))
            if not _still():
                hop = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.translation.y")
                hop.setValues_([0.0, 2.5, 0.0, 0.0])
                hop.setKeyTimes_([0.0, 0.2, 0.4, 1.0])
                hop.setDuration_(0.9)
                hop.setRepeatCount_(float("inf"))
                hop.setBeginTime_(Quartz.CACurrentMediaTime() + i * 0.15)
                dot.addAnimation_forKey_(hop, "hop")
            chip.addSublayer_(dot)
        show = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        show.setValues_([0.0, 1.0, 1.0, 0.0])
        show.setKeyTimes_([0.0, 0.08, 0.9, 1.0])
        show.setDuration_(max(0.8, seconds))
        show.setBeginTime_(Quartz.CACurrentMediaTime() + glide)
        show.setFillMode_(Quartz.kCAFillModeBackwards)
        chip.addAnimation_forKey_(show, "show")
        root.addSublayer_(chip)
        self._later(glide + max(0.8, seconds) + 0.1, layers)
        # Keep the cursor at the field while the typing lasts.
        token = self._token
        AppHelper.callLater(glide + max(0.8, seconds) + CURSOR_LINGER, lambda: self._hide_cursor(token))

    def _scroll(self, x, y, direction):
        found = self._locate(x, y)
        if found is None:
            return
        glide = self.glide_time(x, y)
        self._move_cursor(found, x, y, glide, "")
        root, (lx, ly), _, _ = found
        if _still():
            return
        rgb = gfx.accent()
        down = str(direction).lower() in ("down", "d")
        up = str(direction).lower() in ("up", "u")
        dx, dy = (0, -1) if down else (0, 1) if up else ((1, 0) if str(direction).lower().startswith("r") else (-1, 0))
        layers = []
        for i in range(2):
            mark = Quartz.CAShapeLayer.layer()
            mark.setBounds_(Quartz.CGRectMake(0, 0, 12, 12))
            mark.setPosition_(Quartz.CGPointMake(lx - 10 + dx * 6, ly - 30 + dy * 6))
            mark.setPath_(_chevron_path(dx, dy))
            mark.setFillColor_(None)
            mark.setStrokeColor_(gfx.cg(gfx.light(rgb)))
            mark.setLineWidth_(2.0)
            mark.setLineCap_(Quartz.kCALineCapRound)
            mark.setOpacity_(0)
            go = Quartz.CABasicAnimation.animationWithKeyPath_("transform.translation")
            go.setFromValue_(AppKit.NSValue.valueWithSize_(AppKit.NSMakeSize(0, 0)))
            go.setToValue_(AppKit.NSValue.valueWithSize_(AppKit.NSMakeSize(dx * 14, dy * 14)))
            fade = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
            fade.setValues_([0.0, 0.95, 0.0])
            group = Quartz.CAAnimationGroup.animation()
            group.setAnimations_([go, fade])
            group.setDuration_(0.55)
            group.setBeginTime_(Quartz.CACurrentMediaTime() + glide + i * 0.14)
            mark.addAnimation_forKey_(group, "scroll")
            root.addSublayer_(mark)
            layers.append(mark)
        self._later(glide + 1.0, layers)

    def _drag(self, x0, y0, x1, y1, seconds, label, glide):
        found = self._locate(x0, y0)
        if found is None:
            return
        self._move_cursor(found, x0, y0, glide, label)
        self._press(glide)
        root, (lx0, ly0), frame, _ = found
        lx1, ly1 = x1 - frame.origin.x, (_primary_height() - y1) - frame.origin.y
        rgb = gfx.accent()
        begin = Quartz.CACurrentMediaTime() + glide + 0.12
        trail = Quartz.CAShapeLayer.layer()
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(path, None, lx0, ly0)
        Quartz.CGPathAddLineToPoint(path, None, lx1, ly1)
        trail.setPath_(path)
        trail.setFillColor_(None)
        trail.setStrokeColor_(gfx.cg(rgb, 0.85))
        trail.setLineWidth_(3.0)
        trail.setLineCap_(Quartz.kCALineCapRound)
        trail.setLineDashPattern_([2, 7])
        trail.setShadowColor_(gfx.cg(rgb))
        trail.setShadowOpacity_(0.6)
        trail.setShadowRadius_(4)
        trail.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        trail.setStrokeEnd_(0.0)
        draw = Quartz.CABasicAnimation.animationWithKeyPath_("strokeEnd")
        draw.setFromValue_(0.0)
        draw.setToValue_(1.0)
        draw.setDuration_(seconds)
        draw.setBeginTime_(begin)
        draw.setFillMode_(Quartz.kCAFillModeForwards)
        draw.setRemovedOnCompletion_(False)
        draw.setTimingFunction_(_ease())
        fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
        fade.setFromValue_(1.0)
        fade.setToValue_(0.0)
        fade.setDuration_(0.5)
        fade.setBeginTime_(begin + seconds + 0.25)
        fade.setFillMode_(Quartz.kCAFillModeForwards)
        fade.setRemovedOnCompletion_(False)
        trail.addAnimation_forKey_(draw, "draw")
        trail.addAnimation_forKey_(fade, "fade")
        root.addSublayer_(trail)
        arrow = self._cursor[1]
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        arrow.setPosition_(Quartz.CGPointMake(lx1, ly1))
        Quartz.CATransaction.commit()
        move = Quartz.CABasicAnimation.animationWithKeyPath_("position")
        move.setFromValue_(AppKit.NSValue.valueWithPoint_(Quartz.CGPointMake(lx0, ly0)))
        move.setToValue_(AppKit.NSValue.valueWithPoint_(Quartz.CGPointMake(lx1, ly1)))
        move.setDuration_(seconds)
        move.setBeginTime_(begin)
        move.setFillMode_(Quartz.kCAFillModeBackwards)
        move.setTimingFunction_(_ease())
        arrow.addAnimation_forKey_(move, "drag")
        self._at = (x1, y1)
        self._ripple(root, (lx1, ly1), rgb, delay=glide + 0.12 + seconds, radius=9, rings=1)
        self._later(glide + seconds + 1.2, [trail])
        token = self._token
        AppHelper.callLater(glide + seconds + CURSOR_LINGER, lambda: self._hide_cursor(token))

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


ARROW = 1.2              # the arrow's size against a 15 x 22 pt pointer
CURSOR_APPEAR = 0.18     # the cursor fading in where it is first needed
CURSOR_LINGER = 2.4      # how long it stays after Mint's last action


def glide_seconds(start, end) -> float:
    """A short hop is quick, a long one never slow: 0.16-0.34 s."""
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    return round(min(0.34, max(0.16, 0.12 + distance / 3200)), 3)


def _still() -> bool:
    try:
        from mint.ui import kinetics
        return bool(kinetics.reduce_motion())
    except Exception:
        return False


def _arrow_path():
    """A pointer arrow, tip at the top left of a 15 x 22 box (times ARROW), y up."""
    path = Quartz.CGPathCreateMutable()
    points = [(px * ARROW, py * ARROW) for px, py in
              [(0.5, 21.5), (0.5, 4.0), (4.8, 8.0), (7.8, 1.0), (10.8, 2.2), (7.9, 9.0), (13.8, 9.0)]]
    Quartz.CGPathMoveToPoint(path, None, *points[0])
    for p in points[1:]:
        Quartz.CGPathAddLineToPoint(path, None, *p)
    Quartz.CGPathCloseSubpath(path)
    return path


def _chevron_path(dx, dy):
    path = Quartz.CGPathCreateMutable()
    if dx == 0:
        Quartz.CGPathMoveToPoint(path, None, 1, 6 - 3 * dy)
        Quartz.CGPathAddLineToPoint(path, None, 6, 6 + 3 * dy)
        Quartz.CGPathAddLineToPoint(path, None, 11, 6 - 3 * dy)
    else:
        Quartz.CGPathMoveToPoint(path, None, 6 - 3 * dx, 1)
        Quartz.CGPathAddLineToPoint(path, None, 6 + 3 * dx, 6)
        Quartz.CGPathAddLineToPoint(path, None, 6 - 3 * dx, 11)
    return path


def _glow(**where) -> None:
    """Light up the window Mint is acting in (window_glow.py, its own setting); never in the way."""
    if not getattr(fx, "built", False):
        return                              # the --tool harness and tests: no UI
    try:
        from mint.ui import window_glow
        window_glow.glow(**where)
    except Exception:
        pass


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
