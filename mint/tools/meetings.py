"""Meetings: record a call without a bot, then write its transcript and notes.

    "record this meeting"                      "take notes of this call"
    "stop recording"                           "what did we decide in the Acme call?"
    "what were my action items from yesterday's standup?"

Nothing joins the call. MintRecorder (launcher/recorder, a small Swift helper inside
Mint.app) records two tracks on the Mac, side by side and on the same clock:

    others.wav   everything the Mac plays - the other people in Google Meet (in any
                 browser), Zoom, Teams, Slack huddles, FaceTime... - through a Core Audio
                 process tap (macOS 14.2+, the "System Audio Recording Only" permission)
    you.wav      the microphone

When the recording stops, both tracks are transcribed by Gemini in two-minute pieces
(silent pieces are skipped). Mic lines are "You", call lines are "Others", or the
speaker's name when it is said. Your voice leaking from the speakers into the mic (no
headphones) is dropped where the call track has the same words at the same time. Then a
Flash model writes the notes: summary, decisions, action items with owners, open
questions and key quotes with their times.

Everything goes into <storage>/Meetings/<YYYY-MM-DD HH.MM> <title>/:
transcript.md, notes.md, meta.json and the audio (compressed to .m4a afterwards).
Audio is uploaded to Gemini only for a recording the user started.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mint.core import config

log = logging.getLogger("mint.tools.meetings")

TRACKS = (("you", "You"), ("others", "Others"))
NOTES_MODELS = [m for m in (os.environ.get("MINT_NOTES_MODEL"), "gemini-3.7-flash", "gemini-3.5-flash",
                            "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest") if m]
KEEP_WAV = os.environ.get("MINT_MEETING_KEEP_WAV") == "1"
MEETING_SITES = re.compile(r"meet\.google\.com/[a-z]{3,4}-|teams\.(?:microsoft|live)\.com|zoom\.us/(?:wc|j)/|"
                           r"app\.zoom\.us|whereby\.com/|app\.slack\.com/huddle|meet\.jit\.si/|webex\.com/", re.I)
CHROME_FAMILY = {"com.google.Chrome": "Google Chrome", "com.brave.Browser": "Brave Browser",
                 "com.microsoft.edgemac": "Microsoft Edge", "company.thebrowser.Browser": "Arc"}
# Meeting apps (and their helpers) by bundle id: one of them playing sound means the call is still on.
MEETING_BUNDLES = ("us.zoom.", "com.microsoft.teams", "com.apple.FaceTime", "com.cisco.webex", "Cisco-Systems.Spark",
                   "com.gotomeeting.", "com.around.", "co.teamgram.Tuple", "com.hnc.Discord", "com.skype.")
MAX_RECORDING = 4 * 3600   # the recorder stops by itself after this (its --seconds)
AUTO_STOP_IDLE = 5 * 60    # no call app on the mic this long -> the call is over, recording stops
AUTO_STOP_POLL = 20        # how often the call is checked while recording (seconds)
RESUME_AFTER = 120         # an unfinished meeting folder untouched this long, with no recorder, is picked up

_lock = threading.RLock()
_rec: dict = {}            # the recording in progress (see "Recording"): proc, folder, title, started, levels...
_processing: dict[str, str] = {}   # folder -> what is happening ("transcribing 3/10")


# --- Where things are ---------------------------------------------------------------------

def storage_root() -> Path:
    """The Mint storage folder: Settings > Storage, else ~/Documents/Mint."""
    try:
        from mint.core import prefs
        chosen = str(prefs.get("storage_folder") or "").strip()
    except Exception:
        chosen = ""
    return Path(os.path.expanduser(chosen)) if chosen else Path.home() / "Documents" / "Mint"


def meetings_root() -> Path:
    from mint.core import config
    return config.storage("Meetings")


def recorder_path() -> Path | None:
    """MintRecorder: $MINT_RECORDER, inside the running Mint.app, the dev build, the installed app."""
    places = [os.environ.get("MINT_RECORDER", "")]
    if os.environ.get("MINT_APP_PATH"):
        places.append(os.path.join(os.environ["MINT_APP_PATH"], "Contents", "MacOS", "MintRecorder"))
    places += [str(config.PROJECT_ROOT / "build" / "MintRecorder"),
               str(Path.home() / "Applications" / "Mint.app" / "Contents" / "MacOS" / "MintRecorder")]
    for place in places:
        if place and os.path.isfile(place) and os.access(place, os.X_OK):
            return Path(place)
    return None


def _safe_name(title: str) -> str:
    name = re.sub(r"[/\\:*?\"<>|\n\r\t]+", " ", title).strip(" .")
    return re.sub(r"\s+", " ", name)[:80] or "Meeting"


def _stamp(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def _duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if seconds < 60:
        return f"{int(seconds)} s"
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def _read_meta(folder: Path) -> dict:
    try:
        return json.loads((folder / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


def _write_meta(folder: Path, meta: dict) -> None:
    tmp = folder / f"meta.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    tmp.replace(folder / "meta.json")


def _notify(text: str) -> None:
    try:
        from mint.tools.work import _notify_mint
        _notify_mint(text)
    except Exception as error:
        log.info("notify: %s", error)


# --- What the meeting is called ----------------------------------------------------------

def _recorder_json(*args: str, timeout: float = 5) -> dict:
    binary = recorder_path()
    if binary is None:
        return {}
    try:
        done = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=timeout)
        return json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return {}


def meeting_status() -> dict:
    """Does a call look active? (meeting apps running, who holds the mic). Mint itself is left out."""
    return _recorder_json("meeting-status", "--ignore", str(os.getpid()))


def check() -> dict:
    """Permissions and devices, without asking for anything."""
    return _recorder_json("check")


def _calendar_title() -> str:
    """The calendar event happening now (or starting within 10 minutes), only if Calendar access
    was already given - never asks."""
    try:
        import EventKit
        from Foundation import NSDate
        if EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent) not in (3, 4):
            return ""
        from mint.tools import everyday as skills
        store, problem = skills._event_store(EventKit.EKEntityTypeEvent)
        if problem or store is None:
            return ""
        now = time.time()
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            NSDate.dateWithTimeIntervalSince1970_(now - 4 * 3600), NSDate.dateWithTimeIntervalSince1970_(now + 600), None)
        best, best_score = "", -1.0
        for event in store.eventsMatchingPredicate_(predicate) or []:
            if event.isAllDay():
                continue
            begins = float(event.startDate().timeIntervalSince1970())
            ends = float(event.endDate().timeIntervalSince1970())
            if not (begins - 600 <= now <= ends + 300):
                continue
            where = " ".join(str(x or "") for x in (event.location(), event.notes(), event.URL()))
            # A call link beats a plain block of time; the most recently started wins ties.
            score = (2 if MEETING_SITES.search(where) or "zoom" in where.lower() else 0) + begins / 1e10
            if score > best_score:
                best, best_score = str(event.title() or ""), score
        return best.strip()
    except Exception as error:
        log.info("calendar title: %s", error)
        return ""


def _browser_call() -> tuple[str, str]:
    """(site, tab title) of a call open in a running Chromium browser or Safari, else ("", "")."""
    try:
        import AppKit
        running = {str(a.bundleIdentifier() or "") for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()}
    except Exception:
        return "", ""
    scripts = []
    for bundle, name in CHROME_FAMILY.items():
        if bundle in running:
            scripts.append(f'tell application "{name}" to repeat with w in windows\n'
                           f'repeat with t in tabs of w\nset out to out & (URL of t) & tab & (title of t) & linefeed\n'
                           f'end repeat\nend repeat')
    if "com.apple.Safari" in running:
        scripts.append('tell application "Safari" to repeat with w in windows\nrepeat with t in tabs of w\n'
                       'set out to out & (URL of t) & tab & (name of t) & linefeed\nend repeat\nend repeat')
    for body in scripts:
        script = f'set out to ""\n{body}\nreturn out'
        try:
            done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in done.stdout.splitlines():
            url, _, title = line.partition("\t")
            if MEETING_SITES.search(url):
                site = ("Google Meet" if "meet.google" in url else "Teams" if "teams." in url else
                        "Zoom" if "zoom.us" in url else "Slack huddle" if "slack.com" in url else "Call")
                return site, title.strip()
    return "", ""


def guess_title() -> str:
    """From the calendar event now, else the call's browser tab, else the meeting app in use."""
    title = _calendar_title()
    if title:
        return title
    site, tab = _browser_call()
    if site:
        # "Meet - Weekly sync" -> "Weekly sync"; "Meet - abc-defg-hij" says nothing -> "Google Meet call".
        tab = re.sub(r"^(?:Meet|Google Meet|Microsoft Teams|Zoom)\s*[-–|:]\s*", "", tab).strip()
        tab = re.sub(r"\s*[-–|]\s*(?:Google Meet|Microsoft Teams|Zoom|Slack)$", "", tab).strip()
        if tab and not re.fullmatch(r"[a-z]{3,4}-[a-z]{4}-[a-z]{3}|Meet|Google Meet|Microsoft Teams|Zoom", tab):
            return tab[:80]
        return f"{site} call"
    app = meeting_status().get("call_app")
    if app and app != "browser":
        return f"{app} call"
    return ""


