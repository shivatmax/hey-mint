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
# The menu bar face (pref menubar_face): state -> (eyes, mouth, dot colour, dot blinks).
FACES = {
    "starting": ("open", "w", None, False),
    "sleeping": ("closed", "w", None, False),
    "awake": ("open", "w", (0.24, 0.86, 0.62), False),          # mint: listening
    "thinking": ("up", "w", (0.62, 0.45, 1.0), False),          # violet, eyes up
    "working": ("open", "w", (0.28, 0.6, 1.0), True),           # blue, blinking
    "speaking": ("open", "talk", None, False),
    "paused": ("closed", "w", (0.62, 0.62, 0.66), False),       # grey: microphone off
    "offline": ("dash", "flat", (1.0, 0.62, 0.22), False),      # orange
    "error": ("dash", "flat", (1.0, 0.3, 0.32), False),         # red
    "done": ("happy", "w", (0.3, 0.84, 0.42), False),           # green
}
FACE = 18.0                 # points; drawn in code, crisp at any scale
DOT = 4.6
DOT_AT = (2.7, 2.7)         # the dot's centre, from the image's top left


def face_image(eyes: str, mouth: str = "w", gap: bool = False):
    """Mint's round face as an 18 pt template image (the menu bar tints it for light and dark): a disc
    with the eyes and ω mouth cut out of it, and with `gap`, a notch cut for the status dot."""
    def draw(rect):
        AppKit.NSColor.blackColor().set()
        AppKit.NSBezierPath.bezierPathWithOvalInRect_(AppKit.NSMakeRect(1.5, 1.5, 15.0, 15.0)).fill()
        AppKit.NSGraphicsContext.currentContext().setCompositingOperation_(
            AppKit.NSCompositingOperationDestinationOut)

        def line(points, width=1.35, curve=None):
            path = AppKit.NSBezierPath.bezierPath()
            path.setLineWidth_(width)
            path.setLineCapStyle_(AppKit.NSLineCapStyleRound)
            path.setLineJoinStyle_(AppKit.NSLineJoinStyleRound)
            path.moveToPoint_(points[0])
            if curve is None:
                for p in points[1:]:
                    path.lineToPoint_(p)
            else:
                start = points[0]
                for (end, c) in curve:            # a quadratic curve through `c`, as a cubic
                    c1 = (start[0] + 2 / 3 * (c[0] - start[0]), start[1] + 2 / 3 * (c[1] - start[1]))
                    c2 = (end[0] + 2 / 3 * (c[0] - end[0]), end[1] + 2 / 3 * (c[1] - end[1]))
                    path.curveToPoint_controlPoint1_controlPoint2_(end, c1, c2)
                    start = end
            path.stroke()

        for cx in (6.3, 11.7):
            if eyes in ("open", "up"):
                h = 4.4 if eyes == "open" else 3.8
                cy = 7.9 if eyes == "open" else 6.7
                cx += 0.0 if eyes == "open" else 0.5             # looking up and away: thinking
                AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    AppKit.NSMakeRect(cx - 1.15, cy - h / 2, 2.3, h), 1.15, 1.15).fill()
            elif eyes == "closed":                               # ‿ asleep
                line([(cx - 1.5, 7.9)], curve=[((cx + 1.5, 7.9), (cx, 9.7))])
            elif eyes == "happy":                                # ∩ done
                line([(cx - 1.5, 8.6)], curve=[((cx + 1.5, 8.6), (cx, 6.4))])
            else:                                                # – error
                line([(cx - 1.3, 8.0), (cx + 1.3, 8.0)])
        if mouth == "talk":
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(AppKit.NSMakeRect(7.7, 10.9, 2.6, 2.2)).fill()
        elif mouth == "flat":
            line([(7.8, 12.0), (10.2, 12.0)], width=1.15)
        else:                                                    # ω
            line([(7.0, 11.2)], width=1.1, curve=[((9.0, 11.4), (8.0, 13.0)), ((11.0, 11.2), (10.0, 13.0))])
        if gap:
            r = DOT / 2 + 0.95
            AppKit.NSColor.blackColor().set()
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                AppKit.NSMakeRect(DOT_AT[0] - r, DOT_AT[1] - r, 2 * r, 2 * r)).fill()
        return True
    image = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(AppKit.NSMakeSize(FACE, FACE), True, draw)
    image.setTemplate_(True)
    image.setAccessibilityDescription_(prefs.name())
    return image


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
           ("notch_mode", "Dynamic Island at the notch"),
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
        self._faces: dict = {}             # (eyes, mouth, gap) -> NSImage, drawn once
        self._face_key = None              # what the menu bar shows now
        self._flash = None                 # (face, until): "done" / "error" for a moment
        self._dot = None
        self._state = "starting"
        self._note = ""
        self._hotkeys = None

    # --- construction (main thread) ------------------------------------------

    def build(self) -> None:
        bar = AppKit.NSStatusBar.systemStatusBar()
        self._item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self._item.button().setTitle_(GLYPHS["starting"])
        self._item.button().setToolTip_(prefs.name())
        self._apply_face()
        prefs.on_change(lambda key, value: key == "menubar_face" and AppHelper.callAfter(self._apply_face))

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
        self._register_talk_hold(keys)
        if keys.get("clipboard"):
            from mint.ui import clipboard_window
            if self._hotkeys.register(keys["clipboard"], clipboard_window.window.toggle):
                print(f"  [shortcut: {hotkeys.display(keys['clipboard'])} opens the clipboard]", flush=True)
        if keys.get("hide") and self.hud is not None and self._hotkeys.register(keys["hide"], self.hud.toggle_hidden):
            print(f"  [shortcut: {hotkeys.display(keys['hide'])} hides or shows {prefs.name()}]", flush=True)
        self._register_dictation(keys)

    def _register_talk_hold(self, keys: dict) -> None:
        """Hold to talk: listen while held (no wake word), act when let go. fn+⌃ by default (a chord of modifiers),
        or any key combo."""
        if getattr(self, "_talk_chord", None) is not None:
            self._talk_chord.stop()
            self._talk_chord = None
        key = str(keys.get("talk_hold") or "")
        press, release = (lambda: self.fire("talk_hold")), (lambda: self.fire("talk_release"))
        if hotkeys.is_chord(key):
            self._talk_chord = hotkeys.ModifierChord(key, press, release)
            self._talk_chord.start()
        elif not (key and self._hotkeys.register(key, press, on_release=release)):
            return
        print(f"  [shortcut: hold {hotkeys.display(key)} and talk to {prefs.name()} - let go and it works on it]",
              flush=True)

    def _register_dictation(self, keys: dict) -> None:
        """Dictation (dictation.py): hold the key to dictate at the cursor, tap it twice for hands-free."""
        from mint.voice import dictation
        if getattr(self, "_dictation_hold", None) is not None:
            self._dictation_hold.stop()
            self._dictation_hold = None
        key = str(keys.get("dictate") or "")
        if key in hotkeys.MODIFIER_KEYS:
            self._dictation_hold = hotkeys.ModifierHold(key, dictation.start, dictation.finish, dictation.toggle,
                                                        on_cancel=dictation.cancel)
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
        """What changes from minute to minute: the microphone and spoken replies. Everything you set once
        (voice lock, theme, position, the orb's looks, the notch, screen sharing) lives in Settings."""
        self._row(menu, "Microphone", "toggle:mic", checked=bool(prefs.get("mic")))
        self._row(menu, "Spoken replies", "toggle:voice", checked=bool(prefs.get("voice")))
        hidden = bool(getattr(self.hud, "hidden", False))
        key = (prefs.get("shortcuts") or {}).get("hide") or ""
        self._row(menu, ("Show " if hidden else "Hide ") + prefs.name()
                  + (f"  ({hotkeys.display(key)})" if key else ""), "hide")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._row(menu, "Settings…", "open_settings", key=",")

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
        try:
            from mint.tools import automations
            if automations.halted():
                self._row(menu, "Paused — Resume everything", "halt:off")
            else:
                self._row(menu, "Pause everything", "halt:on")
        except Exception:
            pass
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
        try:
            from mint.tools import agent_checks
            usage = agent_checks.limits_line()          # Claude's / Codex's plan limits, percent used
            if usage:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                self._row(menu, "Usage used:  " + usage, enabled=False)
        except Exception:
            pass
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add_settings(menu)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._row(menu, f"What {prefs.name()} can do…", "onboarding:tour")
        self._row(menu, "Welcome tour…", "onboarding:show")
        self._row(menu, "Grant Accessibility…", "grant")
        self._row(menu, "Forget conversation history", "forget")
        if self.log_path:
            self._row(menu, "Open log", "log")
        self._row(menu, "Report a problem…", "report:open")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        from mint.app import power
        if power.can_restart():
            self._row(menu, f"Restart {prefs.name()}", "power:restart")
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
        elif kind == "report":
            from mint.app import report
            report.open_issue()
        elif kind == "onboarding":
            from mint.ui import onboarding
            if value == "tour":
                onboarding.show_tour()
            else:
                onboarding.onboarding.show()
        elif kind == "halt":
            from mint.tools import automations       # stops running jobs: off the main thread
            threading.Thread(target=automations.halt, args=(value == "on", "menu"), daemon=True).start()
        elif kind == "brain":
            from mint.ui import brain as brain_window
            brain_window.open_window(value or "skills")
        elif action == "power:restart":
            from mint.app import power
            power.restart("menu")
        else:
            self.fire(action)

    def fire(self, action: str, *args) -> None:
        """Run a callback. UI actions stay on the main thread; the rest get a thread."""
        if action == "console" and self.hud is not None:
            AppHelper.callAfter(self._toggle_console)
            return
        if action == "hide" and self.hud is not None:
            self.hud.toggle_hidden()                 # main thread inside
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
        try:
            from mint.ui import sfx
            sfx.set_state(state, note)     # sounds keep out of the microphone while listening
        except Exception:
            pass
        AppHelper.callAfter(self._apply_face)
        if self.hud is not None:
            self.hud.set_state(state, note)

    # --- the menu bar face (main thread) ---------------------------------------------

    def flash_face(self, face: str, seconds: float = 3.0) -> None:
        """Show "done" or "error" on the menu bar face for a moment, then the state again. Any thread."""
        import time
        self._flash = (face, time.monotonic() + seconds)
        AppHelper.callAfter(self._apply_face)
        AppHelper.callLater(seconds + 0.05, self._apply_face)

    def _apply_face(self) -> None:
        if self._item is None:
            return
        import time
        button = self._item.button()
        state = self._state
        if self._flash is not None:
            if time.monotonic() < self._flash[1]:
                state = self._flash[0]
            else:
                self._flash = None
        if not prefs.get("menubar_face"):
            if self._face_key is not None:
                button.setImage_(None)
                self._face_key = None
                self._show_dot(None, False)
            button.setTitle_(GLYPHS.get(self._state, "○"))
            return
        eyes, mouth, colour, blink = FACES.get(state, FACES["starting"])
        key = (eyes, mouth, colour, blink)
        if key == self._face_key:
            return                         # redraw only when the face changes
        self._face_key = key
        shape = (eyes, mouth, colour is not None)
        if shape not in self._faces:
            self._faces[shape] = face_image(*shape)
        button.setTitle_("")
        button.setImage_(self._faces[shape])
        button.setImagePosition_(AppKit.NSImageOnly)
        self._show_dot(colour, blink)

    def _show_dot(self, colour, blink: bool) -> None:
        """The state dot at the face's top left: a coloured layer over the template image, so the
        face keeps the menu bar's own tint and the dot keeps its colour."""
        import Quartz

        from mint.ui import kinetics
        button = self._item.button()
        if colour is None:
            if self._dot is not None:
                self._dot.setHidden_(True)
            return
        if self._dot is None:
            self._dot = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, DOT, DOT))
            self._dot.setWantsLayer_(True)
            self._dot.layer().setCornerRadius_(DOT / 2)
            button.addSubview_(self._dot)
        bounds = button.bounds()
        ox = (bounds.size.width - FACE) / 2 + DOT_AT[0] - DOT / 2
        top = (bounds.size.height - FACE) / 2 + DOT_AT[1] - DOT / 2
        y = top if button.isFlipped() else bounds.size.height - top - DOT
        self._dot.setFrame_(AppKit.NSMakeRect(round(ox * 2) / 2, round(y * 2) / 2, DOT, DOT))
        self._dot.setHidden_(False)
        layer = self._dot.layer()
        layer.setBackgroundColor_(Quartz.CGColorCreateSRGB(*colour, 1.0))
        layer.removeAnimationForKey_("blink")
        if blink and not kinetics.reduce_motion():
            kinetics.pulse(layer, key="blink", seconds=1.1, low=0.25, high=1.0)

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
        if not ok:
            self.flash_face("error", 2.5)
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
        self.flash_face("done", 3.0)
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
    _edit_keys(app)
    build()
    AppHelper.runEventLoop(installInterrupt=True)


def _edit_keys(app) -> None:
    """A hidden Edit menu, so ⌘V, ⌘C, ⌘X, ⌘A and ⌘Z work in Mint's text fields (Settings, key prompts,
    the chat). An accessory app shows no menu bar, but macOS still routes those keys through the main
    menu - without one, pasting an API key into Settings did nothing."""
    main = AppKit.NSMenu.alloc().init()
    for title, items in (("Mint", []),
                         ("Edit", [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), None, ("Cut", "cut:", "x"),
                                   ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                                   ("Select All", "selectAll:", "a")])):
        holder = AppKit.NSMenuItem.alloc().init()
        menu = AppKit.NSMenu.alloc().initWithTitle_(title)
        for item in items:
            if item is None:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            words, action, key = item
            menu.addItemWithTitle_action_keyEquivalent_(words, action, key)
        holder.setSubmenu_(menu)
        main.addItem_(holder)
    app.setMainMenu_(main)
    from mint.ui import pasteable
    pasteable.install()                   # and the keys themselves, should the menu ever be missing


def quit_app() -> None:
    AppHelper.callAfter(lambda: AppKit.NSApplication.sharedApplication().terminate_(None))
