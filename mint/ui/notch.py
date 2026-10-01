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
from mint.core import prefs

log = logging.getLogger("mint.ui.notch")

PREF = "notch_mode"
FACE = 20              # the little Mint's diameter
WING = 32              # how far the compact shape reaches out on each side of the camera
EAR = 7                # the concave top corners, where the shape meets the screen edge
MAX_W = 380            # the widest the dropped-down shape gets for words
PAD_X = 16             # text inset from the shape's sides
BTN, STEP = 26, 32     # hover controls: button size, spacing
PLAYER_W, PLAYER_H = 360, 110      # the music player inside the open notch
PAUSED_WINGS = 12.0    # a paused song keeps the notch's wings this long, then the little Mint returns
INNER_OPEN = 0.8                     # resting on the small row's plain part opens the full notch after this
PEEK_AFTER = 0.3
AWAY_HOVERED, AWAY_CLICKED = 0.6, 2.0   # an opened notch folds this long after the pointer leaves (seconds)     # a short rest peeks the controls; a clicked-open notch folds after this long unvisited
FACE_PULL = 30.0                     # how far (points) the little Mint is pulled before it lets go of the notch
HOME_W, BODY_H = 640, 150          # the open notch on hover (boring.notch's size): header row + body
AGENT_H = 196                      # the Agents tab ("Claude mode"): a coding agent's session, live
AGENT_MINI_H = 60                  # ... minimized: one line
AGENT_DONE_OPEN = 6.0              # a finished agent's summary stays open this long
BIG_W, BIG_H = 760, 430            # the notch grown for search results ("show more", 3x3 / 3x2 / 3x1)
SIDE_W = HOME_W - 32 - PLAYER_W - 12   # the right-hand pane: the week calendar or the battery
WIN_W, WIN_H = 900, 860    # room for the notch to wrap cards (two side by side) or the chat under it
# Over the menu bar, as notch apps do (main menu + 3).
LEVEL = Quartz.CGWindowLevelForKey(Quartz.kCGMainMenuWindowLevelKey) + 3


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
        self._face = (self.owner is not None and self.owner.on_face(
            self.convertPoint_fromView_(event.locationInWindow(), None)))
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
        try:
            self.owner.tick()
        except Exception:
            log.exception("notch tick failed")


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
        self.hover_since = 0.0
        self.left_at = 0.0
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
        self.pin_away = AWAY_CLICKED         # how long the pointer may be away before an opened notch folds
        self.pinned = False                  # opened by a click: the full notch stays until clicked again
        self.search_until = 0.0              # Mint just showed files/apps: the notch stays open on Search
        self._paused_since = 0.0             # when the song in the wings was paused
        self._mint_words = ""                # what Mint is saying (the open notch's Mint pane shows it)
        self.allow_key = False
        self.agent_until = 0.0               # an agent finished: the notch shows its summary until then
        self._agents_hooked = False
        self._search_hooked = False
        self._drag_count = -1

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
        host.setFrame_(Quartz.CGRectMake(0, 0, WIN_W, WIN_H))
        box.layer().addSublayer_(host)
        self.face_host = host
        from mint.ui.orb import Orb
        self.orb = Orb(host, self.face_center, FACE)
        for layer in (self.orb.ring, self.orb.spinner, self.orb.progress_ring):   # no outer rings in a tight spot
            layer.removeAllAnimations()
            layer.setHidden_(True)
        self.orb.in_notch = True                # expressions float their extras downwards, inside the notch

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
        for symbol, tip, fn in (("mic.fill", "Microphone on/off", lambda: prefs.toggle("mic")),
                                ("speaker.wave.2.fill", "Spoken replies on/off", lambda: prefs.toggle("voice")),
                                ("bubble.left.and.bubble.right.fill", "Open the chat", self._chat),
                                ("moon.zzz.fill", "Sleep", lambda: self.hud._fire("sleep")),
                                ("eye.slash", "Visible in screen sharing", self._eye),
                                ("sparkles", "Claude mode: your coding agents", self._agents_open),
                                ("slider.horizontal.3", "Settings", self._menu_from_button)):
            act = MintNotchAct.alloc().initWithFn_(fn)
            self._acts.append(act)
            button = MintNotchButton.buttonWithImage_target_action_(gfx.symbol(symbol, 12), act, "fire:")
            button.setBordered_(False)
            button.setToolTip_(tip)
            button.setContentTintColor_(_white(0.85))
            button.setWantsLayer_(True)
            button.layer().setCornerRadius_(BTN / 2)
            button.layer().setBackgroundColor_(_white(0.1).CGColor())
            button.setAlphaValue_(0.0)
            button.setHidden_(True)
            box.addSubview_(button)
            self.buttons.append((symbol, button))

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
        x, y = self.face_center
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
        for layer in (self.shape, self.mask):
            old = (layer.presentationLayer() or layer).path() if animate else None
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setDisableActions_(True)
            layer.setPath_(path)
            Quartz.CATransaction.commit()
            if animate and old is not None:
                spring = Quartz.CASpringAnimation.animationWithKeyPath_("path")
                spring.setFromValue_(old)
                spring.setToValue_(path)
                # An unhurried drop and rise, settling without overshoot (critically damped): nothing
                # swings out past where the shape is going. Wrapping a card, the shape leads (faster),
                # so the card never shows outside it while both grow.
                stiffness = 300.0 if lead else 230.0          # response about 0.5 s (boring.notch's close spring)
                ratio = 1.0
                growing = w * h > self.size[0] * self.size[1] + 1
                if not growing:
                    stiffness = 330.0                         # folding back is quicker, never bouncy
                elif not lead and opened and prefs.get("notch_playful") is not False:
                    ratio = 0.8                               # opening: a breath of overshoot, then it settles
                spring.setStiffness_(stiffness)
                spring.setDamping_(2.0 * ratio * math.sqrt(stiffness))
                spring.setMass_(1.0)
                spring.setDuration_(spring.settlingDuration())
                layer.addAnimation_forKey_(spring, "path")
        self.shape.setShadowOpacity_(0.7 if opened else 0.0)     # black 0.7, radius 6, only when open
        self.shape.setShadowRadius_(6)
        self.size = (w, h)

    # --- the HUD's hooks ------------------------------------------------------------------------

    def show_words(self, shown, full) -> None:
        self.caption = (shown, full) if full is not None and full.length() else None

    def set_progress(self, fraction) -> None:
        self.progress = fraction

    # --- every frame ---------------------------------------------------------------------------

    def tick(self) -> None:
        hud = self.hud
        if hud is None or not getattr(hud, "_built", False) or self.phase != "on":
            return                              # off, or a transition is steering the shape
        now = time.monotonic()
        mouse = AppKit.NSEvent.mouseLocation()
        island = self._island()
        inside = self._inside(mouse)
        near_notch = (abs(mouse.x - self.cx) < self.nw / 2 + WING and mouse.y > self.top - self.nh - 4)
        if inside or near_notch:
            self.left_at = now
            if not self.hover_since:
                self.hover_since = now
        elif now - self.left_at > 0.1:                  # closes a beat after the pointer leaves
            self.hover_since = 0.0
        # The full notch opens and closes by CLICKING it (a click anywhere on it but a button); hovering only
        # ever shows the small row. Left alone for a while, an opened notch folds back by itself.
        if self.pinned and not (inside or near_notch) and now - self.left_at > self.pin_away:
            self.pinned = False
        # Resting on the small row's plain part (not on a button) for a moment is meant: it opens in full.
        # Passing over, or reaching for a button, never does.
        if inside and self.mode == "hover" and not self.pinned:
            frame = self.panel.frame()
            px, py = mouse.x - frame.origin.x, mouse.y - frame.origin.y
            on_button = any(not b.isHidden() and AppKit.NSPointInRect(AppKit.NSMakePoint(px, py),
                                                                      AppKit.NSInsetRect(b.frame(), -5, -5))
                            for _, b in self.buttons)
            if on_button:
                self.inner_since = 0.0
            else:
                self.inner_since = self.inner_since or now
                if now - self.inner_since > INNER_OPEN:
                    self.pinned, self.inner_since, self.pin_away = True, 0.0, AWAY_HOVERED
        else:
            self.inner_since = 0.0
        # Two stages, so crossing the notch on the way somewhere doesn't throw the whole thing open:
        # a short rest shows the few controls that matter; staying on it opens the full notch.
        rested = now - self.hover_since if self.hover_since else 0.0
        peeking = rested > PEEK_AFTER
        hovering = self.pinned
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
        if not self._agents_hooked:
            self._hook_agents()                 # coding agents: the wing, and the notch opens when they need you
        agent_push = (self._agents_need_you() and prefs.get("agent_approvals") is not False) or \
            now < self.agent_until
        self._agent_push = agent_push
        if agent_push and self.tab != "agents" and not hovering:
            self.tab = "agents"
        music = self._music()                 # something playing: the notch becomes the player
        self._no_music_card(music)
        controls = prefs.get("notch_controls") is not False
        # What Mint just brought up (found files, a song starting) opens at once, even while it's still
        # talking: the notch grows into it for a few seconds, then folds back (hover brings it all back).
        pushed = (now < self.search_until and self.tab == "search" or agent_push and self.tab == "agents") \
            and not chat_open
        if shown_island or guests:
            mode = "wrap"
            width, height = self._wrap(island.rect if shown_island else None, guests, compact_w)
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
        elif prefs.get("notch_idle_face") is False and state in ("sleeping", "awake") and not activity:
            mode = "plain"                      # just the notch until something happens
            width, height = self.nw + 8, self.nh    # a hair wider, so the black covers the hardware edge
        else:
            mode = "compact"
            width, height = compact_w, self.nh
        self._watch_drag(mouse)
        if playful and mode in ("open", "hover", "wrap", "music", "home") and self.mode in ("compact", "plain"):
            self.orb.hop()                      # the little Mint hops as the shape opens
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
        wings = bool(music) and mode in ("compact", "plain", "music") and (mode == "music" or not (busy or stale))
        agent_wing = wings and mode != "music" and self._agent_wing()
        self._music_wings(music if wings else None, right=not agent_wing)
        self._home(mode, music)
        self._battery_wings(mode == "battery")
        self._music_player(mode == "music" or (mode == "home" and self.tab == "home" and bool(music)
                                               and not self._mint_words), height,
                           home=(mode == "home"))
        if mode == "battery":
            wings = True                        # the battery owns both wings for the moment
        if not wings or agent_wing:
            self._indicator(state, activity, now)    # (with music on: the agent takes the right wing)
        showing_face = mode != "plain" and not wings
        if (self.face_host.opacity() > 0.5) != showing_face:
            Quartz.CATransaction.begin()
            # Gone at once when something else takes the wing (the battery's words were drawn under a
            # face still fading out); back with a short fade.
            Quartz.CATransaction.setDisableActions_(not showing_face)
            Quartz.CATransaction.setAnimationDuration_(0.2)
            self.face_host.setOpacity_(1.0 if showing_face else 0.0)
            Quartz.CATransaction.commit()
        # Clicks only on the shape itself; everywhere else the window is air.
        self.panel.setIgnoresMouseEvents_(not inside)
        self.mode = mode

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
        for module, attr in (("mint.ui.clipboard_window", "window"), ("mint.ui.image_card", "card")):
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
        player = sys.modules.get("mint.ui.music_player")
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
            return importlib.import_module("mint." + name)
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
        h["tabs"]["agents"] = button("sparkles", "Claude mode: your coding agents", lambda: self._set_tab("agents"),
                                     left + 108, top_y)
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
        h["side"] = None
        calendar = self._mod("notch_calendar") if prefs.get("notch_calendar") is not False else None
        try:
            if calendar is not None and calendar.available():
                h["side"], h["side_update"] = calendar.view(SIDE_W, BODY_H - 16)
            elif battery is not None and battery.available():
                h["side"], h["side_update"] = battery.view(SIDE_W, BODY_H - 16)
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
        self.tab = tab
        if tab == "search":
            self._focus_search()

    # --- Claude mode: coding agents in the notch (notch_agents) ------------------------------------

    def _agents_mod(self):
        if prefs.get("agent_mode") == "off":
            return None
        return self._mod("notch_agents")

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
            # The agent needs you, or it is done: the notch opens on it (the wing tells the rest).
            if kind in ("waiting", "asking"):
                self.tab = "agents"
            elif kind in ("finished", "failed") and prefs.get("agent_open_on_done") is not False:
                self.tab = "agents"
                self.agent_until = time.monotonic() + AGENT_DONE_OPEN
            elif kind == "started" and getattr(self, "orb", None) is not None and self.mode in ("compact", "plain"):
                self.orb.hop()
        try:
            mod.on_event(event)
        except Exception:
            log.debug("agents hook failed", exc_info=True)

    def _agents_need_you(self) -> bool:
        mod = self._agents_mod() if self._agents_hooked else None
        try:
            return mod is not None and mod.needs_you()
        except Exception:
            return False

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
                                                 height))
        elif not view.isHidden():
            self._conceal(view)

    def _agents_open(self) -> None:
        """The small row's sparkles button (or "Claude mode" by voice): the full notch, on the Agents tab."""
        self.tab = "agents"
        self._agents_asked = True
        now = time.monotonic()
        self.agent_until = max(self.agent_until, now + 10.0)    # (asked by voice the pointer is elsewhere)
        self.left_at = now
        self.pinned, self.pin_away = True, AWAY_CLICKED

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
                (self.hover_since or self._typing() or time.monotonic() < self.search_until)
        except Exception:
            return False

    def _typing(self) -> bool:
        return self.tab == "search" and (self.panel.isKeyWindow() or self._holding())

    def _search_dragging(self) -> bool:
        mod = self._mod("notch_search") if "mint.ui.notch_search" in __import__("sys").modules else None
        try:
            return bool(mod is not None and mod.dragging())
        except Exception:
            return False

    def _holding(self) -> bool:
        """Something opened from the notch is up (Quick Look, a menu, a share sheet): don't close under it."""
        if getattr(self, "_menus", 0) > 0 or self._search_dragging():
            return True
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
                self._reveal(view, AppKit.NSMakeRect(WIN_W / 2 - size[0] / 2, body_top - size[1], *size))
            elif not view.isHidden():
                self._conceal(view)
        self.allow_key = on
        if not on and self.panel.isKeyWindow():
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
            return shelf is not None and (shelf.available() or time.monotonic() < self.drag_until)
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
                and not self._typing() and now >= self.agent_until and not self._agents_need_you() \
                and not getattr(self, "_agents_asked", False):
            # A fresh open starts at home (a drag, results or an agent pick the tab); in Claude mode, on the
            # agents while one is at work.
            mod = self._agents_mod() if agents_ok else None
            claude = mod is not None and (prefs.get("agent_mode") == "on" or mod.busy())
            self.tab = "agents" if claude else "home"
        self._agents_asked = False
        body_top = WIN_H - self.nh - 8
        body_bottom = body_top - BODY_H
        left = WIN_W / 2 - HOME_W / 2 + 16
        right = WIN_W / 2 + HOME_W / 2 - 16
        top_y = WIN_H - self.nh / 2
        # header
        search_ok = self._search_ok()
        for name, b in h["tabs"].items():
            show = on and {"home": shelf_ok or search_ok or agents_ok, "shelf": shelf_ok, "search": search_ok,
                           "agents": agents_ok}[name]
            b.setHidden_(not show)
            if show:
                b.layer().setBackgroundColor_(_white(0.16 if self.tab == name else 0.0).CGColor())
                b.setContentTintColor_(_white(0.95 if self.tab == name else 0.55))
        h["gear"].setHidden_(not on)
        h["chat"].setHidden_(not on)
        badge = h["badge"]
        if badge is not None:
            size = badge.frame().size
            badge.setFrameOrigin_(AppKit.NSMakePoint(right - 72 - size.width, top_y - size.height / 2))
            badge.setHidden_(not on)
        # body: home tab
        home = on and self.tab == "home"
        words = getattr(self, "_mint_words", "")
        mint_pane = home and (not music or bool(words))       # Mint talking beats the player
        if mint_pane:
            from mint.ui.hud import TITLES
            state = getattr(self.hud, "_state", "")
            title = "Mic off" if not prefs.get("mic") else (TITLES.get(state, "") or "Listening")
            h["title"].setStringValue_(title)
            h["hint"].setMaximumNumberOfLines_(3 if words else 2)
            h["hint"].setTextColor_(_white(0.85 if words else 0.55))
            h["hint"].setStringValue_(words or ("Say “Hey Mint”, or press the chat button." if prefs.get("mic")
                                                else "Press the chat button, or turn the mic on below."))
            self._reveal(h["title"], AppKit.NSMakeRect(left, body_top - 34, PLAYER_W, 22))
            self._reveal(h["hint"], AppKit.NSMakeRect(left, body_top - 84, PLAYER_W, 46) if words
                         else AppKit.NSMakeRect(left, body_top - 70, PLAYER_W, 32))
        else:
            for key in ("title", "hint"):
                self._fade(h[key], False)
        side = h["side"]
        if side is not None:
            if home:
                if side.isHidden():
                    self._repaint_soon(h.get("side_update"))
                self._reveal(side, AppKit.NSMakeRect(right - SIDE_W, body_bottom + 8, SIDE_W, BODY_H - 16))
            elif not side.isHidden():
                self._conceal(side)
        self._search_pane(on and self.tab == "search", body_top)
        self._agents_pane(on and self.tab == "agents", body_top)
        # body: shelf tab
        shelf = h["shelf"]
        if shelf is not None:
            if on and self.tab == "shelf":
                if getattr(self, "_shelf_dirty", False) or shelf.isHidden():
                    self._shelf_dirty = False
                    self._repaint_soon(h.get("shelf_update"))
                self._reveal(shelf, AppKit.NSMakeRect(WIN_W / 2 - (HOME_W - 24) / 2, body_bottom, HOME_W - 24, BODY_H))
            elif not shelf.isHidden():
                self._conceal(shelf)

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

    def dragged_in(self, pasteboard) -> bool:
        if self._search_dragging():
            return False
        shelf = self._mod("notch_shelf")
        if shelf is None or not shelf.accepts_drag(pasteboard):
            return False
        shelf.set_drag_over(True)
        self.tab = "shelf"
        self.drag_until = time.monotonic() + 0.6
        return True

    def dragged_out(self) -> None:
        shelf = self._mod("notch_shelf")
        if shelf is not None:
            shelf.set_drag_over(False)

    def dropped(self, pasteboard) -> bool:
        shelf = self._mod("notch_shelf")
        if shelf is None:
            return False
        added = shelf.add_paths(shelf.paths_from(pasteboard))
        shelf.set_drag_over(False)
        self.tab, self.drag_until = "shelf", time.monotonic() + 2.5       # stay open to show it landed
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
        player = sys.modules.get("mint.ui.music_player")
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
            self._reveal(player, AppKit.NSMakeRect(x, y, PLAYER_W, PLAYER_H))
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
            self._reveal(self.words, AppKit.NSMakeRect(left - 2, y - th, inner + 4, th))
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
        hover = mode == "hover" or (mode == "home" and self.tab == "home"
                                    and (not self._music() or bool(getattr(self, "_mint_words", ""))))
        # (also when the row is simply not showing: folding the full notch from its Shelf or Search tab
        # leaves the buttons faded out, and the small row came up as an empty black shape)
        row = self._row()
        entering = hover and (self.mode not in ("hover", "home") or self.buttons[0][1].alphaValue() < 0.01)
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
                AppHelper.callLater(0.04 + 0.035 * i, lambda b=button: self.mode in ("hover", "home") and self._pop(b))
            elif not hover:
                self._fade(button, False)
        self._paint_buttons()

    def _row(self) -> list:
        """The small row's buttons that apply now (the Claude mode button only while there are agents)."""
        agents = self._agents_ok()
        if agents and not getattr(self, "_agents_seen", False):
            self._agents_seen = True
            for symbol, button in self.buttons:      # the new button pops in with the others next time
                if symbol == "sparkles":
                    button.setAlphaValue_(0.0)
        return [(sym, b) for sym, b in self.buttons if sym != "sparkles" or agents]

    def _reveal(self, view, frame) -> None:
        """Content arriving as the notch opens: a beat after the shape starts to grow it settles down a few
        points, grows from 96% and fades in, as boring.notch's and Coucou's content do. Already showing:
        it just takes its new frame."""
        concealing = getattr(self, "_concealing", {})
        if view.isHidden() or view.alphaValue() < 0.05 or id(view) in concealing:
            concealing.pop(id(view), None)
            view.setHidden_(False)
            view.setFrame_(AppKit.NSOffsetRect(frame, 0, 8))
            view.setAlphaValue_(0.0)
            AppKit.NSAnimationContext.beginGrouping()
            context = AppKit.NSAnimationContext.currentContext()
            context.setDuration_(0.38)
            context.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithControlPoints____(0.2, 0.9, 0.3, 1.0))
            view.animator().setFrame_(frame)
            view.animator().setAlphaValue_(1.0)
            AppKit.NSAnimationContext.endGrouping()
            layer = view.layer()
            if layer is not None and prefs.get("notch_playful") is not False:
                grow = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
                grow.setFromValue_(0.96)
                grow.setToValue_(1.0)
                grow.setDuration_(0.42)
                grow.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithControlPoints____(0.2, 0.9, 0.3, 1.0))
                layer.addAnimation_forKey_(grow, "reveal")
        elif not AppKit.NSEqualRects(view.frame(), frame):
            view.setFrame_(frame)

    def _conceal(self, view) -> None:
        """Content leaving (a tab change, the notch folding): a quick fade and a slight shrink (0.14 s), then
        hidden - unless it was asked back meanwhile."""
        if view is None or view.isHidden():
            return
        concealing = self.__dict__.setdefault("_concealing", {})
        if id(view) in concealing:
            return
        token = object()
        concealing[id(view)] = token
        AppKit.NSAnimationContext.beginGrouping()
        context = AppKit.NSAnimationContext.currentContext()
        context.setDuration_(0.14)
        context.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseIn))
        view.animator().setAlphaValue_(0.0)
        AppKit.NSAnimationContext.endGrouping()
        layer = view.layer()
        if layer is not None:
            shrink = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
            shrink.setFromValue_(1.0)
            shrink.setToValue_(0.97)
            shrink.setDuration_(0.14)
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
        AppHelper.callLater(0.16, done)

    def _pop(self, button) -> None:
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.16)
        button.animator().setAlphaValue_(1.0)
        AppKit.NSAnimationContext.endGrouping()
        layer = button.layer()
        if layer is not None and prefs.get("notch_playful") is not False:
            # Each control pops up from small with a little spring, one after another.
            layer.setAnchorPoint_(Quartz.CGPointMake(0.5, 0.5))
            frame = button.frame()
            layer.setPosition_(Quartz.CGPointMake(frame.origin.x + frame.size.width / 2,
                                                  frame.origin.y + frame.size.height / 2))
            pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
            pop.setFromValue_(0.55)
            pop.setToValue_(1.0)
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
        red = AppKit.NSColor.systemRedColor()
        for symbol, button in self.buttons:
            if symbol.startswith("mic"):
                button.setImage_(gfx.symbol("mic.fill" if mic else "mic.slash.fill", 13))
                button.setContentTintColor_(_white(0.85) if mic else red)
            elif symbol.startswith("speaker"):
                button.setImage_(gfx.symbol("speaker.wave.2.fill" if voice else "speaker.slash.fill", 13))
                button.setContentTintColor_(_white(0.85) if voice else red)
            elif symbol.startswith("eye"):
                button.setImage_(gfx.symbol("eye.fill" if shown else "eye.slash", 13))
                button.setContentTintColor_(red if shown else _white(0.85))

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
            elif kind == "mic-off":
                glyph, tint = "mic.slash.fill", (1.0, 0.3, 0.3)
            elif kind == "sleep":
                glyph, tint = "moon.zzz.fill", (0.75, 0.78, 0.9)
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

    def _agent_motion(self, kind: str, old: str) -> None:
        """The wing's agent mark moves with the agent: a slow twinkle-spin while it works, a pulse while it
        waits for you, a springy pop when it is done (or a shake when it failed)."""
        glyph = self.glyph
        glyph.removeAnimationForKey_("agent")
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

    # --- clicks --------------------------------------------------------------------------------

    def clicked(self) -> None:
        """A click on the notch (not on one of its buttons) opens the full notch, or folds it back; with the
        chat open it closes the chat. The chat has its own button."""
        if getattr(getattr(self.hud, "chat", None), "is_open", False):
            self._chat()
            return
        self.pinned = not self.pinned
        self.pin_away = AWAY_CLICKED

    def _chat(self) -> None:
        self.hud._fire("console")

    def _eye(self) -> None:
        from mint.ui import sharing
        sharing.set_visible(not sharing.visible())

    def _menu_from_button(self) -> None:
        self._popup(None)

    # --- drag the little Mint out: the notch gives way to the orb ---------------------------------

    def on_face(self, point) -> bool:
        x, y = self.face_center
        return self.phase == "on" and (point.x - x) ** 2 + (point.y - y) ** 2 <= (FACE / 2 + 6) ** 2

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
        # The host layer covers the whole window and turns about its middle: scale about the face instead.
        rx, ry = self.face_center[0] - WIN_W / 2, self.face_center[1] - WIN_H / 2
        t = Quartz.CGAffineTransformIdentity
        t = Quartz.CGAffineTransformTranslate(t, dx * give, dy * give)         # (each call acts first)
        t = Quartz.CGAffineTransformTranslate(t, rx, ry)
        t = Quartz.CGAffineTransformScale(t, stretch, stretch)
        t = Quartz.CGAffineTransformTranslate(t, -rx, -ry)
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
        self.pinned = False
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
        self.face_host.setAffineTransform_(Quartz.CGAffineTransformIdentity)
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
            AppHelper.callAfter(notch.set_progress, (done / total) if total and done < total else None)
    hud._render_bubble, hud._hide_bubble = render, hide
    hud._chat_frame, hud._home_center, hud.progress = chat_frame, home, progress
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
