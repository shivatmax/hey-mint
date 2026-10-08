"""Soft UI sounds (pref ui_sounds).

    sfx.play(name)   # "open", "close", "tick", "done", "error", "poke", "dizzy", "hello", "send", "drop"

Ten tiny sounds, made here with plain maths (no sample files to ship): sine voices with gentle
envelopes, a little bell shimmer, one breath of filtered noise. They are written once as 16-bit mono
WAVs to ~/Library/Application Support/Mint/sounds/ (the version is in the file name, so a change to
a recipe makes new files) and played with NSSound.

play() is cheap and safe from any thread (a worker plays the sound): it returns at once when the setting is off, when the same
sound played under 150 ms ago, while Mint is dictating, in a call or recording a meeting (a chime
would land in the recording), or before the files exist - the first call makes them on a worker
thread and that one sound is skipped.
"""

from __future__ import annotations

import array
import logging
import math
import os
import random
import sys
import threading
import time
import wave
from pathlib import Path

log = logging.getLogger("mint.ui.sfx")

NAMES = ("open", "close", "tick", "done", "error", "poke", "dizzy", "hello", "send", "drop")
VERSION = 2                 # bump when a recipe changes: new file names, so the sounds are made again
RATE = 44100
MASTER = 0.8                # NSSound volume at play(volume=1.0)
GAP = 0.15                  # the same sound at most once per GAP seconds
# Sounds that mark the moment the island opens or wakes: they play even while Mint listens.
WAKE_OK = {"open", "close", "hello"}
# Settings ▸ Appearance & Sound turns these groups on and off (prefs sounds_notch / sounds_tasks / sounds_play).
GROUPS = {"open": "notch", "close": "notch", "tick": "tasks", "done": "tasks", "error": "tasks", "send": "tasks",
          "drop": "tasks", "poke": "play", "dizzy": "play", "hello": "play"}


def folder() -> Path:
    custom = os.environ.get("MINT_SOUNDS_DIR")      # tests write somewhere throwaway
    if custom:
        return Path(custom)
    return Path.home() / "Library" / "Application Support" / "Mint" / "sounds"


def path(name: str) -> Path:
    return folder() / f"{name}-v{VERSION}.wav"


# --- synthesis (pure Python, any thread) ----------------------------------------------------------

TAU = 2 * math.pi


def _n(t: float) -> int:
    return int(round(t * RATE))


def _env(i: int, attack: int, decay_tau: float) -> float:
    """A soft raised-cosine attack into an exponential decay (tau in seconds)."""
    if i < attack:
        return 0.5 - 0.5 * math.cos(math.pi * i / max(1, attack))
    return math.exp(-(i - attack) / (decay_tau * RATE))


def _mix(out: list, start: float, voice: list, gain: float = 1.0) -> None:
    s = _n(start)
    need = s + len(voice)
    if need > len(out):
        out.extend([0.0] * (need - len(out)))
    for i, v in enumerate(voice):
        out[s + i] += v * gain


def _tone(seconds, f0, f1=None, glide=0.02, tau=0.08, attack=0.004, partials=((1, 1.0),), vibrato=None):
    """A sine voice: pitch glides f0 -> f1 over `glide` s (exponentially), `partials` are
    (frequency ratio, level) pairs, vibrato = (rate Hz, depth fraction, decay tau s)."""
    f1 = f0 if f1 is None else f1
    n = _n(seconds)
    att = _n(attack)
    out = [0.0] * n
    phases = [0.0] * len(partials)
    for i in range(n):
        t = i / RATE
        k = min(1.0, t / glide) if glide > 0 else 1.0
        k = 1 - (1 - k) ** 3                              # ease out: lands on the note softly
        f = f0 * (f1 / f0) ** k
        if vibrato:
            rate, depth, vtau = vibrato
            f *= 1 + depth * math.sin(TAU * rate * t) * math.exp(-t / vtau)
        e = _env(i, att, tau)
        v = 0.0
        for j, (ratio, level) in enumerate(partials):
            phases[j] += TAU * f * ratio / RATE
            # Upper partials die faster, like a struck glass.
            v += level * math.sin(phases[j]) * (e ** ratio if ratio > 1 else e)
        out[i] = v
    return out