# --- Recording ----------------------------------------------------------------------------

# One recording at a time. `_rec` is that recording's own dict, replaced (never emptied in place) when it
# ends, so a thread holding it - the reader, stop(), the call watcher - never picks up the next one:
#   {"starting": True, "title", "started"}      while start() guesses the title and launches the recorder
#   {"proc", "folder", "title", "started", ...} recording ({"stopping": True} once it is being stopped)

# --- The call Mint has noticed (set by the session when a call app takes the mic) ------------

_call: dict = {}


def call_started(app: str) -> None:
    """A call app took the microphone: the recorder control appears. Its title is looked up aside."""
    with _lock:
        if _call.get("app"):
            return
        _call.update(app=app, since=time.time(), title="")

    def look():
        try:
            title = guess_title()
        except Exception:
            title = ""
        with _lock:
            if _call.get("app") == app:
                _call["title"] = title
    threading.Thread(target=look, daemon=True, name="meeting-title").start()


def call_ended() -> None:
    with _lock:
        _call.clear()


def on_call() -> dict:
    """{"app", "since", "title"} while a call has the microphone, else {}."""
    with _lock:
        return dict(_call)


def phase() -> str:
    """starting | recording | stopping | "" - for the recorder control."""
    with _lock:
        if _rec.get("starting"):
            return "starting"
        if _rec.get("stopping"):
            return "stopping"
        if is_recording():
            return "recording"
        return "starting" if _rec else ""


def current_title() -> str:
    with _lock:
        return str(_rec.get("title") or "")


def processing() -> dict:
    """Meetings whose notes are being written: folder -> what is happening."""
    with _lock:
        return dict(_processing)


def is_recording() -> bool:
    with _lock:
        proc = _rec.get("proc")
        return proc is not None and proc.poll() is None and not _rec.get("stopping")


def elapsed() -> float:
    with _lock:
        return time.time() - _rec["started"] if is_recording() else 0.0


def busy() -> bool:
    """Recording (or starting / stopping one) or still writing notes (Mint must stay loaded)."""
    with _lock:
        return bool(_rec) or bool(_processing)


def levels() -> dict:
    """The latest input levels (RMS 0..1) for a recording indicator: {"others", "you", "seconds"}."""
    with _lock:
        return dict(_rec.get("levels") or {})


def _drop(rec: dict) -> None:
    """Forget `rec` as the current recording (only if it still is)."""
    global _rec
    with _lock:
        if _rec is rec:
            _rec = {}


def _has_audio(folder: Path) -> bool:
    """At least a second of sound on disk (a WAV header alone is 44 bytes)."""
    for name, _label in TRACKS:
        for ext, least in ((".wav", 44 + 32000), (".m4a", 1000)):
            try:
                if (folder / f"{name}{ext}").stat().st_size > least:
                    return True
            except OSError:
                pass
    return False


def _reader(rec: dict, started: threading.Event) -> None:
    proc = rec["proc"]
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("event")
        with _lock:
            if kind == "started":
                rec["tracks"] = {"others": bool(event.get("system_audio")), "you": bool(event.get("mic"))}
                rec["input_device"] = event.get("input_device", "")
                started.set()
            elif kind == "level":
                rec["levels"] = {k: event.get(k) for k in ("others", "you", "seconds")}
            elif kind == "warning":
                rec.setdefault("warnings", []).append(str(event.get("message", "")))
            elif kind == "error":
                rec["error"] = str(event.get("message", ""))
                started.set()
            elif kind == "stopped":
                rec["seconds"] = float(event.get("seconds") or 0)
                rec["why"] = str(event.get("why") or "")
                rec["had_sound"] = {k: event.get(f"{k}_had_sound") for k in ("others", "you")}
    started.set()
    rec["eof"].set()
    proc.wait()
    with _lock:
        if _rec is not rec or rec.get("stopping"):
            return
        rec["stopping"] = True          # this thread finishes it
        answered = rec.get("answered")
        folder = rec["folder"]
        if rec.get("why") == "time limit":
            reason = f"it reached the {_duration(MAX_RECORDING)} limit"
        else:
            reason = rec.get("error") or f"the recorder exited ({proc.returncode})"
    log.warning("recorder ended: %s", reason)
    if _has_audio(folder):
        _finish_recording(rec, reason=reason)
        _notify(f"(Meeting recording stopped by itself: {reason}. Writing the transcript and notes of what was "
                "recorded; tell the user briefly.)")
        return
    # Nothing was recorded (a permission refused, the device gone at once): no empty meeting is left behind.
    _stop_video(rec)
    _drop(rec)
    shutil.rmtree(folder, ignore_errors=True)
    if answered:
        _notify(f"(The meeting recording could not start: {reason}. Nothing was recorded; tell the user briefly.)")


def _call_active(recorder_pid: int) -> bool | None:
    """Is a call still using the Mac? A meeting app or browser on the mic, or a meeting app playing
    sound. None when the recorder cannot tell."""
    info = _recorder_json("meeting-status", "--ignore", f"{os.getpid()},{recorder_pid}")
    if not info:
        return None
    if info.get("call_likely"):
        return True
    return any(str(b).startswith(MEETING_BUNDLES) for b in info.get("audio_output_users") or [])


def _watch_call(rec: dict) -> None:
    """Stop the recording once the call is over: no call on the mic for AUTO_STOP_IDLE seconds. Only
    after a call was seen at all - an in-person meeting (just the mic) is never stopped this way."""
    proc = rec["proc"]
    seen, quiet_since = False, time.time()
    while not rec["eof"].wait(AUTO_STOP_POLL):
        with _lock:
            if _rec is not rec or rec.get("stopping") or proc.poll() is not None:
                return
        active = _call_active(proc.pid)
        now = time.time()
        if active is None:
            continue
        if active:
            seen, quiet_since = True, now
            continue
        if seen and now - quiet_since >= AUTO_STOP_IDLE:
            log.info("no call for %d s: stopping the recording", now - quiet_since)
            text = _stop(rec, reason="the call ended")
            if text.startswith("Stopped"):
                _notify(f"(Meeting notes) The call seems to be over, so I stopped recording '{rec['title']}' - "
                        f"no call app has used the microphone for {_duration(AUTO_STOP_IDLE)}. Writing the "
                        "transcript and notes now. (Tell the user briefly.)")
            return


