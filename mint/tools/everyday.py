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


def _not_a_copy(board) -> None:
    """Tell the clipboard history that the clipboard's current contents are Mint borrowing it (a paste),
    not something the user copied (bench: Mint's own pastes showed up as the user's copies)."""
    try:
        from mint.tools import clipboard as clip_tools
        clip_tools._own_counts.add(board.changeCount())
    except Exception:
        pass


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
                _not_a_copy(self.board)
        _not_a_copy(self.board)            # what Mint pasted, and then the user's clip put back, are not copies
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
    if "FAILED" not in verdict:
        from mint.tools import undo
        undo.typed(text, press_return, pid)
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
    from mint.screen import axkit
    if axkit.recent_click(front.processIdentifier(), 30.0) is not None:
        # Mint just clicked into this app (click_text / click_at / ui_act): Electron apps hide the
        # box that has the cursor from Accessibility, so trust the click; _verify_typed checks after.
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
        was = _dark_mode()
        ok, out = _osascript("tell application \"System Events\" to tell appearance "
                             f"preferences to set dark mode to {value}")
        if ok and was is not None and _dark_mode() != was:
            from mint.tools import undo
            undo.record("dark_mode", f"{'light' if was else 'dark'} mode", {"kind": "dark_mode", "on": was})
        return "Done." if ok else _automation_hint(out, "System Events")
    if action in {"mute", "unmute"}:
        ok, was = _osascript("output muted of (get volume settings)", timeout=4)
        _osascript(f"set volume output muted {'true' if action == 'mute' else 'false'}")
        if ok and was in ("true", "false") and (was == "true") != (action == "mute"):
            from mint.tools import undo
            undo.record("mute", "muting the sound" if action == "mute" else "unmuting the sound",
                        {"kind": "mute", "muted": was == "true"})
        return "Muted." if action == "mute" else "Unmuted."
    return "Unknown action. Use lock, sleep_display, dark_mode_on, dark_mode_off, toggle_dark_mode, mute or unmute."


def _dark_mode() -> bool | None:
    """Whether dark mode is on, read without asking System Events (no Automation prompt)."""
    try:
        done = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True,
                              timeout=4, check=False)
        return done.stdout.strip() == "Dark"
    except (OSError, subprocess.TimeoutExpired):
        return None


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
        # A long-lived store does not see calendars and lists made since (by the user in Calendar, or by
        # a script): bench, "no calendar called 'Mint Bench'" a minute after it was made.
        try:
            _store.refreshSourcesIfNecessary()
        except Exception:
            pass
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


def calendar_rows(day: dt.date | None = None) -> list[dict]:
    """One day's events as data (for the island's schedule card): {"start", "end" (datetimes), "title",
    "all_day", "place", "rgb"}. [] when the calendar cannot be read."""
    import AppKit
    import EventKit
    from Foundation import NSDate

    store, problem = _event_store(EventKit.EKEntityTypeEvent)
    if problem:
        return []
    start = dt.datetime.combine(day or dt.date.today(), dt.time.min)
    end = start + dt.timedelta(days=1)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        NSDate.dateWithTimeIntervalSince1970_(start.timestamp()),
        NSDate.dateWithTimeIntervalSince1970_(end.timestamp()), None)
    rows = []
    for event in sorted(store.eventsMatchingPredicate_(predicate) or [],
                        key=lambda e: e.startDate().timeIntervalSince1970())[:30]:
        rgb = (0.04, 0.52, 1.0)
        try:
            color = event.calendar().color().colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
            rgb = (color.redComponent(), color.greenComponent(), color.blueComponent())
        except Exception:
            pass
        rows.append({"start": dt.datetime.fromtimestamp(event.startDate().timeIntervalSince1970()),
                     "end": dt.datetime.fromtimestamp(event.endDate().timeIntervalSince1970()),
                     "title": str(event.title() or "(untitled)"), "all_day": bool(event.isAllDay()),
                     "place": str(event.location() or ""), "rgb": rgb})
    return rows


