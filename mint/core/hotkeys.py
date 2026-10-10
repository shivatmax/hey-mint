"""System-wide keyboard shortcuts, such as ⌘J to open the console.

Carbon's RegisterEventHotKey, through ctypes: the same API Desktop Voice's
Control-M uses. It needs no permission, works whichever app is in front, and
consumes the keystroke, so the app in front does not also act on it. Must be
called on the main thread; Cocoa's run loop delivers the events.

Shortcuts are written "cmd+j", "ctrl+option+space", "cmd+shift+h". A shortcut can also
report its release (hold-to-talk). A single modifier key held down on its own - "right_option",
"right_command", "right_control", "right_shift", "fn" (Wispr-Flow style) - is watched by
ModifierHold instead: hold it to act while held, or tap it twice to start and tap once more to stop.
Two or more modifiers held together with no other key - "fn+ctrl" - are a chord (ModifierChord): hold to talk.
"""

from __future__ import annotations

import ctypes
import logging

from mint.tools.fastinput import KEYS

log = logging.getLogger("mint.core.hotkeys")

_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")


def _code(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


class _HotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

_carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
_carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, _HANDLER, ctypes.c_uint32,
                                        ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p)]
_carbon.InstallEventHandler.restype = ctypes.c_int32
_carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _HotKeyID, ctypes.c_void_p,
                                        ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
_carbon.RegisterEventHotKey.restype = ctypes.c_int32
_carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
_carbon.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
_carbon.GetEventParameter.restype = ctypes.c_int32
_carbon.GetEventKind.argtypes = [ctypes.c_void_p]
_carbon.GetEventKind.restype = ctypes.c_uint32

_SIGNATURE = _code("JRVS")
_MODIFIERS = {
    "cmd": 1 << 8, "command": 1 << 8, "⌘": 1 << 8,
    "shift": 1 << 9, "⇧": 1 << 9,
    "option": 1 << 11, "opt": 1 << 11, "alt": 1 << 11, "⌥": 1 << 11,
    "ctrl": 1 << 12, "control": 1 << 12, "⌃": 1 << 12,
}
_SYMBOLS = {"cmd": "⌘", "command": "⌘", "shift": "⇧", "option": "⌥", "opt": "⌥", "alt": "⌥",
            "ctrl": "⌃", "control": "⌃"}


def parse(shortcut: str) -> tuple[int, int] | None:
    """'cmd+j' -> (key code, Carbon modifier mask). None if it cannot be read."""
    parts = [p.strip().lower() for p in shortcut.replace("-", "+").split("+") if p.strip()]
    if not parts:
        return None
    *mods, key = parts
    if key not in KEYS or any(m not in _MODIFIERS for m in mods):
        return None
    mask = 0
    for m in mods:
        mask |= _MODIFIERS[m]
    return KEYS[key], mask


def display(shortcut: str) -> str:
    """'cmd+shift+j' -> '⇧⌘J', the way macOS menus write it; 'right_option' -> 'Right ⌥'."""
    if shortcut in MODIFIER_NAMES:
        return MODIFIER_NAMES[shortcut]
    if is_chord(shortcut):
        names = {CHORD_FLAGS[p]: _CHORD_SYMBOLS[p] for p in shortcut.lower().split("+")}
        return " ".join(names[f] for f in sorted(names, key=_CHORD_ORDER.index))
    parts = [p.strip().lower() for p in shortcut.split("+") if p.strip()]
    if not parts:
        return ""
    *mods, key = parts
    order = {"⌃": 0, "⌥": 1, "⇧": 2, "⌘": 3}
    symbols = sorted({_SYMBOLS.get(m, m) for m in mods}, key=lambda s: order.get(s, 9))
    return "".join(symbols) + (key.upper() if len(key) == 1 else key.capitalize())


