"""On-device wake word detection.

While asleep, microphone audio goes only to this model - a small ONNX network
that runs locally in about a millisecond per frame. Nothing reaches the network
until the wake word fires, so an always-on microphone does not mean an
always-streaming one.
"""

from __future__ import annotations

import logging
import time

import numpy as np

log = logging.getLogger("mint.voice.wake")

# The frame size openWakeWord expects: 80ms of 16kHz mono audio.
FRAME_SAMPLES = 1280


class WakeWord:
    def __init__(self, name: str = "alexa", threshold: float = 0.5,
                 refractory: float = 2.0) -> None:
        from openwakeword.model import Model

        self.name = name
        self.threshold = threshold
        # After firing, ignore the model for a moment; one utterance otherwise
        # triggers several times as the phrase slides through the window.
        self.refractory = refractory
        self._last_fire = 0.0
        self._model = Model(wakeword_models=[name], inference_framework="onnx")
        self._buffer = np.empty(0, dtype=np.int16)
        log.info("wake word ready: %s (threshold %.2f)", name, threshold)

    def score(self, pcm: bytes) -> float:
        """Feed raw 16-bit mono PCM. Returns the highest score seen in it."""
        samples = np.frombuffer(pcm, dtype=np.int16)
        self._buffer = np.concatenate([self._buffer, samples])

        best = 0.0
        while len(self._buffer) >= FRAME_SAMPLES:
            frame, self._buffer = self._buffer[:FRAME_SAMPLES], self._buffer[FRAME_SAMPLES:]
            scores = self._model.predict(frame)
            best = max(best, float(scores.get(self.name, 0.0)))
        return best

    def heard(self, pcm: bytes) -> bool:
        """True when the wake word just fired."""
        now = time.monotonic()
        if now - self._last_fire < self.refractory:
            # Still keep the model fed so its internal window stays continuous.
            self.score(pcm)
            return False
        if self.score(pcm) >= self.threshold:
            self._last_fire = now
            self.reset()
            return True
        return False

    def reset(self) -> None:
        """Clear internal state so the next listen starts clean."""
        self._buffer = np.empty(0, dtype=np.int16)
        try:
            self._model.reset()
        except Exception:
            pass


