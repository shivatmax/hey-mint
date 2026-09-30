"""The orb moves like a little creature: when told to, when it points at
something, and - now and then - on its own, doing a small trick.

Every move is a path flown at 60 frames a second, not a straight slide:
curves and hops, speeding up and slowing down, leaning into the direction it
travels, stretching when fast, squashing and wobbling when it lands, eyes
looking where it is going.

* nudge  - "go a little down", "move up a lot", "you're covering that, move":
           a short swoop that overshoots a touch and settles (the spot is saved).
* visit  - when Mint marks something on screen, the orb swoops over beside it,
           looks at it, and swoops home a few seconds later.
* circle - once round the front window: slow into the corners, fast along the
           edges, easing in and out.
* tricks - hops, a loop-the-loop, a figure eight, a bouncing ball, a bumblebee
           zigzag, chasing a sparkle, a quick peek, a spinning hop. On its own,
           only while nobody is using the computer and nothing is going on,
           about once in ten minutes ("wander" setting); or on request.

Moves the HUD's orb window (found through emotes, which the HUD attaches at
build). Public methods are safe from any thread.
"""

from __future__ import annotations

import logging
import math
import random
import time

import AppKit
import Quartz
from PyObjCTools import AppHelper

from mint.core import prefs

log = logging.getLogger("mint.ui.motion")

AMOUNTS = {"little": 70, "bit": 70, "slightly": 70, "small": 70, "medium": 160, "more": 160,
           "lot": 320, "far": 320, "much": 320}
WANDER_CHECK = 45.0        # seconds between chances to wander
WANDER_CHANCE = 0.08       # per check: about once every ten minutes
IDLE_BEFORE_WANDER = 8.0   # the user must have been still this long
FPS = 60.0
TRICKS = ["hops", "loop", "figure8", "bounce", "bee", "chase", "peek", "spin"]


def _hud():
    from mint.ui.emotes import emotes
    return emotes.hud


def _visible():
    screens = AppKit.NSScreen.screens()
    return (screens[0] if screens else AppKit.NSScreen.mainScreen()).visibleFrame()


def _user_idle() -> float:
    try:
        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType)
    except Exception:
        return 0.0


def _emote(name: str) -> None:
    try:
        from mint.ui.emotes import emotes
        emotes.play(name)
    except Exception:
        log.debug("emote failed", exc_info=True)


# --- timing: t (0..1 of the time) -> u (0..1 of the way) ------------------------------------

def linear(t):
    return t


def in_out(t):
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def out_cubic(t):
    return 1 - (1 - t) ** 3


def in_quad(t):
    return t * t


def out_quad(t):
    return 1 - (1 - t) ** 2


def out_back(t, s=1.5):
    """Arrives, overshoots a little, settles."""
    return 1 + (s + 1) * (t - 1) ** 3 + s * (t - 1) ** 2


def wave(k=2.0, a=0.55):
    """Eased in and out, with k surges of speed on the way (fast, slow, fast...)."""
    def ease(t):
        e = in_out(t)
        return e + a * math.sin(2 * math.pi * k * e) / (2 * math.pi * k)
    return ease


# --- paths: u (0..1) -> (x, y), Cocoa points ------------------------------------------------

def _lerp(a, b, u):
    return a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u


def line(a, b):
    return lambda u: _lerp(a, b, u)


def quad(a, c, b):
    def p(u):
        v = 1 - u
        return (v * v * a[0] + 2 * v * u * c[0] + u * u * b[0], v * v * a[1] + 2 * v * u * c[1] + u * u * b[1])
    return p


def cubic(a, c1, c2, b):
    def p(u):
        v = 1 - u
        return (v ** 3 * a[0] + 3 * v * v * u * c1[0] + 3 * v * u * u * c2[0] + u ** 3 * b[0],
                v ** 3 * a[1] + 3 * v * v * u * c1[1] + 3 * v * u * u * c2[1] + u ** 3 * b[1])
    return p


def hop(a, b, height):
    def p(u):
        x, y = _lerp(a, b, u)
        return x, y + 4 * height * u * (1 - u)
    return p


def loop(centre, radius, start_angle, turns=1.0):
    def p(u):
        angle = start_angle + 2 * math.pi * turns * u
        return centre[0] + radius * math.cos(angle), centre[1] + radius * math.sin(angle)
    return p