def _noise(seconds, f_lo, f_hi, q=1.2, tau=None, rise=0.6, seed=7):
    """Band-passed noise whose centre sweeps f_lo -> f_hi: a breath, a whoosh."""
    rng = random.Random(seed)
    n = _n(seconds)
    out = [0.0] * n
    z1 = z2 = 0.0
    for i in range(n):
        t = i / n
        fc = f_lo * (f_hi / f_lo) ** t
        # A state-variable band-pass, retuned every sample (cheap and stable at these settings).
        g = 2 * math.sin(math.pi * min(fc, RATE / 6) / RATE)
        x = rng.uniform(-1, 1)
        hp = x - z2 - z1 / q
        z1 += g * hp
        z2 += g * z1
        amp = math.sin(math.pi * min(1.0, t / rise) / 2) ** 2 if t < rise else (1 - (t - rise) / (1 - rise)) ** 2
        out[i] = z1 * amp
    return out


def _finish(samples: list, peak_db: float = -6.0, rms_db: float = -20.0, fade_in=0.003, fade_out=0.012):
    """No DC, no clicks (raised-cosine edges), loudness about rms_db with the peak kept under peak_db."""
    if not samples:
        return samples
    mean = sum(samples) / len(samples)
    s = [v - mean for v in samples]
    a, b = _n(fade_in), _n(fade_out)
    for i in range(min(a, len(s))):
        s[i] *= 0.5 - 0.5 * math.cos(math.pi * i / a)
    for i in range(min(b, len(s))):
        s[-1 - i] *= 0.5 - 0.5 * math.cos(math.pi * i / b)
    rms = math.sqrt(sum(v * v for v in s) / len(s)) or 1e-9
    peak = max(abs(v) for v in s) or 1e-9
    gain = min(10 ** (rms_db / 20) / rms, 10 ** (peak_db / 20) / peak)
    return [v * gain for v in s]


def _bell(f, seconds=0.32, tau=0.12, level=1.0):
    # Struck-glass partials: slightly inharmonic, quiet, fast-dying.
    return _tone(seconds, f, tau=tau, attack=0.003, partials=((1, level), (2.0, 0.18 * level), (2.76, 0.07 * level)))


def recipe(name: str) -> list:
    out: list = []
    if name == "open":                        # soft rising two-note bloop
        _mix(out, 0.0, _tone(0.12, 520, 660, glide=0.03, tau=0.05, partials=((1, 1), (2, 0.12))))
        _mix(out, 0.07, _tone(0.16, 780, 990, glide=0.03, tau=0.06, partials=((1, 1), (2, 0.12))), 0.9)
        return _finish(out)
    if name == "close":                       # the same, falling
        _mix(out, 0.0, _tone(0.12, 990, 780, glide=0.03, tau=0.05, partials=((1, 1), (2, 0.12))), 0.9)
        _mix(out, 0.07, _tone(0.17, 660, 520, glide=0.04, tau=0.06, partials=((1, 1), (2, 0.12))))
        return _finish(out)
    if name == "tick":                        # very short glassy tick
        _mix(out, 0.0, _tone(0.06, 2400, tau=0.010, attack=0.0015,
                             partials=((1, 1.0), (2.76, 0.35), (4.1, 0.1))))
        return _finish(out, peak_db=-10.0, rms_db=-24.0, fade_in=0.0015, fade_out=0.008)
    if name == "done":                        # bright three-note chime (C6 E6 G6)
        for k, f in enumerate((1046.5, 1318.5, 1568.0)):
            _mix(out, k * 0.075, _bell(f, 0.34 - k * 0.02, tau=0.13), 1.0 - k * 0.08)
        return _finish(out, rms_db=-21.0)
    if name == "error":                       # soft low double thud, not an alarm
        _mix(out, 0.0, _tone(0.14, 210, 140, glide=0.05, tau=0.045, attack=0.003, partials=((1, 1), (2, 0.25))))
        _mix(out, 0.15, _tone(0.16, 180, 115, glide=0.06, tau=0.05, attack=0.003, partials=((1, 1), (2, 0.25))), 0.85)
        return _finish(out, rms_db=-19.0)
    if name == "poke":                        # rubbery boing
        _mix(out, 0.0, _tone(0.30, 240, 420, glide=0.09, tau=0.11, attack=0.003,
                             partials=((1, 1), (2, 0.2), (3, 0.05)), vibrato=(17, 0.16, 0.09)))
        return _finish(out)
    if name == "dizzy":                       # wobbly whirl, wandering down
        _mix(out, 0.0, _tone(0.45, 900, 520, glide=0.42, tau=0.22, attack=0.03,
                             partials=((1, 1), (2, 0.1)), vibrato=(7.5, 0.12, 2.0)))
        _mix(out, 0.02, _tone(0.43, 1350, 780, glide=0.40, tau=0.18, attack=0.04,
                              partials=((1, 1),), vibrato=(8.3, 0.14, 2.0)), 0.3)
        return _finish(out, rms_db=-21.0)
    if name == "hello":                       # sparkly arpeggio with a shimmer on top
        for k, f in enumerate((783.99, 1046.5, 1318.5, 1568.0, 2093.0)):
            _mix(out, k * 0.05, _bell(f, 0.26, tau=0.09, level=1.0), 0.95 - k * 0.1)
        shimmer = _noise(0.36, 5000, 9000, q=4.0, rise=0.3, seed=3)
        for i in range(len(shimmer)):                       # glitter: a fast flutter on the noise
            shimmer[i] *= 0.5 + 0.5 * math.sin(TAU * 31 * i / RATE)
        _mix(out, 0.06, shimmer, 0.35)
        return _finish(out, rms_db=-21.0)
    if name == "send":                        # whoosh up
        _mix(out, 0.0, _noise(0.26, 500, 4200, q=1.6, rise=0.7, seed=11), 1.0)
        _mix(out, 0.05, _tone(0.21, 420, 1250, glide=0.18, tau=0.12, attack=0.03), 0.22)
        return _finish(out, rms_db=-22.0)
    if name == "drop":                        # soft plop
        _mix(out, 0.0, _tone(0.15, 950, 260, glide=0.05, tau=0.04, attack=0.002, partials=((1, 1), (2, 0.08))))
        return _finish(out, rms_db=-20.0)
    raise KeyError(name)


