"""Settings: one window, laid out like System Settings - a sidebar of pages, each a column of
cards with a label on the left and its control on the right.

Everything here writes settings.json through prefs, so changes apply the same way as a menu
click or a hand edit: listeners in main.py react (audio devices rebuild the engine, a new name
trains its wake word, a new voice or language reconnects, and so on).

Pages
  General                 "Needs your OK" (important macOS permissions still missing, with Allow…), assistant
                          name, personality, your name, about you, start at login, Quit, Show advanced settings
  Microphone & voice      the microphone test, Train my voice and the voice lock, the wake word and its
                          sensitivity, microphone and speaker, what to do during calls
  Speaking                which of the 30 Gemini voices, speaking style, language, captions
  Appearance & Sound      where Mint lives, theme, animation level, effects, sounds and volume
  Shortcuts               keys for the chat, talking, dictation, the clipboard; Apple Shortcuts below
  Accounts & connections  API keys, Google, Telegram, Email control; then the apps Mint works with
  Models & agents         Gemini keys, the agents (providers and backups when advanced)
  Skills & Memory         counts, and the window to edit them
  Permissions & Privacy   every macOS permission (Allowed / Not allowed, Allow…), the Mint folder, meetings,
                          clipboard, the activity timeline
  Usage                   tokens per model: today, 7 days, 30 days
  Updates & Help          version, updates, the guide, reporting a problem, restarting Mint

Old page keys still work (ALIASES): "audio" opens Microphone & voice, "apple_shortcuts" Shortcuts,
"connectors" Accounts & connections, "permissions" and "privacy" Permissions & Privacy (key "storage").

Basic and advanced: each page shows what most people change; the technical rows (a custom wake phrase,
echo cancellation, model providers, guard levels…) are built only when the "settings_advanced" pref is on
(`self._advanced()`): the Essentials | All settings switch at the top right of a page, General ▸ Show
advanced settings, or the small Show all settings line at the bottom of a page.

Permissions (permissions.py, shared with the welcome window) are read with the page's facts; while General or
Permissions & Privacy is on screen they are read again every few seconds (and when the window comes back to the
front), off the main thread, and the page is rebuilt only when one changed - so allowing Mint in System
Settings shows here at once.

Looks: brand marks (brands.icon_tile - an official logo file, the installed app's icon, or a drawn tile:
Google's four-colour G, Gemini's sparkle…, the same mark everywhere),
prominent tinted buttons for the main action of a row (Add key…, Connect…, Allow…), and small coloured status
badges (_badge: Set / Not set / Allowed).

Nothing that can wait runs on the main thread: what a page shows from elsewhere (files, CoreAudio,
`shortcuts list`, other modules and their locks, the network) is read by its _facts_<page> on a
worker thread, and the page is built from that. A fast read (almost always) is waited for, up to
LOAD_WAIT; a slow one shows "Loading…" and the page fills in when it arrives; a stuck one says so
after LOAD_SLOW, logs where the read is stuck, and offers Try again. Buttons that talk to the network
or other apps do it on a thread too. Before, one slow read froze all of Mint - this window, the orb,
the menu - and Mint has no Dock icon or Force Quit entry, so the only way out was restarting the Mac.
"""

from __future__ import annotations

import functools
import logging
import os
import subprocess
import sys
import threading
import time
import traceback

import AppKit
import objc
from PyObjCTools import AppHelper

from mint.voice import devices as audio_devices
from mint.core import prefs
from mint.ui import settings_art
from mint.voice import voicelock
from mint.voice import voices
from mint.ui.brands import BRANDS, ICON, brand as _brand, icon_tile as _icon_tile, rgb as _rgb  # noqa: F401 - used here and by tests
from mint.ui.brands import brand_image as _brand_image

log = logging.getLogger("mint.settings")

W, H = 840, 600
SIDEBAR = 230                 # room for "Accounts & connections"
PAD = 28                      # the page's side margins
ROW = 44
LOAD_WAIT = 0.25              # the main thread waits this long for a page's facts, then shows "Loading…"
LOAD_SLOW = 8.0               # still not there: say so, offer Try again, log where the read is stuck
# Pop-up options: (value, title[, icon[, subtitle]]) - icon: an SF Symbol name, (symbol, rgb), an NSImage or a
# callable returning one (settings_art.resolve); subtitle: a grey second line in the menu.
_BLUE, _GREEN, _ORANGE, _RED = (0.2, 0.55, 1.0), (0.2, 0.72, 0.42), (1.0, 0.58, 0.1), (0.95, 0.3, 0.3)
_PURPLE, _PINK, _GREY, _TEAL = (0.58, 0.42, 0.95), (1.0, 0.38, 0.6), (0.5, 0.54, 0.62), (0.12, 0.68, 0.66)
ECHO = [("auto", "Automatic", ("wand.and.stars", _BLUE), "Off with headphones, on with speakers"),
        ("on", "Always on", ("checkmark.circle.fill", _GREEN)), ("off", "Off", ("xmark.circle", _GREY))]
STRICTNESS = [("relaxed", "Relaxed", ("lock.open.fill", _GREEN), "Lets more through"),
              ("balanced", "Balanced (recommended)", ("lock.fill", _BLUE)),
              ("strict", "Strict", ("lock.shield.fill", _ORANGE), "Only a close match to your voice")]
THEMES = [(theme, title, functools.partial(settings_art.orb, theme))
          for theme, title in (("mint", "Mint"), ("blue", "Classic blue"), ("aurora", "Aurora"),
                               ("sunset", "Sunset"), ("rose", "Rose"), ("mono", "Mono"))]
PERSONALITIES = [("", "Default", ("person.crop.circle", _GREY)),
                 ("funny and playful, with light jokes", "Funny", ("face.smiling.inverse", _ORANGE)),
                 ("calm, gentle and patient", "Calm", ("leaf.fill", _GREEN)),
                 ("loves a friendly argument - pushes back and makes its case", "Argumentative",
                  ("bubble.left.and.bubble.right.fill", _RED)),
                 ("dry and a little sarcastic, but kind", "Sarcastic", ("theatermasks.fill", _PURPLE)),
                 ("an upbeat coach who cheers me on", "Motivating", ("flame.fill", (1.0, 0.45, 0.2))),
                 ("formal and precise, like a butler", "Formal", ("text.book.closed.fill", _BLUE))]
STYLES = [("", "Natural (no style)", ("waveform", _GREY)),
          ("warm and calm", "Warm and calm", ("cup.and.saucer.fill", (0.82, 0.52, 0.3))),
          ("cheerful and energetic", "Cheerful and energetic", ("sun.max.fill", (0.98, 0.72, 0.05))),
          ("slow and clear", "Slow and clear", ("tortoise.fill", _GREEN)),
          ("brisk and to the point", "Brisk and to the point", ("hare.fill", _ORANGE)),
          ("soft and gentle", "Soft and gentle", ("cloud.fill", (0.45, 0.68, 0.98))),
          ("professional and confident", "Professional and confident", ("briefcase.fill", _BLUE)),
          ("friendly, with an Indian English accent", "Friendly, Indian English accent",
           functools.partial(settings_art.flag, "Hindi"))]
VOICE_FILTERS = [("all", "All 30 voices", ("person.2.fill", _GREY)),
                 ("female", "Female voices", ("person.fill", settings_art.GENDER_RGB["female"])),
                 ("male", "Male voices", ("person.fill", settings_art.GENDER_RGB["male"]))]
UNLOAD = [(0, "Never (recommended)", ("bolt.fill", _GREEN), "Answers at once")] + [
    (m, f"After {words} asleep", ("moon.zzz.fill", _PURPLE), "Wakes a second or two slower")
    for m, words in ((5, "5 minutes"), (10, "10 minutes"), (20, "20 minutes"), (30, "30 minutes"), (60, "an hour"))]
MCP_NAMES = {"claude": "Claude Code", "codex": "Codex"}
WHERE = [(False, "A floating orb", functools.partial(settings_art.where, False), "Anywhere on screen - drag it"),
         (True, "The notch", functools.partial(settings_art.where, True), "Lives in the island at the top")]
OPEN_TO = [("auto", "Auto", ("sparkles", _PURPLE), "Home, or your agents while they work"),
           ("home", "Home", ("house.fill", _BLUE)), ("search", "Search", ("magnifyingglass", _TEAL)),
           ("shelf", "Shelf", ("tray.full.fill", _ORANGE)),
           ("agents", "Coding agents", ("chevron.left.forwardslash.chevron.right", (0.85, 0.47, 0.34)))]
MOTION = [("full", "Full (playful)", ("sparkles", _PINK), "Bounces, squishes and little hops"),
          ("calm", "Calm", ("leaf.fill", _GREEN), "Gentle, no bounce"),
          ("minimal", "Minimal (fades only)", ("circle.dotted", _GREY), "Like macOS Reduce motion")]
AGENT_MODES = [("quiet", "Pop-ups only", ("bubble.left.fill", _BLUE), "When one asks or finishes"),
               ("auto", "Live while they work", ("play.circle.fill", _GREEN), "Step by step as they go"),
               ("on", "Always (Agents tab)", ("rectangle.stack.fill", _PURPLE), "The notch opens to your agents"),
               ("off", "Off", ("xmark.circle", _GREY))]
WAKE_SENSITIVITY = [("normal", "Normal", ("ear", _BLUE)),
                    ("high", "High - catches more", ("ear.badge.waveform", _ORANGE)),
                    ("low", "Low - fewer false starts", ("checkmark.shield", _GREEN))]
def _updating(status, requested, ready) -> bool:
    """An update on its way: downloading, being checked or installed, or asked for and not downloaded yet (and not
    failed or done - those show their own words and buttons)."""
    if status in ("downloading", "verifying", "installing"):
        return True
    return bool(requested) and not ready and status not in ("error", "ready", "installed", "current", "blocked")


NOTCH_OPEN_AFTER = [(0.5, "Half a second", ("hare.fill", _GREEN)), (0.8, "Under a second", ("timer", _BLUE),
                                                                            "Default"),
                    (1.2, "About a second", ("timer", _BLUE)), (2.0, "Two seconds", ("tortoise.fill", _ORANGE))]
NOTCH_CLOSE_AFTER = [(1.0, "1 second", ("hare.fill", _GREEN), "Default"), (2.0, "2 seconds", ("timer", _BLUE)),
                     (3.0, "3 seconds", ("timer", _BLUE)), (5.0, "5 seconds", ("timer", _BLUE)),
                     (8.0, "8 seconds", ("tortoise.fill", _ORANGE))]
FOLLOW_UP = [(0, "No - always say the wake word", ("mic.slash", _GREY))] + [
    (s, f"{s} seconds", ("timer", _BLUE)) for s in (4, 6, 8, 12)]
GUARD_LEVELS = [("all", "Deleting, changes and system commands", ("checkmark.shield.fill", _GREEN), "Safest"),
                ("delete", "Only deleting and system commands", ("trash.fill", _ORANGE)),
                ("off", "Never ask", ("exclamationmark.shield.fill", _RED), "Mint just does it")]
MEET_SHARE = [("screen", "The whole screen", ("display", _GREEN)), ("off", "Don't share", ("eye.slash", _GREY))]
AGENT_TELEGRAM = [("away", "When I'm away from the Mac", ("figure.walk", _BLUE)),
                  ("always", "Always", ("paperplane.fill", (0.16, 0.62, 0.93))), ("off", "Never", ("bell.slash", _GREY))]
POSITIONS = [(spot, title, functools.partial(settings_art.position, spot))
             for spot, title in (("top-right", "Top right"), ("top-left", "Top left"), ("top-center", "Top centre"),
                                 ("bottom-right", "Bottom right"), ("bottom-left", "Bottom left"),
                                 ("custom", "Where I dragged it"))]
LANGUAGES = [("auto", "Same as I speak (automatic)"), ("English", "English"), ("Hindi", "Hindi"),
             ("Hinglish", "Hinglish"), ("Bengali", "Bengali"), ("Marathi", "Marathi"), ("Tamil", "Tamil"),
             ("Telugu", "Telugu"), ("Gujarati", "Gujarati"), ("Kannada", "Kannada"), ("Punjabi", "Punjabi"),
             ("Urdu", "Urdu"), ("Spanish", "Spanish"), ("French", "French"), ("German", "German"),
             ("Italian", "Italian"), ("Portuguese", "Portuguese"), ("Dutch", "Dutch"), ("Russian", "Russian"),
             ("Turkish", "Turkish"), ("Arabic", "Arabic"), ("Japanese", "Japanese"), ("Korean", "Korean"),
             ("Chinese (Mandarin)", "Chinese (Mandarin)"), ("Indonesian", "Indonesian"),
             ("Vietnamese", "Vietnamese"), ("Thai", "Thai")]
LANGUAGES = [(v, t, functools.partial(settings_art.flag, v) if v in settings_art.FLAGS else ("globe", _BLUE),
              settings_art.NATIVE.get(v, "")) for v, t in LANGUAGES]
SCREENSHOTS = [("both", "A file and the clipboard", ("square.on.square.fill", _BLUE)),
               ("clipboard", "The clipboard only", ("doc.on.clipboard.fill", _PURPLE)),
               ("file", "A file only", ("doc.fill", _TEAL))]
CALLS = [("listen", "Keep listening for the wake word", ("ear", _GREEN)),
         ("stop", "Stop listening until the call ends", ("mic.slash.fill", _ORANGE)),
         ("nothing", "Do nothing special", ("phone", _GREY))]
# (env, title, hint, advanced only)
KEYS = [("GEMINI_API_KEY", "Gemini", "Required - free at aistudio.google.com/apikey", False),
        ("TYPESAFE_API_KEY", "TypeSafe (Jev)", "Optional - surer clicking and typing in any app", False),
        ("OPENAI_API_KEY", "OpenAI", "Optional - background agents. Other model providers (Claude, Groq, "
                                     "Ollama…) are in Models & agents.", True)]
PAGES = [("general", "General", "gearshape"), ("voice", "Microphone & voice", "mic"),
         ("speaking", "Speaking", "person.wave.2"),
         ("looks", "Appearance & Sound", "paintpalette"), ("shortcuts", "Shortcuts", "command"),
         ("accounts", "Accounts & connections", "person.crop.circle"),
         ("models", "Models & agents", "cpu"),
         ("brain", "Skills & Memory", "brain"),
         ("storage", "Permissions & Privacy", "hand.raised"), ("usage", "Usage", "chart.bar"),
         ("help", "Updates & Help", "questionmark.circle")]
# Pages that were merged into another: show("audio") opens Microphone & voice, and so on.
ALIASES = {"audio": "voice", "apple_shortcuts": "shortcuts", "connectors": "accounts", "permissions": "storage",
           "privacy": "storage"}
# Pages with rows that only "All settings" shows: they get the Essentials | All settings switch at the top.
ADVANCED_PAGES = {"general", "voice", "looks", "shortcuts", "accounts", "models", "help"}
# Pages that show permissions: read again every PERM_EVERY seconds while one is on screen.
PERM_PAGES = {"general": "important", "storage": "all"}
PERM_EVERY = 3.0
# A small coloured symbol before a section's title.
SECTION_ICONS = {
    "Needs your OK": ("exclamationmark.triangle.fill", (1.0, 0.62, 0.04)),
    "Permissions": ("hand.raised.fill", (0.2, 0.47, 1.0)),
    "Assistant": ("person.crop.circle.fill", (0.2, 0.75, 0.55)), "Behaviour": ("slider.horizontal.3", (0.55, 0.45, 0.95)),
    "Microphone test": ("waveform", (1.0, 0.36, 0.55)), "Your voice": ("person.wave.2.fill", (0.2, 0.6, 1.0)),
    "Wake word": ("ear.fill", (1.0, 0.6, 0.1)), "Microphone and speaker": ("speaker.wave.2.fill", (0.45, 0.5, 0.62)),
    "Calls and meetings": ("phone.fill", (0.2, 0.75, 0.4)), "Voice": ("person.wave.2.fill", (0.2, 0.6, 1.0)),
    "Language": ("globe", (0.2, 0.6, 1.0)), "Replies": ("text.bubble.fill", (0.55, 0.45, 0.95)),
    "View": ("macwindow", (0.2, 0.6, 1.0)), "Sounds": ("speaker.wave.2.fill", (1.0, 0.45, 0.3)),
    "Keyboard shortcuts": ("command", (0.45, 0.5, 0.62)), "Dictation": ("mic.fill", (1.0, 0.42, 0.62)),
    "API keys": ("key.fill", (0.95, 0.68, 0.0)), "Google: Gmail, Calendar and Meet": ("g.circle.fill", (0.26, 0.52, 0.96)),
    "Telegram remote control": ("paperplane.fill", (0.16, 0.62, 0.93)), "Email control": ("at", (1.0, 0.55, 0.1)),
    "Gemini": ("sparkles", (0.4, 0.45, 0.98)), "AI models for your agents": ("cpu.fill", (0.55, 0.45, 0.95)),
    "Agents": ("person.3.fill", (0.24, 0.72, 0.62)), "Mint folder": ("folder.fill", (0.2, 0.6, 1.0)),
    "Meetings": ("video.fill", (0.2, 0.75, 0.4)), "Clipboard": ("doc.on.clipboard.fill", (0.55, 0.45, 0.95)),
    "Privacy": ("lock.fill", (0.45, 0.5, 0.62)), "Settings window": ("gearshape.fill", (0.45, 0.5, 0.62)),
    "Tokens by model": ("chart.bar.fill", (0.2, 0.6, 1.0)), "Last 14 days": ("calendar", (1.0, 0.36, 0.33)),
    "Hey Mint": ("arrow.down.circle.fill", (0.2, 0.75, 0.4)), "Your Apple Shortcuts": ("square.stack.3d.up.fill",
                                                                                       (0.93, 0.27, 0.55)),
    "Claude mode (coding agents)": ("chevron.left.forwardslash.chevron.right", (0.85, 0.47, 0.34)),
    "Backups for every agent": ("arrow.triangle.2.circlepath", (0.2, 0.6, 1.0)), "Connected apps": ("checkmark.circle.fill", (0.2, 0.75, 0.4)),
    "Available on this Mac": ("plus.circle.fill", (1.0, 0.6, 0.1)), "Skills": ("graduationcap.fill", (0.2, 0.6, 1.0)),
    "Memory": ("brain.head.profile", (1.0, 0.42, 0.62)), "Help": ("questionmark.circle.fill", (0.2, 0.6, 1.0)),
}