def figure8(centre, rx, ry, side=1):
    def p(u):
        angle = 2 * math.pi * u
        return centre[0] + side * rx * math.sin(angle), centre[1] + ry * math.sin(angle) * math.cos(angle)
    return p


def wavy(a, b, amp, waves):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length, dx / length

    def p(u):
        x, y = _lerp(a, b, u)
        off = amp * math.sin(2 * math.pi * waves * u) * math.sin(math.pi * u)
        return x + nx * off, y + ny * off
    return p


def hold(point):
    return lambda u: point


def timed_polyline(points, corner_speed=0.45):
    """A path through `points` where u is TIME: slower where it turns, faster on straights."""
    segs = []
    for i in range(len(points) - 1):
        (ax, ay), (bx, by) = points[i], points[i + 1]
        length = math.hypot(bx - ax, by - ay)
        turn = 0.0
        if i > 0:
            (px, py) = points[i - 1]
            a1 = math.atan2(ay - py, ax - px)
            a2 = math.atan2(by - ay, bx - ax)
            turn = abs((a2 - a1 + math.pi) % (2 * math.pi) - math.pi)
        speed = corner_speed if turn > 0.05 else 1.0
        segs.append((length / speed, points[i], points[i + 1]))
    total = sum(s[0] for s in segs) or 1.0
    table, acc = [], 0.0
    for dt, a, b in segs:
        table.append((acc / total, (acc + dt) / total, a, b))
        acc += dt

    def p(u):
        for t0, t1, a, b in table:
            if u <= t1:
                return _lerp(a, b, 0.0 if t1 == t0 else (u - t0) / (t1 - t0))
        return table[-1][3]
    return p


class Leg:
    """One piece of a flight."""

    def __init__(self, path, seconds, ease=in_out, land=0.0, spin=0.0, on_end=None, gaze=None,
                 stretch=True, scale=None, free=False):
        self.path, self.seconds, self.ease = path, max(0.05, seconds), ease
        self.land, self.spin, self.on_end, self.gaze, self.stretch = land, spin, on_end, gaze, stretch
        self.scale = scale        # (from, to): the orb grows or shrinks along the leg
        self.free = free          # may leave the visible frame (flying into the notch, over the menu bar)


class MintMotionTicker(AppKit.NSObject):
    def tick_(self, timer):
        try:
            motion._frame()
        except Exception:
            log.exception("motion frame failed")
            motion._stop()


