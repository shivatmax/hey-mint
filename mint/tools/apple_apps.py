"""Apple's own Mac apps, deeper than "make one": change what is already there.

    notes             search, read, append, prepend, replace, rename, move, folders, recent, delete
    reminders_manage  list (by list, due today, overdue), complete, uncomplete, reschedule, move, rename, delete
    contacts          look someone up (phones, emails, birthday, company, address) - read only
    calendar_manage   find, move, rename, change location / notes, delete
    maps              directions in Apple Maps, with the travel time when MapKit can work it out
    safari            add to the Reading List, the current page, bookmarks in a folder (read only)
    photos            count / show photos from a date range or album, export some to a folder
    iwork             a Pages document, Keynote presentation or Numbers sheet from what Mint wrote

Notes, Contacts, Safari, Photos and iWork go through AppleScript (Mint's Automation permission);
Reminders and Calendar through EventKit (skills._event_store), like create_reminder / create_event.
Names are matched the way skills._pick matches them (case, spaces and "my ... list" ignored), then
by containing words. Every change records its way back in undo.py: a note keeps its previous body,
a reminder or event its previous fields (a deleted one is made again from them). Deleting happens
only when the user's own request asks for it, and a note goes to Recently Deleted, never further.
Nothing here sends anything anywhere.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import logging
import os
import plistlib
import re
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from mint.tools import everyday as skills
from mint.tools import undo

log = logging.getLogger("mint.tools.apple_apps")

F = "␞"          # field separator in AppleScript output
R = "␟"          # row separator

# Where Safari keeps bookmarks and the Reading List (readable only with Full Disk Access).
BOOKMARKS = Path.home() / "Library" / "Safari" / "Bookmarks.plist"

_ISO = """
on isoDate(d)
    if d is missing value then return ""
    set t to time of d
    return (year of d as text) & "-" & my pad((month of d) as integer) & "-" & my pad(day of d) & "T" & my pad(t div 3600) & ":" & my pad((t mod 3600) div 60)
end isoDate

on pad(n)
    return text -2 thru -1 of ("0" & (n as text))
end pad
"""


# --- shared helpers --------------------------------------------------------------------------------

def _osa(script: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Run AppleScript (the tests swap this for a route through Mint.app's permissions)."""
    return skills._osascript(script, timeout)


def _q(text) -> str:
    return skills._as_string(str(text))


def _failed(out: str, app: str) -> str:
    return "FAILED: " + skills._automation_hint(re.sub(r"^(FAILED:\s*)+", "", str(out)), app)


def _request() -> str:
    try:
        from mint.app import live
        return " ".join((live.request() or "").lower().replace("’", "'").split())
    except Exception:
        return ""


_DELETE_ASK = re.compile(r"\b(delete|remove|trash|erase|get rid of|throw (it |that |them )?away|bin it|cancel)\b")


def _asked_to_delete() -> bool:
    """Deleting only when the user's own words ask for it (never on the model's initiative)."""
    return bool(_DELETE_ASK.search(_request()))


def _not_asked(what: str) -> str:
    return (f"NOT DELETED: the user didn't ask to delete {what}. Only delete when they say so; ask them first if "
            "that is what they want.")


_FILLER = {"my", "the", "a", "an", "note", "notes", "list", "reminder", "reminders", "event", "called", "named",
           "in", "on", "for", "to", "of", "and"}


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", str(text).lower()) if w not in _FILLER}


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _candidates(names: list[str], wanted: str, kind: str) -> tuple[list[int], bool]:
    """(indices of the names `wanted` means, exact?). First the way skills._pick matches (case, spaces and
    'my ... list' ignored); then names containing it; then names holding all its words."""
    wanted = str(wanted or "").strip()
    if not names or not wanted:
        return [], False
    real = skills._pick(names, wanted, kind)
    if real is not None:
        key = _squash(skills._plain_name(real, kind))
        return [i for i, n in enumerate(names) if _squash(skills._plain_name(n, kind)) == key], True
    key = _squash(skills._plain_name(wanted, kind))
    inside = [i for i, n in enumerate(names) if key and key in _squash(n)]
    if inside:
        return inside, False
    words = _words(wanted)
    if not words:
        return [], False
    return [i for i, n in enumerate(names) if words <= _words(n)], False


def _near(names: list[str], wanted: str, limit: int = 6) -> str:
    """A few names close to `wanted`, for 'not found' answers."""
    import difflib
    close = difflib.get_close_matches(str(wanted).lower(), [n.lower() for n in names], n=limit, cutoff=0.3)
    picked = [next(n for n in names if n.lower() == c) for c in close]
    words = _words(wanted)
    picked += [n for n in names if n not in picked and words & _words(n)][:limit - len(picked)]
    return ", ".join(f"'{n}'" for n in picked[:limit])


def _ago(when: dt.datetime | None) -> str:
    if when is None:
        return ""
    secs = (dt.datetime.now() - when).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    days = int(secs // 86400)
    return "yesterday" if days == 1 else f"{days} days ago" if days < 14 else when.strftime("%-d %b")


def _iso(text: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(text).strip()) if str(text or "").strip() else None
    except ValueError:
        return None


def _speak_time(when: dt.datetime, with_day: bool = True) -> str:
    today = dt.date.today()
    clock = when.strftime("%-I:%M %p").replace(":00 ", " ")
    if not with_day:
        return clock
    if when.date() == today:
        day = "today"
    elif when.date() == today + dt.timedelta(days=1):
        day = "tomorrow"
    elif when.date() == today - dt.timedelta(days=1):
        day = "yesterday"
    elif abs((when.date() - today).days) < 7:
        day = when.strftime("%A")
    else:
        day = when.strftime("%a %-d %b")
    return f"{day} {clock}"


def _speak_day(day: dt.date) -> str:
    today = dt.date.today()
    if day == today:
        return "today"
    if day == today + dt.timedelta(days=1):
        return "tomorrow"
    if day == today - dt.timedelta(days=1):
        return "yesterday"
    return day.strftime("%A") if abs((day - today).days) < 7 else day.strftime("%a %-d %b")


def _nsdate(when: dt.datetime):
    from Foundation import NSDate
    return NSDate.dateWithTimeIntervalSince1970_(when.timestamp())


def _py(nsdate) -> dt.datetime | None:
    return dt.datetime.fromtimestamp(nsdate.timeIntervalSince1970()) if nsdate is not None else None


def _new_day(when: dt.datetime) -> dt.datetime:
    """skills._say_day ("tomorrow" in the request decides the day), unless the request names two days
    ("move tomorrow's call to today"): then it can't tell which one is the new one."""
    days = set(re.findall(r"\b(today|tonight|tomorrow|day after tomorrow)\b", _request()))
    if len(days - {"tonight"}) > 1 or ("tonight" in days and "tomorrow" in days):
        return when
    return skills._say_day(when)[0]


def _as_date(var: str, when: dt.datetime) -> str:
    """AppleScript lines setting `var` to `when`, whatever the locale."""
    return (f"set {var} to current date\nset day of {var} to 1\nset year of {var} to {when.year}\n"
            f"set month of {var} to {when.month}\nset day of {var} to {when.day}\n"
            f"set time of {var} to {when.hour * 3600 + when.minute * 60 + when.second}\n")


def _running(bundle: str) -> bool:
    import AppKit
    return bool(AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle))


def _installed(bundle: str) -> Path | None:
    import AppKit
    url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle)
    return Path(str(url.path())) if url is not None else None


# ==================================================================================================
# Notes
# ==================================================================================================

_TRASH_FOLDERS = {"recently deleted", "zuletzt gelöscht", "supprimés récemment", "eliminados recientemente",
                  "eliminati di recente", "recentemente apagadas", "onlangs verwijderd", "最近删除", "最近削除した項目"}


def _is_trash(folder: str) -> bool:
    return str(folder).strip().lower() in _TRASH_FOLDERS


def _catalog(dates: bool = False) -> tuple[list[dict] | None, str]:
    """Every note outside Recently Deleted: {"id", "name", "folder", "folder_id", ("modified")}."""
    trash = "{" + ", ".join(_q(name) for name in sorted(_TRASH_FOLDERS)) + "}"
    when = f' & "{F}" & my isoDate(item i of ds)' if dates else ""
    script = f"""
tell application "Notes"
    set out to ""
    repeat with f in folders
        set fn to name of f
        if fn is not in {trash} then
            set fid to id of f
            set ids to id of notes of f
            set nms to name of notes of f
            {"set ds to modification date of notes of f" if dates else ""}
            repeat with i from 1 to count of ids
                set out to out & (item i of ids) & "{F}" & (item i of nms) & "{F}" & fn & "{F}" & fid{when} & "{R}"
            end repeat
        end if
    end repeat
    return out
end tell
{_ISO if dates else ""}"""
    ok, out = _osa(script, timeout=60)
    if not ok:
        return None, _failed(out, "Notes")
    rows = []
    for line in out.split(R):
        parts = line.split(F)
        if len(parts) < 4:
            continue
        row = {"id": parts[0], "name": parts[1], "folder": parts[2], "folder_id": parts[3],
               "trash": _is_trash(parts[2])}
        if dates:
            row["modified"] = _iso(parts[4]) if len(parts) > 4 else None
        rows.append(row)
    return rows, ""


def _note_folders() -> tuple[list[dict] | None, str]:
    ok, out = _osa(f"""
tell application "Notes"
    set out to ""
    repeat with f in folders
        set cn to ""
        try
            set cn to name of container of f
        end try
        set out to out & (id of f) & "{F}" & (name of f) & "{F}" & (count of notes of f) & "{F}" & cn & "{R}"
    end repeat
    return out
end tell""")
    if not ok:
        return None, _failed(out, "Notes")
    rows = []
    for line in out.split(R):
        parts = line.split(F)
        if len(parts) >= 3:
            rows.append({"id": parts[0], "name": parts[1], "count": int(parts[2]) if parts[2].isdigit() else 0,
                         "trash": _is_trash(parts[1]), "account": parts[3] if len(parts) > 3 else ""})
    return rows, ""


def _pick_folder(folders: list[dict], wanted: str) -> dict | None:
    live = [f for f in folders if not f["trash"]]
    real = skills._pick([f["name"] for f in live], wanted, "folder")
    return next((f for f in live if f["name"] == real), None) if real else None


def _find_note(note: str, folder: str = "") -> tuple[dict | None, str]:
    """The note the user means (not in Recently Deleted), or a sentence saying why not."""
    rows, problem = _catalog()
    if rows is None:
        return None, problem
    rows = [r for r in rows if not r["trash"]]
    if folder:
        folders, problem = _note_folders()
        if folders is None:
            return None, problem
        chosen = _pick_folder(folders, folder)
        if chosen is None:
            names = [f["name"] for f in folders if not f["trash"]]
            return None, f"Notes has no folder called '{folder}'. Its folders: {', '.join(dict.fromkeys(names))}."
        rows = [r for r in rows if r["folder_id"] == chosen["id"]]
    if not str(note or "").strip():
        return None, "Which note? Give its title or words from it."
    hits, exact = _candidates([r["name"] for r in rows], note, "note")
    if not hits:
        near = _near([r["name"] for r in rows], note)
        return None, (f"No note called '{note}'" + (f" in '{folder}'" if folder else "") + "."
                      + (f" Close ones: {near}." if near else "")
                      + " To start one, use create_note.")
    if len(hits) > 1 and not exact:
        names = [f"'{rows[i]['name']}' ({rows[i]['folder']})" for i in hits[:6]]
        return None, f"More than one note matches '{note}': {', '.join(names)}. Ask the user which one."
    return rows[hits[0]], ""


