"""Sub-agents as little creatures that pop out of a toy box beside Mint.

Every sub-agent gets its own original critter - an owlet, a bunny, a sprout, a
fox kit, a kitten, a chick, a baby dragon or a bear cub - in the agent's colour.
They live in a small gift box that sits right next to Mint's orb (or on top of
the chat when it is open), and never roam the screen:

* start     - the box wiggles, the lid pops, sparkles fly and the critter jumps
              out in a spinning arc, lands with a squish, waves, shows its name.
* working   - it bobs in its spot, blinks, wiggles its ears; a small bubble over
              its head shows what it is doing (a globe, a magnifier, a pencil...)
              or "..." while it thinks.
* asking    - a "?" bubble, it hops for attention and its name tag says to click.
* done      - happy eyes, a flip and confetti, then it walks back and hops into
              the box; the lid shuts. Failed: a sad face and a tear. Stopped: a
              startled "!" and a quick dive home.

Hover a critter: it looks at you and a tag says who it is and what it is doing.
Click: it reacts (a hop and a heart, a spin, a giggle) and says its status;
click a critter with a question, or double-click any, to open the chat.
Right-click: open the chat or stop that agent. Click the box: everyone waves.
Poke one many times quickly and it gets dizzy.

The stage window is click-through except exactly over a critter or the box, and
follows Mint's screen-sharing setting like every other Mint window.
"""

from __future__ import annotations

import logging
import math
import random
import time

import AppKit
import Quartz
from PyObjCTools import AppHelper

from . import gfx, prefs

log = logging.getLogger("mint.critters")

W, H = 56.0, 64.0          # one critter's drawing box; its feet at (28, 0)
CX = 28.0
STAGE_W, STAGE_H = 400.0, 200.0
FLOOR = 34.0               # the floor line inside the stage (name tags go below)
SLOT = 46.0                # spacing between critters
FIRST = 50.0               # box to the first critter
MAX_SLOTS = 7

DARK = (0.13, 0.10, 0.16)
PINK = (1.0, 0.55, 0.66)
WHITE = (1.0, 1.0, 1.0)

SPECIES = ["owl", "bun", "sprout", "kit", "cat", "chick", "drake", "bear"]
BY_NAME = {"astra": "owl", "luna": "bun", "codex": "sprout", "sage": "kit"}

# What the bubble over a working critter shows, by the agent tool it is using.
TOOL_SYMBOLS = {
    "web_search": "magnifyingglass", "fetch_url": "globe", "read_url": "globe",
    "write_file": "pencil", "read_file": "doc.text", "list_files": "folder.fill",
    "run_command": "terminal.fill", "ask_user": "questionmark", "report_progress": "chart.bar.fill",
    "browser": "safari.fill", "look": "eye.fill", "click": "hand.tap.fill", "type_text": "keyboard",
}


def _rgb(value) -> tuple:
    value = str(value or "#7FD1FF").lstrip("#")
    try:
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (0.5, 0.82, 1.0)


def _ease(name=Quartz.kCAMediaTimingFunctionEaseInEaseOut):
    return Quartz.CAMediaTimingFunction.functionWithName_(name)


def _species(name: str) -> str:
    name = (name or "").strip().lower()
    if name in BY_NAME:
        return BY_NAME[name]
    return SPECIES[sum(ord(c) * (i + 1) for i, c in enumerate(name)) % len(SPECIES)]


def _symbol_for(tool: str) -> str:
    if tool in TOOL_SYMBOLS:
        return TOOL_SYMBOLS[tool]
    try:
        from . import activity
        return activity.SYMBOLS.get(activity.kind(tool), "sparkles")
    except Exception:
        return "sparkles"


# --- drawing helpers ----------------------------------------------------------------------

def _path(*steps):
    """A path from ('m', x, y), ('l', x, y), ('q', cx, cy, x, y), ('c', ...), ('z',), ('e', x, y, w, h)."""
    p = Quartz.CGPathCreateMutable()
    for s in steps:
        op = s[0]
        if op == "m":
            Quartz.CGPathMoveToPoint(p, None, s[1], s[2])
        elif op == "l":
            Quartz.CGPathAddLineToPoint(p, None, s[1], s[2])
        elif op == "q":
            Quartz.CGPathAddQuadCurveToPoint(p, None, s[1], s[2], s[3], s[4])
        elif op == "c":
            Quartz.CGPathAddCurveToPoint(p, None, s[1], s[2], s[3], s[4], s[5], s[6])
        elif op == "e":
            Quartz.CGPathAddEllipseInRect(p, None, Quartz.CGRectMake(s[1], s[2], s[3], s[4]))
        elif op == "r":
            Quartz.CGPathAddRoundedRect(p, None, Quartz.CGRectMake(s[1], s[2], s[3], s[4]), s[5], s[5])
        elif op == "z":
            Quartz.CGPathCloseSubpath(p)
    return p


def _ellipse(cx, cy, w, h):
    return _path(("e", cx - w / 2, cy - h / 2, w, h))


def _shape(path, fill=None, stroke=None, width=0.0, alpha=1.0, bounds=(W, H)):
    layer = Quartz.CAShapeLayer.layer()
    layer.setBounds_(Quartz.CGRectMake(0, 0, *bounds))
    layer.setPosition_(Quartz.CGPointMake(bounds[0] / 2, bounds[1] / 2))
    layer.setPath_(path)
    layer.setFillColor_(gfx.cg(fill, alpha) if fill is not None else None)
    if stroke is not None:
        layer.setStrokeColor_(gfx.cg(stroke, alpha))
        layer.setLineWidth_(width)
        layer.setLineCap_(Quartz.kCALineCapRound)
        layer.setLineJoin_(Quartz.kCALineJoinRound)
    return layer


def _pivot(layer, x, y, bounds=(W, H)) -> None:
    """Make `layer` (covering the whole drawing box) turn about (x, y)."""
    layer.setAnchorPoint_(Quartz.CGPointMake(x / bounds[0], y / bounds[1]))
    layer.setPosition_(Quartz.CGPointMake(x, y))


def _keys(layer, key, values, duration, times=None, repeat=0.0, additive=False, name=None,
          delay=0.0, cubic=False):
    a = Quartz.CAKeyframeAnimation.animationWithKeyPath_(key)
    a.setValues_(values)
    if times:
        a.setKeyTimes_(times)
    a.setDuration_(duration)
    if repeat:
        a.setRepeatCount_(repeat)
    a.setAdditive_(additive)
    if cubic:
        a.setCalculationMode_(Quartz.kCAAnimationCubic)
    if delay:
        a.setBeginTime_(Quartz.CACurrentMediaTime() + delay)
        a.setFillMode_(Quartz.kCAFillModeBackwards)
    layer.addAnimation_forKey_(a, name or key)
    return a


def _spring(layer, key, start, end, damping=9.0, stiffness=220.0, name=None):
    s = Quartz.CASpringAnimation.animationWithKeyPath_(key)
    s.setFromValue_(start)
    s.setToValue_(end)
    s.setDamping_(damping)
    s.setStiffness_(stiffness)
    s.setMass_(0.8)
    s.setDuration_(s.settlingDuration())
    layer.addAnimation_forKey_(s, name or key)


def _no_actions(fn):
    Quartz.CATransaction.begin()
    Quartz.CATransaction.setDisableActions_(True)
    try:
        fn()
    finally:
        Quartz.CATransaction.commit()


def _text_layer(text, size=10.0, color=WHITE, bold=True):
    font = AppKit.NSFont.systemFontOfSize_weight_(size, AppKit.NSFontWeightBold if bold else AppKit.NSFontWeightMedium)
    attrs = {AppKit.NSFontAttributeName: font}
    measured = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs).size()
    layer = Quartz.CATextLayer.layer()
    layer.setString_(text)
    layer.setFont_(font)
    layer.setFontSize_(size)
    layer.setForegroundColor_(gfx.cg(color))
    layer.setAlignmentMode_(Quartz.kCAAlignmentCenter)
    layer.setContentsScale_(2.0)
    layer.setBounds_(Quartz.CGRectMake(0, 0, math.ceil(measured.width) + 2, math.ceil(measured.height)))
    return layer, measured


def _symbol_layer(name, size, rgb, outline=False):
    """An SF Symbol drawn in a colour (a tinted layer masked by the symbol).
    `outline` adds a soft dark edge so pastel colours still read on a white page."""
    image = gfx.symbol(name, size, "bold")
    s = image.size()
    if outline:
        holder = Quartz.CALayer.layer()
        holder.setBounds_(Quartz.CGRectMake(0, 0, s.width, s.height))
        inner = _symbol_layer(name, size, rgb)
        inner.setPosition_(Quartz.CGPointMake(s.width / 2, s.height / 2))
        holder.addSublayer_(inner)
        holder.setShadowColor_(gfx.cg(DARK))
        holder.setShadowOpacity_(0.55)
        holder.setShadowRadius_(1.2)
        holder.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        return holder
    tint = Quartz.CALayer.layer()
    tint.setBounds_(Quartz.CGRectMake(0, 0, s.width, s.height))
    tint.setBackgroundColor_(gfx.cg(rgb))
    mask = Quartz.CALayer.layer()
    mask.setBounds_(Quartz.CGRectMake(0, 0, s.width, s.height))
    mask.setPosition_(Quartz.CGPointMake(s.width / 2, s.height / 2))
    mask.setContents_(image)
    mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
    tint.setMask_(mask)
    return tint


def _pill(text, rgb, size=10.0, second=""):
    """A dark rounded name tag, optionally with a second, lighter line."""
    top, m1 = _text_layer(text, size, gfx.light(rgb))
    lines = [top]
    width, height = m1.width, m1.height
    if second:
        low, m2 = _text_layer(second, size - 1, (0.92, 0.92, 0.95), bold=False)
        lines.append(low)
        width, height = max(width, m2.width), height + m2.height
    pill = Quartz.CALayer.layer()
    pill.setBounds_(Quartz.CGRectMake(0, 0, width + 14, height + 6))
    pill.setCornerRadius_(min(9.0, (height + 6) / 2))
    pill.setBackgroundColor_(gfx.cg((0.08, 0.08, 0.11), 0.9))
    pill.setBorderColor_(gfx.cg(rgb, 0.8))
    pill.setBorderWidth_(1.0)
    y = height + 3
    for line in lines:
        b = line.bounds()
        y -= b.size.height
        line.setPosition_(Quartz.CGPointMake((width + 14) / 2, y + b.size.height / 2))
        pill.addSublayer_(line)
    return pill


