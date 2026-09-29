"""Undo across Mint: "undo that", "undo the last 3 things", "what can you undo?", "redo".

Whatever Mint changes, the module that changed it calls record() right after, with a plain
summary and the way back as data: a kind and its arguments, e.g. {"kind": "brightness",
"value": 40}. Data rather than a closure, so the journal can be a file - undo.jsonl in
Application Support/Mint, the last 50 entries, each kept for a day - and still works after
a restart. The kinds are the functions registered below with @inverse.

Undoing runs an inverse, and the modules' own hooks fire again while it does (setting the
brightness back records that change). Those records are caught instead of journalled and
become the entry's redo, so "redo" works for anything whose way back goes through a hooked
function; an inverse can also hand back its redo itself.

Things with no way back - a message sent with Return, an email draft - are recorded with
inverse=None and the reason, so "undo that" says so plainly rather than quietly undoing
something older instead.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("mint.tools.undo")

JOURNAL = Path.home() / "Library" / "Application Support" / "Mint" / "undo.jsonl"
KEEP = 50
DAY = 24 * 3600

_lock = threading.RLock()
_local = threading.local()          # per thread: records being caught (a batch, or an undo running)
_INVERSES: dict = {}
_FAILED = ("FAILED", "REFUSED", "NOT DONE", "CAN'T", "Can't")


def inverse(kind: str):
    """Register the function that runs inverses of this kind: spec -> sentence, or (sentence, redo spec)."""
    def register(fn):
        _INVERSES[kind] = fn
        return fn
    return register


# --- the journal ---------------------------------------------------------------------------------

def _files() -> Path:
    """Where copies needed for an undo live (a clipboard picture); a day old = gone."""
    folder = JOURNAL.parent / "undo"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def _load() -> list[dict]:
    try:
        lines = JOURNAL.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    cutoff = time.time() - DAY
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("at", 0) >= cutoff:
            rows.append(row)
    return rows[-KEEP:]


def _save(rows: list[dict]) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOURNAL.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows[-KEEP:]), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(JOURNAL)
    folder = JOURNAL.parent / "undo"
    if folder.is_dir():
        for old in folder.iterdir():
            try:
                if time.time() - old.stat().st_mtime > DAY:
                    old.unlink()
            except OSError:
                pass


def _update(entry_id: str, **changes) -> None:
    """Change one entry in place, re-reading the file first: another thread may have added to it."""
    with _lock:
        rows = _load()
        for row in rows:
            if row.get("id") == entry_id:
                row.update(changes)
        _save(rows)


def record(kind: str, summary: str, inverse: dict | None = None, why: str = "") -> None:
    """Note something Mint just did. `inverse` puts it back; None = it can't be undone, and `why` says
    why. Never raises: a journal problem must not break the action itself."""
    try:
        caught = getattr(_local, "caught", None)
        if caught is not None:
            caught.append({"kind": kind, "summary": summary, "inverse": inverse, "why": why})
            return
        json.dumps(inverse)
        entry = {"id": uuid.uuid4().hex[:8], "at": time.time(), "kind": kind, "summary": summary,
                 "inverse": inverse, "why": why, "undone": False}
        with _lock:
            rows = _load()
            rows.append(entry)
            _save(rows)
        log.info("undo: recorded %s: %s", kind, summary)
    except Exception as error:
        log.info("undo: could not record %s: %s", kind, error)


@contextmanager
def together(kind: str, summary: str):
    """Everything recorded inside is one entry: "Chrome left, Slack right" is undone in one go."""
    if getattr(_local, "caught", None) is not None:
        yield                           # already inside one (or an undo): the outer one keeps the steps
        return
    _local.caught = []
    try:
        yield
    finally:
        steps, _local.caught = _local.caught, None
        backs = [s["inverse"] for s in steps if s["inverse"]]
        if backs:
            record(kind, summary, backs[0] if len(backs) == 1 else {"kind": "steps", "steps": backs})


def replaying() -> bool:
    """True while an inverse runs: a module's own undo then leaves the journal to us."""
    return bool(getattr(_local, "replaying", False))


def settled(kind: str) -> None:
    """A module undid its own last action (edit_selection action=undo, tidy action=undo): mark the
    newest entry of that kind undone, so "undo" doesn't try it a second time."""
    if replaying():
        return
    try:
        with _lock:
            rows = _load()
            for row in reversed(rows):
                if row.get("kind") == kind and not row.get("undone"):
                    row.update(undone=True, undone_at=time.time(), redo=None)
                    break
            _save(rows)
    except Exception as error:
        log.info("undo: settled %s: %s", kind, error)


