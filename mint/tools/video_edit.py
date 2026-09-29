"""Editing videos by voice: "cut the first 10 seconds", "make it vertical for Reels", "add captions".

How it works, in plain words:

1. Which video. A file path or name, or nothing / "this" / "it" = the video Mint edited a
   moment ago, else the one selected in Finder, else the video Mint watched last, else the
   newest video on the Desktop / in Downloads / Movies / Mint's screen recordings.
   "the last screen recording" = the newest file in Mint's Videos/Recordings folder.
2. A plan. ffprobe measures the video (length, size, frame rate, sound), then Gemini turns
   the user's words into a short list of operations from a FIXED vocabulary (trim, cut,
   speed, crop_aspect, captions, remove_silence, compress, gif, concat, music, ...). The
   model never writes a command: every operation is checked (times against the length,
   numbers against ranges) and this module builds the ffmpeg arguments itself, as a list,
   never through a shell.
3. One render. Everything is one ffmpeg filter graph: the parts to keep (trim, cut, the
   speech when silences are removed), joined clips, speed, rotation, the new frame shape,
   size, burned-in captions, fades, background music - so the picture is encoded once.
   This ffmpeg build has no libass/drawtext, so captions are drawn with PIL as transparent
   pictures and laid over the video at their times; a soft subtitle track and an .srt file
   come with them. Long jobs run in the background (progress from `-progress`) and Mint is
   told when they are done.
4. QA. The result is measured again with ffprobe - length against the plan, picture and
   sound present, resolution/shape as asked, size under the target - and three frames
   (start, middle, end) go on a contact sheet. The answer ends with a one-line verdict.

The original is never changed: the result is saved next to it as "<name> (edited).mp4".
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("mint.tools.video_edit")

HOME = Path.home()
WORK = HOME / "Library" / "Application Support" / "Mint" / "video_edits"
LAST = WORK / "last.json"
SYNC_WAIT = 20.0              # the tool answers within this; longer edits finish in the background
FOLLOW_UP = 45 * 60           # "now make it vertical" edits the last result for this long
PLAN_MODELS = [m for m in (os.environ.get("MINT_EDIT_MODEL"), "gemini-3.5-flash-lite", "gemini-3.7-flash",
                           "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-flash-lite-latest") if m]
NO_FFMPEG = ("Video editing needs ffmpeg, which is not installed on this Mac. Install it with Homebrew in "
             "Terminal: brew install ffmpeg - then ask again.")
RATIOS = {"9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1), "4:5": (4, 5), "5:4": (5, 4), "4:3": (4, 3),
          "3:4": (3, 4), "21:9": (21, 9)}
AUDIO_FORMATS = {"mp3", "m4a", "wav"}

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_reserved: set[str] = set()


# --- ffmpeg ------------------------------------------------------------------------------

def _bin(name: str) -> str | None:
    """The app is not started from a shell, so Homebrew's folders may not be on PATH."""
    for candidate in (shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def _stamp(seconds: float) -> str:
    from mint.tools.video import _stamp as stamp
    return stamp(seconds)


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def probe(path: str) -> dict:
    """Length, size, picture and sound of a media file, from ffprobe."""
    ffprobe = _bin("ffprobe")
    if ffprobe is None:
        raise RuntimeError(NO_FFMPEG)
    done = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"ffprobe could not read {Path(path).name}: {done.stderr.strip()[:200]}")
    data = json.loads(done.stdout or "{}")
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    durations = [float(x) for x in [fmt.get("duration")] + [s.get("duration") for s in streams] if _num(x)]
    info = {"path": path, "name": Path(path).name, "duration": float(fmt["duration"]) if _num(fmt.get("duration"))
            else max(durations or [0.0]), "bytes": int(fmt.get("size") or os.path.getsize(path)),
            "has_video": video is not None, "has_audio": audio is not None,
            "subtitles": sum(1 for s in streams if s.get("codec_type") == "subtitle"),
            "width": 0, "height": 0, "fps": 0.0, "vcodec": "", "acodec": "", "channels": 0,
            "format": fmt.get("format_name", "")}
    if video is not None:
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        rotation = 0
        for side in video.get("side_data_list") or []:
            if "rotation" in side:
                rotation = int(float(side["rotation"]))
        rotation = rotation or int(float((video.get("tags") or {}).get("rotate", 0) or 0))
        if abs(rotation) % 180 == 90:          # ffmpeg turns phone videos upright when decoding
            w, h = h, w
        info.update(width=w, height=h, vcodec=video.get("codec_name", ""),
                    fps=_rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")))
    if audio is not None:
        info.update(acodec=audio.get("codec_name", ""), channels=int(audio.get("channels") or 0))
    return info


def _num(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _rate(text) -> float:
    try:
        a, b = str(text).split("/")
        return round(float(a) / float(b), 3) if float(b) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _describe(info: dict) -> str:
    size = info["bytes"] / 1e6
    bits = [f"{_stamp(info['duration'])} long ({info['duration']:.1f} s)"]
    if info["has_video"]:
        w, h = info["width"], info["height"]
        shape = "vertical" if h > w else "square" if h == w else "landscape"
        g = math.gcd(w, h) or 1
        ratio = f"{w // g}:{h // g}" if w // g < 40 else f"{w / h:.2f}:1"
        bits.append(f"{w}x{h} ({shape}, {ratio}), {info['fps']:g} fps, {info['vcodec'] or '?'}")
    else:
        bits.append("no picture (audio only)")
    bits.append(f"sound: {info['acodec']} {'stereo' if info['channels'] == 2 else str(info['channels']) + ' ch'}"
                if info["has_audio"] else "no sound")
    if info["subtitles"]:
        bits.append(f"{info['subtitles']} subtitle track(s)")
    bits.append(f"{size:.1f} MB" if size >= 1 else f"{info['bytes'] / 1e3:.0f} KB")
    return ", ".join(bits)


# --- Which video ----------------------------------------------------------------------------

_THIS = re.compile(r"(?:(?:the|this|that|my|it)\s*)?(?:(?:last|latest|newest|recent|same|edited|new)\s+)?"
                   r"(?:video|clip|movie|file|one|edit|result|it|this|that)?(?:\s+again)?"
                   r"(?:\s+(?:you|mint)\s+(?:just\s+)?(?:made|edited|watched|saved))?", re.I)


def _video_file(path: str) -> bool:
    from mint.tools.video import VIDEO_EXT
    return Path(path).suffix.lower() in VIDEO_EXT | {".gif"}


def _last_edit() -> str:
    try:
        last = json.loads(LAST.read_text())
    except (OSError, ValueError):
        return ""
    path = str(last.get("path") or "")
    fresh = time.time() - float(last.get("when") or 0) < FOLLOW_UP
    return path if fresh and path and Path(path).exists() and not path.endswith(".gif") else ""


def _last_watched() -> str:
    """The local file watch_video looked at last, in the past few hours."""
    try:
        from mint.tools import video
        rows = video._index()
        if not rows or time.time() - float(rows[0].get("when") or 0) > 3 * 3600:
            return ""
        meta = video._load(rows[0]["key"]) or {}
        path = str(meta.get("source") if meta.get("kind") == "file" else meta.get("file") or "")
        return path if path and Path(path).is_file() else ""
    except Exception:
        return ""


def _recordings() -> Path:
    from mint.core import config
    return config.storage("Videos") / "Recordings"


def _newest_recording() -> str:
    candidates = []
    for folder, pattern in ((_recordings(), "*"), (HOME / "Desktop", "Screen Recording*"),
                            (HOME / "Movies", "Screen Recording*")):
        try:
            candidates += [p for p in folder.glob(pattern) if p.is_file() and _video_file(str(p))]
        except OSError:
            continue
    return str(max(candidates, key=lambda p: p.stat().st_mtime)) if candidates else ""


def _newest_local() -> str:
    from mint.tools import video
    found = [p for p in (video._newest_local(), _newest_recording()) if p]
    return max(found, key=os.path.getmtime) if found else ""


def _finder_all() -> list[str]:
    """Every video selected in Finder, by name (for "join these clips")."""
    script = ('tell application "Finder"\nset out to ""\nrepeat with f in (selection as list)\n'
              'set out to out & POSIX path of (f as alias) & linefeed\nend repeat\nreturn out\nend tell')
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    paths = [p for p in done.stdout.splitlines() if p.strip() and _video_file(p) and Path(p).is_file()]
    return sorted(paths, key=lambda p: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p)])