# --- where things go: the list, folder or calendar the user named ---------------------------------

def _plain_name(name: str, kind: str) -> str:
    """'my Mint Bench list' -> 'mint bench' (for comparing a spoken name with a real one)."""
    text = " ".join(str(name or "").lower().replace("’", "'").strip(" '\"").split())
    text = re.sub(r"^(?:my|the)\s+", "", text)
    return re.sub(rf"\s+{kind}$", "", text).strip()


def _pick(names: list[str], wanted: str, kind: str) -> str | None:
    """The real name among `names` that `wanted` means (case and 'my … list' ignored), or None."""
    key = _plain_name(wanted, kind)
    if not key:
        return None
    exact = next((n for n in names if _plain_name(n, kind) == key), None)
    if exact:
        return exact
    # "MintBench" for "Mint Bench", "work-notes" for "Work Notes": the same name without spaces or marks
    # (bench: a new "MintBench" folder was made next to the user's "Mint Bench").
    squash = re.sub(r"[^a-z0-9]", "", key.lower())
    return next((n for n in names if re.sub(r"[^a-z0-9]", "", _plain_name(n, kind).lower()) == squash), None)


def _named_in_request(names: list[str], kind: str) -> str | None:
    """An existing list/folder/calendar the user's request names as one ("my Mint Bench list",
    "the folder called Work"). Needed because the model often leaves the list out: in the 29 Sep
    reliability run "add … to my Mint Bench list" went to the default list three times."""
    try:
        from mint.app import live
        text = " ".join(live.request().lower().replace("’", "'").split())
    except Exception:
        return None
    if not text:
        return None
    for name in sorted(names, key=len, reverse=True):
        n = re.escape(" ".join(name.lower().split()))
        if re.search(rf"\b{n}\s+{kind}s?\b|\b{kind}\s+(?:called|named)\s+['\"]?{n}\b", text):
            return name
    return None


def _reminder_calendar(store, name: str | None, create: bool):
    """(EKCalendar or None for the default, problem or None) for a Reminders list the user named."""
    import EventKit

    lists = list(store.calendarsForEntityType_(EventKit.EKEntityTypeReminder) or [])
    titles = [str(c.title()) for c in lists]
    wanted = name or _named_in_request(titles, "list")
    if not wanted:
        return store.defaultCalendarForNewReminders(), None
    real = _pick(titles, wanted, "list")
    if real is not None:
        return lists[titles.index(real)], None
    label = " ".join(str(wanted).strip(" '\"").split())
    if not create:
        return None, (f"NOT CREATED: Reminders has no list called '{label}'. Its lists: {', '.join(titles)}. "
                      "Ask the user whether to make that list (then call again with create_list=true) or which "
                      "list to use - do not put it in another list without asking.")
    default = store.defaultCalendarForNewReminders()
    made = EventKit.EKCalendar.calendarForEntityType_eventStore_(EventKit.EKEntityTypeReminder, store)
    made.setTitle_(label)
    made.setSource_(default.source())
    ok, error = store.saveCalendar_commit_error_(made, True, None)
    if not ok:
        return None, f"FAILED: could not make the Reminders list '{label}': {error}"
    return made, None


def _open_reminders(store, calendar) -> list:
    """The incomplete reminders in one list."""
    done = threading.Event()
    found: list = []

    def got(reminders):
        found.extend(reminders or [])
        done.set()

    predicate = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, [calendar])
    store.fetchRemindersMatchingPredicate_completion_(predicate, got)
    done.wait(20)
    return found


# The reminder Mint made last: a follow-up ("actually make it 11") should change it, not add a second
# one. In the 29 Sep run the follow-up came back as a new reminder titled "… (bring the invoice)".
_last_reminder: dict = {}


