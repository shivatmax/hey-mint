"""Dictation, Wispr-Flow style: hold a key, talk, let go - what you said is typed where your
cursor is, cleaned up the way you meant to write it.

* Hold the dictation key (Right ⌥ by default, Settings ▸ Shortcuts) and talk; let go to paste.
  Or tap it twice to dictate hands-free, and tap once more to finish. Esc or ✕ throws it away.
* The words come from Mint's own microphone stream (already open, echo-cancelled), so it
  starts instantly; while you dictate Mint itself does not listen or answer.
* Gemini writes it down as you meant it: punctuation and capitals, no "um"s, your
  corrections applied ("Friday - no, Thursday" -> "Thursday"), lists as lists, and the
  language you spoke (Hindi, English or both, in the script you'd write them in). It knows
  the app you're typing into (a chat gets a chat message, Mail gets an email) and the words in
  your dictionary (Settings, "dictation_words").
* The text is pasted at the cursor and your clipboard is put back. The last dictations are
  kept on this Mac ("paste my last dictation" if it landed nowhere).

The island shows it: a waveform while you talk, "Writing…", then "✓ Pasted".
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
import wave
from pathlib import Path

log = logging.getLogger("mint.voice.dictation")

RATE = 16000
HEAR = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash"]      # fast first: ~2 s
TIDY = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash"]
HISTORY = Path.home() / "Library" / "Application Support" / "Mint" / "dictations.jsonl"
MAX_SECONDS = 6 * 60

_lock = threading.RLock()
_state: dict = {"mode": "idle"}        # idle | recording | writing | done | failed
_levels: list[float] = []
RING_SECONDS = 0.5                     # what the mic heard just before the key went down


class Capture:
    """The microphone from the instant a key goes down, for dictation and hold-to-talk.

    Seen 10 Oct: the first words were lost (recording began only once the hold was sure, 0.28 s in) and, with
    Mint paused, its audio engine was off and restarting it gave silence for a moment ("didn't hear anything").
    Now: begin() at key-down, with the last half second Mint's engine heard; Mint's own echo-cancelled stream
    when it is running and real; otherwise a plain microphone stream opened at once (~0.1 s)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.on = False
        self.chunks: list[bytes] = []
        self.real_at = 0.0                 # last real (not all-zero) frame from Mint's engine
        self.stream = None
        self.began = 0.0
        self.lock = threading.RLock()

    def begin(self) -> bool:
        """Start (again: nothing). False if it was already on."""
        with self.lock:
            if self.on:
                return False
            self.on, self.stream, self.real_at = True, None, 0.0
            self.began = time.monotonic()
            self.chunks = list(_ring)
        threading.Thread(target=self._plain_mic_if_needed, daemon=True, name=f"{self.name}-mic").start()
        return True

    def feed(self, pcm: bytes, plain: bool = False) -> None:
        with self.lock:
            if not self.on:
                return
            if not plain:
                if self.stream is not None or not any(pcm):
                    return                 # the plain mic took over, or the engine is still starting (zeros)
                self.real_at = time.monotonic()
            self.chunks.append(pcm)

    def end(self) -> bytes:
        with self.lock:
            self.on = False
            pcm, self.chunks = b"".join(self.chunks), []
        return pcm

    def seconds(self) -> float:
        with self.lock:
            return sum(map(len, self.chunks)) / 2 / RATE

    def _plain_mic_if_needed(self) -> None:
        time.sleep(0.1)
        with self.lock:
            if not self.on or self.real_at:
                return                     # Mint's engine is feeding real sound
        try:
            import pyaudio
            audio = pyaudio.PyAudio()
            stream = audio.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True, frames_per_buffer=800)
        except Exception as error:
            log.info("%s microphone: %s", self.name, error)
            return
        with self.lock:
            if not self.on or self.real_at:
                stream.close()
                audio.terminate()
                return
            self.stream = stream
            self.chunks = [c for c in self.chunks if any(c)]
        log.info("%s: plain microphone (Mint's audio engine is off or starting)", self.name)
        try:
            while self.on:
                pcm = stream.read(800, exception_on_overflow=False)
                self.feed(pcm, plain=True)
                if self is _rec:
                    _level(pcm)
        finally:
            stream.stop_stream()
            stream.close()
            audio.terminate()


