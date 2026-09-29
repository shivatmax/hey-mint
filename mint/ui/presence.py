"""Mint's on-screen presence: a menu bar item, the floating HUD, and the
on-screen effects.

The Cocoa run loop owns the main thread; the asyncio session runs on a worker
thread and calls into `Presence`, which hops to the main thread as needed.
"""

from __future__ import annotations

import logging
import subprocess
import threading

import AppKit
import objc
from PyObjCTools import AppHelper

from mint.ui import effects
from mint.core import hotkeys
from mint.core import prefs
from mint.ui.hud import HUD

log = logging.getLogger("mint.ui.presence")

# state -> menu bar glyph
GLYPHS = {
    "starting": "◌", "sleeping": "○", "awake": "●", "thinking": "◐",
    "working": "◈", "speaking": "◉", "paused": "⏸", "offline": "⊘",
}
LABELS = {
    "starting": "Starting…", "sleeping": "Asleep — say the wake word",
    "awake": "Listening", "thinking": "Thinking…", "working": "Working on the screen",
    "speaking": "Speaking", "paused": "Microphone off", "offline": "Disconnected",
}
THEME_NAMES = {"mint": "Mint", "blue": "Classic blue", "aurora": "Aurora", "sunset": "Sunset",
               "rose": "Rose", "mono": "Mono"}
POSITION_NAMES = {"top-right": "Top right", "top-left": "Top left", "top-center": "Top centre",
                  "bottom-right": "Bottom right", "bottom-left": "Bottom left"}
TOGGLES = (("face", "Face on the orb"), ("word_animation", "Word-by-word captions"),
           ("cursor_effects", "On-screen effects (sparks, ripples, highlights)"),
           ("listen_while_working", "Listen while working"),
           ("cute_agents", "Cute critter helpers (sub-agents)"),
           ("cute_effects", "Cute action flourishes"),
           ("auto_emotions", "Expressions during conversation"),
           ("share_visible", "Visible in screen sharing"))


