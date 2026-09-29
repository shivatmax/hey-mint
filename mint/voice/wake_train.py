"""A wake word of the user's choosing ("Hey Jarvis"), trained on this Mac.

The same recipe and the same model file as the shipped "Hey Mint"
(scripts/train_hey_mint.py, wake.MintWake): a logistic regression over 16 frames
of openWakeWord's speech features, so the Python detector and Mint Ear load it
unchanged. Nothing leaves the Mac.

* Positives: the phrase from the Mac's own English voices at three speeds,
  each copy also faster/slower, quieter, with hiss, a hum, or a small room's
  echo - plus, if given, the user's own takes (the strongest signal by far).
* Hard negatives: near misses made from the phrase itself ("hey Travis",
  "jar of this", "hey there" for "Hey Jarvis"), the shipped everyday speech
  and LibriSpeech windows, and "Hey Mint" itself.
* Checked before it is saved: whole voice families are kept out of training
  and must still wake it; unseen near misses and everyday sentences must not.
  The threshold is set from those numbers, and a phrase that cannot reach a
  usable accept rate is refused with advice rather than shipped half-working.

About 70-80 s on an Apple M5: ~30 s of `say` (one call per voice and speed, split
at the pauses), ~40 s of features on a few threads, the rest fitting and checking.
Voices from `say` are for the user's own model on their own Mac only.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from mint.voice import wake

log = logging.getLogger("mint.voice.wake_train")

RATE = 16000
FRAME = wake.FRAME_SAMPLES
SPEEDS = (150, 185, 220)                 # `say -r`, words per minute
GREETINGS = {"hey", "hi", "hello", "ok", "okay", "yo", "hiya", "oi"}
# `say` voices that sing, bubble or whisper: nothing a person sounds like.
NOVELTY = {"albert", "bad news", "bahh", "bells", "boing", "bubbles", "cellos", "good news", "jester",
           "organ", "superstar", "trinoids", "whisper", "wobble", "zarvox"}
REQUESTS = ["open Slack", "what's the time", "turn the volume down", "read my last email"]
# Everyday speech that must not wake it, said by the training voices. Without it the
# model learned "a voice, then quiet" and woke at the end of ordinary sentences.
EVERYDAY = [
    "Can you send me the file when you get a chance?", "I'll be home a little late tonight.",
    "The meeting has been moved to next Tuesday.", "Hey, how are you doing today?",
    "What time does the store close on Sundays?", "Please remember to water the plants.",
    "That was the best pizza I've had in years.", "We are running out of coffee again.",
    "Hey there, long time no see!", "Did you watch the match last night?",
    "I need to finish this report by Friday.", "Let me check my calendar and get back to you.",
    "The kids have a holiday on Monday.", "Okay, sounds good, see you then.",
    "Hey, can you pass me the salt please?", "It's going to rain all weekend apparently.",
]
# Everyday speech for the check, by the held-out voices (never trained on).
SENTENCES = [
    "Could you move the meeting to three thirty on Thursday afternoon?",
    "I left my charger at the office, so my laptop is almost dead.",
    "Honestly the traffic this morning was worse than I expected.",
    "Let's order some food, I haven't eaten anything since breakfast.",
    "The presentation went well, but the client wants a few changes.",
    "Hey, did anyone see where I put the car keys last night?",
    "We should probably book the tickets before the prices go up.",
    "My sister is visiting next weekend with her two kids.",
    "Turn left at the signal and then take the second exit.",
    "I think the printer on the third floor is out of paper again.",
]
MIN_SYLLABLES = 2          # "Hey Mint" is two; one ("Max") matches everyday speech far too often
MAX_SECONDS = 1.6          # the detector sees 1.28 s; a longer phrase is only partly heard
MIN_ACCEPT = 0.8           # held-out voices that must wake it, or the phrase is refused
MAX_LOOKALIKES = 0.1       # unseen near misses ("hey there", "jar of vis") allowed to wake it
MAX_CLOSE = 0.5            # ... and those one sound off ("hey carvis"): a person might answer those too

_busy = threading.Lock()


class WakeTrainError(Exception):
    """The phrase was refused or could not be trained. str() is advice for the user;
    `details` holds the check's numbers when it got that far."""

    def __init__(self, advice: str, details: dict | None = None) -> None:
        super().__init__(advice)
        self.details = details or {}


# --- the phrase -------------------------------------------------------------------------

def _words(phrase: str) -> list[str]:
    return re.findall(r"[a-z']+", phrase.lower())