def page_for(key: str | None) -> str:
    """The page a key opens: itself, the page it was merged into, or General for a key that isn't one."""
    key = ALIASES.get(key or "", key or "")
    return key if key in {k for k, _, _ in PAGES} else "general"


class _SettingsTarget(AppKit.NSObject):
    """Action target and text delegate for every control in the window."""

    def initWithOwner_(self, owner):
        self = objc.super(_SettingsTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def changed_(self, sender):
        self.owner._changed(sender)

    def controlTextDidChange_(self, note):
        self.owner._changed(note.object())

    def controlTextDidEndEditing_(self, note):
        self.owner._ended(note.object())

    def textDidChange_(self, note):
        self.owner._changed(note.object())

    def windowWillClose_(self, note):
        self.owner._closed()

    def windowDidBecomeKey_(self, note):
        self.owner._perm_check()            # back from System Settings: show a permission just allowed


class _SettingsFlipped(AppKit.NSView):
    def isFlipped(self):
        return True


class _SettingsSideItem(AppKit.NSView):
    """One sidebar entry: an icon and a title; the chosen one sits on an accent pill."""

    def isFlipped(self):
        return True

    def mouseDown_(self, event):
        self.owner.select(self.key)


def _cgc(color, alpha: float | None = None):
    """An NSColor (system colours too) as a CGColor for a layer; NSColor.CGColor() comes back
    as a raw pointer in PyObjC."""
    import Quartz
    rgb = color.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
    return Quartz.CGColorCreateGenericRGB(rgb.redComponent(), rgb.greenComponent(), rgb.blueComponent(),
                                          rgb.alphaComponent() if alpha is None else alpha)


def _menu(popup, options):
    """Fill a pop-up with options - (value, title[, icon[, subtitle]]), see SettingsWindow._popup. -> popup."""
    for option in options:
        settings_art.add_item(popup, str(option[1]), option[2] if len(option) > 2 else None,
                              option[3] if len(option) > 3 else "")
    return popup


def _text_height(text: str, size: float, width: float) -> float:
    font = AppKit.NSFont.systemFontOfSize_(size)
    # A label's cell lays text out 2 pt in from each side (line fragment padding): measured at the full width, a
    # line that only just fits here wraps in the label and its last word was cut off (the Status badge hint).
    rect = AppKit.NSString.stringWithString_(text).boundingRectWithSize_options_attributes_(
        AppKit.NSMakeSize(max(1.0, width - 5), 10_000), AppKit.NSStringDrawingUsesLineFragmentOrigin,
        {AppKit.NSFontAttributeName: font})
    return float(rect.size.height) + 2


# --- colour: brand tiles, badges, prominent buttons ----------------------------------------------------

TONES = {"ok": "systemGreenColor", "warn": "systemOrangeColor", "bad": "systemRedColor", "info": "systemBlueColor",
         "off": "secondaryLabelColor"}


def _text_width(text: str, size: float, bold: bool = False) -> float:
    font = (AppKit.NSFont.systemFontOfSize_weight_(size, AppKit.NSFontWeightSemibold) if bold
            else AppKit.NSFont.systemFontOfSize_(size))
    return float(AppKit.NSString.stringWithString_(text).sizeWithAttributes_({AppKit.NSFontAttributeName: font}).width)


def _badge(view, text: str, right: float, mid_y: float, tone: str = "ok", max_w: float = 0.0):
    """A small coloured pill (Set ✓, Not set, Allowed…), right-aligned at `right`, centred on `mid_y`."""
    color = getattr(AppKit.NSColor, TONES.get(tone, "secondaryLabelColor"))()
    h = 20.0
    w = _text_width(text, 11, bold=True) + 20
    if max_w:
        w = min(w, max_w)
    pill = _SettingsFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(right - w, mid_y - h / 2, w, h))
    pill.setWantsLayer_(True)
    pill.layer().setCornerRadius_(h / 2)
    pill.layer().setBackgroundColor_(_cgc(color, 0.16))
    label = AppKit.NSTextField.labelWithString_(text)
    label.setFrame_(AppKit.NSMakeRect(6, 2, w - 12, 16))
    label.setFont_(AppKit.NSFont.systemFontOfSize_weight_(11, AppKit.NSFontWeightSemibold))
    label.setTextColor_(color)
    label.setAlignment_(AppKit.NSTextAlignmentCenter)
    label.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    pill.addSubview_(label)
    pill.label = label
    view.addSubview_(pill)
    return pill


def _prominent(button, color=None) -> None:
    """The main action of a row: a filled button in the accent colour (or `color`, an NSColor or rgb)."""
    if isinstance(color, tuple):
        color = _rgb(color)
    button.setBezelColor_(color or AppKit.NSColor.controlAccentColor())
    if hasattr(button, "setTintProminence_"):
        button.setTintProminence_(getattr(AppKit, "NSTintProminencePrimary", 2))


class _Page:
    """Builds one page top-down: section() opens a card, rows go in it, end() closes it."""

    def __init__(self, owner, doc, width: float) -> None:
        self.owner, self.doc, self.width = owner, doc, width
        self.y = 0.0
        self.card = None
        self.card_y = 0.0
        self.hint = None                  # the last row's hint label
        self.title_y = 0.0                # where the last section's title is

    def section(self, title: str = "", icon=None) -> None:
        """A card, with `title` above it - and a small coloured symbol before the title (`icon`: (symbol, rgb),
        or SECTION_ICONS by title; "" for none)."""
        if title:
            symbol = SECTION_ICONS.get(title) if icon is None else icon
            x = 4
            if symbol:
                image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol[0], None)
                if image is not None:
                    config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                        12, AppKit.NSFontWeightSemibold)
                    glyph = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(4, self.y + 1, 16, 16))
                    glyph.setImage_(image.imageWithSymbolConfiguration_(config))
                    glyph.setContentTintColor_(_rgb(symbol[1]))
                    self.doc.addSubview_(glyph)
                    x = 25
            label = self.owner._label(self.doc, title, x, self.y, self.width - x, h=18, size=13, bold=True)
            label.setTextColor_(AppKit.NSColor.labelColor())
            self.title_y = self.y
            self.y += 26
        card = _SettingsFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, self.y, self.width, 10))
        card.setWantsLayer_(True)
        card.layer().setCornerRadius_(10)
        card.layer().setBackgroundColor_(_cgc(AppKit.NSColor.controlBackgroundColor()))
        card.layer().setBorderColor_(_cgc(AppKit.NSColor.separatorColor()))
        card.layer().setBorderWidth_(0.5)
        self.doc.addSubview_(card)
        self.card, self.card_y = card, 0.0

    def row(self, title: str, hint: str = "", height: float = 0, control_w: float = 0, icon: str = "") -> tuple:
        """A row with `title` (and a grey `hint` under it) on the left - after a brand tile when `icon` is a
        BRANDS kind (or a picture drawn by `icon(card, x, y, size)` when it is a callable). -> (card, top of the row, x where the control goes, row height). Rows grow to fit a long
        hint; self.hint is the hint's label (a Test result can be written there)."""
        left = 16 + (ICON + 12 if icon else 0)
        label_w = self.width - left - 16 - (control_w + 16 if control_w else 0)
        hint_h = _text_height(hint, 11, label_w) if hint else 0
        height = max(height or (ROW + 8 if icon else ROW), 26 + hint_h + (14 if hint else 0))
        if self.card_y:
            line = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(16, self.card_y, self.width - 32, 1))
            line.setBoxType_(AppKit.NSBoxSeparator)
            self.card.addSubview_(line)
        top = self.card_y
        self.hint = None
        if callable(icon):                 # a picture of its own: icon(card, x, y, size)
            icon(self.card, 16, top + (height - ICON) / 2, ICON)
        elif icon:
            _icon_tile(self.card, 16, top + (height - ICON) / 2, icon)
        if title:
            title_y = top + (height - (18 + (hint_h + 2 if hint else 0))) / 2
            self.owner._label(self.card, title, left, title_y, label_w, h=18, size=13)
            if hint:
                self.hint = self.owner._label(self.card, hint, left, title_y + 19, label_w, h=hint_h, size=11,
                                              alpha=0.55, lines=0)
        self.card_y += height
        return self.card, top, self.width - 16 - control_w, height

    def text(self, text: str, size: float = 12, alpha: float = 0.75) -> object:
        """A full-width paragraph inside the card."""
        h = _text_height(text, size, self.width - 32)
        if self.card_y:
            line = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(16, self.card_y, self.width - 32, 1))
            line.setBoxType_(AppKit.NSBoxSeparator)
            self.card.addSubview_(line)
        label = self.owner._label(self.card, text, 16, self.card_y + 12, self.width - 32, h=h, size=size,
                                  alpha=alpha, lines=0)
        self.card_y += h + 24
        return label

    def end(self, note: str = "") -> None:
        frame = self.card.frame()
        self.card.setFrame_(AppKit.NSMakeRect(frame.origin.x, frame.origin.y, frame.size.width, self.card_y))
        self.y += self.card_y + 8
        if note:
            h = _text_height(note, 11, self.width - 8)
            self.owner._label(self.doc, note, 4, self.y, self.width - 8, h=h, size=11, alpha=0.55, lines=0)
            self.y += h
        self.y += 22
        self.card = None


