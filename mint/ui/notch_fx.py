"""Scenes for the open notch's body (plans/grokbot-notch-overhaul.md, phases 2, 3 and 5).

Each scene is one self-contained view, sized to the island's body by the caller, and a small controller:

    view, c = greeting(w, h, on_done)                  # sparkle streams, warm glow, Mint drops in and waves (~3.2 s)
    view, c = error_card(w, h, title, reason, on_retry) # sad red Mint, ghost triangles, a round retry button
    view, c = drop_zone(w, h)                           # dashed marching border; c.hover(on), c.dropped()
    view, c = progress_bar(w, h, title)                 # Mint rides the bar; c.set(fraction), c.finish(ok)
    should_greet()                                      # first wake of the calendar day, and the pref is on

A scene starts by itself when its view lands in a window (or call c.start()); c.close() stops it and takes
the view out. Every scene owns a little Mint (a real orb.Orb, so it blinks and keeps its ω mouth); the new
orb verbs - wave(), morph("folder"), expression("dash"), thumb_mode(on) - are used when orb.py has them,
otherwise these scenes draw their own hands, folder lid and flat eyes.

All motion goes through kinetics: under Reduce motion there are fades only - no travel, bounce or particles.
Main thread.
"""

from __future__ import annotations

import datetime
import logging
import math
import time
from pathlib import Path

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx
from mint.ui import kinetics
from mint.core import prefs

log = logging.getLogger("mint.ui.notch_fx")

# The notch palette (the Grok Bot study): black island, inner cards with a faint hairline.
CARD = (0.086, 0.086, 0.094)          # #161618
HAIRLINE = 0.06                       # white alpha of the card's 1 px edge
RADIUS = 20.0
RED = (0.90, 0.28, 0.30)              # #E5484D
REASON_RED = (1.0, 0.43, 0.43)        # red that still reads as small text on the dark card
ORANGE = (0.98, 0.50, 0.24)
GREEN = (0.36, 0.84, 0.50)
TEAL = (0.30, 0.79, 0.70)
WARM = (1.0, 0.83, 0.60)

GREETED = Path.home() / "Library" / "Application Support" / "Mint" / "greeted.txt"   # tests monkeypatch this


def _sfx(name: str) -> None:
    try:
        from mint.ui import sfx
        sfx.play(name)
    except Exception:
        pass


def _white(alpha=1.0):
    return AppKit.NSColor.colorWithWhite_alpha_(1.0, alpha)


def _cgw(alpha=1.0):
    return AppKit.NSColor.colorWithWhite_alpha_(1.0, alpha).CGColor()


def _quiet(fn):
    """Run fn() inside a transaction with no implicit animations."""
    Quartz.CATransaction.begin()
    Quartz.CATransaction.setDisableActions_(True)
    try:
        fn()
    finally:
        Quartz.CATransaction.commit()


def _later(scene, seconds, fn):
    """fn() after `seconds`, unless the scene was closed (or restarted) meanwhile."""
    run = scene._run

    def go():
        if scene.alive and scene._run == run:
            try:
                fn()
            except Exception:
                log.debug("notch scene step failed", exc_info=True)
    AppHelper.callLater(seconds, go)


def _keys(layer, key, values, seconds, times=None, additive=False, repeat=0.0, anim_key=None):
    anim = Quartz.CAKeyframeAnimation.animationWithKeyPath_(key)
    anim.setValues_(values)
    if times:
        anim.setKeyTimes_(times)
    anim.setDuration_(seconds)
    anim.setAdditive_(additive)
    if repeat:
        anim.setRepeatCount_(repeat)
    anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
        Quartz.kCAMediaTimingFunctionEaseInEaseOut))
    layer.addAnimation_forKey_(anim, anim_key or key)
    return anim


def _spin_forever(layer, seconds, clockwise=True, key="fx-spin"):
    turn = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
    turn.setFromValue_(0.0)
    turn.setToValue_((-2 if clockwise else 2) * math.pi)
    turn.setDuration_(seconds)
    turn.setRepeatCount_(float("inf"))
    layer.addAnimation_forKey_(turn, key)


def _radial(frame, rgb, stops):
    """A soft radial glow filling `frame`: stops = [(alpha, location), ...] from the centre out."""
    g = Quartz.CAGradientLayer.layer()
    g.setType_(Quartz.kCAGradientLayerRadial)
    g.setFrame_(frame)
    g.setStartPoint_(Quartz.CGPointMake(0.5, 0.5))
    g.setEndPoint_(Quartz.CGPointMake(1.0, 1.0))
    g.setColors_([gfx.cg(rgb, a) for a, _ in stops])
    g.setLocations_([loc for _, loc in stops])
    return g


def _linear(frame, colors, start=(0.5, 0.0), end=(0.5, 1.0), locations=None):
    g = Quartz.CAGradientLayer.layer()
    g.setFrame_(frame)
    g.setStartPoint_(Quartz.CGPointMake(*start))
    g.setEndPoint_(Quartz.CGPointMake(*end))
    g.setColors_(colors)
    if locations:
        g.setLocations_(locations)
    return g


def _corner_glow(parent, w, h, rgb, strength=1.0):
    """Light rising from the card's bottom corners (error: red/orange, done/upload: green)."""
    holder = Quartz.CALayer.layer()
    holder.setFrame_(Quartz.CGRectMake(0, 0, w, h))
    gw, gh = max(w * 0.62, 180), h * 1.5
    for x in (0.0, w):
        holder.addSublayer_(_radial(Quartz.CGRectMake(x - gw / 2, -gh / 2, gw, gh), rgb,
                                    [(0.55 * strength, 0.0), (0.22 * strength, 0.45), (0.0, 1.0)]))
    holder.addSublayer_(_linear(Quartz.CGRectMake(0, 0, w, h * 0.55),
                                [gfx.cg(rgb, 0.16 * strength), gfx.cg(rgb, 0.0)]))
    parent.addSublayer_(holder)
    return holder


def _dot_image(px=8):
    image = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(
        AppKit.NSMakeSize(px, px), False,
        lambda rect: (AppKit.NSColor.whiteColor().set(),
                      AppKit.NSBezierPath.bezierPathWithOvalInRect_(rect).fill(), True)[-1])
    return gfx.cg_image(image)


def _bar_image(w=8, h=3):
    image = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(
        AppKit.NSMakeSize(w, h), False,
        lambda rect: (AppKit.NSColor.whiteColor().set(),
                      AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 1, 1).fill(), True)[-1])
    return gfx.cg_image(image)