def syllables(word: str) -> int:
    """Vowel groups, less a silent final e: rough, but enough to tell "Max" from "Jarvis"."""
    word = word.lower().strip("'")
    groups = len(re.findall(r"[aeiouy]+", word))
    if word.endswith("e") and not word.endswith(("le", "ee", "ye")) and groups > 1:
        groups -= 1
    return max(1, groups)


def check_phrase(phrase: str) -> str | None:
    """Advice if the phrase cannot make a good wake word, else None. Short phrases
    match too much everyday speech; long ones do not fit the 1.3 s the detector hears."""
    words = _words(phrase)
    if not words:
        return "Type the phrase in letters, for example “Hey Jarvis”."
    if len(words) > 4:
        return "Keep it to two or three words - the detector hears about a second and a quarter."
    count = sum(syllables(w) for w in words)
    if count < MIN_SYLLABLES:
        return (f"“{phrase.strip()}” is too short: one syllable matches everyday speech far too often. "
                "Use two words, like “Hey Jarvis” or “Okay Nova”.")
    if count > 7:
        return "That is too long to say in one breath. Two or three words work best, like “Hey Jarvis”."
    return None


def name_from_phrase(phrase: str) -> str:
    """"Hey Jarvis" -> "Jarvis": the name the assistant would answer to."""
    keys = [w for w in phrase.split() if re.sub(r"[^a-z']", "", w.lower()) not in GREETINGS]
    return " ".join(w.strip(",.!?") for w in keys).strip() or phrase.strip()


def model_path(phrase: str, folder: Path | None = None) -> Path:
    from mint.core import config
    return (folder or config.PROJECT_ROOT / "models") / f"wake_{wake.slug(phrase)}.json"


def _split(word: str) -> list[str]:
    """"jarvis" -> ["jar", "vis"]: one consonant starts each later syllable."""
    parts = re.findall(r"[^aeiouy]*[aeiouy]+(?:[^aeiouy]*$)?", word)
    if len(parts) < 2:
        return [word]
    out = [parts[0]]
    for part in parts[1:]:
        lead = re.match(r"[^aeiouy]*", part).group()
        if len(lead) > 1:                              # "marvin" -> "mar" + "vin"
            out[-1] += lead[:-1]
            part = part[len(lead) - 1:]
        out.append(part)
    return out


def _dictionary() -> list[str]:
    try:
        text = Path("/usr/share/dict/words").read_text(errors="ignore")
    except OSError:
        return []
    return [w for w in text.split() if w.isalpha() and w.islower()]


def _distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def _sound_alikes(word: str) -> list[str]:
    onset = re.match(r"[^aeiouy]*", word).group()
    rest = word[len(onset):]
    out = []
    # Another first sound: "jarvis" -> "travis", "marvis"...
    for other in ("t", "m", "h", "b", "st", "c", "d"):
        if other != onset and rest:
            out.append(other + rest)
    # Another first vowel: "jarvis" -> "jervis", "jorvis".
    vowel = re.search(r"[aeiou]+", word)
    if vowel:
        for other in ("e", "o", "i", "a"):
            if other != vowel.group():
                out.append(word[:vowel.start()] + other + word[vowel.end():])
                if len(out) >= 9:
                    break
    # Real words that are close ("mint" -> "mind", "hint"); `say` reads them naturally.
    starts, rhymes = [], []
    for w in _dictionary():
        if w != word and abs(len(w) - len(word)) <= 1 and (w[:2] == word[:2] or w[-3:] == word[-3:]):
            d = _distance(w, word)
            if d <= (1 if len(word) <= 4 else 2):
                (starts if w[:2] == word[:2] else rhymes).append((d, w))
    out += [w for _, w in sorted(starts)[:3] + sorted(rhymes)[:2]]
    return out


def near_misses(phrase: str) -> list[tuple[str, bool | None]]:
    """Phrases that sound close but must not wake it, made from the phrase itself:
    (text, close). Close (True) ones are one sound off ("hey travis", "hey jervis")
    - a few may still wake it, as they would a person. Others (False: "hey there",
    "hey man") must not. Pieces of the phrase (None: "hey jar", "jar of vis",
    "jarvis" alone) are always trained on, never held out: without them the
    model learns to fire half-way through."""
    words = _words(phrase)
    greet = [w for w in words if w in GREETINGS]
    keys = [w for w in words if w not in GREETINGS] or words[-1:]
    lead = " ".join(greet)
    main = max(keys, key=len)                       # the name: "jarvis" in "hey jarvis"

    def said(word: str) -> str:
        return " ".join(word if w == main else w for w in words)

    out = [(said(w), True) for w in _sound_alikes(main)]
    parts = _split(main)
    if len(parts) >= 2:
        head, tail = parts[0], "".join(parts[1:])
        out += [(f"{head} of {tail}", None), (f"{lead} {head}".strip(), None)]
    if greet:
        out += [(f"{lead} {w}", False) for w in ("there", "guys", "man", "siri", "you")] + [(lead, None)]
    out.append((main, None))                      # the name alone, mid-sentence, is not a wake
    seen, keep = {wake.slug(phrase)}, []
    for text, close in out:
        text = " ".join(text.split())
        if text and wake.slug(text) not in seen:
            seen.add(wake.slug(text))
            keep.append((text, close))
    return keep