def _write(name: str, samples: list) -> Path:
    target = path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    pcm = array.array("h", (max(-32767, min(32767, int(round(v * 32767)))) for v in samples))
    tmp = target.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    os.replace(tmp, target)                  # never a half-written file for NSSound
    return target


def generate(force: bool = False) -> list[Path]:
    """Write any missing sound files (all of them with force). Takes ~a second; not on the main thread."""
    made = []
    for name in NAMES:
        if force or not path(name).exists():
            made.append(_write(name, recipe(name)))
    return made


# --- playing (any thread) ------------------------------------------------------------------------

_lock = threading.Lock()
_ready = {"done": False, "making": False}
_last: dict = {}
_sounds: dict = {}                   # name -> NSSound (the first copy; more are copied for overlap)
_state = {"value": "", "note": ""}   # Mint's state, told by ui.Presence.set_state


def set_state(state: str, note: str = "") -> None:
    _state["value"], _state["note"] = state or "", note or ""


def _ensure() -> bool:
    if _ready["done"]:
        return True
    if all(path(n).exists() for n in NAMES):
        _ready["done"] = True
        return True
    with _lock:
        if _ready["making"]:
            return False
        _ready["making"] = True

    def make():
        try:
            generate()
            _ready["done"] = True
        except Exception:
            log.warning("could not make the UI sounds", exc_info=True)
        finally:
            _ready["making"] = False
    threading.Thread(target=make, daemon=True, name="mint-sfx-make").start()
    return False


def prepare() -> None:
    """Make the sound files now (on a worker thread) if they are missing: call once at startup."""
    _ensure()


# Written out whole: publish/restructure.py rewrites "mint.x" literals to where x lives in the packaged app
# (mint.voice.dictation, mint.app.meet_call...); a name built at run time would miss there.
_MODULES = {"dictation": "mint.voice.dictation", "meet_call": "mint.app.meet_call", "meetings": "mint.tools.meetings"}


def _loaded(name: str):
    # Only modules Mint already uses: importing one here would cost the main thread its first sound.
    return sys.modules.get(_MODULES.get(name, f"{__package__}.{name}"))


def _quiet(name: str) -> bool:
    """True when a sound now would be in the way."""
    try:
        dictation = _loaded("dictation")
        if dictation is not None and dictation.capturing():
            return True
        meet_call = _loaded("meet_call")
        call = meet_call.current() if meet_call is not None else None
        if call is not None and call.live:
            return True
        meetings = _loaded("meetings")
        if meetings is not None and meetings.is_recording():
            return True
    except Exception:
        pass
    # Listening: a chime would go into the microphone. Paused because a call app has the mic
    # (session.py says "Zoom is using the mic"): the user is in a call.
    if _state["value"] == "awake" and name not in WAKE_OK:
        return True
    return _state["value"] == "paused" and "using the mic" in _state["note"]


