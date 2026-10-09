"""Notch mode: Mint lives in the MacBook's camera notch, like the iPhone's Dynamic Island.

Two looks, chosen in the menu ("Dynamic Island at the notch") or by voice ("switch to
notch mode" -> display_mode): the floating orb (default), or this.

In notch mode there is no floating orb. A black shape grows out of the notch itself -
same black, same concave top corners, so it reads as the notch getting bigger:

    compact   [ (•‿•) ▓▓▓ notch ▓▓▓  ▁▃▅ ]       the little Mint on the left of the camera,
                                                   what it is doing on the right (sound bars
                                                   while listening/speaking, a spinner while
                                                   thinking, the task's icon while working)
    open      the shape drops down: the status line and the words as they are spoken
              (word by word), a progress bar for long tasks
    hover     the controls: mic, voice, chat, sleep, screen-share eye, settings
    island    the recorder, cards and lessons of island.py hang from the notch too

The little Mint is Mint's real orb (hud.orb), only smaller: blinking, eyes on the pointer,
every expression (sunglasses, tears, hearts...), the task glyphs. Click it or the shape to
open the chat (it appears under the notch); right-click for the menu.

Screens without a notch get the same shape hanging from the middle of the menu bar.

Built from HUD.build before the expressions attach (they draw on hud.orb). Main thread.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import time

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx
from mint.ui import kinetics
from mint.core import prefs
from mint.ui.notch_state import NotchState

log = logging.getLogger("mint.ui.notch")

PREF = "notch_mode"
FACE = 20              # the little Mint's diameter
WING = 32              # how far the compact shape reaches out on each side of the camera
EAR = 7                # the concave top corners, where the shape meets the screen edge
MAX_W = 380            # the widest the dropped-down shape gets for words
PAD_X = 16             # text inset from the shape's sides
BTN, STEP = 30, 37     # hover controls: button size, spacing
PLAYER_W, PLAYER_H = 360, 110      # the music player inside the open notch
PAUSED_WINGS = 12.0    # a paused song keeps the notch's wings this long, then the little Mint returns
INNER_OPEN = 0.8                     # resting on the small row's plain part opens the full notch after this
                                     # (Settings: notch_open_after)
AFTER_LEAVE = 1.0                    # an opened notch folds this long after the pointer leaves it (notch_close_after);
                                     # the countdown line runs the whole time, from the moment the pointer leaves
PEEK_AFTER = 0.3                     # a short rest peeks the controls (notch_state: an opened notch folds 8 s
                                     # after the pointer leaves, or after a quiet minute under it)
CLOSE_LEAD = 0.12                    # folding: the content goes first, the shape follows this much later
PANE_FACE = 2.2                      # the little Mint grown into the home tab's Mint pane (shared element)
GLOW_D = 84                          # the closed wing's ambient glow behind the little Mint
AMBER = (1.00, 0.72, 0.26)           # needs you (notch_agents' waiting colour)
FACE_PULL = 30.0                     # how far (points) the little Mint is pulled before it lets go of the notch
HOME_W, BODY_H = 640, 150          # the open notch on hover (boring.notch's size): header row + body
AGENT_H = 196                      # the Agents tab ("Claude mode"): a coding agent's session, live
AGENT_MINI_H = 60                  # ... minimized: one line
BIG_W, BIG_H = 760, 430            # the notch grown for search results ("show more", 3x3 / 3x2 / 3x1)
SIDE_W = HOME_W - 32 - PLAYER_W - 12   # the right-hand pane: the week calendar or the battery
WIN_W, WIN_H = 900, 860    # room for the notch to wrap cards (two side by side) or the chat under it
# Over the menu bar, as notch apps do (main menu + 3).
LEVEL = Quartz.CGWindowLevelForKey(Quartz.kCGMainMenuWindowLevelKey) + 3


def _seconds(key: str, default: float) -> float:
    """A timing from Settings, within sane bounds."""
    try:
        return max(0.2, min(30.0, float(prefs.get(key) or default)))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return bool(prefs.get(PREF))


# --- where the notch is -------------------------------------------------------------------------

def geometry():
    """(screen, notch centre x, top y, notch width, notch height, has a real notch) - Cocoa points."""
    screens = list(AppKit.NSScreen.screens() or [])
    notched = [s for s in screens if s.safeAreaInsets().top > 0]
    screen = notched[0] if notched else (screens[0] if screens else AppKit.NSScreen.mainScreen())
    frame = screen.frame()
    top = frame.origin.y + frame.size.height
    if notched:
        height = screen.safeAreaInsets().top
        left, right = screen.auxiliaryTopLeftArea(), screen.auxiliaryTopRightArea()
        if left is not None and right is not None:
            width = frame.size.width - left.size.width - right.size.width + 4
            cx = frame.origin.x + left.size.width - 2 + width / 2
        else:
            width, cx = 185.0, frame.origin.x + frame.size.width / 2
        return screen, cx, top, width, height, True
    visible = screen.visibleFrame()
    bar = top - (visible.origin.y + visible.size.height)
    return screen, frame.origin.x + frame.size.width / 2, top, 150.0, max(24.0, bar or 28.0), False


def island_path(W, H, w, h, ear=EAR, r=10.0):
    """The shape, hanging from the top edge of a WxH box: concave top corners, round bottom
    corners. Always the same list of pieces, so one shape springs smoothly into another."""
    cx = W / 2
    x0, x1, top, bottom = cx - w / 2, cx + w / 2, H, H - h
    r = min(r, h / 2 - 0.5, w / 2 - 0.5)
    p = Quartz.CGPathCreateMutable()
    Quartz.CGPathMoveToPoint(p, None, x0 - ear, top)
    Quartz.CGPathAddQuadCurveToPoint(p, None, x0, top, x0, top - ear)
    Quartz.CGPathAddLineToPoint(p, None, x0, bottom + r)
    Quartz.CGPathAddQuadCurveToPoint(p, None, x0, bottom, x0 + r, bottom)
    Quartz.CGPathAddLineToPoint(p, None, x1 - r, bottom)
    Quartz.CGPathAddQuadCurveToPoint(p, None, x1, bottom, x1, bottom + r)
    Quartz.CGPathAddLineToPoint(p, None, x1, top - ear)
    Quartz.CGPathAddQuadCurveToPoint(p, None, x1, top, x1 + ear, top)
    Quartz.CGPathCloseSubpath(p)
    return p


def rim_paths(W, H, w, h, ear=EAR, r=10.0):
    """The shape's outline without its top (the screen edge): the status ring; and its bottom edge alone: the
    busy sweep's track. The same points as island_path, so on the same spring they stay on its edge."""
    cx = W / 2
    x0, x1, top, bottom = cx - w / 2, cx + w / 2, H, H - h
    r = min(r, h / 2 - 0.5, w / 2 - 0.5)
    ring = Quartz.CGPathCreateMutable()
    Quartz.CGPathMoveToPoint(ring, None, x0 - ear, top)
    Quartz.CGPathAddQuadCurveToPoint(ring, None, x0, top, x0, top - ear)
    Quartz.CGPathAddLineToPoint(ring, None, x0, bottom + r)
    Quartz.CGPathAddQuadCurveToPoint(ring, None, x0, bottom, x0 + r, bottom)
    Quartz.CGPathAddLineToPoint(ring, None, x1 - r, bottom)
    Quartz.CGPathAddQuadCurveToPoint(ring, None, x1, bottom, x1, bottom + r)
    Quartz.CGPathAddLineToPoint(ring, None, x1, top - ear)
    Quartz.CGPathAddQuadCurveToPoint(ring, None, x1, top, x1 + ear, top)
    edge = Quartz.CGPathCreateMutable()
    Quartz.CGPathMoveToPoint(edge, None, x0, bottom + r)
    Quartz.CGPathAddQuadCurveToPoint(edge, None, x0, bottom, x0 + r, bottom)
    Quartz.CGPathAddLineToPoint(edge, None, x1 - r, bottom)
    Quartz.CGPathAddQuadCurveToPoint(edge, None, x1, bottom, x1, bottom + r)
    return ring, edge


def _sfx(name: str) -> None:
    try:
        from mint.ui import sfx
        sfx.play(name)
    except Exception:
        pass


def _idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input anywhere (news waits while you're away)."""
    try:
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType))
    except Exception:
        return 0.0


# --- AppKit pieces ------------------------------------------------------------------------------

class MintNotchPanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        # Only while the Search tab is up: its field takes typing (a non-activating panel, like Spotlight).
        return bool(getattr(notch, "allow_key", False))

    def canBecomeMainWindow(self):
        return False

    def constrainFrameRect_toScreen_(self, rect, screen):
        return rect                      # it belongs over the menu bar, at the very top


class MintNotchView(AppKit.NSView):
    owner = None

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        self._moved = False
        self._down = self.convertPoint_fromView_(event.locationInWindow(), None)
        self._face = (self.owner is not None and self.owner.on_face(self._down))
        self._start = event.locationInWindow()

    def mouseDragged_(self, event):
        if self.owner is not None and getattr(self.owner, "_carrying", False):
            self.owner.carry()
            return
        if not getattr(self, "_face", False) or self.owner is None:
            return
        where = event.locationInWindow()
        dx, dy = where.x - self._start.x, where.y - self._start.y
        if not self._moved and abs(dx) + abs(dy) < 3:
            return
        self._moved = True
        if self.owner.face_drag(dx, dy):
            self._face = False                   # it went out: the rest of this drag is over

    def mouseUp_(self, event):
        if self.owner is not None and getattr(self.owner, "_carrying", False):
            self.owner.carry_end()
        elif getattr(self, "_moved", False):
            if self.owner is not None:
                self.owner.face_release()
        elif self.owner is not None:
            if getattr(self, "_face", False):
                self.owner.poke_face(getattr(self, "_down", None))    # a tap on the little Mint: it reacts
            self.owner.clicked()
        self._face = self._moved = False

    def rightMouseDown_(self, event):
        if self.owner is not None:
            self.owner.right_clicked(event, self)

    # Files dropped on the notch go on the shelf (notch_shelf).
    def draggingEntered_(self, sender):
        ok = self.owner is not None and self.owner.dragged_in(sender.draggingPasteboard())
        return AppKit.NSDragOperationCopy if ok else AppKit.NSDragOperationNone

    def draggingUpdated_(self, sender):
        return self.draggingEntered_(sender)

    def draggingExited_(self, sender):
        if self.owner is not None:
            self.owner.dragged_out()

    def prepareForDragOperation_(self, sender):
        return True

    def performDragOperation_(self, sender):
        return bool(self.owner is not None and self.owner.dropped(sender.draggingPasteboard()))


class MintNotchLabel(AppKit.NSTextField):
    """Text that never takes a click: a label faded to nothing still sat over the buttons of the small
    row (the Mint pane's title and hint), so pressing them did nothing."""

    def hitTest_(self, point):
        return None


class MintNotchButton(AppKit.NSButton):
    def acceptsFirstMouse_(self, event):
        return True


class MintNotchAct(AppKit.NSObject):
    def initWithFn_(self, fn):
        self = objc.super(MintNotchAct, self).init()
        if self is not None:
            self.fn = fn
        return self

    def fire_(self, sender):
        try:
            self.fn()
        except Exception:
            log.exception("notch button failed")