# --- audio ------------------------------------------------------------------------------

def voices() -> tuple[list[str], list[str]]:
    """(training voices, held-out voices): English `say` voices, whole families
    ("Eddy (English (UK))" and "Eddy (English (US))") on one side, so the
    held-out ones are voices the model has never heard."""
    try:
        listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        listing = ""
    names = []
    for line in listing.splitlines():
        m = re.match(r"^(.+?)\s+(en[_-][A-Za-z]+)\s+#", line)
        if m and m.group(1).strip() not in names:
            names.append(m.group(1).strip())
    families: dict[str, list[str]] = {}
    for name in names:
        base = name.split(" (")[0].strip()
        if base.lower() not in NOVELTY:
            families.setdefault(base, []).append(name)
    order = sorted(families)
    held = {f for i, f in enumerate(order) if i % 4 == 1}
    train = [v for f in order if f not in held for v in families[f]]
    test = [v for f in order if f in held for v in families[f]]
    return train, test


def _say(voice: str, texts: list[str], rate: int | None, folder: str) -> list[np.ndarray | None]:
    """Each text in one voice. One `say` call for all of them, split at the
    silences put between them: a call costs ~0.5 s however much it says."""
    def run(items: list[str]) -> np.ndarray | None:
        path = os.path.join(folder, f"{threading.get_ident()}_{abs(hash((voice, rate, tuple(items))))}.wav")
        cmd = ["say", "-v", voice, "-o", path, "--file-format=WAVE", "--data-format=LEI16@16000"]
        try:
            subprocess.run(cmd + (["-r", str(rate)] if rate else []) + [" [[slnc 900]] ".join(items)],
                           check=True, capture_output=True, timeout=60)
            with wave.open(path) as w:
                audio = np.frombuffer(w.readframes(w.getnframes()), np.int16)
            os.unlink(path)
            return audio
        except Exception:
            return None

    audio = run(texts)
    parts = _pieces(audio) if audio is not None else []
    if len(parts) == len(texts):
        return parts
    return [_one(run([t])) for t in texts]           # a voice that ignored the pauses: one at a time


def _one(audio: np.ndarray | None) -> np.ndarray | None:
    return audio if audio is not None and audio.size > RATE // 10 else None


def _pieces(audio: np.ndarray, gap: float = 0.45) -> list[np.ndarray]:
    """Split at every stretch of quiet longer than `gap` seconds."""
    x = audio.astype(np.float32)
    hop = 160
    count = x.size // hop
    if count < 5:
        return []
    energy = np.sqrt(np.mean(x[:count * hop].reshape(count, hop) ** 2, axis=1))
    loud = energy > max(0.02 * energy.max(), 30.0)
    out, start, quiet = [], None, 0
    for i, on in enumerate(loud):
        if on:
            start = i if start is None else start
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet * 0.01 >= gap:
                out.append((start, i - quiet + 1))
                start, quiet = None, 0
    if start is not None:
        out.append((start, count))
    return [audio[max(0, a - 3) * hop:min(count, b + 4) * hop] for a, b in out]


def _trim(audio: np.ndarray) -> np.ndarray:
    """Cut leading and trailing quiet (10 ms steps) - the window after the phrase's
    end is what the model learns, so the end must be where the voice stops."""
    x = audio.astype(np.float32)
    hop = 160
    count = x.size // hop
    if count < 5:
        return audio
    energy = np.sqrt(np.mean(x[:count * hop].reshape(count, hop) ** 2, axis=1))
    loud = np.nonzero(energy > max(0.06 * energy.max(), 2.5 * np.percentile(energy, 10), 30.0))[0]
    if not loud.size:
        return audio
    start, end = max(0, loud[0] - 3), min(count, loud[-1] + 4)
    return audio[start * hop:end * hop]


def _pcm(clip) -> np.ndarray:
    """int16 samples from int16 or float (-1..1) audio."""
    clip = np.asarray(clip)
    if clip.dtype.kind == "f":
        return (np.clip(clip, -1, 1) * 32767).astype(np.int16)
    return clip.astype(np.int16)


