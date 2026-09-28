"""Watching videos: understand a video fast and for few tokens.

A model could be shown the whole video, frame after frame, but that costs about
300 tokens for every second of it. Instead a video is boiled down to two small
things, the way a person skims one:

* what is SAID - a transcript with timestamps. The captions the site already
  has when there are any (YouTube: free and instant), otherwise the audio
  transcribed by a Flash model in parallel two-minute pieces (or on the Mac,
  when parakeet-mlx is installed).
* what it LOOKS like - a handful of keyframes picked where the picture changes
  (scene cuts), laid out with their times on one contact sheet, plus numbers
  measured on the Mac: cuts per minute, brightness, the main colours.

A Flash model reads those and writes the digest: what it is, the key moments
with timestamps, the look and feel, and the answer to the user's question. A
ten-minute talk comes to a few thousand tokens instead of about 180,000.

Every video is cached (~/Library/Application Support/Mint/videos/<id>/), so
follow-up questions, "show me the frame at 3:20" and "the video from earlier"
are instant, and a readable transcript is saved in ~/Documents/Mint/videos/.

Nothing new has to be installed for local files: frames come from AVFoundation
and audio from afconvert, both part of macOS. Web videos (YouTube, Vimeo, X,
Loom, TikTok...) are fetched with yt-dlp; if a YouTube video cannot be fetched,
Gemini watches it from its address at low resolution instead.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("mint.tools.video")

HOME = Path.home()
CACHE = HOME / "Library" / "Application Support" / "Mint" / "videos"
SAVED = HOME / "Documents" / "Mint" / "videos"     # default; _saved() follows Settings > Storage


def _saved() -> Path:
    from mint.core import config
    return config.storage("Videos")
INDEX = CACHE / "index.json"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpg", ".mpeg", ".3gp", ".m4a", ".mp3", ".wav",
             ".aac", ".flac", ".aiff", ".aif", ".caf", ".ogg", ".opus"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".aiff", ".aif", ".caf", ".ogg", ".opus"}
VIDEO_SITES = re.compile(r"youtube\.com|youtu\.be|vimeo\.com|loom\.com|x\.com|twitter\.com|tiktok\.com|"
                         r"instagram\.com|facebook\.com|fb\.watch|twitch\.tv|dailymotion\.com|reddit\.com|"
                         r"linkedin\.com|ted\.com|streamable\.com|\.mp4\b|\.mov\b|\.webm\b", re.I)
YOUTUBE = re.compile(r"(?:youtube\.com/(?:watch\?.*?v=|shorts/|live/|embed/)|youtu\.be/)([\w-]{11})", re.I)

# Audio sent to the transcriber at a time. Gemini's timestamps drift inside long audio (a
# 15-minute talk came back ending at 17:08); two-minute pieces start at exact offsets, so
# no time can be off by more than a few seconds.
CHUNK_SECONDS = 120
MAX_SECONDS = 4 * 3600       # longer than this: only the first four hours
SYNC_WAIT = 25.0             # the tool answers within this; longer jobs finish in the background
TRANSCRIBE_MODELS = [m for m in (os.environ.get("MINT_TRANSCRIBE_MODEL"), "gemini-3.5-flash-lite",
                                 "gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-3.5-flash",
                                 "gemini-3.7-flash") if m]
# Lite first: in testing the Flash models answered 503/504 after ~11 s each, and the
# lite digest was as good for this job (it only reads a transcript and one sheet).
DIGEST_MODELS = [m for m in (os.environ.get("MINT_VIDEO_MODEL"), "gemini-3.5-flash-lite", "gemini-3.7-flash",
                             "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-flash-lite-latest") if m]

_jobs: dict[str, tuple[dict, threading.Thread]] = {}
_lock = threading.Lock()


# --- Where the video is -----------------------------------------------------------------

def _stamp(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def parse_time(text: str) -> float | None:
    """'3:20', '1:02:03', '200', '3m20s', '90s' -> seconds."""
    text = str(text or "").strip().lower()
    if not text:
        return None
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}(\.\d+)?", text):
        total = 0.0
        for part in text.split(":"):
            total = total * 60 + float(part)
        return total
    m = re.fullmatch(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+(?:\.\d+)?)\s*s(?:ec)?)?", text)
    if m and any(m.groups()):
        h, mi, s = (float(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + s
    try:
        return float(text)
    except ValueError:
        return None


def _front_video_url() -> str:
    """The address of the tab in front, when it is a video page."""
    try:
        from mint.tools import harness as harness_tools
        app = harness_tools._pick_browser()
        if app is None:
            return ""
        url, _title = harness_tools._tab_info(app)
        return url if url and VIDEO_SITES.search(url) else ""
    except Exception as error:
        log.debug("front tab: %s", error)
        return ""


def _finder_selection() -> str:
    script = ('tell application "Finder" to if (count of (selection as list)) > 0 then '
              'return POSIX path of ((item 1 of (selection as list)) as alias)')
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=4)
        path = done.stdout.strip()
        return path if path and Path(path).suffix.lower() in VIDEO_EXT else ""
    except Exception:
        return ""


def _newest_local(days: float = 3) -> str:
    """The newest video in Desktop, Downloads or Movies (screen recordings land there)."""
    newest, when = "", time.time() - days * 86400
    for folder in (HOME / "Desktop", HOME / "Downloads", HOME / "Movies"):
        try:
            for entry in os.scandir(folder):
                if entry.is_file() and Path(entry.name).suffix.lower() in VIDEO_EXT - AUDIO_EXT:
                    mtime = entry.stat().st_mtime
                    if mtime > when:
                        newest, when = entry.path, mtime
        except OSError:
            continue
    return newest


def resolve(source: str) -> tuple[str, str]:
    """What the user means by `source` -> ("url", address) or ("file", path).
    Empty or 'this' means: the video page in front, else the video selected in
    Finder, else the newest video on the Desktop / in Downloads / Movies."""
    text = str(source or "").strip().strip('"').strip("'")
    if re.match(r"https?://", text, re.I) or re.match(r"(www\.)?(youtu\.be|youtube\.com)/", text, re.I):
        return "url", text if text.lower().startswith("http") else "https://" + text
    if text:
        path = Path(os.path.expanduser(text))
        if not path.is_absolute():
            for base in (HOME / "Desktop", HOME / "Downloads", HOME / "Movies", HOME / "Documents", HOME):
                if (base / text).exists():
                    path = base / text
                    break
        if path.exists() and path.is_file():
            return "file", str(path)
        if len(text.split()) > 3 or text.lower() in {"this", "that", "it", "the video", "this video", "that video"}:
            text = ""
        else:
            found = _spotlight_video(text)
            if found:
                return "file", found
    if not text:
        for finder in (_front_video_url, _finder_selection, _newest_local):
            found = finder()
            if found:
                return ("url" if found.startswith("http") else "file"), found
    raise LookupError("Could not find that video. Give a link, a file path, or open it in the browser first.")


def _spotlight_video(words: str) -> str:
    try:
        done = subprocess.run(["mdfind", "-onlyin", str(HOME), f"kMDItemContentTypeTree == 'public.movie' && "
                               f"kMDItemFSName == '*{words.replace(chr(39), '')}*'cd"],
                              capture_output=True, text=True, timeout=6)
        hits = [p for p in done.stdout.splitlines() if "/Library/" not in p]
        return max(hits, key=lambda p: os.path.getmtime(p)) if hits else ""
    except Exception:
        return ""


def _key(kind: str, where: str) -> str:
    if kind == "file":
        st = os.stat(where)
        where = f"{where}|{st.st_size}|{int(st.st_mtime)}"
    else:
        m = YOUTUBE.search(where)
        where = f"yt:{m.group(1)}" if m else where.split("#")[0]
    return hashlib.sha1(where.encode()).hexdigest()[:14]


# --- Fetching a web video -----------------------------------------------------------------

def _ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except ImportError:
        return None


def _info(url: str) -> dict:
    yt_dlp = _ytdlp()
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed")
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                           "socket_timeout": 20}) as ydl:
        return ydl.extract_info(url, download=False)


def _download(url: str, folder: Path, fmt: str, name: str) -> Path | None:
    yt_dlp = _ytdlp()
    options = {"quiet": True, "no_warnings": True, "noplaylist": True, "format": fmt, "socket_timeout": 20,
               "outtmpl": str(folder / f"{name}.%(ext)s"), "overwrites": True, "retries": 2,
               "max_filesize": 1_500_000_000, "noprogress": True}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except Exception as error:
        log.info("download %s (%s) failed: %s", name, fmt, str(error)[:200])
        return None
    found = [p for p in folder.glob(f"{name}.*") if p.suffix not in {".part", ".ytdl"}]
    return found[0] if found else None


def _caption_track(info: dict) -> tuple[str, str] | None:
    """(url, ext) of the best captions: the uploader's own before automatic ones, in
    the language the video is spoken in (YouTube marks its original automatic track
    '-orig'; the others are machine translations), English when that is unknown.
    The choice follows select_caption in bradautomates/claude-video (MIT)."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if v and k != "live_chat"}
    automatic = {k: v for k, v in (info.get("automatic_captions") or {}).items() if v and k != "live_chat"}
    originals = sorted(k for k in automatic if k.endswith("-orig"))
    spoken = (originals[0][:-5] if len(originals) == 1 else info.get("language") or "").split("-")[0]

    def rank(key: str):
        base = key.removesuffix("-orig").split("-")[0]
        return (bool(spoken) and base != spoken, not key.endswith("-orig"), base != "en", key)

    for pool in (manual, automatic):
        keys = sorted(pool, key=rank)
        if pool is automatic and spoken:
            keys = [k for k in keys if k.removesuffix("-orig").split("-")[0] == spoken] or keys
        for key in keys:
            tracks = {t.get("ext"): t.get("url") for t in pool[key] if t.get("url")}
            for ext in ("json3", "vtt"):
                if tracks.get(ext):
                    return tracks[ext], ext
    return None