class MintNotchTicker(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(MintNotchTicker, self).init()
        if self is not None:
            self.owner = owner
        return self

    def tick_(self, timer):
        from mint.ui.hud import _PROFILE, _profiled
        started = time.perf_counter() if _PROFILE else 0.0
        try:
            self.owner.tick()
        except Exception:
            log.exception("notch tick failed")
        if _PROFILE:
            _profiled("notch", time.perf_counter() - started)


def _in_call() -> bool:
    try:
        from mint.app import meet_call
        call = meet_call.current()
        return call is not None and call.live
    except Exception:
        return False


def _white(alpha=1.0):
    return AppKit.NSColor.colorWithWhite_alpha_(1.0, alpha)


# --- the island at the notch --------------------------------------------------------------------

class Notch:
    def __init__(self) -> None:
        self.hud = None
        self.size = (0.0, 0.0)
        self.mode = ""
        self.caption = None             # (shown, full) attributed strings while words are showing
        self.progress = None            # 0..1 for long tasks
        # Hover dwell, a notch you opened and when it folds, and the agents' alerts one at a time (notch_state).
        self.st = NotchState(timing={"peek": PEEK_AFTER, "after_leave": AFTER_LEAVE, "countdown": AFTER_LEAVE})
        self._acts = []
        self._ind = ""
        self._styled: dict = {}             # guest windows dressed as part of the notch -> how they were
        self._backs: dict = {}              # the black backing views added under a dressed chat
        self._faces: list = []              # guest cards' own little faces, hidden while in the notch
        self.phase = "off"                  # off / entering / on / leaving
        self.music_peek_until = 0.0          # the player shows in the notch until then (music just started)
        self.tab = "home"                    # the open notch's tab: home / shelf
        self.home = None                     # the open notch's pieces, built the first time it opens
        self.battery_peek_until = 0.0        # plugged in / unplugged: the wings show it for a moment
        self.battery_event = ""
        self.drag_until = 0.0                # a file is being dragged near the notch: open on the shelf
        self._carrying = False               # the little Mint was pulled out and is still being carried
        self.inner_since = 0.0               # since when the pointer has rested on the small row's plain part
        self.search_until = 0.0              # Mint just showed files/apps: the notch stays open on Search
        self._paused_since = 0.0             # when the song in the wings was paused
        self._mint_words = ""                # what Mint is saying (the open notch's Mint pane shows it)
        self.allow_key = False
        self._calm, self._calm_skip = False, 0   # nothing moving on the notch: ticks at 10 a second (tick)
        self._agents_hooked = False
        self._search_hooked = False
        self._drag_count = -1
        self._agent_push = False
        self._alert_id = None                # the alert on show last frame (a new one: tab, focus, flash, sound)
        self._sync_at = self._idle_at = 0.0  # the agents' needs-you and the idle time, looked at a few times a second
        self._hold_until = 0.0               # folding: the shape waits this long for the content to go
        self._open_started = 0.0             # the shape began to open: content reveals staggered after this
        self._face_where = "wing"            # the little Mint: in the wing, or grown into the Mint pane
        self._face_spot = (0.0, 0.0, 1.0)    # ... its centre (window points) and scale there
        self._rim_status = None              # (kind, colour) the rim and the glow show
        self._cd = None                      # the countdown line's (start, end)
        self._sweep_w = 0.0
        self._scene = None                   # (kind, view, controller): the one scene in the body (notch_fx/composer)
        # "Set up" chips (notch_tips): waiting for macOS's answer since, just set up until, what the Mint pane says.
        self._tip_wait: dict = {}
        self._tip_prompted: set = set()      # ... the ones macOS showed its own box for (it can say "denied")
        self._tip_party: dict = {}
        self._tip_say = None                 # (title, words, until)
        self._tip_hold = 0.0                 # the notch stays open until then (you're answering macOS's box)
        self._tip_hover = None
        self._tip_polling = False
        self._tip_front = None               # the app in front before Mint asked macOS (it goes back there)
        self._mouse_through = None           # last setIgnoresMouseEvents_ value (set only when it changes)
        self._scene_since = 0.0
        self._scene_shown = False
        self._failed = None                  # (when, reason, the user's words): a failed step, for the error card

    @property
    def pinned(self) -> bool:
        """Opened by you (a click, resting on it, the sparkles button): the full notch, until it folds."""
        return self.st.is_open

    @pinned.setter
    def pinned(self, value: bool) -> None:
        if value:
            self.st.open_by("click", time.monotonic())
        else:
            self.st.close(time.monotonic())

    # --- building -------------------------------------------------------------------------------

    def build(self, hud) -> None:
        self.hud = hud
        self.screen, self.cx, self.top, self.nw, self.nh, self.real = geometry()
        frame = AppKit.NSMakeRect(self.cx - WIN_W / 2, self.top - WIN_H, WIN_W, WIN_H)
        panel = MintNotchPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        panel.setLevel_(LEVEL)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)
        self._mouse_through = True
        panel.setHidesOnDeactivate_(False)
        panel.setFloatingPanel_(True)
        panel.setLevel_(LEVEL)           # after setFloatingPanel, which resets the level to 3
        panel.setMovable_(False)
        panel.setAllowsToolTipsWhenApplicationIsInactive_(True)     # a result's full path on hover
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        from mint.ui import effects
        panel.setSharingType_(effects.SHARING)
        root = MintNotchView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, WIN_W, WIN_H))
        root.owner = self
        root.setWantsLayer_(True)
        panel.setContentView_(root)
        self.panel, self.root = panel, root

        # The black shape, and a container for everything inside it, clipped to the same shape.
        self.shape = Quartz.CAShapeLayer.layer()
        self.shape.setBounds_(Quartz.CGRectMake(0, 0, WIN_W, WIN_H))
        self.shape.setAnchorPoint_(Quartz.CGPointMake(0.5, 1.0))      # breathes from the top centre
        self.shape.setPosition_(Quartz.CGPointMake(WIN_W / 2, WIN_H))
        self.shape.setFillColor_(AppKit.NSColor.blackColor().CGColor())
        self.shape.setShadowColor_(AppKit.NSColor.blackColor().CGColor())
        self.shape.setShadowOpacity_(0.0)
        self.shape.setShadowRadius_(6)
        self.shape.setShadowOffset_(Quartz.CGSizeMake(0, -3))
        root.layer().addSublayer_(self.shape)
        box = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, WIN_W, WIN_H))
        box.setWantsLayer_(True)
        root.addSubview_(box)
        self.mask = Quartz.CAShapeLayer.layer()
        self.mask.setFillColor_(AppKit.NSColor.blackColor().CGColor())
        box.layer().setMask_(self.mask)
        self.box = box

        # The little Mint, left of the camera.
        self.face_center = (WIN_W / 2 - self.nw / 2 - WING / 2 + 3, WIN_H - self.nh / 2)
        host = Quartz.CALayer.layer()
        host.setBounds_(Quartz.CGRectMake(0, 0, WIN_W, WIN_H))
        # Anchored on the face, so moving the host moves the face and scaling it grows the face in place
        # (the shared-element flight between the wing and the Mint pane, and the elastic pull).
        host.setAnchorPoint_(Quartz.CGPointMake(self.face_center[0] / WIN_W, self.face_center[1] / WIN_H))
        host.setPosition_(Quartz.CGPointMake(*self.face_center))
        box.layer().addSublayer_(host)
        self.face_host = host
        self._face_spot = (self.face_center[0], self.face_center[1], 1.0)
        from mint.ui.orb import Orb
        self.orb = Orb(host, self.face_center, FACE)
        for layer in (self.orb.ring, self.orb.spinner, self.orb.progress_ring):   # no outer rings in a tight spot
            layer.removeAllAnimations()
            layer.setHidden_(True)
        self.orb.in_notch = True                # expressions float their extras downwards, inside the notch
        # Mic off: a small crossed-out mic tucked against the little Mint's lower right (it flies with the face).
        self.mic_badge = self._build_mic_badge(host)
        self._crisp(host, 2.0 * PANE_FACE)      # grown into the pane it stays sharp
        # The wing's ambient light: a soft state-coloured glow behind the face, bleeding into the black (clipped
        # to the island with everything in the box). Under the face, and it flies with it.
        glow = Quartz.CAGradientLayer.layer()
        glow.setType_(Quartz.kCAGradientLayerRadial)
        glow.setBounds_(Quartz.CGRectMake(0, 0, GLOW_D, GLOW_D))
        glow.setPosition_(Quartz.CGPointMake(*self.face_center))
        glow.setStartPoint_(Quartz.CGPointMake(0.5, 0.5))
        glow.setEndPoint_(Quartz.CGPointMake(1.0, 1.0))
        glow.setLocations_([0.0, 0.42, 1.0])
        wrap = Quartz.CALayer.layer()               # fades in and out; the glow inside breathes
        wrap.setFrame_(Quartz.CGRectMake(0, 0, WIN_W, WIN_H))
        wrap.setOpacity_(0.0)
        wrap.addSublayer_(glow)
        host.insertSublayer_atIndex_(wrap, 0)
        self.glow, self.glow_wrap = glow, wrap
        self._crisp(wrap, 2.0 * PANE_FACE)

        # Right of the camera: what is going on.
        self.ind_center = (WIN_W / 2 + self.nw / 2 + WING / 2 - 3, WIN_H - self.nh / 2)
        self._build_indicator(box.layer())

        # The dropped-down part: status, words, progress, controls.
        self.status = self._label(11, _white(0.55), AppKit.NSFontWeightMedium)
        self.words = self._label(13, _white(0.95), AppKit.NSFontWeightMedium, lines=4)
        self.track = Quartz.CALayer.layer()
        self.track.setBackgroundColor_(_white(0.15).CGColor())
        self.track.setCornerRadius_(1.5)
        self.fill = Quartz.CALayer.layer()
        self.fill.setAnchorPoint_(Quartz.CGPointMake(0, 0.5))
        self.fill.setCornerRadius_(1.5)
        for layer in (self.track, self.fill):
            layer.setOpacity_(0.0)
            box.layer().addSublayer_(layer)
        self.buttons = []
        for symbol, tip, fn in (("stop.fill", "Stop (same as saying “stop”)", lambda: self.hud._fire("stop")),
                                ("mic.fill", "Microphone on/off", lambda: prefs.toggle("mic")),
                                ("speaker.wave.2.fill", "Spoken replies on/off", lambda: prefs.toggle("voice")),
                                ("bubble.left.and.bubble.right.fill", "Open the chat", self._chat),
                                ("moon.zzz.fill", "Sleep", lambda: self.hud._fire("sleep")),
                                ("eye.slash", "Visible in screen sharing", self._eye),
                                ("arrow.up.to.line", "Hide Mint: the notch stays, the little Mint and its icons go (⌃⌥H)",
                                 lambda: self.hud.set_hidden(not self.hud.hidden)),
                                ("slider.horizontal.3", "Settings", self._menu_from_button)):
            act = MintNotchAct.alloc().initWithFn_(fn)
            self._acts.append(act)
            button = MintNotchButton.buttonWithImage_target_action_(gfx.symbol(symbol, 14), act, "fire:")
            button.setBordered_(False)
            button.setToolTip_(tip)
            button.setContentTintColor_(_white(0.96))
            button.setWantsLayer_(True)
            button.layer().setCornerRadius_(BTN / 2)
            button.layer().setBackgroundColor_(_white(0.15).CGColor())
            button.setAlphaValue_(0.0)
            button.setHidden_(True)
            box.addSubview_(button)
            self.buttons.append((symbol, button))

        self._build_rim(box.layer())
        self._resize(self.nw + 2 * WING, self.nh, animate=False)
        self.ticker = MintNotchTicker.alloc().initWithOwner_(self)
        timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 30, self.ticker, "tick:", None, True)
        AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, AppKit.NSRunLoopCommonModes)
        center = AppKit.NSNotificationCenter.defaultCenter()
        self._menus = 0

        def menu(delta):
            self._menus = max(0, self._menus + delta)
        self._menu_obs = [
            center.addObserverForName_object_queue_usingBlock_(AppKit.NSMenuDidBeginTrackingNotification, None, None,
                                                               lambda note: menu(1)),
            center.addObserverForName_object_queue_usingBlock_(AppKit.NSMenuDidEndTrackingNotification, None, None,
                                                               lambda note: menu(-1))]
        self._screens = center.addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationDidChangeScreenParametersNotification, None, None, lambda note: self._moved())
        self._watch_esc()

    def _crisp(self, layer, scale: float) -> None:
        """Render a layer tree at `scale` points per pixel, so a transform that grows it doesn't blur it."""
        try:
            layer.setContentsScale_(max(layer.contentsScale(), scale))
            for sub in layer.sublayers() or []:
                self._crisp(sub, scale)
        except Exception:
            pass

    def _build_rim(self, parent) -> None:
        """Status on the shape itself, inside its edge (the box clips the outer half): a ring (needs you: amber,
        pulsing), a one-shot flash (done: green, error: red), a light sweeping along the bottom edge (busy), and
        the countdown line of a notch about to fold. All follow the shape's spring (_resize)."""
        def ring_layer(into):
            layer = Quartz.CAShapeLayer.layer()
            layer.setFillColor_(None)
            layer.setLineWidth_(3.0)                # 1.5 pt shows: the outer half is clipped
            layer.setLineJoin_(Quartz.kCALineJoinRound)
            layer.setLineCap_(Quartz.kCALineCapRound)
            layer.setShadowOffset_(Quartz.CGSizeMake(0, 0))
            layer.setShadowRadius_(5.0)
            layer.setShadowOpacity_(0.9)
            into.addSublayer_(layer)
            return layer
        rim_wrap = Quartz.CALayer.layer()           # fades in and out; the ring inside pulses
        rim_wrap.setFrame_(Quartz.CGRectMake(0, 0, WIN_W, WIN_H))
        rim_wrap.setOpacity_(0.0)
        parent.addSublayer_(rim_wrap)
        self.rim, self.rim_wrap = ring_layer(rim_wrap), rim_wrap
        self.flash_rim = ring_layer(parent)
        self.flash_rim.setOpacity_(0.0)
        sweep = Quartz.CALayer.layer()
        sweep.setFrame_(Quartz.CGRectMake(0, 0, WIN_W, WIN_H))
        sweep.setOpacity_(0.0)
        track = Quartz.CAShapeLayer.layer()
        track.setFrame_(Quartz.CGRectMake(0, 0, WIN_W, WIN_H))
        track.setFillColor_(None)
        track.setStrokeColor_(AppKit.NSColor.blackColor().CGColor())
        track.setLineWidth_(4.0)                    # 2 pt inside the edge
        track.setLineCap_(Quartz.kCALineCapRound)
        sweep.setMask_(track)
        band = Quartz.CAGradientLayer.layer()
        band.setBounds_(Quartz.CGRectMake(0, 0, 150, WIN_H))
        band.setPosition_(Quartz.CGPointMake(WIN_W / 2, WIN_H / 2))
        band.setStartPoint_(Quartz.CGPointMake(0.0, 0.5))
        band.setEndPoint_(Quartz.CGPointMake(1.0, 0.5))
        sweep.addSublayer_(band)
        parent.addSublayer_(sweep)
        self.sweep, self.sweep_track, self.sweep_band = sweep, track, band
        line = Quartz.CALayer.layer()
        line.setBounds_(Quartz.CGRectMake(0, 0, 100, 2))
        line.setCornerRadius_(1.0)
        line.setBackgroundColor_(_white(0.42).CGColor())
        line.setOpacity_(0.0)
        parent.addSublayer_(line)
        self.cd_line = line

    def _watch_esc(self) -> None:
        """Esc with the pointer on the notch (or its Search field focused): the alert on show is snoozed."""
        def seen(event):
            try:
                if event.keyCode() == 53:
                    AppHelper.callAfter(self._esc)
            except Exception:
                pass

        def local(event):
            seen(event)
            return event
        mask = AppKit.NSEventMaskKeyDown
        try:
            # (the global one only hears keys once Mint may monitor input; it never sees what is typed, only Esc)
            self._esc_monitors = [AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, seen),
                                  AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, local)]
        except Exception:
            log.debug("no Esc monitor", exc_info=True)

    def _esc(self) -> None:
        if not _live[0] or self.phase != "on":
            return
        if (self.tab == "search" and self.panel.isKeyWindow()) or self._composing():
            return                                  # Esc belongs to the search field / the composer (it cancels)
        if not (self._inside(AppKit.NSEvent.mouseLocation()) or self.panel.isKeyWindow()):
            return
        self.snooze()

    def snooze(self) -> None:
        """Esc (or "dismiss"): the notch folds at once; news on show goes, needs-you alerts wait until it's opened."""
        if self.st.is_open or self.st.shown() is not None:
            self.st.snooze(time.monotonic())
            _sfx("close")

    def _label(self, size, color, weight, lines=1):
        field = MintNotchLabel.wrappingLabelWithString_("")
        field.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, weight))
        field.setTextColor_(color)
        field.setMaximumNumberOfLines_(lines)
        field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail if lines == 1 else AppKit.NSLineBreakByWordWrapping)
        field.setAlphaValue_(0.0)
        field.setSelectable_(False)
        self.box.addSubview_(field)
        return field

    def _build_indicator(self, parent) -> None:
        x, y = self.ind_center
        self.bars = []
        for i in range(4):
            bar = Quartz.CALayer.layer()
            bar.setBounds_(Quartz.CGRectMake(0, 0, 3, 4))
            bar.setCornerRadius_(1.5)
            bar.setPosition_(Quartz.CGPointMake(x - 7.5 + i * 5, y))
            bar.setBackgroundColor_(_white(0.85).CGColor())
            parent.addSublayer_(bar)
            self.bars.append(bar)
        ring = Quartz.CAShapeLayer.layer()
        ring.setBounds_(Quartz.CGRectMake(0, 0, 14, 14))
        ring.setPosition_(Quartz.CGPointMake(x, y))
        ring.setPath_(Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(1, 1, 12, 12), None))
        ring.setFillColor_(None)
        ring.setLineWidth_(2.0)
        ring.setLineCap_(Quartz.kCALineCapRound)
        ring.setStrokeEnd_(0.7)
        ring.setHidden_(True)
        spin = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
        spin.setFromValue_(0.0)
        spin.setToValue_(-2 * math.pi)
        spin.setDuration_(0.9)
        spin.setRepeatCount_(float("inf"))
        ring.addAnimation_forKey_(spin, "spin")
        parent.addSublayer_(ring)
        self.ring = ring
        glyph = Quartz.CALayer.layer()
        glyph.setBounds_(Quartz.CGRectMake(0, 0, 15, 15))
        glyph.setPosition_(Quartz.CGPointMake(x, y))
        glyph.setHidden_(True)
        mask = Quartz.CALayer.layer()
        mask.setFrame_(Quartz.CGRectMake(0, 0, 15, 15))
        mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
        glyph.setMask_(mask)
        parent.addSublayer_(glyph)
        self.glyph, self.glyph_mask = glyph, mask

    # --- geometry -----------------------------------------------------------------------------

    def _moved(self) -> None:
        """Screens changed (a display plugged in, resolution): follow the notch."""
        self.screen, self.cx, self.top, self.nw, self.nh, self.real = geometry()
        self.panel.setFrame_display_(AppKit.NSMakeRect(self.cx - WIN_W / 2, self.top - WIN_H, WIN_W, WIN_H), True)

    def global_face(self):
        """The little Mint's centre in screen points (the HUD's orb_center while in notch mode)."""
        x, y = self._face_spot[:2] if self._face_spot[0] else self.face_center
        return self.cx - WIN_W / 2 + x, self.top - WIN_H + y

    def bottom(self) -> float:
        """The shape's bottom edge in screen points."""
        return self.top - self.size[1]

    def _rect_screen(self):
        w, h = self.size
        ear = getattr(self, "ear", EAR)
        return (self.cx - w / 2 - ear, self.top - h, w + 2 * ear, h)

    def _inside(self, point) -> bool:
        x, y, w, h = self._rect_screen()
        return x <= point.x <= x + w and y <= point.y <= y + h

    def _resize(self, w, h, animate=True, lead=False) -> None:
        w, h = round(w), round(h)
        if (w, h) == self.size:
            return
        # boring.notch's radii, growing with the shape on the same spring: closed ears 6 and bottom
        # 14 (like the hardware notch), open 19 and 24.
        opened = h > self.nh + 2
        ear, radius = (19.0, 24.0) if opened else (6.0, 14.0)
        self.ear = ear
        path = island_path(WIN_W, WIN_H, w, h, ear=ear, r=radius)
        ring, edge = rim_paths(WIN_W, WIN_H, w, h, ear=ear, r=radius)
        # An unhurried drop (kinetics "gentle": critically damped, ~0.45 s), nothing swinging out past where the
        # shape is going. Wrapping a card the shape leads ("snappy"), so the card never shows outside it while
        # both grow; folding back is quick too. The status rim and the sweep's track ride the same spring, so
        # they stay on the edge. (Reduce motion: kinetics makes it a short ease.)
        growing = w * h > self.size[0] * self.size[1] + 1
        preset = "snappy" if lead or not growing else "gentle"
        layers = ((self.shape, path), (self.mask, path), (self.rim, ring), (self.flash_rim, ring),
                  (self.sweep_track, edge))
        for layer, new in layers:
            old = (layer.presentationLayer() or layer).path() if animate else None
            if old is not None:
                kinetics.spring(layer, "path", old, new, preset, anim_key="path")
            else:
                layer.removeAnimationForKey_("path")
                Quartz.CATransaction.begin()
                Quartz.CATransaction.setDisableActions_(True)
                layer.setPath_(new)
                Quartz.CATransaction.commit()
        self.shape.setShadowOpacity_(0.7 if opened else 0.0)     # black 0.7, radius 6, only when open
        self.shape.setShadowRadius_(6)
        self.size = (w, h)
        if self._rim_status and self._rim_status[0] == "busy" and abs(w - self._sweep_w) > 4:
            self._sweep_travel()                    # the sweep crosses the new width
        if self._cd:
            self._place_cd_line()

    # --- the HUD's hooks ------------------------------------------------------------------------

    def show_words(self, shown, full) -> None:
        self.caption = (shown, full) if full is not None and full.length() else None

    def set_progress(self, fraction, label: str = "") -> None:
        """hud.progress: the small bar in the open notch; a long job (or one while the notch is open) gets the
        progress scene, Mint riding the bar, instead - quick jobs never fling the notch open."""
        self.progress = fraction
        now = time.monotonic()
        scene = self._scene_ctl("progress")
        if fraction is None:
            self._prog_since = 0.0
            if scene is not None:
                scene.finish(True)
                AppHelper.callLater(1.2, lambda: self._hide_scene("progress", scene))
            return
        self._prog_since = getattr(self, "_prog_since", 0.0) or now
        self._prog_at = now
        if scene is None and _live[0] and (self.st.is_open or self.mode == "home" or now - self._prog_since > 3.0):
            symbol = "sparkles"
            try:
                from mint.ui import activity as act
                kind = (getattr(self.hud, "_activity", None) or {}).get("kind", "")
                symbol = act.SYMBOLS.get(kind, "sparkles") if kind else symbol
            except Exception:
                pass
            from mint.ui import notch_fx
            scene = self._show_scene("progress", lambda w, h: notch_fx.progress_bar(w, h, label or "Working on it",
                                                                                      symbol))
        if scene is not None:
            scene.set(fraction, title=label or None)

    # --- scenes in the open notch's body (notch_fx, notch_composer) -------------------------------

    def _scene_ctl(self, kind: str):
        return self._scene[2] if self._scene is not None and self._scene[0] == kind else None

    def _show_scene(self, kind: str, make, w: float = HOME_W - 24, h: float = BODY_H):
        """One scene at a time in the body: make(w, h) -> (view, controller). It's added hidden; the tick reveals it
        (the notch opens into the home layout around it, the other panes step aside, the header stays)."""
        if self._scene is not None:
            if self._scene[0] == kind:
                return self._scene[2]
            self._hide_scene()
        try:
            view, ctl = make(w, h)
        except Exception:
            log.exception("notch scene %s failed", kind)
            return None
        view.setHidden_(True)
        self.box.addSubview_(view)
        self._scene = (kind, view, ctl)
        self._scene_since = time.monotonic()
        return ctl

    def _hide_scene(self, kind: str | None = None, ctl=None) -> None:
        """The scene goes (only the one asked for, if `kind`/`ctl` say which): content out, then closed."""
        scene = self._scene
        if scene is None or (kind is not None and scene[0] != kind) or (ctl is not None and scene[2] is not ctl):
            return
        self._scene = None
        _, view, ctl = scene
        self._conceal(view)

        def close():
            try:
                ctl.close()
            except Exception:
                log.debug("scene close failed", exc_info=True)
        AppHelper.callLater(0.2, close)

    def _scenes(self, now: float, mouse, inside: bool, state: str, alert) -> bool:
        """Start and end the scenes the tick looks after; True while one should show (an alert on show beats the
        greeting and the progress bar, not the composer, a drop or the error card)."""
        # The greeting: the first wake of the day.
        previous, self._last_state = getattr(self, "_last_state", state), state
        if state == "awake" and previous in ("sleeping", "starting") and self._scene is None:
            try:
                from mint.ui import notch_fx
                if notch_fx.should_greet():
                    holder = {}

                    def done():
                        self._hide_scene("greeting", holder.get("ctl"))
                    holder["ctl"] = self._show_scene("greeting", lambda w, h: notch_fx.greeting(w, h, on_done=done))
            except Exception:
                log.debug("greeting failed", exc_info=True)
        kind = self._scene[0] if self._scene is not None else None
        if kind == "greeting" and now - self._scene_since > 4.5:
            self._hide_scene("greeting")             # (in case its on_done never came)
        # The error card: a failed step, once the turn has settled (nothing running any more).
        failed = getattr(self, "_failed", None)
        if failed and state not in ("thinking", "working") and getattr(self.hud, "_activity", None) is None:
            self._failed = None
            if now - failed[0] < 8.0 and kind not in ("composer", "drop"):
                self.show_error("Mint couldn't finish that", failed[1], failed[2])
                kind = "error"
        if kind == "error":
            pressed = bool(AppKit.NSEvent.pressedMouseButtons() & 1)
            if now - self._scene_since > 8.0 or (pressed and not inside and now - self._scene_since > 0.4):
                self._hide_scene("error")            # 8 s, or a click somewhere else
        # The composer: empty, it goes with a click somewhere else or when the notch folds.
        if kind == "composer" and not self._composer_text() and now - self._scene_since > 0.4:
            pressed = bool(AppKit.NSEvent.pressedMouseButtons() & 1)
            if (pressed and not inside) or not self.st.is_open:
                self._hide_scene("composer")
        # The drop zone goes with the drag.
        if kind == "drop" and now > self.drag_until and not getattr(self, "_drop_done", False):
            self._hide_scene("drop")
        # A progress bar nobody updates any more (stopped mid-way).
        if kind == "progress" and now - getattr(self, "_prog_at", now) > 20.0 and state not in ("thinking", "working"):
            self.progress, self._prog_since = None, 0.0
            self._hide_scene("progress")
        if self._scene is None:
            return False
        return alert is None or self._scene[0] in ("composer", "drop", "error")

    def step_ended(self, ok: bool, text: str) -> None:
        """A step of Mint's turn ended (hud.activity_end). A failure is remembered with the user's words; the tick
        shows the error card once the turn settles. A later success in the same turn forgets it."""
        if ok:
            self._failed = None
            return
        words = " ".join(w[0] for w in getattr(getattr(self.hud, "_you", None), "words", []) or [])
        reason = f"{text} didn't work." if text else "Something went wrong on the way."
        self._failed = (time.monotonic(), reason, words.strip())

    def show_error(self, title: str, reason: str = "", request: str = "") -> None:
        """The error card: sad red Mint, the reason, a round retry that sends `request` again the way the chat
        does. A red rim flash and a shake. Goes after 8 s or a click elsewhere."""
        from mint.ui import notch_fx
        if self._scene_ctl("error") is not None:
            self._hide_scene("error")
        retry = None
        if request:
            def retry():
                self.hud._fire("submit", request)
                AppHelper.callLater(0.5, lambda: self._hide_scene("error"))
        self._show_scene("error", lambda w, h: notch_fx.error_card(w, h, title, reason, on_retry=retry))
        self.flash(gfx.RED, shake=True)

    def compose(self) -> None:
        """The header's "+": the island becomes an input. Enter sends to Mint like the chat does, then the notch
        goes back to its working card; Esc or "+" again puts it away."""
        if self._scene_ctl("composer") is not None:
            self._hide_scene("composer")
            return
        from mint.ui import notch_composer
        holder = {}

        def send(text):
            self.hud._fire("submit", text)
            self.tab = "home"                 # the working card (Mint's pane), whichever tab "+" was pressed on
            AppHelper.callLater(0.3, lambda: self._hide_scene("composer", holder.get("ctl")))

        def cancel():
            self._hide_scene("composer", holder.get("ctl"))

        def mic():
            prefs.toggle("mic")
        ctl = self._show_scene("composer", lambda w, h: notch_composer.composer(w, h, send, cancel, on_mic=mic))
        holder["ctl"] = ctl
        if ctl is None:
            return
        if not self.st.is_open:
            _sfx("open")
        self.st.open_by("click", time.monotonic())        # afterwards it stays open on the working card
        self.allow_key = True
        ctl.focus(self.panel)

    def _composing(self) -> bool:
        return self._scene is not None and self._scene[0] == "composer"

    def _composer_text(self) -> str:
        ctl = self._scene_ctl("composer") if self._scene is not None else None
        try:
            return (ctl.text() or "").strip() if ctl is not None else ""
        except Exception:
            return ""

    def _put_away_scene(self) -> None:
        """The user chose something else in the notch (a tab, the chat): the scenes they can dismiss go - the
        composer, the error card, the greeting."""
        if self._scene is not None and self._scene[0] in ("composer", "error", "greeting"):
            self._hide_scene()

    def poke_face(self, point) -> None:
        """A plain click on the little Mint: orb.poke with the offset from its centre (y up)."""
        poke = getattr(getattr(self, "orb", None), "poke", None)
        if poke is None or point is None:
            return
        x, y = self._face_spot[:2]
        try:
            poke(point.x - x, point.y - y)
        except Exception:
            log.debug("poke failed", exc_info=True)

    # --- every frame ---------------------------------------------------------------------------

    def tick(self) -> None:
        hud = self.hud
        if hud is None or not getattr(hud, "_built", False) or self.phase != "on":
            return                              # off, or a transition is steering the shape
        now = time.monotonic()
        mouse = AppKit.NSEvent.mouseLocation()
        if self._calm and not self._stirring(hud, mouse, now):
            # Nothing on the notch is moving and nothing is near: a full look 10 times a second is plenty.
            # Anything happening (the pointer coming close, Mint waking, words, a song) is back at 30.
            self._calm_skip = (self._calm_skip + 1) % 3
            if self._calm_skip:
                return
        self._calm_skip = 0
        island = self._island()
        inside = self._inside(mouse)
        near_notch = (abs(mouse.x - self.cx) < self.nw / 2 + WING and mouse.y > self.top - self.nh - 4)
        st = self.st
        if not self._agents_hooked:
            self._hook_agents()                 # coding agents: the wing, and their alerts (notch_state)
        if now - self._idle_at > 1.0:
            self._idle_at = now
            st.idle(_idle_seconds())            # away (3 min without input): news waits
        if self._agents_hooked and now - self._sync_at > 0.25:
            self._sync_agents(now)
        # notch_state keeps the timing: the peek's dwell (starting over while the pointer wanders), a notch you
        # opened folding 8 s after the pointer leaves or after a quiet minute under it, `armed`, the alerts.
        leave = _seconds("notch_close_after", AFTER_LEAVE)
        if st.T.get("after_leave") != leave:
            st.T["after_leave"] = st.T["countdown"] = leave
        st.pointer(inside or near_notch, now, (mouse.x, mouse.y))
        if st.is_open and (self._typing() or self._holding()):
            st.keep(now)                        # typing in Search, a menu, Quick Look or a share sheet from it
        # Resting on the small row's plain part (not on a button) for a moment is meant: it opens in full.
        # Passing over, reaching for a button, or a notch that just folded under the pointer never does.
        if inside and self.mode == "hover" and not st.is_open and st.armed:
            frame = self.panel.frame()
            px, py = mouse.x - frame.origin.x, mouse.y - frame.origin.y
            on_button = any(not b.isHidden() and AppKit.NSPointInRect(AppKit.NSMakePoint(px, py),
                                                                      AppKit.NSInsetRect(b.frame(), -5, -5))
                            for _, b in self.buttons)
            if on_button:
                self.inner_since = 0.0
            else:
                self.inner_since = max(self.inner_since or now, st.still_at)    # wandering starts it over
                if now - self.inner_since > _seconds("notch_open_after", INNER_OPEN):
                    self.inner_since = 0.0
                    st.open_by("hover", now)
                    _sfx("open")
        else:
            self.inner_since = 0.0
        # Two stages, so crossing the notch on the way somewhere doesn't throw the whole thing open:
        # a short rest shows the few controls that matter; staying on it (or a click) opens the full notch.
        peeking = st.peeking(now)
        hovering = st.is_open
        chat_open = getattr(getattr(hud, "chat", None), "is_open", False)
        state = getattr(hud, "_state", "")
        activity = getattr(hud, "_activity", None)
        said = getattr(getattr(hud, "_said", None), "words", None)
        you = getattr(getattr(hud, "_you", None), "words", None)
        # Only real words or a task open the shape: a bare status ("Mic off") stays an icon.
        caption = self.caption if (said or you or activity) and prefs.get("notch_words") is not False else None
        compact_w = self.nw + 2 * WING
        playful = prefs.get("notch_playful") is not False

        guests = self._guests(chat_open)
        shown_island = island is not None and island.shown and island.rect is not None
        if not self._search_hooked:
            self._hook_search()                 # Mint's file/app results open the notch on Search
        # One alert at a time, from the queue: needs-you first (held until answered), then done (5 s) / failed (8 s).
        alert = st.shown()
        self._follow_alert(alert)
        scene = self._scenes(now, mouse, inside, state, alert)    # greeting, error card, drop zone, progress, composer
        self._scene_shown = scene
        agent_push = alert is not None and not scene
        self._agent_push = agent_push
        if agent_push and self.tab != "agents" and not hovering:
            self.tab = "agents"
        music = self._music()                 # something playing: the notch becomes the player
        self._no_music_card(music)
        controls = prefs.get("notch_controls") is not False
        bar_info = self._bar_info()
        # What Mint just brought up (found files, a song starting) opens at once, even while it's still
        # talking: the notch grows into it for a few seconds, then folds back (hover brings it all back).
        pushed = (now < self.search_until and self.tab == "search" or agent_push and self.tab == "agents") \
            and not chat_open
        if shown_island or guests:
            mode = "wrap"
            width, height = self._wrap(island.rect if shown_island else None, guests, compact_w)
        elif scene:
            mode = "home"                       # a scene: the home layout around it, header on top
            width, height = HOME_W, self.nh + 8 + self._scene[1].frame().size.height + 10
        elif pushed and self._search_big():
            mode = "home"
            width, height = BIG_W, self.nh + 8 + BIG_H + 10
        elif pushed:
            mode = "home"
            width, height = HOME_W, self.nh + 8 + self._body_h() + 10
        elif music and now < self.music_peek_until and not hovering and not chat_open:
            mode = "music"
            width = max(compact_w, PLAYER_W + 20)
            height = self.nh + 4 + PLAYER_H + 10
        elif (caption or self.progress) and not chat_open:
            # Words and tasks come before the idle states: they take the player's place for a moment, then it returns.
            text_w, text_h = self._text_size(caption)
            if hovering and controls:
                mode = "home"                   # staying opens it all, Mint's words in its pane
                width, height = HOME_W, self.nh + 8 + self._body_h() + 10
            elif peeking and controls:
                mode = "hover"                  # a short rest: the words and the few controls
                width = max(compact_w, len(self._row()) * STEP + 20, text_w + 2 * PAD_X)
                height = self.nh + (text_h + 10 if caption else 4) + BTN + 14
            else:
                mode = "open"
                width = max(compact_w, text_w + 2 * PAD_X)
                height = self.nh + text_h + 18 + (8 if self.progress else 0)
                # Don't shrink for a few points ("Working." -> "Working..." made it wobble).
                if self.mode == "open" and self.size[1] == round(height) and 0 < self.size[0] - width < 24:
                    width = self.size[0]
        elif self._search_big() and not chat_open:
            mode = "home"                       # grown for search results: the grid
            width, height = BIG_W, self.nh + 8 + BIG_H + 10
        elif (hovering or now < self.drag_until or now < self.search_until or self._typing()) and controls \
                and not chat_open:
            mode = "home"                       # boring.notch's open notch: tabs, player or Mint, calendar, shelf
            width, height = HOME_W, self.nh + 8 + self._body_h() + 10
        elif peeking and controls and not chat_open and now >= self.battery_peek_until:
            mode = "hover"                      # a short rest on the notch: just the few controls
            width = max(compact_w, len(self._row()) * STEP + 20)
            height = self.nh + 4 + BTN + 14
        elif now < self.battery_peek_until and not chat_open:
            mode = "battery"                    # plugged in / unplugged: said in the wings for a moment
            width, height = self.nw + 2 * 92, self.nh
        elif self._emoting(now):
            mode = "emote"                      # an expression: room under the little Mint for its extras
            width, height = compact_w + 48, self.nh + 46
        elif bar_info is not None and not getattr(hud, "hidden", False) and state in ("sleeping", "awake", "paused") \
                and not activity and not chat_open:
            mode = "bar"                        # Mint idle, agents at work: their lead's step under the camera
            width, height = max(compact_w, self._bar_width(bar_info) + 16), self.nh + 30
        elif (prefs.get("notch_idle_face") is False or getattr(hud, "hidden", False)) \
                and state in ("sleeping", "awake", "paused") and not activity:
            mode = "plain"                      # just the notch until something happens
            width, height = self.nw + 8, self.nh    # a hair wider, so the black covers the hardware edge
        else:
            mode = "compact"
            width, height = compact_w, self.nh
        self._watch_drag(mouse)
        if playful and mode in ("open", "hover", "wrap", "music", "home", "bar") and self.mode in ("compact", "plain"):
            self.orb.hop()                      # the little Mint hops as the shape opens
        if mode != self.mode and mode in ("home", "hover", "open", "music", "bar"):
            self._open_started = now            # the shape starts to open: its content follows, staggered
        # Folding: the content goes first (~0.12 s), then the shape - never the black closing over content still
        # showing. Growing (and wrapping a guest, where the shape leads) is at once.
        shrinking = width * height < self.size[0] * self.size[1] - 1 and mode != "wrap"
        if not shrinking:
            self._hold_until = 0.0
        elif mode != self.mode and self.mode in ("home", "hover", "open", "music", "bar") and not self._hold_until:
            self._hold_until = now + CLOSE_LEAD
        if not (self._hold_until and now < self._hold_until):
            self._hold_until = 0.0
            self._resize(width, height, lead=(mode == "wrap"))
        self._breathe(state, mode, playful)
        self._mint_words = caption[1] if caption else ""
        self._layout_content(mode, caption, width, height)
        # The artwork and bars take the wings only while the song is what matters: Mint listening,
        # thinking or talking shows the little Mint again, and a paused song gives way after a while.
        busy = state in ("awake", "thinking", "working", "speaking") or bool(activity or said or you)
        if music and not music.get("playing"):
            self._paused_since = self._paused_since or now
        else:
            self._paused_since = 0.0
        stale = bool(self._paused_since) and now - self._paused_since > PAUSED_WINGS
        wings = bool(music) and mode in ("compact", "plain", "music") and (mode == "music" or not (busy or stale)) \
            and not (getattr(hud, "hidden", False) and mode == "plain")
        agent_wing = wings and mode != "music" and self._agent_wing()
        self._music_wings(music if wings else None, right=not agent_wing)
        self._home(mode, music)
        self._battery_wings(mode == "battery")
        self._music_player(mode == "music" or (mode == "home" and self.tab == "home" and bool(music)
                                               and not self._mint_words and not scene), height,
                           home=(mode == "home"))
        if mode == "battery":
            wings = True                        # the battery owns both wings for the moment
        self._mode_now = mode
        if not wings or agent_wing:
            self._indicator(state, activity, now)    # (with music on: the agent takes the right wing)
        else:
            self._wing_faces(False)
        self._agents_bar(mode == "bar", width)
        # One Mint on screen: a scene with its own (greeting, error, drop, progress) has the face for now.
        showing_face = mode != "plain" and not wings and not (scene and self._scene[0] != "composer")
        # The little Mint, a shared element: grown into the Mint pane while it shows, back in the wing otherwise.
        self._place_face("pane" if mode == "home" and getattr(self, "_mint_pane_on", False) else "wing")
        self._mic_badge(showing_face and self._face_where == "wing" and not prefs.get("mic") and activity is None
                        and state not in ("speaking", "thinking", "working"))
        self._show_status(state, activity, alert, mode)
        self._countdown(now, mode)
        self._calm = (mode in ("compact", "plain") and not music and not activity and not caption and not guests
                      and not shown_island and not agent_push and not pushed and not self.progress
                      and not st.is_open and not st.hover and state in ("sleeping", "paused", "offline"))
        if (self.face_host.opacity() > 0.5) != showing_face:
            Quartz.CATransaction.begin()
            # Gone at once when something else takes the wing (the battery's words were drawn under a
            # face still fading out); back with a short fade.
            Quartz.CATransaction.setDisableActions_(not showing_face)
            Quartz.CATransaction.setAnimationDuration_(0.2)
            self.face_host.setOpacity_(1.0 if showing_face else 0.0)
            Quartz.CATransaction.commit()
        # Clicks only on the shape itself; everywhere else the window is air. (Only when it changes: telling the
        # window server every frame, with the pointer on the shape, could leave the pointer hidden.)
        if self._mouse_through != (not inside):
            self._mouse_through = not inside
            self.panel.setIgnoresMouseEvents_(not inside)
        self.mode = mode

    def _stirring(self, hud, mouse, now: float) -> bool:
        """Cheap: is anything starting that the notch must follow at full speed?"""
        if getattr(hud, "_state", "") not in ("sleeping", "paused", "offline") or getattr(hud, "_activity", None):
            return True
        if getattr(getattr(hud, "_said", None), "words", None) or getattr(getattr(hud, "_you", None), "words", None):
            return True
        if abs(mouse.x - self.cx) < self.nw / 2 + WING + 160 and mouse.y > self.top - self.nh - 160:
            return True                         # the pointer is coming (hover must feel instant)
        if now < max(self.battery_peek_until, self.search_until, self.drag_until, self.music_peek_until):
            return True
        st = getattr(self, "st", None)
        if st is not None and st.next_wake(now) < 0.2:
            return True                         # an alert is about to show, run out or fold the notch
        try:
            from mint.ui.emotes import emotes
            return now - getattr(emotes, "last_played", 0.0) < 4.0
        except Exception:
            return False

    def _emoting(self, now) -> bool:
        try:
            from mint.ui.emotes import EMOTES, emotes
            last = getattr(emotes, "last_played", 0.0)
            key = getattr(emotes, "_last_key", "")
            length = EMOTES.get(key, (2.6,))[0] if key else 2.6
            return emotes.orb is self.orb and now - last < min(length, 4.0)
        except Exception:
            return False

    def _island(self):
        try:
            from mint.ui import island
            return island.island if getattr(island.island, "panel", None) is not None else None
        except Exception:
            return None

    # --- the notch becomes the card ---------------------------------------------------------

    def _guests(self, chat_open) -> list:
        """Mint's windows that hang under the notch right now: the chat, the clipboard, the
        image card. The notch grows around them, so it becomes them rather than a card
        floating below it."""
        import sys
        found = []
        if chat_open and getattr(self.hud.chat, "window", None) is not None:
            found.append(("chat", self.hud.chat.window))
        for module, attr in (("mint.ui.clipboard_window", "window"), ("mint.ui.image_card", "card"), ("mint.core.guard", "card")):
            owner = getattr(sys.modules.get(module), attr, None)
            panel = getattr(owner, "panel", None)
            if panel is not None and panel.isVisible() and panel.alphaValue() > 0.3:
                found.append((module, panel))
        for key, window in found:
            self._dress(key, window)
            owner = getattr(sys.modules.get(key), "window" if key.endswith("clipboard_window") else "card", None)
            host = getattr(owner, "face_host", None)
            if host is not None and not host.isHidden():
                host.setHidden_(True)            # the notch has its own little Mint: one face, not two
                self._faces.append(host)
        return [window for _, window in found]

    def _dress(self, key, window) -> None:
        """Once per window: above the black shape, no shadow of its own, black like the notch."""
        ident = id(window)
        if ident in self._styled:
            return
        layer = window.contentView().layer() if key != "chat" else None
        self._styled[ident] = (window, key, window.level(), window.hasShadow(), window.appearance(),
                               layer.backgroundColor() if layer is not None else None,
                               layer.borderWidth() if layer is not None else 0.0)
        try:
            window.setLevel_(LEVEL + 1)
            window.setHasShadow_(False)
            if key == "chat":
                window.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
                # Notch-black under the chat's content (its own grey glass read as a box inside the notch).
                # A layer just under the header dot: above the glass's own blur, below everything drawn on it.
                content = window.contentView()
                back = Quartz.CALayer.layer()
                back.setFrame_(content.bounds())
                back.setAutoresizingMask_(Quartz.kCALayerWidthSizable | Quartz.kCALayerHeightSizable)
                back.setBackgroundColor_(AppKit.NSColor.blackColor().CGColor())
                back.setCornerRadius_(20)
                dot = getattr(self.hud.chat, "_dot", None)
                if dot is not None and dot.superlayer() is content.layer():
                    content.layer().insertSublayer_below_(back, dot)
                else:
                    content.layer().insertSublayer_atIndex_(back, 0)
                self._backs[ident] = back
            else:
                layer = window.contentView().layer()
                if layer is not None:
                    layer.setBackgroundColor_(AppKit.NSColor.blackColor().CGColor())
                    layer.setBorderWidth_(0.0)
        except Exception:
            log.debug("could not dress a window for the notch", exc_info=True)

    def undress(self) -> None:
        """Leaving the notch: every dressed window goes back to how it was."""
        for window, key, level, shadow, appearance, background, border in self._styled.values():
            try:
                window.setLevel_(level)
                window.setHasShadow_(shadow)
                window.setAppearance_(appearance)
                layer = window.contentView().layer() if key != "chat" else None
                if layer is not None:
                    layer.setBackgroundColor_(background)
                    layer.setBorderWidth_(border)
            except Exception:
                log.debug("could not undress a window", exc_info=True)
        for back in self._backs.values():
            back.removeFromSuperlayer()
        self._backs.clear()
        for host in self._faces:
            host.setHidden_(False)
        self._faces.clear()
        self._styled.clear()
        self.panel.setIgnoresMouseEvents_(True)
        self._mouse_through = True

    def _wrap(self, island_rect, guests, compact_w):
        """Stack the island's scene and the guests under the notch (never on top of each other)
        and return the shape's size that wraps them all."""
        rects = []
        floor = self.screen.visibleFrame().origin.y + 8
        top_row = self.top - self.nh - 6
        below = top_row
        if island_rect is not None:
            rects.append(island_rect)
            below = island_rect[1] - 8
        placed = []
        for window in sorted(guests, key=lambda w: -w.frame().origin.y):
            f = window.frame()
            x, y, w, h = f.origin.x, f.origin.y, f.size.width, f.size.height
            overlaps = any(x < px + pw and px < x + w and y < py + ph and py < y + h for px, py, pw, ph in placed)
            if y + h > below + 1 or overlaps:
                if below - h >= floor and not placed:
                    y = below - h                    # room below what is above: hang under it
                else:
                    # No room below: beside the previous card, top-aligned, never off the screen.
                    px, py, pw, ph = placed[-1] if placed else (self.cx - w / 2, below - h, w, h)
                    x = px + pw + 12 if px + pw + 12 + w <= self.cx + WIN_W / 2 - EAR - 20 else px - 12 - w
                    y = max(floor, (py + ph) - h)
                window.setFrameOrigin_(AppKit.NSMakePoint(x, y))
            placed.append((x, y, w, h))
            rects.append((x, y, w, h))
            below = min(below, y - 8)
        reach = max(max(abs(x - self.cx), abs(x + w - self.cx)) for x, y, w, h in rects)
        width = max(compact_w, 2 * reach + 16)
        height = self.top - min(y for x, y, w, h in rects) + 8
        return min(width, WIN_W - 2 * EAR - 2), min(height, WIN_H)

    # --- the notch becomes the music player ---------------------------------------------------

    def _music(self):
        """What is playing (music_player.compact_info), or None when nothing is."""
        import sys
        player = sys.modules.get(f"{__package__}.music_player")
        if player is None or prefs.get("notch_music") is False:
            return None
        try:
            info = player.compact_info(self.nh - 10)
        except Exception:
            log.debug("music info failed", exc_info=True)
            return None
        return info if info.get("show") else None

    # --- the open notch: tabs, Mint or the player, the calendar, the shelf ---------------------

    def _mod(self, name):
        try:
            import importlib
            # By this package, not "mint.": the packaged app keeps these in mint.ui (restructure.py), and a flat
            # "mint.ui.notch_shelf" there is no module - the open notch lost Search, the shelf and the calendar.
            return importlib.import_module(f"{__package__}.{name}")
        except Exception:
            log.debug("no %s", name, exc_info=True)
            return None

    def _build_home(self) -> None:
        """Once, the first time the notch opens: the tab row, the header's battery and gear, the Mint
        pane, the calendar (or battery) pane and the shelf. Each module's view is built once and kept."""
        h = {"acts": []}
        top_y = WIN_H - self.nh / 2
        left = WIN_W / 2 - HOME_W / 2 + 16
        right = WIN_W / 2 + HOME_W / 2 - 16

        def button(symbol, tip, fn, x, y, size=24):
            act = MintNotchAct.alloc().initWithFn_(fn)
            h["acts"].append(act)
            b = MintNotchButton.buttonWithImage_target_action_(gfx.symbol(symbol, 11), act, "fire:")
            b.setBordered_(False)
            b.setToolTip_(tip)
            b.setContentTintColor_(_white(0.8))
            b.setWantsLayer_(True)
            b.layer().setCornerRadius_(size / 2 - 2)
            b.setFrame_(AppKit.NSMakeRect(x, y - size / 2, size + 8, size))
            b.setHidden_(True)
            self.box.addSubview_(b)
            return b
        h["tabs"] = {"home": button("house.fill", "Home", lambda: self._set_tab("home"), left, top_y),
                     "search": button("magnifyingglass", "Search files and apps", lambda: self._set_tab("search"),
                                      left + 36, top_y),
                     "shelf": button("tray.full.fill", "Shelf: files to AirDrop or share", lambda: self._set_tab("shelf"),
                                     left + 72, top_y)}
        h["tabs"]["agents"] = button("sparkles", "Coding agents: Claude Code and Codex", lambda: self._set_tab("agents"),
                                     left + 108, top_y)
        h["tabs"]["compose"] = button("plus", "Type to Mint here (Enter sends, Esc cancels)", self.compose,
                                      left + 144, top_y)
        h["gear"] = button("gearshape.fill", "Menu and Settings", self._menu_from_button, right - 32, top_y)
        h["chat"] = button("bubble.left.and.bubble.right.fill", "Open the chat", self._chat, right - 64, top_y)
        battery = self._mod("notch_battery") if prefs.get("notch_battery") is not False else None
        h["badge"] = None
        if battery is not None and battery.available():
            try:
                badge = battery.compact_badge(20)
                badge.setHidden_(True)
                self.box.addSubview_(badge)
                h["badge"] = badge
                battery.charging_changed(self.peek_battery)
            except Exception:
                log.debug("battery badge failed", exc_info=True)
        # The Mint pane (no music): what Mint is doing and how to reach it; the controls go under it.
        h["title"] = self._label(15, _white(0.95), AppKit.NSFontWeightSemibold)
        h["hint"] = self._label(11.5, _white(0.55), AppKit.NSFontWeightMedium, lines=2)
        # The right pane: the week calendar if Mint may read calendars, else the battery.
        h["side"] = h["side_kind"] = None
        h["tips"] = None                    # "Set up" chips under the hint, built when there is one (notch_tips)
        calendar = self._mod("notch_calendar") if prefs.get("notch_calendar") is not False else None
        try:
            if calendar is not None and calendar.available():
                h["side"], h["side_update"] = calendar.view(SIDE_W, BODY_H - 16)
                h["side_kind"] = "calendar"
            elif battery is not None and battery.available():
                h["side"], h["side_update"] = battery.view(SIDE_W, BODY_H - 16)
                h["side_kind"] = "battery"
        except Exception:
            log.debug("side pane failed", exc_info=True)
        if h["side"] is not None:
            h["side"].setHidden_(True)
            self.box.addSubview_(h["side"])
        # The shelf.
        h["shelf"] = None
        shelf = self._mod("notch_shelf") if prefs.get("notch_shelf") is not False else None
        if shelf is not None:
            try:
                h["shelf"], h["shelf_update"] = shelf.view(HOME_W - 24, BODY_H)
                h["shelf"].setHidden_(True)
                self.box.addSubview_(h["shelf"])
                shelf.on_change(lambda: setattr(self, "_shelf_dirty", True))
            except Exception:
                log.debug("shelf failed", exc_info=True)
        h["agents"] = None                  # built the first time the tab shows (notch_agents)
        self.home = h
        self.root.registerForDraggedTypes_([AppKit.NSPasteboardTypeFileURL])

    def _repaint_soon(self, update) -> None:
        """The modules paint only while visible: ask again just after the view is shown."""
        if update is None:
            return

        def paint():
            try:
                update()
            except Exception:
                log.debug("notch pane repaint failed", exc_info=True)
        AppHelper.callLater(0.05, paint)
        AppHelper.callLater(0.4, paint)

    def _set_tab(self, tab: str) -> None:
        self._put_away_scene()
        self.tab = tab
        if tab == "search":
            self._focus_search()

    # --- Claude mode: coding agents in the notch (notch_agents) ------------------------------------

    def _agents_mod(self):
        if prefs.get("agent_mode") == "off":
            return None
        mod = self._mod("notch_agents")
        try:
            return None if mod is None or mod.mode() == "off" else mod   # (no Claude Code or Codex on this Mac)
        except Exception:
            return mod

    def _agents_ok(self) -> bool:
        mod = self._agents_mod()
        try:
            return mod is not None and mod.available()
        except Exception:
            return False

    def _hook_agents(self) -> None:
        mod = self._agents_mod()
        if mod is None or self._agents_hooked:
            return
        self._agents_hooked = True

        def event(kind, session):
            # The agents' moments become alerts, shown one at a time (notch_state): needs-you first, held until
            # answered; then "done" (5 s) and "failed" (8 s), each timed from when it shows. The wing tells the rest.
            now = time.monotonic()
            key = getattr(session, "key", None)
            if not key:
                return
            if kind in ("waiting", "asking"):
                if prefs.get("agent_approvals") is not False:
                    self.st.alert("need:" + key, "need", now, session=key)
            elif kind in ("finished", "failed"):
                self.st.resolve(now, session=key, kind="need")
                if prefs.get("agent_open_on_done") is not False:
                    news = "done" if kind == "finished" else "error"
                    self.st.alert(f"{news}:{key}:{getattr(session, 'since', now)}", news, now, session=key)
            elif kind == "started":
                self.st.resolve(now, session=key)        # it moved on: its old news isn't worth showing
                if getattr(self, "orb", None) is not None and self.mode in ("compact", "plain") and mod.live():
                    self.orb.hop()
        try:
            mod.on_event(event)
        except Exception:
            log.debug("agents hook failed", exc_info=True)

    def _sync_agents(self, now: float) -> None:
        """A few times a second: the sessions that need you (also those asking from before Mint started) are
        alerts, and the ones answered are resolved."""
        self._sync_at = now
        try:
            mod = self._agents_mod()
            self._wing = mod.wing() if mod is not None else None     # (the rim and the glow read it)
        except Exception:
            self._wing = None
        keys = set()
        if self._agents_mod() is not None and prefs.get("agent_approvals") is not False:
            try:
                from mint.tools import agent_watch
                keys = {s.key for s in agent_watch.sessions() if s.approval or s.state in ("waiting", "asking")}
            except Exception:
                return
        self.st.sync_needs(keys, now)

    def _follow_alert(self, alert) -> None:
        """A new alert on show: the Agents tab on its session; done and failed flash the rim and sound once."""
        ident = alert.id if alert is not None else None
        if ident == self._alert_id:
            return
        had, self._alert_id = self._alert_id, ident
        try:
            mod = self._agents_mod()
        except Exception:
            mod = None
        if alert is None:
            # The queue is empty: the Agents tab goes back to its own order.
            release = getattr(mod, "release", None)
            if had is not None and release is not None:
                try:
                    release()
                except Exception:
                    log.debug("agents release failed", exc_info=True)
            return
        if alert.session and mod is not None:
            try:
                mod.focus(alert.session)            # held on this session until the next alert or release()
            except Exception:
                log.debug("agents focus failed", exc_info=True)
        if alert.kind == "done":
            self.flash(gfx.GREEN)
            _sfx("done")
        elif alert.kind == "error":
            self.flash(gfx.RED, shake=True)
            _sfx("error")

    def _agents_pane(self, on: bool, body_top: float) -> None:
        h = self.home
        view = h.get("agents")
        if on and view is None:
            mod = self._agents_mod()
            if mod is None:
                return
            try:
                view, update = mod.view(HOME_W - 24, AGENT_H)
                view.setHidden_(True)
                self.box.addSubview_(view)
                h["agents"], h["agents_update"] = view, update
            except Exception:
                log.debug("agents view failed", exc_info=True)
                return
        if view is None:
            return
        if on:
            height = self._body_h()
            pane = getattr(view, "pane", None)
            if pane is not None:
                pane.set_height(height)
            if view.isHidden():
                self._repaint_soon(h.get("agents_update"))
            self._reveal(view, AppKit.NSMakeRect(WIN_W / 2 - (HOME_W - 24) / 2, body_top - height, HOME_W - 24,
                                                 height), delay=0.06)
        elif not view.isHidden():
            self._conceal(view)

    def _agents_open(self) -> None:
        """"Claude mode" (by voice, or the menu): the full notch, on the Agents tab. Asked by
        voice the pointer is elsewhere: it folds 8 s later unless the pointer comes (notch_state)."""
        self.tab = "agents"
        self._agents_asked = True
        if not self.st.is_open:
            _sfx("open")
        self.st.open_by("voice", time.monotonic())

    def _agents_mini(self) -> bool:
        """The Agents tab as one line: when minimized, and when it opened by itself (an agent finished or asks)
        rather than by a click."""
        return bool(prefs.get("agent_compact")) or (getattr(self, "_agent_push", False) and not self.pinned)

    def _body_h(self) -> float:
        if self.tab != "agents":
            return BODY_H
        return AGENT_MINI_H if self._agents_mini() else AGENT_H

    # --- Spotlight in the notch (notch_search) -----------------------------------------------------

    def _search_mod(self):
        if prefs.get("notch_search") is False:
            return None
        return self._mod("notch_search")

    def _search_ok(self) -> bool:
        mod = self._search_mod()
        try:
            return mod is not None and mod.available()
        except Exception:
            return False

    def _search_big(self) -> bool:
        mod = self._search_mod()
        try:
            return self.tab == "search" and mod is not None and mod.want_expanded() and \
                (self.st.hover or self._typing() or time.monotonic() < self.search_until)
        except Exception:
            return False

    def _typing(self) -> bool:
        if self._composing() and self.panel.isKeyWindow() and self._composer_text():
            return True                     # words typed: don't fold under them (an empty box doesn't hold it)
        return self.tab == "search" and (self.panel.isKeyWindow() or self._holding())

    def _search_dragging(self) -> bool:
        mod = self._mod("notch_search") if f"{__package__}.notch_search" in __import__("sys").modules else None
        try:
            return bool(mod is not None and mod.dragging())
        except Exception:
            return False

    def _holding(self) -> bool:
        """Something opened from the notch is up (Quick Look, a menu, a share sheet): don't close under it."""
        if getattr(self, "_menus", 0) > 0 or self._search_dragging():
            return True
        if time.monotonic() < getattr(self, "_tip_hold", 0.0):
            return True                         # a "Set up" chip waits for your answer in macOS's box
        composer = self._scene_ctl("composer") if getattr(self, "_scene", None) is not None else None
        try:
            if composer is not None and composer.holding():
                return True                     # its Open panel (attach) is up
        except Exception:
            pass
        try:
            import Quartz as _q  # noqa: F401
            from Quartz import QLPreviewPanel
            if QLPreviewPanel.sharedPreviewPanelExists() and QLPreviewPanel.sharedPreviewPanel().isVisible():
                return True
        except Exception:
            pass
        for window in AppKit.NSApplication.sharedApplication().windows():
            if window.isSheet() and window.isVisible():
                return True
        return False

    def _hook_search(self) -> None:
        mod = self._search_mod()
        if mod is None or self._search_hooked:
            return
        self._search_hooked = True

        def changed(*_):
            # Mint showed files or apps: open on the Search tab for a while (the pointer or typing keeps it).
            try:
                if mod.has_results():
                    self.tab = "search"
                    self.search_until = time.monotonic() + 15.0
            except Exception:
                pass
        try:
            mod.on_change(changed)
        except Exception:
            log.debug("search hook failed", exc_info=True)

    def _search_pane(self, on: bool, body_top: float) -> None:
        """One search view, resized in place between the row (616x150) and the grid (736x430), so what
        is typed, the selection and the focus survive "Show more"."""
        mod = self._search_mod()
        if mod is None:
            self.allow_key = self._composing()
            if not self.allow_key and self.panel.isKeyWindow():
                self.panel.resignKeyWindow()
            return
        self._hook_search()
        h = self.home
        view = h.get("search")
        if on and view is None:
            try:
                view, update = mod.view(HOME_W - 24, BODY_H)
                view.setHidden_(True)
                self.box.addSubview_(view)
                h["search"], h["search_update"], h["search_size"] = view, update, (HOME_W - 24, BODY_H)
            except Exception:
                log.debug("search view failed", exc_info=True)
                return
        if view is not None:
            if on:
                size = (BIG_W - 24, BIG_H) if self._search_big() else (HOME_W - 24, BODY_H)
                if size != h.get("search_size"):
                    try:
                        mod.resize(view, *size)
                    except Exception:
                        log.debug("search resize failed", exc_info=True)
                    h["search_size"] = size
                    self._repaint_soon(h.get("search_update"))
                if view.isHidden():
                    self._repaint_soon(h.get("search_update"))
                self._reveal(view, AppKit.NSMakeRect(WIN_W / 2 - size[0] / 2, body_top - size[1], *size), delay=0.06)
            elif not view.isHidden():
                self._conceal(view)
        composing = self._composing()
        self.allow_key = on or composing           # (the composer's field takes typing too)
        if not on and not composing and self.panel.isKeyWindow():
            self.panel.resignKeyWindow()

    def _focus_search(self) -> None:
        mod = self._search_mod()
        if mod is None:
            return
        self.allow_key = True

        def focus():
            h = self.home or {}
            view = h.get("search")
            if view is None:
                return
            self.panel.makeKeyWindow()
            if not self.panel.isKeyWindow():             # a background app can be refused: ask to come forward
                AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                self.panel.makeKeyWindow()
            try:
                mod.focus_field(view)
            except Exception:
                log.debug("search focus failed", exc_info=True)
        AppHelper.callLater(0.08, focus)

    def _shelf_ok(self) -> bool:
        shelf = self._mod("notch_shelf") if prefs.get("notch_shelf") is not False else None
        try:
            return shelf is not None and (shelf.available() or time.monotonic() < max(
                self.drag_until, getattr(self, "_tips_shelf_until", 0.0)))   # (the "AirDrop" tip: an empty shelf)
        except Exception:
            return False

    def _home(self, mode, music) -> None:
        on = mode == "home"
        if on and self.home is None:
            self._build_home()
        h = self.home
        if h is None:
            return
        shelf_ok = self._shelf_ok()
        agents_ok = self._agents_ok()
        if (self.tab == "shelf" and not shelf_ok) or (self.tab == "search" and not self._search_ok()) or \
                (self.tab == "agents" and not agents_ok):
            self.tab = "home"
        now = time.monotonic()
        if on and not self.mode == "home" and now >= self.drag_until and now >= self.search_until \
                and not self._typing() and self.st.shown() is None \
                and not getattr(self, "_agents_asked", False):
            # A fresh open starts at home (a drag, results or an agent pick the tab); in Claude mode, on the
            # agents while one is at work.
            mod = self._agents_mod() if agents_ok else None
            claude = mod is not None and (mod.mode() == "on" or mod.live() and mod.busy())
            self.tab = "agents" if claude else "home"
            first = prefs.get("notch_open_to")          # Settings ▸ Appearance & Sound: the tab it opens to
            if first in ("home", "search", "shelf", "agents") and {
                    "home": True, "search": self._search_ok(), "shelf": shelf_ok, "agents": agents_ok}[first]:
                self.tab = first
        self._agents_asked = False
        body_top = WIN_H - self.nh - 8
        body_bottom = body_top - BODY_H
        left = WIN_W / 2 - HOME_W / 2 + 16
        right = WIN_W / 2 + HOME_W / 2 - 16
        top_y = WIN_H - self.nh / 2
        # header (it arrives last as the notch opens)
        search_ok = self._search_ok()
        composer = prefs.get("notch_composer") is not False
        slot = 0
        for name, b in h["tabs"].items():
            show = on and {"home": shelf_ok or search_ok or agents_ok or composer, "shelf": shelf_ok,
                           "search": search_ok, "agents": agents_ok, "compose": composer}[name]
            if show:                                # side by side, no gaps for the tabs that don't apply
                if abs(b.frame().origin.x - (left + 36 * slot)) > 0.5:
                    b.setFrameOrigin_(AppKit.NSMakePoint(left + 36 * slot, b.frame().origin.y))
                slot += 1
            self._top(b, show)
            if show:
                picked = self._composing() if name == "compose" else (self.tab == name and not self._scene_shown)
                b.layer().setBackgroundColor_(_white(0.16 if picked else 0.0).CGColor())
                b.setContentTintColor_(_white(0.95 if picked else 0.55))
        self._top(h["gear"], on)
        self._top(h["chat"], on)
        badge = h["badge"]
        if badge is not None:
            size = badge.frame().size
            badge.setFrameOrigin_(AppKit.NSMakePoint(right - 72 - size.width, top_y - size.height / 2))
            self._top(badge, on)
        # "+2 waiting": alerts queued behind the one on show (notch_state)
        waiting = self.st.queued() if on else 0
        if waiting and h.get("queued") is None:
            h["queued"] = self._label(11, gfx.ns(AMBER, 0.9), AppKit.NSFontWeightSemibold)
        if h.get("queued") is not None:
            if waiting:
                after = max([AppKit.NSMaxX(b.frame()) for b in h["tabs"].values() if not b.isHidden()] or [left])
                h["queued"].setStringValue_(f"+{waiting} waiting")
                self._reveal(h["queued"], AppKit.NSMakeRect(after + 8, top_y - 8, 110, 16), delay=0.2, style="fade")
            else:
                self._fade(h["queued"], False)
        # body: a scene takes it whole (the other panes step aside), else the tab
        scene = self._scene if self._scene_shown else None
        body = on and scene is None
        if self._scene is not None:
            view = self._scene[1]
            if on and scene is not None:
                size = view.frame().size
                self._reveal(view, AppKit.NSMakeRect(WIN_W / 2 - size.width / 2, body_top - size.height,
                                                     size.width, size.height), delay=0.06)
            elif not view.isHidden():
                self._conceal(view)                 # (kept: an alert or the chat has the notch for now)
        # body: home tab
        home = body and self.tab == "home"
        words = getattr(self, "_mint_words", "")
        mint_pane = home and (not music or bool(words))       # Mint talking beats the player
        self._mint_pane_on = mint_pane
        if mint_pane:
            from mint.ui.hud import TITLES
            state = getattr(self.hud, "_state", "")
            title = "Mic off" if not prefs.get("mic") else (TITLES.get(state, "") or "Listening")
            # The "Set up" chips talk here: what a chip is for (pointer on it), what to do in macOS's box, "connected".
            note = None if words else self._tip_words(time.monotonic())
            if note is not None and note[0]:
                title = note[0]
            from mint.voice.wake import display_phrase
            hint = words or (note[1] if note is not None else None) or (
                f"Say “{display_phrase()}”, or press the chat button." if prefs.get("mic")
                else "Press the chat button, or turn the mic on below.")
            said = getattr(self, "_pane_said", None)
            if not words and said and (title, hint) != said and not h["hint"].isHidden() \
                    and h["hint"].alphaValue() > 0.5:
                if h["hint"].layer() is not None:
                    kinetics.wipe_in(h["hint"].layer(), 0.28)          # new words wipe in over the old
                if title != said[0] and h["title"].layer() is not None:
                    kinetics.wipe_in(h["title"].layer(), 0.28)
            self._pane_said = (title, hint)
            h["title"].setStringValue_(title)
            h["hint"].setMaximumNumberOfLines_(3 if words else 2)
            h["hint"].setTextColor_(_white(0.85 if words or note is not None else 0.55))
            h["hint"].setStringValue_(hint)
            # The little Mint flies in on the left (_place_face); the words sit beside it and wipe in.
            tx = left + FACE * PANE_FACE + 14
            self._reveal(h["title"], AppKit.NSMakeRect(tx, body_top - 34, left + PLAYER_W - tx, 22),
                         delay=0.16, style="wipe")
            self._reveal(h["hint"], AppKit.NSMakeRect(tx, body_top - 84, left + PLAYER_W - tx, 46) if words
                         else AppKit.NSMakeRect(tx, body_top - 70, left + PLAYER_W - tx, 32), delay=0.2, style="wipe")
        else:
            for key in ("title", "hint"):
                self._fade(h[key], False)
        idle = mint_pane and not words and getattr(self.hud, "_activity", None) is None and \
            getattr(self.hud, "_state", "") not in ("thinking", "working", "speaking")
        self._tips(idle, left, body_top)
        side = h["side"]
        if side is not None:
            if home:
                if side.isHidden():
                    self._repaint_soon(h.get("side_update"))
                self._reveal(side, AppKit.NSMakeRect(right - SIDE_W, body_bottom + 8, SIDE_W, BODY_H - 16), delay=0.08)
            elif not side.isHidden():
                self._conceal(side)
        self._search_pane(body and self.tab == "search", body_top)
        self._agents_pane(body and self.tab == "agents", body_top)
        # body: shelf tab
        shelf = h["shelf"]
        if shelf is not None:
            if body and self.tab == "shelf":
                if getattr(self, "_shelf_dirty", False) or shelf.isHidden():
                    self._shelf_dirty = False
                    self._repaint_soon(h.get("shelf_update"))
                self._reveal(shelf, AppKit.NSMakeRect(WIN_W / 2 - (HOME_W - 24) / 2, body_bottom, HOME_W - 24, BODY_H),
                             delay=0.06)
            elif not shelf.isHidden():
                self._conceal(shelf)

    # --- "Set up" chips on the home tab: what isn't set up yet (notch_tips) ------------------------------

    def _tips(self, on: bool, left: float, body_top: float) -> None:
        """Between the hint and the controls, while Mint is idle on the home tab: a chip per thing not set up
        (Calendars, Claude Code, the shelf...). Statuses are read off the main thread every few seconds. A chip
        just set up stays a moment longer, green with a tick, before it shrinks away."""
        h = self.home
        tips = self._mod("notch_tips") if on else None
        keys = []
        if tips is not None:
            now = time.monotonic()
            self._tips_check(now)
            keys = tips.pick(getattr(self, "_tips_facts", None) or {}, tips.dismissed())
            party = {k for k, until in self._tip_party.items() if now < until}
            if party:
                keys = [k for k, *_ in tips.TIPS if k in party or k in keys]
        strip = h.get("tips")
        if keys and strip is None:
            try:
                strip = h["tips"] = tips.Strip(PLAYER_W + 4)      # (the calendar pane starts 12 pt further)
                strip.on_hover = self._tip_hovered
                strip.view.setHidden_(True)
                self.box.addSubview_(strip.view)
            except Exception:
                log.debug("notch tips failed", exc_info=True)
                return
        if strip is None:
            return
        if keys:
            strip.set(keys, lambda key: AppHelper.callAfter(self._tip_clicked, key),
                      lambda key: AppHelper.callAfter(self._tip_closed, key))
            self._reveal(strip.view, AppKit.NSMakeRect(left, body_top - 94, PLAYER_W + 4, strip.view.frame().size.height),
                         delay=0.24, style="fade")
        elif not strip.view.isHidden():
            self._conceal(strip.view)

    def _tips_check(self, now: float, force: bool = False) -> None:
        """Read what is set up on a thread (permissions, Claude Code's hooks, the shelf), at most every 4 s."""
        if getattr(self, "_tips_busy", False) or (not force and now - getattr(self, "_tips_at", -99.0) < 4.0):
            return
        self._tips_busy, self._tips_at = True, now
        tips = self._mod("notch_tips")

        def read():
            try:
                facts = tips.facts()
            except Exception:
                log.debug("notch tips: statuses", exc_info=True)
                facts = None

            def done():
                self._tips_busy = False
                if facts is not None:
                    self._tips_facts = facts
                    if facts.get("calendar") == "allowed":
                        self._side_to_calendar()
            AppHelper.callAfter(done)
        import threading
        threading.Thread(target=read, daemon=True, name="notch-tips").start()

    def _tip_clicked(self, key: str) -> None:
        """A chip: it starts waiting (a spinning ring, the Mint pane says what to do in macOS's box) and the notch
        stays open while you answer; the answer is looked for twice a second, and lands as a green tick."""
        tips = self._mod("notch_tips")
        if tips is None:
            return
        now = time.monotonic()
        strip = (self.home or {}).get("tips")
        if key in tips.PERMISSION_TIPS or key == "claude":
            if key in self._tip_wait:
                return                          # already waiting for this one
            prompt = tips.will_prompt(key) if key != "claude" else True
            self._tip_wait[key] = now
            (self._tip_prompted.add if prompt else self._tip_prompted.discard)(key)
            self._tip_say = (*tips.waiting_note(key, prompt), now + self.TIP_WAIT)
            self._tip_hold = now + self.TIP_WAIT
            if strip is not None:
                strip.busy(key, True)
            _sfx("tick")
            if key == "claude":
                from mint.ui import notch_agents
                AppHelper.callLater(0.3, notch_agents._connect_hooks)     # asks first, then writes the hooks
            else:
                # A beat first: the chip shows it's on it, and the click is over before macOS's box takes focus.
                # (Calendars and Reminders: Mint comes to the front for macOS's box, and the answer comes back here.)
                self._tip_front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
                AppHelper.callLater(0.3, lambda: tips.ask(
                    key, lambda ok: AppHelper.callAfter(self._tip_answer, key, ok)))
            self._tip_poll()
            return
        if key == "shelf":
            self._tips_shelf_until = now + 90.0
            tips.dismiss("shelf")               # (seen: the shelf is its own explanation)
            self._set_tab("shelf")
        elif key == "phone":
            self.hud._fire("open_settings", "accounts")
        for delay in (1.0, 3.0, 8.0):
            AppHelper.callLater(delay, lambda: self._tips_check(time.monotonic(), force=True))

    TIP_WAIT = 60.0                             # how long a chip waits for macOS's answer (and holds the notch open)

    def _tip_poll(self) -> None:
        """Twice a second while a chip waits: is it set up yet? (Read on a thread; never prompts.)"""
        if not self._tip_wait or self._tip_polling:
            return
        self._tip_polling = True
        tips = self._mod("notch_tips")
        waiting = dict(self._tip_wait)

        def read():
            answers = {}
            for key in waiting:
                try:
                    if key == "claude":
                        from mint.tools import agent_hooks
                        answers[key] = "allowed" if agent_hooks.installed() else "ask"
                    else:
                        from mint.core import permissions
                        answers[key] = permissions.status(key, set())
                        if answers[key] != "allowed" and key in ("calendar", "reminders") \
                                and permissions.granted_now(key):
                            answers[key] = "allowed"   # (turned on in System Settings: the old status lags)
                except Exception:
                    log.debug("tip status %s", key, exc_info=True)

            def apply():
                self._tip_polling = False
                now = time.monotonic()
                for key, answer in answers.items():
                    if key not in self._tip_wait:
                        continue
                    if answer == "allowed":
                        self._tip_success(key)
                    elif answer == "denied" and key in ("calendar", "reminders") and key in self._tip_prompted \
                            and now - self._tip_wait[key] > 0.8:
                        self._tip_failed(key)          # (Calendars and Reminders say so; the rest can't tell)
                    elif now - self._tip_wait[key] > self.TIP_WAIT:
                        self._tip_gave_up(key)
                if self._tip_wait:
                    AppHelper.callLater(0.5, self._tip_poll)
            AppHelper.callAfter(apply)
        import threading
        threading.Thread(target=read, daemon=True, name="notch-tip-poll").start()

    def _tip_answer(self, key: str, ok: bool) -> None:
        """macOS's answer to the Calendars / Reminders box. No box at all (it can't ask from here): its pane in
        System Settings opens instead, and the chip keeps waiting for the switch."""
        front, self._tip_front = getattr(self, "_tip_front", None), None
        try:
            mine = AppKit.NSRunningApplication.currentApplication()
            if front is not None and front != mine and mine.isActive():
                front.activateWithOptions_(0)      # back to the app you were in
        except Exception:
            log.debug("front app", exc_info=True)
        if key not in self._tip_wait:
            return
        if ok:
            self._tip_success(key)
            return
        from mint.core import permissions
        if permissions.status(key, set()) == "denied":
            self._tip_failed(key)
            return
        tips = self._mod("notch_tips")
        now = time.monotonic()
        permissions.open_pane(key)
        self._tip_prompted.discard(key)
        self._tip_wait[key] = now
        self._tip_say = (tips.waiting_note(key, False)[0], tips.SETTINGS_WORDS, now + self.TIP_WAIT)
        self._tip_hold = now + self.TIP_WAIT

    def _tip_success(self, key: str) -> None:
        tips = self._mod("notch_tips")
        now = time.monotonic()
        self._tip_wait.pop(key, None)
        facts = dict(getattr(self, "_tips_facts", None) or {})
        if key == "claude":
            facts["claude_connected"] = True
        else:
            facts[key] = "allowed"
        self._tips_facts = facts                # (so the chip doesn't come back before the next full read)
        print(f"  [set up from the notch: {tips.tip(key)[3]}]", flush=True)
        self._tip_party[key] = now + 1.7        # green with a tick for a moment, then it shrinks away
        self._tip_say = (*tips.done_note(key), now + 6.0)
        self._tip_hold = now + 4.5
        strip = (self.home or {}).get("tips")
        if strip is not None:
            strip.done(key)
        _sfx("done")
        try:
            self.orb.hop()
            self.orb.burst(gfx.GREEN, stars=True, amount=0.45)
        except Exception:
            log.debug("tip celebration", exc_info=True)
        if key == "calendar":
            self._side_to_calendar()            # the week slides in on the right
        self._cursor_back()
        AppHelper.callLater(1.0, lambda: self._tips_check(time.monotonic(), force=True))

    def _tip_failed(self, key: str) -> None:
        tips = self._mod("notch_tips")
        now = time.monotonic()
        self._tip_wait.pop(key, None)
        self._tip_say = (*tips.DENIED, now + 6.0)
        self._tip_hold = now + 3.0
        strip = (self.home or {}).get("tips")
        if strip is not None:
            strip.nope(key)
        self._cursor_back()

    def _tip_gave_up(self, key: str) -> None:
        self._tip_wait.pop(key, None)
        self._tip_say = ("Not set up yet", "No rush: click it again whenever you like.", time.monotonic() + 5.0)
        self._tip_hold = 0.0
        strip = (self.home or {}).get("tips")
        if strip is not None:
            strip.busy(key, False)

    def _tip_hovered(self, key) -> None:
        self._tip_hover = key

    def _tip_words(self, now: float):
        """(title or None, words) for the Mint pane while the chips have something to say, else None."""
        hover = self._tip_hover
        if hover and hover not in self._tip_wait and hover not in self._tip_party:
            tips = self._mod("notch_tips")
            try:
                return None, tips.tip(hover)[4]
            except Exception:
                return None
        say = self._tip_say
        if say is not None and now < say[2]:
            return say[0], say[1]
        return None

    def _cursor_back(self) -> None:
        """macOS's box can leave the pointer hidden over the notch: show it again if it is over us."""
        try:
            if self._inside(AppKit.NSEvent.mouseLocation()):
                AppKit.NSCursor.setHiddenUntilMouseMoves_(False)
                AppKit.NSCursor.arrowCursor().set()
                self.panel.invalidateCursorRectsForView_(self.root)
        except Exception:
            log.debug("cursor", exc_info=True)

    def _tip_closed(self, key: str) -> None:
        tips = self._mod("notch_tips")
        if tips is not None:
            tips.dismiss(key)
        self._tip_wait.pop(key, None)
        if self._tip_hover == key:
            self._tip_hover = None

    def _side_to_calendar(self) -> None:
        """Calendars were just allowed: the right-hand pane becomes the week calendar (it was the battery)."""
        h = self.home
        if h is None or h.get("side_kind") == "calendar" or prefs.get("notch_calendar") is False:
            return
        calendar = self._mod("notch_calendar")
        try:
            if calendar is None or not calendar.available():
                return
            view, update = calendar.view(SIDE_W, BODY_H - 16)
        except Exception:
            log.debug("calendar pane failed", exc_info=True)
            return
        if h["side"] is not None:
            h["side"].removeFromSuperview()
        view.setHidden_(True)
        self.box.addSubview_(view)
        h["side"], h["side_update"], h["side_kind"] = view, update, "calendar"

    # --- files dragged to the notch go on the shelf ---------------------------------------------

    def _watch_drag(self, mouse) -> None:
        """A file drag coming near the notch opens it on the shelf, as boring.notch does."""
        if prefs.get("notch_shelf") is False or self._search_dragging():
            return                               # (a search result being dragged OUT is not for the shelf)
        board = AppKit.NSPasteboard.pasteboardWithName_(AppKit.NSPasteboardNameDrag)
        pressed = bool(AppKit.NSEvent.pressedMouseButtons() & 1)
        count = board.changeCount()
        if not pressed:
            self._drag_count = count
            return
        if count == self._drag_count:
            return                               # the button is down, but no drag started
        near = abs(mouse.x - self.cx) < HOME_W / 2 + 40 and mouse.y > self.top - self.nh - 160
        shelf = self._mod("notch_shelf")
        if near and shelf is not None and shelf.accepts_drag(board):
            if time.monotonic() >= self.drag_until:
                shelf.set_drag_over(True)
            self.drag_until = time.monotonic() + 0.6
            self.tab = "shelf"
            self.panel.setIgnoresMouseEvents_(False)
            self._mouse_through = False
            self._drop_zone()

    def _drop_zone(self):
        """A file coming: the body becomes the drop zone (notch_fx: a dashed marching border, Mint as a folder)."""
        if self._composing():
            return None
        ctl = self._scene_ctl("drop")
        if ctl is None:
            from mint.ui import notch_fx
            self._drop_done = False
            ctl = self._show_scene("drop", notch_fx.drop_zone)
        return ctl

    def dragged_in(self, pasteboard) -> bool:
        if self._search_dragging():
            return False
        shelf = self._mod("notch_shelf")
        if shelf is None or not shelf.accepts_drag(pasteboard):
            return False
        shelf.set_drag_over(True)
        self.tab = "shelf"
        self.drag_until = time.monotonic() + 0.6
        ctl = self._drop_zone()
        if ctl is not None:
            ctl.hover(True)
        return True

    def dragged_out(self) -> None:
        shelf = self._mod("notch_shelf")
        if shelf is not None:
            shelf.set_drag_over(False)
        ctl = self._scene_ctl("drop")
        if ctl is not None:
            ctl.hover(False)                    # (it goes with the drag: the tick, once it's no longer near)

    def dropped(self, pasteboard) -> bool:
        shelf = self._mod("notch_shelf")
        if shelf is None:
            return False
        added = shelf.add_paths(shelf.paths_from(pasteboard))
        shelf.set_drag_over(False)
        self.tab, self.drag_until = "shelf", time.monotonic() + 3.7       # stay open to show it landed
        ctl = self._scene_ctl("drop")
        if ctl is not None:
            self._drop_done = True
            ctl.dropped()                       # "Got it", then the shelf with the file on it
            AppHelper.callLater(1.2, lambda: self._hide_scene("drop", ctl))
        print(f"  [notch shelf: {added} file(s) added]", flush=True)
        return added > 0

    # --- plugged in / unplugged ----------------------------------------------------------------

    def peek_battery(self, info) -> None:
        self.battery_event = str((info or {}).get("event") or "")
        self.battery_peek_until = time.monotonic() + 3.0

    def _battery_wings(self, on: bool) -> None:
        """For a moment: what happened on the left ("Plugged In"), the badge on the right."""
        label = getattr(self, "_battery_label", None)
        if on and label is None:
            label = self._label(11, _white(0.9), AppKit.NSFontWeightSemibold)
            label.setAlignment_(AppKit.NSTextAlignmentRight)
            self._battery_label = label
        badge = self.home.get("badge") if self.home else None
        if on and badge is None:
            if self.home is None:
                self._build_home()
            badge = self.home.get("badge")
        if label is not None:
            if on:
                short = {"Charging battery": "Charging", "Not charging": "Not charging", "Low Power: On": "Low Power",
                         "Low Power: Off": "Low Power off"}   # the wing holds about 12 letters
                label.setStringValue_(short.get(self.battery_event, self.battery_event) or "Battery")
                label.setFrame_(AppKit.NSMakeRect(WIN_W / 2 - self.nw / 2 - 90, WIN_H - self.nh / 2 - 8, 82, 16))
                label.setAlphaValue_(1.0)
            else:
                label.setAlphaValue_(0.0)
        if on and badge is not None:
            size = badge.frame().size
            badge.setFrameOrigin_(AppKit.NSMakePoint(WIN_W / 2 + self.nw / 2 + 10, WIN_H - self.nh / 2 - size.height / 2))
            badge.setHidden_(False)
        if on:
            for layer in list(self.bars) + [self.ring, self.glyph]:
                layer.setHidden_(True)
            self._ind = ""

    def peek_music(self, seconds: float = 6.0) -> None:
        """Music just started (music_player calls this instead of showing its card in notch mode):
        the notch opens as the player for a few seconds, then closes to the artwork and bars."""
        self.music_peek_until = time.monotonic() + seconds

    def _no_music_card(self, music) -> None:
        """In the notch the player lives inside the notch, never as a card of its own under it."""
        import sys
        player = sys.modules.get(f"{__package__}.music_player")
        card = getattr(player, "card", None)
        try:
            if card is not None and card.is_open():
                card.hide()
                self.peek_music(8.0)
        except Exception:
            log.debug("could not fold the music card into the notch", exc_info=True)

    def _agent_wing(self) -> bool:
        if not self._agents_hooked:
            return False
        mod = self._agents_mod()
        try:
            return bool(mod is not None and mod.wing())
        except Exception:
            return False

    def _music_wings(self, info, right: bool = True) -> None:
        """Closed notch while music plays: the artwork left of the camera, dancing bars right of it
        (the little Mint and the status icon step aside), as the iPhone does. right=False: a coding agent
        has the right wing, the bars step aside."""
        views = []
        if info is not None:
            pairs = (("art_view", self.face_center), ("bars_view", self.ind_center)) if right else \
                (("art_view", self.face_center),)
            for key, center in pairs:
                view = info.get(key)
                if view is None:
                    continue
                if view.superview() is not self.box:
                    self.box.addSubview_(view)
                size = view.frame().size
                view.setFrameOrigin_(AppKit.NSMakePoint(center[0] - size.width / 2, center[1] - size.height / 2))
                view.setHidden_(False)
                views.append(view)
        for view in getattr(self, "_wing_views", []):
            if view not in views:
                view.setHidden_(True)
        self._wing_views = views
        hide = info is not None and right
        if hide != getattr(self, "_indicator_hidden", False):
            self._indicator_hidden = hide
            for layer in list(self.bars) + [self.ring, self.glyph]:
                layer.setHidden_(True)
            self._ind = ""                          # repaint the indicator when the music stops

    def _music_player(self, on: bool, height: float, home: bool = False) -> None:
        """Hovering while music plays: the notch opens into the full player."""
        if on and getattr(self, "player", None) is None:
            try:
                from mint.ui import music_player
                self.player, self.player_update = music_player.player_view(PLAYER_W, PLAYER_H)
                self.box.addSubview_(self.player)
                self.player.setHidden_(True)
            except Exception:
                log.debug("no music player view", exc_info=True)
                self.player = False
        player = getattr(self, "player", None)
        if not player:
            return
        if on:
            if player.isHidden():
                try:
                    from mint.tools import music
                    self.player_update(music.cached())          # paint at once
                except Exception:
                    pass
            if home:                            # the home tab: on the left, beside the calendar
                x = WIN_W / 2 - HOME_W / 2 + 16
                y = WIN_H - self.nh - 8 - BODY_H + (BODY_H - PLAYER_H) / 2
            else:
                x, y = WIN_W / 2 - PLAYER_W / 2, WIN_H - self.nh - 4 - PLAYER_H
            self._reveal(player, AppKit.NSMakeRect(x, y, PLAYER_W, PLAYER_H), delay=0.06)
        elif not player.isHidden():
            self._conceal(player)

    # --- sizes ---------------------------------------------------------------------------------

    def _text_size(self, caption):
        """(width, height) the words need: as wide as the sentence up to MAX_W, then wrapping."""
        if not caption:
            return 0.0, 0.0
        full = caption[1]
        options = AppKit.NSStringDrawingUsesLineFragmentOrigin | AppKit.NSStringDrawingUsesFontLeading
        one_line = full.boundingRectWithSize_options_(AppKit.NSMakeSize(4000, 400), options).size.width
        width = min(MAX_W - 2 * PAD_X, math.ceil(one_line) + 14)
        # The field draws with a few points of padding: measure a little narrower than it is,
        # or a line that "fits" wraps anyway and the last words are cut off.
        rect = full.boundingRectWithSize_options_(AppKit.NSMakeSize(width - 10, 400), options)
        return width, min(math.ceil(rect.size.height) + 4, 4 * 18 + 4)

    def _breathe(self, state, mode, playful) -> None:
        """While Mint speaks, the shape swells a touch with its voice."""
        scale = 1.0
        if playful and state == "speaking" and mode in ("open", "compact"):
            level = float(getattr(self.hud, "_level", 0.0) or 0.0)
            scale = 1.0 + min(0.04, level * 0.06)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.shape.setAffineTransform_(Quartz.CGAffineTransformMakeScale(scale, 1.0 + (scale - 1.0) * 0.6))
        Quartz.CATransaction.commit()

    def _layout_content(self, mode, caption, width, height) -> None:
        show_text = mode in ("open", "hover")
        left = WIN_W / 2 - width / 2 + PAD_X
        inner = width - 2 * PAD_X
        y = WIN_H - self.nh - 6
        if caption and show_text:
            shown, full = caption
            _, th = self._text_size(caption)
            if shown is not getattr(self, "_shown", None):
                self._shown = shown
                self.words.setAttributedStringValue_(shown)
            self._reveal(self.words, AppKit.NSMakeRect(left - 2, y - th, inner + 4, th), delay=0.06, style="wipe")
            y -= th + 4
        self._fade(self.status, False)
        if not (show_text and caption):
            self._fade(self.words, False)
        visible = show_text and bool(self.progress)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        if visible:
            self.track.setFrame_(Quartz.CGRectMake(left, y - 5, inner, 3))
        self.fill.setPosition_(Quartz.CGPointMake(left, y - 3.5))
        self.fill.setBounds_(Quartz.CGRectMake(0, 0, inner * float(self.progress or 0), 3))
        self.fill.setBackgroundColor_(gfx.cg(gfx.accent()))
        Quartz.CATransaction.commit()
        self.track.setOpacity_(1.0 if visible else 0.0)
        self.fill.setOpacity_(1.0 if visible else 0.0)
        # Controls: one row along the bottom, popping in one after another.
        hover = mode == "hover" or (mode == "home" and self.tab == "home" and not self._scene_shown
                                    and (not self._music() or bool(getattr(self, "_mint_words", ""))))
        # (also when the row is simply not showing: folding the full notch from its Shelf or Search tab
        # leaves the buttons faded out, and the small row came up as an empty black shape)
        row = self._row()
        # (the first button SHOWN: Stop is first in the list but hidden while Mint is idle - checking it made the row
        # restart its pop-in every frame, so the buttons flickered at almost no opacity)
        entering = hover and bool(row) and (self.mode not in ("hover", "home") or row[0][1].alphaValue() < 0.01)
        start = WIN_W / 2 - (len(row) - 1) * STEP / 2
        row_y = WIN_H - height + 9
        if mode == "home":                      # in the Mint pane, under its status
            start = WIN_W / 2 - HOME_W / 2 + 16 + BTN / 2
            row_y = WIN_H - self.nh - 8 - BODY_H + 20
        for (symbol, button) in self.buttons:
            if (symbol, button) not in row and not button.isHidden():
                self._conceal(button)
        for i, (symbol, button) in enumerate(row):
            if hover:                                   # leaving: they fade where they are, not in the notch row
                button.setFrame_(AppKit.NSMakeRect(start + i * STEP - BTN / 2, row_y, BTN, BTN))
            if entering:
                button.setHidden_(False)
                button.setAlphaValue_(0.0)
                # (opening, after the pane they sit in)
                AppHelper.callLater((0.14 if self._fresh() else 0.04) + 0.035 * i,
                                    lambda b=button: self.mode in ("hover", "home") and self._pop(b))
            elif not hover:
                self._fade(button, False)
        self._paint_buttons()

    def _row(self) -> list:
        """The small row's buttons that apply now (Stop only while something is going on). Claude mode isn't
        here: it's a tab of the full notch, and "Claude mode" by voice."""
        busy = getattr(self.hud, "_state", "") in ("thinking", "working", "speaking") or \
            getattr(self.hud, "_activity", None) is not None
        return [(sym, b) for sym, b in self.buttons if sym != "stop.fill" or busy]

    def _fresh(self) -> bool:
        """The shape just started to open: content arrives staggered behind it (else at once, e.g. a tab change)."""
        return time.monotonic() - self._open_started < 0.15

    def _reveal(self, view, frame, delay: float = 0.0, style: str = "blur") -> None:
        """Content arriving as the notch opens (Grok Bot's entrance, kinetics): panes rise from 96% out of a blur
        ("blur"), text rows wipe in left to right ("wipe"), small things fade ("fade") - each `delay` after the
        shape starts to grow, so they come in one after another. Already showing: it just takes its new frame.
        (Reduce motion: kinetics turns all of it into short fades.)"""
        concealing = getattr(self, "_concealing", {})
        if view.isHidden() or view.alphaValue() < 0.05 or id(view) in concealing:
            concealing.pop(id(view), None)
            delay = delay if self._fresh() else 0.0
            if frame is not None:
                view.setFrame_(frame)
            view.setHidden_(False)
            view.setAlphaValue_(1.0)
            if view.layer() is None:
                view.setWantsLayer_(True)           # (a new subview's layer only comes with the next display)
            layer = view.layer()
            if layer is None:
                return
            layer.removeAnimationForKey_("reveal")
            if style == "wipe":
                kinetics.wipe_in(layer, 0.3, delay)
            elif style == "fade":
                kinetics.basic(layer, "opacity", 0.0, 1.0, 0.2, delay, anim_key="reveal-o")
            else:
                if not kinetics.reduce_motion():
                    view.setLayerUsesCoreImageFilters_(True)          # (for the blur)
                    # AppKit pins a view's layer by its corner: grow from the middle by sliding 2% with the scale.
                    size = layer.bounds().size
                    kinetics.spring(layer, "transform.translation.x", size.width * 0.02, 0.0, "snappy", delay,
                                    anim_key="reveal-x")
                    kinetics.spring(layer, "transform.translation.y", size.height * 0.02, 0.0, "snappy", delay,
                                    anim_key="reveal-y")
                kinetics.blur_in(layer, 0.32, delay)

                def unblur():
                    if layer.filters():
                        layer.setFilters_(None)       # a blur left at 0 still costs a pass every frame
                AppHelper.callLater(delay + 0.45, unblur)
        elif frame is not None and not AppKit.NSEqualRects(view.frame(), frame):
            view.setFrame_(frame)

    def _top(self, view, show: bool) -> None:
        """The header's tabs, buttons and badge: they fade in last as the notch opens, and go with the content."""
        if show:
            if view.isHidden() or id(view) in getattr(self, "_concealing", {}):
                self._reveal(view, None, delay=0.22, style="fade")
        elif not view.isHidden():
            self._conceal(view)

    def _conceal(self, view) -> None:
        """Content leaving (a tab change, the notch folding): a quick fade and a slight shrink (0.12 s), before the
        shape folds (tick holds it CLOSE_LEAD) - then hidden, unless it was asked back meanwhile."""
        if view is None or view.isHidden():
            return
        concealing = self.__dict__.setdefault("_concealing", {})
        if id(view) in concealing:
            return
        token = object()
        concealing[id(view)] = token
        AppKit.NSAnimationContext.beginGrouping()
        context = AppKit.NSAnimationContext.currentContext()
        context.setDuration_(0.12)
        context.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseIn))
        view.animator().setAlphaValue_(0.0)
        AppKit.NSAnimationContext.endGrouping()
        layer = view.layer()
        if layer is not None and not kinetics.reduce_motion():
            shrink = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
            shrink.setFromValue_(1.0)
            shrink.setToValue_(0.97)
            shrink.setDuration_(0.12)
            shrink.setFillMode_(Quartz.kCAFillModeForwards)
            shrink.setRemovedOnCompletion_(False)
            layer.addAnimation_forKey_(shrink, "reveal")

        def done():
            if concealing.get(id(view)) is token:
                del concealing[id(view)]
                view.setHidden_(True)
                view.setAlphaValue_(0.0)
                if view.layer() is not None:
                    view.layer().removeAnimationForKey_("reveal")
        AppHelper.callLater(0.14, done)

    def _pop(self, button) -> None:
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.16)
        button.animator().setAlphaValue_(1.0)
        AppKit.NSAnimationContext.endGrouping()
        layer = button.layer()
        if layer is not None and prefs.get("notch_playful") is not False and not kinetics.reduce_motion():
            # Each control drops in with a little spring, one after another. (Never move the layer's anchor:
            # AppKit places a layer-backed button by its corner, and a centred anchor shifted every button
            # half its size down and left, half outside the notch.)
            pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.translation.y")
            pop.setFromValue_(7.0)
            pop.setToValue_(0.0)
            pop.setDamping_(12.0)
            pop.setStiffness_(320.0)
            pop.setMass_(0.7)
            pop.setDuration_(pop.settlingDuration())
            layer.addAnimation_forKey_(pop, "pop")

    def _fade(self, view, on: bool) -> None:
        want = 1.0 if on else 0.0
        if abs(view.alphaValue() - want) < 0.01:
            if not on and not isinstance(view, AppKit.NSTextField):
                view.setHidden_(True)
            return
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.18 if on else 0.1)
        view.animator().setAlphaValue_(want)
        AppKit.NSAnimationContext.endGrouping()

    def _paint_buttons(self) -> None:
        mic, voice = bool(prefs.get("mic")), bool(prefs.get("voice"))
        try:
            from mint.ui import sharing
            shown = sharing.visible()
        except Exception:
            shown = False
        hidden = bool(getattr(self.hud, "hidden", False))
        painted = (mic, voice, shown, hidden, len(self.buttons))
        if painted == getattr(self, "_painted", None):
            return                              # nothing changed: images and tints stay (every frame cost ~0.3 ms)
        self._painted = painted
        red = AppKit.NSColor.systemRedColor()
        for symbol, button in self.buttons:
            if symbol.startswith("mic"):
                button.setImage_(gfx.symbol("mic.fill" if mic else "mic.slash.fill", 14))
                button.setContentTintColor_(_white(0.96) if mic else red)
            elif symbol.startswith("speaker"):
                button.setImage_(gfx.symbol("speaker.wave.2.fill" if voice else "speaker.slash.fill", 14))
                button.setContentTintColor_(_white(0.96) if voice else red)
            elif symbol.startswith("eye"):
                button.setImage_(gfx.symbol("eye.fill" if shown else "eye.slash", 14))
                button.setContentTintColor_(red if shown else _white(0.96))
            elif symbol == "stop.fill":
                button.setContentTintColor_(red)
            elif symbol == "arrow.up.to.line":
                # Hide <-> Show: the little Mint and the side icons, not the notch itself.
                button.setImage_(gfx.symbol("arrow.down.to.line" if hidden else "arrow.up.to.line", 14))
                button.setToolTip_("Show Mint beside the notch again" if hidden else
                                   "Hide Mint: the notch stays, the little Mint and its icons go (⌃⌥H)")

    def _indicator(self, state, activity, now) -> None:
        """Right of the camera: bars while listening or speaking, a spinner while thinking,
        the task's icon while working, a crossed-out mic when the mic is off."""
        if activity is not None:
            kind = "task:" + activity.get("kind", "")
        elif state in ("thinking", "working"):
            kind = "spin"
        elif not prefs.get("mic") and state in ("paused", "sleeping", "awake"):
            kind = "mic-off"
        elif state in ("sleeping", "paused", "offline"):
            kind = "sleep"
        else:
            kind = "bars"
        agent = None
        if kind in ("bars", "sleep", "mic-off") and state != "speaking" and self._agents_hooked:
            mod = self._agents_mod()
            try:
                agent = mod.wing() if mod is not None else None
            except Exception:
                agent = None
            if agent:
                kind = f"agent:{agent['state']}:{agent['app']}"   # Mint is idle: the wing tells how the agent is doing
        if kind in ("bars", "sleep", "mic-off") and state != "speaking" and _in_call():
            kind = "meet"                           # on a Google Meet call with the user: a red camera, beating
        accent = gfx.accent()
        if kind != self._ind:
            old = self._ind
            self._ind = kind
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setDisableActions_(True)
            for bar in self.bars:
                bar.setHidden_(kind != "bars")
            self.ring.setHidden_(kind != "spin")
            self.ring.setStrokeColor_(gfx.cg(gfx.light(accent)))
            glyph = None
            tint = gfx.light(accent)
            if kind.startswith("task:"):
                from mint.ui import activity as act
                glyph = act.SYMBOLS.get(kind[5:], "sparkles")
            elif kind in ("mic-off", "sleep"):
                glyph = None                        # resting: just the little Mint (mic off: its own small badge)
            elif kind == "meet":
                glyph, tint = "video.fill", (1.0, 0.36, 0.36)
            elif kind.startswith("agent:"):
                _, a_state, a_app = kind.split(":")
                from mint.ui.notch_agents import APP_RGB, STATE_RGB
                glyph, tint = {"waiting": ("exclamationmark.circle.fill", STATE_RGB["waiting"]),
                               "asking": ("questionmark.circle.fill", STATE_RGB["asking"]),
                               "done": ("checkmark.circle.fill", STATE_RGB["done"]),
                               "failed": ("xmark.circle.fill", STATE_RGB["failed"])}.get(
                    a_state, ("sparkle", APP_RGB.get(a_app, gfx.light(accent))))
            self.glyph.setHidden_(glyph is None)
            if glyph:
                self.glyph_mask.setContents_(gfx.symbol(glyph, 13, "bold"))
                self.glyph.setBackgroundColor_(gfx.cg(tint))
            Quartz.CATransaction.commit()
            self._agent_motion(kind, old)
        # An agent at work: its pals' faces take the right wing (notch_agents.wing_view), the glyph steps aside.
        faces = self._wing_faces(kind.startswith("agent:"))
        if kind.startswith("agent:") and self.glyph.isHidden() != faces:
            self.glyph.setHidden_(faces)
        if kind == "bars":
            level = float(getattr(self.hud, "_level", 0.0) or 0.0)
            speaking = state == "speaking"
            color = gfx.cg(gfx.light(accent)) if speaking else _white(0.7).CGColor()
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setDisableActions_(True)
            for i, bar in enumerate(self.bars):
                wobble = 0.5 + 0.5 * math.sin(now * (7 + i * 1.7) + i)
                height = 3 + (12 * min(1.0, level * 1.4) + (2.5 if speaking else 1.2)) * wobble
                bar.setBounds_(Quartz.CGRectMake(0, 0, 3, height))
                bar.setBackgroundColor_(color)
            Quartz.CATransaction.commit()

    def _wing_faces(self, want: bool) -> bool:
        """The closed notch's right wing as the agents' faces (up to 4: 1 big, 2, a 2x2, or 3 and "+N"), just
        right of the camera - never over it. True while they show."""
        # A plain notch (hidden, or the idle face off) is just the notch: no faces sticking out of it.
        want = want and getattr(self, "_mode_now", None) != "plain" and not getattr(self.hud, "hidden", False)
        mod = self._agents_mod() if want and self._agents_hooked else None
        try:
            width = float(mod.wing_width()) if mod is not None and hasattr(mod, "wing_view") else 0.0
        except Exception:
            width = 0.0
        view = getattr(self, "_wing_view", None)
        if width > 0 and view is None:
            try:
                view, _update = mod.wing_view(self.nh)
                view.setHidden_(True)
                self.box.addSubview_(view)
                self._wing_view = view
            except Exception:
                log.debug("agents wing failed", exc_info=True)
                self._wing_view = view = False
        if not view:
            return False
        show = width > 0
        if show:
            origin = AppKit.NSMakePoint(WIN_W / 2 + self.nw / 2, WIN_H - self.nh)
            if not AppKit.NSEqualPoints(view.frame().origin, origin):
                view.setFrameOrigin_(origin)
        if view.isHidden() == show:
            view.setHidden_(not show)
        return show

    def _bar_info(self):
        """The collapsed bar's data (notch_agents.compact()) while it should show, else None."""
        if not self._agents_hooked or prefs.get("notch_agents_bar") is False:
            return None
        mod = self._agents_mod()
        if mod is not None and hasattr(mod, "live") and not mod.live():
            return None                                  # pop-ups only: no live bar while they work
        try:
            return mod.compact() if mod is not None and hasattr(mod, "compact_view") else None
        except Exception:
            return None

    def _bar_width(self, info) -> float:
        """Just wide enough for the lead agent's line (its step is cut with … past a cap); it grows at once but
        only shrinks for a real difference, so the island doesn't wobble as the step text changes."""
        mod = self._agents_mod()
        try:
            want = float(mod.bar_width(info))
        except Exception:
            want = 220.0
        want = min(340.0, max(170.0, math.ceil(want / 4.0) * 4.0))
        have = getattr(self, "_bar_w", 0.0)
        if want > have or have - want >= 36:
            self._bar_w = want
        return self._bar_w

    def _agents_bar(self, on: bool, width: float) -> None:
        """Host notch_agents.compact_view under the camera row in "bar" mode (it updates itself)."""
        view = getattr(self, "_bar_view", None)
        if on:
            w = getattr(self, "_bar_w", 200.0)
            if view is not None and abs(view.frame().size.width - w) > 0.5:
                view.removeFromSuperview()           # (built at a width: a new one for the new width)
                view = None
            if view is None:
                mod = self._agents_mod()
                try:
                    view, _update = mod.compact_view(w, 24)
                except Exception:
                    log.debug("agents bar failed", exc_info=True)
                    return
                view.setHidden_(True)
                self.box.addSubview_(view)
                self._bar_view = view
            self._reveal(view, AppKit.NSMakeRect(WIN_W / 2 - w / 2, WIN_H - self.nh - 27, w, 24), delay=0.06,
                         style="fade")
        elif view is not None and not view.isHidden():
            self._conceal(view)

    def _agent_motion(self, kind: str, old: str) -> None:
        """The wing's agent mark moves with the agent: a slow twinkle-spin while it works, a pulse while it
        waits for you, a springy pop when it is done (or a shake when it failed)."""
        glyph = self.glyph
        glyph.removeAnimationForKey_("agent")
        if kind == "meet":
            beat = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
            beat.setValues_([1.0, 0.45, 1.0])
            beat.setDuration_(1.8)
            beat.setRepeatCount_(float("inf"))
            glyph.addAnimation_forKey_(beat, "agent")
            return
        if not kind.startswith("agent:"):
            return
        a_state = kind.split(":")[1]
        if a_state in ("working", "thinking"):
            # (numbers only: a keyframe list of raw CATransform3D structs aborts Core Animation's commit)
            turn = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
            turn.setFromValue_(0.0)
            turn.setToValue_(-2 * math.pi)
            twinkle = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
            twinkle.setValues_([1.0, 1.18, 1.0, 0.86, 1.0])
            group = Quartz.CAAnimationGroup.animation()
            group.setAnimations_([turn, twinkle])
            group.setDuration_(2.4 if a_state == "working" else 3.6)
            group.setRepeatCount_(float("inf"))
            glyph.addAnimation_forKey_(group, "agent")
        elif a_state in ("waiting", "asking"):
            pulse = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
            pulse.setValues_([1.0, 1.28, 0.92, 1.08, 1.0])
            pulse.setKeyTimes_([0, 0.18, 0.36, 0.52, 1.0])
            pulse.setDuration_(1.3)
            pulse.setRepeatCount_(float("inf"))
            glyph.addAnimation_forKey_(pulse, "agent")
        elif a_state == "done":
            pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
            pop.setFromValue_(0.3)
            pop.setToValue_(1.0)
            pop.setDamping_(9.0)
            pop.setStiffness_(260.0)
            pop.setDuration_(pop.settlingDuration())
            glyph.addAnimation_forKey_(pop, "agent")
            if not old.startswith("agent:done"):
                self.orb.hop()
                self.orb.burst(gfx.GREEN, stars=True, amount=0.5)
        elif a_state == "failed":
            shake = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.translation.x")
            shake.setValues_([0, -3, 3, -2, 2, 0])
            shake.setDuration_(0.4)
            glyph.addAnimation_forKey_(shake, "agent")

    # --- the little Mint as a shared element ----------------------------------------------------

    def _face_target(self, where: str):
        """(x, y, scale) of the little Mint: in the wing, or grown at the left of the home tab's Mint pane."""
        if where == "pane":
            left = WIN_W / 2 - HOME_W / 2 + 16
            return left + FACE * PANE_FACE / 2 + 2, WIN_H - self.nh - 8 - 42, PANE_FACE
        return self.face_center[0], self.face_center[1], 1.0

    MIC_BADGE = 9.0

    def _build_mic_badge(self, host):
        d = self.MIC_BADGE
        x, y = self.face_center
        badge = Quartz.CALayer.layer()
        badge.setBounds_(Quartz.CGRectMake(0, 0, d, d))
        # Lower right of the face, short of the camera housing (the face sits 13 pt left of it).
        badge.setPosition_(Quartz.CGPointMake(x + 7.0, y - 6.0))
        badge.setCornerRadius_(d / 2)
        badge.setBackgroundColor_(gfx.cg((0.13, 0.13, 0.15), 1.0))
        badge.setBorderWidth_(1.0)
        badge.setBorderColor_(gfx.cg((0.0, 0.0, 0.0), 1.0))
        glyph = Quartz.CALayer.layer()
        glyph.setFrame_(Quartz.CGRectMake(1.5, 1.5, d - 3, d - 3))
        glyph.setBackgroundColor_(gfx.cg((1.0, 0.36, 0.36)))
        mask = Quartz.CALayer.layer()
        mask.setFrame_(Quartz.CGRectMake(0, 0, d - 3, d - 3))
        mask.setContents_(gfx.symbol("mic.slash.fill", 12, "bold"))
        mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
        glyph.setMask_(mask)
        badge.addSublayer_(glyph)
        badge.setOpacity_(0.0)
        badge.setHidden_(True)
        host.addSublayer_(badge)
        self._mic_on_face = False
        return badge

    def _mic_badge(self, on: bool) -> None:
        badge = getattr(self, "mic_badge", None)
        if badge is None or on == self._mic_on_face:
            return
        self._mic_on_face = on
        if on:
            badge.setHidden_(False)
            badge.removeAnimationForKey_("kin-fade")
            kinetics.pop(badge, "bouncy", start=0.3, delay=0.1)
        else:
            kinetics.fade(badge, False, 0.15)

    def _place_face(self, where: str) -> None:
        """The face travels between its wing spot and the Mint pane on one spring (kinetics.fly), from wherever
        it is now - it never jumps or fades. Under Reduce motion it fades across instead."""
        if where == self._face_where:
            return
        self._face_where = where
        x, y, scale = self._face_target(where)
        self._face_spot = (x, y, scale)
        host = self.face_host
        shown = host.presentationLayer() or host
        here = shown.position()
        try:
            was = float(shown.valueForKeyPath_("transform.scale"))
        except Exception:
            was = 1.0
        if kinetics.reduce_motion():
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setDisableActions_(True)
            host.setPosition_(Quartz.CGPointMake(x, y))
            host.setAffineTransform_(Quartz.CGAffineTransformMakeScale(scale, scale))
            Quartz.CATransaction.commit()
            kinetics.basic(host, "opacity", 0.0, host.opacity(), 0.2, anim_key="face-fade", keep=False)
            return
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        host.setAffineTransform_(Quartz.CGAffineTransformIdentity)    # (a pull's stretch, if any)
        Quartz.CATransaction.commit()
        # (snappy: it lands before the words beside it wipe in, never across them)
        kinetics.fly(host, (here.x, here.y), (x, y), was, scale, "snappy", 0.02 if self._fresh() else 0.0)

    # --- status on the shape: the rim, the sweep, the wing's glow, the countdown --------------------

    def _show_status(self, state, activity, alert, mode) -> None:
        """State said by the shape itself, redundantly with the face: needs you = an amber ring pulsing every
        1.4 s; busy = a light sweeping along the bottom edge in Mint's (or the agent's) colour; and a soft glow
        of the same colour behind the little Mint, breathing while busy. Done/error flash once (_follow_alert)."""
        agent = getattr(self, "_wing", None) if self._agents_hooked else None
        a_state = agent.get("state") if agent else ""
        kind, rgb = None, None
        if getattr(self.hud, "hidden", False) and mode == "plain":
            pass                                        # hidden: just the notch
        elif (alert is not None and alert.kind == "need") or a_state in ("waiting", "asking"):
            kind, rgb = "waiting", AMBER
        elif alert is not None and alert.kind == "error":
            kind, rgb = "error", gfx.RED
        elif alert is not None and alert.kind == "done":
            kind, rgb = "done", gfx.GREEN
        elif state in ("thinking", "working") or activity is not None:
            kind, rgb = "busy", gfx.state_rgb(state if state in ("thinking", "working") else "working")
        elif a_state in ("working", "thinking"):
            from mint.ui.notch_agents import APP_RGB
            kind, rgb = "busy", tuple(agent.get("hue") or APP_RGB.get(agent.get("app"), gfx.accent()))
        if (kind, rgb) == self._rim_status:
            return
        self._rim_status = (kind, rgb)
        rim, sweep, glow = self.rim, self.sweep, self.glow
        # the ring: needs you
        if kind == "waiting":
            rim.setStrokeColor_(gfx.cg(rgb))
            rim.setShadowColor_(gfx.cg(rgb))
            kinetics.fade(self.rim_wrap, True, 0.25)
            kinetics.pulse(rim, "rim-pulse", 1.4, 0.3, 1.0)
        else:
            kinetics.fade(self.rim_wrap, False, 0.25)
        # the sweep: busy
        if kind == "busy":
            light = gfx.mix(rgb, (1.0, 1.0, 1.0), 0.35)
            self.sweep_band.setColors_([gfx.cg(light, 0.0), gfx.cg(light, 0.95), gfx.cg(light, 0.0)])
            self._sweep_travel()
            kinetics.fade(sweep, True, 0.3)
        else:
            kinetics.fade(sweep, False, 0.3)
        # the glow behind the face
        if kind is not None:
            strength = 0.75 if kind in ("error", "done") else 0.62
            glow.setColors_([gfx.cg(rgb, strength), gfx.cg(rgb, strength * 0.3), gfx.cg(rgb, 0.0)])
            kinetics.fade(self.glow_wrap, True, 0.35)
            if kind in ("busy", "waiting"):
                kinetics.pulse(glow, "glow-breathe", 1.4 if kind == "waiting" else 2.8, 0.55, 1.0)
            else:
                glow.removeAnimationForKey_("glow-breathe")
        else:
            kinetics.fade(self.glow_wrap, False, 0.4)

        def settle(seen=self._rim_status):
            if self._rim_status is not seen:
                return
            if kind != "waiting":
                rim.removeAnimationForKey_("rim-pulse")
            if kind != "busy":
                self.sweep_band.removeAnimationForKey_("travel")
            if kind is None:
                glow.removeAnimationForKey_("glow-breathe")
        AppHelper.callLater(0.45, settle)              # nothing left running behind a faded layer

    def _sweep_travel(self) -> None:
        """The busy light: a soft band crosses the bottom edge, rests, crosses again (the track masks it to the
        edge). Under Reduce motion it doesn't travel: the whole edge glows faintly instead."""
        w = float(self.size[0])
        self._sweep_w = w
        band = self.sweep_band
        band.removeAnimationForKey_("travel")
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        if kinetics.reduce_motion():
            band.setBounds_(Quartz.CGRectMake(0, 0, w * 1.6, WIN_H))
            band.setPosition_(Quartz.CGPointMake(WIN_W / 2, WIN_H / 2))
            Quartz.CATransaction.commit()
            return
        bw = max(60.0, min(150.0, w * 0.4))
        band.setBounds_(Quartz.CGRectMake(0, 0, bw, WIN_H))
        band.setPosition_(Quartz.CGPointMake(WIN_W / 2 - w / 2 - bw, WIN_H / 2))     # resting: off the edge
        Quartz.CATransaction.commit()
        cross = Quartz.CABasicAnimation.animationWithKeyPath_("position.x")
        cross.setFromValue_(WIN_W / 2 - w / 2 - bw / 2)
        cross.setToValue_(WIN_W / 2 + w / 2 + bw / 2)
        cross.setDuration_(0.9 + w / 700.0)
        cross.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
            Quartz.kCAMediaTimingFunctionEaseInEaseOut))
        group = Quartz.CAAnimationGroup.animation()
        group.setAnimations_([cross])
        group.setDuration_(cross.duration() + 0.7)       # a rest between passes
        group.setRepeatCount_(float("inf"))
        band.addAnimation_forKey_(group, "travel")

    def flash(self, rgb, shake: bool = False) -> None:
        """One flash of the rim: done = green, thickening with a little overshoot; error = red, and the island
        shakes +-5 pt over 0.45 s. (Reduce motion: the flash fades, no shake.)"""
        flash = self.flash_rim
        flash.setStrokeColor_(gfx.cg(rgb))
        flash.setShadowColor_(gfx.cg(rgb))
        beat = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        beat.setValues_([0.0, 1.0, 0.9, 0.0])
        beat.setKeyTimes_([0.0, 0.12, 0.5, 1.0])
        beat.setDuration_(1.0)
        flash.addAnimation_forKey_(beat, "flash")
        kinetics.spring(flash, "lineWidth", 0.0, 5.0, "bouncy", anim_key="flash-w")
        if shake:
            kinetics.shake(self.root.layer(), 5.0, 0.45)

    def _countdown(self, now: float, mode: str) -> None:
        """The last 3 s before the notch folds by itself: a 2 pt line along the bottom inner edge shrinks to nothing
        (transform.scale.x, linear - Core Animation runs it, not the tick)."""
        cd = self.st.countdown(now) if mode == "home" else None
        if cd == self._cd:
            return
        self._cd = cd
        line = self.cd_line
        if cd is None:
            kinetics.fade(line, False, 0.15)
            return
        start, end = cd
        self._place_cd_line()
        left = max(0.05, end - now)
        kinetics.fade(line, True, 0.15)
        kinetics.basic(line, "transform.scale.x", min(1.0, left / max(0.01, end - start)), 0.0, left,
                       timing=Quartz.kCAMediaTimingFunctionLinear, anim_key="countdown")

    def _place_cd_line(self) -> None:
        w, h = self.size
        radius = 24.0 if h > self.nh + 2 else 14.0
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.cd_line.setBounds_(Quartz.CGRectMake(0, 0, max(20.0, w - 2 * radius - 16), 2))
        self.cd_line.setPosition_(Quartz.CGPointMake(WIN_W / 2, WIN_H - h + 4))
        Quartz.CATransaction.commit()

    # --- clicks --------------------------------------------------------------------------------

    def clicked(self) -> None:
        """A click on the notch (not on one of its buttons) opens the full notch, or folds it back; with the
        chat open it closes the chat. The chat has its own button."""
        try:
            from mint.ui import onboarding
            if onboarding.resume_if_unfinished():
                return                      # setup closed half-way: a click on Mint brings it back
        except Exception:
            pass
        if getattr(getattr(self.hud, "chat", None), "is_open", False):
            self._chat()
            return
        # Opened, it folds 8 s after the pointer leaves or after a quiet minute under it; folded by a click, it
        # doesn't peek again until the pointer leaves (notch_state).
        _sfx("open" if self.st.click(time.monotonic()) == "opened" else "close")

    def _chat(self) -> None:
        self._put_away_scene()
        self.hud._fire("console")

    def _eye(self) -> None:
        from mint.ui import sharing
        sharing.set_visible(not sharing.visible())

    def _menu_from_button(self) -> None:
        """The gear (open notch) or the small row's settings button: the menu rolls down right under it."""
        factory = getattr(self.hud, "menu_factory", None)
        menu = factory() if factory else None
        button = self._menu_button()
        if menu is None or button is None or button.window() is None:
            self._popup(None)
            return
        from mint.ui.notch_menu import menu as dropdown
        if dropdown.is_open():
            dropdown.close()
            return
        rect = button.window().convertRectToScreen_(button.convertRect_toView_(button.bounds(), None))
        self._menus = getattr(self, "_menus", 0) + 1          # the notch stays open while it's down

        def closed():
            self._menus = max(0, self._menus - 1)

        def settings():
            self.st.close(time.monotonic())                  # opened only to get to Settings: fold the notch
        dropdown.show(menu, rect, LEVEL, on_close=closed, on_settings=settings)

    def _menu_button(self):
        """The settings button the pointer is on (or nearest): the open notch's gear, or the small row's."""
        mouse = AppKit.NSEvent.mouseLocation()
        found = []
        home = getattr(self, "home", None) or {}
        candidates = [home.get("gear")] + [b for sym, b in getattr(self, "buttons", []) if sym == "slider.horizontal.3"]
        for b in candidates:
            if b is None or b.isHidden() or b.alphaValue() < 0.05 or b.window() is None:
                continue
            r = b.window().convertRectToScreen_(b.convertRect_toView_(b.bounds(), None))
            cx, cy = r.origin.x + r.size.width / 2, r.origin.y + r.size.height / 2
            found.append(((cx - mouse.x) ** 2 + (cy - mouse.y) ** 2, b))
        return min(found, key=lambda f: f[0])[1] if found else None

    # --- drag the little Mint out: the notch gives way to the orb ---------------------------------

    def on_face(self, point) -> bool:
        x, y, scale = self._face_spot
        return self.phase == "on" and (point.x - x) ** 2 + (point.y - y) ** 2 <= (FACE * scale / 2 + 6) ** 2

    def face_drag(self, dx: float, dy: float) -> bool:
        """The face follows the pointer a little, like something held by an elastic; pulled far enough it
        lets go: Mint leaves the notch (the orb drops out) and stays the orb. True once it has."""
        pull = math.hypot(dx, dy)
        if pull >= FACE_PULL:
            self._pull_out()
            return True
        stretch = 1.0 + 0.16 * pull / FACE_PULL
        give = min(1.0, pull / FACE_PULL) * 10.0 / max(pull, 0.001)        # at most 10 pt of give
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        # The host layer is anchored on the face: it stretches about the face (at its spot's size) and gives a little.
        scale = self._face_spot[2] * stretch
        t = Quartz.CGAffineTransformIdentity
        t = Quartz.CGAffineTransformTranslate(t, dx * give, dy * give)         # (each call acts first)
        t = Quartz.CGAffineTransformScale(t, scale, scale)
        self.face_host.setAffineTransform_(t)
        Quartz.CATransaction.commit()
        return False

    # --- the drop zone: the orb being dragged toward the notch ---------------------------------------

    def dock_zone(self, on: bool, t: float = 1.0) -> None:
        """The orb is close: the notch opens up like a drop target (a bulge, a dashed mint outline and "Drop Mint
        here", the way a file is invited onto it); away again, it closes. Releasing flies the orb in."""
        hud = self.hud
        if hud is None or _busy[0] or _live[0]:
            return
        if on:
            if getattr(self, "panel", None) is None:
                self.build(hud)
            if self.phase in ("off", None, ""):
                self.phase = "docking"
                self.panel.setIgnoresMouseEvents_(True)
                self._mouse_through = True
                self.panel.orderFrontRegardless()
                self.face_host.setOpacity_(0.0)
                for layer in list(self.bars) + [self.ring, self.glyph]:
                    layer.setHidden_(True)
                self._ind = ""
            if self.phase != "docking":
                return
            w, h = self.nw + 2 * WING + 30 + 66 * t, self.nh + 18 + 28 * t       # swells as the orb nears
            self._dock = (w, h)
            self._resize(w, h)
            self._dock_visuals(True, w, h)
        elif self.phase == "docking":
            self._resize(self.nw, self.nh)
            self._dock_visuals(False, 0, 0)
            self.phase = "off"
            AppHelper.callLater(0.5, lambda: self.phase == "off" and not _live[0] and self.panel.orderOut_(None))

    def dock_jiggle(self) -> None:
        """The orb was let go over the notch: it bulges, wobbles and settles, like jelly taking something in."""
        if self.phase != "docking" or not getattr(self, "_dock", None):
            return
        w, h = self._dock
        steps = ((0.0, w + 34, h + 14), (0.14, w - 16, h - 6), (0.28, w + 16, h + 8), (0.42, w, h))
        for delay, ww, hh in steps:
            AppHelper.callLater(delay, lambda a=ww, b=hh: self.phase == "docking" and self._resize(a, b))

    def _dock_visuals(self, on: bool, w: float, h: float) -> None:
        if getattr(self, "_dock_ring", None) is None:
            ring = Quartz.CAShapeLayer.layer()
            ring.setFillColor_(None)
            ring.setLineWidth_(1.6)
            ring.setLineDashPattern_([6, 5])
            ring.setLineCap_("round")
            ring.setOpacity_(0.0)
            self.box.layer().addSublayer_(ring)
            self._dock_ring = ring
            self._dock_text = self._label(11.5, _white(0.9), AppKit.NSFontWeightSemibold)
            self._dock_text.setAlignment_(AppKit.NSTextAlignmentCenter)
            self._dock_text.setStringValue_("Drop Mint here")
        ring, text = self._dock_ring, self._dock_text
        if on:
            top = WIN_H - self.nh - 4
            rect = Quartz.CGRectMake(WIN_W / 2 - w / 2 + 14, WIN_H - h + 8, w - 28, top - (WIN_H - h + 8))
            ring.setPath_(Quartz.CGPathCreateWithRoundedRect(rect, 14, 14, None))
            ring.setStrokeColor_(gfx.cg(gfx.accent()))
            mid = (rect.origin.y + rect.size.height / 2)
            text.setFrame_(AppKit.NSMakeRect(WIN_W / 2 - 80, mid - 8, 160, 16))      # centred in the outline
            text.setTextColor_(AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*gfx.accent(), 1.0)
                               if len(gfx.accent()) == 3 else _white(0.9))
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.25)
        ring.setOpacity_(0.95 if on else 0.0)
        Quartz.CATransaction.commit()
        text.setAlphaValue_(1.0 if on else 0.0)

    def _pull_out(self) -> None:
        """The pull let go: the little Mint turns into the floating orb right where it was, pops to full size
        under the pointer and goes on following it (carry) until the button is released; then it lands with a
        hop and stays there. The notch closes behind it. Orb mode is saved."""
        hud = self.hud
        if _busy[0] or not _live[0]:
            return
        _busy[0] = True
        self._carrying = True
        self.st.close(time.monotonic())
        face = self.global_face()
        self._face_reset(animated=False)
        notch.phase = "leaving"
        _depart()                                # the orb is Mint again (and _live is False)
        window = hud._orb_window
        size = window.frame().size
        window.setFrameOrigin_(AppKit.NSMakePoint(face[0] - size.width / 2, face[1] - size.height / 2))
        window.setAlphaValue_(1.0)
        window.setLevel_(LEVEL + 3)              # above the notch it is leaving
        window.orderFrontRegardless()
        self._resize(self.nw, self.nh)           # the notch closes
        pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
        pop.setFromValue_(0.3)
        pop.setToValue_(1.0)
        pop.setDamping_(10.0)
        pop.setStiffness_(240.0)
        pop.setDuration_(pop.settlingDuration())
        hud.orb.body.addAnimation_forKey_(pop, "pull-out")
        hud.orb.burst(gfx.accent(), stars=True, amount=0.6)
        self._grab = None
        print("  [display: the little Mint was pulled out of the notch]", flush=True)
        prefs.set(PREF, False)

    def carry(self) -> None:
        """While the button is still down after the pull: the orb follows the pointer, a little behind."""
        window = self.hud._orb_window
        size = window.frame().size
        mouse = AppKit.NSEvent.mouseLocation()
        target = (mouse.x - size.width / 2, mouse.y - size.height / 2)
        here = window.frame().origin
        window.setFrameOrigin_(AppKit.NSMakePoint(here.x + (target[0] - here.x) * 0.55,
                                                  here.y + (target[1] - here.y) * 0.55))
        self.hud._orb_moved(final=False)

    def carry_end(self) -> None:
        """Released: it settles under the pointer with a hop, and that is where it lives now."""
        self._carrying = False
        window = self.hud._orb_window
        size = window.frame().size
        mouse = AppKit.NSEvent.mouseLocation()
        window.setFrameOrigin_(AppKit.NSMakePoint(mouse.x - size.width / 2, mouse.y - size.height / 2))
        self.hud._orb_moved(final=True)
        self.hud.orb.hop()
        _gone()
        AppHelper.callLater(1.0, lambda: window.setLevel_(AppKit.NSStatusWindowLevel))

    def face_release(self) -> None:
        self._face_reset(animated=True)         # let go early: it springs back

    def _face_reset(self, animated: bool) -> None:
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(not animated)
        if animated:
            Quartz.CATransaction.setAnimationDuration_(0.3)
            Quartz.CATransaction.setAnimationTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                Quartz.kCAMediaTimingFunctionEaseOut))
        scale = self._face_spot[2]
        self.face_host.setAffineTransform_(Quartz.CGAffineTransformMakeScale(scale, scale))
        Quartz.CATransaction.commit()

    def right_clicked(self, event, view) -> None:
        self._popup(event)

    def _popup(self, event) -> None:
        factory = getattr(self.hud, "menu_factory", None)
        menu = factory() if factory else None
        if menu is None:
            return
        if event is not None:
            AppKit.NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self.root)
        else:
            point = AppKit.NSMakePoint(WIN_W / 2, WIN_H - self.size[1] - 4)
            menu.popUpMenuPositioningItem_atLocation_inView_(None, point, self.root)