def _note_get(ident: str) -> tuple[dict | None, str]:
    ok, out = _osa(f"""
tell application "Notes"
    if not (exists note id {_q(ident)}) then return "GONE"
    set n to note id {_q(ident)}
    if password protected of n then return "LOCKED{F}" & (name of n)
    set fn to ""
    try
        set fn to name of container of n
    end try
    return "OK{F}" & (name of n) & "{F}" & (count of attachments of n) & "{F}" & fn & "{F}" & (body of n) & "{F}" & (plaintext of n)
end tell""")
    if not ok:
        return None, _failed(out, "Notes")
    if out == "GONE":
        return None, "FAILED: that note isn't there any more."
    parts = out.split(F)
    if parts[0] == "LOCKED":
        return None, f"The note '{parts[1] if len(parts) > 1 else ''}' is locked; the user has to open it themselves."
    if len(parts) < 6:
        return None, "FAILED: Notes gave back something unexpected."
    return {"id": ident, "name": parts[1], "attachments": int(parts[2]) if parts[2].isdigit() else 0,
            "folder": parts[3], "body": parts[4], "text": F.join(parts[5:])}, ""


def _note_set(ident: str, body: str) -> tuple[bool, str, str]:
    """Set a note's HTML body -> (ok, its name after, its plain text after)."""
    ok, out = _osa(f"""
tell application "Notes"
    set n to note id {_q(ident)}
    set body of n to {_q(body)}
    return (name of n) & "{F}" & (plaintext of n)
end tell""")
    if not ok:
        return False, _failed(out, "Notes"), ""
    name, _, text = out.partition(F)
    return True, name, text


def _digest(text: str) -> str:
    return hashlib.sha1(" ".join(str(text).split()).encode()).hexdigest()[:16]


def _keep_body(body: str) -> dict:
    """Where the old body lives for undo: in the journal, or a private file when it is big."""
    if len(body) < 60_000:
        return {"body": body}
    path = undo._files() / f"note-{uuid.uuid4().hex[:10]}.html"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
    return {"body_file": str(path)}


def _record_body(ident: str, title: str, old_body: str, text_after: str, summary: str) -> None:
    undo.record("note_edit", summary, {"kind": "note_body", "id": ident, "title": title,
                                       "after": _digest(text_after), **_keep_body(old_body)})


@undo.inverse("note_body")
def _undo_note_body(spec: dict):
    """Put a note's previous body back - only if nobody changed the note since Mint did."""
    got, problem = _note_get(str(spec["id"]))
    if got is None:
        return problem if problem.startswith("FAILED") else f"FAILED: {problem}"
    if spec.get("after") and _digest(got["text"]) != spec["after"]:
        return f"FAILED: the note '{got['name']}' has been changed since, so Mint left it alone."
    body = spec.get("body")
    if body is None:
        try:
            body = Path(spec["body_file"]).read_text(encoding="utf-8")
        except (OSError, KeyError):
            return "FAILED: the saved copy of the note is gone."
    ok, name, text = _note_set(got["id"], body)
    if not ok:
        return name
    redo = {"kind": "note_body", "id": got["id"], "title": name, "after": _digest(text), **_keep_body(got["body"])}
    return f"the note '{name}' is back as it was.", redo


@undo.inverse("note_move")
def _undo_note_move(spec: dict):
    """A note Mint moved or deleted goes back to the folder it was in."""
    ident, title = str(spec["id"]), str(spec.get("title") or "")
    ok, out = _osa(f"""
tell application "Notes"
    if not (exists note id {_q(ident)}) then return "GONE"
    if not (exists folder id {_q(spec["folder_id"])}) then return "NOFOLDER"
    set n to note id {_q(ident)}
    set was to ""
    try
        set was to id of container of n
    end try
    move n to folder id {_q(spec["folder_id"])}
    return "OK{F}" & was
end tell""")
    if not ok:
        return _failed(out, "Notes")
    if out == "GONE":
        return f"FAILED: the note '{title}' isn't in Notes any more (Recently Deleted empties after 30 days)."
    if out == "NOFOLDER":
        return f"FAILED: the folder '{spec.get('folder', '')}' it came from is gone."
    was = out.partition(F)[2]
    redo = ({"kind": "note_delete", "id": ident, "title": title} if spec.get("was_deleted")
            else {"kind": "note_move", "id": ident, "title": title, "folder_id": was, "folder": ""} if was else None)
    return f"the note '{title}' is back in '{spec.get('folder', 'its folder')}'.", redo


# --- note bodies (Notes' HTML) ---------------------------------------------------------------------

_FIRST_BLOCK = re.compile(r"^(\s*(?:<html\b.*?<body[^>]*>)?\s*)(<(div|h1|h2|h3|p)\b[^>]*>.*?</\3>)", re.S | re.I)
_EMPTY_TAIL = re.compile(r"(?:\s*<div>\s*(?:<br\s*/?>)?\s*</div>)+\s*$", re.I)


def _clean_lines(text: str) -> list[str]:
    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _unbullet(line: str) -> str:
    return re.sub(r"^[-*•–]\s+", "", line.strip())


def _divs(lines: list[str]) -> str:
    return "".join(f"<div>{html.escape(line, quote=False) or '<br>'}</div>" for line in lines)


def _split_end(body: str) -> tuple[str, str]:
    """(the body's content, its closing wrapper '</body></html>' if any)."""
    at = body.lower().rfind("</body>")
    return (body[:at], body[at:]) if at >= 0 else (body, "")


def _append_html(body: str, lines: list[str]) -> str:
    core, tail = _split_end(body)
    core = _EMPTY_TAIL.sub("", core.rstrip())
    last_list = re.search(r"</(ul|ol)>\s*$", core, re.I)
    if last_list:                 # a list note: the new lines become items of that list
        items = "".join(f"<li>{html.escape(_unbullet(line), quote=False)}</li>" for line in lines if line.strip())
        return core[:last_list.start()] + items + core[last_list.start():] + tail
    return core + _divs(lines) + tail


def _prepend_html(body: str, lines: list[str]) -> str:
    """New lines go right under the title (the first line names the note)."""
    first = _FIRST_BLOCK.search(body)
    if not first:
        return _divs(lines) + body
    return body[:first.end()] + _divs(lines) + body[first.end():]


def _retitle_html(body: str, title: str) -> str:
    first = _FIRST_BLOCK.search(body)
    if not first:
        return f"<div><h1>{html.escape(title, quote=False)}</h1></div>" + body
    parts = re.split(r"(<[^>]+>)", first.group(2))
    placed = False
    for i, part in enumerate(parts):
        if part.startswith("<") or not part.strip():
            continue
        parts[i] = "" if placed else html.escape(title, quote=False)
        placed = True
    block = "".join(parts) if placed else first.group(2).replace(">", ">" + html.escape(title, quote=False), 1)
    return body[:first.start(2)] + block + body[first.end(2):]


def _pattern(find: str, flags: int) -> re.Pattern:
    text = re.escape(find)
    if re.match(r"\w", find):
        text = r"(?<!\w)" + text
    if re.search(r"\w$", find):
        text += r"(?!\w)"
    return re.compile(text, flags)


_BLOCK_TAG = re.compile(r"</?(div|p|li|ul|ol|h[1-6]|br|tr|td|th|table|blockquote|pre)\b", re.I)


def _replace_html(body: str, find: str, new: str, every: bool = True) -> tuple[str, int]:
    """Replace `find` with `new` in the note's text, leaving its tags (bold, lists, headings) alone. Words are
    matched whole across formatting ("<b>milk</b>shake" is not "milk"); text split by formatting ("<b>oat</b>
    milk") takes the formatting of where it starts. Case-sensitive first, then ignoring case."""
    parts = re.split(r"(<[^>]+>)", body)
    for flags in (0, re.I):
        rx = _pattern(find, flags)
        joined, spans = "", {}
        for i, part in enumerate(parts):
            if part.startswith("<"):
                if _BLOCK_TAG.match(part):
                    joined += "\n"                     # lines never run together
                continue
            if part:
                text = html.unescape(part)
                spans[i] = (len(joined), len(joined) + len(text))
                joined += text
        found = list(rx.finditer(joined))[:None if every else 1]
        if not found:
            continue
        edits: dict[int, list[tuple[int, int, str]]] = {}
        for match in found:
            first = True
            for i, (a, b) in spans.items():
                if b <= match.start() or a >= match.end():
                    continue
                cut = (max(match.start(), a) - a, min(match.end(), b) - a, new if first else "")
                edits.setdefault(i, []).append(cut)
                first = False
        out = list(parts)
        for i, cuts in edits.items():
            text = html.unescape(parts[i])
            for a, b, put in sorted(cuts, reverse=True):
                text = text[:a] + put + text[b:]
            out[i] = html.escape(text, quote=False)
        return "".join(out), len(found)
    return body, 0


def _snippet(text: str, query: str, width: int = 70) -> str:
    flat = " ".join(str(text).split())
    at = flat.lower().find(query.lower())
    if at < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, at - width // 3)
    piece = flat[start:start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(flat) else "")


# --- the notes tool ----------------------------------------------------------------------------------

def notes_search(query: str, folder: str = "", limit: int = 8) -> str:
    query = " ".join(str(query or "").split())
    if not query:
        return "Search for what? Give words from the note."
    where = ""
    if folder:
        folders, problem = _note_folders()
        if folders is None:
            return problem
        chosen = _pick_folder(folders, folder)
        if chosen is None:
            return f"Notes has no folder called '{folder}'."
        where = f"if (id of f) is {_q(chosen['id'])} then"
    limit = max(1, min(int(limit or 8), 15))
    ok, out = _osa(f"""
tell application "Notes"
    set out to ""
    set n to 0
    repeat with f in folders
        set fn to name of f
        {where or "if true then"}
            set found to (notes of f whose plaintext contains {_q(query)})
            repeat with x in found
                if n < {limit} then
                    set n to n + 1
                    set pt to ""
                    try
                        set pt to plaintext of x
                    end try
                    set out to out & (name of x) & "{F}" & fn & "{F}" & pt & "{R}"
                end if
            end repeat
        end if
    end repeat
    return out
end tell""", timeout=60)
    if not ok:
        return _failed(out, "Notes")
    hits = []
    for line in out.split(R):
        parts = line.split(F)
        if len(parts) >= 3 and not _is_trash(parts[1]):
            hits.append(f"'{parts[0]}' ({parts[1]}): {_snippet(F.join(parts[2:]), query)}")
    if not hits:
        return f"No note has '{query}' in it" + (f" in '{folder}'." if folder else ".")
    return f"{len(hits)} note{'s' if len(hits) > 1 else ''} with '{query}': " + " | ".join(hits)


