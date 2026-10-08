"""Set up from the notch: small chips on the open notch's home tab for what isn't set up yet.

    ( •‿• )  Asleep
             Say "Hey Mint", or press the chat button.
    Set up  [📅 Calendar ×] [👆 Click & type ×] [✦ Claude Code ×]
    🎙 🔊 💬 🌙 👁 ⤒ ⚙

Only while Mint is idle (not talking, no reply showing), and only for what is missing: Calendars,
Accessibility, Screen Recording, Reminders (permissions.py asks, the same as the welcome window),
Claude Code installed but not connected (notch_agents' Connect), the shelf never used (opens the
Shelf tab: drop files, AirDrop), and no phone remote (Settings ▸ Accounts). Most important first,
at most MAX. A chip's × hides it for good (pref home_tips_dismissed).

pick() is the pure part (which chips, in what order); facts() reads the statuses (any thread - the
notch calls it off the main thread); Strip draws the chips (main thread).
"""

from __future__ import annotations

import logging

from mint.core import prefs

log = logging.getLogger("mint.ui.notch_tips")

PREF = "home_tips_dismissed"
MAX = 4                       # at most this many chips (fewer when they don't fit the row)
# (key, SF Symbol, rgb, label, tooltip) - most important first.
TIPS = (
    ("calendar", "calendar", (1.0, 0.36, 0.33), "Calendar",
     "Connect your calendars: I'll tell you what's next and add events."),
    ("accessibility", "hand.point.up.left.fill", (0.36, 0.56, 1.0), "Click & type",
     "Let me click, type and arrange windows for you (Accessibility)."),
    ("screen", "rectangle.dashed.badge.record", (0.64, 0.47, 1.0), "See screen",
     "Let me see your screen when you ask about it (Screen Recording)."),
    ("claude", "sparkles", (0.98, 0.56, 0.36), "Claude Code",
     "Connect Claude Code: answer its questions and permission requests from the notch."),
    ("shelf", "tray.and.arrow.down.fill", (0.25, 0.72, 1.0), "AirDrop",
     "The shelf: drop files on the notch to keep them handy, then drag them out, AirDrop or share them."),
    ("reminders", "checklist", (0.3, 0.86, 0.55), "Reminders",
     "Connect Reminders: I'll read and add your reminders."),
    ("phone", "iphone", (0.2, 0.8, 0.72), "Phone remote",
     "Control me from your phone, by Telegram or email (Settings ▸ Accounts & connections)."),
)
PERMISSION_TIPS = ("calendar", "accessibility", "screen", "reminders")
_asked: set = set()           # permissions asked for from here (a second click opens System Settings)


def needed(key: str, facts: dict) -> bool:
    """Is `key` not set up yet? A fact not known yet counts as set up (no chip until it is checked)."""
    if key in PERMISSION_TIPS:
        return facts.get(key, "allowed") != "allowed"
    if key == "claude":
        return bool(facts.get("claude_installed")) and not facts.get("claude_connected", True)
    if key == "shelf":
        return not facts.get("shelf_used", True)
    if key == "phone":
        return not facts.get("remote_on", True)
    return False


def pick(facts: dict, dismissed=(), limit: int = MAX) -> list[str]:
    """The chips to show, most important first: not set up, not dismissed, at most `limit`."""
    gone = set(dismissed or ())
    return [key for key, *_ in TIPS if key not in gone and needed(key, facts)][:max(0, limit)]


def tip(key: str) -> tuple:
    return next(t for t in TIPS if t[0] == key)


def dismissed() -> list[str]:
    value = prefs.get(PREF)
    return [str(k) for k in value] if isinstance(value, (list, tuple)) else []


def dismiss(key: str) -> None:
    now = dismissed()
    if key not in now:
        prefs.set(PREF, now + [key])


def facts() -> dict:
    """What is set up (any thread; never prompts). Each part on its own: one failing leaves it unknown."""
    out: dict = {}
    try:
        from mint.core import permissions
        for kind in PERMISSION_TIPS:
            out[kind] = permissions.status(kind, _asked)
    except Exception:
        log.debug("permission statuses", exc_info=True)
    try:
        from mint.tools import agent_watch
        out["claude_installed"] = bool(agent_watch.installed().get("claude")) and prefs.get("agent_mode") != "off"
        from mint.tools import agent_hooks
        out["claude_connected"] = bool(agent_hooks.installed())
    except Exception:
        log.debug("claude code status", exc_info=True)
    try:
        from mint.ui import notch_shelf
        out["shelf_used"] = notch_shelf.STORE.exists() or bool(notch_shelf.paths())
    except Exception:
        log.debug("shelf status", exc_info=True)
    out["remote_on"] = bool(prefs.get("telegram_enabled") or prefs.get("email_enabled"))
    try:
        from mint.ui import brands
        brands.warm(list(BRAND.values()))          # (finding the apps asks LaunchServices: not on the main thread)
    except Exception:
        log.debug("brands", exc_info=True)
    return out


