"""The macOS permissions Mint asks for: what each one is for, whether it is allowed, and asking for it.

Shared by the welcome window (onboarding's Permissions page: the six in PERMISSIONS, in its order) and
Settings (General's "Needs your OK" card for the IMPORTANT ones that are missing, and the full list - ALL,
with Full Disk Access and Automation too - on Permissions & Privacy).

status() never prompts and is cheap, but it asks macOS (and, for Automation, other apps): Settings calls it
on its reader thread. ask() shows macOS's own prompt the first time, else opens the right pane of System
Settings (only there can a "no" become a "yes").
"""

from __future__ import annotations

import logging
import os
import threading

import AppKit
import Quartz

log = logging.getLogger("mint.core.permissions")

# (kind, SF Symbol, rgb, title, why, required) - the welcome window's six.
PERMISSIONS = (
    ("microphone", "mic.fill", (1.0, 0.42, 0.62), "Microphone", "So I can hear my wake word and everything you ask.", True),
    ("accessibility", "hand.point.up.left.fill", (0.36, 0.56, 1.0), "Accessibility", "To click, type, scroll and arrange windows for you.", False),
    ("screen", "rectangle.dashed.badge.record", (0.64, 0.47, 1.0), "Screen Recording", "To see what's on your screen when you ask about it.", False),
    ("input", "keyboard.fill", (1.0, 0.62, 0.22), "Input Monitoring", "For the dictation key, and when you teach me a task.", False),
    ("calendar", "calendar", (1.0, 0.36, 0.33), "Calendars", "To tell you what's next and add events.", False),
    ("reminders", "checklist", (0.3, 0.86, 0.55), "Reminders", "To remind you of things at the right time.", False),
)
# Settings lists these too: macOS never asks for Full Disk Access, and asks for Automation once per app.
EXTRA = (
    ("files", "externaldrive.fill", (0.42, 0.5, 0.62), "Full Disk Access",
     "To read Safari bookmarks, past notifications and other protected files.", False),
    ("automation", "gearshape.2.fill", (0.2, 0.68, 0.9), "Automation",
     "To work inside Notes, Mail, Music and other apps. macOS asks once for each app.", False),
)
ALL = PERMISSIONS + EXTRA
PANES = {"microphone": "Privacy_Microphone", "accessibility": "Privacy_Accessibility", "screen": "Privacy_ScreenCapture",
         "input": "Privacy_ListenEvent", "calendar": "Privacy_Calendars", "reminders": "Privacy_Reminders",
         "files": "Privacy_AllFiles", "automation": "Privacy_Automation"}
# Missing one of these breaks something people use every day: Settings ▸ General asks for them at the top.
IMPORTANT = ("microphone", "accessibility", "screen", "calendar")
# What stops working without it ({name}: the assistant's name), in plain words.
LOSES = {
    "microphone": "{name} can't hear you - no wake word, no talking to it.",
    "accessibility": "{name} can't click, type or move windows for you, and dictation can't type.",
    "screen": "{name} can't see your screen when you ask about what's on it.",
    "input": "The dictation key and teaching {name} a task don't work.",
    "calendar": "{name} can't tell you what's next or add events.",
    "reminders": "{name} can't read or add your reminders.",
    "files": "{name} can't search or open files in protected folders, Safari bookmarks or past notifications.",
    "automation": "{name} can't work inside Notes, Mail, Music and similar apps.",
}
# What it is for, in Settings' words.
FOR = {
    "microphone": "Hearing the wake word and everything you ask.",
    "accessibility": "Clicking, typing, scrolling and arranging windows for you.",
    "screen": "Seeing your screen when you ask about it.",
    "input": "The dictation key, and teaching {name} a task.",
    "calendar": "What's next, and adding events.",
    "reminders": "Reading and adding reminders.",
    "files": "Safari bookmarks, past notifications and other protected files.",
    "automation": "Working inside Notes, Mail, Music and other apps. macOS asks once for each.",
}
# Apps whose Automation answer we read (only while they run - macOS can't tell otherwise) and ask for.
AUTOMATION_APPS = (("com.apple.finder", "Finder"), ("com.apple.systemevents", "System Events"),
                   ("com.apple.Notes", "Notes"), ("com.apple.mail", "Mail"), ("com.apple.Music", "Music"))
ASK_AUTOMATION = (("com.apple.systemevents", "System Events"), ("com.apple.Notes", "Notes"))
_store = [None]          # the EventKit store a calendar/reminders request runs on (kept alive for its answer)


def title(kind: str) -> str:
    return next((t for k, _, _, t, _, _ in ALL if k == kind), kind)


def status(kind: str, asked=()) -> str:
    """'allowed', 'denied' (only System Settings can change it) or 'ask'. Never prompts. `asked`: the kinds
    already asked for here - one macOS can't report on that is still not allowed then counts as denied."""
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
                # macOS says yes, but its answer can be stale: switched on, yet every picture is grey but for Mint's
                # own windows (10 Oct, after its monthly check / an update). Then it is not allowed in any way that
                # matters - so the Set up chips, Settings and the ask flow all treat it as off.
                return "denied" if screen_blind() else "allowed"
        elif kind == "input":
            if Quartz.CGPreflightListenEventAccess():
                return "allowed"
        elif kind in ("calendar", "reminders"):
            import EventKit
            entity = EventKit.EKEntityTypeEvent if kind == "calendar" else EventKit.EKEntityTypeReminder
            status = int(EventKit.EKEventStore.authorizationStatusForEntityType_(entity))
            return "allowed" if status in (3, 4) else ("ask" if status == 0 else "denied")
        elif kind == "files":
            return _full_disk()
        elif kind == "automation":
            return _automation()
    except Exception:
        log.debug("permission status %s", kind, exc_info=True)
    return "denied" if kind in asked else "ask"


