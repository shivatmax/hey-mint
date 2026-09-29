"""Screen recording: a video of the screen, one window or a part of the screen.

    "record my screen"                          "record the Chrome window with sound"
    "record the left half of the screen"        "record the video player"
    "stop recording"                            "are you still recording?"

MintScreen (launcher/screenrec, a small Swift helper inside Mint.app) records with
ScreenCaptureKit into an .mp4 (H.264, and AAC tracks for the sound the Mac plays and the
microphone when asked). Mint's own windows (the orb) are left out, and so is Mint's voice.

What to record:
    screen   the main display - starts at once
    window   the front window of an app (or of the app in front)
    area     a part of the screen, said in words: "left half", "top-right quarter",
             "middle third", "the Chrome window", or anything else ("the video player",
             "this area") - then Gemini finds it on a screenshot

A window or an area is first shown as a box on the screen ("Record this area?"), and only
recorded once the user confirms (action=confirm, within two minutes) - a guessed area is
easy to get wrong. Recordings go to <storage>/Videos/Recordings/Screen Recording <date>.mp4
and stop by themselves after two hours.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from mint.core import config

log = logging.getLogger("mint.tools.screenrec")

MAX_RECORDING = 2 * 3600   # the helper stops by itself after this (its --seconds)
PENDING_FOR = 120          # a shown area waits this long for "yes"
BOX_MODELS = [m for m in (os.environ.get("MINT_SCREENREC_MODEL"), "gemini-3.5-flash", "gemini-3.7-flash",
                          "gemini-robotics-er-2-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite") if m]

_lock = threading.RLock()
_rec: dict = {}            # the recording: proc, file, what, started, info, events... ({"stopping": True} while stopping)
_pending: dict = {}        # an area/window shown on screen, waiting for confirm
_last: dict = {}           # the last finished recording: file, seconds, why


# --- The helper ---------------------------------------------------------------------------

def helper_path() -> Path | None:
    """MintScreen: $MINT_SCREEN, inside the running Mint.app, the dev build, the installed app."""
    places = [os.environ.get("MINT_SCREEN", "")]
    if os.environ.get("MINT_APP_PATH"):
        places.append(os.path.join(os.environ["MINT_APP_PATH"], "Contents", "MacOS", "MintScreen"))
    places += [str(config.PROJECT_ROOT / "build" / "MintScreen"),
               str(Path.home() / "Applications" / "Mint.app" / "Contents" / "MacOS" / "MintScreen")]
    for place in places:
        if place and os.path.isfile(place) and os.access(place, os.X_OK):
            return Path(place)
    return None


def _helper_json(*args: str, timeout: float = 8):
    """The helper's last JSON line for a one-shot command (windows, displays, check), or None."""
    binary = helper_path()
    if binary is None:
        return None
    try:
        done = subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        lines = done.stdout.strip().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def check() -> dict:
    """{"screen_recording": bool, "mic_permission": ..., "macos": ...} without asking for anything."""
    return _helper_json("check") or {}


def windows() -> list[dict]:
    """On-screen windows, front first: [{id, pid, app, bundle, title, frame [x,y,w,h], front_app}]."""
    rows = _helper_json("windows")
    if isinstance(rows, dict) and rows.get("event") == "error":
        raise RuntimeError(rows.get("message") or "cannot list windows")
    return [w for w in rows or [] if w.get("pid") != os.getpid()]


def main_display() -> tuple[float, float, float, float]:
    """The main display in global points (top-left origin)."""
    try:
        import Quartz
        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return (b.origin.x, b.origin.y, b.size.width, b.size.height)
    except Exception:
        rows = _helper_json("displays") or []
        frame = next((d["frame"] for d in rows if d.get("main")), None) or (rows[0]["frame"] if rows else None)
        return tuple(frame) if frame else (0.0, 0.0, 1440.0, 900.0)


# --- Where to record ----------------------------------------------------------------------

_NUM = r"-?\d+(?:\.\d+)?"