def ask(kind: str, done=None) -> None:
    """Main thread (a click): macOS's own prompt the first time, else its pane in System Settings. done(ok) gets
    the answer to Calendars' and Reminders' box (any thread)."""
    from mint.core import permissions
    permissions.ask(kind, _asked, done)




def will_prompt(key: str) -> bool:
    """Will a click show macOS's own box (True), or open System Settings (False)?"""
    if key not in PERMISSION_TIPS or key in _asked:
        return False
    try:
        from mint.core import permissions
        return permissions.status(key, _asked) == "ask"
    except Exception:
        return False


# What the Mint pane says while a chip waits for an answer, and once it's set up: (title, words).
WAITING = {
    "calendar": ("Connecting Calendar", "Choose “Allow Full Access” in the box macOS just opened."),
    "reminders": ("Connecting Reminders", "Choose “Allow Full Access” in the box macOS just opened."),
    "screen": ("Letting me see the screen", "In System Settings, turn on Hey Mint under Screen Recording."),
    "accessibility": ("Letting me click and type", "In System Settings, turn on Hey Mint under Accessibility."),
    "claude": ("Connecting Claude Code", "Say yes in the box, and I'll hook into Claude Code."),
}
SETTINGS_WORDS = "In System Settings, turn on Hey Mint. I'll notice right away."
DONE = {
    "calendar": ("Calendar connected", "Your week is on the right. Ask “what's next today?”"),
    "reminders": ("Reminders connected", "Try “remind me to call Sam at 5”."),
    "screen": ("I can see your screen", "Ask “what's on my screen?” whenever you like."),
    "accessibility": ("I can click and type", "Ask me to fill a form, or tidy your windows."),
    "claude": ("Claude Code connected", "Its questions and permission requests show up here."),
}
DENIED = ("Not allowed yet", "Click it again to open System Settings and turn me on there.")


def waiting_note(key: str, prompt: bool) -> tuple:
    title, words = WAITING.get(key, (f"Setting up {tip(key)[3]}", SETTINGS_WORDS))
    if key in ("calendar", "reminders") and not prompt:
        words = SETTINGS_WORDS
    return title, words


def done_note(key: str) -> tuple:
    return DONE.get(key, (f"{tip(key)[3]} is set up", "All done."))


# --- the chips (main thread) -------------------------------------------------------------------------------

H = 24.0                      # a chip's height
GAP = 5.0
_classes: dict = {}


def _scale_about_centre(layer, size, frm: float, to: float, preset: str = "snappy") -> None:
    """Scale a view's layer about its centre (a layer-backed view's anchor is its corner)."""
    import Quartz
    from mint.ui import kinetics
    w, h = size

    def m(k):
        t = Quartz.CATransform3DMakeTranslation(-w / 2, -h / 2, 0)
        t = Quartz.CATransform3DConcat(t, Quartz.CATransform3DMakeScale(k, k, 1))
        return Quartz.CATransform3DConcat(t, Quartz.CATransform3DMakeTranslation(w / 2, h / 2, 0))
    import AppKit
    kinetics.spring(layer, "transform", AppKit.NSValue.valueWithCATransform3D_(m(frm)),
                    AppKit.NSValue.valueWithCATransform3D_(m(to)), preset, anim_key="tip-scale")