class HotKeys:
    def __init__(self) -> None:
        self._callbacks: dict[int, callable] = {}
        self._releases: dict[int, callable] = {}
        self._refs: dict[int, ctypes.c_void_p] = {}
        self._next = 1
        self._handler = None

    def _install(self) -> None:
        if self._handler is not None:
            return

        def handle(_call, event, _data):
            hot = _HotKeyID()
            status = _carbon.GetEventParameter(event, _code("----"), _code("hkid"), None,
                                               ctypes.sizeof(hot), None, ctypes.byref(hot))
            if status == 0 and hot.signature == _SIGNATURE:
                released = _carbon.GetEventKind(event) == 6            # kEventHotKeyReleased
                callback = (self._releases if released else self._callbacks).get(hot.id)
                if callback is not None:
                    try:
                        callback()
                    except Exception:
                        log.exception("shortcut handler failed")
            return 0

        self._handler = _HANDLER(handle)   # keep a reference, or ctypes frees it
        # kEventClassKeyboard: kEventHotKeyPressed (5) and kEventHotKeyReleased (6)
        specs = (_EventTypeSpec * 2)(_EventTypeSpec(_code("keyb"), 5), _EventTypeSpec(_code("keyb"), 6))
        ref = ctypes.c_void_p()
        status = _carbon.InstallEventHandler(_carbon.GetApplicationEventTarget(), self._handler,
                                             2, specs, None, ctypes.byref(ref))
        if status != 0:
            raise OSError(f"InstallEventHandler failed ({status})")

    def register(self, shortcut: str, callback, on_release=None) -> bool:
        """Register a shortcut (`on_release` too, for hold-to-talk). False if it cannot be parsed or is
        taken."""
        parsed = parse(shortcut or "")
        if parsed is None:
            if shortcut:
                log.warning("cannot read shortcut %r", shortcut)
            return False
        self._install()
        key, mask = parsed
        ident = self._next
        self._next += 1
        ref = ctypes.c_void_p()
        status = _carbon.RegisterEventHotKey(key, mask, _HotKeyID(_SIGNATURE, ident),
                                             _carbon.GetApplicationEventTarget(), 0, ctypes.byref(ref))
        if status != 0:
            # -9878 (eventHotKeyExistsErr): another app already owns it.
            log.warning("shortcut %s is not available (%s)", shortcut, status)
            print(f"  [shortcut {display(shortcut)} is taken by another app ({status})]", flush=True)
            return False
        self._callbacks[ident] = callback
        if on_release is not None:
            self._releases[ident] = on_release
        self._refs[ident] = ref
        return True

    def clear(self) -> None:
        for ref in self._refs.values():
            _carbon.UnregisterEventHotKey(ref)
        self._refs.clear()
        self._callbacks.clear()
        self._releases.clear()


# --- a modifier key on its own -------------------------------------------------------------

MODIFIER_KEYS = {"right_option": (61, 1 << 19), "right_command": (54, 1 << 20), "right_control": (62, 1 << 18),
                 "right_shift": (60, 1 << 17), "fn": (63, 1 << 23)}
MODIFIER_NAMES = {"right_option": "Right ⌥", "right_command": "Right ⌘", "right_control": "Right ⌃",
                  "right_shift": "Right ⇧", "fn": "fn"}


# Modifiers held together on their own (a chord): NSEvent's device-independent flags.
CHORD_FLAGS = {"fn": 1 << 23, "ctrl": 1 << 18, "control": 1 << 18, "option": 1 << 19, "opt": 1 << 19,
               "alt": 1 << 19, "cmd": 1 << 20, "command": 1 << 20, "shift": 1 << 17}
_CHORD_SYMBOLS = {"fn": "fn", "ctrl": "⌃", "control": "⌃", "option": "⌥", "opt": "⌥", "alt": "⌥", "cmd": "⌘",
                  "command": "⌘", "shift": "⇧"}
_CHORD_ORDER = [1 << 23, 1 << 18, 1 << 19, 1 << 17, 1 << 20]
_ALL_MODS = sum(set(CHORD_FLAGS.values()))


def is_chord(shortcut: str) -> bool:
    """'fn+ctrl' (two or more modifiers, no other key)."""
    parts = [p.strip().lower() for p in (shortcut or "").split("+") if p.strip()]
    return len(parts) >= 2 and all(p in CHORD_FLAGS for p in parts) and len({CHORD_FLAGS[p] for p in parts}) >= 2


def chord_mask(shortcut: str) -> int:
    mask = 0
    for part in shortcut.lower().split("+"):
        mask |= CHORD_FLAGS[part.strip()]
    return mask


class ModifierChord:
    """Modifiers held together and nothing else (fn+⌃): on_hold once they are all down for a moment, on_release
    as soon as one is let go. A key typed before the hold counts as a shortcut (⌃fn-arrow...), not ours."""

    HOLD = 0.15

    def __init__(self, shortcut: str, on_hold, on_release, on_down=None, on_drop=None) -> None:
        self.mask = chord_mask(shortcut)
        self.on_hold, self.on_release = on_hold, on_release
        self.on_down, self.on_drop = on_down, on_drop      # the chord formed / broke before the hold (see ModifierHold)
        self.down_at = 0.0
        self.holding = False
        self.spoiled = False
        self.monitors = []

    def start(self) -> None:
        import AppKit
        self.monitors = [
            AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskFlagsChanged, self.flags),
            AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, self.key),
            AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskFlagsChanged,
                                                                         self._local_flags),
            AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, self._local_key)]

    def stop(self) -> None:
        import AppKit
        for monitor in self.monitors:
            if monitor is not None:
                AppKit.NSEvent.removeMonitor_(monitor)
        self.monitors = []

    def _local_flags(self, event):
        self.flags(event)
        return event

    def _local_key(self, event):
        self.key(event)
        return event

    def key(self, event) -> None:
        if self.down_at and not self.holding:
            if not self.spoiled:
                ModifierHold._call(self.on_drop)
            self.spoiled = True

    def flags(self, event, later=None) -> None:
        """One FlagsChanged event. `later(seconds, fn)` schedules the hold check (AppHelper.callLater)."""
        import time as _time
        held = int(event.modifierFlags()) & _ALL_MODS
        now = _time.monotonic()
        if held == self.mask:
            if not self.down_at:
                self.down_at, self.spoiled = now, False
                stamp = now
                ModifierHold._call(self.on_down)

                def check():
                    if self.down_at == stamp and not self.spoiled and not self.holding:
                        self.holding = True
                        ModifierHold._call(self.on_hold)
                if later is None:
                    from PyObjCTools import AppHelper
                    later = AppHelper.callLater
                later(self.HOLD, check)
            return
        if self.down_at:
            self.down_at = 0.0
            if self.holding:
                self.holding = False
                ModifierHold._call(self.on_release)
            elif not self.spoiled:
                ModifierHold._call(self.on_drop)