notch = Notch()


# --- installing into the HUD ---------------------------------------------------------------------

def install(hud) -> bool:
    """HUD.build, before the expressions attach. Hooks the HUD once (the hooks only act while
    Mint is in the notch) and, in notch mode, moves Mint into the notch straight away."""
    notch.hud = hud
    _hook(hud)
    _watch_setting()
    try:
        from mint.ui import notch_agents
        notch_agents.start()                 # Claude mode: coding agents (orb mode shows them as a card)
    except Exception:
        log.exception("Claude mode failed to start")
    if not enabled():
        return False
    try:
        enter(animated=False)
    except Exception:
        log.exception("notch mode failed to build; keeping the orb")
        print("  [notch mode failed to build - using the orb]", flush=True)
        return False
    return True


# --- the HUD's hooks: they route to the notch only while Mint lives there ------------------------

_live = [False]          # Mint is in the notch now
_busy = [False]          # a transition is playing


def active() -> bool:
    return _live[0]


def _hook(hud) -> None:
    if getattr(hud, "_notch_hooked", False):
        return
    hud._notch_hooked = True
    from mint.ui.chat import H as CHAT_H
    from mint.ui.chat import W as CHAT_W
    render_orig, hide_orig = hud._render_bubble, hud._hide_bubble
    chat_orig, home_orig, progress_orig = hud._chat_frame, hud._home_center, hud.progress
    hud._orb_home = home_orig                        # where the floating orb lives

    def render(now):
        if not _live[0]:
            return render_orig(now)
        # The words (word by word) show inside the notch instead of in a bubble beside the orb.
        if hud.chat.is_open:
            notch.show_words(None, None)
            hud._bubble_dirty = False
            hud._notch_chat_seen = True
            return
        if getattr(hud, "_notch_chat_seen", False):
            # The chat just closed: what it showed is read already; don't reopen the notch with it.
            hud._notch_chat_seen = False
            hud._you.clear()
            hud._said.clear()
            if hud._activity is None:
                hud._status = ""
        shown, full, moving = hud._bubble_text(now, bool(prefs.get("word_animation")))
        hud._bubble_moving = moving
        hud._bubble_dirty = moving
        if full.length() == 0:
            hide()
            return
        notch.show_words(shown, full)
        hud._bubble_visible = True

    def hide():
        if not _live[0]:
            return hide_orig()
        notch.show_words(None, None)
        hud._bubble_visible = False

    def chat_frame():
        if not _live[0]:
            return chat_orig()
        visible = notch.screen.visibleFrame()          # the chat opens centred under the notch
        x = min(max(notch.cx - CHAT_W / 2, visible.origin.x + 6), visible.origin.x + visible.size.width - CHAT_W - 6)
        y = max(visible.origin.y + 6, notch.top - notch.nh - 12 - CHAT_H)
        return AppKit.NSMakeRect(x, y, CHAT_W, CHAT_H)

    def home():
        return notch.global_face() if _live[0] else home_orig()

    def progress(done, total, label=""):
        progress_orig(done, total, label)
        if _live[0]:
            AppHelper.callAfter(notch.set_progress, (done / total) if total and done < total else None, label)
    end_orig = hud.activity_end

    def activity_end(name, ok):
        # A failed step: remember what it was and what the user asked, for the error card (shown once the turn
        # settles, so one failed try that Mint recovers from mid-turn doesn't throw it open).
        current = getattr(hud, "_activity", None)
        end_orig(name, ok)
        if _live[0]:
            AppHelper.callAfter(notch.step_ended, ok, (current or {}).get("text") or "")
    hud._render_bubble, hud._hide_bubble = render, hide
    hud._chat_frame, hud._home_center, hud.progress = chat_frame, home, progress
    hud.activity_end = activity_end
    _hook_motion()


