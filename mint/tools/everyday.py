"""Everyday skills: dictation, selection, status, calendar, reminders, notes, mail.

Each function is synchronous, returns one plain sentence for the model, and never
raises for an expected failure (missing permission, app not set up) - it explains
instead, so the assistant can tell the user what to do.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import threading
import time
import urllib.parse

import AppKit

from mint.tools import fastinput


# --- helpers -----------------------------------------------------------------

def _osascript(script: str, timeout: float = 12) -> tuple[bool, str]:
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    return done.returncode == 0, (done.stdout or done.stderr or "").strip()


def _as_string(text: str) -> str:
    """Quote text for AppleScript."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _automation_hint(output: str, app: str) -> str:
    if "-1743" in output or "Not authorized" in output:
        return (f"macOS has not allowed Mint to control {app}. Approve the prompt, or turn it "
                f"on in System Settings > Privacy & Security > Automation.")
    return output[:200]


# NSPasteboard is not thread-safe: the clipboard-history watcher reading it while a tool wrote to it
# crashed Python (NSRangeException / segfault in _updateTypeCacheIfNeeded). Everyone who touches the
# pasteboard holds this lock; the watcher skips a beat when it is busy.
BOARD_LOCK = threading.RLock()


class _Clipboard:
    """Save and restore the user's clipboard around an operation that borrows it."""

    def __init__(self) -> None:
        self.board = AppKit.NSPasteboard.generalPasteboard()

    def __enter__(self):
        BOARD_LOCK.acquire()
        self.saved = self.board.stringForType_(AppKit.NSPasteboardTypeString)
        return self

    def __exit__(self, *exc) -> None:
        def restore():
            with BOARD_LOCK:
                self.board.clearContents()
                if self.saved is not None:
                    self.board.setString_forType_(self.saved, AppKit.NSPasteboardTypeString)
        BOARD_LOCK.release()
        # Restore after a beat, so the paste has definitely read the new value.
        threading.Timer(0.6, restore).start()


# --- dictation and selection -------------------------------------------------

def type_text(text: str, press_return: bool = False, pid: int | None = None) -> str:
    """Insert text at the cursor in whatever field has focus. Instant.

    With `pid`, the paste goes only to that process.
    """
    if not text:
        return "Nothing to type."
    if not fastinput.has_accessibility():
        return ("Cannot type: Mint lacks Accessibility permission. Use the desktop tool "
                "instead, or grant Accessibility.")
    _wait_for_page()
    editor = _focus_web_editor()
    if not editor:
        problem = _no_text_field()
        if problem:
            # Refuse rather than paste into a page and press Return on it: in
            # testing, text meant for a Google Doc was sent to a Gmail tab that
            # had come to the front, where Return would open an email.
            return problem
    from mint.screen import ocr
    target = ocr._front_window()   # the window being typed into, fixed now
    before = _visible_text(target)
    from mint.ui.effects import fx
    fx.highlight_focused(seconds=min(4.0, 1.6 + len(text) / 400), label="Typing")
    from mint.app import control
    if control.stopped():
        return "STOPPED by the user before typing; nothing was typed."
    with _Clipboard() as clip:
        clip.board.clearContents()
        clip.board.setString_forType_(text, AppKit.NSPasteboardTypeString)
        # Pasting is instant and exact, where synthesising each keystroke is slow
        # and mangles non-ASCII text and keyboard layouts.
        fastinput.press_key("v", ["command"], pid=pid)
        time.sleep(0.12)
        if press_return:
            fastinput.press_key("return", pid=pid)
    done = f"Typed {len(text)} characters" + (" and pressed Return" if press_return else "")
    verdict = _verify_typed(text, before, target)
    if press_return and "FAILED" in verdict:
        # Return in a chat box sends the message and clears the box, and the app
        # may still be switching to the conversation. Reported as FAILED, the model
        # typed the prompt again (ChatGPT, 24 Sep) - so say what is known.
        verdict = (". Not confirmed on screen yet: Return may already have SENT it (a chat clears its box). "
                   "Check with look before typing it again - never send it twice.")
    if "FAILED" not in verdict and "not verified" not in verdict and "Not confirmed" not in verdict:
        verdict += _name_untitled_doc(text)
    return done + verdict