def _speed(audio: np.ndarray, factor: float) -> np.ndarray:
    """Faster (>1) or slower, pitch moving with it - a higher or deeper voice."""
    n = int(audio.size / factor)
    return np.interp(np.linspace(0, audio.size - 1, n), np.arange(audio.size), audio.astype(np.float32))


def _room(audio: np.ndarray, rng) -> np.ndarray:
    """A small room: a synthetic impulse response (direct sound plus a decaying tail)."""
    rt60 = rng.uniform(0.15, 0.5)
    t = np.arange(int(RATE * rt60)) / RATE
    tail = rng.normal(0, 1, t.size) * np.exp(-6.9 * t / rt60)
    ir = np.concatenate([[1.0], np.zeros(int(RATE * rng.uniform(0.002, 0.01))), tail * rng.uniform(0.15, 0.4)])
    n = audio.size + ir.size
    wet = np.fft.irfft(np.fft.rfft(audio.astype(np.float64), n) * np.fft.rfft(ir, n), n)[:audio.size]
    peak = np.abs(wet).max() or 1.0
    return wet * (np.abs(audio).max() / peak)


def _hum(size: int, level: float, rng) -> np.ndarray:
    """Low rumble (fans, traffic, an air conditioner): white noise, low-passed."""
    from scipy.signal import lfilter
    out = lfilter([1.0], [1.0, -0.98], rng.normal(0, 1, size))
    out -= out.mean()
    return out / (out.std() or 1.0) * level


def _variants(clip: np.ndarray, rng, many: bool) -> list[tuple[np.ndarray, float, float]]:
    """(audio, hiss, rumble) copies of one take: as said, quieter, louder, faster,
    slower, in a room, over hiss or a low hum."""
    x = clip.astype(np.float32)
    out = [(x, 0.0, 0.0), (x, 40.0, 0.0), (_room(x, rng), 150.0, 0.0), (x * 0.35, 40.0, 0.0)]
    if many:
        out += [(_speed(x, 0.92), 60.0, 0.0), (_room(_speed(x, 1.08), rng), 60.0, 0.0), (x, 400.0, 0.0),
                (x * 1.6, 40.0, 500.0)]
    return out


# --- features ---------------------------------------------------------------------------

_pool: list = []                     # idle feature extractors (ONNX sessions), shared by worker threads
_pool_lock = threading.Lock()


class _Borrowed:
    """One feature extractor for the length of a job. Kept in a plain list rather than
    thread-locals: ONNX Runtime aborts if its sessions are freed during interpreter exit."""

    def __enter__(self):
        with _pool_lock:
            self.features = _pool.pop() if _pool else None
        if self.features is None:
            from mint.voice.features import Features
            self.features = Features()
        return self.features

    def __exit__(self, *exc):
        with _pool_lock:
            _pool.append(self.features)


def _stream(audio: np.ndarray, hiss: float, seed: int, front: float = 1.0, back: float = 0.6,
            rumble: float = 0.0) -> tuple[list[np.ndarray], int]:
    """Every 80 ms feature window of a clip, streamed exactly as the microphone is
    (see wake.clip_windows), and the sample where the clip's audio begins."""
    lead = int(RATE * front)
    x = np.concatenate([np.zeros(lead, np.float32), audio.astype(np.float32), np.zeros(int(RATE * back), np.float32)])
    rng = np.random.default_rng(seed)
    if hiss:
        x = x + rng.normal(0, hiss, x.size)
    if rumble:
        x = x + _hum(x.size, rumble, rng)
    x = np.clip(x, -32768, 32767).astype(np.int16)
    out = []
    with _Borrowed() as features:
        features.reset()
        for i in range(0, x.size - FRAME + 1, FRAME):
            features(x[i:i + FRAME])
            out.append(features.get_features(16)[0].reshape(-1))
    return out, lead


def _positive(job) -> list[np.ndarray]:
    """The few windows ending just after the phrase (as wake.clip_windows picks them)."""
    audio, end, hiss, rumble, seed = job
    windows, lead = _stream(audio, hiss, seed, rumble=rumble)
    stop = (lead + end) // FRAME
    return windows[max(0, stop - 1):stop + 3]


def _negative(job) -> list[np.ndarray]:
    audio, hiss, rumble, seed = job
    windows, _ = _stream(audio, hiss, seed, back=1.0, rumble=rumble)
    return windows[16:]


def _probabilities(model: dict, windows: list[np.ndarray]) -> np.ndarray:
    if not windows:
        return np.zeros(0)
    W = np.vstack(windows)
    z = ((W - np.asarray(model["mean"])) / np.asarray(model["scale"])) @ np.asarray(model["coef"])
    return 1 / (1 + np.exp(-np.clip(z + model["intercept"], -30, 30)))