class Motion:
    def __init__(self) -> None:
        self.gaze = None            # (x, y) Cocoa point the eyes look at, until gaze_until
        self.gaze_until = 0.0
        self._away = False          # visiting or doing a trick: not at home
        self._previous = None       # where the orb was before the last commanded move
        self._token = 0
        self._started = False
        # the flight in progress
        self._legs: list[Leg] = []
        self._leg = 0
        self._leg_start = 0.0
        self._then = None
        self._save = False
        self._last = None
        self._frames = 0
        self._timer = None
        self._ticker = None
        self._squash, self._squash_v = 0.0, 0.0
        self._spin_base = 0.0
        self._size = 1.0            # the orb's drawn size (shrinks flying into the notch)

    # --- geometry (main thread) ---------------------------------------------------------

    def _center(self):
        hud = _hud()
        if hud is None:
            return None
        frame = hud._orb_window.frame()
        return frame.origin.x + frame.size.width / 2, frame.origin.y + frame.size.height / 2

    def _clamp(self, cx, cy):
        v = _visible()
        return (min(max(cx, v.origin.x + 30), v.origin.x + v.size.width - 30),
                min(max(cy, v.origin.y + 30), v.origin.y + v.size.height - 30))

    def _inward(self, point):
        """(dx, dy) unit-ish signs pointing from `point` toward the middle of the screen."""
        v = _visible()
        return (1 if point[0] < v.origin.x + v.size.width / 2 else -1,
                1 if point[1] < v.origin.y + v.size.height / 2 else -1)

    def _place(self, x, y) -> None:
        window = _hud()._orb_window
        size = window.frame().size
        window.setFrameOrigin_(AppKit.NSMakePoint(x - size.width / 2, y - size.height / 2))

    # --- the flight driver (main thread) ---------------------------------------------------

    def _fly(self, legs, then=None, save=False) -> int:
        """Start a flight (replacing any in progress). Returns its token."""
        self._token += 1
        self._legs, self._leg, self._then, self._save = list(legs), 0, then, save
        self._leg_start = time.monotonic()
        self._last = self._center()
        self._spin_base = 0.0
        if self._timer is None:
            if self._ticker is None:
                self._ticker = MintMotionTicker.alloc().init()
            self._timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                1 / FPS, self._ticker, "tick:", None, True)
            AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, AppKit.NSRunLoopCommonModes)
        return self._token

    def _stop(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        self._legs = []
        self._squash = self._squash_v = 0.0
        self._size = 1.0
        self._deform(0, 0, 0)

    def _cancel(self) -> None:
        """The user grabbed the orb (or a new command came): drop the flight where it is."""
        self._token += 1
        self._away = False
        self._stop()

    def _frame(self) -> None:
        hud = _hud()
        if hud is None:
            self._stop()
            return
        if getattr(hud, "_interactive", False) and AppKit.NSEvent.pressedMouseButtons() & 1:
            self._cancel()                       # being dragged: the hand wins
            return
        now = time.monotonic()
        self._frames += 1
        # The landing jelly: a damped spring on the squash.
        self._squash_v += -0.2 * self._squash - 0.16 * self._squash_v
        self._squash += self._squash_v
        if not self._legs:
            self._deform(0, 0, 0)
            if abs(self._squash) < 0.004 and abs(self._squash_v) < 0.004:
                self._stop()
            return
        leg = self._legs[self._leg]
        t = min(1.0, (now - self._leg_start) / leg.seconds)
        u = leg.ease(t)
        x, y = leg.path(u) if leg.free else self._clamp(*leg.path(u))
        if leg.scale is not None:
            a, b = leg.scale
            self._size = a + (b - a) * u
        self._place(x, y)
        lx, ly = self._last or (x, y)
        vx, vy = (x - lx) * FPS, (y - ly) * FPS
        self._last = (x, y)
        spin = self._spin_base + 2 * math.pi * leg.spin * u
        self._deform(vx if leg.stretch else 0, vy if leg.stretch else 0, spin)
        if leg.gaze is not None:
            self.gaze, self.gaze_until = leg.gaze, now + 0.3
        elif math.hypot(vx, vy) > 60:
            self.gaze, self.gaze_until = (x + vx * 0.35, y + vy * 0.35), now + 0.3
        if self._frames % 3 == 0:
            try:
                hud._orb_moved(final=False)     # the bubble and chat follow
            except Exception:
                pass
        if t >= 1.0:
            self._spin_base = spin % (2 * math.pi)
            if leg.land:
                self._squash, self._squash_v = leg.land, 0.0
            if leg.on_end:
                try:
                    leg.on_end()
                except Exception:
                    log.debug("leg callback failed", exc_info=True)
            self._leg += 1
            self._leg_start = now
            if self._leg >= len(self._legs):
                self._legs = []
                self._spin_base = 0.0
                then, save = self._then, self._save
                self._then = None
                try:
                    hud._orb_moved(final=save)
                except Exception:
                    log.debug("orb move bookkeeping failed", exc_info=True)
                if then:
                    then()

    def _deform(self, vx, vy, spin) -> None:
        """Lean into the motion, stretch along it when fast, squash on landing, spin."""
        hud = _hud()
        if hud is None:
            return
        layer = hud._orb_window.contentView().layer()
        if layer is None:
            return
        speed = math.hypot(vx, vy)
        k = min(0.2, speed / 3000.0)
        lean = max(-0.28, min(0.28, -vx / 1800.0))
        s = max(-0.35, min(0.35, self._squash))
        size = getattr(self, "_size", 1.0)
        if k < 0.002 and abs(lean) < 0.002 and abs(s) < 0.002 and abs(spin) < 0.002 and abs(size - 1) < 0.002:
            transform = Quartz.CATransform3DIdentity
        else:
            b = layer.bounds().size
            anchor = layer.anchorPoint()
            px, py = b.width / 2 - anchor.x * b.width, b.height / 2 - anchor.y * b.height
            theta = math.atan2(vy, vx)
            t = Quartz.CATransform3DMakeTranslation(px, py, 0)
            t = Quartz.CATransform3DScale(t, size, size, 1)
            t = Quartz.CATransform3DRotate(t, lean + spin, 0, 0, 1)
            t = Quartz.CATransform3DRotate(t, theta, 0, 0, 1)
            t = Quartz.CATransform3DScale(t, 1 + k, 1 - 0.6 * k, 1)
            t = Quartz.CATransform3DRotate(t, -theta, 0, 0, 1)
            t = Quartz.CATransform3DTranslate(t, 0, -20, 0)          # squash from the bottom of the orb
            t = Quartz.CATransform3DScale(t, 1 + s, 1 - s, 1)
            t = Quartz.CATransform3DTranslate(t, -px, -py + 20, 0)
            transform = t
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        layer.setSublayerTransform_(transform)
        Quartz.CATransaction.commit()

    # --- told to move ---------------------------------------------------------------------

    def nudge(self, direction: str, amount: str = "little") -> str:
        direction = direction.strip().lower()
        step = next((v for k, v in AMOUNTS.items() if k in amount.lower()), 70)
        if _hud() is None:
            return "The orb is not on screen (no HUD), so it cannot move."
        known = ("up", "down", "left", "right", "away", "aside", "out of the way", "center", "centre",
                 "middle", "back", "undo", "previous")
        if direction not in known:
            return (f"Unknown direction '{direction}'. Use up, down, left, right, away, or centre; for "
                    "a corner use set_preference position.")

        def go():
            center = self._center()
            if center is None:
                return
            self._away = False
            cx, cy = center
            if direction in ("back", "undo", "previous"):
                if self._previous is None:
                    return
                target, self._previous = self._previous, None
            else:
                self._previous = (cx, cy)             # so "go back" can undo this move
                v = _visible()
                if direction in ("up", "down", "left", "right"):
                    dx = {"left": -step, "right": step}.get(direction, 0)
                    dy = {"up": step, "down": -step}.get(direction, 0)
                    target = (cx + dx, cy + dy)
                elif direction in ("away", "aside", "out of the way"):
                    upper = cy > v.origin.y + v.size.height / 2
                    target = (cx, v.origin.y + 60 if upper else v.origin.y + v.size.height - 60)
                else:
                    target = (v.origin.x + v.size.width / 2, v.origin.y + v.size.height / 2)
            target = self._clamp(*target)
            self._fly([self._swoop((cx, cy), target)], save=True)
        AppHelper.callAfter(go)
        return f"Moved {direction}" + (f" by about {step} points" if direction in ("up", "down", "left", "right") else "") + "."

    def _swoop(self, a, b, seconds=None, land=0.2):
        """A short curved hop from a to b that overshoots a touch and settles."""
        distance = math.hypot(b[0] - a[0], b[1] - a[1])
        mid = _lerp(a, b, 0.5)
        dx, dy = b[0] - a[0], b[1] - a[1]
        bulge = min(40.0, 12 + distance * 0.12)
        # Bow the path sideways (upwards for horizontal moves): a hop, not a slide.
        nx, ny = (-dy, dx) if abs(dx) < abs(dy) else (0, abs(dx))
        n = math.hypot(nx, ny) or 1.0
        control = (mid[0] + nx / n * bulge, mid[1] + ny / n * bulge)
        return Leg(quad(a, control, b), seconds or min(1.1, 0.45 + distance / 900), ease=out_back, land=land)

    # --- pointing -------------------------------------------------------------------------

    def visit_quartz(self, box, stay: float = 7.0) -> None:
        """Swoop beside a Quartz rect, look at it, come home after `stay` seconds."""
        primary = AppKit.NSScreen.screens()[0].frame().size.height
        x, y, w, h = box
        target = (x + w / 2, primary - (y + h / 2))
        AppHelper.callAfter(self._visit, target, stay)

    def _visit(self, target, stay) -> None:
        center = self._center()
        if center is None:
            return
        home = self._home()
        tx, ty = target
        v = _visible()
        # Beside the mark, never on top of it: to the right if there is room.
        side = 1 if tx + 150 < v.origin.x + v.size.width else -1
        spot = self._clamp(tx + side * 110, ty + 60)
        self._away = True
        leg = self._swoop(center, spot, seconds=0.8, land=0.15)
        leg.gaze = target
        token = self._fly([leg])
        self.gaze, self.gaze_until = target, time.monotonic() + stay + 1.0

        def back():
            if token != self._token:
                return
            self._away = False
            self._fly([self._swoop(self._center() or spot, home, seconds=0.9, land=0.22)])
        AppHelper.callLater(0.8 + stay, back)

    def _home(self):
        hud = _hud()
        try:
            return hud._home_center()
        except Exception:
            return self._center()

    def look(self):
        """For emotes' tick: where the eyes should look, if anywhere special."""
        if self.gaze is not None and time.monotonic() < self.gaze_until:
            return self.gaze
        return None

    # --- tricks ---------------------------------------------------------------------------

    def _trick_legs(self, name: str, start):
        """The legs of a trick that starts and ends at `start`."""
        ix, iy = self._inward(start)
        sx, sy = start
        if name == "hops":
            legs, at = [], start
            for i in range(3):
                nxt = (at[0] + ix * 44, at[1])
                legs.append(Leg(hop(at, nxt, 26), 0.36, ease=linear, land=0.24))
                at = nxt
            legs.append(Leg(hold(at), 0.5, on_end=lambda: _emote(random.choice(["smile", "wave", "wink"]))))
            legs.append(Leg(hop(at, start, 64), 0.85, ease=in_out, land=0.3))
            return legs
        if name == "loop":
            top = (sx + ix * 70, sy + iy * 110)
            r = 42
            centre = (top[0], top[1] - iy * r)
            return [Leg(quad(start, (sx + ix * 10, sy + iy * 110), top), 0.6, ease=in_out),
                    Leg(loop(centre, r, math.pi / 2 if iy > 0 else -math.pi / 2, turns=-ix * iy), 1.2,
                        ease=in_out),
                    Leg(quad(top, (sx + ix * 90, sy + iy * 20), start), 0.7, ease=in_out, land=0.3,
                        on_end=lambda: _emote(random.choice(["dizzy", "laugh"])))]
        if name == "figure8":
            centre = (sx + ix * 90, sy + iy * 90)
            return [Leg(quad(start, (sx, sy + iy * 90), centre), 0.55, ease=in_out),
                    Leg(figure8(centre, 75, 48, side=ix), 2.4, ease=wave(2, 0.5)),
                    Leg(quad(centre, (centre[0], sy), start), 0.6, ease=in_out, land=0.28,
                        on_end=lambda: _emote("smile"))]
        if name == "bounce":
            legs = []
            for height, up, down, land in ((140, 0.46, 0.42, 0.34), (55, 0.28, 0.26, 0.24), (18, 0.15, 0.14, 0.14)):
                peak = (sx, sy + iy * height)
                legs.append(Leg(line(start, peak), up, ease=out_quad))
                legs.append(Leg(line(peak, start), down, ease=in_quad, land=land))
            legs[-1].on_end = lambda: _emote("laugh")
            return legs
        if name == "bee":
            target = (sx + ix * random.uniform(200, 280), sy + iy * random.uniform(40, 130))
            return [Leg(wavy(start, target, 20, 3), 2.0, ease=in_out),
                    Leg(hold(target), 0.9, gaze=(target[0] + ix * 200, target[1]),
                        on_end=lambda: _emote("thinking")),
                    Leg(wavy(target, start, 16, 2.5), 1.8, ease=in_out, land=0.24)]
        if name == "chase":
            star = (sx + ix * random.uniform(160, 240), sy + iy * random.uniform(60, 150))
            return [Leg(hold(start), 0.02, on_end=lambda: _sparkle(star, 1.6)),
                    Leg(hold(start), 0.45, gaze=star, stretch=False),
                    Leg(cubic(start, (sx - ix * 30, sy + iy * 40), (star[0] - ix * 60, star[1] + 60), star),
                        0.6, ease=in_quad, land=0.26, on_end=lambda: _caught(star)),
                    Leg(hold(star), 0.5),
                    Leg(hop(star, start, 50), 0.9, ease=in_out, land=0.3)]
        if name == "peek":
            spot = (sx + ix * 150, sy + iy * 70)
            return [Leg(quad(start, (sx + ix * 40, sy + iy * 110), spot), 0.45, ease=out_back),
                    Leg(hold(spot), 0.5, gaze=(spot[0] + ix * 300, spot[1])),
                    Leg(hold(spot), 0.5, gaze=(spot[0] - ix * 300, spot[1] + 40),
                        on_end=lambda: _emote("surprised")),
                    Leg(wavy(spot, start, 8, 2), 1.5, ease=in_out, land=0.22)]
        # spin: a hop in place with a full turn, then a little one the other way
        return [Leg(hop(start, start, 46), 0.62, ease=linear, spin=1.0, land=0.3),
                Leg(hop(start, start, 18), 0.36, ease=linear, spin=-1.0, land=0.18,
                    on_end=lambda: _emote("wink"))]

    def trick(self, name: str = "", force: bool = True) -> str:
        """Do one trick (a random one if `name` is empty) and come home."""
        name = (name or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")
        name = {"figureeight": "figure8", "8": "figure8", "zigzag": "bee", "bumblebee": "bee",
                "hop": "hops", "jump": "hops", "loopdeloop": "loop", "looptheloop": "loop", "ball": "bounce",
                "sparkle": "chase", "spinning": "spin", "twirl": "spin"}.get(name, name)
        if name and name not in TRICKS:
            return f"Unknown trick '{name}'. Tricks: {', '.join(TRICKS)}."

        def go():
            hud = _hud()
            if hud is None or (self._away and not force):
                return
            if not force:
                busy = getattr(hud, "_activity", None) is not None or getattr(hud, "_state", "") in (
                    "working", "thinking", "speaking", "paused", "offline", "starting")
                chat_open = getattr(getattr(hud, "chat", None), "is_open", False)
                if busy or chat_open or _user_idle() < IDLE_BEFORE_WANDER:
                    return
            start = self._home() if not self._away else self._center()
            current = self._center()
            if current is None:
                return
            chosen = name or random.choice(TRICKS)
            legs = self._trick_legs(chosen, start)
            if math.hypot(current[0] - start[0], current[1] - start[1]) > 4:
                legs.insert(0, self._swoop(current, start))
            self._away = True
            print(f"  [orb does a trick: {chosen}]", flush=True)
            token = self._fly(legs)

            def home():
                if token == self._token:
                    self._away = False
            self._then = home
        AppHelper.callAfter(go)
        return f"Doing a {name or 'little'} trick; back home in a few seconds."

    def tricks(self, names) -> str:
        """Several tricks back to back in one go ("do all your tricks"); names or ["all"]."""
        wanted = []
        for raw in names or []:
            key = str(raw).strip().lower()
            if key in ("all", "every", "everything", "*"):
                wanted += TRICKS
                continue
            key = key.replace(" ", "").replace("-", "").replace("_", "")
            key = {"figureeight": "figure8", "zigzag": "bee", "bumblebee": "bee", "hop": "hops",
                   "jump": "hops", "ball": "bounce", "twirl": "spin"}.get(key, key)
            if key in TRICKS:
                wanted.append(key)
        if not wanted:
            return f"No known tricks in {names}. Tricks: {', '.join(TRICKS)} (or 'all')."

        def go():
            if _hud() is None:
                return
            start = self._home()
            current = self._center() or start
            legs = []
            if math.hypot(current[0] - start[0], current[1] - start[1]) > 4:
                legs.append(self._swoop(current, start))
            for i, name in enumerate(wanted):
                legs += self._trick_legs(name, start)
                if i < len(wanted) - 1:
                    legs.append(Leg(hold(start), 0.45))          # a breath between tricks
            self._away = True
            print(f"  [orb does tricks: {', '.join(wanted)}]", flush=True)
            token = self._fly(legs)

            def home():
                if token == self._token:
                    self._away = False
            self._then = home
        AppHelper.callAfter(go)
        seconds = sum({"hops": 3.5, "loop": 2.6, "figure8": 3.6, "bounce": 1.8, "bee": 4.8, "chase": 2.5,
                       "peek": 3.0, "spin": 1.0}[n] + 0.45 for n in wanted)
        return (f"Doing {len(wanted)} tricks back to back: {', '.join(wanted)} - about {round(seconds)} "
                "seconds, all in this one call; it comes home at the end.")

    # --- wandering ------------------------------------------------------------------------

    def start(self) -> None:
        if not self._started:
            self._started = True
            AppHelper.callLater(WANDER_CHECK, self._maybe_wander)

    def _maybe_wander(self) -> None:
        AppHelper.callLater(WANDER_CHECK, self._maybe_wander)
        if prefs.get("wander") is False or random.random() > WANDER_CHANCE:
            return
        self.wander(force=False)

    def wander(self, force: bool = True) -> str:
        """A random little trick near home. `force` skips the idle checks."""
        return self.trick("", force=force)

    # --- circling the window --------------------------------------------------------------

    def circle(self, seconds: float = 5.0) -> str:
        """Once round the front window - slow in the corners, fast on the edges - then home."""
        def go():
            hud = _hud()
            if hud is None:
                return
            rect = _front_window_cocoa()
            if rect is None:
                v = _visible()
                rect = (v.origin.x + 80, v.origin.y + 80, v.size.width - 160, v.size.height - 160)
            points = [self._clamp(*p) for p in _circle_path(rect, points=160)]
            perimeter = sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))
            duration = max(seconds, min(9.0, perimeter / 700.0))     # big windows: take longer, not faster
            home = self._home()
            current = self._center() or home
            self._away = True
            print("  [orb circles the window]", flush=True)
            legs = [self._swoop(current, points[0], seconds=0.8, land=0.0),
                    Leg(timed_polyline(points), duration, ease=in_out),
                    Leg(hold(points[-1]), 0.1, land=0.2,
                        on_end=lambda: _emote("dance" if random.random() < 0.5 else "wink")),
                    Leg(hold(points[-1]), 1.1),
                    self._swoop(points[-1], home, seconds=1.0, land=0.25)]
            token = self._fly(legs)

            def done():
                if token == self._token:
                    self._away = False
            self._then = done
        AppHelper.callAfter(go)
        return "Circling around the window now; back home in a few seconds."