def _rounded_triangle(size, corner):
    """An equilateral triangle pointing up, corners rounded, centred in a size x size box."""
    r = size / 2
    c = (size / 2, size / 2 - size * 0.06)
    pts = [(c[0] + r * math.cos(a), c[1] + r * math.sin(a))
           for a in (math.pi / 2, math.pi / 2 + 2 * math.pi / 3, math.pi / 2 + 4 * math.pi / 3)]
    p = Quartz.CGPathCreateMutable()
    mid = ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)
    Quartz.CGPathMoveToPoint(p, None, *mid)
    for i in (1, 2, 0):
        a, b = pts[i], pts[(i + 1) % 3]
        Quartz.CGPathAddArcToPoint(p, None, a[0], a[1], b[0], b[1], corner)
    Quartz.CGPathCloseSubpath(p)
    return p


# --- AppKit pieces ------------------------------------------------------------------------------

class MintFxSceneView(AppKit.NSView):
    """A scene's root view: starts the scene when it lands in a window, pauses its ticking when it leaves."""
    scene = None

    def acceptsFirstMouse_(self, event):
        return True

    def viewDidMoveToWindow(self):
        scene = self.scene
        if scene is None:
            return
        if self.window() is not None:
            scene._appeared()
        else:
            scene._idle()

    def viewDidUnhide(self):
        scene = self.scene
        if scene is not None:
            scene._unhidden()


class MintFxLabel(AppKit.NSTextField):
    """Text that never takes a click (the notch's own rule: faded labels sat over buttons)."""

    def hitTest_(self, point):
        return None


class MintFxButton(AppKit.NSButton):
    def acceptsFirstMouse_(self, event):
        return True


class MintFxAct(AppKit.NSObject):
    def initWithFn_(self, fn):
        self = objc.super(MintFxAct, self).init()
        if self is None:
            return None
        self.fn = fn
        return self

    def fire_(self, sender):
        try:
            self.fn()
        except Exception:
            log.exception("notch scene button failed")


class MintFxTicker(AppKit.NSObject):
    def tick_(self, timer):
        _tick_all()


_live: list = []
_clock = {"timer": None, "target": None}


def _tick_all() -> None:
    now = time.monotonic()
    for scene in list(_live):
        try:
            scene._tick(now)
        except Exception:
            log.debug("notch scene tick failed", exc_info=True)
    if not _live and _clock["timer"] is not None:
        _clock["timer"].invalidate()
        _clock["timer"] = None


def _ticking(scene, on: bool) -> None:
    if on and scene not in _live:
        _live.append(scene)
        if _clock["timer"] is None:
            target = _clock["target"] = _clock["target"] or MintFxTicker.alloc().init()
            timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                1 / 30, target, "tick:", None, True)
            AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, AppKit.NSRunLoopCommonModes)
            _clock["timer"] = timer
    elif not on and scene in _live:
        _live.remove(scene)


def label(text, size, weight=AppKit.NSFontWeightRegular, color=None, lines=1):
    field = MintFxLabel.wrappingLabelWithString_(text or "")
    field.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, weight))
    field.setTextColor_(color or _white(0.92))
    field.setMaximumNumberOfLines_(lines)
    field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail if lines == 1 else AppKit.NSLineBreakByWordWrapping)
    if lines > 1:
        field.cell().setTruncatesLastVisibleLine_(True)
    field.setSelectable_(False)
    field.setDrawsBackground_(False)
    field.setBezeled_(False)
    return field


def _fit_height(field, width) -> float:
    size = field.cell().cellSizeForBounds_(AppKit.NSMakeRect(0, 0, width, 1000))
    return math.ceil(size.height)


def _text_width(text, size, weight) -> float:
    font = AppKit.NSFont.systemFontOfSize_weight_(size, weight)
    s = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, {AppKit.NSFontAttributeName: font})
    return math.ceil(s.size().width) + 6


# --- the little Mint in a scene -----------------------------------------------------------------