def _captions(info: dict) -> list[dict]:
    track = _caption_track(info)
    if not track:
        return []
    url, ext = track
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(request, timeout=20).read().decode("utf-8", "replace")
    return _parse_json3(raw) if ext == "json3" else _parse_vtt(raw)


def _parse_json3(raw: str) -> list[dict]:
    out = []
    for event in json.loads(raw).get("events") or []:
        text = "".join(seg.get("utf8", "") for seg in event.get("segs") or []).replace("\n", " ").strip()
        if text:
            out.append({"s": event.get("tStartMs", 0) / 1000, "text": text})
    return _dedupe(out)


def _parse_vtt(raw: str) -> list[dict]:
    out = []
    for block in re.split(r"\n\s*\n", raw):
        m = re.search(r"(\d+:)?(\d\d):(\d\d)[.,](\d{3}) -->", block)
        if not m:
            continue
        start = int(m.group(1)[:-1] if m.group(1) else 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        lines = block[m.end():].split("\n")[1:]
        text = re.sub(r"<[^>]+>", "", " ".join(lines)).strip()
        if text:
            out.append({"s": float(start), "text": text})
    return _dedupe(out)


def _dedupe(rows: list[dict]) -> list[dict]:
    """Automatic captions repeat the previous line as the next one rolls in."""
    out: list[dict] = []
    for row in rows:
        text = " ".join(row["text"].split())
        if out and (text == out[-1]["text"] or out[-1]["text"].endswith(text)):
            continue
        if out and text.startswith(out[-1]["text"]):
            out[-1]["text"] = text
            continue
        out.append({"s": row["s"], "text": text})
    return out


# --- Frames (AVFoundation) ---------------------------------------------------------------

_warm_lock = threading.Lock()
_warmed = False


def _warm() -> None:
    """Look up the framework functions once, on one thread: PyObjC's lazy loader is
    not thread-safe (four threads asking for CGContextDrawImage at once raised KeyError)."""
    global _warmed
    with _warm_lock:
        if _warmed:
            return
        import AVFoundation as AV
        import CoreMedia as CM
        import Quartz
        for name in ("CGImageGetWidth", "CGImageGetHeight", "CGBitmapContextCreate", "CGColorSpaceCreateDeviceRGB",
                     "kCGImageAlphaNoneSkipLast", "CGContextDrawImage", "CGRectMake"):
            getattr(Quartz, name)
        for name in ("CMTimeMakeWithSeconds", "CMTimeGetSeconds"):
            getattr(CM, name)
        for name in ("AVAssetImageGenerator", "AVURLAsset", "AVMediaTypeVideo"):
            getattr(AV, name)
        _warmed = True


def _asset(path: str):
    _warm()
    import AVFoundation as AV
    from Foundation import NSURL
    return AV.AVURLAsset.URLAssetWithURL_options_(NSURL.fileURLWithPath_(path), None)


def _duration(path: str) -> float:
    import CoreMedia as CM
    return float(CM.CMTimeGetSeconds(_asset(path).duration()) or 0)


def _has_video(path: str) -> bool:
    import AVFoundation as AV
    return bool(_asset(path).tracksWithMediaType_(AV.AVMediaTypeVideo))


def _generator(asset, size: int, tolerance: float):
    import AVFoundation as AV
    import CoreMedia as CM
    g = AV.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
    g.setAppliesPreferredTrackTransform_(True)
    g.setMaximumSize_((size, size))
    t = CM.CMTimeMakeWithSeconds(tolerance, 600)
    g.setRequestedTimeToleranceBefore_(t)
    g.setRequestedTimeToleranceAfter_(t)
    return g


def _pixels(cg, w: int | None = None, h: int | None = None):
    import numpy as np
    import Quartz
    w = w or Quartz.CGImageGetWidth(cg)
    h = h or Quartz.CGImageGetHeight(cg)
    buf = bytearray(w * h * 4)
    ctx = Quartz.CGBitmapContextCreate(buf, w, h, 8, w * 4, Quartz.CGColorSpaceCreateDeviceRGB(),
                                       Quartz.kCGImageAlphaNoneSkipLast)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), cg)
    return np.frombuffer(bytes(buf), np.uint8).reshape(h, w, 4)[:, :, :3]


