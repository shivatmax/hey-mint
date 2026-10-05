"""Mint on screen: a tiny orb, a word bubble, and a chat that opens on click.

* The orb (orb.py) is always there - a small Siri-like swirl with a face,
  dimmed and dozing while asleep. Click it to open the chat, drag it anywhere,
  right-click it for settings. It is click-through everywhere but its own
  circle.
* The bubble beside it shows what is being said, word by word, and what Mint
  is doing. It fades when things go quiet. Always click-through.
* The chat (chat.py) is closed by default: history, a box to type in, stop,
  mic, voice and appearance.

Keyboard focus is the one hard rule: Mint types into whatever app is in
front, so nothing here holds the keyboard except the open chat, and the chat
hands it back before Mint clicks or types elsewhere.

All public methods are safe from any thread.
"""

from __future__ import annotations

import math
import re
import time

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import activity
from mint.ui import effects
from mint.ui import gfx
from mint.ui import look
from mint.core import prefs
from mint.ui.chat import ChatPanel
from mint.ui.chat import H as CHAT_H
from mint.ui.chat import W as CHAT_W
from mint.ui.orb import Orb

S = 124            # orb window size
D = 44             # orb diameter
EDGE = 46          # orb centre distance from the screen edges
BUBBLE_MAX = 290
IDLE_HIDE = 6.0    # the bubble fades after this long with nothing new
WORD_IN = 0.34     # a caption word takes this long to rise into place
STAGGER = 0.055

TITLES = {
    "starting": "Starting…", "sleeping": "Asleep", "awake": "Listening…",
    "thinking": "Thinking…", "working": "Working…", "speaking": "Speaking",
    "paused": "Mic off", "offline": "Disconnected",
}
SCREEN_KINDS = {"click", "type", "scroll", "open", "web", "switch"}


def _ease(name=Quartz.kCAMediaTimingFunctionEaseInEaseOut):
    return Quartz.CAMediaTimingFunction.functionWithName_(name)


def _visible_frame():
    screens = AppKit.NSScreen.screens()
    return (screens[0] if screens else AppKit.NSScreen.mainScreen()).visibleFrame()


# --- captions, word by word ------------------------------------------------------------

class _Words:
    """A caption as a list of words, each with the moment it appears."""

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.words: list[list] = []
        self._joined = True
        self._last = 0.0

    def add(self, chunk: str, now: float) -> int:
        added = 0
        for token in re.findall(r"\s+|\S+", chunk):
            if token.isspace():
                self._joined = False
                continue
            if self.words and self._joined:
                self.words[-1][0] += token
            else:
                at = max(now, min(self._last + STAGGER, now + 0.5))
                self.words.append([token, at])
                self._last = at
                added += 1
            self._joined = True
        if len(self.words) > 240:
            self.words = self.words[-160:]
        return added

    def tail(self, limit: int) -> list:
        out, total = [], 0
        for word in reversed(self.words):
            total += len(word[0]) + 1
            if total > limit and out:
                break
            out.append(word)
        out.reverse()
        return out


# --- windows ---------------------------------------------------------------------------

class _Panel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class _OrbView(AppKit.NSView):
    """Click to open the chat, drag to move, right-click for settings."""

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        self.start = AppKit.NSEvent.mouseLocation()
        self.origin = self.window().frame().origin
        self.dragged = False

    def mouseDragged_(self, event):
        now = AppKit.NSEvent.mouseLocation()
        dx, dy = now.x - self.start.x, now.y - self.start.y
        if not self.dragged and math.hypot(dx, dy) < 4:
            return
        if not self.dragged:                    # held above the notch (and its drop zone), not under them
            self.window().setLevel_(ORB_HELD_LEVEL)
        self.dragged = True
        self.window().setFrameOrigin_(AppKit.NSMakePoint(self.origin.x + dx, self.origin.y + dy))
        self.owner._orb_moved(final=False)
        self.owner._dock_hint()

    def mouseUp_(self, event):
        if getattr(self, "dragged", False):
            self.owner._orb_moved(final=True)
            self.owner._maybe_dock()
            window = self.window()
            AppHelper.callLater(1.2, lambda: window.setLevel_(AppKit.NSStatusWindowLevel))
        else:
            self.owner._orb_clicked()

    def rightMouseDown_(self, event):
        self.owner._orb_menu(self)