class Face:
    """A real Mint (orb.Orb) inside a scene, in a holder layer the scene can move, fade and scale without
    fighting the orb's own breathing. Uses the new orb verbs when orb.py has them, else draws its own."""

    def __init__(self, parent, center, d: float, state: str = "awake") -> None:
        self.d = d
        size = d * 2.6
        holder = Quartz.CALayer.layer()
        holder.setBounds_(Quartz.CGRectMake(0, 0, size, size))
        holder.setPosition_(Quartz.CGPointMake(*center))
        parent.addSublayer_(holder)
        self.layer = holder
        self.c = (size / 2, size / 2)
        from mint.ui.orb import Orb
        orb = Orb(holder, self.c, d)
        orb.in_notch = True
        orb.apply_state(state)
        for layer in (orb.ring, orb.spinner, orb.progress_ring, orb.badge):
            layer.removeAllAnimations()
            layer.setHidden_(True)
        self.orb = orb
        self.look = None
        self._hands = None
        self._lid = None
        self._tint = None
        self._dash = None
        self._dot = None
        self.tick(time.monotonic())             # eyes in place before the first frame

    def _verb(self, name, *args) -> bool:
        fn = getattr(self.orb, name, None)
        if not callable(fn):
            return False
        try:
            fn(*args)
            return True
        except Exception:
            log.debug("orb.%s failed", name, exc_info=True)
            return False

    def tick(self, now: float) -> None:
        self.orb.tick(now, 0.0, self.look, False, True)       # visible_work: no random hops mid-scene

    def blink(self) -> None:
        self.orb.blink()

    def hop(self) -> None:
        self.orb.hop()

    def shake(self) -> None:
        kinetics.shake(self.layer, px=4.0)

    # -- hands --

    def wave(self, seconds: float = 1.3) -> None:
        if self._verb("wave"):
            return
        if kinetics.reduce_motion():
            return
        d = self.d
        if self._hands is None:
            self._hands = []
            for side in (-1, 1):
                arm = Quartz.CALayer.layer()
                arm.setBounds_(Quartz.CGRectMake(0, 0, 0, 0))
                arm.setPosition_(Quartz.CGPointMake(*self.c))
                hand = Quartz.CAGradientLayer.layer()
                hd = d * 0.27
                hand.setBounds_(Quartz.CGRectMake(0, 0, hd, hd))
                hand.setCornerRadius_(hd / 2)
                hand.setPosition_(Quartz.CGPointMake(side * d * 0.66, -d * 0.2))
                hand.setColors_([_cgw(0.80), _cgw(0.98)])        # lit from above, like the orb's gloss
                hand.setShadowColor_(AppKit.NSColor.blackColor().CGColor())
                hand.setShadowOpacity_(0.35)
                hand.setShadowRadius_(2.5)
                hand.setShadowOffset_(Quartz.CGSizeMake(0, -1))
                hand.setOpacity_(0.0)
                arm.addSublayer_(hand)
                self.layer.addSublayer_(arm)
                self._hands.append((side, arm, hand))
        for i, (side, arm, hand) in enumerate(self._hands):
            kinetics.pop(hand, "bouncy", 0.2, delay=i * 0.06)
            # The hand swings on an arc round the face: up and out, back, up again (a wave, not a spin).
            a = 0.42
            _keys(arm, "transform.rotation.z",
                  [0, -side * a, -side * a * 0.25, -side * a, -side * a * 0.25, -side * a * 0.6, 0],
                  seconds, [0, 0.18, 0.36, 0.54, 0.72, 0.88, 1], anim_key="fx-wave")

    def hands_away(self) -> None:
        for _, arm, hand in self._hands or []:
            kinetics.basic(hand, "opacity", hand.opacity(), 0.0, 0.22, anim_key="fx-hand-o")
            kinetics.basic(hand, "transform.scale", 1.0, 0.3, 0.22, anim_key="fx-hand-s")

    # -- shapes --

    def morph(self, shape) -> None:
        """'folder': a rounded window with a dark lid and the same eyes; None: back to the ball."""
        if self._verb("morph", shape):
            return
        d, core = self.d, self.orb.core
        if shape:
            if self._lid is None:
                lid = Quartz.CALayer.layer()
                lid.setFrame_(Quartz.CGRectMake(0, d * 0.76, d, d * 0.24))
                lid.setBackgroundColor_(gfx.cg((0.04, 0.06, 0.12), 0.92))
                lid.setOpacity_(0.0)
                core.insertSublayer_below_(lid, self.orb.face)
                self._lid = lid
            self.orb._reshape(d * 0.24)
            kinetics.basic(self._lid, "opacity", self._lid.opacity(), 1.0, 0.22, delay=0.05, anim_key="fx-lid")
            self._face_y(-d * 0.08)
        else:
            self.orb._reshape(d / 2)
            if self._lid is not None:
                kinetics.basic(self._lid, "opacity", self._lid.opacity(), 0.0, 0.18, anim_key="fx-lid")
            self._face_y(0.0)

    def _face_y(self, dy: float) -> None:
        face = self.orb.face
        old = face.presentationLayer().position() if face.presentationLayer() is not None else face.position()
        new = (self.d / 2, self.d / 2 + dy)
        kinetics.spring(face, "position", (old.x, old.y), new, "snappy", anim_key="fx-face-y")

    def thumb(self, on: bool) -> None:
        self._verb("thumb_mode", on)

    # -- expressions --

    def tint(self, rgb, on: bool = True) -> None:
        """The state colour rising through the lower half of the ball (error red, done green)."""
        d, core = self.d, self.orb.core
        if self._tint is None:
            t = _linear(Quartz.CGRectMake(0, 0, d, d), [gfx.cg(rgb, 0.92), gfx.cg(rgb, 0.5), gfx.cg(rgb, 0.0)],
                        locations=[0.0, 0.45, 0.9])
            t.setOpacity_(0.0)
            core.insertSublayer_below_(t, self.orb.face)
            self._tint = t
        else:
            self._tint.setColors_([gfx.cg(rgb, 0.92), gfx.cg(rgb, 0.5), gfx.cg(rgb, 0.0)])
        kinetics.basic(self._tint, "opacity", self._tint.opacity(), 1.0 if on else 0.0, 0.35, anim_key="fx-tint")

    def status_dot(self, rgb) -> None:
        """A solid state dot at the face's top-left (the Grok Bot badge)."""
        d = self.d
        if self._dot is None:
            dot = Quartz.CALayer.layer()
            dd = max(6.0, d * 0.2)
            dot.setBounds_(Quartz.CGRectMake(0, 0, dd, dd))
            dot.setCornerRadius_(dd / 2)
            dot.setPosition_(Quartz.CGPointMake(self.c[0] - d * 0.36, self.c[1] + d * 0.36))
            dot.setBorderWidth_(1.5)
            dot.setBorderColor_(gfx.cg(CARD))
            self.layer.addSublayer_(dot)
            self._dot = dot
        self._dot.setBackgroundColor_(gfx.cg(rgb))
        kinetics.pop(self._dot, "bouncy", 0.3, delay=0.15)

    def expression(self, name) -> None:
        """'dash': flat tired eyes and a small frown (error); None: Mint's own face back."""
        if self._verb("expression", name):
            return
        orb, s = self.orb, self.orb._face_scale
        if name == "dash":
            if self._dash is None:
                frown = Quartz.CAShapeLayer.layer()
                p = Quartz.CGPathCreateMutable()
                Quartz.CGPathMoveToPoint(p, None, -3.2 * s, 0)
                Quartz.CGPathAddQuadCurveToPoint(p, None, 0, 2.6 * s, 3.2 * s, 0)
                frown.setPath_(p)
                frown.setFillColor_(None)
                frown.setStrokeColor_(gfx.cg((0.04, 0.06, 0.12), 0.82))
                frown.setLineWidth_(1.5 * s)
                frown.setLineCap_(Quartz.kCALineCapRound)
                frown.setPosition_(Quartz.CGPointMake(self.d / 2, self.d / 2 - 7.5 * s))
                orb.face.addSublayer_(frown)
                self._dash = frown
            self._dash.setHidden_(False)
            orb.rest_mouth.setHidden_(True)
            for eye, _ in orb.eyes:
                eye.setBounds_(Quartz.CGRectMake(0, 0, 7.5 * s, 2.6 * s))
                eye.setCornerRadius_(1.3 * s)
                for shine in eye.sublayers() or []:
                    shine.setHidden_(True)
            orb._next_blink = float("inf")
        else:
            if self._dash is not None:
                self._dash.setHidden_(True)
            orb.rest_mouth.setHidden_(False)
            for eye, _ in orb.eyes:
                eye.setBounds_(Quartz.CGRectMake(0, 0, 5 * s, 9 * s))
                eye.setCornerRadius_(2.5 * s)
                for shine in eye.sublayers() or []:
                    shine.setHidden_(False)
            orb._next_blink = time.monotonic() + 1.5


# --- the scene base -----------------------------------------------------------------------------