def _same_reminder(store, calendar, title: str) -> tuple[object | None, str]:
    """(an open reminder this call should update instead of duplicating, leftover words for its notes)."""
    key = " ".join(title.lower().split())
    for item in _open_reminders(store, calendar):
        if " ".join(str(item.title() or "").lower().split()) == key:
            return item, ""
    last = _last_reminder
    if last and time.monotonic() - last["at"] < 15 * 60:
        old = " ".join(last["title"].lower().split())
        rest = re.match(r"^\s*(?:\((.+)\)|[-–:,]\s*(.+))\s*$", key[len(old):]) if key.startswith(old) else None
        if rest:
            item = store.calendarItemWithIdentifier_(last["id"])
            if item is not None and not item.isCompleted():
                start = len(title) - len(key[len(old):].lstrip())
                return item, title[start:].strip(" ()-–:,")
    return None, ""


def _due_components(due: dt.datetime):
    import Foundation
    from Foundation import NSCalendar, NSDate

    units = (Foundation.NSCalendarUnitYear | Foundation.NSCalendarUnitMonth | Foundation.NSCalendarUnitDay
             | Foundation.NSCalendarUnitHour | Foundation.NSCalendarUnitMinute)
    return NSCalendar.currentCalendar().components_fromDate_(units, NSDate.dateWithTimeIntervalSince1970_(due.timestamp()))


def _say_day(when: dt.datetime) -> tuple[dt.datetime, bool]:
    """"Tomorrow" / "today" / "tonight" in the user's request decide the day, whatever date the model worked
    out: just after midnight it still counted from the day before (bench, a run that crossed midnight)."""
    from mint.app import live
    request = " ".join((live.request() or "").lower().split())
    today = dt.date.today()
    if re.search(r"\bday after tomorrow\b", request):
        day = today + dt.timedelta(days=2)
    elif re.search(r"\btomorrow\b", request):
        day = today + dt.timedelta(days=1)
    elif re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", request):
        day = today
    else:
        return when, False
    if when.date() == day or re.search(r"\b(\d{1,2} (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|"
                                       r"(mon|tues|wednes|thurs|fri|satur|sun)day)", request):
        return when, False                                   # the right day, or the request names a date too
    print(f"  [date: {when.date()} -> {day}, as the request says]", flush=True)
    return when.replace(year=day.year, month=day.month, day=day.day), True


def create_reminder(title: str, in_minutes: int | None = None, when: str | None = None,
                    list_name: str | None = None, notes: str | None = None, create_list: bool = False) -> str:
    """A reminder, optionally due at a time, in the list the user named (else the default list).

    A list that does not exist is made only with `create_list`; otherwise the answer says so and lists
    the real ones. An open reminder with the same title in that list (or the one just made, when the
    new title only adds words to it) is changed instead of duplicated.
    """
    import EventKit
    from Foundation import NSDate

    store, problem = _event_store(EventKit.EKEntityTypeReminder)
    if problem:
        return problem
    title = " ".join(str(title or "").split())
    if not title:
        return "FAILED: a reminder needs a title."

    due = None
    if in_minutes:
        due = dt.datetime.now() + dt.timedelta(minutes=int(in_minutes))
    elif when:
        try:
            due = dt.datetime.fromisoformat(str(when).strip())
        except ValueError:
            return "Could not read that time. Give it as 2026-09-24T09:00, or as minutes from now."
        due, _moved = _say_day(due)

    calendar, problem = _reminder_calendar(store, list_name, bool(create_list))
    if problem:
        return problem
    existing, extra = _same_reminder(store, calendar, title) if calendar is not None else (None, "")
    reminder = existing or EventKit.EKReminder.reminderWithEventStore_(store)
    if existing is None:
        reminder.setTitle_(title)
        reminder.setCalendar_(calendar)
    notes = " ".join(str(notes).split()) if notes else extra
    if notes:
        reminder.setNotes_(notes)
    if due is not None:
        reminder.setDueDateComponents_(_due_components(due))
        for alarm in list(reminder.alarms() or []):
            reminder.removeAlarm_(alarm)
        reminder.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(NSDate.dateWithTimeIntervalSince1970_(due.timestamp())))

    ok, error = store.saveReminder_commit_error_(reminder, True, None)
    if not ok:
        return f"Could not save the reminder: {error}"
    where = str(reminder.calendar().title()) if reminder.calendar() is not None else "Reminders"
    name = str(reminder.title())
    _last_reminder.update({"id": str(reminder.calendarItemIdentifier()), "title": name, "at": time.monotonic()})
    when_text = due.strftime(", due %A %-d %B at %-I:%M %p") if due else ""
    from mint.tools import undo
    if existing is not None:
        undo.record("reminder", f"changing the reminder '{name}'", None,
                    "change it back in Reminders; it was an existing reminder, so it was not deleted.")
        return (f"Updated the existing reminder '{name}' in the '{where}' list (not duplicated){when_text}"
                + (f", notes: {notes}" if notes else "") + ".")
    undo.record("reminder", f"creating the reminder '{name}'",
                {"kind": "reminder_delete", "id": str(reminder.calendarItemIdentifier()), "title": name})
    return f"Reminder set in the '{where}' list: {name}{when_text}" + (f", notes: {notes}" if notes else "") + "."


