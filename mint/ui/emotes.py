"""Expressions for the orb: faces, little hands, and reactions to being teased.

    smile  laugh  love  blush  cry  angry  surprised  sleepy  dizzy
    cool  thinking  wink  kiss  wave  clap  praise  dance  yes  no

Everything is drawn on top of the orb (orb.py) from outside it: an expression
hides the orb's own face for a moment, draws its own - heart eyes, tears,
sunglasses - adds props around the body (mitten hands, floating hearts,
music notes, Zzz), moves the body, and then gives the face back. The orb
itself is not modified, so its states, blinking and eye-tracking carry on.

Teasing is watched the same way. Rapid clicks on the orb are pokes (the first
click still opens the chat); a long hover makes it shy; darting the pointer
on and off, or circling it, gets a reaction too.

`play(name)` is safe from any thread. `ensure_attached()` finds the live HUD
and hooks it up; it is idempotent.
"""

from __future__ import annotations

import gc
import logging
import math
import random
import time

import AppKit
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx

log = logging.getLogger("mint.ui.emotes")

INK = (0.04, 0.06, 0.12)
HEART = (1.0, 0.30, 0.45)
TEAR = (0.55, 0.82, 1.0)
PINK = (1.0, 0.45, 0.6)

# name -> (seconds, description for the tool)
EMOTES = {
    "smile": (2.6, "a big happy smile with rosy cheeks"),
    "laugh": (2.8, "laughing: squeezed eyes, open mouth, bouncing"),
    "love": (3.0, "heart eyes and floating hearts"),
    "blush": (2.8, "shy: blushing cheeks, eyes down"),
    "cry": (4.0, "sad and crying: droopy brows, tears, frown"),
    "angry": (2.6, "annoyed: frowning brows, red flush, a huff"),
    "surprised": (2.2, "wow: big round eyes, O mouth, a jump"),
    "sleepy": (3.4, "sleepy: closed eyes and floating Zzz"),
    "dizzy": (3.0, "dizzy: X eyes, wobble, circling stars"),
    "cool": (3.2, "cool: sunglasses slide down, smirk"),
    "thinking": (2.8, "hmm: eyes up, a question mark"),
    "wink": (1.8, "a wink and a smile"),
    "kiss": (2.4, "blows a kiss: wink and a flying heart"),
    "wave": (2.4, "waves hello or goodbye with a little hand"),
    "clap": (2.6, "claps its little hands, with sparkles"),
    "praise": (2.8, "thumbs up and a cheer, for good work"),
    "dance": (4.8, "a little dance: sways, hops, hands up, music notes"),
    "yes": (1.4, "nods yes"),
    "no": (1.4, "shakes its head no"),
    "show": (20.0, "the whole critter family pops out of the toy box and puts on a show: a wave, a solo "
                   "each, a dance and a bow"),
}
ALIASES = {"happy": "smile", "heart": "love", "hearts": "love", "shy": "blush", "sad": "cry",
           "crying": "cry", "mad": "angry", "annoyed": "angry", "wow": "surprised", "shock": "surprised",
           "tired": "sleepy", "sleep": "sleepy", "confused": "thinking", "hmm": "thinking",
           "sunglasses": "cool", "hi": "wave", "hello": "wave", "bye": "wave", "applause": "clap",
           "cheer": "praise", "thumbs up": "praise", "good job": "praise", "celebrate": "praise",
           "nod": "yes", "shake head": "no", "giggle": "laugh", "haha": "laugh", "boogie": "dance",
           "party": "show", "perform": "show", "performance": "show", "concert": "show", "showtime": "show",
           "family": "show", "a show": "show", "put on a show": "show", "critter show": "show", "critters": "show", "troupe": "show", "parade": "show"}


def resolve(name: str) -> str | None:
    key = " ".join(name.lower().replace("_", " ").split())
    if key in EMOTES:
        return key
    # "show" only as its own word or phrase: "show me a heart" is love, not the critter show.
    return (ALIASES.get(key) or next((k for k in EMOTES if k in key and k != "show"), None)
            or next((v for a, v in ALIASES.items() if a in key), None))


def _ease(name=Quartz.kCAMediaTimingFunctionEaseInEaseOut):
    return Quartz.CAMediaTimingFunction.functionWithName_(name)


def _keys(layer, key, values, duration, times=None, repeat=1.0, additive=False, begin=0.0,
          name=None, ease=None):
    animation = Quartz.CAKeyframeAnimation.animationWithKeyPath_(key)
    animation.setValues_(values)
    if times:
        animation.setKeyTimes_(times)
    animation.setDuration_(duration)
    animation.setRepeatCount_(repeat)
    animation.setAdditive_(additive)
    if ease:
        animation.setTimingFunction_(_ease(ease))
    if begin:
        animation.setBeginTime_(Quartz.CACurrentMediaTime() + begin)
        animation.setFillMode_(Quartz.kCAFillModeBackwards)
    layer.addAnimation_forKey_(animation, name or key)


def _stroke(path, rgb=INK, width=2.2, alpha=0.9):
    shape = Quartz.CAShapeLayer.layer()
    shape.setPath_(path)
    shape.setFillColor_(None)
    shape.setStrokeColor_(gfx.cg(rgb, alpha))
    shape.setLineWidth_(width)
    shape.setLineCap_(Quartz.kCALineCapRound)
    shape.setLineJoin_(Quartz.kCALineJoinRound)
    return shape


