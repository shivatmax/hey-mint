"""Settings: one window for who you are, your voice, audio devices and looks.

Everything here writes settings.json through prefs, so changes apply the same
way as a menu click or a hand edit: listeners in main.py react (audio devices
rebuild the engine, a new name trains its wake word, and so on).

Tabs
  You         assistant name, your name, about you
  Your voice  train / retrain / forget your voice, voice lock, strictness,
              ignore talk not meant for the assistant
  <Name>'s voice  which of the 30 Gemini voices it speaks in (with previews),
              speaking style
  Audio       microphone, speaker, echo cancellation, stepping aside for calls
  Appearance  theme, position, spoken replies, face, effects, captions
"""

from __future__ import annotations

import AppKit
import objc
from PyObjCTools import AppHelper

from mint.voice import devices as audio_devices
from mint.core import prefs
from mint.voice import voicelock
from mint.voice import voices

W, H = 600, 470
BAR = 46          # the strip under the tabs: start at login, Quit
ECHO = [("auto", "Automatic - off with headphones, on with speakers"),
        ("on", "Always on - talk over it on speakers; other apps get a little quieter"),
        ("off", "Off - never touches other apps' volume; mic closes while it speaks")]
STRICTNESS = [("relaxed", "Relaxed - fewer missed words, slightly easier to fool"),
              ("balanced", "Balanced (recommended)"),
              ("strict", "Strict - nobody else gets through; may need you to speak clearly")]
THEMES = [("mint", "Mint"), ("blue", "Classic blue"), ("aurora", "Aurora"), ("sunset", "Sunset"),
          ("rose", "Rose"), ("mono", "Mono")]
STYLES = [("", "Natural (no style)"), ("warm and calm", "Warm and calm"),
          ("cheerful and energetic", "Cheerful and energetic"), ("slow and clear", "Slow and clear"),
          ("brisk and to the point", "Brisk and to the point"), ("soft and gentle", "Soft and gentle"),
          ("professional and confident", "Professional and confident"),
          ("friendly, with an Indian English accent", "Friendly, Indian English accent")]
VOICE_FILTERS = [("all", "All 30 voices"), ("female", "Female voices"), ("male", "Male voices")]
UNLOAD = [(0, "Never - always stay loaded (recommended)"), (5, "After 5 minutes asleep (experimental)"),
          (10, "After 10 minutes asleep (experimental)"), (20, "After 20 minutes asleep (experimental)"),
          (30, "After 30 minutes asleep (experimental)"), (60, "After an hour asleep (experimental)")]
POSITIONS = [("top-right", "Top right"), ("top-left", "Top left"), ("top-center", "Top centre"),
             ("bottom-right", "Bottom right"), ("bottom-left", "Bottom left"), ("custom", "Where I dragged it")]


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


