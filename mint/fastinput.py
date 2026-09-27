"""Tier 0: direct system calls. No model, no accessibility walk, no waiting.

Everything here finishes in milliseconds. Scrolling, keys, volume, launching an
app or a URL. These are the actions that make the assistant feel instant, so they
must never be routed through the screen-driving loop.

Key events need Accessibility permission for the process running this code (the
terminal app, or Python itself). Launching apps and URLs needs no permission.
"""

from __future__ import annotations

import subprocess
import time

import ApplicationServices
import Quartz

# Virtual key codes, the same ones the Swift side uses.
KEYS = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "forwarddelete": 117,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4, "i": 34,
    "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35, "q": 12,
    "r": 15, "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7, "y": 16, "z": 6,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25, "comma": 43, "period": 47, "slash": 44, "minus": 27,
    "equal": 24, "grave": 50, "[": 33, "]": 30, "leftbracket": 33, "rightbracket": 30,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

MODIFIERS = {
    "command": Quartz.kCGEventFlagMaskCommand, "cmd": Quartz.kCGEventFlagMaskCommand,
    "control": Quartz.kCGEventFlagMaskControl, "ctrl": Quartz.kCGEventFlagMaskControl,
    "option": Quartz.kCGEventFlagMaskAlternate, "alt": Quartz.kCGEventFlagMaskAlternate,
    "shift": Quartz.kCGEventFlagMaskShift,
    "function": Quartz.kCGEventFlagMaskSecondaryFn, "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}


def has_accessibility() -> bool:
    """Whether THIS process may post key events. Granted to the terminal, not to Mint."""
    return bool(ApplicationServices.AXIsProcessTrusted())


def request_accessibility() -> bool:
    """Ask macOS for Accessibility, showing the system prompt.

    An application does not appear in the Accessibility list until it has asked
    at least once, which is why adding "Mint" by hand is impossible: the
    permission belongs to the terminal hosting this process, and the terminal
    has never asked. This call makes it ask, which puts it in the list.
    """
    return bool(ApplicationServices.AXIsProcessTrustedWithOptions(
        {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
    ))


def _post(event, pid: int | None = None) -> None:
    """Post an event system-wide, or straight to one process.

    Targeting a pid matters for multi-key sequences: in testing, focus moved to
    another app halfway through a Slack quick-switch, and the rest of the keys
    typed into that app. Events posted to a pid cannot land anywhere else.
    """
    from . import control
    if control.stopped():          # the user said stop: no more input from Mint
        return
    if pid is not None:
        Quartz.CGEventPostToPid(pid, event)
    else:
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


# Without Accessibility, macOS drops synthesised events without any error, so a
# scroll would report success while nothing moved. Say so instead, and point the
# model at the desktop tool, whose app does hold the permission.
_NO_ACCESS = ("Did nothing: Mint lacks Accessibility permission, so key and scroll "
              "events are blocked. Use the desktop tool for this instead.")


def press_key(key: str, modifiers: list[str] | None = None, times: int = 1,
              pid: int | None = None) -> str:
    """Press a key, optionally with modifiers, one or more times.

    With `pid`, the key goes only to that process, whatever is in front.
    """
    if not has_accessibility():
        return _NO_ACCESS
    name = key.strip().lower()
    if name not in KEYS:
        return f"Unknown key '{key}'. Known keys include return, escape, tab, arrows, letters and digits."
    code = KEYS[name]
    flags = 0
    for modifier in modifiers or []:
        found = MODIFIERS.get(modifier.strip().lower())
        if found is None:
            return f"Unknown modifier '{modifier}'. Use command, control, option, shift or fn."
        flags |= found

    times = max(1, min(int(times), 50))
    for _ in range(times):
        down = Quartz.CGEventCreateKeyboardEvent(None, code, True)
        up = Quartz.CGEventCreateKeyboardEvent(None, code, False)
        # Always set the flags, even to none: an event made without a source
        # takes the current modifier state, and a Return right after a ⌘V went
        # out as ⌘Return - Chrome then opened the address in a background tab
        # and ChatGPT dropped the message (24 Sep).
        Quartz.CGEventSetFlags(down, flags)
        Quartz.CGEventSetFlags(up, flags)
        _post(down, pid)
        _post(up, pid)
        time.sleep(0.012)

    spoken = "+".join((modifiers or []) + [name])
    return f"Pressed {spoken}" + (f" {times} times" if times > 1 else "")


def scroll(direction: str = "down", amount: int = 3) -> str:
    """Scroll the window under the pointer. `amount` is roughly one screen per 5."""
    if not has_accessibility():
        return _NO_ACCESS
    direction = direction.strip().lower()
    if direction not in {"up", "down", "left", "right"}:
        return "Direction must be up, down, left or right."

    amount = max(1, min(int(amount), 40))
    step = 5
    vertical = horizontal = 0
    if direction == "down":
        vertical = -step
    elif direction == "up":
        vertical = step
    elif direction == "left":
        horizontal = step
    else:
        horizontal = -step

    # A wheel event goes to the window under its location, which by default is
    # wherever the pointer happens to be - often another app. Aim it at the
    # front window instead, without moving the visible pointer.
    target = _front_window_point()
    if target is not None:
        from .effects import fx
        fx.scroll(target.x, target.y, direction)
    for _ in range(amount):
        event = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 2, vertical, horizontal
        )
        if target is not None:
            Quartz.CGEventSetLocation(event, target)
        _post(event)
        time.sleep(0.02)
    return f"Scrolled {direction} {amount} times"


def _front_window_point():
    """A point well inside the front app's frontmost window, in global coordinates."""
    import AppKit

    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return None
    pid = front.processIdentifier()
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    # The list is ordered front to back, so the first match is the front window.
    for window in windows:
        if window.get("kCGWindowOwnerPID") != pid or window.get("kCGWindowLayer", 0) != 0:
            continue
        bounds = window.get("kCGWindowBounds") or {}
        width, height = bounds.get("Width", 0), bounds.get("Height", 0)
        if width < 100 or height < 100:
            continue
        # Below the toolbar, a little right of centre, where page content is.
        return Quartz.CGPointMake(bounds["X"] + width * 0.6, bounds["Y"] + height * 0.55)
    return None


def set_volume(level: int) -> str:
    """Set output volume, 0 to 100."""
    level = max(0, min(int(level), 100))
    subprocess.run(
        ["osascript", "-e", f"set volume output volume {level}"],
        capture_output=True, check=False,
    )
    return f"Volume set to {level}"


def media_key(action: str) -> str:
    """Play/pause, next or previous track, using the hardware media keys.

    These are NSSystemDefined events rather than ordinary key events, so they go
    through AppKit; CGEventCreateKeyboardEvent cannot express them.
    """
    import AppKit  # imported lazily: only this function needs it

    if not has_accessibility():
        return _NO_ACCESS
    codes = {"playpause": 16, "next": 17, "previous": 18}
    action = action.strip().lower()
    if action not in codes:
        return "Action must be playpause, next or previous."
    code = codes[action]
    for state in (0xA, 0xB):  # key down, then key up
        event = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            AppKit.NSEventTypeSystemDefined, AppKit.NSZeroPoint, state << 8,
            0, 0, None, 8, (code << 16) | (state << 8), -1,
        )
        _post(event.CGEvent())
    return f"Sent media key: {action}"