_images: dict = {}


def _particle(name):
    if name not in _images:
        try:
            _images[name] = gfx.cg_image(gfx.symbol(name, 12, "bold", white=True))
        except Exception:
            _images[name] = None
    return _images[name]


def confetti(root, x, y, colors, names=("heart.fill", "star.fill", "sparkle"), amount=1.0, up=True):
    """A small burst of hearts, stars and sparkles at (x, y) in `root`."""
    emitter = Quartz.CAEmitterLayer.layer()
    emitter.setEmitterPosition_(Quartz.CGPointMake(x, y))
    emitter.setEmitterShape_(Quartz.kCAEmitterLayerPoint)
    cells = []
    for i, name in enumerate(names):
        image = _particle(name)
        if image is None:
            continue
        for rgb in colors:
            cell = Quartz.CAEmitterCell.emitterCell()
            cell.setContents_(image)
            cell.setBirthRate_(90 * amount / max(1, len(colors)))    # a burst lasts 0.14 s
            cell.setLifetime_(1.1)
            cell.setLifetimeRange_(0.3)
            cell.setVelocity_(70)
            cell.setVelocityRange_(30)
            cell.setEmissionLongitude_(math.pi / 2 if up else 0)
            cell.setEmissionRange_(math.pi * (0.7 if up else 2))
            cell.setYAcceleration_(-120 if up else 0)
            cell.setScale_(0.55)
            cell.setScaleRange_(0.2)
            cell.setScaleSpeed_(-0.25)
            cell.setAlphaSpeed_(-0.8)
            cell.setSpin_(1.5)
            cell.setSpinRange_(4)
            cell.setColor_(gfx.cg(rgb))
            cells.append(cell)
    emitter.setEmitterCells_(cells)
    emitter.setBeginTime_(Quartz.CACurrentMediaTime())
    root.addSublayer_(emitter)
    AppHelper.callLater(0.14, lambda: emitter.setBirthRate_(0.0))
    AppHelper.callLater(1.8, emitter.removeFromSuperlayer)


def float_up(root, x, y, name, rgb, size=11.0, rise=34.0, seconds=1.1, drift=0.0, delay=0.0):
    """One symbol (a heart, a note...) floats up from (x, y), sways and fades."""
    layer = _symbol_layer(name, size, rgb, outline=True)
    layer.setPosition_(Quartz.CGPointMake(x, y))
    layer.setOpacity_(0.0)
    root.addSublayer_(layer)
    _keys(layer, "position.y", [y, y + rise], seconds, additive=False, delay=delay, name="rise")
    _keys(layer, "position.x", [x, x + drift + 3, x + drift - 3, x + drift], seconds, delay=delay,
          cubic=True, name="sway")
    _keys(layer, "opacity", [0.0, 1.0, 1.0, 0.0], seconds, [0, 0.15, 0.6, 1], delay=delay, name="fade")
    _keys(layer, "transform.scale", [0.3, 1.15, 1.0, 0.8], seconds, [0, 0.2, 0.5, 1], delay=delay, name="pop")
    AppHelper.callLater(delay + seconds + 0.1, layer.removeFromSuperlayer)
    return layer


# --- one creature ---------------------------------------------------------------------------

