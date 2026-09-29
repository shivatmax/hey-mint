"""AppleScript for the sandboxed fixtures: the "Mint Bench" Reminders list, Calendar calendar and
Notes folder, and "[MintBench]" Mail drafts.

Reminders and Calendar use EventKit in this process when it already has full access (rb/ek.py):
Reminders' AppleScript took ~110 s for one `exists list` here. Otherwise, two ways to run a script:
  direct  osascript from this process. macOS asks once per app ("<your terminal> wants to control
          Reminders"); until that is answered, osascript hangs. `permission()` asks macOS WITHOUT
          prompting whether it is already allowed.
  mint    through Mint.app's own permissions, with Mint's documented test aid
          `open -g -n -W ~/Applications/Mint.app --args --script steps.json` (its run_applescript
          tool; the script's result is read back from mint.log). The request step lets the
          fixture scripts delete and close, which run_applescript otherwise refuses.
Backend "auto": EventKit, else direct where macOS already allows it, else mint. Reminders and Calendar use
EventKit under every backend but "direct" when this process already has full access: it needs no prompt and
no AppleScript (29 Sep, `--fixtures mint`: Reminders' AppleScript failed with -1728 and Calendar's timed out
behind run_applescript's 30 s cap, so 5 tasks lost their fixtures or cleanup).
With `allow_prompts=False`, anything that could put a permission dialog on screen is skipped.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import ek, mintlink

APPS = {"reminders": ("Reminders", "com.apple.reminders"), "calendar": ("Calendar", "com.apple.iCal"),
        "notes": ("Notes", "com.apple.Notes"), "mail": ("Mail", "com.apple.mail"),
        "finder": ("Finder", "com.apple.finder")}
LIST = CALENDAR = FOLDER = "Mint Bench"
SEP = "|~|"
MINT_APP = Path.home() / "Applications/Mint.app"


class Unavailable(Exception):
    """The app cannot be scripted without a permission prompt (or at all)."""


# --- permission, without prompting ----------------------------------------------------------

def permission(app_key: str) -> int:
    """0 allowed, -1743 denied, -1744 would prompt, -600 app not running (unknown), else an error."""
    try:
        cs = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    except OSError:
        return -1

    class AEDesc(ctypes.Structure):
        _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]

    def fourcc(s: str) -> int:
        return int.from_bytes(s.encode(), "big")

    cs.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(AEDesc)]
    cs.AEDeterminePermissionToAutomateTarget.argtypes = [ctypes.POINTER(AEDesc), ctypes.c_uint32,
                                                         ctypes.c_uint32, ctypes.c_bool]
    cs.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
    cs.AEDisposeDesc.argtypes = [ctypes.POINTER(AEDesc)]
    desc = AEDesc()
    bundle = APPS[app_key][1].encode()
    if cs.AECreateDesc(fourcc("bund"), bundle, len(bundle), ctypes.byref(desc)):
        return -1
    try:
        return int(cs.AEDeterminePermissionToAutomateTarget(ctypes.byref(desc), fourcc("****"),
                                                            fourcc("****"), False))
    finally:
        cs.AEDisposeDesc(ctypes.byref(desc))


def app_running(name: str) -> bool:
    import AppKit
    return any(a.localizedName() == name for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications())


# --- running scripts --------------------------------------------------------------------------

class Apple:
    def __init__(self, backend: str = "auto", allow_prompts: bool = True) -> None:
        self.backend = backend
        self.allow_prompts = allow_prompts
        self.chosen: dict[str, str] = {}
        self.calls = 0
        self.warmed: set[str] = set()

    def route(self, app_key: str) -> str:
        """Which backend to use for this app, or raise Unavailable."""
        if app_key in self.chosen:
            return self.chosen[app_key]
        if self.backend == "off":
            raise Unavailable("fixtures for Apple apps are off (--fixtures off)")
        if app_key in ("reminders", "calendar") and self.backend != "direct":
            # Only an explicit --fixtures eventkit asks macOS (a prompt that blocks until answered).
            if ek.authorized(app_key) or (self.backend == "eventkit" and self.allow_prompts and ek.request(app_key)):
                self.chosen[app_key] = "eventkit"
                return "eventkit"
        if self.backend == "eventkit":
            raise Unavailable(f"this process has no full EventKit access to {APPS[app_key][0]}")
        status = permission(app_key)
        if self.backend == "direct":
            if status == -1743:
                raise Unavailable(f"macOS denied this terminal control of {APPS[app_key][0]} "
                                  "(System Settings > Privacy & Security > Automation)")
            if status not in (0, -600) and not self.allow_prompts:
                raise Unavailable(f"controlling {APPS[app_key][0]} would show a permission prompt "
                                  f"(status {status}); run once with --allow-prompts and answer it")
            route = "direct"
        elif self.backend == "mint":
            if not self.allow_prompts:
                raise Unavailable(f"the mint backend cannot tell without trying whether Mint may control "
                                  f"{APPS[app_key][0]}; not trying because of --no-prompt")
            route = "mint"
        else:   # auto
            if status == 0:
                route = "direct"
            elif not self.allow_prompts:
                raise Unavailable(f"{APPS[app_key][0]}: this terminal is not allowed to control it (status "
                                  f"{status}) and --no-prompt forbids trying through Mint.app")
            else:
                route = "mint"
        if route == "mint" and not MINT_APP.exists():
            raise Unavailable(f"{MINT_APP} not found for the mint backend")
        self.chosen[app_key] = route
        return route

    def run(self, app_key: str, script: str, timeout: float = 60.0, verbs: str = "", retry: bool = True) -> str:
        """Run `script` (it should `return` text). Raises Unavailable or RuntimeError.
        `retry=False` for slow sweeps: a script Mint gave up on keeps the app busy, so trying again
        only piles up more work."""
        self.calls += 1
        if self.route(app_key) == "direct":
            return _direct(script, timeout)
        if app_key not in self.warmed:
            self.warmed.add(app_key)
            _launch(APPS[app_key][0])
        # Mint's run_applescript stops a script after 30 s. An app that is still starting (Calendar, 29 Sep:
        # four timeouts in a row) answers once it is up, so try again rather than fail the task.
        for attempt in range(3):
            try:
                return _via_mint(script, timeout, verbs)
            except RuntimeError as error:
                if "timed out" not in str(error) or attempt == 2 or not retry:
                    raise
                _launch(APPS[app_key][0], settle=10)
        raise AssertionError("unreachable")


def _direct(script: str, timeout: float) -> str:
    try:
        done = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"osascript timed out after {timeout:.0f}s (a permission prompt waiting?)") from error
    if done.returncode != 0:
        err = done.stderr.strip()
        if "-1743" in err:
            raise Unavailable("macOS denied Automation: " + err[:160])
        raise RuntimeError(err[:400])
    return done.stdout.rstrip("\n")


def _launch(app: str, settle: float = 3.0) -> None:
    """Start an app in the background (needs no Automation permission) and give it a moment."""
    was_running = app_running(app)
    subprocess.run(["open", "-g", "-a", app], check=False, capture_output=True, timeout=30)
    deadline = time.monotonic() + 30
    while not app_running(app) and time.monotonic() < deadline:
        time.sleep(0.5)
    if not was_running or settle > 3:
        time.sleep(settle)


def _via_mint(script: str, timeout: float, verbs: str) -> str:
    nonce = uuid.uuid4().hex[:10]
    # Handlers cannot be nested: keep the script's own handlers at the top level.
    handler = re.compile(r"^on (\w+)\(.*?^end \1[ \t]*$", re.M | re.S)
    handlers = "\n".join(m.group(0) for m in handler.finditer(script))
    body = handler.sub("", script)
    wrapped = (f"on mbBody()\n{body}\nend mbBody\n{handlers}\n"
               f'return "<<MB{nonce}>>" & (mbBody() as text) & "<<END>>"')
    # run_applescript refuses delete/close/buy/pay/… unless the "user" asked for it: this is that request.
    # It quotes the script, because fixture DATA holds such words too: the todo note "Buy printer ink" and
    # the cleanup marker "pay the invoice" were refused on 29 Sep. This is the test aid's own request, not
    # the model's: Mint's check on what the model runs is unchanged.
    request = f"please {verbs or 'make and read'} the Mint Bench benchmark fixtures, exactly this script: {body}"
    steps = [["request", request], ["run_applescript", {"script": wrapped}]]
    with tempfile.NamedTemporaryFile("w", suffix=".json", prefix="mintbench-", delete=False) as f:
        json.dump(steps, f)
        path = f.name
    offset = mintlink.log_size()
    try:
        subprocess.run(["open", "-g", "-n", "-W", str(MINT_APP), "--args", "--script", path],
                       timeout=timeout + 30, check=False, capture_output=True)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Mint.app --script did not finish (a permission prompt waiting?)") from error
    finally:
        Path(path).unlink(missing_ok=True)
    text = mintlink.read_from(offset)
    found = re.search(rf"<<MB{nonce}>>(.*?)<<END>>", text, re.S)
    if found:
        return found.group(1)
    failed = re.search(r"==== script run_applescript \([\d.]+s\): ((?:FAILED|REFUSED)[^\n]*)", text)
    if failed:
        if "not allowed Mint to control" in failed.group(1):
            raise Unavailable(failed.group(1)[:200])
        raise RuntimeError(failed.group(1)[:400])
    raise RuntimeError("no result from Mint.app --script (see mint.log)")


# --- AppleScript helpers ------------------------------------------------------------------------

def q(text: str) -> str:
    """An AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def as_date(var: str, when: dt.datetime) -> str:
    """Lines that set AppleScript variable `var` to `when` (locale-proof)."""
    return (f"set {var} to current date\nset day of {var} to 1\nset year of {var} to {when.year}\n"
            f"set month of {var} to {when.month}\nset day of {var} to {when.day}\n"
            f"set time of {var} to {when.hour * 3600 + when.minute * 60}")


