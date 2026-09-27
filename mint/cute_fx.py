"""Small cute flourishes around Mint's orb, a different one for each kind of action.

The orb already turns into the task (orb.set_badge). On top of that, each kind
of action gets its own little moment beside the orb:

    open app      stars fan out            web         a paper plane takes off
    search        a magnifier circles      typing      letters float up
    read / look   sparkles twinkle         scroll      chevrons bounce down
    files, pdf    papers flutter up        mail        an envelope flies off with a heart
    calendar      a calendar pops          timer       a clock circles
    sound         music notes              code        { } float up
    plan          ticks float up           screenshot  a camera flash ring
    thinking      sparkles                 anything    a sparkle or two

Finishing well sends up a couple of hearts; failing puts a tiny rain cloud over
the orb for a moment. Where a click lands, a paw print is stamped under the
ripple. All drawn on the effects overlay (click-through, out of Mint's own
screenshots). Off with the "cute_effects" setting.
"""

from __future__ import annotations

import logging
import math
import random
import time

import Quartz
from PyObjCTools import AppHelper

from . import gfx, prefs
from .critters import PINK, _keys, _symbol_layer, _text_layer, confetti, float_up

log = logging.getLogger("mint.cute_fx")

YELLOW = (1.0, 0.85, 0.35)
SKY = (0.6, 0.85, 1.0)
MINT = (0.6, 1.0, 0.8)
LILAC = (0.8, 0.7, 1.0)

# kind -> (motion, symbols)
KINDS = {
    "open": ("fan", ["star.fill", "sparkle", "star.fill"]),
    "web": ("fly", ["paperplane.fill"]),
    "search": ("orbit", ["magnifyingglass"]),
    "type": ("letters", []),
    "read": ("twinkle", ["sparkle"]),
    "look": ("twinkle", ["sparkle", "eye.fill"]),
    "scroll": ("bounce", ["chevron.down"]),
    "write": ("flutter", ["doc.fill", "pencil"]),
    "pdf": ("flutter", ["doc.richtext.fill"]),
    "file": ("flutter", ["doc.fill", "folder.fill"]),
    "clip": ("flutter", ["doc.on.clipboard.fill"]),
    "mail": ("fly", ["envelope.fill"]),
    "calendar": ("fan", ["calendar", "star.fill"]),
    "timer": ("orbit", ["clock.fill"]),
    "sound": ("float", ["music.note", "music.quarternote.3", "music.note"]),
    "system": ("fan", ["gearshape.fill", "sparkle"]),
    "plan": ("float", ["checkmark.circle.fill", "checkmark.circle.fill"]),
    "code": ("float", ["curlybraces", "chevron.left.forwardslash.chevron.right"]),
    "menu": ("fan", ["sparkle", "sparkle"]),
    "switch": ("orbit", ["rectangle.on.rectangle"]),
    "shot": ("flash", []),
    "think": ("twinkle", ["sparkle"]),
}


def enabled() -> bool:
    return prefs.get("cute_effects") is not False


def _colors():
    return [PINK, YELLOW, SKY, MINT, LILAC, gfx.light(gfx.accent())]