class Critter:
    def __init__(self, run: str, name: str, rgb: tuple, taken=(), species: str | None = None) -> None:
        self.run, self.name, self.rgb = run, name, rgb
        self.performer = False        # part of a show, not a real agent
        self.species = species or _species(name)
        if self.species in taken and name.strip().lower() not in BY_NAME and not species:
            # Two of the same side by side are hard to tell apart: take one nobody has.
            self.species = next((s for s in SPECIES if s not in taken), self.species)
        self.state = "waiting"        # waiting -> jumping -> out -> going -> gone
        self.status = ""
        self.mood = "normal"
        self.token = 0
        self.x = 0.0                  # its spot on the floor (stage coordinates)
        self.pokes: list[float] = []
        self.next_blink = time.monotonic() + random.uniform(1.0, 3.5)
        self.next_fidget = time.monotonic() + random.uniform(4.0, 9.0)
        self.tag = None
        self.bubble = None
        self.bubble_kind = ""
        self.pending = None           # the latest event that came while it was still jumping out
        self.parent = None            # run id of the agent that called this one in to help, if any
        self._build()

    # --- construction --------------------------------------------------------------

    def _build(self) -> None:
        rgb = self.rgb
        fur = gfx.mix(rgb, WHITE, 0.22)
        edge = gfx.mix(rgb, DARK, 0.45)
        belly = gfx.mix(rgb, WHITE, 0.68)
        self.fur, self.edge = fur, edge

        box = Quartz.CALayer.layer()            # position = feet; jumps, turns, scales
        box.setBounds_(Quartz.CGRectMake(0, 0, W, H))
        box.setAnchorPoint_(Quartz.CGPointMake(0.5, 0.0))
        self.box = box
        body = Quartz.CALayer.layer()           # squash, stretch and bob, from the feet
        body.setBounds_(Quartz.CGRectMake(0, 0, W, H))
        body.setAnchorPoint_(Quartz.CGPointMake(0.5, 0.0))
        body.setPosition_(Quartz.CGPointMake(CX, 0))
        scaler = Quartz.CALayer.layer()         # a helper's helper is drawn a little smaller
        scaler.setBounds_(Quartz.CGRectMake(0, 0, W, H))
        scaler.setAnchorPoint_(Quartz.CGPointMake(0.5, 0.0))
        scaler.setPosition_(Quartz.CGPointMake(CX, 0))
        scaler.addSublayer_(body)
        box.addSublayer_(scaler)
        self.scaler = scaler
        self.body = body
        self.parts: dict[str, list] = {"ears": [], "tail": [], "wings": [], "leaves": []}

        behind, front = self._features(fur, edge, belly)
        for layer in behind:
            body.addSublayer_(layer)
        # Feet, body, belly.
        foot = gfx.mix(rgb, DARK, 0.25) if self.species not in ("chick",) else (1.0, 0.62, 0.2)
        self.feet = []
        for fx_ in (21.0, 35.0):
            f = _shape(_ellipse(fx_, 3.2, 10, 6), foot, edge, 1.0)
            body.addSublayer_(f)
            self.feet.append(f)
        tall = 30 if self.species == "drake" else 28
        wide = 32 if self.species in ("chick", "bear") else 30
        body.addSublayer_(_shape(_ellipse(CX, 4 + tall / 2, wide, tall), fur, edge, 1.3))
        body.addSublayer_(_shape(_ellipse(CX, 12.5, 18, 13), belly))
        for layer in front:
            body.addSublayer_(layer)

        # Cheeks, eyes, mouth.
        for cx in (17.5, 38.5):
            body.addSublayer_(_shape(_ellipse(cx, 16.5, 6, 3.4), PINK, alpha=0.6))
        self.eyes = []
        big = self.species == "owl"
        for ex in (22.5, 33.5):
            eye = Quartz.CALayer.layer()
            eye.setBounds_(Quartz.CGRectMake(0, 0, 8, 9))
            eye.setPosition_(Quartz.CGPointMake(ex, 22.0))
            if big:
                ring = _shape(_ellipse(4, 4.5, 10, 10), WHITE, edge, 0.8, bounds=(8, 9))
                eye.addSublayer_(ring)
            ball = _shape(_ellipse(4, 4.5, 5.2 if not big else 5.6, 6.6 if not big else 6.2), DARK, bounds=(8, 9))
            shine = _shape(_ellipse(5.3, 6.3, 2.2, 2.2), WHITE, bounds=(8, 9))
            eye.addSublayer_(ball)
            eye.addSublayer_(shine)
            body.addSublayer_(eye)
            self.eyes.append(eye)
        # Happy "^ ^" eyes and dizzy "@ @" eyes, shown instead of the round ones.
        self.happy_eyes = _shape(_path(("m", 20, 21), ("q", 22.5, 25.5, 25, 21),
                                       ("m", 31, 21), ("q", 33.5, 25.5, 36, 21)), None, DARK, 1.7)
        self.happy_eyes.setHidden_(True)
        body.addSublayer_(self.happy_eyes)
        spiral = []
        for ex in (22.5, 33.5):
            spiral += [("m", ex + 2.4, 22), ("q", ex + 2.4, 24.6, ex, 24.6), ("q", ex - 2.6, 24.6, ex - 2.6, 22),
                       ("q", ex - 2.6, 19.8, ex, 19.8), ("q", ex + 1.4, 19.8, ex + 1.4, 22)]
        self.dizzy_eyes = _shape(_path(*spiral), None, DARK, 1.2)
        self.dizzy_eyes.setHidden_(True)
        body.addSublayer_(self.dizzy_eyes)
        if self.species in ("owl", "chick"):
            body.addSublayer_(_shape(_path(("m", 26, 18.5), ("l", 30, 18.5), ("l", 28, 15.4), ("z",)),
                                     (1.0, 0.66, 0.2), gfx.mix((1.0, 0.66, 0.2), DARK, 0.4), 0.8))
        self.mouth = _shape(None, None, DARK, 1.3)
        body.addSublayer_(self.mouth)
        if self.species == "cat":
            body.addSublayer_(_shape(_path(("m", 16, 17.5), ("l", 9, 19.5), ("m", 16, 16), ("l", 9, 15),
                                           ("m", 40, 17.5), ("l", 47, 19.5), ("m", 40, 16), ("l", 47, 15)),
                                     None, edge, 0.8))
        self.set_mood("normal")

    def _features(self, fur, edge, belly):
        """(layers behind the body, layers in front) for this species."""
        s, behind, front = self.species, [], []
        inner = gfx.mix(PINK, fur, 0.25)

        def ear(points, inner_points=None, pivot=None, into=behind):
            steps = [("m", *points[0])] + [("l", *p) for p in points[1:]] + [("z",)]
            layer = _shape(_path(*steps), fur, edge, 1.2)
            if inner_points:
                steps = [("m", *inner_points[0])] + [("l", *p) for p in inner_points[1:]] + [("z",)]
                layer.addSublayer_(_shape(_path(*steps), inner))
            if pivot:
                _pivot(layer, *pivot)
            into.append(layer)
            self.parts["ears"].append(layer)

        if s in ("kit", "cat"):
            tip = 45 if s == "kit" else 42
            ear([(17, 27), (14, tip), (25, 31)], [(18.5, 29.5), (16.2, tip - 6), (22.5, 31)], (20, 29))
            ear([(39, 27), (42, tip), (31, 31)], [(37.5, 29.5), (39.8, tip - 6), (33.5, 31)], (36, 29))
        elif s == "bun":
            for cx, lean in ((22.5, -1), (33.5, 1)):
                layer = _shape(_path(("e", cx - 4.5, 27, 9, 24)), fur, edge, 1.2)
                layer.addSublayer_(_shape(_path(("e", cx - 2.2, 30, 4.4, 17)), inner))
                _pivot(layer, cx, 28)
                layer.setAffineTransform_(Quartz.CGAffineTransformMakeRotation(-lean * 0.12))
                behind.append(layer)
                self.parts["ears"].append(layer)
        elif s == "owl":
            ear([(15, 26), (14, 38), (23, 31)], None, (18, 29))
            ear([(41, 26), (42, 38), (33, 31)], None, (38, 29))
        elif s == "bear":
            for cx in (17.0, 39.0):
                layer = _shape(_ellipse(cx, 30.5, 11, 11), fur, edge, 1.2)
                layer.addSublayer_(_shape(_ellipse(cx, 30.5, 5, 5), inner))
                _pivot(layer, cx, 28)
                behind.append(layer)
                self.parts["ears"].append(layer)
        elif s == "drake":
            horn = (1.0, 0.93, 0.78)
            for pts in ([(20, 31), (17.5, 40), (24.5, 32.5)], [(36, 31), (38.5, 40), (31.5, 32.5)]):
                layer = _shape(_path(("m", *pts[0]), ("l", *pts[1]), ("l", *pts[2]), ("z",)), horn, edge, 1.0)
                behind.append(layer)
            for side in (-1, 1):
                x0 = CX + side * 11
                wing = _shape(_path(("m", x0, 22), ("q", x0 + side * 12, 32, x0 + side * 15, 26),
                                    ("q", x0 + side * 11, 22, x0 + side * 13, 17), ("q", x0 + side * 7, 18, x0, 16),
                                    ("z",)), gfx.mix(fur, belly, 0.4), edge, 1.1)
                _pivot(wing, x0, 19)
                behind.append(wing)
                self.parts["wings"].append(wing)
            front.append(_shape(_path(("m", 25, 32), ("l", 28, 36), ("l", 31, 32), ("z",)), belly, edge, 0.9))
        elif s == "chick":
            for dx, h in ((-3, 5), (0, 7), (3, 5)):
                layer = _shape(_ellipse(CX + dx, 32 + h / 2, 3.2, h), fur, edge, 0.9)
                front.append(layer)
            for side in (-1, 1):
                wing = _shape(_ellipse(CX + side * 15.5, 15, 7, 12), gfx.mix(fur, edge, 0.2), edge, 1.0)
                _pivot(wing, CX + side * 14, 19)
                front.append(wing)
                self.parts["wings"].append(wing)
        elif s == "sprout":
            green = (0.42, 0.82, 0.42)
            leaf_edge = (0.2, 0.5, 0.25)
            front.append(_shape(_path(("m", CX, 31), ("l", CX, 36)), None, leaf_edge, 1.4))
            for side in (-1, 1):
                leaf = _shape(_path(("m", CX, 36), ("q", CX + side * 4, 45, CX + side * 13, 44),
                                    ("q", CX + side * 9, 35, CX, 36), ("z",)), green, leaf_edge, 1.0)
                _pivot(leaf, CX, 36)
                front.append(leaf)
                self.parts["leaves"].append(leaf)

        if s == "kit":
            tail = _shape(_path(("m", 40, 8), ("q", 55, 5, 53, 20), ("q", 52, 28, 45, 25),
                                ("q", 47, 15, 40, 13), ("z",)), fur, edge, 1.2)
            tail.addSublayer_(_shape(_ellipse(50.5, 22.5, 6, 6), WHITE))
            _pivot(tail, 41, 10)
            behind.append(tail)
            self.parts["tail"].append(tail)
        elif s == "cat":
            tail = _shape(_path(("m", 40, 7), ("q", 51, 6, 49, 18), ("q", 48, 25, 53, 26)), None, edge, 3.6)
            inner_tail = _shape(_path(("m", 40, 7), ("q", 51, 6, 49, 18), ("q", 48, 25, 53, 26)), None, fur, 2.2)
            tail.addSublayer_(inner_tail)
            _pivot(tail, 41, 8)
            behind.append(tail)
            self.parts["tail"].append(tail)
        elif s in ("bun", "bear"):
            tail = _shape(_ellipse(43.5, 9, 8 if s == "bun" else 6, 8 if s == "bun" else 6),
                          WHITE if s == "bun" else fur, edge, 1.0)
            behind.append(tail)
        elif s == "drake":
            tail = _shape(_path(("m", 40, 6), ("q", 50, 4, 54, 9), ("l", 51, 10), ("l", 53, 14),
                                ("q", 47, 12, 41, 13), ("z",)), fur, edge, 1.1)
            _pivot(tail, 41, 9)
            behind.append(tail)
            self.parts["tail"].append(tail)
        elif s == "owl":
            for side in (-1, 1):
                wing = _shape(_ellipse(CX + side * 15, 14, 7, 14), gfx.mix(fur, edge, 0.25), edge, 1.0)
                _pivot(wing, CX + side * 14, 19)
                front.append(wing)
                self.parts["wings"].append(wing)
        return behind, front

    # --- faces ------------------------------------------------------------------------

    def set_mood(self, mood: str) -> None:
        self.mood = mood
        red = (0.62, 0.16, 0.24)
        paths = {
            "normal": (_path(("m", 25, 17), ("q", 28, 14, 31, 17)), None),
            "happy": (_path(("m", 24.5, 17), ("q", 28, 11.5, 31.5, 17), ("z",)), red),
            "surprised": (_ellipse(28, 14.6, 3.4, 4.0), red),
            "sad": (_path(("m", 25, 14), ("q", 28, 17, 31, 14)), None),
            "asking": (_ellipse(28, 15, 2.6, 2.6), red),
            "dizzy": (_path(("m", 24.5, 15), ("q", 26.2, 17, 28, 15), ("q", 29.8, 13, 31.5, 15)), None),
        }
        path, fill = paths.get(mood, paths["normal"])

        def apply():
            self.mouth.setPath_(path)
            self.mouth.setFillColor_(gfx.cg(fill) if fill else None)
            happy = mood == "happy"
            dizzy = mood == "dizzy"
            self.happy_eyes.setHidden_(not happy)
            self.dizzy_eyes.setHidden_(not dizzy)
            for eye in self.eyes:
                eye.setHidden_(happy or dizzy)
        _no_actions(apply)
        if self.species in ("owl", "chick"):
            self.mouth.setHidden_(mood in ("normal", "asking"))

    def make_helper(self) -> None:
        """Called in by another agent: a bit smaller, so it reads as that one's helper."""
        _no_actions(lambda: self.scaler.setTransform_(Quartz.CATransform3DMakeScale(0.8, 0.8, 1)))

    def blink(self) -> None:
        for eye in self.eyes:
            _keys(eye, "transform.scale.y", [1.0, 0.1, 1.0], 0.18, name="blink")

    def look(self, dx: float, dy: float) -> None:
        """Eyes toward a direction (a unit-ish vector), a couple of points at most."""
        length = math.hypot(dx, dy) or 1.0
        ox, oy = 1.4 * dx / length, 1.1 * dy / length

        def apply():
            for eye, ex in zip(self.eyes, (22.5, 33.5)):
                eye.setPosition_(Quartz.CGPointMake(ex + ox, 22.0 + oy))
        _no_actions(apply)

    SIGNATURES = {"owl": "hoo hoo!", "bun": "thump thump", "sprout": "whirr!", "kit": "chases its tail",
                  "cat": "streeetch", "chick": "peck peck", "drake": "rawr!", "bear": "bear hug!"}

    def signature(self, root, direction: int = -1) -> str:
        """This species' own little move. Returns a caption for it."""
        s = self.species
        if s == "owl":                      # head tilts side to side, a slow big blink
            _keys(self.body, "transform.rotation.z", [0, 0.32, 0.32, -0.32, -0.32, 0], 1.1,
                  [0, 0.15, 0.4, 0.55, 0.85, 1], additive=True, name="tilt")
            AppHelper.callLater(0.45, self.blink)
            float_up(root, self.box.position().x + 12, FLOOR + 40, "music.note", gfx.light(self.rgb), drift=6)
        elif s == "bun":                    # one ear flops over, then two thumps
            if self.parts["ears"]:
                _keys(self.parts["ears"][-1], "transform.rotation.z", [0, -0.9, -0.9, 0], 1.0, [0, 0.2, 0.7, 1],
                      additive=True, name="flop")
            self.hop(6, 0.25)
            self.hop(6, 0.25, delay=0.35)
        elif s == "sprout":                 # leaves whirl like a propeller and lift it up
            for leaf in self.parts["leaves"]:
                _keys(leaf, "transform.rotation.z", [0, 0.6, -0.6, 0.6, -0.6, 0], 0.9, additive=True, name="whirl")
            self.hop(22, 0.9)
            confetti(root, self.box.position().x, FLOOR + 44, [(0.5, 0.9, 0.5), (1.0, 0.95, 0.6)],
                     names=("leaf.fill", "sparkle"), amount=0.5)
        elif s == "kit":                    # chases its tail: two quick turns
            self.spin(2, 0.9)
            for tail in self.parts["tail"]:
                _keys(tail, "transform.rotation.z", [0, 0.5, -0.4, 0.5, 0], 0.9, additive=True, name="swish")
            self.hop(8, 0.9)
        elif s == "cat":                    # a long lazy stretch, then a heart
            _keys(self.body, "transform.scale.x", [1, 1.32, 1.32, 1], 1.1, [0, 0.3, 0.75, 1], name="stretchx")
            _keys(self.body, "transform.scale.y", [1, 0.74, 0.74, 1], 1.1, [0, 0.3, 0.75, 1], name="stretchy")
            AppHelper.callLater(0.9, lambda: self.heart(root, 1))
        elif s == "chick":                  # three quick pecks with flapping wings
            _keys(self.body, "transform.rotation.z", [0, direction * 0.35, 0, direction * 0.35, 0,
                                                      direction * 0.35, 0], 0.9, additive=True, name="peck")
            self.flap_fast(0.9)
        elif s == "drake":                  # puffs a tiny flame
            self.flap_fast(0.7)
            p = self.box.position()
            flame = _symbol_layer("flame.fill", 12, (1.0, 0.55, 0.2), outline=True)
            flame.setPosition_(Quartz.CGPointMake(p.x + direction * 16, p.y + 16))
            flame.setOpacity_(0.0)
            root.addSublayer_(flame)
            _keys(flame, "position.x", [p.x + direction * 16, p.x + direction * 46], 0.7, name="shoot")
            _keys(flame, "transform.scale", [0.3, 1.3, 0.6], 0.7, name="grow")
            _keys(flame, "opacity", [0, 1, 0], 0.7, name="fade")
            AppHelper.callLater(0.75, flame.removeFromSuperlayer)
            self.squish()
        else:                               # bear: a big wave and a hug
            self.wiggle()
            self.hop(10, 0.5)
            AppHelper.callLater(0.4, lambda: self.heart(root, 2))
        return self.SIGNATURES.get(s, "ta-da!")

    def fidget(self) -> None:
        """Something small and alive: an ear flick, a tail swish, a flap, a leaf sway."""
        for ear in self.parts["ears"]:
            _keys(ear, "transform.rotation.z", [0, 0.25, -0.1, 0], 0.5, additive=True, name="flick",
                  delay=random.uniform(0, 0.12))
        for tail in self.parts["tail"]:
            _keys(tail, "transform.rotation.z", [0, 0.35, -0.2, 0.15, 0], 0.9, additive=True, name="swish")
        for wing in self.parts["wings"]:
            side = 1 if wing.position().x > CX else -1
            _keys(wing, "transform.rotation.z", [0, side * 0.5, 0, side * 0.4, 0], 0.5, additive=True, name="flap")
        for leaf in self.parts["leaves"]:
            _keys(leaf, "transform.rotation.z", [0, 0.2, -0.2, 0], 1.0, additive=True, name="sway")

    def flap_fast(self, seconds: float = 0.8) -> None:
        for wing in self.parts["wings"]:
            side = 1 if wing.position().x > CX else -1
            _keys(wing, "transform.rotation.z", [0, side * 0.7, 0], 0.16, additive=True, name="flap",
                  repeat=max(1.0, seconds / 0.16))
        for ear in self.parts["ears"]:
            _keys(ear, "transform.rotation.z", [0, -0.3, 0], 0.3, additive=True, name="flick")

    # --- motion -----------------------------------------------------------------------

    def bob(self, on: bool, fast: bool = False) -> None:
        self.body.removeAnimationForKey_("bob")
        if on:
            _keys(self.body, "transform.translation.y", [0, 1.8, 0], 0.45 if fast else 1.0,
                  repeat=float("inf"), additive=True, name="bob", cubic=True)

    def hop(self, height: float = 12.0, seconds: float = 0.45, delay: float = 0.0) -> None:
        _keys(self.box, "position.y", [0, height, 0], seconds, [0, 0.45, 1], additive=True,
              name=f"hop{random.random()}", delay=delay)
        _keys(self.body, "transform.scale.y", [1, 0.78, 1.12, 1, 0.82, 1], seconds,
              [0, 0.08, 0.3, 0.8, 0.9, 1], additive=False, name="squash", delay=delay)

    def attention(self, on: bool) -> None:
        self.box.removeAnimationForKey_("attention")
        if on:
            _keys(self.box, "position.y", [0, 10, 0, 0], 1.3, [0, 0.2, 0.4, 1], repeat=float("inf"),
                  additive=True, name="attention")

    def squish(self) -> None:
        _keys(self.body, "transform.scale.y", [0.62, 1.12, 0.95, 1.0], 0.45, [0, 0.35, 0.7, 1], name="squash")
        _keys(self.body, "transform.scale.x", [1.3, 0.9, 1.04, 1.0], 0.45, [0, 0.35, 0.7, 1], name="squashx")

    def spin(self, turns: float = 1.0, seconds: float = 0.6) -> None:
        _keys(self.body, "transform.rotation.y", [0, 2 * math.pi * turns], seconds, name="spin")

    def flip(self, direction: int = 1) -> None:
        """A somersault in the air, turning about its middle rather than its feet."""
        _keys(self.box, "position.y", [0, 34, 0], 0.7, [0, 0.5, 1], additive=True, name="flipup")
        values = []
        for k in range(13):
            t = Quartz.CATransform3DMakeTranslation(0, 18, 0)
            t = Quartz.CATransform3DRotate(t, -direction * 2 * math.pi * k / 12, 0, 0, 1)
            values.append(AppKit.NSValue.valueWithCATransform3D_(Quartz.CATransform3DTranslate(t, 0, -18, 0)))
        _keys(self.body, "transform", values, 0.7, name="flip")

    def wiggle(self) -> None:
        _keys(self.body, "transform.rotation.z", [0, 0.18, -0.18, 0.12, -0.12, 0], 0.6, name="wiggle")

    def walk(self, on: bool, fast: bool = False) -> None:
        step = 0.16 if fast else 0.22
        for i, foot in enumerate(self.feet):
            foot.removeAnimationForKey_("step")
            if on:
                _keys(foot, "transform.translation.y", [0, 3.0, 0, 0], step * 2, [0, 0.25, 0.5, 1],
                      repeat=float("inf"), additive=True, name="step", delay=step * i)
        self.body.removeAnimationForKey_("waddle")
        if on:
            _keys(self.body, "transform.rotation.z", [0, 0.07, 0, -0.07, 0], step * 2, repeat=float("inf"),
                  additive=True, name="waddle")

    def tear(self, root) -> None:
        """A single tear rolls down from an eye."""
        drop = _shape(_path(("m", 3, 7), ("q", 6, 2, 3, 0), ("q", 0, 2, 3, 7), ("z",)),
                      (0.55, 0.8, 1.0), WHITE, 0.6, bounds=(6, 7))
        start = Quartz.CGPointMake(CX - 7, 19)
        drop.setPosition_(start)
        self.box.addSublayer_(drop)          # rides along if it walks away
        _keys(drop, "position.y", [start.y, start.y - 14], 1.0, name="fall")
        _keys(drop, "opacity", [0, 1, 1, 0], 1.0, [0, 0.1, 0.7, 1], name="fade")
        AppHelper.callLater(1.05, drop.removeFromSuperlayer)

    def heart(self, root, count: int = 1) -> None:
        p = self.box.position()
        for i in range(count):
            float_up(root, p.x + random.uniform(-8, 8), p.y + 40, "heart.fill",
                     random.choice([PINK, (1.0, 0.4, 0.5), gfx.light(self.rgb)]), size=10 + random.random() * 3,
                     drift=random.uniform(-10, 10), delay=i * 0.12)

    # --- bubble and tag ----------------------------------------------------------------

    def show_bubble(self, kind: str, symbol: str = "") -> None:
        """Over its head: 'think' (three dots), 'ask' (?), 'wow' (!), or 'tool' with a symbol."""
        if self.bubble_kind == f"{kind}:{symbol}" and self.bubble is not None:
            return
        self.hide_bubble(quick=True)
        self.bubble_kind = f"{kind}:{symbol}"
        ask = kind == "ask"
        bw, bh = (22.0, 18.0)
        bubble = Quartz.CALayer.layer()
        bubble.setBounds_(Quartz.CGRectMake(0, 0, bw, bh + 5))
        bubble.setAnchorPoint_(Quartz.CGPointMake(0.3, 0.0))
        bubble.setPosition_(Quartz.CGPointMake(CX + 6, 44))
        back = gfx.mix(self.rgb, WHITE, 0.15) if ask else WHITE
        shape = _shape(_path(("r", 0, 5, bw, bh, 8), ("m", 4, 7), ("l", 3, 0), ("l", 10, 6), ("z",)),
                       back, gfx.mix(self.rgb, DARK, 0.2), 1.0, bounds=(bw, bh + 5))
        shape.setShadowColor_(gfx.cg(DARK))
        shape.setShadowOpacity_(0.25)
        shape.setShadowRadius_(2)
        shape.setShadowOffset_(Quartz.CGSizeMake(0, -1))
        bubble.addSublayer_(shape)
        ink = gfx.mix(self.rgb, DARK, 0.5)
        if kind == "think":
            for i in range(3):
                dot = _shape(_ellipse(0, 0, 3.4, 3.4), ink, bounds=(0.01, 0.01))
                dot.setPosition_(Quartz.CGPointMake(6 + i * 5, 5 + bh / 2))
                bubble.addSublayer_(dot)
                _keys(dot, "transform.translation.y", [0, 2.5, 0, 0], 0.9, [0, 0.18, 0.36, 1],
                      repeat=float("inf"), additive=True, name="dot", delay=i * 0.15)
        else:
            name = {"ask": "questionmark", "wow": "exclamationmark"}.get(kind, symbol or "sparkles")
            glyph = _symbol_layer(name, 10.5, WHITE if ask else ink)
            glyph.setPosition_(Quartz.CGPointMake(bw / 2, 5 + bh / 2))
            bubble.addSublayer_(glyph)
            if kind == "tool":
                if name in ("globe", "gearshape.fill", "arrow.triangle.2.circlepath"):
                    _keys(glyph, "transform.rotation.z", [0, -2 * math.pi], 2.4, repeat=float("inf"), name="work")
                elif name in ("magnifyingglass", "eye.fill"):
                    _keys(glyph, "transform.translation.x", [0, 2.2, 0, -2.2, 0], 1.4, repeat=float("inf"),
                          additive=True, name="work", cubic=True)
                elif name in ("pencil", "keyboard", "terminal.fill"):
                    _keys(glyph, "transform.rotation.z", [0, 0.25, 0, 0.18, 0], 0.5, repeat=float("inf"),
                          additive=True, name="work")
                else:
                    _keys(glyph, "transform.scale", [1, 1.15, 1], 0.9, repeat=float("inf"), name="work")
            if ask:
                _keys(bubble, "transform.rotation.z", [0, 0.12, -0.12, 0], 0.7, repeat=float("inf"),
                      additive=True, name="wobble")
        self.box.addSublayer_(bubble)
        _spring(bubble, "transform.scale", 0.2, 1.0, damping=8, stiffness=260, name="pop")
        self.bubble = bubble

    def hide_bubble(self, quick: bool = False) -> None:
        bubble, self.bubble, self.bubble_kind = self.bubble, None, ""
        if bubble is None:
            return
        if quick:
            bubble.removeFromSuperlayer()
            return
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.2)
        bubble.setOpacity_(0.0)
        bubble.setTransform_(Quartz.CATransform3DMakeScale(0.3, 0.3, 1))
        Quartz.CATransaction.commit()
        AppHelper.callLater(0.25, bubble.removeFromSuperlayer)

    def show_tag(self, root, second: str = "", seconds: float | None = None) -> None:
        if stage._tagged is not None and stage._tagged is not self:
            stage._tagged.hide_tag()         # one name tag at a time: they would overlap
        self.hide_tag()
        stage._tagged = self
        tag = _pill(self.name, self.rgb, 10.0, second[:46])
        p = self.box.position()
        tag.setPosition_(Quartz.CGPointMake(p.x, FLOOR - 4 - tag.bounds().size.height / 2))
        root.addSublayer_(tag)
        _spring(tag, "transform.scale", 0.5, 1.0, damping=10, stiffness=300, name="pop")
        self.tag = tag
        if seconds:
            token = id(tag)
            AppHelper.callLater(seconds, lambda: self.tag is not None and id(self.tag) == token and self.hide_tag())

    def hide_tag(self) -> None:
        if self.tag is not None:
            self.tag.removeFromSuperlayer()
            self.tag = None
        if stage._tagged is self:
            stage._tagged = None

    def hit(self, x: float, y: float) -> bool:
        p = self.box.presentationLayer().position() if self.box.presentationLayer() else self.box.position()
        return abs(x - p.x) < 19 and p.y - 4 < y < p.y + 42