# --- calendar events --------------------------------------------------------------------------------

def _event_calendar(store, name: str | None, create: bool):
    """(EKCalendar, problem) for the calendar the user named, else the default one."""
    import EventKit

    calendars = [c for c in (store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or [])
                 if c.allowsContentModifications()]
    titles = [str(c.title()) for c in calendars]
    wanted = name or _named_in_request(titles, "calendar")
    if not wanted:
        return store.defaultCalendarForNewEvents(), None
    real = _pick(titles, wanted, "calendar")
    if real is not None:
        return calendars[titles.index(real)], None
    label = " ".join(str(wanted).strip(" '\"").split())
    if not create:
        return None, (f"NOT BOOKED: there is no calendar called '{label}' that can be written to. Calendars: "
                      f"{', '.join(titles)}. Ask the user which one to use, or whether to make it (then call "
                      "again with create_calendar=true).")
    made = EventKit.EKCalendar.calendarForEntityType_eventStore_(EventKit.EKEntityTypeEvent, store)
    made.setTitle_(label)
    made.setSource_(store.defaultCalendarForNewEvents().source())
    ok, error = store.saveCalendar_commit_error_(made, True, None)
    if not ok:
        return None, f"FAILED: could not make the calendar '{label}': {error}"
    return made, None


def next_free(busy: list[tuple[dt.datetime, dt.datetime]], start: dt.datetime, length: dt.timedelta,
              latest: dt.datetime) -> dt.datetime | None:
    """The first start at or after `start`, on a half-hour step, where `length` fits between the busy spans
    and ends by `latest`. None if it does not fit."""
    slot = start
    while slot + length <= latest:
        clash = [b for b in busy if b[0] < slot + length and slot < b[1]]
        if not clash:
            return slot
        slot = max(e for _, e in clash)
        if slot.minute % 30 or slot.second:                     # back onto the half-hour grid
            slot = slot.replace(second=0, microsecond=0) + dt.timedelta(minutes=30 - slot.minute % 30)
    return None


