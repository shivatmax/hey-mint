"""The orb: a small Siri-like swirl with a face.

Built from Core Animation layers into any layer-backed view, centred on a given
point. Two conic gradients turn against each other inside a circle, faster
when Mint thinks or speaks; a glass highlight sits on top; the face (eyes
that blink and look around, a mouth that moves with the voice, cheeks when
happy) sits on that. Around it: a glow that swells with the voice, a pulse
ring, a comet spinner while busy, a progress ring for long tasks, and a badge
showing what kind of work is going on.

The state reads even when the face is tiny (the notch): a status badge at the
top-left (badge.py), a tint over the lower half, and the eyes' shape
(expression: dash, halfmoon, arc, dot, wide). The face sits on a sphere: eyes
foreshorten as they slide toward the rim. Poke it (poke): a slap, three quick
ones make it dizzy, keep going and it gets annoyed. For scenes: wave() brings
out two little hands, morph("folder") turns the face into a window, thumb_mode()
makes it small enough to ride a progress bar.

Main thread only.
"""

from __future__ import annotations

import math
import random
import time

import AppKit
import Quartz
from PyObjCTools import AppHelper

from mint.ui import activity
from mint.ui import gfx
from mint.ui import kinetics
from mint.core import prefs

INK = (0.04, 0.06, 0.12)          # the face's colour

# Eye shapes, in face units (a 44 pt face): width, height, corner radius, tilt (inner ends down),
# and which catch-lights show (both / one / none). "arc" and "closed" are drawn differently.
EYES = {
    "normal": (5.0, 9.0, 2.5, 0.0, 2),
    "wide": (6.4, 10.6, 3.2, 0.0, 2),
    "dot": (4.4, 4.6, 2.2, 0.0, 1),
    "dash": (6.6, 1.9, 0.95, 0.0, 0),
    "halfmoon": (7.0, 4.4, 3.5, 0.24, 0),
    "arc": (5.0, 1.6, 0.8, 0.0, 0),
    "closed": (5.0, 2.0, 1.0, 0.0, 0),
}
EXPRESSIONS = ("normal", "dash", "halfmoon", "arc", "dot", "wide")

# Blink (from the peer's spec): close 80 ms, open 140 ms.
_BLINK_CLOSE = (0.55, 0.0, 0.9, 0.45)
_BLINK_OPEN = (0.15, 0.6, 0.3, 1.0)


def _ease(name=Quartz.kCAMediaTimingFunctionEaseInEaseOut):
    return Quartz.CAMediaTimingFunction.functionWithName_(name)


def _circle(layer, diameter, center):
    layer.setBounds_(Quartz.CGRectMake(0, 0, diameter, diameter))
    layer.setPosition_(Quartz.CGPointMake(*center))
    layer.setCornerRadius_(diameter / 2)
    return layer


def _spin(layer, seconds, clockwise=True, key="spin"):
    turn = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
    turn.setFromValue_(0.0)
    turn.setToValue_((-2 if clockwise else 2) * math.pi)
    turn.setDuration_(seconds)
    turn.setRepeatCount_(float("inf"))
    layer.addAnimation_forKey_(turn, key)


def _set_speed(layer, speed):
    """Change how fast a layer's animations run without making them jump."""
    now = Quartz.CACurrentMediaTime()
    local = layer.convertTime_fromLayer_(now, None)
    layer.setSpeed_(speed)
    layer.setTimeOffset_(local)
    layer.setBeginTime_(now)