class _Handler(AppKit.NSObject):
    """Target for menu items, and delegate that refreshes the menu as it opens."""

    def initWithOwner_(self, owner):
        self = objc.super(_Handler, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def fire_(self, sender):
        self.owner._menu_action(str(sender.representedObject()))

    def menuNeedsUpdate_(self, menu):
        self.owner._fill_menu(menu)

    def poll_(self, timer):
        prefs.check_file()


class Presence:
    """Menu bar item, HUD and effects. Safe to call from any thread."""

    def __init__(self, wake_word: str = "hey_mint", hands_free: bool = True,
                 show_hud: bool = True, log_path: str | None = None) -> None:
        self.wake_word = wake_word.replace("_", " ")
        self.hands_free = hands_free
        self.hud = HUD() if show_hud else None
        self.log_path = log_path
        self.callbacks: dict[str, callable] = {}
        self._item = None
        self._state = "starting"
        self._note = ""
        self._hotkeys = None

    # --- construction (main thread) ------------------------------------------

    def build(self) -> None:
        bar = AppKit.NSStatusBar.systemStatusBar()
        self._item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self._item.button().setTitle_(GLYPHS["starting"])
        self._item.button().setToolTip_(prefs.name())

        self._handler = _Handler.alloc().initWithOwner_(self)
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self._handler)
        self._fill_menu(menu)
        self._item.setMenu_(menu)
        self.callbacks.setdefault("log", self._open_log)
        self.callbacks.setdefault("settings", self._open_settings)

        if self.hud is not None:
            self.hud.fire = self.fire
            self.hud.menu_factory = self._settings_menu
            self.hud.build()
        effects.fx.build()
        self.register_shortcuts()
        # Hand edits to settings.json apply within two seconds.
        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, self._handler, "poll:", None, True)

    def register_shortcuts(self) -> None:
        """(Re)register the global shortcuts from settings.json."""
        if self._hotkeys is None:
            self._hotkeys = hotkeys.HotKeys()
        self._hotkeys.clear()
        keys = prefs.get("shortcuts")
        if self._hotkeys.register(keys.get("toggle", ""), lambda: self.fire("console")):
            print(f"  [shortcut: {hotkeys.display(keys['toggle'])} opens and closes {prefs.name()}]", flush=True)
        if keys.get("close"):
            self._hotkeys.register(keys["close"], lambda: self.fire("dismiss"))
        if keys.get("talk") and self._hotkeys.register(keys["talk"], lambda: self.fire("wake")):
            print(f"  [shortcut: {hotkeys.display(keys['talk'])} - talk to {prefs.name()} without the wake word]",
                  flush=True)
        if keys.get("clipboard"):
            from mint.ui import clipboard_window
            if self._hotkeys.register(keys["clipboard"], clipboard_window.window.toggle):
                print(f"  [shortcut: {hotkeys.display(keys['clipboard'])} opens the clipboard]", flush=True)
        self._register_dictation(keys)

    def _register_dictation(self, keys: dict) -> None:
        """Dictation (dictation.py): hold the key to dictate at the cursor, tap it twice for hands-free."""
        from mint.voice import dictation
        if getattr(self, "_dictation_hold", None) is not None:
            self._dictation_hold.stop()
            self._dictation_hold = None
        key = str(keys.get("dictate") or "")
        if key in hotkeys.MODIFIER_KEYS:
            self._dictation_hold = hotkeys.ModifierHold(key, dictation.start, dictation.finish, dictation.toggle)
            self._dictation_hold.start()
            print(f"  [shortcut: hold {hotkeys.display(key)} to dictate, tap it twice for hands-free]", flush=True)
        elif key and self._hotkeys.register(key, dictation.start, on_release=dictation.finish):
            print(f"  [shortcut: hold {hotkeys.display(key)} to dictate]", flush=True)
        if keys.get("dictate_toggle"):
            self._hotkeys.register(keys["dictate_toggle"], dictation.toggle)

    # --- menus --------------------------------------------------------------------

    def _row(self, menu, title, action=None, enabled=True, key="", checked=None):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, "fire:" if action else None, key)
        item.setEnabled_(enabled)
        if action:
            item.setTarget_(self._handler)
            item.setRepresentedObject_(action)
        if checked is not None:
            item.setState_(AppKit.NSControlStateValueOn if checked else AppKit.NSControlStateValueOff)
        menu.addItem_(item)
        return item

    def _submenu(self, menu, title):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        sub = AppKit.NSMenu.alloc().initWithTitle_(title)
        sub.setAutoenablesItems_(False)
        item.setSubmenu_(sub)
        menu.addItem_(item)
        return sub

    def _add_settings(self, menu) -> None:
        """Microphone, voice and appearance: shared by the menu bar and the HUD."""
        self._row(menu, "Microphone", "toggle:mic", checked=bool(prefs.get("mic")))
        self._row(menu, "Spoken replies", "toggle:voice", checked=bool(prefs.get("voice")))
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        from mint.voice import voicelock
        enrolled = voicelock.lock.enrolled
        self._row(menu, "Retrain my voice…" if enrolled or voicelock.lock.stale else "Train my voice…", "train_voice")
        if voicelock.lock.stale:
            self._row(menu, "Voice model upgraded - please retrain", enabled=False)
        if enrolled:
            self._row(menu, "Voice lock - only my voice", "toggle:voice_lock", checked=bool(prefs.get("voice_lock")))
            self._row(menu, "Forget my voice", "forget_voice")
        self._row(menu, f"Ignore talk not meant for {prefs.name()}", "toggle:addressee_check",
                  checked=bool(prefs.get("addressee_check")))
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        themes = self._submenu(menu, "Theme")
        for key, name in THEME_NAMES.items():
            self._row(themes, name, f"theme:{key}", checked=prefs.get("theme") == key)
        places = self._submenu(menu, "Position")
        for key, name in POSITION_NAMES.items():
            self._row(places, name, f"position:{key}", checked=prefs.get("position") == key)
        if prefs.get("position") == "custom":
            self._row(places, "Where I dragged it", None, enabled=False, checked=True)
        for key, name in TOGGLES:
            self._row(menu, name, f"toggle:{key}", checked=bool(prefs.get(key)))
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._row(menu, "Settings…", "open_settings", key=",")
        self._row(menu, "Skills & memory…", "brain:skills")
        self._row(menu, "Edit the settings file…", "settings")
        self._row(menu, "Accounts, aliases and routines…", "customise")

    def _settings_menu(self):
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self._row(menu, "Go to sleep", "sleep")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add_settings(menu)
        return menu

    def _fill_menu(self, menu) -> None:
        menu.removeAllItems()
        label = LABELS.get(self._state, self._state)
        self._row(menu, f"{label} — {self._note}" if self._note else label, enabled=False)
        if self.hands_free:
            self._row(menu, f'Say "{self.wake_word}" to wake', enabled=False)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        shortcut = hotkeys.display(prefs.get("shortcuts").get("toggle", ""))
        self._row(menu, "Open chat…" + (f"   {shortcut}" if shortcut else ""), "console")
        self._row(menu, "Wake now", "wake")
        self._row(menu, "Go to sleep", "sleep")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        try:
            from mint.tools import meetings
            if meetings.is_recording():
                seconds = int(meetings.elapsed())
                self._row(menu, f"● Stop recording ({seconds // 60}:{seconds % 60:02d})", "meeting:stop")
            else:
                self._row(menu, "Record meeting", "meeting:start")
            self._row(menu, "Meeting notes…", "meeting:folder")
            clip_key = hotkeys.display(prefs.get("shortcuts").get("clipboard", ""))
            self._row(menu, "Clipboard…" + (f"  {clip_key}" if clip_key else ""), "clipboard:open")
        except Exception:
            pass
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add_settings(menu)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._row(menu, "Grant Accessibility…", "grant")
        self._row(menu, "Forget conversation history", "forget")
        if self.log_path:
            self._row(menu, "Open log", "log")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._row(menu, f"Quit {prefs.name()}", "quit", key="q")

    def _menu_action(self, action: str) -> None:
        """Main thread. Settings change at once; everything else goes to callbacks."""
        kind, _, value = action.partition(":")
        if kind == "toggle":
            prefs.toggle(value)
        elif kind == "theme":
            prefs.set("theme", value)
        elif kind == "position":
            prefs.set("position", value)
        elif kind == "meeting":
            self.fire(f"meeting_{value}")
        elif kind == "clipboard":
            from mint.ui import clipboard_window
            clipboard_window.window.show()
        elif kind == "brain":
            from mint.ui import brain as brain_window
            brain_window.open_window(value or "skills")
        else:
            self.fire(action)

    def fire(self, action: str, *args) -> None:
        """Run a callback. UI actions stay on the main thread; the rest get a thread."""
        if action == "console" and self.hud is not None:
            AppHelper.callAfter(self._toggle_console)
            return
        callback = self.callbacks.get(action)
        if callback:
            threading.Thread(target=callback, args=args, daemon=True).start()

    def _toggle_console(self) -> None:
        opening = not self.hud.console_open
        self.hud.toggle_console()
        callback = self.callbacks.get("console_opened" if opening else "console_closed")
        if callback:
            threading.Thread(target=callback, daemon=True).start()

    def _open_log(self) -> None:
        if self.log_path:
            subprocess.run(["open", "-a", "Console", self.log_path], check=False)

    def _open_settings(self) -> None:
        prefs.write_full()      # every setting spelled out, so there is something to edit
        subprocess.run(["open", "-e", str(prefs.PATH)], check=False)

    # --- public API (any thread) ------------------------------------------------

    def on(self, action: str, callback) -> None:
        self.callbacks[action] = callback

    def set_state(self, state: str, note: str = "") -> None:
        self._state, self._note = state, note

        def apply():
            if self._item is not None:
                self._item.button().setTitle_(GLYPHS.get(state, "○"))
        AppHelper.callAfter(apply)
        if self.hud is not None:
            self.hud.set_state(state, note)

    def set_paused(self, paused: bool) -> None:
        self.set_state("paused" if paused else ("sleeping" if self.hands_free else "awake"))
        if self.hud is not None:
            self.hud.refresh()

    def refresh(self) -> None:
        if self.hud is not None:
            self.hud.refresh()

    def close_console(self) -> None:
        if self.hud is not None:
            AppHelper.callAfter(self.hud.close_console)

    # Captions and activity, forwarded to the HUD.
    def user_said(self, text: str, new_turn: bool = False) -> None:
        if self.hud is not None:
            self.hud.user_said(text, new_turn)

    def assistant_said(self, text: str, new_turn: bool = False) -> None:
        if self.hud is not None:
            self.hud.assistant_said(text, new_turn)

    def action(self, text: str) -> None:
        if self.hud is not None:
            self.hud.action(text)

    def activity_start(self, name: str, args: dict) -> None:
        if self.hud is not None:
            self.hud.activity_start(name, args)

    def activity_end(self, name: str, ok: bool) -> None:
        if self.hud is not None:
            self.hud.activity_end(name, ok)

    def needs_screen(self, name: str) -> bool:
        return self.hud.needs_screen(name) if self.hud is not None else False

    def stopped(self) -> None:
        if self.hud is not None:
            self.hud.stopped()

    def show_chat(self, want: bool) -> None:
        if self.hud is not None:
            self.hud.show_chat(want)

    # --- chat: clear, summarise, new session ---------------------------------------
    def _chat(self):
        return self.hud.chat if self.hud is not None and getattr(self.hud, "_built", False) else None

    def chat_reset(self, note: str = "") -> None:
        if self._chat() is not None:
            AppHelper.callAfter(self._chat().reset, note)

    def chat_clear(self) -> None:
        if self._chat() is not None:
            AppHelper.callAfter(self._chat().clear)

    def chat_note(self, text: str) -> None:
        if self._chat() is not None:
            self._chat().note(text)

    def chat_card(self, text: str) -> None:
        if self._chat() is not None:
            AppHelper.callAfter(lambda: self._chat()._add("summary", text))

    def chat_transcript(self) -> str:
        """Blocking; call from a worker thread."""
        if self._chat() is None:
            return ""
        from mint.screen.ground import _on_main
        return _on_main(self._chat().transcript)

    def celebrate(self) -> None:
        if self.hud is not None:
            self.hud.celebrate()

    def new_exchange(self) -> None:
        if self.hud is not None:
            self.hud.new_exchange()

    def set_level(self, level: float) -> None:
        if self.hud is not None:
            self.hud.set_level(level)

    def progress(self, done: int, total: int, label: str = "") -> None:
        if self.hud is not None:
            self.hud.progress(done, total, label)


def run_cocoa(build) -> None:
    """Start the Cocoa app on the current (main) thread and never return."""
    app = AppKit.NSApplication.sharedApplication()
    # Accessory: a menu bar item with no Dock icon and no main window.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    build()
    AppHelper.runEventLoop(installInterrupt=True)


def quit_app() -> None:
    AppHelper.callAfter(lambda: AppKit.NSApplication.sharedApplication().terminate_(None))