def start(title: str = "", video: bool = False, mic: bool = True) -> str:
    """Record the meeting's audio (the user's mic and the call) - and with `video`, the call's window too,
    into the same folder."""
    global _rec
    with _lock:
        if _rec.get("starting"):
            return "Already starting a recording."
        if _rec.get("stopping"):
            return f"Still stopping the recording of '{_rec['title']}' - try again in a moment."
        if is_recording():
            return f"Already recording '{_rec['title']}' ({_stamp(elapsed())} so far)."
        if _rec:
            return "The last recording is just ending - try again in a moment."
        # Taken now: guessing the title can take a while, and a second start must not launch a second recorder.
        rec = {"starting": True, "title": (title or "").strip() or "Meeting", "started": time.time()}
        _rec = rec
    try:
        binary = recorder_path()
        if binary is None:
            _drop(rec)
            return "The meeting recorder (MintRecorder) is not installed. Run install.sh to rebuild Mint."
        title = (title or "").strip() or guess_title() or "Meeting"
        now = dt.datetime.now()
        folder = meetings_root() / f"{now:%Y-%m-%d %H.%M} {_safe_name(title)}"
        n = 2
        while folder.exists():
            folder = folder.with_name(f"{now:%Y-%m-%d %H.%M} {_safe_name(title)} ({n})")
            n += 1
        folder.mkdir(parents=True)
        os.chmod(folder, 0o700)
        meta = {"title": title, "started": now.isoformat(timespec="seconds"), "status": "recording"}
        earlier = _earlier_part(title, now)
        if earlier is not None:
            meta["continues"] = earlier.name
        _write_meta(folder, meta)
        try:
            proc = subprocess.Popen([str(binary), "record", "--out", str(folder), "--seconds", str(MAX_RECORDING)]
                                    + ([] if mic else ["--no-mic"]),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, bufsize=1)
        except OSError as error:
            _drop(rec)
            shutil.rmtree(folder, ignore_errors=True)
            return f"Could not start the recorder: {error}"
    except BaseException:
        _drop(rec)
        raise
    started = threading.Event()
    with _lock:
        rec.pop("starting", None)
        rec.update(proc=proc, folder=folder, title=title, started=time.time(), warnings=[], levels={},
                   eof=threading.Event())
    threading.Thread(target=_reader, args=(rec, started), daemon=True, name="meeting-recorder").start()
    threading.Thread(target=_watch_call, args=(rec,), daemon=True, name="meeting-call-watch").start()
    # The first recording may wait on a permission prompt: answer anyway after a few seconds.
    started.wait(8)
    if rec.get("error"):
        try:
            proc.wait(2)        # an error usually ends the recorder
        except subprocess.TimeoutExpired:
            pass
    with _lock:
        rec["answered"] = True
        error, warnings, tracks = rec.get("error"), list(rec.get("warnings", [])), rec.get("tracks")
        failed = bool(error) and proc.poll() is not None
        mine = failed and _rec is rec and not rec.get("stopping")
        if mine:
            rec["stopping"] = True
    if failed:
        if mine:
            _drop(rec)
            shutil.rmtree(folder, ignore_errors=True)
        return f"Could not record: {error}"
    if tracks is None:
        return (f"Starting to record '{title}'. macOS may be asking for permission to record the microphone "
                "or system audio - approve it.")
    what = ("your microphone and the call audio" if tracks.get("you") and tracks.get("others")
            else "only your microphone" if tracks.get("you") else "only the call audio")
    if video:
        from mint.tools import screenrec
        call = on_call()
        answer = screenrec.record_meeting_video(folder / "video.mp4", call.get("app", ""), call.get("title", ""),
                                                mic=mic)
        if screenrec.recording_file() == str(folder / "video.mp4"):
            with _lock:
                rec["video"] = str(folder / "video.mp4")
            meta = _read_meta(folder)
            meta["video"] = "video.mp4"
            _write_meta(folder, meta)
            what += ", and a video of " + (answer.split("Recording ", 1)[-1].split(" (")[0] or "the call")
        else:
            what += f" (the video did not start: {answer[:160]})"
    text = f"Recording '{title}' - {what}. Say 'stop recording' when the meeting ends."
    if warnings:
        text += " Note: " + "; ".join(warnings)
    return text


def _finish_recording(rec: dict, reason: str = "") -> Path:
    """Close the recorder's paperwork for `rec` and start the transcript and notes in the background."""
    global _rec
    _stop_video(rec)
    folder = rec["folder"]
    with _lock:
        if _rec is rec:
            _rec = {}
        _processing[str(folder)] = "queued"      # busy() from the recording straight into the notes
    meta = _read_meta(folder)
    seconds = rec.get("seconds") or (time.time() - rec["started"])
    meta.update(title=rec["title"], seconds=round(seconds, 1), status="recorded",
                ended=dt.datetime.now().isoformat(timespec="seconds"), warnings=rec.get("warnings", []),
                tracks=rec.get("tracks") or {}, had_sound=rec.get("had_sound") or {},
                input_device=rec.get("input_device", ""))
    if reason:
        meta["stopped_because"] = reason
    _write_meta(folder, meta)
    threading.Thread(target=_process_and_tell, args=(folder,), daemon=True, name="meeting-notes").start()
    return folder


def _stop(rec: dict | None, reason: str = "") -> str:
    """Stop `rec` (None: the current recording) and hand it to _finish_recording - that very recording,
    even if another one has started meanwhile."""
    with _lock:
        rec = _rec if rec is None else rec
        proc = rec.get("proc")
        if rec.get("starting"):
            return "The recording is still starting - try again in a moment."
        if rec.get("stopping"):
            return f"Already stopping '{rec['title']}'."
        if proc is None or proc.poll() is not None or _rec is not rec:
            return "No meeting is being recorded."
        rec["stopping"] = True
        title, seconds = rec["title"], time.time() - rec["started"]
    try:
        proc.stdin.write("stop\n")
        proc.stdin.flush()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
    rec["eof"].wait(2)      # the reader takes the "stopped" line
    _finish_recording(rec, reason)
    return f"Stopped '{title}' after {_duration(seconds)}. Writing the transcript and notes..."


def stop() -> str:
    return _stop(None)


def _stop_video(rec: dict) -> None:
    """The meeting's video ends with its audio."""
    if not rec.get("video"):
        return
    try:
        from mint.tools import screenrec
        if screenrec.recording_file() == rec["video"]:
            screenrec.stop()
    except Exception:
        log.exception("stopping the meeting video")


def has_video() -> bool:
    with _lock:
        return bool(_rec.get("video"))


CONTINUE_WITHIN = 15 * 60    # a meeting restarted this soon under the same title is the same meeting


def _earlier_part(title: str, now: dt.datetime) -> Path | None:
    """The recording of this same meeting that was stopped minutes ago (stopped by mistake, then started
    again): its transcript goes into this part's notes."""
    try:
        folders = sorted((f for f in meetings_root().iterdir() if f.is_dir()), key=lambda f: f.name, reverse=True)
    except OSError:
        return None
    for folder in folders[:6]:
        meta = _read_meta(folder)
        if meta.get("title", "").strip().lower() != title.strip().lower() or not meta.get("ended"):
            continue
        try:
            ended = dt.datetime.fromisoformat(meta["ended"])
        except ValueError:
            continue
        if 0 <= (now - ended).total_seconds() <= CONTINUE_WITHIN:
            return folder
    return None


def _earlier_transcript(meta: dict, folder: Path) -> str:
    """The transcripts of the earlier parts of this meeting (oldest first), for its notes."""
    parts, seen = [], set()
    while meta.get("continues") and meta["continues"] not in seen:
        seen.add(meta["continues"])
        earlier = folder.parent / meta["continues"]
        try:
            parts.insert(0, (earlier / "transcript.md").read_text())
        except OSError:
            break
        meta = _read_meta(earlier)
    if not parts:
        return ""
    return ("EARLIER PART(S) OF THIS SAME MEETING (the recording was stopped and started again; its timestamps "
            "restart at 0:00):\n" + "\n".join(parts) + "\nTHE PART RECORDED NOW:\n")


