"""The orb: a small Siri-like swirl with a face.

Built from Core Animation layers into any layer-backed view, centred on a given
point. Two conic gradients turn against each other inside a circle, faster
when Mint thinks or speaks; a glass highlight sits on top; the face (eyes
that blink and look around, a mouth that moves with the voice, cheeks when
happy) sits on that. Around it: a glow that swells with the voice, a pulse
ring, a comet spinner while busy, a progress ring for long tasks, and a badge
showing what kind of work is going on.

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
from mint.core import prefs

INK = (0.04, 0.06, 0.12)          # the face's colour


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
        self._leaf_droop = 0.0
        self._next_z = 0.0
        self._yawn_until = 0.0
        self._look_around_until = 0.0
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
        for dx in (-6.5 * s, 6.5 * s):
            eye = Quartz.CALayer.layer()
            eye.setBounds_(Quartz.CGRectMake(0, 0, 5 * s, 9 * s))
            eye.setCornerRadius_(2.5 * s)
            eye.setBackgroundColor_(gfx.cg(INK, 0.9))
            # Two catch-lights make the eyes sparkle.
            for (hx, hy, hd) in ((3.5, 6.6, 2.3), (1.6, 2.6, 1.1)):
                shine = Quartz.CALayer.layer()
                shine.setBounds_(Quartz.CGRectMake(0, 0, hd * s, hd * s))
                shine.setCornerRadius_(hd * s / 2)
                shine.setPosition_(Quartz.CGPointMake(hx * s, hy * s))
                shine.setBackgroundColor_(white_(1.0, 0.95))
                eye.addSublayer_(shine)
            face.addSublayer_(eye)
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
            face.addSublayer_(smile)
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
        self._build_leaf(body, bc, d)

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

    def _build_leaf(self, body, bc, d) -> None:
        """A little mint sprout on top: two leaves on a stem. It sways, perks up when the
        pointer is near, wiggles when happy and droops while asleep."""
        s = d / 44.0
        w, h = 30 * s, 22 * s
        sprout = Quartz.CALayer.layer()
        sprout.setBounds_(Quartz.CGRectMake(0, 0, w, h))
        sprout.setAnchorPoint_(Quartz.CGPointMake(0.5, 0.0))
        sprout.setPosition_(Quartz.CGPointMake(bc[0], bc[1] + d / 2 - 2.5 * s))
        body.addSublayer_(sprout)
        green, deep, vein = (0.55, 0.9, 0.62), (0.2, 0.55, 0.34), (0.3, 0.68, 0.45)
        stem = Quartz.CAShapeLayer.layer()
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(path, None, 15 * s, 0)
        Quartz.CGPathAddQuadCurveToPoint(path, None, 14 * s, 3.5 * s, 15 * s, 6.5 * s)
        stem.setPath_(path)
        stem.setFillColor_(None)
        stem.setStrokeColor_(gfx.cg(deep))
        stem.setLineWidth_(1.4 * s)
        stem.setLineCap_(Quartz.kCALineCapRound)
        sprout.addSublayer_(stem)
        self.leaves = []
        for side, size in ((-1, 1.0), (1, 1.12)):
            bx, by = 15 * s, 6.5 * s
            tip = (15 + side * 11 * size, 6.5 + 8.5 * size)
            leaf = Quartz.CAShapeLayer.layer()
            leaf.setBounds_(Quartz.CGRectMake(0, 0, w, h))
            leaf.setAnchorPoint_(Quartz.CGPointMake(bx / w, by / h))
            leaf.setPosition_(Quartz.CGPointMake(bx, by))
            outline = Quartz.CGPathCreateMutable()
            Quartz.CGPathMoveToPoint(outline, None, bx, by)
            Quartz.CGPathAddCurveToPoint(outline, None, (15 + side * 4 * size) * s, (6.5 - 1.5 * size) * s,
                                         (15 + side * 11 * size) * s, (6.5 + 2 * size) * s, tip[0] * s, tip[1] * s)
            Quartz.CGPathAddCurveToPoint(outline, None, (15 + side * 7 * size) * s, (6.5 + 10 * size) * s,
                                         (15 + side * 1 * size) * s, (6.5 + 6 * size) * s, bx, by)
            leaf.setPath_(outline)
            leaf.setFillColor_(gfx.cg(green))
            leaf.setStrokeColor_(gfx.cg(deep))
            leaf.setLineWidth_(0.9 * s)
            leaf.setLineJoin_(Quartz.kCALineJoinRound)
            rib = Quartz.CAShapeLayer.layer()
            rib.setFrame_(Quartz.CGRectMake(0, 0, w, h))
            line = Quartz.CGPathCreateMutable()
            Quartz.CGPathMoveToPoint(line, None, bx, by)
            Quartz.CGPathAddQuadCurveToPoint(line, None, (15 + side * 6 * size) * s, (6.5 + 3 * size) * s,
                                             (15 + side * 8.5 * size) * s, (6.5 + 6.2 * size) * s)
            rib.setPath_(line)
            rib.setFillColor_(None)
            rib.setStrokeColor_(gfx.cg(vein))
            rib.setLineWidth_(0.7 * s)
            rib.setLineCap_(Quartz.kCALineCapRound)
            leaf.addSublayer_(rib)
            sprout.addSublayer_(leaf)
            self.leaves.append((leaf, side))
        sway = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.rotation.z")
        sway.setValues_([0.0, 0.09, 0.0, -0.09, 0.0])
        sway.setDuration_(3.4)
        sway.setRepeatCount_(float("inf"))
        sway.setAdditive_(True)
        sway.setCalculationMode_(Quartz.kCAAnimationCubic)
        sprout.addAnimation_forKey_(sway, "sway")
        self.sprout = sprout

    def leaf_wiggle(self) -> None:
        """The leaves flutter (happy, poked, or just because)."""
        for leaf, side in getattr(self, "leaves", []):
            wiggle = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.rotation.z")
            wiggle.setValues_([0, side * 0.35, -side * 0.15, side * 0.25, 0])
            wiggle.setDuration_(0.6)
            wiggle.setAdditive_(True)
            leaf.addAnimation_forKey_(wiggle, "wiggle")

    def leaf_twirl(self) -> None:
        spin = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.rotation.y")
        spin.setValues_([0, 2 * math.pi])
        spin.setDuration_(0.8)
        spin.setTimingFunction_(_ease())
        self.sprout.addAnimation_forKey_(spin, "twirl")

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
        self._reshape(self.d * 0.5)
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
        AppHelper.callLater(0.9, back)

    # --- flourishes -----------------------------------------------------------------

    def burst(self, rgb, stars: bool = False, amount: float = 1.0) -> None:
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
        self._body_keys("transform.translation.x", [0, -5, 5, -4, 4, -2, 0], 0.42)

    def hop(self) -> None:
        self._body_keys("transform.translation.y", [0, 7, -2, 2, 0], 0.5, [0, 0.35, 0.65, 0.85, 1])

    def boot(self) -> None:
        self._spring(self.core, 0.45, 1.0, damping=7, stiffness=200, key="boot")
        self.burst(gfx.state_rgb("awake"), amount=0.45)
        self.blink()

    def celebrate(self) -> None:
        self.burst(gfx.accent(), stars=True, amount=1.5)
        self._happy_until = time.monotonic() + 1.8
        self.hop()

    def stopped(self) -> None:
        """The user said stop: a firm red flash and a little flinch."""
        self.finish_badge(False, symbol="stop.fill")
        self.burst(gfx.RED, amount=0.5)
        flash = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        flash.setValues_([1.0, 0.3, 1.0])
        flash.setDuration_(0.35)
        self.core.addAnimation_forKey_(flash, "flash")

    def blink(self) -> None:
        for eye, _ in self.eyes:
            blink = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale.y")
            blink.setValues_([1.0, 0.1, 1.0])
            blink.setDuration_(0.16)
            eye.addAnimation_forKey_(blink, "blink")

    def wink(self) -> None:
        eye = self.eyes[1][0]
        wink = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale.y")
        wink.setValues_([1.0, 0.1, 0.1, 1.0])
        wink.setKeyTimes_([0, 0.25, 0.75, 1])
        wink.setDuration_(0.4)
        eye.addAnimation_forKey_(wink, "blink")

    # --- per frame ------------------------------------------------------------------

    def tick(self, now: float, level: float, look, hovering: bool, visible_work: bool) -> None:
        """30 Hz. `look` is a (dx, dy) direction in screen points or None."""
        state = self.state
        self._hover += ((1.0 if hovering else 0.0) - self._hover) * 0.25
        asleep = state in ("sleeping", "paused", "offline")
        breath = 0.03 * math.sin(now * (1.2 if asleep else 2.1))
        scale = (0.86 if asleep else 1.0) + 0.1 * self._hover + breath + level * 0.14

        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.body.setAffineTransform_(Quartz.CGAffineTransformMakeScale(scale, scale))
        self.glow.setOpacity_((0.18 if asleep else 0.35) + level * 0.5 + 0.15 * self._hover)
        g = 1.0 + level * 0.7
        self.glow.setAffineTransform_(Quartz.CGAffineTransformMakeScale(g, g))
        # The sprout: droops while asleep, perks up when the pointer comes close.
        self._leaf_droop += ((0.6 if asleep else 0.0) - self._leaf_droop) * 0.06
        for leaf, side in getattr(self, "leaves", []):
            leaf.setAffineTransform_(Quartz.CGAffineTransformMakeRotation(-side * self._leaf_droop))
        perk = 1.0 + 0.2 * self._hover
        if hasattr(self, "sprout"):
            self.sprout.setAffineTransform_(Quartz.CGAffineTransformMakeScale(perk, perk))
        Quartz.CATransaction.commit()
        if asleep and prefs.get("face") and now >= self._next_z:
            self._next_z = now + random.uniform(1.6, 2.6)
            self._snooze()

        if prefs.get("face"):
            self._face(now, level, look)
        if not asleep and not visible_work and now >= self._next_whimsy:
            self._next_whimsy = now + random.uniform(8.0, 15.0)
            choice = random.choice(("hop", "wink", "sparkle", "wiggle", "leaf", "twirl", "look", "yawn"))
            if choice == "hop":
                self.hop()
                self.leaf_wiggle()
            elif choice == "leaf":
                self.leaf_wiggle()
            elif choice == "twirl":
                self.leaf_twirl()
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

    def _face(self, now: float, level: float, look) -> None:
        state = self.state
        s = self._face_scale
        happy = now < self._happy_until
        closed = state in ("sleeping", "paused", "starting", "offline")
        tx, ty = 0.0, 0.0
        if now < self._look_around_until:
            left = self._look_around_until - now
            tx, ty = 3.0 * math.sin(left * 3.2), 1.0
        elif state == "thinking":
            tx, ty = 2.4 + 0.6 * math.sin(now * 1.7), 2.4
        elif look is not None:
            dx, dy = look
            dist = math.hypot(dx, dy)
            if dist > 1:
                pull = min(1.0, dist / 300)
                tx, ty = 3.0 * pull * dx / dist, 2.4 * pull * dy / dist
        elif state == "working":
            ty = -2.2
        self._eye_offset[0] += (tx * s - self._eye_offset[0]) * 0.22
        self._eye_offset[1] += (ty * s - self._eye_offset[1]) * 0.22

        yawning = now < self._yawn_until
        mode = "happy" if happy and not closed else ("closed" if closed or yawning else "open")
        d = self.d
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        for (eye, dx), smile in zip(self.eyes, self.smiles):
            x = d / 2 + dx + self._eye_offset[0]
            y = d / 2 + 2 * s + self._eye_offset[1]
            eye.setPosition_(Quartz.CGPointMake(x, y))
            smile.setPosition_(Quartz.CGPointMake(x, y - 1))
            if mode != self._eye_mode:
                eye.setOpacity_(0.0 if mode == "happy" else 1.0)
                smile.setOpacity_(1.0 if mode == "happy" else 0.0)
                eye.setBounds_(Quartz.CGRectMake(0, 0, 5 * s, (2 if mode == "closed" else 9) * s))
                for shine in eye.sublayers() or []:
                    shine.setHidden_(mode == "closed")
        for cheek in self.cheeks:
            cheek.setOpacity_(0.9 if mode == "happy" else 0.45)      # always a little rosy
        speaking = state == "speaking"
        self.mouth.setOpacity_(0.85 if speaking or yawning else 0.0)
        self.rest_mouth.setOpacity_(0.0 if speaking or yawning or mode == "happy" else 0.85)
        self.rest_mouth.setPosition_(Quartz.CGPointMake(d / 2 + self._eye_offset[0] * 0.6,
                                                        d / 2 - 6 * s + self._eye_offset[1] * 0.5))
        if yawning and not speaking:
            open_ = math.sin(math.pi * (1 - (self._yawn_until - now) / 1.3))
            h = (2 + 6 * open_) * s
            self.mouth.setBounds_(Quartz.CGRectMake(0, 0, (5 + 2 * open_) * s, h))
            self.mouth.setCornerRadius_(h / 2)
            self.mouth.setPosition_(Quartz.CGPointMake(d / 2, d / 2 - 7 * s))
        if speaking:
            h = (1.6 + 6.0 * min(1.0, level * 1.6)) * s
            self.mouth.setBounds_(Quartz.CGRectMake(0, 0, (6 + 2 * level) * s, h))
            self.mouth.setCornerRadius_(min(h, 6 * s) / 2)
            self.mouth.setPosition_(Quartz.CGPointMake(d / 2 + self._eye_offset[0] * 0.6,
                                                       d / 2 - 7 * s + self._eye_offset[1] * 0.5))
        Quartz.CATransaction.commit()
        self._eye_mode = mode

        if mode == "open" and now >= self._next_blink:
            self.blink()
            if random.random() < 0.2:
                AppHelper.callLater(0.28, self.blink)
            self._next_blink = now + random.uniform(2.2, 5.5)