def _grab(generator, seconds: float):
    import CoreMedia as CM
    try:
        cg, _actual = generator.copyCGImageAtTime_actualTime_error_(CM.CMTimeMakeWithSeconds(seconds, 600), None, None)
        return cg
    except Exception:
        return None


def frame_at(path: str, seconds: float, size: int = 1024):
    """One exact frame as a PIL image."""
    from PIL import Image
    cg = _grab(_generator(_asset(path), size, 0.0), seconds)
    if cg is None:
        cg = _grab(_generator(_asset(path), size, 1.0), seconds)
    return Image.fromarray(_pixels(cg)) if cg is not None else None


def keyframes(path: str, duration: float, want: int = 12) -> tuple[list[float], dict]:
    """Times of `want` frames that together show the video - one per shot, the
    longest shots first - and measurements of its look (cuts per minute,
    brightness, colourfulness)."""
    import numpy as np
    probes = int(min(180, max(24, duration / 2)))
    times = [duration * (i + 0.5) / probes for i in range(probes)]
    asset = _asset(path)

    def work(chunk):
        g = _generator(asset, 96, duration / probes / 2)
        out = []
        for t in chunk:
            cg = _grab(g, t)
            out.append((t, _pixels(cg, 32, 18).astype(np.float32) if cg is not None else None))
        return out

    with ThreadPoolExecutor(4) as pool:
        got = sorted((x for part in pool.map(work, [times[i::4] for i in range(4)]) for x in part),
                     key=lambda x: x[0])
    got = [(t, p) for t, p in got if p is not None]
    if not got:
        return [], {}
    diffs = [0.0] + [float(np.abs(got[i][1] - got[i - 1][1]).mean()) for i in range(1, len(got))]
    typical = float(np.median(diffs[1:])) if len(diffs) > 2 else 0.0
    threshold = max(18.0, typical * 3)
    cuts = [i for i in range(1, len(got)) if diffs[i] > threshold]
    # Shots: runs of probes between cuts. One frame from the middle of each, longest shots first.
    bounds = [0] + cuts + [len(got)]
    shots = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1) if bounds[i + 1] > bounds[i]]
    shots.sort(key=lambda s: s[1] - s[0], reverse=True)
    chosen: list[int] = []
    for a, b in shots:
        mid = (a + b - 1) // 2
        if all(float(np.abs(got[mid][1] - got[c][1]).mean()) > 10 for c in chosen):
            chosen.append(mid)
        if len(chosen) >= want:
            break
    # Too few distinct shots (a talking head, a screen recording): fill evenly in time.
    if len(chosen) < min(want, 6):
        for i in np.linspace(0, len(got) - 1, min(want, len(got))).astype(int):
            if all(abs(int(i) - c) > len(got) / (want * 2) for c in chosen):
                chosen.append(int(i))
            if len(chosen) >= min(want, 8):
                break
    stack = np.stack([p for _, p in got])
    rgb = stack.reshape(-1, 3)
    mx, mn = rgb.max(axis=1), rgb.min(axis=1)
    look = {"cuts": len(cuts), "cuts_per_minute": round(len(cuts) / max(duration / 60, 0.5), 1),
            "brightness": int(rgb.mean() / 2.55), "colourfulness": int((mx - mn).mean() / 2.55),
            "detectable": probes >= duration / 4}
    return sorted(got[i][0] for i in chosen), look