def _snapshot(folder: Path, scratch: Path) -> float:
    """Copies of the tracks being recorded, with WAV headers that match what is written so far.
    -> seconds recorded."""
    import struct
    longest = 0
    for name, _label in TRACKS:
        source = folder / f"{name}.wav"
        if not source.exists():
            continue
        data = source.read_bytes()
        body = (len(data) - 44) // 2 * 2
        if body <= 0:
            continue
        head = bytearray(data[:44])
        struct.pack_into("<I", head, 4, 36 + body)
        struct.pack_into("<I", head, 40, body)
        (scratch / f"{name}.wav").write_bytes(bytes(head) + data[44:44 + body])
        longest = max(longest, body)
    return longest / 32000


def notes_so_far(folder: Path | None = None) -> str:
    """Transcript and notes of the meeting being recorded, up to now - the recording goes on."""
    import tempfile
    with _lock:
        if folder is None:
            if not is_recording():
                return "No meeting is being recorded right now."
            folder = _rec["folder"]
    folder = Path(folder)
    meta = _read_meta(folder)
    scratch = Path(tempfile.mkdtemp(prefix="mint-meeting-"))
    try:
        meta["seconds"] = round(_snapshot(folder, scratch), 1)
        if meta["seconds"] < 5:
            return "The recording has only just started - nothing to summarise yet."
        rows = transcribe(scratch)
        transcript = transcript_markdown(rows, meta)
        if not rows and not meta.get("continues"):
            return f"Nothing has been said yet in the {_duration(meta['seconds'])} recorded."
        notes, _model = write_notes(folder, _earlier_transcript(meta, folder) + transcript, meta)
        (folder / "notes so far.md").write_text(notes)
        (folder / "transcript so far.md").write_text(transcript)
        return (f"(The meeting '{meta.get('title')}' is STILL being recorded - notes of the first "
                f"{_duration(meta['seconds'])}, saved in {folder}/notes so far.md; the full notes come when it "
                "ends. Tell the user the gist briefly, or answer what they asked from it.)\n\n" + notes[:3000])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def status() -> str:
    with _lock:
        if _rec.get("starting"):
            return "Starting to record..."
        if _rec.get("stopping"):
            return f"Stopping the recording of '{_rec['title']}'..."
        if is_recording():
            lv = _rec.get("levels") or {}
            text = f"Recording '{_rec['title']}' for {_stamp(elapsed())}"
            if lv:
                text += f" (levels: call {lv.get('others') or 0:.3f}, mic {lv.get('you') or 0:.3f})"
            if _rec.get("warnings"):
                text += ". " + "; ".join(_rec["warnings"])
            return text + "."
        if _processing:
            return "Not recording. " + "; ".join(f"{Path(f).name}: {what}" for f, what in _processing.items()) + "."
    last = list_meetings(1)
    return "Not recording." + (f" Last meeting: {last[0]['name']} ({last[0]['state']})." if last else "")


# --- Unfinished meetings ------------------------------------------------------------------

RETRYABLE = ("partial", "failed", "recording", "recorded", "transcribing")


def _recorder_folders() -> set[str]:
    """Folders a MintRecorder process (this Mint's or any other) is recording into now."""
    try:
        done = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    out = set()
    for line in done.stdout.splitlines():
        if "MintRecorder" in line and " record --out " in line:
            out.add(re.sub(r"(?: --(?:seconds \S+|no-mic|no-system))+$", "", line.split(" record --out ", 1)[1]))
    return out


def _queue(folders: list[Path]) -> list[Path]:
    """Mark folders as waiting for their notes (busy() stays True); the ones already taken are left out."""
    recording = _recorder_folders()
    taken = []
    with _lock:
        for folder in folders:
            if str(folder) in _processing or _rec.get("folder") == folder or str(folder) in recording:
                continue
            _processing[str(folder)] = "queued"
            taken.append(folder)
    return taken


def _process_all(folders: list[Path]) -> list[dict]:
    """process() each queued folder in turn; ones with no audio at all are marked failed."""
    results = []
    for folder in folders:
        try:
            if not _has_audio(folder):
                meta = _read_meta(folder)
                meta.update(status="failed", error="nothing was recorded (no audio saved)", no_audio=True)
                _write_meta(folder, meta)
                results.append(dict(meta, folder=str(folder)))
                continue
            meta = _read_meta(folder)
            if meta.get("status") == "recording":
                meta.setdefault("stopped_because", "Mint quit during the recording")
                _write_meta(folder, meta)
            results.append(process(folder))
        except Exception as error:
            log.exception("resuming %s", folder.name)
            results.append({"title": folder.name, "status": "failed", "error": str(error)[:300], "folder": str(folder)})
        finally:
            with _lock:
                _processing.pop(str(folder), None)
    return results


def resume_pending(min_age: float = RESUME_AFTER) -> list[str]:
    """Meetings left unfinished (Mint quit or crashed while recording or writing the notes): folders whose
    meta says recording/recorded/transcribing, untouched for `min_age` seconds, with no recorder writing
    them. Their transcript and notes are written in the background, one after another, then Mint is told.
    -> the folder names taken."""
    try:
        folders = sorted(p for p in meetings_root().iterdir() if p.is_dir())
    except OSError:
        return []
    now, stale = time.time(), []
    for folder in folders:
        if _read_meta(folder).get("status") not in ("recording", "recorded", "transcribing"):
            continue
        times = [p.stat().st_mtime for p in (folder / "meta.json", *(folder / f"{n}.wav" for n, _l in TRACKS))
                 if p.exists()]
        if times and now - max(times) >= min_age:
            stale.append(folder)
    todo = _queue(stale)
    if todo:
        threading.Thread(target=_resume, args=(todo,), daemon=True, name="meeting-resume").start()
    return [f.name for f in todo]


def _resume(folders: list[Path]) -> None:
    results = _process_all(folders)
    lines = "\n".join(f"- '{m.get('title')}': {_state_text(m)} ({m.get('folder')})" for m in results)
    _notify(f"(Mint was closed before the notes of {len(results)} recorded meeting(s) were written. They have "
            f"been picked up again:\n{lines}\nTell the user briefly.)")


def retry(which: str = "") -> str:
    """Write the transcript and notes again: the named meeting, else the latest partial/interrupted one."""
    if which:
        matches = find(which)
    else:
        matches = [m for m in list_meetings(100) if m["status"] in RETRYABLE and _has_audio(Path(m["folder"]))]
    if not matches:
        return (f"No recorded meeting matches '{which}'." + _recent_hint() if which
                else "No meeting is waiting for its notes to be redone.")
    m = matches[0]
    folder = Path(m["folder"])
    if not _has_audio(folder):
        return f"'{m['title']}' ({m['name'][:16]}) has no audio kept, so its notes cannot be redone."
    if not _queue([folder]):
        return f"'{m['title']}' ({m['name'][:16]}) is being recorded or written already: {m['state']}."
    threading.Thread(target=_process_and_tell, args=(folder,), daemon=True, name="meeting-notes").start()
    return f"Writing the transcript and notes of '{m['title']}' ({m['name'][:16]}) again; they arrive when ready."


# --- Transcription ------------------------------------------------------------------------

# Gemini's own timestamps drift inside a two-minute piece (in testing, a line at 1:09 came back as
# 1:26). So the Mac finds the stretches of speech from the audio level first, Gemini is told where
# they are and says which stretch each line starts in, and the line gets that stretch's exact time.

_TIMING = """
Speech was detected at these times in the clip (seconds from its start), numbered:
{segments}
Return JSON: a list of {{{fields}"seg": <number of the stretch where the item starts>, "text": "<words>"}},
one item per sentence or short phrase (at most about 25 words), in order. Several items may share a stretch."""