def _play_now(name: str, volume: float) -> None:
    import AppKit
    sound = _sounds.get(name)
    if sound is None:
        sound = AppKit.NSSound.alloc().initWithContentsOfFile_byReference_(str(path(name)), True)
        if sound is None:
            return
        _sounds[name] = sound
    if sound.isPlaying():
        sound = sound.copy()                 # overlap rather than cut the first one off
    sound.setVolume_(max(0.0, min(1.0, MASTER * volume)))
    sound.play()


_queue = None


def _player() -> None:
    # NSSound.play() takes ~10 ms (the very first ~100 ms, waking the audio device): one worker thread
    # pays that, in order, so the main thread never does. (NSSound learns a sound has ended through
    # the main run loop, which Mint always runs - so isPlaying, and the overlap copy, stay right.)
    import objc
    while True:
        name, volume = _queue.get()
        with objc.autorelease_pool():
            try:
                _play_now(name, volume)
            except Exception:
                log.debug("sound %s failed", name, exc_info=True)


def play(name: str, volume: float = 1.0) -> None:
    global _queue
    try:
        if name not in NAMES:
            return
        from mint.core import prefs
        if not prefs.get("ui_sounds") or prefs.get(f"sounds_{GROUPS.get(name, 'tasks')}") is False:
            return
        try:
            volume *= max(0.0, min(1.0, float(prefs.get("sound_volume") if prefs.get("sound_volume") is not None
                                              else 0.7)))
        except (TypeError, ValueError):
            pass
        if volume <= 0.01:
            return
        now = time.monotonic()
        if now - _last.get(name, 0.0) < GAP:
            return
        if not _ensure() or _quiet(name):
            return
        _last[name] = now
        if _queue is None:
            with _lock:
                if _queue is None:
                    import queue
                    _queue = queue.Queue()
                    threading.Thread(target=_player, daemon=True, name="mint-sfx").start()
        _queue.put((name, volume))
    except Exception:
        log.debug("sound %s failed", name, exc_info=True)


# --- checking the sounds without ears -------------------------------------------------------------

def sample(name: str = "done") -> None:
    """Settings' "Play a sample": the sound at the current volume, even if its group is off (not if all are off)."""
    _last.pop(name, None)
    from mint.core import prefs
    if not prefs.get("ui_sounds") or not _ensure():
        return
    try:
        volume = max(0.0, min(1.0, float(prefs.get("sound_volume"))))
    except (TypeError, ValueError):
        volume = 0.7
    global _queue
    if _queue is None:
        with _lock:
            if _queue is None:
                import queue
                _queue = queue.Queue()
                threading.Thread(target=_player, daemon=True, name="mint-sfx").start()
    _queue.put((name, volume))


def analyse(samples: list) -> dict:
    n = len(samples)
    peak = max(abs(v) for v in samples)
    rms = math.sqrt(sum(v * v for v in samples) / n)
    edge = _n(0.001)
    return {
        "ms": round(1000 * n / RATE),
        "peak_db": round(20 * math.log10(peak), 1),
        "rms_db": round(20 * math.log10(rms), 1),
        "dc": round(sum(samples) / n, 6),
        "clip": sum(1 for v in samples if abs(v) >= 0.999),
        "first": round(abs(samples[0]), 5),                          # edges at silence: no click
        "last": round(abs(samples[-1]), 5),
        "end": round(max(abs(v) for v in samples[-edge:]), 4),      # loudest sample in the last ms
        "jump": round(max(abs(samples[i] - samples[i - 1]) for i in range(1, n)), 3),  # biggest step
    }


if __name__ == "__main__":
    # python -m mint.sfx [--write]   prints the table; --write also makes the files (MINT_SOUNDS_DIR to move them)
    import sys
    print(f"{'name':7} {'ms':>4} {'peak dB':>8} {'rms dB':>7} {'dc':>9} {'clip':>4} {'first':>7} {'last':>7} "
          f"{'end 1ms':>7} {'jump':>6}")
    for nm in NAMES:
        a = analyse(recipe(nm))
        print(f"{nm:7} {a['ms']:>4} {a['peak_db']:>8} {a['rms_db']:>7} {a['dc']:>9} {a['clip']:>4} "
              f"{a['first']:>7} {a['last']:>7} {a['end']:>7} {a['jump']:>6}")
    if "--write" in sys.argv:
        for p in generate(force=True):
            print("wrote", p)