def _run(spec: dict) -> tuple[bool, str, dict | None]:
    """Run one inverse -> (worked, what it said, the redo)."""
    fn = _INVERSES.get(str((spec or {}).get("kind")))
    if fn is None:
        return False, f"FAILED: Mint doesn't know how to undo '{(spec or {}).get('kind')}'.", None
    outer, was = getattr(_local, "caught", None), replaying()
    _local.caught, _local.replaying = [], True
    try:
        said = fn(dict(spec))
    except Exception as error:
        log.exception("undo %s failed", spec.get("kind"))
        said = f"FAILED: {error}"
    finally:
        caught = _local.caught
        _local.caught, _local.replaying = outer, was
    redo = None
    if isinstance(said, tuple):
        said, redo = said
    said = str(said)
    if redo is None:
        backs = [c["inverse"] for c in caught if c["inverse"]]
        redo = backs[0] if len(backs) == 1 else {"kind": "steps", "steps": backs} if backs else None
    return not said.startswith(_FAILED), said, redo


def _ago(at: float) -> str:
    age = int(time.time() - at)
    return "just now" if age < 60 else f"{age // 60} min ago" if age < 3600 else f"{age // 3600} h ago"


def _matches(row: dict, words: str) -> bool:
    text = f"{row.get('kind', '')} {row.get('summary', '')}".lower()
    wanted = [w for w in words.lower().replace("'", " ").split() if len(w) > 2 and w not in
              {"the", "that", "change", "last", "undo", "you", "made", "did", "what", "thing"}]
    return all(w.rstrip("s") in text for w in wanted) if wanted else True


def undo_last(count: int = 1, match: str = "") -> str:
    count = max(1, min(int(count or 1), 10))
    with _lock:
        rows = _load()
    live = [r for r in reversed(rows) if not r.get("undone")]
    if match:
        live = [r for r in live if _matches(r, match)]
        if not live:
            return (f"Nothing like '{match}' in what Mint did in the last 24 hours that can be undone. Sent "
                    "messages and emails, and things done on web pages, can't be taken back by Mint.")
    if not live:
        return ("Nothing to undo: Mint hasn't changed anything in the last 24 hours that it can put back. Sent "
                "messages and emails, and things done on web pages, can't be taken back by Mint.")
    done, lines = 0, []
    for row in live[:count]:
        if row.get("inverse") is None:
            why = row.get("why") or "there is no way back for that."
            # Passed over from now on, so the next "undo" reaches the thing before it.
            _update(row["id"], undone=True, undone_at=time.time(), redo=None, skipped=True)
            after = next((r for r in live if r is not row and r["at"] < row["at"] and r.get("inverse")), None)
            lines.append(f"Can't undo {row['summary']}: {why}"
                         + (f" Say 'undo' again to undo the thing before it ({after['summary']})." if after else ""))
            break
        ok, said, redo = _run(row["inverse"])
        if not ok:
            lines.append(f"Couldn't undo {row['summary']}: {said.split(':', 1)[-1].strip()}")
            break
        _update(row["id"], undone=True, undone_at=time.time(), redo=redo)
        done += 1
        lines.append(f"Undid {row['summary']}: {said}")
    if count > 1 and done == len(live) < count:
        lines.append(f"That was all {done} - nothing older to undo.")
    return "\n".join(lines)


def redo() -> str:
    with _lock:
        rows = _load()
    undone = [r for r in rows if r.get("undone") and not r.get("skipped")]
    if not undone:
        return "Nothing to redo: nothing has been undone in the last 24 hours."
    row = max(undone, key=lambda r: r.get("undone_at", 0))
    if not row.get("redo"):
        return f"Can't redo {row['summary']} by itself; ask for it again instead."
    ok, said, again = _run(row["redo"])
    if not ok:
        return f"Couldn't redo {row['summary']}: {said.split(':', 1)[-1].strip()}"
    with _lock:
        rows = [r for r in _load() if r.get("id") != row["id"]]
        row.update(undone=False, redo=None, at=time.time(), inverse=again or row["inverse"])
        row.pop("undone_at", None)
        rows.append(row)
        _save(rows)
    return f"Redid {row['summary']}: {said}"