_YOU_PROMPT = """This is the microphone of the person recording a meeting ("the user").
Transcribe exactly what the user says, in the original language. Only the main, close voice: skip faint,
distant or tinny voices (other participants coming out of the speakers). Silence or no clear speech: return [].
Do not summarise, do not translate, do not add labels.""" + _TIMING.replace("{fields}", "")

_OTHERS_PROMPT = """This is the audio of a video call (the other participants, as the computer played it).
Transcribe the speech exactly, in the original language. "who": the speaker's name when it is said or clear
from the conversation (someone introduces themselves, or is addressed by name and answers); otherwise, when
different voices speak, "Speaker 1", "Speaker 2"... in order of first appearance in this clip; "" when only one
voice speaks. Ignore notification sounds and music. Silence: return []. Do not summarise, do not
translate.""" + _TIMING.replace("{fields}", '"who": "<speaker>", ')


def speech_segments(samples, rate: int = 16000, gap: float = 0.45, shortest: float = 0.2) -> list[tuple[float, float]]:
    """(start, end) seconds where someone speaks, from the level in 20 ms frames: above the
    noise floor (its 20th percentile) by 8 dB and above -47 dBFS; pauses under `gap` bridged."""
    import numpy as np
    hop = rate // 50
    n = len(samples) // hop
    if n == 0:
        return []
    frames = samples[: n * hop].astype(np.float32).reshape(n, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    threshold = max(float(np.percentile(rms, 20)) * 2.5, 150.0)
    loud = rms > threshold
    out: list[list[float]] = []
    i = 0
    while i < n:
        if loud[i]:
            j = i
            while j < n and loud[j]:
                j += 1
            begin, end = i / 50, j / 50
            if out and begin - out[-1][1] < gap:
                out[-1][1] = end
            else:
                out.append([begin, end])
            i = j
        else:
            i += 1
    return [(a, b) for a, b in out if b - a >= shortest]


def _fewer(segments: list[tuple[float, float]], most: int = 90) -> list[tuple[float, float]]:
    """Merge across the shortest pauses until at most `most` stretches are left (keeps the prompt small)."""
    segs = [list(x) for x in segments]
    while len(segs) > most:
        k = min(range(len(segs) - 1), key=lambda i: segs[i + 1][0] - segs[i][1])
        segs[k][1] = segs[k + 1][1]
        del segs[k + 1]
    return [(a, b) for a, b in segs]


def _place(rows: list[dict], segments: list[tuple[float, float]], length: float) -> list[float]:
    """A time for each row: the start of its stretch, or spread through the stretch by words when
    several rows share one. Rows whose stretch number is missing or out of order use Gemini's time."""
    times: list[float | None] = []
    last = -1
    for r in rows:
        try:
            seg = int(r.get("seg"))
        except (TypeError, ValueError):
            seg = -1
        if 0 <= seg < len(segments) and seg >= last:
            times.append(None)
            r["_seg"] = seg
            last = seg
        else:
            r["_seg"] = None
            try:
                times.append(min(max(0.0, float(r.get("s") or 0)), length))
            except (TypeError, ValueError):
                times.append(0.0)
    by_seg: dict[int, list[int]] = {}
    for i, r in enumerate(rows):
        if r["_seg"] is not None:
            by_seg.setdefault(r["_seg"], []).append(i)
    for seg, members in by_seg.items():
        a, b = segments[seg]
        words = [max(1, len(str(rows[i].get("text", "")).split())) for i in members]
        total, before = sum(words), 0
        for i, w in zip(members, words):
            times[i] = a + (b - a) * before / total
            before += w
    for r in rows:
        r.pop("_seg", None)
    return [float(t or 0) for t in times]


def _transcribe_track(wav: Path, prompt: str, on_piece=None) -> list[dict]:
    """One track -> rows {"s", "who", "text", "piece"}, in 2-minute pieces, silent ones skipped."""
    import numpy as np
    from google.genai import types
    from mint.core.llm import generate, parse_json
    from mint.tools.video import CHUNK_SECONDS, TRANSCRIBE_MODELS, _chunks
    pieces = _chunks(wav)

    def one(item):
        index, (start, data, loud) = item
        try:
            samples = np.frombuffer(data[44:], np.int16)
            segments = _fewer(speech_segments(samples)) if loud else []
            if not segments:
                return []
            listing = "\n".join(f"#{i}: {a:.1f}-{b:.1f}" for i, (a, b) in enumerate(segments))
            try:
                text, _model = generate([types.Part.from_bytes(data=data, mime_type="audio/wav"),
                                         prompt.format(segments=listing)], TRANSCRIBE_MODELS, json_mode=True)
                rows = parse_json(text) if text.strip() else []
            except Exception as error:
                log.info("piece at %s: %s", _stamp(start), error)
                return [{"s": start + segments[0][0], "who": "", "piece": index, "failed": True,
                         "text": f"[{_stamp(start)}-{_stamp(start + CHUNK_SECONDS)} could not be transcribed]"}]
            rows = [r for r in (rows if isinstance(rows, list) else [])
                    if isinstance(r, dict) and str(r.get("text", "")).strip()]
            times = _place(rows, segments, len(samples) / 16000)
            return [{"s": start + t, "who": str(r.get("who") or "").strip(), "text": str(r["text"]).strip(),
                     "piece": index} for t, r in zip(times, rows)]
        finally:
            if on_piece:
                on_piece(len(pieces))

    with ThreadPoolExecutor(6) as pool:
        parts = list(pool.map(one, enumerate(pieces)))
    return sorted((r for part in parts for r in part), key=lambda r: r["s"])


_NAMES = """Below is a meeting transcript. "You" is the user. Other lines are "<piece>/<label>": labels such as
"Speaker 1" or "?" were given separately inside each 2-minute piece, so "3/Speaker 1" and "4/Speaker 1" may be
different people. For each such label, give the person's name ONLY when the conversation makes it clear (they
introduce themselves, are addressed by name and answer, a name already used for the same voice's lines in the
same piece...). Return JSON: {{"<piece>/<label>": "<name>"}} - only labels you are sure about; {{}} if none.

{lines}"""


def _name_speakers(you: list[dict], others: list[dict]) -> None:
    """Put names on "Speaker N" labels where the talk makes it clear (one small text call)."""
    unnamed = {(r["piece"], r["who"] or "?") for r in others if not r["who"] or re.fullmatch(r"Speaker\s*\d+", r["who"], re.I)}
    if not unnamed or not others:
        return
    rows = sorted([dict(r, _you=True) for r in you] + others, key=lambda r: r["s"])
    lines = "\n".join(f"[{_stamp(r['s'])}] " + ("You" if r.get("_you") else f"{r['piece']}/{r['who'] or '?'}")
                      + f": {r['text']}" for r in rows)[:200_000]
    from mint.core.llm import ask_json
    names = ask_json(_NAMES.format(lines=lines))
    if not isinstance(names, dict):
        return
    for r in others:
        name = names.get(f"{r['piece']}/{r['who'] or '?'}")
        if isinstance(name, str) and name.strip() and (not r["who"] or r["who"].lower().startswith("speaker")):
            if name.strip().lower() not in ("you", "unknown", "?"):
                r["who"] = name.strip()[:40]


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _drop_echo(you: list[dict], others: list[dict]) -> list[dict]:
    """Mic lines that are really the call playing through the speakers: the call track has the
    same words (or a line containing them) within a few seconds."""
    kept = []
    for row in you:
        mine = _words(row["text"])
        mine_set = set(mine.split())
        echo = False
        for o in others:
            if abs(o["s"] - row["s"]) > 6 or o["text"].startswith("["):
                continue
            theirs = _words(o["text"])
            if difflib.SequenceMatcher(None, mine, theirs).ratio() >= 0.6:
                echo = True
            elif mine_set and len(mine_set & set(theirs.split())) / len(mine_set) >= 0.75 and len(mine_set) <= len(theirs.split()):
                echo = True     # a fragment of their line ("Hi, yes," of "Hi, yes, loud and clear")
            if echo:
                break
        if not echo or not mine:
            kept.append(row)
    return kept


def _audio_file(folder: Path, name: str) -> Path | None:
    for ext in (".wav", ".m4a"):
        if (folder / f"{name}{ext}").exists():
            return folder / f"{name}{ext}"
    return None


def _as_wav(path: Path, scratch: Path) -> Path:
    """The transcriber wants 16 kHz mono WAV; recordings already are, re-processed .m4a are not."""
    if path.suffix == ".wav":
        return path
    out = scratch / f"{path.stem}.16k.wav"
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(path), str(out)],
                   capture_output=True, timeout=900, check=True)
    return out