def _fill(path, rgb=INK, alpha=0.9):
    shape = Quartz.CAShapeLayer.layer()
    shape.setPath_(path)
    shape.setFillColor_(gfx.cg(rgb, alpha))
    return shape


def _path(*segments):
    """Tiny path language: ("M", x, y), ("L", x, y), ("Q", cx, cy, x, y)."""
    path = Quartz.CGPathCreateMutable()
    for seg in segments:
        op, *v = seg
        if op == "M":
            Quartz.CGPathMoveToPoint(path, None, *v)
        elif op == "L":
            Quartz.CGPathAddLineToPoint(path, None, *v)
        elif op == "Q":
            Quartz.CGPathAddQuadCurveToPoint(path, None, *v)
        elif op == "C":
            Quartz.CGPathAddCurveToPoint(path, None, *v)
    return path


def _heart_path(cx, cy, w):
    """A heart centred on (cx, cy), w wide, point down (layer coordinates: y up)."""
    h = w * 0.9
    top = cy + h * 0.32
    return _path(("M", cx, cy - h / 2),
                 ("C", cx - w * 0.62, cy - h * 0.05, cx - w * 0.5, top + h * 0.45, cx, top),
                 ("C", cx + w * 0.5, top + h * 0.45, cx + w * 0.62, cy - h * 0.05, cx, cy - h / 2))


