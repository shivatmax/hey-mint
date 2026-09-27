"""Annotations drawn over any app: a box, an underline, a highlighter stroke,
a hand-drawn circle or an arrow - with an optional note beside it.

Used when Mint shows the user something ("where does it say the deadline?",
"underline the part you mean"). A transparent, click-through overlay covers
each display; it is excluded from screen capture, so the marks never confuse
Mint's own reading of the screen, and it never takes a click.

Coordinates are Quartz screen points (origin top left), the same units text
recognition returns. Every public function is safe from any thread.
"""

from __future__ import annotations

import math
import os
import random

import AppKit
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx

STYLES = ("box", "underline", "highlight", "circle", "arrow")
_SHARING = (AppKit.NSWindowSharingReadOnly if os.environ.get("MINT_CAPTURE")
            else AppKit.NSWindowSharingNone)


def _ease(name=Quartz.kCAMediaTimingFunctionEaseOut):
    return Quartz.CAMediaTimingFunction.functionWithName_(name)


class Marks:
    def __init__(self) -> None:
        self._overlays: list[tuple] = []     # (frame, window, root, scale)
        self._layers: list = []
        self._token = 0

    # --- overlay windows (main thread) --------------------------------------------

    def _ensure(self) -> None:
        if self._overlays:
            return
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
            window.setSharingType_(_SHARING)
            view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, frame.size.width, frame.size.height))
            view.setWantsLayer_(True)
            window.setContentView_(view)
            window.orderFrontRegardless()
            self._overlays.append((frame, window, view.layer(), screen.backingScaleFactor()))

    def _local(self, x: float, y: float, w: float, h: float):
        """Quartz rect (top-left origin) -> (root layer, Cocoa-local rect, scale)."""
        self._ensure()
        primary = AppKit.NSScreen.screens()[0].frame().size.height
        cx, cy = x, primary - y - h
        for frame, _, root, scale in self._overlays:
            if AppKit.NSPointInRect((cx + w / 2, cy + h / 2), frame):
                return root, Quartz.CGRectMake(cx - frame.origin.x, cy - frame.origin.y, w, h), scale
        frame, _, root, scale = self._overlays[0]
        return root, Quartz.CGRectMake(cx - frame.origin.x, cy - frame.origin.y, w, h), scale

    # --- public ---------------------------------------------------------------------

    def show(self, rects: list[tuple], style: str = "box", note: str = "", seconds: float = 10.0,
             clear: bool = True) -> None:
        """Mark one or more Quartz rects (e.g. each line of a sentence)."""
        style = style if style in STYLES else "box"
        AppHelper.callAfter(self._show, list(rects), style, note, seconds, clear)

    def clear(self) -> None:
        AppHelper.callAfter(self._clear)

    # --- drawing (main thread) ---------------------------------------------------------

    def _clear(self) -> None:
        self._token += 1
        for layer in self._layers:
            layer.removeFromSuperlayer()
        self._layers = []

    def _add(self, root, layer):
        root.addSublayer_(layer)
        self._layers.append(layer)
        return layer

    def _show(self, rects, style, note, seconds, clear) -> None:
        if not rects:
            return
        if clear:
            self._clear()
        self._token += 1
        token = self._token
        rgb = gfx.accent()
        ink = (1.0, 0.82, 0.18) if style == "highlight" else rgb
        first_root = first_rect = None
        for n, (x, y, w, h) in enumerate(rects):
            root, rect, scale = self._local(x, y, w, h)
            first_root = first_root or root
            first_rect = first_rect or rect
            getattr(self, f"_{style}")(root, rect, ink, delay=n * 0.12)
        if note:
            self._note(first_root, first_rect, note, style)
        AppHelper.callLater(seconds, lambda: self._fade(token))

    def _fade(self, token: int) -> None:
        if token != self._token:
            return
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.4)
        for layer in self._layers:
            layer.setOpacity_(0.0)
        Quartz.CATransaction.commit()
        AppHelper.callLater(0.45, lambda: token == self._token and self._clear())

    def _shape(self, path, rgb, width=3.0, fill=None, glow=True):
        shape = Quartz.CAShapeLayer.layer()
        shape.setPath_(path)
        shape.setFillColor_(gfx.cg(*fill) if fill else None)
        shape.setStrokeColor_(gfx.cg(rgb))
        shape.setLineWidth_(width)
        shape.setLineCap_(Quartz.kCALineCapRound)
        shape.setLineJoin_(Quartz.kCALineJoinRound)
        if glow:
            shape.setShadowColor_(gfx.cg(rgb))
            shape.setShadowOpacity_(0.9)
            shape.setShadowRadius_(6)
            shape.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        return shape

    def _draw_in(self, shape, delay=0.0, duration=0.45):
        draw = Quartz.CABasicAnimation.animationWithKeyPath_("strokeEnd")
        draw.setFromValue_(0.0)
        draw.setToValue_(1.0)
        draw.setDuration_(duration)
        draw.setTimingFunction_(_ease(Quartz.kCAMediaTimingFunctionEaseInEaseOut))
        if delay:
            draw.setBeginTime_(Quartz.CACurrentMediaTime() + delay)
            draw.setFillMode_(Quartz.kCAFillModeBackwards)
        shape.addAnimation_forKey_(draw, "draw")

    def _box(self, root, rect, rgb, delay=0.0):
        r = Quartz.CGRectInset(rect, -6, -5)
        shape = self._shape(Quartz.CGPathCreateWithRoundedRect(r, 7, 7, None), rgb, 3.0,
                            fill=(rgb, 0.10))
        self._add(root, shape)
        self._draw_in(shape, delay, 0.5)
        pulse = Quartz.CAKeyframeAnimation.animationWithKeyPath_("shadowRadius")
        pulse.setValues_([6.0, 12.0, 6.0])
        pulse.setDuration_(1.6)
        pulse.setRepeatCount_(float("inf"))
        shape.addAnimation_forKey_(pulse, "pulse")

    def _underline(self, root, rect, rgb, delay=0.0):
        x, y, w = rect.origin.x, rect.origin.y - 3, rect.size.width
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(path, None, x - 2, y)
        # A slightly hand-drawn line: tiny waves along the way.
        steps = max(2, int(w / 24))
        for i in range(1, steps + 1):
            px = x - 2 + (w + 4) * i / steps
            Quartz.CGPathAddQuadCurveToPoint(path, None, px - (w + 4) / steps / 2, y + (1.2 if i % 2 else -1.2), px, y)
        shape = self._shape(path, rgb, 3.2)
        self._add(root, shape)
        self._draw_in(shape, delay, 0.35 + min(w, 600) / 1200)

    def _highlight(self, root, rect, rgb, delay=0.0):
        r = Quartz.CGRectInset(rect, -3, -2)
        band = Quartz.CALayer.layer()
        band.setFrame_(r)
        band.setBackgroundColor_(gfx.cg(rgb, 0.38))
        band.setCornerRadius_(3)
        try:
            band.setCompositingFilter_("multiplyBlendMode")
        except Exception:
            pass
        band.setAnchorPoint_(Quartz.CGPointMake(0, 0.5))
        band.setPosition_(Quartz.CGPointMake(r.origin.x, r.origin.y + r.size.height / 2))
        self._add(root, band)
        sweep = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale.x")
        sweep.setFromValue_(0.0)
        sweep.setToValue_(1.0)
        sweep.setDuration_(0.4 + min(r.size.width, 600) / 1500)
        sweep.setTimingFunction_(_ease())
        if delay:
            sweep.setBeginTime_(Quartz.CACurrentMediaTime() + delay)
            sweep.setFillMode_(Quartz.kCAFillModeBackwards)
        band.addAnimation_forKey_(sweep, "sweep")

    def _circle(self, root, rect, rgb, delay=0.0):
        cx, cy = rect.origin.x + rect.size.width / 2, rect.origin.y + rect.size.height / 2
        rx, ry = rect.size.width / 2 + 14, rect.size.height / 2 + 12
        path = Quartz.CGPathCreateMutable()
        wobble = random.uniform(-0.2, 0.2)
        for i in range(0, 73):                       # a little more than once round, like a pen
            a = 2 * math.pi * i / 64 + wobble
            k = 1 + 0.04 * math.sin(i * 0.7)
            px, py = cx + rx * k * math.cos(a), cy + ry * k * math.sin(a)
            if i == 0:
                Quartz.CGPathMoveToPoint(path, None, px, py)
            else:
                Quartz.CGPathAddLineToPoint(path, None, px, py)
        shape = self._shape(path, rgb, 3.0)
        self._add(root, shape)
        self._draw_in(shape, delay, 0.6)

    def _arrow(self, root, rect, rgb, delay=0.0):
        # From up-left of the target, curving in to point at its left edge.
        tx, ty = rect.origin.x - 6, rect.origin.y + rect.size.height / 2
        sx, sy = tx - 90, ty + 70
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(path, None, sx, sy)
        Quartz.CGPathAddQuadCurveToPoint(path, None, sx + 10, ty + 8, tx, ty)
        head = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(head, None, tx - 13, ty + 9)
        Quartz.CGPathAddLineToPoint(head, None, tx, ty)
        Quartz.CGPathAddLineToPoint(head, None, tx - 14, ty - 6)
        shaft = self._shape(path, rgb, 3.4)
        tip = self._shape(head, rgb, 3.4)
        self._add(root, shaft)
        self._add(root, tip)
        self._draw_in(shaft, delay, 0.45)
        tip.setOpacity_(0.0)
        AppHelper.callLater(delay + 0.4, lambda: tip.setOpacity_(1.0) if tip in self._layers else None)
        self._box(root, rect, rgb, delay + 0.3)

    def _note(self, root, rect, text, style):
        font = AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightSemibold)
        size = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            text, {AppKit.NSFontAttributeName: font}).size()
        w, h = min(size.width + 24, 420), 26
        x = rect.origin.x - 6
        y = rect.origin.y + rect.size.height + 12        # above the mark
        if style == "arrow":
            x, y = rect.origin.x - 96 - w / 2, rect.origin.y + rect.size.height / 2 + 76
        pill = Quartz.CALayer.layer()
        pill.setFrame_(Quartz.CGRectMake(x, y, w, h))
        pill.setCornerRadius_(h / 2)
        pill.setBackgroundColor_(gfx.cg((0.07, 0.08, 0.11), 0.92))
        pill.setBorderColor_(gfx.cg(gfx.accent(), 0.8))
        pill.setBorderWidth_(1)
        pill.setShadowOpacity_(0.4)
        pill.setShadowRadius_(8)
        pill.setShadowOffset_(Quartz.CGSizeMake(0, -2))
        label = Quartz.CATextLayer.layer()
        label.setString_(text)
        label.setFont_(font)
        label.setFontSize_(13)
        label.setForegroundColor_(gfx.cg((1, 1, 1), 0.96))
        label.setAlignmentMode_(Quartz.kCAAlignmentCenter)
        label.setTruncationMode_(Quartz.kCATruncationEnd)
        label.setContentsScale_(2.0)
        label.setFrame_(Quartz.CGRectMake(10, (h - 17) / 2 - 1, w - 20, 17))
        pill.addSublayer_(label)
        self._add(root, pill)
        pop = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
        pop.setValues_([0.6, 1.06, 1.0])
        pop.setDuration_(0.3)
        pill.addAnimation_forKey_(pop, "pop")


marks = Marks()