_ring: list[bytes] = []


def ring(pcm: bytes) -> None:
    """Every frame of Mint's own microphone stream (session): the last RING_SECONDS, for a capture's start."""
    _ring.append(pcm)
    while len(_ring) > 1 and sum(map(len, _ring)) > RATE * 2 * RING_SECONDS:
        _ring.pop(0)


_rec = Capture("dictation")
talk = Capture("hold-to-talk")            # session.hold_to_talk


def _level(pcm: bytes) -> None:
    import numpy as np
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    with _lock:
        _levels.append(float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0)
        del _levels[:-120]


# --- state for the island ----------------------------------------------------------------------

def snapshot() -> dict:
    """{"mode", "seconds", "levels", "hands_free", "text", "error"} for the island ({} when idle)."""
    with _lock:
        mode = _state.get("mode", "idle")
        if mode == "idle":
            return {}
        if mode in ("done", "failed") and time.time() - _state.get("ended", 0) > 1.8:
            _state.update(mode="idle")
            return {}
        return {"mode": mode, "seconds": time.time() - _state.get("started", time.time()),
                "levels": list(_levels[-24:]), "hands_free": _state.get("hands_free", False),
                "text": _state.get("text", ""), "error": _state.get("error", "")}


def capturing() -> bool:
    """The dictation key is down (or hands-free is on): Mint's stream is for the dictation, not for Mint."""
    return _rec.on


# --- recording --------------------------------------------------------------------------------

def feed(pcm: bytes) -> None:
    """16 kHz 16-bit mono from the session's microphone while the key is down."""
    if not _rec.on:
        return
    _rec.feed(pcm)
    if _rec.stream is None:
        _level(pcm)
    if _state.get("mode") == "recording" and time.time() - _state.get("started", time.time()) > MAX_SECONDS:
        threading.Thread(target=finish, daemon=True).start()


def prime() -> None:
    """The dictation key went down: record from now, quietly - it may still turn out to be a tap or a shortcut."""
    with _lock:
        if _state.get("mode") in ("recording", "writing"):
            return
    if _rec.begin():
        with _lock:
            _levels.clear()


def unprime() -> None:
    """It was a tap or a shortcut, not a hold: throw the quiet recording away."""
    with _lock:
        if _state.get("mode") == "recording":
            return
    _rec.end()


def start(hands_free: bool = False) -> None:
    with _lock:
        if _state.get("mode") in ("recording", "writing"):
            return
        if not _rec.on:
            _levels.clear()
        _state.clear()
        _state.update(mode="recording", started=time.time(), hands_free=hands_free, app=_front_app())
    _rec.begin()                       # (already on since the key went down: keeps what it has)
    print(f"  {time.strftime('%H:%M:%S')} [dictation: listening{' (hands-free)' if hands_free else ''}]", flush=True)


def cancel() -> None:
    with _lock:
        recording = _state.get("mode") == "recording"
        if recording:
            _state.update(mode="idle")
    _rec.end()
    if recording:
        print(f"  {time.strftime('%H:%M:%S')} [dictation: cancelled]", flush=True)


def finish() -> None:
    """Stop recording, write it down, paste it. Runs the slow part on its own thread."""
    with _lock:
        if _state.get("mode") != "recording":
            return
        _state.update(mode="writing", stopped=time.time())
    pcm = _rec.end()
    threading.Thread(target=_write, args=(pcm,), daemon=True, name="dictation").start()