def _hook_motion() -> None:
    """In the notch the orb's tricks and trips across the screen do not apply."""
    try:
        from mint.ui.motion import motion
    except Exception:
        return
    message = ("Mint is in notch mode (Dynamic Island) and stays in the notch; switch to orb mode "
               "(display_mode orb) to move it around.")
    for name in ("nudge", "trick", "tricks", "wander", "circle", "visit_quartz"):
        original = getattr(motion, name)

        def gated(*args, _orig=original, _name=name, **kwargs):
            if _live[0]:
                return None if _name == "visit_quartz" else message
            return _orig(*args, **kwargs)
        setattr(motion, name, gated)


# --- into the notch and out again ----------------------------------------------------------------

def enter(animated: bool = True) -> None:
    """Mint moves into the notch. Animated: the orb looks up, crouches, and flies into the notch in a
    spinning arc, shrinking as it goes; the notch opens its mouth, swallows it, and the little Mint
    pops out beside the camera."""
    hud = notch.hud
    if hud is None or _live[0] or _busy[0]:
        return
    if getattr(notch, "panel", None) is None:
        notch.build(hud)
    notch.phase = "entering"
    if getattr(notch, "_dock_ring", None) is not None:
        notch._dock_visuals(False, 0, 0)         # the drop zone's outline and words go as the orb flies in
    notch.panel.orderFrontRegardless()
    if not animated:
        _arrive()
        return
    _busy[0] = True
    from mint.ui.motion import Leg, cubic, hold, in_quad, motion
    start = hud.orb_center()
    target = notch.global_face()
    notch._resize(notch.nw, notch.nh, animate=False)      # it starts as just the notch
    notch.face_host.setOpacity_(0.0)
    distance = math.hypot(target[0] - start[0], target[1] - start[1])
    seconds = min(1.25, max(0.75, distance / 1000))
    up = (start[0] + (target[0] - start[0]) * 0.15, start[1] + 140)
    near = (target[0], target[1] - 90)
    flight = [Leg(hold(start), 0.32, gaze=target, stretch=False, land=0.32),        # look up, crouch
              Leg(cubic(start, up, near, target), seconds, ease=in_quad, spin=1.5,  # launch, faster and faster
                  scale=(1.0, 0.42), free=True, gaze=target)]
    hud.orb.apply_state("awake", bool(prefs.get("voice")))
    AppHelper.callLater(0.32 + seconds * 0.55, lambda: notch._resize(notch.nw + 2 * WING + 22, notch.nh + 16))
    print("  [display: the orb flies into the notch]", flush=True)
    motion._fly(flight, then=_arrive)