def listing() -> str:
    with _lock:
        rows = _load()
    if not rows:
        return "Nothing Mint did in the last 24 hours can be undone yet."
    lines, n = [], 0
    for row in reversed(rows):
        if row.get("undone"):
            continue
        n += 1
        tail = "" if row.get("inverse") else f" (can't be undone: {row.get('why') or 'no way back'})"
        lines.append(f"{n}. {row['summary']} - {_ago(row['at'])}{tail}")
        if n >= 15:
            break
    redoable = [r for r in rows if r.get("undone") and r.get("redo") and not r.get("skipped")]
    text = ("What Mint can undo, newest first:\n" + "\n".join(lines)) if lines else "Nothing left to undo."
    if redoable:
        last = max(redoable, key=lambda r: r.get("undone_at", 0))
        text += f"\nCan redo: {last['summary']}."
    return text


# --- what makes an inverse: small helpers the hooks call ---------------------------------------

def front() -> tuple[str, str]:
    """(app, window title) in front."""
    try:
        import AppKit
        from mint.screen import axkit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        window = axkit.focused_window(app.processIdentifier()) if app is not None else None
        title = str(axkit.attr(window, "AXTitle") or "") if window is not None else ""
        return (str(app.localizedName()) if app is not None else ""), title
    except Exception:
        return "", ""


def typed(text: str, press_return: bool = False, pid: int | None = None) -> None:
    """Text Mint pasted at the cursor: the app's own ⌘Z takes a paste back in one step."""
    app, title = front()
    if pid:
        try:
            import AppKit
            running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            app, title = (str(running.localizedName()), "") if running is not None else (app, title)
        except Exception:
            pass
    words = " ".join(str(text).split())
    what = f"typing \"{words[:40]}{'…' if len(words) > 40 else ''}\" in {app or 'the front app'}"
    if press_return:
        record("typed", what + " and pressing Return", None,
               "Return may have sent or submitted it, and a sent message can't be taken back by Mint.")
        return
    record("typed", what, {"kind": "app_keys", "app": app, "title": title, "key": "z"})


def clip_image(png: bytes) -> str:
    """Keep a clipboard picture for a day, for putting it back."""
    import hashlib
    path = _files() / f"clip-{hashlib.sha1(png).hexdigest()[:16]}.png"
    if not path.exists():
        path.write_bytes(png)
        os.chmod(path, 0o600)
    return str(path)


# --- inverses --------------------------------------------------------------------------------------

@inverse("steps")
def _steps(spec: dict):
    said, redos, failed = [], [], []
    for step in reversed(spec.get("steps") or []):
        ok, text, redo = _run(step)
        (said if ok else failed).append(text)
        if redo:
            redos.append(redo)
    if failed and not said:
        return failed[0]
    text = " ".join(said) + (f" (not all of it: {' '.join(failed)})" if failed else "")
    return text, ({"kind": "steps", "steps": redos} if redos else None)


@inverse("brightness")
def _brightness(spec: dict) -> str:
    from mint.tools import mac as macctl
    said = macctl.brightness(str(int(spec["value"])))
    return said if said.startswith("FAILED") else f"brightness back to {int(spec['value'])}%."


@inverse("volume")
def _volume(spec: dict) -> str:
    from mint.tools import fastinput
    fastinput.set_volume(int(spec["level"]))
    return f"volume back to {int(spec['level'])}."


@inverse("mute")
def _mute(spec: dict) -> str:
    from mint.tools import everyday as skills
    skills.system_action("mute" if spec.get("muted") else "unmute")
    return "sound muted again." if spec.get("muted") else "sound back on."


@inverse("dark_mode")
def _dark_mode(spec: dict) -> str:
    from mint.tools import everyday as skills
    said = skills.system_action("dark_mode_on" if spec.get("on") else "dark_mode_off")
    return said if said != "Done." else f"{'dark' if spec.get('on') else 'light'} mode again."


@inverse("focus")
def _focus(spec: dict) -> str:
    from mint.tools import mac as macctl
    said = macctl.focus(spec.get("state", "off"))
    return said if said.startswith(("FAILED", "NOT DONE")) else f"Do Not Disturb {spec.get('state', 'off')} again."


@inverse("night_shift")
def _night_shift(spec: dict) -> str:
    from mint.tools import mac as macctl
    return macctl.night_shift("on" if spec.get("on") else "off")


