"""The status badge on Mint's face (plans/grokbot-notch-overhaul.md, phase 2).

A small coloured circle with a dark ring at the face's top-left, so what Mint is up to reads even
when the face is tiny (the notch):

    working    three dots cycling            thinking   the dots breathing slowly
    listening  a little mic (a pulsing dot when tiny)
    error      a solid red dot, pulses once  done       a green dot that pops
    idle       nothing (it scales away)

Hovering the face springs the circle into a pill with the word ("Working") and back.
Owned by the Orb (orb.status); main thread only. Pref status_badge.

    badge.set_base(kind)              # from the orb's state; None hides it
    badge.flash("done"|"error", secs) # a moment on top of the base, then back to it
    badge.hover(on)                   # the pill
    badge.tick(now)                   # 30 Hz, from Orb.tick: ends flashes
"""

from __future__ import annotations

import time

import AppKit
import Quartz

from mint.ui import gfx
from mint.ui import kinetics
from mint.core import prefs

# The state language, shared by the badge, the face tint and anyone who wants to match them.
COLORS = {
    "working": (0.22, 0.62, 1.00),
    "thinking": (0.62, 0.46, 1.00),
    "listening": (0.30, 0.86, 0.72),
    "error": (1.00, 0.28, 0.28),
    "done": (0.27, 0.86, 0.43),
}
WORDS = {"working": "Working", "thinking": "Thinking", "listening": "Listening", "error": "Error", "done": "Done"}
# Orb state -> badge kind (anything else: no badge).
FROM_STATE = {"working": "working", "thinking": "thinking", "awake": "listening"}

INK = (0.03, 0.05, 0.10)
RING = (0.02, 0.02, 0.03)


def kind_for(state: str) -> str | None:
    return FROM_STATE.get(state)


def _cg(rgb, alpha=1.0):
    return gfx.cg(rgb, alpha)