class SettingsWindow:
    def __init__(self, actions: dict) -> None:
        """actions: callbacks by name - "train_voice", "forget_voice", "audio_status"."""
        self.actions = actions
        self.window = None
        self._handlers: dict[int, callable] = {}
        self._ended_handlers: dict[int, callable] = {}      # text fields: Enter or leaving the field

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
        field.setFont_(AppKit.NSFont.boldSystemFontOfSize_(size) if bold else AppKit.NSFont.systemFontOfSize_(size))
        field.setTextColor_(AppKit.NSColor.labelColor().colorWithAlphaComponent_(alpha))
        field.setMaximumNumberOfLines_(lines)
        field.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        view.addSubview_(field)
        return field

    def _text(self, view, key, x, y, w, placeholder="", settle: float = 0.0):
        """A text field saved as it is typed - or, with `settle`, only on Enter,
        on leaving the field, or `settle` seconds after the typing stops. The
        speaking style uses that: every save reconnects the live session, and
        saving per keystroke reconnected on each letter."""
        field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 24))
        field.setStringValue_(str(prefs.get(key) or ""))
        field.setPlaceholderString_(placeholder)
        field.setDelegate_(self.target)

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
        popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(AppKit.NSMakeRect(x, y, w, 26), False)
        for _, title in options:
            popup.addItemWithTitle_(title)
        values = [v for v, _ in options]
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

    def _check(self, view, key, title, x, y, w):
        box = AppKit.NSButton.checkboxWithTitle_target_action_(title, None, None)
        box.setFrame_(AppKit.NSMakeRect(x, y, w, 20))
        box.setState_(AppKit.NSControlStateValueOn if prefs.get(key) else AppKit.NSControlStateValueOff)
        self._on(box, lambda c: prefs.set(key, c.state() == AppKit.NSControlStateValueOn))
        view.addSubview_(box)
        return box

    def _button(self, view, title, x, y, w, handler):
        button = AppKit.NSButton.buttonWithTitle_target_action_(title, None, None)
        button.setBezelStyle_(AppKit.NSBezelStyleRounded)
        button.setFrame_(AppKit.NSMakeRect(x, y, w, 30))
        self._on(button, lambda c: handler())
        view.addSubview_(button)
        return button

    # --- tabs ------------------------------------------------------------------------

    def _tab_you(self, view) -> None:
        name = prefs.name()
        self._label(view, "Assistant", 24, 350, 200, bold=True, size=13)
        self._label(view, "Name", 24, 318, 110)
        self._text(view, "assistant_name", 140, 314, 220, "Mint")
        self.wake_note = self._label(view, f"Wake word: “Hey {name}”. A new name gets its own wake word "
                                     "(about a minute to set up); then retrain your voice for it.",
                                     140, 270, 400, h=36, size=11, alpha=0.65, lines=2)
        self._label(view, "You", 24, 232, 200, bold=True, size=13)
        self._label(view, "Your name", 24, 200, 110)
        self._text(view, "user_name", 140, 196, 220, "What should it call you?")
        self._label(view, "About you", 24, 164, 110)
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(140, 60, 420, 124))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(AppKit.NSBezelBorder)
        about = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 400, 124))
        about.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        about.setString_(str(prefs.get("about_me") or ""))
        about.setDelegate_(self.target)
        self._handlers[objc.pyobjc_id(about)] = lambda c: prefs.set("about_me", str(c.string()).strip())
        scroll.setDocumentView_(about)
        view.addSubview_(scroll)
        self._label(view, "Your work, the people and projects you mention, how you like answers - it is "
                          "given to the assistant at the start of every conversation.",
                    140, 22, 420, h=32, size=11, alpha=0.65, lines=2)

    def _tab_voice(self, view) -> None:
        lock = voicelock.lock
        if lock.stale:
            status = "Your voice needs retraining (the voice model was upgraded)."
        elif lock.enrolled:
            status = f"Trained {lock.enrolled_at}. Only your voice gets through while the voice lock is on."
        else:
            status = "Not trained yet - anyone's voice can wake and talk to the assistant."
        self.voice_status = self._label(view, status, 24, 360, 540, h=36, size=12, lines=2)
        self._button(view, "Retrain my voice…" if lock.enrolled or lock.stale else "Train my voice…",
                     24, 318, 180, lambda: self._act("train_voice"))
        if lock.enrolled:
            self._button(view, "Forget my voice", 212, 318, 150, lambda: self._act("forget_voice"))
        self._label(view, (f"Training takes about two minutes in a quiet room, at your usual distance from "
                           f"the Mac: say “Hey {prefs.name()}” eight times, then read eight short sentences. "
                           "Then a live test marks everything said as “that's you” or “not you”."),
                    24, 256, 540, h=48, size=11, alpha=0.65, lines=3)
        self._check(view, "voice_lock", "Voice lock - only my voice reaches the assistant", 24, 218, 520)
        self._label(view, "Strictness", 24, 184, 110)
        self._popup(view, "lock_strictness", STRICTNESS, 140, 178, 420)
        self._check(view, "addressee_check", f"Ignore things I say to other people (not to {prefs.name()})",
                    24, 138, 520)
        self._label(view, "Tip: if it misses you often, retrain where you usually sit, or choose Relaxed. "
                          "If someone else gets through, choose Strict.",
                    24, 90, 540, h=32, size=11, alpha=0.65, lines=2)

    def _tab_speaking(self, view) -> None:
        from mint.core import config
        name = prefs.name()
        current = prefs.get("voice_name") or config.VOICE
        self._label(view, f"How {name} sounds", 24, 360, 400, bold=True, size=13)
        self._label(view, "Show", 24, 324, 110)
        shown = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(AppKit.NSMakeRect(140, 318, 200, 26), False)
        for _, title in VOICE_FILTERS:
            shown.addItemWithTitle_(title)
        view.addSubview_(shown)
        self._label(view, "Voice", 24, 290, 110)
        picker = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(AppKit.NSMakeRect(140, 284, 300, 26), False)
        view.addSubview_(picker)
        listed: list[str] = []

        def fill(kind: str) -> None:
            listed[:] = [v for v, g, _ in voices.catalog() if kind == "all" or g == kind]
            chosen = prefs.get("voice_name") or config.VOICE
            if chosen not in listed:                     # keep the current one visible
                listed.insert(0, chosen)
            picker.removeAllItems()
            for voice in listed:
                picker.addItemWithTitle_(voices.label(voice) + ("   (current)" if voice == chosen else ""))
            picker.selectItemAtIndex_(listed.index(chosen))

        fill("all")
        self._on(shown, lambda c: fill(VOICE_FILTERS[c.indexOfSelectedItem()][0]))

        def picked(control) -> None:
            prefs.set("voice_name", listed[control.indexOfSelectedItem()])
            fill(VOICE_FILTERS[shown.indexOfSelectedItem()][0])
            status.setStringValue_(f"Switching {name} to {prefs.get('voice_name')}… it applies in a few "
                                   "seconds, once it has finished speaking.")
        self._on(picker, picked)

        self._label(view, "Style", 24, 246, 110)
        style = self._text(view, "speaking_style", 140, 242, 300, "e.g. warm and calm, a little faster",
                           settle=3.0)
        presets = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(AppKit.NSMakeRect(448, 241, 112, 26), True)
        presets.addItemWithTitle_("Presets")
        for _, title in STYLES:
            presets.addItemWithTitle_(title)
        view.addSubview_(presets)

        def preset(control) -> None:
            value = STYLES[control.indexOfSelectedItem() - 1][0]
            style.setStringValue_(value)
            prefs.set("speaking_style", value)
        self._on(presets, preset)

        def listen() -> None:
            voice = listed[picker.indexOfSelectedItem()]
            status.setStringValue_(f"Getting a sample of {voice}… (a few seconds the first time)")
            button.setEnabled_(False)

            def done(error) -> None:
                button.setEnabled_(True)
                status.setStringValue_(f"Could not play a sample: {error[:120]}" if error
                                       else f"Playing {voice}" + (f", {str(style.stringValue()).strip()}"
                                                                  if str(style.stringValue()).strip() else "") + ".")
            voices.preview(voice, str(style.stringValue()).strip(), done)
        button = self._button(view, "▶ Listen", 448, 282, 112, listen)
        status = self._label(view, "", 140, 200, 420, h=32, size=11, alpha=0.75, lines=2)
        self._label(view, f"These are the 30 voices Gemini can speak in live conversation. Style is "
                          f"how {name} talks - pace, mood, accent - and applies to every voice. Changes "
                          "apply in a few seconds without losing the conversation.",
                    24, 128, 536, h=48, size=11, alpha=0.65, lines=3)
        self._label(view, f'You can also just say: "Hey {name}, use a male voice", "switch to Puck", '
                          '"talk slower", "sound more cheerful", "what voices do you have?"',
                    24, 84, 536, h=32, size=11, alpha=0.65, lines=2)

    def _devices(self, output: bool) -> list[tuple[str, str]]:
        found = [("", "System default")]
        for d in audio_devices.devices():
            if (d["outputs"] if output else d["inputs"]) and d["transport"] != "aggregate":
                found.append((d["uid"], f"{d['name']}  ({d['transport']})"))
        return found

    def _tab_audio(self, view) -> None:
        self._label(view, "Microphone", 24, 360, 110)
        self._popup(view, "input_device", self._devices(False), 140, 354, 420)
        self._label(view, "Speaker", 24, 324, 110)
        self._popup(view, "output_device", self._devices(True), 140, 318, 420)
        self._label(view, "Echo cancellation", 24, 288, 116)
        self._popup(view, "echo_cancellation", ECHO, 140, 282, 420)
        self._check(view, "share_mic", "Step aside while a call or meeting uses the mic (Zoom, Meet, Teams…)",
                    24, 240, 540)
        self.mic_users = self._label(view, "", 44, 196, 520, h=36, size=11, alpha=0.7, lines=2)
        self._button(view, "Refresh", 24, 150, 100, self._refresh_audio)
        self._button(view, "Also step aside for these apps", 132, 150, 240, self._add_mic_apps)
        self.audio_status = self._label(view, "", 24, 96, 540, h=40, size=11, alpha=0.7, lines=2)
        self._label(view, f"{prefs.name()} picks changes up at once; the microphone restarts in about a second.",
                    24, 60, 540, size=11, alpha=0.5)
        self._refresh_audio()

    def _refresh_audio(self) -> None:
        users = audio_devices.mic_users()
        self._seen = users
        self.mic_users.setStringValue_(
            "Using the microphone now: " + ", ".join(sorted({u["name"] for u in users})) if users
            else "No other app is using the microphone right now.")
        status = self.actions.get("audio_status")
        self.audio_status.setStringValue_(f"Now: {status()}" if status else "")

    def _add_mic_apps(self) -> None:
        extra = list(prefs.get("share_mic_apps") or [])
        for user in getattr(self, "_seen", []):
            if user["bundle"] and user["bundle"] not in extra:
                extra.append(user["bundle"])
        prefs.set("share_mic_apps", extra)
        self.mic_users.setStringValue_("Will also step aside for: " + ", ".join(extra) if extra else
                                       "No other app is using the microphone right now.")

    def _tab_looks(self, view) -> None:
        self._label(view, "Theme", 24, 360, 110)
        self._popup(view, "theme", THEMES, 140, 354, 240)
        self._label(view, "Position", 24, 324, 110)
        self._popup(view, "position", POSITIONS, 140, 318, 240)
        self._check(view, "voice", "Spoken replies (off: replies appear as text only)", 24, 276, 520)
        self._check(view, "face", "Face on the orb", 24, 246, 520)
        self._check(view, "cursor_effects", "On-screen effects (sparks, ripples, highlights)", 24, 216, 520)
        self._check(view, "word_animation", "Word-by-word captions", 24, 186, 520)
        self._check(view, "listen_while_working", "Keep listening while it works", 24, 156, 520)
        self._label(view, "Free memory", 24, 116, 110)
        self._popup(view, "unload_after_minutes", UNLOAD, 140, 110, 300)
        self._label(view, "Asleep this long, Mint unloads (~270 MB freed) and a small listener (~35 MB) keeps "
                          "the orb, ⌘J and \u201cHey Mint\u201d. Waking it takes ~1 s more; what you say meanwhile is kept.",
                    140, 64, 420, h=40, size=11, alpha=0.65, lines=3)

    def _tab_brain(self, view) -> None:
        """Skills & Memory: counts here; seeing and editing them is its own, bigger window."""
        from mint.ui import brain as brain_window
        from mint.knowledge import memory as membank
        from mint.knowledge import skills as skillbook
        skills = skillbook.all_skills()
        blocks = membank.blocks()
        fixed = sum(1 for b in blocks if b.get("pinned"))
        reliable = sum(1 for s in skills if skillbook.rank(s) == "reliable")
        self._label(view, "Skills", 24, 360, 200, bold=True, size=13)
        self._label(view, f"{len(skills)} learned how-tos ({reliable} reliable). Jev picks the right one "
                          "for each request; they improve as they are used.", 24, 318, 520, h=36, lines=2, alpha=0.8)
        self._button(view, "See and edit skills…", 24, 280, 200, lambda: brain_window.open_window("skills"))
        self._label(view, "Memory", 24, 230, 200, bold=True, size=13)
        self._label(view, f"{len(blocks)} facts, {fixed} fixed (given to every conversation). The rest are "
                          "looked up only when a question needs them.", 24, 188, 520, h=36, lines=2, alpha=0.8)
        self._button(view, "See and edit memory…", 24, 150, 200, lambda: brain_window.open_window("memory"))
        self._label(view, 'You can also just ask: "show me your skills", "what do you remember about me?", '
                          '"forget that", "remember that my manager is Meera".', 24, 90, 520, h=36, lines=2,
                    alpha=0.6, size=11)

    def _act(self, name: str) -> None:
        callback = self.actions.get(name)
        if callback:
            callback()
        AppHelper.callLater(0.5, self.refresh)

    # --- window ------------------------------------------------------------------------

    def show(self) -> None:
        """Main thread."""
        if self.window is not None:
            self.refresh()
            self._front()
            return
        self.target = _SettingsTarget.alloc().initWithOwner_(self)
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable)
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H + BAR), style, AppKit.NSBackingStoreBuffered, False)
        window.setTitle_(f"{prefs.name()} Settings")
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self.target)
        window.center()
        self.window = window
        self._fill()
        self._front()

    def _fill(self) -> None:
        tabs = AppKit.NSTabView.alloc().initWithFrame_(AppKit.NSMakeRect(8, 8 + BAR, W - 16, H - 16))
        for title, build in (("You", self._tab_you), ("Your voice", self._tab_voice),
                             (f"{prefs.name()}'s voice", self._tab_speaking),
                             ("Audio", self._tab_audio), ("Appearance", self._tab_looks),
                             ("Skills & Memory", self._tab_brain)):
            item = AppKit.NSTabViewItem.alloc().initWithIdentifier_(title)
            item.setLabel_(title)
            view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W - 32, H - 60))
            build(view)
            item.setView_(view)
            tabs.addTabViewItem_(item)
        self.tabs = tabs
        content = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H + BAR))
        content.addSubview_(tabs)
        self._power_bar(content)
        self.window.setContentView_(content)

    def _power_bar(self, view) -> None:
        """Under the tabs, on every tab: start at login, and Quit (everything, fully off)."""
        from mint.app import power
        line = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(16, BAR + 2, W - 32, 1))
        line.setBoxType_(AppKit.NSBoxSeparator)
        view.addSubview_(line)
        login = AppKit.NSButton.checkboxWithTitle_target_action_(f"Start {prefs.name()} when I log in", None, None)
        login.setFrame_(AppKit.NSMakeRect(20, 12, 260, 20))
        login.setState_(AppKit.NSControlStateValueOn if power.starts_at_login() else AppKit.NSControlStateValueOff)
        self._on(login, lambda c: power.set_start_at_login(c.state() == AppKit.NSControlStateValueOn))
        view.addSubview_(login)
        self._label(view, "Stops everything it started too.", W - 390, 13, 200, size=11, alpha=0.55)
        quit_button = self._button(view, f"Quit {prefs.name()}", W - 180, 6, 160,
                                   lambda: power.quit_fully(reason="Settings"))
        quit_button.setKeyEquivalent_("q")
        quit_button.setKeyEquivalentModifierMask_(AppKit.NSEventModifierFlagCommand)
        if hasattr(quit_button, "setHasDestructiveAction_"):       # macOS 11+: drawn as a destructive action
            quit_button.setHasDestructiveAction_(True)

    def refresh(self) -> None:
        """Rebuild the contents (after training, forgetting, a device change)."""
        if self.window is None:
            return
        selected = self.tabs.indexOfTabViewItem_(self.tabs.selectedTabViewItem())
        self._handlers.clear()
        self._ended_handlers.clear()
        self._fill()
        self.tabs.selectTabViewItemAtIndex_(max(0, selected))
        self.window.setTitle_(f"{prefs.name()} Settings")

    def _front(self) -> None:
        # An accessory app has to activate itself for its window to take typing.
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def _closed(self) -> None:
        self.window = None