def _front_window_cocoa():
    """The front app's window as a Cocoa rect (x, y, w, h), or None."""
    try:
        from mint.screen import ocr
        w = ocr._front_window()
    except Exception:
        w = None
    if not w:
        return None
    primary = AppKit.NSScreen.screens()[0].frame().size.height
    return (w["x"], primary - w["y"] - w["h"], w["w"], w["h"])


def _circle_path(rect, margin: float = 26.0, points: int = 110, radius: float = 40.0):
    """Evenly spaced points once round the outside of a rect (a rounded rectangle),
    clockwise from the top-right corner."""
    x, y, w, h = rect
    x, y, w, h = x - margin, y - margin, w + 2 * margin, h + 2 * margin
    r = min(radius, w / 2, h / 2)
    outline = []
    corners = [((x + w - r, y + h - r), 0.0),      # top right, angle 0 -> 90
               ((x + r, y + h - r), 90.0),         # top left, 90 -> 180
               ((x + r, y + r), 180.0),            # bottom left
               ((x + w - r, y + r), 270.0)]        # bottom right
    for (cx, cy), a0 in corners:
        for k in range(9):
            a = math.radians(a0 + 90 * k / 8)
            outline.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    outline.append(outline[0])
    lengths = [math.dist(outline[i], outline[i + 1]) for i in range(len(outline) - 1)]
    total = sum(lengths)
    out, walked, seg = [], 0.0, 0
    for n in range(points + 1):
        target = total * n / points
        while seg < len(lengths) - 1 and walked + lengths[seg] < target:
            walked += lengths[seg]
            seg += 1
        t = 0.0 if lengths[seg] == 0 else (target - walked) / lengths[seg]
        (ax, ay), (bx, by) = outline[seg], outline[seg + 1]
        out.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    return out