class Scene:
    """A view (w x h) with a card filling it, an `art` layer stack under any controls, and a face."""

    def __init__(self, w: float, h: float, card: bool = True) -> None:
        self.w, self.h = float(w), float(h)
        self.alive = True
        self.started = False
        self._run = 0
        view = MintFxSceneView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.w, self.h))
        view.setWantsLayer_(True)
        view.scene = self
        self.view = view
        # Layers live in their own subview, so labels and buttons (later subviews) always sit above them.
        art = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.w, self.h))
        art.setWantsLayer_(True)
        art.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        view.addSubview_(art)
        self.art = art
        self.card = Quartz.CALayer.layer()
        self.card.setFrame_(Quartz.CGRectMake(0, 0, self.w, self.h))
        self.card.setCornerRadius_(RADIUS)
        self.card.setMasksToBounds_(True)
        if card:
            self.card.setBackgroundColor_(gfx.cg(CARD))
        art.layer().addSublayer_(self.card)
        self.edge = Quartz.CALayer.layer()          # the hairline sits over everything in the card
        self.edge.setFrame_(Quartz.CGRectMake(0, 0, self.w, self.h))
        self.edge.setCornerRadius_(RADIUS)
        self.edge.setBorderWidth_(1.0)
        self.edge.setBorderColor_(_cgw(HAIRLINE if card else 0.0))
        art.layer().addSublayer_(self.edge)
        self.face = None
        self._acts = []

    # lifecycle
    def _appeared(self) -> None:
        if not self.alive:
            return
        _ticking(self, True)
        if not self.started and not self.view.isHiddenOrHasHiddenAncestor():
            self.start()                      # (added hidden, as the notch's panes are: starts when shown)

    def _idle(self) -> None:
        _ticking(self, False)

    def _unhidden(self) -> None:
        if self.alive and not self.started and self.view.window() is not None:
            self.start()

    def start(self) -> None:
        """Play the scene from the beginning (called by itself the first time the view is in a window)."""
        self.started = True
        self._run += 1
        _ticking(self, True)
        self._play()

    def _play(self) -> None:
        pass

    def _tick(self, now: float) -> None:
        view = self.view
        if self.face is not None and view.window() is not None and not view.isHiddenOrHasHiddenAncestor():
            self.face.tick(now)

    def close(self, remove: bool = True) -> None:
        """Stop everything (timers, pending steps); take the view out of its superview."""
        self.alive = False
        _ticking(self, False)
        if remove and self.view.superview() is not None:
            self.view.removeFromSuperview()
        self.view.scene = None

    def _button(self, frame, fn, image=None, tip=""):
        act = MintFxAct.alloc().initWithFn_(fn)
        self._acts.append(act)
        b = MintFxButton.alloc().initWithFrame_(frame)
        b.setTarget_(act)
        b.setAction_("fire:")
        b.setBordered_(False)
        b.setTitle_("")
        if image is not None:
            b.setImage_(image)
        b.setToolTip_(tip)
        b.setWantsLayer_(True)
        self.view.addSubview_(b)
        return b


# --- 1. greeting ------------------------------------------------------------------------------

GREETED_FMT = "%Y-%m-%d"


def should_greet(mark: bool = True) -> bool:
    """True on the first wake of the calendar day while the greeting pref is on (and, by default, remembers
    today so the next wake doesn't greet again)."""
    try:
        if prefs.get("greeting") is False:
            return False
    except Exception:
        return False
    today = datetime.date.today().strftime(GREETED_FMT)
    try:
        last = GREETED.read_text().strip() if GREETED.exists() else ""
    except OSError:
        last = ""
    if last == today:
        return False
    if mark:
        try:
            GREETED.parent.mkdir(parents=True, exist_ok=True)
            GREETED.write_text(today + "\n")
        except OSError:
            log.debug("could not remember the greeting", exc_info=True)
    return True