class Orb:
    def __init__(self, root, center, diameter: float = 44) -> None:
        self.root = root
        self.center = center
        self.d = diameter
        self.state = "starting"
        self._eye_offset = [0.0, 0.0]
        self._eye_mode = "open"
        self._next_blink = time.monotonic() + 2.0
        self._next_whimsy = time.monotonic() + 9.0
        self._happy_until = 0.0
        self._hover = 0.0
        self._next_z = 0.0
        self._yawn_until = 0.0
        self._look_around_until = 0.0
        self.in_notch = False             # set by notch.py: extras float downwards, no pill
        self.primary = False              # set by the HUD on the orb it drives: the status badge shows
        self._busy = False
        # The face on a sphere: gaze angles (eased), a kick that springs back (slaps), extras (dizzy).
        self._gaze = [0.0, 0.0]
        self._kick = [0.0, 0.0]
        self._kick_v = [0.0, 0.0]
        self._face_drop = 0.0             # the folder morph moves the face down under its bar
        self._expr = "normal"
        self._expr_until = 0.0
        self._shape = "normal"            # the eye shape on screen
        self._shape_seq = 0
        self._pokes: list[float] = []
        self._roll_at = 0.0               # dizzy: when the roll started
        self._annoyed_until = 0.0
        self._swell = 1.0
        self._thumb = False
        self._size = 1.0
        self._morphed = None
        self._tint_kind = None
        self._build()

    # --- construction ------------------------------------------------------------

    def _build(self) -> None:
        d, c = self.d, self.center
        # Everything hangs off `body`, so hover, hops and shakes move it all.
        body = Quartz.CALayer.layer()
        body.setBounds_(Quartz.CGRectMake(0, 0, d * 2.6, d * 2.6))
        body.setPosition_(Quartz.CGPointMake(*c))
        self.root.addSublayer_(body)
        self.body = body
        bc = (d * 1.3, d * 1.3)          # centre inside body
        self._bc = bc

        self.glow = _circle(Quartz.CALayer.layer(), d * 0.8, bc)
        self.glow.setShadowOpacity_(1.0)
        self.glow.setShadowRadius_(d * 0.38)
        self.glow.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        self.glow.setOpacity_(0.4)
        body.addSublayer_(self.glow)

        ring_bounds = Quartz.CGRectMake(0, 0, d, d)
        self.ring = Quartz.CAShapeLayer.layer()
        self.ring.setBounds_(ring_bounds)
        self.ring.setPosition_(Quartz.CGPointMake(*bc))
        self.ring.setPath_(Quartz.CGPathCreateWithEllipseInRect(ring_bounds, None))
        self.ring.setFillColor_(None)
        self.ring.setLineWidth_(1.5)
        self.ring.setOpacity_(0)
        body.addSublayer_(self.ring)

        # The swirl, clipped to a circle.
        core = _circle(Quartz.CALayer.layer(), d, bc)
        core.setMasksToBounds_(True)
        core.setBorderWidth_(0.6)
        core.setBorderColor_(AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.35).CGColor())
        body.addSublayer_(core)
        self.core = core
        mid = (d / 2, d / 2)
        self.base = _circle(Quartz.CAGradientLayer.layer(), d, mid)
        self.base.setType_(Quartz.kCAGradientLayerRadial)
        self.base.setStartPoint_(Quartz.CGPointMake(0.5, 0.5))
        self.base.setEndPoint_(Quartz.CGPointMake(1.0, 1.0))
        core.addSublayer_(self.base)
        self.swirl_a = _circle(Quartz.CAGradientLayer.layer(), d * 1.6, mid)
        self.swirl_b = _circle(Quartz.CAGradientLayer.layer(), d * 1.45, mid)
        for layer in (self.swirl_a, self.swirl_b):
            layer.setType_(Quartz.kCAGradientLayerConic)
            layer.setStartPoint_(Quartz.CGPointMake(0.5, 0.5))
            layer.setEndPoint_(Quartz.CGPointMake(0.5, 0.0))
            core.addSublayer_(layer)
        self.swirl_b.setOpacity_(0.6)
        try:
            self.swirl_b.setCompositingFilter_("screenBlendMode")
        except Exception:
            pass
        _spin(self.swirl_a, 7.0, clockwise=True)
        _spin(self.swirl_b, 4.6, clockwise=False)
        # A soft darker centre keeps the face readable over the colours.
        self.shade = _circle(Quartz.CAGradientLayer.layer(), d, mid)
        self.shade.setType_(Quartz.kCAGradientLayerRadial)
        self.shade.setStartPoint_(Quartz.CGPointMake(0.5, 0.5))
        self.shade.setEndPoint_(Quartz.CGPointMake(1.0, 1.0))
        white = AppKit.NSColor.colorWithWhite_alpha_
        self.shade.setColors_([white(1.0, 0.28).CGColor(), white(1.0, 0.0).CGColor()])
        core.addSublayer_(self.shade)
        # The state tint: the lower half takes the state's colour (blue working, red error...) so the
        # state reads even on a tiny face; the swirl still shows through. Wider than the circle so the
        # folder morph's corners are covered too.
        self.tint = Quartz.CAGradientLayer.layer()
        self.tint.setFrame_(Quartz.CGRectMake(-d * 0.1, -d * 0.1, d * 1.2, d * 1.2))
        self.tint.setStartPoint_(Quartz.CGPointMake(0.5, 0.0))
        self.tint.setEndPoint_(Quartz.CGPointMake(0.5, 0.56))
        self.tint.setColors_([white(1.0, 0.0).CGColor(), white(1.0, 0.0).CGColor()])
        self.tint.setOpacity_(0.0)
        core.addSublayer_(self.tint)
        gloss = Quartz.CAGradientLayer.layer()
        gloss.setFrame_(Quartz.CGRectMake(d * 0.14, d * 0.52, d * 0.72, d * 0.42))
        gloss.setCornerRadius_(d * 0.21)
        gloss.setColors_([white(1.0, 0.0).CGColor(), white(1.0, 0.38).CGColor()])
        core.addSublayer_(gloss)

        # Reading: a bright bar sweeps across.
        self.scan = Quartz.CALayer.layer()
        self.scan.setBounds_(Quartz.CGRectMake(0, 0, d, 2.5))
        self.scan.setPosition_(Quartz.CGPointMake(d / 2, d / 2))
        self.scan.setBackgroundColor_(white(1.0, 0.95).CGColor())
        self.scan.setShadowColor_(AppKit.NSColor.whiteColor().CGColor())
        self.scan.setShadowOpacity_(1.0)
        self.scan.setShadowRadius_(3)
        self.scan.setOpacity_(0)
        core.addSublayer_(self.scan)

        # The face, in one layer so it can melt away while the orb becomes the task.
        face = Quartz.CALayer.layer()
        face.setBounds_(Quartz.CGRectMake(0, 0, d, d))
        face.setPosition_(Quartz.CGPointMake(d / 2, d / 2))
        core.addSublayer_(face)
        self.face = face
        s = d / 44.0
        self._face_scale = s

        def white_(w, a):
            return AppKit.NSColor.colorWithWhite_alpha_(w, a).CGColor()
        self.eyes, self.smiles, self.cheeks = [], [], []
        self._sockets = []
        for dx in (-6.5 * s, 6.5 * s):
            # Each eye hangs in a socket: the socket slides over the sphere and foreshortens near the
            # rim; the eye inside changes shape and blinks. (Two layers, so the transforms don't fight.)
            socket = Quartz.CALayer.layer()
            socket.setBounds_(Quartz.CGRectMake(0, 0, 0, 0))
            socket.setPosition_(Quartz.CGPointMake(d / 2 + dx, d / 2 + 2 * s))
            face.addSublayer_(socket)
            self._sockets.append(socket)
            eye = Quartz.CALayer.layer()
            eye.setBounds_(Quartz.CGRectMake(0, 0, 5 * s, 9 * s))
            eye.setCornerRadius_(2.5 * s)
            eye.setPosition_(Quartz.CGPointMake(0, 0))
            eye.setBackgroundColor_(gfx.cg(INK, 0.9))
            # Two catch-lights make the eyes sparkle.
            for (hx, hy, hd) in ((3.5, 6.6, 2.3), (1.6, 2.6, 1.1)):
                shine = Quartz.CALayer.layer()
                shine.setBounds_(Quartz.CGRectMake(0, 0, hd * s, hd * s))
                shine.setCornerRadius_(hd * s / 2)
                shine.setPosition_(Quartz.CGPointMake(hx * s, hy * s))
                shine.setBackgroundColor_(white_(1.0, 0.95))
                eye.addSublayer_(shine)
            socket.addSublayer_(eye)
            self.eyes.append((eye, dx))
            smile = Quartz.CAShapeLayer.layer()
            arc = Quartz.CGPathCreateMutable()
            Quartz.CGPathMoveToPoint(arc, None, -3.5 * s, -1 * s)
            Quartz.CGPathAddQuadCurveToPoint(arc, None, 0, 4 * s, 3.5 * s, -1 * s)
            smile.setPath_(arc)
            smile.setFillColor_(None)
            smile.setStrokeColor_(gfx.cg(INK, 0.88))
            smile.setLineWidth_(2.2 * s)
            smile.setLineCap_(Quartz.kCALineCapRound)
            smile.setOpacity_(0)
            smile.setPosition_(Quartz.CGPointMake(0, -1))
            socket.addSublayer_(smile)
            self.smiles.append(smile)
            cheek = Quartz.CALayer.layer()
            cheek.setBounds_(Quartz.CGRectMake(0, 0, 6 * s, 3.5 * s))
            cheek.setCornerRadius_(1.75 * s)
            cheek.setBackgroundColor_(gfx.cg((1.0, 0.45, 0.6), 0.55))
            cheek.setPosition_(Quartz.CGPointMake(d / 2 + dx * 1.45, d / 2 - 4 * s))
            cheek.setOpacity_(0)
            face.addSublayer_(cheek)
            self.cheeks.append(cheek)
        self.mouth = Quartz.CALayer.layer()
        self.mouth.setBounds_(Quartz.CGRectMake(0, 0, 7 * s, 2 * s))
        self.mouth.setCornerRadius_(1 * s)
        self.mouth.setBackgroundColor_(gfx.cg(INK, 0.8))
        self.mouth.setPosition_(Quartz.CGPointMake(d / 2, d / 2 - 7 * s))
        self.mouth.setOpacity_(0)
        face.addSublayer_(self.mouth)
        # At rest, a tiny cat mouth.
        rest = Quartz.CAShapeLayer.layer()
        rest.setBounds_(Quartz.CGRectMake(0, 0, 8 * s, 4 * s))
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(path, None, 1 * s, 3 * s)
        Quartz.CGPathAddQuadCurveToPoint(path, None, 2.5 * s, 0.6 * s, 4 * s, 2.6 * s)
        Quartz.CGPathAddQuadCurveToPoint(path, None, 5.5 * s, 0.6 * s, 7 * s, 3 * s)
        rest.setPath_(path)
        rest.setFillColor_(None)
        rest.setStrokeColor_(gfx.cg(INK, 0.82))
        rest.setLineWidth_(1.35 * s)
        rest.setLineCap_(Quartz.kCALineCapRound)
        rest.setLineJoin_(Quartz.kCALineJoinRound)
        rest.setPosition_(Quartz.CGPointMake(d / 2, d / 2 - 6 * s))
        face.addSublayer_(rest)
        self.rest_mouth = rest

        # The task glyph: the orb turns into what it is doing - a folder, a magnifier, the
        # app's own icon - instead of anything popping up elsewhere on screen.
        g = d * 0.54
        glyph = Quartz.CALayer.layer()
        glyph.setBounds_(Quartz.CGRectMake(0, 0, g, g))
        glyph.setPosition_(Quartz.CGPointMake(d / 2, d / 2))
        glyph.setOpacity_(0)
        glyph.setShadowColor_(gfx.cg(INK))
        glyph.setShadowOpacity_(0.25)
        glyph.setShadowRadius_(2)
        glyph.setShadowOffset_(Quartz.CGSizeMake(0, -0.5))
        core.addSublayer_(glyph)
        tint = Quartz.CALayer.layer()
        tint.setFrame_(Quartz.CGRectMake(0, 0, g, g))
        tint.setBackgroundColor_(white(1.0, 0.96).CGColor())
        tint_mask = Quartz.CALayer.layer()
        tint_mask.setFrame_(Quartz.CGRectMake(0, 0, g, g))
        tint_mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
        tint.setMask_(tint_mask)
        glyph.addSublayer_(tint)
        picture = Quartz.CALayer.layer()
        picture.setFrame_(Quartz.CGRectMake(-g * 0.12, -g * 0.12, g * 1.24, g * 1.24))
        picture.setContentsGravity_(Quartz.kCAGravityResizeAspect)
        glyph.addSublayer_(picture)
        self.glyph, self.glyph_tint, self.glyph_mask, self.glyph_picture = glyph, tint, tint_mask, picture
        self._morph_seq = 0

        # Busy: a comet arc. Long tasks: a progress ring.
        outer = Quartz.CGRectMake(0, 0, d + 12, d + 12)
        self.spinner = Quartz.CAShapeLayer.layer()
        self.spinner.setBounds_(outer)
        self.spinner.setPosition_(Quartz.CGPointMake(*bc))
        self.spinner.setPath_(Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectInset(outer, 2, 2), None))
        self.spinner.setFillColor_(None)
        self.spinner.setLineWidth_(2.5)
        self.spinner.setLineCap_(Quartz.kCALineCapRound)
        self.spinner.setStrokeEnd_(0.24)
        self.spinner.setOpacity_(0)
        body.addSublayer_(self.spinner)
        self.progress_ring = Quartz.CAShapeLayer.layer()
        self.progress_ring.setBounds_(outer)
        self.progress_ring.setPosition_(Quartz.CGPointMake(*bc))
        path = Quartz.CGPathCreateMutable()
        r = (d + 12) / 2 - 2
        Quartz.CGPathAddArc(path, None, (d + 12) / 2, (d + 12) / 2, r, math.pi / 2, math.pi / 2 - 2 * math.pi, True)
        self.progress_ring.setPath_(path)
        self.progress_ring.setFillColor_(None)
        self.progress_ring.setLineWidth_(3)
        self.progress_ring.setLineCap_(Quartz.kCALineCapRound)
        self.progress_ring.setStrokeEnd_(0.0)
        self.progress_ring.setOpacity_(0)
        body.addSublayer_(self.progress_ring)

        # The activity badge, bottom right.
        badge = _circle(Quartz.CALayer.layer(), 20, (bc[0] + d * 0.38, bc[1] - d * 0.38))
        badge.setBackgroundColor_(gfx.cg((0.09, 0.10, 0.13), 0.96))
        badge.setBorderWidth_(1.5)
        badge.setShadowOpacity_(0.5)
        badge.setShadowRadius_(4)
        badge.setShadowOffset_(Quartz.CGSizeMake(0, -1))
        badge.setOpacity_(0)
        body.addSublayer_(badge)
        icon = Quartz.CALayer.layer()
        icon.setBounds_(Quartz.CGRectMake(0, 0, 12, 12))
        icon.setPosition_(Quartz.CGPointMake(10, 10))
        mask = Quartz.CALayer.layer()
        mask.setFrame_(Quartz.CGRectMake(0, 0, 12, 12))
        mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
        icon.setMask_(mask)
        badge.addSublayer_(icon)
        picture = Quartz.CALayer.layer()
        picture.setBounds_(Quartz.CGRectMake(0, 0, 16, 16))
        picture.setPosition_(Quartz.CGPointMake(10, 10))
        picture.setContentsGravity_(Quartz.kCAGravityResizeAspect)
        badge.addSublayer_(picture)
        self.badge, self.badge_icon, self.badge_mask, self.badge_picture = badge, icon, mask, picture
        # The status badge, top left (dots while working, red on error, green when done).
        from mint.ui.badge import StatusBadge
        self.status = StatusBadge(self)

        emitter = Quartz.CAEmitterLayer.layer()
        emitter.setFrame_(self.root.bounds())
        emitter.setEmitterPosition_(Quartz.CGPointMake(*c))
        emitter.setEmitterShape_(Quartz.kCAEmitterLayerCircle)
        emitter.setEmitterMode_(Quartz.kCAEmitterLayerOutline)
        emitter.setEmitterSize_(Quartz.CGSizeMake(d * 0.8, d * 0.8))
        emitter.setRenderMode_(Quartz.kCAEmitterLayerAdditive)
        emitter.setBirthRate_(0)
        self.root.addSublayer_(emitter)
        self.emitter = emitter
        self._dot_image = gfx.cg_image(gfx.symbol("circle.fill", 10, white=True))
        self._star_image = gfx.cg_image(gfx.symbol("sparkle", 14, white=True))
        self.apply_state("starting")

    def _snooze(self) -> None:
        """A small 'z' drifts up while asleep."""
        d = self.d
        bx, by = self._bc
        z = Quartz.CATextLayer.layer()
        size = random.choice((7.0, 8.5, 10.0))
        z.setString_("z")
        z.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, AppKit.NSFontWeightBold))
        z.setFontSize_(size)
        z.setForegroundColor_(gfx.cg(gfx.light(gfx.accent())))
        z.setContentsScale_(2.0)
        z.setAlignmentMode_(Quartz.kCAAlignmentCenter)
        z.setBounds_(Quartz.CGRectMake(0, 0, 12, 14))
        start = Quartz.CGPointMake(bx + d * 0.36, by + d * 0.3)
        z.setPosition_(start)
        z.setOpacity_(0.0)
        z.setShadowColor_(gfx.cg(INK))
        z.setShadowOpacity_(0.3)
        z.setShadowRadius_(1)
        z.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        self.body.addSublayer_(z)
        rise = Quartz.CAKeyframeAnimation.animationWithKeyPath_("position")
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(path, None, start.x, start.y)
        Quartz.CGPathAddCurveToPoint(path, None, start.x + 8, start.y + 6, start.x - 2, start.y + 14,
                                     start.x + 7, start.y + 22)
        rise.setPath_(path)
        fade = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        fade.setValues_([0.0, 0.9, 0.9, 0.0])
        fade.setKeyTimes_([0, 0.2, 0.7, 1])
        grow = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
        grow.setFromValue_(0.5)
        grow.setToValue_(1.2)
        group = Quartz.CAAnimationGroup.animation()
        group.setAnimations_([rise, fade, grow])
        group.setDuration_(2.2)
        z.addAnimation_forKey_(group, "snooze")
        AppHelper.callLater(2.25, z.removeFromSuperlayer)

    def yawn(self) -> None:
        self._yawn_until = time.monotonic() + 1.3

    # --- state -------------------------------------------------------------------------

    def apply_state(self, state: str, voice_on: bool = True) -> None:
        self.state = state
        rgb = gfx.state_rgb(state)
        pal = gfx.palette()
        dim = state in ("sleeping", "paused", "starting", "offline")
        if dim:
            colors = [gfx.mix(rgb, (0.35, 0.37, 0.42), 0.4), rgb, gfx.light(rgb), rgb]
        else:
            colors = [pal["awake"], pal["thinking"], pal["speaking"], pal["accent"]]
        cg = [gfx.cg(x) for x in colors]
        self.swirl_a.setColors_(cg + [cg[0]])
        clear = gfx.cg(colors[0], 0.0)
        self.swirl_b.setColors_([clear, gfx.cg(colors[2], 0.9), clear, gfx.cg(colors[3], 0.9), clear])
        self.base.setColors_([gfx.cg(gfx.light(rgb)), gfx.cg(rgb), gfx.cg(gfx.dark(rgb))])
        bright = gfx.cg(rgb)
        self.glow.setBackgroundColor_(bright)
        self.glow.setShadowColor_(bright)
        self.ring.setStrokeColor_(bright)
        self.spinner.setStrokeColor_(gfx.cg(gfx.light(rgb)))
        self.progress_ring.setStrokeColor_(gfx.cg(pal["accent"]))
        self.badge.setBorderColor_(bright)
        self.badge_icon.setBackgroundColor_(gfx.cg(gfx.light(rgb)))

        # Faster swirl when busy, lazy when asleep.
        speed = {"sleeping": 0.25, "paused": 0.0, "starting": 0.5, "offline": 0.0,
                 "awake": 1.0, "thinking": 2.4, "working": 2.8, "speaking": 1.8}.get(state, 1.0)
        for layer in (self.swirl_a, self.swirl_b):
            _set_speed(layer, speed)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.4)
        self.core.setOpacity_(0.62 if dim else 1.0)
        Quartz.CATransaction.commit()

        face = bool(prefs.get("face"))
        for eye, _ in self.eyes:
            eye.setHidden_(not face)
        for layer in (*self.smiles, *self.cheeks, self.mouth):
            layer.setHidden_(not face)

        self.spinner.removeAllAnimations()
        busy = state in ("thinking", "working")
        self.spinner.setOpacity_(0.95 if busy else 0.0)
        if busy:
            _spin(self.spinner, 0.8 if state == "working" else 1.3)

        self.ring.removeAllAnimations()
        if state in ("awake", "working", "thinking"):
            grow = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
            grow.setFromValue_(0.95)
            grow.setToValue_(1.7)
            fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            fade.setFromValue_(0.7)
            fade.setToValue_(0.0)
            group = Quartz.CAAnimationGroup.animation()
            group.setAnimations_([grow, fade])
            group.setDuration_(1.6 if state == "awake" else 1.0)
            group.setRepeatCount_(float("inf"))
            group.setTimingFunction_(_ease(Quartz.kCAMediaTimingFunctionEaseOut))
            self.ring.addAnimation_forKey_(group, "pulse")

        from mint.ui import badge
        if dim:
            self.status.flash_kind = None        # asleep or off: no red dot hanging on from before
        self.status.set_base(badge.kind_for(state))
        self._update_tint()

    # --- state tint -------------------------------------------------------------------

    TINTED = ("working", "thinking", "error", "done")

    def _update_tint(self) -> None:
        """The lower half in the state's colour: what the badge says right now, if it is worth a tint."""
        kind = self.status.current()
        kind = kind if kind in self.TINTED else None
        if kind == self._tint_kind:
            return
        self._tint_kind = kind
        from mint.ui.badge import COLORS
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.45)
        if kind is not None:
            rgb = COLORS[kind]
            strong = 0.85 if kind in ("error", "done") else 0.8
            self.tint.setColors_([gfx.cg(rgb, strong), gfx.cg(rgb, strong * 0.6), gfx.cg(rgb, 0.0)])
            self.tint.setLocations_([0.0, 0.5, 1.0])
            self.tint.setOpacity_(1.0)
        else:
            self.tint.setOpacity_(0.0)
        Quartz.CATransaction.commit()

    def set_progress(self, fraction: float | None) -> None:
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.5)
        if fraction is None:
            self.progress_ring.setOpacity_(0.0)
        else:
            self.progress_ring.setOpacity_(1.0)
            self.progress_ring.setStrokeEnd_(max(0.02, min(1.0, fraction)))
        Quartz.CATransaction.commit()

    # --- badge ------------------------------------------------------------------------

    # What the orb turns into for each kind of work: a symbol, a shape, a motion.
    GLYPHS = {"open": "square.grid.2x2.fill", "web": "globe", "click": "hand.tap.fill", "type": "character.cursor.ibeam",
              "read": "text.viewfinder", "look": "eye.fill", "scroll": "arrow.up.and.down", "write": "square.and.pencil",
              "pdf": "doc.richtext.fill", "calendar": "calendar", "mail": "envelope.fill", "timer": "timer",
              "sound": "speaker.wave.2.fill", "system": "gearshape.fill", "plan": "list.bullet",
              "switch": "rectangle.on.rectangle", "file": "folder.fill", "search": "magnifyingglass",
              "code": "curlybraces", "menu": "filemenu.and.selection", "think": "sparkles",
              "shot": "camera.viewfinder", "clip": "doc.on.clipboard"}
    # Kinds that make the orb a soft rounded tile (like an app or a document); the rest stay round.
    TILES = {"open", "file", "write", "pdf", "mail", "calendar", "switch", "code", "menu", "plan", "system", "clip"}

    def set_badge(self, kind: str, icon=None) -> None:
        """Become the task: the face melts away and the orb shows what it is doing."""
        self._morph_seq += 1
        d = self.d
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.badge.setOpacity_(0)
        if icon is not None:
            self.glyph_picture.setContents_(icon)
            self.glyph_picture.setHidden_(False)
            self.glyph_tint.setHidden_(True)
        else:
            self.glyph_mask.setContents_(gfx.symbol(self.GLYPHS.get(kind, "sparkles"), 40, "bold"))
            self.glyph_tint.setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.96).CGColor())
            self.glyph_tint.setHidden_(False)
            self.glyph_picture.setHidden_(True)
        Quartz.CATransaction.commit()
        self.glyph.setShadowOpacity_(0.25)
        self._fade(self.face, 0.0, 0.18, scale=0.55)
        self._fade(self.glyph, 1.0, 0.2)
        self._spring(self.glyph, 0.2, 1.0, damping=9, stiffness=240, key="in")
        tile = kind in self.TILES or icon is not None
        self._reshape(d * (0.3 if tile else 0.5))
        if not kinetics.reduce_motion():
            self._body_keys("transform.scale.x", [0.0, 0.12, -0.06, 0.0], 0.42)     # a little jelly squish
        self._kind_motion(kind)

    def _fade(self, layer, opacity, duration, scale=None):
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(duration)
        layer.setOpacity_(opacity)
        if scale is not None:
            layer.setAffineTransform_(Quartz.CGAffineTransformMakeScale(scale, scale) if opacity == 0
                                      else Quartz.CGAffineTransformIdentity)
        Quartz.CATransaction.commit()

    def _reshape(self, radius):
        """Morph the orb between a circle and a soft rounded tile."""
        current = self.core.presentationLayer().cornerRadius() if self.core.presentationLayer() else self.core.cornerRadius()
        if abs(current - radius) < 0.5:
            return
        morph = Quartz.CASpringAnimation.animationWithKeyPath_("cornerRadius")
        morph.setFromValue_(current)
        morph.setToValue_(radius)
        morph.setDamping_(11)
        morph.setStiffness_(180)
        morph.setMass_(0.8)
        morph.setDuration_(morph.settlingDuration())
        self.core.setCornerRadius_(radius)
        self.core.addAnimation_forKey_(morph, "shape")

    def _spring(self, layer, start, end, damping=8, stiffness=200, key="pop"):
        pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
        pop.setFromValue_(start)
        pop.setToValue_(end)
        pop.setDamping_(damping)
        pop.setStiffness_(stiffness)
        pop.setMass_(0.8)
        pop.setDuration_(pop.settlingDuration())
        layer.addAnimation_forKey_(pop, key)

    def _kind_motion(self, kind: str) -> None:
        target = self.glyph
        target.removeAnimationForKey_("kind")
        self.scan.removeAllAnimations()
        self.scan.setOpacity_(0)
        if kinetics.reduce_motion():           # the glyph alone says it; no loops
            return
        g = self.d / 2

        def forever(key, values, duration, times=None):
            animation = Quartz.CAKeyframeAnimation.animationWithKeyPath_(key)
            animation.setValues_(values)
            if times:
                animation.setKeyTimes_(times)
            animation.setDuration_(duration)
            animation.setRepeatCount_(float("inf"))
            animation.setCalculationMode_(Quartz.kCAAnimationCubic)
            target.addAnimation_forKey_(animation, "kind")

        if kind == "click":           # a tap: press in, spring back
            forever("transform.scale", [1.0, 0.78, 1.06, 1.0, 1.0], 0.8, [0, 0.14, 0.3, 0.42, 1])
        elif kind == "type":          # typing: tiny hops
            forever("transform.translation.y", [0.0, 1.6, 0.0, 1.0, 0.0], 0.34)
        elif kind == "search":        # a magnifier looking around in a little circle
            path = Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(g - 3.5, g - 3.5, 7, 7), None)
            orbit = Quartz.CAKeyframeAnimation.animationWithKeyPath_("position")
            orbit.setPath_(path)
            orbit.setDuration_(1.3)
            orbit.setRepeatCount_(float("inf"))
            orbit.setCalculationMode_(Quartz.kCAAnimationPaced)
            target.addAnimation_forKey_(orbit, "kind")
        elif kind in ("read", "look"):
            self.scan.setOpacity_(0.7)
            sweep = Quartz.CABasicAnimation.animationWithKeyPath_("position.y")
            sweep.setFromValue_(4.0)
            sweep.setToValue_(self.d - 4.0)
            sweep.setDuration_(0.9)
            sweep.setAutoreverses_(True)
            sweep.setRepeatCount_(float("inf"))
            sweep.setTimingFunction_(_ease())
            self.scan.addAnimation_forKey_(sweep, "scan")
        elif kind in ("file", "open", "switch", "pdf", "write"):   # a soft bob
            forever("transform.translation.y", [0.0, 1.8, 0.0], 1.0)
        elif kind in ("web", "scroll"):
            forever("transform.rotation.z", [0.0, 0.22, -0.22, 0.0], 1.4)
        elif kind == "code":
            forever("transform.rotation.z", [0.0, 0.12, -0.12, 0.0], 0.9)
        elif kind == "timer":
            forever("transform.rotation.z", [0.0, -2 * math.pi], 2.0)
        else:                          # breathe
            forever("transform.scale", [1.0, 1.1, 1.0], 1.1)

    def finish_badge(self, ok: bool, symbol: str | None = None) -> None:
        """The glyph becomes a tick or a cross, then the face comes back and the orb is round again."""
        self._morph_seq += 1
        seq = self._morph_seq
        self.glyph.removeAnimationForKey_("kind")
        self.scan.removeAllAnimations()
        self.scan.setOpacity_(0)
        color = gfx.GREEN if ok else gfx.RED
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.badge.setOpacity_(0)
        self.glyph_mask.setContents_(gfx.symbol(symbol or ("checkmark" if ok else "xmark"), 40, "bold"))
        self.glyph_tint.setBackgroundColor_(gfx.cg(gfx.mix(color, (1, 1, 1), 0.1)))
        self.glyph_tint.setHidden_(False)
        self.glyph_picture.setHidden_(True)
        self.glyph.setOpacity_(1.0)
        Quartz.CATransaction.commit()
        self.glyph.setShadowOpacity_(0.55)
        self._spring(self.glyph, 0.4, 1.0, damping=7, stiffness=300, key="in")
        self._reshape(self.d * (0.24 if self._morphed == "folder" else 0.5))
        # The state language: a green dot for a moment, or a red one (and a red tint) for longer.
        self.status.flash("done" if ok else "error", 1.8 if ok else 4.0)
        self._update_tint()
        if ok:
            self.hop()
            self._happy_until = time.monotonic() + 1.4
        else:
            self.shake()

        def back():
            if seq != self._morph_seq:          # a new task started meanwhile
                return
            self._fade(self.glyph, 0.0, 0.25)
            self._fade(self.face, 1.0, 0.3, scale=1.0)
            # The face comes back with how it went: content arcs, or flat tired eyes for a while.
            if not ok:
                self.expression("dash", 3.0)
        AppHelper.callLater(0.9, back)

    # --- flourishes -----------------------------------------------------------------

    def burst(self, rgb, stars: bool = False, amount: float = 1.0) -> None:
        if kinetics.reduce_motion():
            return
        cells = []
        for image, rate, scale in ((self._dot_image, 110, 0.36), (self._star_image, 40 if stars else 0, 0.6)):
            if not rate or image is None:
                continue
            cell = Quartz.CAEmitterCell.emitterCell()
            cell.setContents_(image)
            cell.setBirthRate_(rate * amount)
            cell.setLifetime_(0.85)
            cell.setLifetimeRange_(0.3)
            cell.setVelocity_(55)
            cell.setVelocityRange_(30)
            cell.setEmissionRange_(2 * math.pi)
            cell.setScale_(scale)
            cell.setScaleRange_(scale * 0.5)
            cell.setScaleSpeed_(-0.4)
            cell.setAlphaSpeed_(-1.1)
            cell.setSpin_(2.0)
            cell.setSpinRange_(5.0)
            cell.setColor_(gfx.cg(gfx.mix(rgb, gfx.light(rgb), 0.35)))
            cells.append(cell)
        self.emitter.setEmitterCells_(cells)
        self.emitter.setBeginTime_(Quartz.CACurrentMediaTime())
        self.emitter.setBirthRate_(1.0)
        AppHelper.callLater(0.12, lambda: self.emitter.setBirthRate_(0.0))

    def _body_keys(self, key, values, duration, times=None) -> None:
        animation = Quartz.CAKeyframeAnimation.animationWithKeyPath_(key)
        animation.setValues_(values)
        if times:
            animation.setKeyTimes_(times)
        animation.setDuration_(duration)
        animation.setAdditive_(True)
        self.body.addAnimation_forKey_(animation, key)

    def shake(self) -> None:
        if kinetics.reduce_motion():
            return
        self._body_keys("transform.translation.x", [0, -5, 5, -4, 4, -2, 0], 0.42)

    def hop(self) -> None:
        """A little jump with squash and stretch: it crouches, stretches tall on the way up, squashes wide as
        it lands and wobbles back (additive, so it rides on the breathing and hover scale)."""
        if kinetics.reduce_motion():
            return
        self._body_keys("transform.translation.y", [0, -1.5, 7, -2, 2, 0], 0.56, [0, 0.12, 0.42, 0.68, 0.86, 1])
        if prefs.get("notch_playful") is False:
            return
        self._body_keys("transform.scale.y", [0, -0.08, 0.09, 0.0, -0.09, 0.03, 0],
                        0.56, [0, 0.12, 0.3, 0.5, 0.68, 0.84, 1])
        self._body_keys("transform.scale.x", [0, 0.08, -0.07, 0.0, 0.1, -0.03, 0],
                        0.56, [0, 0.12, 0.3, 0.5, 0.68, 0.84, 1])

    def boot(self) -> None:
        if kinetics.reduce_motion():
            kinetics.basic(self.core, "opacity", 0.0, self.core.opacity(), 0.25, anim_key="boot", keep=False)
        else:
            self._spring(self.core, 0.45, 1.0, damping=7, stiffness=200, key="boot")
        self.burst(gfx.state_rgb("awake"), amount=0.45)
        self.blink()

    def celebrate(self) -> None:
        self.burst(gfx.accent(), stars=True, amount=1.5)
        self._happy_until = time.monotonic() + 1.8
        self.status.flash("done", 2.5)
        self._update_tint()
        self.hop()

    def stopped(self) -> None:
        """The user said stop: a firm red flash and a little flinch."""
        self.finish_badge(False, symbol="stop.fill")
        self.burst(gfx.RED, amount=0.5)
        flash = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        flash.setValues_([1.0, 0.3, 1.0])
        flash.setDuration_(0.35)
        self.core.addAnimation_forKey_(flash, "flash")


    # --- blinking and eye shapes --------------------------------------------------------

    @staticmethod
    def _blink_anim(hold: float = 0.0):
        """Close in 80 ms with a falling ease, open in 140 ms with a soft rise."""
        close = Quartz.CAMediaTimingFunction.functionWithControlPoints____(*_BLINK_CLOSE)
        open_ = Quartz.CAMediaTimingFunction.functionWithControlPoints____(*_BLINK_OPEN)
        total = 0.08 + hold + 0.14
        blink = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale.y")
        if hold:
            blink.setValues_([1.0, 0.08, 0.08, 1.0])
            blink.setKeyTimes_([0.0, 0.08 / total, (0.08 + hold) / total, 1.0])
            blink.setTimingFunctions_([close, _ease(Quartz.kCAMediaTimingFunctionLinear), open_])
        else:
            blink.setValues_([1.0, 0.08, 1.0])
            blink.setKeyTimes_([0.0, 0.08 / total, 1.0])
            blink.setTimingFunctions_([close, open_])
        blink.setDuration_(total)
        return blink

    def blink(self, double: bool = False) -> None:
        for eye, _ in self.eyes:
            eye.addAnimation_forKey_(self._blink_anim(), "blink")
        if double:
            AppHelper.callLater(0.3, self.blink)

    def wink(self) -> None:
        eye = self.eyes[1][0]
        eye.addAnimation_forKey_(self._blink_anim(hold=0.18), "blink")

    def expression(self, name: str, seconds: float | None = None) -> None:
        """Eyes-only expression: "dash" (flat: error, tired), "halfmoon" (annoyed), "arc" (content),
        "dot" (small, looking), "wide" (surprised), "normal". For `seconds`, or until the next call."""
        if name not in EXPRESSIONS or name == "normal":
            self._expr, self._expr_until = "normal", 0.0
            return
        self._expr = name
        self._expr_until = time.monotonic() + seconds if seconds else float("inf")

    def _shape_for(self, now: float, closed: bool, happy: bool) -> str:
        if closed or now < self._yawn_until:
            return "closed"
        if happy:
            return "arc"
        if now < self._expr_until:
            return self._expr
        return "normal"

    def _set_shape(self, shape: str, quick: bool = False) -> None:
        """Morph the eyes to a new shape; a big change hides behind a blink (swapped while shut)."""
        old, self._shape = self._shape, shape
        self._shape_seq += 1
        seq = self._shape_seq
        if quick or "closed" in (old, shape):
            self._apply_shape(shape)
            return
        self.blink()

        def swap():
            if seq == self._shape_seq:
                self._apply_shape(shape)
        AppHelper.callLater(0.075, swap)

    def _apply_shape(self, shape: str) -> None:
        s = self._face_scale
        w, h, r, tilt, shines = EYES[shape]
        arc = shape == "arc"
        all_corners = (Quartz.kCALayerMinXMinYCorner | Quartz.kCALayerMaxXMinYCorner
                       | Quartz.kCALayerMinXMaxYCorner | Quartz.kCALayerMaxXMaxYCorner)
        bottom = Quartz.kCALayerMinXMinYCorner | Quartz.kCALayerMaxXMinYCorner
        for (eye, _), smile in zip(self.eyes, self.smiles):
            pres = eye.presentationLayer() or eye
            b = pres.bounds()
            kinetics.spring(eye, "bounds", (0.0, 0.0, b.size.width, b.size.height), (0.0, 0.0, w * s, h * s),
                            "bouncy", anim_key="eye-shape")
            kinetics.spring(eye, "cornerRadius", pres.cornerRadius(), r * s, "bouncy", anim_key="eye-round")
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setAnimationDuration_(0.12)
            eye.setMaskedCorners_(bottom if shape == "halfmoon" else all_corners)
            eye.setOpacity_(0.0 if arc else 1.0)
            smile.setOpacity_(1.0 if arc else 0.0)
            Quartz.CATransaction.commit()
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setDisableActions_(True)
            big, small = (eye.sublayers() or [None, None])[:2]
            k = 1.15 if shape == "wide" else (0.7 if shape == "dot" else 1.0)
            for shine, (fx, fy, size), on in ((big, (0.7, 0.73, 2.3), shines >= 1),
                                              (small, (0.32, 0.29, 1.1), shines >= 2)):
                if shine is None:
                    continue
                shine.setHidden_(not on)
                shine.setBounds_(Quartz.CGRectMake(0, 0, size * k * s, size * k * s))
                shine.setCornerRadius_(size * k * s / 2)
                shine.setPosition_(Quartz.CGPointMake(w * s * fx, h * s * fy))
            Quartz.CATransaction.commit()
        self._tilt_target = tilt

    # --- poke play ----------------------------------------------------------------------

    ROLL, WOOZY = 1.4, 1.5          # dizzy: the eyes roll twice, then a woozy sway

    def poke(self, dx: float = 0.0, dy: float = 0.0) -> str:
        """A click on the face. (dx, dy): where, from the face's centre (screen points; y up).
        -> "slap" | "dizzy" | "annoyed" | "ignored" | "blink" | "off". The state underneath carries on."""
        if prefs.get("poke_play") is False:
            return "off"
        now = time.monotonic()
        if kinetics.reduce_motion():
            self.blink()
            return "blink"
        if now < self._annoyed_until:
            self.shake()                         # "I said stop"
            return "ignored"
        self._pokes = [t for t in self._pokes if now - t < 6.0] + [now]
        if len(self._pokes) >= 6:
            self._pokes = []
            self._annoyed()
            return "annoyed"
        quick = [t for t in self._pokes if now - t < 1.3]
        if len(quick) >= 3 and now - self._roll_at > self.ROLL + self.WOOZY:
            self._dizzy()
            return "dizzy"
        self._slap(dx, dy)
        return "slap"

    def _sound(self, name: str) -> None:
        try:
            from mint.ui import sfx
            sfx.play(name)
        except Exception:
            pass

    def _squash(self, k: float = 0.15, stiffness: float = 180.0, damping: float = 9.0) -> None:
        """Squashed wide (k) then a wobbly spring back; additive, so it rides on the breathing scale.
        Animation: Calm settles without the wobble (critically damped)."""
        if kinetics.calm():
            damping = max(damping, 2.0 * stiffness ** 0.5)
        for key, value in (("transform.scale.x", k), ("transform.scale.y", -k)):
            wobble = Quartz.CASpringAnimation.animationWithKeyPath_(key)
            wobble.setFromValue_(value)
            wobble.setToValue_(0.0)
            wobble.setAdditive_(True)
            wobble.setStiffness_(stiffness)
            wobble.setDamping_(damping)
            wobble.setMass_(1.0)
            wobble.setDuration_(min(1.2, wobble.settlingDuration()))
            self.body.addAnimation_forKey_(wobble, "poke-" + key)

    def _slap(self, dx: float, dy: float) -> None:
        self._squash(0.15)
        side = -1.0 if dx > 0 else (1.0 if dx < 0 else random.choice((-1.0, 1.0)))
        dist = math.hypot(dx, dy) or 1.0
        # The eyes jerk away from the hit (one slides round behind the curve), squeezed shut a moment.
        self._kick = [side * 1.0, -0.35 * dy / dist]
        self._kick_v = [0.0, 0.0]
        self._expr, self._expr_until = "dash", time.monotonic() + 0.28
        self._quick_shape = True
        self._sound("poke")

    def _dizzy(self) -> None:
        self._roll_at = time.monotonic()
        self._squash(0.1)
        self._kick = [0.0, 0.0]
        self._sound("dizzy")
        self._dizzy_stars(self.ROLL + self.WOOZY)
        AppHelper.callLater(self.ROLL, lambda: self._body_keys(
            "transform.rotation.z", [0, 0.16, -0.13, 0.1, -0.06, 0.02, 0], self.WOOZY))

    def _annoyed(self) -> None:
        self._annoyed_until = time.monotonic() + 4.0
        self.expression("halfmoon", 4.0)
        self._quick_shape = True
        self._squash(-0.05, stiffness=260.0, damping=12.0)
        self._sound("poke")
        for i, side in enumerate((-1, 1)):
            AppHelper.callLater(0.3 + i * 0.32, lambda side=side: self._puff(side))

    def _puff(self, side: int) -> None:
        """A tiny huff of steam off one side of the head."""
        d = self.d
        bx, by = self._bc
        size = max(3.0, d * 0.14)
        down = -1 if self.in_notch else 1
        puff = Quartz.CALayer.layer()
        puff.setBounds_(Quartz.CGRectMake(0, 0, size, size))
        puff.setCornerRadius_(size / 2)
        puff.setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.95).CGColor())
        puff.setShadowColor_(AppKit.NSColor.whiteColor().CGColor())     # soft, like steam
        puff.setShadowOpacity_(0.8)
        puff.setShadowRadius_(size * 0.4)
        puff.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        start = (bx + side * d * 0.46, by + down * d * 0.3)
        puff.setPosition_(Quartz.CGPointMake(*start))
        puff.setOpacity_(0.0)
        self.body.addSublayer_(puff)
        end = (start[0] + side * d * 0.28, start[1] + down * d * 0.22)
        kinetics.basic(puff, "position", start, end, 0.6, anim_key="puff-p", keep=False)
        kinetics.basic(puff, "transform.scale", 0.4, 1.6, 0.6, anim_key="puff-s", keep=False)
        fade = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        fade.setValues_([0.0, 0.95, 0.0])
        fade.setKeyTimes_([0, 0.2, 1])
        fade.setDuration_(0.6)
        puff.addAnimation_forKey_(fade, "puff-o")
        AppHelper.callLater(0.62, puff.removeFromSuperlayer)

    def _dizzy_stars(self, seconds: float) -> None:
        """Three little stars circling the head (not in the notch: no room above)."""
        if self.in_notch or self.d < 30:
            return
        d = self.d
        bx, by = self._bc
        w, h = d * 0.95, d * 0.26
        ring = Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(bx - w / 2, by + d * 0.42 - h / 2, w, h), None)
        size = d * 0.2
        for i in range(3):
            star = Quartz.CALayer.layer()
            star.setBounds_(Quartz.CGRectMake(0, 0, size, size))
            mask = Quartz.CALayer.layer()
            mask.setFrame_(Quartz.CGRectMake(0, 0, size, size))
            mask.setContents_(gfx.symbol("star.fill", size * 2, "bold"))
            mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
            star.setMask_(mask)
            star.setBackgroundColor_(gfx.cg((1.0, 0.86, 0.35)))
            star.setShadowColor_(gfx.cg(INK))
            star.setShadowOpacity_(0.35)
            star.setShadowRadius_(1)
            star.setShadowOffset_(Quartz.CGSizeMake(0, 0))
            star.setOpacity_(0.0)
            self.body.addSublayer_(star)
            orbit = Quartz.CAKeyframeAnimation.animationWithKeyPath_("position")
            orbit.setPath_(ring)
            orbit.setDuration_(0.9)
            orbit.setRepeatCount_(float("inf"))
            orbit.setCalculationMode_(Quartz.kCAAnimationPaced)
            orbit.setTimeOffset_(i * 0.3)
            star.addAnimation_forKey_(orbit, "orbit")
            fade = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
            fade.setValues_([0.0, 1.0, 1.0, 0.0])
            fade.setKeyTimes_([0, 0.1, 0.85, 1])
            fade.setDuration_(seconds)
            star.addAnimation_forKey_(fade, "fade")
            AppHelper.callLater(seconds, star.removeFromSuperlayer)

    # --- scenes (greeting, files, progress) ----------------------------------------------

    def wave(self, seconds: float = 1.6) -> None:
        """Two little round hands come out beside the face, wave, and pop away."""
        if kinetics.reduce_motion():
            self.blink()
            return
        d = self.d
        bx, by = self._bc
        size = max(5.0, d * 0.27)
        hands = []
        for i, side in enumerate((-1, 1)):
            hand = Quartz.CAGradientLayer.layer()
            hand.setType_(Quartz.kCAGradientLayerRadial)
            hand.setStartPoint_(Quartz.CGPointMake(0.38, 0.66))
            hand.setEndPoint_(Quartz.CGPointMake(1.1, -0.1))
            hand.setColors_([gfx.cg((1.0, 1.0, 1.0)), gfx.cg(gfx.mix(gfx.light(gfx.accent()), (0.85, 0.88, 0.95), 0.4))])
            hand.setBounds_(Quartz.CGRectMake(0, 0, size, size))
            hand.setCornerRadius_(size / 2)
            hand.setMasksToBounds_(False)
            hand.setBorderWidth_(max(0.5, size * 0.06))
            hand.setBorderColor_(AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.75).CGColor())
            hand.setShadowColor_(gfx.cg(INK))
            hand.setShadowOpacity_(0.3)
            hand.setShadowRadius_(max(1.0, size * 0.12))
            hand.setShadowOffset_(Quartz.CGSizeMake(0, -0.5))
            # The wrist is below the hand: it swings from there.
            hand.setAnchorPoint_(Quartz.CGPointMake(0.5, -0.9))
            hand.setPosition_(Quartz.CGPointMake(bx + side * d * 0.66, by - d * 0.42))
            self.body.addSublayer_(hand)
            hands.append(hand)
            kinetics.pop(hand, "bouncy", start=0.1, delay=i * 0.07)
            begin = Quartz.CACurrentMediaTime() + i * 0.07
            # Up from below the face, then a wave from the wrist (both additive: they ride on the pop).
            rise = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.translation.y")
            rise.setValues_([-d * 0.3, d * 0.08, 0.0])
            rise.setKeyTimes_([0, 0.6, 1])
            rise.setDuration_(0.32)
            rise.setAdditive_(True)
            rise.setBeginTime_(begin)
            rise.setFillMode_(Quartz.kCAFillModeBackwards)
            hand.addAnimation_forKey_(rise, "rise")
            swing = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.rotation.z")
            a = 0.62 * -side
            # Opposite phases, so the hands wave like two little paws rather than one stiff pair.
            swing.setValues_([0.0, a, -a * 0.45, a, -a * 0.45, a * 0.8, 0.0] if i == 0 else
                             [0.0, -a * 0.45, a, -a * 0.45, a, -a * 0.3, 0.0])
            swing.setCalculationMode_(Quartz.kCAAnimationCubic)
            swing.setDuration_(max(0.6, seconds - 0.45))
            swing.setAdditive_(True)
            swing.setBeginTime_(begin + 0.22)
            swing.setFillMode_(Quartz.kCAFillModeBackwards)
            hand.addAnimation_forKey_(swing, "wave")

        def away():
            for hand in hands:
                kinetics.basic(hand, "transform.scale", 1.0, 0.05, 0.2, timing=Quartz.kCAMediaTimingFunctionEaseIn,
                               anim_key="away")
                kinetics.basic(hand, "opacity", 1.0, 0.0, 0.2, anim_key="away-o")
            AppHelper.callLater(0.22, lambda: [h.removeFromSuperlayer() for h in hands])
        AppHelper.callLater(max(0.5, seconds - 0.2), away)
        self.blink()

    def morph(self, shape: str | None = None) -> None:
        """"folder": the round face becomes a rounded window/folder with a black bar on top (the eyes
        stay); None: back to the circle. A spring on the bounds and corners."""
        folder = shape == "folder"
        if folder == (self._morphed == "folder"):
            return
        self._morphed = "folder" if folder else None
        d = self.d
        w, h = (d * 1.18, d * 1.0) if folder else (d, d)
        radius = d * 0.24 if folder else d / 2
        pres = self.core.presentationLayer() or self.core
        b = pres.bounds()
        # Bounds grow round the centre (origin below zero), so everything inside stays centred.
        kinetics.spring(self.core, "bounds", (b.origin.x, b.origin.y, b.size.width, b.size.height),
                        (-(w - d) / 2, -(h - d) / 2, w, h), "bouncy", anim_key="morph")
        kinetics.spring(self.core, "cornerRadius", pres.cornerRadius(), radius, "bouncy", anim_key="shape")
        bar = getattr(self, "_bar", None)
        if bar is None:
            bar = Quartz.CALayer.layer()
            bar.setBackgroundColor_(gfx.cg((0.02, 0.02, 0.03), 0.95))
            bar.setAnchorPoint_(Quartz.CGPointMake(0.5, 1.0))
            bar.setOpacity_(0.0)
            self.core.insertSublayer_below_(bar, self.face)
            self._bar = bar
        inset, bh = d * 0.075, d * 0.21
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        bar.setBounds_(Quartz.CGRectMake(0, 0, d * 1.18 - 2 * inset, bh))
        bar.setCornerRadius_(bh / 2)
        bar.setPosition_(Quartz.CGPointMake(d / 2, d / 2 + d * 0.5 - inset))
        Quartz.CATransaction.commit()
        kinetics.fade(bar, folder, 0.18 if folder else 0.12, delay=0.05 if folder else 0.0)
        if folder:
            kinetics.spring(bar, "transform.scale", 0.4, 1.0, "bouncy", anim_key="bar-in")
        bx, by = self._bc
        self.status.move_to((bx - w / 2 + d * 0.1, by + h / 2 - d * 0.08) if folder else self.status._home())

    def thumb_mode(self, on: bool = True) -> None:
        """Small and glowless, to ride a progress bar as its thumb; no badge, no rings."""
        on = bool(on)
        if on == self._thumb:
            return
        self._thumb = on
        self.status.hide_for("thumb", on)
        extras = (self.ring, self.spinner, self.progress_ring)
        if on:
            self._thumb_hidden = [layer.isHidden() for layer in extras]
            for layer in extras:
                layer.setHidden_(True)
        else:
            for layer, was in zip(extras, getattr(self, "_thumb_hidden", [False] * 3)):
                layer.setHidden_(was)

    # --- per frame ------------------------------------------------------------------

    def tick(self, now: float, level: float, look, hovering: bool, visible_work: bool) -> None:
        """30 Hz. `look` is a (dx, dy) direction in screen points or None."""
        state = self.state
        self._busy = bool(visible_work) or state in ("thinking", "working")
        self._hover += ((1.0 if hovering else 0.0) - self._hover) * 0.25
        asleep = state in ("sleeping", "paused", "offline")
        breath = 0.03 * math.sin(now * (1.2 if asleep else 2.1))
        annoyed = now < self._annoyed_until
        self._swell += ((1.12 if annoyed else 1.0) - self._swell) * 0.2
        self._size += ((0.62 if self._thumb else 1.0) - self._size) * 0.25
        scale = ((0.86 if asleep else 1.0) + 0.1 * self._hover + breath + level * 0.14) * self._swell * self._size

        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.body.setAffineTransform_(Quartz.CGAffineTransformMakeScale(scale, scale))
        glow = (0.18 if asleep else 0.35) + level * 0.5 + 0.15 * self._hover
        self.glow.setOpacity_(0.0 if self._thumb else glow)
        g = 1.0 + level * 0.7
        self.glow.setAffineTransform_(Quartz.CGAffineTransformMakeScale(g, g))
        Quartz.CATransaction.commit()
        self.status.tick(now)
        self.status.hover(hovering and not self._thumb)
        self._update_tint()
        if asleep and prefs.get("face") and now >= self._next_z:
            self._next_z = now + random.uniform(1.6, 2.6)
            self._snooze()

        if prefs.get("face"):
            self._face(now, level, look)
        playing = annoyed or now - self._roll_at < self.ROLL + self.WOOZY
        if not asleep and not visible_work and not playing and now >= self._next_whimsy:
            calm = kinetics.calm()                   # Calm / Minimal / Reduce motion: only the quiet ones, rarer
            self._next_whimsy = now + (random.uniform(25.0, 45.0) if calm else random.uniform(8.0, 15.0))
            choice = random.choice(("wink", "look") if calm else ("hop", "wink", "sparkle", "wiggle", "look", "yawn"))
            if choice == "hop":
                self.hop()
            elif choice == "look" and prefs.get("face"):
                self._look_around_until = now + 2.2
            elif choice == "yawn" and prefs.get("face"):
                self.yawn()
            elif choice == "wink" and prefs.get("face"):
                self.wink()
            elif choice == "sparkle":
                self.burst(gfx.accent(), stars=True, amount=0.3)
            else:
                self._body_keys("transform.rotation.z", [0, 0.14, -0.14, 0], 0.8)

    def _sphere(self, x0: float, y0: float, yaw: float, pitch: float):
        """A point drawn on the ball at (x0, y0) from the centre, after the ball turns by yaw (right) and
        pitch (up): -> (x, y, squeeze_x, squeeze_y, in_front)."""
        r = self.d * 0.47
        x, y = x0 / r, y0 / r
        z = math.sqrt(max(0.0, 1.0 - x * x - y * y))
        x, z = x * math.cos(yaw) + z * math.sin(yaw), -x * math.sin(yaw) + z * math.cos(yaw)
        y, z = y * math.cos(pitch) + z * math.sin(pitch), -y * math.sin(pitch) + z * math.cos(pitch)
        return (x * r, y * r, math.sqrt(max(0.0, 1.0 - x * x)), math.sqrt(max(0.0, 1.0 - y * y)), z > 0.0)

    def _face(self, now: float, level: float, look) -> None:
        state = self.state
        s = self._face_scale
        happy = now < self._happy_until
        closed = state in ("sleeping", "paused", "starting", "offline")
        # Where it looks, as turns of the ball (radians): yaw right, pitch up.
        tp, tq = 0.0, 0.0
        if now < self._look_around_until:
            left = self._look_around_until - now
            tp, tq = 0.42 * math.sin(left * 3.2), 0.14
        elif state == "thinking":
            tp, tq = 0.34 + 0.08 * math.sin(now * 1.7), 0.32
        elif look is not None:
            dx, dy = look
            dist = math.hypot(dx, dy)
            if dist > 1:
                pull = min(1.0, dist / 260)
                tp, tq = 0.72 * pull * dx / dist, 0.46 * pull * dy / dist
        elif state == "working":
            tq = -0.3
        self._gaze[0] += (tp - self._gaze[0]) * 0.14          # the eyes trail the pointer a little
        self._gaze[1] += (tq - self._gaze[1]) * 0.14
        for i in (0, 1):                                      # a slap's kick springs back, with a little recoil
            self._kick_v[i] += (-95.0 * self._kick[i] - 9.5 * self._kick_v[i]) / 30.0
            self._kick[i] += self._kick_v[i] / 30.0
        yaw = self._gaze[0] + self._kick[0]
        pitch = self._gaze[1] + self._kick[1]
        rolling = now - self._roll_at
        if rolling < self.ROLL:                               # dizzy: the ball rolls forward twice
            t = rolling / self.ROLL
            pitch += (t * t * (3 - 2 * t)) * 4 * math.pi
            yaw += 0.22 * math.sin(rolling * 7.0)
        elif rolling < self.ROLL + self.WOOZY:                # then a woozy sway
            fade = 1 - (rolling - self.ROLL) / self.WOOZY
            yaw += 0.34 * fade * math.sin(rolling * 5.5)
            pitch += 0.12 * fade * math.sin(rolling * 11.0)
        self._face_drop += ((self.d * 0.1 if self._morphed == "folder" else 0.0) - self._face_drop) * 0.25
        self._tilt = getattr(self, "_tilt", 0.0)
        self._tilt += (getattr(self, "_tilt_target", 0.0) - self._tilt) * 0.3

        shape = self._shape_for(now, closed, happy)
        if shape != self._shape:
            self._set_shape(shape, quick=getattr(self, "_quick_shape", False))
        self._quick_shape = False

        d = self.d
        cx, cy = d / 2, d / 2 - self._face_drop
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        for side, socket, (eye, dx) in zip((-1, 1), self._sockets, self.eyes):
            x, y, fx, fy, front = self._sphere(dx, 2 * s, yaw, pitch)
            socket.setPosition_(Quartz.CGPointMake(cx + x, cy + y))
            socket.setHidden_(not front)
            t = Quartz.CGAffineTransformMakeScale(max(0.05, fx), max(0.05, fy))
            socket.setAffineTransform_(Quartz.CGAffineTransformRotate(t, side * self._tilt))
        self._eye_offset = [self._sockets[0].position().x - (cx - 6.5 * s), self._sockets[0].position().y - (cy + 2 * s)]
        cheek_on = 0.9 if shape == "arc" and happy else 0.45                 # always a little rosy
        for cheek, x0 in zip(self.cheeks, (-9.4 * s, 9.4 * s)):
            x, y, fx, fy, front = self._sphere(x0, -4 * s, yaw, pitch)
            cheek.setPosition_(Quartz.CGPointMake(cx + x, cy + y))
            cheek.setAffineTransform_(Quartz.CGAffineTransformMakeScale(max(0.05, fx), max(0.05, fy)))
            cheek.setOpacity_(cheek_on if front else 0.0)
        speaking = state == "speaking"
        yawning = now < self._yawn_until
        x, y, fx, fy, front = self._sphere(0.0, -6 * s, yaw, pitch)
        self.rest_mouth.setOpacity_(0.0 if speaking or yawning or happy or not front else 0.85)
        self.rest_mouth.setPosition_(Quartz.CGPointMake(cx + x, cy + y))
        self.rest_mouth.setAffineTransform_(Quartz.CGAffineTransformMakeScale(max(0.05, fx), max(0.05, fy)))
        x, y, fx, fy, front = self._sphere(0.0, -7 * s, yaw, pitch)
        self.mouth.setOpacity_(0.85 if (speaking or yawning) and front else 0.0)
        self.mouth.setPosition_(Quartz.CGPointMake(cx + x, cy + y))
        self.mouth.setAffineTransform_(Quartz.CGAffineTransformMakeScale(max(0.05, fx), max(0.05, fy)))
        if yawning and not speaking:
            open_ = math.sin(math.pi * (1 - (self._yawn_until - now) / 1.3))
            h = (2 + 6 * open_) * s
            self.mouth.setBounds_(Quartz.CGRectMake(0, 0, (5 + 2 * open_) * s, h))
            self.mouth.setCornerRadius_(h / 2)
        if speaking:
            h = (1.6 + 6.0 * min(1.0, level * 1.6)) * s
            self.mouth.setBounds_(Quartz.CGRectMake(0, 0, (6 + 2 * level) * s, h))
            self.mouth.setCornerRadius_(min(h, 6 * s) / 2)
        Quartz.CATransaction.commit()
        self._eye_mode = "happy" if shape == "arc" else ("closed" if shape == "closed" else "open")

        blinks = shape in ("normal", "wide", "dot") and rolling > self.ROLL
        if blinks and now >= self._next_blink:
            self.blink(double=random.random() < (0.3 if self._busy else 0.2))
            self._next_blink = now + (random.uniform(0.9, 2.4) if self._busy else random.uniform(2.2, 5.0))