def _stream_scores(job) -> np.ndarray:
    """Per-window scores of a clip heard live: 1.5 s of room before it, hiss throughout."""
    model, audio, hiss, seed = job
    windows, _ = _stream(audio, hiss, seed, front=1.5, back=1.0)
    return _probabilities(model, windows[16:])


def _peak(scores: np.ndarray, need: int = 2) -> float:
    """The highest level held for `need` windows in a row: fires iff this >= threshold."""
    if scores.size < need:
        return 0.0
    return float(np.max(np.min(np.stack([scores[i:scores.size - need + 1 + i] for i in range(need)]), axis=0)))


def _fires(scores: np.ndarray, threshold: float, need: int = 2, refractory: int = 25) -> int:
    """Wake events in a stream, with the detector's 2 s (25 windows) refractory."""
    count, run, last = 0, 0, -refractory
    for i, p in enumerate(scores):
        run = run + 1 if p >= threshold else 0
        if run >= need and i - last >= refractory:
            count, run, last = count + 1, 0, i
    return count


# --- training ---------------------------------------------------------------------------

def train(phrase: str, recordings: list | None = None, progress=None, folder: Path | None = None) -> Path:
    """Train, check and save a detector for `phrase`. Returns the model path
    (models/wake_<slug>.json, plus _data.npz for later personal retraining).

    `recordings`: optional takes of the user saying it (16 kHz mono, float -1..1
    or int16; see record_samples). `progress(fraction, message)` is called along
    the way. Raises WakeTrainError with advice if the phrase is refused."""
    from mint.core import config

    phrase = " ".join(phrase.split())
    advice = check_phrase(phrase)
    if advice:
        raise WakeTrainError(advice)
    if not _busy.acquire(blocking=False):
        raise WakeTrainError("A wake word is already being trained - wait for it to finish.")
    try:
        return _train(phrase, recordings or [], progress or (lambda fraction, message: None),
                      folder or config.PROJECT_ROOT / "models")
    finally:
        with _pool_lock:
            _pool.clear()                     # ~10 MB each: not kept once trained
        _busy.release()