def _name_untitled_doc(text: str) -> str:
    """Give a new Google Doc a real name, from the first line written into it.

    Every new doc is "Untitled document". In testing, "add a line to that
    briefing doc" then matched an older untitled test doc and edited the wrong
    one. A person names a document when they make it; so does this.
    """
    import AppKit
    import ApplicationServices as AX

    from mint.tools.documents import _ax, _find_web_area

    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return ""
    app = AX.AXUIElementCreateApplication(front.processIdentifier())
    window = _ax(app, "AXFocusedWindow")
    title = (_ax(window, "AXTitle") or "") if window is not None else ""
    _debug(f"name: window title is '{title[:60]}'")
    if not title.startswith("Untitled document"):
        return ""
    name = next((line.strip(" -#*") for line in text.splitlines() if line.strip()), "")[:60].strip()
    if not name:
        return ""

    # Find the title box ("Rename") and replace its text.
    area = _find_web_area(window)
    stack, field, seen = [area] if area is not None else [], None, 0
    while stack and seen < 3000 and field is None:
        node = stack.pop()
        seen += 1
        if _ax(node, "AXRole") == "AXTextField" and "rename" in str(_ax(node, "AXDescription") or "").lower():
            field = node
        stack.extend(_ax(node, "AXChildren") or [])
    _debug(f"name: title field {'found' if field is not None else 'NOT found'} after {seen} nodes")
    if field is None:
        return ""
    AX.AXUIElementSetAttributeValue(field, "AXFocused", True)
    time.sleep(0.3)
    fastinput.press_key("a", ["command"])
    with _Clipboard() as clip:
        clip.board.clearContents()
        clip.board.setString_forType_(name, AppKit.NSPasteboardTypeString)
        fastinput.press_key("v", ["command"])
        time.sleep(0.15)
    fastinput.press_key("return")
    time.sleep(1.0)
    new_title = _ax(_ax(app, "AXFocusedWindow"), "AXTitle") or ""
    _debug(f"name: window title after renaming is '{new_title[:60]}'")
    if new_title.startswith(name[:20]):
        return f" Named the new doc '{name}' - refer to it by that name from now on."
    return ""


def _wait_for_page(limit: float = 12.0) -> None:
    """If the front window is a web page still loading, wait for it.

    "Loaded" is not enough: 2.5s after opening docs.new the tab had loaded an
    intermediate page titled just "Untitled" and had not yet become the Doc.
    Wait for both a loaded page and a real title.
    """
    import AppKit
    import ApplicationServices as AX

    from mint.tools.documents import _ax, _find_web_area

    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return
    app = AX.AXUIElementCreateApplication(front.processIdentifier())
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        window = _ax(app, "AXFocusedWindow")
        title = (_ax(window, "AXTitle") or "") if window is not None else ""
        area = _find_web_area(window) if window is not None else None
        placeholder = title.split(" - ")[0].strip() in ("", "Untitled", "New Tab", "Loading…", "Loading...")
        if (area is None or _ax(area, "AXLoaded") is not False) and not placeholder:
            return
        time.sleep(0.3)


def _debug(message: str) -> None:
    import logging
    logging.getLogger("mint.tools.everyday").info(message)
    if os.environ.get("MINT_DEBUG"):
        print(f"  [debug] {message}", flush=True)


# Web editors that draw their own page and take typing only after a click into
# it. In testing, a paste into a freshly opened Google Doc went nowhere until
# the page had been clicked.
_WEB_EDITORS = ("Google Docs", "Google Sheets", "Google Slides", "Notion")


_EDITABLE_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}