def _palette(images, colours: int = 5) -> list[str]:
    from PIL import Image
    if not images:
        return []
    strip = Image.new("RGB", (160 * len(images), 90))
    for i, img in enumerate(images):
        strip.paste(img.resize((160, 90)), (160 * i, 0))
    q = strip.quantize(colors=colours, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()[:colours * 3]
    counts = sorted(q.getcolors(), reverse=True)
    return [f"#{pal[i * 3]:02X}{pal[i * 3 + 1]:02X}{pal[i * 3 + 2]:02X}" for _, i in counts[:colours]]


def contact_sheet(frames: list[tuple[float, object]], width: int = 1536):
    """The frames in a grid, each labelled with its time: one image instead of many."""
    from PIL import Image, ImageDraw, ImageFont
    if not frames:
        return None
    cols = 4 if len(frames) > 6 else min(3, len(frames))
    rows = -(-len(frames) // cols)
    tile_w = width // cols
    ratio = frames[0][1].height / frames[0][1].width
    tile_h = int(tile_w * ratio)
    sheet = Image.new("RGB", (tile_w * cols, tile_h * rows), "black")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNSMono.ttf", max(14, tile_w // 16))
    except OSError:
        font = ImageFont.load_default()
    for n, (t, img) in enumerate(frames):
        x, y = (n % cols) * tile_w, (n // cols) * tile_h
        sheet.paste(img.resize((tile_w, tile_h)), (x, y))
        label = _stamp(t)
        box = draw.textbbox((0, 0), label, font=font)
        draw.rectangle((x + 4, y + 4, x + 14 + box[2], y + 10 + box[3]), fill=(0, 0, 0))
        draw.text((x + 9, y + 6), label, fill=(255, 255, 255), font=font)
    return sheet


# --- Audio and transcription -------------------------------------------------------------

def _audio(path: str, folder: Path) -> Path | None:
    """16 kHz mono WAV with afconvert (part of macOS), ffmpeg as a fallback."""
    out = folder / "audio.wav"
    for command in (["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", path, str(out)],
                    [shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", path, "-vn", "-ac", "1",
                     "-ar", "16000", "-t", str(MAX_SECONDS), str(out)]):
        try:
            done = subprocess.run(command, capture_output=True, timeout=900)
            if done.returncode == 0 and out.exists() and out.stat().st_size > 4000:
                return out
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def _chunks(wav: Path, seconds: int = CHUNK_SECONDS) -> list[tuple[float, bytes, bool]]:
    """(start, wav bytes, has sound) pieces of the audio."""
    import numpy as np
    out = []
    with wave.open(str(wav)) as w:
        rate, total = w.getframerate(), w.getnframes()
        total = min(total, rate * MAX_SECONDS)
        start = 0
        while start < total:
            frames = w.readframes(min(rate * seconds, total - start))
            samples = np.frombuffer(frames, np.int16)
            loud = bool(samples.size) and float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) > 60
            buf = io.BytesIO()
            with wave.open(buf, "wb") as piece:
                piece.setnchannels(1)
                piece.setsampwidth(2)
                piece.setframerate(rate)
                piece.writeframes(frames)
            out.append((start / rate, buf.getvalue(), loud))
            start += rate * seconds
    return out


_TRANSCRIBE = """Transcribe the speech in this audio exactly as spoken, in its original language.
Return JSON: a list of {"s": <seconds from the start of THIS clip, a number>, "text": "<words>"}.
One item per sentence or short phrase (at most about 25 words), in order. When the speaker changes,
start a new item and begin its text with a short label like "A:" or "B:" (use names if they are said).
Where there is music or a notable sound and nobody speaks, add an item whose text is a few words in square
brackets describing it, e.g. "[upbeat synth music]" or "[crowd cheering]" - at most one per 30 seconds.
Silence: return []. Do not summarise, do not translate."""


from mint.core.llm import generate  # noqa: E402  (shared with the other background jobs)
from mint.core.llm import parse_json as _json  # noqa: E402


def _transcribe_gemini(wav: Path, on_progress=None) -> list[dict]:
    from google.genai import types
    pieces = _chunks(wav)
    done_count = [0]

    def one(piece):
        start, data, loud = piece
        if not loud:
            return []
        try:
            text, _model = generate([types.Part.from_bytes(data=data, mime_type="audio/wav"), _TRANSCRIBE],
                                    TRANSCRIBE_MODELS, json_mode=True)
            rows = _json(text) if text.strip() else []
        except Exception as error:
            # One piece failing should not lose the rest of the transcript.
            log.info("piece at %s: %s", _stamp(start), error)
            return [{"s": start, "text": f"[{_stamp(start)}-{_stamp(start + CHUNK_SECONDS)} could not be transcribed]"}]
        done_count[0] += 1
        if on_progress:
            on_progress(done_count[0], len(pieces))
        rows = [r for r in rows if isinstance(r, dict) and str(r.get("text", "")).strip()]
        length = len(data) / 32000
        times = [float(r.get("s") or 0) for r in rows]
        # Times past the end of the piece mean they ran fast: squeeze them back in.
        scale = min(1.0, (length - 2) / max(times)) if times and max(times) > length else 1.0
        return [{"s": start + t * scale, "text": str(r["text"]).strip()} for t, r in zip(times, rows)]

    with ThreadPoolExecutor(6) as pool:
        parts = list(pool.map(one, pieces))
    rows = [r for part in parts for r in part]
    return sorted(rows, key=lambda r: r["s"])


def _transcribe_local(wav: Path) -> list[dict] | None:
    """On the Mac, when parakeet-mlx is installed (optional; Apache-2.0, no torch):
    exact timestamps, no upload, no quota. English and 24 other European languages."""
    try:
        from parakeet_mlx import from_pretrained
    except ImportError:
        return None
    model = from_pretrained(os.environ.get("MINT_PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3"))
    result = model.transcribe(str(wav), chunk_duration=120, overlap_duration=15)
    return [{"s": float(s.start), "text": s.text.strip()} for s in result.sentences if s.text.strip()]


def transcribe(wav: Path, on_progress=None) -> tuple[list[dict], str]:
    if os.environ.get("MINT_TRANSCRIBE", "").lower() != "gemini":
        try:
            rows = _transcribe_local(wav)
            if rows is not None:
                return rows, "on this Mac (Parakeet)"
        except Exception as error:
            log.info("local transcription failed: %s", error)
    return _transcribe_gemini(wav, on_progress), "Gemini"


def transcript_text(rows: list[dict], every: float = 20.0) -> str:
    """Lines joined into paragraphs of about `every` seconds, each with its time:
    fewer timestamps, the same words."""
    lines, start, words = [], None, []
    for row in rows:
        if start is None:
            start = row["s"]
        if row["s"] - start >= every or (row["text"][:3].endswith(":") and words):
            lines.append(f"[{_stamp(start)}] {' '.join(words)}")
            start, words = row["s"], []
        words.append(row["text"])
    if words:
        lines.append(f"[{_stamp(start)}] {' '.join(words)}")
    return "\n".join(lines)


# --- Understanding ------------------------------------------------------------------------

_DIGEST = """You are watching a video for someone, from its transcript (timestamps in [m:ss]) and a
contact sheet of keyframes (each tile is labelled with its time). Write for a voice assistant that will
tell the user about it. Plain Markdown, at most {words} words:

**What it is** - one or two sentences: the kind of video, who, about what.
**Key moments** - 4 to 8 bullets, each starting with its timestamp, in order.
**Look and feel** - the visual style from the frames and the measurements: setting, people/objects,
shot types, on-screen text, colours, editing pace, mood, production quality. Concrete, not generic.
{question_part}
Only say what the transcript or frames show. If there is no speech, rely on the frames and say so."""


def _meta_text(meta: dict) -> str:
    bits = [f"Title: {meta.get('title') or '(none)'}", f"Length: {_stamp(meta.get('duration') or 0)}"]
    for key, label in (("uploader", "By"), ("upload_date", "Uploaded"), ("source", "Source")):
        if meta.get(key):
            bits.append(f"{label}: {meta[key]}")
    if meta.get("chapters"):
        bits.append("Chapters: " + "; ".join(f"{_stamp(c['s'])} {c['title']}" for c in meta["chapters"][:30]))
    if meta.get("description"):
        bits.append("Description: " + " ".join(meta["description"].split())[:700])
    look = meta.get("look") or {}
    if look:
        bits.append(f"Measured look: {look.get('cuts_per_minute')} cuts per minute ({look.get('cuts')} cuts), "
                    f"brightness {look.get('brightness')}/100, colourfulness {look.get('colourfulness')}/100, "
                    f"main colours {', '.join(meta.get('palette') or [])}")
    bits.append(f"Transcript: {meta.get('transcript_source') or 'none'}")
    return "\n".join(bits)


def _ask_model(folder: Path, meta: dict, prompt: str, words: int = 350) -> str:
    from google.genai import types
    transcript = (folder / "transcript.txt").read_text() if (folder / "transcript.txt").exists() else ""
    contents: list = [_meta_text(meta)]
    sheet = folder / "sheet.jpg"
    if sheet.exists():
        contents += ["Contact sheet of keyframes:", types.Part.from_bytes(data=sheet.read_bytes(), mime_type="image/jpeg")]
    contents += ["Transcript:\n" + (transcript or "(no speech)"), prompt]
    text, model = generate(contents, DIGEST_MODELS)
    log.info("video answer by %s (%d chars of transcript)", model, len(transcript))
    return text.strip()


def _watch_on_gemini(url: str, folder: Path, meta: dict, question: str) -> str:
    """YouTube only: Gemini watches the video from its address, at low resolution and
    one frame every five seconds - used when the video could not be fetched here."""
    from google.genai import types
    from mint.core.llm import client
    prompt = _DIGEST.format(words=400, question_part=_question_part(question)) + (
        "\n\nAfter the digest, add a section **Transcript** with the speech as lines '[m:ss] words', "
        "one line per 20 seconds or so.")
    last = None
    for model in DIGEST_MODELS[:3]:
        try:
            reply = client().models.generate_content(
                model=model,
                contents=types.Content(parts=[
                    types.Part(file_data=types.FileData(file_uri=url),
                               video_metadata=types.VideoMetadata(fps=0.2)),
                    types.Part(text=prompt)]),
                config=types.GenerateContentConfig(media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW))
            text = str(reply.text or "").strip()
            digest, _, transcript = text.partition("**Transcript**")
            (folder / "transcript.txt").write_text(transcript.strip(" :\n"))
            meta["transcript_source"] = "Gemini watched it from its address"
            return digest.strip()
        except Exception as error:
            last = error
            log.info("gemini direct %s: %s", model, str(error)[:160])
    raise RuntimeError(f"Gemini could not watch it either: {str(last)[:160]}")


def _question_part(question: str) -> str:
    if not question:
        return ""
    return (f"**Answer** - then answer this, citing timestamps: {question}\nIf it asks for feedback, suggestions "
            "or how to improve the video, be a sharp editor: 4-6 concrete, specific changes tied to timestamps "
            "(the hook in the first 3 seconds, pacing and cuts, clarity of the message, on-screen text and "
            "captions, audio and music, the ending and call to action) - not generic praise.")


# --- The whole job ------------------------------------------------------------------------

def _index() -> list[dict]:
    try:
        return json.loads(INDEX.read_text())
    except (OSError, ValueError):
        return []


def _write_json(path: Path, value) -> None:
    """Whole or not at all, readable only by the user (what was watched is private)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(value, indent=1, ensure_ascii=False))
    tmp.replace(path)


def _remember(key: str, meta: dict) -> None:
    with _lock:
        rows = [r for r in _index() if r.get("key") != key]
        rows.insert(0, {"key": key, "title": meta.get("title"), "source": meta.get("source"), "when": time.time()})
        _write_json(INDEX, rows[:50])


def _load(key: str) -> dict | None:
    try:
        return json.loads((CACHE / key / "meta.json").read_text())
    except (OSError, ValueError):
        return None


def _save_meta(folder: Path, meta: dict) -> None:
    _write_json(folder / "meta.json", meta)


def _safe_name(title: str) -> str:
    return (re.sub(r"[^\w\s.-]", "", title or "video").strip()[:70] or "video").replace("  ", " ")


def analyse(kind: str, where: str, question: str = "", frames: int = 12, progress=None) -> dict:
    """Fetch, transcribe, pick keyframes and write the digest. -> meta (cached)."""
    key = _key(kind, where)
    folder = CACHE / key
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    say = progress or (lambda text: None)
    meta: dict = {"key": key, "source": where, "kind": kind, "started": time.time()}
    local = where if kind == "file" else ""
    captions: list[dict] = []
    if kind == "url":
        say("Getting the video's details")
        try:
            info = _info(where)
            meta.update(title=info.get("title"), duration=info.get("duration"), uploader=info.get("uploader"),
                        upload_date=info.get("upload_date"), description=info.get("description"),
                        chapters=[{"s": c.get("start_time", 0), "title": c.get("title", "")}
                                  for c in info.get("chapters") or []])
            try:
                captions = _captions(info)
            except Exception as error:
                log.info("captions: %s", error)
            if (info.get("duration") or 0) <= MAX_SECONDS:
                say("Downloading a small copy to look at")
                video_fmt = ("bv*[vcodec^=avc1][height<=480]/bv*[ext=mp4][height<=480]/b[ext=mp4][height<=480]/"
                             "bv*[vcodec^=avc1]/b[ext=mp4]/b")
                got = _download(where, folder, video_fmt, "video")
                local = str(got) if got else ""
                if not captions:
                    audio = _download(where, folder, "ba[ext=m4a]/ba[acodec^=mp4a]/ba/b", "audio")
                    if audio is not None:
                        meta["audio_file"] = str(audio)
        except Exception as error:
            log.info("yt-dlp: %s", str(error)[:300])
            meta["fetch_error"] = str(error)[:300]
        if not local and not meta.get("audio_file"):
            if YOUTUBE.search(where):
                say("Asking Gemini to watch it from its address")
                meta["digest"] = _watch_on_gemini(where, folder, meta, question)
                meta["done"] = time.time()
                _finish(folder, meta)
                return meta
            raise RuntimeError("Could not download that video" + (f": {meta['fetch_error']}" if meta.get("fetch_error")
                                                                  else ". Is yt-dlp installed?"))
    else:
        meta.update(title=Path(where).stem, duration=_duration(where))
    if local:
        meta["file"] = local
        meta["duration"] = meta.get("duration") or _duration(local)
    duration = float(meta.get("duration") or 0)

    # Pictures and sound at the same time.
    def pictures():
        if not local or not _has_video(local):
            return
        say("Picking the keyframes")
        times, look = keyframes(local, duration, frames)
        shots = []
        for t in times:
            img = frame_at(local, t, 640)
            if img is not None:
                shots.append((t, img))
                img.save(folder / f"frame-{int(t):05d}.jpg", quality=82)
        sheet = contact_sheet(shots)
        if sheet is not None:
            sheet.save(folder / "sheet.jpg", quality=80)
        meta.update(look=look, frames=[t for t, _ in shots], palette=_palette([img for _, img in shots]))

    def sound():
        if captions:
            meta["transcript_source"] = "the video's captions"
            return captions
        source = meta.get("audio_file") or local
        if not source:
            return []
        wav = _audio(source, folder)
        if wav is None:
            meta["transcript_source"] = "none (no audio track)"
            return []
        say("Listening to it")
        rows, how = transcribe(wav, lambda n, total: say(f"Listening: part {n} of {total}") if total > 1 else None)
        meta["transcript_source"] = f"transcribed {how}" if rows else "none (no speech found)"
        wav.unlink(missing_ok=True)
        return rows

    with ThreadPoolExecutor(2) as pool:
        pics, rows_f = pool.submit(pictures), pool.submit(sound)
        pics.result()
        rows = rows_f.result()
    (folder / "transcript.json").write_text(json.dumps(rows, ensure_ascii=False))
    (folder / "transcript.txt").write_text(transcript_text(rows))
    meta["words"] = sum(len(r["text"].split()) for r in rows)
    say("Writing it up")
    meta["digest"] = _ask_model(folder, meta, _DIGEST.format(words=350, question_part=_question_part(question)))
    meta["done"] = time.time()
    _finish(folder, meta)
    return meta


def _finish(folder: Path, meta: dict) -> None:
    _save_meta(folder, meta)
    _remember(meta["key"], meta)
    try:
        out = _saved() / f"{_safe_name(meta.get('title'))}.md"
        transcript = (folder / "transcript.txt").read_text() if (folder / "transcript.txt").exists() else ""
        out.write_text(f"# {meta.get('title') or 'Video'}\n\nSource: {meta.get('source')}\n\n"
                       f"{meta.get('digest', '')}\n\n## Transcript ({meta.get('transcript_source', 'none')})\n\n"
                       f"{transcript or '(no speech)'}\n")
        meta["saved"] = str(out)
        _save_meta(folder, meta)
    except OSError as error:
        log.info("saving transcript: %s", error)


def _prune(keep: int = 6) -> None:
    """A web video's download is only a working copy (for exact frames): keep the
    newest few; the keyframes, transcript and notes of every video stay."""
    for row in _index()[keep:]:
        for p in (CACHE / row["key"]).glob("video.*"):
            p.unlink(missing_ok=True)
        for p in (CACHE / row["key"]).glob("audio.*"):
            p.unlink(missing_ok=True)


# --- The tool -----------------------------------------------------------------------------

def _pick_cached(source: str) -> dict | None:
    """'' -> the video watched last; otherwise one watched before whose title or
    address matches the words."""
    rows = _index()
    if not rows:
        return None
    if not source:
        return _load(rows[0]["key"])
    words = source.lower()
    for row in rows:
        if words in (row.get("title") or "").lower() or words in (row.get("source") or "").lower():
            return _load(row["key"])
    return None


def _report(meta: dict, question: str = "") -> str:
    head = (f"Watched '{meta.get('title') or 'the video'}' ({_stamp(meta.get('duration') or 0)}; "
            f"transcript: {meta.get('transcript_source', 'none')}; {len(meta.get('frames') or [])} keyframes).")
    tail = (f"\nSaved the transcript and notes to {meta['saved']}." if meta.get("saved") else "")
    tail += ("\nFor more: watch_video again with a `question` (answered from the transcript and frames, with "
             "timestamps), or `at` a time to see that exact frame.")
    return f"{head}\n\n{meta.get('digest', '')}{tail}"


def _frame_image(meta: dict, at: float):
    folder = CACHE / meta["key"]
    img = None
    if meta.get("file") and Path(meta["file"]).exists():
        img = frame_at(meta["file"], at, 1024)
    if img is None:
        near = sorted(folder.glob("frame-*.jpg"), key=lambda p: abs(int(p.stem.split("-")[1]) - at))
        if near:
            from PIL import Image
            img = Image.open(near[0])
            at = float(near[0].stem.split("-")[1])
    return img, at


def _encode(img) -> dict:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    return {"mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}


def answer(meta: dict, question: str) -> str:
    folder = CACHE / meta["key"]
    prompt = ("Answer the user's question about this video from the transcript and keyframes, in at most 150 "
              "words, citing timestamps [m:ss] for where things happen. If the answer is not in them, say so. "
              f"Question: {question}")
    return _ask_model(folder, meta, prompt)


def _notify(text: str) -> None:
    try:
        from mint.tools.work import _notify_mint
        _notify_mint(text)
    except Exception as error:
        log.info("notify: %s", error)


def tool(args: dict) -> tuple[str, dict | None]:
    """watch_video: -> (text for the model, an image or None)."""
    source = str(args.get("source") or "").strip()
    question = str(args.get("question") or "").strip()
    at = parse_time(str(args.get("at") or ""))
    fresh = bool(args.get("fresh"))
    frames = int(args.get("frames") or 12)
    frames = max(4, min(frames, 24))

    meta = None
    if not source and (question or at is not None):
        source = _front_video_url()      # "what did he just say?" is about the video on screen, if there is one
    if not fresh and (question or at is not None) and (not source or not re.match(r"https?://|/|~", source)):
        meta = _pick_cached(source)
    try:
        if meta is None:
            kind, where = resolve(source)
            meta = None if fresh else _load(_key(kind, where))
            if meta is not None and not meta.get("digest"):
                meta = None
        else:
            kind, where = meta["kind"], meta["source"]
    except LookupError as error:
        return f"FAILED: {error}", None
    except OSError as error:
        return f"FAILED: could not open that file: {error}", None

    if meta is not None:
        _remember(meta["key"], meta)
        if at is not None:
            img, shown = _frame_image(meta, at)
            if img is None:
                return f"FAILED: no frame at {_stamp(at)} (the video file is gone).", None
            said = _said_near(meta, shown)
            return (f"The frame at {_stamp(shown)} of '{meta.get('title')}' was just sent to you as an image."
                    + (f" Said around then: {said}" if said else "")), _encode(img)
        if question:
            return answer(meta, question), None
        return "Already watched this one:\n" + _report(meta), None

    # A new video: run the job, answer within SYNC_WAIT, otherwise finish in the background.
    state, running = _start_job(kind, where, question, frames)
    if running is None:
        return "Still watching that video; you will get a message when it is done.", None
    running.join(SYNC_WAIT)
    with _lock:
        if running.is_alive():
            state["late"] = True       # under the lock: the job checks it under the same lock when it ends
            step = state["step"].lower()
        else:
            step = ""
    if step:
        return (f"Watching it now ({step}). It is a longer one, so it continues in the background: "
                "you will get a message with what it is about the moment it is done. Tell the user in a few words, "
                "then carry on."), None
    if state.get("error"):
        return f"FAILED: could not watch the video: {state['error']}", None
    return _report(state["meta"], question), None


def _start_job(kind: str, where: str, question: str, frames: int = 12, notify: bool = True):
    """One job per video at a time (Mint and the agents share them). -> (state, thread),
    or (state, None) when that video is already being watched."""
    job_key = _key(kind, where)
    state: dict = {"step": "Starting"}

    def progress(text):
        state["step"] = text
        log.info("video %s: %s", job_key, text)

    def run():
        try:
            state["meta"] = analyse(kind, where, question, frames, progress)
        except Exception as error:
            log.exception("watch_video failed")
            state["error"] = str(error)[:400]
            # A failed job is in no index, so nothing else would ever remove its downloads.
            for pattern in ("video.*", "audio.*"):
                for p in (CACHE / job_key).glob(pattern):
                    p.unlink(missing_ok=True)
        finally:
            with _lock:
                _jobs.pop(job_key, None)
                late = state.get("late")
            _prune()
            if late and notify:
                if state.get("meta"):
                    asked = (f" The user asked: {question!r} - answer that from the **Answer** part." if question
                             else " Tell the user the gist in a few sentences.")
                    _notify("(Mint's video watcher, not the user.) Finished watching the video:\n"
                            + _report(state["meta"], question) + "\n" + asked)
                else:
                    _notify(f"(Mint's video watcher, not the user.) Could not watch the video: {state.get('error')}")

    with _lock:
        if job_key in _jobs:
            return _jobs[job_key][0], None
        thread = threading.Thread(target=run, daemon=True, name="watch-video")
        _jobs[job_key] = (state, thread)
        thread.start()
    return state, thread


def _said_near(meta: dict, at: float, span: float = 12.0) -> str:
    try:
        rows = json.loads((CACHE / meta["key"] / "transcript.json").read_text())
    except (OSError, ValueError):
        return ""
    near = [r["text"] for r in rows if at - span <= r["s"] <= at + span]
    return " ".join(near)[:400]


def watch_for_agent(source: str, question: str = "", workspace: Path | None = None) -> str:
    """For sub-agents: the whole job, blocking, as text (digest + transcript). Agents may
    watch web videos and files in their own workspace - not the user's files, Finder
    selection or screen recordings, which would go to the agent's model provider."""
    source = str(source or "").strip()
    if re.match(r"https?://", source, re.I):
        kind, where = "url", source
    else:
        if workspace is None or not source:
            return "watch_video needs a link (https://...) or a file in your workspace."
        path = (workspace / source.lstrip("/")).resolve()
        if workspace.resolve() not in path.parents or not path.is_file():
            return f"No video '{source}' in your workspace. Give a link, or a file in your workspace."
        kind, where = "file", str(path)
    meta = _load(_key(kind, where))
    if meta is None or not meta.get("digest"):
        state, thread = _start_job(kind, where, question, notify=False)
        if thread is None:                     # Mint is already watching it: wait for that job
            with _lock:
                running = _jobs.get(_key(kind, where))
            if running is not None:
                running[1].join(1800)
            meta = _load(_key(kind, where))
            if meta is None:
                return "Could not watch that video."
        else:
            thread.join(1800)
            if state.get("error") or not state.get("meta"):
                return f"Could not watch that video: {state.get('error', 'it took too long')}"
            meta = state["meta"]
    elif question:
        meta = dict(meta, digest=meta["digest"] + "\n\n**Answer**\n" + answer(meta, question))
    transcript = (CACHE / meta["key"] / "transcript.txt").read_text() if (CACHE / meta["key"] / "transcript.txt").exists() else ""
    return _report(meta) + "\n\nTranscript:\n" + (transcript[:24000] or "(no speech)")


# --- Declarations --------------------------------------------------------------------------

PROMPT = """Videos: to watch, summarise, transcribe or answer anything about a video - a link, a file, \
"this video" in the browser, a screen recording - call watch_video (no source = the video in front, else \
the one selected in Finder, else the newest recording). It reads the transcript and a few keyframes, so it \
is fast; never play a video and look at it frame by frame instead. Follow-up questions about the same \
video: watch_video with `question` (answered with timestamps), or `at` a time to see that frame. Long \
videos finish in the background and a message arrives when they are done. ALWAYS put what the user wants in \
`question` (a summary, feedback, suggestions to improve it, the aesthetic, a specific detail) - the first call \
too. Any later question about a video (suggestions, "what about the ending?") is a new watch_video call with \
that `question`: never answer it from the earlier summary alone."""


def declarations():
    from google.genai import types

    def schema(kind, description):
        return types.Schema(type=kind, description=description)
    return [types.FunctionDeclaration(
        name="watch_video",
        description=("Watch and understand a video in seconds: a YouTube / X / Vimeo / Loom / TikTok / Instagram "
                     "link, a video or audio file, or - with no source - the video page in front, the video "
                     "selected in Finder, or the newest screen recording. Returns what it is, the key moments "
                     "with timestamps, the look and feel (style, colours, pace, mood) and the answer to "
                     "`question`, and saves the full transcript in Mint's Videos folder. For a video watched "
                     "before (source empty = the last one), `question` is answered from its transcript and "
                     "keyframes and `at` shows you the exact frame at that time."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "source": schema(types.Type.STRING, "link, file path or file name; empty = the video in front / "
                                                "the last one watched"),
            "question": schema(types.Type.STRING, "what the user wants from it, in their words: summary, feedback, "
                                                  "improvement suggestions, the aesthetic, a detail"),
            "at": schema(types.Type.STRING, "a time like '3:20' to see that frame (optional)"),
            "frames": schema(types.Type.INTEGER, "keyframes to look at, 4-24 (default 12; more for a "
                                                 "visual/aesthetic question)"),
            "fresh": schema(types.Type.BOOLEAN, "watch again even if it was watched before")}))]