def create_event(title: str, start: str, end: str | None = None, minutes: int | None = None,
                 calendar: str | None = None, location: str | None = None, notes: str | None = None,
                 allow_overlap: bool = False, create_calendar: bool = False) -> str:
    """A Calendar event, through EventKit (Calendar's AppleScript took over 30 s on 29 Sep).

    Goes in the calendar the user named (else the default). If it overlaps an event on that calendar
    it is NOT booked unless `allow_overlap`: the answer names the clash and the next free slot of the
    same length that day, for the model to use or ask about.
    """
    import EventKit
    from Foundation import NSDate

    store, problem = _event_store(EventKit.EKEntityTypeEvent)
    if problem:
        return problem
    title = " ".join(str(title or "").split())
    try:
        begins = dt.datetime.fromisoformat(str(start).strip())
        ends = dt.datetime.fromisoformat(str(end).strip()) if end else begins + dt.timedelta(minutes=int(minutes or 60))
    except ValueError:
        return "Could not read that time. Give start (and end) as local ISO times, e.g. 2026-10-06T15:00."
    fixed, _moved = _say_day(begins)
    ends, begins = ends + (fixed - begins), fixed
    if not title or ends <= begins:
        return "FAILED: an event needs a title and an end after its start."
    target, problem = _event_calendar(store, calendar, bool(create_calendar))
    if problem:
        return problem

    def ns(when: dt.datetime):
        return NSDate.dateWithTimeIntervalSince1970_(when.timestamp())

    def py(nsdate) -> dt.datetime:
        return dt.datetime.fromtimestamp(nsdate.timeIntervalSince1970())

    day0 = dt.datetime.combine(begins.date(), dt.time.min)
    found = store.eventsMatchingPredicate_(store.predicateForEventsWithStartDate_endDate_calendars_(
        ns(day0), ns(day0 + dt.timedelta(days=1)), None)) or []
    same = [e for e in found if e.calendar() is not None and e.calendar().calendarIdentifier() == target.calendarIdentifier()
            and not e.isAllDay()]
    for e in same:
        if " ".join(str(e.title() or "").lower().split()) == title.lower() and py(e.startDate()) == begins:
            return f"Already on the '{target.title()}' calendar: {title} at {begins:%-I:%M %p} - not booked twice."
    busy = sorted((py(e.startDate()), py(e.endDate())) for e in same)
    clashes = [e for e in same if py(e.startDate()) < ends and begins < py(e.endDate())]
    if clashes and not allow_overlap:
        spans = "; ".join(f"'{e.title()}' {py(e.startDate()):%-I:%M}-{py(e.endDate()):%-I:%M %p}" for e in clashes)
        free = next_free(busy, begins, ends - begins, day0 + dt.timedelta(hours=22))
        offer = (f" The next free slot of the same length that day is {free:%-I:%M %p}-{free + (ends - begins):%-I:%M %p} "
                 f"(start {free:%Y-%m-%dT%H:%M})." if free else " Nothing that long is free later that day.")
        return (f"NOT BOOKED: {begins:%-I:%M %p} clashes on the '{target.title()}' calendar with {spans}.{offer} "
                "If the user said what to do on a clash, do that (call again with the new start); otherwise ask. "
                "allow_overlap=true books on top.")

    event = EventKit.EKEvent.eventWithEventStore_(store)
    event.setTitle_(title)
    event.setStartDate_(ns(begins))
    event.setEndDate_(ns(ends))
    event.setCalendar_(target)
    if location:
        event.setLocation_(str(location))
    if notes:
        event.setNotes_(str(notes))
    ok, error = store.saveEvent_span_commit_error_(event, EventKit.EKSpanThisEvent, True, None)
    if not ok:
        return f"FAILED: could not save the event: {error}"
    from mint.tools import undo
    undo.record("event", f"adding '{title}' to the calendar", None, "delete the event in Calendar.")
    others = [e for e in found if e not in same and not e.isAllDay()
              and py(e.startDate()) < ends and begins < py(e.endDate())]
    note = (" (Also at that time on other calendars: " + "; ".join(f"'{e.title()}'" for e in others[:3]) + ".)"
            if others else "")
    return (f"Added '{title}' to the '{target.title()}' calendar: {begins:%A %-d %B}, "
            f"{begins:%-I:%M %p}-{ends:%-I:%M %p}.{note}")


# --- notes and mail ------------------------------------------------------------

_NOTE_SEP = "␞"


def _note_folders() -> tuple[list[str] | None, str]:
    ok, out = _osascript('tell application "Notes"\nset out to ""\nrepeat with f in folders\n'
                         f'set out to out & (name of f) & "{_NOTE_SEP}"\nend repeat\nreturn out\nend tell', timeout=20)
    if not ok:
        return None, out
    return [n for n in out.split(_NOTE_SEP) if n.strip()], ""


