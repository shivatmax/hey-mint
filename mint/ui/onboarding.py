"""Onboarding: the first thing a new user sees, and the "Welcome tour…" in the menu bar.

Seven pages in one dark window, each fading and springing in:

  welcome      Mint's face (it follows your pointer), what Mint is, Get started.
  about        your name, what to call Mint (a new name gets its own wake word), what you do and
               anything else, shown back in a live preview. Saved to Settings ▸ You.
  voice        six voices to tap and hear (voices.preview); the pick is Settings ▸ Voice.
  permissions  Microphone, Accessibility, Screen Recording, Input Monitoring, Calendars and
               Reminders: why each is needed, Allow (macOS's own prompt, or its Settings pane), and
               the status, checked every second.
  shortcuts    the four keys, as keycaps; click one and press new keys (settings_window.record_keys).
  tour         fourteen things Mint does, each with its clip from the guide, playing one after another.
  done         you're set; three first things to try (a click sends it to Mint).

Esc, Skip or the close button ends it at any page; prefs "onboarded" then keeps it from showing
again. The clips come from the runtime's media/tour (install.sh copies them) or the guide site.
"""

from __future__ import annotations

import logging
import math
import threading
import time

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.core import config
from mint.ui import gfx
from mint.core import prefs

log = logging.getLogger("mint.ui.onboarding")

W, H = 980, 660
PAGES = ("welcome", "about", "voice", "permissions", "shortcuts", "tour", "done")
TOP, BOTTOM = (0.95, 0.97, 1.0), (0.80, 0.87, 1.0)       # a pale sky, lighter at the top
INK = (0.07, 0.11, 0.22)                                    # deep navy text
DIM = (0.36, 0.42, 0.56)
WHITE = (1.0, 1.0, 1.0)
SKY = (0.22, 0.49, 1.0)                                     # the buttons and highlights
DEEP = (0.12, 0.36, 0.88)                                   # blue text on glass
OK = (0.09, 0.6, 0.35)                                      # "Allowed"
GREEN = (0.3, 0.86, 0.55)
BLUE = (0.36, 0.56, 1.0)
VIOLET = (0.64, 0.47, 1.0)
ORANGE = (1.0, 0.62, 0.22)
PINK = (1.0, 0.42, 0.62)
SITE = "https://hey-mint.pages.dev/media"

ROLES = ("Developer", "Designer", "Founder", "Student", "Writer", "Marketer", "Researcher", "Manager")
VOICES = ("Zephyr", "Aoede", "Kore", "Puck", "Charon", "Sulafat")

PERMISSIONS = (
    ("microphone", "mic.fill", PINK, "Microphone", "So I can hear “Hey Mint” and everything you ask.", True),
    ("accessibility", "hand.point.up.left.fill", BLUE, "Accessibility", "To click, type, scroll and arrange windows for you.", False),
    ("screen", "rectangle.dashed.badge.record", VIOLET, "Screen Recording", "To see what's on your screen when you ask about it.", False),
    ("input", "keyboard.fill", ORANGE, "Input Monitoring", "For the dictation key, and when you teach me a task.", False),
    ("calendar", "calendar", (1.0, 0.36, 0.33), "Calendars", "To tell you what's next and add events.", False),
    ("reminders", "checklist", GREEN, "Reminders", "To remind you of things at the right time.", False),
)
PANES = {"microphone": "Privacy_Microphone", "accessibility": "Privacy_Accessibility", "screen": "Privacy_ScreenCapture",
         "input": "Privacy_ListenEvent", "calendar": "Privacy_Calendars", "reminders": "Privacy_Reminders"}

SHORTCUTS = (
    ("talk", "waveform", "Talk to Mint", "Start talking without saying “Hey Mint”.", False),
    ("dictate", "mic.fill", "Dictate anywhere", "Hold, talk, let go: clean text lands at your cursor. Tap twice for hands-free.", True),
    ("toggle", "bubble.left.and.bubble.right.fill", "Open the chat", "Type to me instead of talking.", False),
    ("clipboard", "doc.on.clipboard.fill", "Open the clipboard", "Everything you copied, and every screenshot.", False),
)

TOUR = (
    ("doing", "cursorarrow.click.2", "Talk, and it's done",
     "Ask in plain words. I open apps, click, type and fill things in while you watch, and stop the moment you say “stop”.",
     ("reply to Priya's email", "open my last invoice")),
    ("island-schedule", "calendar", "Your day at a glance",
     "I change shape to show what matters right now: your next meetings, reminders, a call, a download.",
     ("what's on my calendar today?", "put lunch with Sam on Friday")),
    ("island-meeting", "record.circle", "Meeting notes",
     "On a call I turn into a recorder. When it ends you get the notes, the decisions and who does what.",
     ("record this meeting", "summarize the call so far")),
    ("dictation", "mic.fill", "Dictate anywhere",
     "Hold the dictate key, talk, let go. Clean, punctuated text lands wherever your cursor is, in any app.",
     ("um, looks good, no wait, ship it Thursday",)),
    ("clipboard-window", "doc.on.clipboard", "A clipboard that remembers",
     "Everything you copy and every screenshot, kept. Pick several, pin up to 20, paste them in order.",
     ("paste the last 5 screenshots in the chat", "open my clipboard")),
    ("convert", "doc.richtext", "Documents",
     "Copy the text off anything, turn a table into Excel, or an English PDF into a Hindi Word document.",
     ("make an Excel of this table", "convert this PDF to a Hindi doc")),
    ("video-edit", "film", "Edit videos by voice",
     "Trim, reframe for Reels, add captions, cut the silences. I check the result before I say it's done.",
     ("make it vertical and add captions", "cut the first 10 seconds")),
    ("translate", "character.bubble", "Translate in place",
     "Text in another language is translated right where it is on screen, in a colour and size that fit.",
     ("translate what's on my screen",)),
    ("drop", "tray.and.arrow.down", "Drop files on me",
     "Drag a PDF, a video or a folder to me and pick what to do with it.",
     ("summarize this", "translate it into Hindi")),
    ("agent", "sparkles", "Agents for long jobs",
     "Hand off research, writing or building. Agents work in the background and report back when they're done.",
     ("research the best CRM for a small team",)),
    ("island-teach", "graduationcap", "Teach me a task",
     "Show me once while I watch. Next time, just ask and I'll do it the same way.",
     ("watch how I do this", "do my expense report")),
    ("image-card", "photo.on.rectangle.angled", "Make and edit pictures",
     "Describe a picture and Apple's Image Playground draws it on your Mac. Say a change to redraw it, then save it.",
     ("make an illustration of a lighthouse at sunset", "add a sailing boat")),
    ("apple-shortcuts", "square.stack.3d.up", "Shortcuts, made for you",
     "Say what a shortcut should do. I plan the steps, you say yes, and it lands in the Shortcuts app.",
     ("make a shortcut that turns on dark mode", "run Hello and Date")),
    ("island-trackers", "bell.badge", "Let me know when…",
     "I keep an eye on downloads, uploads, long commands and Claude sessions, and tell you when they finish.",
     ("tell me when the download finishes",)),
)

TRY = (
    ("calendar", "What's on my calendar today?", "Your day, on the island"),
    ("photo.on.rectangle.angled", "Make an illustration of a lighthouse at sunset", "Then say a change to redraw it"),
    ("waveform.badge.mic", "Train my voice", "So only you can wake me"),
)


# --- small helpers --------------------------------------------------------------------------------