def _beyond_limit(wav: Path) -> float:
    """Seconds of `wav` past the transcriber's limit (video.MAX_SECONDS), which are left out."""
    import wave
    from mint.tools.video import MAX_SECONDS
    try:
        with wave.open(str(wav)) as w:
            beyond = w.getnframes() / w.getframerate() - MAX_SECONDS
        return beyond if beyond > 1 else 0.0
    except (OSError, EOFError, wave.Error):
        return 0.0


def transcribe(folder: Path, progress=None, report: dict | None = None) -> list[dict]:
    """Both tracks -> rows {"s", "speaker", "text"} merged by time. `report` gets "failed_pieces" (pieces
    that could not be transcribed) and "cut_seconds" (audio past the length limit, left out)."""
    tracks = {}
    todo = [(name, label, _audio_file(folder, name)) for name, label in TRACKS]
    todo = [t for t in todo if t[2] is not None]
    done, total = [0], [0]
    lock = threading.Lock()

    def on_piece(count):
        with lock:
            done[0] += 1
            if progress:
                progress(done[0], total[0])

    for name, _label, path in todo:
        total[0] += max(1, int((path.stat().st_size - 44) / 32000 / 120) + 1) if path.suffix == ".wav" else 1
    from mint.core import llm
    llm.client()    # made once here: threads racing to make it close each other's connections
    scratch = folder / ".work"
    scratch.mkdir(exist_ok=True)
    try:
        wavs = {name: _as_wav(path, scratch) for name, _label, path in todo}
        cut = max([_beyond_limit(wav) for wav in wavs.values()] or [0.0])
        with ThreadPoolExecutor(2) as pool:
            futures = {name: pool.submit(_transcribe_track, wav, _YOU_PROMPT if name == "you" else _OTHERS_PROMPT,
                                         on_piece)
                       for name, wav in wavs.items()}
            tracks = {name: f.result() for name, f in futures.items()}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if report is not None:
        report["failed_pieces"] = sum(1 for part in tracks.values() for r in part if r.get("failed"))
        report["cut_seconds"] = round(cut, 1)
    you, others = tracks.get("you", []), tracks.get("others", [])
    you = _drop_echo(you, others)
    try:
        _name_speakers(you, others)
    except Exception as error:
        log.info("naming speakers: %s", error)
    rows = [{"s": r["s"], "speaker": "You", "text": r["text"]} for r in you]
    for r in others:
        who = r.get("who", "")
        if not who or who.lower() in ("you", "user", "others", "speaker"):
            speaker = "Others"
        elif re.fullmatch(r"(?:Speaker|Voice)\s*\d+", who, re.I):
            speaker = f"Others ({who.split()[-1]})"
        else:
            speaker = who
        rows.append({"s": r["s"], "speaker": speaker, "text": r["text"]})
    return sorted(rows, key=lambda r: (r["s"], r["speaker"] != "You"))


def transcript_markdown(rows: list[dict], meta: dict) -> str:
    """Rows joined into turns: a new line when the speaker changes or after ~30 s."""
    head = [f"# {meta.get('title') or 'Meeting'}", "",
            f"{_when(meta)} · {_duration(float(meta.get('seconds') or 0))} · "
            "**You** = your microphone, **Others** = the call audio (numbered voices are per 2-minute piece)", ""]
    lines, speaker, start, words = [], None, 0.0, []
    for row in rows:
        if words and (row["speaker"] != speaker or row["s"] - start > 30):
            lines.append(f"[{_stamp(start)}] **{speaker}:** {' '.join(words)}")
            words = []
        if not words:
            speaker, start = row["speaker"], row["s"]
        words.append(row["text"])
    if words:
        lines.append(f"[{_stamp(start)}] **{speaker}:** {' '.join(words)}")
    if not lines:
        lines = ["(No speech was recognised.)"]
    return "\n".join(head) + "\n" + "\n\n".join(lines) + "\n"


def _when(meta: dict) -> str:
    try:
        return dt.datetime.fromisoformat(meta["started"]).strftime("%A %-d %B %Y, %H:%M")
    except (KeyError, ValueError):
        return ""


# --- Notes --------------------------------------------------------------------------------

_NOTES = """You are writing the notes of a meeting the user recorded, from its transcript.
"You" is the user (their microphone); "Others" / names are the other participants from the call audio
("Others (2)" means a second voice within that 2-minute piece, not necessarily the same person all
meeting). Timestamps are [m:ss] from the start.

Write Markdown, in the language of the meeting, concise and concrete, only what the transcript supports:

# <a short, specific title for the meeting{title_hint}>
**Date:** {when} · **Length:** {length}
**Attendees:** <"You" and the names of people who spoke or were addressed; unnamed voices may well be those
same people, so never list or count labels like "Others (2)"; omit the line if nobody is identifiable>

## Summary
3-6 sentences: purpose, what was discussed, where it landed.

## Decisions
- bullet per decision (with [m:ss]); "None recorded." if none

## Action items
- [ ] **<owner>** - <task> (due <date> if said) [m:ss]   ("You" when the user took it on; a name when the
  owner is clear from context, e.g. the person from the company that has the CFO; else "Unassigned");
  "None recorded." if none

## Open questions
- unresolved questions or follow-ups; omit the section if none

## Key quotes
- [m:ss] **<speaker>:** "<exact short quote>"  (2-5 of the most important)

Transcript:
"""


def write_notes(folder: Path, transcript: str, meta: dict) -> tuple[str, str]:
    """-> (notes markdown, model)."""
    from mint.core.llm import generate
    given = meta.get("title") or ""
    hint = f"; it was recorded as '{given}' - keep that unless it is generic" if given else ""
    prompt = _NOTES.format(title_hint=hint, when=_when(meta) or "unknown",
                           length=_duration(float(meta.get("seconds") or 0))) + transcript[:400_000]
    text, model = generate(prompt, NOTES_MODELS)
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    return text + "\n", model


def _compress(folder: Path) -> None:
    """WAV (about 115 MB an hour a track) -> AAC .m4a (about 14 MB), once transcribed."""
    if KEEP_WAV:
        return
    try:
        from mint.core import prefs
        keep = prefs.get("meeting_keep_audio")
    except Exception:
        keep = True
    for name, _label in TRACKS:
        if keep is False:
            # Settings > Storage: "keep the recording" is off - the transcript and notes are what stays.
            (folder / f"{name}.wav").unlink(missing_ok=True)
            continue
        wav = folder / f"{name}.wav"
        m4a = folder / f"{name}.m4a"
        if not wav.exists():
            continue
        try:
            done = subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", "32000", str(wav), str(m4a)],
                                  capture_output=True, timeout=1800)
            if done.returncode == 0 and m4a.exists() and m4a.stat().st_size > 1000:
                wav.unlink()
        except (OSError, subprocess.TimeoutExpired) as error:
            log.info("compress %s: %s", wav.name, error)