class Emotes:
    def __init__(self) -> None:
        self.hud = None
        self.orb = None
        self._layers: list = []          # everything the current expression added
        self._face_hidden = False
        self._until = 0.0
        self._token = 0
        self._attach_tries = 0
        # teasing
        self._clicks: list[float] = []
        self._pokes = 0
        self._hovering = False
        self._hover_start = 0.0
        self._shy_done = False
        self._enters: list[float] = []
        self._angle = None
        self._spin = 0.0
        self._spin_since = 0.0
        self._last_tease = 0.0

    # --- attaching to the live HUD -------------------------------------------------------

    def ensure_attached(self) -> None:
        if self.orb is None:
            AppHelper.callAfter(self._find)

    def _find(self) -> None:
        if self.orb is not None:
            return
        from mint.ui import hud as hud_module
        found = [o for o in gc.get_objects() if isinstance(o, hud_module.HUD) and getattr(o, "orb", None)]
        if found:
            self.attach(found[0])
            return
        self._attach_tries += 1
        if self._attach_tries < 60:
            AppHelper.callLater(1.0, self._find)

    def _wrap_tick(self, orb) -> None:
        """Watch hovers and follow motion's gaze on this orb (once per orb)."""
        if getattr(orb, "_emotes_wrapped", False):
            return
        orb._emotes_wrapped = True
        hud = self.hud
        original_tick = orb.tick

        def tick(now, level, look, hovering, visible_work, _orig=original_tick):
            # While the orb points at something it marked, its eyes stay on it.
            try:
                from mint.ui.motion import motion
                target = motion.look()
                if target is not None:
                    frame = hud._orb_window.frame()
                    look = (target[0] - (frame.origin.x + frame.size.width / 2),
                            target[1] - (frame.origin.y + frame.size.height / 2))
            except Exception:
                pass
            _orig(now, level, look, hovering, visible_work)
            try:
                self._watch_hover(now, hovering, visible_work)
            except Exception:
                log.debug("hover watch failed", exc_info=True)
        orb.tick = tick

    def rebind(self, orb) -> None:
        """Draw on another orb from now on (Mint moving between the floating orb and the notch)."""
        if orb is None or orb is self.orb:
            return
        try:
            self._clear()                         # whatever was playing stays with the old orb
        except Exception:
            pass
        self.orb = orb
        self._wrap_tick(orb)
        try:
            from mint.ui import sharing
            if sharing._dot is not None:
                sharing._dot.removeFromSuperlayer()
                sharing._dot = None
            sharing.apply()                       # the "on air" dot follows onto the new orb
        except Exception:
            log.debug("sharing dot did not move", exc_info=True)

    def attach(self, hud) -> None:
        """Hook the HUD's orb: expressions draw on it; clicks and hovers are watched."""
        if self.orb is not None:
            return
        self.hud, self.orb = hud, hud.orb
        self._wrap_tick(self.orb)

        if hasattr(hud, "on_poke"):
            # The HUD's own extension point: called on each click of the orb;
            # returning True means it was a poke and the chat is left alone.
            hud.on_poke = self._poked
            original_click = None
        else:
            original_click = getattr(hud, "_orb_clicked", None)
        if original_click is not None:
            def clicked(_orig=original_click):
                if self._poked():
                    return
                _orig()
            hud._orb_clicked = clicked
        print("  [expressions ready: say 'smile', 'dance', 'clap'… or poke the orb]", flush=True)
        from mint.ui.motion import motion
        motion.start()                   # rare idle wandering
        from mint.ui import sharing
        sharing.install()                # visible-in-screen-share switch
        from mint.ui import flourishes as cute_fx
        cute_fx.attach(hud)              # a little flourish per kind of action, paw prints on clicks
        from mint.ui import moods
        moods.attach(hud)                # feelings during the conversation; sunglasses when a task is done

    # --- public -------------------------------------------------------------------------

    def play(self, name: str) -> str:
        key = resolve(name)
        if key is None:
            return f"Unknown expression '{name}'. Known: {', '.join(EMOTES)}."
        if key == "show":
            # Not drawn on the orb: the critter family comes out of its toy box (critters.py).
            from mint.ui.critters import stage
            AppHelper.callAfter(lambda: self.orb is not None and self._play("dance"))
            return stage.perform()
        if self.orb is None:
            self.ensure_attached()
            if self.orb is None:
                AppHelper.callLater(1.2, lambda: self.orb and self._play(key))
                return f"Showing {key}."
        AppHelper.callAfter(self._play, key)
        return f"Showing {key}: {EMOTES[key][1]}."

    # --- drawing --------------------------------------------------------------------------

    def _clear(self) -> None:
        for layer in self._layers:
            layer.removeFromSuperlayer()
        self._layers = []
        for key in ("em-sway", "em-hop", "em-shift", "em-tilt", "em-scale"):
            self.orb.body.removeAnimationForKey_(key)
        self._show_face(True)

    def _show_face(self, on: bool) -> None:
        orb = self.orb
        for eye, _ in orb.eyes:
            eye.setHidden_(not on)
        for layer in list(orb.smiles) + list(orb.cheeks) + [orb.mouth] + (
                [orb.rest_mouth] if getattr(orb, "rest_mouth", None) is not None else []):
            layer.setHidden_(not on)
        self._face_hidden = not on

    def _add(self, layer, parent=None):
        (parent or self.orb.core).addSublayer_(layer)
        self._layers.append(layer)
        return layer

    def _play(self, key: str) -> None:
        if self.orb is None:
            return
        self._clear()
        self._token += 1
        token = self._token
        seconds = EMOTES[key][0]
        self.last_played = time.monotonic()
        orb = self.orb
        d = orb.d
        s = d / 44.0
        c = d / 2                                     # core centre
        ex, ey = 6.5 * s, 2 * s                       # eye offsets from centre
        try:
            getattr(self, f"_e_{key}")(d, s, c, ex, ey)
        except Exception:
            log.exception("expression %s failed", key)
            self._clear()
            return
        AppHelper.callLater(seconds, lambda: self._finish(token))

    def _finish(self, token: int) -> None:
        if token != self._token or self.orb is None:
            return
        fade = list(self._layers)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.25)
        for layer in fade:
            layer.setOpacity_(0.0)
        Quartz.CATransaction.commit()

        def done():
            if token == self._token:
                self._clear()
        AppHelper.callLater(0.28, done)

    # face pieces (core coordinates, y up) ---------------------------------------------

    def _face_layer(self):
        """A layer the size of the core that holds this expression's face."""
        d = self.orb.d
        face = Quartz.CALayer.layer()
        face.setFrame_(Quartz.CGRectMake(0, 0, d, d))
        self._show_face(False)
        return self._add(face)

    def _happy_eyes(self, face, s, c, ex, ey, only=(-1, 1)):
        for side in only:
            x = c + side * ex
            face.addSublayer_(_stroke(_path(("M", x - 3.5 * s, c + ey - 1 * s),
                                            ("Q", x, c + ey + 4 * s, x + 3.5 * s, c + ey - 1 * s)),
                                      width=2.2 * s))

    def _open_eyes(self, face, s, c, ex, ey, w=5, h=9, only=(-1, 1)):
        for side in only:
            eye = Quartz.CALayer.layer()
            eye.setBounds_(Quartz.CGRectMake(0, 0, w * s, h * s))
            eye.setCornerRadius_(min(w, h) * s / 2)
            eye.setBackgroundColor_(gfx.cg(INK, 0.9))
            eye.setPosition_(Quartz.CGPointMake(c + side * ex, c + ey))
            face.addSublayer_(eye)

    def _cheeks(self, face, s, c, alpha=0.8, size=1.0):
        for side in (-1, 1):
            cheek = Quartz.CALayer.layer()
            cheek.setBounds_(Quartz.CGRectMake(0, 0, 7 * s * size, 4 * s * size))
            cheek.setCornerRadius_(2 * s * size)
            cheek.setBackgroundColor_(gfx.cg(PINK, alpha))
            cheek.setPosition_(Quartz.CGPointMake(c + side * 10 * s, c - 3.5 * s))
            face.addSublayer_(cheek)

    def _smile(self, face, s, c, wide=4.5, y=-6.5, depth=4.5):
        face.addSublayer_(_stroke(_path(("M", c - wide * s, c + y * s),
                                        ("Q", c, c + (y - depth) * s, c + wide * s, c + y * s)),
                                  width=2.0 * s))

    def _brows(self, face, s, c, ex, inner_up: bool):
        for side in (-1, 1):
            outer = (c + side * (ex + 3.5 * s), c + (8.5 if inner_up else 10.5) * s)
            inner = (c + side * (ex - 3.0 * s), c + (10.5 if inner_up else 7.5) * s)
            face.addSublayer_(_stroke(_path(("M", *outer), ("L", *inner)), width=1.8 * s))

    # props (body coordinates) ------------------------------------------------------------

    def _float(self, symbol: str, rgb, count=4, rise=34, size=10, spread=18, duration=1.6, stagger=0.35,
               start=None):
        orb = self.orb
        bx, by = start or orb._bc
        for i in range(count):
            layer = Quartz.CALayer.layer()
            layer.setBounds_(Quartz.CGRectMake(0, 0, size, size))
            mask = Quartz.CALayer.layer()
            mask.setFrame_(Quartz.CGRectMake(0, 0, size, size))
            mask.setContents_(gfx.symbol(symbol, size))
            mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
            layer.setMask_(mask)
            layer.setBackgroundColor_(gfx.cg(rgb))
            x = bx + random.uniform(-spread, spread)
            y = by + orb.d * 0.35
            layer.setPosition_(Quartz.CGPointMake(x, y))
            layer.setOpacity_(0.0)
            self._add(layer, orb.body)
            begin = i * stagger
            _keys(layer, "position.y", [y, y + rise], duration, begin=begin, ease=Quartz.kCAMediaTimingFunctionEaseOut)
            _keys(layer, "position.x", [x, x + random.uniform(-8, 8), x], duration, begin=begin)
            _keys(layer, "opacity", [0.0, 1.0, 1.0, 0.0], duration, [0, 0.15, 0.7, 1], begin=begin)
            _keys(layer, "transform.scale", [0.4, 1.1, 0.9], duration, begin=begin)

    def _hand(self, side: int, symbol: str | None = None, size: float = 0.3):
        """A little mitten beside the body; `symbol` draws a hand shape instead."""
        orb = self.orb
        d = orb.d
        bx, by = orb._bc
        rgb = gfx.light(gfx.state_rgb(orb.state if orb.state not in ("sleeping", "paused", "offline", "starting")
                                      else "awake"))
        hand = Quartz.CALayer.layer()
        w = d * size
        hand.setBounds_(Quartz.CGRectMake(0, 0, w, w))
        hand.setPosition_(Quartz.CGPointMake(bx + side * d * 0.66, by - d * 0.12))
        if symbol:
            mask = Quartz.CALayer.layer()
            mask.setFrame_(Quartz.CGRectMake(0, 0, w, w))
            mask.setContents_(gfx.symbol(symbol, w))
            mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
            hand.setMask_(mask)
            if side < 0:
                hand.setAffineTransform_(Quartz.CGAffineTransformMakeScale(-1, 1))
        else:
            hand.setCornerRadius_(w / 2)
            hand.setBorderWidth_(1.0)
            hand.setBorderColor_(AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.6).CGColor())
        hand.setBackgroundColor_(gfx.cg(rgb))
        hand.setShadowOpacity_(0.35)
        hand.setShadowRadius_(2)
        hand.setShadowOffset_(Quartz.CGSizeMake(0, -1))
        self._add(hand, orb.body)
        _keys(hand, "transform.scale", [0.1, 1.15, 1.0], 0.3, name="pop")
        return hand

    def _body(self, key, values, duration, times=None, repeat=1.0, name=None):
        _keys(self.orb.body, key, values, duration, times, repeat=repeat, additive=True,
              name=name or "em-" + key.split(".")[-1])

    # --- the expressions ------------------------------------------------------------------

    def _e_smile(self, d, s, c, ex, ey):
        face = self._face_layer()
        self._happy_eyes(face, s, c, ex, ey)
        self._cheeks(face, s, c)
        self._smile(face, s, c)
        self._body("transform.translation.y", [0, 4, 0], 0.5, name="em-hop")
        self.orb.burst(gfx.accent(), stars=True, amount=0.3)

    def _e_laugh(self, d, s, c, ex, ey):
        face = self._face_layer()
        for side in (-1, 1):                      # squeezed >< eyes
            x = c + side * ex
            face.addSublayer_(_stroke(_path(("M", x - side * 3 * s, c + ey + 3 * s), ("L", x + side * 2 * s, c + ey),
                                            ("L", x - side * 3 * s, c + ey - 3 * s)), width=2 * s))
        mouth = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(mouth, None, c - 5 * s, c - 5 * s)
        Quartz.CGPathAddLineToPoint(mouth, None, c + 5 * s, c - 5 * s)
        Quartz.CGPathAddArc(mouth, None, c, c - 5 * s, 5 * s, 0, math.pi, True)
        face.addSublayer_(_fill(mouth))
        self._cheeks(face, s, c, 0.7)
        self._body("transform.translation.y", [0, 3, 0, 3, 0, 3, 0], 0.9, repeat=3, name="em-hop")
        self._body("transform.rotation.z", [0, 0.06, -0.06, 0], 0.3, repeat=6, name="em-tilt")

    def _e_love(self, d, s, c, ex, ey):
        face = self._face_layer()
        for side in (-1, 1):
            heart = _fill(_heart_path(c + side * ex, c + ey, 8 * s), HEART, 1.0)
            face.addSublayer_(heart)
        self._smile(face, s, c, wide=3.5, depth=3)
        for layer in face.sublayers()[:2]:
            _keys(layer, "transform.scale", [1.0, 1.25, 1.0], 0.5, repeat=6)
        self._float("heart.fill", HEART, count=5, rise=40, size=11)

    def _e_blush(self, d, s, c, ex, ey):
        face = self._face_layer()
        self._happy_eyes(face, s, c, ex, ey)
        self._cheeks(face, s, c, alpha=0.95, size=1.25)
        face.addSublayer_(_stroke(_path(("M", c - 2.5 * s, c - 7 * s), ("Q", c, c - 9 * s, c + 2.5 * s, c - 7 * s)),
                                  width=1.8 * s))
        self._body("transform.scale", [0, -0.08, -0.08, 0], 2.6, [0, 0.15, 0.85, 1], name="em-scale")
        self._body("transform.rotation.z", [0, 0.1, 0.1, 0], 2.6, [0, 0.2, 0.8, 1], name="em-tilt")
        self._float("heart.fill", PINK, count=2, rise=26, size=8, stagger=0.8)

    def _e_cry(self, d, s, c, ex, ey):
        face = self._face_layer()
        for side in (-1, 1):                      # closed, drooping eyes
            x = c + side * ex
            face.addSublayer_(_stroke(_path(("M", x - 3.5 * s, c + ey + 1 * s),
                                            ("Q", x, c + ey - 2.5 * s, x + 3.5 * s, c + ey + 1 * s)), width=2 * s))
        self._brows(face, s, c, ex, inner_up=True)
        face.addSublayer_(_stroke(_path(("M", c - 4 * s, c - 9 * s), ("Q", c, c - 5 * s, c + 4 * s, c - 9 * s)),
                                  width=2 * s))
        for i in range(8):                        # tears
            side = -1 if i % 2 == 0 else 1
            # White with a blue edge: plain blue tears vanished against the orb.
            tear = _fill(_path(("M", 0, 3.4 * s), ("Q", 2.6 * s, -0.5 * s, 0, -2.4 * s),
                               ("Q", -2.6 * s, -0.5 * s, 0, 3.4 * s)), (0.94, 0.98, 1.0), 1.0)
            tear.setStrokeColor_(gfx.cg((0.2, 0.5, 0.95), 0.95))
            tear.setLineWidth_(0.9 * s)
            x, y = c + side * (ex + 1 * s), c + ey - 3 * s
            tear.setPosition_(Quartz.CGPointMake(x, y))
            tear.setOpacity_(0.0)
            face.addSublayer_(tear)
            begin = (i // 2) * 0.45 + (0.2 if side > 0 else 0)
            _keys(tear, "position.y", [y, y - 16 * s], 0.8, begin=begin, ease=Quartz.kCAMediaTimingFunctionEaseIn)
            _keys(tear, "opacity", [0.0, 1.0, 1.0, 0.0], 0.8, [0, 0.1, 0.7, 1], begin=begin)
        self._body("transform.translation.y", [0, -3, -3, 0], 4.0, [0, 0.1, 0.9, 1], name="em-shift")
        self._body("transform.translation.x", [0, 1, -1, 0], 0.25, repeat=12, name="em-sway")

    def _e_angry(self, d, s, c, ex, ey):
        face = self._face_layer()
        self._open_eyes(face, s, c, ex, ey - 1 * s, h=6)
        self._brows(face, s, c, ex, inner_up=False)
        face.addSublayer_(_stroke(_path(("M", c - 3.5 * s, c - 8.5 * s), ("Q", c, c - 6 * s, c + 3.5 * s, c - 8.5 * s)),
                                  width=2 * s))
        flush = Quartz.CALayer.layer()
        flush.setFrame_(Quartz.CGRectMake(0, 0, d, d))
        flush.setBackgroundColor_(gfx.cg((1.0, 0.25, 0.2), 0.0))
        face.insertSublayer_atIndex_(flush, 0)
        _keys(flush, "backgroundColor", [gfx.cg((1.0, 0.25, 0.2), 0.0), gfx.cg((1.0, 0.25, 0.2), 0.35),
                                         gfx.cg((1.0, 0.25, 0.2), 0.35), gfx.cg((1.0, 0.25, 0.2), 0.0)],
              2.6, [0, 0.2, 0.8, 1])
        self._body("transform.translation.x", [0, -3, 3, -2, 2, 0], 0.4, repeat=2, name="em-sway")
        self._float("cloud.fill", (0.85, 0.85, 0.9), count=2, rise=18, size=9, spread=14, stagger=0.4,
                    start=(self.orb._bc[0], self.orb._bc[1] + d * 0.15))

    def _e_surprised(self, d, s, c, ex, ey):
        face = self._face_layer()
        for side in (-1, 1):
            ring = Quartz.CALayer.layer()
            ring.setBounds_(Quartz.CGRectMake(0, 0, 8 * s, 8 * s))
            ring.setCornerRadius_(4 * s)
            ring.setBackgroundColor_(gfx.cg((1, 1, 1), 0.95))
            ring.setBorderWidth_(1.6 * s)
            ring.setBorderColor_(gfx.cg(INK, 0.9))
            ring.setPosition_(Quartz.CGPointMake(c + side * ex, c + ey + 1 * s))
            pupil = Quartz.CALayer.layer()
            pupil.setBounds_(Quartz.CGRectMake(0, 0, 3.5 * s, 3.5 * s))
            pupil.setCornerRadius_(1.75 * s)
            pupil.setBackgroundColor_(gfx.cg(INK))
            pupil.setPosition_(Quartz.CGPointMake(4 * s, 4 * s))
            ring.addSublayer_(pupil)
            face.addSublayer_(ring)
        mouth = Quartz.CALayer.layer()
        mouth.setBounds_(Quartz.CGRectMake(0, 0, 5 * s, 6 * s))
        mouth.setCornerRadius_(2.5 * s)
        mouth.setBackgroundColor_(gfx.cg(INK, 0.85))
        mouth.setPosition_(Quartz.CGPointMake(c, c - 8 * s))
        face.addSublayer_(mouth)
        self._body("transform.translation.y", [0, 9, -2, 0], 0.45, [0, 0.4, 0.75, 1], name="em-hop")
        self._float("exclamationmark", (1.0, 0.85, 0.3), count=1, rise=20, size=12, spread=0)

    def _e_sleepy(self, d, s, c, ex, ey):
        face = self._face_layer()
        for side in (-1, 1):
            x = c + side * ex
            face.addSublayer_(_stroke(_path(("M", x - 3 * s, c + ey), ("L", x + 3 * s, c + ey)), width=2 * s))
        mouth = Quartz.CALayer.layer()
        mouth.setBounds_(Quartz.CGRectMake(0, 0, 3.5 * s, 4 * s))
        mouth.setCornerRadius_(1.75 * s)
        mouth.setBackgroundColor_(gfx.cg(INK, 0.7))
        mouth.setPosition_(Quartz.CGPointMake(c + 2 * s, c - 8 * s))
        face.addSublayer_(mouth)
        _keys(mouth, "transform.scale", [0.6, 1.2, 0.6], 1.2, repeat=3)
        bx, by = self.orb._bc
        self._float("zzz", (0.85, 0.9, 1.0), count=3, rise=30, size=11, spread=6, stagger=0.8, duration=1.8,
                    start=(bx + d * 0.35, by))
        self._body("transform.rotation.z", [0, -0.12, -0.12, 0], 3.4, [0, 0.2, 0.85, 1], name="em-tilt")

    def _e_dizzy(self, d, s, c, ex, ey):
        face = self._face_layer()
        for side in (-1, 1):
            x, y = c + side * ex, c + ey
            k = 3 * s
            face.addSublayer_(_stroke(_path(("M", x - k, y - k), ("L", x + k, y + k)), width=1.9 * s))
            face.addSublayer_(_stroke(_path(("M", x - k, y + k), ("L", x + k, y - k)), width=1.9 * s))
        face.addSublayer_(_stroke(_path(("M", c - 4 * s, c - 7 * s), ("Q", c - 2 * s, c - 9 * s, c, c - 7 * s),
                                        ("Q", c + 2 * s, c - 5 * s, c + 4 * s, c - 7 * s)), width=1.8 * s))
        self._body("transform.rotation.z", [0, 0.18, -0.18, 0], 0.7, repeat=4, name="em-tilt")
        orb = self.orb
        bx, by = orb._bc
        ring = Quartz.CALayer.layer()
        ring.setBounds_(Quartz.CGRectMake(0, 0, d * 1.3, d * 0.5))
        ring.setPosition_(Quartz.CGPointMake(bx, by + d * 0.55))
        self._add(ring, orb.body)
        for i in range(3):
            star = Quartz.CALayer.layer()
            star.setBounds_(Quartz.CGRectMake(0, 0, 9, 9))
            mask = Quartz.CALayer.layer()
            mask.setFrame_(Quartz.CGRectMake(0, 0, 9, 9))
            mask.setContents_(gfx.symbol("star.fill", 9))
            star.setMask_(mask)
            star.setBackgroundColor_(gfx.cg((1.0, 0.85, 0.3)))
            ring.addSublayer_(star)
            path = Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(0, 0, d * 1.3, d * 0.5), None)
            orbit = Quartz.CAKeyframeAnimation.animationWithKeyPath_("position")
            orbit.setPath_(path)
            orbit.setDuration_(1.1)
            orbit.setRepeatCount_(3)
            orbit.setTimeOffset_(i * 1.1 / 3)
            orbit.setCalculationMode_(Quartz.kCAAnimationPaced)
            star.addAnimation_forKey_(orbit, "orbit")

    def _e_cool(self, d, s, c, ex, ey):
        face = self._face_layer()
        glasses = Quartz.CAShapeLayer.layer()
        path = Quartz.CGPathCreateMutable()
        for side in (-1, 1):
            Quartz.CGPathAddRoundedRect(path, None, Quartz.CGRectMake(c + side * ex - 5 * s, c + ey - 3.5 * s,
                                                                      10 * s, 7 * s), 2.5 * s, 2.5 * s)
        Quartz.CGPathAddRect(path, None, Quartz.CGRectMake(c - ex + 4 * s, c + ey + 1.5 * s, 2 * ex - 8 * s, 1.5 * s))
        glasses.setPath_(path)
        glasses.setFillColor_(gfx.cg((0.02, 0.02, 0.04), 0.96))
        shine = _stroke(_path(("M", c - ex - 3 * s, c + ey + 1.5 * s), ("L", c - ex - 1 * s, c + ey + 2.5 * s)),
                        (1, 1, 1), 1.0 * s, 0.8)
        glasses.addSublayer_(shine)
        face.addSublayer_(glasses)
        _keys(glasses, "position.y", [14 * s, 0.0], 0.45, ease=Quartz.kCAMediaTimingFunctionEaseOut)
        face.addSublayer_(_stroke(_path(("M", c - 3 * s, c - 7.5 * s), ("Q", c + 1 * s, c - 9.5 * s, c + 4.5 * s, c - 6 * s)),
                                  width=1.9 * s))
        self._body("transform.rotation.z", [0, -0.08, -0.08, 0], 3.2, [0, 0.15, 0.85, 1], name="em-tilt")
        AppHelper.callLater(0.5, lambda: self.orb and self.orb.burst((1, 1, 1), stars=True, amount=0.25))

    def _e_thinking(self, d, s, c, ex, ey):
        face = self._face_layer()
        self._open_eyes(face, s, c + 0, ex, ey + 2 * s, h=7)
        for layer in face.sublayers():
            layer.setPosition_(Quartz.CGPointMake(layer.position().x + 2 * s, layer.position().y))
        face.addSublayer_(_stroke(_path(("M", c - 2 * s, c - 7.5 * s), ("L", c + 3.5 * s, c - 6.5 * s)), width=1.8 * s))
        bx, by = self.orb._bc
        self._float("questionmark", (1, 1, 1), count=2, rise=22, size=12, spread=4, stagger=1.0, duration=1.4,
                    start=(bx + d * 0.4, by))
        self._body("transform.rotation.z", [0, 0.1, 0.1, 0], 2.8, [0, 0.2, 0.85, 1], name="em-tilt")

    def _e_wink(self, d, s, c, ex, ey):
        face = self._face_layer()
        self._open_eyes(face, s, c, ex, ey, only=(-1,))
        self._happy_eyes(face, s, c, ex, ey, only=(1,))
        self._smile(face, s, c, wide=4, depth=3.5)
        self._cheeks(face, s, c, 0.5)
        self._body("transform.rotation.z", [0, -0.1, 0], 0.5, name="em-tilt")

    def _e_kiss(self, d, s, c, ex, ey):
        face = self._face_layer()
        self._open_eyes(face, s, c, ex, ey, only=(-1,))
        self._happy_eyes(face, s, c, ex, ey, only=(1,))
        face.addSublayer_(_stroke(_path(("M", c - 1 * s, c - 5.5 * s), ("Q", c + 3 * s, c - 7 * s, c, c - 8 * s),
                                        ("Q", c + 3 * s, c - 9 * s, c - 1 * s, c - 10.5 * s)), width=1.7 * s))
        self._cheeks(face, s, c, 0.8)
        bx, by = self.orb._bc
        heart = Quartz.CALayer.layer()
        heart.setBounds_(Quartz.CGRectMake(0, 0, 12, 12))
        mask = Quartz.CALayer.layer()
        mask.setFrame_(Quartz.CGRectMake(0, 0, 12, 12))
        mask.setContents_(gfx.symbol("heart.fill", 12))
        heart.setMask_(mask)
        heart.setBackgroundColor_(gfx.cg(HEART))
        heart.setPosition_(Quartz.CGPointMake(bx, by - d * 0.1))
        heart.setOpacity_(0.0)
        self._add(heart, self.orb.body)
        _keys(heart, "position", [AppKit.NSValue.valueWithPoint_((bx, by - d * 0.1)),
                                  AppKit.NSValue.valueWithPoint_((bx + d * 0.55, by + d * 0.35)),
                                  AppKit.NSValue.valueWithPoint_((bx + d * 0.9, by + d * 0.8))], 1.4, begin=0.3)
        _keys(heart, "opacity", [0.0, 1.0, 1.0, 0.0], 1.4, [0, 0.1, 0.7, 1], begin=0.3)
        _keys(heart, "transform.scale", [0.3, 1.2, 1.4], 1.4, begin=0.3)

    def _e_wave(self, d, s, c, ex, ey):
        face = self._face_layer()
        self._happy_eyes(face, s, c, ex, ey)
        self._smile(face, s, c, wide=4, depth=3.5)
        hand = self._hand(1, "hand.raised.fill", 0.36)
        bx, by = self.orb._bc
        up = AppKit.NSValue.valueWithPoint_((bx + d * 0.66, by + d * 0.22))
        _keys(hand, "position", [AppKit.NSValue.valueWithPoint_((bx + d * 0.66, by - d * 0.12)), up], 0.25,
              name="raise")
        hand.setPosition_(Quartz.CGPointMake(bx + d * 0.66, by + d * 0.22))
        _keys(hand, "transform.rotation.z", [0.0, 0.45, -0.25, 0.45, -0.25, 0.45, 0.0], 1.8, begin=0.25,
              name="wave")
        self._body("transform.rotation.z", [0, 0.06, -0.06, 0.06, 0], 1.8, name="em-tilt")

    def _e_clap(self, d, s, c, ex, ey):
        face = self._face_layer()
        self._happy_eyes(face, s, c, ex, ey)
        mouth = Quartz.CGPathCreateMutable()
        Quartz.CGPathMoveToPoint(mouth, None, c - 4 * s, c - 5.5 * s)
        Quartz.CGPathAddLineToPoint(mouth, None, c + 4 * s, c - 5.5 * s)
        Quartz.CGPathAddArc(mouth, None, c, c - 5.5 * s, 4 * s, 0, math.pi, True)
        face.addSublayer_(_fill(mouth))
        bx, by = self.orb._bc
        claps, period = 5, 0.34
        for side in (-1, 1):
            hand = self._hand(side, size=0.28)
            out = bx + side * d * 0.66
            near = bx + side * d * 0.14
            values, times = [], []
            for i in range(claps):
                values += [out, near]
                times += [i / claps, (i + 0.5) / claps]
            values.append(out)
            times.append(1.0)
            hand.setPosition_(Quartz.CGPointMake(out, by - d * 0.42))
            _keys(hand, "position.x", values, claps * period, times, begin=0.15, name="clap")
        for i in range(claps):
            AppHelper.callLater(0.15 + (i + 0.5) * period, lambda: self.orb and self.orb.burst(
                gfx.accent(), stars=True, amount=0.18))
        self._body("transform.translation.y", [0, 2, 0], period, repeat=claps, name="em-hop")

    def _e_praise(self, d, s, c, ex, ey):
        self._e_smile(d, s, c, ex, ey)
        bx, by = self.orb._bc
        thumb = self._hand(1, "hand.thumbsup.fill", 0.42)
        thumb.setPosition_(Quartz.CGPointMake(bx + d * 0.7, by + d * 0.1))
        _keys(thumb, "transform.rotation.z", [0.0, -0.25, 0.1, 0.0], 0.6, begin=0.3, name="wiggle")
        AppHelper.callLater(0.2, lambda: self.orb and self.orb.burst(gfx.accent(), stars=True, amount=1.2))
        self._float("star.fill", (1.0, 0.85, 0.3), count=4, rise=34, size=9)

    def _e_dance(self, d, s, c, ex, ey):
        face = self._face_layer()
        self._happy_eyes(face, s, c, ex, ey)
        self._smile(face, s, c, wide=4.5, depth=5)
        self._cheeks(face, s, c, 0.6)
        beat = 0.42
        beats = 10
        self._body("transform.rotation.z", [0, 0.22, 0, -0.22, 0], beat * 2, repeat=beats / 2, name="em-tilt")
        self._body("transform.translation.y", [0, 6, 0], beat, repeat=beats, name="em-hop")
        self._body("transform.translation.x", [0, 5, 0, -5, 0], beat * 2, repeat=beats / 2, name="em-sway")
        bx, by = self.orb._bc
        for side in (-1, 1):
            hand = self._hand(side, size=0.26)
            low, high = by - d * 0.12, by + d * 0.38
            hand.setPosition_(Quartz.CGPointMake(bx + side * d * 0.68, low))
            first, second = (high, low) if side < 0 else (low, high)
            _keys(hand, "position.y", [low, first, low, second, low], beat * 2, repeat=beats / 2, name="dance")
        colours = [(1.0, 0.4, 0.6), (0.4, 0.8, 1.0), (1.0, 0.85, 0.3), (0.5, 1.0, 0.6)]
        for i in range(5):
            AppHelper.callLater(i * 0.8, lambda i=i: self.orb and self._float(
                random.choice(("music.note", "music.quarternote.3", "music.note")), colours[i % 4],
                count=1, rise=36, size=11, spread=22))

    def _e_yes(self, d, s, c, ex, ey):
        self._body("transform.translation.y", [0, -4, 0, -4, 0], 0.9, name="em-hop")

    def _e_no(self, d, s, c, ex, ey):
        self._body("transform.translation.x", [0, -5, 5, -5, 5, 0], 0.9, name="em-sway")

    # --- teasing -------------------------------------------------------------------------

    def _react(self, key: str) -> None:
        now = time.monotonic()
        if now - self._last_tease < 1.0:
            return
        self._last_tease = now
        print(f"  [orb reacts: {key}]", flush=True)
        self._play(key)

    def _poked(self) -> bool:
        """A click on the orb. True when it was a poke (so the chat is left alone)."""
        now = time.monotonic()
        self._clicks = [t for t in self._clicks if now - t < 2.5] + [now]
        if len(self._clicks) >= 2 and now - self._clicks[-2] < 0.6:
            self._pokes += 1
            self._react(["laugh", "surprised", "dizzy", "angry"][min(self._pokes - 1, 3)]
                        if self._pokes < 6 else random.choice(["angry", "cry", "dizzy"]))
            return True
        self._pokes = 0
        return False

    def _watch_hover(self, now: float, hovering: bool, busy: bool) -> None:
        if busy or self.hud is None:
            self._hovering = hovering
            return
        if hovering and not self._hovering:
            self._hover_start = now
            self._shy_done = False
            self._enters = [t for t in self._enters if now - t < 4.0] + [now]
            if len(self._enters) >= 4:                       # darting on and off
                self._enters = []
                self._react(random.choice(["surprised", "laugh", "wink"]))
        self._hovering = hovering
        if not hovering:
            self._angle = None
            self._spin = 0.0
            return
        if not self._shy_done and now - self._hover_start > 2.5:
            self._shy_done = True
            self._react(random.choice(["blush", "blush", "love"]))
        # Circling the orb with the pointer makes it dizzy.
        try:
            frame = self.hud._orb_window.frame()
            cx, cy = frame.origin.x + frame.size.width / 2, frame.origin.y + frame.size.height / 2
            mouse = AppKit.NSEvent.mouseLocation()
            angle = math.atan2(mouse.y - cy, mouse.x - cx)
        except Exception:
            return
        if self._angle is not None:
            delta = (angle - self._angle + math.pi) % (2 * math.pi) - math.pi
            if now - self._spin_since > 3.0:
                self._spin, self._spin_since = 0.0, now
            self._spin += delta
            if abs(self._spin) > 2.6 * math.pi:
                self._spin = 0.0
                self._react("dizzy")
        self._angle = angle


emotes = Emotes()


def play(name: str) -> str:
    return emotes.play(name)


def ensure_attached() -> None:
    emotes.ensure_attached()


def attach(hud) -> None:
    """Called by HUD.build: hook the orb up for expressions and teasing."""
    emotes.attach(hud)