class MintWake:
    """"Hey Mint": a small classifier over openWakeWord's speech features.

    No pretrained model exists for "Hey Mint", and an open-vocabulary keyword
    spotter (sherpa-onnx, GigaSpeech) caught only 21 of 36 test phrases - and
    missed the Indian English voices almost entirely. So this is trained:

    * models/hey_mint.json - shipped. Logistic regression on 16 frames (1.28 s)
      of openWakeWord embeddings, trained on "Hey Mint" from 12 macOS voices at
      four speeds against everyday speech, lookalikes ("hey man", "peppermint",
      "hey Mindy") and real LibriSpeech speakers (scripts/train_hey_mint.py).
    * hey_mint_personal.json - written by "Train my voice": the same, retrained
      with the user's own recordings. In testing: 5/5 of the user's phrases,
      1/40 lookalikes, 0 false wakes in 47 minutes of other people's speech.
    * models/wake_<phrase>.json - a phrase of the user's choosing ("Hey Jarvis"),
      the same kind of model, built on this Mac by wake_train.py. Several
      phrases can be on at once: each is one more dot product on the same
      features, and heard_phrase says which one fired.

    Same interface as WakeWord: heard(pcm), score(pcm), reset(), threshold.
    Inference is one dot product per 80 ms, on features the wake engine
    computes anyway.
    """

    GENERIC = None       # set below, once config is importable
    PERSONAL = None

    def __init__(self, threshold: float | None = None, refractory: float = 2.0) -> None:
        from mint.voice.features import Features

        self.name = "hey_mint"
        self.refractory = refractory
        self._last_fire = 0.0
        self._features = Features()
        self._buffer = np.empty(0, dtype=np.int16)
        self._consumed = 0                      # samples turned into feature frames so far
        self.phrase_end_lag: int | None = None  # at a fire: samples heard since "Hey Mint" ended
        self.heard_phrase: str | None = None    # which phrase fired, when several are on
        self.personal = False
        self._models: list[_Model] = []
        self.load()
        if threshold is not None:
            self.threshold = self._models[0].threshold = threshold

    def load(self) -> None:
        """The models for the wake phrases (prefs "wake_phrase" and "wake_models";
        see phrases()): for each, the user's own if trained, else one built on this
        Mac. A phrase with no model, or a broken one, is skipped; with none left,
        "Hey Mint" (the user's own, else the shipped one) - it always wakes."""
        from mint.core import config

        models = []
        for phrase in phrases():
            for path in _candidates(phrase):
                model = _Model.read(path, phrase)
                if model is not None:
                    models.append(model)
                    break
        if not models:
            root = config.PROJECT_ROOT
            for path in (root / "hey_mint_personal.json", root / "models" / "hey_mint.json"):
                model = _Model.read(path, "Hey Mint")
                if model is not None:
                    models.append(model)
                    break
        if not models:
            raise RuntimeError("no usable wake word model (models/hey_mint.json is missing or broken)")
        self._models = models
        first = models[0]
        # The first phrase's numbers, as before (enroll and the benches score with _probability).
        self._mean, self._scale, self._coef, self._bias = first.mean, first.scale, first.coef, first.bias
        self.threshold, self.need = first.threshold, first.need
        self.phrase = first.phrase
        self.personal = first.path.name.endswith("_personal.json")
        log.info("wake: %s", ", ".join(f"{m.phrase!r} ({m.path.name}, threshold {m.threshold:.2f})" for m in models))

    def _probability(self, window: np.ndarray) -> float:
        z = float(((window.reshape(-1) - self._mean) / self._scale) @ self._coef + self._bias)
        return 1.0 / (1.0 + np.exp(-max(-30.0, min(30.0, z))))

    def _windows(self, pcm: bytes):
        """Per 80 ms window: each phrase's score (one dot product each, same features)."""
        samples = np.frombuffer(pcm, dtype=np.int16)
        self._buffer = np.concatenate([self._buffer, samples])
        while len(self._buffer) >= FRAME_SAMPLES:
            frame, self._buffer = self._buffer[:FRAME_SAMPLES], self._buffer[FRAME_SAMPLES:]
            self._features(frame)
            self._consumed += FRAME_SAMPLES
            window = self._features.get_features(16)
            if window.shape[1] == 16:
                flat = window.reshape(-1)
                scores = []
                for model in self._models:
                    p = model.probability(flat)
                    model.recent.append((self._consumed, p))
                    if len(model.recent) > 16:
                        del model.recent[0]
                    scores.append(p)
                yield scores

    def score(self, pcm: bytes) -> float:
        best = 0.0
        for scores in self._windows(pcm):
            best = max(best, *scores)
        return best

    def heard(self, pcm: bytes) -> bool:
        now = time.monotonic()
        fired = None
        for scores in self._windows(pcm):
            for model, p in zip(self._models, scores):
                model.run = model.run + 1 if p >= model.threshold else 0
                if model.run >= model.need and now - self._last_fire >= self.refractory and fired is None:
                    fired = model
        if fired is not None:
            self._last_fire = now
            for model in self._models:
                model.run = 0
            self.heard_phrase = fired.phrase
            # Where the phrase ended, for phrase_cut: the window where this run
            # of scores first rose past 0.3. (The peak, and the fire itself,
            # come up to 0.7 s later when the request follows in one breath.)
            end = self._consumed + len(self._buffer)
            recent = fired.recent
            j = len(recent) - 1
            while j > 0 and recent[j - 1][1] >= 0.3:
                j -= 1
            self.phrase_end_lag = end - recent[j][0] if recent else None
        return fired is not None

    def reset(self) -> None:
        self._buffer = np.empty(0, dtype=np.int16)
        for model in self._models:
            model.run = 0
        try:
            self._features.reset()
        except Exception:
            pass