class SettingsWindow:
    def __init__(self, actions: dict) -> None:
        """actions: callbacks by name - "train_voice", "forget_voice", "audio_status"."""
        self.actions = actions
        self.window = None
        self.page_key = "general"
        self._handlers: dict[int, callable] = {}
        self._ended_handlers: dict[int, callable] = {}      # text fields: Enter or leaving the field
        self._jobs: dict[str, dict] = {}                    # page -> its newest facts read (_facts)
        self._jobs_lock = threading.Lock()
        self._shown_parts: set[str] = set()                 # facts keys the page on screen uses (_part)
        self._anchors: dict[str, float] = {}                # name -> y of a part of the page (_anchor)
        self._reveal = ""                                   # scroll to this anchor once it is built (select)
        self._opened: set[str] = set()                      # cards opened with Set up… (Telegram, Email)
        self._perm_asked: set[str] = set()                  # permissions asked for here (permissions.ask)
        self._perm_seen = None                              # (kinds, statuses) the page on screen shows
        self._perm_busy = False                             # a permissions read is under way
        self._perm_fast_until = 0.0                         # after an Allow… click: look every second for a while
        self._perm_gen = 0                                  # one watch loop per window

    # --- building blocks -------------------------------------------------------------

    def _on(self, control, handler) -> None:
        self._handlers[objc.pyobjc_id(control)] = handler
        if hasattr(control, "setTarget_"):
            control.setTarget_(self.target)
            control.setAction_("changed:")

    def _changed(self, control) -> None:
        handler = self._handlers.get(objc.pyobjc_id(control))
        if handler is not None:
            handler(control)

    def _ended(self, control) -> None:
        handler = self._ended_handlers.get(objc.pyobjc_id(control))
        if handler is not None:
            handler(control)

    def _label(self, view, text, x, y, w, h=18, size=12, bold=False, alpha=1.0, lines=1):
        field = AppKit.NSTextField.labelWithString_(text)
        field.setFrame_(AppKit.NSMakeRect(x, y, w, h))
        field.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, AppKit.NSFontWeightSemibold) if bold
                       else AppKit.NSFont.systemFontOfSize_(size))
        field.setTextColor_(AppKit.NSColor.labelColor().colorWithAlphaComponent_(alpha))
        field.setMaximumNumberOfLines_(lines)
        field.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        view.addSubview_(field)
        return field

    def _text(self, view, key, x, y, w, placeholder="", settle: float = 0.0):
        """A text field saved as it is typed - or, with `settle`, only on Enter, on leaving the
        field, or `settle` seconds after the typing stops (the speaking style: every save
        reconnects the live session, and saving per keystroke reconnected on each letter)."""
        field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 24))
        field.setStringValue_(str(prefs.get(key) or ""))
        field.setPlaceholderString_(placeholder)
        field.setDelegate_(self.target)
        field.setBezelStyle_(AppKit.NSTextFieldRoundedBezel)

        def save(control=field):
            prefs.set(key, str(control.stringValue()).strip())
        if not settle:
            self._handlers[objc.pyobjc_id(field)] = save
        else:
            typed = [0]

            def later(control):
                typed[0] += 1
                mine = typed[0]
                AppHelper.callLater(settle, lambda: typed[0] == mine and save())
            self._handlers[objc.pyobjc_id(field)] = later
            self._ended_handlers[objc.pyobjc_id(field)] = lambda c: (typed.__setitem__(0, typed[0] + 1), save())
        view.addSubview_(field)
        return field

    def _popup(self, view, key, options, x, y, w, on_change=None):
        """A pop-up for pref `key`, drawn in the window's style (settings_art.popup). options: (value, title),
        (value, title, icon) or (value, title, icon, subtitle) - icon: an SF Symbol name, (symbol, rgb), an
        NSImage or a callable returning one; subtitle: a grey second line in the menu."""
        popup = _menu(settings_art.popup(AppKit.NSMakeRect(x, y, w, 26)), options)
        settings_art.fit(popup, w)                   # every closed title fits whole (grows leftwards)
        values = [o[0] for o in options]
        current = prefs.get(key)
        if current in values:
            popup.selectItemAtIndex_(values.index(current))

        def chosen(control):
            prefs.set(key, values[control.indexOfSelectedItem()])
            if on_change:
                on_change()
        self._on(popup, chosen)
        view.addSubview_(popup)
        return popup

    def _switch(self, view, x, y, on: bool, handler):
        switch = AppKit.NSSwitch.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, 38, 22))
        switch.setControlSize_(AppKit.NSControlSizeSmall)
        switch.setState_(AppKit.NSControlStateValueOn if on else AppKit.NSControlStateValueOff)
        self._on(switch, lambda c: handler(c.state() == AppKit.NSControlStateValueOn))
        view.addSubview_(switch)
        return switch

    def _button(self, view, title, x, y, w, handler, primary=False):
        """A push button; `primary` (True, or a colour): the row's main action, filled in the accent colour.
        `w` is a minimum: it grows to fit its title (now and when the title changes), keeping its right edge."""
        button = settings_art.push_button(title, AppKit.NSMakeRect(x, y, w, 28))
        if primary:
            _prominent(button, None if primary is True else primary)
        self._on(button, lambda c: handler())
        view.addSubview_(button)
        return button

    # Rows: a label on the left, the control right-aligned, vertically centred.

    def _row_switch(self, page, key, title, hint=""):
        card, top, x, h = page.row(title, hint, control_w=38)
        return self._switch(card, x, top + (h - 22) / 2, bool(prefs.get(key)), lambda on: prefs.set(key, on))

    def _row_slider(self, page, key, title, lo, hi, hint="", w=200, on_release=None):
        card, top, x, h = page.row(title, hint, control_w=w)
        slider = AppKit.NSSlider.sliderWithValue_minValue_maxValue_target_action_(
            float(prefs.get(key) if prefs.get(key) is not None else (lo + hi) / 2), lo, hi, None, None)
        slider.setFrame_(AppKit.NSMakeRect(x, top + (h - 22) / 2, w, 22))
        slider.setControlSize_(AppKit.NSControlSizeSmall)
        slider.setContinuous_(True)

        def moved(control):
            prefs.set(key, round(float(control.doubleValue()), 2))
            event = AppKit.NSApp.currentEvent()
            if on_release is not None and event is not None and event.type() == AppKit.NSEventTypeLeftMouseUp:
                on_release()
        self._on(slider, moved)
        card.addSubview_(slider)
        return slider

    def _sample_sound(self, name: str) -> None:
        try:
            from mint.ui import sfx
            sfx.sample(name)
        except Exception:
            log.debug("sample sound failed", exc_info=True)

    def _row_popup(self, page, key, title, options, hint="", w=250, on_change=None):
        """`w` is a minimum: the pop-up is as wide as its longest closed title needs (up to about half the row)."""
        w = settings_art.fit_width(_menu(settings_art.popup(AppKit.NSMakeRect(0, 0, w, 26)), options), w,
                                   page.width * 0.55)
        card, top, x, h = page.row(title, hint, control_w=w)
        return self._popup(card, key, options, x, top + (h - 26) / 2, w, on_change)

    def _row_text(self, page, key, title, placeholder="", hint="", w=250, settle=0.0):
        card, top, x, h = page.row(title, hint, control_w=w)
        return self._text(card, key, x, top + (h - 24) / 2, w, placeholder, settle)

    def _row_buttons(self, page, title, buttons, hint="", icon=""):
        """buttons: [(title, width, handler)] or [(title, width, handler, primary)], right-aligned in order. A width
        is a minimum: each button is as wide as its title needs."""
        buttons = [(words, settings_art.button_width(words, w), handler, *style)
                   for words, w, handler, *style in buttons]
        total = sum(b[1] for b in buttons) + 8 * (len(buttons) - 1)
        card, top, x, h = page.row(title, hint, control_w=total, icon=icon)
        made = []
        for words, w, handler, *style in buttons:
            made.append(self._button(card, words, x, top + (h - 28) / 2, w, handler, primary=bool(style and style[0])))
            x += w + 8
        return made

    def _row_badge(self, page, title, words, tone="ok", hint="", icon="", w=150):
        """A row whose right side is a status badge (Added ✓, Allowed…)."""
        card, top, x, h = page.row(title, hint, control_w=w, icon=icon)
        return _badge(card, words, x + w, top + h / 2, tone, max_w=w)

    def _key_row(self, page, title, hint, value, on_add, icon="", test=None, words=None):
        """A key or a secret: a badge (Set ••••1234 ✓ / Not set), Add key… (the prominent main action) or
        Change…, and optionally Test. `value`: the key (only its last four characters are shown) or a bool.
        -> the Test button (or None)."""
        badge_w, add_w, test_w = 132, 100, 64
        control_w = badge_w + 10 + add_w + (8 + test_w if test else 0)
        card, top, x, h = page.row(title, hint, control_w=control_w, icon=icon)
        is_set = bool(value)
        if words is None:
            words = (f"Set ••••{value[-4:]}" if isinstance(value, str) and len(value) > 8 else "Set") if is_set \
                else "Not set"
        _badge(card, words + (" ✓" if is_set else ""), x + badge_w, top + h / 2, "ok" if is_set else "warn",
               max_w=badge_w)
        bx = x + badge_w + 10
        self._button(card, "Change…" if is_set else "Add key…", bx, top + (h - 28) / 2, add_w, on_add,
                     primary=not is_set)
        if not test:
            return None
        button = self._button(card, "Test", bx + add_w + 8, top + (h - 28) / 2, test_w, test)
        button.setEnabled_(is_set)
        return button

    def _row_value(self, page, title, value, hint="", w=280):
        card, top, x, h = page.row(title, hint, control_w=w)
        label = self._label(card, value, x, top + (h - 18) / 2, w, size=12, alpha=0.7)
        label.setAlignment_(AppKit.NSTextAlignmentRight)
        label.setLineBreakMode_(AppKit.NSLineBreakByTruncatingMiddle)
        return label

    # Basic and advanced: one pref for the whole window.

    def _advanced(self) -> bool:
        """Show the technical rows too (General ▸ Show advanced settings, or Show more on a page)."""
        return bool(prefs.get("settings_advanced"))

    def _set_advanced(self, on: bool) -> None:
        prefs.set("settings_advanced", bool(on))
        # After the click has finished: the rebuild replaces the control that was clicked.
        AppHelper.callAfter(lambda: self.refresh(keep_scroll=True))

    def _more_section(self, page, what: str) -> None:
        """The last line of a page that has advanced rows: what "All settings" adds, and a link that shows it
        (the Essentials | All settings switch at the top of the page does the same)."""
        on = self._advanced()
        self._note_link(page, "Showing all settings on every page." if on else f"More settings: {what}",
                        "Show essentials only" if on else "Show all settings", lambda: self._set_advanced(not on))

    def _note_link(self, page, words: str, title: str, handler) -> None:
        """A small grey line between cards, with a link at its right."""
        link = self._link(page.doc, title, page.width - 16, page.y - 1, handler, max_w=page.width / 2)
        text_w = link.frame().origin.x - 4 - 12
        h = _text_height(words, 11, text_w)
        self._label(page.doc, words, 4, page.y, text_w, h=h, size=11, alpha=0.5, lines=0)
        page.y += max(h, 18) + 18

    def _link(self, view, title, right, y, handler, max_w: float = 240):
        """A borderless button in the accent colour, like a link: as wide as its words (at most `max_w`, the title
        cut with "…" beyond that), its right edge at `right`. No trailing dots on the words ("All permissions")."""
        title = settings_art.plain_title(title)
        button = AppKit.NSButton.buttonWithTitle_target_action_(title, None, None)
        button.setBordered_(False)
        button.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_attributes_(title, {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(12, AppKit.NSFontWeightSemibold),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.controlAccentColor()}))
        button.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        button.sizeToFit()
        w = min(float(button.frame().size.width) + 2, max_w)
        button.setFrame_(AppKit.NSMakeRect(right - w, y, w, 18))
        self._on(button, lambda c: handler())
        view.addSubview_(button)
        return button

    def _anchor(self, page, name: str) -> None:
        """Remember where `name` starts on this page, for select(page, anchor=name) (a connector's Settings…, the
        setup guide) - a section, or a row inside the card being built."""
        self._anchors[name] = page.y + (page.card_y if page.card is not None else 0.0)

    def _open_card(self, name: str) -> None:
        self._opened.add(name)
        self._reveal = name
        self.refresh(keep_scroll=True)

    def showing(self, key: str) -> bool:
        """Is the window open on `key`'s page (old keys count for the page they are part of now)?"""
        return self.window is not None and self.page_key == page_for(key)

    def _part(self, page, key: str, build, title: str = "") -> None:
        """Part of a page that reads its own facts (`_facts_<key>`, off the main thread), so the rest of the page
        does not wait for it: "Loading…" in its place until they arrive, then the page is built again."""
        self._shown_parts.add(key)
        facts = self._facts(key)
        if facts is None:
            self._loading_card(page, key, title)
            return
        ok, value = facts
        if not ok:
            self._problem_card(page, key, value)
            return
        build(page, value)

    # Permissions (permissions.py): read with the page's facts, and again every few seconds while shown.

    def _needs_ok_section(self, page, statuses: dict) -> None:
        """General, at the top: the important permissions still missing - what stops working without each, and
        Allow…. Nothing at all when they are allowed."""
        from mint.core import permissions
        self._perm_seen = ("important", dict(statuses))
        missing = permissions.missing_important(statuses)
        if not missing:
            return
        name = prefs.name()
        page.section("Needs your OK")
        self._link(page.doc, "All permissions…", page.width - 16, page.title_y, lambda: self.select("storage",
                   anchor="permissions"), max_w=page.width / 3)
        page.card.layer().setBorderColor_(_cgc(AppKit.NSColor.systemOrangeColor(), 0.6))
        page.card.layer().setBorderWidth_(1.0)
        for kind in missing:
            self._perm_row(page, kind, statuses.get(kind, "ask"), permissions.LOSES[kind].format(name=name),
                           badge=False)
        page.end()

    def _permissions_section(self, page, statuses: dict) -> None:
        """Permissions & Privacy: every permission, Allowed or Not allowed, with Allow… for the missing ones."""
        from mint.core import permissions
        self._perm_seen = ("all", dict(statuses))
        name = prefs.name()
        allowed = sum(1 for kind, *_ in permissions.ALL if statuses.get(kind) == "allowed")
        self._anchor(page, "permissions")
        page.section("Permissions")
        page.text(f"What macOS lets {name} do - {allowed} of {len(permissions.ALL)} allowed. Allow… asks macOS, or "
                  f"opens the place in System Settings where you switch {name} on.", size=12, alpha=0.7)
        for kind, *_ in permissions.ALL:
            status = statuses.get(kind, "ask")
            hint = (permissions.FOR if status == "allowed" else permissions.LOSES)[kind].format(name=name)
            self._perm_row(page, kind, status, hint)
        page.end(f"macOS keeps these in System Settings ▸ Privacy & Security. After allowing Screen Recording or "
                 f"Input Monitoring, {name} may need a restart (General ▸ Restart).")

    def _perm_row(self, page, kind: str, status: str, hint: str, badge: bool = True) -> None:
        """One permission: its tile, what it is for (or what stops working), a badge, and Allow… / Open
        Settings… while it is missing."""
        from mint.core import permissions
        badge_w, button_w = (112 if badge else 0), 132
        card, top, x, h = page.row(permissions.title(kind), hint, control_w=badge_w + (10 if badge else 0) + button_w,
                                   icon=f"perm:{kind}")
        if badge:
            words, tone = (("Allowed ✓", "ok") if status == "allowed" else
                           ("Not allowed", "bad" if kind == "microphone" else "warn"))
            _badge(card, words, x + badge_w, top + h / 2, tone, max_w=badge_w)
        if status != "allowed":
            title = "Allow…" if status == "ask" else "Open Settings…"
            w = 92 if status == "ask" else button_w
            self._button(card, title, x + badge_w + (10 if badge else 0) + button_w - w, top + (h - 28) / 2, w,
                         lambda: self._allow_permission(kind), primary=True)

    def _allow_permission(self, kind: str) -> None:
        """Allow…: macOS's prompt (or the right pane of System Settings); then look again every second for a
        minute, so the row turns to Allowed as soon as it is."""
        from mint.core import permissions
        permissions.ask(kind, self._perm_asked)
        self._perm_fast_until = time.monotonic() + 60
        AppHelper.callLater(1.0, self._perm_check)

    def _perm_check(self) -> None:
        """Read the permissions again, off the main thread, while a page that shows them is on screen; rebuild the
        page only when one changed."""
        seen = self._perm_seen
        if (self.window is None or self._perm_busy or seen is None or PERM_PAGES.get(self.page_key) != seen[0]
                or not self.window.isVisible()):
            return
        from mint.core import permissions
        kinds = permissions.IMPORTANT if seen[0] == "important" else None
        asked = set(self._perm_asked)
        self._perm_busy = True

        def done(now) -> None:
            self._perm_busy = False
            if isinstance(now, dict) and self._perm_seen is seen and now != seen[1]:
                self.refresh(keep_scroll=True)
        self._background("permissions-watch", lambda: permissions.snapshot(asked, kinds), done)

    def _perm_watch(self, gen: int) -> None:
        if gen != self._perm_gen or self.window is None:
            return
        self._perm_check()
        AppHelper.callLater(1.0 if time.monotonic() < self._perm_fast_until else PERM_EVERY, self._perm_watch, gen)

    # --- pages -----------------------------------------------------------------------

    def _facts_general(self) -> dict:
        from mint.core import permissions
        from mint.app import power
        return {"login": power.starts_at_login(), "can_restart": power.can_restart(),
                "perms": permissions.snapshot(set(self._perm_asked), permissions.IMPORTANT)}

    def _page_general(self, page, facts: dict) -> None:
        from mint.app import power
        name = prefs.name()
        if facts.get("perms") is not None:
            self._needs_ok_section(page, facts["perms"])
        page.section("Assistant")
        self._row_text(page, "assistant_name", "Name", "Mint",
                       hint=f"You wake it with “Hey {name}”. A new name gets its own wake word (about a minute).")
        self._personality_row(page, name)
        self._row_text(page, "user_name", "Your name", "What should it call you?")
        card, top, _, _ = page.row("About you", "Your work, people and projects, how you like answers. Given to "
                                   f"{name} at the start of every conversation.", height=150, control_w=300)
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(page.width - 316, top + 14, 300, 122))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(AppKit.NSBezelBorder)
        about = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 290, 122))
        about.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        about.setTextContainerInset_(AppKit.NSMakeSize(4, 6))
        about.setString_(str(prefs.get("about_me") or ""))
        about.setDelegate_(self.target)
        self._handlers[objc.pyobjc_id(about)] = lambda c: prefs.set("about_me", str(c.string()).strip())
        scroll.setDocumentView_(about)
        card.addSubview_(scroll)
        page.end()

        page.section(f"Getting to know {name}", icon=("sparkles", (1.0, 0.62, 0.04)))
        self._row_buttons(page, f"What {name} can do",
                          [("Show me", 110, self._show_tour, True)],
                          "Each thing it can do, with what to say.")
        self._row_buttons(page, "The whole setup again", [("Welcome tour…", 130, self._show_setup)],
                          "Name, voice, permissions, shortcuts and the clips - your answers are kept.")
        page.end()

        advanced = self._advanced()
        page.section("Behaviour")
        card, top, x, h = page.row(f"Start {name} when I log in", control_w=38)
        self._switch(card, x, top + (h - 22) / 2, facts["login"],
                     lambda on: self._background("login", lambda: power.set_start_at_login(on)))
        self._row_switch(page, "listen_while_working", "Keep listening while it works",
                         "Say “stop” any time to cut it off.")
        if advanced:
            self._row_switch(page, "share_visible", "Visible in screen sharing",
                             "Shows in Meet, Zoom and screenshots. Off hides Mint from them (the eye button does "
                             "the same).")
            self._row_popup(page, "follow_up_seconds", "Keep listening after a reply", FOLLOW_UP,
                            f"Then it waits for “{__import__('mint.voice.wake', fromlist=['x']).display_phrase()}” again. "
                            "Talk not meant for Mint never keeps it open.", w=230)
            self._row_popup(page, "guard", "Ask before deleting or changing", GUARD_LEVELS,
                            "Mint (and its helpers, and Claude Code when connected) explains and waits for your "
                            "yes - at the Mac or on Telegram.", w=300)
            self._row_popup(page, "unload_after_minutes", "Free memory when asleep", UNLOAD, w=220,
                            hint="Unloads (~270 MB freed); a small listener (~35 MB) keeps the orb, ⌘J and the "
                                 "wake word. Waking takes about a second longer.")
        page.end()

        page.section()
        if facts["can_restart"]:
            self._row_buttons(page, f"Restart {name}", [(f"Restart {name}", 130, lambda: self._restart())],
                              hint="Stops and starts it again - when something seems stuck. The conversation is kept.")
        self._row_buttons(page, f"Quit {name}", [(f"Quit {name}", 130, lambda: power.quit_fully(reason="Settings"))],
                          hint="Stops everything it started too.")
        page.end()

        page.section("Settings window")
        card, top, x, h = page.row("Show advanced settings",
                                   "Technical settings on every page: your own wake phrase, echo cancellation, "
                                   "model providers, follow-ups, safety levels… Off: just the essentials.",
                                   control_w=38)
        self._switch(card, x, top + (h - 22) / 2, advanced, self._set_advanced)
        page.end()

    def _facts_voice(self) -> dict:
        """The wake word and the voice lock, CoreAudio's devices and who records now, and the engine's state."""
        from mint.voice import wake
        from mint.voice import wake_train  # noqa: F401 - imported here, off the main thread
        lock = voicelock.lock
        status = self.actions.get("audio_status")
        return {"active": wake.active_phrases(), "stale": bool(lock.stale), "enrolled": bool(lock.enrolled),
                "enrolled_at": str(lock.enrolled_at or ""),
                "inputs": self._devices(False), "outputs": self._devices(True), "mic_users": audio_devices.mic_users(),
                "status": status() if status else ""}

    def _page_voice(self, page, facts: dict) -> None:
        """Microphone & voice: one test, one training, the wake word, the devices, and calls."""
        name = prefs.name()
        active = facts.get("active") or [f"Hey {name}"]
        advanced = self._advanced()
        self._mic_test_section(page, active[0])

        if facts["stale"]:
            status = "Needs training again (the voice model was upgraded)"
        elif facts["enrolled"]:
            status = f"Trained {facts['enrolled_at']}".rstrip()
        else:
            status = "Not trained yet"
        self._anchor(page, "voice")                  # the setup guide's "train my voice"
        page.section("Your voice")
        trained = facts["enrolled"] or facts["stale"]
        self._row_buttons(page, status, [("Retrain my voice…" if trained else "Train my voice…", 150,
                                          lambda: self._act("train_voice"), not trained)],
                          hint=f"About two minutes in a quiet room: say “{active[0]}” eight times, then read eight "
                               f"short sentences. {name} then wakes more surely for you, and the voice lock knows "
                               "you.")
        self._row_switch(page, "voice_lock", "Voice lock",
                         f"Only your voice reaches {name}." + ("" if trained else " Train your voice first."))
        if advanced:
            self._row_popup(page, "lock_strictness", "Voice lock strictness", STRICTNESS, w=220,
                            hint="Misses you often: train again where you sit, or choose Relaxed. Someone else gets "
                                 "through: choose Strict.")
            self._row_switch(page, "addressee_check", "Ignore talk meant for others",
                             f"Things you say to other people, not to {name}.")
            if facts["enrolled"]:
                self._row_buttons(page, "Forget my voice", [("Forget", 90, lambda: self._act("forget_voice"))],
                                  hint="Deletes your voiceprint. Anyone's voice can wake it again.")
        page.end()

        page.section("Wake word")
        self._row_value(page, "Listening for", " · ".join(f"“{p}”" for p in active), w=300)
        self._row_popup(page, "wake_sensitivity", "Sensitivity", WAKE_SENSITIVITY, w=220,
                        hint="Misses you often: choose High. Wakes up on its own: choose Low.")
        page.end()
        if advanced:
            self._wake_section(page, active)

        self._devices_section(page, facts)
        self._calls_section(page, facts)
        self._more_section(page, "Your own wake phrase, voice lock strictness, echo cancellation, and the apps "
                                 "Mint makes way for on calls.")

    def _mic_test_section(self, page, phrase: str) -> None:
        """Is Mint hearing me? A live level, "Hey Mint" caught or not, and what is in the way (mictest.py)."""
        name = prefs.name()
        page.section("Microphone test")
        card, top, x, h = page.row(f"Is {name} hearing me?", f"Press Test microphone and say “{phrase}”. It checks "
                                   "the microphone, the wake word and - if it's on - the voice lock.", control_w=318)
        meter = AppKit.NSLevelIndicator.alloc().initWithFrame_(AppKit.NSMakeRect(x, top + (h - 16) / 2, 160, 16))
        meter.setLevelIndicatorStyle_(AppKit.NSLevelIndicatorStyleContinuousCapacity)
        meter.setMinValue_(0)
        meter.setMaxValue_(1)
        meter.setWarningValue_(0.85)
        meter.setCriticalValue_(0.97)
        card.addSubview_(meter)
        # Room for a few findings (the label does not grow after the page is laid out).
        results = page.text("Not tested yet.\n \n \n \n ", size=12, alpha=0.8)

        def level(value, score, need):
            AppHelper.callAfter(lambda: (meter.setDoubleValue_(value),
                                         results.setStringValue_(f"Listening… say “{phrase}”   "
                                                                 f"(wake word {score:.2f} / {need:.2f})")))

        def mic_on_then_test():
            turn_on = self.actions.get("mic_on")
            if turn_on:
                turn_on()
            test()

        def done(findings, facts):
            def show():
                meter.setDoubleValue_(0)
                marks = {"ok": "✓", "problem": "✗", "tip": "•"}
                results.setStringValue_("\n".join(f"{marks.get(kind, '•')} {words}" for kind, words in findings)
                                        or "No problems found.")
                button.setEnabled_(True)
                if not facts.get("mic_on", True):          # the microphone is switched off in Mint
                    button.setTitle_("Turn mic on")
                    self._on(button, lambda c: mic_on_then_test())
                else:
                    button.setTitle_("Test again")
                    self._on(button, lambda c: test())
            AppHelper.callAfter(show)

        def test():
            run = self.actions.get("mic_test")
            if run is None:
                results.setStringValue_(f"{name} isn't running - start it and test again.")
                return
            button.setEnabled_(False)
            results.setStringValue_(f"Listening… say “{phrase}”")
            run(level, done)
        button = self._button(card, "Test microphone", x + 168, top + (h - 28) / 2, 150, test, primary=True)
        page.end()

    def _wake_section(self, page, active: list[str]) -> None:
        """Advanced: a wake phrase of the user's own ("Hey Jarvis"), trained on this Mac in about a minute and a
        half. Separate from Train my voice, which teaches the current wake word the user's voice."""
        from mint.voice import wake_train
        state = self.__dict__.setdefault("_wake_state", {"text": "", "takes": [], "busy": False, "progress": 0.0})
        page.section("Use a different wake phrase")
        page.text(f"Wake {prefs.name()} with a phrase of your own instead of “{active[0]}”. This is not Train my "
                  "voice: it builds a new wake word on this Mac (about a minute and a half).", size=12, alpha=0.7)
        card, top, x, h = page.row("New wake phrase", "Two or three words, like “Hey Jarvis” or “Okay Nova”.",
                                   control_w=250)
        field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(x, top + (h - 24) / 2, 250, 24))
        field.setBezelStyle_(AppKit.NSTextFieldRoundedBezel)
        field.setPlaceholderString_(active[0])
        field.setStringValue_(state.get("phrase", ""))
        field.setDelegate_(self.target)
        card.addSubview_(field)

        def typed(control):
            state["phrase"] = str(control.stringValue()).strip()
            advice = wake_train.check_phrase(state["phrase"]) if state["phrase"] else None
            status.setStringValue_(advice or state["text"] or " ")
        self._handlers[objc.pyobjc_id(field)] = typed

        def say(text: str) -> None:
            state["text"] = text
            AppHelper.callAfter(lambda: self._wake_status.setStringValue_(text))

        def record() -> None:
            phrase = state.get("phrase") or active[0]
            if state["busy"]:
                return
            state["busy"] = True

            def on_take(i, n, stage):
                say(f"Say “{phrase}” now ({i + 1} of {n})…" if stage == "say" else f"Got take {i + 1} of {n}.")

            def run():
                try:
                    state["takes"] = wake_train.record_samples(4, 2.5, on_take)
                    say(f"Recorded 4 takes of “{phrase}”. Now press Train this phrase.")
                finally:
                    state["busy"] = False
            threading.Thread(target=run, daemon=True, name="wake-takes").start()

        def train() -> None:
            phrase = state.get("phrase", "")
            advice = wake_train.check_phrase(phrase) if phrase else "Type the new wake phrase first."
            if advice or state["busy"]:
                say(advice or "Busy - one moment.")
                return
            state["busy"] = True

            def progress(fraction, message):
                state["progress"] = float(fraction)
                AppHelper.callAfter(lambda: self._wake_bar.setDoubleValue_(100 * float(fraction)))
                say(f"Training “{phrase}”: {message}")

            def done(path, problem):
                state["busy"] = False
                state["progress"] = 0.0
                if problem:
                    say(problem)
                    return
                report = wake_train.report(phrase)
                prefs.set("wake_phrase", phrase)
                say(f"Done: “{phrase}” now wakes {prefs.name()} (it caught {round(100 * report.get('accept_held_out', 0))}% "
                    "of voices it never heard). If the voice lock is on, use Retrain my voice above so it knows the "
                    "new phrase.")
                AppHelper.callAfter(self.refresh)
            wake_train.start(phrase, state["takes"] or None, progress, done)

        def test() -> None:
            """Say the phrase typed above (or, with none typed, the current wake word) and see if it is caught."""
            if state["busy"]:
                return
            state["busy"] = True
            typed_phrase = state.get("phrase", "")

            def run():
                phrase = typed_phrase or active[0]
                try:
                    if (typed_phrase and typed_phrase.lower() not in (p.lower() for p in active)
                            and not wake_train.model_path(typed_phrase).exists()):
                        say(f"“{typed_phrase}” isn't trained yet - press Train this phrase first.")
                        return
                    take = wake_train.record_samples(1, 3.0, lambda i, n, stage: say(
                        f"Say “{phrase}” now…" if stage == "say" else "Checking…"))[0]
                    got, need = wake_train.score(take, phrase)
                    say(f"Heard “{phrase}” (score {got:.2f}, needs {need:.2f})." if got >= need else
                        f"Didn't catch “{phrase}” (score {got:.2f}, needs {need:.2f}). Try closer, or record 4 takes "
                        "and train it again.")
                except Exception as error:
                    say(f"Couldn't test: {error}")
                finally:
                    state["busy"] = False
            threading.Thread(target=run, daemon=True, name="wake-test").start()

        self._row_buttons(page, "Record yourself saying it (optional)", [("Record 4 takes", 130, record)],
                          hint="Makes the new phrase surer for your voice and accent. About 15 seconds.")
        card, top, x, h = page.row("", height=ROW, control_w=0)
        train_x = page.width - 16 - 140
        test_x = train_x - 8 - 130
        bar = AppKit.NSProgressIndicator.alloc().initWithFrame_(AppKit.NSMakeRect(16, top + (h - 12) / 2,
                                                                                   test_x - 12 - 16, 12))
        bar.setIndeterminate_(False)
        bar.setMinValue_(0)
        bar.setMaxValue_(100)
        bar.setDoubleValue_(100 * state["progress"])
        card.addSubview_(bar)
        self._wake_bar = bar
        self._button(card, "Test this phrase", test_x, top + (h - 28) / 2, 130, test)
        self._button(card, "Train this phrase", train_x, top + (h - 28) / 2, 140, train)
        status = page.text(state["text"] or "Type a phrase, optionally record 4 takes, then Train this phrase.\n ",
                           size=11, alpha=0.65)
        self._wake_status = status
        if str(prefs.get("wake_phrase") or "").strip():
            card, top, x, h = page.row("Also wake on “Hey Mint”", control_w=38)
            extra = prefs.get("wake_models") or []
            self._switch(card, x, top + (h - 22) / 2, "Hey Mint" in extra,
                         lambda on: prefs.set("wake_models", ["Hey Mint"] if on else []))
            self._row_buttons(page, f"Back to “Hey {prefs.name()}” only", [("Reset", 90, lambda: (
                prefs.set("wake_phrase", ""), prefs.set("wake_models", []), self.refresh()))])
        page.end("Trained here from many built-in voices plus your takes. Nothing leaves your Mac.")

    def _personality_row(self, page, name: str) -> None:
        """How the assistant comes across: a short line in the user's words, or a preset."""
        from mint.core.custom import PERSONALITY_LIMIT
        card, top, x, h = page.row("Personality", f"How {name} speaks and behaves - funny, calm, argumentative… "
                                   f"Up to {PERSONALITY_LIMIT} characters; empty is the default.", control_w=350)
        field = self._text(card, "personality", x, top + (h - 24) / 2, 250, "e.g. funny and a bit sarcastic",
                           settle=3.0)
        count = self._label(card, "", x, top + (h - 24) / 2 + 26, 250, h=14, size=10, alpha=0.5)
        count.setAlignment_(AppKit.NSTextAlignmentRight)
        save_later = self._handlers[objc.pyobjc_id(field)]

        def typed(control) -> None:
            value = str(control.stringValue())
            if len(value) > PERSONALITY_LIMIT:
                value = value[:PERSONALITY_LIMIT]
                control.setStringValue_(value)
                AppKit.NSBeep()
            count.setStringValue_(f"{len(value)}/{PERSONALITY_LIMIT}")
            save_later(control)
        self._handlers[objc.pyobjc_id(field)] = typed
        count.setStringValue_(f"{len(str(field.stringValue()))}/{PERSONALITY_LIMIT}")

        presets = _menu(settings_art.popup(AppKit.NSMakeRect(x + 258, top + (h - 26) / 2, 92, 26), True),
                        [(None, "Presets")] + PERSONALITIES)
        card.addSubview_(presets)

        def preset(control) -> None:
            value = PERSONALITIES[control.indexOfSelectedItem() - 1][0]
            field.setStringValue_(value)
            count.setStringValue_(f"{len(value)}/{PERSONALITY_LIMIT}")
            prefs.set("personality", value)
        self._on(presets, preset)

    def _facts_speaking(self) -> dict:
        from mint.core import config
        catalog = voices.catalog()
        names = {v for v, _, _ in catalog} | {str(prefs.get("voice_name") or config.VOICE)}
        return {"catalog": catalog, "labels": {v: voices.label(v) for v in names}, "default": config.VOICE}

    def _page_speaking(self, page, facts: dict) -> None:
        name = prefs.name()
        page.section("Voice")
        card, top, x, h = page.row("Show", control_w=250)
        shown = _menu(settings_art.popup(AppKit.NSMakeRect(x, top + (h - 26) / 2, 250, 26)), VOICE_FILTERS)
        settings_art.fit(shown)
        card.addSubview_(shown)
        card, top, x, h = page.row("Voice", "Previews take a few seconds the first time.", control_w=350)
        picker = settings_art.popup(AppKit.NSMakeRect(x, top + (h - 26) / 2, 250, 26))
        card.addSubview_(picker)
        listed: list[str] = []

        def fill(kind: str) -> None:
            listed[:] = [v for v, g, _ in facts["catalog"] if kind == "all" or g == kind]
            chosen = prefs.get("voice_name") or facts["default"]
            if chosen not in listed:                     # keep the current one visible
                listed.insert(0, chosen)
            picker.removeAllItems()
            genders = {v: (g, t) for v, g, t in facts["catalog"]}
            for voice in listed:                         # an avatar, then name · female/male · its character
                gender, tone = genders.get(voice, ("", ""))
                settings_art.add_item(picker, facts["labels"].get(voice, voice),
                                      functools.partial(settings_art.avatar, voice),
                                      attributed=settings_art.voice_title(voice, gender, tone) if gender or tone
                                      else None)
            picker.selectItemAtIndex_(listed.index(chosen))
            settings_art.fit(picker)

        fill("all")
        self._on(shown, lambda c: fill(VOICE_FILTERS[c.indexOfSelectedItem()][0]))

        def picked(control) -> None:
            prefs.set("voice_name", listed[control.indexOfSelectedItem()])
            fill(VOICE_FILTERS[shown.indexOfSelectedItem()][0])
            status.setStringValue_(f"Switching to {prefs.get('voice_name')} - it applies once {name} finishes "
                                   "speaking.")
        self._on(picker, picked)

        def listen() -> None:
            voice = listed[picker.indexOfSelectedItem()]
            status.setStringValue_(f"Getting a sample of {voice}…")
            button.setEnabled_(False)

            def done(error) -> None:
                button.setEnabled_(True)
                status.setStringValue_(f"Could not play a sample: {error[:120]}" if error else f"Playing {voice}.")
            voices.preview(voice, str(style.stringValue()).strip(), done)
        button = self._button(card, "▶ Listen", x + 258, top + (h - 28) / 2, 92, listen)

        card, top, x, h = page.row("Style", "Pace, mood, accent - for every voice.", control_w=350)
        style = self._text(card, "speaking_style", x, top + (h - 24) / 2, 250, "e.g. warm and calm", settle=3.0)
        presets = _menu(settings_art.popup(AppKit.NSMakeRect(x + 258, top + (h - 26) / 2, 92, 26), True),
                        [(None, "Presets")] + STYLES)
        card.addSubview_(presets)

        def preset(control) -> None:
            value = STYLES[control.indexOfSelectedItem() - 1][0]
            style.setStringValue_(value)
            prefs.set("speaking_style", value)
        self._on(presets, preset)
        status = page.text("", size=11, alpha=0.6)
        status.setStringValue_("You can also say: “use a male voice”, “switch to Puck”, “talk slower”.")
        page.end()

        page.section("Language")
        self._row_popup(page, "reply_language", "Answer in", LANGUAGES, w=250,
                        hint=f"Automatic answers in the language you speak. Pick one to make {name} always speak "
                             "and write in it.")
        page.end("The Settings window itself is in English for now.")

        page.section("Replies")
        self._row_switch(page, "voice", "Spoken replies", "Off: replies appear as text only.")
        self._row_switch(page, "word_animation", "Word-by-word captions")
        page.end()

    def _devices(self, output: bool) -> list[tuple]:
        """[(uid, name, (symbol, rgb), transport)] - the icon (mic, speaker, headphones, AirPods, display…) is
        drawn later, on the main thread, when the menu is built."""
        found = [("", "System default", settings_art.device_spec("", "", output),
                  "Follows macOS Sound settings")]
        for d in audio_devices.devices():
            if (d["outputs"] if output else d["inputs"]) and d["transport"] != "aggregate":
                transport = str(d["transport"])
                found.append((d["uid"], d["name"], settings_art.device_spec(d["name"], transport, output),
                              transport[:1].upper() + transport[1:]))
        return found

    def _devices_section(self, page, facts: dict) -> None:
        """Microphone & voice ▸ Microphone and speaker. `facts` from _facts_voice (CoreAudio, read off the main
        thread)."""
        page.section("Microphone and speaker")
        self._row_popup(page, "input_device", "Microphone", facts["inputs"], w=300)
        self._row_popup(page, "output_device", "Speaker", facts["outputs"], w=300)
        if self._advanced():
            self._row_popup(page, "echo_cancellation", "Echo cancellation", ECHO, w=200,
                            hint="Automatic: off with headphones, on with speakers.")
        page.end("Changes apply at once; the microphone restarts in about a second.")

    def _calls_section(self, page, facts: dict) -> None:
        """Microphone & voice ▸ Calls and meetings: one choice for the two prefs it is made of - share_mic (make
        way for a call at all) and listen_in_calls (while making way, still listen for the wake word)."""
        name = prefs.name()
        phrase = (facts.get("active") or [f"Hey {name}"])[0]
        page.section("Calls and meetings")
        current = ("nothing" if not prefs.get("share_mic") else
                   "stop" if prefs.get("listen_in_calls") is False else "listen")
        calls_w = settings_art.fit_width(_menu(settings_art.popup(AppKit.NSMakeRect(0, 0, 270, 26)), CALLS), 270)
        card, top, x, h = page.row("During calls",
                                   f"Meet, Zoom, FaceTime… Keep listening: {name} still hears “{phrase}” without "
                                   "disturbing the call. Stop: it lets go of the microphone until the call ends.",
                                   control_w=calls_w)
        popup = _menu(settings_art.popup(AppKit.NSMakeRect(x, top + (h - 26) / 2, calls_w, 26)), CALLS)
        popup.selectItemAtIndex_([c[0] for c in CALLS].index(current))

        def chosen(control) -> None:
            value = CALLS[control.indexOfSelectedItem()][0]
            if value == "nothing":
                prefs.set("share_mic", False)
            else:
                prefs.set("listen_in_calls", value == "listen")
                prefs.set("share_mic", True)
        self._on(popup, chosen)
        card.addSubview_(popup)
        if self._advanced():
            users = facts["mic_users"]
            self._seen = users
            self.mic_users = page.text("Using the microphone now: " + ", ".join(sorted({u["name"] for u in users}))
                                       if users else "No other app is using the microphone right now.",
                                       size=11, alpha=0.65)
            self._row_buttons(page, "Other call apps", [("Refresh", 90, self.refresh),
                                                        ("Also make way for these", 190, self._add_mic_apps)],
                              hint="Common call, meeting and recording apps are known. For another one: start it, "
                                   "Refresh, then add it.")
            if facts["status"]:
                page.text(f"Now: {facts['status']}", size=11, alpha=0.65)
        page.end()

    def _add_mic_apps(self) -> None:
        extra = list(prefs.get("share_mic_apps") or [])
        for user in getattr(self, "_seen", []):
            if user["bundle"] and user["bundle"] not in extra:
                extra.append(user["bundle"])
        prefs.set("share_mic_apps", extra)
        self.mic_users.setStringValue_("Will also make way for: " + ", ".join(extra) if extra else
                                       "No other app is using the microphone right now.")

    def _facts_looks(self) -> dict:
        mcp = {}
        try:
            from mint.tools import agent_mcp
            for app in ("claude", "codex"):
                mcp[app] = None if not agent_mcp.available(app) else bool(agent_mcp.connected(app))
        except Exception:
            mcp = {"claude": None, "codex": None}
        try:
            from mint.tools import agent_checks
            from mint.tools import agent_watch
            usage = agent_checks.limits_line()
            limits = agent_watch.limits()            # files: read here, off the main thread
        except Exception:
            usage, limits = "", None
        try:
            from mint.tools import agent_watch
            here = any(agent_watch.installed().values()) or prefs.is_set("agent_mode")
        except Exception:
            here = True
        return {"hooks": self._hooks_title(), "limits": self._limits_title(), "mcp": mcp, "usage": usage,
                "limits_data": limits, "agents_here": here}

    def _page_looks(self, page, facts: dict) -> None:
        """Appearance & Sound. The few things most people change; the rest with advanced settings."""
        name = prefs.name()
        advanced = self._advanced()
        page.section("View")
        self._row_popup(page, "notch_mode", f"Where {name} lives", WHERE,
                        "Switching plays live: the orb flies into the notch, or drops out of it.", w=200)
        self._row_popup(page, "theme", "Theme", THEMES, w=200)
        if not prefs.get("notch_mode"):
            self._row_popup(page, "position", "Orb position", POSITIONS, w=200)
        self._row_popup(page, "motion", "How much it moves", MOTION,
                        "Calm: gentle, no bounce. Minimal: fades only (as with macOS Reduce motion).", w=200)
        if prefs.get("notch_mode"):
            self._row_popup(page, "notch_open_after", "Open the full notch after", NOTCH_OPEN_AFTER,
                            "Resting on the small row of controls (a click opens it at once).", w=200)
            self._row_popup(page, "notch_close_after", "Fold it after you move away", NOTCH_CLOSE_AFTER,
                            "A thin line along its bottom shows the time left.", w=200)
        if advanced:
            if prefs.get("notch_mode"):
                self._row_popup(page, "position", "Orb position", POSITIONS, "When it isn't in the notch.", w=200)
            self._row_popup(page, "notch_open_to", "The open notch shows", OPEN_TO,
                            "Auto: Home, or your coding agents while they work.", w=200)
            self._row_switch(page, "face", "Face on the orb")
            self._row_switch(page, "menubar_face", "Face in the menu bar",
                             "The menu bar icon is Mint's face and shows what it's doing.")
        key = (prefs.get("shortcuts") or {}).get("hide") or ""
        from mint.core import hotkeys
        page.end(f"To hide {name}, use the menu bar or {hotkeys.display(key) if key else 'Shortcuts'}: it "
                 f"comes out when you say Hey {name} and hides again when it goes back to sleep.")

        page.section("Sounds")
        self._row_switch(page, "ui_sounds", "Little sounds",
                         "Off unless you turn it on. Soft sounds for done, error and pokes; quiet while you "
                         "dictate or are on a call.")
        self._row_slider(page, "sound_volume", "Volume", 0.0, 1.0, on_release=lambda: self._sample_sound("done"))
        if advanced:
            self._row_switch(page, "sounds_notch", "Notch opening and closing", "Only with Little sounds on.")
            self._row_switch(page, "sounds_tasks", "Tasks", "Done, failed, a message sent, a file dropped.")
            self._row_switch(page, "sounds_play", "Play", "Pokes, dizzy and the morning hello.")
            self._row_buttons(page, "Hear them", [("Done", 80, lambda: self._sample_sound("done")),
                                                  ("Error", 80, lambda: self._sample_sound("error")),
                                                  ("Poke", 80, lambda: self._sample_sound("poke"))])
        page.end()

        self._anchor(page, "claude_code")            # the setup guide's "connect Claude Code"
        page.section("Claude mode (coding agents)")
        if not facts.get("agents_here", True):
            self._row_value(page, "Not on this Mac", "",
                            "Neither Claude Code nor Codex is installed, so this stays off. Install one and Mint "
                            "turns on its pop-ups by itself.", w=40)
        else:
            self._agent_rows(page, facts)
            if advanced:
                self._agent_more_rows(page, facts)
        page.end()

        if advanced:
            self._looks_more_rows(page, facts)
        self._more_section(page, "Face, animations, the notch's parts, which sounds play, and more for Claude Code "
                                 "and Codex.")

    def _agent_rows(self, page, facts: dict) -> None:
        self._row_switch(page, "agent_approvals", "Pop up when one asks you something",
                         "A permission request or a question: Allow, Deny or pick the answer right in the pop-up.")
        self._row_switch(page, "agent_open_on_done", "Pop up when one finishes",
                         "What it did, for a few seconds. Nothing shows while they work.")
        self._limits_rows(page, facts)
        # Connecting Claude Code is a setup step: in the basic view too.
        self._agent_hooks_row = self._row_buttons(
            page, "Approve from the notch", [(facts["hooks"], 150, self._toggle_hooks,
                                              facts["hooks"] != "Disconnect")],
            "Allow, Always or Deny Claude Code's permission requests, and answer its questions, from the notch (adds "
            "a hook to ~/.claude/settings.json; a backup is kept). The terminal still asks too.", icon="anthropic")

    def _limits_rows(self, page, facts: dict) -> None:
        """Usage limits used: per provider (Claude, Codex) a slim bar for the 5-hour and the weekly window - percent
        used, coloured green / amber / red, and when it resets. Not seen yet when there is nothing to show."""
        hint = ("Claude's from the Claude app (it notes them while it's open), Codex's from its logs. Ask: \"how "
                "much Claude do I have left?\"")
        data = facts.get("limits_data")
        if data is None:                             # no limits read (older facts): the one-line summary
            self._row_value(page, "Usage limits used", facts.get("usage") or "Not seen yet", hint, w=250)
            return
        seen = {app: x for app, x in (data or {}).items() if isinstance(x, dict) and ("5h" in x or "week" in x)}
        if not seen:
            self._row_badge(page, "Usage limits used", "Not seen yet", tone="off", hint=hint, w=110)
            return
        page.row("Usage limits used", hint)
        meter_w, gap = 150, 16
        now = time.time()
        for app, name, brand in (("claude", "Claude", "anthropic"), ("codex", "Codex", "openai")):
            x = seen.get(app)
            if x is None:
                self._row_badge(page, name, "Not seen yet", tone="off", icon=brand, w=110)
                continue
            when = settings_art.ago(x.get("at") or 0, now)
            card, top, left, h = page.row(name, f"Seen {when}" if when else "", height=58,
                                          control_w=2 * meter_w + gap, icon=brand)
            for i, (key, label) in enumerate((("5h", "5 hours"), ("week", "Week"))):
                used = x.get(key)
                meter = settings_art.usage_meter(
                    AppKit.NSMakeRect(left + i * (meter_w + gap), top + (h - 40) / 2 + 1, meter_w, 40),
                    label, None if used is None else float(used), x.get(f"{key}_resets") or 0, now)
                meter.setAccessibilityLabel_(f"{name} {label.lower()} limit")
                card.addSubview_(meter)

    def _show_tour(self) -> None:
        from mint.ui import onboarding
        onboarding.show_tour()

    def _show_setup(self) -> None:
        from mint.ui import onboarding
        onboarding.show()

    def _looks_more_rows(self, page, facts: dict) -> None:
        """Appearance & Sound, advanced: animation, the notch's parts, the music player."""
        page.section("Animation")
        self._row_switch(page, "auto_emotions", "Expressions during conversation",
                         "Smiles, laughs and hearts that follow what you say.")
        self._row_switch(page, "status_badge", "Status badge",
                         "Dots on its face while it works, red on an error, green when done. Point at it for the word.")
        self._row_switch(page, "cute_effects", "Action flourishes", "Little animations when it clicks, types or finishes.")
        self._row_switch(page, "cute_agents", "Critter helpers", "Cute critters that stand for its sub-agents.")
        self._row_switch(page, "cursor_effects", "On-screen effects", "Sparks, ripples and highlights.")
        self._row_switch(page, "window_glow", "Glow round its window",
                         "A soft glowing border round the window Mint is working in, so you can see where it is.")
        self._row_switch(page, "poke_play", "Poke to play",
                         "Click Mint: a slap. Three quick ones make it dizzy; keep going and it gets annoyed.")
        self._row_switch(page, "greeting", "Morning hello", "A sparkly hello the first time Mint wakes up each day.")
        self._row_switch(page, "wander", "Little trips", "Now and then the orb takes a short trip near its spot.")
        self._row_switch(page, "music_player_auto", "Music player",
                         "A mini player with the song, art and controls when Spotify or Music plays.")
        page.end()

        page.section("Notch (Dynamic Island)")
        self._row_switch(page, "notch_words", "Words in the notch", "What Mint says, word by word, as it drops down.")
        self._row_switch(page, "notch_controls", "Controls on hover", "Mic, voice, chat, sleep and more when you point at it.")
        self._row_switch(page, "notch_idle_face", "Little Mint when idle", "Off: a plain notch until something happens.")
        self._row_switch(page, "notch_playful", "Playful motion", "Bouncy springs, a hop as it opens, breathing with the voice.")
        self._row_switch(page, "notch_music", "Music in the notch", "Artwork and dancing bars while something plays; the player when you point at it.")
        self._row_switch(page, "notch_shelf", "File shelf", "Drag files to the notch to keep them handy; AirDrop, share or open them.")
        self._row_switch(page, "notch_calendar", "Calendar", "The week and today's events in the open notch.")
        self._row_switch(page, "notch_battery", "Battery", "The charge in the open notch, and a peek when you plug in or unplug.")
        self._row_switch(page, "notch_search", "Search in the notch", "Find files and apps from the notch; what Mint finds shows there too.")
        self._row_switch(page, "notch_composer", "Type in the notch", "The + in the open notch turns it into a box to type a request.")
        page.end()

    def _agent_more_rows(self, page, facts: dict) -> None:
        """Claude mode, advanced: live progress, Telegram, checks, hooks and the MCP connection."""
        self._row_popup(page, "agent_mode", "Claude Code and Codex", AGENT_MODES,
                        "Pop-ups only: when one asks or finishes. Live: also step by step while they work.", w=200)
        self._row_switch(page, "notch_agents_bar", "Agents bar under the notch",
                         "Live only: a slim bar with the current step while Claude Code or Codex works.")
        self._row_popup(page, "agent_telegram", "Message me on Telegram", AGENT_TELEGRAM,
                        "When an agent needs you or finishes: Allow / Deny there, or reply to tell it what to do.",
                        w=200)
        self._row_switch(page, "agent_checks", "Check agents' work",
                         "Reads their test runs, risky steps and changed files; the verdict shows in alerts and answers.")
        self._row_switch(page, "agent_fix_loop", "Send Claude back to fix failing tests",
                         "Needs Mint's Claude Code hooks. At most twice per request.")
        mcp = facts.get("mcp") or {}
        self._mcp_buttons = dict(zip(("claude", "codex"), self._row_buttons(
            page, "Let agents ask Mint", [(self._mcp_title(app, mcp.get(app)), {"claude": 178, "codex": 132}[app],
                                           lambda a=app: self._toggle_mcp(a)) for app in ("claude", "codex")],
            "Claude Code and Codex can check their own work with Mint before saying done (read-only).")))
        for app, button in self._mcp_buttons.items():
            if mcp.get(app) is None:
                button.setEnabled_(False)
                button.setToolTip_(f"{MCP_NAMES[app]} isn't installed on this Mac.")
        self._mcp_state = dict(mcp)
        self._agent_limits_row = self._row_buttons(
            page, "Claude's limits in a terminal", [(facts["limits"], 210, self._toggle_limits)],
            "Only for Claude Code in a terminal (the Claude app's are read without this): a status line passes "
            "its 5-hour and weekly limits to Mint, and keeps showing yours.")

    def _meet_setup(self) -> None:
        from mint.app import meet_call
        threading.Thread(target=meet_call.setup, name="meet-setup", daemon=True).start()

    def _page_shortcuts(self, page, facts: dict) -> None:
        name = prefs.name()
        page.section("Keyboard shortcuts")
        rows = (("toggle", "Open or close the chat", False), ("talk", f"Talk to {name} (no wake word)", False),
                ("dictate", "Dictate at the cursor (hold)", True), ("dictate_toggle", "Dictate hands-free", False),
                ("clipboard", "Open the clipboard", False), ("hide", f"Hide or show {name}", False))
        for key, title, modifier_ok in rows:
            card, top, x, h = page.row(title, control_w=250)
            button = self._button(card, "", x, top + (h - 28) / 2, 170, lambda: None)
            self._shortcut_button(button, key, modifier_ok)
            self._button(card, "Clear", x + 178, top + (h - 28) / 2, 72,
                         lambda k=key, b=button: self._set_shortcut(k, "", b))
        page.end("Click a shortcut, then press the keys you want (Esc cancels). They work in any app.")

        page.section("Dictation")
        page.text("Hold the dictate key, talk, let go: your words are typed where the cursor is, cleaned up. Tap it "
                  "twice to dictate hands-free, once more to finish. One key on its own works too: Right ⌥, Right ⌘, "
                  "Right ⌃, Right ⇧ or fn.", size=12, alpha=0.7)
        self._row_text(page, "dictation_words", "Words to spell exactly", "names, product words, jargon", w=280)
        page.end()

        # Apple Shortcuts (the Shortcuts app): `shortcuts list` can be slow, so the keys above never wait for it.
        self._anchor(page, "shortcuts")                  # the Apple Shortcuts connector's Settings…
        self._part(page, "apple_shortcuts", self._apple_shortcuts_sections, "Apple Shortcuts")
        self._more_section(page, "Make a new Apple Shortcut by describing it in plain words.")

    def _shortcut_button(self, button, key: str, modifier_ok: bool) -> None:
        from mint.core import hotkeys
        button.setTitle_(hotkeys.display(prefs.get("shortcuts").get(key, "")) or "Not set")
        self._on(button, lambda c: self._record_shortcut(button, key, modifier_ok))

    def _set_shortcut(self, key: str, value: str, button) -> None:
        from mint.core import hotkeys
        keys = dict(prefs.get("shortcuts"))
        keys[key] = value
        prefs.set("shortcuts", keys)
        button.setTitle_(hotkeys.display(value) or "Not set")

    def _record_shortcut(self, button, key: str, modifier_ok: bool) -> None:
        """Wait for the next key combination (or, for dictation, one modifier on its own)."""
        from mint.core import hotkeys

        def done(value):
            if value is None:
                button.setTitle_(hotkeys.display(prefs.get("shortcuts").get(key, "")) or "Not set")
            else:
                self._set_shortcut(key, value, button)
        record_keys(modifier_ok, button.setTitle_, done)

    def _facts_apple_shortcuts(self) -> dict:
        """`shortcuts list` (cached 5 minutes; Refresh asks again) - a command that can take long."""
        from mint.tools import shortcut_library
        from mint.tools import shortcut_maker  # noqa: F401 - imported here, off the main thread
        fresh = bool((self.__dict__.get("_apple_sc") or {}).pop("fresh", False))
        have = shortcut_library.installed(fresh=fresh)
        theirs = shortcut_library.user_shortcuts()
        return {"have": have, "registry": shortcut_library.registry(), "theirs": theirs,
                "allowed": {t: shortcut_library.allowed(t) for t in theirs[:80]}}

    def _apple_shortcuts_sections(self, page, facts: dict) -> None:
        """Shortcuts ▸ Apple Shortcuts (not keys): Mint's own, the user's (run / allow), and - advanced - making
        new ones. `facts` from _facts_apple_shortcuts, read off the main thread (_part)."""
        from mint.tools import shortcut_library
        from mint.tools import shortcut_maker
        name = prefs.name()
        state = self.__dict__.setdefault("_apple_sc", {"desc": "", "plan": None, "status": "", "lib": "", "ran": "",
                                                       "listening": False, "watching": set()})

        def later(fn) -> None:
            def run() -> None:
                if self.showing("apple_shortcuts"):
                    fn()
            AppHelper.callAfter(run)

        def background(fn, label: str) -> None:
            threading.Thread(target=fn, daemon=True, name=f"settings-{label}").start()

        def status_row(words: str, buttons: list, title: str = ""):
            """A row with a grey status line on the left (and `title` above it) and buttons on the right."""
            total = sum(b[1] for b in buttons) + 8 * max(0, len(buttons) - 1)
            card, top, x, h = page.row("", height=54 if title else ROW, control_w=total)
            if title:
                self._label(card, title, 16, top + 8, x - 24, h=18, size=13)
            label = self._label(card, words, 16, top + (28 if title else (h - 16) / 2), x - 24, h=16, size=11,
                                alpha=0.6)
            label.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
            for text, w, handler, *style in buttons:
                self._button(card, text, x, top + (h - 28) / 2, w, handler, primary=bool(style and style[0]))
                x += w + 8
            return label

        def watch(names: list[str]) -> None:
            """After an Add: look for the user's click for 5 minutes, then show the page again."""
            def run() -> None:
                deadline = time.time() + 300
                while time.time() < deadline:
                    time.sleep(4)
                    have = shortcut_library.installed(fresh=True)
                    if all(n in have for n in names):
                        state["lib"] = f"Added {', '.join(names)} ✓"
                        later(self.refresh)
                        break
                state["watching"].difference_update(names)
            fresh = [n for n in names if n not in state["watching"]]
            if fresh:
                state["watching"].update(fresh)
                background(run, "shortcut-watch")

        # --- Mint's own shortcuts ---
        have = facts["have"]
        page.section(f"Apple Shortcuts that {name} uses", icon=("square.2.layers.3d.fill", (0.93, 0.27, 0.55)))
        page.text(f"A few things only the Shortcuts app can do (Image Playground pictures, Do Not Disturb), so {name} "
                  "uses small shortcuts of its own. Add opens one in Shortcuts, where you click “Add Shortcut” once.",
                  size=12, alpha=0.7)
        missing = []
        for item in facts["registry"]:
            if item["name"] in have:
                self._row_badge(page, item["name"], "Added ✓", "ok", hint=item["purpose"], icon="shortcuts", w=100)
            else:
                missing.append(item["name"])
                self._row_buttons(page, item["name"], [("Add…", 100, lambda n=item["name"]: add([n]), True)],
                                  hint=item["purpose"], icon="shortcuts")

        def add(names: list[str]) -> None:
            lib_status.setStringValue_("Preparing…")

            def run() -> None:
                said = shortcut_library.offer(names[0]) if len(names) == 1 else shortcut_library.offer_missing()
                words = ("Opened in Shortcuts - click “Add Shortcut” there." if said.startswith("NOT DONE")
                         else said.removeprefix("FAILED: "))
                state["lib"] = words
                AppHelper.callAfter(lib_status.setStringValue_, words)
                if said.startswith("NOT DONE"):
                    watch(names)
            background(run, "shortcut-add")

        def reload() -> None:
            # Ask `shortcuts list` again off the main thread, then rebuild where the page was scrolled to - not a
            # "Loading…" page that jumps back to the top.
            state["lib"] = "Checking your shortcuts…"
            lib_status.setStringValue_(state["lib"])

            def run() -> None:
                try:
                    shortcut_library.installed(fresh=True)
                except Exception:
                    log.debug("shortcuts refresh failed", exc_info=True)
                state["lib"] = ""
                later(lambda: self.refresh(keep_scroll=True))
            background(run, "shortcut-refresh")
        buttons = [("Refresh", 90, reload)]
        if missing:
            buttons.append(("Add all missing", 140, lambda: add(list(missing)), True))
        lib_status = status_row(state["lib"], buttons)
        page.end()

        # --- The user's shortcuts ---
        page.section("Your Apple Shortcuts")
        theirs = facts["theirs"]
        if not theirs:
            page.text("None yet. Make one in the Shortcuts app (a Home scene, a Focus, a playlist) - then say “run "
                      "<its name>”.", size=12, alpha=0.7)
        ran = None
        for title in theirs[:80]:
            card, top, x, h = page.row(title, control_w=262)
            words = self._label(card, "Mint may run it", x, top + (h - 16) / 2, 128, h=16, size=11, alpha=0.6)
            words.setAlignment_(AppKit.NSTextAlignmentRight)
            self._switch(card, x + 136, top + (h - 22) / 2, facts["allowed"].get(title, True),
                         lambda on, t=title: shortcut_library.set_allowed(t, on))
            self._button(card, "Run", x + 186, top + (h - 28) / 2, 76, lambda t=title: run_one(t))
        if len(theirs) > 80:
            page.text(f"… and {len(theirs) - 80} more.", size=11, alpha=0.55)
        if theirs:
            ran = status_row(state["ran"] or "Run shows what the shortcut gives back.", [])

        def run_one(title: str) -> None:
            if ran is not None:
                ran.setStringValue_(f"Running “{title}”…")

            def go() -> None:
                result = shortcut_library.run(title)
                state["ran"] = f"“{title}”: {result}"
                if ran is not None:
                    AppHelper.callAfter(ran.setStringValue_, state["ran"])
            background(go, "shortcut-run")
        page.end(f"Switched off, {name} won't run that shortcut when asked. Run here always works.")

        # --- Making a new one (advanced, or while a plan made here is still on screen) ---
        if not (self._advanced() or state["plan"] or state["desc"]):
            return
        page.section("Make a new Apple Shortcut")
        page.text(f"Say what it should do and {name} plans it from the Shortcuts actions it knows - for example "
                  "“say hello and show today's date” or “set the volume to 30% and turn on dark mode”.",
                  size=12, alpha=0.7)
        card, top, x, h = page.row("", height=52, control_w=page.width - 32)
        field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(16, top + 14, page.width - 32 - 112, 24))
        field.setStringValue_(state["desc"])
        field.setPlaceholderString_("a shortcut that…")
        field.setBezelStyle_(AppKit.NSTextFieldRoundedBezel)
        field.cell().setUsesSingleLineMode_(True)
        field.cell().setScrollable_(True)
        field.setDelegate_(self.target)
        self._handlers[objc.pyobjc_id(field)] = lambda c: state.update(desc=str(c.stringValue()))
        card.addSubview_(field)
        self._button(card, "Plan it", page.width - 16 - 100, top + 12, 100, lambda: plan_it())
        result = state["plan"]
        if result:
            page.text(shortcut_maker.plan_text(result).split("\n", 1)[-1] if result.get("steps")
                      else shortcut_maker.plan_text(result).removeprefix("FAILED: ").removeprefix("REFUSED: "),
                      size=12, alpha=0.85)
        if result and result.get("steps"):
            status = status_row(state["status"] or "Not made yet.", [("Create", 100, lambda: create())],
                                title=f"Name: {result['name']}")
        else:
            status = status_row(state["status"] or "Plan it shows the steps first; nothing is made until Create.",
                                [])
        page.end("It opens in the Shortcuts app and you click “Add Shortcut”. Mint never makes shortcuts that send "
                 "messages or delete things; an email is only a draft, and a shell command is always shown first.")

        def plan_it() -> None:
            desc = str(field.stringValue()).strip()
            state["desc"] = desc
            if not desc:
                status.setStringValue_("Say what the shortcut should do first.")
                return
            status.setStringValue_("Planning…")

            def run() -> None:
                state["plan"] = shortcut_maker.plan(desc)
                state["status"] = ""
                later(self.refresh)
            background(run, "shortcut-plan")

        def create() -> None:
            result = state["plan"]
            status.setStringValue_("Building and signing…")
            if not state["listening"]:
                state["listening"] = True

                def heard(made: str, how: str) -> None:
                    state["status"] = {"added": f"Added “{made}” ✓ - say “run {made}”.",
                                       "not added": f"“{made}” wasn't added (no click in 5 minutes). Create opens it "
                                                    "again."}.get(how, state["status"])
                    later(self.refresh)
                shortcut_maker.on_status(heard)

            def run() -> None:
                said = shortcut_maker.create(result["id"])
                words = (f"Opened “{result['name']}” in Shortcuts - click “Add Shortcut” there."
                         if said.startswith("NOT DONE") else said.removeprefix("FAILED: "))
                state["status"] = words
                AppHelper.callAfter(status.setStringValue_, words)
            background(run, "shortcut-create")

    def _facts_accounts(self) -> dict:
        from mint.tools import accounts
        from mint.ui import brands
        from mint.app import email_remote
        from mint.app import telegram  # noqa: F401 - imported here, off the main thread
        brands.warm()                     # which brands' apps are installed (LaunchServices), for the icon tiles
        facts = {"telegram": telegram.status(), "email": email_remote.status()}
        # Mail's account names for Email control's popup (None: Mail isn't open - asking would open it).
        facts["email"]["mail_accounts"] = (accounts.mail_accounts() if facts["email"].get("backend") == "mail"
                                           else None)
        return facts

    def _page_accounts(self, page, facts: dict) -> None:
        """Accounts & connections: keys and accounts first, then the apps and services Mint works with."""
        from mint.tools import accounts
        from mint.ui import settings_connectors
        advanced = self._advanced()
        name = prefs.name()
        self._anchor(page, "keys")                   # the setup guide's keys (TypeSafe…)
        page.section("API keys")
        icons = {"GEMINI_API_KEY": "gemini", "TYPESAFE_API_KEY": "typesafe", "OPENAI_API_KEY": "openai"}
        for env, title, hint, more in KEYS:
            if more and not advanced:
                continue
            self._key_row(page, title, hint, os.environ.get(env, ""), lambda e=env, t=title: self._change_key(e, t),
                          icon=icons.get(env, "key"))
        page.end("Keys stay on this Mac, in a file only you can read. Nothing goes through any Hey Mint server - "
                 "there isn't one.")

        self._anchor(page, "google")
        page.section("Google: Gmail, Calendar and Meet")
        page.text("Add your Google account in System Settings ▸ Internet Accounts (turn on Mail and Calendars). "
                  f"macOS keeps it in sync, and {name} reads, triages and drafts email in Mail and plans "
                  "in Calendar, all on this Mac. No sign-in with Mint and no extra permissions.", size=12, alpha=0.75)
        status = page.text("Checking…\n ", size=11, alpha=0.6)
        self._row_buttons(page, "Gmail and Calendar", [("Check again", 110, lambda: check()),
                                                       ("Connect Google…", 150, accounts.open_internet_accounts, True)],
                          hint="Through macOS's Internet Accounts.", icon="google")
        self._anchor(page, "meet")
        self._row_buttons(page, "Google Meet (sign in once)", [("Open sign-in…", 150, self._meet_setup, True)],
                          f"For “start a Google Meet”: {name}'s Meet window has its own Chrome profile. You sign in "
                          "there; Mint never types passwords.", icon="meet")
        if advanced:
            self._row_popup(page, "meet_share", "Share in the call", MEET_SHARE,
                            "\"Start a Google Meet\" (or /meet on Telegram): Mint joins, shares this, and sends you "
                            "the link.", w=200)
        page.end("Drafts only: Mint never sends an email itself - except Email control's answers, which go only "
                 "to the verified, allowed sender of an emailed request, in its thread.")

        def check():
            status.setStringValue_("Checking…")

            def run():
                words = accounts.summary()
                AppHelper.callAfter(status.setStringValue_, words)
            threading.Thread(target=run, daemon=True, name="settings-accounts").start()
        check()

        # Telegram and Email control: a card each once set up (or opened, or with advanced settings); before that,
        # one line each with Set up…
        from mint.app import telegram
        tg, em = facts["telegram"], facts["email"]
        tg_full = (advanced or "telegram" in self._opened or bool(os.environ.get(telegram.TOKEN_ENV))
                   or bool(tg.get("enabled") or tg.get("paired")))
        em_full = advanced or "email" in self._opened or bool(em.get("enabled") or em.get("address"))
        if not (tg_full and em_full):
            page.section(f"Control {name} from anywhere", icon=("antenna.radiowaves.left.and.right",
                                                                (0.16, 0.62, 0.93)))
            if not tg_full:
                self._anchor(page, "telegram")
                self._row_buttons(page, "Telegram", [("Set up…", 100, lambda: self._open_card("telegram"), True)],
                                  hint="Text or send voice notes to your own Telegram bot from your phone.",
                                  icon="telegram")
            if not em_full:
                self._anchor(page, "email")
                self._row_buttons(page, "Email", [("Set up…", 100, lambda: self._open_card("email"), True)],
                                  hint="Email a request from anywhere and get the answer by email.", icon="email")
            page.end()
        if tg_full:
            self._anchor(page, "telegram")
            self._telegram_card(page, tg)
        if em_full:
            self._anchor(page, "email")
            self._email_card(page, em)

        self._note_link(page, f"What macOS lets {name} do (Microphone, Accessibility, Calendars…) is on its own page.",
                        "Permissions ▸", lambda: self.select("storage", anchor="permissions"))

        # The apps and services (settings_connectors): their own read, so the accounts above never wait for it.
        self._anchor(page, "connectors")             # the setup guide's "apps"
        self._part(page, "connectors", lambda p, f: settings_connectors.page(self, p, f), "Apps and services")
        self._more_section(page, "The OpenAI key, Meet screen sharing, email rules, making connectors for any app, "
                                 "and apps not on this Mac yet.")

    def _telegram_card(self, page, state: dict) -> None:
        """Telegram remote control: the bot token, the switches, and pairing with one phone. `state` is the
        bridge's status, read off the main thread by _facts_accounts."""
        from mint.app import telegram
        page.section("Telegram remote control")
        page.text(f"Text or send voice notes to your own Telegram bot from anywhere; {prefs.name()} does it on this "
                  "Mac and shows each step in the chat. Make a bot with @BotFather in Telegram, then add its token "
                  "here.", size=12, alpha=0.75)
        token = os.environ.get(telegram.TOKEN_ENV, "")
        self._key_row(page, "Bot token", "From @BotFather. Kept on this Mac, in a file only you can read.", token,
                      lambda: self._change_key(telegram.TOKEN_ENV, "Telegram bot"), icon="telegram")
        doing = ("Add the bot token first." if not token else "Off." if not state["enabled"]
                 else state["error"] or (f"Connected as {state['bot']}." if state["running"] else "Connecting…"))
        card, top, x, h = page.row("Remote control", doing, control_w=38)

        def switch(on: bool) -> None:
            prefs.set("telegram_enabled", on)
            telegram.refresh()
            AppHelper.callLater(1.5, self.refresh)
        self._switch(card, x, top + (h - 22) / 2, state["enabled"], switch)
        who = state["paired"]
        if who:
            def unpair() -> None:
                # Tells the phone over the network: never on the main thread.
                self._background("telegram-unpair", telegram.unpair, lambda _: self.refresh())
            self._row_buttons(page, f"Paired with {who['name']}", [("Unpair", 100, unpair)],
                              hint=f"Since {who['paired_at']}. Only this Telegram account can control "
                                   f"{prefs.name()}; everyone else is ignored.")
        else:
            code = state["code"]
            card, top, x, h = page.row("Pairing code", "Send this code to your bot from your phone. The first "
                                       "Telegram account that sends it becomes the only one that can control "
                                       f"{prefs.name()}.", control_w=210)
            big = self._label(card, f"{code[:3]} {code[3:]}", x, top + (h - 28) / 2, 110, h=28, size=22, bold=True)
            big.setSelectable_(True)

            def new_code() -> None:
                self._background("telegram-code", lambda: telegram.pairing_code(new=True), lambda _: self.refresh())
            self._button(card, "New code", x + 118, top + (h - 28) / 2, 92, new_code)
        self._row_switch(page, "telegram_read_only", "Read-only from Telegram",
                         hint="Requests from your phone never send, delete or buy anything. Otherwise the usual "
                              "rules apply: risky steps need your go-ahead.")
        self._row_switch(page, "telegram_notify", "Alerts to Telegram",
                         hint="Finished downloads, trackers and agent results also go to the chat.")
        page.end("Only this Mac talks to Telegram, over HTTPS; there is no server in between. Every remote request is "
                 "logged in ~/Library/Application Support/Mint/remote.log.")

        seen = (bool(who), state["code"], state["running"], state["error"])

        def watch() -> None:
            """Pairing happens on the phone: show it here as soon as it does (asked off the main thread)."""
            if self.window is None or self.page_key != "accounts":
                return

            def compare(now) -> None:
                if self.page_key != "accounts":
                    return
                if isinstance(now, dict) and (bool(now["paired"]), now["code"], now["running"], now["error"]) != seen:
                    self.refresh()
                    return
                AppHelper.callLater(2.0, watch)
            self._background("telegram-watch", telegram.status, compare)
        if state["enabled"] and token:
            AppHelper.callLater(2.0, watch)

    def _email_card(self, page, state: dict) -> None:
        """Email remote control: how mail is read (Apple Mail - no password - or an app password in the Keychain),
        the address, who may send requests, the subject prefix and the secret word. `state` is
        email_remote.status() plus Mail's account names, read off the main thread by _facts_accounts."""
        from mint.app import email_remote
        mail = state.get("backend", "mail") == "mail"
        advanced = self._advanced()
        prefix = str(prefs.get("email_prefix") or "Mint:").strip()
        page.section("Email control")
        if mail:
            page.text(f"Email {prefs.name()} a request from anywhere - no Telegram needed - and get the answer by "
                      "email. It reads and answers through the Mail app on this Mac, with the account you added "
                      "in System Settings ▸ Internet Accounts: no password to make or paste. Mail opens in the "
                      f"background while this is on. Start the subject with “{prefix}”.", size=12, alpha=0.75)
        else:
            page.text(f"Email {prefs.name()} a request from anywhere - no Telegram needed - and get the answer by "
                      "email. Give it an address (your Gmail, or one made for it) and that account's app password: "
                      "for Gmail, turn on 2-Step Verification, then make one at myaccount.google.com/apppasswords. "
                      f"Start the subject with “{prefix}”.", size=12, alpha=0.75)

        def changed() -> None:
            email_remote.refresh()
            AppHelper.callLater(0.5, self.refresh)
        if advanced:
            self._row_popup(page, "email_backend", "Read mail with",
                            [("auto", "Automatic", ("wand.and.stars", _BLUE)),
                             ("imap", "Gmail app password", functools.partial(_brand_image, "gmail", 18)),
                             ("mail", "Apple Mail (no password)", functools.partial(_brand_image, "mail", 18))],
                            hint="Automatic: the app password when one is saved - Gmail tells Mint about a new message "
                                 "at once and answers come back in seconds - else Apple Mail (checks every 30 s; works "
                                 "when Google won't make an app password).", on_change=changed)
        self._row_text(page, "email_address", "Email address", "you@gmail.com",
                       hint=("The inbox it watches and answers from." if not mail else
                             "Your own address, as in Mail: requests from it are allowed when the list below is "
                             "empty." if advanced else "Your own address, as in Mail: send requests from it."),
                       settle=1.0)
        if mail:
            names = state.get("mail_accounts")
            current = state.get("mail_account", "")
            options = [("", "Every inbox", ("tray.2.fill", _BLUE))] + [(n, n, ("envelope.fill", _GREY))
                                                                        for n in names or []]
            if current and current not in (names or []):
                options.append((current, current, ("envelope.fill", _GREY)))
            self._row_popup(page, "email_mail_account", "Mail account", options,
                            hint=("Mail isn't open, so its accounts aren't listed yet." if names is None
                                  else "Add accounts in System Settings ▸ Internet Accounts (turn on Mail)."
                                  if not names else "The inbox it watches; replies go from that account."),
                            on_change=changed)
            if state.get("chosen") == "auto" and advanced:
                self._email_password_row(page, state, "Optional: save one and Mint reads Gmail directly - "
                                                      "answers in seconds instead of through Mail.")
            ready = bool(state["address"])
            doing = ("Add your email address first." if not ready else "Off." if not state["enabled"]
                     else state["error"] or (f"Watching {state.get('where', 'Mail')}." if state["running"]
                                             else "Starting…"))
        else:
            self._key_row(page, "App password", "You paste it; it goes into the macOS Keychain and is never shown "
                          "again.", bool(state["password_set"]), lambda: self._email_secret("password"), icon="key")
            doing = ("Add the address and its app password first." if not (state["address"] and state["password_set"])
                     else "Off." if not state["enabled"]
                     else state["error"] or (f"Watching {state['address']}." + (" New mail is seen at once (IDLE)."
                                                                                 if state.get("idle") else "")
                                             if state["running"] else "Connecting…"))
        card, top, x, h = page.row("Email control", doing, control_w=38)

        def switch(on: bool) -> None:
            prefs.set("email_enabled", on)
            email_remote.refresh()
            AppHelper.callLater(3.0, self.refresh)
        self._switch(card, x, top + (h - 22) / 2, state["enabled"], switch)
        if advanced or state["allow"] == "all":
            self._row_popup(page, "email_allow", "Who may send requests",
                            [("list", "Only the addresses below", ("person.crop.circle.badge.checkmark", _GREEN)),
                             ("all", "Anyone who knows the secret word", ("key.fill", (0.95, 0.68, 0.0)))],
                            hint="Either way, the mail server must vouch that a message really is from its sender.",
                            on_change=lambda: AppHelper.callLater(0.5, self.refresh))
        if advanced:
            self._row_text(page, "email_allowed", "Allowed senders", "you@example.com, …",
                           hint="Separated by commas. Empty: only the address above (email yourself).", settle=1.0)
            self._row_text(page, "email_prefix", "Subject starts with", "Mint:",
                           hint="Only these messages are read; newsletters and ads never are. \"Mint: /help\" lists "
                                "the commands.", w=160, settle=1.0)
        self._key_row(page, "Secret word", "Must also be in the subject. Recommended; required when anyone may send "
                      "requests.", bool(state["secret_set"]), lambda: self._email_secret("secret"),
                      words=None if state["secret_set"] or state["allow"] != "all" else "Needed")
        self._row_switch(page, "email_read_only", "Read-only by email",
                         hint="Emailed requests never send, delete or buy anything.")
        page.end("A message counts only when the mail server verified its sender (DKIM, or SPF with DMARC): a forged "
                 "From is ignored. Answers go only to that sender, in the same thread; handled messages are marked "
                 "read. Only this Mac talks to the mail server, through Mail or directly. Every request is logged in "
                 "~/Library/Application Support/Mint/remote.log." if advanced else
                 "Only mail from a verified sender counts, and answers go only to that sender. Every request is "
                 "logged.")

    def _email_password_row(self, page, state: dict, hint: str) -> None:
        """The app-password row (Set / Not set, Add… or Change…), for "Automatic" while Mail is the way in."""
        self._key_row(page, "App password", hint, bool(state["password_set"]), lambda: self._email_secret("password"),
                      icon="key")

    def _email_secret(self, which: str) -> None:
        """The app password (pasted by the user) or the secret word: straight into the Keychain, never shown."""
        from mint.app import email_remote
        address = str(prefs.get("email_address") or "").strip().lower()
        alert = AppKit.NSAlert.alloc().init()
        if which == "password" and not address:
            alert.setMessageText_("Add the email address first.")
            alert.runModal()
            return
        if which == "password":
            alert.setMessageText_(f"App password for {address}")
            alert.setInformativeText_("Paste the app password made for this (Gmail: myaccount.google.com/"
                                      "apppasswords). It is saved in the macOS Keychain only. Leave it empty and "
                                      "press Save to remove it.")
        else:
            alert.setMessageText_("Secret word")
            alert.setInformativeText_("A word every emailed request must have in its subject, e.g. \"Mint: "
                                      "pineapple what's on today?\". Leave it empty and press Save to remove it.")
        field = AppKit.NSSecureTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 320, 24))
        alert.setAccessoryView_(field)
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        value = str(field.stringValue())

        def save() -> bool:
            if which == "password":
                return email_remote.save_password(address, value)
            return email_remote.save_secret(value)
        self._background("email-secret", save, lambda _: self.refresh())

    def _change_key(self, env: str, title: str) -> None:
        """Paste a new key; it goes into .env (mode 600) and the environment."""
        from mint.core import config
        from mint.core import llm
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(f"{title} API key")
        alert.setInformativeText_("Paste the key. It is saved on this Mac only. Leave it empty and press Save to "
                                  "remove an optional key.")
        field = AppKit.NSSecureTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 320, 24))
        alert.setAccessoryView_(field)
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        value = str(field.stringValue()).strip()
        if env == config.API_KEY_ENV and not value:
            return                                                    # the Gemini key is required
        _write_env(env, value)
        if value:
            os.environ[env] = value
        else:
            os.environ.pop(env, None)
        if env == config.API_KEY_ENV:
            llm._client_cache = None
        if env == "TELEGRAM_BOT_TOKEN":
            from mint.app import telegram
            telegram.refresh()
        self.refresh()

    def _facts_connectors(self) -> dict:
        from mint.ui import settings_connectors
        return settings_connectors.facts(self)

    def _facts_models(self) -> dict:
        from mint.ui import brands
        from mint.ui import settings_models
        brands.warm()
        return settings_models.facts()

    def _page_models(self, page, facts: dict) -> None:
        from mint.ui import settings_models
        settings_models.page(self, page, facts)

    def _facts_brain(self) -> dict:
        from mint.ui import brain as brain_window
        from mint.knowledge import memory as membank
        from mint.knowledge import skills as skillbook  # noqa: F401 - imported here, off the main thread
        skills = skillbook.all_skills()
        blocks = membank.blocks()
        return {"skills": len(skills), "reliable": sum(1 for s in skills if skillbook.rank(s) == "reliable"),
                "facts": len(blocks), "fixed": sum(1 for b in blocks if b.get("pinned"))}

    def _page_brain(self, page, facts: dict) -> None:
        """Skills & Memory: counts here; seeing and editing them is its own, bigger window."""
        from mint.ui import brain as brain_window
        skills, reliable, fixed = facts["skills"], facts["reliable"], facts["fixed"]
        page.section("Skills")
        self._row_buttons(page, f"{skills} learned how-tos ({reliable} reliable)",
                          [("See and edit…", 130, lambda: brain_window.open_window("skills"))],
                          hint="Jev picks the right one for each request; they improve as they are used.")
        page.end()
        page.section("Memory")
        self._row_buttons(page, f"{facts['facts']} facts, {fixed} fixed",
                          [("See and edit…", 130, lambda: brain_window.open_window("memory"))],
                          hint="Fixed facts go into every conversation; the rest are looked up when needed.")
        page.end('You can also ask: “what do you remember about me?”, “forget that”, “remember that my '
                 'manager is Meera”.')

    def _facts_storage(self) -> dict:
        from mint.tools import clipboard as clip_tools
        from mint.core import config
        from mint.core import permissions
        from mint.knowledge import timeline  # noqa: F401 - imported here, off the main thread
        return {"folder": str(config.storage()), "perms": permissions.snapshot(set(self._perm_asked))}

    def _page_storage(self, page, facts: dict) -> None:
        """Every macOS permission, where Mint keeps what it makes, and the switches for what it records."""
        from mint.core import config
        if facts.get("perms") is not None:
            self._permissions_section(page, facts["perms"])
        page.section("Mint folder")
        path = self._row_value(page, "Saved in", facts["folder"].replace(os.path.expanduser("~"), "~"),
                               hint="Meetings, videos, documents and agent work, one folder per kind.", w=300)

        def choose() -> None:
            panel = AppKit.NSOpenPanel.openPanel()
            panel.setCanChooseDirectories_(True)
            panel.setCanChooseFiles_(False)
            panel.setCanCreateDirectories_(True)
            panel.setPrompt_("Use this folder")
            if panel.runModal() == AppKit.NSModalResponseOK and panel.URL() is not None:
                prefs.set("storage_folder", str(panel.URL().path()))
                path.setStringValue_(str(config.storage()).replace(os.path.expanduser("~"), "~"))

        def default() -> None:
            prefs.set("storage_folder", "")
            path.setStringValue_(str(config.storage()).replace(os.path.expanduser("~"), "~"))
        self._row_buttons(page, "", [("Choose…", 100, choose), ("Show in Finder", 130, lambda: (
            AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.fileURLWithPath_(str(config.storage()))))),
            ("Use the default", 130, default)])
        page.end("Files already saved stay where they are.")

        page.section("Meetings")
        self._row_switch(page, "meeting_offer", "Offer to take notes on calls", "Meet, Zoom, Teams…")
        self._row_switch(page, "meeting_keep_audio", "Keep the recording", "Next to the transcript and notes.")
        page.end()

        page.section("Clipboard")
        self._row_popup(page, "screenshot_to", "Screenshots go to", SCREENSHOTS, w=220)

        def clear_clipboard() -> None:
            from mint.tools import clipboard as clip_tools

            def work() -> None:
                clip_tools._load()
                clip_tools.delete([h["id"] for h in clip_tools.HISTORY])
            self._background("clipboard-clear", work)
        self._row_buttons(page, "Clipboard history", [("Clear history", 120, clear_clipboard)],
                          hint="Kept on this Mac for 7 days. Passwords and keys are never kept.")
        page.end()

        page.section("Privacy")
        self._row_switch(page, "timeline", "Activity timeline",
                         "Notes the app, window and web page in front (text only, 14 days). Never screenshots or "
                         "typing; private windows and password managers are skipped.")

        def forget_timeline() -> None:
            from mint.knowledge import timeline
            self._background("timeline-clear", timeline.clear)
        self._row_buttons(page, "", [("Delete the timeline", 160, forget_timeline)])
        page.end()

    def _facts_usage(self) -> dict:
        from mint.core import usage
        days = (1, 7, 30)[getattr(self, "_usage_period", 0)]
        return {"rows": usage.totals(days), "daily": usage.daily(14)}

    def _page_usage(self, page, facts: dict) -> None:
        from mint.core import usage
        page.section()
        card, top, x, h = page.row("Period", control_w=260)
        periods = AppKit.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Today", "7 days", "30 days"], AppKit.NSSegmentSwitchTrackingSelectOne, None, None)
        periods.setFrame_(AppKit.NSMakeRect(x, top + (h - 24) / 2, 260, 24))
        periods.setSelectedSegment_(getattr(self, "_usage_period", 0))
        card.addSubview_(periods)
        page.end()

        rows = facts["rows"]
        page.section("Tokens by model")
        columns = [("Model", 0), ("Requests", 90), ("Input", 110), ("Output", 110)]
        card, top, _, _ = page.row("", height=34)
        x = page.width - 16
        for words, w in reversed(columns[1:]):
            x -= w
            head = self._label(card, words, x, top + 9, w, size=11, alpha=0.5)
            head.setAlignment_(AppKit.NSTextAlignmentRight)
        self._label(card, "Model", 16, top + 9, 200, size=11, alpha=0.5)
        total = {"requests": 0, "input": 0, "output": 0}
        for row in rows or []:
            card, top, _, _ = page.row("", height=34)
            self._label(card, row["model"], 16, top + 8, page.width - 360, size=12)
            x = page.width - 16
            for key, w in (("output", 110), ("input", 110), ("requests", 90)):
                x -= w
                value = self._label(card, f"{row[key]:,}", x, top + 8, w, size=12, alpha=0.8)
                value.setAlignment_(AppKit.NSTextAlignmentRight)
                total[key] += row[key]
        if not rows:
            page.text("Nothing yet for this period.", size=12, alpha=0.55)
        else:
            card, top, _, _ = page.row("", height=34)
            self._label(card, "Total", 16, top + 8, 200, size=12, bold=True)
            x = page.width - 16
            for key, w in (("output", 110), ("input", 110), ("requests", 90)):
                x -= w
                value = self._label(card, f"{total[key]:,}", x, top + 8, w, size=12, bold=True)
                value.setAlignment_(AppKit.NSTextAlignmentRight)
        page.end("Counts only - never what was said. Input includes what Mint sends with each request (the "
                 "conversation so far, screenshots). Free Gemini keys cost nothing; limits are per day.")

        page.section("Last 14 days")
        card, top, _, _ = page.row("", height=110)
        daily = facts["daily"]
        peak = max([t for _, t in daily] + [1])
        bar_w = (page.width - 32) / len(daily)
        accent = AppKit.NSColor.controlAccentColor()
        for i, (day, tokens) in enumerate(daily):
            height = max(2.0, 70 * tokens / peak)
            bar = AppKit.NSView.alloc().initWithFrame_(
                AppKit.NSMakeRect(16 + i * bar_w + 3, top + 84 - height, bar_w - 6, height))
            bar.setWantsLayer_(True)
            bar.layer().setCornerRadius_(3)
            bar.layer().setBackgroundColor_(_cgc(accent, 0.85 if tokens else 0.15))
            bar.setToolTip_(f"{day}: {tokens:,} tokens")
            card.addSubview_(bar)
            if i in (0, len(daily) - 1):
                self._label(card, day[5:], 16 + i * bar_w - 4, top + 88, bar_w + 8, size=10, alpha=0.5)
        page.end()

        page.section()
        self._row_buttons(page, "Reset the counts", [("Reset", 90, lambda: self._background(
            "usage-reset", usage.clear, lambda _: self.refresh()))])
        page.end()

        def chosen(control):
            self._usage_period = int(control.selectedSegment())
            self.refresh()
        self._on(periods, chosen)

    def _facts_help(self) -> dict:
        from mint.app import power
        from mint.app import report
        from mint.app import updater  # noqa: F401 - imported here, off the main thread
        return {"info": updater.info(), "can_restart": power.can_restart()}

    def _page_help(self, page, facts: dict) -> None:
        from mint.app import report
        from mint.app import updater
        info = facts["info"]
        current, latest = info.get("current") or "?", info.get("latest")
        newer = bool(latest) and updater.newer(latest, current)
        page.section("Hey Mint")
        # The app's own version (it showed mint/__init__'s stale 0.3.1 under v0.6.1).
        self._row_value(page, "Version", current + (f"  ·  {latest} is out" if newer else ""))
        if info.get("can_update"):
            self._row_switch(page, "auto_update", "Update automatically",
                             "Checks at launch and twice a day; installs while you're away and reopens in place. "
                             "Your permissions stay.")
        words = info.get("detail") or (f"Version {latest} is ready." if newer else
                                       f"You have the newest version ({current})." if latest else "")
        moving = bool(info.get("can_update")) and _updating(info.get("status"), info.get("requested"),
                                                            info.get("ready"))
        if moving:
            self._update_progress(page)          # the progress row says it all, live
            buttons = []
        else:
            status = page.text(words or " ", size=11, alpha=0.6)
            buttons = [("Check for updates", 150, lambda: self._check_updates(status))]
        if moving:
            pass                                 # (no Install button while it's on its way)
        elif newer and info.get("can_update") and info.get("ready"):
            buttons.append((f"Install {latest} now", 150, self._install_now, True))
        elif newer and info.get("can_update"):
            buttons.append((f"Install {latest}", 130, lambda: self._install_update(status), True))
        elif newer:
            # This copy can't swap itself (not the downloaded app, or no write access): a plain download,
            # never a terminal command.
            buttons.append((f"Download {latest}", 140, lambda: _open(info.get("download") or info["page"]), True))
        if buttons:
            self._row_buttons(page, "", buttons)
        if not info.get("can_update"):
            page.text("This copy doesn't update itself. Press Download when a new version is out, open it, and drag "
                      "Hey Mint into Applications - your settings and permissions stay.", size=11, alpha=0.55)
        page.end("Free and open source (GPL-3.0).")

        page.section("Help")
        self._row_buttons(page, "The guide", [("Open the guide", 140, lambda: _open("https://hey-mint.pages.dev/docs"))],
                          hint="Everything it can do, with short videos.")
        self._row_buttons(page, "Something went wrong?", [("Report a problem…", 160, report.open_issue)],
                          hint="Opens a new GitHub issue in your browser. Recent errors (nothing personal) are copied "
                               "for you to paste in if you like. Nothing is sent until you submit it.")
        if self._advanced():
            self._row_buttons(page, "The log", [("Open log", 110, lambda: _open_file(report.LOG))],
                              hint="What Mint did and any errors, as text.")
        self._row_buttons(page, "Is everything set up?", [("Check my setup", 140, self._check_setup)],
                          hint="Checks permissions, keys, models and connections. Read-only.")
        if facts["can_restart"]:
            self._row_buttons(page, "Something stuck?", [(f"Restart {prefs.name()}", 130, lambda: self._restart())],
                              hint=f"Restarts {prefs.name()} - the conversation is kept. If it is too stuck to open "
                                   "this window, press ⌃⌥⌘M (Control-Option-Command-M) anywhere, or use the "
                                   "menu-bar item that appears when it stops responding.")
        page.end()

        if self._advanced():
            page.section("Files you can edit")
            self._row_buttons(page, "The settings file", [("Edit…", 110, self._edit_settings_file)],
                              hint="Every setting, spelled out as text.")
            self._row_buttons(page, "Accounts, aliases and routines", [("Edit…", 110, self._edit_custom_file)],
                              hint="Your own routines, spoken aliases and extra accounts.")
            page.end()
        self._more_section(page, "The log, and the settings files you can edit by hand.")

    def _hooks_title(self) -> str:
        try:
            from mint.tools import agent_hooks
            return "Disconnect" if agent_hooks.installed() else "Connect Claude Code"
        except Exception:
            return "Connect Claude Code"

    def _toggle_hooks(self) -> None:
        from mint.tools import agent_hooks
        connected = agent_hooks.installed()
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Stop answering Claude Code from the notch?" if connected
                              else "Answer Claude Code from the notch?")
        alert.setInformativeText_(agent_hooks.preview(not connected))
        alert.addButtonWithTitle_("Disconnect" if connected else "Connect")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        said = agent_hooks.uninstall() if connected else agent_hooks.install()
        for button in getattr(self, "_agent_hooks_row", None) or []:
            button.setTitle_(self._hooks_title())
        note = AppKit.NSAlert.alloc().init()
        note.setMessageText_("Claude Code")
        note.setInformativeText_(said)
        note.runModal()

    def _check_setup(self) -> None:
        def work():
            import contextlib
            import io
            from mint.app import doctor
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                doctor.run()
            return buf.getvalue().strip() or "No output."

        def show(text):
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Mint's setup check")
            scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 520, 300))
            scroll.setHasVerticalScroller_(True)
            scroll.setBorderType_(AppKit.NSBezelBorder)
            view = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 520, 300))
            view.setEditable_(False)
            view.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(11, AppKit.NSFontWeightRegular))
            view.setString_(text)
            view.setAutoresizingMask_(AppKit.NSViewWidthSizable)
            scroll.setDocumentView_(view)
            alert.setAccessoryView_(scroll)
            alert.addButtonWithTitle_("OK")
            alert.runModal()
        self._background("doctor", work, show)

    def _mcp_title(self, app: str, on) -> str:
        return f"{'Disconnect' if on else 'Connect'} {MCP_NAMES[app]}"

    def _toggle_mcp(self, app: str) -> None:
        from mint.tools import agent_mcp
        on = bool(getattr(self, "_mcp_state", {}).get(app))
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(f"Stop letting {MCP_NAMES[app]} ask Mint?" if on else f"Let {MCP_NAMES[app]} ask Mint?")
        alert.setInformativeText_(agent_mcp.preview(app, not on))
        alert.addButtonWithTitle_("Disconnect" if on else "Connect")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        button = getattr(self, "_mcp_buttons", {}).get(app)
        if button is not None:
            button.setEnabled_(False)

        def work():
            said = agent_mcp.disconnect(app) if on else agent_mcp.connect(app)
            return said, bool(agent_mcp.connected(app))

        def done(result):
            said, now = result if isinstance(result, tuple) else (str(result), on)
            self._mcp_state = {**getattr(self, "_mcp_state", {}), app: now}
            if button is not None:
                button.setTitle_(self._mcp_title(app, now))
                button.setEnabled_(True)
            note = AppKit.NSAlert.alloc().init()
            note.setMessageText_(MCP_NAMES[app])
            note.setInformativeText_(said)
            note.runModal()
        self._background(f"mcp-{app}", work, done)

    def _limits_title(self) -> str:
        try:
            from mint.tools import agent_hooks
            return "Stop showing Claude's limits" if agent_hooks.statusline_installed() else "Show Claude's usage limits"
        except Exception:
            return "Show Claude's usage limits"

    def _toggle_limits(self) -> None:
        from mint.tools import agent_hooks
        on = agent_hooks.statusline_installed()
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Stop reading Claude's usage limits?" if on else "Show Claude's usage limits?")
        alert.setInformativeText_(agent_hooks.statusline_preview(not on))
        alert.addButtonWithTitle_("Stop" if on else "Show")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        said = agent_hooks.uninstall_statusline() if on else agent_hooks.install_statusline()
        for button in getattr(self, "_agent_limits_row", None) or []:
            button.setTitle_(self._limits_title())
        note = AppKit.NSAlert.alloc().init()
        note.setMessageText_("Claude's usage limits")
        note.setInformativeText_(said)
        note.runModal()

    def _edit_settings_file(self) -> None:
        prefs.write_full()
        subprocess.run(["open", "-e", str(prefs.PATH)], check=False)

    def _edit_custom_file(self) -> None:
        from mint.core import custom
        if not custom.PATH.exists():
            example = custom.PATH.with_name("custom.example.json")
            custom.PATH.write_text(example.read_text() if example.exists() else "{}\n")
        subprocess.run(["open", "-e", str(custom.PATH)], check=False)

    def _install_update(self, status) -> None:
        """Downloading, checking the signature and swapping the app take a while: on a thread. The page shows the
        progress row at once (not the same button again)."""
        from mint.app import updater
        status.setStringValue_("Starting the download…")
        self._background("update-install", updater.install, lambda said: (status.setStringValue_(said),
                                                                          self.refresh(keep_scroll=True)))
        AppHelper.callLater(0.4, lambda: self.window is not None and self.refresh(keep_scroll=True))

    def _install_now(self) -> None:
        """Downloaded and checked: swap it in and restart now (the user asked; otherwise it waits for idle)."""
        from mint.app import updater
        self._background("update-now", updater.install_now)

    def _update_progress(self, page) -> None:
        """While an update downloads, checks and installs: a spinner, what it's doing with the percent and the
        megabytes, and a bar - updated live (twice a second) until it's ready, then the page is built again."""
        card, top, _x, _h = page.row("", height=64)
        width = page.width
        spinner = AppKit.NSProgressIndicator.alloc().initWithFrame_(AppKit.NSMakeRect(16, top + 12, 16, 16))
        spinner.setStyle_(AppKit.NSProgressIndicatorStyleSpinning)
        spinner.setControlSize_(AppKit.NSControlSizeSmall)
        spinner.setDisplayedWhenStopped_(False)
        spinner.startAnimation_(None)
        card.addSubview_(spinner)
        label = self._label(card, "", 40, top + 11, width - 56, h=18, size=13)
        bar = AppKit.NSProgressIndicator.alloc().initWithFrame_(AppKit.NSMakeRect(16, top + 37, width - 32, 10))
        bar.setStyle_(AppKit.NSProgressIndicatorStyleBar)
        bar.setIndeterminate_(False)
        bar.setMinValue_(0.0)
        bar.setMaxValue_(1.0)
        card.addSubview_(bar)
        self._upd_view = (spinner, label, bar)
        self._update_tick()
        if getattr(self, "_upd_timer", None) is None:
            self._upd_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                0.5, True, lambda timer: self._update_tick())

    def _update_tick(self) -> None:
        from mint.app import updater
        view = getattr(self, "_upd_view", None)
        if self.window is None or view is None or view[1].window() is None:
            self._stop_update_timer()
            return
        spinner, label, bar = view
        p = updater.progress()
        version = p["version"] or "the update"
        status = p["status"]
        if status == "downloading":
            mb = p["size"] / 1e6
            label.setStringValue_(f"Downloading Hey Mint {version} · {p['progress'] * 100:.0f}%"
                                  + (f" · {p['progress'] * mb:.0f} of {mb:.0f} MB" if mb else ""))
            bar.setIndeterminate_(False)
            bar.setDoubleValue_(p["progress"])
        elif status == "verifying":
            label.setStringValue_(f"Checking Hey Mint {version} (its signature and checksum)…")
            bar.setIndeterminate_(True)
            bar.startAnimation_(None)
        elif status == "installing":
            label.setStringValue_(f"Installing Hey Mint {version} - Mint restarts in a few seconds…")
            bar.setIndeterminate_(True)
            bar.startAnimation_(None)
        elif _updating(status, p["requested"], p["ready"]):
            label.setStringValue_(f"Getting Hey Mint {version}…")
            bar.setIndeterminate_(True)
            bar.startAnimation_(None)
        else:                                   # ready, installed or failed: the page says it with its buttons
            self._stop_update_timer()
            self.refresh(keep_scroll=True)

    def _stop_update_timer(self) -> None:
        timer = getattr(self, "_upd_timer", None)
        if timer is not None:
            timer.invalidate()
        self._upd_timer = None

    def _restart(self) -> None:
        from mint.app import power
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(f"Restart {prefs.name()}?")
        alert.setInformativeText_("It stops everything it is doing and starts again in a few seconds. The "
                                  "conversation is kept.")
        alert.addButtonWithTitle_("Restart")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() == AppKit.NSAlertFirstButtonReturn:
            power.restart("Settings")

    def _check_updates(self, status) -> None:
        from mint.app import updater
        status.setStringValue_("Checking…")

        def run():
            words = updater.check(now=True)
            AppHelper.callAfter(lambda: (status.setStringValue_(words), self.refresh()))
        threading.Thread(target=run, daemon=True, name="settings-update").start()

    def _act(self, name: str) -> None:
        callback = self.actions.get(name)
        if callback:
            callback()
        AppHelper.callLater(0.5, self.refresh)

    # --- a page's facts, read off the main thread ----------------------------------------

    def _facts(self, key: str):
        """(True, facts) / (False, why the read failed) / None: still reading - the page shows "Loading…"
        and is built again when they arrive. Every build reads afresh (a refresh after a change shows
        the change); a read still under way, or one that just finished, is reused."""
        reader = getattr(self, f"_facts_{key}", None)
        if reader is None:
            return True, {}
        with self._jobs_lock:
            job = self._jobs.get(key)
            stale = job is not None and job["done"].is_set() and (
                job["used"] or time.monotonic() - job["finished"] > 5.0)
            if job is None or stale:
                job = self._start_read(key, reader)
        if not job["done"].wait(LOAD_WAIT):
            with self._jobs_lock:
                if not job["done"].is_set():
                    job["notify"] = True              # the reader rebuilds the page when it is done
                    return None
        job["used"] = True
        return job["result"]

    def _start_read(self, key: str, reader) -> dict:
        """Under _jobs_lock. The reader runs on its own thread; it must not touch views."""
        job = {"done": threading.Event(), "result": None, "used": False, "notify": False, "slow": False,
               "started": time.monotonic(), "finished": 0.0, "thread": None}
        self._jobs[key] = job

        def run() -> None:
            try:
                result = (True, reader())
            except Exception as error:
                log.exception("settings: reading the %s page failed", key)
                result = (False, f"{type(error).__name__}: {error}")
            with self._jobs_lock:
                job["result"], job["finished"] = result, time.monotonic()
                job["done"].set()
                notify = job["notify"]
            if job["slow"]:
                log.warning("settings: the %s page loaded after %.0f s", key, job["finished"] - job["started"])
            if notify:
                AppHelper.callAfter(self._arrived, key, job)
        job["thread"] = threading.Thread(target=run, daemon=True, name=f"settings-read-{key}")
        job["thread"].start()
        return job

    def _arrived(self, key: str, job: dict) -> None:
        if self.window is not None and key in self._shown_parts and self._jobs.get(key) is job and not job["used"]:
            self.refresh(keep_scroll=True)

    def _check_slow(self, key: str, job: dict) -> None:
        """LOAD_SLOW after "Loading…": still reading. Log where the read is stuck - the next report then
        names the culprit - and tell the user, with Try again."""
        if job["done"].is_set() or job["slow"] or self._jobs.get(key) is not job:
            return
        job["slow"] = True
        frame = sys._current_frames().get(job["thread"].ident)
        stack = "".join(traceback.format_stack(frame)) if frame is not None else "  (no Python frame)\n"
        log.warning("settings: the %s page is still loading after %.0f s; the read is here:\n%s", key,
                    time.monotonic() - job["started"], stack)
        if self.window is not None and key in self._shown_parts:
            self.refresh(keep_scroll=True)

    def _retry(self, key: str) -> None:
        """Try again: a new read (a stuck one carries on in the background; what it brings is dropped)."""
        with self._jobs_lock:
            self._jobs.pop(key, None)
        self.refresh()

    def _loading_card(self, page, key: str, title: str = "") -> None:
        job = self._jobs.get(key)
        slow = bool(job and job["slow"])
        page.section(title)
        card, top, _, h = page.row("", height=56)
        spinner = AppKit.NSProgressIndicator.alloc().initWithFrame_(AppKit.NSMakeRect(16, top + (h - 16) / 2, 16, 16))
        spinner.setStyle_(AppKit.NSProgressIndicatorStyleSpinning)
        spinner.setControlSize_(AppKit.NSControlSizeSmall)
        spinner.setIndeterminate_(True)
        spinner.startAnimation_(None)
        card.addSubview_(spinner)
        self._label(card, "Still loading…" if slow else "Loading…", 42, top + (h - 18) / 2, page.width - 60, size=13)
        if slow:
            page.text("Something this page reads is slow to answer. The rest of Mint keeps working, and the page "
                      "fills in by itself when the answer comes.", size=11, alpha=0.6)
            self._row_buttons(page, "", [("Try again", 100, lambda: self._retry(key)),
                                         ("Open log", 100, _open_log)])
        page.end()
        if job is not None and not slow:
            wait = max(0.5, LOAD_SLOW - (time.monotonic() - job["started"]))
            AppHelper.callLater(wait, self._check_slow, key, job)

    def _problem_card(self, page, key: str, why: str) -> None:
        page.section()
        page.text(f"This page couldn't load: {why[:300]}", size=12, alpha=0.8)
        self._row_buttons(page, "", [("Try again", 100, lambda: self._retry(key)), ("Open log", 100, _open_log)])
        page.end("The details are in the log. The rest of Mint keeps working.")

    def _build_page(self, column, width: float) -> _Page:
        """The page's facts (or "Loading…", or what went wrong), then its views."""
        key = self.page_key
        self._shown_parts = {key}
        self._anchors = {}
        self._perm_seen = None                  # set again by a permissions section the page shows
        page = _Page(self, column, width)
        facts = self._facts(key)
        if facts is None:
            self._loading_card(page, key)
            return page
        ok, value = facts
        if not ok:
            self._problem_card(page, key, value)
            return page
        try:
            getattr(self, f"_page_{key}")(page, value)
        except Exception as error:
            log.exception("settings: building the %s page failed", key)
            for view in list(column.subviews()):
                view.removeFromSuperview()
            page = _Page(self, column, width)
            self._problem_card(page, key, f"{type(error).__name__}: {error}")
        return page

    def _background(self, label: str, work, then=None) -> None:
        """Run `work()` on a thread (it may talk to the network or another app); then(result) on the main
        thread if the window is still open. A failure is logged and handed to `then` as the text."""
        def run() -> None:
            try:
                result = work()
            except Exception as error:
                log.exception("settings: %s failed", label)
                result = f"Couldn't: {error}"
            if then is not None:
                AppHelper.callAfter(lambda: self.window is not None and then(result))
        threading.Thread(target=run, daemon=True, name=f"settings-{label}").start()

    # --- window ------------------------------------------------------------------------

    def show(self, page: str | None = None) -> None:
        """Main thread. `page`: a page key, or an old one ("audio", "apple_shortcuts", "connectors")."""
        if page:
            self.page_key = page_for(page)
        if self.window is not None:
            self.refresh()
            self._front()
            return
        self.target = _SettingsTarget.alloc().initWithOwner_(self)
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable | AppKit.NSWindowStyleMaskFullSizeContentView)
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H), style, AppKit.NSBackingStoreBuffered, False)
        window.setTitle_(f"{prefs.name()} Settings")
        window.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        window.setTitlebarAppearsTransparent_(True)
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self.target)
        window.center()
        self.window = window
        self._fill()
        self._front()
        self._perm_gen += 1                     # watch the permissions while General or Permissions & Privacy shows
        self._perm_busy = False
        AppHelper.callLater(PERM_EVERY, self._perm_watch, self._perm_gen)

    def _fill(self) -> None:
        content = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        side = AppKit.NSVisualEffectView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, SIDEBAR, H))
        side.setMaterial_(AppKit.NSVisualEffectMaterialSidebar)
        side.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        content.addSubview_(side)
        items = _SettingsFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, SIDEBAR, H))
        side.addSubview_(items)
        accent = AppKit.NSColor.controlAccentColor()
        y = 52
        for key, title, icon in PAGES:
            item = _SettingsSideItem.alloc().initWithFrame_(AppKit.NSMakeRect(10, y, SIDEBAR - 20, 30))
            item.owner, item.key = self, key
            item.setWantsLayer_(True)
            item.layer().setCornerRadius_(7)
            on = key == self.page_key
            if on:
                item.layer().setBackgroundColor_(_cgc(accent))
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(icon, None)
            glyph = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(10, 6, 18, 18))
            if image is not None:
                glyph.setImage_(image)
            glyph.setContentTintColor_(AppKit.NSColor.whiteColor() if on else accent)
            item.addSubview_(glyph)
            label = self._label(item, title, 36, 6, SIDEBAR - 20 - 36 - 4, h=18, size=13)
            label.setTextColor_(AppKit.NSColor.whiteColor() if on else AppKit.NSColor.labelColor())
            items.addSubview_(item)
            y += 32

        # The page: its title, then a scrolling column of cards.
        body_w = W - SIDEBAR
        self.page_key = page_for(self.page_key)
        title = dict((k, t) for k, t, _ in PAGES)[self.page_key]
        self._label(content, title, SIDEBAR + PAD, H - 58, body_w - 2 * PAD - 220, h=28, size=20, bold=True)
        if self.page_key in ADVANCED_PAGES:
            self._advanced_switch(content, W - PAD - 200, H - 56, 200)
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(SIDEBAR, 0, body_w, H - 70))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        doc = _SettingsFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, body_w, 10))
        column = _SettingsFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(PAD, 8, body_w - 2 * PAD, 10))
        doc.addSubview_(column)
        page = self._build_page(column, body_w - 2 * PAD - 4)
        column.setFrame_(AppKit.NSMakeRect(PAD, 8, body_w - 2 * PAD, page.y))
        doc.setFrame_(AppKit.NSMakeRect(0, 0, body_w, max(page.y + 24, H - 70)))
        scroll.setDocumentView_(doc)
        content.addSubview_(scroll)
        self._scroll = scroll
        self.window.setContentView_(content)

    def _advanced_switch(self, view, x: float, y: float, w: float):
        """Top right of a page: Essentials | All settings - the "settings_advanced" pref, for every page."""
        switch = AppKit.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Essentials", "All settings"], AppKit.NSSegmentSwitchTrackingSelectOne, None, None)
        switch.setFrame_(AppKit.NSMakeRect(x, y, w, 24))
        switch.setSegmentDistribution_(AppKit.NSSegmentDistributionFillEqually)
        switch.setSelectedSegment_(1 if self._advanced() else 0)
        switch.setToolTip_("All settings: the technical ones too, on every page.")
        self._on(switch, lambda c: self._set_advanced(c.selectedSegment() == 1))
        view.addSubview_(switch)
        self._advanced_control = switch
        return switch

    def select(self, key: str, anchor: str = "") -> None:
        """Open a page (old keys work too); `anchor`: scroll to that part of it - and open it if it is a card that
        starts closed (a connector's Settings… for Telegram)."""
        cancel_recording()
        self.page_key = page_for(key)
        self._reveal = anchor
        if anchor in ("telegram", "email"):
            self._opened.add(anchor)
        self.refresh()

    def refresh(self, keep_scroll: bool = False) -> None:
        """Rebuild the contents (after training, forgetting, a device change, a new page). keep_scroll: stay where
        the page was scrolled to (a button in the middle of it)."""
        if self.window is None:
            return
        scroll = getattr(self, "_scroll", None)
        y = scroll.contentView().bounds().origin.y if keep_scroll and scroll is not None else 0.0
        pending = self.__dict__.pop("_pending_scroll", 0.0)
        if keep_scroll:
            y = max(y, pending)                       # a rebuild while the page was still loading
        self._handlers.clear()
        self._ended_handlers.clear()
        self._fill()
        if self._reveal and self._reveal in self._anchors:          # select(page, anchor=…): there, once built
            y, self._reveal = max(1.0, self._anchors[self._reveal]), ""
        if y and self._scroll is not None:
            clip = self._scroll.contentView()
            doc_h = self._scroll.documentView().frame().size.height
            if doc_h - clip.bounds().size.height < y:
                self._pending_scroll = y          # still short ("Loading…"): the next rebuild goes back there
            y = min(y, max(0.0, doc_h - clip.bounds().size.height))
            clip.scrollToPoint_(AppKit.NSMakePoint(0, y))
            self._scroll.reflectScrolledClipView_(clip)
        self.window.setTitle_(f"{prefs.name()} Settings")

    def _front(self) -> None:
        # An accessory app has to activate itself for its window to take typing.
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def _closed(self) -> None:
        cancel_recording()
        self.window = None