@inverse("keep_awake")
def _keep_awake(spec: dict) -> str:
    from mint.tools import mac as macctl
    if spec.get("state") == "off":
        macctl.keep_awake("off")
        return "the Mac can sleep normally again."
    return macctl.keep_awake("on", float(spec.get("minutes") or 0))


@inverse("wifi")
def _wifi(spec: dict) -> str:
    from mint.tools import mac as macctl
    return macctl.wifi("on" if spec.get("on") else "off")


@inverse("window_frame")
def _window_frame(spec: dict):
    from mint.tools import mac as macctl
    return macctl.put_window(spec)


@inverse("window_fullscreen")
def _window_fullscreen(spec: dict):
    from mint.tools import mac as macctl
    return macctl.set_fullscreen(spec.get("app", ""), bool(spec.get("on")))


@inverse("app_keys")
def _app_keys(spec: dict):
    """⌘Z (or ⌘⇧Z to redo) in the app it happened in - only if that window is still the one in front."""
    import AppKit
    from mint.tools import fastinput
    app, title = str(spec.get("app") or ""), str(spec.get("title") or "")
    running = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                    if str(a.localizedName() or "") == app), None) if app else None
    if running is None:
        return f"FAILED: {app or 'that app'} isn't open any more."
    if front()[0] != app:
        running.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        time.sleep(0.4)
    now_app, now_title = front()
    if now_app != app or (title and now_title and now_title != title):
        return (f"FAILED: the {app} window it went into ('{title}') isn't in front any more. Switch to it and "
                "press ⌘Z there.")
    redo = spec.get("redo")
    fastinput.press_key("z", ["command", "shift"] if redo else ["command"])
    back = dict(spec, redo=not redo)
    return (f"pressed {'⌘⇧Z' if redo else '⌘Z'} in {app}" + (f" ('{title}')" if title else "") + "."), back


@inverse("edit_selection")
def _edit_selection(spec: dict) -> str:
    from mint.tools import clipboard as clip_tools
    from mint.tools import rewrite
    rewrite._last.update(original=spec["original"], new=spec["new"], app=spec.get("app", ""), at=time.time())
    said = rewrite.undo()
    if said.startswith(("Put the original", "Undid the edit")):
        return said
    rewrite._last.clear()
    clip_tools._put_text(spec["original"])
    return (f"couldn't reach the rewritten text in {spec.get('app') or 'the app'} (it isn't selected there any "
            "more), so the original text is on the clipboard: select the new text and paste to put it back.")


@inverse("tidy")
def _tidy(spec: dict) -> str:
    from mint.tools import tidy
    return tidy.undo(str(spec.get("journal") or ""))


@inverse("reminder_delete")
def _reminder_delete(spec: dict) -> str:
    import EventKit
    from mint.tools import everyday as skills
    store, problem = skills._event_store(EventKit.EKEntityTypeReminder)
    if problem:
        return f"FAILED: {problem}"
    item = store.calendarItemWithIdentifier_(spec["id"])
    if item is None:
        return f"FAILED: the reminder '{spec.get('title', '')}' is already gone."
    ok, error = store.removeReminder_commit_error_(item, True, None)
    return f"deleted the reminder '{spec.get('title', '')}'." if ok else f"FAILED: {error}"


@inverse("event_delete")
def _event_delete(spec: dict) -> str:
    import EventKit
    from mint.tools import everyday as skills
    store, problem = skills._event_store(EventKit.EKEntityTypeEvent)
    if problem:
        return f"FAILED: {problem}"
    event = store.eventWithIdentifier_(spec["id"])
    if event is None:
        return f"FAILED: the event '{spec.get('title', '')}' is already gone."
    ok, error = store.removeEvent_span_commit_error_(event, EventKit.EKSpanThisEvent, True, None)
    return f"deleted the event '{spec.get('title', '')}'." if ok else f"FAILED: {error}"


@inverse("note_delete")
def _note_delete(spec: dict) -> str:
    """Notes' own delete moves a note to Recently Deleted, where it stays 30 days."""
    from mint.tools import everyday as skills
    ident = skills._as_string(str(spec["id"]))
    ok, out = skills._osascript(f"tell application \"Notes\"\nif exists note id {ident} then\ndelete note id "
                                f"{ident}\nreturn \"deleted\"\nend if\nend tell")
    if not ok:
        return f"FAILED: {skills._automation_hint(out, 'Notes')}"
    if out != "deleted":
        return f"FAILED: the note '{spec.get('title', '')}' is already gone."
    return f"moved the note '{spec.get('title', '')}' to Recently Deleted in Notes."


