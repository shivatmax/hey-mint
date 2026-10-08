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


def ask(kind: str) -> None:
    """Main thread (a click): macOS's own prompt the first time, else its pane in System Settings."""
    from mint.core import permissions
    permissions.ask(kind, _asked)


# --- the chips (main thread) -------------------------------------------------------------------------------

H = 24.0                      # a chip's height
GAP = 5.0
_classes: dict = {}


def _chip_class():
    """MintNotchTipChip, defined once (Objective-C class names are global)."""
    if "chip" in _classes:
        return _classes["chip"]
    import AppKit
    import objc

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
            self.paint_(True)

        def mouseExited_(self, event):
            self.paint_(False)

        def mouseDown_(self, event):
            self.layer().setOpacity_(0.7)

        def mouseUp_(self, event):
            self.layer().setOpacity_(1.0)
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

    _classes.update(chip=MintNotchTipChip, act=MintNotchTipAct, close=MintNotchTipClose)
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
    """A row of chips, left to right: "Set up" and one chip per tip that fits `width`."""

    def __init__(self, width: float) -> None:
        import AppKit
        _chip_class()
        self.width = width
        self.keys: list = []
        self.shown: list = []
        self.acts: list = []
        self.view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, width, H))
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

    def set(self, keys, clicked, closed) -> list:
        """Show these tips (main thread); clicked(key) / closed(key) on a click. Returns the keys that fit."""
        keys = list(keys)
        if keys == self.keys:
            return self.shown
        self.keys = keys
        import AppKit
        from mint.ui import gfx
        for sub in list(self.view.subviews()):
            if sub is not self.caption:
                sub.removeFromSuperview()
        self.acts = []
        self.shown = []
        cap = self.caption.frame().size
        self.caption.setFrameOrigin_(AppKit.NSMakePoint(2, round((H - cap.height) / 2)))
        x = 2 + cap.width + 6
        chip_cls, act_cls, close_cls = _classes["chip"], _classes["act"], _classes["close"]
        for key in keys:
            _, symbol, rgb, label, why = tip(key)
            words = self._text(label, 10.5, (1.0, 1.0, 1.0), 0.92, AppKit.NSFontWeightSemibold)
            tw = words.frame().size.width
            w = 3 + 16 + 4 + tw + 2 + 10 + 3          # dot, words, ×
            if x + w > self.width:
                break
            chip = chip_cls.alloc().initWithFrame_(AppKit.NSMakeRect(x, 0, w, H))
            chip.setWantsLayer_(True)
            chip.layer().setCornerRadius_(H / 2)
            chip.layer().setBorderWidth_(1.0)
            chip.rgb = rgb
            chip.paint_(False)
            chip.fn = (lambda k=key: clicked(k))
            chip.setToolTip_(why)
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
            close.setToolTip_("Hide this tip")
            close.setFrame_(AppKit.NSMakeRect(w - 3 - 12, (H - 14) / 2, 12, 14))
            chip.addSubview_(close)
            chip.close = close
            self.view.addSubview_(chip)
            self.shown.append(key)
            x += w + GAP
        return self.shown