def _no_text_field() -> str | None:
    """A FAILED message if nothing that accepts text has keyboard focus, else None."""
    import AppKit
    import ApplicationServices as AX

    from mint.tools.documents import _ax

    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return "FAILED: no app is in front to type into."
    app = AX.AXUIElementCreateApplication(front.processIdentifier())
    focused = _ax(app, "AXFocusedUIElement")
    if focused is not None and (_ax(focused, "AXRole") in _EDITABLE_ROLES
                                or _ax(focused, "AXEditableAncestor") is not None):
        return None
    window = _ax(app, "AXFocusedWindow")
    title = (_ax(window, "AXTitle") or "") if window is not None else ""
    return (f"FAILED: nothing was typed - no text field has focus in the front window "
            f"('{title[:70]}' in {front.localizedName()}). Bring the right window or tab to the "
            "front and click into the field first (desktop tool: 'click the ... tab' / "
            "'click the ... field'), then type again.")


def _focus_web_editor() -> bool:
    """Click into the page body of a web editor, the way a person would first.
    Returns True if the front window is a known web editor."""
    import AppKit
    import ApplicationServices as AX
    import Quartz

    from mint.tools.documents import _ax, _find_web_area

    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return False
    app = AX.AXUIElementCreateApplication(front.processIdentifier())
    window = _ax(app, "AXFocusedWindow")
    title = (_ax(window, "AXTitle") or "") if window is not None else ""
    if not any(editor in title for editor in _WEB_EDITORS):
        _debug(f"focus: '{title[:50]}' is not a known web editor; not clicking")
        return False
    # Give the editor's own text input keyboard focus through Accessibility.
    # An earlier version clicked the middle of the page instead; in a narrow
    # window that point was Google Docs' tab sidebar, and the click created a
    # new document tab. Focus the real element; never guess a position.
    # A web editor draws its page before its input exists: typed ~2s after
    # opening, Google Docs had no "Document content" yet and the text was lost.
    # Wait (up to 10s) for a body input to appear, not just for the page.
    target, deadline = None, time.monotonic() + 10
    while time.monotonic() < deadline:
        window = _ax(app, "AXFocusedWindow") or window
        area = _find_web_area(window)
        target = _largest_text_input(area) if area is not None else None
        if target is not None and _ax(target, "AXRole") == "AXTextArea":
            break
        time.sleep(0.4)
    if target is None:
        _debug("focus: no text input found in the editor page")
        return False
    if "Google Docs" in title:
        # The editor's input appears before the editor is ready to take a
        # paste; typed straight away, the text was dropped.
        time.sleep(1.2)
    AX.AXUIElementSetAttributeValue(target, "AXFocused", True)
    time.sleep(0.3)
    _debug(f"focus: focused {_ax(target, 'AXRole')} '{_ax(target, 'AXDescription') or _ax(target, 'AXTitle')}'")
    if "Google Docs" in title:
        # Append at the end rather than wherever the cursor last was.
        fastinput.press_key("down", ["command"])
        time.sleep(0.15)
    return True


def _largest_text_input(root, limit: int = 3000):
    """An editor's main body input under `root`.

    Multi-line text areas (a document body) beat single-line fields, and title
    or rename fields are skipped: in testing the widest input on a new Google
    Doc was its "Rename" title box, and the briefing became the doc's title.
    """
    import ApplicationServices as AX

    from mint.tools.documents import _ax

    best, best_rank, stack, seen = None, (-1, 0.0), [root], 0
    while stack and seen < limit:
        node = stack.pop()
        seen += 1
        role = _ax(node, "AXRole")
        if role in ("AXTextArea", "AXTextField"):
            label = " ".join(str(_ax(node, a) or "") for a in ("AXDescription", "AXTitle", "AXPlaceholderValue")).lower()
            if not any(word in label for word in ("rename", "title", "search", "find")):
                size = _ax(node, "AXSize")
                dims = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)[1] if size is not None else None
                area = dims.width * dims.height if dims is not None else 0.0
                named_body = any(word in label for word in ("document content", "page content", "editor", "body"))
                rank = (2 if named_body else 1 if role == "AXTextArea" else 0, area)
                if rank > best_rank:
                    best, best_rank = node, rank
        stack.extend(_ax(node, "AXChildren") or [])
    return best