_blind_seen = {"at": -1e9, "blind": False}
BLIND_RECHECK = 30.0        # seconds a "can it really see?" answer is kept (it takes a picture)


def screen_blind() -> bool:
    """Screen Recording is on for macOS but captures show only Mint's own windows (vision.sees_other_apps)."""
    import time
    now = time.monotonic()
    if now - _blind_seen["at"] < BLIND_RECHECK:
        return _blind_seen["blind"]
    try:
        from mint.screen import vision
        blind = vision.sees_other_apps() is False
    except Exception:
        blind = False
    _blind_seen.update(at=now, blind=blind)
    return blind


def snapshot(asked=(), kinds=None) -> dict:
    """{kind: status} for every permission (or `kinds`). Off the main thread (Automation asks other apps)."""
    return {kind: status(kind, asked) for kind in (kinds or [k for k, *_ in ALL])}


def missing_important(statuses: dict) -> list[str]:
    return [kind for kind in IMPORTANT if statuses.get(kind, "allowed") != "allowed"]


def _full_disk() -> str:
    """Full Disk Access has no API: reading a file only it opens is the check. macOS never asks for it."""
    home = os.path.expanduser("~")
    for path in (f"{home}/Library/Application Support/com.apple.TCC/TCC.db", f"{home}/Library/Safari/Bookmarks.plist",
                 f"{home}/Library/Safari"):
        try:
            if os.path.isdir(path):
                os.listdir(path)
            else:
                os.close(os.open(path, os.O_RDONLY))
            return "allowed"
        except FileNotFoundError:
            continue
        except OSError:
            return "denied"
    return "denied"


def _automation() -> str:
    """Denied for any app that answers no; allowed when some app said yes; else not asked yet. Only running
    apps can be read without asking."""
    from mint.tools import connectors
    answers = [connectors.automation(bundle) for bundle, _ in AUTOMATION_APPS]
    if connectors.AE_DENIED in answers:
        return "denied"
    return "allowed" if connectors.AE_OK in answers else "ask"


def granted_now(kind: str) -> bool:
    """Calendars / Reminders, looked at afresh: a running app's status() can stay "not asked" after the switch
    is turned on in System Settings, but a new store sees the lists at once. Off the main thread."""
    try:
        import EventKit
        entity = EventKit.EKEntityTypeEvent if kind == "calendar" else EventKit.EKEntityTypeReminder
        return bool(EventKit.EKEventStore.alloc().init().calendarsForEntityType_(entity))
    except Exception:
        log.debug("fresh look at %s", kind, exc_info=True)
        return False


def open_pane(kind: str) -> None:
    AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(
        f"x-apple.systempreferences:com.apple.preference.security?{PANES[kind]}"))


def ask(kind: str, asked: set, done=None) -> None:
    """Main thread (a click). macOS's own prompt the first time it can ask, else the pane of System Settings
    where the user turns Mint on. `asked` (the caller's set) remembers what was asked already.
    done(ok), for Calendars and Reminders, gets macOS's answer (any thread). macOS only shows those two boxes
    for the app in front, so asking from the notch brings Mint to the front first (the caller puts the app
    you were in back)."""
    if kind == "automation":                # reading it asks other apps, and asking waits for an answer: a thread
        first = kind not in asked
        asked.add(kind)
        threading.Thread(target=_ask_automation, args=(first,), daemon=True, name="permissions-automation").start()
        return
    current = status(kind, asked)
    if current == "allowed":
        return
    first = kind not in asked
    asked.add(kind)
    if kind == "screen" and Quartz.CGPreflightScreenCaptureAccess():
        open_pane(kind)            # switched on but not working: macOS shows no box for it - only the switch helps
        return
    try:
        if current == "ask" and first:
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
                # macOS shows its box only once per app; after that the call does nothing on screen (10 Oct: asked,
                # nothing appeared). The switch's page opens too, so there is always something to answer.
                open_pane(kind)
                return
            if kind == "input":
                Quartz.CGRequestListenEventAccess()
                return
            if kind in ("calendar", "reminders"):
                import EventKit
                if _store[0] is None:
                    _store[0] = EventKit.EKEventStore.alloc().init()
                if done is not None:
                    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

                def answered(ok, error):
                    if error is not None:
                        log.info("%s request: %s", kind, error)
                    if done is not None:
                        done(bool(ok))
                if kind == "calendar":
                    _store[0].requestFullAccessToEventsWithCompletion_(answered)
                else:
                    _store[0].requestFullAccessToRemindersWithCompletion_(answered)
                return
    except Exception:
        log.debug("permission request %s", kind, exc_info=True)
    open_pane(kind)


def _ask_automation(first: bool) -> None:
    """macOS asks once per app, and only while it runs: ask for the two Mint uses most (blocks until answered).
    Asked before, or already turned off for an app: System Settings ▸ Automation instead."""
    from mint.tools import connectors
    if not first or _automation() == "denied":
        open_pane("automation")
        return
    for bundle, name in ASK_AUTOMATION:
        try:
            log.info("automation: %s", connectors.ask_automation(bundle, name))
        except Exception:
            log.debug("automation request %s", name, exc_info=True)