def process(folder: Path, compress: bool = True) -> dict:
    """Transcript and notes for a recorded meeting folder. -> meta (with "status")."""
    folder = Path(folder)
    meta = _read_meta(folder)
    key = str(folder)

    def progress(done, total):
        with _lock:
            _processing[key] = f"transcribing {done}/{total}"

    with _lock:
        _processing[key] = "transcribing"
    try:
        meta["status"] = "transcribing"
        _write_meta(folder, meta)
        t0 = time.time()
        report: dict = {}
        rows = transcribe(folder, progress, report)
        meta["transcribe_seconds"] = round(time.time() - t0, 1)
        failed, cut = report.get("failed_pieces", 0), report.get("cut_seconds", 0)
        if not meta.get("seconds"):
            sizes = [(folder / f"{n}.wav").stat().st_size for n, _l in TRACKS if (folder / f"{n}.wav").exists()]
            meta["seconds"] = round(max(sizes) / 32000, 1) if sizes else max([r["s"] for r in rows] or [0])
        transcript = transcript_markdown(rows, meta)
        (folder / "transcript.md").write_text(transcript)
        meta["lines"] = len(rows)
        with _lock:
            _processing[key] = "writing notes"
        if rows:
            notes, model = write_notes(folder, _earlier_transcript(meta, folder) + transcript, meta)
            meta["notes_model"] = model
            found = re.match(r"#\s+(.+)", notes)
            if found and (not meta.get("title") or meta["title"] == "Meeting" or meta["title"].endswith(" call")):
                meta["title"] = found.group(1).strip()[:80]
        else:
            notes = f"# {meta.get('title') or 'Meeting'}\n\n**Date:** {_when(meta)}\n\nNo speech was recognised.\n"
        (folder / "notes.md").write_text(notes)
        for name in ("notes so far.md", "transcript so far.md"):
            (folder / name).unlink(missing_ok=True)
        (folder / "transcript.md").write_text(transcript_markdown(rows, meta))   # with the final title
        # Anything left out: "partial", and the audio stays as it is (WAV) for action=retry.
        complete = not failed and not cut
        meta["status"] = "done" if complete else "partial"
        for field, value in (("failed_pieces", failed), ("cut_seconds", cut)):
            if value:
                meta[field] = value
            else:
                meta.pop(field, None)
        meta.pop("error", None)
        _write_meta(folder, meta)
        if compress and complete:
            _compress(folder)
        # A generic folder name ("Meeting") gets the title the notes found.
        wanted = folder.with_name(folder.name[:16] + " " + _safe_name(meta["title"]))
        if folder.name[17:] in ("Meeting", "") and wanted != folder and not wanted.exists():
            folder.rename(wanted)
            folder = wanted
        meta["folder"] = str(folder)
        return meta
    except Exception as error:
        log.exception("meeting notes failed")
        meta.update(status="failed", error=str(error)[:300])
        _write_meta(folder, meta)
        meta["folder"] = str(folder)
        return meta
    finally:
        with _lock:
            _processing.pop(key, None)


def _missing(meta: dict) -> str:
    """What a partial transcript lacks, in words ("" when nothing)."""
    parts = []
    if meta.get("failed_pieces"):
        n = int(meta["failed_pieces"])
        parts.append(f"{n} two-minute piece{'s' if n != 1 else ''} could not be transcribed")
    if meta.get("cut_seconds"):
        parts.append(f"the last {_duration(float(meta['cut_seconds']))} (past the length limit) were left out")
    return " and ".join(parts)


def _state_text(meta: dict) -> str:
    """A meeting's state for lists and status lines."""
    state = meta.get("status") or "unknown"
    if state == "partial":
        return f"partial - {_missing(meta) or 'some audio was not transcribed'}; audio kept, action=retry redoes it"
    if state == "failed":
        retry_hint = "" if meta.get("no_audio") else "; action=retry redoes it"
        return f"failed ({meta.get('error') or 'unknown error'}){retry_hint}"
    return state


def _process_and_tell(folder: Path) -> None:
    try:
        meta = process(folder)
    finally:
        with _lock:
            _processing.pop(str(folder), None)     # queued by _finish_recording/_queue: never left behind
    if meta.get("status") in ("done", "partial"):
        try:
            notes = (Path(meta["folder"]) / "notes.md").read_text()[:2500]
        except OSError:
            notes = ""
        gap = ""
        if meta["status"] == "partial":
            gap = (f" They are only partial: {_missing(meta)}. The audio is kept, so they can be redone later "
                   "(meeting action=retry) - mention this.")
        _notify(f"(The notes of the meeting '{meta.get('title')}' are ready, saved in {meta['folder']}.{gap} Tell the "
                f"user in one or two sentences - the gist and how many action items; do not read it all out.)\n\n{notes}")
    else:
        _notify(f"(The meeting '{meta.get('title')}' was recorded, but writing its notes failed: "
                f"{meta.get('error', 'unknown error')}. The audio is kept in {meta['folder']}. Tell the user briefly.)")


# --- Past meetings ------------------------------------------------------------------------

def list_meetings(limit: int = 20) -> list[dict]:
    """Newest first: {"name", "title", "folder", "started", "seconds", "state"}."""
    root = meetings_root()
    out = []
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        meta = _read_meta(folder)
        status = meta.get("status") or ("done" if (folder / "notes.md").exists() else "unknown")
        with _lock:
            working, current = _processing.get(str(folder)), _rec.get("folder") == folder
        if working:
            state = working
        elif current:
            state = "recording"
        elif status in ("recording", "recorded", "transcribing"):
            state = "recorded, no notes (interrupted); action=retry writes them"
        else:
            state = _state_text(dict(meta, status=status))
        out.append({"name": folder.name, "title": meta.get("title") or folder.name[17:], "folder": str(folder),
                    "started": meta.get("started") or "", "seconds": meta.get("seconds") or 0, "state": state,
                    "status": status})
        if len(out) >= limit:
            break
    return out


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def find(query: str) -> list[dict]:
    """Meetings matching a description ("yesterday's standup", "the Acme call", "last"), best first."""
    meetings = list_meetings(500)
    q = (query or "").lower().strip()
    if not q or q in ("last", "latest", "last meeting", "the last one", "most recent"):
        return meetings[:1]
    today = dt.date.today()
    days: set[dt.date] = set()
    if "today" in q:
        days.add(today)
    if "yesterday" in q:
        days.add(today - dt.timedelta(days=1))
    for i, name in enumerate(_WEEKDAYS):
        if name in q:
            back = (today.weekday() - i) % 7 or (7 if "last" in q else 0)
            days.add(today - dt.timedelta(days=back))
    date = re.search(r"\d{4}-\d{2}-\d{2}", q)
    if date:
        try:
            days.add(dt.date.fromisoformat(date.group()))
        except ValueError:      # "2026-13-45": no such day - match on the words instead
            pass
    stop = {"the", "a", "an", "in", "on", "of", "call", "meeting", "meetings", "with", "from", "today", "yesterday",
            "last", "what", "did", "we", "decide", "s", "my", "our", "this", "that", "week"} | set(_WEEKDAYS)
    words = [w for w in re.findall(r"\w+", q) if w not in stop and len(w) > 1]
    scored = []
    for m in meetings:
        score = 0.0
        try:
            day = dt.date.fromisoformat(m["name"][:10])
        except ValueError:
            day = None
        if days:
            if day not in days:
                continue
            score += 2
        title = m["title"].lower()
        score += sum(3 for w in words if w in title)
        if words and score < 3 * len(words):
            try:
                body = (Path(m["folder"]) / "notes.md").read_text().lower()
            except OSError:
                body = ""
            score += sum(1 for w in words if w in body)
        if score > 0 or (days and not words):
            scored.append((score, m))
    scored.sort(key=lambda x: -x[0])     # stable: newest first among equals
    return [m for _s, m in scored]