def _arrive() -> None:
    """The swap: from here on the little Mint in the notch is Mint's orb."""
    hud = notch.hud
    window = hud._orb_window
    window.orderOut_(None)
    window.setAlphaValue_(0.0)
    notch.old_orb = hud.orb if hud.orb is not notch.orb else getattr(notch, "old_orb", hud.orb)
    hud.orb = notch.orb
    hud.orb.apply_state(getattr(hud, "_state", "awake"), bool(prefs.get("voice")))
    try:
        from mint.ui.emotes import emotes
        if emotes.orb is not None:                   # at start-up they attach to hud.orb themselves, later
            emotes.rebind(notch.orb)
    except Exception:
        log.debug("expressions did not follow into the notch", exc_info=True)
    try:
        hud._hide_bubble()                           # the old bubble, if it was up
    except Exception:
        pass
    _live[0] = True
    face = notch.global_face()                       # the hidden floating window parks on the little Mint,
    size = window.frame().size                       # so the chat, sparks and cards find the notch
    window.setFrameOrigin_(AppKit.NSMakePoint(face[0] - size.width / 2, face[1] - size.height / 2))
    hud._bubble.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
    _hang_island_scenes()
    notch.phase = "on"
    # The little Mint pops out beside the camera.
    notch.face_host.setOpacity_(1.0)
    pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
    pop.setFromValue_(0.2)
    pop.setToValue_(1.0)
    pop.setDamping_(9.0)
    pop.setStiffness_(260.0)
    pop.setDuration_(pop.settlingDuration())
    notch.orb.body.addAnimation_forKey_(pop, "arrive")
    if _busy[0]:
        notch.orb.burst(gfx.accent(), stars=True, amount=0.8)
        AppHelper.callLater(0.35, notch.orb.hop)
    else:
        AppHelper.callLater(0.3, notch.orb.boot)
    _busy[0] = False
    print(f"  [notch mode: Mint lives in the {'notch' if notch.real else 'top of the screen'}]", flush=True)


