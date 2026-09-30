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
WING = 38              # how far the compact shape reaches out on each side of the camera
EAR = 7                # the concave top corners, where the shape meets the screen edge
OPEN_W = 440           # the dropped-down shape's width
WIN_W, WIN_H = 700, 300
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
        self.words = self._label(14, _white(0.95), AppKit.NSFontWeightMedium, lines=3)
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
            button = MintNotchButton.buttonWithImage_target_action_(gfx.symbol(symbol, 13), act, "fire:")
            button.setBordered_(False)
            button.setToolTip_(tip)
            button.setContentTintColor_(_white(0.85))
            button.setAlphaValue_(0.0)
            button.setHidden_(True)
            box.addSubview_(button)
            self.buttons.append((symbol, button))

        self._resize(self.nw + 2 * WING, self.nh, animate=False)
        panel.orderFrontRegardless()
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
        radius = 10.0 if h <= self.nh + 2 else 22.0
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
                spring.setDamping_(17.0)
                spring.setStiffness_(210.0)
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
        if hud is None or not getattr(hud, "_built", False):
            return
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
        status = ""
        try:
            status = hud._status_line(now)
        except Exception:
            pass

        if island is not None and island.shown and island.rect is not None:
            mode = "island"
            x, y, w, h = island.rect
            width = max(self.nw + 2 * WING, w + 20)
            height = self.top - y + 10
        elif hovering and not chat_open:
            mode = "hover"
            width, height = OPEN_W, self.nh + (self._text_height() + 64 if self.caption else 70)
        elif (self.caption or activity or self.progress) and not chat_open:
            mode = "open"
            width, height = OPEN_W, self.nh + self._text_height() + 20
        else:
            mode = "compact"
            width, height = self.nw + 2 * WING, self.nh
        self._resize(width, height)
        self._layout_content(mode, status)
        self._indicator(state, activity, now)
        # Clicks only on the shape itself; everywhere else the window is air.
        self.panel.setIgnoresMouseEvents_(not inside)
        self.mode = mode

    def _island(self):
        try:
            from mint.ui import island
            return island.island if getattr(island.island, "panel", None) is not None else None
        except Exception:
            return None

    def _words_height(self, width) -> float:
        # The field draws with a few points of padding: measure a little narrower than it is,
        # or a line that "fits" wraps anyway and the last words are cut off.
        rect = self.caption[1].boundingRectWithSize_options_(
            AppKit.NSMakeSize(width - 10, 400),
            AppKit.NSStringDrawingUsesLineFragmentOrigin | AppKit.NSStringDrawingUsesFontLeading)
        return min(math.ceil(rect.size.height) + 4, 4 * 18 + 4)

    def _text_height(self) -> float:
        # The words already start with the status line (the HUD puts it there).
        height = self._words_height(OPEN_W - 44) if self.caption else 16.0
        if self.progress:
            height += 10
        return height

    def _layout_content(self, mode, status) -> None:
        left = WIN_W / 2 - OPEN_W / 2 + 22
        width = OPEN_W - 44
        y = WIN_H - self.nh - 8
        show_text = mode in ("open", "hover")
        # With no words to show, a status line of its own (e.g. "Listening" while hovering).
        from mint.ui.hud import TITLES
        self.status.setStringValue_(status or TITLES.get(self.hud._state, "") or "Mint")
        self.status.setFrame_(AppKit.NSMakeRect(left, y - 16, width, 16))
        if not self.caption:
            y -= 20
        if self.caption and show_text:
            shown, full = self.caption
            th = self._words_height(width)
            if shown is not getattr(self, "_shown", None):
                self._shown = shown
                self.words.setAttributedStringValue_(shown)
            self.words.setFrame_(AppKit.NSMakeRect(left, y - th, width, th))
            y -= th + 6
        self._fade(self.status, show_text and not self.caption)
        self._fade(self.words, show_text and bool(self.caption))
        # progress
        visible = show_text and bool(self.progress)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.track.setFrame_(Quartz.CGRectMake(left, y - 4, width, 3))
        self.fill.setPosition_(Quartz.CGPointMake(left, y - 2.5))
        self.fill.setBounds_(Quartz.CGRectMake(0, 0, width * float(self.progress or 0), 3))
        self.fill.setBackgroundColor_(gfx.cg(gfx.accent()))
        Quartz.CATransaction.commit()
        self.track.setOpacity_(1.0 if visible else 0.0)
        self.fill.setOpacity_(1.0 if visible else 0.0)
        # controls
        hover = mode == "hover"
        count = len(self.buttons)
        spacing = 44
        start = WIN_W / 2 - (count - 1) * spacing / 2
        for i, (symbol, button) in enumerate(self.buttons):
            below = (self._text_height() + 50) if self.caption else 60
            button.setFrame_(AppKit.NSMakeRect(start + i * spacing - 15, WIN_H - self.nh - below, 30, 30))
            if hover and button.isHidden():
                button.setHidden_(False)
            self._fade(button, hover)
        self._paint_buttons()

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
    """HUD.build, before the expressions attach: in notch mode, move the orb into the notch."""
    if not enabled():
        _watch_setting(False)
        return False
    try:
        notch.build(hud)
    except Exception:
        log.exception("notch mode failed to build; keeping the orb")
        print("  [notch mode failed to build - using the orb]", flush=True)
        _watch_setting(False)
        return False
    old = hud.orb
    hud.orb = notch.orb
    hud.orb.apply_state(getattr(hud, "_state", "starting"), bool(prefs.get("voice")))
    AppHelper.callLater(0.3, hud.orb.boot)
    try:
        old.root.setHidden_(True)
    except Exception:
        pass
    # The floating orb's window stays, hidden, parked on the little Mint: everything that places
    # itself by the orb (the chat, click sparks, critters, cards) then finds the notch.
    face = notch.global_face()
    hud._home_center = lambda: face
    window = hud._orb_window
    size = window.frame().size
    window.setFrameOrigin_(AppKit.NSMakePoint(face[0] - size.width / 2, face[1] - size.height / 2))
    window.orderOut_(None)
    window.setAlphaValue_(0.0)
    try:
        window.orderFrontRegardless = lambda: None     # nothing brings the floating orb back
    except Exception:
        pass
    hud._bubble.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
    _route_words(hud)
    _route_chat(hud)
    _route_progress(hud)
    _still_orb()
    AppHelper.callLater(0.5, _hang_island_scenes)
    _watch_setting(True)
    print(f"  [notch mode: Mint lives in the {'notch' if notch.real else 'top of the screen'}]", flush=True)
    return True


