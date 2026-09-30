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
WIN_W, WIN_H = 760, 820    # room for the notch to wrap a card or the chat hanging under it
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
        return False

    def canBecomeMainWindow(self):
        return False

    def constrainFrameRect_toScreen_(self, rect, screen):
        return rect                      # it belongs over the menu bar, at the very top


class MintNotchView(AppKit.NSView):
    owner = None

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        if self.owner is not None:
            self.owner.clicked()

    def rightMouseDown_(self, event):
        if self.owner is not None:
            self.owner.right_clicked(event, self)


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
        self.phase = "off"                  # off / entering / on / leaving

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
        self.shape.setShadowRadius_(12)
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
        for layer in (self.orb.ring, self.orb.spinner):          # no outer rings in a tight spot
            layer.removeAllAnimations()
            layer.setHidden_(True)

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
        self._screens = center.addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationDidChangeScreenParametersNotification, None, None, lambda note: self._moved())

    def _label(self, size, color, weight, lines=1):
        field = AppKit.NSTextField.wrappingLabelWithString_("")
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
        return (self.cx - w / 2 - EAR, self.top - h, w + 2 * EAR, h)

    def _inside(self, point) -> bool:
        x, y, w, h = self._rect_screen()
        return x <= point.x <= x + w and y <= point.y <= y + h

    def _resize(self, w, h, animate=True) -> None:
        w, h = round(w), round(h)
        if (w, h) == self.size:
            return
        radius = 10.0 if h <= self.nh + 2 else (26.0 if h > 180 else 20.0)
        path = island_path(WIN_W, WIN_H, w, h, r=radius)
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
                playful = prefs.get("notch_playful") is not False
                spring.setDamping_(13.5 if playful else 24.0)          # a little bounce, or none
                spring.setStiffness_(240.0 if playful else 280.0)
                spring.setMass_(1.0)
                spring.setDuration_(spring.settlingDuration())
                layer.addAnimation_forKey_(spring, "path")
        opened = h > self.nh + 2
        self.shape.setShadowOpacity_(0.45 if opened else 0.0)
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
        elif now - self.left_at > 0.35:
            self.hover_since = 0.0
        hovering = bool(self.hover_since) and now - self.hover_since > 0.18
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
        if shown_island or guests:
            mode = "wrap"
            width, height = self._wrap(island.rect if shown_island else None, guests, compact_w)
        elif hovering and prefs.get("notch_controls") is not False and not chat_open:
            mode = "hover"
            text_w, text_h = self._text_size(caption)
            row_w = len(self.buttons) * STEP + 20
            width = max(compact_w, row_w, text_w + 2 * PAD_X)
            height = self.nh + (text_h + 10 if caption else 4) + BTN + 14
        elif (caption or self.progress) and not chat_open:
            mode = "open"
            text_w, text_h = self._text_size(caption)
            width = max(compact_w, text_w + 2 * PAD_X)
            height = self.nh + text_h + 18 + (8 if self.progress else 0)
        elif prefs.get("notch_idle_face") is False and state in ("sleeping", "awake") and not activity:
            mode = "plain"                      # just the notch until something happens
            width, height = self.nw, self.nh
        else:
            mode = "compact"
            width, height = compact_w, self.nh
        if playful and mode in ("open", "hover", "wrap") and self.mode in ("compact", "plain"):
            self.orb.hop()                      # the little Mint hops as the shape opens
        self._resize(width, height)
        self._breathe(state, mode, playful)
        self._layout_content(mode, caption, width, height)
        self._indicator(state, activity, now)
        showing_face = mode != "plain"
        self.face_host.setOpacity_(1.0 if showing_face else 0.0)
        # Clicks only on the shape itself; everywhere else the window is air.
        self.panel.setIgnoresMouseEvents_(not inside)
        self.mode = mode

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
        self._styled.clear()
        self.panel.setIgnoresMouseEvents_(True)

    def _wrap(self, island_rect, guests, compact_w):
        """Stack the island's scene and the guests under the notch (never on top of each other)
        and return the shape's size that wraps them all."""
        rects = []
        below = self.top - self.nh - 6
        if island_rect is not None:
            rects.append(island_rect)
            below = island_rect[1] - 8
        for window in sorted(guests, key=lambda w: -w.frame().origin.y):
            f = window.frame()
            if f.origin.y + f.size.height > below + 1:          # would overlap what is above: move down
                window.setFrameOrigin_(AppKit.NSMakePoint(f.origin.x, below - f.size.height))
                f = window.frame()
            rects.append((f.origin.x, f.origin.y, f.size.width, f.size.height))
            below = f.origin.y - 8
        reach = max(max(abs(x - self.cx), abs(x + w - self.cx)) for x, y, w, h in rects)
        width = max(compact_w, 2 * reach + 16)
        height = self.top - min(y for x, y, w, h in rects) + 8
        return min(width, WIN_W - 2 * EAR - 2), min(height, WIN_H)

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
            self.words.setFrame_(AppKit.NSMakeRect(left - 2, y - th, inner + 4, th))
            y -= th + 4
        self._fade(self.status, False)
        self._fade(self.words, show_text and bool(caption))
        visible = show_text and bool(self.progress)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.track.setFrame_(Quartz.CGRectMake(left, y - 5, inner, 3))
        self.fill.setPosition_(Quartz.CGPointMake(left, y - 3.5))
        self.fill.setBounds_(Quartz.CGRectMake(0, 0, inner * float(self.progress or 0), 3))
        self.fill.setBackgroundColor_(gfx.cg(gfx.accent()))
        Quartz.CATransaction.commit()
        self.track.setOpacity_(1.0 if visible else 0.0)
        self.fill.setOpacity_(1.0 if visible else 0.0)
        # Controls: one row along the bottom, popping in one after another.
        hover = mode == "hover"
        entering = hover and self.mode != "hover"
        start = WIN_W / 2 - (len(self.buttons) - 1) * STEP / 2
        row_y = WIN_H - height + 9
        for i, (symbol, button) in enumerate(self.buttons):
            button.setFrame_(AppKit.NSMakeRect(start + i * STEP - BTN / 2, row_y, BTN, BTN))
            if entering:
                button.setHidden_(False)
                button.setAlphaValue_(0.0)
                AppHelper.callLater(0.04 + 0.035 * i, lambda b=button: self.mode == "hover" and self._pop(b))
            elif not hover:
                self._fade(button, False)
        self._paint_buttons()

    def _pop(self, button) -> None:
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.16)
        button.animator().setAlphaValue_(1.0)
        AppKit.NSAnimationContext.endGrouping()

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
        accent = gfx.accent()
        if kind != self._ind:
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
            self.glyph.setHidden_(glyph is None)
            if glyph:
                self.glyph_mask.setContents_(gfx.symbol(glyph, 13, "bold"))
                self.glyph.setBackgroundColor_(gfx.cg(tint))
            Quartz.CATransaction.commit()
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

    # --- clicks --------------------------------------------------------------------------------

    def clicked(self) -> None:
        self._chat()

    def _chat(self) -> None:
        self.hud._fire("console")

    def _eye(self) -> None:
        from mint.ui import sharing
        sharing.set_visible(not sharing.visible())

    def _menu_from_button(self) -> None:
        self._popup(None)

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
            return
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
    notch.phase = "off"
    _busy[0] = False
    AppHelper.callLater(0.45, lambda: (not _live[0]) and notch.panel.orderOut_(None))
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

    def layout(width, height):
        x = notch.cx - width / 2
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
    for name in ("_layout", "_upper"):               # back to the class's own placement
        isl.__dict__.pop(name, None)
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


def _switch(want: bool) -> None:
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
    if bool(prefs.get(PREF)) == want and active() == want:
        return f"Mint is already in {'notch' if want else 'orb'} mode."
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