def _cg(rgb, alpha=1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _ns(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _font(size, weight=AppKit.NSFontWeightRegular, rounded=False):
    font = AppKit.NSFont.systemFontOfSize_weight_(size, weight)
    if rounded:
        descriptor = font.fontDescriptor().fontDescriptorWithDesign_(AppKit.NSFontDescriptorSystemDesignRounded)
        if descriptor is not None:
            font = AppKit.NSFont.fontWithDescriptor_size_(descriptor, size) or font
    return font


def _accent():
    """The highlight colour: sky blue (Mint's own mint stays on its face)."""
    return SKY


def _mint():
    return tuple(gfx.accent())


def _glass(view, radius: float, tint=None):
    """Liquid Glass (NSGlassEffectView, macOS 26+) behind a view's content; frosted white before that."""
    cls = getattr(AppKit, "NSGlassEffectView", None)
    if cls is None:
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(_cg(WHITE, 0.6))
        view.layer().setCornerRadius_(radius)
        return None
    glass = cls.alloc().initWithFrame_(view.bounds())
    glass.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    glass.setCornerRadius_(radius)
    if tint is not None:
        glass.setTintColor_(tint)
    view.addSubview_positioned_relativeTo_(glass, AppKit.NSWindowBelow, None)
    view.glass = glass
    return glass


def _tint(view, rgb=None, alpha=0.0) -> None:
    """Colour a glass surface (or, without glass, its layer)."""
    glass = getattr(view, "glass", None)
    color = _ns(rgb, alpha) if rgb is not None and alpha else None
    if glass is not None:
        glass.setTintColor_(color)
    else:
        view.layer().setBackgroundColor_(_cg(rgb, alpha) if color is not None else _cg(WHITE, 0.6))


def _media(name: str, ext: str):
    """A tour clip or poster: on this Mac if install.sh copied it, otherwise from the guide site."""
    for base in (config.PROJECT_ROOT / "media" / "tour", config.PROJECT_ROOT / "guide" / "media"):
        path = base / f"{name}.{ext}"
        if path.exists():
            return AppKit.NSURL.fileURLWithPath_(str(path))
    return AppKit.NSURL.URLWithString_(f"{SITE}/{name}.{ext}")


def _caps(value: str) -> list[str]:
    """'ctrl+option+space' -> ['⌃', '⌥', 'Space']; 'right_option' -> ['Right ⌥']."""
    from mint.core import hotkeys
    if not value:
        return []
    if value in hotkeys.MODIFIER_NAMES:
        return [hotkeys.MODIFIER_NAMES[value]]
    symbols = {"ctrl": "⌃", "control": "⌃", "option": "⌥", "alt": "⌥", "shift": "⇧", "cmd": "⌘", "command": "⌘"}
    keys = {"space": "Space", "return": "↩", "enter": "↩", "escape": "Esc", "tab": "⇥", "delete": "⌫",
            "left": "←", "right": "→", "up": "↑", "down": "↓"}
    parts = [p.strip().lower() for p in value.split("+") if p.strip()]
    *mods, key = parts
    order = {"⌃": 0, "⌥": 1, "⇧": 2, "⌘": 3}
    caps = sorted({symbols.get(m, m) for m in mods}, key=lambda s: order.get(s, 9))
    return caps + [keys.get(key, key.upper() if len(key) == 1 else key.capitalize())]


def _enter(view, delay: float, rise: float = 18.0, scale: float = 1.0) -> None:
    """Fade and spring a view in, `delay` seconds from now (its layer keeps its place)."""
    view.setWantsLayer_(True)
    layer = view.layer()
    start = Quartz.CACurrentMediaTime() + delay
    fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
    fade.setFromValue_(0.0)
    fade.setToValue_(1.0)
    fade.setDuration_(0.45)
    fade.setBeginTime_(start)
    fade.setFillMode_(Quartz.kCAFillModeBackwards)
    fade.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseOut))
    layer.addAnimation_forKey_(fade, "enter-fade")
    if rise:
        spring = Quartz.CASpringAnimation.animationWithKeyPath_("transform.translation.y")
        spring.setFromValue_(rise)
        spring.setToValue_(0.0)
        spring.setDamping_(18)
        spring.setStiffness_(170)
        spring.setMass_(1)
        spring.setDuration_(spring.settlingDuration())
        spring.setBeginTime_(start)
        spring.setFillMode_(Quartz.kCAFillModeBackwards)
        layer.addAnimation_forKey_(spring, "enter-rise")
    if scale != 1.0:
        grow = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
        grow.setFromValue_(scale)
        grow.setToValue_(1.0)
        grow.setDamping_(14)
        grow.setStiffness_(160)
        grow.setDuration_(grow.settlingDuration())
        grow.setBeginTime_(start)
        grow.setFillMode_(Quartz.kCAFillModeBackwards)
        layer.addAnimation_forKey_(grow, "enter-scale")


def _pop(view) -> None:
    view.setWantsLayer_(True)
    pop = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
    pop.setValues_([1.0, 1.06, 0.98, 1.0])
    pop.setKeyTimes_([0.0, 0.3, 0.65, 1.0])
    pop.setDuration_(0.35)
    view.layer().addAnimation_forKey_(pop, "pop")


# --- views ------------------------------------------------------------------------------------------

class _OnbWindow(AppKit.NSWindow):
    def canBecomeKeyWindow(self):
        return True

    def keyDown_(self, event):
        owner = getattr(self, "owner", None)
        if owner is None or not owner.key(event):
            objc.super(_OnbWindow, self).keyDown_(event)

    def cancelOperation_(self, sender):
        self.owner.skip()


class _OnbFlipped(AppKit.NSView):
    def isFlipped(self):
        return True


class _OnbOverlay(AppKit.NSView):
    """A layer above a glass card for lines and badges; clicks go through to the card."""

    def isFlipped(self):
        return True

    def hitTest_(self, point):
        return None


class _OnbClick(AppKit.NSView):
    """A clickable surface (buttons, cards, chips): a pointing hand, a hover tint, a click callback."""

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.pointingHandCursor())

    def updateTrackingAreas(self):
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        options = (AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways
                   | AppKit.NSTrackingInVisibleRect)
        self.addTrackingArea_(AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None))
        objc.super(_OnbClick, self).updateTrackingAreas()

    def mouseEntered_(self, event):
        hover = getattr(self, "on_hover", None)
        if hover:
            hover(True)

    def mouseExited_(self, event):
        hover = getattr(self, "on_hover", None)
        if hover:
            hover(False)

    def mouseDown_(self, event):
        self.setWantsLayer_(True)
        self.layer().setOpacity_(0.82)

    def mouseUp_(self, event):
        self.layer().setOpacity_(1.0)
        inside = AppKit.NSPointInRect(self.convertPoint_fromView_(event.locationInWindow(), None), self.bounds())
        click = getattr(self, "on_click", None)
        if inside and click:
            click()