# --- the toy box ---------------------------------------------------------------------------

class ToyBox:
    """A little gift box: a base with a ribbon, a hinged lid with a bow."""
    BW, BH = 60.0, 56.0

    def __init__(self) -> None:
        accent = gfx.accent()
        base_rgb = gfx.mix(accent, WHITE, 0.3)
        edge = gfx.mix(accent, DARK, 0.45)
        ribbon = (1.0, 0.5, 0.66)
        ribbon_edge = gfx.mix(ribbon, DARK, 0.35)
        c = self.BW / 2
        b = (self.BW, self.BH)
        root = Quartz.CALayer.layer()
        root.setBounds_(Quartz.CGRectMake(0, 0, *b))
        root.setAnchorPoint_(Quartz.CGPointMake(0.5, 0.0))
        self.root = root
        shadow = _shape(_ellipse(c, 1.5, 38, 6), DARK, alpha=0.22, bounds=b)
        root.addSublayer_(shadow)
        body = Quartz.CALayer.layer()
        body.setBounds_(Quartz.CGRectMake(0, 0, *b))
        body.setAnchorPoint_(Quartz.CGPointMake(0.5, 0.0))
        body.setPosition_(Quartz.CGPointMake(c, 0))
        root.addSublayer_(body)
        self.body = body
        body.addSublayer_(_shape(_path(("r", c - 16, 1, 32, 22, 5)), base_rgb, edge, 1.3, bounds=b))
        body.addSublayer_(_shape(_path(("r", c - 3.5, 1.6, 7, 20.8, 1.5)), ribbon, ribbon_edge, 0.8, bounds=b))
        # A face on the box too: it is part of the family.
        for ex in (c - 8, c + 8):
            body.addSublayer_(_shape(_ellipse(ex, 12, 2.6, 3.2), DARK, bounds=b))
        body.addSublayer_(_shape(_path(("m", c - 10.5, 8.5), ("q", c - 9, 7.4, c - 7.5, 8.5)), None, PINK, 1.2, bounds=b))
        self.inside = _shape(_path(("r", c - 14, 21, 28, 5, 2)), gfx.mix(accent, DARK, 0.5), bounds=b)
        self.inside.setHidden_(True)
        body.addSublayer_(self.inside)
        lid = _shape(_path(("r", c - 18, 22, 36, 7.5, 3)), gfx.mix(base_rgb, WHITE, 0.12), edge, 1.3, bounds=b)
        lid.addSublayer_(_shape(_path(("r", c - 3.5, 22.4, 7, 6.8, 1.5)), ribbon, ribbon_edge, 0.8, bounds=b))
        bow = _shape(_path(("m", c, 30), ("q", c - 10, 38, c - 8, 30), ("q", c - 6, 27, c, 30),
                           ("m", c, 30), ("q", c + 10, 38, c + 8, 30), ("q", c + 6, 27, c, 30)),
                     ribbon, ribbon_edge, 0.9, bounds=b)
        lid.addSublayer_(bow)
        lid.addSublayer_(_shape(_ellipse(c, 30, 3.4, 3.4), ribbon_edge, bounds=b))
        self.lid = lid
        self.hinge_left = True
        _pivot(lid, c + 18, 22, b)
        body.addSublayer_(lid)
        self.opened = 0

    def set_hinge(self, direction: int) -> None:
        """The lid opens toward the critters' side (direction -1 = left)."""
        c = self.BW / 2
        _no_actions(lambda: _pivot(self.lid, c + 18 if direction < 0 else c - 18, 22, (self.BW, self.BH)))
        self.direction = direction

    def appear(self) -> None:
        self.root.setOpacity_(1.0)
        _spring(self.root, "transform.scale", 0.05, 1.0, damping=7, stiffness=200, name="appear")
        _keys(self.body, "transform.rotation.z", [0, 0.2, -0.15, 0.08, 0], 0.7, delay=0.1, name="wiggle")

    def vanish(self) -> None:
        _keys(self.root, "transform.scale", [1.0, 1.15, 0.0], 0.4, [0, 0.3, 1], name="vanish")
        _keys(self.root, "opacity", [1.0, 1.0, 0.0], 0.4, [0, 0.5, 1], name="fade")
        _no_actions(lambda: self.root.setOpacity_(0.0))

    def wiggle(self) -> None:
        _keys(self.body, "transform.rotation.z", [0, 0.12, -0.12, 0.1, -0.1, 0], 0.4, name="wiggle")
        _keys(self.body, "transform.scale.y", [1, 0.9, 1.06, 1], 0.4, name="squash")

    def open(self) -> None:
        self.opened += 1
        if self.opened > 1:
            return
        direction = getattr(self, "direction", -1)
        # The lid pops up and tips back, away from the critters' side.
        lifted = Quartz.CATransform3DMakeTranslation(-direction * 4, 13, 0)
        lifted = Quartz.CATransform3DRotate(lifted, -0.55 if direction < 0 else 0.55, 0, 0, 1)
        self.inside.setHidden_(False)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.22)
        Quartz.CATransaction.setAnimationTimingFunction_(_ease(Quartz.kCAMediaTimingFunctionEaseOut))
        self.lid.setTransform_(lifted)
        Quartz.CATransaction.commit()

    def close(self, force: bool = False) -> None:
        self.opened = 0 if force else max(0, self.opened - 1)
        if self.opened:
            return
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.3)
        Quartz.CATransaction.setAnimationTimingFunction_(_ease(Quartz.kCAMediaTimingFunctionEaseIn))
        self.lid.setTransform_(Quartz.CATransform3DIdentity)
        Quartz.CATransaction.commit()
        AppHelper.callLater(0.3, lambda: self.opened or (self.inside.setHidden_(True), self.wiggle()))

    def hit(self, x: float, y: float) -> bool:
        p = self.root.position()
        return self.root.opacity() > 0.5 and abs(x - p.x) < 19 and p.y - 2 < y < p.y + 36


