"""⌘V, ⌘C, ⌘X, ⌘A and ⌘Z in every text field of Mint, even without a working main menu.

macOS sends those keys through the app's main menu (ui._edit_keys builds a hidden Edit menu). An accessory app
has been seen to lose it (a window opened before the menu existed, a panel that never becomes the key window),
and then pasting an API key into a field silently did nothing. This is the belt to that braces: one local key
monitor that, when the window's first responder is a text view (every field's editor is one), does the edit
itself and swallows the key so the menu doesn't do it a second time.
"""

from __future__ import annotations

import logging

import AppKit

log = logging.getLogger("mint.ui.pasteable")

_monitor = [None]
_KEYS = {"v": "paste:", "c": "copy:", "x": "cut:", "a": "selectAll:"}


def handle(event):
    """The monitor: the event back (not ours), or None (done here)."""
    try:
        flags = int(event.modifierFlags()) & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
        if not flags & AppKit.NSEventModifierFlagCommand or flags & (AppKit.NSEventModifierFlagOption
                                                                     | AppKit.NSEventModifierFlagControl):
            return event
        key = str(event.charactersIgnoringModifiers() or "").lower()
        if key not in _KEYS and key != "z":
            return event
        # The window by number among live windows, not event.window(): 10 Oct 17:23:58 Mint crashed (SIGSEGV in a
        # local event monitor) while a bot token was pasted into Settings - a stale window pointer is the suspect.
        app = AppKit.NSApplication.sharedApplication()
        window = app.windowWithWindowNumber_(int(event.windowNumber())) or app.keyWindow()
        target = window.firstResponder() if window is not None else None
        if not isinstance(target, AppKit.NSTextView):
            return event
        editable = bool(target.isEditable())
        if key == "z":
            manager = target.undoManager()
            if not editable or manager is None:
                return event
            if flags & AppKit.NSEventModifierFlagShift:
                manager.redo()
            else:
                manager.undo()
            return None
        if key in ("v", "x") and not editable:
            return event
        target.performSelector_withObject_(_KEYS[key], None)
        return None
    except Exception:
        log.debug("edit key", exc_info=True)
        return event


def install() -> None:
    """Once, on the main thread."""
    if _monitor[0] is None:
        _monitor[0] = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, handle)