def notes_read(note: str, folder: str = "") -> str:
    row, problem = _find_note(note, folder)
    if row is None:
        return problem
    got, problem = _note_get(row["id"])
    if got is None:
        return problem
    text = got["text"].strip()
    lines = text.split("\n")
    if lines and lines[0].strip() == got["name"].strip():
        text = "\n".join(lines[1:]).strip()
    if len(text) > 4000:
        text = text[:4000] + " … (truncated)"
    extra = f" It also has {got['attachments']} attachment(s)." if got["attachments"] else ""
    return f"The note '{got['name']}' ({got['folder'] or row['folder']}):\n{text or '(empty)'}{extra}"


def _change_note(note: str, folder: str, change, summary: str, check=None) -> tuple[str, str]:
    """Read the note, change its body with `change(body) -> (new body, problem)`, write it, verify
    (`check(plain text after, name after) -> problem`), and keep the old body for undo.
    -> (problem or "", the note's name before the change)."""
    row, problem = _find_note(note, folder)
    if row is None:
        return problem, ""
    got, problem = _note_get(row["id"])
    if got is None:
        return problem, ""
    if got["attachments"]:
        return (f"NOT CHANGED: the note '{got['name']}' has {got['attachments']} picture(s) or attachment(s), and "
                "rewriting it through scripting can lose them. Tell the user; they can edit it in Notes."), got["name"]
    new_body, problem = change(got["body"])
    if problem:
        return problem, got["name"]
    if new_body == got["body"]:
        return f"The note '{got['name']}' already reads that way; nothing to change.", got["name"]
    ok, name, text = _note_set(got["id"], new_body)
    if not ok:
        return name, got["name"]
    wrong = check(text, name) if check else ""
    _record_body(got["id"], name, got["body"], text, summary.format(name=got["name"]))
    if wrong:
        return f"FAILED: Notes took the change but {wrong}. Say 'undo' to put the note back.", got["name"]
    return "", got["name"]


def notes_append(note: str, text: str, folder: str = "", top: bool = False) -> str:
    lines = _clean_lines(text)
    if not lines:
        return "Add what? Give the lines to add."
    add = _prepend_html if top else _append_html

    def check(after: str, _name: str) -> str:
        missing = [line for line in lines if line.strip()
                   and " ".join(_unbullet(line).split()) not in " ".join(after.split())]
        return f"these lines didn't show up in it: {', '.join(missing[:3])}" if missing else ""

    said, name = _change_note(note, folder, lambda body: (add(body, lines), ""),
                              f"adding {len(lines)} line{'s' if len(lines) > 1 else ''} to the note '{{name}}'", check)
    if said:
        return said
    where = "at the top of" if top else "to"
    shown = "; ".join(_unbullet(line) for line in lines[:4] if line.strip()) + (" …" if len(lines) > 4 else "")
    return f"Added {shown} {where} the note '{name}'."


def notes_replace(note: str, find: str, new: str, folder: str = "", every: bool = True) -> str:
    find = str(find or "")
    if not find.strip():
        return "Replace what? Give the text to find."
    counted = [0]

    def change(body: str):
        out, n = _replace_html(body, find, str(new or ""), every)
        counted[0] = n
        return out, ("" if n else f"'{find}' isn't in that note, so nothing changed.")

    def check(after: str, _name: str) -> str:
        return "" if not new or " ".join(str(new).split()) in " ".join(after.split()) else "the new text isn't in it"

    said, name = _change_note(note, folder, change, f"changing '{find[:30]}' in the note '{{name}}'", check)
    if said:
        return said
    n = counted[0]
    return (f"Changed '{find}' to '{new}' in '{name}'" + (f" ({n} places)." if n > 1 else ".")
            if new else f"Took '{find}' out of '{name}'.")


def notes_rename(note: str, title: str, folder: str = "") -> str:
    title = " ".join(str(title or "").split())
    if not title:
        return "Rename it to what?"

    def check(_after: str, name: str) -> str:
        return "" if name.strip() == title else f"its title reads '{name}'"

    said, name = _change_note(note, folder, lambda body: (_retitle_html(body, title), ""),
                              "renaming the note '{name}'", check)
    return said or f"Renamed the note '{name}' to '{title}'."


def notes_move(note: str, folder: str, create_folder: bool = False) -> str:
    if not str(folder or "").strip():
        return "Move it to which folder?"
    row, problem = _find_note(note)
    if row is None:
        return problem
    folders, problem = _note_folders()
    if folders is None:
        return problem
    target = _pick_folder(folders, folder)
    if target is None:
        label = " ".join(str(folder).strip(" '\"").split())
        if not create_folder:
            names = [f["name"] for f in folders if not f["trash"]]
            return (f"NOT MOVED: Notes has no folder called '{label}'. Its folders: {', '.join(dict.fromkeys(names))}. "
                    "Ask the user which one, or whether to make it (then call again with create_folder=true).")
        ok, out = _osa(f'tell application "Notes"\nset f to make new folder with properties {{name:{_q(label)}}}\n'
                       f'return id of f\nend tell')
        if not ok:
            return _failed(out, "Notes")
        target = {"id": out.strip(), "name": label}
    if target["id"] == row["folder_id"]:
        return f"'{row['name']}' is already in '{target['name']}'."
    ok, out = _osa(f"""
tell application "Notes"
    move note id {_q(row["id"])} to folder id {_q(target["id"])}
    set ids to id of (notes of folder id {_q(target["id"])} whose name is {_q(row["name"])})
    if (count of ids) is 0 then return ""
    return item 1 of ids
end tell""")
    if not ok:
        return _failed(out, "Notes")
    now_id = out.strip() or row["id"]
    undo.record("note_move", f"moving the note '{row['name']}' to '{target['name']}'",
                {"kind": "note_move", "id": now_id, "title": row["name"], "folder_id": row["folder_id"],
                 "folder": row["folder"]})
    return f"Moved '{row['name']}' from '{row['folder']}' to '{target['name']}'."


def notes_delete(note: str, folder: str = "") -> str:
    if not _asked_to_delete():
        return _not_asked(f"the note '{note}'")
    row, problem = _find_note(note, folder)
    if row is None:
        return problem
    ok, out = _osa(f"""
tell application "Notes"
    delete note id {_q(row["id"])}
    if exists note id {_q(row["id"])} then return {_q(row["id"])}
    set out to ""
    repeat with f in folders
        if (id of f) is not {_q(row["folder_id"])} then
            try
                set ids to id of (notes of f whose name is {_q(row["name"])})
                if (count of ids) > 0 then set out to item 1 of ids
            end try
        end if
    end repeat
    return out
end tell""")
    if not ok:
        return _failed(out, "Notes")
    undo.record("note_delete", f"deleting the note '{row['name']}'",
                {"kind": "note_move", "id": out.strip() or row["id"], "title": row["name"],
                 "folder_id": row["folder_id"], "folder": row["folder"], "was_deleted": True})
    return f"Moved the note '{row['name']}' to Recently Deleted (Notes keeps it 30 days; 'undo' brings it back)."


def notes_folders() -> str:
    folders, problem = _note_folders()
    if folders is None:
        return problem
    live = [f for f in folders if not f["trash"]]
    if not live:
        return "Notes has no folders yet."
    twice = {f["name"] for f in live if sum(g["name"] == f["name"] for g in live) > 1}
    shown = [f"{f['name']}" + (f" in {f['account']}" if f["name"] in twice and f["account"] else "") + f" ({f['count']})"
             for f in live if f["count"] or f["name"] not in twice]
    return "Notes folders: " + ", ".join(shown) + "."


def notes_recent(count: int = 5) -> str:
    rows, problem = _catalog(dates=True)
    if rows is None:
        return problem
    rows = sorted((r for r in rows if not r["trash"] and r.get("modified")), key=lambda r: r["modified"], reverse=True)
    if not rows:
        return "There are no notes."
    count = max(1, min(int(count or 5), 15))
    return "Last edited: " + "; ".join(f"'{r['name']}' ({r['folder']}, {_ago(r['modified'])})" for r in rows[:count]) + "."


def notes_tool(args: dict) -> str:
    action = str(args.get("action") or "read").lower()
    note, folder = str(args.get("note") or ""), str(args.get("folder") or "")
    text = str(args.get("text") or "")
    if action == "search":
        return notes_search(str(args.get("query") or text or note), folder, int(args.get("count") or 8))
    if action == "read":
        return notes_read(note, folder)
    if action == "append":
        return notes_append(note, text, folder)
    if action == "prepend":
        return notes_append(note, text, folder, top=True)
    if action == "replace":
        return notes_replace(note, str(args.get("find") or ""), text, folder, args.get("all") is not False)
    if action == "rename":
        return notes_rename(note, text, folder)
    if action == "move":
        return notes_move(note, folder or text, bool(args.get("create_folder")))
    if action == "folders":
        return notes_folders()
    if action == "recent":
        return notes_recent(int(args.get("count") or 5))
    if action == "delete":
        return notes_delete(note, folder)
    return "Unknown notes action. Use search, read, append, prepend, replace, rename, move, folders, recent or delete."


# ==================================================================================================
# Reminders (EventKit)
# ==================================================================================================

def _reminder_store():
    import EventKit
    return skills._event_store(EventKit.EKEntityTypeReminder)


def _fetch(store, predicate) -> list:
    done, found = threading.Event(), []

    def got(items):
        found.extend(items or [])
        done.set()

    store.fetchRemindersMatchingPredicate_completion_(predicate, got)
    done.wait(20)
    return found


_UNDEFINED = 2 ** 40      # NSDateComponentUndefined is NSIntegerMax


def _due(reminder) -> tuple[dt.datetime | None, bool]:
    """(when it is due, date only?)"""
    from Foundation import NSCalendar
    comps = reminder.dueDateComponents()
    if comps is None:
        return None, False
    date_only = comps.hour() > _UNDEFINED
    date = NSCalendar.currentCalendar().dateFromComponents_(comps)
    return _py(date), date_only


def _date_components(day: dt.date):
    from Foundation import NSDateComponents
    comps = NSDateComponents.alloc().init()
    comps.setYear_(day.year)
    comps.setMonth_(day.month)
    comps.setDay_(day.day)
    return comps


def _reminder_snapshot(item) -> dict:
    due, date_only = _due(item)
    alarms = [_py(a.absoluteDate()).isoformat() for a in (item.alarms() or []) if a.absoluteDate() is not None]
    return {"kind": "reminder_restore", "id": str(item.calendarItemIdentifier()), "title": str(item.title() or ""),
            "notes": str(item.notes() or ""), "list": str(item.calendar().calendarIdentifier()),
            "list_name": str(item.calendar().title()), "done": bool(item.isCompleted()),
            "due": due.isoformat() if due else "", "date_only": date_only, "alarms": alarms,
            "priority": int(item.priority() or 0)}


def _set_due(item, when: dt.datetime | None, date_only: bool = False, alarms: list[str] | None = None) -> None:
    import EventKit
    for alarm in list(item.alarms() or []):
        item.removeAlarm_(alarm)
    if when is None:
        item.setDueDateComponents_(None)
        return
    item.setDueDateComponents_(_date_components(when.date()) if date_only else skills._due_components(when))
    for moment in (alarms if alarms is not None else ([] if date_only else [when.isoformat()])):
        item.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(_nsdate(dt.datetime.fromisoformat(moment))))