def toggle() -> None:
    """Hands-free: the first call starts, the next one finishes."""
    if _state.get("mode") == "recording":
        finish()
    else:
        start(hands_free=True)


def _end(mode: str, **extra) -> None:
    with _lock:
        _state.update(mode=mode, ended=time.time(), **extra)


# --- writing it down --------------------------------------------------------------------------

def _front_app() -> str:
    try:
        import AppKit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName() or "") if app else ""
    except Exception:
        return ""


def _wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)
    return buffer.getvalue()


def _spoken(pcm: bytes) -> bool:
    """Was anything said? (A near-silent tap must not become made-up text.)"""
    import numpy as np
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    if samples.size < RATE * 0.35:
        return False
    frames = samples[: samples.size // 1600 * 1600].reshape(-1, 1600)
    loud = np.sqrt(np.mean(frames * frames, axis=1))
    floor = float(np.percentile(loud, 10))            # the room, between words
    return int((loud > max(0.015, floor * 3)).sum()) >= 3


_TIDY = """Rewrite this dictated text the way the speaker meant to write it, as {style}.
Rules: fix punctuation and capitals; remove filler words (um, uh, erm, like, you know, so at the start) and false \
starts; when the speaker corrects themselves ("Friday, no wait, Thursday", "scratch that", "I mean"), keep ONLY the \
correction; spoken list items become a list; "new line" / "new paragraph" become line breaks; keep the language(s) \
and script as given (Hinglish in Latin letters, Hindi in Devanagari). Never answer it, never add or drop meaning.{extra}
Examples:
"um so lets meet friday no wait make that thursday at three" -> "Let's meet Thursday at three."
"uh can you send the deck, I mean the final deck, by tonight" -> "Can you send the final deck by tonight?"
Return ONLY the rewritten text.

Dictated: {text}"""


def _style(app: str) -> str:
    name = app.lower()
    if any(k in name for k in ("slack", "whatsapp", "messages", "telegram", "discord", "teams")):
        return "a chat message (natural, short)"
    if name in ("mail", "outlook", "spark", "superhuman"):
        return "an email (proper sentences and paragraphs)"
    if any(k in name for k in ("code", "cursor", "terminal", "iterm", "xcode", "claude", "chatgpt")):
        return "a prompt or code comment (keep technical words exactly)"
    return "clean written text"


def transcribe(pcm: bytes, app: str = "") -> str:
    """Two quick steps: hear it word for word (audio -> text), then tidy it as written text. The
    fast models skip the tidy rules when asked to do both from audio at once."""
    from google.genai import types
    from mint.core import llm
    from mint.core import prefs
    words = [w.strip() for w in str(prefs.get("dictation_words") or "").split(",") if w.strip()]
    hint = f" Words they use, spelled exactly: {', '.join(words[:60])}." if words else ""
    heard, _ = llm.generate([types.Part.from_bytes(data=_wav(pcm), mime_type="audio/wav"),
                             "Transcribe this speech word for word, in the language and script spoken (Hindi in "
                             "Devanagari, English in Latin, Hinglish in Latin)." + hint + " If nothing is said, "
                             "return an empty string. Return only the transcript."], HEAR)
    heard = heard.strip().strip('"').strip()
    if not heard or heard.lower() in ("(empty)", "empty", '""'):
        return ""
    name = prefs.get("user_name") or ""
    extra = (f" The speaker is {name}." if name else "") + hint + (f" They are typing into {app}." if app else "")
    try:
        text, _ = llm.generate(_TIDY.format(style=_style(app), extra=extra, text=heard), TIDY)
        text = text.strip().strip('"').strip()
        return text or heard
    except Exception as error:
        log.info("dictation tidy: %s", error)
        return heard


def _paste(text: str) -> bool:
    import AppKit

    from mint.tools import fastinput
    from mint.tools import everyday as skills
    if not fastinput.has_accessibility():
        return False
    with skills._Clipboard() as clip:
        clip.board.clearContents()
        clip.board.setString_forType_(text, AppKit.NSPasteboardTypeString)
        fastinput.press_key("v", ["command"])
        time.sleep(0.12)
    return True


def _write(pcm: bytes) -> None:
    seconds = len(pcm) / 2 / RATE
    if pcm and not any(pcm):
        # Pure digital zeros, not a quiet room: the microphone itself sent nothing. Seen 10 Oct 19:20: dictation
        # began 1 s after Mint's audio engine restarted (resumed, voice on), and echo-cancelled input is silent
        # for a moment then - it read as "didn't hear anything", and nothing was ever sent to Gemini.
        _end("failed", error="The mic was still starting - try again")
        print(f"  {time.strftime('%H:%M:%S')} [dictation: the microphone sent only silence for {seconds:.1f}s "
              "(audio engine starting?) - nothing sent to Gemini]", flush=True)
        return
    if not _spoken(pcm):
        _end("failed", error="Didn't hear anything")
        print(f"  {time.strftime('%H:%M:%S')} [dictation: nothing heard in {seconds:.1f}s (too quiet) - nothing "
              "sent to Gemini]", flush=True)
        return
    started = time.monotonic()
    try:
        text = transcribe(pcm, _state.get("app", ""))
    except Exception as error:
        log.exception("dictation")
        _end("failed", error="Couldn't reach Gemini")
        print(f"  [dictation failed: {str(error)[:120]}]", flush=True)
        return
    if not text:
        _end("failed", error="Didn't catch that")
        return
    pasted = _paste(text)
    _remember(text, seconds)
    _end("done", text=text, pasted=pasted)
    print(f"  {time.strftime('%H:%M:%S')} [dictation: {seconds:.1f}s -> {len(text)} chars in "
          f"{time.monotonic() - started:.1f}s{'' if pasted else ', NOT pasted (no Accessibility)'}]", flush=True)


def _remember(text: str, seconds: float) -> None:
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        rows = HISTORY.read_text().splitlines()[-49:] if HISTORY.exists() else []
        rows.append(json.dumps({"at": time.time(), "seconds": round(seconds, 1), "text": text}, ensure_ascii=False))
        HISTORY.write_text("\n".join(rows) + "\n")
        HISTORY.chmod(0o600)
    except OSError:
        pass


def last(n: int = 1) -> list[dict]:
    try:
        rows = [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []
    return rows[-n:]


# --- the tool (for "paste my last dictation") ------------------------------------------------------

PROMPT = """Dictation: the user dictates with a key (hold Right ⌥, or their own in Settings ▸ Shortcuts) - you \
do not hear it. "paste my last dictation" / "what did I just dictate?" -> dictation action=last (paste=true to \
paste it again). "how do I dictate?" -> explain: hold the key, talk, let go; tap twice for hands-free."""


def declarations():
    from google.genai import types
    return [types.FunctionDeclaration(
        name="dictation",
        description=("The user's recent dictations (Wispr-style: hold a key, talk, the text is typed at the "
                     "cursor). last: the most recent ones; paste=true pastes the latest again at the cursor."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=types.Type.STRING, enum=["last"]),
            "count": types.Schema(type=types.Type.INTEGER, description="how many recent ones (default 1)"),
            "paste": types.Schema(type=types.Type.BOOLEAN, description="paste the latest again")},
            required=["action"]))]


def tool(args: dict) -> str:
    rows = last(max(1, min(int(args.get("count") or 1), 10)))
    if not rows:
        return "No dictations yet."
    if args.get("paste"):
        return ("Pasted it again." if _paste(rows[-1]["text"]) else "Could not paste (no Accessibility).")
    return "Recent dictations:\n" + "\n".join(
        f"- {time.strftime('%H:%M', time.localtime(r['at']))}: {r['text']}" for r in rows)


HANDLERS = {"dictation": tool}