def leave(animated: bool = True) -> None:
    """Mint comes out of the notch. Animated: the notch bulges and spits the little Mint out; it drops
    from under the notch, growing as it falls, and bounces down to its spot as the floating orb."""
    hud = notch.hud
    if hud is None or not _live[0] or _busy[0]:
        return
    _busy[0] = True
    notch.phase = "leaving"
    home = hud._orb_home()
    face = notch.global_face()
    if animated:
        notch._resize(notch.nw + 2 * WING + 26, notch.nh + 26)       # the notch bulges...
        notch.orb.hop()

    def spit():
        _depart()
        window = hud._orb_window
        size = window.frame().size
        window.setFrameOrigin_(AppKit.NSMakePoint(face[0] - size.width / 2, face[1] - size.height / 2))
        window.setAlphaValue_(1.0)
        window.orderFrontRegardless()
        notch._resize(notch.nw, notch.nh)                             # ...and closes behind it
        if not animated:
            window.setFrameOrigin_(AppKit.NSMakePoint(home[0] - size.width / 2, home[1] - size.height / 2))
            _gone()
            return
        from mint.ui.motion import Leg, hop, motion, out_quad, quad
        drop = (face[0] + (home[0] - face[0]) * 0.25, face[1] - 110)
        far = math.hypot(home[0] - drop[0], home[1] - drop[1])
        flight = [Leg(quad(face, (face[0], face[1] - 40), drop), 0.42, ease=out_quad, spin=-1.0,
                      scale=(0.42, 0.8), free=True),                   # out and down, spinning
                  Leg(hop(drop, home, min(160, 60 + far * 0.15)), min(1.0, 0.5 + far / 1600),
                      scale=(0.8, 1.0), land=0.34),                    # a big bouncing hop home
                  Leg(hop(home, home, 20), 0.3, land=0.2)]             # and a little one
        print("  [display: the orb drops out of the notch]", flush=True)
        motion._fly(flight, then=_gone)
    AppHelper.callLater(0.28 if animated else 0.0, spit)