def _apply_reminder(store, item, snap: dict) -> None:
    item.setTitle_(snap["title"])
    item.setNotes_(snap.get("notes") or None)
    calendar = store.calendarWithIdentifier_(snap["list"]) if snap.get("list") else None
    if calendar is not None:
        item.setCalendar_(calendar)
    item.setCompleted_(bool(snap.get("done")))
    item.setPriority_(int(snap.get("priority") or 0))
    _set_due(item, _iso(snap.get("due")), bool(snap.get("date_only")), list(snap.get("alarms") or []))


@undo.inverse("reminder_restore")
def _undo_reminder(spec: dict):
    """Put a reminder's fields back; a deleted one is made again from them."""
    import EventKit
    store, problem = _reminder_store()
    if problem:
        return f"FAILED: {problem}"
    item = store.calendarItemWithIdentifier_(spec["id"])
    if item is None:
        item = EventKit.EKReminder.reminderWithEventStore_(store)
        _apply_reminder(store, item, spec)
        if item.calendar() is None:
            item.setCalendar_(store.defaultCalendarForNewReminders())
        ok, error = store.saveReminder_commit_error_(item, True, None)
        if not ok:
            return f"FAILED: {error}"
        return (f"the reminder '{spec['title']}' is back in '{item.calendar().title()}'.",
                {"kind": "reminder_delete", "id": str(item.calendarItemIdentifier()), "title": spec["title"]})
    before = _reminder_snapshot(item)
    _apply_reminder(store, item, spec)
    ok, error = store.saveReminder_commit_error_(item, True, None)
    if not ok:
        return f"FAILED: {error}"
    return f"the reminder '{spec['title']}' is back as it was.", before


def _reminder_lists(store) -> list:
    import EventKit
    return list(store.calendarsForEntityType_(EventKit.EKEntityTypeReminder) or [])


def _list_named(store, name: str) -> tuple[object | None, str]:
    lists = _reminder_lists(store)
    titles = [str(c.title()) for c in lists]
    real = skills._pick(titles, name, "list")
    if real is None:
        return None, f"Reminders has no list called '{name}'. Its lists: {', '.join(titles)}."
    return lists[titles.index(real)], ""


def _open_items(store, calendars) -> list:
    return _fetch(store, store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, calendars))


def _done_items(store, calendars, days: int = 60) -> list:
    now = dt.datetime.now()
    return _fetch(store, store.predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
        _nsdate(now - dt.timedelta(days=days)), _nsdate(now + dt.timedelta(minutes=5)), calendars))


def _find_reminder(store, title: str, list_name: str = "", completed: bool = False) -> tuple[object | None, str]:
    calendars = None
    if list_name:
        calendar, problem = _list_named(store, list_name)
        if calendar is None:
            return None, problem
        calendars = [calendar]
    items = _done_items(store, calendars) if completed else _open_items(store, calendars)
    names = [str(i.title() or "") for i in items]
    hits, exact = _candidates(names, title, "reminder")
    if not hits:
        state = "completed (in the last 60 days)" if completed else "open"
        near = _near(names, title)
        return None, (f"No {state} reminder called '{title}'" + (f" in '{list_name}'" if list_name else "") + "."
                      + (f" Close ones: {near}." if near else ""))
    if len(hits) > 1 and not exact:
        shown = [f"'{names[i]}' ({items[i].calendar().title()})" for i in hits[:6]]
        return None, f"More than one reminder matches '{title}': {', '.join(shown)}. Ask the user which one."
    return items[hits[0]], ""


def _due_text(item) -> str:
    due, date_only = _due(item)
    if due is None:
        return ""
    return _speak_day(due.date()) if date_only else _speak_time(due)


def reminders_list(list_name: str = "", due: str = "") -> str:
    store, problem = _reminder_store()
    if problem:
        return problem
    calendars = None
    if list_name:
        calendar, problem = _list_named(store, list_name)
        if calendar is None:
            return problem
        calendars = [calendar]
    due = (due or ("all" if list_name else "today")).lower()
    items = _open_items(store, calendars)
    now = dt.datetime.now()
    end_today = dt.datetime.combine(dt.date.today(), dt.time.max)

    def when(item):
        moment, date_only = _due(item)
        if moment is not None and date_only:
            moment = dt.datetime.combine(moment.date(), dt.time.max)      # due "that day": overdue only after it
        return moment

    def label(item, with_list: bool) -> str:
        bits = [x for x in (_due_text(item), str(item.calendar().title()) if with_list else "") if x]
        return f"{item.title()}" + (f" ({', '.join(bits)})" if bits else "")

    if due in ("today", "overdue"):
        late = sorted([i for i in items if when(i) and when(i) < now], key=when)
        today = sorted([i for i in items if when(i) and now <= when(i) <= end_today], key=when)
        parts = []
        if late:
            parts.append(f"Overdue ({len(late)}): " + "; ".join(label(i, not list_name) for i in late[:12]))
        if due == "today" and today:
            parts.append(f"Due today ({len(today)}): " + "; ".join(label(i, not list_name) for i in today[:12]))
        if not parts:
            return "Nothing overdue." if due == "overdue" else "Nothing due today, and nothing overdue."
        return ". ".join(parts) + "."
    if not items:
        return f"The '{list_name}' list is empty." if list_name else "No open reminders."
    items.sort(key=lambda i: (when(i) is None, when(i) or now))
    head = f"'{calendars[0].title()}' has {len(items)}" if list_name else f"{len(items)} open reminders"
    return f"{head}: " + "; ".join(label(i, not list_name) for i in items[:20]) + (" …" if len(items) > 20 else "") + "."


def _save_reminder(store, item, before: dict, summary: str) -> str:
    ok, error = store.saveReminder_commit_error_(item, True, None)
    if not ok:
        return f"FAILED: could not save the reminder: {error}"
    undo.record("reminder_edit", summary, before)
    return ""


def reminders_tool(args: dict) -> str:
    import EventKit
    action = str(args.get("action") or "list").lower()
    if action == "list":
        return reminders_list(str(args.get("list") or ""), str(args.get("due") or ""))
    store, problem = _reminder_store()
    if problem:
        return problem
    title, list_name = str(args.get("reminder") or ""), str(args.get("list") or "")
    if not title.strip():
        return "Which reminder? Give its title."
    item, problem = _find_reminder(store, title, list_name, completed=(action == "uncomplete"))
    if item is None:
        return problem
    name, before = str(item.title()), _reminder_snapshot(item)
    where = str(item.calendar().title())

    if action in ("complete", "uncomplete"):
        item.setCompleted_(action == "complete")
        said = _save_reminder(store, item, before, f"{'ticking off' if action == 'complete' else 'reopening'} '{name}'")
        return said or (f"Ticked off '{name}'." if action == "complete" else f"'{name}' is open again in '{where}'.")

    if action == "reschedule":
        when_text = str(args.get("when") or "").strip()
        if args.get("in_minutes"):
            due, date_only = dt.datetime.now() + dt.timedelta(minutes=int(args["in_minutes"])), False
        elif when_text.lower() in ("none", "no date", "clear", "remove"):
            due, date_only = None, False
        else:
            due = _iso(when_text)
            if due is None:
                return "Give the new time as 2026-10-02T09:00 (or just the date 2026-10-02), or minutes from now."
            date_only = len(when_text) <= 10
            due = _new_day(due)
        _set_due(item, due, date_only)
        said = _save_reminder(store, item, before, f"rescheduling '{name}'")
        if said:
            return said
        if due is None:
            return f"'{name}' has no due date now."
        return f"'{name}' is now due " + (_speak_day(due.date()) if date_only else _speak_time(due)) + "."

    if action == "move":
        to = str(args.get("to_list") or "")
        if not to:
            return "Move it to which list?"
        calendar, problem = _list_named(store, to)
        if calendar is None:
            return "NOT MOVED: " + problem + " Ask the user which list."
        if calendar.calendarIdentifier() == item.calendar().calendarIdentifier():
            return f"'{name}' is already in '{where}'."
        item.setCalendar_(calendar)
        said = _save_reminder(store, item, before, f"moving '{name}' to the '{calendar.title()}' list")
        return said or f"Moved '{name}' from '{where}' to '{calendar.title()}'."

    if action == "rename":
        new = " ".join(str(args.get("new_title") or "").split())
        if not new:
            return "Rename it to what?"
        item.setTitle_(new)
        said = _save_reminder(store, item, before, f"renaming the reminder '{name}'")
        return said or f"Renamed '{name}' to '{new}'."

    if action == "delete":
        if not _asked_to_delete():
            return _not_asked(f"the reminder '{name}'")
        ok, error = store.removeReminder_commit_error_(item, True, None)
        if not ok:
            return f"FAILED: {error}"
        undo.record("reminder_edit", f"deleting the reminder '{name}'", before)
        return f"Deleted the reminder '{name}' from '{where}'."
    return "Unknown action. Use list, complete, uncomplete, reschedule, move, rename or delete."


# ==================================================================================================
# Calendar (EventKit)
# ==================================================================================================

def _calendar_store():
    import EventKit
    return skills._event_store(EventKit.EKEntityTypeEvent)


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_day(text: str) -> dt.date | None:
    text = str(text or "").strip().lower()
    today = dt.date.today()
    if not text:
        return None
    moment = _iso(text)
    if moment is not None:
        return moment.date()
    if text in ("today", "tonight"):
        return today
    if text == "tomorrow":
        return today + dt.timedelta(days=1)
    if text == "yesterday":
        return today - dt.timedelta(days=1)
    for i, name in enumerate(_WEEKDAYS):
        if text.replace("next ", "").replace("this ", "").startswith(name[:3]):
            ahead = (i - today.weekday()) % 7
            return today + dt.timedelta(days=ahead + (7 if text.startswith("next ") and ahead == 0 else 0))
    return None


def _event_snapshot(event, at: dt.datetime | None = None) -> dict:
    return {"kind": "event_restore", "id": str(event.eventIdentifier()), "title": str(event.title() or ""),
            "start": _py(event.startDate()).isoformat(), "end": _py(event.endDate()).isoformat(),
            "all_day": bool(event.isAllDay()), "location": str(event.location() or ""),
            "notes": str(event.notes() or ""), "calendar": str(event.calendar().calendarIdentifier()),
            "at": (at or _py(event.startDate())).isoformat()}


def _events_between(store, start: dt.datetime, end: dt.datetime) -> list:
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(_nsdate(start), _nsdate(end), None)
    return sorted(store.eventsMatchingPredicate_(predicate) or [], key=lambda e: e.startDate().timeIntervalSince1970())


def _event_at(store, ident: str, at: str):
    """The event (the right occurrence of a repeating one) that starts at `at`."""
    moment = _iso(at)
    if moment is not None:
        for event in _events_between(store, moment - dt.timedelta(minutes=1), moment + dt.timedelta(days=1)):
            if str(event.eventIdentifier()) == ident and abs((_py(event.startDate()) - moment).total_seconds()) < 60:
                return event
    return store.eventWithIdentifier_(ident)