class _Model:
    """One wake phrase's classifier, and its own run of high scores."""

    SIZE = 16 * 96          # 16 windows of openWakeWord's 96-number embedding

    def __init__(self, path, phrase: str, data: dict) -> None:
        self.path, self.phrase = path, phrase
        self.mean = np.asarray(data["mean"], dtype=np.float32).reshape(-1)
        self.scale = np.asarray(data["scale"], dtype=np.float32).reshape(-1)
        self.coef = np.asarray(data["coef"], dtype=np.float32).reshape(-1)
        self.bias = float(data["intercept"])
        self.threshold = float(data.get("threshold", 0.9))
        self.need = int(data.get("need", 2))      # consecutive 80 ms windows above threshold
        self.run = 0
        self.recent: list[tuple[int, float]] = []  # (sample position of a window's end, score)

    @classmethod
    def read(cls, path, phrase: str):
        """The model at `path`, or None if it is missing or unusable (a half-written
        or hand-edited file must not take the wake word away)."""
        import json
        if path is None or not path.exists():
            return None
        try:
            model = cls(path, phrase, json.loads(path.read_text()))
            numbers = np.concatenate([model.mean, model.scale, model.coef, [model.bias, model.threshold]])
            if not (model.mean.size == model.scale.size == model.coef.size == cls.SIZE) \
                    or not np.all(np.isfinite(numbers)) or np.any(model.scale <= 0) \
                    or not 0 < model.threshold < 1 or not 1 <= model.need <= 8:
                raise ValueError("wrong shape or values")
            return model
        except Exception as error:
            log.warning("wake model %s unusable (%s); skipped", path, error)
            return None

    def probability(self, flat: np.ndarray) -> float:
        z = float(((flat - self.mean) / self.scale) @ self.coef + self.bias)
        return 1.0 / (1.0 + np.exp(-max(-30.0, min(30.0, z))))


def phrases() -> list[str]:
    """The phrases that should wake Mint, main one first: prefs "wake_phrase"
    (blank: "Hey <assistant name>", as before there was a setting) plus any in
    "wake_models" (e.g. ["Hey Mint"] to keep both)."""
    from mint.core import prefs
    main = str(prefs.get("wake_phrase") or "").strip() or f"Hey {prefs.name()}"
    extra = prefs.get("wake_models") or []
    out, seen = [], set()
    for phrase in [main, *(extra if isinstance(extra, list) else [])]:
        phrase = " ".join(str(phrase).split())
        if phrase and slug(phrase) not in seen:
            seen.add(slug(phrase))
            out.append(phrase)
    return out


def active_phrases() -> list[str]:
    """The phrases that will actually wake it: those with a model on disk, else "Hey Mint"."""
    ready_ = [p for p in phrases() if any(c is not None and c.exists() for c in _candidates(p))]
    return ready_ or ["Hey Mint"]


def _candidates(phrase: str) -> list:
    """Model files for a phrase, best first: the user's own ("Train my voice", or a
    custom phrase trained with their takes) - the newer of the two - then the
    general one (shipped for "Hey Mint", else built on this Mac by train_name)."""
    from mint.core import config
    word = slug(phrase)
    root = config.PROJECT_ROOT
    trained = root / "models" / f"wake_{word}.json"          # wake_train.train
    if word == "hey_" + slug(_display_name()):
        _paths()                                              # the assistant's name: as it always was
        personal, general = MintWake.PERSONAL, MintWake.GENERIC
    else:
        personal = root / f"{word}_personal.json"
        general = root / "models" / "hey_mint.json" if word == "hey_mint" else root / "wake" / f"{word}.json"
    own = sorted([p for p in (personal, trained) if p is not None and p.exists()],
                 key=lambda p: p.stat().st_mtime, reverse=True)
    return own + [general]


def slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "mint"


def _display_name() -> str:
    from mint.core import prefs
    return prefs.name()


def _set_paths(word: str) -> None:
    from mint.core import config
    if word == "mint":
        MintWake.GENERIC = config.PROJECT_ROOT / "models" / "hey_mint.json"
    else:
        MintWake.GENERIC = config.PROJECT_ROOT / "wake" / f"hey_{word}.json"
    MintWake.PERSONAL = config.PROJECT_ROOT / f"hey_{word}_personal.json"