def find_source(source: str) -> tuple[str, str]:
    """-> (path, how it was found). Raises LookupError."""
    text = str(source or "").strip().strip('"').strip("'")
    low = text.lower()
    path = Path(os.path.expanduser(text)) if text else None
    if path is not None and path.is_absolute() and path.is_file():
        return str(path), "the file given"
    if text and re.search(r"screen\s*-?record|recording", low):
        found = _newest_recording()
        if found:
            return found, "the newest screen recording"
        raise LookupError("There is no screen recording yet.")
    if not text or _THIS.fullmatch(low):
        from mint.tools import video
        for finder, how in ((_last_edit, "the video edited a moment ago"),
                            (video._finder_selection, "the video selected in Finder"),
                            (_last_watched, "the video watched last"),
                            (_newest_local, "the newest video on this Mac")):
            try:
                found = finder()
            except Exception:
                found = ""
            if found and _video_file(found):
                return found, how
        raise LookupError("Which video? Give a file name or path, or select it in Finder.")
    from mint.tools import video
    kind, where = video.resolve(text)
    if kind == "url":
        raise LookupError("That is a web video; editing works on files on this Mac. Download it first, "
                          "or give the file.")
    return where, "the file given"


# --- The plan -------------------------------------------------------------------------------

_PLAN = """You turn a spoken video-editing request into a short list of operations for a safe video editor.

THE VIDEO: {facts}
EXTRA FILES the user gave (refer to them by number): {extras}
REQUEST: "{instruction}"

Operations - use ONLY these. All times are seconds (numbers) on the ORIGINAL video's timeline (0 to {duration:.2f}).
- trim {{"start": s, "end": s}}  keep only this part ("trim it to 0:30-1:45" = 30..105, "just the first minute" = 0..60)
- cut {{"start": s, "end": s}}  remove this part ("cut the first 10 seconds" = 0..10, "remove the last 5 seconds" = \
{duration:.2f}-5..{duration:.2f}). Repeatable.
- remove_silence {{"threshold_db": -35, "min_silence": 0.6}}  drop pauses and silent parts ("tighten it", "remove \
dead air"); a lower threshold (-45) removes only near-total silence
- speed {{"factor": 1.5}}  0.25 to 4 ("speed it up" with no number = 1.5, "double speed" = 2, "slow motion" = 0.5)
- crop_aspect {{"ratio": "9:16", "mode": "crop"}}  ratio one of 9:16 (vertical: Reels, Shorts, TikTok, Stories), \
1:1 (square), 4:5 (Instagram post), 16:9, 4:3, 21:9. mode: "crop" fills the frame by cutting the sides (camera \
footage, people); "blur" shows the whole picture on a blurred copy of itself (screen recordings, slides, text, or \
"don't cut anything off"); "pad" shows the whole picture with black bars
- crop_bars {{}}  crop out black bars / letterboxing
- rotate {{"degrees": 90}}  clockwise 90, 180 or 270 ("rotate left" / counter-clockwise = 270)
- flip {{"direction": "horizontal"}}  mirror: horizontal or vertical
- scale {{"height": 720}}  export at 720p / 1080p / 480p / 4K=2160 (the short side of the picture)
- captions {{"language": "", "mode": "burn"}}  transcribe the speech and burn captions into the picture; language \
only when captions in another language are asked for (e.g. "Spanish"); mode "soft" = a subtitle track viewers can \
turn on and off, only when the user asks for that
- mute {{}}  remove the sound
- volume {{"factor": 1.5}}  louder (>1) or quieter (<1), 0 to 4
- music {{"extra": 1, "volume": 0.15}}  mix extra file N under the sound; "quietly"/"underneath"/"soft" = 0.12, \
"background" = 0.2, normal = 0.4, "loud" = 0.7
- concat {{"order": ["main", 1, 2]}}  join clips in playing order: "main" is THE VIDEO, numbers are extra files
- fade {{"in": 1, "out": 1}}  fade from/to black and silence at the start/end, seconds (0 = none)
- compress {{"target_mb": 25}}  make the file smaller than this many MB ("small enough for email" = 20, "for \
Discord" = 10, "smaller" with no number = half of the current size)
- gif {{"start": s, "end": s, "fps": 12, "width": 480}}  make an animated GIF (of that part; no start/end = all)
- extract_audio {{"format": "mp3"}}  save only the sound: mp3, m4a or wav

Return JSON: {{"ops": [{{"op": "trim", "start": 30, "end": 105}}, ...], "summary": "<what will be done, a few plain \
words>", "unsupported": "<the part of the request these operations cannot do, else empty>"}}
Use as few operations as needed, in the order the user said them. Never add anything the user did not ask for. \
If only part of the request is possible, plan that part and name the rest in "unsupported". If nothing is possible, \
return an empty "ops"."""


def plan(instruction: str, info: dict, extras: list[dict]) -> dict:
    from mint.core import llm
    extra_text = "; ".join(f"{i + 1}: {e['name']} ({'video' if e['has_video'] else 'audio'}, "
                           f"{_stamp(e['duration'])})" for i, e in enumerate(extras)) or "none"
    prompt = _PLAN.format(facts=f"{info['name']}: {_describe(info)}", extras=extra_text,
                          instruction=instruction.replace('"', "'")[:600], duration=info["duration"])
    text, model = llm.generate(prompt, PLAN_MODELS, json_mode=True)
    answer = llm.parse_json(text)
    if isinstance(answer, list):         # sometimes the whole answer comes wrapped in a list
        answer = answer[0] if len(answer) == 1 and isinstance(answer[0], dict) and "ops" in answer[0] \
            else {"ops": answer}
    if not isinstance(answer, dict):
        raise ValueError("the plan was not understood")
    answer["model"] = model
    log.info("plan by %s: %s", model, json.dumps(answer)[:600])
    return answer