def _apply_event(store, event, snap: dict) -> None:
    event.setTitle_(snap["title"])
    event.setAllDay_(bool(snap.get("all_day")))
    event.setStartDate_(_nsdate(dt.datetime.fromisoformat(snap["start"])))
    event.setEndDate_(_nsdate(dt.datetime.fromisoformat(snap["end"])))
    event.setLocation_(snap.get("location") or None)
    event.setNotes_(snap.get("notes") or None)
    calendar = store.calendarWithIdentifier_(snap["calendar"]) if snap.get("calendar") else None
    if calendar is not None:
        event.setCalendar_(calendar)


@undo.inverse("event_restore")
def _undo_event(spec: dict):
    """Put an event's time, title, place and notes back; a deleted one is made again."""
    import EventKit
    store, problem = _calendar_store()
    if problem:
        return f"FAILED: {problem}"
    event = _event_at(store, spec["id"], spec.get("at") or "") if not spec.get("deleted") else None
    if event is None:
        event = EventKit.EKEvent.eventWithEventStore_(store)
        _apply_event(store, event, spec)
        if event.calendar() is None:
            event.setCalendar_(store.defaultCalendarForNewEvents())
        ok, error = store.saveEvent_span_commit_error_(event, EventKit.EKSpanThisEvent, True, None)
        if not ok:
            return f"FAILED: {error}"
        return (f"'{spec['title']}' is back on the calendar, {_speak_time(dt.datetime.fromisoformat(spec['start']))}.",
                {"kind": "event_delete", "id": str(event.eventIdentifier()), "title": spec["title"]})
    before = _event_snapshot(event, at=dt.datetime.fromisoformat(spec["start"]))
    _apply_event(store, event, spec)
    ok, error = store.saveEvent_span_commit_error_(event, EventKit.EKSpanThisEvent, True, None)
    if not ok:
        return f"FAILED: {error}"
    return f"'{spec['title']}' is back as it was ({_speak_time(dt.datetime.fromisoformat(spec['start']))}).", before


def _find_event(store, title: str, day: str = "") -> tuple[object | None, str]:
    if not str(title or "").strip():
        return None, "Which event? Give its title."
    date = _parse_day(day)
    if day and date is None:
        return None, f"Could not read the day '{day}'. Give it as 2026-10-02, today, tomorrow or a weekday."
    now = dt.datetime.now()
    if date:
        start = dt.datetime.combine(date, dt.time.min)
        events = _events_between(store, start, start + dt.timedelta(days=1))
    else:
        events = _events_between(store, now - dt.timedelta(days=1), now + dt.timedelta(days=45))
    names = [str(e.title() or "") for e in events]
    hits, exact = _candidates(names, title, "event")
    if not hits:
        near = _near(names, title)
        span = f"on {date:%A %-d %B}" if date else "from yesterday through the next 45 days"
        return None, f"No event called '{title}' {span}." + (f" Close ones: {near}." if near else "")
    titles = {names[i].lower() for i in hits}
    if len(titles) > 1 and not exact:
        shown = [f"'{names[i]}' {_speak_time(_py(events[i].startDate()))}" for i in hits[:6]]
        return None, f"More than one event matches '{title}': {', '.join(shown)}. Ask the user which one."
    upcoming = [i for i in hits if _py(events[i].endDate()) >= now] or hits
    return events[upcoming[0]], ""


def calendar_tool(args: dict) -> str:
    import EventKit
    action = str(args.get("action") or "find").lower()
    store, problem = _calendar_store()
    if problem:
        return problem
    event, problem = _find_event(store, str(args.get("event") or ""), str(args.get("day") or ""))
    if event is None:
        return problem
    name, begins, ends = str(event.title()), _py(event.startDate()), _py(event.endDate())
    when = f"all day {_speak_day(begins.date())}" if event.isAllDay() else _speak_time(begins)
    if action == "find":
        place = f" at {event.location()}" if event.location() else ""
        return f"'{name}' is {when}-{_speak_time(ends, False)}{place}, on the '{event.calendar().title()}' calendar."
    if not event.calendar().allowsContentModifications():
        return f"NOT CHANGED: '{name}' is on the '{event.calendar().title()}' calendar, which can't be changed from here."
    before = _event_snapshot(event)
    span = EventKit.EKSpanThisEvent

    def save(summary: str) -> str:
        ok, error = store.saveEvent_span_commit_error_(event, span, True, None)
        if not ok:
            return f"FAILED: could not save the event: {error}"
        undo.record("event_edit", summary, {**before, "at": _py(event.startDate()).isoformat()})
        return ""

    if action == "move":
        start_text = str(args.get("start") or "").strip()
        new = _iso(start_text)
        if new is None:
            return "Give the new start as 2026-10-02T15:00, or just a date (2026-10-02) to keep the time."
        length = ends - begins
        if len(start_text) <= 10:                                  # a day only: keep the time
            new = dt.datetime.combine(new.date(), begins.time())
        elif event.isAllDay():
            event.setAllDay_(False)
            length = dt.timedelta(hours=1)
        new = _new_day(new)
        end_new = _iso(args.get("end") or "") or new + length
        if end_new <= new:
            return "FAILED: the end must be after the start."
        event.setStartDate_(_nsdate(new))
        event.setEndDate_(_nsdate(end_new))
        said = save(f"moving '{name}' to {_speak_time(new)}")
        if said:
            return said
        others = [e for e in _events_between(store, new, end_new)
                  if str(e.eventIdentifier()) != str(event.eventIdentifier()) and not e.isAllDay()
                  and e.calendar().calendarIdentifier() == event.calendar().calendarIdentifier()]
        clash = (" It overlaps " + ", ".join(f"'{e.title()}'" for e in others[:3]) + ".") if others else ""
        return f"Moved '{name}' to {_speak_time(new)}-{_speak_time(end_new, False)}.{clash}"
    if action == "rename":
        title = " ".join(str(args.get("new_title") or "").split())
        if not title:
            return "Rename it to what?"
        event.setTitle_(title)
        return save(f"renaming the event '{name}'") or f"Renamed '{name}' to '{title}'."
    if action == "update":
        changed = []
        if args.get("location") is not None:
            event.setLocation_(str(args["location"]) or None)
            changed.append(f"place: {args['location'] or 'none'}")
        if args.get("notes") is not None:
            event.setNotes_(str(args["notes"]) or None)
            changed.append("notes")
        if not changed:
            return "Change what? Give a location or notes."
        return save(f"changing '{name}' ({', '.join(changed)})") or f"Updated '{name}': {', '.join(changed)}."
    if action == "delete":
        if not _asked_to_delete():
            return _not_asked(f"the event '{name}'")
        ok, error = store.removeEvent_span_commit_error_(event, span, True, None)
        if not ok:
            return f"FAILED: {error}"
        undo.record("event_edit", f"deleting the event '{name}'", {**before, "deleted": True})
        repeat = " (just this one; the rest of the series stays)" if event.hasRecurrenceRules() else ""
        return f"Deleted '{name}' ({when}){repeat}."
    return "Unknown action. Use find, move, rename, update or delete."


# ==================================================================================================
# Contacts (read only, through the Contacts app's scripting)
# ==================================================================================================

def _label(raw: str) -> str:
    text = re.sub(r"^_\$!<(.+)>!\$_$", r"\1", str(raw or "")).strip().lower()
    return text


def contacts_lookup(name: str, field: str = "all") -> str:
    name = " ".join(str(name or "").split())
    if not name:
        return "Look up whom? Give a name."
    was_running = _running("com.apple.AddressBook")
    query = re.sub(r"'s$", "", name)
    ok, out = _osa(f"""
tell application "Contacts"
    set found to (people whose name contains {_q(query)})
    if (count of found) is 0 then
        try
            set found to (people whose nickname contains {_q(query)})
        end try
    end if
    set out to ""
    set k to 0
    repeat with p in found
        set k to k + 1
        if k > 6 then exit repeat
        set ph to ""
        repeat with x in phones of p
            set ph to ph & (label of x) & ":" & (value of x) & ";"
        end repeat
        set em to ""
        repeat with x in emails of p
            set em to em & (label of x) & ":" & (value of x) & ";"
        end repeat
        set ad to ""
        repeat with x in addresses of p
            try
                set ad to ad & (label of x) & ":" & (formatted address of x) & ";"
            end try
        end repeat
        set bd to ""
        try
            set bd to my isoDate(birth date of p)
        end try
        set org to ""
        try
            set org to organization of p
            if org is missing value then set org to ""
        end try
        set job to ""
        try
            set job to job title of p
            if job is missing value then set job to ""
        end try
        set fnm to ""
        try
            set fnm to first name of p
            if fnm is missing value then set fnm to ""
        end try
        set out to out & (name of p) & "{F}" & fnm & "{F}" & ph & "{F}" & em & "{F}" & bd & "{F}" & org & "{F}" & job & "{F}" & ad & "{R}"
    end repeat
    set total to count of found
    {"" if was_running else "quit"}
    return (total as text) & "{R}" & out
end tell
{_ISO}""", timeout=40)
    if not ok:
        return "Could not read Contacts. " + skills._automation_hint(out, "Contacts")
    total_text, _, rest = out.partition(R)
    people = []
    for line in rest.split(R):
        parts = line.split(F)
        if len(parts) >= 8:
            people.append(dict(zip(("name", "first", "phones", "emails", "birthday", "company", "job", "addresses"),
                                   parts[:7] + [F.join(parts[7:])])))
    if not people:
        return f"No contact called '{name}'."
    key = query.lower()
    people.sort(key=lambda p: (p["first"].lower() != key and p["name"].lower() != key, p["name"]))
    exact = [p for p in people if p["name"].lower() == key or p["first"].lower() == key]
    shown = exact or people
    total = int(total_text) if total_text.strip().isdigit() else len(people)
    if len(shown) > 3:
        return (f"{total} contacts match '{name}': " + ", ".join(p["name"] for p in shown[:6])
                + ". Ask the user which one.")
    field = str(field or "all").lower()
    return " ".join(_describe_person(p, field) for p in shown[:3])


def _pairs(raw: str) -> list[tuple[str, str]]:
    out = []
    for piece in raw.split(";"):
        if ":" in piece:
            label, _, value = piece.partition(":")
            if value.strip():
                out.append((_label(label), " ".join(value.split())))
    return out


def _describe_person(p: dict, field: str) -> str:
    bits = []
    if field in ("all", "phone", "phones"):
        phones = _pairs(p["phones"])
        bits.append("phone " + ", ".join(f"{v} ({l})" if l else v for l, v in phones[:3]) if phones else "no phone")
    if field in ("all", "email", "emails"):
        emails = _pairs(p["emails"])
        bits.append("email " + ", ".join(f"{v} ({l})" if l else v for l, v in emails[:3]) if emails else "no email")
    if field in ("all", "birthday"):
        born = _iso(p["birthday"])
        if born:
            today = dt.date.today()
            nxt = born.date().replace(year=today.year)
            if nxt < today:
                nxt = nxt.replace(year=today.year + 1)
            days = (nxt - today).days
            soon = "today!" if days == 0 else "tomorrow" if days == 1 else f"in {days} days" if days < 60 else ""
            year = f" {born.year}" if born.year > 1604 else ""
            bits.append(f"birthday {born:%-d %B}{year}" + (f" ({soon})" if soon else ""))
        elif field == "birthday":
            bits.append("no birthday saved")
    if field in ("all", "company", "work") and (p["company"] or p["job"]):
        bits.append("works " + " at ".join(x for x in (p["job"], p["company"]) if x))
    if field in ("all", "address") and p["addresses"]:
        addresses = _pairs(p["addresses"].replace("\n", ", "))
        if addresses:
            bits.append("address " + "; ".join(f"{v} ({l})" if l else v for l, v in addresses[:2]))
    elif field == "address":
        bits.append("no address saved")
    return f"{p['name']}: " + "; ".join(bits) + "."