class CuteFX:
    def __init__(self) -> None:
        self.hud = None
        self._last: dict[str, float] = {}

    # --- where to draw ----------------------------------------------------------

    def _where(self):
        """(root layer, orb centre in that layer, direction toward the screen's middle)."""
        from .effects import fx
        if not fx.built or not fx._overlays or fx.origin is None:
            return None
        centre = fx.origin()
        if centre is None:
            return None
        for frame, _, root, _ in fx._overlays:
            if frame.origin.x <= centre[0] <= frame.origin.x + frame.size.width and \
                    frame.origin.y <= centre[1] <= frame.origin.y + frame.size.height:
                x, y = centre[0] - frame.origin.x, centre[1] - frame.origin.y
                return root, (x, y), (-1 if x > frame.size.width / 2 else 1), (1 if y < frame.size.height / 2 else -1)
        return None

    # --- hooks (any thread) -------------------------------------------------------

    def started(self, name: str) -> None:
        try:
            from . import activity
            kind = activity.kind(name)
        except Exception:
            kind = "think"
        now = time.monotonic()
        if now - self._last.get(kind, 0.0) < 1.4:        # typing in bursts: once is enough
            return
        self._last[kind] = now
        AppHelper.callAfter(self._safe, self.play, kind)

    def ended(self, ok: bool) -> None:
        AppHelper.callAfter(self._safe, self.finish, ok)

    def _safe(self, fn, *args):
        if not enabled():
            return
        try:
            fn(*args)
        except Exception:
            log.debug("cute effect failed", exc_info=True)

    # --- the flourishes (main thread) ------------------------------------------------

    def play(self, kind: str) -> None:
        where = self._where()
        if where is None:
            return
        root, (x, y), side, up = where
        motion, names = KINDS.get(kind, ("twinkle", ["sparkle"]))
        colors = _colors()
        random.shuffle(colors)
        top = y + 26 * up
        if motion == "float":
            for i, name in enumerate(names):
                float_up(root, x + random.uniform(-14, 14), top, name, colors[i % len(colors)],
                         size=11, rise=36 * up, drift=side * random.uniform(4, 14), delay=i * 0.16)
        elif motion == "fan":
            for i, name in enumerate(names + names[:1]):
                angle = math.radians(90 * up + (i - 1.5) * 32)
                self._fling(root, x, y + 10 * up, name, colors[i % len(colors)],
                            x + math.cos(angle) * 46, y + math.sin(angle) * 46, delay=i * 0.05)
        elif motion == "fly":
            self._fly(root, x, y, names[0], colors[0], side, up)
        elif motion == "orbit":
            self._orbit(root, x, y, names[0], colors[0])
        elif motion == "letters":
            for i in range(3):
                self._letter(root, x + side * (18 + i * 9), top, random.choice("abcdefghkmnprstwxyz"),
                             colors[i % len(colors)], up, delay=i * 0.12)
        elif motion == "twinkle":
            for i in range(4):
                angle = random.uniform(0, 2 * math.pi)
                r = random.uniform(26, 40)
                self._twinkle(root, x + math.cos(angle) * r, y + math.sin(angle) * r,
                              names[i % len(names)], colors[i % len(colors)], delay=i * 0.11)
        elif motion == "bounce":
            for i in range(2):
                self._bounce(root, x + side * 30, y + 8 - i * 12, names[0], colors[i], delay=i * 0.12)
        elif motion == "flutter":
            for i, name in enumerate(names):
                self._flutter(root, x + side * (14 + i * 12), top, name, colors[i % len(colors)], up, delay=i * 0.18)
        elif motion == "flash":
            self._flash(root, x, y)

    def finish(self, ok: bool) -> None:
        where = self._where()
        if where is None:
            return
        root, (x, y), side, up = where
        if ok:
            for i in range(2):
                float_up(root, x + side * (10 + i * 10), y + 22 * up, "heart.fill",
                         random.choice([PINK, (1.0, 0.45, 0.55)]), size=10 + i * 2, rise=30 * up,
                         drift=side * 6, delay=i * 0.14)
        else:
            self._rain(root, x, y + 34 * up)

    def celebrate(self) -> None:
        where = self._where()
        if where is None:
            return
        root, (x, y), _, up = where
        confetti(root, x, y + 10 * up, _colors(), amount=1.4, up=up > 0)

    def paw(self, x: float, y: float) -> None:
        """A paw print stamped where a click lands (Quartz global point)."""
        from .effects import fx
        found = fx._locate(x, y)
        if found is None:
            return
        root, (lx, ly), _, _ = found
        stamp = _symbol_layer("pawprint.fill", 14, gfx.mix(PINK, gfx.light(gfx.accent()), 0.3), outline=True)
        stamp.setPosition_(Quartz.CGPointMake(lx + 9, ly - 11))
        stamp.setAffineTransform_(Quartz.CGAffineTransformMakeRotation(random.uniform(-0.4, 0.4)))
        stamp.setOpacity_(0.0)
        root.addSublayer_(stamp)
        _keys(stamp, "transform.scale", [1.8, 0.85, 1.0, 1.0], 0.9, [0, 0.18, 0.3, 1], name="stamp")
        _keys(stamp, "opacity", [0.0, 0.95, 0.95, 0.0], 0.9, [0, 0.12, 0.6, 1], name="fade")
        AppHelper.callLater(0.95, stamp.removeFromSuperlayer)

    # --- motion pieces -------------------------------------------------------------------

    def _add(self, root, name, rgb, size, x, y):
        layer = _symbol_layer(name, size + 2, rgb, outline=True)
        layer.setPosition_(Quartz.CGPointMake(x, y))
        layer.setOpacity_(0.0)
        root.addSublayer_(layer)
        return layer

    def _fling(self, root, x0, y0, name, rgb, x1, y1, delay=0.0):
        layer = self._add(root, name, rgb, 10, x0, y0)
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(path, None, x0, y0)
        Quartz.CGPathAddQuadCurveToPoint(path, None, (x0 + x1) / 2, max(y0, y1) + 14, x1, y1)
        fly = Quartz.CAKeyframeAnimation.animationWithKeyPath_("position")
        fly.setPath_(path)
        fly.setDuration_(0.8)
        fly.setBeginTime_(Quartz.CACurrentMediaTime() + delay)
        fly.setFillMode_(Quartz.kCAFillModeBackwards)
        fly.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseOut))
        layer.addAnimation_forKey_(fly, "fly")
        _keys(layer, "opacity", [0, 1, 1, 0], 0.8, [0, 0.1, 0.6, 1], delay=delay, name="fade")
        _keys(layer, "transform.rotation.z", [0, random.choice([-1, 1]) * 2.5], 0.8, delay=delay, name="spin")
        _keys(layer, "transform.scale", [0.3, 1.2, 0.8], 0.8, delay=delay, name="pop")
        AppHelper.callLater(delay + 0.85, layer.removeFromSuperlayer)

    def _fly(self, root, x, y, name, rgb, side, up):
        """Take off from the orb in a swoop toward the middle of the screen, with a sparkle trail."""
        layer = self._add(root, name, rgb, 13, x, y)
        x1, y1 = x + side * 150, y + up * 70
        path = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(path, None, x + side * 12, y)
        Quartz.CGPathAddCurveToPoint(path, None, x + side * 50, y - up * 22, x + side * 90, y + up * 90, x1, y1)
        fly = Quartz.CAKeyframeAnimation.animationWithKeyPath_("position")
        fly.setPath_(path)
        fly.setDuration_(1.1)
        fly.setRotationMode_(Quartz.kCAAnimationRotateAuto)
        fly.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseIn))
        layer.addAnimation_forKey_(fly, "fly")
        if side < 0:
            layer.setTransform_(Quartz.CATransform3DMakeScale(1, -1, 1))      # nose first, not upside down
        _keys(layer, "opacity", [0, 1, 1, 0], 1.1, [0, 0.1, 0.75, 1], name="fade")
        for i in range(4):
            t = 0.2 + i * 0.18
            px = x + side * (20 + i * 32)
            py = y + up * (-8 + i * 18)
            self._twinkle(root, px, py, "sparkle", PINK if i % 2 else YELLOW, delay=t, size=7)
        AppHelper.callLater(1.15, layer.removeFromSuperlayer)
        if name == "envelope.fill":
            AppHelper.callLater(0.9, lambda: float_up(root, x1, y1, "heart.fill", PINK, size=10, rise=20))

    def _orbit(self, root, x, y, name, rgb):
        layer = self._add(root, name, rgb, 11, x + 34, y)
        orbit = Quartz.CAKeyframeAnimation.animationWithKeyPath_("position")
        orbit.setPath_(Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(x - 34, y - 34, 68, 68), None))
        orbit.setDuration_(1.2)
        orbit.setCalculationMode_(Quartz.kCAAnimationPaced)
        layer.addAnimation_forKey_(orbit, "orbit")
        _keys(layer, "opacity", [0, 1, 1, 0], 1.2, [0, 0.1, 0.8, 1], name="fade")
        _keys(layer, "transform.scale", [0.4, 1.1, 1.0, 1.4], 1.2, [0, 0.2, 0.8, 1], name="pop")
        AppHelper.callLater(1.25, layer.removeFromSuperlayer)

    def _letter(self, root, x, y, char, rgb, up, delay=0.0):
        layer, _ = _text_layer(char, 14, rgb)
        layer.setShadowColor_(gfx.cg((0.13, 0.10, 0.16)))
        layer.setShadowOpacity_(0.55)
        layer.setShadowRadius_(1.2)
        layer.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        layer.setPosition_(Quartz.CGPointMake(x, y))
        layer.setOpacity_(0.0)
        root.addSublayer_(layer)
        _keys(layer, "position.y", [y, y + up * 30], 0.9, delay=delay, name="rise")
        _keys(layer, "opacity", [0, 1, 1, 0], 0.9, [0, 0.15, 0.6, 1], delay=delay, name="fade")
        _keys(layer, "transform.rotation.z", [0, random.uniform(-0.5, 0.5)], 0.9, delay=delay, name="tilt")
        _keys(layer, "transform.scale", [0.4, 1.2, 1.0], 0.9, [0, 0.25, 1], delay=delay, name="pop")
        AppHelper.callLater(delay + 0.95, layer.removeFromSuperlayer)

    def _twinkle(self, root, x, y, name, rgb, delay=0.0, size=10):
        layer = self._add(root, name, rgb, size, x, y)
        _keys(layer, "transform.scale", [0.1, 1.2, 0.1], 0.6, delay=delay, name="twinkle")
        _keys(layer, "opacity", [0, 1, 0], 0.6, delay=delay, name="fade")
        _keys(layer, "transform.rotation.z", [0, 1.2], 0.6, delay=delay, name="spin")
        AppHelper.callLater(delay + 0.65, layer.removeFromSuperlayer)

    def _bounce(self, root, x, y, name, rgb, delay=0.0):
        layer = self._add(root, name, rgb, 11, x, y)
        _keys(layer, "position.y", [y + 10, y, y + 5, y, y + 2, y], 0.8, [0, 0.3, 0.5, 0.7, 0.85, 1],
              delay=delay, name="bounce")
        _keys(layer, "opacity", [0, 1, 1, 0], 0.9, [0, 0.1, 0.7, 1], delay=delay, name="fade")
        AppHelper.callLater(delay + 0.95, layer.removeFromSuperlayer)

    def _flutter(self, root, x, y, name, rgb, up, delay=0.0):
        layer = self._add(root, name, rgb, 11, x, y)
        _keys(layer, "position.y", [y, y + up * 34], 1.2, delay=delay, name="rise")
        _keys(layer, "position.x", [x, x + 7, x - 5, x + 4], 1.2, delay=delay, name="sway")
        _keys(layer, "transform.rotation.z", [0, 0.35, -0.3, 0.2], 1.2, delay=delay, name="rock")
        _keys(layer, "opacity", [0, 1, 1, 0], 1.2, [0, 0.12, 0.7, 1], delay=delay, name="fade")
        AppHelper.callLater(delay + 1.25, layer.removeFromSuperlayer)

    def _flash(self, root, x, y):
        ring = Quartz.CAShapeLayer.layer()
        ring.setBounds_(Quartz.CGRectMake(0, 0, 60, 60))
        ring.setPosition_(Quartz.CGPointMake(x, y))
        ring.setPath_(Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(0, 0, 60, 60), None))
        ring.setFillColor_(gfx.cg((1, 1, 1), 0.35))
        ring.setStrokeColor_(gfx.cg((1, 1, 1)))
        ring.setLineWidth_(2)
        ring.setOpacity_(0.0)
        root.addSublayer_(ring)
        _keys(ring, "transform.scale", [0.4, 1.6], 0.45, name="grow")
        _keys(ring, "opacity", [0.9, 0.0], 0.45, name="fade")
        AppHelper.callLater(0.5, ring.removeFromSuperlayer)
        self._twinkle(root, x + 20, y + 20, "sparkle", YELLOW, delay=0.1)

    def _rain(self, root, x, y):
        """A tiny rain cloud over the orb for a moment: that did not work."""
        cloud = self._add(root, "cloud.rain.fill", SKY, 16, x, y)
        _keys(cloud, "opacity", [0, 1, 1, 0], 1.6, [0, 0.12, 0.8, 1], name="fade")
        _keys(cloud, "position.x", [x - 3, x + 3, x - 3], 1.6, cubic=True, name="drift")
        _keys(cloud, "transform.scale", [0.5, 1.1, 1.0], 0.4, name="pop")
        AppHelper.callLater(1.65, cloud.removeFromSuperlayer)


cute = CuteFX()


def attach(hud) -> None:
    """Wrap the HUD's action hooks and the click ripple (once)."""
    if getattr(hud, "_cute_fx", False):
        return
    hud._cute_fx = True
    cute.hud = hud
    start, end, celebrate = hud.activity_start, hud.activity_end, hud.celebrate

    def activity_start(name, args):
        start(name, args)
        cute.started(name)

    def activity_end(name, ok):
        end(name, ok)
        cute.ended(ok)

    def celebrated():
        celebrate()
        AppHelper.callAfter(cute._safe, cute.celebrate)
    hud.activity_start, hud.activity_end, hud.celebrate = activity_start, activity_end, celebrated

    from .effects import fx
    if not getattr(fx, "_cute_paws", False):
        fx._cute_paws = True
        click = fx._click

        def paw_click(x, y, label):
            click(x, y, label)
            if enabled():
                try:
                    cute.paw(x, y)
                except Exception:
                    log.debug("paw stamp failed", exc_info=True)
        fx._click = paw_click