def create_note(title: str, body: str = "", folder: str | None = None, create_folder: bool = False) -> str:
    """A note in the Notes folder the user named (else Notes' default folder). A folder that does not
    exist is made only with `create_folder`; otherwise the answer lists the real ones."""
    import html as _html

    title = " ".join(str(title or "").split())
    text = str(body or "")
    lines = text.split("\n")
    if lines and lines[0].strip().lower() == title.lower():      # the model often repeats the title
        lines = lines[1:]
    markup = "<h1>" + _html.escape(title) + "</h1>" + "".join(
        f"<div>{_html.escape(line) or '<br>'}</div>" for line in lines)

    folders, problem = _note_folders()
    if folders is None:
        return _automation_hint(problem, "Notes")
    wanted = folder or _named_in_request(folders, "folder")
    target = _pick(folders, wanted, "folder") if wanted else None
    make_folder = ""
    if wanted and target is None:
        label = " ".join(str(wanted).strip(" '\"").split())
        if not create_folder:
            return (f"NOT CREATED: Notes has no folder called '{label}'. Its folders: "
                    f"{', '.join(dict.fromkeys(folders))}. Ask the "
                    "user whether to make it (then call again with create_folder=true) or which folder to use.")
        target = label
        make_folder = (f"if not (exists folder {_as_string(label)}) then make new folder with properties "
                       f"{{name:{_as_string(label)}}}\n")
    where = f"at folder {_as_string(target)} " if target else ""
    # Body only: Notes names a note after its first line, and a `name` as well wrote the title twice.
    ok, out = _osascript(
        f"tell application \"Notes\"\n{make_folder}set n to make new note {where}with properties "
        f"{{body:{_as_string(markup)}}}\n"
        f"set f to \"\"\ntry\nset f to name of container of n\nend try\n"
        f"return (id of n) & \"{_NOTE_SEP}\" & f\nend tell", timeout=20)
    if not ok:
        return _automation_hint(out, "Notes")
    ident, _, place = out.partition(_NOTE_SEP)
    place = place or target or ""                    # a new note's container is not always readable
    from mint.tools import undo
    undo.record("note", f"creating the note '{title}'", {"kind": "note_delete", "id": ident, "title": title})
    return f"Created the note '{title}'" + (f" in the '{place}' folder" if place else "") + "."


def _mail_handler() -> str:
    """Bundle id of the app that opens mailto: links ('' if unknown)."""
    try:
        from Foundation import NSURL
        url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(NSURL.URLWithString_("mailto:x@y.z"))
        bundle = AppKit.NSBundle.bundleWithURL_(url) if url is not None else None
        return str(bundle.bundleIdentifier() or "") if bundle is not None else ""
    except Exception:
        return ""


def _wants_mail_app(app: str | None) -> bool:
    """Draft in Apple Mail (a real, saved draft) rather than through the default mailto: handler?"""
    if app:
        return bool(re.search(r"\b(apple\s*)?mail(\.app)?\b(?!to)", str(app).lower())) \
            and not re.search(r"gmail|outlook|chrome|default", str(app).lower())
    try:
        from mint.app import live
        request = live.request().lower()
    except Exception:
        request = ""
    if re.search(r"\b(mail app|apple mail|mail\.app|in mail\b|the mail application)", request):
        return True
    return _mail_handler() == "com.apple.mail"


def _addresses(to: str) -> list[str]:
    found = re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", str(to or ""))
    return list(dict.fromkeys(found))