# ==================================================================================================
# Maps
# ==================================================================================================

_MODES = {"driving": ("d", 1, "by car"), "car": ("d", 1, "by car"), "drive": ("d", 1, "by car"),
          "walking": ("w", 2, "on foot"), "walk": ("w", 2, "on foot"),
          "transit": ("r", 4, "by public transport"), "public transport": ("r", 4, "by public transport"),
          "train": ("r", 4, "by public transport"), "bus": ("r", 4, "by public transport"),
          "cycling": ("c", 8, "by bike"), "bike": ("c", 8, "by bike"), "bicycle": ("c", 8, "by bike")}

_ETA_JS = r"""
ObjC.import('MapKit');
function run(argv) {
  var out = {};
  function spin(done, secs) {
    var until = $.NSDate.dateWithTimeIntervalSinceNow(secs);
    while (!done() && $.NSDate.date.compare(until) < 0)
      $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.1));
  }
  function find(q, key) {
    var req = $.MKLocalSearchRequest.alloc.init;
    req.naturalLanguageQuery = q;
    var finished = false;
    $.MKLocalSearch.alloc.initWithRequest(req).startWithCompletionHandler(function (resp, err) {
      if (resp && !resp.isNil() && resp.mapItems.count > 0) out[key] = resp.mapItems.objectAtIndex(0);
      finished = true;
    });
    spin(function () { return finished; }, 8);
  }
  find(argv[0], 'a');
  find(argv[1], 'b');
  if (!out.a || !out.b) return JSON.stringify({error: 'place', a: !!out.a, b: !!out.b});
  var r = $.MKDirectionsRequest.alloc.init;
  r.source = out.a;
  r.destination = out.b;
  r.transportType = parseInt(argv[2]);
  var res = null;
  $.MKDirections.alloc.initWithRequest(r).calculateETAWithCompletionHandler(function (resp, err) {
    if (resp && !resp.isNil()) res = {secs: resp.expectedTravelTime, meters: resp.distance};
    else res = {error: (err && !err.isNil()) ? ObjC.unwrap(err.localizedDescription) : 'no route'};
  });
  spin(function () { return res !== null; }, 12);
  res = res || {error: 'timeout'};
  res.a = ObjC.unwrap(out.a.name);
  res.b = ObjC.unwrap(out.b.name);
  return JSON.stringify(res);
}
"""


def maps_url(origin: str, destination: str, mode: str = "driving") -> str:
    flag = _MODES.get(str(mode or "driving").lower(), _MODES["driving"])[0]
    query = {"daddr": destination, "dirflg": flag}
    if origin and origin.strip().lower() not in ("here", "current location", "my location"):
        query = {"saddr": origin, **query}
    return "maps://?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)


def travel_time(origin: str, destination: str, mode: str = "driving") -> dict:
    """{"secs", "meters", "a", "b"} from MapKit, or {"error"}. Needs a named start: without Location
    permission 'here' is unknown."""
    import json
    kind = _MODES.get(str(mode or "driving").lower(), _MODES["driving"])[1]
    try:
        done = subprocess.run(["osascript", "-l", "JavaScript", "-e", _ETA_JS, origin, destination, str(kind)],
                              capture_output=True, text=True, timeout=30, check=False)
        return json.loads(done.stdout.strip() or "{}") or {"error": done.stderr.strip()[:120] or "no answer"}
    except (subprocess.TimeoutExpired, ValueError, OSError) as error:
        return {"error": str(error)[:120]}


