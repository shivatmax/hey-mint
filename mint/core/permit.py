"""Permissions at the moment they are needed. Seen 9 Oct: a new user asked Mint to search Telegram; it tried, failed
and never said why, and nothing asked for anything. Now, when a request needs a permission Mint doesn't have, Mint asks
for it right then (macOS's own box the first time, else System Settings open at the right switch), tells the model to
say so in one line, and carries on by itself once it is switched on. Asked again after a "no", it says plainly that it
can't do this without it - and opens the switch again.

Accessibility is checked before the tool runs (AXIsProcessTrusted is exact). Screen Recording is only known from a
failed look (CGPreflightScreenCaptureAccess can say no while capture works), so it is caught from the tool's result.
"""

from __future__ import annotations

import logging
import threading
import time

from mint.core import permissions

log = logging.getLogger("mint.core.permit")

# Tools that click, type or read another app through Accessibility.
NEEDS_ACCESSIBILITY = {"ui_act", "ui_elements", "click_at", "click_text", "drag", "type_text", "press_key", "scroll",
                       "get_selected_text", "desktop", "edit_selection"}
# What a tool says when it ran into a missing permission anyway (checked inside the tools).
_SAID = {"accessibility": ("lacks Accessibility permission", "without Accessibility permission"),
         "screen": ("lacks Screen Recording permission",)}
WATCH_FOR = 180.0           # how long Mint waits for the switch before it stops watching
AGAIN_AFTER = 20.0          # a second tool in the same breath doesn't open System Settings twice

_asked: set = set()         # permissions.ask's memory: the first time macOS's own box, then System Settings
_times: dict[str, list[float]] = {}
_watching: set[str] = set()
_lock = threading.Lock()


def missing(name: str, args: dict | None = None) -> str | None:
    """The permission this call needs and Mint lacks, or None."""
    if name in NEEDS_ACCESSIBILITY and permissions.status("accessibility") != "allowed":
        if name in _ENGINE_CAN and _separate_engine():
            return None             # the separate engine app holds its own grant (tools.py hands these to it)
        return "accessibility"
    return None


_ENGINE_CAN = {"scroll", "press_key", "type_text", "get_selected_text", "desktop"}


def _separate_engine() -> bool:
    try:
        from mint.core import config
        found = config.engine()
        return bool(found) and found[0] == "app"
    except Exception:
        return False


def from_result(result: str) -> str | None:
    """The permission a tool's result says was missing, or None."""
    text = str(result or "")
    return next((kind for kind, phrases in _SAID.items() if any(p in text for p in phrases)), None)


def request(kind: str, tool: str = "", resume=None) -> str:
    """Ask for `kind` now and say what to tell the user. `resume(kind)` is called (any thread) once it is on."""
    from mint.core import prefs
    name = prefs.name()
    title = permissions.title(kind)
    now = time.monotonic()
    with _lock:
        times = _times.setdefault(kind, [])
        recent = bool(times) and now - times[-1] < AGAIN_AFTER
        if not recent:
            times.append(now)
        tries = len(times)
    if not recent:
        try:
            permissions.ask(kind, _asked)
        except Exception:
            log.debug("ask %s", kind, exc_info=True)
            permissions.open_pane(kind)
        _watch(kind, resume)
    where = f"System Settings ▸ Privacy & Security ▸ {title}"
    loses = permissions.LOSES.get(kind, "").format(name=name)
    if tries <= 1:
        shown = ("macOS's box asking for it is on screen now (it may say 'would like to control this computer'; "
                 "the button is Open System Settings)" if kind == "accessibility" else
                 f"macOS's box asking for it is on screen now, or {where} is open")
        return (f"NEEDS PERMISSION: nothing was done - {name} needs {title} for this ({loses}). {shown}. Tell the user "
                f"in one short sentence to switch on {name} there; you carry on by yourself as soon as it is on - "
                "don't try other tools for this meanwhile, and don't say it is done."
                + (" macOS may then ask to Quit & Reopen: that's fine." if kind == "screen" else ""))
    return (f"STILL NO PERMISSION: {title} is still off for {name} (asked {tries} times), so nothing was done. {where} "
            f"is open again at the right switch. Tell the user plainly: {name} can't do this without {title} - switch "
            f"{name} on there (if it is already on, switch it off and on again). You carry on once it is on; don't "
            "try other ways meanwhile.")


def _watch(kind: str, resume) -> None:
    """Watch for the switch (once per permission at a time); call resume(kind) when it flips on."""
    with _lock:
        if kind in _watching:
            return
        _watching.add(kind)

    def run():
        try:
            end = time.monotonic() + WATCH_FOR
            while time.monotonic() < end:
                time.sleep(1.0)
                if permissions.status(kind) == "allowed":
                    log.info("permission %s switched on", kind)
                    with _lock:
                        _times.pop(kind, None)
                    if resume is not None:
                        resume(kind)
                    return
        finally:
            with _lock:
                _watching.discard(kind)
    threading.Thread(target=run, name=f"permit-{kind}", daemon=True).start()


# macOS's own permission boxes: Accessibility's "would like to control this computer", and Sequoia's monthly "is
# requesting to bypass the system private window picker" for Screen Recording (seen 9 Oct, 00:59, over Telegram).
_SYSTEM_BOXES = {"UserNotificationCenter", "universalAccessAuthWarn", "SecurityAgent", "tccd"}


def system_box_open() -> bool:
    """A macOS permission box is on screen (Mint can't, and mustn't, click it)."""
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        return any(str(w.get("kCGWindowOwnerName", "")) in _SYSTEM_BOXES and float(w.get("kCGWindowAlpha", 1) or 0) > 0
                   for w in windows or [])
    except Exception:
        return False


BOX_NOTE = (" [A macOS box is open on screen, likely asking about one of Mint's permissions. Mint can't answer it: "
            "tell the user in one sentence what it asks and to click Allow (or Open System Settings and switch Mint "
            "on), then carry on.]")


# What Mint knows from the start of every conversation (session.py puts it in the instructions): which permissions it
# has, what the missing ones stop, and what to do when a request needs one. Calendars and Reminders aren't read here:
# their own tools ask for them.
_KNOWN = ("accessibility", "screen", "microphone", "input")


def prompt_note(statuses: dict | None = None) -> str:
    """The permissions paragraph of the system instruction."""
    from mint.core import prefs
    name = prefs.name()
    if statuses is None:
        statuses = {}
        for kind in _KNOWN:
            try:
                statuses[kind] = permissions.status(kind)
            except Exception:
                statuses[kind] = "ask"
    on = [permissions.title(k) for k in _KNOWN if statuses.get(k) == "allowed"]
    off = [k for k in _KNOWN if statuses.get(k) != "allowed"]
    lines = [f"Your macOS permissions right now - on: {', '.join(on) or 'none'}."]
    if off:
        lines.append("Off: " + "; ".join(
            f"{permissions.title(k)} ({permissions.LOSES[k].format(name=name).rstrip('.')}"
            + (", though it may only be unconfirmed until a look" if k == "screen" else "") + ")" for k in off) + ".")
    lines.append(
        "A tool needing a permission that is off asks for it (macOS's box, else System Settings open at the right "
        "switch): tell the user in one plain sentence what to switch on and why. A macOS box is theirs - you can't "
        "click it. Still try what is asked: the tools check again every time.")
    return "\n".join(lines)