def _mail_draft(to: str, subject: str, body: str) -> str:
    """A draft in Apple Mail: an open compose window, also saved to Drafts. Never sent."""
    recipients = "".join(f"make new to recipient at end of to recipients with properties {{address:{_as_string(a)}}}\n"
                         for a in _addresses(to))
    script = f"""
tell application "Mail"
    if (count of accounts) is 0 then return "NOACCOUNT"
    set m to make new outgoing message with properties {{subject:{_as_string(subject)}, content:{_as_string(body)}, visible:true}}
    tell m
        {recipients}
    end tell
    try
        save m
    end try
    set rcpt to ""
    repeat with r in (to recipients of m)
        set rcpt to rcpt & (address of r) & ","
    end repeat
    return (id of m as text) & "{_NOTE_SEP}" & (subject of m) & "{_NOTE_SEP}" & rcpt
end tell"""
    ok, out = _osascript(script, timeout=45)
    if not ok:
        return "FAILED: could not make the draft in Mail. " + _automation_hint(out, "Mail")
    if out == "NOACCOUNT":
        return ("FAILED: the Mail app has no email account set up, so it cannot hold a draft. Tell the user; "
                "or draft it in their webmail instead.")
    ident, got_subject, got_to = (out.split(_NOTE_SEP) + ["", ""])[:3]
    missing = [a for a in _addresses(to) if a.lower() not in got_to.lower()]
    if got_subject != subject or missing:
        return (f"FAILED: the Mail draft came out wrong (subject '{got_subject}', to '{got_to.strip(',')}'). "
                "It was NOT sent. Tell the user to check the open draft.")
    from mint.tools import undo
    undo.record("email", f"making an email draft{' to ' + to if to else ''} in Mail", None,
                "it was only a draft and was never sent; close its window and delete it from Drafts to discard it.")
    return (f"Made a draft in the Mail app to {got_to.strip(',') or 'nobody yet'}, subject '{got_subject}' - open "
            "for the user to review and saved in Drafts. NOT sent; the user sends it.")


def compose_email(to: str = "", subject: str = "", body: str = "", app: str | None = None) -> str:
    """A pre-filled draft for the user to review. Never sends.

    In Apple Mail - when the user asks for the Mail app, `app` says so, or Mail is the default mail
    app - it is a real draft made through Mail's scripting and checked. Otherwise a mailto: link opens
    it in the default mail app. (In the 29 Sep run "in the Mail app, draft …" went through mailto:,
    and mailto: opened Chrome, the default handler here: nothing reached Mail.)
    """
    if _wants_mail_app(app):
        return _mail_draft(to, subject, body)
    query = urllib.parse.urlencode({"subject": subject, "body": body},
                                   quote_via=urllib.parse.quote)
    url = f"mailto:{urllib.parse.quote(to, safe='@,')}?{query}"
    subprocess.run(["open", url], check=False)
    handler = _mail_handler()
    app_name = {"com.google.Chrome": "Google Chrome", "com.apple.Safari": "Safari",
                "com.microsoft.Outlook": "Outlook", "com.apple.mail": "Mail"}.get(handler, handler or "the default mail app")
    from mint.tools import undo
    undo.record("email", f"opening an email draft{' to ' + to if to else ''}", None,
                "it was only a draft and was never sent; close the draft window to discard it.")
    return (f"Opened a draft in {app_name} (the default mail app, through a mailto: link). It has not been sent; "
            "the user reviews and sends it. If they wanted it in the Mail app, call again with app='Mail'.")


def list_emails(count: int = 5) -> str:
    """Most recent messages in Mail.app's inbox."""
    count = max(1, min(int(count), 15))
    script = f"""
    tell application "Mail"
        set out to ""
        set n to count of messages of inbox
        if n > {count} then set n to {count}
        if n is 0 then return ""
        set msgs to messages 1 thru n of inbox
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
        set m to message {int(number)} of inbox
        return (sender of m) & linefeed & (subject of m) & linefeed & linefeed & (content of m)
    end tell"""
    ok, out = _osascript(script, timeout=20)
    if not ok:
        return "Could not read that message. " + _automation_hint(out, "Mail")
    return out if len(out) <= 6000 else out[:6000] + " … (truncated)"
