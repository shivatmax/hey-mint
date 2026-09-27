"""System-wide keyboard shortcuts, such as ⌘J to open the console.

Carbon's RegisterEventHotKey, through ctypes: the same API Desktop Voice's
Control-M uses. It needs no permission, works whichever app is in front, and
consumes the keystroke, so the app in front does not also act on it. Must be
called on the main thread; Cocoa's run loop delivers the events.

Shortcuts are written "cmd+j", "ctrl+option+space", "cmd+shift+h".
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
    """'cmd+shift+j' -> '⇧⌘J', the way macOS menus write it."""
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
                callback = self._callbacks.get(hot.id)
                if callback is not None:
                    try:
                        callback()
                    except Exception:
                        log.exception("shortcut handler failed")
            return 0

        self._handler = _HANDLER(handle)   # keep a reference, or ctypes frees it
        spec = _EventTypeSpec(_code("keyb"), 5)   # kEventClassKeyboard, kEventHotKeyPressed
        ref = ctypes.c_void_p()
        status = _carbon.InstallEventHandler(_carbon.GetApplicationEventTarget(), self._handler,
                                             1, ctypes.byref(spec), None, ctypes.byref(ref))
        if status != 0:
            raise OSError(f"InstallEventHandler failed ({status})")

    def register(self, shortcut: str, callback) -> bool:
        """Register a shortcut. False if it cannot be parsed or is taken."""
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
        self._refs[ident] = ref
        return True

    def clear(self) -> None:
        for ref in self._refs.values():
            _carbon.UnregisterEventHotKey(ref)
        self._refs.clear()
        self._callbacks.clear()