def _open_log() -> None:
    from mint.app import report
    _open_file(report.LOG)


def _open(url: str) -> None:
    AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(url))


def _open_file(path) -> None:
    AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.fileURLWithPath_(str(path)))


def _write_env(name: str, value: str) -> None:
    """Set (or, empty, remove) NAME=value in Mint's .env, keeping every other line; mode 600."""
    from pathlib import Path
    from mint.core import config
    path = Path(config.__file__).resolve().parent.parent / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    kept = [line for line in lines
            if line.strip().removeprefix("export ").split("=", 1)[0].strip() != name]
    if value:
        kept.append(f"{name}={value}")
    path.write_text("\n".join(kept) + "\n")
    os.chmod(path, 0o600)


_recorder = [None]


def record_keys(modifier_ok: bool, prompt, done) -> None:
    """Main thread. Wait for the next key combination (or, when `modifier_ok`, one modifier key pressed
    and let go on its own, like Right ⌥). prompt(text) shows what to press; done(value) gets the
    shortcut ("ctrl+option+space", "right_option") or None on Esc. Used by Settings and onboarding."""
    from mint.tools import fastinput
    if _recorder[0] is not None:
        AppKit.NSEvent.removeMonitor_(_recorder[0])
    prompt("Press the keys…")
    names = {code: name for name, code in fastinput.KEYS.items()}
    pending = {"modifier": None}
    sides = {61: "right_option", 54: "right_command", 62: "right_control", 60: "right_shift", 63: "fn"}

    def finish(value):
        if _recorder[0] is not None:
            AppKit.NSEvent.removeMonitor_(_recorder[0])
        _recorder[0] = None
        done(value)

    def handle(event):
        kind = event.type()
        flags = int(event.modifierFlags())
        if kind == AppKit.NSEventTypeKeyDown:
            code = int(event.keyCode())
            if code == 53:                                             # Esc
                finish(None)
                return None
            mods = [name for name, bit in (("ctrl", 1 << 18), ("option", 1 << 19), ("shift", 1 << 17),
                                           ("cmd", 1 << 20)) if flags & bit]
            name = names.get(code)
            if name and (mods or name.startswith("f")):
                finish("+".join(mods + [name]))
            else:
                prompt("Add ⌘, ⌥, ⌃ or ⇧…")
            pending["modifier"] = None
            return None
        if kind == AppKit.NSEventTypeFlagsChanged and modifier_ok:
            code = int(event.keyCode())
            if code in sides:
                if pending["modifier"] is None:
                    pending["modifier"] = sides[code]
                elif pending["modifier"] == sides[code]:
                    finish(sides[code])                                # pressed and let go on its own
        return event
    mask = AppKit.NSEventMaskKeyDown | AppKit.NSEventMaskFlagsChanged
    _recorder[0] = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, handle)


def cancel_recording() -> None:
    if _recorder[0] is not None:
        AppKit.NSEvent.removeMonitor_(_recorder[0])
        _recorder[0] = None