def _chip_class():
    """MintNotchTipChip and friends, defined once (Objective-C class names are global)."""
    if "chip" in _classes:
        return _classes["chip"]
    import AppKit
    import objc

    class MintNotchTipStrip(AppKit.NSView):
        """The row: a click between chips or on "Set up" lands here and goes nowhere (it used to reach the notch
        underneath, which took it as "fold")."""

        def acceptsFirstMouse_(self, event):
            return True

        def hitTest_(self, point):
            hit = objc.super(MintNotchTipStrip, self).hitTest_(point)
            if hit is None:
                return None
            return hit if isinstance(hit, (MintNotchTipChip, MintNotchTipClose)) else self

        def mouseDown_(self, event):
            pass

        def mouseUp_(self, event):
            pass

    class MintNotchTipChip(AppKit.NSView):
        """A chip: the whole of it is one click (its icon and words never take it), the × another."""

        def acceptsFirstMouse_(self, event):
            return True

        def hitTest_(self, point):
            hit = objc.super(MintNotchTipChip, self).hitTest_(point)
            if hit is None:
                return None
            close = getattr(self, "close", None)
            return hit if close is not None and hit is close else self

        def updateTrackingAreas(self):
            objc.super(MintNotchTipChip, self).updateTrackingAreas()
            for area in list(self.trackingAreas()):
                self.removeTrackingArea_(area)
            options = (AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways
                       | AppKit.NSTrackingInVisibleRect)
            self.addTrackingArea_(AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                self.bounds(), options, self, None))

        def mouseEntered_(self, event):
            self.hover = True
            self.paint_(True)
            if getattr(self, "on_hover", None) is not None:
                self.on_hover(self.key)

        def mouseExited_(self, event):
            self.hover = False
            self.paint_(False)
            if getattr(self, "on_hover", None) is not None:
                self.on_hover(None)

        def mouseDown_(self, event):
            self.pressed = getattr(self, "state", "") not in ("busy", "done", "leaving")
            if self.pressed:
                _scale_about_centre(self.layer(), (self.frame().size.width, H), 1.0, 0.94)

        def mouseUp_(self, event):
            if not getattr(self, "pressed", False):
                return
            self.pressed = False
            _scale_about_centre(self.layer(), (self.frame().size.width, H), 0.94, 1.0, "bouncy")
            if getattr(self, "state", "") in ("busy", "done", "leaving"):
                return
            point = self.convertPoint_fromView_(event.locationInWindow(), None)
            if AppKit.NSPointInRect(point, self.bounds()) and getattr(self, "fn", None) is not None:
                try:
                    self.fn()
                except Exception:
                    log.exception("notch tip failed")

        def paint_(self, hover):
            rgb = self.rgb
            self.layer().setBackgroundColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                rgb[0] * 0.25, rgb[1] * 0.25, rgb[2] * 0.25, 1.0 if hover else 0.85).CGColor())
            self.layer().setBorderColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                rgb[0], rgb[1], rgb[2], 0.75 if hover else 0.4).CGColor())

    class MintNotchTipAct(AppKit.NSObject):
        def initWithFn_(self, fn):
            self = objc.super(MintNotchTipAct, self).init()
            if self is not None:
                self.fn = fn
            return self

        def fire_(self, sender):
            try:
                self.fn()
            except Exception:
                log.exception("notch tip dismiss failed")

    class MintNotchTipClose(AppKit.NSButton):
        def acceptsFirstMouse_(self, event):
            return True

    _classes.update(chip=MintNotchTipChip, act=MintNotchTipAct, close=MintNotchTipClose, strip=MintNotchTipStrip)
    return MintNotchTipChip


# Tips about a brand show its real mark (brands.py: the logo file or the app's own icon); AirDrop shows macOS's own
# AirDrop icon. The rest (macOS permissions, the phone) keep their SF Symbol in a coloured dot.
BRAND = {"calendar": "calendar_app", "claude": "anthropic"}


def _mark(key: str):
    try:
        if key == "shelf":
            from mint.ui import notch_shelf
            service = notch_shelf.airdrop_service()
            return service.image() if service is not None else None
        if key in BRAND:
            from mint.ui import brands
            return brands.brand_image(BRAND[key], 16)
    except Exception:
        log.debug("no mark for %s", key, exc_info=True)
    return None