def _duration(secs: float) -> str:
    minutes = max(1, round(secs / 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min" if minutes % 60 else f"{minutes // 60} h"


def _distance(meters: float) -> str:
    try:
        from Foundation import NSLocale
        metric = bool(NSLocale.currentLocale().usesMetricSystem())
    except Exception:
        metric = True
    return f"{meters / 1000:.1f} km" if metric else f"{meters / 1609.344:.1f} miles"


def maps_tool(args: dict) -> str:
    action = str(args.get("action") or "directions").lower()
    origin, destination = str(args.get("from") or "").strip(), str(args.get("to") or "").strip()
    mode = str(args.get("mode") or "driving").lower()
    if action == "show":
        place = destination or origin
        if not place:
            return "Show where?"
        subprocess.run(["open", "maps://?" + urllib.parse.urlencode({"q": place}, quote_via=urllib.parse.quote)],
                       check=False)
        return f"Opened {place} in Maps."
    if not destination:
        return "Directions to where?"
    words = _MODES.get(mode, _MODES["driving"])[2]
    here = not origin or origin.lower() in ("here", "current location", "my location")
    eta = ""
    if not here:
        got = travel_time(origin, destination, mode)
        if "secs" in got:
            eta = (f"About {_duration(got['secs'])} {words} ({_distance(got['meters'])}) from {got.get('a') or origin} "
                   f"to {got.get('b') or destination}")
    if action == "eta" and not eta:
        return ("Couldn't work out the travel time" + (" from here (Mint doesn't know where 'here' is); give a "
                "starting place, or open directions in Maps" if here else "") + ".")
    if action == "eta":
        return eta + "."
    url = maps_url(origin, destination, mode)
    if args.get("open") is not False:
        subprocess.run(["open", url], check=False)
    opened = f"opened directions {words} to {destination} in Maps" + ("" if not here else " from where you are")
    return (f"{eta}; {opened}." if eta else opened[0].upper() + opened[1:] + ".")


# ==================================================================================================
# Safari
# ==================================================================================================

def _bookmark_tree() -> tuple[dict | None, str]:
    try:
        with open(BOOKMARKS, "rb") as handle:
            return plistlib.load(handle), ""
    except PermissionError:
        return None, ("macOS protects Safari's bookmarks: Mint can read them only with Full Disk Access (System "
                      "Settings > Privacy & Security > Full Disk Access > Mint).")
    except (OSError, plistlib.InvalidFileException) as error:
        return None, f"Couldn't read Safari's bookmarks: {error}"


_SPECIAL = {"BookmarksBar": "Favorites", "BookmarksMenu": "Bookmarks Menu", "com.apple.ReadingList": "Reading List"}


def _folder_title(node: dict) -> str:
    title = str(node.get("Title") or "")
    return _SPECIAL.get(title, title)


def _walk_folders(node: dict, path: str = ""):
    for child in node.get("Children") or []:
        if child.get("WebBookmarkType") == "WebBookmarkTypeList":
            title = _folder_title(child)
            yield child, title, (f"{path}/{title}" if path else title)
            yield from _walk_folders(child, f"{path}/{title}" if path else title)


def _leaf(child: dict) -> tuple[str, str]:
    title = str((child.get("URIDictionary") or {}).get("title") or child.get("Title") or "")
    return title, str(child.get("URLString") or "")


def safari_bookmarks(folder: str = "") -> str:
    tree, problem = _bookmark_tree()
    if tree is None:
        return problem
    folders = list(_walk_folders(tree))
    if not folder:
        tops = [f"{title} ({len([c for c in (node.get('Children') or []) if c.get('WebBookmarkType') != 'WebBookmarkTypeList'])})"
                for node, title, path in folders if "/" not in path]
        return "Safari's bookmark folders: " + ", ".join(tops) + ". Name one to hear what's in it."
    wanted = {"favorites bar": "Favorites", "favourites": "Favorites", "favourites bar": "Favorites",
              "bookmarks bar": "Favorites"}.get(folder.strip().lower(), folder)
    titles = [title for _, title, _ in folders]
    hits, _exact = _candidates(titles, wanted, "folder")
    if not hits:
        return f"Safari has no bookmark folder called '{folder}'. Folders: {', '.join(dict.fromkeys(titles))}."
    node, title, path = folders[hits[0]]
    items, subs = [], []
    for child in node.get("Children") or []:
        if child.get("WebBookmarkType") == "WebBookmarkTypeList":
            subs.append(_folder_title(child))
        else:
            name, url = _leaf(child)
            host = urllib.parse.urlparse(url).netloc.removeprefix("www.")
            items.append(f"{name or host}" + (f" ({host})" if name and host and host.lower() not in name.lower() else ""))
    if not items and not subs:
        return f"The '{title}' folder is empty."
    text = f"'{path}' has {len(items)} bookmark{'s' if len(items) != 1 else ''}"
    text += (": " + "; ".join(items[:20]) + (" …" if len(items) > 20 else "")) if items else ""
    return text + (f". Folders in it: {', '.join(subs)}." if subs else ".")


def safari_current() -> tuple[str, str, str]:
    """(title, url, problem) of Safari's front tab."""
    if not _running("com.apple.Safari"):
        return "", "", "Safari isn't open."
    ok, out = _osa(f'tell application "Safari"\nif (count of windows) is 0 then return "NONE"\n'
                   f'set t to current tab of front window\nreturn (name of t) & "{F}" & (URL of t)\nend tell')
    if not ok:
        return "", "", _failed(out, "Safari")
    if out == "NONE":
        return "", "", "Safari has no window open."
    title, _, url = out.partition(F)
    return title, url, ""


def safari_tool(args: dict) -> str:
    action = str(args.get("action") or "current").lower()
    if action == "current":
        title, url, problem = safari_current()
        return problem or f"Safari is on '{title}': {url}"
    if action == "bookmarks":
        return safari_bookmarks(str(args.get("folder") or ""))
    if action == "reading_list":
        url, title = str(args.get("url") or "").strip(), str(args.get("title") or "").strip()
        if not url:
            title, url, problem = safari_current()
            if problem:
                return problem + " Give the page's address to add it."
        if not re.match(r"^https?://", url):
            return "FAILED: the Reading List takes web addresses (http or https)."
        ok, out = _osa(f'tell application "Safari" to add reading list item {_q(url)}'
                       + (f" with title {_q(title)}" if title else ""))
        if not ok:
            return _failed(out, "Safari")
        undo.record("safari", f"adding '{title or url}' to Safari's Reading List", None,
                    "Safari can't remove Reading List items by script; right-click it in the Reading List > Remove Item.")
        return f"Added '{title or url}' to Safari's Reading List."
    return "Unknown safari action. Use current, reading_list or bookmarks."


# ==================================================================================================
# Photos
# ==================================================================================================

def _photo_range(start: str = "", end: str = "", when: str = "") -> tuple[dt.datetime | None, dt.datetime | None, str]:
    """(from, to, label). Accepts ISO dates or everyday words."""
    today = dt.date.today()
    day = dt.timedelta(days=1)
    words = str(when or "").strip().lower()
    monday = today - dt.timedelta(days=today.weekday())
    spans = {"today": (today, today + day), "yesterday": (today - day, today),
             "this week": (monday, today + day), "last week": (monday - 7 * day, monday),
             "this weekend": (monday + 5 * day, monday + 7 * day),
             "last weekend": (monday - 2 * day, monday),
             "this month": (today.replace(day=1), today + day),
             "last month": ((today.replace(day=1) - day).replace(day=1), today.replace(day=1)),
             "this year": (today.replace(month=1, day=1), today + day),
             "last year": (today.replace(year=today.year - 1, month=1, day=1), today.replace(month=1, day=1))}
    if words in spans:
        a, b = spans[words]
        return dt.datetime.combine(a, dt.time.min), dt.datetime.combine(b, dt.time.min), words
    a, b = _iso(start), _iso(end)
    if a is None and b is None:
        return None, None, ""
    if a is not None and len(str(start).strip()) <= 10:
        a = dt.datetime.combine(a.date(), dt.time.min)
    if b is not None and len(str(end).strip()) <= 10:
        b = dt.datetime.combine(b.date(), dt.time.min) + day            # an end day counts whole
    a = a or dt.datetime(1970, 1, 2)
    b = b or dt.datetime.combine(today, dt.time.min) + day
    last = b - dt.timedelta(seconds=1)
    label = f"{a:%-d %b}" + (f" to {last:%-d %b}" if last.date() != a.date() else "")
    return a, b, label


def _photos_selection(album: str, a, b) -> tuple[str, str, str]:
    """(AppleScript lines setting the dates d1/d2 - outside any tell -, the line setting `found` - inside
    `tell application "Photos"` -, problem)."""
    dates = (_as_date("d1", a) + _as_date("d2", b)) if a is not None else ""
    within = " whose date ≥ d1 and date < d2" if a is not None else ""
    if album:
        ok, out = _osa('tell application "Photos"\nset AppleScript\'s text item delimiters to "' + F + '"\n'
                       'set t to (name of albums) as text\nset AppleScript\'s text item delimiters to ""\nreturn t\nend tell',
                       timeout=60)
        if not ok:
            return "", "", _failed(out, "Photos")
        names = [n for n in out.split(F) if n]
        hits, _ = _candidates(names, album, "album")
        if not hits:
            return "", "", (f"Photos has no album called '{album}'."
                            + (f" Albums: {', '.join(names[:12])}." if names else ""))
        return dates, f"set found to (media items of album {_q(names[hits[0]])}{within})\n", ""
    if a is None:
        return "", "", "Which photos? Give a date range (start/end) or an album."
    return dates, f"set found to (media items{within})\n", ""


def photos_tool(args: dict) -> str:
    action = str(args.get("action") or "find").lower()
    album = str(args.get("album") or "").strip()
    a, b, label = _photo_range(str(args.get("start") or ""), str(args.get("end") or ""), str(args.get("when") or ""))
    label = (f"'{album}', {label}" if label else f"the album '{album}'") if album else label
    dates, found, problem = _photos_selection(album, a, b)
    if problem:
        return problem
    if action in ("find", "count"):
        ok, out = _osa(f'{dates}tell application "Photos"\n{found}return (count of found) as text\nend tell',
                       timeout=90)
        if not ok:
            return _failed(out, "Photos")
        n = int(out) if out.strip().isdigit() else 0
        return f"{n} photos and videos from {label}." if n else f"No photos from {label}."
    if action == "show":
        target = f"album {_q(album)}" if album and a is None else "item 1 of found"
        ok, out = _osa(f'{dates}tell application "Photos"\n{found}if (count of found) is 0 then return "0"\n'
                       f'activate\nspotlight {target}\nreturn (count of found) as text\nend tell', timeout=90)
        if not ok:
            return _failed(out, "Photos")
        n = int(out) if out.strip().isdigit() else 0
        return f"Showing {label} in Photos ({n} items)." if n else f"No photos from {label}."
    if action == "export":
        count = max(1, min(int(args.get("count") or 10), 200))
        from mint.core import config
        from mint.tools import harness as harness_tools
        from mint.tools import saveto
        safe = re.sub(r"[/:]", "-", label).replace("'", "").strip() or "photos"
        folder = saveto._free(config.storage("Images") / f"Photos - {safe}")
        wanted = str(args.get("save_to") or "").strip()
        if wanted:
            base = Path(os.path.expanduser(wanted)).resolve()
            why = harness_tools._blocked(base, write=True)
            if why:
                return f"NOT EXPORTED: {why}"
            folder = saveto._free(base / f"Photos - {safe}") if base.exists() else base
        folder.mkdir(parents=True, exist_ok=True)           # always a new folder: undo trashes it whole
        ok, out = _osa(f"""{dates}
tell application "Photos"
    {found}    set n to count of found
    if n is 0 then return "0"
    if n > {count} then set found to items 1 thru {count} of found
    export found to (POSIX file {_q(str(folder))} as alias)
    return (count of found) as text
end tell""", timeout=300)
        files = [p for p in folder.iterdir() if not p.name.startswith(".")]
        if not files:
            if not any(folder.iterdir()):
                folder.rmdir()
            if not ok:
                return _failed(out, "Photos")
            return f"No photos from {label}." if out.strip() == "0" else "FAILED: Photos exported nothing."
        undo.record("photos", f"exporting {len(files)} photos to {folder.name}",
                    {"kind": "file_trash", "path": str(folder), "mtime": folder.stat().st_mtime})
        return f"Exported {len(files)} from {label} to {undo._short(str(folder))}."
    return "Unknown photos action. Use find, show or export."


# ==================================================================================================
# iWork: Pages, Keynote, Numbers
# ==================================================================================================

_IWORK = {"pages": ("Pages", "com.apple.iWork.Pages", ".pages"),
          "keynote": ("Keynote", "com.apple.iWork.Keynote", ".key"),
          "numbers": ("Numbers", "com.apple.iWork.Numbers", ".numbers")}


def _slides(content: str) -> list[tuple[str, list[str]]]:
    """Slides from text: separated by '---' lines or blank lines; first line the title, the rest bullets."""
    blocks = re.split(r"\n\s*(?:---+|===+)\s*\n|\n\s*\n", str(content or "").replace("\r\n", "\n").strip())
    slides = []
    for block in blocks:
        lines = [re.sub(r"^\s*(?:[-*•–]|\d+[.)])\s+", "", line).strip() for line in block.split("\n") if line.strip()]
        if lines:
            slides.append((lines[0].lstrip("# ").strip(), lines[1:]))
    return slides


def _table(content: str) -> list[list[str]]:
    """Rows from CSV, tab-separated or a Markdown table."""
    import csv
    import io
    text = str(content or "").replace("\r\n", "\n").strip()
    lines = [line for line in text.split("\n") if line.strip() and not re.match(r"^\s*\|?\s*:?-{2,}", line)]
    if lines and all("|" in line for line in lines):
        return [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    sep = "\t" if "\t" in text else ";" if text.count(";") > text.count(",") else ","
    return [row for row in csv.reader(io.StringIO("\n".join(lines)), delimiter=sep)]


def _cell(value: str) -> str:
    text = str(value).strip()
    if re.fullmatch(r"-?(?:0|[1-9]\d{0,14})(?:\.\d+)?", text):
        return text
    return _q(text)


def iwork_tool(args: dict) -> str:
    app_key = str(args.get("app") or "pages").lower()
    if app_key not in _IWORK:
        return "Which app: pages, keynote or numbers?"
    app, bundle, suffix = _IWORK[app_key]
    if _installed(bundle) is None:
        return f"{app} isn't installed on this Mac. It's free on the App Store; or ask for a Word/Excel file instead."
    title = " ".join(str(args.get("title") or "").split()) or {"pages": "Document", "keynote": "Presentation",
                                                                  "numbers": "Spreadsheet"}[app_key]
    content = str(args.get("content") or "")
    from mint.core import config
    from mint.tools import saveto
    safe = re.sub(r"[/:\\]", "-", title)[:80].strip() or "Untitled"
    target, moved = saveto.destination(config.storage("Documents") / f"{safe}{suffix}", suffix,
                                       str(args.get("save_to") or ""))
    if target.exists():
        target = saveto._free(target)
    show = args.get("open") is not False
    ending = f'save d in POSIX file {_q(str(target))}\n' + ("activate\n" if show else "close d saving no\n")

    if app_key == "pages":
        body = (title + "\n\n" + content.strip()) if content.strip() else title
        script = f"""
tell application "Pages"
    set d to make new document
    set body text of d to {_q(body)}
    try
        set size of paragraph 1 of body text of d to 24
        set font of paragraph 1 of body text of d to "Helvetica Neue Bold"
    end try
    {ending}    return "ok"
end tell"""
    elif app_key == "keynote":
        slides = _slides(content)
        subtitle = str(args.get("subtitle") or "")
        items = ", ".join("{" + _q(head) + ", " + _q("\n".join(bullets)) + "}" for head, bullets in slides[:60])
        script = f"""
tell application "Keynote"
    set d to make new document
    tell d
        try
            set object text of default title item of slide 1 to {_q(title)}
        end try
        try
            set object text of default body item of slide 1 to {_q(subtitle)}
        end try
        set bulletMaster to missing value
        repeat with m in master slides
            if (name of m) contains "Bullets" and (name of m) contains "Title" then
                set bulletMaster to m
                exit repeat
            end if
        end repeat
        repeat with s in {{{items}}}
            if bulletMaster is missing value then
                set x to make new slide
            else
                set x to make new slide with properties {{base slide:bulletMaster}}
            end if
            try
                set object text of default title item of x to item 1 of s
            end try
            try
                set object text of default body item of x to item 2 of s
            end try
        end repeat
    end tell
    {ending}    return "ok"
end tell"""
    else:
        rows = _table(content)[:200]
        if not rows:
            return "Give the sheet's rows (CSV, tab-separated or a table)."
        width = min(max(len(r) for r in rows), 26)
        data = ", ".join("{" + ", ".join(_cell(c) for c in (r + [""] * width)[:width]) + "}" for r in rows)
        script = f"""
tell application "Numbers"
    set d to make new document
    tell table 1 of sheet 1 of d
        set row count to {max(len(rows), 2)}
        set column count to {max(width, 1)}
        set header column count to 0
        set r to 0
        repeat with rowData in {{{data}}}
            set r to r + 1
            set c to 0
            repeat with v in rowData
                set c to c + 1
                set value of cell c of row r to (contents of v)
            end repeat
        end repeat
    end tell
    try
        set name of sheet 1 of d to {_q(title[:30])}
    end try
    {ending}    return "ok"
end tell"""
    ok, out = _osa(script, timeout=120)
    if not ok:
        return _failed(out, app)
    for _ in range(20):
        if target.exists():
            break
        time.sleep(0.25)
    if not target.exists():
        return f"FAILED: {app} made the {app_key} document but it wasn't saved at {undo._short(str(target))}."
    undo.record("iwork", f"making the {app} file '{target.name}'",
                {"kind": "file_trash", "path": str(target), "mtime": target.stat().st_mtime})
    what = {"pages": "document", "keynote": f"presentation ({len(_slides(content)) + 1} slides)",
            "numbers": "spreadsheet"}[app_key]
    return (f"Made the {app} {what} '{target.stem}', saved at {undo._short(str(target))}"
            + (" and open." if show else ".") + (f" {moved}" if moved else ""))


# ==================================================================================================
# The tools
# ==================================================================================================

def contacts_tool(args: dict) -> str:
    return contacts_lookup(str(args.get("name") or ""), str(args.get("field") or "all"))


PROMPT = """Apple's apps, beyond making new things (short spoken answers; never send or share anything):
- Notes: "update my grocery note: add bread" / "put eggs on my shopping list note" -> notes action=append (one line per \
item; a list note gets new list items). "what's in my packing list note" -> action=read. "change 'milk' to 'oat milk' in \
groceries" -> action=replace find=milk text="oat milk". "find my note about the wifi password" -> action=search. Also \
prepend, rename (text=new title), move (folder), folders, recent ("my last note"). Use these to CHANGE a note - never \
create_note for a note that exists (create_note only for a new one; if append says there is no such note, ask or \
create it). delete only when the user asked; it goes to Recently Deleted. Every change can be undone.
- Reminders: "what's due today" / "what's overdue" -> reminders_manage action=list due=today|overdue; "what's on my \
Groceries list" -> action=list list=Groceries. "I did X" / "mark X done" -> complete (uncomplete to reopen); "move the \
dentist reminder to Friday 9" -> reschedule when=ISO (a date alone keeps it all-day); "put X on my Work list" -> \
move to_list; rename; delete only when asked. New reminders still use create_reminder.
- Calendar: "move my 3pm with Sam to 4" / "push the standup to tomorrow" -> calendar_manage action=move event=<title> \
day=<its current day> start=<new ISO start> (a date alone keeps the time; the length stays). Also rename, update \
(location / notes), find ("when is my dentist appointment"), delete only when asked (undo puts it back). New events \
still use create_event; the day's list still calendar_events.
- Contacts: "what's Sam's email", "Priya's number", "when is Priya's birthday" -> contacts name=<name> field=email|phone|\
birthday|address|company|all. Read only. Say just what was asked; don't read out other details.
- Maps: "directions to the airport", "how do I get to X by train" -> maps action=directions to=... (from= a named start; \
leave it out for here) mode=driving|walking|transit|cycling. "how long to drive from A to B" -> action=eta (needs a \
named start). action=show opens a place.
- Safari: "add this page to my reading list" -> safari action=reading_list (the front tab unless url given); "what \
page is Safari on" -> action=current; "what's in my Recipes bookmarks folder" -> action=bookmarks folder=Recipes.
- Photos: "how many photos did I take last weekend", "show my photos from last weekend", "photos in my Japan album" \
-> photos action=find|show with when=today|yesterday|this week|last week|this weekend|last weekend|this month|last \
month|this year|last year, or start/end ISO dates, and/or album. "export 10 of them to my Desktop" -> action=export \
count=10 save_to=~/Desktop.
- Pages / Keynote / Numbers: "make a Keynote about X" / "put this in a Pages document" / "a Numbers sheet of these \
prices" -> iwork app=pages|keynote|numbers title=... content=... (you write the content: Pages plain paragraphs; \
Keynote slides separated by blank lines, first line the slide title, then one bullet per line; Numbers rows as CSV \
with a header row). It saves in Mint's folder (or save_to) and opens it."""


def declarations():
    from google.genai import types
    S, I, B = types.Type.STRING, types.Type.INTEGER, types.Type.BOOLEAN

    def fn(name, description, props, required):
        return types.FunctionDeclaration(name=name, description=description, parameters=types.Schema(
            type=types.Type.OBJECT, properties={k: types.Schema(**v) for k, v in props.items()}, required=required))

    return [
        fn("notes",
           "Work with notes that already exist in Apple Notes: search (words in title or text, with snippets), read "
           "(full text), append / prepend lines, replace text inside, rename, move to a folder, list folders, recent "
           "(last edited), delete (only when the user asked; to Recently Deleted). Changes can be undone.",
           {"action": {"type": S, "enum": ["search", "read", "append", "prepend", "replace", "rename", "move",
                                           "folders", "recent", "delete"]},
            "note": {"type": S, "description": "The note's title or words from it ('my grocery note' -> 'grocery')."},
            "text": {"type": S, "description": "append/prepend: the lines to add, one item per line; replace: the new "
                                               "text; rename: the new title."},
            "find": {"type": S, "description": "replace: the text to change."},
            "all": {"type": B, "description": "replace: every place it appears (default true)."},
            "folder": {"type": S, "description": "The folder the note is in (to narrow it down), or move: where to."},
            "create_folder": {"type": B, "description": "move: make the folder if missing (only when asked)."},
            "query": {"type": S, "description": "search: the words to look for."},
            "count": {"type": I, "description": "recent/search: how many (default 5/8)."}},
           ["action"]),
        fn("reminders_manage",
           "Manage existing reminders (Reminders app): list (a list's items, or due today / overdue across lists), "
           "complete, uncomplete, reschedule, move to another list, rename, delete (only when the user asked). "
           "Changes can be undone. Making a NEW reminder is create_reminder.",
           {"action": {"type": S, "enum": ["list", "complete", "uncomplete", "reschedule", "move", "rename", "delete"]},
            "reminder": {"type": S, "description": "The reminder's title (or words from it)."},
            "list": {"type": S, "description": "The Reminders list it is in / to list."},
            "due": {"type": S, "enum": ["today", "overdue", "all"], "description": "list: which ones."},
            "when": {"type": S, "description": "reschedule: new local ISO time (2026-10-02T09:00), a date alone "
                                               "(2026-10-02), or 'none' to clear."},
            "in_minutes": {"type": I, "description": "reschedule: due this many minutes from now."},
            "to_list": {"type": S, "description": "move: the list to move it to."},
            "new_title": {"type": S, "description": "rename: the new title."}},
           ["action"]),
        fn("contacts",
           "Look someone up in the user's Contacts: phones, emails, birthday, company, address. Read only; never "
           "share it anywhere.",
           {"name": {"type": S, "description": "The person's name as the user said it."},
            "field": {"type": S, "enum": ["all", "phone", "email", "birthday", "address", "company"]}},
           ["name"]),
        fn("calendar_manage",
           "Change an existing Calendar event, found by title and day: find, move (new start; length kept), rename, "
           "update (location, notes), delete (only when the user asked; undo recreates it). New events: create_event.",
           {"action": {"type": S, "enum": ["find", "move", "rename", "update", "delete"]},
            "event": {"type": S, "description": "The event's title (or words from it)."},
            "day": {"type": S, "description": "The day it is on NOW: ISO date, today, tomorrow or a weekday. Leave "
                                              "out to look through the next weeks."},
            "start": {"type": S, "description": "move: new local ISO start (2026-10-02T16:00), or a date alone to "
                                                "keep the time."},
            "end": {"type": S, "description": "move: new end (default: same length)."},
            "new_title": {"type": S},
            "location": {"type": S, "description": "update: the new place ('' clears it)."},
            "notes": {"type": S, "description": "update: the new notes ('' clears them)."}},
           ["action", "event"]),
        fn("maps",
           "Apple Maps: directions (opens Maps with the route, and says the travel time when a start is named), eta "
           "(just the travel time), show (a place).",
           {"action": {"type": S, "enum": ["directions", "eta", "show"]},
            "to": {"type": S, "description": "Destination (an address or place name)."},
            "from": {"type": S, "description": "Start; leave out for the current location."},
            "mode": {"type": S, "enum": ["driving", "walking", "transit", "cycling"]}},
           ["action"]),
        fn("safari",
           "Safari: current (the front tab's title and address), reading_list (add a page to the Reading List - the "
           "front tab unless url is given), bookmarks (list a bookmarks folder; read only).",
           {"action": {"type": S, "enum": ["current", "reading_list", "bookmarks"]},
            "url": {"type": S}, "title": {"type": S},
            "folder": {"type": S, "description": "bookmarks: the folder, e.g. 'Favorites' or 'Recipes'."}},
           ["action"]),
        fn("photos",
           "Apple Photos (the library is never changed): find (count photos and videos from a date range and/or "
           "album), show (open Photos there), export (copy N of them to a folder).",
           {"action": {"type": S, "enum": ["find", "show", "export"]},
            "when": {"type": S, "enum": ["today", "yesterday", "this week", "last week", "this weekend",
                                         "last weekend", "this month", "last month", "this year", "last year"]},
            "start": {"type": S, "description": "ISO date or time the range starts."},
            "end": {"type": S, "description": "ISO date the range ends (that day included)."},
            "album": {"type": S},
            "count": {"type": I, "description": "export: how many (default 10)."},
            "save_to": {"type": S, "description": "export: the folder (default Mint's Images folder)."}},
           ["action"]),
        fn("iwork",
           "Make a Pages document, Keynote presentation or Numbers spreadsheet from content you write; saves it in "
           "Mint's folder (or save_to) and opens it. Says so if the app isn't installed.",
           {"app": {"type": S, "enum": ["pages", "keynote", "numbers"]},
            "title": {"type": S},
            "content": {"type": S, "description": "Pages: the text. Keynote: slides separated by blank lines, first "
                                                  "line the slide title, then one bullet per line. Numbers: rows as "
                                                  "CSV with a header row."},
            "subtitle": {"type": S, "description": "Keynote: the title slide's subtitle."},
            "save_to": {"type": S, "description": "A folder or full path, if the user named one."}},
           ["app", "content"]),
    ]


HANDLERS = {"notes": notes_tool, "reminders_manage": reminders_tool, "contacts": contacts_tool,
            "calendar_manage": calendar_tool, "maps": maps_tool, "safari": safari_tool, "photos": photos_tool,
            "iwork": iwork_tool}

# For activity.py (the island's icon kinds, app icons and labels while a tool runs).
KINDS = {"notes": "write", "reminders_manage": "calendar", "contacts": "read", "calendar_manage": "calendar",
         "maps": "web", "safari": "web", "photos": "look", "iwork": "file"}
BUNDLES = {"notes": "com.apple.Notes", "reminders_manage": "com.apple.reminders", "contacts": "com.apple.AddressBook",
           "calendar_manage": "com.apple.iCal", "maps": "com.apple.Maps", "safari": "com.apple.Safari",
           "photos": "com.apple.Photos"}


def label(name: str, a: dict) -> str:
    """What the island says while one of these runs."""
    action = str(a.get("action") or "")
    if name == "notes":
        return {"search": f"Searching notes · {a.get('query') or ''}", "folders": "Checking Notes folders",
                "recent": "Checking recent notes"}.get(action, f"Notes · {action} {a.get('note') or ''}").strip(" ·")
    if name == "reminders_manage":
        return f"Reminders · {action} {a.get('reminder') or a.get('list') or a.get('due') or ''}".strip(" ·")
    if name == "calendar_manage":
        return f"Calendar · {action} {a.get('event') or ''}".strip(" ·")
    if name == "contacts":
        return f"Looking up {a.get('name') or 'a contact'}"
    if name == "maps":
        return f"Maps · {a.get('to') or ''}".strip(" ·")
    if name == "safari":
        return {"reading_list": "Adding to the Reading List", "bookmarks": "Reading bookmarks"}.get(action, "Safari")
    if name == "photos":
        return f"Photos · {a.get('when') or a.get('album') or action}"
    if name == "iwork":
        return f"Making a {str(a.get('app') or 'Pages').capitalize()} file · {a.get('title') or ''}".strip(" ·")
    return name