def _visible_text(window: dict | None = None) -> str | None:
    """A window's text as it looks right now, lowercased; None if unreadable.

    Pass the window typed into: reading "whatever is in front" at check time
    read the wrong window whenever the user switched apps in between.
    """
    from mint.screen import ocr

    try:
        items, _ = ocr.read_screen()
    except Exception:
        return None
    region = window or ocr._front_window()
    return " ".join(i["text"] for i in items if region is None or ocr._inside(i, region)).lower()


def _verify_typed(text: str, before: str | None, target: dict | None = None) -> str:
    """Check the text newly appeared in the front window.

    Sending a paste is not the same as the text arriving: in testing a paste
    went to a Chrome profile-picker window and the tool still reported success.
    And "visible afterwards" is not enough either: the same words were already
    on screen in an open PDF, and the check passed on an empty document. So
    only text that was not there before counts. A read takes about 0.1s.
    """
    import re

    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", text)]
    if len(words) < 2:
        return "."   # too short to check reliably
    time.sleep(0.6)
    # If the user brought another app forward, its window now covers the one
    # typed into, and reading the screen proves nothing either way.
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if target is not None and front is not None and (front.localizedName() or "") != target.get("app"):
        return (f" (typed, but not verified: {front.localizedName()} came to the front before it "
                "could be checked).")
    after = _visible_text(target)
    if after is None or before is None:
        return " (not verified: the screen could not be read)."
    # Any distinctive run of two or three typed words that is newly on screen
    # counts. Stricter versions failed on text that had landed: a document had
    # scrolled sideways so its opening words were hidden, and in a narrow window
    # every line was cut off ("Mint test bri…", "Look what y…").
    runs = []
    for size in (3, 2):
        for i in range(len(words) - size + 1):
            run = tuple(words[i:i + size])
            if len("".join(run)) >= 8:
                runs.append(run)

    def appeared(now: str) -> bool:
        for run in runs[:80]:
            pattern = r"\b" + r"\W+".join(re.escape(w) for w in run)
            if len(re.findall(pattern, now)) > len(re.findall(pattern, before)):
                return True
        return False

    if appeared(after):
        return ", and it is now visible on screen."
    # A long paste can take a moment to render; look once more before failing.
    time.sleep(1.2)
    again = _visible_text(target)
    if again is not None and appeared(again):
        return ", and it is now visible on screen."
    return (". FAILED: the text did not appear in the front window afterwards, so it did not "
            "land where intended - the field may not have had focus, or another window was in front.")


def get_selected_text() -> str:
    """The text currently selected in the front app."""
    if not fastinput.has_accessibility():
        return "Cannot read the selection: Mint lacks Accessibility permission."
    with _Clipboard() as clip:
        before = clip.board.changeCount()
        fastinput.press_key("c", ["command"])
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline and clip.board.changeCount() == before:
            time.sleep(0.03)
        if clip.board.changeCount() == before:
            return "Nothing is selected, or the front app does not allow copying."
        text = clip.board.stringForType_(AppKit.NSPasteboardTypeString) or ""
    text = str(text)
    if not text.strip():
        return "The selection holds no text."
    return text if len(text) <= 8000 else text[:8000] + " … (truncated)"


# --- status ------------------------------------------------------------------

def get_status() -> str:
    """Local time and date, battery, and volume."""
    now = dt.datetime.now()
    parts = [now.strftime("It is %-I:%M %p on %A, %B %-d, %Y")]

    batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    if found := re.search(r"(\d+)%;\s*([^;]+);", batt):
        level, state = found.group(1), found.group(2).strip()
        parts.append(f"battery {level}% ({state})")

    ok, volume = _osascript("output volume of (get volume settings)", timeout=4)
    if ok and volume.isdigit():
        parts.append(f"volume {volume}%")
    return ", ".join(parts) + "."