class StatusBadge:
    def __init__(self, orb) -> None:
        self.orb = orb
        d = orb.d
        self.h = h = max(7.0, round(d * 0.36 * 2) / 2)
        self.base = None            # from the state
        self.flash_kind = None      # done / error, for a moment
        self.flash_until = 0.0
        self.shown = None           # what is on screen now
        self.hovering = False
        self.expanded = False
        self.hidden_by = set()      # thumb mode, morphs...: reasons to keep it away
        self.anchor = self._home()

        # The pill: its right end stays put (anchor), it grows to the left. Bounds start left of
        # zero when wide, so the dots keep their place in the round end.
        pill = Quartz.CALayer.layer()
        pill.setAnchorPoint_(Quartz.CGPointMake(1.0, 0.5))
        pill.setBounds_(Quartz.CGRectMake(0, 0, h, h))
        pill.setCornerRadius_(h / 2)
        pill.setBorderWidth_(max(1.0, h * 0.11))
        pill.setBorderColor_(_cg(RING, 0.95))
        pill.setBackgroundColor_(_cg(COLORS["working"]))
        pill.setPosition_(Quartz.CGPointMake(self.anchor[0] + h / 2, self.anchor[1]))
        pill.setOpacity_(0.0)
        pill.setHidden_(True)
        pill.setMasksToBounds_(True)          # the word stays inside while the pill grows
        orb.body.addSublayer_(pill)
        self.layer = pill
        # The round badge's content (dots, mic) in one layer, so the pill can fade it as a whole.
        inner = Quartz.CALayer.layer()
        inner.setFrame_(Quartz.CGRectMake(0, 0, h, h))
        pill.addSublayer_(inner)
        self.inner = inner

        self.dots = []
        dot = max(1.3, h * 0.2)
        gap = h * 0.24
        for i in (-1, 0, 1):
            layer = Quartz.CALayer.layer()
            layer.setBounds_(Quartz.CGRectMake(0, 0, dot, dot))
            layer.setCornerRadius_(dot / 2)
            layer.setBackgroundColor_(_cg(INK, 0.88))
            layer.setPosition_(Quartz.CGPointMake(h / 2 + i * gap, h / 2))
            inner.addSublayer_(layer)
            self.dots.append(layer)

        # Listening: a mic (or one dot when the badge is too small for a glyph).
        g = h * 0.6
        mic = Quartz.CALayer.layer()
        mic.setBounds_(Quartz.CGRectMake(0, 0, g, g))
        mic.setPosition_(Quartz.CGPointMake(h / 2, h / 2))
        mic.setBackgroundColor_(_cg(INK, 0.88))
        if h >= 11:
            mask = Quartz.CALayer.layer()
            mask.setFrame_(Quartz.CGRectMake(0, 0, g, g))
            mask.setContents_(gfx.symbol("mic.fill", max(8.0, g * 2), "bold"))
            mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
            mic.setMask_(mask)
        else:
            small = h * 0.34
            mic.setBounds_(Quartz.CGRectMake(0, 0, small, small))
            mic.setCornerRadius_(small / 2)
        mic.setOpacity_(0.0)
        inner.addSublayer_(mic)
        self.mic = mic

        text = Quartz.CATextLayer.layer()
        self.font_size = max(7.0, h * 0.6)
        text.setFont_(AppKit.NSFont.systemFontOfSize_weight_(self.font_size, AppKit.NSFontWeightSemibold))
        text.setFontSize_(self.font_size)
        text.setForegroundColor_(_cg(INK, 0.92))
        text.setAlignmentMode_(Quartz.kCAAlignmentCenter)
        text.setContentsScale_(2.0)
        text.setOpacity_(0.0)
        pill.addSublayer_(text)
        self.text = text

    # --- geometry ---------------------------------------------------------------------

    def _home(self):
        """The badge's centre: on the face's rim at the top-left (body coordinates)."""
        bx, by = self.orb._bc
        k = 0.37 * self.orb.d
        return (bx - k, by + k)

    def move_to(self, point, animate: bool = True) -> None:
        """Follow the face's corner (the folder morph widens the face)."""
        self.anchor = point
        layer = self.layer
        old = layer.presentationLayer().position() if layer.presentationLayer() is not None else layer.position()
        new = (point[0] + self.h / 2 + self._shift(), point[1])
        if animate:
            kinetics.spring(layer, "position", (old.x, old.y), new, "snappy", anim_key="badge-move")
        else:
            layer.setPosition_(Quartz.CGPointMake(*new))

    def _shift(self) -> float:
        """How far right the pill must sit to stay inside the window (it grows to the left)."""
        if not self.expanded:
            return 0.0
        w = self._pill_width()
        orb = self.orb
        left_in_root = orb.center[0] - orb._bc[0] + self.anchor[0] + self.h / 2 - w
        return max(0.0, 3.0 - left_in_root)

    def _pill_width(self) -> float:
        word = WORDS.get(self.shown or "", "")
        font = AppKit.NSFont.systemFontOfSize_weight_(self.font_size, AppKit.NSFontWeightSemibold)
        size = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            word, {AppKit.NSFontAttributeName: font}).size()
        return max(self.h * 2, size.width + self.h * 1.1)

    # --- what it shows ------------------------------------------------------------------

    def enabled(self) -> bool:
        return (bool(getattr(self.orb, "primary", False)) and prefs.get("status_badge") is not False
                and not self.hidden_by)

    def set_base(self, kind: str | None) -> None:
        self.base = kind
        self._refresh()

    def flash(self, kind: str, seconds: float) -> None:
        self.flash_kind, self.flash_until = kind, time.monotonic() + seconds
        self._refresh(force=True)

    def current(self) -> str | None:
        """What the badge means right now (also the face tint's cue)."""
        if self.flash_kind and time.monotonic() < self.flash_until:
            return self.flash_kind
        return self.base

    def hide_for(self, reason: str, on: bool) -> None:
        (self.hidden_by.add if on else self.hidden_by.discard)(reason)
        self._refresh()

    def tick(self, now: float) -> None:
        if self.flash_kind and time.monotonic() >= self.flash_until:
            self.flash_kind = None
            self._refresh()
        elif (self.shown is not None) != self.enabled() and (self.current() is not None):
            self._refresh()                       # the pref or `primary` changed

    def _refresh(self, force: bool = False) -> None:
        want = self.current() if self.enabled() else None
        if want == self.shown and not force:
            return
        before, self.shown = self.shown, want
        layer = self.layer
        if want is None:
            if before is not None:
                self._collapse(animate=False)
                self._out()
            return
        rgb = COLORS[want]
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.25)
        layer.setBackgroundColor_(_cg(rgb))
        Quartz.CATransaction.commit()
        self._content(want)
        if before is None:
            layer.setHidden_(False)
            layer.removeAnimationForKey_("badge-out")
            layer.removeAnimationForKey_("badge-out-o")
            if kinetics.reduce_motion():
                kinetics.basic(layer, "opacity", 0.0, 1.0, 0.18, anim_key="badge-in-o")
                layer.setValue_forKeyPath_(1.0, "transform.scale")
            else:
                kinetics.pop(layer, "bouncy", start=0.2)
        else:
            layer.setOpacity_(1.0)
        if want == "error" and not kinetics.reduce_motion():
            self._keys(layer, "transform.scale", [1.0, 1.45, 0.9, 1.08, 1.0], 0.6, "badge-pulse")
        elif want == "done" and before is not None and not kinetics.reduce_motion():
            kinetics.spring(layer, "transform.scale", 0.5, 1.0, "bouncy", anim_key="badge-pop")
        if self.expanded:
            self._expand(animate=True)            # the word changes with the state
        elif self.hovering:
            self.hover(True)

    def _out(self) -> None:
        layer = self.layer
        old = layer.presentationLayer().opacity() if layer.presentationLayer() is not None else 1.0
        kinetics.basic(layer, "opacity", old, 0.0, 0.2, anim_key="badge-out-o")
        if not kinetics.reduce_motion():
            kinetics.basic(layer, "transform.scale", 1.0, 0.2, 0.22,
                           timing=Quartz.kCAMediaTimingFunctionEaseIn, anim_key="badge-out", keep=False)

    def _content(self, kind: str) -> None:
        """The inside of the round badge: dots, a mic, or nothing (a solid dot)."""
        reduce = kinetics.reduce_motion()
        for dot in self.dots:
            dot.removeAllAnimations()
        self.mic.removeAllAnimations()
        dots_on = kind in ("working", "thinking")
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.15)
        for dot in self.dots:
            dot.setOpacity_(1.0 if dots_on else 0.0)
        self.mic.setOpacity_(1.0 if kind == "listening" else 0.0)
        Quartz.CATransaction.commit()
        # Error and done: the badge is a plain dot, a size smaller.
        small = kind in ("error", "done")
        h = self.h * (0.78 if small else 1.0)
        if not self.expanded:
            self._set_bounds(h, h, animate=True)
        if reduce:
            return
        if kind == "working":
            for i, dot in enumerate(self.dots):
                self._keys(dot, "opacity", [0.4, 1.0, 0.4, 0.4], 0.9, "cycle-o", [0, 0.25, 0.5, 1], offset=i)
                self._keys(dot, "transform.scale", [0.75, 1.25, 0.75, 0.75], 0.9, "cycle-s",
                           [0, 0.25, 0.5, 1], offset=i)
        elif kind == "thinking":
            for i, dot in enumerate(self.dots):
                self._keys(dot, "opacity", [0.35, 1.0, 0.35], 1.8, "breathe-o", offset=i, step=0.22)
                self._keys(dot, "transform.scale", [0.85, 1.1, 0.85], 1.8, "breathe-s", offset=i, step=0.22)
        elif kind == "listening":
            self._keys(self.mic, "transform.scale", [1.0, 1.18, 1.0], 1.4, "listen")

    def _keys(self, layer, key, values, seconds, name, times=None, offset=0, step=0.15, repeat=True):
        anim = Quartz.CAKeyframeAnimation.animationWithKeyPath_(key)
        anim.setValues_(values)
        if times:
            anim.setKeyTimes_(times)
        anim.setDuration_(seconds)
        anim.setCalculationMode_(Quartz.kCAAnimationCubic)
        if repeat and name not in ("badge-pulse",):
            anim.setRepeatCount_(float("inf"))
            anim.setTimeOffset_(seconds - (offset * step) % seconds)    # staggered, no waiting at the start
        layer.addAnimation_forKey_(anim, name)

    # --- the pill ----------------------------------------------------------------------

    def hover(self, on: bool) -> None:
        on = bool(on)
        if on == self.hovering and on == self.expanded:
            return
        self.hovering = on
        want = on and self.shown is not None and not getattr(self.orb, "in_notch", False)
        if want and not self.expanded:
            self._expand(animate=True)
        elif not want and self.expanded:
            self._collapse(animate=True)

    def _set_bounds(self, w: float, h: float, animate: bool) -> None:
        layer = self.layer
        new = (-(w - self.h), (self.h - h) / 2, w, h)
        if animate:
            pres = layer.presentationLayer() or layer
            b = pres.bounds()
            kinetics.spring(layer, "bounds", (b.origin.x, b.origin.y, b.size.width, b.size.height), new,
                            "snappy", anim_key="badge-bounds")
            kinetics.spring(layer, "cornerRadius", pres.cornerRadius(), h / 2, "snappy", anim_key="badge-radius")
        else:
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setDisableActions_(True)
            layer.setBounds_(Quartz.CGRectMake(*new))
            layer.setCornerRadius_(h / 2)
            Quartz.CATransaction.commit()

    def _expand(self, animate: bool) -> None:
        self.expanded = True
        h = self.h
        w = self._pill_width()
        self._set_bounds(w, h, animate)
        self.move_to(self.anchor, animate)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.text.setString_(WORDS.get(self.shown or "", ""))
        th = self.font_size * 1.3
        self.text.setFrame_(Quartz.CGRectMake(-(w - h), (h - th) / 2 - self.font_size * 0.04, w, th))
        self.text.setOpacity_(1.0)
        Quartz.CATransaction.commit()
        kinetics.wipe_in(self.text, seconds=0.28, delay=0.06)
        kinetics.fade(self.inner, False, 0.1)

    def _collapse(self, animate: bool) -> None:
        self.expanded = False
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.12)
        self.text.setOpacity_(0.0)
        Quartz.CATransaction.commit()
        kind = self.shown
        small = kind in ("error", "done")
        h = self.h * (0.78 if small else 1.0)
        self._set_bounds(h, h, animate)
        self.move_to(self.anchor, animate)
        if kind is not None:
            self._content(kind)
        kinetics.fade(self.inner, True, 0.18, delay=0.08)