def _time(value, duration: float) -> float | None:
    """A time the model gave: seconds, 'm:ss', 'end'; negative = from the end."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        if value.strip().lower() in {"end", "the end"}:
            return duration
        from mint.tools.video import parse_time
        parsed = parse_time(value.strip().lstrip("-"))
        if parsed is None:
            raise ValueError(f"'{value}' is not a time")
        value = -parsed if value.strip().startswith("-") else parsed
    value = float(value)
    return duration + value if value < 0 else value


def _range(op: dict, duration: float, name: str) -> tuple[float, float]:
    start = _time(op.get("start"), duration)
    end = _time(op.get("end"), duration)
    start = 0.0 if start is None else start
    end = duration if end is None else end
    if start >= duration - 0.05:
        raise ValueError(f"{name}: {_stamp(start)} is past the end - the video is only {_stamp(duration)} long")
    end = min(end, duration)
    if end - start < 0.1:
        raise ValueError(f"{name}: the part {_stamp(start)}-{_stamp(end)} is empty")
    return max(0.0, start), end


def _clip(value, low: float, high: float, default: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(value) else min(high, max(low, value))


def _intersect(segments: list[tuple[float, float]], a: float, b: float) -> list[tuple[float, float]]:
    return [(max(s, a), min(e, b)) for s, e in segments if min(e, b) - max(s, a) > 0.04]


def _subtract(segments: list[tuple[float, float]], a: float, b: float) -> list[tuple[float, float]]:
    out = []
    for s, e in segments:
        if b <= s or a >= e:
            out.append((s, e))
            continue
        if a - s > 0.04:
            out.append((s, a))
        if e - b > 0.04:
            out.append((b, e))
    return out


def validate(raw: dict, info: dict, extras: list[dict]) -> tuple[dict, list[str]]:
    """The model's ops -> a checked edit (everything this module will do), and notes.
    Raises ValueError with a plain reason when the edit cannot be done."""
    duration = info["duration"]
    edit: dict = {"segments": [(0.0, duration)], "speed": 1.0, "rotate": 0, "flip": "", "crop_bars": False,
                  "aspect": None, "aspect_mode": "crop", "height": None, "captions": None, "mute": False,
                  "volume": 1.0, "music": None, "order": None, "fade_in": 0.0, "fade_out": 0.0,
                  "target_mb": None, "gif": None, "audio_format": None, "silence": None, "ops": []}
    notes: list[str] = []
    ops = raw.get("ops") or []
    if not isinstance(ops, list):
        raise ValueError("the plan had no operations")
    for item in ops[:14]:
        if not isinstance(item, dict):
            continue
        if "op" not in item and len(item) == 1:            # {"trim": {...}} as well as {"op": "trim", ...}
            (name, params), = item.items()
            item = dict(params if isinstance(params, dict) else {}, op=name)
        name = str(item.get("op") or "").strip().lower()
        if name == "trim":
            a, b = _range(item, duration, "trim")
            edit["segments"] = _intersect(edit["segments"], a, b)
        elif name == "cut":
            a, b = _range(item, duration, "cut")
            edit["segments"] = _subtract(edit["segments"], a, b)
        elif name == "remove_silence":
            if not info["has_audio"]:
                raise ValueError("it has no sound, so there are no silent parts to find")
            edit["silence"] = (_clip(item.get("threshold_db"), -70, -15, -35),
                               _clip(item.get("min_silence"), 0.2, 5, 0.6))
        elif name == "speed":
            edit["speed"] = _clip(item.get("factor"), 0.25, 4, 1.5)
            if _num(item.get("factor")) and abs(float(item["factor"]) - edit["speed"]) > 1e-6:
                notes.append(f"speed kept to {edit['speed']:g}x (0.25x to 4x is possible)")
        elif name == "crop_aspect":
            ratio = str(item.get("ratio") or "9:16").replace("/", ":").replace(" ", "")
            if ratio not in RATIOS:
                raise ValueError(f"the shape {ratio} is not one I can make ({', '.join(RATIOS)})")
            edit["aspect"] = ratio
            mode = str(item.get("mode") or "crop").lower()
            edit["aspect_mode"] = mode if mode in {"crop", "blur", "pad"} else "crop"
        elif name == "crop_bars":
            edit["crop_bars"] = True
        elif name == "rotate":
            degrees = int(_clip(item.get("degrees"), -270, 270, 90)) % 360
            degrees = min((90, 180, 270), key=lambda d: abs(d - degrees)) if degrees else 0
            edit["rotate"] = (edit["rotate"] + degrees) % 360
        elif name == "flip":
            edit["flip"] = "vertical" if str(item.get("direction", "")).lower().startswith("v") else "horizontal"
        elif name == "scale":
            edit["height"] = _even(_clip(item.get("height"), 144, 2160, 720))
        elif name == "captions":
            if not info["has_audio"]:
                raise ValueError("it has no sound, so there is nothing to caption")
            edit["captions"] = {"language": str(item.get("language") or "").strip(),
                                "mode": "soft" if str(item.get("mode") or "").lower() == "soft" else "burn"}
        elif name == "mute":
            edit["mute"] = True
        elif name == "volume":
            edit["volume"] = _clip(item.get("factor"), 0, 4, 1.0)
        elif name == "music":
            n = int(_clip(item.get("extra"), 1, 99, 1))
            if n > len(extras) or not extras[n - 1]["has_audio"]:
                raise ValueError("which music? Give the music file as an extra file")
            edit["music"] = {"extra": n - 1, "volume": _clip(item.get("volume"), 0.01, 1.5, 0.15)}
        elif name == "concat":
            order = item.get("order") or ["main"] + list(range(1, len(extras) + 1))
            clean = []
            for part in order:
                if str(part).lower() == "main":
                    clean.append("main")
                elif str(part).isdigit() and 1 <= int(part) <= len(extras):
                    clean.append(int(part) - 1)
            if "main" not in clean:
                clean.insert(0, "main")
            if len(clean) < 2:
                raise ValueError("which clips should be joined? Give the other clips as extra files")
            edit["order"] = clean
        elif name == "fade":
            edit["fade_in"] = _clip(item.get("in"), 0, 10, 0)
            edit["fade_out"] = _clip(item.get("out"), 0, 10, 0)
        elif name == "compress":
            edit["target_mb"] = _clip(item.get("target_mb"), 0.5, 4000, round(info["bytes"] / 2e6, 1))
        elif name == "gif":
            if item.get("start") not in (None, "") or item.get("end") not in (None, ""):
                a, b = _range(item, duration, "gif")
                edit["segments"] = _intersect(edit["segments"], a, b)
            edit["gif"] = {"fps": int(_clip(item.get("fps"), 4, 30, 12)),
                           "width": _even(_clip(item.get("width"), 120, 1280, 480))}
        elif name == "extract_audio":
            fmt = str(item.get("format") or "mp3").lower().lstrip(".")
            edit["audio_format"] = fmt if fmt in AUDIO_FORMATS else "mp3"
        else:
            notes.append(f"skipped '{name}' (not something this editor does)")
            continue
        edit["ops"].append({k: v for k, v in item.items()})
    if not edit["ops"]:
        reason = str(raw.get("summary") or "").strip() if raw.get("unsupported") else ""
        raise ValueError(reason.rstrip(".") or (f"this editor cannot {raw['unsupported']}" if raw.get("unsupported")
                                                 else "I could not tell what to change"))
    if not edit["segments"]:
        raise ValueError("those cuts leave nothing of the video")
    if edit["gif"] and edit["audio_format"]:
        raise ValueError("a GIF and an audio file at once - ask for one at a time")
    if edit["audio_format"] and edit["mute"]:
        raise ValueError("muting and extracting the sound contradict each other")
    if not info["has_video"]:
        video_ops = [k for k in ("aspect", "crop_bars", "height", "captions", "gif") if edit[k]]
        if video_ops or edit["rotate"] or edit["flip"]:
            raise ValueError("it is an audio file with no picture")
        edit["audio_format"] = edit["audio_format"] or (
            Path(info["path"]).suffix.lower().lstrip(".") if Path(info["path"]).suffix.lower().lstrip(".")
            in AUDIO_FORMATS else "m4a")
    if edit["audio_format"]:
        for key, label in (("captions", "captions"), ("aspect", "a new shape"), ("height", "a new size")):
            if edit[key]:
                notes.append(f"ignored {label} (the result is only sound)")
                edit[key] = None
        if not info["has_audio"] and not edit["music"]:
            raise ValueError("it has no sound to extract")
    if edit["gif"] and edit["music"]:
        notes.append("a GIF has no sound, so no music")
        edit["music"] = None
    if edit["gif"] and edit["target_mb"]:
        notes.append("a GIF's size follows its width and frame rate; ask for a smaller width to shrink it")
        edit["target_mb"] = None
    if edit["order"]:
        for part in edit["order"]:
            if part != "main" and info["has_video"] and not edit["audio_format"] and not extras[part]["has_video"]:
                raise ValueError(f"{extras[part]['name']} has no picture, so it cannot be joined to a video "
                                 "(use it as music instead?)")
    if edit["mute"] and edit["volume"] != 1.0:
        edit["volume"] = 1.0
    if raw.get("unsupported"):
        notes.append(f"not done: {raw['unsupported']}")
    return edit, notes


# --- Measuring the source: silences and black bars --------------------------------------------

def _run_ff(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    ffmpeg = _bin("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(NO_FFMPEG)
    return subprocess.run([ffmpeg, "-hide_banner", "-nostdin", *args], capture_output=True, text=True,
                          timeout=timeout)


def speech_parts(path: str, segments: list[tuple[float, float]], threshold_db: float, min_silence: float,
                 pad: float = 0.15) -> list[tuple[float, float]]:
    """The parts of `segments` that are not silent (silencedetect), with a little padding."""
    out: list[tuple[float, float]] = []
    for a, b in segments:
        done = _run_ff(["-nostats", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", path, "-map", "0:a:0", "-af",
                        f"silencedetect=noise={threshold_db:g}dB:d={min_silence:g}", "-f", "null", "-"],
                       timeout=1800)
        silences, start = [], None
        for line in done.stderr.splitlines():
            m = re.search(r"silence_start: (-?[\d.]+)", line)
            if m:
                start = max(0.0, float(m.group(1)))
            m = re.search(r"silence_end: ([\d.]+)", line)
            if m and start is not None:
                silences.append((start, float(m.group(1))))
                start = None
        if start is not None:
            silences.append((start, b - a))
        keep = [(0.0, b - a)]
        for s, e in silences:
            keep = _subtract(keep, s + pad, e - pad)
        out += [(a + s, a + e) for s, e in keep if e - s > 0.12]
    return out


def black_bars(path: str, info: dict) -> tuple[int, int, int, int] | None:
    """(w, h, x, y) of the picture inside black bars, looked for at three points; None = no bars."""
    boxes = []
    for t in (info["duration"] * 0.2, info["duration"] * 0.5, info["duration"] * 0.8):
        done = _run_ff(["-nostats", "-ss", f"{t:.2f}", "-i", info["path"], "-frames:v", "12", "-vf",
                        "cropdetect=limit=24:round=2:reset=0", "-an", "-f", "null", "-"], timeout=120)
        found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", done.stderr)
        if found:
            boxes.append(tuple(int(v) for v in found[-1]))
    if not boxes:
        return None
    x1 = min(b[2] for b in boxes)
    y1 = min(b[3] for b in boxes)
    x2 = max(b[2] + b[0] for b in boxes)
    y2 = max(b[3] + b[1] for b in boxes)
    w, h = (x2 - x1) // 2 * 2, (y2 - y1) // 2 * 2
    if w >= info["width"] - 8 and h >= info["height"] - 8 or w < 32 or h < 32:
        return None
    return w, h, x1, y1


# --- The filter graph -----------------------------------------------------------------------

def _atempo(factor: float) -> list[str]:
    parts = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.4f}")
    return parts


def _shape(job: dict) -> tuple[int, int]:
    """The picture's size after crop_bars / rotate (what joined clips are fitted to)."""
    info, edit = job["info"], job["edit"]
    w, h = (job["bars"][0], job["bars"][1]) if job.get("bars") else (info["width"], info["height"])
    if edit["rotate"] in (90, 270):
        w, h = h, w
    return _even(w), _even(h)