def _paths() -> None:
    """Point MintWake at the models for the assistant's current name."""
    _set_paths(slug(_display_name()))


def generic_data_path(name: str):
    """Training windows for "Hey <name>" (positives) with the shipped negatives."""
    from mint.core import config
    word = slug(name)
    trained = config.PROJECT_ROOT / "models" / f"wake_hey_{word}_data.npz"      # wake_train.train
    if trained.exists():
        return trained
    if word == "mint":
        return config.PROJECT_ROOT / "models" / "hey_mint_data.npz"
    return config.PROJECT_ROOT / "wake" / f"hey_{word}_data.npz"


def ready(name: str) -> bool:
    """A model exists for "Hey <name>" (built by train_name or wake_train)."""
    return slug(name) == "mint" or any(p is not None and p.exists() for p in _candidates(f"Hey {name}"))


def train_name(name: str, progress=None) -> dict:
    """Build the general "Hey <name>" model on this Mac, the way "Hey Mint" was
    built (scripts/train_hey_mint.py): the Mac's own voices say it at four speeds
    and four noise levels; everyday speech, lookalikes and real speakers (the
    shipped negative windows) must not wake it. About a minute."""
    import json
    import subprocess
    import tempfile
    import wave

    from mint.voice.features import Features

    from mint.core import config
    from mint.voice import voicelock

    word = slug(name)
    phrase = f"Hey {name.strip()}"
    features = Features()
    shipped = np.load(config.PROJECT_ROOT / "models" / "hey_mint_data.npz")
    negatives = list(shipped["negatives"].astype(np.float32))
    # "Hey Mint" itself is now a lookalike, not the wake word.
    negatives += list(shipped["positives"][::4].astype(np.float32))
    positives = []
    seed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for v, voice in enumerate(voicelock._SYSTEM_VOICES):
            if progress:
                progress(f"Teaching the new wake word ({v + 1}/{len(voicelock._SYSTEM_VOICES)})…")
            for rate in (150, 175, 200, 225):
                aiff, wav = f"{tmp}/{v}_{rate}.aiff", f"{tmp}/{v}_{rate}.wav"
                try:
                    subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", aiff, phrase],
                                   check=True, capture_output=True, timeout=15)
                    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav],
                                   check=True, capture_output=True, timeout=15)
                except Exception:
                    continue
                w = wave.open(wav)
                clip = np.frombuffer(w.readframes(w.getnframes()), np.int16)
                for noise in NOISE_LEVELS:
                    seed += 1
                    positives += clip_windows(features, clip, True, noise=noise, seed=seed)
    if len(positives) < 40:
        raise RuntimeError("could not synthesise enough examples of the new wake word")
    model = train(positives, negatives, threshold=0.85)
    folder = config.PROJECT_ROOT / "wake"
    folder.mkdir(exist_ok=True)
    (folder / f"hey_{word}.json").write_text(json.dumps(model))
    np.savez_compressed(folder / f"hey_{word}_data.npz", positives=np.vstack(positives).astype(np.float16),
                        negatives=np.vstack(negatives).astype(np.float16))
    return {"phrase": phrase, "positive_windows": len(positives)}


_paths()


# --- training (used by scripts/train_hey_mint.py and by "Train my voice") -------------

NOISE_LEVELS = (0, 40, 150, 400)   # background hiss added in training: a real mic is never silent