class _OnbTarget(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(_OnbTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def tick_(self, timer):
        try:
            self.owner.tick()
        except Exception:
            log.debug("onboarding tick", exc_info=True)

    def controlTextDidChange_(self, note):
        self.owner.typed(note.object())

    def controlTextDidBeginEditing_(self, note):
        self.owner.focus(note.object(), True)

    def controlTextDidEndEditing_(self, note):
        self.owner.focus(note.object(), False)

    def windowWillClose_(self, note):
        self.owner.closed()


# --- the window ---------------------------------------------------------------------------------------

class Onboarding:
    def __init__(self) -> None:
        self.window = None
        self.page = 0
        self.view = None                 # the current page's view
        self.orbs: list = []             # (Orb, host view)
        self.fields: dict = {}
        self.boxes: dict = {}
        self.roles: set[str] = set()
        self.voice = ""
        self.perm_rows: dict = {}
        self.asked: set[str] = set()
        self.player = None
        self.player_end = None
        self.tour_index = 0
        self.tour_auto = True
        self.recording = None
        self._ticks = 0
        self._store = None

    # --- building -------------------------------------------------------------------------------

    def _build(self) -> None:
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskFullSizeContentView)
        window = _OnbWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H), style, AppKit.NSBackingStoreBuffered, False)
        window.owner = self
        window.setTitle_(f"Welcome to {prefs.name()}")
        window.setTitlebarAppearsTransparent_(True)
        window.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        window.setMovableByWindowBackground_(True)
        window.setReleasedWhenClosed_(False)
        window.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
        window.setBackgroundColor_(_ns(BOTTOM))
        for button in (AppKit.NSWindowMiniaturizeButton, AppKit.NSWindowZoomButton):
            control = window.standardWindowButton_(button)
            if control is not None:
                control.setHidden_(True)
        try:
            from mint.ui.effects import SHARING
            window.setSharingType_(SHARING)
        except Exception:
            pass
        self.target = _OnbTarget.alloc().initWithOwner_(self)
        window.setDelegate_(self.target)
        root = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        root.setWantsLayer_(True)
        window.setContentView_(root)
        self.window, self.root = window, root
        self._background(root.layer())
        self.stage = _OnbFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        root.addSubview_(self.stage)
        self.chrome = _OnbFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        self._chrome()
        self.timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 30, self.target, "tick:", None, True)

    def _background(self, layer) -> None:
        base = Quartz.CAGradientLayer.layer()
        base.setFrame_(Quartz.CGRectMake(0, 0, W, H))
        base.setColors_([_cg(BOTTOM), _cg(TOP)])
        layer.addSublayer_(base)
        # Three soft lights drifting slowly; each page moves them somewhere new.
        self.lights = []
        for rgb, size, period in (((0.3, 0.6, 1.0), 780, 13.0), ((0.45, 0.86, 1.0), 700, 17.0),
                                  ((0.72, 0.64, 1.0), 640, 21.0)):
            light = Quartz.CAGradientLayer.layer()
            light.setType_(Quartz.kCAGradientLayerRadial)
            light.setBounds_(Quartz.CGRectMake(0, 0, size, size))
            light.setColors_([_cg(rgb, 0.5), _cg(rgb, 0.18), _cg(rgb, 0.0)])
            light.setLocations_([0.0, 0.45, 1.0])
            light.setStartPoint_(Quartz.CGPointMake(0.5, 0.5))
            light.setEndPoint_(Quartz.CGPointMake(1.0, 1.0))
            wander = Quartz.CABasicAnimation.animationWithKeyPath_("transform.translation")
            wander.setFromValue_(AppKit.NSValue.valueWithSize_((-40, -30)))
            wander.setToValue_(AppKit.NSValue.valueWithSize_((50, 40)))
            wander.setDuration_(period)
            wander.setAutoreverses_(True)
            wander.setRepeatCount_(1e9)
            wander.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                Quartz.kCAMediaTimingFunctionEaseInEaseOut))
            light.addAnimation_forKey_(wander, "wander")
            layer.addSublayer_(light)
            self.lights.append(light)
        # A hairline of light along the top edge, like glass.
        edge = Quartz.CAGradientLayer.layer()
        edge.setFrame_(Quartz.CGRectMake(0, H - 1, W, 1))
        edge.setStartPoint_(Quartz.CGPointMake(0, 0.5))
        edge.setEndPoint_(Quartz.CGPointMake(1, 0.5))
        edge.setColors_([_cg(WHITE, 0.0), _cg(WHITE, 0.9), _cg(WHITE, 0.0)])
        layer.addSublayer_(edge)
        self._drift(0, animated=False)

    def _drift(self, page: int, animated: bool = True) -> None:
        spots = (((490, 380), (200, 150), (820, 560)), ((850, 420), (120, 120), (620, 620)),
                 ((140, 480), (860, 160), (520, 40)), ((820, 560), (160, 260), (480, 120)),
                 ((180, 560), (820, 200), (600, 640)), ((880, 520), (320, 80), (80, 600)),
                 ((490, 400), (160, 560), (840, 160)))
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(1.6 if animated else 0.0)
        Quartz.CATransaction.setDisableActions_(not animated)
        for light, (x, y) in zip(self.lights, spots[page % len(spots)]):
            light.setPosition_(Quartz.CGPointMake(x, y))
        Quartz.CATransaction.commit()

    def _chrome(self) -> None:
        """The progress bar, Skip, Back and Continue: over every page."""
        chrome = self.chrome
        self.root.addSubview_(chrome)
        chrome.setWantsLayer_(True)
        self.segments = []
        for _ in PAGES[1:]:
            segment = Quartz.CALayer.layer()
            segment.setCornerRadius_(2)
            chrome.layer().addSublayer_(segment)
            self.segments.append(segment)
        self.skip_button = self._text_button(chrome, "Skip setup", W - 128, 16, 104, self.skip, align="right")
        self.back_button = self._text_button(chrome, "‹  Back", 36, H - 70, 90, self.back, align="left")
        self.next_button = self._pill(chrome, "Continue", W - 36 - 168, H - 78, 168, 46, self.next)

    def _paint_chrome(self, animated: bool = True) -> None:
        page = self.page
        mint = _accent()
        widths = [34 if i + 1 == page else 16 for i in range(len(self.segments))]
        total = sum(widths) + 6 * (len(widths) - 1)
        x = (W - total) / 2
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.45 if animated else 0.0)
        Quartz.CATransaction.setDisableActions_(not animated)
        for i, (segment, width) in enumerate(zip(self.segments, widths)):
            segment.setFrame_(Quartz.CGRectMake(x, H - 30, width, 4))
            done = i + 1 < page
            segment.setBackgroundColor_(_cg(mint) if i + 1 == page else _cg(INK, 0.32 if done else 0.1))
            segment.setOpacity_(0.0 if page == 0 else 1.0)
            x += width + 6
        Quartz.CATransaction.commit()
        name = PAGES[page]
        self.skip_button.setHidden_(name == "done")
        self.back_button.setHidden_(page <= 1)
        self.next_button.setHidden_(name in ("welcome", "done"))
        self.next_button.label.setStringValue_({"permissions": "Continue", "tour": "Finish tour"}.get(name, "Continue"))
        self._center(self.next_button)

    # --- reusable pieces ----------------------------------------------------------------------------

    def _label(self, view, text, x, y, w, size=14, weight=AppKit.NSFontWeightRegular, rgb=INK, alpha=1.0,
               rounded=False, lines=1, align=AppKit.NSTextAlignmentLeft, h=None):
        field = AppKit.NSTextField.wrappingLabelWithString_(text) if lines > 1 else AppKit.NSTextField.labelWithString_(text)
        font = _font(size, weight, rounded)
        field.setFont_(font)
        field.setTextColor_(_ns(rgb, alpha))
        field.setAlignment_(align)
        field.setMaximumNumberOfLines_(lines)
        if lines == 1:
            field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        line = math.ceil(font.ascender() - font.descender() + font.leading()) + 2
        field.setFrame_(AppKit.NSMakeRect(x, y, w, h or line * lines + (4 if lines > 1 else 0)))
        view.addSubview_(field)
        return field

    def _center(self, button) -> None:
        """Centre a pill's label (and icon) in it."""
        label = button.label
        label.sizeToFit()
        size = label.frame().size
        icon = getattr(button, "icon", None)
        icon_w = 22 if icon is not None else 0
        frame = button.frame()
        x = (frame.size.width - size.width - icon_w) / 2
        y = (frame.size.height - size.height) / 2
        if icon is not None and getattr(button, "trailing", False):
            label.setFrameOrigin_(AppKit.NSMakePoint(x, y))
            icon.setFrame_(AppKit.NSMakeRect(x + size.width + 6, (frame.size.height - 16) / 2, 16, 16))
            return
        if icon is not None:
            icon.setFrame_(AppKit.NSMakeRect(x, (frame.size.height - 16) / 2, 16, 16))
        label.setFrameOrigin_(AppKit.NSMakePoint(x + icon_w, y))

    def _pill(self, view, text, x, y, w, h, action, primary=True, symbol=None, size=15, trailing=False):
        """A rounded button: solid sky blue with a glassy sheen (primary), or Liquid Glass (secondary)."""
        button = _OnbClick.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
        button.setWantsLayer_(True)
        layer = button.layer()
        layer.setCornerRadius_(h / 2)
        if primary:
            rest, over = _cg(SKY), _cg((0.36, 0.6, 1.0))
            layer.setBackgroundColor_(rest)
            layer.setShadowColor_(_cg(SKY))
            layer.setShadowOpacity_(0.4)
            layer.setShadowRadius_(14)
            layer.setShadowOffset_(Quartz.CGSizeMake(0, -3))
            sheen = Quartz.CAGradientLayer.layer()
            sheen.setFrame_(Quartz.CGRectMake(0, 0, w, h))
            sheen.setCornerRadius_(h / 2)
            sheen.setColors_([_cg(WHITE, 0.32), _cg(WHITE, 0.0), _cg(WHITE, 0.0)])
            sheen.setLocations_([0.0, 0.55, 1.0])
            sheen.setStartPoint_(Quartz.CGPointMake(0.5, 0.0))
            sheen.setEndPoint_(Quartz.CGPointMake(0.5, 1.0))
            layer.addSublayer_(sheen)
            layer.setBorderColor_(_cg(WHITE, 0.35))
            layer.setBorderWidth_(0.5)
            button.on_hover = lambda on: layer.setBackgroundColor_(over if on else rest)
        else:
            _glass(button, h / 2)
            button.on_hover = lambda on: _tint(button, WHITE, 0.55 if on else 0.0)
        ink = WHITE if primary else INK
        button.label = self._label(button, text, 0, 0, w, size=size, weight=AppKit.NSFontWeightSemibold, rgb=ink,
                                   rounded=True)
        button.icon = None
        if symbol:
            icon = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 16, 16))
            icon.setImage_(gfx.symbol(symbol, 13))
            icon.setContentTintColor_(_ns(ink))
            button.addSubview_(icon)
            button.icon = icon
        button.trailing = trailing
        button.on_click = action
        view.addSubview_(button)
        self._center(button)
        return button

    def _text_button(self, view, text, x, y, w, action, align="left"):
        button = _OnbClick.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 28))
        label = self._label(button, text, 0, 5, w, size=13, weight=AppKit.NSFontWeightMedium, rgb=DIM,
                            align=AppKit.NSTextAlignmentRight if align == "right" else AppKit.NSTextAlignmentLeft)
        button.on_click = action
        button.on_hover = lambda on: label.setTextColor_(_ns(INK if on else DIM))
        view.addSubview_(button)
        return button

    def _card(self, view, x, y, w, h, radius=18, action=None, fill=0.0):
        """A Liquid Glass card; clickable ones brighten under the pointer."""
        card = (_OnbClick if action else _OnbFlipped).alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
        card.setWantsLayer_(True)
        layer = card.layer()
        layer.setCornerRadius_(radius)
        layer.setShadowColor_(_cg((0.1, 0.2, 0.5)))
        layer.setShadowOpacity_(0.1)
        layer.setShadowRadius_(18)
        layer.setShadowOffset_(Quartz.CGSizeMake(0, -6))
        _glass(card, radius)
        card.top = _OnbOverlay.alloc().initWithFrame_(card.bounds())
        card.top.setWantsLayer_(True)
        card.addSubview_(card.top)
        if fill:
            _tint(card, WHITE, fill)
        if action:
            card.on_click = action
            card.on_hover = lambda on: getattr(card, "chosen", False) or _tint(card, WHITE, 0.5 if on else fill)
        view.addSubview_(card)
        return card

    def _tile(self, view, symbol, rgb, x, y, size=36):
        tile = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, size, size))
        tile.setWantsLayer_(True)
        grad = Quartz.CAGradientLayer.layer()
        grad.setFrame_(Quartz.CGRectMake(0, 0, size, size))
        grad.setCornerRadius_(size * 0.28)
        grad.setColors_([_cg(rgb, 0.95), _cg(tuple(c * 0.72 for c in rgb), 0.95)])
        tile.layer().addSublayer_(grad)
        image = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, size, size))
        image.setImage_(gfx.symbol(symbol, size * 0.42))
        image.setContentTintColor_(AppKit.NSColor.whiteColor())
        image.setImageScaling_(AppKit.NSImageScaleNone)
        tile.addSubview_(image)
        view.addSubview_(tile)
        return tile

    def _orb(self, view, cx, cy, diameter, halo=False):
        """Mint's face (orb.Orb) at (cx, cy) in the flipped page; it looks at the pointer."""
        from mint.ui.orb import Orb
        size = diameter * 2.2
        host = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(cx - size / 2, cy - size / 2, size, size))
        host.setWantsLayer_(True)
        if halo:
            glow = Quartz.CAGradientLayer.layer()
            glow.setType_(Quartz.kCAGradientLayerRadial)
            glow.setFrame_(Quartz.CGRectMake(0, 0, size, size))
            glow.setColors_([_cg(_mint(), 0.45), _cg(WHITE, 0.25), _cg(WHITE, 0.0)])
            glow.setLocations_([0.0, 0.45, 1.0])
            glow.setStartPoint_(Quartz.CGPointMake(0.5, 0.5))
            glow.setEndPoint_(Quartz.CGPointMake(1.0, 1.0))
            breathe = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
            breathe.setFromValue_(0.86)
            breathe.setToValue_(1.08)
            breathe.setDuration_(2.6)
            breathe.setAutoreverses_(True)
            breathe.setRepeatCount_(1e9)
            breathe.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                Quartz.kCAMediaTimingFunctionEaseInEaseOut))
            glow.addAnimation_forKey_(breathe, "breathe")
            host.layer().addSublayer_(glow)
        face = Quartz.CALayer.layer()
        face.setFrame_(Quartz.CGRectMake(0, 0, size, size))
        host.layer().addSublayer_(face)
        orb = Orb(face, (size / 2, size / 2), diameter)
        orb.apply_state("awake", True)
        for ring in (orb.ring, orb.spinner):
            ring.removeAllAnimations()
            ring.setOpacity_(0.0)
        view.addSubview_(host)
        self.orbs.append((orb, host))
        return orb, host

    def _field(self, view, key, x, y, w, placeholder, value):
        box = _OnbFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 46))
        box.setWantsLayer_(True)
        box.layer().setCornerRadius_(14)
        box.layer().setBorderColor_(_cg(WHITE, 0.0))
        box.layer().setBorderWidth_(1.5)
        _glass(box, 14)
        field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(14, 12, w - 28, 24))
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setFocusRingType_(AppKit.NSFocusRingTypeNone)
        field.setFont_(_font(16, AppKit.NSFontWeightMedium))
        field.setTextColor_(_ns(INK))
        field.setStringValue_(value or "")
        field.setPlaceholderAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            placeholder, {AppKit.NSForegroundColorAttributeName: _ns(DIM, 0.75),
                          AppKit.NSFontAttributeName: _font(16, AppKit.NSFontWeightMedium)}))
        field.cell().setUsesSingleLineMode_(True)
        field.cell().setScrollable_(True)
        field.setDelegate_(self.target)
        box.addSubview_(field)
        view.addSubview_(box)
        self.fields[key] = field
        self.boxes[objc.pyobjc_id(field)] = box
        return box

    # --- pages ----------------------------------------------------------------------------------------

    def _page_welcome(self, page) -> list:
        name = prefs.name()
        orb, host = self._orb(page, W / 2, 214, 128, halo=True)
        AppHelper.callLater(0.7, orb.hop)
        AppHelper.callLater(1.2, lambda: orb.burst(_accent(), stars=True, amount=0.8))
        title = self._label(page, f"Hi, I'm {name}.", 0, 318, W, size=50, weight=AppKit.NSFontWeightBold,
                            rounded=True, align=AppKit.NSTextAlignmentCenter)
        sub = self._label(page, "Your Mac, by voice. I listen, see what's on your screen and get things done: "
                                "email, calendar, documents, videos and meetings.", (W - 600) / 2, 390, 600,
                          size=17, rgb=DIM, lines=2, align=AppKit.NSTextAlignmentCenter)
        start = self._pill(page, "Get started", (W - 210) / 2, 476, 210, 50, self.next, symbol="arrow.right", size=16,
                           trailing=True)
        hint = self._label(page, "About two minutes  ·  Esc to skip", 0, 542, W, size=12, rgb=DIM, alpha=0.8,
                           align=AppKit.NSTextAlignmentCenter)
        return [(host, 0.0, 0.6), title, sub, start, hint]

    def _heading(self, page, eyebrow, title, sub, x=80, w=560):
        mint = _accent()
        brow = self._label(page, eyebrow.upper(), x, 76, w, size=12, weight=AppKit.NSFontWeightBold, rgb=mint)
        brow.setAttributedStringValue_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            eyebrow.upper(), {AppKit.NSKernAttributeName: 1.6, AppKit.NSForegroundColorAttributeName: _ns(DEEP),
                              AppKit.NSFontAttributeName: _font(12, AppKit.NSFontWeightBold)}))
        head = self._label(page, title, x, 98, w + 200, size=34, weight=AppKit.NSFontWeightBold, rounded=True)
        text = self._label(page, sub, x, 146, w + 160, size=15, rgb=DIM, lines=2)
        return [brow, head, text]

    def _page_about(self, page) -> list:
        items = self._heading(page, "About you", "Let's get acquainted.",
                              "So I know what to call you, and what you'd like to call me.")
        about = str(prefs.get("about_me") or "")
        extra = about
        if about.startswith("Role: "):
            first, _, extra = about.partition("\n")
            self.roles = {r.strip() for r in first[6:].rstrip(".").split(",") if r.strip() in ROLES}
        y = 200
        items.append(self._label(page, "Your name", 80, y, 300, size=12, weight=AppKit.NSFontWeightSemibold, rgb=DIM))
        items.append(self._field(page, "user_name", 80, y + 20, 420, "What should I call you?",
                                 str(prefs.get("user_name") or "")))
        y += 82
        items.append(self._label(page, "Call me", 80, y, 300, size=12, weight=AppKit.NSFontWeightSemibold, rgb=DIM))
        name = str(prefs.get("assistant_name") or "")
        items.append(self._field(page, "assistant_name", 80, y + 20, 420, "Mint", "" if name == "Mint" else name))
        self.wake_note = self._label(page, "", 80, y + 70, 420, size=12, rgb=DIM, alpha=0.85)
        items.append(self.wake_note)
        y += 104
        items.append(self._label(page, "What do you do?", 80, y, 300, size=12, weight=AppKit.NSFontWeightSemibold,
                                 rgb=DIM))
        x, row_y = 80, y + 22
        self.role_chips = {}
        for role in ROLES:
            width = int(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                role, {AppKit.NSFontAttributeName: _font(13, AppKit.NSFontWeightMedium)}).size().width) + 30
            if x + width > 500:
                x, row_y = 80, row_y + 40
            chip = self._pill(page, role, x, row_y, width, 32, lambda r=role: self._role(r), primary=False, size=13)
            self.role_chips[role] = chip
            self._paint_role(role)
            items.append(chip)
            x += width + 8
        y = row_y + 52
        items.append(self._label(page, "Anything else I should know?", 80, y, 300, size=12,
                                 weight=AppKit.NSFontWeightSemibold, rgb=DIM))
        items.append(self._field(page, "about_me", 80, y + 20, 420, "e.g. I run a design studio. Keep answers short.",
                                 extra.strip()))
        # The preview: Mint meets you.
        card = self._card(page, 560, 196, 340, 330, radius=24)
        self._orb(card, 170, 92, 64, halo=True)
        bubble = _OnbFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(28, 160, 284, 112))
        bubble.setWantsLayer_(True)
        bubble.layer().setCornerRadius_(18)
        bubble.layer().setBackgroundColor_(_cg(WHITE, 0.6))
        bubble.layer().setBorderColor_(_cg(WHITE, 0.9))
        bubble.layer().setBorderWidth_(1)
        card.addSubview_(bubble)
        self.greeting = self._label(bubble, "", 16, 16, 252, size=15, weight=AppKit.NSFontWeightMedium, lines=4,
                                    align=AppKit.NSTextAlignmentCenter, h=80)
        self.bubble = bubble
        self._label(card, "Stays on this Mac", 0, 292, 340, size=12, rgb=DIM, alpha=0.8,
                    align=AppKit.NSTextAlignmentCenter)
        items.append((card, 0.18, 0.94))
        self._greet(pop=False)
        return items

    def _role(self, role: str) -> None:
        self.roles ^= {role}
        self._paint_role(role)
        _pop(self.role_chips[role])
        self._greet()

    def _paint_role(self, role: str) -> None:
        chip = self.role_chips[role]
        on = role in self.roles
        mint = _accent()
        chip.chosen = on
        _tint(chip, mint, 0.22) if on else _tint(chip)
        chip.layer().setBorderColor_(_cg(mint, 0.85) if on else _cg(WHITE, 0.0))
        chip.layer().setBorderWidth_(1.2 if on else 0)
        chip.label.setTextColor_(_ns(DEEP if on else INK))
        chip.on_hover = None if on else (lambda over, c=chip: getattr(c, "chosen", False) or _tint(
            c, WHITE, 0.55 if over else 0.0))

    def _greet(self, pop: bool = True) -> None:
        you = str(self.fields["user_name"].stringValue()).strip() if "user_name" in self.fields else ""
        me = str(self.fields["assistant_name"].stringValue()).strip() if "assistant_name" in self.fields else ""
        me = me or "Mint"
        hello = f"Nice to meet you, {you.split()[0]}!" if you else "Nice to meet you!"
        roles = sorted(self.roles, key=ROLES.index)
        what = ""
        if roles:
            joined = roles[0].lower() if len(roles) == 1 else ", ".join(r.lower() for r in roles[:-1]) + \
                f" and {roles[-1].lower()}"
            what = f" I'll keep in mind you're a {joined}." if len(roles) == 1 else f" A {joined}, nice."
        self.greeting.setStringValue_(f"{hello} I'm {me}.{what} Say “Hey {me}” whenever you need me.")
        self.wake_note.setStringValue_(f"A new name gets its own wake word, “Hey {me}”." if me != prefs.name()
                                       else f"Wake me with “Hey {me}”.")
        if pop:
            _pop(self.bubble)

    def _page_voice(self, page) -> list:
        items = self._heading(page, "Voice", "Pick my voice.",
                              "Tap a voice to hear it. You can change it any time in Settings ▸ Voice.")
        from mint.voice.voices import GENDER
        from mint.tools.extra import VOICES as TONES
        self.voice = str(prefs.get("voice_name") or config.VOICE)
        self.voice_cards = {}
        for i, name in enumerate(VOICES):
            col, row = i % 3, i // 3
            card = self._card(page, 80 + col * 282, 208 + row * 122, 266, 106, action=lambda n=name: self._pick_voice(n))
            avatar = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(20, 27, 52, 52))
            avatar.setWantsLayer_(True)
            grad = Quartz.CAGradientLayer.layer()
            grad.setFrame_(Quartz.CGRectMake(0, 0, 52, 52))
            grad.setCornerRadius_(26)
            hue = (_mint(), BLUE, VIOLET, ORANGE, PINK, GREEN)[i]
            grad.setColors_([_cg(hue, 0.95), _cg(tuple(c * 0.6 for c in hue), 0.95)])
            grad.setStartPoint_(Quartz.CGPointMake(0, 1))
            grad.setEndPoint_(Quartz.CGPointMake(1, 0))
            avatar.layer().addSublayer_(grad)
            bars = []
            for b in range(4):
                bar = Quartz.CALayer.layer()
                bar.setFrame_(Quartz.CGRectMake(15 + b * 6.5, 16, 3.5, 20))
                bar.setCornerRadius_(1.75)
                bar.setBackgroundColor_(_cg(WHITE, 0.95))
                bar.setAffineTransform_(Quartz.CGAffineTransformMakeScale(1, (0.35, 0.8, 0.55, 0.3)[b]))
                avatar.layer().addSublayer_(bar)
                bars.append(bar)
            card.addSubview_(avatar)
            self._label(card, name, 88, 28, 150, size=18, weight=AppKit.NSFontWeightSemibold, rounded=True)
            self._label(card, f"{GENDER.get(name, '').capitalize()} · {TONES.get(name, '')}", 88, 54, 150, size=13,
                        rgb=DIM)
            state = self._label(card, "", 88, 74, 160, size=11, weight=AppKit.NSFontWeightMedium, rgb=_accent())
            check = gfx.number_badge("", 22, _cg(SKY), _cg(WHITE), 11)
            check.setPosition_(Quartz.CGPointMake(266 - 22, 22))
            mark = Quartz.CALayer.layer()
            mark.setFrame_(Quartz.CGRectMake(5, 5, 12, 12))
            mark.setContents_(gfx.cg_image(gfx.symbol("checkmark", 9, weight="bold", white=True)))
            mark.setContentsGravity_(Quartz.kCAGravityResizeAspect)
            check.addSublayer_(mark)
            card.top.layer().addSublayer_(check)
            card.bars, card.state, card.check = bars, state, check
            self.voice_cards[name] = card
            self._paint_voice(name)
            items.append((card, 0.0, 0.94))
        items.append(self._label(page, "30 voices in all, and a speaking style of your own, in Settings ▸ Voice.",
                                 80, 468, 600, size=12, rgb=DIM, alpha=0.85))
        return items

    def _paint_voice(self, name: str) -> None:
        card = self.voice_cards[name]
        mint = _accent()
        on = name == self.voice
        card.chosen = on
        _tint(card, mint, 0.16) if on else _tint(card)
        card.layer().setBorderColor_(_cg(mint, 0.9) if on else _cg(WHITE, 0.0))
        card.layer().setBorderWidth_(1.5 if on else 0)
        card.check.setOpacity_(1.0 if on else 0.0)

    def _pick_voice(self, name: str) -> None:
        old, self.voice = self.voice, name
        for n in (old, name):
            if n in self.voice_cards:
                self._paint_voice(n)
        card = self.voice_cards[name]
        _pop(card)
        card.state.setStringValue_("Getting ready…")
        from mint.voice import voices

        def started(error):
            if error:
                card.state.setStringValue_("Couldn't play it just now")
                return
            card.state.setStringValue_("Playing")
            self._dance(card, 4.5)
            AppHelper.callLater(4.5, lambda: card.state.setStringValue_(""))
        voices.preview(name, "", started)

    @staticmethod
    def _dance(card, seconds: float) -> None:
        for i, bar in enumerate(card.bars):
            dance = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale.y")
            dance.setValues_([0.3, 1.0, 0.45, 0.85, 0.3])
            dance.setDuration_(0.7 + i * 0.13)
            dance.setRepeatCount_(seconds / (0.7 + i * 0.13))
            bar.addAnimation_forKey_(dance, "dance")

    # permissions

    def _page_permissions(self, page) -> list:
        items = self._heading(page, "Permissions", "Let me help properly.",
                              "macOS asks you to allow each of these. I can't work without the microphone; "
                              "the rest unlock more of what I can do.")
        self.summary = self._pill(page, "", W - 80 - 150, 104, 150, 32, lambda: None, primary=False, size=13)
        self.summary.on_click = None
        items.append(self.summary)
        box = self._card(page, 80, 204, W - 160, 6 * 58 + 8, radius=18, fill=0.2)
        items.append((box, 0.1, 0.97))
        self.perm_rows = {}
        for i, (kind, symbol, rgb, title, why, required) in enumerate(PERMISSIONS):
            y = 4 + i * 58
            if i:
                line = Quartz.CALayer.layer()
                line.setFrame_(Quartz.CGRectMake(70, y, W - 160 - 90, 0.5))
                line.setBackgroundColor_(_cg(INK, 0.08))
                box.top.layer().addSublayer_(line)
            self._tile(box, symbol, rgb, 20, y + 11)
            label = self._label(box, title, 70, y + 10, 300, size=15, weight=AppKit.NSFontWeightSemibold)
            if required:
                label.sizeToFit()
                self._label(box, "REQUIRED", 70 + label.frame().size.width + 8, y + 13, 70, size=9,
                            weight=AppKit.NSFontWeightBold, rgb=(0.86, 0.18, 0.4))
            self._label(box, why, 70, y + 31, 520, size=13, rgb=DIM)
            button = self._pill(box, "Allow", W - 160 - 20 - 132, y + 13, 132, 32, lambda k=kind: self._allow(k),
                                primary=False, size=13)
            button.label.setTextColor_(_ns(DEEP))
            _tint(button, WHITE, 0.6)
            button.on_hover = lambda on, b=button: _tint(b, WHITE, 0.9 if on else 0.6)
            done = self._label(box, "Allowed", W - 160 - 20 - 132, y + 19, 132, size=13,
                               weight=AppKit.NSFontWeightSemibold, rgb=OK, align=AppKit.NSTextAlignmentRight)
            self.perm_rows[kind] = (button, done, None)
        self._check_permissions(first=True)
        return items

    def _status(self, kind: str) -> str:
        """'allowed', 'denied' (only System Settings can change it) or 'ask'."""
        try:
            if kind == "microphone":
                import AVFoundation
                status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
                return {3: "allowed", 0: "ask"}.get(int(status), "denied")
            if kind == "accessibility":
                import ApplicationServices
                if ApplicationServices.AXIsProcessTrusted():
                    return "allowed"
            elif kind == "screen":
                if Quartz.CGPreflightScreenCaptureAccess():
                    return "allowed"
            elif kind == "input":
                if Quartz.CGPreflightListenEventAccess():
                    return "allowed"
            elif kind in ("calendar", "reminders"):
                import EventKit
                entity = EventKit.EKEntityTypeEvent if kind == "calendar" else EventKit.EKEntityTypeReminder
                status = int(EventKit.EKEventStore.authorizationStatusForEntityType_(entity))
                return "allowed" if status in (3, 4) else ("ask" if status == 0 else "denied")
        except Exception:
            log.debug("permission status %s", kind, exc_info=True)
        return "denied" if kind in self.asked else "ask"

    def _allow(self, kind: str) -> None:
        status = self._status(kind)
        if status == "allowed":
            return
        first = kind not in self.asked
        self.asked.add(kind)
        try:
            if status == "ask" and first:
                if kind == "microphone":
                    import AVFoundation
                    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                        AVFoundation.AVMediaTypeAudio, lambda ok: None)
                    return
                if kind == "accessibility":
                    from mint.tools import fastinput
                    fastinput.request_accessibility()
                    return
                if kind == "screen":
                    Quartz.CGRequestScreenCaptureAccess()
                    return
                if kind == "input":
                    Quartz.CGRequestListenEventAccess()
                    return
                if kind in ("calendar", "reminders"):
                    import EventKit
                    if self._store is None:
                        self._store = EventKit.EKEventStore.alloc().init()
                    if kind == "calendar":
                        self._store.requestFullAccessToEventsWithCompletion_(lambda ok, error: None)
                    else:
                        self._store.requestFullAccessToRemindersWithCompletion_(lambda ok, error: None)
                    return
        except Exception:
            log.debug("permission request %s", kind, exc_info=True)
        AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(
            f"x-apple.systempreferences:com.apple.preference.security?{PANES[kind]}"))

    def _check_permissions(self, first: bool = False) -> None:
        allowed = 0
        for kind, (button, done, last) in list(self.perm_rows.items()):
            status = self._status(kind)
            allowed += status == "allowed"
            if status == last:
                continue
            button.setHidden_(status == "allowed")
            done.setHidden_(status != "allowed")
            text = "Open Settings" if status == "denied" else "Allow"
            if button.label.stringValue() != text:
                button.label.setStringValue_(text)
                self._center(button)
            if status == "allowed":
                done.setStringValue_("✓  Allowed")
                if not first:
                    _pop(done)
            self.perm_rows[kind] = (button, done, status)
        text = f"{allowed} of {len(PERMISSIONS)} allowed"
        if self.summary.label.stringValue() != text:
            self.summary.label.setStringValue_(text)
            self.summary.label.setTextColor_(_ns(OK if allowed == len(PERMISSIONS) else INK))
            self._center(self.summary)
            if not first:
                _pop(self.summary)

    # shortcuts

    def _page_shortcuts(self, page) -> list:
        items = self._heading(page, "Shortcuts", "Your keys to me.",
                              "They work in any app. Click a shortcut to change it; Esc cancels.")
        self.key_cards = {}
        for i, (key, symbol, title, text, modifier_ok) in enumerate(SHORTCUTS):
            col, row = i % 2, i // 2
            card = self._card(page, 80 + col * 418, 204 + row * 168, 402, 152,
                              action=lambda k=key, m=modifier_ok: self._record(k, m))
            self._tile(card, symbol, (_accent(), BLUE, VIOLET, ORANGE)[i], 22, 22, size=38)
            self._label(card, title, 74, 22, 300, size=17, weight=AppKit.NSFontWeightSemibold, rounded=True)
            self._label(card, text, 74, 46, 306, size=13, rgb=DIM, lines=2)
            keys = _OnbFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(22, 98, 358, 36))
            card.addSubview_(keys)
            card.keys = keys
            change = self._label(card, "Change", 300, 106, 80, size=12, weight=AppKit.NSFontWeightMedium, rgb=DIM,
                                 align=AppKit.NSTextAlignmentRight)
            card.change = change
            card.on_hover = (lambda over, c=card: _tint(c, WHITE, 0.5 if over else 0.0)
                             or c.change.setTextColor_(_ns(DEEP if over else DIM)))
            self.key_cards[key] = card
            self._paint_keys(key)
            items.append((card, 0.0, 0.95))
        return items

    def _paint_keys(self, key: str, prompt: str = "") -> None:
        card = self.key_cards[key]
        for view in list(card.keys.subviews()):
            view.removeFromSuperview()
        mint = _accent()
        if prompt:
            self._label(card.keys, prompt, 0, 8, 300, size=15, weight=AppKit.NSFontWeightSemibold, rgb=mint)
            card.layer().setBorderColor_(_cg(mint, 0.9))
            card.layer().setBorderWidth_(1.5)
            pulse = Quartz.CABasicAnimation.animationWithKeyPath_("borderColor")
            pulse.setFromValue_(_cg(mint, 0.9))
            pulse.setToValue_(_cg(mint, 0.25))
            pulse.setDuration_(0.8)
            pulse.setAutoreverses_(True)
            pulse.setRepeatCount_(1e9)
            card.layer().addAnimation_forKey_(pulse, "listening")
            return
        card.layer().removeAnimationForKey_("listening")
        card.layer().setBorderColor_(_cg(WHITE, 0.0))
        card.layer().setBorderWidth_(0)
        value = prefs.get("shortcuts").get(key, "")
        caps = _caps(value)
        x = 0
        if key == "dictate" and caps:
            word = self._label(card.keys, "Hold", 0, 9, 40, size=13, weight=AppKit.NSFontWeightMedium, rgb=DIM)
            word.sizeToFit()
            x = word.frame().size.width + 10
        if not caps:
            self._label(card.keys, "Not set", 0, 9, 200, size=14, weight=AppKit.NSFontWeightMedium, rgb=DIM)
        for cap in caps:
            font = _font(15 if len(cap) == 1 else 13, AppKit.NSFontWeightSemibold, rounded=True)
            width = max(36, int(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                cap, {AppKit.NSFontAttributeName: font}).size().width) + 22)
            cap_view = _OnbFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(x, 0, width, 34))
            cap_view.setWantsLayer_(True)
            layer = cap_view.layer()
            layer.setCornerRadius_(8)
            layer.setBackgroundColor_(_cg(WHITE, 0.92))
            layer.setBorderColor_(_cg(INK, 0.1))
            layer.setBorderWidth_(0.5)
            layer.setShadowColor_(_cg((0.1, 0.2, 0.45)))
            layer.setShadowOpacity_(0.22)
            layer.setShadowRadius_(1)
            layer.setShadowOffset_(Quartz.CGSizeMake(0, -2))
            label = self._label(cap_view, cap, 0, 0, width, size=15 if len(cap) == 1 else 13,
                                weight=AppKit.NSFontWeightSemibold, rounded=True, align=AppKit.NSTextAlignmentCenter)
            label.sizeToFit()
            label.setFrame_(AppKit.NSMakeRect(0, (34 - label.frame().size.height) / 2, width,
                                              label.frame().size.height))
            card.keys.addSubview_(cap_view)
            x += width + 6

    def _record(self, key: str, modifier_ok: bool) -> None:
        from mint.ui.settings import record_keys
        if self.recording and self.recording != key:
            self._paint_keys(self.recording)
        self.recording = key

        def done(value):
            self.recording = None
            if value is not None:
                keys = dict(prefs.get("shortcuts"))
                keys[key] = value
                prefs.set("shortcuts", keys)
            self._paint_keys(key)
            _pop(self.key_cards[key].keys)
        record_keys(modifier_ok, lambda text: self._paint_keys(key, text), done)

    # tour

    def _page_tour(self, page) -> list:
        items = []
        head = self._label(page, "What I can do", 40, 70, 250, size=13, weight=AppKit.NSFontWeightBold, rgb=_accent())
        head.setAttributedStringValue_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "WHAT I CAN DO", {AppKit.NSKernAttributeName: 1.6, AppKit.NSForegroundColorAttributeName: _ns(DEEP),
                              AppKit.NSFontAttributeName: _font(12, AppKit.NSFontWeightBold)}))
        items.append(head)
        self.tour_items = []
        # The list fits between the heading and Back, however many features there are.
        step = min(38.0, (H - 96 - 78) / len(TOUR))
        tall = min(34.0, step - 3)
        for i, (_clip, symbol, title, _text, _say) in enumerate(TOUR):
            row = _OnbClick.alloc().initWithFrame_(AppKit.NSMakeRect(28, 96 + i * step, 256, tall))
            row.setWantsLayer_(True)
            row.layer().setCornerRadius_(10)
            icon = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(12, (tall - 16) / 2, 16, 16))
            icon.setImage_(gfx.symbol(symbol, 12))
            row.addSubview_(icon)
            label = self._label(row, title, 38, (tall - 18) / 2, 210, size=13, weight=AppKit.NSFontWeightMedium)
            row.icon, row.text = icon, label
            row.on_click = lambda n=i: self._show_feature(n, by_hand=True)
            row.on_hover = lambda over, r=row: getattr(r, "chosen", False) or r.layer().setBackgroundColor_(
                _cg(WHITE, 0.45 if over else 0.0))
            page.addSubview_(row)
            self.tour_items.append(row)
            items.append((row, 0.0, 1.0))
        # The clip, and what it shows.
        frame = _OnbFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(316, 70, 624, 366))
        frame.setWantsLayer_(True)
        frame.layer().setCornerRadius_(18)
        frame.layer().setMasksToBounds_(True)
        frame.layer().setBackgroundColor_(_cg((0.97, 0.97, 1.0)))
        frame.layer().setBorderColor_(_cg(WHITE, 0.95))
        frame.layer().setBorderWidth_(3)
        page.addSubview_(frame)
        self.poster = AppKit.NSImageView.alloc().initWithFrame_(frame.bounds())
        self.poster.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        frame.addSubview_(self.poster)
        video = AppKit.NSView.alloc().initWithFrame_(frame.bounds())
        video.setWantsLayer_(True)
        frame.addSubview_(video)
        import AVFoundation
        self.player = AVFoundation.AVPlayer.alloc().init()
        self.player.setMuted_(True)
        player_layer = AVFoundation.AVPlayerLayer.playerLayerWithPlayer_(self.player)
        player_layer.setFrame_(video.bounds())
        player_layer.setVideoGravity_(AVFoundation.AVLayerVideoGravityResizeAspect)
        video.layer().addSublayer_(player_layer)
        self.video_frame = frame
        self.counter = self._pill(frame, "", 14, 14, 64, 26, lambda: None, primary=False, size=11)
        self.counter.glass.removeFromSuperview()
        self.counter.glass = None
        self.counter.layer().setBackgroundColor_(_cg(INK, 0.62))
        self.counter.label.setTextColor_(_ns(WHITE))
        self.counter.on_click, self.counter.on_hover = None, None
        items.append((frame, 0.08, 0.97))
        self.feature_title = self._label(page, "", 316, 452, 624, size=24, weight=AppKit.NSFontWeightBold, rounded=True)
        self.feature_text = self._label(page, "", 316, 486, 624, size=14, rgb=DIM, lines=2)
        self.feature_say = _OnbFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(316, 532, 624, 32))
        page.addSubview_(self.feature_say)
        items += [self.feature_title, self.feature_text, self.feature_say]
        self.tour_auto = True
        self._show_feature(0)
        return items

    def _show_feature(self, index: int, by_hand: bool = False) -> None:
        import AVFoundation
        if by_hand:
            self.tour_auto = False
        index %= len(TOUR)
        self.tour_index = index
        clip, _symbol, title, text, say = TOUR[index]
        mint = _accent()
        for i, row in enumerate(self.tour_items):
            on = i == index
            row.chosen = on
            row.layer().setBackgroundColor_(_cg(WHITE, 0.7) if on else _cg(WHITE, 0.0))
            row.text.setTextColor_(_ns(INK if on else DIM))
            row.text.setFont_(_font(13, AppKit.NSFontWeightSemibold if on else AppKit.NSFontWeightMedium))
            row.icon.setContentTintColor_(_ns(SKY if on else DIM))
        fade = Quartz.CATransition.animation()
        fade.setType_(Quartz.kCATransitionFade)
        fade.setDuration_(0.35)
        self.video_frame.layer().addAnimation_forKey_(fade, "swap")
        url = _media(clip, "jpg")
        self.poster.setImage_(None)
        if url.isFileURL():
            self.poster.setImage_(AppKit.NSImage.alloc().initWithContentsOfURL_(url))
        else:
            def fetch(u=url, want=index):
                image = AppKit.NSImage.alloc().initWithContentsOfURL_(u)
                if image is not None:
                    AppHelper.callAfter(lambda: self.tour_index == want and self.poster.setImage_(image))
            threading.Thread(target=fetch, daemon=True, name="tour-poster").start()
        item = AVFoundation.AVPlayerItem.playerItemWithURL_(_media(clip, "mp4"))
        center = AppKit.NSNotificationCenter.defaultCenter()
        if self.player_end is not None:
            center.removeObserver_(self.player_end)
        self.player_end = center.addObserverForName_object_queue_usingBlock_(
            AVFoundation.AVPlayerItemDidPlayToEndTimeNotification, item, AppKit.NSOperationQueue.mainQueue(),
            lambda note: self._clip_ended())
        self.player.replaceCurrentItemWithPlayerItem_(item)
        self.player.play()
        self.counter.label.setStringValue_(f"{index + 1} of {len(TOUR)}")
        self._center(self.counter)
        self.feature_title.setStringValue_(title)
        self.feature_text.setStringValue_(text)
        for view in list(self.feature_say.subviews()):
            view.removeFromSuperview()
        x = 0
        for phrase in say:
            words = f"“{phrase}”"
            width = int(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                words, {AppKit.NSFontAttributeName: _font(13, AppKit.NSFontWeightMedium)}).size().width) + 28
            chip = self._pill(self.feature_say, words, x, 0, width, 30, lambda: None, primary=False, size=13)
            chip.on_click, chip.on_hover = None, None
            chip.label.setTextColor_(_ns(DEEP))
            _enter(chip, 0.12 + x / 3000, rise=8)
            x += width + 8
        for view in (self.feature_title, self.feature_text):
            _enter(view, 0.0, rise=8)

    def _clip_ended(self) -> None:
        if self.view is None or PAGES[self.page] != "tour":
            return
        if self.tour_auto and self.tour_index < len(TOUR) - 1:
            self._show_feature(self.tour_index + 1)
        else:
            self.player.seekToTime_(_zero())
            self.player.play()

    # done

    def _page_done(self, page) -> list:
        you = str(prefs.get("user_name") or "").split()
        me = prefs.name()
        orb, host = self._orb(page, W / 2, 172, 112, halo=True)
        AppHelper.callLater(0.5, orb.celebrate)
        AppHelper.callLater(0.4, self._confetti)
        title = self._label(page, f"You're all set{', ' + you[0] if you else ''}.", 0, 262, W, size=44,
                            weight=AppKit.NSFontWeightBold, rounded=True, align=AppKit.NSTextAlignmentCenter)
        talk = "".join(_caps(prefs.get("shortcuts").get("talk", "")))
        sub = self._label(page, f"Say “Hey {me}”" + (f" or press {talk}" if talk else "") + " whenever you need me. "
                          "Try one of these:", (W - 700) / 2, 326, 700, size=16, rgb=DIM, lines=2,
                          align=AppKit.NSTextAlignmentCenter)
        items = [(host, 0.0, 0.6), title, sub]
        x = (W - (3 * 264 + 2 * 14)) / 2
        for i, (symbol, words, hint) in enumerate(TRY):
            card = self._card(page, x + i * 278, 384, 264, 118, action=lambda w=words: self._try(w))
            self._tile(card, symbol, (BLUE, ORANGE, VIOLET)[i], 20, 18, size=34)
            self._label(card, f"“{words}”", 20, 64, 230, size=14, weight=AppKit.NSFontWeightSemibold)
            self._label(card, hint, 20, 86, 230, size=12, rgb=DIM)
            arrow = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(264 - 36, 22, 16, 16))
            arrow.setImage_(gfx.symbol("arrow.up.right", 11))
            arrow.setContentTintColor_(_ns(DIM))
            card.addSubview_(arrow)
            items.append((card, 0.0, 0.94))
        items.append(self._pill(page, f"Start using {me}", (W - 240) / 2, 534, 240, 50, self.finish,
                                symbol="sparkles", size=16))
        return items

    def _confetti(self) -> None:
        if self.view is None:
            return
        emitter = Quartz.CAEmitterLayer.layer()
        emitter.setEmitterPosition_(Quartz.CGPointMake(W / 2, H + 10))
        emitter.setEmitterSize_(Quartz.CGSizeMake(W * 0.8, 1))
        emitter.setEmitterShape_(Quartz.kCAEmitterLayerLine)
        cells = []
        for rgb in (_mint(), BLUE, VIOLET, ORANGE, PINK, (1.0, 0.8, 0.2)):
            for shape in ("circle.fill", "star.fill", "rectangle.fill"):
                cell = Quartz.CAEmitterCell.emitterCell()
                cell.setContents_(gfx.cg_image(gfx.symbol(shape, 9, white=True)))
                cell.setColor_(_cg(rgb))
                cell.setBirthRate_(5)
                cell.setLifetime_(4.5)
                cell.setVelocity_(300)
                cell.setVelocityRange_(120)
                cell.setEmissionLongitude_(-math.pi / 2)
                cell.setEmissionRange_(0.6)
                cell.setYAcceleration_(-260)
                cell.setSpin_(2.5)
                cell.setSpinRange_(5)
                cell.setScale_(0.9)
                cell.setScaleRange_(0.5)
                cell.setAlphaSpeed_(-0.2)
                cells.append(cell)
        emitter.setEmitterCells_(cells)
        emitter.setBeginTime_(Quartz.CACurrentMediaTime())
        self.root.layer().addSublayer_(emitter)
        AppHelper.callLater(0.45, lambda: emitter.setBirthRate_(0))
        AppHelper.callLater(6.0, emitter.removeFromSuperlayer)

    def _try(self, words: str) -> None:
        self.finish()
        try:
            from mint.ui.island import island
            if island.hud is not None:
                AppHelper.callLater(0.6, lambda: island.hud._fire("submit", words))
        except Exception:
            log.debug("try it", exc_info=True)

    # --- moving between pages -----------------------------------------------------------------------------

    def _go(self, page: int) -> None:
        page = max(0, min(page, len(PAGES) - 1))
        forward = page >= self.page
        self._leave()
        old = self.view
        if old is not None:
            AppKit.NSAnimationContext.beginGrouping()
            AppKit.NSAnimationContext.currentContext().setDuration_(0.22)
            old.animator().setAlphaValue_(0.0)
            AppKit.NSAnimationContext.endGrouping()
            slide = Quartz.CABasicAnimation.animationWithKeyPath_("transform.translation.x")
            slide.setToValue_(-40.0 if forward else 40.0)
            slide.setDuration_(0.22)
            slide.setFillMode_(Quartz.kCAFillModeForwards)
            slide.setRemovedOnCompletion_(False)
            old.setWantsLayer_(True)
            old.layer().addAnimation_forKey_(slide, "leave")
            orbs = [o for o in self.orbs if o[1].isDescendantOf_(old)]
            self.orbs = [o for o in self.orbs if o not in orbs]
            AppHelper.callLater(0.25, old.removeFromSuperview)
        self.page = page
        self.fields = {}
        view = _OnbFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        view.setWantsLayer_(True)
        self.stage.addSubview_(view)
        self.view = view
        items = getattr(self, f"_page_{PAGES[page]}")(view)
        for n, item in enumerate(items):
            target, extra, scale = item if isinstance(item, tuple) else (item, 0.0, 1.0)
            _enter(target, 0.12 + n * 0.045 + extra, scale=scale)
        self._paint_chrome()
        self._drift(page)
        first = next((f for f in self.fields.values()), None)
        self.window.makeFirstResponder_(first if first is not None else self.window.contentView())

    def _leave(self) -> None:
        """Keep what this page asked for."""
        name = PAGES[self.page] if self.view is not None else ""
        if name == "about" and self.fields:
            you = str(self.fields["user_name"].stringValue()).strip()
            me = str(self.fields["assistant_name"].stringValue()).strip() or "Mint"
            extra = str(self.fields["about_me"].stringValue()).strip()
            roles = sorted(self.roles, key=ROLES.index)
            about = (f"Role: {', '.join(roles)}.\n" if roles else "") + extra
            if you != str(prefs.get("user_name") or ""):
                prefs.set("user_name", you)
            if about.strip() != str(prefs.get("about_me") or "").strip():
                prefs.set("about_me", about.strip())
            if me != prefs.name():
                prefs.set("assistant_name", me)
        elif name == "voice" and self.voice and self.voice != str(prefs.get("voice_name") or config.VOICE):
            prefs.set("voice_name", self.voice)
        elif name == "shortcuts" and self.recording:
            from mint.ui.settings import cancel_recording
            cancel_recording()
            self.recording = None
        elif name == "tour" and self.player is not None:
            self.player.pause()
            if self.player_end is not None:
                AppKit.NSNotificationCenter.defaultCenter().removeObserver_(self.player_end)
                self.player_end = None

    def next(self) -> None:
        if self.page < len(PAGES) - 1:
            self._go(self.page + 1)

    def back(self) -> None:
        if self.page > 1:
            self._go(self.page - 1)

    def skip(self) -> None:
        self.finish()

    def finish(self) -> None:
        if self.window is None or not self.window.isVisible():
            return
        self._leave()
        self.view = None
        prefs.set("onboarded", True)
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.3)
        self.window.animator().setAlphaValue_(0.0)
        AppKit.NSAnimationContext.endGrouping()
        AppHelper.callLater(0.32, self.window.orderOut_, None)
        try:
            from mint.ui.island import island
            if island.hud is not None:
                AppHelper.callLater(0.5, lambda: island.hud.orb.celebrate())
        except Exception:
            pass

    def closed(self) -> None:
        """The window's close button: the same as Skip."""
        if self.view is not None:
            self._leave()
            self.view = None
            prefs.set("onboarded", True)

    # --- events -------------------------------------------------------------------------------------------

    def key(self, event) -> bool:
        code = int(event.keyCode())
        name = PAGES[self.page]
        if code in (36, 76):                                      # Return
            if name == "done":
                self.finish()
            else:
                self.next()
            return True
        if name == "tour" and code in (123, 124, 125, 126):      # arrows
            self._show_feature(self.tour_index + (1 if code in (124, 125) else -1), by_hand=True)
            return True
        if code == 53:
            self.skip()
            return True
        return False

    def typed(self, field) -> None:
        if PAGES[self.page] == "about":
            self._greet(pop=False)

    def focus(self, field, on: bool) -> None:
        box = self.boxes.get(objc.pyobjc_id(field))
        if box is None:
            return
        mint = _accent()
        box.layer().setBorderColor_(_cg(mint, 0.8) if on else _cg(WHITE, 0.0))
        _tint(box, WHITE, 0.45 if on else 0.0)

    def tick(self) -> None:
        if self.window is None or not self.window.isVisible():
            return
        now = time.monotonic()
        mouse = AppKit.NSEvent.mouseLocation()
        for orb, host in self.orbs:
            frame = self.window.convertRectToScreen_(host.convertRect_toView_(host.bounds(), None))
            cx, cy = frame.origin.x + frame.size.width / 2, frame.origin.y + frame.size.height / 2
            orb.tick(now, 0.0, (mouse.x - cx, mouse.y - cy), False, False)
        self._ticks += 1
        if self._ticks % 30 == 0 and PAGES[self.page] == "permissions" and self.view is not None:
            self._check_permissions()

    # --- showing --------------------------------------------------------------------------------------------

    def show(self, page: int = 0) -> None:
        """Main thread."""
        if self.window is None:
            self._build()
        self.orbs = []
        if self.view is not None:
            self.view.removeFromSuperview()
            self.view = None
        self.page = page
        self.window.setAlphaValue_(1.0)
        self.window.center()
        AppKit.NSApp().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self._go(page)
        self._paint_chrome(animated=False)


def _zero():
    import CoreMedia
    return CoreMedia.CMTimeMake(0, 1)


onboarding = Onboarding()


def show(page: int = 0) -> None:
    """From any thread."""
    AppHelper.callAfter(onboarding.show, page)


def needed() -> bool:
    return not prefs.get("onboarded")