class Greeting(Scene):
    SECONDS = 3.2

    def __init__(self, w, h, on_done=None) -> None:
        super().__init__(w, h)
        self.on_done = on_done
        w, h = self.w, self.h
        d = max(30.0, min(58.0, h * 0.38))
        self.center = (w / 2, h / 2 - h * 0.02)
        cx, cy = self.center
        # Warm light behind Mint.
        gw, gh = min(w, h * 3.4), h * 1.7
        self.glow = _radial(Quartz.CGRectMake(cx - gw / 2, cy - gh / 2, gw, gh), WARM,
                            [(0.34, 0.0), (0.14, 0.38), (0.0, 1.0)])
        self.glow.setOpacity_(0.0)
        self.card.addSublayer_(self.glow)
        # Sparkle streams: columns of fine dots drifting up, spread across the card (none over the face).
        self.columns = []
        dot = _dot_image(8)
        n = max(4, min(10, int(w / 64)))
        spots = [w * (i + 0.5) / n for i in range(n)]
        spots = [x for x in spots if abs(x - cx) > d * 0.95]
        if len(spots) % 2:                                     # keep it symmetric
            spots = [x for x in spots if abs(x - cx) > d * 1.6] or spots
        for x in spots:
            col = Quartz.CAEmitterLayer.layer()
            col.setFrame_(Quartz.CGRectMake(0, 0, w, h))
            cw = 20 + 12 * ((int(x) * 7919) % 5) / 4
            col.setEmitterPosition_(Quartz.CGPointMake(x, h / 2))
            col.setEmitterSize_(Quartz.CGSizeMake(cw, h * 1.05))
            col.setEmitterShape_(Quartz.kCAEmitterLayerRectangle)
            col.setEmitterMode_(Quartz.kCAEmitterLayerSurface)
            col.setRenderMode_(Quartz.kCAEmitterLayerAdditive)
            cell = Quartz.CAEmitterCell.emitterCell()
            cell.setContents_(dot)
            cell.setBirthRate_(cw * h / 13.0)
            cell.setLifetime_(1.25)
            cell.setLifetimeRange_(0.5)
            cell.setVelocity_(13)
            cell.setVelocityRange_(8)
            cell.setEmissionLongitude_(math.pi / 2)
            cell.setEmissionRange_(0.3)
            cell.setScale_(0.11)
            cell.setScaleRange_(0.07)
            cell.setColor_(_cgw(0.9))
            cell.setAlphaRange_(0.5)
            cell.setAlphaSpeed_(-0.7)
            col.setEmitterCells_([cell])
            col.setBirthRate_(0.0)
            self.card.addSublayer_(col)
            self.columns.append((abs(x - cx), col))
        # A ring of sparkles bursting round Mint as it lands.
        ring = Quartz.CAEmitterLayer.layer()
        ring.setFrame_(Quartz.CGRectMake(0, 0, w, h))
        ring.setEmitterPosition_(Quartz.CGPointMake(cx, cy))
        ring.setEmitterSize_(Quartz.CGSizeMake(d * 1.5, d * 1.5))
        ring.setEmitterShape_(Quartz.kCAEmitterLayerCircle)
        ring.setEmitterMode_(Quartz.kCAEmitterLayerOutline)
        ring.setRenderMode_(Quartz.kCAEmitterLayerAdditive)
        cell = Quartz.CAEmitterCell.emitterCell()
        cell.setContents_(dot)
        cell.setBirthRate_(520)
        cell.setLifetime_(0.75)
        cell.setLifetimeRange_(0.3)
        cell.setVelocity_(26)
        cell.setVelocityRange_(18)
        cell.setEmissionRange_(2 * math.pi)
        cell.setScale_(0.2)
        cell.setScaleRange_(0.1)
        cell.setColor_(gfx.cg(WARM, 0.95))
        cell.setAlphaSpeed_(-1.2)
        ring.setEmitterCells_([cell])
        ring.setBirthRate_(0.0)
        self.card.addSublayer_(ring)
        self.ring = ring
        # A few confetti flecks in Mint's colours.
        confetti = Quartz.CAEmitterLayer.layer()
        confetti.setFrame_(Quartz.CGRectMake(0, 0, w, h))
        confetti.setEmitterPosition_(Quartz.CGPointMake(w / 2, h / 2))
        confetti.setEmitterSize_(Quartz.CGSizeMake(w * 0.96, h * 0.9))
        confetti.setEmitterShape_(Quartz.kCAEmitterLayerRectangle)
        confetti.setEmitterMode_(Quartz.kCAEmitterLayerSurface)
        bar = _bar_image()
        pal = gfx.palette()
        cells = []
        for rgb in (pal["awake"], pal["thinking"], pal["working"], pal["speaking"], (1.0, 0.45, 0.62)):
            c = Quartz.CAEmitterCell.emitterCell()
            c.setContents_(bar)
            c.setBirthRate_(max(0.8, w / 420))
            c.setLifetime_(1.6)
            c.setLifetimeRange_(0.6)
            c.setVelocity_(14)
            c.setVelocityRange_(10)
            c.setEmissionLongitude_(math.pi / 2)
            c.setEmissionRange_(0.8)
            c.setSpin_(1.0)
            c.setSpinRange_(4.0)
            c.setScale_(0.55)
            c.setScaleRange_(0.2)
            c.setColor_(gfx.cg(rgb, 0.95))
            c.setAlphaSpeed_(-0.45)
            cells.append(c)
        confetti.setEmitterCells_(cells)
        confetti.setBirthRate_(0.0)
        self.card.addSublayer_(confetti)
        self.confetti = confetti
        # Thin light lines at the card's ends as the streams clear.
        self.edges = []
        for x in (3.0, w - 5.0):
            line = _linear(Quartz.CGRectMake(x, h * 0.08, 2.0, h * 0.84),
                           [_cgw(0.0), _cgw(0.75), _cgw(0.0)], locations=[0.0, 0.5, 1.0])
            line.setCornerRadius_(1.0)
            line.setOpacity_(0.0)
            self.card.addSublayer_(line)
            self.edges.append(line)
        self.face = Face(self.card, self.center, d)
        self.face.layer.setOpacity_(0.0)

    def _play(self) -> None:
        cx, cy = self.center
        reduce = kinetics.reduce_motion()
        kinetics.basic(self.glow, "opacity", 0.0, 1.0, 0.5, anim_key="fx-glow")
        _sfx("hello")
        if reduce:
            kinetics.basic(self.face.layer, "opacity", 0.0, 1.0, 0.35, delay=0.15, anim_key="fx-face-o")
            _later(self, 0.9, self.face.blink)
            _later(self, 1.5, lambda: kinetics.basic(self.glow, "opacity", 1.0, 0.45, 0.4, anim_key="fx-glow"))
            _later(self, 1.8, self._done)
            return
        now = Quartz.CACurrentMediaTime()
        for _, col in self.columns:
            col.setBeginTime_(now)
            col.setBirthRate_(1.0)
        self.confetti.setBeginTime_(now)
        self.confetti.setBirthRate_(1.0)
        # Mint drops in from the top edge and lands with a little bounce; sparkles ring it as it lands.
        drop = 0.28
        kinetics.basic(self.face.layer, "opacity", 0.0, 1.0, 0.18, delay=drop, anim_key="fx-face-o")
        kinetics.spring(self.face.layer, "position", (cx, cy + self.h * 0.75), (cx, cy), "bouncy", delay=drop,
                        anim_key="fx-drop")
        _later(self, drop + 0.22, self._ring)
        _later(self, drop + 0.7, self.face.blink)
        _later(self, drop + 0.95, lambda: self.face.wave(1.4))
        # The streams clear from the middle out, so Mint is left in the warm light; then the ends shimmer.
        order = sorted(self.columns, key=lambda item: item[0])
        for i, (_, col) in enumerate(order):
            _later(self, 1.35 + i * (0.85 / max(1, len(order))), lambda col=col: col.setBirthRate_(0.0))
        _later(self, 1.6, lambda: self.confetti.setBirthRate_(0.0))
        for i, line in enumerate(self.edges):
            _later(self, 2.15, lambda line=line: _keys(line, "opacity", [0.0, 1.0, 0.0], 0.6, [0, 0.35, 1],
                                                       anim_key="fx-edge"))
        _later(self, 2.35, lambda: self.face.blink())
        _later(self, 2.55, self.face.hands_away)
        _later(self, 2.7, lambda: kinetics.basic(self.glow, "opacity", 1.0, 0.45, 0.5, anim_key="fx-glow"))
        _later(self, self.SECONDS, self._done)

    def _ring(self) -> None:
        self.ring.setBeginTime_(Quartz.CACurrentMediaTime())
        self.ring.setBirthRate_(1.0)
        _later(self, 0.16, lambda: self.ring.setBirthRate_(0.0))

    def _done(self) -> None:
        if self.on_done is not None:
            try:
                self.on_done()
            except Exception:
                log.exception("greeting on_done failed")


def greeting(w: float, h: float, on_done=None):
    """The island-body hello: (view, controller). ~3.2 s, then on_done(). The caller removes the view."""
    scene = Greeting(w, h, on_done)
    return scene.view, scene


# --- 2. error card ----------------------------------------------------------------------------