def notify(title: str, message: str = "") -> str:
    _osascript(f"display notification {_as_string(message)} with title {_as_string(title)}")
    return "Notification shown."


def system_action(action: str) -> str:
    action = action.strip().lower().replace(" ", "_")
    if action == "lock":
        fastinput.press_key("q", ["control", "command"])
        return "Locked the screen."
    if action == "sleep_display":
        subprocess.run(["pmset", "displaysleepnow"], check=False)
        return "Display is going to sleep."
    if action in {"dark_mode_on", "dark_mode_off", "toggle_dark_mode"}:
        value = {"dark_mode_on": "true", "dark_mode_off": "false",
                 "toggle_dark_mode": "not dark mode"}[action]
        ok, out = _osascript("tell application \"System Events\" to tell appearance "
                             f"preferences to set dark mode to {value}")
        return "Done." if ok else _automation_hint(out, "System Events")
    if action in {"mute", "unmute"}:
        _osascript(f"set volume output muted {'true' if action == 'mute' else 'false'}")
        return "Muted." if action == "mute" else "Unmuted."
    return "Unknown action. Use lock, sleep_display, dark_mode_on, dark_mode_off, toggle_dark_mode, mute or unmute."


# --- calendar and reminders (EventKit) ---------------------------------------

_store = None


def _event_store(entity: int):
    """An EKEventStore with access to `entity`, asking once if needed."""
    import EventKit

    global _store
    if _store is None:
        _store = EventKit.EKEventStore.alloc().init()

    status = EventKit.EKEventStore.authorizationStatusForEntityType_(entity)
    # 3 = authorized (legacy), 4 = full access.
    if status in (3, 4):
        return _store, None
    if status in (1, 2):
        kind = "Calendars" if entity == EventKit.EKEntityTypeEvent else "Reminders"
        return None, (f"Mint is not allowed to use {kind}. Turn it on in System Settings > "
                      f"Privacy & Security > {kind}.")

    granted = threading.Event()
    result = {"ok": False}

    def done(ok, error):
        result["ok"] = bool(ok)
        granted.set()

    if entity == EventKit.EKEntityTypeEvent:
        _store.requestFullAccessToEventsWithCompletion_(done)
    else:
        _store.requestFullAccessToRemindersWithCompletion_(done)
    granted.wait(60)
    if not result["ok"]:
        return None, "Access was not granted. Approve the prompt and ask again."
    return _store, None


def calendar_events(days: int = 1) -> str:
    """Events from the start of today through `days` days."""
    import EventKit
    from Foundation import NSDate

    store, problem = _event_store(EventKit.EKEntityTypeEvent)
    if problem:
        return problem

    days = max(1, min(int(days), 14))
    start = dt.datetime.combine(dt.date.today(), dt.time.min)
    end = start + dt.timedelta(days=days)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        NSDate.dateWithTimeIntervalSince1970_(start.timestamp()),
        NSDate.dateWithTimeIntervalSince1970_(end.timestamp()), None)
    events = sorted(store.eventsMatchingPredicate_(predicate) or [],
                    key=lambda e: e.startDate().timeIntervalSince1970())
    if not events:
        return "Nothing on the calendar." if days == 1 else f"Nothing on the calendar in the next {days} days."

    lines = []
    for event in events[:25]:
        begins = dt.datetime.fromtimestamp(event.startDate().timeIntervalSince1970())
        when = "all day" if event.isAllDay() else begins.strftime("%-I:%M %p")
        day = "" if days == 1 else begins.strftime("%a ")
        place = f" at {event.location()}" if event.location() else ""
        lines.append(f"{day}{when}: {event.title()}{place}")
    return "; ".join(lines)