def preset_rect(region: str, screen: tuple | None = None) -> tuple[tuple, str] | None:
    """A part of the screen said in words -> ((x, y, w, h) in global points, a description), or None.
    Halves, quarters/corners, thirds, the middle, or four numbers "x, y, w, h"."""
    text = " " + re.sub(r"[^a-z0-9.,\- ]+", " ", (region or "").lower().replace("-", " ")) + " "
    text = re.sub(r"\s+", " ", text)
    X, Y, W, H = screen or main_display()
    numbers = re.findall(_NUM, region or "")
    if len(numbers) == 4 and not re.search(r"[a-z]{3,}", text.replace("x", "").replace("y", "")):
        x, y, w, h = (float(n) for n in numbers)
        if w > 0 and h > 0:
            return (x, y, w, h), f"the area {int(x)},{int(y)} {int(w)}x{int(h)}"

    def has(*words):
        return all(re.search(rf"\b{w}\b", text) for w in words)
    top, bottom = has("top") or has("upper"), has("bottom") or has("lower")
    left, right = has("left"), has("right")
    if has("quarter") or has("corner") or ((top or bottom) and (left or right)):
        if (top or bottom) and (left or right):
            fx, fy = (0.5 if right else 0.0), (0.5 if bottom else 0.0)
            name = f"{'bottom' if bottom else 'top'}-{'right' if right else 'left'} quarter"
            return (X + fx * W, Y + fy * H, W / 2, H / 2), f"the {name} of the screen"
    if has("thirds?"):
        k = 2 / 3 if re.search(r"\b(two|2) thirds\b", text) else 1 / 3
        if left:
            return (X, Y, W * k, H), "the left third of the screen" if k < 0.5 else "the left two thirds"
        if right:
            return (X + W * (1 - k), Y, W * k, H), "the right third of the screen" if k < 0.5 else "the right two thirds"
        if top:
            return (X, Y, W, H * k), "the top third of the screen"
        if bottom:
            return (X, Y + H * (1 - k), W, H * k), "the bottom third of the screen"
        if has("middle") or has("center") or has("centre"):
            return (X + W / 3, Y, W / 3, H), "the middle third of the screen"
    if has("half") or has("side") or text.strip() in ("left", "right", "top", "bottom"):
        if left:
            return (X, Y, W / 2, H), "the left half of the screen"
        if right:
            return (X + W / 2, Y, W / 2, H), "the right half of the screen"
        if top:
            return (X, Y, W, H / 2), "the top half of the screen"
        if bottom:
            return (X, Y + H / 2, W, H / 2), "the bottom half of the screen"
    if re.fullmatch(r" (the )?(middle|center|centre)( of the screen)? ", text):
        return (X + W / 4, Y + H / 4, W / 2, H / 2), "the middle of the screen"
    if re.fullmatch(r" (the )?(whole |full |entire )?(screen|display|desktop) ", text):
        return (X, Y, W, H), "the whole screen"
    return None


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _best(rows: list[dict]) -> dict:
    """An app's main window among its on-screen ones (front first): apps also have thin helper windows
    (Chrome in full screen: its toolbar strips), so the first titled, reasonably big one, else the biggest."""
    big = [w for w in rows if w["frame"][2] >= 120 and w["frame"][3] >= 80] or rows
    titled = [w for w in big if (w.get("title") or "").strip()]
    return titled[0] if titled else max(big, key=lambda w: w["frame"][2] * w["frame"][3])


def find_window(app: str = "", rows: list[dict] | None = None) -> dict | None:
    """The front window of `app` (a name as said: "chrome", "the TextEdit window"), or of the app in
    front when `app` is empty."""
    rows = windows() if rows is None else rows
    if not rows:
        return None

    def of(hit: dict) -> dict:
        return _best([w for w in rows if w.get("pid") == hit.get("pid")])
    wanted = re.sub(r"\b(the|a|an|my|window|app|application|of|in|from)\b", " ", (app or "").lower())
    wanted = _norm(wanted)
    if not wanted:
        return of(next((w for w in rows if w.get("front_app")), rows[0]))
    for w in rows:          # front first, so the first hit is the app's front window
        names = [_norm(w.get("app") or ""), _norm((w.get("bundle") or "").split(".")[-1])]
        if any(n and (wanted == n or wanted in n or (len(n) >= 4 and n in wanted)) for n in names):
            return of(w)
    for w in rows:          # a window title ("the Wikipedia window")
        if len(wanted) >= 4 and wanted in _norm(w.get("title") or ""):
            return w
    apps = {_norm(w.get("app") or ""): w for w in reversed(rows)}
    close = difflib.get_close_matches(wanted, list(apps), n=1, cutoff=0.75)
    return of(apps[close[0]]) if close else None