class ErrorCard(Scene):
    def __init__(self, w, h, title, reason, on_retry=None) -> None:
        super().__init__(w, h)
        self.on_retry = on_retry
        w, h = self.w, self.h
        d = max(30.0, min(54.0, h * 0.36))
        btn = 34.0 if h >= 90 else 30.0
        margin = 22.0
        gap = 20.0 if w > 450 else 14.0
        face_slot = d * 1.55
        text_w = min(320.0, w - 2 * margin - face_slot - btn - 2 * gap)
        group = face_slot + gap + text_w + gap + btn
        x0 = max(margin, (w - group) / 2)
        cy = h / 2
        self.center = (x0 + face_slot / 2, cy)
        fx, fy = self.center

        self.glow = _corner_glow(self.card, w, h, ORANGE, 0.8)
        self.glow.addSublayer_(_radial(Quartz.CGRectMake(fx - d * 2, fy - d * 1.6, d * 4, d * 3.2), RED,
                                       [(0.22, 0.0), (0.0, 1.0)]))
        self.glow.setOpacity_(0.0)
        # Ghost warning triangles behind Mint, slowly turning and breathing red.
        self.triangles = []
        for i, (scale, alpha, secs) in enumerate(((1.55, 0.55, 16.0), (2.05, 0.32, -22.0), (2.6, 0.18, 28.0))):
            size = d * scale
            tri = Quartz.CAShapeLayer.layer()
            tri.setBounds_(Quartz.CGRectMake(0, 0, size, size))
            tri.setPosition_(Quartz.CGPointMake(fx, fy))
            tri.setPath_(_rounded_triangle(size, size * 0.12))
            tri.setFillColor_(None)
            tri.setStrokeColor_(gfx.cg(RED, alpha))
            tri.setLineWidth_(1.6)
            tri.setShadowColor_(gfx.cg(RED))
            tri.setShadowOpacity_(0.7)
            tri.setShadowRadius_(4.0)
            tri.setShadowOffset_(Quartz.CGSizeMake(0, 0))
            tri.setOpacity_(0.0)
            tri.setAffineTransform_(Quartz.CGAffineTransformMakeRotation(0.35 * (i - 1)))
            self.card.addSublayer_(tri)
            self.triangles.append((tri, secs))
        self.face = Face(self.card, self.center, d)

        # Words: title (white, semibold) over the reason (red, two lines at most).
        tx = x0 + face_slot + gap
        self.title = label(title, 13.5 if w > 450 else 12.5, AppKit.NSFontWeightSemibold, _white(0.93))
        self.reason = label(reason, 11.5 if w > 450 else 10.5, AppKit.NSFontWeightRegular,
                            gfx.ns(REASON_RED, 0.95), lines=2)
        self.text_x, self.text_w = tx, text_w
        self.view.addSubview_(self.title)
        self.view.addSubview_(self.reason)
        self._layout_text()

        # The round retry button: an arrow that turns when pressed.
        bx, by = tx + text_w + gap, cy - btn / 2
        ring = Quartz.CALayer.layer()
        ring.setFrame_(Quartz.CGRectMake(bx, by, btn, btn))
        ring.setCornerRadius_(btn / 2)
        ring.setBackgroundColor_(_cgw(0.08))
        ring.setBorderWidth_(1.0)
        ring.setBorderColor_(_cgw(0.08))
        self.card.addSublayer_(ring)
        icon = Quartz.CALayer.layer()
        g = btn * 0.46
        icon.setBounds_(Quartz.CGRectMake(0, 0, g, g))
        icon.setPosition_(Quartz.CGPointMake(bx + btn / 2, cy))
        icon.setBackgroundColor_(_cgw(0.88))
        mask = Quartz.CALayer.layer()
        mask.setFrame_(Quartz.CGRectMake(0, 0, g, g))
        mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
        mask.setContents_(gfx.cg_image(gfx.symbol("arrow.clockwise", g, "semibold", white=True)))
        icon.setMask_(mask)
        self.card.addSublayer_(icon)
        self.ring_layer, self.icon = ring, icon
        self.button = self._button(AppKit.NSMakeRect(bx, by, btn, btn), self.retry, tip="Try again")
        if on_retry is None:
            self.button.setHidden_(True)
            ring.setHidden_(True)
            icon.setHidden_(True)

    def _layout_text(self) -> None:
        tx, tw, cy = self.text_x, self.text_w, self.h / 2
        th = _fit_height(self.title, tw)
        rh = _fit_height(self.reason, tw) if self.reason.stringValue() else 0
        rh = min(rh, 34)
        block = th + (3 + rh if rh else 0)
        top = cy + block / 2
        self.title.setFrame_(AppKit.NSMakeRect(tx, top - th, tw, th))
        self.reason.setFrame_(AppKit.NSMakeRect(tx, top - th - 3 - rh, tw, rh))
        self.reason.setHidden_(not rh)

    def set(self, title=None, reason=None) -> None:
        if title is not None:
            self.title.setStringValue_(title)
        if reason is not None:
            self.reason.setStringValue_(reason)
        self._layout_text()

    def _play(self) -> None:
        face = self.face
        face.tint(RED)
        face.expression("dash")
        face.status_dot(RED)
        _sfx("error")
        reduce = kinetics.reduce_motion()
        kinetics.basic(self.glow, "opacity", 0.0, 1.0, 0.6, anim_key="fx-glow")
        if not reduce:
            kinetics.spring(self.glow, "transform.translation.y", -self.h * 0.25, 0.0, "gentle", anim_key="fx-rise")
        for i, (tri, secs) in enumerate(self.triangles):
            kinetics.basic(tri, "opacity", 0.0, 1.0, 0.5, delay=0.1 + i * 0.12, anim_key="fx-tri-o")
            if not reduce:
                _spin_forever(tri, abs(secs), clockwise=secs > 0)
                pulse = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
                pulse.setFromValue_(0.94)
                pulse.setToValue_(1.06)
                pulse.setDuration_(1.6)
                pulse.setAutoreverses_(True)
                pulse.setRepeatCount_(float("inf"))
                pulse.setBeginTime_(Quartz.CACurrentMediaTime() + i * 0.5)
                pulse.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                    Quartz.kCAMediaTimingFunctionEaseInEaseOut))
                tri.addAnimation_forKey_(pulse, "fx-tri-pulse")
        for i, view in enumerate((self.title, self.reason)):
            if view.layer() is None:
                view.setWantsLayer_(True)
            kinetics.basic(view.layer(), "opacity", 0.0, 1.0, 0.28, delay=0.08 + i * 0.06, anim_key="fx-text",
                           keep=False)
        if reduce:
            return
        kinetics.shake(self.card, px=5.0)
        # The sad pulse: shrink, swell, settle - then again every few seconds, gently.
        group = Quartz.CAAnimationGroup.animation()
        sad = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
        sad.setValues_([1.0, 0.86, 1.07, 0.98, 1.0])
        sad.setKeyTimes_([0, 0.3, 0.62, 0.82, 1])
        sad.setDuration_(1.0)
        sad.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
            Quartz.kCAMediaTimingFunctionEaseInEaseOut))
        group.setAnimations_([sad])
        group.setDuration_(3.4)
        group.setRepeatCount_(float("inf"))
        group.setBeginTime_(Quartz.CACurrentMediaTime() + 0.25)
        self.face.layer.addAnimation_forKey_(group, "fx-sad")

    def retry(self) -> None:
        if not kinetics.reduce_motion():
            kinetics.basic(self.icon, "transform.rotation.z", 0.0, -2 * math.pi, 0.5, keep=False,
                           anim_key="fx-retry", timing=Quartz.kCAMediaTimingFunctionEaseInEaseOut)
        kinetics.basic(self.ring_layer, "backgroundColor", _cgw(0.22), _cgw(0.08), 0.4, keep=False,
                       anim_key="fx-press")
        _sfx("tick")
        if self.on_retry is not None:
            try:
                self.on_retry()
            except Exception:
                log.exception("retry failed")