def _train(phrase: str, recordings: list, progress, folder: Path) -> Path:
    from mint.core import config

    started = time.time()
    stages: dict = {}
    rng = np.random.default_rng(len(phrase))
    train_voices, held_voices = voices()
    if len(train_voices) < 4 or not held_voices:
        raise WakeTrainError("This Mac has too few English voices to learn from. Add some in System Settings "
                             "▸ Accessibility ▸ Spoken Content ▸ System Voice ▸ Manage Voices, then try again.")
    lookalikes = near_misses(phrase)
    held_look = [t for tier in (True, False) for t in [x for x, c in lookalikes if c is tier][1::3]]
    train_look = [t for t, _ in lookalikes if t not in held_look]
    close = {t for t, c in lookalikes if c}
    phrase_text = phrase if phrase[-1] in ".!?" else phrase + "."   # a full stop: said as a finished call

    # 1. Voices: one `say` call per voice and speed, a few at once.
    progress(0.02, "Listening to the Mac's voices say it…")
    calls = []                                        # (voice, rate, [(kind, text)])
    calm = phrase.rstrip(".!?")
    for i, v in enumerate(train_voices):
        for r in SPEEDS:
            items = [("pos", phrase_text), ("pos", calm + "!"), ("pos", calm + "?")]
            items += [("cont", f"{calm}, {REQUESTS[(i + k) % len(REQUESTS)]}.") for k in range(2)]
            if r == SPEEDS[i % len(SPEEDS)]:
                items += [("neg", t + ".") for t in train_look]
                items += [("neg", EVERYDAY[(i * 5 + k) % len(EVERYDAY)]) for k in range(5)]
            calls.append((v, r, items))
    for i, v in enumerate(held_voices):
        calls.append((v, 165, [("held", phrase_text), ("heldcont", f"{phrase}, {REQUESTS[i % len(REQUESTS)]}.")]
                      + [("heldlook", t + ".") for t in held_look] + [("speech", t) for t in SENTENCES]))
        calls.append((v, 205, [("held", phrase_text)]))
    clips: dict[str, list] = {}
    done = 0
    with tempfile.TemporaryDirectory() as tmp, futures.ThreadPoolExecutor(4) as pool:
        running = {pool.submit(_say, v, [t for _, t in items], r, tmp): (v, r, items) for v, r, items in calls}
        for job in futures.as_completed(running):
            v, r, items = running[job]
            for (kind, text), audio in zip(items, job.result()):
                if audio is not None:
                    clips.setdefault(kind, []).append((v, r, audio, text))
            done += 1
            progress(0.02 + 0.3 * done / len(calls), f"Listening to the Mac's voices say it ({done}/{len(calls)})…")
    stages["voices"] = round(time.time() - started, 1)
    positives_raw = [(v, r, _trim(a)) for v, r, a, _ in clips.get("pos", [])]
    if len(positives_raw) < 12:
        raise WakeTrainError("The Mac's voices could not say the phrase. Check that it is written in English words.")
    length = float(np.median([a.size for _, _, a in positives_raw])) / RATE
    if length > MAX_SECONDS:
        raise WakeTrainError(f"“{phrase}” takes about {length:.1f} s to say - too long for the detector, which "
                             "hears about 1.3 s. Try a shorter phrase, like “Hey Jarvis”.")
    alone: dict = {}                    # where the phrase ends in "Hey Jarvis, open Slack": as said alone
    for v, r, a in positives_raw:
        alone.setdefault((v, r), a.size)

    # 2. Feature windows (the two ONNX models; ~20 ms a clip, a few threads).
    progress(0.35, "Learning the sound of it…")
    pos_jobs, seed = [], 0
    for v, r, audio in positives_raw:
        variants = _variants(audio, rng, many=True)
        # Always over hiss and in a room (where held-out voices were missed most), plus a
        # different mix of the others for each take.
        for k in [1, 2, *(rng.choice([0, 3, 4, 5, 6, 7], 3, replace=False))]:
            x, hiss, rumble = variants[k]
            seed += 1
            pos_jobs.append((x, x.size, hiss, rumble, seed))
    for v, r, audio, _ in clips.get("cont", []):
        audio = _trim(audio)
        end = alone.get((v, r))
        if end:
            for x, hiss, rumble in _variants(audio, rng, many=False)[1:3]:
                seed += 1
                pos_jobs.append((x, end, hiss, rumble, seed))
    mine, user_jobs = [], 0
    for take in recordings:
        audio = _trim(_pcm(take))
        if audio.size < RATE // 4:
            continue
        mine.append(audio)
        for x, hiss, rumble in _variants(audio, rng, many=True):
            seed += 1
            pos_jobs.append((x, x.size, hiss, rumble, seed))
            user_jobs += 1
    neg_jobs = []
    for i, (_, _, audio, _) in enumerate(clips.get("neg", [])):
        seed += 1
        x = _room(audio, rng) if i % 3 == 0 else audio.astype(np.float32)
        neg_jobs.append((x, (0.0, 40.0, 150.0, 60.0)[i % 4], 400.0 if i % 4 == 3 else 0.0, seed))
    workers = max(2, min(6, (os.cpu_count() or 4) - 2))
    with futures.ThreadPoolExecutor(workers) as pool:
        positive_sets = list(pool.map(_positive, pos_jobs))
        progress(0.62, "Learning what it is not…")
        negative_sets = list(pool.map(_negative, neg_jobs))
    positives = [w for s in positive_sets for w in s]
    if user_jobs:
        # The user's own takes count three times over (as in "Train my voice").
        positives += [w for s in positive_sets[-user_jobs:] for w in s] * 2
    near = [w for s in negative_sets for w in s]
    shipped = np.load(config.PROJECT_ROOT / "models" / "hey_mint_data.npz")
    order = np.random.default_rng(0).permutation(len(shipped["negatives"]))
    cut = len(order) // 4
    everyday = shipped["negatives"][order[cut:]].astype(np.float32)
    everyday_held = shipped["negatives"][order[:cut]].astype(np.float32)
    negatives = list(everyday) + near
    if wake.slug(phrase) != "hey_mint":
        negatives += list(shipped["positives"][::4].astype(np.float32))    # "Hey Mint" is now a lookalike

    stages["features"] = round(time.time() - started, 1)
    # 3. The classifier.
    progress(0.7, f"Training on {len(positives)} examples of it and {len(negatives)} of other speech…")
    # Stronger regularisation than "Hey Mint"'s 0.01: on voices it had not heard, C=0.001
    # woke for 94% with no false wakes in the check; 0.01-0.1 fired on everyday sentences.
    model = wake.train(positives, negatives, threshold=0.85, C=0.001)

    stages["fit"] = round(time.time() - started, 1)
    # 4. The check, on voices and near misses it never heard.
    progress(0.8, "Checking it on voices it has never heard…")
    held = ([(a, 60.0) for _, _, a, _ in clips.get("held", [])]
            + [(_room(a, rng), 150.0) for _, _, a, _ in clips.get("held", [])]
            + [(a, 60.0) for _, _, a, _ in clips.get("heldcont", [])])
    held_names = ([f"{v} {r}" for v, r, _, _ in clips.get("held", [])]
                  + [f"{v} {r} room" for v, r, _, _ in clips.get("held", [])]
                  + [f"{v} +request" for v, _, _, _ in clips.get("heldcont", [])])
    look = [a for _, _, a, _ in clips.get("heldlook", [])]
    look_names = [(v, t.rstrip(".")) for v, _, _, t in clips.get("heldlook", [])]
    look_close = np.array([t.rstrip(".") in close for _, t in look_names], bool)
    speech = [a for _, _, a, _ in clips.get("speech", [])]
    with futures.ThreadPoolExecutor(workers) as pool:
        held_scores = list(pool.map(_stream_scores, [(model, a, h, i) for i, (a, h) in enumerate(held)]))
        look_scores = list(pool.map(_stream_scores, [(model, a, 60.0, i) for i, a in enumerate(look)]))
        speech_scores = list(pool.map(_stream_scores, [(model, a, 60.0, i) for i, a in enumerate(speech)]))
        mine_scores = list(pool.map(_stream_scores, [(model, a, 40.0, i) for i, a in enumerate(mine)]))
    held_peaks = np.array([_peak(s) for s in held_scores])
    look_peaks = np.array([_peak(s) for s in look_scores])
    everyday_p = _probabilities(model, list(everyday_held))
    speech_minutes = sum(a.size for a in speech) / RATE / 60

    def quality(t: float) -> dict:
        return {"threshold": round(float(t), 2),
                "accept": float(np.mean(held_peaks >= t)) if held_peaks.size else 0.0,
                "close_lookalikes": float(np.mean(look_peaks[look_close] >= t)) if look_close.any() else 0.0,
                "other_lookalikes": float(np.mean(look_peaks[~look_close] >= t)) if (~look_close).any() else 0.0,
                "speech_fires": sum(_fires(s, t) for s in speech_scores),
                "window_false": float(np.mean(everyday_p >= t)) if everyday_p.size else 0.0}

    grid = [quality(t) for t in np.arange(0.5, 0.96, 0.05)]
    details = {"grid": grid, "speech": [(t[:40], round(_peak(s), 2)) for (_, _, _, t), s
                                        in zip(clips.get("speech", []), speech_scores) if _peak(s) >= 0.5], "held": [(v, round(_peak(s), 2)) for v, s in zip(held_names, held_scores)],
               "lookalikes": [(v, t, round(_peak(s), 2)) for (v, t), s in zip(look_names, look_scores)],
               "stages": stages}
    safe = [q for q in grid if q["speech_fires"] == 0 and q["other_lookalikes"] <= MAX_LOOKALIKES
            and q["close_lookalikes"] <= MAX_CLOSE and q["window_false"] <= 0.002]
    if not safe:
        raise WakeTrainError(f"“{phrase}” sounds too much like everyday speech and similar words to be safe. "
                             "Pick something more distinctive - a less common name, or add a word "
                             "(“Hey Jarvis”, “Okay Nova”).", details)
    # A margin above the lowest safe threshold (real rooms bring sounds the check did
    # not), but not so much that it gives up accepts.
    lowest = safe[0]
    choice = next((q for q in reversed(safe) if q["threshold"] <= min(0.9, lowest["threshold"] + 0.2)
                   and q["accept"] >= lowest["accept"] - 0.05), lowest)
    if mine_scores:
        # The user's own takes must wake it: lower the bar to them if needed, never under the safe floor.
        yours = min(_peak(s) for s in mine_scores)
        if yours < choice["threshold"]:
            choice = quality(max(lowest["threshold"], round(yours - 0.05, 2)))
    if choice["accept"] < MIN_ACCEPT:
        raise WakeTrainError(
            f"“{phrase}” was recognised in only {choice['accept']:.0%} of voices it had not heard. "
            + ("Record yourself saying it (3-5 takes) and train again, or pick a phrase with clearer, "
               "more distinct sounds." if not mine else
               "Try a phrase with clearer, more distinct sounds, like “Hey Jarvis”."), details)

    model["threshold"] = choice["threshold"]
    model["phrase"] = phrase
    model["report"] = {
        "accept_held_out": round(choice["accept"], 3), "held_out_clips": int(held_peaks.size),
        "lookalike_accept": round(choice["other_lookalikes"], 3),
        "close_lookalike_accept": round(choice["close_lookalikes"], 3), "lookalikes_checked": int(look_peaks.size),
        "false_per_hour_estimate": round(choice["speech_fires"] / speech_minutes * 60, 1) if speech_minutes else None,
        "speech_minutes_checked": round(speech_minutes, 1),
        "window_false_rate": round(choice["window_false"], 5),
        "your_takes": len(mine), "your_scores": [round(_peak(s), 2) for s in mine_scores],
        "voices": train_voices, "held_out_voices": held_voices, "near_misses": lookalikes,
        "positive_windows": len(positives), "negative_windows": len(negatives),
        "seconds": round(time.time() - started, 1), "stages": stages, "grid": grid,
        "trained": time.strftime("%Y-%m-%d %H:%M"),
    }
    folder.mkdir(parents=True, exist_ok=True)
    path = model_path(phrase, folder)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(model))
    temporary.replace(path)                     # a detector reloading mid-write never sees half a file
    # Kept for retraining with the user's voice later (enroll), subsampled to ~10 MB (as the shipped 960 + 3000).
    pick = np.random.default_rng(1)
    pos = pick.permutation(len(positives))[:1200]
    neg = pick.permutation(len(negatives))[:3000]
    np.savez_compressed(path.with_name(path.stem + "_data.npz"),
                        positives=np.vstack([positives[i] for i in pos]).astype(np.float16),
                        negatives=np.vstack([negatives[i] for i in neg]).astype(np.float16))
    progress(1.0, f"“{phrase}” is ready: woke for {choice['accept']:.0%} of unseen voices.")
    log.info("wake word %r trained: %s", phrase, {k: v for k, v in model["report"].items()
                                                   if not isinstance(v, list)})
    return path