def create_reminder(title: str, in_minutes: int | None = None, when: str | None = None) -> str:
    """A reminder in the default list, optionally due at a time."""
    import EventKit
    import Foundation
    from Foundation import NSCalendar, NSDate

    store, problem = _event_store(EventKit.EKEntityTypeReminder)
    if problem:
        return problem

    due = None
    if in_minutes:
        due = dt.datetime.now() + dt.timedelta(minutes=int(in_minutes))
    elif when:
        try:
            due = dt.datetime.fromisoformat(when)
        except ValueError:
            return "Could not read that time. Give it as 2026-09-24T09:00, or as minutes from now."

    reminder = EventKit.EKReminder.reminderWithEventStore_(store)
    reminder.setTitle_(title)
    reminder.setCalendar_(store.defaultCalendarForNewReminders())
    if due is not None:
        calendar = NSCalendar.currentCalendar()
        units = (Foundation.NSCalendarUnitYear | Foundation.NSCalendarUnitMonth
                 | Foundation.NSCalendarUnitDay | Foundation.NSCalendarUnitHour
                 | Foundation.NSCalendarUnitMinute)
        components = calendar.components_fromDate_(
            units, NSDate.dateWithTimeIntervalSince1970_(due.timestamp()))
        reminder.setDueDateComponents_(components)
        reminder.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(
            NSDate.dateWithTimeIntervalSince1970_(due.timestamp())))

    ok, error = store.saveReminder_commit_error_(reminder, True, None)
    if not ok:
        return f"Could not save the reminder: {error}"
    return f"Reminder set: {title}" + (due.strftime(", due %A at %-I:%M %p") if due else "") + "."


# --- notes and mail ------------------------------------------------------------

def create_note(title: str, body: str = "") -> str:
    html = "<h1>" + title + "</h1>" + "".join(
        f"<div>{line or '<br>'}</div>" for line in body.split("\n"))
    ok, out = _osascript(
        f"tell application \"Notes\" to make new note with properties "
        f"{{name:{_as_string(title)}, body:{_as_string(html)}}}")
    return f"Created the note '{title}'." if ok else _automation_hint(out, "Notes")


def compose_email(to: str = "", subject: str = "", body: str = "") -> str:
    """Open a pre-filled draft in the default mail app. Never sends."""
    query = urllib.parse.urlencode({"subject": subject, "body": body},
                                   quote_via=urllib.parse.quote)
    url = f"mailto:{urllib.parse.quote(to, safe='@,')}?{query}"
    subprocess.run(["open", url], check=False)
    return "Opened a draft in the mail app. It has not been sent; the user reviews and sends it."


def list_emails(count: int = 5) -> str:
    """Most recent messages in Mail.app's inbox."""
    count = max(1, min(int(count), 15))
    script = f"""
    tell application "Mail"
        set out to ""
        set msgs to messages of inbox
        set n to count of msgs
        if n > {count} then set n to {count}
        repeat with i from 1 to n
            set m to item i of msgs
            set flag to ""
            if read status of m is false then set flag to "[unread] "
            set out to out & i & ". " & flag & (sender of m) & " — " & (subject of m) & linefeed
        end repeat
        return out
    end tell"""
    ok, out = _osascript(script, timeout=20)
    if not ok:
        return ("Could not read Mail. " + _automation_hint(out, "Mail") +
                " This works only with the Mail app set up with an account.")
    return out or "The inbox is empty."


def read_email(number: int = 1) -> str:
    """Body of the Nth message in Mail.app's inbox (1 = newest)."""
    script = f"""
    tell application "Mail"
        set m to item {int(number)} of (messages of inbox)
        return (sender of m) & linefeed & (subject of m) & linefeed & linefeed & (content of m)
    end tell"""
    ok, out = _osascript(script, timeout=20)
    if not ok:
        return "Could not read that message. " + _automation_hint(out, "Mail")
    return out if len(out) <= 6000 else out[:6000] + " … (truncated)"