def _final_size(job: dict) -> tuple[int, int]:
    edit = job["edit"]
    w, h = _shape(job)
    if edit["aspect"]:
        rw, rh = RATIOS[edit["aspect"]]
        short = min(w, h)
        w, h = (short, _even(short * rh / rw)) if rw <= rh else (_even(short * rw / rh), short)
    if edit["height"]:
        short = edit["height"]
        w, h = (short, _even(h * short / w)) if w < h else (_even(w * short / h), short)
    return w, h


def _own_audio(job: dict, audio_only: bool = False) -> bool:
    """The result carries the clips' own sound (not only music)."""
    info, edit, extras = job["info"], job["edit"], job["extras"]
    return not edit["gif"] and (audio_only or not edit["mute"]) and (
        info["has_audio"] or any(extras[p]["has_audio"] for p in edit["order"] or [] if p != "main"))


def build_graph(job: dict, audio_only: bool = False, captions_list: str = "",
                copy_video: bool = False) -> tuple[list[str], str, list[str]]:
    """-> (input args, filter graph, map args). Every value in it comes from checked numbers.
    copy_video: the picture is copied as it is (map 0:v:0), the graph is the sound only."""
    info, edit, extras = job["info"], job["edit"], job["extras"]
    segments = job["segments"]
    want_video = info["has_video"] and not edit["audio_format"] and not audio_only and not copy_video
    own_audio = _own_audio(job, audio_only)
    music_on = bool(edit["music"]) and not audio_only and not edit["gif"]
    want_audio = own_audio or music_on
    inputs: list[str] = []
    # The main video: seek straight to the first kept moment and stop after the last one.
    first, last = segments[0][0], segments[-1][1]
    whole = first <= 0.01 and last >= info["duration"] - 0.01
    if not whole:
        inputs += ["-ss", f"{first:.3f}", "-t", f"{last - first:.3f}"]
    inputs += ["-i", info["path"]]
    shifted = [(a - first, b - first) for a, b in segments]
    order = edit["order"] or ["main"]
    extra_input = {}
    for part in order:
        if part != "main":
            extra_input[part] = len(extra_input) + 1
            inputs += ["-i", extras[part]["path"]]
    n_inputs = 1 + len(extra_input)
    music_input = captions_input = None
    if music_on:
        music_input = n_inputs
        inputs += ["-stream_loop", "-1", "-i", extras[edit["music"]["extra"]]["path"]]
        n_inputs += 1
    if captions_list:
        captions_input = n_inputs
        inputs += ["-f", "concat", "-safe", "0", "-i", captions_list]
        n_inputs += 1

    chains: list[str] = []
    W, H = _shape(job)
    fps = info["fps"] if 1 <= info["fps"] <= 60 else 30
    joined = len(order) > 1
    main_audio = info["has_audio"]

    # Main pieces.
    pre = []
    if job.get("bars"):
        bw, bh, bx, by = job["bars"]
        pre.append(f"crop={bw}:{bh}:{bx}:{by}")
    pre += {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}.get(edit["rotate"], [])
    if edit["flip"]:
        pre.append("hflip" if edit["flip"] == "horizontal" else "vflip")
    if joined:
        pre += [f"scale={W}:{H}", "setsar=1", f"fps={fps:g}", "format=yuv420p"]
    single = len(shifted) == 1
    k = len(shifted)
    v_pieces, a_pieces = [], []
    if want_video:
        head = "[0:v:0]" + ",".join(pre or ["null"])
        if single:
            chains.append(head + "[mv0]")
        else:
            chains.append(head + f",split={k}" + "".join(f"[ms{i}]" for i in range(k)))
            for i, (a, b) in enumerate(shifted):
                chains.append(f"[ms{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[mv{i}]")
        v_pieces = [f"[mv{i}]" for i in range(k)]

    def norm(channels: int) -> str:
        """48 kHz stereo; a mono voice goes to both sides at full level (not the -3 dB of a plain upmix)."""
        mono = "pan=stereo|c0=c0|c1=c0," if channels == 1 else ""
        return f"{mono}aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    if own_audio:
        if main_audio:
            if single:
                length = shifted[0][1] - shifted[0][0]
                chains.append(f"[0:a:0]{norm(info['channels'])},apad,atrim=duration={length:.3f}[ma0]")
            else:
                chains.append(f"[0:a:0]{norm(info['channels'])},asplit={k}" + "".join(f"[as{i}]" for i in range(k)))
                for i, (a, b) in enumerate(shifted):
                    chains.append(f"[as{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,apad,"
                                  f"atrim=duration={b - a:.3f}[ma{i}]")
        else:
            for i, (a, b) in enumerate(shifted):
                chains.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={b - a:.3f}[ma{i}]")
        a_pieces = [f"[ma{i}]" for i in range(k)]

    # The whole timeline, in playing order.
    streams: list[tuple[str, str]] = []
    for part in order:
        if part == "main":
            streams += list(zip(v_pieces or [""] * k, a_pieces or [""] * k))
            continue
        n, e = extra_input[part], extras[part]
        v = a = ""
        if want_video:
            chains.append(f"[{n}:v:0]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:"
                          f"(oh-ih)/2,setsar=1,fps={fps:g},format=yuv420p[xv{n}]")
            v = f"[xv{n}]"
        if own_audio:
            source = f"[{n}:a:0]{norm(e['channels'])}" if e["has_audio"] else "anullsrc=r=48000:cl=stereo"
            chains.append(f"{source},apad,atrim=duration={e['duration']:.3f}[xa{n}]")
            a = f"[xa{n}]"
        streams.append((v, a))
    if len(streams) > 1:
        pads = "".join(v + a for v, a in streams)
        outs = ("[cv]" if want_video else "") + ("[ca]" if own_audio else "")
        if want_video or own_audio:
            chains.append(f"{pads}concat=n={len(streams)}:v={int(want_video)}:a={int(own_audio)}{outs}")
        vlabel, alabel = "[cv]", "[ca]"
    else:
        vlabel, alabel = streams[0]
    total = job["planned"]

    maps: list[str] = ["-map", "0:v:0"] if copy_video else []
    if want_video:
        vf = []
        if edit["speed"] != 1:
            vf.append(f"setpts=PTS/{edit['speed']:.4f}")
        cw, ch = W, H
        if edit["aspect"]:
            fw, fh = _final_size({**job, "edit": {**edit, "height": None}})
            rw, rh = RATIOS[edit["aspect"]]
            if edit["aspect_mode"] == "crop":
                if W / H > rw / rh:
                    cw, ch = _even(H * rw / rh), H
                else:
                    cw, ch = W, _even(W * rh / rw)
                vf += [f"crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})/2", f"scale={fw}:{fh}:flags=lanczos", "setsar=1"]
            elif edit["aspect_mode"] == "pad":
                vf += [f"scale={fw}:{fh}:force_original_aspect_ratio=decrease",
                       f"pad={fw}:{fh}:(ow-iw)/2:(oh-ih)/2:black", "setsar=1"]
            cw, ch = fw, fh
        body = ",".join(vf) or "null"
        if edit["aspect"] and edit["aspect_mode"] == "blur":
            chains.append(f"{vlabel}{body},split=2[bgi][fgi]")
            chains.append(f"[bgi]scale={cw}:{ch}:force_original_aspect_ratio=increase,crop={cw}:{ch},"
                          f"scale={_even(cw / 8)}:{_even(ch / 8)},boxblur=6:2,scale={cw}:{ch}[bg]")
            chains.append(f"[fgi]scale={cw}:{ch}:force_original_aspect_ratio=decrease[fg]")
            chains.append("[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v1]")
        else:
            chains.append(f"{vlabel}{body}[v1]")
        fw, fh = _final_size(job)
        chains.append(f"[v1]scale={fw}:{fh}:flags=lanczos,setsar=1[v2]")
        current = "[v2]"
        if captions_input is not None:
            chains.append(f"[{captions_input}:v]format=rgba[capv]")
            chains.append(f"{current}[capv]overlay=eof_action=pass:format=auto[v3]")
            current = "[v3]"
        fx = []
        if edit["fade_in"]:
            fx.append(f"fade=t=in:st=0:d={edit['fade_in']:.2f}")
        if edit["fade_out"]:
            fx.append(f"fade=t=out:st={max(0.0, total - edit['fade_out']):.3f}:d={edit['fade_out']:.2f}")
        if edit["gif"]:
            g = edit["gif"]
            fx += [f"fps={g['fps']}", f"scale={g['width']}:-2:flags=lanczos", "split[ga][gb]"]
            chains.append(f"{current}{','.join(fx)}")
            chains.append("[ga]palettegen=stats_mode=diff[pal]")
            chains.append("[gb][pal]paletteuse=dither=bayer:bayer_scale=4[vout]")
        else:
            fx.append("format=yuv420p")
            chains.append(f"{current}{','.join(fx)}[vout]")
        maps += ["-map", "[vout]"]
    if want_audio:
        af = []
        if edit["speed"] != 1:
            af += _atempo(edit["speed"])
        if edit["volume"] != 1 and not edit["mute"]:
            af.append(f"volume={edit['volume']:.3f}")
        current = ""
        if own_audio:
            chains.append(f"{alabel}{','.join(af) or 'anull'}[a1]")
            current = "[a1]"
        if music_input is not None:
            m = edit["music"]
            chains.append(f"[{music_input}:a:0]{norm(extras[m['extra']]['channels'])},"
                          f"volume={m['volume']:.3f},atrim=duration={total:.3f},"
                          f"afade=t=out:st={max(0.0, total - 2):.3f}:d=2[mus]")
            if current:
                chains.append(f"{current}[mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a2]")
                current = "[a2]"
            else:
                current = "[mus]"
        fx = []
        if edit["fade_in"] and not audio_only:
            fx.append(f"afade=t=in:st=0:d={edit['fade_in']:.2f}")
        if edit["fade_out"] and not audio_only:
            fx.append(f"afade=t=out:st={max(0.0, total - edit['fade_out']):.3f}:d={edit['fade_out']:.2f}")
        chains.append(f"{current}{','.join(fx) or 'anull'}[aout]")
        maps += ["-map", "[aout]"]
    return inputs, ";".join(chains), maps