# --- a sparkle to chase (drawn on the effects overlay) -----------------------------------------

_star = None


def _overlay_at(point):
    from mint.ui.effects import fx
    if not fx.built or not fx._overlays:
        return None
    for frame, _, root, _ in fx._overlays:
        if AppKit.NSPointInRect(point, frame):
            return root, (point[0] - frame.origin.x, point[1] - frame.origin.y)
    return None


def _sparkle(point, seconds) -> None:
    global _star
    found = _overlay_at(point)
    if found is None:
        return
    root, (x, y) = found
    try:
        from mint.ui.critters import _keys, _symbol_layer
        star = _symbol_layer("star.fill", 16, (1.0, 0.85, 0.35), outline=True)
    except Exception:
        return
    star.setPosition_(Quartz.CGPointMake(x, y))
    root.addSublayer_(star)
    _keys(star, "transform.rotation.z", [0, 2 * math.pi], 1.6, repeat=float("inf"), name="spin")
    _keys(star, "transform.scale", [0.2, 1.3, 1.0], 0.4, name="pop")
    _keys(star, "position.y", [y, y + 5, y], 0.8, repeat=float("inf"), name="bob")
    _star = star
    AppHelper.callLater(seconds + 0.8, star.removeFromSuperlayer)


def _caught(point) -> None:
    global _star
    if _star is not None:
        _star.removeFromSuperlayer()
        _star = None
    try:
        from mint.ui.emotes import emotes
        orb = emotes.orb
        orb.burst((1.0, 0.85, 0.35), stars=True, amount=1.3)
    except Exception:
        pass
    _emote("laugh")


motion = Motion()