def _route_words(hud) -> None:
    """The words (word by word) show inside the notch instead of in a bubble beside the orb."""
    def render(now):
        if hud.chat.is_open:
            notch.show_words(None, None)
            hud._bubble_dirty = False
            return
        animate = bool(prefs.get("word_animation"))
        shown, full, moving = hud._bubble_text(now, animate)
        hud._bubble_moving = moving
        hud._bubble_dirty = moving
        if full.length() == 0:
            hide()
            return
        notch.show_words(shown, full)
        hud._bubble_visible = True

    def hide():
        notch.show_words(None, None)
        hud._bubble_visible = False
    hud._render_bubble = render
    hud._hide_bubble = hide


def _route_chat(hud) -> None:
    """The chat opens centred under the notch."""
    from mint.ui.chat import H as CHAT_H
    from mint.ui.chat import W as CHAT_W

    def frame():
        visible = notch.screen.visibleFrame()
        x = min(max(notch.cx - CHAT_W / 2, visible.origin.x + 6), visible.origin.x + visible.size.width - CHAT_W - 6)
        y = max(visible.origin.y + 6, notch.top - notch.nh - 12 - CHAT_H)
        return AppKit.NSMakeRect(x, y, CHAT_W, CHAT_H)
    hud._chat_frame = frame


def _route_progress(hud) -> None:
    original = hud.progress

    def progress(done, total, label=""):
        original(done, total, label)
        AppHelper.callAfter(notch.set_progress, (done / total) if total and done < total else None)
    hud.progress = progress


def _still_orb() -> None:
    """The orb's tricks and trips across the screen do not apply: Mint stays in the notch."""
    try:
        from mint.ui.motion import motion
    except Exception:
        return
    message = ("Mint is in notch mode (Dynamic Island) and stays in the notch; switch to orb mode "
               "(display_mode orb) to move it around.")
    motion.nudge = lambda *a, **k: message
    motion.trick = lambda *a, **k: message
    motion.tricks = lambda *a, **k: message
    motion.wander = lambda *a, **k: message
    motion.circle = lambda *a, **k: message
    motion.visit_quartz = lambda *a, **k: None


def _hang_island_scenes() -> None:
    """island.py's recorder, cards and lessons hang from the notch instead of opening at the orb."""
    try:
        from mint.ui import island as island_module
    except Exception:
        return
    isl = island_module.island
    if getattr(isl, "panel", None) is None:
        return
    isl.panel.setLevel_(LEVEL + 1)                    # above the notch's black shape
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


# --- switching between the orb and the notch -----------------------------------------------------

_watching = [False]


def _watch_setting(active: bool) -> None:
    if _watching[0]:
        return
    _watching[0] = True

    def changed(key, value):
        if key == PREF and bool(value) != active:
            AppHelper.callAfter(_relaunch, bool(value))
    prefs.on_change(changed)


def set_mode(mode: str) -> str:
    """'orb' or 'notch' (from the display_mode tool or the menu)."""
    mode = (mode or "").strip().lower()
    want = mode in ("notch", "island", "dynamic island", "dynamic_island", "top")
    if mode not in ("orb", "circle", "floating", "notch", "island", "dynamic island", "dynamic_island", "top"):
        return "Say orb (the floating circle) or notch (the Dynamic Island at the camera)."
    if bool(prefs.get(PREF)) == want:
        return f"Mint is already in {'notch' if want else 'orb'} mode."
    prefs.set(PREF, want)
    return (f"Switching to {'notch mode: Mint moves into the camera notch like a Dynamic Island' if want else 'orb mode: the floating orb comes back'}. "
            "Mint restarts in a few seconds to change its look - say one short sentence, then stop.")


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