# --- Rendering --------------------------------------------------------------------------------

def _ffmpeg_progress(args: list[str], total: float, state: dict, label: str, timeout: float = 6 * 3600) -> None:
    """Run ffmpeg with -progress on stdout; state['step'] shows the percentage."""
    ffmpeg = _bin("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(NO_FFMPEG)
    WORK.mkdir(parents=True, exist_ok=True)
    errors = open(state["folder"] / "ffmpeg.log", "w+")
    command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
               *args]
    log.info("ffmpeg: %s", " ".join(command)[:2000])
    started = time.time()
    with errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
        state["process"] = process
        for line in process.stdout:
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if m and total > 0:
                pct = min(99, int(int(m.group(1)) / 1e6 / total * 100))
                state["step"] = f"{label} {pct}%"
                state["percent"] = pct
            if time.time() - started > timeout:
                process.kill()
        code = process.wait()
        state.pop("process", None)
        if code != 0:
            errors.seek(0)
            detail = " ".join(errors.read().split())[-400:]
            raise RuntimeError(f"ffmpeg failed ({label}): {detail or 'exit ' + str(code)}")


def _video_codec(job: dict, bitrate_k: int | None) -> list[str]:
    if bitrate_k:
        return ["-c:v", "libx264", "-preset", "medium", "-b:v", f"{bitrate_k}k", "-maxrate", f"{int(bitrate_k * 1.3)}k",
                "-bufsize", f"{bitrate_k * 2}k", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]


def _budget(job: dict) -> dict | None:
    """Bitrates that fit target_mb, and a smaller picture when the bitrate is low."""
    target = job["edit"]["target_mb"]
    if not target:
        return None
    total_k = target * 8000 * 0.92 / max(job["planned"], 0.5)
    audio_k = 0 if not job["has_audio_out"] else 128 if total_k > 1500 else 96 if total_k > 600 else 64
    video_k = int(total_k - audio_k)
    if video_k < 60:
        raise ValueError(f"{_stamp(job['planned'])} of video cannot fit in {target:g} MB at a watchable quality; "
                         "cut it shorter or allow a bigger size")
    cap = None if video_k >= 4000 else 1080 if video_k >= 1800 else 720 if video_k >= 900 else \
        480 if video_k >= 450 else 360
    return {"video_k": video_k, "audio_k": audio_k, "cap": cap}


def _render(job: dict, state: dict, out: Path) -> None:
    edit, info = job["edit"], job["info"]
    copy_video = bool(job.get("copy_video"))
    inputs, graph, maps = build_graph(job, captions_list=job.get("captions_list", ""), copy_video=copy_video)
    codec: list[str] = []
    budget = job.get("budget")
    if edit["gif"]:
        codec = ["-loop", "0"]
    elif edit["audio_format"]:
        fmt = edit["audio_format"]
        codec = {"mp3": ["-c:a", "libmp3lame", "-q:a", "2"], "m4a": ["-c:a", "aac", "-b:a", "192k"],
                 "wav": ["-c:a", "pcm_s16le"]}[fmt]
    else:
        if copy_video:
            codec = ["-c:v", "copy"] + (["-tag:v", "hvc1"] if info["vcodec"] == "hevc" else [])
        else:
            codec = _video_codec(job, budget["video_k"] if budget else None)
        if not job["has_audio_out"]:
            codec += ["-an"]
        else:
            codec += ["-c:a", "aac", "-b:a", f"{budget['audio_k'] if budget else 160}k"]
        if job.get("srt") and edit["captions"] and edit["captions"]["mode"] == "soft":
            n = inputs.count("-i")
            inputs = inputs + ["-i", str(job["srt"])]
            maps = maps + ["-map", f"{n}:s:0"]
            codec += ["-c:s", "mov_text"]
            lang = _iso639(edit["captions"].get("language") if edit["captions"] else "")
            if lang:
                codec += ["-metadata:s:s:0", f"language={lang}"]
        codec += ["-movflags", "+faststart"]
    limit = ["-t", f"{job['planned'] + 0.3:.3f}"] if edit["music"] else []
    args = [*inputs, *(["-filter_complex", graph] if graph else []), *maps, *codec, *limit, str(out)]
    _ffmpeg_progress(args, job["planned"], state, "Rendering")