def clip_windows(features, audio: np.ndarray, positive: bool, stride: int = 2,
                 noise: float = 0.0, seed: int = 0) -> list[np.ndarray]:
    """Feature windows from one clip, streamed exactly as the microphone is.
    Positives: the few windows ending just after the phrase; negatives: all.

    `noise` adds Gaussian background of that standard deviation (int16 units)
    to the whole clip. Trained on clean audio only, the detector scored a
    clean "Hey Mint" 0.95 but the same phrase under faint hiss (std 40, about
    -58 dBFS) only 0.70 - below its threshold - so training mixes levels."""
    features.reset()
    front, back = (1.0, 0.6) if positive else (1.0, 1.0)
    x = np.concatenate([np.zeros(int(16000 * front), np.float32), audio.astype(np.float32),
                        np.zeros(int(16000 * back), np.float32)])
    if noise:
        x = x + np.random.default_rng(seed).normal(0, noise, x.size)
    x = np.clip(x, -32768, 32767).astype(np.int16)
    out = []
    for i in range(0, x.size - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        features(x[i:i + FRAME_SAMPLES])
        window = features.get_features(16)
        out.append(window[0].reshape(-1) if window.shape[1] == 16 else None)
    if positive:
        end = int((16000 * front + audio.size) // FRAME_SAMPLES)
        return [w for w in out[max(0, end - 1):end + 3] if w is not None]
    return [w for w in out[16::stride] if w is not None]


def train(positives: list[np.ndarray], negatives: list[np.ndarray], threshold: float, C: float = 0.01) -> dict:
    """Logistic regression on feature windows -> the JSON MintWake loads."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    X = np.vstack(positives + negatives).astype(np.float32)
    y = np.array([1] * len(positives) + [0] * len(negatives))
    scaler = StandardScaler().fit(X)
    model = LogisticRegression(C=C, max_iter=3000, class_weight="balanced").fit(scaler.transform(X), y)
    return {"mean": [round(float(v), 5) for v in scaler.mean_],
            "scale": [round(float(v), 5) for v in scaler.scale_],
            "coef": [round(float(v), 6) for v in model.coef_[0]],
            "intercept": float(model.intercept_[0]), "threshold": threshold, "need": 2}


class PreRoll:
    """A short rolling buffer of recent audio.

    When the wake word fires, the words just after it are already spoken. This
    replays the last moment of audio into the session so "Hey Mint, open
    Finder" does not lose "open Finder".
    """

    def __init__(self, seconds: float = 1.0, rate: int = 16000) -> None:
        self._limit = int(seconds * rate * 2)  # 2 bytes per sample
        self._chunks: list[bytes] = []
        self._size = 0

    def add(self, pcm: bytes) -> None:
        self._chunks.append(pcm)
        self._size += len(pcm)
        while self._size > self._limit and self._chunks:
            self._size -= len(self._chunks.pop(0))

    def peek(self) -> bytes:
        return b"".join(self._chunks)

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        self._size = 0
        return data


def phrase_cut(pcm: bytes, lag: int | None) -> int:
    """Byte offset in `pcm` (the audio up to a wake-word fire) where the wake
    phrase ends, so only what follows goes to Gemini. Heard, "Hey Mint" came
    back as "payment" (most days), "hymen", "Hey man" - and the model then
    answered about a electricity payment.

    `lag`: MintWake.phrase_end_lag, samples since its scores began to rise.
    From there, the first 40 ms of quiet within -50..+300 ms is the gap after
    "Mint"; with none, +150 ms. On the user's own recordings, each "Hey Mint"
    followed by one of their sentences after 0-500 ms (bench/wake_trim_bench.py):
    median 60 ms early (the "t" may stay), 35 of 37 within 120 ms, worst 380 ms
    late (the first syllable of the request lost). No lag (a built-in
    openWakeWord model): nothing is cut."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    hop = 160                                             # 10 ms
    count = samples.size // hop
    if lag is None or count < 30:
        return 0
    energy = np.sqrt(np.mean(samples[:count * hop].reshape(count, hop) ** 2, axis=1))
    floor, peak = np.percentile(energy, 10), np.percentile(energy, 98)
    voiced = energy > max(3 * floor, 0.08 * peak, 60.0)
    rise = max(0, count - int(lag) // hop)
    for i in range(max(0, rise - 5), min(count - 4, rise + 30)):
        if not voiced[i:i + 4].any():
            return (i + 1) * hop * 2
    return min(count, rise + 15) * hop * 2


def rms(pcm: bytes) -> float:
    """Loudness of a frame, 0..1. Used to notice that someone is still talking."""
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)) / 32768.0)