ORB_HELD_LEVEL = Quartz.CGWindowLevelForKey(Quartz.kCGMainMenuWindowLevelKey) + 6   # over the notch (main menu + 3)


class _Ticker(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(_Ticker, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def tick_(self, timer):
        self.owner._tick()


def _panel(frame, click_through=True):
    style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
    panel = _Panel.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, style, AppKit.NSBackingStoreBuffered, False)
    panel.setLevel_(AppKit.NSStatusWindowLevel)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(AppKit.NSColor.clearColor())
    panel.setHasShadow_(False)
    panel.setIgnoresMouseEvents_(click_through)
    panel.setHidesOnDeactivate_(False)
    panel.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        | AppKit.NSWindowCollectionBehaviorStationary
        | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
    # Out of screenshots, so Mint never reads its own captions off the screen.
    panel.setSharingType_(effects.SHARING)
    look.follow_system(panel)          # light or dark with the system, like any Apple panel
    return panel


class HUD:
    def __init__(self) -> None:
        self._state = "starting"
        self._built = False
        self._level_target = 0.0
        self._level = 0.0
        self._last_activity = 0.0
        self._you = _Words()
        self._said = _Words()
        self._status = ""
        self._activity: dict | None = None
        self._bubble_visible = False
        self._bubble_dirty = False
        self._bubble_moving = False
        self._look_at = None
        self._interactive = False
        self._action_seq = 0
        self.hidden = False                  # "Hide" (notch row, menu, ⌃⌥H): nothing of Mint shows until it is needed
        self.fire = None                     # set by Presence: fire(action, *args)
        self.menu_factory = None             # set by Presence: -> NSMenu

    # --- construction (main thread) ---------------------------------------------

    def build(self) -> None:
        center = self._home_center()
        self._orb_window = _panel(AppKit.NSMakeRect(center[0] - S / 2, center[1] - S / 2, S, S),
                                  click_through=True)
        view = _OrbView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, S, S))
        view.owner = self
        view.setWantsLayer_(True)
        self._orb_window.setContentView_(view)
        self.orb = Orb(view.layer(), (S / 2, S / 2), D)
        self._orb_window.orderFrontRegardless()

        self._bubble = _panel(AppKit.NSMakeRect(0, 0, 120, 36))
        self._bubble.setHasShadow_(True)
        # System glass, rounded by a mask image (a layer cornerRadius left a square
        # patch of blur round the card), light or dark with the system.
        blur = look.glass(AppKit.NSMakeRect(0, 0, 120, 36), radius=16)
        self._bubble.setContentView_(blur)
        self._bubble_label = AppKit.NSTextField.wrappingLabelWithString_("")
        self._bubble_label.setFrame_(AppKit.NSMakeRect(12, 8, 96, 20))
        blur.addSubview_(self._bubble_label)
        self._bubble.setAlphaValue_(0.0)

        self.chat = ChatPanel(self._fire, lambda: self.menu_factory())
        self.chat.build()

        self._built = True
        self.orb.apply_state(self._state, bool(prefs.get("voice")))
        self._ticker = _Ticker.alloc().initWithOwner_(self)
        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 30, self._ticker, "tick:", None, True)
        effects.fx.origin = self.orb_center
        effects.fx.on_target = self._glance
        AppHelper.callLater(0.3, self.orb.boot)
        # Notch mode (notch.py): the orb moves into the camera notch, like a Dynamic Island.
        # Before the expressions attach, since they draw on whichever orb is self.orb.
        try:
            from mint.ui import notch
            notch.install(self)
        except ImportError:
            pass
        except Exception:
            import logging
            logging.getLogger("mint.ui.hud").exception("notch mode failed")
        # Extension point: expressions (emotes.py) attach here, if present.
        try:
            from mint.ui import emotes
            emotes.attach(self)
        except ImportError:
            pass
        except Exception:
            import logging
            logging.getLogger("mint.ui.hud").exception("emotes failed to attach")
        # The island: the orb turns into a recorder, a schedule card, a lesson... (island.py).
        try:
            from mint.ui import island
            island.attach(self)
        except ImportError:
            pass
        # While Mint learns a task by watching: a mint ring on the pointer, click ripples (teach_fx.py).
        try:
            from mint.ui import teach_fx
            teach_fx.attach(self)
        except ImportError:
            pass

    # --- geometry -----------------------------------------------------------------------

    def _home_center(self):
        screen = _visible_frame()
        left, bottom = screen.origin.x, screen.origin.y
        right, top = left + screen.size.width, bottom + screen.size.height
        position = prefs.get("position")
        origin = prefs.get("origin")
        if position == "custom" and isinstance(origin, list) and len(origin) == 2:
            return (min(max(origin[0], left + 24), right - 24), min(max(origin[1], bottom + 24), top - 24))
        x = {"top-left": left + EDGE, "bottom-left": left + EDGE,
             "top-center": left + screen.size.width / 2}.get(position, right - EDGE)
        y = bottom + EDGE if str(position).startswith("bottom") else top - EDGE
        return (x, y)

    def orb_center(self):
        if not self._built:
            return None
        frame = self._orb_window.frame()
        return (frame.origin.x + S / 2, frame.origin.y + S / 2)

    def _chat_frame(self):
        cx, cy = self.orb_center()
        screen = _visible_frame()
        on_right = cx > screen.origin.x + screen.size.width / 2
        upper = cy > screen.origin.y + screen.size.height / 2
        x = cx + 30 - CHAT_W if on_right else cx - 30
        y = cy - 36 - CHAT_H if upper else cy + 36
        x = min(max(x, screen.origin.x + 6), screen.origin.x + screen.size.width - CHAT_W - 6)
        y = min(max(y, screen.origin.y + 6), screen.origin.y + screen.size.height - CHAT_H - 6)
        return AppKit.NSMakeRect(x, y, CHAT_W, CHAT_H)

    def _place_bubble(self, width, height) -> None:
        cx, cy = self.orb_center()
        screen = _visible_frame()
        on_right = cx > screen.origin.x + screen.size.width / 2
        x = cx - 34 - width if on_right else cx + 34
        y = cy - height / 2
        y = min(max(y, screen.origin.y + 4), screen.origin.y + screen.size.height - height - 4)
        self._bubble.setFrame_display_(AppKit.NSMakeRect(x, y, width, height), True)
        self._bubble.invalidateShadow()          # the shadow follows the rounded glass, not a square

    DOCK_RADIUS = 190.0                 # let go this close to the notch and Mint flies in (a magnet)

    def _notch_distance(self):
        """How far the orb is from the notch (None when docking doesn't apply: already in it, or in notch mode)."""
        try:
            from mint.ui import notch
            if notch.enabled() or notch.active():
                return None
            _, cx, top, _, nh, _ = notch.geometry()
            ox, oy = self.orb_center()
            return math.hypot(ox - cx, oy - (top - nh / 2))
        except Exception:
            return None

    def _near_notch(self) -> bool:
        dist = self._notch_distance()
        return dist is not None and dist < self.DOCK_RADIUS

    def _squeeze(self, scale: float, animated: bool = False) -> None:
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(not animated)
        if animated:
            Quartz.CATransaction.setAnimationDuration_(0.2)
        self.orb.body.setValue_forKeyPath_(scale, "transform.scale")
        Quartz.CATransaction.commit()

    def _dock_hint(self) -> None:
        """Dragging the orb toward the notch: the closer it gets, the more the notch swells open (a drop
        target) and the smaller the orb gets, as if it were being drawn in."""
        from mint.ui import notch
        dist = self._notch_distance()
        near = dist is not None and dist < self.DOCK_RADIUS
        t = 0.0 if not near else max(0.0, min(1.0, (self.DOCK_RADIUS - dist) / (self.DOCK_RADIUS - 40.0)))
        t = round(t * 4) / 4                       # in quarter steps: the notch's own spring does the in-between
        if near and not getattr(self, "_docking", False):
            self.orb.hop()
        if (near, t) != getattr(self, "_dock_state", (False, 0.0)):
            self._dock_state = (near, t)
            self._squeeze(1.0 - 0.38 * t, animated=True)
            notch.notch.dock_zone(near, t)
        self._docking = near

    def _maybe_dock(self) -> None:
        from mint.ui import notch
        self._docking = False
        self._dock_state = (False, 0.0)
        if not self._near_notch():
            self._squeeze(1.0, animated=True)
            notch.notch.dock_zone(False)
            return
        # Let go over the notch: the orb jiggles like jelly while it is drawn up to the notch, the notch
        # bulges and settles, then the flight into the notch takes over.
        body = self.orb.body
        jiggle = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
        jiggle.setValues_([0.62, 0.34, 0.74, 0.44, 0.62, 0.5, 0.56])
        jiggle.setDuration_(0.5)
        body.addAnimation_forKey_(jiggle, "dock-jiggle")
        _, cx, top, _, nh, _ = notch.geometry()
        window = self._orb_window
        size = window.frame().size
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.45)
        window.animator().setFrameOrigin_(AppKit.NSMakePoint(cx - size.width / 2, top - nh - size.height * 0.5))
        AppKit.NSAnimationContext.endGrouping()
        notch.notch.dock_jiggle()

        def fly():
            self._squeeze(1.0)
            body.removeAnimationForKey_("dock-jiggle")
            prefs.set("notch_mode", True)        # the settings switch plays the flight into the notch
        AppHelper.callLater(0.5, fly)

    def _orb_moved(self, final: bool) -> None:
        if self.chat.is_open:
            self.chat.move(self._chat_frame())
        if self._bubble_visible:
            self._bubble_dirty = True
        if final:
            cx, cy = self.orb_center()
            prefs.set("origin", [round(cx), round(cy)])
            prefs.set("position", "custom")

    on_poke = None      # optional: callable() -> bool; True means the click was handled

    def _orb_clicked(self) -> None:
        if self.on_poke is not None:
            try:
                if self.on_poke():
                    return
            except Exception:
                pass
        self._fire("console")

    def _orb_menu(self, view) -> None:
        if self.menu_factory is None:
            return
        menu = self.menu_factory()
        menu.popUpMenuPositioningItem_atLocation_inView_(None, AppKit.NSMakePoint(S / 2, S / 2 - 20), view)

    def _glance(self, point) -> None:
        self._look_at = point

    # --- public API (any thread) ------------------------------------------------------

    def _fire(self, action: str, *args) -> None:
        if self.fire is not None:
            self.fire(action, *args)

    def set_hidden(self, on: bool) -> None:
        """Hide Mint or bring it back. Main thread. In the notch only the little Mint and the icons beside the camera go
        (the notch still opens on hover or a tap); the orb fades away. It comes back by itself when it has something to
        say or do ("Hey Mint", a reply, news), or with the Show button / ⌃⌥H."""
        on = bool(on)
        if on == self.hidden:
            return
        self.hidden = on
        from mint.ui import notch
        if not notch.active() and getattr(self, "_orb_window", None) is not None:
            window = self._orb_window
            AppKit.NSAnimationContext.beginGrouping()
            AppKit.NSAnimationContext.currentContext().setDuration_(0.25)
            window.animator().setAlphaValue_(0.0 if on else 1.0)
            AppKit.NSAnimationContext.endGrouping()
            if on:
                AppHelper.callLater(0.3, lambda: self.hidden and window.orderOut_(None))
            else:
                window.orderFrontRegardless()
                self.orb.hop()
        if on:
            self._hide_bubble()
        from mint.core import hotkeys
        key = (prefs.get("shortcuts") or {}).get("hide") or ""
        back = f", {hotkeys.display(key)}" if key else ""
        print(f"  [{'hidden - Hey Mint, the Show button' + back + ' bring Mint back' if on else 'shown'}]",
              flush=True)

    def toggle_hidden(self) -> None:
        AppHelper.callAfter(lambda: self.set_hidden(not self.hidden))

    def set_state(self, state: str, note: str = "") -> None:
        def apply():
            previous = self._state
            woke = state == "awake" and previous in ("sleeping", "paused", "starting", "offline")
            if self.hidden and (woke or (state in ("thinking", "working", "speaking") and previous != state)):
                self.set_hidden(False)           # woken, or it has something to say or do: back in sight
            self._state = state
            self._touch()
            if self._built:
                self.orb.apply_state(state, bool(prefs.get("voice")))
                if state == "awake" and previous in ("sleeping", "starting"):
                    self.orb.boot()
                self._update_chat_status()
            self._bubble_dirty = True
            if state == "sleeping":
                AppHelper.callLater(2.2, self._hide_bubble_if_idle)
        AppHelper.callAfter(apply)

    def user_said(self, text: str, new_turn: bool = False) -> None:
        def apply():
            if new_turn:
                self._you.clear()
                self._said.clear()
            self._you.add(text, time.monotonic())
            self._bubble_dirty = self._bubble_moving = True
            self._touch()
        AppHelper.callAfter(apply)
        if self._built:
            self.chat.said("user", text, new_turn)

    def assistant_said(self, text: str, new_turn: bool = False) -> None:
        def apply():
            if new_turn:
                self._said.clear()
            added = self._said.add(text, time.monotonic())
            if added and not prefs.get("voice"):
                self._level_target = max(self._level_target, 0.3)   # silent: pulse with the words
            self._bubble_dirty = self._bubble_moving = True
            self._touch()
        AppHelper.callAfter(apply)
        if self._built:
            self.chat.said("mint", text, new_turn)

    def action(self, text: str) -> None:
        def apply():
            self._activity = None
            self._status = text
            self._bubble_dirty = True
            self._touch()
        AppHelper.callAfter(apply)
        if self._built:
            self.chat.note(text)

    def activity_start(self, name: str, args: dict) -> None:
        self._action_seq += 1
        key = self._action_seq

        def apply():
            kind = activity.kind(name)
            text = activity.phrase(name, args)
            hint = activity.app_hint(name, args)
            icon = gfx.app_icon(*hint) if hint else None
            self._activity = {"kind": kind, "text": text, "key": key, "name": name}
            self._look_at = None
            self.orb.set_badge(kind, icon)
            self._bubble_dirty = True
            self._touch()
            self._update_chat_status()
        AppHelper.callAfter(apply)
        if self._built and name not in ("step_done",):
            self.chat.action(key, activity.phrase(name, args))

    def activity_end(self, name: str, ok: bool) -> None:
        def apply():
            current = self._activity
            self._activity = None
            if current:
                self._status = ("✓ " if ok else "✗ ") + current["text"]
                self.chat.action_done(current["key"], ok)
            self.orb.finish_badge(ok)
            self._bubble_dirty = True
            self._touch()
            self._update_chat_status()
        AppHelper.callAfter(apply)

    def needs_screen(self, name: str) -> bool:
        """Before a screen action: if the chat holds the keyboard, hand it back
        to the user's app. Returns True if it did (the caller waits a beat)."""
        if activity.kind(name) not in SCREEN_KINDS or not self._built or not self.chat.holds_keyboard():
            return False
        AppHelper.callAfter(self.chat.release_keyboard)
        return True

    def stopped(self) -> None:
        def apply():
            if self._activity is not None:
                self.chat.action_done(self._activity["key"], False, self._activity["text"] + " — stopped")
            self._activity = None
            self._you.clear()                      # "stop" clears the words on screen at once (the notch
            self._said.clear()                     # and the bubble keep them open otherwise)
            self._status = "■ Stopped"
            self.orb.stopped()
            self.orb.set_progress(None)
            self.chat.progress(0, 0)
            self._bubble_dirty = True
            self._touch()
            self._update_chat_status()
        AppHelper.callAfter(apply)
        if self._built:
            self.chat.note("stopped")

    def celebrate(self) -> None:
        AppHelper.callAfter(lambda: self.orb.celebrate())

    def new_exchange(self) -> None:
        def apply():
            self._you.clear()
            self._said.clear()
            if self._activity is None:
                self._status = ""
            self._bubble_dirty = True
        AppHelper.callAfter(apply)
        if self._built:
            self.chat.boundary()

    def set_level(self, level: float) -> None:
        self._level_target = max(self._level_target, min(1.0, level * 9))

    def progress(self, done: int, total: int, label: str = "") -> None:
        def apply():
            self.orb.set_progress(done / total if total else None)
            self.chat.progress(done, total, label)
            if label:
                self._status = label
                self._bubble_dirty = True
            if total and done >= total:
                self.orb.celebrate()
            self._touch()
        AppHelper.callAfter(apply)

    def refresh(self) -> None:
        def apply():
            if not self._built:
                return
            self.orb.apply_state(self._state, bool(prefs.get("voice")))
            if prefs.get("position") != "custom":
                cx, cy = self._home_center()
                self._orb_window.setFrameOrigin_(AppKit.NSMakePoint(cx - S / 2, cy - S / 2))
                self._orb_moved(final=False)
            self.chat.refresh()
            self._bubble_dirty = True
        AppHelper.callAfter(apply)

    def show_chat(self, want: bool) -> None:
        def apply():
            if want and not self.chat.is_open:
                self.open_console()
            elif not want and self.chat.is_open:
                self.close_console()
        AppHelper.callAfter(apply)

    # --- console = the chat (main thread) ---------------------------------------------

    @property
    def console_open(self) -> bool:
        return self._built and self.chat.is_open

    def open_console(self) -> None:
        if not self._built:
            return
        AppKit.NSApplication.sharedApplication().unhideWithoutActivation()    # if anything hid Mint
        self._hide_bubble()
        self.chat.open(self._chat_frame())
        self._update_chat_status()

    def close_console(self, keep_visible: bool = False) -> None:
        if self._built:
            self.chat.close()
            self._bubble_dirty = True

    def toggle_console(self) -> None:
        if self.console_open:
            self.close_console()
        else:
            self.open_console()

    # --- internals (main thread) ------------------------------------------------------

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def _update_chat_status(self) -> None:
        if not self._built:
            return
        text = TITLES.get(self._state, "")
        if self._state == "speaking" and not prefs.get("voice"):
            text = "Replying (silent)"
        if self._activity:
            text = self._activity["text"]
        if not prefs.get("mic") and self._state in ("paused", "sleeping"):
            text = "Mic off"
        busy = self._activity is not None or self._state in ("working", "thinking")
        self.chat.set_status(self._state, text, busy)

    def _status_line(self, now: float) -> str:
        if self._activity:
            text = self._activity["text"]
            if text.endswith("…"):                  # already shortened: no dots after its ellipsis
                return text
            return text + "." * (1 + int(now * 3) % 3)
        if self._status:
            return self._status
        if self._state in ("thinking", "working"):
            return TITLES[self._state]
        return ""

    def _bubble_text(self, now: float, animate: bool):
        """-> (attributed string for display, same text fully shown for sizing, moving)."""
        para = AppKit.NSMutableParagraphStyle.alloc().init()
        para.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        para.setMinimumLineHeight_(17)
        para.setMaximumLineHeight_(17)
        shown = AppKit.NSMutableAttributedString.alloc().init()
        full = AppKit.NSMutableAttributedString.alloc().init()
        font = AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightMedium)
        small = AppKit.NSFont.systemFontOfSize_weight_(11, AppKit.NSFontWeightRegular)
        moving = False

        fg = look.rgb(AppKit.NSColor.labelColor(), self._bubble)     # black in light mode, white in dark

        def put(target, text, rgb, alpha, f=font, offset=0.0):
            target.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(text, {
                AppKit.NSFontAttributeName: f, AppKit.NSForegroundColorAttributeName: gfx.ns(rgb, alpha),
                AppKit.NSBaselineOffsetAttributeName: offset, AppKit.NSParagraphStyleAttributeName: para}))

        status = self._status_line(now)
        words, prefix, accent = None, "", gfx.state_rgb("speaking")
        if self._said.words:
            words = self._said
        elif self._you.words:
            words, prefix, accent = self._you, "You  ", gfx.state_rgb("awake")
        if status:
            for target in (shown, full):
                put(target, status + ("\n" if words else ""), fg, 0.55, small)
        if words:
            if prefix:
                bold = AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightSemibold)
                for target in (shown, full):
                    put(target, prefix, look.ink(accent, self._bubble), 0.95, bold)
            tail = words.tail(180)
            if len(tail) < len(words.words):
                for target in (shown, full):
                    put(target, "… ", fg, 0.5)
            for text, at in tail:
                put(full, text + " ", fg, 0.95)
                age = now - at
                if animate and age < 0:
                    moving = True
                    continue
                if animate and age < WORD_IN:
                    e = 1 - (1 - age / WORD_IN) ** 3
                    put(shown, text + " ", gfx.mix(look.ink(accent, self._bubble), fg, e), 0.1 + 0.85 * e,
                        offset=-4 * (1 - e))
                    moving = True
                else:
                    put(shown, text + " ", fg, 0.95)
        return shown, full, moving

    def _render_bubble(self, now: float) -> None:
        if self.chat.is_open:
            return
        animate = bool(prefs.get("word_animation"))
        shown, full, moving = self._bubble_text(now, animate)
        self._bubble_moving = moving
        self._bubble_dirty = moving
        if full.length() == 0 or self.hidden:
            self._hide_bubble()
            return
        rect = full.boundingRectWithSize_options_(
            AppKit.NSMakeSize(BUBBLE_MAX - 26, 400),
            AppKit.NSStringDrawingUsesLineFragmentOrigin | AppKit.NSStringDrawingUsesFontLeading)
        tw = math.ceil(rect.size.width) + 6
        th = min(math.ceil(rect.size.height) + 2, 17 * 4 + 2)
        width, height = min(BUBBLE_MAX, max(64, tw + 26)), th + 16
        self._bubble_label.setAttributedStringValue_(shown)
        self._bubble_label.setMaximumNumberOfLines_(4)
        self._bubble_label.setFrame_(AppKit.NSMakeRect(13, 8, width - 26, th))
        self._place_bubble(width, height)
        if not self._bubble_visible:
            self._bubble_visible = True
            self._bubble.orderFrontRegardless()
            AppKit.NSAnimationContext.beginGrouping()
            AppKit.NSAnimationContext.currentContext().setDuration_(0.22)
            self._bubble.animator().setAlphaValue_(1.0)
            AppKit.NSAnimationContext.endGrouping()

    def _hide_bubble(self) -> None:
        if not self._bubble_visible:
            return
        self._bubble_visible = False
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.35)
        self._bubble.animator().setAlphaValue_(0.0)
        AppKit.NSAnimationContext.endGrouping()

    def _hide_bubble_if_idle(self) -> None:
        if self._state in ("sleeping", "paused") and self._activity is None:
            self._you.clear()
            self._said.clear()
            self._status = ""
            self._hide_bubble()

    def _tick(self) -> None:
        now = time.monotonic()
        target = self._level_target
        self._level_target *= 0.82
        self._level = self._level * 0.6 + target * 0.4 if target > self._level else self._level * 0.9
        if self._level > 0.05:
            self._touch()
        if not self._built:
            return

        center = self.orb_center()
        mouse = AppKit.NSEvent.mouseLocation()
        dist = math.hypot(mouse.x - center[0], mouse.y - center[1])
        over = dist < D * 0.75
        # Clickable only on the orb itself; everywhere else the window is air.
        dragging = bool(AppKit.NSEvent.pressedMouseButtons() & 1) and self._interactive
        # While the orb has turned into the island (island.py), the island takes the clicks.
        want = (over or dragging) and not getattr(self, "island_active", False)
        if want != self._interactive:
            self._interactive = want
            self._orb_window.setIgnoresMouseEvents_(not want)

        # Eyes: on the work while working or thinking, on the pointer otherwise.
        look = None
        if self._state == "working" and self._look_at is not None:
            look = (self._look_at[0] - center[0], self._look_at[1] - center[1])
        elif self._state not in ("working", "thinking"):
            look = (mouse.x - center[0], mouse.y - center[1])
        busy = self._activity is not None or self._state in ("working", "thinking", "speaking")
        self.orb.tick(now, self._level, look, over, busy)

        if self._bubble_dirty or (self._activity and int(now * 3) != int((now - 1 / 30) * 3)):
            self._render_bubble(now)
        if (self._bubble_visible and not busy and not self.chat.is_open
                and now - self._last_activity > IDLE_HIDE):
            # Faded out means done with: the next thing starts afresh.
            self._you.clear()
            self._said.clear()
            self._status = ""
            self._hide_bubble()