def _iso639(language: str) -> str:
    names = {"english": "eng", "spanish": "spa", "french": "fra", "german": "deu", "hindi": "hin",
             "italian": "ita", "portuguese": "por", "japanese": "jpn", "chinese": "zho", "korean": "kor",
             "arabic": "ara", "russian": "rus", "dutch": "nld"}
    return names.get(str(language or "").strip().lower(), "")


# --- Captions -------------------------------------------------------------------------------

def _speech_wav(job: dict, state: dict) -> Path:
    """The result's own sound (after cuts, joins and speed; before music) as 16 kHz mono - so the
    transcript's times are the result's times."""
    inputs, graph, maps = build_graph(job, audio_only=True)
    out = state["folder"] / "speech.wav"
    _ffmpeg_progress([*inputs, "-filter_complex", graph, *maps, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                      str(out)], job["planned"], state, "Preparing the sound")
    return out


def _translate(rows: list[dict], language: str) -> list[dict]:
    from mint.core import llm
    texts = [r["text"] for r in rows]
    answer = llm.ask_json(f"Translate each caption line into {language}. Keep the same number of lines, in order; "
                          'short, natural subtitles. Return JSON {"lines": [...]}.\n' + json.dumps(texts,
                                                                                                  ensure_ascii=False))
    lines = answer.get("lines") if isinstance(answer, dict) else None
    if not isinstance(lines, list) or len(lines) != len(rows):
        raise RuntimeError(f"could not translate the captions into {language}")
    return [{"s": r["s"], "text": str(t)} for r, t in zip(rows, lines)]


def cues(rows: list[dict], total: float, max_words: int = 7) -> list[tuple[float, float, str]]:
    """Transcript rows (start times only) -> short caption cues (start, end, text)."""
    out: list[tuple[float, float, str]] = []
    rows = sorted((r for r in rows if str(r.get("text", "")).strip()), key=lambda r: float(r["s"]))
    for i, row in enumerate(rows):
        text = re.sub(r"^(?:[A-Z]{1,2}|[A-Z][a-z]+(?: [A-Z][a-z]+)?):\s+", "", str(row["text"]).strip())
        words = text.split()
        if not words or re.fullmatch(r"[\[(][^\])]*(?:silence|pause|no speech|inaudible)[^\])]*[\])]", text, re.I):
            continue
        start = max(float(row["s"]), out[-1][1] if out else 0.0)
        following = float(rows[i + 1]["s"]) if i + 1 < len(rows) else total
        end = min(max(following, start + 0.8), start + max(1.2, 0.42 * len(words) + 0.6), total)
        if end - start < 0.3:
            continue
        pieces = math.ceil(len(words) / max_words)
        size = math.ceil(len(words) / pieces)
        chunks = [words[j:j + size] for j in range(0, len(words), size)]
        t = start
        for chunk in chunks:
            length = (end - start) * len(chunk) / len(words)
            out.append((round(t, 3), round(t + length, 3), " ".join(chunk)))
            t += length
    return out


def _font(size: int, text: str):
    from PIL import ImageFont
    wide = any(ord(ch) > 0x24F for ch in text)
    paths = (["/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"] if wide
             else []) + ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
                         "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"]
    for path in paths:
        try:
            font = ImageFont.truetype(path, size)
            try:
                font.set_variation_by_name("Bold")
            except Exception:
                pass
            return font
        except OSError:
            continue
    return ImageFont.load_default()