def start(phrase: str, recordings: list | None = None, progress=None, done=None,
          folder: Path | None = None) -> threading.Thread:
    """train() on a background thread. done(path, None) or done(None, advice)."""
    def run():
        try:
            path = train(phrase, recordings, progress, folder)
        except WakeTrainError as error:
            if done:
                done(None, str(error))
        except Exception as error:
            log.exception("training the wake word failed")
            if done:
                done(None, f"Training failed ({error}). “Hey Mint” still works.")
        else:
            if done:
                done(path, None)
    thread = threading.Thread(target=run, daemon=True, name="wake-train")
    thread.start()
    return thread


def report(phrase: str) -> dict:
    """What the check found when `phrase` was trained ({} if it is not)."""
    try:
        return json.loads(model_path(phrase).read_text()).get("report", {})
    except Exception:
        return {}


def score(audio, phrase: str) -> tuple[float, float]:
    """(best score, threshold) of `phrase`'s model on one clip - the Settings "Test" button."""
    path = model_path(phrase)
    if not path.exists() and wake.slug(phrase) == "hey_mint":
        from mint.core import config
        path = config.PROJECT_ROOT / "models" / "hey_mint.json"
    model = json.loads(path.read_text())
    return _peak(_stream_scores((model, _trim(_pcm(audio)), 40.0, 0)), int(model.get("need", 2))), \
        float(model.get("threshold", 0.9))