_ISO = """
on isoDate(d)
    if d is missing value then return ""
    set t to time of d
    return (year of d as text) & "-" & pad((month of d) as integer) & "-" & pad(day of d) & "T" & pad(t div 3600) & ":" & pad((t mod 3600) div 60)
end isoDate
on pad(n)
    return text -2 thru -1 of ("0" & (n as text))
end pad
"""


def parse_iso(text: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(text.strip()) if text.strip() else None
    except ValueError:
        return None


def _rows(out: str, fields: tuple[str, ...]) -> list[dict]:
    rows = []
    for line in out.split("\n"):
        if SEP not in line:
            continue
        parts = line.split(SEP)
        rows.append(dict(zip(fields, parts + [""] * (len(fields) - len(parts)))))
    return rows


# --- Reminders ----------------------------------------------------------------------------------

def reminders_ensure(apple: Apple) -> None:
    if apple.route("reminders") == "eventkit":
        return ek.ensure("reminders")
    apple.run("reminders", f'tell application "Reminders"\nif not (exists list {q(LIST)}) then '
                           f'make new list with properties {{name:{q(LIST)}}}\nend tell\nreturn "ok"')


def reminders_add(apple: Apple, name: str, due: dt.datetime | None = None, body: str = "") -> None:
    if apple.route("reminders") == "eventkit":
        return ek.reminder_add(name, due, body)
    props = f"name:{q(name)}" + (f", body:{q(body)}" if body else "") + (", due date:d" if due else "")
    apple.run("reminders", (as_date("d", due) + "\n" if due else "") +
              f'tell application "Reminders" to make new reminder at end of list {q(LIST)} '
              f'with properties {{{props}}}\nreturn "ok"')


def reminders_list(apple: Apple) -> list[dict]:
    """Reminders in the Mint Bench list: name, due (ISO), body, done. [] if there is no list."""
    if apple.route("reminders") == "eventkit":
        return ek.reminders()
    out = apple.run("reminders", f"""
tell application "Reminders"
    if not (exists list {q(LIST)}) then return ""
    set out to ""
    repeat with r in (every reminder of list {q(LIST)})
        set b to body of r
        if b is missing value then set b to ""
        set out to out & (name of r) & "{SEP}" & my isoDate(due date of r) & "{SEP}" & b & "{SEP}" & (completed of r as text) & linefeed
    end repeat
    return out
end tell
{_ISO}""")
    return _rows(out, ("name", "due", "body", "done"))


def _any_of(var: str, markers: tuple[str, ...]) -> str:
    """AppleScript condition: `var` contains one of the markers."""
    return " or ".join(f"{var} contains {q(m)}" for m in markers) or "false"


def reminders_strays(apple: Apple, markers: tuple[str, ...], since: dt.datetime) -> list[str]:
    """Reminders made since `since`, outside the Mint Bench list, whose name has a marker."""
    if apple.route("reminders") == "eventkit":
        return ek.reminder_strays(markers, since)
    # `repeat with r in (every reminder of l whose …)` failed with -1728 ("Can't get item 1 of every
    # reminder of item 1 of every list whose …") on 29 Sep: fetch the matches into a variable first.
    out = apple.run("reminders", as_date("mbSince", since) + f"""
tell application "Reminders"
    set out to ""
    repeat with l in (get lists)
        set ln to name of l
        if ln is not {q(LIST)} then
            try
                set found to (name of every reminder of l whose creation date > mbSince)
            on error
                set found to {{}}
            end try
            repeat with n in found
                set n to n as text
                if {_any_of("n", markers)} then set out to out & ln & ": " & n & linefeed
            end repeat
        end if
    end repeat
    return out
end tell""", timeout=120)
    return [line for line in out.split("\n") if line.strip()]


def reminders_cleanup(apple: Apple, markers: tuple[str, ...], since: dt.datetime) -> None:
    """Delete the Mint Bench list, and marked reminders made since `since` in any other list."""
    if apple.route("reminders") == "eventkit":
        ek.remove("reminders")
        ek.reminder_strays(markers, since, delete=True)
        return
    apple.run("reminders", as_date("mbSince", since) + f"""
tell application "Reminders"
    if exists list {q(LIST)} then delete list {q(LIST)}
    repeat with l in (get lists)
        try
            set found to (every reminder of l whose creation date > mbSince)
        on error
            set found to {{}}
        end try
        repeat with r in found
            set n to name of r
            if {_any_of("n", markers)} then delete r
        end repeat
    end repeat
end tell
return "ok\"""", timeout=120, verbs="delete")


# --- Calendar -------------------------------------------------------------------------------------

def calendar_ensure(apple: Apple) -> None:
    if apple.route("calendar") == "eventkit":
        return ek.ensure("calendar")
    apple.run("calendar", f'tell application "Calendar"\nif not (exists calendar {q(CALENDAR)}) then '
                          f'make new calendar with properties {{name:{q(CALENDAR)}}}\nend tell\nreturn "ok"')


def calendar_add(apple: Apple, summary: str, start: dt.datetime, end: dt.datetime) -> None:
    if apple.route("calendar") == "eventkit":
        return ek.event_add(summary, start, end)
    apple.run("calendar", as_date("d1", start) + "\n" + as_date("d2", end) + "\n" +
              f'tell application "Calendar" to tell calendar {q(CALENDAR)} to make new event with properties '
              f'{{summary:{q(summary)}, start date:d1, end date:d2}}\nreturn "ok"')


def calendar_list(apple: Apple) -> list[dict]:
    if apple.route("calendar") == "eventkit":
        return ek.events()
    out = apple.run("calendar", f"""
tell application "Calendar"
    if not (exists calendar {q(CALENDAR)}) then return ""
    set out to ""
    repeat with e in (every event of calendar {q(CALENDAR)})
        set n to description of e
        if n is missing value then set n to ""
        set out to out & (summary of e) & "{SEP}" & my isoDate(start date of e) & "{SEP}" & my isoDate(end date of e) & "{SEP}" & n & linefeed
    end repeat
    return out
end tell
{_ISO}""")
    return _rows(out, ("summary", "start", "end", "notes"))


def calendar_strays(apple: Apple, markers: tuple[str, ...], since: dt.datetime) -> list[str]:
    """Marked events outside the Mint Bench calendar that start after two days before `since`."""
    if apple.route("calendar") == "eventkit":
        return ek.event_strays(markers, since)
    # Writable calendars only: nothing can be added to Birthdays, Siri Suggestions or subscribed holidays, and
    # sweeping those took over 30 s (Mint's cap) and kept Calendar busy, so the next tasks' fixtures timed
    # out too (29 Sep). The writable ones took ~1 s each.
    out = apple.run("calendar", as_date("mbSince", since) + f"""
set mbSince to mbSince - 2 * days
tell application "Calendar"
    set out to ""
    repeat with c in (get calendars)
        if name of c is not {q(CALENDAR)} and writable of c then
            try
                set found to (summary of every event of c whose start date > mbSince)
            on error
                set found to {{}}
            end try
            repeat with n in found
                set n to n as text
                if {_any_of("n", markers)} then set out to out & (name of c) & ": " & n & linefeed
            end repeat
        end if
    end repeat
    return out
end tell""", timeout=180, retry=False)
    return [line for line in out.split("\n") if line.strip()]


def calendar_cleanup(apple: Apple, markers: tuple[str, ...], since: dt.datetime) -> None:
    if apple.route("calendar") == "eventkit":
        ek.remove("calendar")
        ek.event_strays(markers, since, delete=True)
        return
    # The container first, on its own: the stray sweep reads every calendar and may hit the 30 s cap.
    apple.run("calendar", f'tell application "Calendar"\nif exists calendar {q(CALENDAR)} then delete calendar '
                          f'{q(CALENDAR)}\nend tell\nreturn "ok"', verbs="delete")
    apple.run("calendar", as_date("mbSince", since) + f"""
set mbSince to mbSince - 2 * days
tell application "Calendar"
    repeat with c in (get calendars)
        if writable of c then
            try
                set found to (every event of c whose start date > mbSince)
            on error
                set found to {{}}
            end try
            repeat with e in found
                set n to summary of e
                if {_any_of("n", markers)} then delete e
            end repeat
        end if
    end repeat
end tell
return "ok\"""", timeout=180, verbs="delete", retry=False)


# --- Notes ------------------------------------------------------------------------------------------

def notes_ensure(apple: Apple) -> None:
    apple.run("notes", f'tell application "Notes"\nif not (exists folder {q(FOLDER)}) then '
                       f'make new folder with properties {{name:{q(FOLDER)}}}\nend tell\nreturn "ok"')


def notes_add(apple: Apple, title: str, lines: list[str]) -> None:
    html = f"<h1>{title}</h1>" + "".join(f"<div>{line}</div>" for line in lines)
    apple.run("notes", f'tell application "Notes" to make new note at folder {q(FOLDER)} with properties '
                       f'{{name:{q(title)}, body:{q(html)}}}\nreturn "ok"')


def notes_list(apple: Apple) -> list[dict]:
    out = apple.run("notes", f"""
tell application "Notes"
    if not (exists folder {q(FOLDER)}) then return ""
    set out to ""
    repeat with n in (every note of folder {q(FOLDER)})
        set t to plaintext of n
        set AppleScript's text item delimiters to {{return, linefeed}}
        set parts to text items of t
        set AppleScript's text item delimiters to " / "
        set t to parts as text
        set AppleScript's text item delimiters to ""
        set out to out & (name of n) & "{SEP}" & t & linefeed
    end repeat
    return out
end tell""")
    return _rows(out, ("name", "text"))


def notes_strays(apple: Apple, markers: tuple[str, ...], since: dt.datetime) -> list[str]:
    """Marked notes made since `since` in folders other than Mint Bench (and Recently Deleted).
    Folder by folder: a new note's `container` is unreadable (29 Sep), which reported notes IN the Mint
    Bench folder as strays ("?: …") and made notes_cleanup skip every note."""
    out = apple.run("notes", as_date("mbSince", since) + f"""
tell application "Notes"
    set out to ""
    repeat with f in (get folders)
        set fn to name of f
        if fn is not {q(FOLDER)} and fn is not "Recently Deleted" then
            try
                set found to (name of every note of f whose creation date > mbSince)
            on error
                set found to {{}}
            end try
            repeat with t in found
                set t to t as text
                if {_any_of("t", markers)} then set out to out & fn & ": " & t & linefeed
            end repeat
        end if
    end repeat
    return out
end tell""", timeout=120)
    return [line for line in out.split("\n") if line.strip()]


def notes_cleanup(apple: Apple, markers: tuple[str, ...], since: dt.datetime) -> None:
    """Marked notes made since `since`, folder by folder (never from Recently Deleted: deleting there is
    permanent), then the Mint Bench folder. Deleted notes go to Recently Deleted."""
    apple.run("notes", as_date("mbSince", since) + f"""
tell application "Notes"
    repeat with f in (get folders)
        if name of f is not "Recently Deleted" then
            try
                set found to (every note of f whose creation date > mbSince)
            on error
                set found to {{}}
            end try
            repeat with n in found
                set t to name of n
                if {_any_of("t", markers)} then delete n
            end repeat
        end if
    end repeat
    if exists folder {q(FOLDER)} then delete folder {q(FOLDER)}
end tell
return "ok\"""", timeout=120, verbs="delete")


# --- Mail (drafts only; nothing is ever sent) ------------------------------------------------------

def mail_drafts(apple: Apple, marker: str = "[MintBench]") -> list[dict]:
    """Open compose windows and saved drafts whose subject has the marker: where, subject, to, content."""
    out = apple.run("mail", f"""
tell application "Mail"
    set out to ""
    repeat with m in (get every outgoing message)
        if subject of m contains {q(marker)} then
            set rcpt to ""
            repeat with r in (to recipients of m)
                set rcpt to rcpt & (address of r) & ","
            end repeat
            set c to content of m
            if c is missing value then set c to ""
            set out to out & "compose " & (id of m) & "{SEP}" & (subject of m) & "{SEP}" & rcpt & "{SEP}" & my flat(c as text) & linefeed
        end if
    end repeat
    try
        repeat with m in (every message of drafts mailbox whose subject contains {q(marker)})
            set rcpt to ""
            repeat with r in (to recipients of m)
                set rcpt to rcpt & (address of r) & ","
            end repeat
            set out to out & "draft{SEP}" & (subject of m) & "{SEP}" & rcpt & "{SEP}" & my flat(content of m) & linefeed
        end repeat
    end try
    return out
end tell
on flat(t)
    set AppleScript's text item delimiters to {{return, linefeed}}
    set parts to text items of t
    set AppleScript's text item delimiters to " / "
    set t to parts as text
    set AppleScript's text item delimiters to ""
    return t
end flat""", timeout=90)
    return _rows(out, ("where", "subject", "to", "content"))


def mail_ghosts(apple: Apple, marker: str = "[MintBench]") -> set[str]:
    """Outgoing [MintBench] messages that exist before a task (closed ones linger until Mail quits)."""
    return {d["where"] for d in mail_drafts(apple, marker) if d["where"].startswith("compose")}


def mail_sent(apple: Apple, marker: str = "[MintBench]") -> int:
    """How many SENT messages carry the marker (must always be 0). -1 if unknown."""
    try:
        out = apple.run("mail", f'tell application "Mail" to return (count of (every message of sent mailbox '
                                f'whose subject contains {q(marker)})) as text', timeout=90)
        return int(out.strip() or 0)
    except (RuntimeError, ValueError):
        return -1


def mail_make_draft(apple: Apple, subject: str, to: str, body: str) -> None:
    """An invisible, unsaved outgoing message (for the self-test). Never sent."""
    apple.run("mail", f"""
tell application "Mail"
    set m to make new outgoing message with properties {{subject:{q(subject)}, content:{q(body)}, visible:false}}
    tell m to make new to recipient at end of to recipients with properties {{address:{q(to)}}}
end tell
return "ok\"""")


def mail_cleanup(apple: Apple, marker: str = "[MintBench]") -> None:
    apple.run("mail", f"""
tell application "Mail"
    -- Closing or deleting an invisible outgoing message leaves a scripting ghost until Mail quits
    -- (29 Sep); mail_drafts' callers ignore ghosts by id (see mail_ghosts).
    repeat with m in (get every outgoing message)
        if subject of m contains {q(marker)} then
            try
                close m saving no
            end try
            try
                delete m
            end try
        end if
    end repeat
    try
        delete (every message of drafts mailbox whose subject contains {q(marker)})
    end try
end tell
return "ok\"""", timeout=90, verbs="close and delete")


def container_exists(apple: Apple, app_key: str) -> bool:
    """Is the Mint Bench list / calendar / folder there?"""
    if app_key in ("reminders", "calendar") and apple.route(app_key) == "eventkit":
        return ek.exists(app_key)
    kind = {"reminders": "list", "calendar": "calendar", "notes": "folder"}[app_key]
    out = apple.run(app_key, f'tell application "{APPS[app_key][0]}" to return (exists {kind} {q(LIST)}) as text')
    return out.strip() == "true"


# --- Finder: close windows left showing the sandbox ------------------------------------------------

def finder_close_sandbox(apple: Apple, folder: str = "MintBench") -> None:
    apple.run("finder", f"""
tell application "Finder"
    repeat with w in (every Finder window)
        try
            if (POSIX path of (target of w as alias)) contains {q("/" + folder)} then close w
        end try
    end repeat
end tell
return "ok\"""", timeout=30, verbs="close")


def wait_for(predicate, seconds: float, every: float = 2.0):
    """Poll `predicate()` until it is truthy or time runs out; returns the last value."""
    deadline = time.monotonic() + seconds
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(every)