# --- the stage window -----------------------------------------------------------------------

class MintCritterStageView(AppKit.NSView):
    owner = None

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if self.owner is not None:
            self.owner.clicked(point.x, point.y, event.clickCount())

    def rightMouseDown_(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if self.owner is not None:
            self.owner.right_clicked(point.x, point.y, event, self)


class MintCritterActions(AppKit.NSObject):
    owner = None

    def tick_(self, timer):
        if self.owner is not None:
            try:
                self.owner._tick()
            except Exception:
                log.debug("critter tick failed", exc_info=True)

    def openChat_(self, sender):
        if self.owner is not None:
            self.owner.open_chat()

    def stopAgent_(self, sender):
        if self.owner is not None:
            self.owner.stop_agent(str(sender.representedObject()))


class Stage:
    def __init__(self) -> None:
        self.hub = None
        self.origin = None             # callable -> (x, y) Cocoa global centre of Mint's orb, for tests
        self.window = None
        self.root = None
        self.toy = None
        self.critters: dict[str, Critter] = {}
        self.direction = -1            # critters line up to the left of the box (-1) or right (+1)
        self.box_x = STAGE_W - 40
        self._interactive = False
        self._anchor = None
        self._next_spawn = 0.0
        self._next_home = 0.0
        self._door_run = None          # the critter walking to the box door, if any
        self._hovered = None
        self._tagged = None            # the critter whose name tag is showing
        self._box_pokes: list[float] = []
        self._timer = None
        self._actions = None
        self._showing = False
        self.quiet_until = 0.0         # Mint is clicking: stay click-through until then
        self._show_token = None        # the show in progress, if any

    # --- setup (main thread) -----------------------------------------------------------

    def _build(self) -> None:
        if self.window is not None:
            return
        from .hud import _panel
        self.window = _panel(AppKit.NSMakeRect(0, 0, STAGE_W, STAGE_H), click_through=True)
        view = MintCritterStageView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, STAGE_W, STAGE_H))
        view.owner = self
        view.setWantsLayer_(True)
        self.window.setContentView_(view)
        self.root = view.layer()
        self.toy = ToyBox()
        self.toy.root.setOpacity_(0.0)
        self.root.addSublayer_(self.toy.root)
        self._actions = MintCritterActions.alloc().init()
        self._actions.owner = self
        self._timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 30, self._actions, "tick:", None, True)
        try:
            from . import sharing
            sharing.apply()
        except Exception:
            pass

    def _hud(self):
        try:
            from .emotes import emotes
            return emotes.hud
        except Exception:
            return None

    def _anchor_point(self):
        """(box x, floor y, direction) in global Cocoa points: beside the orb, or on the open chat."""
        hud = self._hud()
        centre = self.origin() if self.origin else (hud.orb_center() if hud is not None else None)
        if centre is None:
            return None
        screens = AppKit.NSScreen.screens()
        visible = (screens[0] if screens else AppKit.NSScreen.mainScreen()).visibleFrame()
        for screen in screens or []:
            if AppKit.NSPointInRect(centre, screen.frame()):
                visible = screen.visibleFrame()
        cx, cy = centre
        right = cx > visible.origin.x + visible.size.width / 2
        upper = cy > visible.origin.y + visible.size.height / 2
        direction = -1 if right else 1
        chat = getattr(hud, "chat", None) if hud is not None else None
        if chat is not None and chat.is_open and chat.window is not None:
            f = chat.window.frame()
            box_x = f.origin.x + f.size.width - 44 if right else f.origin.x + 44
            floor = f.origin.y + f.size.height - 1 if not upper else f.origin.y - 56
        else:
            box_x = cx - 48 if right else cx + 48
            floor = cy + 20 if not upper else cy - 84
        return box_x, floor, direction, visible

    def _place(self, animate: bool) -> None:
        anchor = self._anchor_point()
        if anchor is None:
            return
        box_x, floor, direction, visible = anchor
        key = (round(box_x), round(floor), direction)
        if key == self._anchor:
            return
        self._anchor = key
        local_box = STAGE_W - 40 if direction < 0 else 40
        x = box_x - local_box
        y = floor - FLOOR
        x = min(max(x, visible.origin.x - 20), visible.origin.x + visible.size.width - STAGE_W + 20)
        y = min(max(y, visible.origin.y - 8), visible.origin.y + visible.size.height - STAGE_H)
        local_box = box_x - x
        frame = AppKit.NSMakeRect(x, y, STAGE_W, STAGE_H)
        flipped = direction != self.direction or abs(local_box - self.box_x) > 1
        if animate and self._showing and not flipped:
            AppKit.NSAnimationContext.beginGrouping()
            AppKit.NSAnimationContext.currentContext().setDuration_(0.35)
            self.window.animator().setFrame_display_(frame, True)
            AppKit.NSAnimationContext.endGrouping()
        else:
            self.window.setFrame_display_(frame, True)
        if flipped:
            self.direction, self.box_x = direction, local_box
            self.toy.set_hinge(direction)
            _no_actions(lambda: self.toy.root.setPosition_(Quartz.CGPointMake(self.box_x, FLOOR)))
            self._layout(walk=False)

    def _slot_x(self, index: int) -> float:
        return self.box_x + self.direction * (FIRST + index * SLOT)

    def _staying(self):
        """Critters in their row, each helper right after the agent that called it in."""
        here = [c for c in self.critters.values() if c.state in ("waiting", "jumping", "out")]
        runs = {c.run for c in here}
        ordered: list = []

        def add(critter, depth=0):
            ordered.append(critter)
            if depth < 4:
                for child in here:
                    if child.parent == critter.run:
                        add(child, depth + 1)
        for critter in here:
            if not critter.parent or critter.parent not in runs:
                add(critter)
        return ordered + [c for c in here if c not in ordered]

    def _lead(self, critter):
        """The critter that called this one in, if it is out on the floor."""
        lead = self.critters.get(critter.parent) if critter.parent else None
        return lead if lead is not None and lead.state == "out" else None

    def _layout(self, walk: bool = True) -> None:
        staying = self._staying()
        crowd = len(staying) > MAX_SLOTS
        spacing = SLOT * (MAX_SLOTS / len(staying)) if crowd else SLOT
        for i, critter in enumerate(staying):
            x = self.box_x + self.direction * (FIRST + i * spacing)
            if abs(x - critter.x) < 0.5:
                continue
            critter.x = x
            if critter.state != "out":
                continue
            if walk:
                self._walk_to(critter, x, then=None)
            else:
                _no_actions(lambda c=critter, x=x: c.box.setPosition_(Quartz.CGPointMake(x, FLOOR)))

    # --- events (any thread) --------------------------------------------------------------

    def handle(self, event: dict) -> None:
        AppHelper.callAfter(self._handle_safe, event)

    def _handle_safe(self, event: dict) -> None:
        try:
            self._handle(event)
        except Exception:
            log.exception("critter event failed: %s", event.get("kind"))

    def _handle(self, event: dict) -> None:
        self._build()
        run, kind = event["run"], event["kind"]
        text = str(event.get("text", "") or "")
        critter = self.critters.get(run)
        if kind == "start":
            if critter is not None and critter.state in ("going",):
                # A Codex follow-up re-started the same run while it was going home: back out it comes.
                critter.token += 1
                critter.state = "gone"
                critter.box.removeFromSuperlayer()
                critter.hide_tag()
                critter = None
            if critter is None or critter.state == "gone":
                taken = {c.species for c in self.critters.values() if c.state != "gone"}
                critter = Critter(run, str(event.get("agent", "Agent")), _rgb(event.get("color")), taken)
                parent = event.get("parent")
                if parent and parent in self.critters and parent != run:
                    critter.parent = parent
                    critter.make_helper()
                critter.status = text
                self.critters[run] = critter
                self._spawn(critter)
            else:
                critter.status = text
            return
        if critter is None or critter.state in ("going", "gone"):
            return
        if text and kind in ("tool", "progress", "note", "steered", "answered", "file"):
            critter.status = {"file": f"saved {text}"}.get(kind, text)
        if critter.state != "out":
            critter.pending = event          # replayed when it lands
            return
        self._react(critter, event)

    def _react(self, critter: Critter, event: dict) -> None:
        kind = event["kind"]
        text = str(event.get("text", "") or "")
        if kind == "thinking":
            if critter.mood != "asking":
                critter.show_bubble("think")
        elif kind == "tool":
            critter.show_bubble("tool", _symbol_for(str(event.get("tool", ""))))
            if random.random() < 0.35:
                critter.hop(6, 0.35)
        elif kind == "progress":
            critter.show_bubble("tool", "sparkles")
            critter.hop(8, 0.4)
        elif kind == "note":
            critter.fidget()
        elif kind == "file":
            critter.show_bubble("tool", "doc.fill")
            critter.hop(12, 0.45)
            confetti(self.root, critter.x, FLOOR + 44, [gfx.light(critter.rgb), (1.0, 0.85, 0.3)],
                     names=("star.fill", "sparkle"), amount=0.5)
        elif kind == "ask":
            critter.status = f"asks: {text}" if text else "has a question"
            critter.set_mood("asking")
            critter.show_bubble("ask")
            critter.attention(True)
            critter.show_tag(self.root, "has a question · click me")
        elif kind in ("answered", "steered"):
            critter.attention(False)
            critter.set_mood("happy")
            critter.hop(12)
            critter.heart(self.root, 2)
            critter.show_bubble("think")
            critter.hide_tag()
            token = critter.token
            AppHelper.callLater(1.2, lambda: token == critter.token and critter.mood == "happy"
                                and critter.set_mood("normal"))
        elif kind in ("done", "failed", "stopped"):
            self._finish(critter, kind, text)

    # --- animation sequences (main thread) --------------------------------------------------

    def _show(self) -> None:
        if not self._showing:
            self._showing = True
            self._anchor = None
            self._place(animate=False)
            self.window.orderFrontRegardless()
            _no_actions(lambda: self.toy.root.setPosition_(Quartz.CGPointMake(self.box_x, FLOOR)))
            self.toy.appear()

    def _spawn(self, critter: Critter) -> None:
        was_showing = self._showing
        self._show()
        now = time.monotonic()
        start = max(now + (0.0 if was_showing else 0.55), self._next_spawn)
        self._next_spawn = start + 0.5
        self._layout()
        AppHelper.callLater(start - now, lambda: self._pop_out(critter))

    def _pop_out(self, critter: Critter) -> None:
        if self.critters.get(critter.run) is not critter or critter.state != "waiting":
            return
        critter.state = "jumping"
        token = critter.token
        toy = self.toy
        lead = self._lead(critter)
        if lead is not None:
            # Called in by another agent: it pops out of that critter, not the box.
            lead.wiggle()
            lead.show_tag(self.root, f"calling {critter.name} to help", seconds=1.8)
        else:
            toy.wiggle()
        root = self.root

        def launch():
            if token != critter.token:
                return
            if lead is not None:
                lead.squish()
                lead.flap_fast(0.4)
                origin = lead.box.position()
                start = Quartz.CGPointMake(origin.x, origin.y + 22)
                confetti(root, start.x, start.y + 10, [gfx.light(critter.rgb), gfx.light(lead.rgb)],
                         names=("sparkle",), amount=0.6)
                peak = FLOOR + 62
            else:
                toy.open()
                confetti(root, self.box_x, FLOOR + 28, [gfx.light(critter.rgb), (1.0, 0.85, 0.35), PINK],
                         names=("sparkle", "star.fill"), amount=0.8)
                start = Quartz.CGPointMake(self.box_x, FLOOR + 14)
                peak = FLOOR + 78
            end = Quartz.CGPointMake(critter.x, FLOOR)
            path = _path(("m", start.x, start.y), ("q", (start.x + end.x) / 2, peak, end.x, end.y))

            def place():
                critter.box.setPosition_(end)
                critter.box.setOpacity_(1.0)
            _no_actions(place)
            root.insertSublayer_below_(critter.box, toy.root)     # out from inside the box
            fly = Quartz.CAKeyframeAnimation.animationWithKeyPath_("position")
            fly.setPath_(path)
            fly.setDuration_(0.62)
            fly.setTimingFunction_(_ease())
            critter.box.addAnimation_forKey_(fly, "fly")
            _keys(critter.box, "transform.scale", [0.3, 1.1, 1.0], 0.62, [0, 0.5, 1], name="grow")
            _keys(critter.box, "transform.rotation.z", [0, self.direction * -2 * math.pi, self.direction * -2 * math.pi],
                  0.62, [0, 0.8, 1], name="twirl")
            critter.flap_fast(0.6)
            AppHelper.callLater(0.62, land)
            if lead is None:
                AppHelper.callLater(0.9, toy.close)

        def land():
            if token != critter.token:
                return
            critter.state = "out"
            critter.squish()
            dust = []
            for side in (-1, 1):
                puff = _shape(_ellipse(0, 0, 7, 5), WHITE, alpha=0.8, bounds=(0.01, 0.01))
                puff.setPosition_(Quartz.CGPointMake(critter.x + side * 12, FLOOR + 2))
                root.addSublayer_(puff)
                _keys(puff, "position.x", [critter.x + side * 12, critter.x + side * 24], 0.45, name="out")
                _keys(puff, "opacity", [0.8, 0.0], 0.45, name="fade")
                _keys(puff, "transform.scale", [0.6, 1.4], 0.45, name="grow")
                dust.append(puff)
            AppHelper.callLater(0.5, lambda: [d.removeFromSuperlayer() for d in dust])
            critter.set_mood("happy")
            critter.wiggle()
            critter.fidget()
            critter.show_tag(root, seconds=2.0)
            critter.bob(True)

            def settle():
                if token != critter.token or critter.state != "out":
                    return
                if critter.mood == "happy":
                    critter.set_mood("normal")
                pending = getattr(critter, "pending", None)
                critter.pending = None
                if pending is not None:
                    self._react(critter, pending)
                elif critter.bubble is None and not critter.performer:
                    critter.show_bubble("think")
            AppHelper.callLater(1.0, settle)
        AppHelper.callLater(0.3, launch)

    def _walk_to(self, critter: Critter, x: float, then=None, fast: bool = False) -> None:
        start = critter.box.presentationLayer().position().x if critter.box.presentationLayer() else critter.box.position().x
        distance = abs(x - start)
        if distance < 1:
            if then:
                then()
            return
        seconds = max(0.35, distance / (110 if fast else 55))
        token = critter.token
        critter.walk(True, fast)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(seconds)
        Quartz.CATransaction.setAnimationTimingFunction_(_ease(Quartz.kCAMediaTimingFunctionLinear))
        critter.box.setPosition_(Quartz.CGPointMake(x, FLOOR))
        Quartz.CATransaction.commit()

        def arrived():
            if token != critter.token:
                return
            critter.walk(False)
            if then:
                then()
        AppHelper.callLater(seconds, arrived)

    def _finish(self, critter: Critter, kind: str, text: str) -> None:
        critter.attention(False)
        critter.hide_tag()
        critter.bob(False)
        critter.token += 1
        token = critter.token
        root = self.root
        if kind == "done":
            critter.hide_bubble()
            critter.set_mood("happy")
            critter.flip(-self.direction)
            confetti(root, critter.x, FLOOR + 30, [gfx.light(critter.rgb), PINK, (1.0, 0.85, 0.35), (0.6, 0.9, 1.0)],
                     amount=1.3)
            critter.show_tag(root, "done!", seconds=1.6)
            wait, fast = 1.7, False
        elif kind == "home":                     # end of a show: just go back in
            critter.hide_bubble()
            critter.set_mood("happy")
            wait, fast = 0.1, False
        elif kind == "failed":
            critter.set_mood("sad")
            critter.show_bubble("tool", "cloud.rain.fill")
            AppHelper.callLater(0.3, lambda: critter.tear(root))
            _keys(critter.body, "transform.scale.y", [1, 0.9, 0.9], 0.5, name="droop")
            critter.show_tag(root, "couldn't finish", seconds=2.0)
            wait, fast = 2.3, False
        else:
            critter.set_mood("surprised")
            critter.show_bubble("wow")
            critter.hop(10, 0.35)
            wait, fast = 0.8, True

        def go_home():
            if token != critter.token:
                return
            critter.state = "going"
            critter.hide_bubble()
            critter.hide_tag()
            self._layout()
            if critter.mood == "happy":
                critter.set_mood("normal")
            lead = self._lead(critter)
            if lead is not None:
                into_lead(lead)                  # a helper goes back to the one that called it
                return
            walker = self.critters.get(self._door_run) if self._door_run else None
            if walker is not None and walker is not critter and walker.state == "going":
                hop_in()                         # someone is already at the box door: jump straight in
                return
            self._door_run = critter.run

            def at_door():
                if self._door_run == critter.run:
                    self._door_run = None
                hop_in()
            self._walk_to(critter, self.box_x + self.direction * 30, then=at_door, fast=fast)

        def into_lead(lead):
            here = critter.box.position()
            target = lead.box.position()
            end = Quartz.CGPointMake(target.x, target.y + 20)
            path = _path(("m", here.x, here.y), ("q", (here.x + end.x) / 2, FLOOR + 56, end.x, end.y))
            _no_actions(lambda: (critter.box.setPosition_(end), critter.box.setOpacity_(0.0)))
            fly = Quartz.CAKeyframeAnimation.animationWithKeyPath_("position")
            fly.setPath_(path)
            fly.setDuration_(0.5)
            critter.box.addAnimation_forKey_(fly, "fly")
            _keys(critter.box, "transform.scale", [1.0, 1.0, 0.3], 0.5, [0, 0.5, 1], name="shrink")
            _keys(critter.box, "opacity", [1.0, 1.0, 0.0], 0.5, [0, 0.8, 1], name="fade")

            def caught():
                if lead.state == "out":
                    lead.squish()
                    lead.heart(root, 1)
                vanish()
            AppHelper.callLater(0.5, caught)

        def vanish():
            if token != critter.token:
                return
            critter.state = "gone"
            critter.box.removeFromSuperlayer()
            if self.critters.get(critter.run) is critter:
                self.critters.pop(critter.run, None)
            self._layout()
            if not self.critters:
                AppHelper.callLater(0.9, self._maybe_hide)

        def hop_in():
            if token != critter.token:
                return
            now = time.monotonic()
            turn = max(now, self._next_home)     # one at a time into the box
            self._next_home = turn + 0.45
            if turn > now + 0.01:
                AppHelper.callLater(turn - now, enter)
            else:
                enter()

        def enter():
            if token != critter.token:
                return
            self.toy.open()
            here = critter.box.position()
            path = _path(("m", here.x, here.y), ("q", (here.x + self.box_x) / 2, FLOOR + 50, self.box_x, FLOOR + 16))
            _no_actions(lambda: (critter.box.setPosition_(Quartz.CGPointMake(self.box_x, FLOOR + 16)),
                                 critter.box.setOpacity_(0.0)))
            fly = Quartz.CAKeyframeAnimation.animationWithKeyPath_("position")
            fly.setPath_(path)
            fly.setDuration_(0.5)
            critter.box.addAnimation_forKey_(fly, "fly")
            _keys(critter.box, "transform.scale", [1.0, 1.0, 0.3], 0.5, [0, 0.5, 1], name="shrink")
            _keys(critter.box, "opacity", [1.0, 1.0, 0.0], 0.5, [0, 0.8, 1], name="fade")
            AppHelper.callLater(0.5, gone)

        def gone():
            if token != critter.token:
                return
            critter.state = "gone"
            critter.box.removeFromSuperlayer()
            if self.critters.get(critter.run) is critter:
                self.critters.pop(critter.run, None)
            self.toy.close()
            confetti(root, self.box_x, FLOOR + 26, [gfx.light(critter.rgb)], names=("sparkle",), amount=0.4)
            if not self.critters:
                AppHelper.callLater(0.9, self._maybe_hide)
        AppHelper.callLater(wait, go_home)

    def _maybe_hide(self) -> None:
        if self.critters or not self._showing:
            return
        self.toy.vanish()
        self.toy.close(force=True)

        def hide():
            if not self.critters:
                self._showing = False
                self.window.orderOut_(None)
                self._set_interactive(False)
        AppHelper.callLater(0.45, hide)

    # --- the mouse (main thread) -----------------------------------------------------------

    def _set_interactive(self, want: bool) -> None:
        if want != self._interactive and self.window is not None:
            self._interactive = want
            self.window.setIgnoresMouseEvents_(not want)

    def _tick(self) -> None:
        if not self._showing:
            return
        self._place(animate=True)
        frame = self.window.frame()
        mouse = AppKit.NSEvent.mouseLocation()
        lx, ly = mouse.x - frame.origin.x, mouse.y - frame.origin.y
        over = None
        for critter in self.critters.values():
            if critter.state == "out" and critter.hit(lx, ly):
                over = critter
        on_box = over is None and self.toy.hit(lx, ly)
        hud = self._hud()
        chat = getattr(hud, "chat", None) if hud is not None else None
        if chat is not None and chat.is_open and chat.window is not None and AppKit.NSPointInRect(mouse, chat.window.frame()):
            over, on_box = None, False          # the chat is on top there: its clicks are its own
        # Mint's own clicks are real mouse events: while it works (a tool is
        # running, or a click was just aimed), the critters never catch them.
        busy = (hud is not None and getattr(hud, "_activity", None) is not None) or time.monotonic() < self.quiet_until
        self._set_interactive((over is not None or on_box) and not busy)

        if over is not self._hovered:
            previous, self._hovered = self._hovered, over
            if previous is not None and previous.mood != "asking":
                previous.hide_tag()
            if over is not None:
                over.show_tag(self.root, _short(over.status) or "working")
                over.hop(5, 0.3)
        now = time.monotonic()
        tagged = self._tagged
        if tagged is not None and tagged.tag is not None and tagged.box.presentationLayer() is not None:
            x = tagged.box.presentationLayer().position().x
            _no_actions(lambda: tagged.tag.setPosition_(Quartz.CGPointMake(x, tagged.tag.position().y)))
        if self._tagged is None:
            asking = next((c for c in self.critters.values() if c.state == "out" and c.mood == "asking"), None)
            if asking is not None:
                asking.show_tag(self.root, "has a question · click me")
        for critter in self.critters.values():
            if critter.state != "out":
                continue
            p = critter.box.position()
            critter.look(lx - p.x, ly - (p.y + 22))
            if now > critter.next_blink:
                critter.next_blink = now + random.uniform(2.0, 5.0)
                if critter.mood in ("normal", "asking", "sad"):
                    critter.blink()
            if now > critter.next_fidget:
                critter.next_fidget = now + random.uniform(4.0, 10.0)
                if random.random() < 0.2 and self._show_token is None:
                    critter.signature(self.root, self.direction)
                else:
                    critter.fidget()

    def clicked(self, x: float, y: float, count: int) -> None:
        for critter in self.critters.values():
            if critter.state == "out" and critter.hit(x, y):
                self._poke(critter, count)
                return
        if self.toy.hit(x, y):
            self._poke_box()

    def _poke(self, critter: Critter, count: int) -> None:
        now = time.monotonic()
        critter.pokes = [t for t in critter.pokes if now - t < 2.0] + [now]
        if critter.mood == "asking" or count >= 2:
            critter.hop(10)
            critter.show_tag(self.root, _short(critter.status, 60) or "opening the chat")
            self.open_chat()
            return
        if len(critter.pokes) >= 5:
            critter.pokes = []
            critter.set_mood("dizzy")
            critter.spin(2, 0.9)
            critter.show_tag(self.root, "whoa, dizzy...", seconds=1.8)
            token = critter.token
            AppHelper.callLater(1.8, lambda: token == critter.token and critter.mood == "dizzy"
                                and critter.set_mood("normal"))
            return
        reaction = random.choice(["heart", "spin", "giggle", "signature"])
        critter.set_mood("happy")
        if reaction == "signature":
            caption = critter.signature(self.root, self.direction)
            critter.show_tag(self.root, caption, seconds=2.0)
            token = critter.token
            AppHelper.callLater(1.1, lambda: token == critter.token and critter.mood == "happy"
                                and critter.set_mood("normal"))
            return
        if reaction == "heart":
            critter.hop(14)
            critter.heart(self.root, 2)
        elif reaction == "spin":
            critter.spin(1, 0.55)
            critter.hop(8)
        else:
            critter.wiggle()
            critter.fidget()
            float_up(self.root, critter.x + 12, FLOOR + 38, "music.note", gfx.light(critter.rgb), drift=8)
        critter.show_tag(self.root, _short(critter.status, 60) or "working on it", seconds=3.0)
        token = critter.token
        AppHelper.callLater(1.1, lambda: token == critter.token and critter.mood == "happy"
                            and critter.set_mood("normal"))

    def _poke_box(self) -> None:
        self.toy.wiggle()
        out = [c for c in self.critters.values() if c.state == "out"]
        for i, critter in enumerate(out):
            critter.hop(12, 0.45, delay=0.1 * i)
            AppHelper.callLater(0.1 * i, critter.fidget)
        if out:
            names = ", ".join(c.name for c in out[:4])
            float_up(self.root, self.box_x, FLOOR + 34, "heart.fill", PINK)
            if out[0].tag is None:
                out[0].show_tag(self.root, f"{len(out)} helper{'s' if len(out) != 1 else ''}: {names}", seconds=2.5)

    def right_clicked(self, x: float, y: float, event, view) -> None:
        critter = next((c for c in self.critters.values() if c.state == "out" and c.hit(x, y)), None)
        menu = AppKit.NSMenu.alloc().initWithTitle_("Helpers")
        item = menu.addItemWithTitle_action_keyEquivalent_("Open chat", "openChat:", "")
        item.setTarget_(self._actions)
        targets = [critter] if critter is not None else [c for c in self.critters.values() if c.state == "out"]
        for c in targets:
            stop = menu.addItemWithTitle_action_keyEquivalent_(f"Stop {c.name}", "stopAgent:", "")
            stop.setTarget_(self._actions)
            stop.setRepresentedObject_(c.run)
        AppKit.NSMenu.popUpContextMenu_withEvent_forView_(menu, event, view)

    def open_chat(self) -> None:
        hud = self._hud()
        if hud is not None:
            hud.show_chat(True)

    def stop_agent(self, run: str) -> None:
        if self.hub is not None:
            import threading
            threading.Thread(target=self.hub.cancel, args=(run,), daemon=True).start()

    # --- the show: the whole family performs --------------------------------------------------

    TROUPE = [("Hoot", "owl", "#8B7CFF"), ("Bun", "bun", "#2EC4B6"), ("Kit", "kit", "#FFB547"),
              ("Pip", "chick", "#FFD166"), ("Ember", "drake", "#FF7A6B"), ("Mochi", "cat", "#FF6FB5"),
              ("Sprout", "sprout", "#10A37F"), ("Teddy", "bear", "#C08457")]

    def perform(self) -> str:
        """Every critter species pops out of the box and puts on a little show, then goes home."""
        if self._show_token is not None:
            return "The critters are already putting on a show."
        AppHelper.callAfter(self._perform_safe)
        return ("Showtime: the critter family pops out of the toy box for a stadium wave, a solo each, a dance "
                "and a bow, then hops back in. About 20 seconds.")

    def _perform_safe(self) -> None:
        try:
            self._perform()
        except Exception:
            log.exception("critter show failed")
            self._show_token = None

    def _perform(self) -> None:
        self._build()
        token = object()
        self._show_token = token
        out = [c for c in self.critters.values() if c.state in ("waiting", "jumping", "out")]
        taken = {c.species for c in out}
        room = max(3, MAX_SLOTS - len(out))
        cast = []
        for name, species, color in self.TROUPE:
            if species in taken or len(cast) >= min(6, room):
                continue
            run = f"show-{species}-{int(time.time() * 1000) % 100000}"
            critter = Critter(run, name, _rgb(color), species=species)
            critter.performer = True
            critter.status = "putting on a show"
            self.critters[run] = critter
            cast.append(critter)
            self._spawn(critter)
        print(f"  [critter show: {', '.join(c.name for c in cast)}]", flush=True)
        # When the last one has landed, the acts begin.
        ready = max(0.0, self._next_spawn - time.monotonic()) + 1.3

        def alive():
            return self._show_token is token

        def everyone():
            return [c for c in self.critters.values() if c.state == "out"]

        def wave(rounds=2):
            troupe = everyone()
            for r in range(rounds):
                for i, c in enumerate(troupe):
                    AppHelper.callLater(r * (0.14 * len(troupe) + 0.25) + 0.14 * i,
                                        lambda c=c: c.state == "out" and (c.set_mood("happy"), c.hop(16, 0.4)))
            return rounds * (0.14 * len(troupe) + 0.25) + 0.5

        def solos():
            troupe = everyone()
            for i, c in enumerate(troupe):
                def solo(c=c):
                    if not alive() or c.state != "out":
                        return
                    c.set_mood("happy")
                    caption = c.signature(self.root, self.direction)
                    c.show_tag(self.root, caption, seconds=0.95)
                AppHelper.callLater(i * 1.05, solo)
            return len(troupe) * 1.05 + 0.3

        def dance(beats=6):
            troupe = everyone()
            for b in range(beats):
                def beat(b=b):
                    if not alive():
                        return
                    self.toy.wiggle()
                    for i, c in enumerate(everyone()):
                        if (i + b) % 2 == 0:
                            c.hop(14, 0.4)
                        else:
                            c.spin(1, 0.4)
                        if b % 2 == 0 and i % 2 == 0:
                            float_up(self.root, c.box.position().x + 10, FLOOR + 42,
                                     random.choice(["music.note", "music.quarternote.3"]), gfx.light(c.rgb),
                                     drift=random.uniform(-8, 8))
                AppHelper.callLater(b * 0.46, beat)
            for c in troupe:
                c.bob(True, fast=True)
            return beats * 0.46 + 0.3

        def finale():
            troupe = everyone()
            for i, c in enumerate(troupe):
                c.bob(False)
                c.set_mood("happy")
                c.hop(30, 0.6, delay=0.05 * i)
            mid = troupe[len(troupe) // 2] if troupe else None
            confetti(self.root, (troupe[0].x + troupe[-1].x) / 2 if troupe else self.box_x, FLOOR + 40,
                     [PINK, (1.0, 0.85, 0.35), (0.6, 0.85, 1.0), (0.6, 1.0, 0.8), (0.8, 0.7, 1.0)], amount=2.2)

            def bow():
                for c in everyone():
                    _keys(c.body, "transform.rotation.z", [0, 0.28 * self.direction, 0.28 * self.direction, 0],
                          0.9, [0, 0.3, 0.7, 1], additive=True, name="bow")
                    _keys(c.body, "transform.scale.y", [1, 0.86, 0.86, 1], 0.9, [0, 0.3, 0.7, 1], name="bowy")
                if mid is not None:
                    mid.show_tag(self.root, "thank you!", seconds=1.6)
                try:
                    from .emotes import emotes
                    emotes.play("clap")          # Mint applauds its family
                except Exception:
                    pass
            AppHelper.callLater(0.75, bow)
            return 2.2

        def curtain():
            if not alive():
                return
            performers = [c for c in self.critters.values() if c.performer and c.state == "out"]
            for i, c in enumerate(performers):
                AppHelper.callLater(i * 0.2, lambda c=c: c.state == "out" and self._finish(c, "home", ""))
            for c in [c for c in self.critters.values() if not c.performer and c.state == "out"]:
                c.set_mood("normal")
                if c.bubble is None:
                    c.show_bubble("think")
            AppHelper.callLater(len(performers) * 0.2 + 4.0, lambda: alive() and setattr(self, "_show_token", None))

        def run_acts():
            if not alive():
                return
            t = 0.0
            for act in (wave, solos, dance, finale):
                AppHelper.callLater(t, lambda act=act: alive() and act())
                # Durations are known only roughly up front: estimate from the cast.
                n = max(1, len([c for c in self.critters.values() if c.state in ("waiting", "jumping", "out")]))
                t += {wave: 2 * (0.14 * n + 0.25) + 0.5, solos: n * 1.05 + 0.3, dance: 6 * 0.46 + 0.3,
                      finale: 2.2}[act]
            AppHelper.callLater(t, curtain)
        AppHelper.callLater(ready, run_acts)

    # --- for testing: a fake agent's whole life ------------------------------------------------

    def demo(self, name: str = "Astra", color: str = "#8B7CFF", run: str | None = None,
             outcome: str = "done", step: float = 1.6) -> str:
        run = run or f"demo-{name.lower()}-{int(time.time() * 1000) % 100000}"
        events = [("start", "research the best laptops", {}), ("thinking", "", {}),
                  ("tool", "searching the web", {"tool": "web_search"}),
                  ("tool", "reading a page", {"tool": "fetch_url"}), ("ask", "Budget under $1500?", {}),
                  ("answered", "yes", {}), ("tool", "writing the brief", {"tool": "write_file"}),
                  ("file", "brief.md", {}), (outcome, "brief ready", {})]
        for i, (kind, text, extra) in enumerate(events):
            event = {"kind": kind, "run": run, "agent": name, "color": color, "text": text, **extra}
            AppHelper.callLater(i * step, lambda e=event: self._handle_safe(e))
        return run


def _short(text: str, limit: int = 44) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


stage = Stage()


def enabled() -> bool:
    return prefs.get("cute_agents") is not False


def attach(hub) -> None:
    stage.hub = hub
    hub.on(stage.handle)
    try:
        from .effects import fx
        if not getattr(fx, "_critters_quiet", False):
            fx._critters_quiet = True
            click = fx.click

            def quiet_click(x, y, label=""):
                stage.quiet_until = time.monotonic() + 1.5     # before the click lands, on its thread
                return click(x, y, label)
            fx.click = quiet_click
    except Exception:
        log.debug("could not watch Mint's clicks", exc_info=True)