# --- the user's own takes ---------------------------------------------------------------
# Mint's microphone stream reaches us through the session (as dictation does:
# session._on_audio calls feed() while capturing() is true). Without a running
# session, the microphone is opened here instead.

_lock = threading.Lock()
_take: dict = {"on": False, "chunks": [], "fed": 0.0}


def capturing() -> bool:
    with _lock:
        return _take["on"]


def feed(pcm: bytes) -> None:
    """16 kHz 16-bit mono from the session's microphone while a take is recorded."""
    with _lock:
        if _take["on"]:
            _take["chunks"].append(pcm)
            _take["fed"] = time.monotonic()


def record_samples(n: int = 4, seconds: float = 2.5, on_take=None, pause: float = 0.8) -> list[np.ndarray]:
    """Record `n` takes of the user saying the phrase; float32 -1..1, quiet trimmed.
    on_take(index, n, state) with state "say" (speak now) or "got" (take done)."""
    takes = []
    for i in range(n):
        if on_take:
            on_take(i, n, "say")
        with _lock:
            _take.update(on=True, chunks=[], fed=0.0)
        stream, audio = None, None
        began = time.monotonic()
        time.sleep(0.5)
        with _lock:
            fed = _take["fed"]
        if not fed:
            try:
                import pyaudio
                audio = pyaudio.PyAudio()
                stream = audio.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True, frames_per_buffer=1600)
            except Exception as error:
                log.info("wake word takes: no microphone (%s)", error)
        while time.monotonic() - began < seconds:
            if stream is not None:
                feed(stream.read(1600, exception_on_overflow=False))
            else:
                time.sleep(0.05)
        with _lock:
            _take["on"] = False
            pcm = b"".join(_take["chunks"])
        if stream is not None:
            stream.stop_stream()
            stream.close()
            audio.terminate()
        clip = _trim(np.frombuffer(pcm, np.int16))
        takes.append(clip.astype(np.float32) / 32768)
        if on_take:
            on_take(i, n, "got")
        time.sleep(pause)
    return takes