def error_card(w: float, h: float, title: str, reason: str = "", on_retry=None):
    """A failed request: (view, controller). on_retry() when the round arrow is pressed (hidden if None).
    controller.set(title=None, reason=None) updates the words."""
    scene = ErrorCard(w, h, title, reason, on_retry)
    return scene.view, scene


# --- 3. drop zone -----------------------------------------------------------------------------

class DropZone(Scene):
    TEXT = "Drop to give it to Mint"

    def __init__(self, w, h, text=None) -> None:
        super().__init__(w, h)
        w, h = self.w, self.h
        self._hover = False
        d = max(30.0, min(54.0, h * 0.36))
        text = text or self.TEXT
        size = 13.0 if w > 450 else 12.0
        tw = min(_text_width(text, size, AppKit.NSFontWeightMedium), w * 0.55)
        gap = 26.0 if w > 450 else 16.0
        slot = d * 1.5
        group = slot + gap + tw
        x0 = (w - group) / 2
        self.center = (x0 + slot / 2, h / 2)
        # A green wash for the drop, rising from the bottom.
        self.wash = _corner_glow(self.card, w, h, GREEN, 1.2)
        self.wash.setOpacity_(0.0)
        self.tint = Quartz.CALayer.layer()                  # the card lightens a touch on hover
        self.tint.setFrame_(Quartz.CGRectMake(0, 0, w, h))
        self.tint.setBackgroundColor_(_cgw(0.035))
        self.tint.setOpacity_(0.0)
        self.card.addSublayer_(self.tint)
        # The dashed border, marching slowly.
        border = Quartz.CAShapeLayer.layer()
        border.setFrame_(Quartz.CGRectMake(0, 0, w, h))
        inset = 1.5
        border.setPath_(Quartz.CGPathCreateWithRoundedRect(
            Quartz.CGRectMake(inset, inset, w - 2 * inset, h - 2 * inset), RADIUS - inset, RADIUS - inset, None))
        border.setFillColor_(None)
        border.setStrokeColor_(_cgw(0.26))
        border.setLineWidth_(1.5)
        border.setLineDashPattern_([7, 5])
        border.setLineCap_(Quartz.kCALineCapRound)
        self.art.layer().addSublayer_(border)
        self.border = border
        self.edge.setHidden_(True)
        self.face = Face(self.card, self.center, d)
        self.label = label(text, size, AppKit.NSFontWeightMedium, _white(0.5))
        lh = _fit_height(self.label, tw)
        self.label.setFrame_(AppKit.NSMakeRect(x0 + slot + gap, h / 2 - lh / 2, tw, lh))
        self.view.addSubview_(self.label)

    def _play(self) -> None:
        if not kinetics.reduce_motion():
            march = Quartz.CABasicAnimation.animationWithKeyPath_("lineDashPhase")
            march.setFromValue_(0.0)
            march.setToValue_(-12.0)
            march.setDuration_(0.9)
            march.setRepeatCount_(float("inf"))
            self.border.addAnimation_forKey_(march, "fx-march")
        _later(self, 0.12, lambda: self.face.morph("folder"))

    def hover(self, on: bool) -> None:
        """A file is over the notch (brighter border, Mint leans in) or left again."""
        on = bool(on)
        if on == self._hover:
            return
        self._hover = on
        b = self.border
        kinetics.basic(b, "strokeColor", (b.presentationLayer() or b).strokeColor(), _cgw(0.62 if on else 0.26),
                       0.2, anim_key="fx-stroke")
        kinetics.basic(self.tint, "opacity", self.tint.opacity(), 1.0 if on else 0.0, 0.2, anim_key="fx-tint")
        self.label.setTextColor_(_white(0.88 if on else 0.5))
        layer = self.face.layer
        pres = layer.presentationLayer() or layer
        kinetics.spring(layer, "transform.scale", pres.valueForKeyPath_("transform.scale"), 1.1 if on else 1.0,
                        "bouncy", anim_key="fx-hover-s")
        kinetics.spring(layer, "transform.rotation.z", pres.valueForKeyPath_("transform.rotation.z"),
                        -0.09 if on else 0.0, "bouncy", anim_key="fx-hover-r")

    def dropped(self, text: str = "Got it") -> None:
        """The file landed: green wash from the bottom, Mint back to a ball with a hop."""
        self.hover(False)
        _sfx("drop")
        kinetics.basic(self.wash, "opacity", 0.0, 1.0, 0.25, anim_key="fx-wash")
        if not kinetics.reduce_motion():
            kinetics.spring(self.wash, "transform.translation.y", -self.h * 0.6, 0.0, "gentle", anim_key="fx-rise")
        _later(self, 0.9, lambda: kinetics.basic(self.wash, "opacity", 1.0, 0.0, 0.6, anim_key="fx-wash"))
        kinetics.basic(self.border, "strokeColor", gfx.cg(GREEN, 0.9), _cgw(0.26), 1.1, anim_key="fx-stroke")
        self.face.morph(None)
        _later(self, 0.12, self.face.hop)
        if text:
            self.label.setStringValue_(text)
            self.label.setTextColor_(_white(0.9))


def drop_zone(w: float, h: float, text: str | None = None):
    """A file being dragged over the notch: (view, controller). controller.hover(on), controller.dropped()."""
    scene = DropZone(w, h, text)
    return scene.view, scene


# --- 4. progress with Mint as the thumb ----------------------------------------------------------