def _short(path: str) -> str:
    return path.replace(str(Path.home()), "~", 1)


def _changed(path: Path, mtime) -> bool:
    return mtime is not None and abs(path.stat().st_mtime - float(mtime)) > 1


@inverse("file_move")
def _file_move(spec: dict):
    """Put a moved or renamed file back. Never overwrites."""
    now, was = Path(spec["from"]), Path(spec["to"])
    if not now.exists():
        return f"FAILED: {_short(str(now))} isn't there any more."
    if was.exists():
        return f"FAILED: something new is at {_short(str(was))} now, and Mint won't overwrite it."
    was.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(now), str(was))
    return f"{_short(str(now))} is back at {_short(str(was))}.", {"kind": "file_move", "from": str(was), "to": str(now)}


def _trash(path: Path) -> tuple[bool, str]:
    import AppKit
    from Foundation import NSURL
    ok, url, error = AppKit.NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(path)), None, None)
    return bool(ok), (str(url.path()) if ok and url is not None else str(error))


@inverse("file_untrash")
def _file_untrash(spec: dict):
    trashed, home = Path(spec["trashed"]), Path(spec["to"])
    if not trashed.exists():
        return f"FAILED: {home.name} isn't in the Trash any more (was it emptied?)."
    if home.exists():
        return f"FAILED: something new is at {_short(str(home))}, and Mint won't overwrite it."
    home.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(trashed), str(home))
    return f"put {home.name} back from the Trash to {_short(str(home.parent))}.", {"kind": "file_trash", "path": str(home)}


@inverse("file_trash")
def _file_trash(spec: dict):
    """A file Mint made (created or copied): to the Trash, never deleted outright."""
    path = Path(spec["path"])
    if not path.exists():
        return f"FAILED: {_short(str(path))} is already gone."
    if _changed(path, spec.get("mtime")):
        return f"FAILED: {path.name} has been changed since Mint made it, so Mint left it alone."
    ok, where = _trash(path)
    if not ok:
        return f"FAILED: couldn't move it to the Trash: {where}"
    return f"moved {path.name} to the Trash.", {"kind": "file_untrash", "trashed": where, "to": str(path)}


@inverse("file_restore")
def _file_restore(spec: dict):
    """An overwritten or edited file: its saved previous version goes back (the current one is kept for redo)."""
    path, backup = Path(spec["path"]), Path(spec["backup"])
    if not backup.exists():
        return f"FAILED: the saved previous version of {path.name} is gone."
    if path.exists() and _changed(path, spec.get("mtime")):
        return f"FAILED: {path.name} has been changed again since, so Mint left it alone."
    keep = None
    if path.exists():
        keep = _files() / f"{uuid.uuid4().hex[:8]}-{path.name}"
        shutil.copy2(path, keep)
    shutil.copy2(backup, path)
    redo = {"kind": "file_restore", "path": str(path), "backup": str(keep), "mtime": path.stat().st_mtime} if keep else None
    return f"{path.name} is back to its previous version.", redo


@inverse("file_truncate")
def _file_truncate(spec: dict) -> str:
    """Text appended to a file: cut it back to its old length, if nothing else changed it since."""
    path = Path(spec["path"])
    if not path.exists():
        return f"FAILED: {_short(str(path))} is gone."
    if path.stat().st_size != int(spec["after"]):
        return f"FAILED: {path.name} has been changed since, so Mint left it alone."
    with open(path, "r+b") as handle:
        handle.truncate(int(spec["size"]))
    return f"took the added text back out of {path.name}."


@inverse("folder_remove")
def _folder_remove(spec: dict) -> str:
    path = Path(spec["path"])
    if not path.is_dir():
        return f"FAILED: {_short(str(path))} is already gone."
    if any(path.iterdir()):
        return f"FAILED: {path.name} has things in it now, so Mint left it."
    path.rmdir()
    return f"removed the empty folder {path.name}."


@inverse("clipboard")
def _clipboard(spec: dict):
    from mint.tools import clipboard as clip_tools
    now = clip_tools.undo_snapshot()
    said = clip_tools.undo_put(spec.get("was") or {"type": "empty"})
    return said, ({"kind": "clipboard", "was": now} if now else None)