def open_meeting(which: str, show: bool = False) -> str:
    """The notes of one meeting (for questions about it); `show` opens them on screen."""
    matches = find(which)
    if not matches:
        return f"No recorded meeting matches '{which}'." + _recent_hint()
    m = matches[0]
    folder = Path(m["folder"])
    notes = folder / "notes.md"
    if not notes.exists():
        return f"'{m['title']}' ({m['name'][:16]}) has no notes yet: {m['state']}."
    gap = ""
    if m["status"] == "partial":
        gap = (f"\n(These notes are partial: {_missing(_read_meta(folder)) or 'some audio was not transcribed'}. "
               "The audio is kept; meeting action=retry writes them again.)")
    if show:
        subprocess.run(["open", str(notes)], capture_output=True, check=False)
    text = notes.read_text()
    others = ""
    if len(matches) > 1:
        others = "\nOther matches: " + "; ".join(x["name"] for x in matches[1:4])
    return (f"Notes of '{m['title']}' ({m['name'][:16]}, {_duration(float(m['seconds'] or 0))}), from {notes}"
            f" (full transcript: {folder / 'transcript.md'}):{gap}\n\n{text[:8000]}{others}")


def _recent_hint() -> str:
    recent = list_meetings(5)
    return (" Recent meetings: " + "; ".join(m["name"] for m in recent)) if recent else " No meetings were recorded yet."


def to_reminders(which: str = "", everyone: bool = False) -> str:
    """The meeting's action items as Reminders: the user's own ("You"), or everyone's.
    Each is titled with the task and who owns it; a due day said in the meeting is kept."""
    from mint.tools import everyday as skills
    matches = find(which or "last")
    if not matches:
        return f"No recorded meeting matches '{which}'." + _recent_hint()
    m = matches[0]
    notes = Path(m["folder"]) / "notes.md"
    if not notes.exists():
        return f"'{m['title']}' has no notes yet."
    section = re.search(r"## Action items\s*\n(.*?)(?:\n## |\Z)", notes.read_text(), re.S)
    items = re.findall(r"^- \[[ x]\] \*\*(.+?)\*\*\s*[-–:]\s*(.+?)\s*(?:\[\d+:\d\d\])?\s*$",
                       section.group(1) if section else "", re.M)
    mine = [(who, what) for who, what in items if everyone or who.strip().lower() in ("you", "me")]
    if not mine:
        return (f"'{m['title']}' has no action items" + ("" if everyone else " for you") + "."
                + (f" Others have {len(items)}; ask for everyone's." if items and not everyone else ""))
    made = []
    for who, what in mine:
        title = what if who.strip().lower() in ("you", "me") else f"{who}: {what}"
        result = skills.create_reminder(f"{title} ({m['title']})")
        made.append(title if not result.lower().startswith(("could not", "mint is not allowed", "access")) else
                    f"FAILED {title}: {result}")
    return f"Added {len(mine)} reminder(s) from '{m['title']}': " + "; ".join(made)


def listing(which: str = "") -> str:
    rows = find(which) if which else list_meetings(15)
    if not rows:
        return "No recorded meetings" + (f" match '{which}'." if which else " yet.")
    return "\n".join(f"- {m['name'][:16]} · {m['title']} · {_duration(float(m['seconds'] or 0))} · {m['state']}"
                     for m in rows[:15]) + f"\n(Saved in {meetings_root()})"


# --- The tool -----------------------------------------------------------------------------

PROMPT = """Meetings: Mint can record a call on this Mac without joining it (Google Meet, Zoom, Teams, Slack \
huddles, FaceTime - both the user's mic and the call audio), then write a transcript and notes. "record this \
meeting", "take notes of this call", "start recording" during a call -> meeting action=start (video=true for \
"record the meeting with video" / "video and transcript") (a video of \
the screen - "record my screen", "start video recording" - is screen_record instead) (pass title only if the user \
names the meeting). While recording, "what's been said so far?", "summary so far", "give me the transcript \
and summary", "what did they say about X?" -> action=so_far: it does NOT stop the recording - never stop a \
meeting unless the user says it is over or says stop. "stop recording", "the meeting is over" -> action=stop (when only a screen recording is running, \
screen_record stop); the notes arrive later by \
themselves (recording also stops by itself a few minutes after the call ends, or after 4 hours). \
If notes came out partial or were interrupted, "try the notes again" -> action=retry (which= optional). \
For questions about a past meeting - "what did we decide in the Acme call?", "my action items from \
yesterday's standup" - use action=open with which=<how the user described it> and answer from the notes \
(open with show=true only when the user wants to see them). "Add my action items to reminders" / "remind me \
of what I have to do from that meeting" -> action=reminders (which= the meeting, default the last one; \
everyone=true for all owners). Never start recording on your own; recording is \
only for calls the user asks to record, and tell them it is recording."""


def declarations():
    from google.genai import types

    def s(kind, description):
        return types.Schema(type=kind, description=description)
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="meeting",
        description=("Record a meeting/call on this Mac without a bot (the user's microphone and the call audio as "
                     "two tracks), then a transcript and notes (summary, decisions, action items, quotes) saved in "
                     "Mint's Meetings folder. Actions: start, stop, so_far (transcript and notes of the meeting being "
                     "recorded, up to now - the recording goes on), status, list (past meetings, optionally "
                     "matching `which`), open (the notes of one past meeting, to answer questions about it), "
                     "retry (write the transcript and notes again for `which`, else the latest partial or "
                     "interrupted meeting), reminders (turn the meeting's action items into Reminders: the "
                     "user's own, or everyone's with everyone=true)."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["start", "stop", "so_far", "status", "list", "open",
                                                                "retry", "reminders"]),
            "video": types.Schema(type=types.Type.BOOLEAN, description="start: also record a video of the call's "
                                                                       "window (with sound) - 'record the meeting with video'"),
            "title": s(S, "start: the meeting's name if the user gave one (otherwise it is guessed from the "
                          "calendar or the call)"),
            "which": s(S, "open/list/retry: which meeting, as the user said it - 'Acme call', 'yesterday's standup', "
                          "'Monday', '2026-09-28', 'last'"),
            "show": types.Schema(type=types.Type.BOOLEAN, description="open: also open the notes on screen"),
            "everyone": types.Schema(type=types.Type.BOOLEAN, description="reminders: everyone's action items, "
                                                                           "not only the user's")},
            required=["action"]))]


def tool(args: dict) -> str:
    action = str(args.get("action") or "status").lower()
    if action == "start":
        return start(str(args.get("title") or ""), bool(args.get("video")))
    if action == "stop":
        return stop()
    if action == "so_far":
        return _so_far_tool()
    if action == "list":
        return listing(str(args.get("which") or ""))
    if action == "open":
        return open_meeting(str(args.get("which") or "last"), bool(args.get("show")))
    if action == "retry":
        return retry(str(args.get("which") or ""))
    if action == "reminders":
        return to_reminders(str(args.get("which") or ""), bool(args.get("everyone")))
    return status()


def _so_far_tool() -> str:
    """Answers within 25 s; a long meeting's notes so far arrive a little later as a message."""
    state: dict = {}

    def run():
        try:
            state["result"] = notes_so_far()
        except Exception as error:
            log.exception("notes so far")
            state["result"] = f"FAILED: could not write the notes so far: {error}"
        if state.get("late"):
            _notify(state["result"])
    worker = threading.Thread(target=run, daemon=True, name="meeting-so-far")
    worker.start()
    worker.join(25)
    if worker.is_alive():
        state["late"] = True
        return ("Writing the notes so far - the recording goes on. They come as a message in a moment; tell the "
                "user in a few words.")
    return state["result"]


HANDLERS = {"meeting": tool}