class Strip:
    """A row of chips, left to right: "Set up" and one chip per tip that fits `width`. Changes animate: a new
    chip pops in, a finished one turns green with a tick and shrinks away, the rest glide over to close the gap."""

    def __init__(self, width: float) -> None:
        import AppKit
        _chip_class()
        self.width = width
        self.keys: list = []
        self.shown: list = []
        self.chips: dict = {}         # key -> chip view
        self.acts: list = []
        self.on_hover = None          # fn(key or None): the pointer went onto a chip / off it
        self.view = _classes["strip"].alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, width, H))
        self.view.setWantsLayer_(True)
        self.caption = self._text("Set up", 10.5, (1.0, 1.0, 1.0), 0.5, AppKit.NSFontWeightSemibold)
        self.view.addSubview_(self.caption)

    def _text(self, words, size, rgb, alpha, weight):
        import AppKit
        field = AppKit.NSTextField.labelWithString_(words)
        field.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, weight))
        field.setTextColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha))
        field.setSelectable_(False)
        field.sizeToFit()
        return field

    def _chip(self, key, clicked, closed):
        import AppKit
        from mint.ui import gfx
        chip_cls, act_cls, close_cls = _classes["chip"], _classes["act"], _classes["close"]
        _, symbol, rgb, label, why = tip(key)
        words = self._text(label, 10.5, (1.0, 1.0, 1.0), 0.92, AppKit.NSFontWeightSemibold)
        tw = words.frame().size.width
        w = 3 + 16 + 4 + tw + 2 + 10 + 3          # dot, words, ×
        chip = chip_cls.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, w, H))
        chip.setWantsLayer_(True)
        chip.layer().setCornerRadius_(H / 2)
        chip.layer().setBorderWidth_(1.0)
        chip.key, chip.rgb, chip.state, chip.hover = key, rgb, "", False
        chip.on_hover = lambda k: self.on_hover(k) if self.on_hover is not None else None
        chip.paint_(False)
        chip.fn = (lambda k=key: clicked(k))
        dot = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(3, (H - 16) / 2, 16, 16))
        dot.setWantsLayer_(True)
        mark = _mark(key)
        if mark is not None:                       # the real thing: Calendar's icon, Claude's, AirDrop's
            icon = AppKit.NSImageView.imageViewWithImage_(mark)
            icon.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        else:
            dot.layer().setCornerRadius_(8)
            dot.layer().setBackgroundColor_(gfx.cg(rgb, 0.28))
            icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol(symbol, 8.5, "bold"))
            icon.setContentTintColor_(gfx.ns(gfx.light(rgb)))
        icon.setFrame_(AppKit.NSMakeRect(0, 0, 16, 16))
        dot.addSubview_(icon)
        chip.addSubview_(dot)
        size = words.frame().size
        words.setFrameOrigin_(AppKit.NSMakePoint(23, round((H - size.height) / 2)))
        chip.addSubview_(words)
        act = act_cls.alloc().initWithFn_(lambda k=key: closed(k))
        self.acts.append(act)
        close = close_cls.buttonWithImage_target_action_(gfx.symbol("xmark", 7, "bold"), act, "fire:")
        close.setBordered_(False)
        close.setContentTintColor_(gfx.ns((1.0, 1.0, 1.0), 0.4))
        close.setFrame_(AppKit.NSMakeRect(w - 3 - 12, (H - 14) / 2, 12, 14))
        chip.addSubview_(close)
        chip.close, chip.dot, chip.icon, chip.words = close, dot, icon, words
        return chip

    def set(self, keys, clicked, closed) -> list:
        """Show these tips (main thread); clicked(key) / closed(key) on a click. Returns the keys that fit."""
        keys = list(keys)
        if keys == self.keys:
            return self.shown
        first = not self.keys and not self.chips
        self.keys = keys
        import AppKit
        cap = self.caption.frame().size
        self.caption.setFrameOrigin_(AppKit.NSMakePoint(2, round((H - cap.height) / 2)))
        x = 2 + cap.width + 6
        shown, born = [], []
        for key in keys:
            chip = self.chips.get(key)
            if chip is None:
                chip = self._chip(key, clicked, closed)
            w = chip.frame().size.width
            if x + w > self.width:
                break
            if key not in self.chips:
                chip.setFrameOrigin_(AppKit.NSMakePoint(x, 0))
                self.view.addSubview_(chip)
                self.chips[key] = chip
                born.append(chip)
            elif abs(chip.frame().origin.x - x) > 0.5:
                self._glide(chip, x)
            shown.append(key)
            x += w + GAP
        for key in [k for k in self.chips if k not in shown]:
            self._leave(self.chips.pop(key))
        if not first:                                  # (the first set rises with the notch: _reveal)
            from mint.ui import kinetics
            for i, chip in enumerate(born):
                if kinetics.reduce_motion():
                    kinetics.basic(chip.layer(), "opacity", 0.0, 1.0, 0.18, anim_key="tip-in")
                else:
                    kinetics.basic(chip.layer(), "opacity", 0.0, 1.0, 0.16, 0.05 * i, anim_key="tip-in")
                    _scale_about_centre(chip.layer(), (chip.frame().size.width, H), 0.6, 1.0, "bouncy")
        self.shown = shown
        return shown

    def _glide(self, chip, x: float) -> None:
        import AppKit
        import Quartz
        from mint.ui import kinetics
        if kinetics.reduce_motion():
            chip.setFrameOrigin_(AppKit.NSMakePoint(x, 0))
            return
        AppKit.NSAnimationContext.beginGrouping()
        ctx = AppKit.NSAnimationContext.currentContext()
        ctx.setDuration_(0.38)
        ctx.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithControlPoints____(0.2, 0.9, 0.25, 1.0))
        chip.animator().setFrameOrigin_(AppKit.NSMakePoint(x, 0))
        AppKit.NSAnimationContext.endGrouping()

    def _leave(self, chip) -> None:
        """Shrink and fade away, then gone (it takes no clicks meanwhile)."""
        from PyObjCTools import AppHelper
        from mint.ui import kinetics
        chip.state = "leaving"
        chip.close.setEnabled_(False)
        if chip.hover and self.on_hover is not None:
            self.on_hover(None)
        layer = chip.layer()
        if layer is not None:
            kinetics.basic(layer, "opacity", layer.opacity(), 0.0, 0.2, anim_key="tip-out")
            if not kinetics.reduce_motion():
                _scale_about_centre(layer, (chip.frame().size.width, H), 1.0, 0.55, "snappy")
        AppHelper.callLater(0.26, chip.removeFromSuperview)

    # --- a chip's moment: waiting for macOS's answer, set up, not allowed ---------------------------------

    def busy(self, key: str, on: bool) -> None:
        """Waiting: a ring spins round the chip's icon and its border breathes."""
        import Quartz
        from mint.ui import gfx
        from mint.ui import kinetics
        chip = self.chips.get(key)
        if chip is None or chip.state in ("done", "leaving"):
            return
        ring = getattr(chip, "ring", None)
        if on:
            chip.state = "busy"
            if ring is None:
                ring = Quartz.CAShapeLayer.layer()
                ring.setBounds_(Quartz.CGRectMake(0, 0, 20, 20))
                ring.setPosition_(Quartz.CGPointMake(3 + 8, H / 2))
                ring.setPath_(Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(1, 1, 18, 18), None))
                ring.setFillColor_(None)
                ring.setLineWidth_(1.8)
                ring.setLineCap_(Quartz.kCALineCapRound)
                ring.setStrokeEnd_(0.3)
                ring.setStrokeColor_(gfx.cg(gfx.light(chip.rgb)))
                chip.layer().addSublayer_(ring)
                chip.ring = ring
            ring.setHidden_(False)
            spin = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
            spin.setFromValue_(0.0)
            spin.setToValue_(-6.283)
            spin.setDuration_(0.9)
            spin.setRepeatCount_(float("inf"))
            ring.addAnimation_forKey_(spin, "spin")
            chip.dot.setAlphaValue_(0.55)
            if not kinetics.reduce_motion():
                pulse = Quartz.CABasicAnimation.animationWithKeyPath_("borderColor")
                pulse.setFromValue_(gfx.cg(chip.rgb, 0.35))
                pulse.setToValue_(gfx.cg(gfx.light(chip.rgb), 0.95))
                pulse.setDuration_(0.7)
                pulse.setAutoreverses_(True)
                pulse.setRepeatCount_(float("inf"))
                chip.layer().addAnimation_forKey_(pulse, "tip-breathe")
        else:
            if chip.state == "busy":
                chip.state = ""
            if ring is not None:
                ring.removeAllAnimations()
                ring.setHidden_(True)
            chip.dot.setAlphaValue_(1.0)
            chip.layer().removeAnimationForKey_("tip-breathe")

    def done(self, key: str) -> None:
        """Set up: the chip turns green, its icon becomes a tick, and it pops."""
        import AppKit
        import Quartz
        from mint.ui import gfx
        from mint.ui import kinetics
        chip = self.chips.get(key)
        if chip is None or chip.state in ("done", "leaving"):
            return
        self.busy(key, False)
        chip.state = "done"
        chip.close.setHidden_(True)
        green = gfx.GREEN
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.25)
        chip.layer().setBackgroundColor_(gfx.cg(tuple(c * 0.3 for c in green), 1.0))
        chip.layer().setBorderColor_(gfx.cg(green, 0.9))
        Quartz.CATransaction.commit()
        tick = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("checkmark.circle.fill", 13, "bold"))
        tick.setContentTintColor_(gfx.ns(green))
        tick.setFrame_(AppKit.NSMakeRect(0, 0, 16, 16))
        for sub in list(chip.dot.subviews()):
            sub.removeFromSuperview()
        chip.dot.layer().setBackgroundColor_(None)
        chip.dot.addSubview_(tick)
        if not kinetics.reduce_motion():
            _scale_about_centre(chip.layer(), (chip.frame().size.width, H), 0.82, 1.0, "bouncy")

    def nope(self, key: str) -> None:
        """Not allowed: back to how it was, with a little shake."""
        from mint.ui import kinetics
        chip = self.chips.get(key)
        if chip is None:
            return
        self.busy(key, False)
        kinetics.shake(chip.layer(), 4.0, 0.4)