def _caption_image(text: str, width: int, height: int, path: Path) -> None:
    from PIL import Image, ImageDraw
    portrait = height > width
    size = max(14, int((width * 0.058) if portrait else (height * 0.052)))
    font = _font(size, text)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > width * 0.86:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    step = int(size * 1.3)
    bottom = height * (0.74 if portrait else 0.92)
    y = int(bottom - step * len(lines))
    stroke = max(2, size // 12)
    for text_line in lines:
        w = draw.textlength(text_line, font=font)
        x = int((width - w) / 2)
        pad = size // 4
        draw.rounded_rectangle((x - pad * 2, y - pad, x + w + pad * 2, y + step - pad // 2), radius=pad,
                               fill=(0, 0, 0, 140))
        draw.text((x, y), text_line, font=font, fill=(255, 255, 255, 255), stroke_width=stroke,
                  stroke_fill=(0, 0, 0, 255))
        y += step
    img.save(path, optimize=True)


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def make_captions(job: dict, state: dict) -> int:
    """Transcribe the result's sound, write the .srt and one transparent picture per cue. -> cue count."""
    from mint.tools import video
    wav = _speech_wav(job, state)
    state["step"] = "Transcribing for the captions"
    rows, how = video.transcribe(wav)
    wav.unlink(missing_ok=True)
    language = job["edit"]["captions"].get("language") or ""
    if rows and language and language.lower() not in {"english", "en", "original", "same"}:
        state["step"] = f"Translating the captions into {language}"
        rows = _translate(rows, language)
    items = cues(rows, job["planned"])
    job["caption_source"] = how
    if not items:
        return 0
    folder = state["folder"] / "captions"
    folder.mkdir(exist_ok=True)
    job["srt"] = state["folder"] / "captions.srt"
    job["srt"].write_text("\n".join(f"{n}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}\n"
                                    for n, (a, b, text) in enumerate(items, 1)))
    if job["edit"]["captions"]["mode"] == "soft":
        return len(items)
    width, height = _final_size(job)
    blank = folder / "blank.png"
    from PIL import Image
    Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(blank)
    state["step"] = "Drawing the captions"
    lines = ["ffconcat version 1.0"]
    t = 0.0
    job["cue_images"] = []

    def entry(path: Path, seconds: float):
        lines.append("file '" + str(path).replace("'", "'\\''") + "'")
        lines.append(f"duration {max(seconds, 0.001):.3f}")
    for n, (start, end, text) in enumerate(items, 1):
        if start > t + 0.001:
            entry(blank, start - t)
        png = folder / f"c{n:05d}.png"
        _caption_image(text, width, height, png)
        entry(png, end - start)
        job["cue_images"].append(((start + end) / 2, png))
        t = end
    entry(blank, max(0.05, job["planned"] - t + 1))
    lines.append("file '" + str(blank).replace("'", "'\\''") + "'")
    (folder / "list.ffconcat").write_text("\n".join(lines) + "\n")
    job["captions_list"] = str(folder / "list.ffconcat")
    return len(items)


def _captions_visible(job: dict, path: str) -> tuple[int, int]:
    """Burned captions really are in the picture: at the middle of up to three cues, the frame's
    pixels under the letters must be white and under their outline dark. -> (seen, checked)."""
    import numpy as np
    from PIL import Image
    images = job.get("cue_images") or []
    picks = [images[i] for i in sorted({0, len(images) // 2, len(images) - 1})] if images else []
    seen = 0
    for n, (t, png) in enumerate(picks):
        grab = job["state_folder"] / f"qa-cue-{n}.png"
        done = _run_ff(["-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", str(grab)],
                       timeout=60)
        if done.returncode != 0 or not grab.exists():
            continue
        cue = np.asarray(Image.open(png).convert("RGBA")).astype(int)
        frame = np.asarray(Image.open(grab).convert("L").resize((cue.shape[1], cue.shape[0]))).astype(int)
        solid = cue[:, :, 3] > 250
        white = solid & (cue[:, :, 0] > 250)
        black = solid & (cue[:, :, 0] < 5)
        if white.sum() > 20 and black.sum() > 20 and (frame[white] > 170).mean() > 0.6 \
                and (frame[black] < 90).mean() > 0.6:
            seen += 1
    return seen, len(picks)


# --- QA ---------------------------------------------------------------------------------------

def _frames(path: str, total: float, folder: Path) -> list:
    from PIL import Image
    shots = []
    for i, t in enumerate((min(0.2, total * 0.05), total * 0.5, max(0.0, total - max(0.25, total * 0.03)))):
        out = folder / f"qa-{i}.png"
        done = _run_ff(["-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-vf",
                        "scale=640:-2", str(out)], timeout=60)
        if done.returncode == 0 and out.exists():
            shots.append((t, Image.open(out).convert("RGB")))
    return shots


def qa(job: dict, out: Path) -> tuple[bool, str, str]:
    """Measure the result against the plan. -> (passed, one-line verdict, contact sheet path)."""
    edit = job["edit"]
    got = probe(str(out))
    problems, facts = [], []
    planned = job["planned"]
    tolerance = max(0.4, planned * 0.03) + (2 / edit["gif"]["fps"] if edit["gif"] else 0)
    if abs(got["duration"] - planned) <= tolerance:
        facts.append(f"{_stamp(got['duration'])} long as planned ({got['duration']:.1f} s)")
    else:
        problems.append(f"it is {got['duration']:.1f} s long, planned {planned:.1f} s")
    sheet_path = ""
    if edit["audio_format"]:
        if got["has_video"]:
            problems.append("it still has a picture")
        facts.append(f"{edit['audio_format']} audio")
    else:
        if not got["has_video"]:
            problems.append("the picture is missing")
        else:
            want = _final_size(job)
            if edit["gif"]:
                g = edit["gif"]
                want = (g["width"], _even(g["width"] * want[1] / want[0]))
            size_ok = abs(got["width"] - want[0]) <= 2 and abs(got["height"] - want[1]) <= 2
            shape = f"{got['width']}x{got['height']}"
            if edit["aspect"]:
                rw, rh = RATIOS[edit["aspect"]]
                if abs(got["width"] / got["height"] - rw / rh) > 0.02:
                    problems.append(f"the shape is {shape}, not {edit['aspect']}")
                else:
                    shape += f" ({edit['aspect']})"
            if not size_ok:
                problems.append(f"the picture is {got['width']}x{got['height']}, planned {want[0]}x{want[1]}")
            else:
                facts.append(shape)
            shots = _frames(str(out), got["duration"], job["state_folder"])
            if len(shots) < 3:
                problems.append("could not read frames from the start, middle and end")
            dark = [t for t, img in shots if sum(img.convert("L").resize((32, 18)).getdata()) / 576 < 6]
            if dark and len(dark) == len(shots) and not (edit["fade_in"] or edit["fade_out"]):
                problems.append("every checked frame is black")
            from mint.tools.video import contact_sheet
            sheet = contact_sheet(shots, width=1200)
            if sheet is not None:
                sheet_path = str(job["state_folder"] / "qa-sheet.jpg")
                sheet.save(sheet_path, quality=82)
        if not edit["gif"]:
            if job["has_audio_out"] and not got["has_audio"]:
                problems.append("the sound is missing")
            elif not job["has_audio_out"] and got["has_audio"]:
                problems.append("it still has sound")
            else:
                facts.append("sound present" if got["has_audio"] else "no sound (as asked)" if edit["mute"]
                             else "no sound")
    if edit["captions"]:
        if job.get("caption_count") and edit["captions"]["mode"] == "soft":
            if got["subtitles"]:
                facts.append(f"captions present (subtitle track, {job['caption_count']} lines)")
            else:
                problems.append("the subtitle track is missing")
        elif job.get("caption_count"):
            seen, checked = _captions_visible(job, str(out))
            if checked and seen == checked:
                facts.append(f"captions present ({job['caption_count']} lines, seen in {seen} checked frames)")
            else:
                problems.append(f"captions were seen in only {seen} of {checked} checked frames")
        else:
            problems.append("no speech was found to caption")
    mb = got["bytes"] / 1e6
    if edit["target_mb"]:
        if mb <= edit["target_mb"]:
            facts.append(f"{mb:.1f} MB (under {edit['target_mb']:g} MB)")
        else:
            problems.append(f"it is {mb:.1f} MB, over the {edit['target_mb']:g} MB target")
    else:
        facts.append(f"{mb:.1f} MB" if mb >= 1 else f"{got['bytes'] / 1e3:.0f} KB")
    job["result_info"] = got
    if problems:
        return False, "QA: FAILED - " + "; ".join(problems) + (" (ok: " + ", ".join(facts) + ")" if facts else ""), \
            sheet_path
    return True, "QA: " + ", ".join(facts), sheet_path


# --- The whole edit -------------------------------------------------------------------------

def _output_path(info: dict, edit: dict, name: str) -> Path:
    from mint.core import config
    ext = (".gif" if edit["gif"] else f".{edit['audio_format']}" if edit["audio_format"] else ".mp4")
    source = Path(info["path"])
    folder = source.parent
    if "/Library/" in str(folder) or not os.access(folder, os.W_OK):
        folder = config.storage("Videos")
    name = str(name or "").strip()
    if name:
        given = Path(os.path.expanduser(name))
        if given.is_absolute() and given.parent.is_dir() and str(given).startswith(str(HOME)):
            folder = given.parent
        stem = re.sub(r"[/\\:]", "-", given.stem if given.suffix.lower() in {ext, ".mp4", ".mov", ".gif", ".mp3",
                                                                            ".m4a", ".wav"} else given.name)
    else:
        stem = re.sub(r" \(edited(?: \d+)?\)$", "", source.stem) + " (edited)"
    candidate = folder / f"{stem}{ext}"
    n = 2
    with _lock:
        while candidate.exists() or str(candidate) in _reserved or candidate.resolve() == source.resolve():
            candidate = folder / f"{stem} {n}{ext}" if name else folder / f"{stem[:-1]} {n}){ext}"
            n += 1
        _reserved.add(str(candidate))
    return candidate


def _extras(paths) -> list[dict]:
    if isinstance(paths, str):
        paths = [p for p in re.split(r"\s*[\n;]\s*|,\s+(?=[~/])", paths) if p.strip()]
    out = []
    for text in paths or []:
        path, _how = find_source(str(text)) if str(text).strip() else ("", "")
        if path:
            out.append(probe(path))
    return out


def _edit(args: dict, state: dict) -> str:
    if _bin("ffmpeg") is None or _bin("ffprobe") is None:
        return NO_FFMPEG
    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        return "What should be changed? Say what to do (for example: cut the first 10 seconds)."
    source = str(args.get("source") or "").strip()
    extra = args.get("extra") or []
    state["step"] = "Finding the video"
    if not source and not extra and re.search(r"\b(join|combine|merge|stitch|concat)", instruction, re.I):
        selected = _finder_all()
        if len(selected) >= 2:
            source, extra = selected[0], selected[1:]
    try:
        path, how = find_source(source)
        extras = _extras(extra)
    except LookupError as error:
        return f"FAILED: {error}"
    info = probe(path)
    if info["duration"] <= 0.05:
        return f"FAILED: {info['name']} has no length (not a video?)."
    state["step"] = "Planning the edit"
    try:
        raw = plan(instruction, info, extras)
        edit, notes = validate(raw, info, extras)
    except ValueError as error:
        return (f"Could not plan that edit of {info['name']}: {error}. (The video: {_describe(info)}.) It can trim, "
                "cut, speed up, reshape, caption, remove silences, mute, add music, join, fade, compress, make GIFs "
                "and extract the audio.")
    except RuntimeError as error:
        return f"FAILED: could not plan the edit ({error})."
    job: dict = {"info": info, "edit": edit, "extras": extras, "notes": notes, "summary": raw.get("summary", "")}
    state["summary"] = job["summary"]
    _prune()
    folder = WORK / time.strftime("%Y%m%d-%H%M%S")
    n = 2
    while folder.exists():
        folder = WORK / (time.strftime("%Y%m%d-%H%M%S") + f"-{n}")
        n += 1
    folder.mkdir(parents=True)
    state["folder"] = job["state_folder"] = folder
    # What exactly is kept.
    segments = edit["segments"]
    if edit["silence"]:
        state["step"] = "Finding the silent parts"
        before = sum(b - a for a, b in segments)
        segments = speech_parts(path, segments, *edit["silence"])
        if not segments:
            return f"Could not do that: {info['name']} is silent all through at {edit['silence'][0]:g} dB."
        removed = before - sum(b - a for a, b in segments)
        notes.append(f"removed {removed:.1f} s of silence in {max(0, len(segments) - 1)} gaps")
    if edit["crop_bars"]:
        state["step"] = "Looking for black bars"
        job["bars"] = black_bars(path, info)
        if not job["bars"]:
            notes.append("found no black bars to crop")
    job["segments"] = segments
    extra_time = sum(extras[p]["duration"] for p in edit["order"] or [] if p != "main")
    job["planned"] = (sum(b - a for a, b in segments) + extra_time) / edit["speed"]
    job["has_audio_out"] = _own_audio(job) or (bool(edit["music"]) and not edit["gif"])
    job["budget"] = _budget(job)
    if job["budget"] and job["budget"]["cap"] and info["has_video"] and not edit["gif"]:
        short = min(_final_size(job))
        if job["budget"]["cap"] < short:
            edit["height"] = job["budget"]["cap"]
            notes.append(f"made it {job['budget']['cap']}p to fit {edit['target_mb']:g} MB")
    job["copy_video"] = (info["has_video"] and info["vcodec"] in {"h264", "hevc"} and not edit["audio_format"] and not edit["gif"] and len(segments) == 1
                         and segments[0][0] <= 0.01 and segments[0][1] >= info["duration"] - 0.01
                         and edit["speed"] == 1 and not any(edit[k] for k in ("aspect", "height", "captions", "order",
                                                                             "fade_in", "fade_out", "target_mb",
                                                                             "rotate", "flip"))
                         and not job.get("bars"))
    out = _output_path(info, edit, str(args.get("output") or ""))
    try:
        if edit["captions"]:
            job["caption_count"] = make_captions(job, state)
            if not job["caption_count"]:
                notes.append("no speech was found, so no captions")
                job.pop("captions_list", None)
                job.pop("srt", None)
        render = folder / f"render{out.suffix}"
        state["step"] = "Rendering"
        _render(job, state, render)
        state["step"] = "Checking the result"
        passed, verdict, sheet = qa(job, render)
        if not passed and edit["target_mb"] and job["result_info"]["bytes"] / 1e6 > edit["target_mb"]:
            ratio = edit["target_mb"] / (job["result_info"]["bytes"] / 1e6)
            job["budget"]["video_k"] = max(40, int(job["budget"]["video_k"] * ratio * 0.9))
            state["step"] = "Squeezing it a little more"
            _render(job, state, render)
            passed, verdict, sheet = qa(job, render)
        shutil.move(str(render), str(out))
        if job.get("srt"):
            shutil.copy(str(job["srt"]), str(out.with_suffix(".srt")))
    finally:
        with _lock:
            _reserved.discard(str(out))
    WORK.mkdir(parents=True, exist_ok=True)
    LAST.write_text(json.dumps({"path": str(out), "source": path, "when": time.time(), "instruction": instruction}))
    if args.get("open", True) not in (False, "false", "no", 0):
        subprocess.Popen(["open", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lines = [f"Edited {info['name']} ({how}): {str(job['summary'] or 'done').rstrip('.')}.",
             f"Saved as {out} - the original is unchanged."]
    if job.get("srt"):
        lines.append(f"Captions also saved as {out.with_suffix('.srt').name}.")
    if notes:
        lines.append("Notes: " + "; ".join(notes) + ".")
    lines.append(verdict + ("" if passed else " - tell the user what differs."))
    if sheet:
        lines.append(f"QA frames (start, middle, end): {sheet}")
    return "\n".join(lines)


def _prune(keep: int = 12) -> None:
    """Each edit leaves a small working folder (QA frames, caption pictures, the log): keep the newest few."""
    try:
        folders = sorted((p for p in WORK.iterdir() if p.is_dir() and re.fullmatch(r"\d{8}-\d{6}(?:-\d+)?", p.name)),
                         key=lambda p: p.name)
    except OSError:
        return
    for old in folders[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def _notify(text: str) -> None:
    try:
        from mint.tools.work import _notify_mint
        _notify_mint(text)
    except Exception as error:
        log.info("notify: %s", error)


def edit_video(args: dict) -> str:
    """The tool: answers within SYNC_WAIT seconds; longer edits finish in the background."""
    state: dict = {"step": "Starting", "started": time.time()}
    key = f"edit-{time.time():.3f}"

    def run():
        try:
            state["result"] = _edit(dict(args or {}), state)
        except Exception as error:
            log.exception("edit_video failed")
            state["result"] = f"FAILED: could not edit the video: {str(error)[:400]}"
        with _lock:
            _jobs.pop(key, None)
            late = state.get("late")
        if late:
            _notify("(Mint's video editor, not the user.) The video edit finished:\n" + state["result"]
                    + "\nTell the user in a sentence or two.")

    thread = threading.Thread(target=run, daemon=True, name="edit-video")
    with _lock:
        _jobs[key] = state
    thread.start()
    thread.join(SYNC_WAIT)
    with _lock:
        if thread.is_alive():
            state["late"] = True
            step, summary = state.get("step", ""), state.get("summary", "")
        else:
            step = ""
    if step:
        return (f"Editing it now{(' (' + summary + ')') if summary else ''} - {step.lower()}. It is a longer job, "
                "so it continues in the background; a message comes with the result and its QA check. Tell the "
                "user in a few words, then carry on.")
    return state["result"]


def snapshot() -> dict:
    """For the island: the edit running now - {"step", "percent", "seconds", "summary"} - else {}."""
    with _lock:
        states = list(_jobs.values())
    if not states:
        return {}
    s = states[0]
    return {"step": s.get("step", ""), "percent": s.get("percent", 0), "summary": s.get("summary", ""),
            "seconds": time.time() - s.get("started", time.time())}


def video_info(args: dict) -> str:
    if _bin("ffprobe") is None:
        return NO_FFMPEG
    try:
        path, how = find_source(str(args.get("source") or ""))
        info = probe(path)
    except (LookupError, RuntimeError) as error:
        return f"FAILED: {error}"
    return f"{info['name']} ({how}): {_describe(info)}. Path: {path}"


# --- Declarations ------------------------------------------------------------------------

PROMPT = """Video editing: "cut the first 10 seconds", "trim it to 0:30-1:45", "speed it up", "make it vertical \
for Reels", "add captions", "remove the silent parts", "mute it", "extract the audio", "compress it under 25 MB", \
"make a GIF of 0:10-0:15", "join these clips", "add this music quietly underneath", "rotate it", "export at 720p" \
-> edit_video with the user's words as `instruction` (no source = the video just edited, else the one selected in \
Finder, else the newest one; music or other clips go in `extra`). The original is never changed; the result is \
saved next to it and checked (QA) - tell the user the QA line. video_info = a video's length, size and resolution."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [
        types.FunctionDeclaration(
            name="edit_video",
            description=("Edit a video file by instruction: trim/cut parts, remove silent parts, speed up or slow "
                         "down, make it vertical (9:16) / square / 4:5, crop black bars, rotate or flip, resize "
                         "(720p...), burn in captions (transcribed), mute or change volume, add background music, "
                         "join clips, fade in/out, compress under a size, make a GIF of a part, or extract the "
                         "audio (mp3/m4a/wav). Saves '<name> (edited).mp4' next to the original (never "
                         "overwritten), opens it, and checks the result (QA: length, resolution, sound, captions, "
                         "size). Long edits finish in the background with a message."),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "instruction": types.Schema(type=S, description="what to do, in the user's own words"),
                "source": types.Schema(type=S, description=(
                    "video file path or name; empty or 'this' = the video just edited, else the one selected in "
                    "Finder, else the newest video; 'the last screen recording' works too")),
                "extra": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=S), description=(
                    "other files the edit uses: music to add, or clips to join after the source")),
                "output": types.Schema(type=S, description="optional name for the result"),
                "open": types.Schema(type=types.Type.BOOLEAN, description="open the result when done (default true)")},
                required=["instruction"])),
        types.FunctionDeclaration(
            name="video_info",
            description="A video's length, resolution, shape, frame rate, sound and file size.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "source": types.Schema(type=S, description="file path or name; empty = the video in front / "
                                                           "just edited")})),
    ]


HANDLERS = {"edit_video": edit_video, "video_info": video_info}
