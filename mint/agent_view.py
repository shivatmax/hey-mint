"""Sub-agents on screen: small moons in each agent's colour, circling Mint's orb.

The user asked for animations on the Mint icon only - minimal and cute, not
popping out across the screen. So an agent is a 9-point dot on a ring just
outside Mint's orb: it grows out of the orb when the agent starts, breathes
while it works, swells and pulses with a white rim while it waits for your
answer, gives one soft pulse when it finishes (a small shake if it failed), and
sinks back into the orb. The ring turns slowly while any agent works. Words
(what it is doing, its result) go to the chat, not onto the screen.

Drawn on the effects overlay (click-through, out of screenshots unless
MINT_CAPTURE=1). Events come from the agent hub on any thread; drawing happens
on the main thread.
"""

from __future__ import annotations

import math

import AppKit
import Quartz
from PyObjCTools import AppHelper

from . import gfx
from .effects import fx

MOON = 9           # agent dot diameter
ORBIT = 31         # distance from Mint's centre (its orb is 44 across)


def _rgb(event_or_color) -> tuple:
    value = str(event_or_color or "#7FD1FF").lstrip("#")
    try:
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (0.5, 0.82, 1.0)


def _ease(name=Quartz.kCAMediaTimingFunctionEaseInEaseOut):
    return Quartz.CAMediaTimingFunction.functionWithName_(name)


class _Moon:
    def __init__(self, rgb: tuple) -> None:
        dot = Quartz.CALayer.layer()
        dot.setBounds_(Quartz.CGRectMake(0, 0, MOON, MOON))
        dot.setCornerRadius_(MOON / 2)
        dot.setBackgroundColor_(gfx.cg(rgb))
        dot.setBorderColor_(gfx.cg((1, 1, 1), 0.55))
        dot.setBorderWidth_(0.7)
        dot.setShadowColor_(gfx.cg(rgb))
        dot.setShadowOpacity_(0.9)
        dot.setShadowRadius_(3)
        dot.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        self.layer = dot
        self.leaving = False

    def breathe(self, on: bool) -> None:
        self.layer.removeAnimationForKey_("breathe")
        if on:
            b = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            b.setFromValue_(1.0)
            b.setToValue_(0.55)
            b.setDuration_(0.9)
            b.setAutoreverses_(True)
            b.setRepeatCount_(float("inf"))
            b.setTimingFunction_(_ease())
            self.layer.addAnimation_forKey_(b, "breathe")

    def asking(self, on: bool) -> None:
        self.layer.removeAnimationForKey_("ask")
        self.layer.setBorderColor_(gfx.cg((1, 1, 1), 1.0 if on else 0.55))
        self.layer.setBorderWidth_(1.4 if on else 0.7)
        if on:
            self.breathe(False)
            pulse = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
            pulse.setFromValue_(1.0)
            pulse.setToValue_(1.6)
            pulse.setDuration_(0.6)
            pulse.setAutoreverses_(True)
            pulse.setRepeatCount_(float("inf"))
            pulse.setTimingFunction_(_ease())
            self.layer.addAnimation_forKey_(pulse, "ask")

    def blip(self, size: float = 1.35) -> None:
        b = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
        b.setValues_([1.0, size, 1.0])
        b.setDuration_(0.35)
        b.setTimingFunction_(_ease())
        self.layer.addAnimation_forKey_(b, "blip")