class Progress(Scene):
    def __init__(self, w, h, title, symbol="icloud.and.arrow.up.fill", dashed=False) -> None:
        super().__init__(w, h)
        w, h = self.w, self.h
        self.fraction = 0.0
        inset = max(22.0, w * 0.085)
        self.x0, self.x1 = inset, w - inset
        self.ty = h * 0.42                               # the track's centre line
        self.glow = _corner_glow(self.card, w, h, GREEN, 0.85)
        self.glow.setOpacity_(0.0)
        if dashed:
            border = Quartz.CAShapeLayer.layer()
            border.setPath_(Quartz.CGPathCreateWithRoundedRect(
                Quartz.CGRectMake(1.5, 1.5, w - 3, h - 3), RADIUS - 1.5, RADIUS - 1.5, None))
            border.setFillColor_(None)
            border.setStrokeColor_(_cgw(0.22))
            border.setLineWidth_(1.5)
            border.setLineDashPattern_([7, 5])
            self.art.layer().addSublayer_(border)
            self.edge.setHidden_(True)
        th = 5.0 if h >= 100 else 4.0
        track = Quartz.CALayer.layer()
        track.setFrame_(Quartz.CGRectMake(self.x0, self.ty - th / 2, self.x1 - self.x0, th))
        track.setCornerRadius_(th / 2)
        track.setBackgroundColor_(_cgw(0.1))
        self.card.addSublayer_(track)
        fill = _linear(Quartz.CGRectMake(0, 0, 0, th), [gfx.cg(GREEN), gfx.cg(TEAL)], start=(0, 0.5), end=(1, 0.5))
        fill.setAnchorPoint_(Quartz.CGPointMake(0, 0.5))
        fill.setPosition_(Quartz.CGPointMake(self.x0, self.ty))
        fill.setBounds_(Quartz.CGRectMake(0, 0, th, th))
        fill.setCornerRadius_(th / 2)
        self.card.addSublayer_(fill)
        self.track, self.fill, self.th = track, fill, th
        d = max(16.0, min(24.0, h * 0.16))
        seen = d                                         # how big the thumb looks
        from mint.ui.orb import Orb
        if callable(getattr(Orb, "thumb_mode", None)):
            d /= 0.62                                    # orb.thumb_mode() draws the face at 62%
        self.face = Face(self.card, (self.x0, self.ty), d)
        self.face.thumb(True)
        if hasattr(self.face.orb, "_size") and d != seen:
            self.face.orb._size = 0.62                   # already small on the first frame, not shrinking in
        self.seen = seen
        # A soft white streak behind the thumb while it moves (the motion blur).
        trail = _linear(Quartz.CGRectMake(0, 0, 40, seen * 0.62), [_cgw(0.0), _cgw(0.85)], start=(0, 0.5),
                        end=(1, 0.5))
        trail.setAnchorPoint_(Quartz.CGPointMake(1.0, 0.5))
        trail.setPosition_(Quartz.CGPointMake(*self.face.c))
        trail.setCornerRadius_(seen * 0.31)
        trail.setOpacity_(0.0)
        self.face.layer.insertSublayer_atIndex_(trail, 0)
        self.trail = trail
        # The row above: symbol + title on the left, percent + info on the right.
        size = 12.5 if w > 450 else 11.5
        row_y = self.ty + max(14.0, h * 0.12)
        self.icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol(symbol, size, "semibold"))
        self.icon.setContentTintColor_(_white(0.7))
        self.icon.setFrame_(AppKit.NSMakeRect(self.x0, row_y, size + 6, size + 4))
        self.view.addSubview_(self.icon)
        self.title = label(title, size, AppKit.NSFontWeightMedium, _white(0.82))
        self.title.setFrame_(AppKit.NSMakeRect(self.x0 + size + 10, row_y, (self.x1 - self.x0) * 0.7, size + 5))
        self.view.addSubview_(self.title)
        self.percent = label("0%", size - 0.5, AppKit.NSFontWeightMedium, _white(0.72))
        self.percent.setAlignment_(AppKit.NSTextAlignmentRight)
        info_w = size + 4
        self.percent.setFrame_(AppKit.NSMakeRect(self.x1 - info_w - 64, row_y, 60, size + 5))
        self.view.addSubview_(self.percent)
        self.info = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("info.circle", size - 1.5, "regular"))
        self.info.setContentTintColor_(_white(0.6))
        self.info.setFrame_(AppKit.NSMakeRect(self.x1 - info_w, row_y, info_w, size + 4))
        self.view.addSubview_(self.info)

    def _play(self) -> None:
        kinetics.basic(self.glow, "opacity", 0.0, 0.8, 0.6, anim_key="fx-glow")
        kinetics.pop(self.face.layer, "bouncy", 0.5)

    def set(self, fraction, title: str | None = None, info: str | None = None) -> None:
        """Move the bar (0..1); Mint rides the end of the fill, stretching with the speed."""
        if title is not None:
            self.title.setStringValue_(title)
        if info is not None:
            self.info.setToolTip_(info)
        if fraction is None:
            return
        f = max(0.0, min(1.0, float(fraction)))
        old = self.fraction
        self.fraction = f
        self.percent.setStringValue_(f"{int(round(f * 100))}%")
        span = self.x1 - self.x0
        x = self.x0 + f * span
        pres = self.face.layer.presentationLayer() or self.face.layer
        old_x = pres.position().x
        fill_pres = self.fill.presentationLayer() or self.fill
        old_w = fill_pres.bounds().size.width
        new_w = max(self.th, f * span)
        kinetics.spring(self.fill, "bounds", (0, 0, old_w, self.th), (0, 0, new_w, self.th), "gentle",
                        anim_key="fx-fill")
        kinetics.spring(self.face.layer, "position", (old_x, self.ty), (x, self.ty), "gentle", anim_key="fx-ride")
        dx = abs(x - old_x)
        if dx > 2 and f > old and not kinetics.reduce_motion():
            k = min(0.5, dx / 180.0)
            _keys(self.face.layer, "transform.scale.x", [1.0, 1.0 + k, 1.0 + k * 0.35, 1.0], 0.55,
                  [0, 0.22, 0.6, 1], anim_key="fx-stretch")
            _keys(self.face.layer, "transform.scale.y", [1.0, 1.0 - k * 0.3, 1.0, 1.0], 0.55,
                  [0, 0.22, 0.6, 1], anim_key="fx-squash")
            length = min(70.0, 14.0 + dx * 0.45)
            _quiet(lambda: self.trail.setBounds_(Quartz.CGRectMake(0, 0, length, self.seen * 0.62)))
            _keys(self.trail, "opacity", [0.0, 0.85, 0.0], 0.55, [0, 0.2, 1], anim_key="fx-trail")

    def finish(self, ok: bool = True, title: str | None = None) -> None:
        """Done: fill to the end and a happy hop - or a red bar and a shake."""
        if ok:
            self.set(1.0, title=title)
            _later(self, 0.4, self._happy)
        else:
            if title is not None:
                self.title.setStringValue_(title)
            self.fill.setColors_([gfx.cg(RED), gfx.cg(ORANGE)])
            for g in self.glow.sublayers() or []:
                colors = g.colors()
                if colors:
                    g.setColors_([gfx.cg(RED, Quartz.CGColorGetAlpha(c)) for c in colors])
            self.face.tint(RED)
            self.face.expression("dash")
            self.face.shake()
            _sfx("error")

    def _happy(self) -> None:
        self.face.hop()
        self.face.blink()
        _sfx("done")
        _keys(self.glow, "opacity", [0.8, 1.0, 0.8], 0.9, anim_key="fx-glow-pop")


def progress_bar(w: float, h: float, title: str, symbol: str = "icloud.and.arrow.up.fill", dashed: bool = False):
    """A long job: (view, controller). controller.set(fraction, title=None, info=None), controller.finish(ok)."""
    scene = Progress(w, h, title, symbol, dashed)
    return scene.view, scene