class ModifierHold:
    """One modifier key on its own: hold it (on_hold, then on_release when let go), or tap it twice
    quickly (on_double_tap). Typing with it (right-option accents) does nothing: any other key while it
    is down cancels. Needs Accessibility, like any app watching keys (Mint has it)."""

    HOLD = 0.28           # held this long with nothing else pressed: it is a hold
    DOUBLE = 0.42         # two taps within this: a double tap

    def __init__(self, name: str, on_hold, on_release, on_double_tap=None, on_cancel=None, on_down=None,
                 on_drop=None) -> None:
        self.code, self.flag = MODIFIER_KEYS[name]
        self.on_hold, self.on_release, self.on_double_tap = on_hold, on_release, on_double_tap
        self.on_cancel = on_cancel          # another modifier joined a hold (fn, then ⌃ for hold-to-talk)
        # on_down: the key went down alone - start recording now, the hold may follow (the first words were lost
        # while waiting HOLD seconds to be sure). on_drop: it wasn't a hold after all (a tap, a shortcut).
        self.on_down, self.on_drop = on_down, on_drop
        self.down_at = 0.0
        self.last_tap = 0.0
        self.holding = False
        self.spoiled = False
        self.monitors = []

    def start(self) -> None:
        import AppKit
        mask_flags = AppKit.NSEventMaskFlagsChanged
        mask_keys = AppKit.NSEventMaskKeyDown
        self.monitors = [
            AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask_flags, self._flags),
            AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask_keys, self._key),
            AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask_flags, self._local_flags),
            AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask_keys, self._local_key)]

    def stop(self) -> None:
        import AppKit
        for monitor in self.monitors:
            if monitor is not None:
                AppKit.NSEvent.removeMonitor_(monitor)
        self.monitors = []

    def _local_flags(self, event):
        self._flags(event)
        return event

    def _local_key(self, event):
        self._key(event)
        return event

    def _key(self, event) -> None:
        if self.down_at:
            if not self.spoiled and not self.holding:
                self._call(self.on_drop)
            self.spoiled = True              # a key with it: typing, not a hold

    def _flags(self, event) -> None:
        import time as _time

        from PyObjCTools import AppHelper
        if int(event.keyCode()) != self.code:
            if self.down_at:
                if not self.spoiled and not self.holding:
                    self._call(self.on_drop)
                self.spoiled = True          # another modifier joined: a shortcut, not ours
                if self.holding:
                    self.holding = False     # ...after the hold began: undo it (fn held, then ⌃ = talk to Mint)
                    self._call(self.on_cancel)
            return
        flags = int(event.modifierFlags())
        down = bool(flags & self.flag)
        now = _time.monotonic()
        if down and not self.down_at:
            # Pressed while another modifier is down (⌃ then fn): a chord, not this key alone.
            self.down_at, self.spoiled = now, bool(flags & _ALL_MODS & ~self.flag)
            stamp = now
            if not self.spoiled:
                self._call(self.on_down)

            def check():
                if self.down_at == stamp and not self.spoiled:
                    self.holding = True
                    self._call(self.on_hold)
            AppHelper.callLater(self.HOLD, check)
        elif not down and self.down_at:
            held, self.down_at = now - self.down_at, 0.0
            if self.holding:
                self.holding = False
                self._call(self.on_release)
                return
            if not self.spoiled:
                self._call(self.on_drop)     # a tap: whatever on_down started is not wanted (a double tap restarts)
            if not self.spoiled and held < self.HOLD:
                if now - self.last_tap < self.DOUBLE and self.on_double_tap is not None:
                    self.last_tap = 0.0
                    self._call(self.on_double_tap)
                else:
                    self.last_tap = now

    @staticmethod
    def _call(fn) -> None:
        if fn is None:
            return
        try:
            fn()
        except Exception:
            log.exception("modifier key handler failed")