class AgentView:
    def __init__(self) -> None:
        self.moons: dict[str, _Moon] = {}      # run id -> moon, in start order
        self.ring = None                       # container layer centred on Mint's orb
        self.ring_root = None

    # --- geometry (main thread) --------------------------------------------------------

    def _overlay(self):
        """(root layer, mint orb centre in overlay coordinates)."""
        if not fx.built or not fx._overlays:
            return None
        origin = fx.origin() if fx.origin else None
        for frame, _, root, _ in fx._overlays:
            if origin is None or AppKit.NSPointInRect(origin, frame):
                centre = ((origin[0] - frame.origin.x, origin[1] - frame.origin.y) if origin
                          else (frame.size.width - 60, 80))
                return root, centre
        frame, _, root, _ = fx._overlays[0]
        return root, (frame.size.width - 60, 80)

    def _ring_at(self, root, centre):
        """The turning ring the moons sit on, following Mint's orb (it can be dragged)."""
        if self.ring is None or self.ring_root is not root:
            if self.ring is not None:
                self.ring.removeFromSuperlayer()
            ring = Quartz.CALayer.layer()
            ring.setBounds_(Quartz.CGRectMake(0, 0, 2 * ORBIT, 2 * ORBIT))
            root.addSublayer_(ring)
            self.ring, self.ring_root = ring, root
            for moon in self.moons.values():
                ring.addSublayer_(moon.layer)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.ring.setPosition_(Quartz.CGPointMake(*centre))
        Quartz.CATransaction.commit()
        return self.ring

    def _layout(self) -> None:
        """Spread the moons evenly round the ring; the ring turns while any works."""
        staying = [m for m in self.moons.values() if not m.leaving]
        for i, moon in enumerate(staying):
            angle = math.pi / 2 + 2 * math.pi * i / max(1, len(staying))
            moon.layer.setPosition_(Quartz.CGPointMake(ORBIT + ORBIT * math.cos(angle),
                                                       ORBIT + ORBIT * math.sin(angle)))
        if self.ring is None:
            return
        if staying and self.ring.animationForKey_("turn") is None:
            turn = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
            turn.setFromValue_(0.0)
            turn.setToValue_(-2 * math.pi)
            turn.setDuration_(14.0)
            turn.setRepeatCount_(float("inf"))
            self.ring.addAnimation_forKey_(turn, "turn")
        elif not staying:
            self.ring.removeAnimationForKey_("turn")

    # --- events (any thread) ---------------------------------------------------------------

    def handle(self, event: dict) -> None:
        AppHelper.callAfter(self._handle, event)

    def _handle(self, event: dict) -> None:
        where = self._overlay()
        if where is None:
            return
        root, centre = where
        ring = self._ring_at(root, centre)
        run, kind = event["run"], event["kind"]
        moon = self.moons.get(run)
        if kind == "start" and (moon is None or moon.leaving):
            if moon is not None:
                moon.layer.removeFromSuperlayer()
            self._spawn(run, _rgb(event.get("color")), ring)
            return
        if moon is None or moon.leaving:
            return
        if kind in ("thinking", "tool", "progress", "note"):
            moon.breathe(True)
        elif kind == "file":
            moon.blip(1.25)
        elif kind == "ask":
            moon.asking(True)
        elif kind in ("answered", "steered"):
            moon.asking(False)
            moon.blip()
            moon.breathe(True)
        elif kind in ("done", "failed", "stopped"):
            self._finish(run, moon, kind)

    # --- animation pieces (main thread) ----------------------------------------------------

    def _spawn(self, run, rgb, ring) -> None:
        moon = _Moon(rgb)
        self.moons[run] = moon
        # Out of the orb's centre to its place on the ring, with one small pop.
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        moon.layer.setPosition_(Quartz.CGPointMake(ORBIT, ORBIT))
        ring.addSublayer_(moon.layer)
        Quartz.CATransaction.commit()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.45)
        Quartz.CATransaction.setAnimationTimingFunction_(_ease(Quartz.kCAMediaTimingFunctionEaseOut))
        self._layout()
        Quartz.CATransaction.commit()
        grow = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
        grow.setValues_([0.1, 1.2, 1.0])
        grow.setKeyTimes_([0, 0.7, 1.0])
        grow.setDuration_(0.45)
        moon.layer.addAnimation_forKey_(grow, "grow")
        moon.breathe(True)

    def _finish(self, run, moon, kind) -> None:
        moon.asking(False)
        moon.breathe(False)
        if kind == "done":
            moon.blip(1.5)
        else:
            shake = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.translation.x")
            shake.setValues_([0, -2.5, 2.5, -2, 2, 0])
            shake.setDuration_(0.35)
            shake.setAdditive_(True)
            moon.layer.addAnimation_forKey_(shake, "shake")

        def sink():
            if self.moons.get(run) is not moon:
                return                      # started again meanwhile (a Codex follow-up)
            moon.leaving = True
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setAnimationDuration_(0.5)
            Quartz.CATransaction.setAnimationTimingFunction_(_ease(Quartz.kCAMediaTimingFunctionEaseIn))
            moon.layer.setPosition_(Quartz.CGPointMake(ORBIT, ORBIT))
            moon.layer.setOpacity_(0.0)
            moon.layer.setTransform_(Quartz.CATransform3DMakeScale(0.2, 0.2, 1))
            self._layout()
            Quartz.CATransaction.commit()
            AppHelper.callLater(0.55, lambda: self._remove(run, moon))
        AppHelper.callLater(1.2 if kind == "done" else 2.0, sink)

    def _remove(self, run, moon) -> None:
        if self.moons.get(run) is moon:
            self.moons.pop(run, None)
        moon.layer.removeFromSuperlayer()
        self._layout()


view = AgentView()


def chat_rows(event: dict) -> None:
    """Agent events as lines in Mint's chat."""
    from . import ground
    chat = ground.OWN_CHAT
    if chat is None:
        return
    name, kind, text = event["agent"], event["kind"], str(event.get("text", ""))
    key = f"agent:{event['run']}"
    if kind == "start":
        chat.action(key, f"{name} started: {text[:90]}")
    elif kind == "ask":
        chat.note(f"{name} asks: {text[:160]}")
    elif kind == "steered":
        chat.note(f"→ {name}: {text[:120]}")
    elif kind == "file":
        chat.note(f"{name} saved {text}")
    elif kind == "done":
        chat.action_done(key, True, f"{name} finished: {text[:160]}")
    elif kind in ("failed", "stopped"):
        chat.action_done(key, False, f"{name} {kind}: {text[:120]}")


def attach(hub) -> None:
    # Cute critters popping out of a toy box beside the orb (critters.py);
    # the "cute_agents" setting off brings back these small moons.
    try:
        from . import critters
        if critters.enabled():
            critters.attach(hub)
        else:
            hub.on(view.handle)
    except Exception:
        import logging
        logging.getLogger("mint.agents").exception("critters failed; using moons")
        hub.on(view.handle)
    hub.on(chat_rows)