def _depart() -> None:
    """The swap back: the floating orb is Mint again; the notch lets go of everything."""
    hud = notch.hud
    _live[0] = False
    notch.face_host.setOpacity_(0.0)
    notch.show_words(None, None)
    notch.words.setAlphaValue_(0.0)
    notch.track.setOpacity_(0.0)
    notch.fill.setOpacity_(0.0)
    for _, button in notch.buttons:
        button.setHidden_(True)
        button.setAlphaValue_(0.0)
    for view in (getattr(notch, "_wing_view", None), getattr(notch, "_bar_view", None)):
        if view:
            view.setHidden_(True)               # the agents' wing and bar go with the little Mint
    notch._hide_scene()
    old = getattr(notch, "old_orb", None)
    if old is not None:
        hud.orb = old
        hud.orb.apply_state(getattr(hud, "_state", "awake"), bool(prefs.get("voice")))
        try:
            from mint.ui.emotes import emotes
            emotes.rebind(old)
        except Exception:
            log.debug("expressions did not follow out of the notch", exc_info=True)
    try:
        from mint.ui import look
        look.follow_system(hud._bubble)
    except Exception:
        pass
    _unhang_island_scenes()
    notch.undress()


def _gone() -> None:
    hud = notch.hud
    if getattr(notch, "_carrying", False):
        return                                   # still being carried: carry_end() finishes the exit
    notch.phase = "off"
    _busy[0] = False
    AppHelper.callLater(0.45, lambda: (not _live[0] and notch.phase == "off") and notch.panel.orderOut_(None))
    try:
        hud._orb_moved(final=False)
    except Exception:
        pass
    _emote("wave")
    print("  [orb mode: Mint is the floating orb again]", flush=True)