def _window_named(region: str, rows: list[dict] | None = None) -> dict | None:
    """ "the Chrome window", "Brave", "the Notes app" -> that window, when the words name a running app."""
    text = (region or "").lower()
    if not re.search(r"\b(window|app)\b", text) and len(text.split()) > 3:
        return None
    try:
        rows = windows() if rows is None else rows
    except RuntimeError:
        return None
    stripped = re.sub(r"\b(the|a|an|my|window|app|application|whole|entire|of|in)\b", " ", text).strip()
    return find_window(stripped, rows) if stripped else None


def locate(region: str, point: bool = False) -> tuple[tuple, str]:
    """Ask Gemini where `region` is on a screenshot of the main display -> ((x, y, w, h) points, label).
    `point`: the one thing itself, tight (a button, an icon, a picture) - for showing the user where it is -
    rather than the whole area around it (for recording)."""
    import io

    from google.genai import types

    from mint.core.llm import generate, parse_json
    from mint.screen.ocr import _screen
    from mint.screen.vision import Blind

    image, area = _screen()
    small = image.convert("L").resize((64, 40))
    low, high = small.getextrema()
    if high - low < 12:
        raise Blind()
    shot = image.copy()
    shot.thumbnail((1600, 1600))
    buffer = io.BytesIO()
    shot.convert("RGB").save(buffer, format="jpeg", quality=82)
    hint = ""
    try:
        import Quartz
        at = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        mx = (at.x - area["left"]) / area["width"] * 1000
        my = (at.y - area["top"]) / area["height"] * 1000
        if 0 <= mx <= 1000 and 0 <= my <= 1000:
            hint = f" The mouse pointer is at x={int(mx)}, y={int(my)} on the same 0-1000 scale."
    except Exception:
        pass
    if point:
        prompt = (f'The user asked where this is on their screen: "{region}". Find that one thing on the '
                  "screenshot - a button, icon, menu, field, link, picture or chart - and give a tight box around "
                  "it alone (not the toolbar or panel it sits in). If several match, the most prominent or the one "
                  f"nearest the mouse pointer.{hint} ")
    else:
        prompt = (f'The user wants a screen recording of this part of their screen: "{region}". Find it on the '
                  "screenshot. If they say 'this' or 'here' without naming anything, it is most likely what the "
                  f"mouse pointer is on.{hint} Give the box of the whole thing (a whole video player, panel, "
                  "window or section - not one word inside it). ")
    prompt += ('Reply as JSON: {"box_2d": [ymin, xmin, ymax, xmax], "label": "<what it is, a few words>"} on a '
               '0-1000 scale of the image, or {"box_2d": null, "why": "..."} if it is not on the screen.')
    text, model = generate([types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/jpeg"), prompt],
                           models=BOX_MODELS, json_mode=True)
    answer = parse_json(text)
    if isinstance(answer, list):
        answer = answer[0] if answer else {}
    box = answer.get("box_2d") or answer.get("box")
    if not box or len(box) != 4:
        raise LookupError(answer.get("why") or f"could not find '{region}' on the screen")
    ymin, xmin, ymax, xmax = (max(0.0, min(1000.0, float(v))) for v in box)
    if xmax - xmin < (1 if point else 5) or ymax - ymin < (1 if point else 5):
        raise LookupError(f"could not find '{region}' on the screen")
    x = area["left"] + xmin / 1000 * area["width"]
    y = area["top"] + ymin / 1000 * area["height"]
    w = (xmax - xmin) / 1000 * area["width"]
    h = (ymax - ymin) / 1000 * area["height"]
    log.info("located %r with %s: %s", region, model, box)
    return (x, y, w, h), str(answer.get("label") or region)


def _clip(rect: tuple, screen: tuple | None = None) -> tuple:
    """Round to whole points and keep inside the display (and at least 40 x 40)."""
    X, Y, W, H = screen or main_display()
    x, y, w, h = rect
    x0, y0 = max(X, x), max(Y, y)
    x1, y1 = min(X + W, x + w), min(Y + H, y + h)
    if x1 - x0 < 40 or y1 - y0 < 40:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        x0, x1 = max(X, cx - 20), min(X + W, cx + 20)
        y0, y1 = max(Y, cy - 20), min(Y + H, cy + 20)
    return (round(x0), round(y0), round(x1 - x0), round(y1 - y0))


# --- Recording ----------------------------------------------------------------------------

def _duration(seconds: float) -> str:
    seconds = int(max(0, round(seconds)))
    if seconds < 60:
        return f"{seconds} s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m} min {s} s" if s else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def recordings_folder() -> Path:
    folder = config.storage("Videos") / "Recordings"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _new_file() -> Path:
    now = dt.datetime.now()
    base = f"Screen Recording {now:%Y-%m-%d at %H.%M.%S}"
    path = recordings_folder() / f"{base}.mp4"
    n = 2
    while path.exists():
        path = path.with_name(f"{base} ({n}).mp4")
        n += 1
    return path


def _notify(text: str) -> None:
    try:
        from mint.tools.work import _notify_mint
        _notify_mint(text)
    except Exception as error:
        log.info("notify: %s", error)


def is_recording() -> bool:
    with _lock:
        proc = _rec.get("proc")
        return proc is not None and proc.poll() is None and not _rec.get("stopping")


def elapsed() -> float:
    with _lock:
        return time.time() - _rec["started"] if is_recording() else 0.0


def _pending_alive() -> bool:
    return bool(_pending) and time.time() - _pending.get("made", 0) < PENDING_FOR


def busy() -> bool:
    """Recording (or starting/stopping one), or an area is on screen waiting for "yes" (Mint must stay loaded)."""
    with _lock:
        return bool(_rec) or _pending_alive()


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
                rec["info"] = event
                started.set()
            elif kind == "warning":
                rec.setdefault("warnings", []).append(str(event.get("message", "")))
            elif kind == "error":
                rec["error"] = str(event.get("message", ""))
                started.set()
            elif kind == "stopped":
                rec["seconds"] = float(event.get("seconds") or 0)
                rec["why"] = str(event.get("why") or "")
    started.set()
    proc.wait()
    rec["eof"].set()
    with _lock:
        if _rec is not rec or rec.get("stopping"):
            return
        # It ended by itself: the time limit, the window closed, an error.
        rec["stopping"] = True
        answered = rec.get("answered")
        done = _finish(rec)
    if not answered:
        return
    if done.get("seconds"):
        why = (f"it reached the {_duration(MAX_RECORDING)} limit" if rec.get("why") == "time limit"
               else rec.get("error") or rec.get("why") or f"the recorder exited ({proc.returncode})")
        _notify(f"(Screen recording stopped by itself: {why}. Saved {_duration(done['seconds'])} to "
                f"{done['file']}. Tell the user briefly.)")
    else:
        _notify(f"(The screen recording ended without a video: {rec.get('error') or 'nothing was captured'}. "
                "Tell the user briefly.)")


def _finish(rec: dict) -> dict:
    """Forget `rec` as the current recording and remember it as the last one (call with _lock held)."""
    global _rec, _last
    path = Path(rec["file"])
    ok = path.exists() and path.stat().st_size > 1000
    _last = {"file": str(path) if ok else "", "seconds": (rec.get("seconds") or 0.0) if ok else 0.0,
             "why": rec.get("why") or rec.get("error") or "", "what": rec.get("what", ""), "ended": time.time()}
    if _rec is rec:
        _rec = {}
    return _last


def _record(what: str, target_args: list[str], audio: bool = False, mic: bool = False,
            out: Path | None = None) -> str:
    """Start the helper. `what` describes the target for the user ("the whole screen")."""
    global _rec
    with _lock:
        if _rec.get("starting"):
            return "Already starting a screen recording."
        if _rec.get("stopping"):
            return "The last screen recording is still being saved - try again in a moment."
        if is_recording():
            return f"Already recording {_rec['what']} ({_duration(elapsed())} so far). Say 'stop recording' first."
        rec = {"starting": True, "what": what, "started": time.time()}
        _rec = rec
    binary = helper_path()
    if binary is None:
        with _lock:
            _rec = {}
        return "The screen recorder (MintScreen) is not installed. Run install.sh to rebuild Mint."
    permission = check()
    if permission and permission.get("screen_recording") is False:
        _helper_json("check", "--request")        # puts Mint in the list and shows the system prompt
        with _lock:
            _rec = {}
        return ("Mint needs the Screen Recording permission to record the screen: System Settings > Privacy & "
                "Security > Screen & System Audio Recording > turn on Mint, then ask again.")
    path = out or _new_file()
    command = [str(binary), "record", "--out", str(path), "--seconds", str(MAX_RECORDING),
               "--exclude-pid", f"{os.getpid()},{os.getppid()}", *target_args]
    if audio:
        command.append("--audio")
    if mic:
        command.append("--mic")
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except OSError as error:
        with _lock:
            _rec = {}
        return f"Could not start the screen recorder: {error}"
    started = threading.Event()
    with _lock:
        rec.pop("starting", None)
        rec.update(proc=proc, file=str(path), started=time.time(), warnings=[], eof=threading.Event(),
                   audio=audio, mic=mic)
    threading.Thread(target=_reader, args=(rec, started), daemon=True, name="screen-recorder").start()
    started.wait(10)        # the first recording may wait on a permission prompt
    if rec.get("error"):
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            pass
    with _lock:
        rec["answered"] = True
        error, info, warnings = rec.get("error"), rec.get("info"), list(rec.get("warnings", []))
        failed = bool(error) and proc.poll() is not None
        if failed and _rec is rec and not rec.get("stopping"):
            rec["stopping"] = True
            _finish(rec)
    if failed:
        return f"Could not record the screen: {error}"
    if info is None:
        return (f"Starting to record {what}. macOS may be asking for permission to record the screen - "
                "approve it.")
    sound = []
    if info.get("audio"):
        sound.append("the Mac's sound")
    if info.get("mic"):
        sound.append("your microphone")
    text = (f"Recording {what} ({info.get('width')}x{info.get('height')}"
            + (", with " + " and ".join(sound) if sound else ", no sound") + "). Say 'stop recording' to finish.")
    if warnings:
        text += " Note: " + "; ".join(warnings)
    return text


def stop(reveal: bool = False) -> str:
    with _lock:
        rec = _rec
        proc = rec.get("proc")
        if rec.get("starting"):
            return "The screen recording is still starting - try again in a moment."
        if rec.get("stopping"):
            return "Already saving the screen recording."
        if proc is None or proc.poll() is not None:
            return "Not recording the screen."
        rec["stopping"] = True
    try:
        proc.stdin.write("stop\n")
        proc.stdin.flush()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(20)          # finishing the file takes well under a second; long recordings a little more
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    rec["eof"].wait(3)
    with _lock:
        done = _finish(rec)
    if not done.get("file"):
        return f"The screen recording could not be saved: {rec.get('error') or 'nothing was captured'}."
    if reveal:
        subprocess.run(["open", "-R", done["file"]], check=False)
    return (f"Saved the screen recording of {rec['what']} ({_duration(done['seconds'])}) as {done['file']}"
            + (" - it is shown in Finder." if reveal else "."))


def status() -> str:
    with _lock:
        if _rec.get("starting"):
            return "Starting a screen recording..."
        if _rec.get("stopping"):
            return "Saving the screen recording..."
        if is_recording():
            text = f"Recording {_rec['what']} for {_duration(elapsed())}, into {_rec['file']}."
            if _rec.get("warnings"):
                text += " " + "; ".join(_rec["warnings"])
            return text
        if _pending_alive():
            return f"Not recording yet: waiting for the user to confirm {_pending['what']} (shown on screen)."
        if _last.get("file"):
            return f"Not recording. The last recording: {_last['file']} ({_duration(_last['seconds'])})."
    return "Not recording the screen."


# --- The tool -----------------------------------------------------------------------------

def _show(rect: tuple, note: str) -> None:
    try:
        from mint.ui.marks import marks
        marks.show([rect], style="box", note=note, seconds=PENDING_FOR / 4)
    except Exception as error:
        log.info("marks: %s", error)


def _clear_marks() -> None:
    try:
        from mint.ui.marks import marks
        marks.clear()
    except Exception:
        pass


def start(target: str = "screen", app: str = "", region: str = "", audio: bool = False, mic: bool = False,
          confirm: bool = True) -> str:
    global _pending
    target = (target or "screen").lower().strip()
    if target not in ("screen", "window", "area"):
        target = "area" if region else "window" if app else "screen"
    if target == "area" and not region.strip():
        target = "window" if app else "screen"
    if target == "area" and re.fullmatch(r"\s*(the\s+)?(whole|full|entire)?\s*(screen|display|desktop)\s*",
                                         region.lower()):
        target = "screen"
    with _lock:
        if is_recording():
            return f"Already recording {_rec['what']} ({_duration(elapsed())} so far). Say 'stop recording' first."
    if helper_path() is None:
        return "The screen recorder (MintScreen) is not installed. Run install.sh to rebuild Mint."

    if target == "screen":
        with _lock:
            _pending = {}
        return _record("the whole screen", [], audio, mic)

    window, rect, what = None, None, ""
    try:
        if target == "window":
            window = find_window(app)
            if window is None:
                return (f"No window of '{app}' is on screen." if app else "No window is on screen to record.")
        else:
            screen = main_display()
            found = preset_rect(region, screen)
            if found:
                rect, what = found
                rect = _clip(rect, screen)
            else:
                window = _window_named(region)
                if window is None:
                    try:
                        rect, label = locate(region)
                    except LookupError as error:
                        return f"Could not find that on the screen: {error}. Ask the user to describe it differently."
                    rect, what = _clip(rect, screen), f"the {label}" if not label.lower().startswith("the ") else label
    except RuntimeError as error:
        return f"Could not look at the screen: {error}"
    except Exception as error:
        if type(error).__name__ == "Blind":
            return ("Cannot see the screen - Mint needs the Screen Recording permission (System Settings > Privacy "
                    "& Security > Screen & System Audio Recording).")
        log.exception("screen area")
        return f"Could not find that area: {error}"

    if window is not None:
        title = (window.get("title") or "").strip()
        what = f"the {window.get('app') or 'app'} window" + (f" '{title[:60]}'" if title else "")
        rect = tuple(window["frame"])
        args = ["--window-id", str(window["id"])]
    else:
        args = ["--rect", ",".join(str(int(v)) for v in rect)]
    plan = {"what": what, "args": args, "rect": rect, "audio": audio, "mic": mic, "made": time.time()}
    if not confirm:
        with _lock:
            _pending = {}
        return _record(what, args, audio, mic)
    with _lock:
        _pending = plan
    _show(rect, "Record this window?" if window is not None else "Record this area?")
    x, y, w, h = (int(v) for v in rect)
    return (f"Showing {what} with a box on the screen ({w}x{h} points at {x},{y}) - NOT recording yet. Ask the "
            "user if that is the right area; on yes call screen_record action=confirm, on no action=cancel (or "
            "start again with a better description). It waits 2 minutes.")


def call_window(app: str = "", title: str = "") -> dict | None:
    """The window a call is in: a browser window showing the Meet/Teams/Zoom tab (its title), else the
    call app's window. None when neither is on screen."""
    try:
        rows = windows()
    except RuntimeError:
        return None
    words = [w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 2]
    for w in rows:
        name = (w.get("title") or "").lower()
        if re.search(r"\bmeet\b|google meet|microsoft teams|zoom meeting|huddle", name) or \
                (words and all(x in name for x in words[:3])):
            return w
    if app and app.lower() not in ("helper", "browser"):
        return find_window(app, rows)
    return None


def record_meeting_video(out: Path, app: str = "", title: str = "", mic: bool = True) -> str:
    """For a meeting recorded with video: the call's window (else the whole screen) into `out`, with the
    Mac's sound and the microphone, starting at once."""
    window = call_window(app, title)
    if window is not None:
        what = f"the {window.get('app') or 'call'} window"
        return _record(what, ["--window-id", str(window["id"])], True, mic, out=out)
    return _record("the whole screen", [], True, mic, out=out)


def snapshot() -> dict:
    """For the island: {"seconds", "what", "file"} while recording, {"pending": what} while an area waits
    for a yes, else {}."""
    with _lock:
        if is_recording():
            return {"seconds": time.time() - _rec.get("started", time.time()), "what": _rec.get("what", ""),
                    "file": _rec.get("file", "")}
        if _pending_alive():
            return {"pending": _pending.get("what", "")}
    return {}


def recording_file() -> str:
    with _lock:
        return str(_rec.get("file") or "") if is_recording() else ""


def confirm() -> str:
    global _pending
    with _lock:
        plan = dict(_pending)
        _pending = {}
    if not plan:
        return "Nothing is waiting to be confirmed - use action=start."
    if time.time() - plan["made"] > PENDING_FOR:
        _clear_marks()
        return "That area was shown over 2 minutes ago - start again (action=start)."
    _clear_marks()
    return _record(plan["what"], plan["args"], plan["audio"], plan["mic"])


def cancel() -> str:
    global _pending
    with _lock:
        plan, _pending = _pending, {}
    _clear_marks()
    if plan:
        return f"OK, not recording {plan['what']}."
    if is_recording():
        return "Nothing was waiting to be confirmed; the screen is still being recorded (say 'stop recording')."
    return "Nothing to cancel."


PROMPT = """Screen recording: Mint can record a video of the screen. "record my screen" -> screen_record \
action=start target=screen (starts at once). "record the Chrome window" / "record this window" -> target=window \
app=<the app, empty for the one in front>. "record the left half / top-right quarter / the video player / this \
area" -> target=area region=<the user's words>. For a window or an area Mint first shows a box on the screen and \
does NOT record yet: ask the user "is that the right area?", then action=confirm on yes, action=cancel on no. \
audio=true only when the user wants the sound (the Mac's sound; Mint's own voice is left out), mic=true when \
they want their voice. "stop recording" -> action=stop (reveal=true if they want to see the file); then tell them \
where it was saved. Recordings stop by themselves after 2 hours. If a meeting is being recorded too, "stop \
recording" most likely means the one the user started last - ask if unclear."""


def declarations():
    from google.genai import types

    def s(kind, description, **extra):
        return types.Schema(type=kind, description=description, **extra)
    S, B = types.Type.STRING, types.Type.BOOLEAN
    return [types.FunctionDeclaration(
        name="screen_record",
        description=("Record a video (.mp4) of the screen, one window or a part of the screen, saved in Mint's "
                     "Videos/Recordings folder. Actions: start, stop, status, confirm (start recording the window "
                     "or area just shown on screen), cancel (don't record it). A window or area is shown as a box "
                     "first and only recorded after confirm."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": s(S, "start, stop, status, confirm or cancel",
                        enum=["start", "stop", "status", "confirm", "cancel"]),
            "target": s(S, "start: screen (the whole main display), window (an app's front window) or area "
                           "(a part of the screen described in `region`)", enum=["screen", "window", "area"]),
            "app": s(S, "target=window: the app whose window to record ('Chrome', 'Keynote'); empty = the app "
                        "in front"),
            "region": s(S, "target=area: the part of the screen in the user's words - 'left half', 'top-right "
                           "quarter', 'middle third', 'the video player', 'the chat panel', 'this area'"),
            "audio": s(B, "start: also record the sound the Mac plays"),
            "mic": s(B, "start: also record the microphone (the user's voice)"),
            "confirm": s(B, "start: show a window/area on screen and wait for the user's yes first (default "
                            "true; false only when the user already confirmed exactly this)"),
            "reveal": s(B, "stop: show the saved file in Finder")},
            required=["action"]))]


def tool(args: dict) -> str:
    action = str(args.get("action") or "status").lower()
    if action == "start":
        return start(str(args.get("target") or "screen"), str(args.get("app") or ""),
                     str(args.get("region") or ""), bool(args.get("audio")), bool(args.get("mic")),
                     args.get("confirm") is not False)
    if action == "stop":
        return stop(bool(args.get("reveal")))
    if action == "confirm":
        return confirm()
    if action == "cancel":
        return cancel()
    return status()


HANDLERS = {"screen_record": tool}