# --- Mint's own settings, changed by voice -----------------------------------------------------------
# The session changes them itself (set_preference), so the old value is only known here: a
# settings listener keeps a copy, and after_preference() turns the change into an entry.

SETTINGS = {"spoken_replies": "voice", "microphone": "mic", "face": "face", "effects": "cursor_effects",
            "word_animation": "word_animation", "listen_while_working": "listen_while_working",
            "activity_timeline": "timeline", "instant_commands": "instant_commands",
            "storage_folder": "storage_folder", "screenshot_to": "screenshot_to", "theme": "theme",
            "position": "position"}
_seen: dict = {}
_changes: dict = {}


def _on_setting(key, value) -> None:
    if key in _seen:
        old, _seen[key] = _seen[key], value
        if old != value:
            _changes[key] = (old, value, time.time())


def watch_settings() -> None:
    try:
        from mint.core import prefs
        if _seen:
            return
        _seen.update({key: prefs.get(key) for key in SETTINGS.values()})
        prefs.on_change(_on_setting)
    except Exception as error:
        log.info("undo: can't watch settings: %s", error)


def after_preference(args: dict, result: str) -> None:
    """After set_preference: journal the change it made (the session calls it through autopilot.note)."""
    setting = str((args or {}).get("setting") or "")
    value = str((args or {}).get("value") or "").strip().lower()
    if setting == "activity_timeline" and value in {"clear", "delete", "forget", "erase"}:
        record("setting", "deleting the activity timeline", None, "the timeline was deleted, not set aside.")
        return
    key = SETTINGS.get(setting)
    change = _changes.pop(key, None) if key else None
    if not change or time.time() - change[2] > 30:
        return
    old, new, _ = change
    record("setting", f"Mint's {setting.replace('_', ' ')} setting ({old} → {new})",
           {"kind": "prefs", "values": {key: old}})


@inverse("prefs")
def _prefs(spec: dict):
    from mint.core import prefs
    values = spec.get("values") or {}
    now = {key: prefs.get(key) for key in values}
    for key, value in values.items():
        prefs.set(key, value)
        _changes.pop(key, None)
    shown = ", ".join(f"{k} {v}" for k, v in values.items())
    return f"Mint's setting is back ({shown}).", {"kind": "prefs", "values": now}


watch_settings()


# --- the tool ------------------------------------------------------------------------------------------

PROMPT = """Undo: Mint keeps a journal of what it changed in the last 24 hours. "Undo that" / "put it back" -> \
undo action=last; "undo the last 3 things" -> count=3; "undo the brightness change" / "undo the rename" -> \
action=last match=<those words>; "what can you undo?" -> action=list; "redo" right after an undo -> action=redo. \
It covers brightness, volume, mute, dark mode, Do Not Disturb, Night Shift, keep awake, Wi-Fi, window layouts, text \
you typed or rewrote, clipboard changes, files you moved, renamed, trashed, created or edited, tidy-ups, reminders \
and notes you created, and your own settings. Say in one line what was reversed. A sent message or email, a \
purchase or anything done on a web page can't be undone - say so plainly, never pretend."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="undo",
        description=("Undo what Mint itself did in the last 24 hours, newest first: switches (brightness, volume, "
                     "mute, dark mode, Do Not Disturb, Night Shift, keep awake, Wi-Fi), window layouts, text Mint "
                     "typed or rewrote, clipboard changes, files Mint moved/renamed/trashed/created/edited, "
                     "tidy-ups, reminders and notes Mint created, Mint's own settings. Actions: last (undo the "
                     "newest `count`, or the newest matching `match`), list (what can be undone), redo (the last "
                     "undo). Sent messages and emails and web actions can't be undone."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["last", "list", "redo"], description="default last"),
            "count": types.Schema(type=types.Type.INTEGER, description="last: how many, newest first (default 1)"),
            "match": types.Schema(type=S, description="last: which one in the user's words, e.g. 'brightness', "
                                                      "'the rename', 'window' - for an older action")},
            required=[]))]


def tool(args: dict) -> str:
    action = str(args.get("action") or "last").lower()
    if action == "list":
        return listing()
    if action == "redo":
        return redo()
    return undo_last(int(args.get("count") or 1), str(args.get("match") or ""))


HANDLERS = {"undo": tool}