def _emote(name) -> None:
    try:
        from mint.ui.emotes import emotes
        emotes.play(name)
    except Exception:
        pass


# --- island.py's scenes under the notch -----------------------------------------------------------

_island_saved = {}


def _hang_island_scenes() -> None:
    """island.py's recorder, cards and lessons hang from the notch instead of opening at the orb."""
    try:
        from mint.ui import island as island_module
    except Exception:
        return
    isl = island_module.island
    if getattr(isl, "panel", None) is None:
        AppHelper.callLater(0.5, lambda: _live[0] and _hang_island_scenes())    # not built yet (start-up)
        return
    if not _island_saved:
        _island_saved.update(level=isl.panel.level(), bg=isl.blob.backgroundColor(),
                             border=isl.blob.borderWidth(), shadow=isl.blob.shadowOpacity())
    isl.panel.setLevel_(LEVEL + 1)                    # above the notch's black shape
    isl.blob.setBackgroundColor_(AppKit.NSColor.blackColor().CGColor())     # one piece with the notch
    isl.blob.setBorderWidth_(0.0)
    isl.blob.setShadowOpacity_(0.0)
    try:
        isl.face_host.setHidden_(True)               # the notch has its own little Mint
    except Exception:
        pass
    row = getattr(island_module, "ROW", 40)
    # In the notch, files dragged to it go on the shelf (notch_shelf): the island's own drop target
    # steps aside so the two don't compete. It comes back with the orb.
    drop = getattr(isl, "drop", None)
    if drop is not None and "providers" not in _island_saved:
        _island_saved["providers"] = list(isl.providers)
        isl.providers = [p for p in isl.providers if getattr(p, "__self__", None) is not drop]
    spring = type(isl)._spring

    def gentle(layer, key, old, new, damping=20.0, stiffness=190.0):
        # The same unhurried, overshoot-free spring as the notch, so the scene and the notch grow as one.
        spring(layer, key, old, new, damping=2.0 * math.sqrt(170.0), stiffness=170.0)
    isl._spring = gentle

    def layout(width, height):
        x = notch.cx - width / 2
        if not getattr(isl.scene, "card", False):
            x -= 17        # a compact capsule keeps its (hidden) face slot on the left: centre what shows
        y = notch.top - notch.nh - 6 - height
        isl.face_right, isl.face_top = False, True
        return (x, y, width, height), (x + row / 2, y + height - row / 2)
    isl._layout = layout
    isl._upper = lambda: True


def _unhang_island_scenes() -> None:
    try:
        from mint.ui import island as island_module
    except Exception:
        return
    isl = island_module.island
    for name in ("_layout", "_upper", "_spring"):    # back to the class's own placement and spring
        isl.__dict__.pop(name, None)
    if "providers" in _island_saved:
        isl.providers = _island_saved.pop("providers")
    if _island_saved and getattr(isl, "panel", None) is not None:
        isl.panel.setLevel_(_island_saved["level"])
        isl.blob.setBackgroundColor_(_island_saved["bg"])
        isl.blob.setBorderWidth_(_island_saved["border"])
        isl.blob.setShadowOpacity_(_island_saved["shadow"])
        try:
            isl.face_host.setHidden_(False)
        except Exception:
            pass


# --- switching between the orb and the notch -----------------------------------------------------

_watching = [False]


def _watch_setting() -> None:
    if _watching[0]:
        return
    _watching[0] = True

    def changed(key, value):
        if key == PREF:
            AppHelper.callAfter(_switch, bool(value))
    prefs.on_change(changed)


def _switch(want: bool, tries: int = 0) -> None:
    """Make Mint live in the notch (want) or be the orb, even if another move is still playing: wait for
    it (a flight takes a couple of seconds) and go on, instead of dropping the request - that left the
    setting on "notch" with the orb on screen."""
    if _busy[0]:
        if tries < 40:
            AppHelper.callLater(0.4, lambda: _switch(want, tries + 1))
            return
        log.warning("a display transition never finished; resetting it")
        _busy[0] = False
        notch.phase = "on" if _live[0] else "off"
    if _live[0] == want:
        return
    try:
        if want:
            enter(animated=True)
        else:
            leave(animated=True)
    except Exception:
        log.exception("live switch failed - restarting Mint to change its look")
        _busy[0] = False
        _relaunch(want)


def set_mode(mode: str) -> str:
    """'orb' or 'notch' (from the display_mode tool or the menu)."""
    mode = (mode or "").strip().lower()
    want = mode in ("notch", "island", "dynamic island", "dynamic_island", "top")
    if mode not in ("orb", "circle", "floating", "notch", "island", "dynamic island", "dynamic_island", "top"):
        return "Say orb (the floating circle) or notch (the Dynamic Island at the camera)."
    if bool(prefs.get(PREF)) == want and active() == want and not _busy[0]:
        return f"Mint is already in {'notch' if want else 'orb'} mode."
    if bool(prefs.get(PREF)) == want:
        AppHelper.callAfter(_switch, want)        # the setting already says so but the screen doesn't: redo it
    else:
        prefs.set(PREF, want)
    return ("Flying into the notch now: Mint becomes the Dynamic Island at the camera." if want else
            "Dropping out of the notch now: Mint is the floating orb again.")


RESTART_CODE = 76     # Mint.app (the Ear) brings Mint straight back after any exit other than 0 and 75


def _relaunch(want: bool) -> None:
    """Changing the look rebuilds the HUD: restart Mint.

    Not a normal quit: exit code 0 makes Mint.app (the launcher, Mint Ear) quit too, and a
    relauncher started from Mint was ended with it. So: the same clean-up as quitting, the
    conversation saved (the will-terminate hook), then exit with RESTART_CODE - the launcher
    starts Mint again two seconds later. If the launcher is set to unload instead, a small
    helper asks it to open Mint once it is free."""
    print(f"  [display: switching to {'notch' if want else 'orb'} mode - restarting Mint]", flush=True)
    import sys
    import threading
    post = ("from Foundation import NSDistributedNotificationCenter as C; "
            "C.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_("
            "'local.mint.open', 'console', None, True)")
    subprocess.Popen(["/bin/sh", "-c", f"( sleep 12; '{sys.executable}' -c \"{post}\" ) >/dev/null 2>&1 &"],
                     start_new_session=True)

    def restart():
        time.sleep(2.5)                              # time for the spoken confirmation
        try:
            from mint.app import control
            control.stop()
        except Exception:
            pass
        try:
            from mint.agents.runtime import hub
            hub.cancel("all")
        except Exception:
            pass
        try:
            from mint.app import power
            power._end_children()
            power._quit_engine()
        except Exception:
            log.debug("restart clean-up", exc_info=True)
        done = threading.Event()

        def save():
            try:
                AppKit.NSNotificationCenter.defaultCenter().postNotificationName_object_(
                    AppKit.NSApplicationWillTerminateNotification, AppKit.NSApplication.sharedApplication())
            finally:
                done.set()
        AppHelper.callAfter(save)
        done.wait(8.0)
        os._exit(RESTART_CODE)
    threading.Thread(target=restart, name="mint-display-restart", daemon=True).start()
