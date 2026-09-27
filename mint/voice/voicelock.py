"""Voice lock: only the enrolled user's speech reaches Gemini.

A CAM++ speaker model (3D-Speaker's "common advanced" build, trained on about
200k speakers) turns a stretch of speech into a voiceprint in ~9 ms. Your
enrolment is the average of your voiceprints; every new stretch of speech is
compared with it (cosine similarity) before any of it leaves the Mac.

Benchmarked on 26 real LibriSpeech speakers (bench/voicelock_bench.py), with
the false-reject rate held at 3%:
    0.8 s of speech -> 1.9% false accepts, 1.2 s -> 0.1%, 2.0 s -> 0.0%.
Three other public models (VoxCeleb CAM++, WeSpeaker CAM++, ResNet34) scored
6-46% equal error rates on the same test, so the model choice is not cosmetic.

The Gate holds the start of each utterance until it is sure who is talking,
then sends the held audio in one burst and streams the rest live. For anything
longer than about a second, Gemini has caught up before the sentence ends, so
the lock adds no delay you can feel. Clear cases are decided at 0.8 s; unclear
ones get up to 1.6 s. If someone else carries on straight after you, the
stream closes again mid-utterance.

Memory: the model (28 MB) loads only when the lock is on and a voiceprint
exists - about +90 MB resident in testing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import numpy as np

from mint.core import config

log = logging.getLogger("mint.voice.voicelock")

RATE = 16000
FEATURES = "knf-80-nyquist-v1"
MODEL = config.PROJECT_ROOT / "models" / "campplus.onnx"
PROFILE = config.PROJECT_ROOT / "voiceprint.json"

# Thresholds by length of speech, set from real speakers (8 enrolled users,
# 48 impostor clips, bench/voicelock_bench.py): the highest impostor scored
# 0.26 at 0.8 s, 0.27 at 1.2 s, 0.26 at 1.6 s and 0.29 at 2.4 s, while 95% of
# the users' own speech scored above 0.39 / 0.48 / 0.55 / 0.62. Enrolment
# lowers these for a voice that scores lower against itself (a noisy room, a
# far microphone) - never below FLOOR, and never raises them.
BASE = {0.5: 0.32, 0.8: 0.33, 1.2: 0.35, 1.6: 0.36, 2.4: 0.37}
# Early decisions need a clear margin; the last check decides on the threshold.
OPEN_EARLY = {0.8: 0.40, 1.2: 0.42}
# Calibration never goes below these: just above the highest impostor seen.
FLOOR = {0.5: 0.30, 0.8: 0.31, 1.2: 0.32, 1.6: 0.32, 2.4: 0.33}


def strictness() -> float:
    """Settings ▸ Voice ▸ Strictness, as a shift of every threshold."""
    from mint.core import prefs
    return {"relaxed": -0.05, "strict": 0.06}.get(str(prefs.get("lock_strictness")), 0.0)


def _interp(table: dict, seconds: float) -> float:
    keys = sorted(table)
    if seconds <= keys[0]:
        return table[keys[0]]
    if seconds >= keys[-1]:
        return table[keys[-1]]
    for a, b in zip(keys, keys[1:]):
        if a <= seconds <= b:
            t = (seconds - a) / (b - a)
            return table[a] + t * (table[b] - table[a])
    return table[keys[-1]]


def to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def voiced(audio: np.ndarray, floor: float = 0.01) -> np.ndarray:
    """Only the parts with speech in them: silence dilutes a voiceprint."""
    frames = [audio[i:i + 320] for i in range(0, audio.size - 319, 320)]
    loud = [f for f in frames if np.sqrt(np.mean(f ** 2)) > floor]
    return np.concatenate(loud) if len(loud) >= 10 else audio


_IMPOSTORS: list[np.ndarray] | None = None
_SENTENCES: list[np.ndarray] | None = None
_SYSTEM_VOICES = ["Rishi", "Aman", "Daniel", "Samantha", "Karen", "Moira", "Tessa", "Eddy (English (US))",
                  "Flo (English (UK))", "Reed (English (US))", "Sandy (English (US))", "Shelley (English (UK))"]


def _say(voice: str, text: str, rate: int | None = None) -> np.ndarray | None:
    import subprocess
    import tempfile
    import wave

    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = f"{tmp}/a.aiff", f"{tmp}/a.wav"
        try:
            subprocess.run(["say", "-v", voice, "-o", aiff] + (["-r", str(rate)] if rate else []) + [text],
                           check=True, capture_output=True, timeout=15)
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav],
                           check=True, capture_output=True, timeout=15)
            w = wave.open(wav)
            return to_float(w.readframes(w.getnframes()))
        except Exception:
            return None               # a voice that is not installed


def impostor_sentences() -> list[np.ndarray]:
    """Everyday sentences in the Mac's own voices, for calibrating the lock."""
    global _SENTENCES
    if _SENTENCES is None:
        text = ("Can you open my calendar and tell me what is on for tomorrow morning, "
                "and remind me to call the bank at four.")
        _SENTENCES = [a for a in (_say(v, text) for v in _SYSTEM_VOICES) if a is not None]
    return _SENTENCES


def impostor_wakes() -> list[np.ndarray]:
    """"Hey Mint" in the Mac's own voices (Indian, US, UK, Irish, Australian
    English, male and female) - stand-ins for other people in the room."""
    global _IMPOSTORS
    if _IMPOSTORS is None:
        _IMPOSTORS = [a for v in _SYSTEM_VOICES for r in (165, 205)
                      for a in [_say(v, "Hey Mint", r)] if a is not None]
    return _IMPOSTORS


RECORDINGS = config.PROJECT_ROOT / "voice" / "enrolment.npz"


def save_recordings(sentences: list[np.ndarray], wakes: list[np.ndarray]) -> None:
    """Keep the enrolment audio on this Mac (mode 600), so a better calibration
    later does not need the user to train again."""
    RECORDINGS.parent.mkdir(exist_ok=True)
    os.chmod(RECORDINGS.parent, 0o700)
    np.savez_compressed(RECORDINGS, **{f"s{i}": s.astype(np.float16) for i, s in enumerate(sentences)},
                        **{f"w{i}": w.astype(np.float16) for i, w in enumerate(wakes)})
    os.chmod(RECORDINGS, 0o600)


def load_recordings() -> tuple[list[np.ndarray], list[np.ndarray]] | None:
    try:
        data = np.load(RECORDINGS)
    except OSError:
        return None
    sentences = [data[k].astype(np.float32) for k in sorted(data.files, key=lambda k: int(k[1:])) if k[0] == "s"]
    wakes = [data[k].astype(np.float32) for k in sorted(data.files, key=lambda k: int(k[1:])) if k[0] == "w"]
    return sentences, wakes


class VoiceLock:
    def __init__(self) -> None:
        self._session = None
        self._lock = threading.Lock()
        self.centroid: np.ndarray | None = None
        self.thresholds: dict[float, float] = dict(BASE)
        self.wake_threshold = 0.40          # "Hey Mint" against the sentence voiceprint
        self.templates: np.ndarray | None = None   # the user's own "Hey Mint"s (voiceprints)
        self.template_threshold = 0.6       # "Hey Mint" against those
        self.level = 0.0            # typical loudness of the user's speech at the mic
        self.enrolled_at = ""
        self.stale = False          # a voiceprint from an older version: retrain
        self.nearest_voice: dict = {}
        self._load_profile()

    # --- model ------------------------------------------------------------------

    def _model(self):
        """The CAM++ network on the onnxruntime Mint already has loaded (for the
        wake word). sherpa-onnx was used first; it brings a second runtime
        (+48 MB) and its feature settings did not match the model's training:
        on the same 26 speakers it let 1.9% of impostors through at 0.8 s of
        speech, against 0.4% for the Kaldi-standard features below."""
        if self._session is None:
            import onnxruntime as ort
            started = time.monotonic()
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.enable_cpu_mem_arena = False      # a few MB less, no measurable cost
            self._session = ort.InferenceSession(str(MODEL), options, providers=["CPUExecutionProvider"])
            log.info("speaker model loaded in %.2fs", time.monotonic() - started)
        return self._session

    @staticmethod
    def features(samples: np.ndarray) -> np.ndarray:
        """80 log-mel filterbanks, 25 ms / 10 ms, Kaldi-standard, mean-normalised."""
        import kaldi_native_fbank as knf

        opts = knf.FbankOptions()
        opts.frame_opts.dither = 0
        opts.frame_opts.snip_edges = False
        opts.mel_opts.num_bins = 80
        opts.mel_opts.high_freq = 0          # up to Nyquist, as in 3D-Speaker's training
        bank = knf.OnlineFbank(opts)
        bank.accept_waveform(RATE, samples.astype(np.float32).tolist())
        bank.input_finished()
        frames = np.array([bank.get_frame(i) for i in range(bank.num_frames_ready)], dtype=np.float32)
        return frames - frames.mean(axis=0, keepdims=True)

    def embed(self, samples: np.ndarray) -> np.ndarray:
        feats = self.features(samples)
        with self._lock:
            vector = self._model().run(None, {"x": feats[None]})[0][0]
        return vector / (np.linalg.norm(vector) or 1.0)

    # --- profile ----------------------------------------------------------------

    @property
    def enrolled(self) -> bool:
        return self.centroid is not None

    def _load_profile(self) -> None:
        try:
            data = json.loads(PROFILE.read_text())
            if data.get("features") != FEATURES:
                # Made with sherpa-onnx's features: its numbers mean nothing to
                # this model setup. Kept on disk; the user is asked to retrain.
                self.stale = True
                return
            self.centroid = np.asarray(data["centroid"], dtype=np.float32)
            self.thresholds = {float(k): float(v) for k, v in data.get("thresholds", {}).items()} or dict(BASE)
            self.wake_threshold = float(data.get("wake_threshold", 0.45))
            if data.get("templates"):
                self.templates = np.asarray(data["templates"], dtype=np.float32)
                self.template_threshold = float(data.get("template_threshold", 0.6))
            else:
                # A voiceprint from before the wake phrase was checked on its own
                # (its wake threshold came out at 0.29 and let others in).
                self.wake_threshold = max(self.wake_threshold, 0.45)
            self.level = float(data.get("level", 0.0))
            self.enrolled_at = data.get("enrolled_at", "")
        except FileNotFoundError:
            pass
        except Exception as error:
            log.warning("voiceprint unreadable, ignoring it: %s", error)

    def forget(self) -> None:
        self.centroid = None
        self.thresholds = dict(BASE)
        try:
            PROFILE.unlink()
        except OSError:
            pass

    def threshold(self, seconds: float) -> float:
        return _interp(self.thresholds, seconds) + strictness()

    # --- "Hey Mint": is it the user saying it? ------------------------------------

    def template_score(self, samples: np.ndarray) -> float:
        """Against the user's own recorded "Hey Mint"s: the mean of the three best
        matches. Same words, same voice - far sharper on a short phrase than the
        sentence voiceprint (in testing 0.94 vs 0.46 for the user's plain "Hey
        Mint", while 22 other voices peaked at 0.45)."""
        if self.templates is None or samples.size < RATE * 0.25:
            return 0.0
        scores = np.sort(self.templates @ self.embed(samples))
        return float(np.mean(scores[-3:]))

    def wake_verdict(self, samples: np.ndarray) -> tuple[str, float, float]:
        """"yes", "no" or "maybe" for a wake phrase, with (template, voiceprint) scores.
        Either check clearly passing is enough: the template one is best for a
        bare "Hey Mint", the voiceprint for "Hey Mint, open Slack…"."""
        t = self.template_score(samples)
        c = self.score(samples)
        if self.templates is None:
            return ("yes" if c >= self.wake_threshold else "maybe" if c >= self.wake_threshold - 0.10 else "no"), t, c
        # The voiceprint alone may wake only if the phrase also resembles the
        # user's own "Hey Mint": in testing, a voice close to the user's (two
        # Indian English voices) passed on the voiceprint (0.44) while its
        # phrase scored 0.26.
        t_bar, c_bar = self.template_threshold + strictness(), self.wake_threshold + strictness()
        if t >= t_bar or (c >= c_bar and t >= t_bar - 0.2):
            return "yes", t, c
        if t >= t_bar - 0.15 or c >= c_bar - 0.10:
            return "maybe", t, c
        return "no", t, c

    def _calibrate_wake(self, centroid: np.ndarray, wake_clips: list[np.ndarray]) -> dict:
        """Thresholds for the wake check, from the user's own "Hey Mint"s (left
        one out each) against "Hey Mint" said by the Mac's other voices. The
        first enrolment put this at 0.29 against the voiceprint alone, which
        let other people's "Hey Mint" through."""
        clips = [voiced(c) for c in wake_clips if c.size > RATE * 0.3]
        impostors = [voiced(c) for c in impostor_wakes()]
        report: dict = {}
        if len(clips) >= 4:
            templates = np.vstack([self.embed(c) for c in clips])
            own = []
            for i in range(len(templates)):
                others = np.delete(templates, i, axis=0)
                own.append(float(np.mean(np.sort(others @ templates[i])[-3:])))
            self.templates = templates
            fake = [float(np.mean(np.sort(templates @ self.embed(c))[-3:])) for c in impostors]
            low = sorted(own)[max(0, len(own) // 8 - 1)]
            # The 90th percentile, not the maximum: one synthetic voice that
            # happens to sound like the user should not lock the user out.
            top = float(np.percentile(fake, 90)) if fake else 0.45
            self.template_threshold = round(min(max(top + 0.08, 0.5), 0.85), 3)
            if self.template_threshold > low - 0.03:  # never above the user's own recordings
                self.template_threshold = round(max(0.5, min(low - 0.03, (top + low) / 2)), 3)
            report.update(template_threshold=self.template_threshold, your_template_scores=[round(x, 2) for x in own],
                          impostor_template_p90=round(top, 2))
        fake_c = [float(self.embed(c) @ centroid) for c in impostors]
        top_c = float(np.percentile(fake_c, 90)) if fake_c else 0.30
        self.wake_threshold = round(min(max(0.36, top_c + 0.08), 0.6), 3)
        report.update(wake_threshold=self.wake_threshold,
                      impostor_voiceprint_p90=round(top_c, 2) if fake_c else None)
        return report

    def score(self, samples: np.ndarray) -> float:
        if self.centroid is None or samples.size < RATE * 0.25:
            return 0.0
        return float(self.embed(samples) @ self.centroid)

    def enroll(self, sentences: list[np.ndarray], wake_clips: list[np.ndarray]) -> dict:
        """Build the voiceprint from read sentences (and wake-word clips), and
        calibrate thresholds on the user's own held-out speech."""
        speech = np.concatenate(sentences) if sentences else np.zeros(0, np.float32)
        if speech.size < RATE * 12:
            raise ValueError("Not enough speech to enrol - about 20 seconds of reading is needed.")
        # Voiceprints of 3 s pieces; the centroid is their mean.
        pieces = [speech[i:i + 3 * RATE] for i in range(0, speech.size - 2 * RATE, 3 * RATE)]
        vectors = [self.embed(p) for p in pieces]
        centroid = np.mean(vectors, axis=0)
        centroid /= np.linalg.norm(centroid)

        # Calibrate: score short crops of each sentence against a centroid that
        # left that sentence out, so the numbers are honest.
        own: dict[float, list[float]] = {s: [] for s in BASE}
        for index, sentence in enumerate(sentences):
            others = [s for j, s in enumerate(sentences) if j != index]
            if not others:
                continue
            rest = np.concatenate(others)
            ref = np.mean([self.embed(rest[i:i + 3 * RATE])
                           for i in range(0, rest.size - 2 * RATE, 3 * RATE)], axis=0)
            ref /= np.linalg.norm(ref)
            for seconds in BASE:
                n = int(seconds * RATE)
                for start in range(int(0.2 * RATE), max(int(0.2 * RATE) + 1, sentence.size - n), n):
                    crop = sentence[start:start + n]
                    if crop.size == n:
                        own[seconds].append(float(self.embed(crop) @ ref))
        # Voices like the user's: the Mac's own voices reading the same kind
        # of sentences, scored at each length. A voice close to the user's
        # (same accent, same sex) raises the bar for this user - as far as the
        # user's own speech allows.
        near: dict[float, float] = {}
        for clip in impostor_sentences():
            for seconds in BASE:
                n = int(seconds * RATE)
                for start in range(int(0.3 * RATE), max(int(0.3 * RATE) + 1, clip.size - n), n):
                    crop = clip[start:start + n]
                    if crop.size == n:
                        near[seconds] = max(near.get(seconds, -1.0), float(self.embed(crop) @ centroid))
        thresholds = {}
        for seconds, base in BASE.items():
            scores = sorted(own[seconds])
            wanted = max(base, near.get(seconds, -1.0) + 0.08)
            if len(scores) >= 4:
                # Accept at least ~95% of the user's own speech of this length,
                # never below the impostor floor.
                fifth = scores[max(0, int(len(scores) * 0.05) - 1)]
                thresholds[seconds] = round(max(FLOOR[seconds], min(wanted, fifth - 0.02)), 3)
            else:
                thresholds[seconds] = base
        self.nearest_voice = {k: round(v, 2) for k, v in near.items()}
        level = float(np.median([np.sqrt(np.mean(s ** 2)) for s in sentences]))
        wake = self._calibrate_wake(centroid, wake_clips)

        self.centroid, self.thresholds, self.level = centroid, thresholds, level
        self.enrolled_at = time.strftime("%Y-%m-%d %H:%M")
        self.stale = False
        PROFILE.write_text(json.dumps({
            "centroid": [round(float(x), 6) for x in centroid],
            "thresholds": {str(k): v for k, v in thresholds.items()},
            "wake_threshold": round(self.wake_threshold, 3),
            "templates": [[round(float(x), 6) for x in t] for t in self.templates] if self.templates is not None else [],
            "template_threshold": round(self.template_threshold, 3),
            "level": round(level, 5),
            "enrolled_at": self.enrolled_at, "seconds": round(speech.size / RATE, 1),
            "features": FEATURES,
        }))
        os.chmod(PROFILE, 0o600)
        spread = float(np.mean([v @ centroid for v in vectors]))
        return {"seconds": round(speech.size / RATE, 1), "consistency": round(spread, 3),
                "thresholds": thresholds, "nearest_system_voice": self.nearest_voice, **wake}


class Gate:
    """Decides, as audio streams in, which of it is the user's.

    feed() takes 16 kHz 16-bit mono PCM with its loudness and returns what to
    send to Gemini now - the user's audio (possibly a burst of held audio), or
    silence in place of anything else - plus an event:
    "pending" (speech started, held), "open" (verified: sent), "closed"
    (someone else: dropped), "end" (the utterance finished), or None.
    """

    PREROLL = 0.30        # seconds kept from just before speech starts
    HANGOVER = 0.45       # quiet that ends an utterance
    CHECKS = (0.8, 1.2, 1.6)
    RECHECK = 1.6         # while open, re-verify every this much speech
    TRUST = 1.5           # a pause shorter than this keeps the speaker verified

    def __init__(self, lock: VoiceLock) -> None:
        self.lock = lock
        self.noise = 0.004
        self._reset()
        self.last_score = 0.0
        self.last_decision_ms = 0.0
        self._trusted_until = 0.0
        self._trusted = False
        self._strict = 0.0         # extra margin while confirming an unsure wake
        self._clock = 0.0          # seconds of audio fed: pauses are measured in audio time

    def _reset(self) -> None:
        self.state = "idle"        # idle | pending | open | closed
        self._held: list[bytes] = []
        self._voiced = 0.0         # seconds of speech in this utterance
        self._quiet = 0.0
        self._loud_run = 0
        self._pre: list[bytes] = []
        self._pre_len = 0.0
        self._next_check = 0
        self._since_check = 0.0
        self._started = 0.0
        self._utterance: list[bytes] = []

    def speech_threshold(self) -> float:
        return max(0.010, self.noise * 3.5)

    def _decide(self, final: bool = False) -> str | None:
        audio = to_float(b"".join(self._utterance))
        seconds = self._voiced
        started = time.perf_counter()
        score = self.lock.score(audio)
        self.last_decision_ms = (time.perf_counter() - started) * 1000
        self.last_score = score
        threshold = self.lock.threshold(seconds) + self._strict
        early = threshold if self._trusted else max(threshold + 0.06, _interp(OPEN_EARLY, seconds))
        if score >= threshold if final else score >= early:
            return "open"
        if score < threshold - 0.15 or final:
            return "closed"
        return None               # not sure yet: wait for more speech

    def feed(self, pcm: bytes, level: float) -> tuple[bytes, str | None]:
        seconds = len(pcm) / 2 / RATE
        self._clock += seconds
        speaking = level > self.speech_threshold()
        if not speaking and self.state == "idle":
            # Learn the room while nobody is talking.
            self.noise = 0.97 * self.noise + 0.03 * min(level, 0.03)

        if self.state == "idle":
            self._pre.append(pcm)
            self._pre_len += seconds
            while self._pre_len > self.PREROLL and len(self._pre) > 1:
                self._pre_len -= len(self._pre.pop(0)) / 2 / RATE
            self._loud_run = self._loud_run + 1 if speaking else 0
            if self._loud_run >= 2:           # two loud frames in a row: speech, not a click
                self._utterance = list(self._pre)
                self._voiced = self._pre_len
                self._next_check = 0
                self._started = time.monotonic()
                # The user only paused mid-thought: still verify - nothing
                # unverified leaves the Mac - but on the plain threshold at the
                # first check, rather than waiting for a wide margin.
                self._trusted = self._clock < self._trusted_until
                self.state = "pending"
                self._held = list(self._pre)
                return b"", "pending"
            # Nobody is talking: send silence, not nothing. Gemini decides the
            # user has finished by hearing ~0.7 s of quiet; with no audio at
            # all it waited indefinitely in testing and never answered.
            return b"\x00" * len(pcm), None

        # Inside an utterance.
        self._utterance.append(pcm)
        if speaking:
            self._voiced += seconds
            self._quiet = 0.0
        else:
            self._quiet += seconds
        ended = self._quiet >= self.HANGOVER

        if self.state == "pending":
            self._held.append(pcm)
            decision = None
            if ended:
                decision = self._decide(final=True) if self._voiced >= 0.3 else "closed"
            elif self._next_check < len(self.CHECKS) and self._voiced >= self.CHECKS[self._next_check]:
                final = self._next_check == len(self.CHECKS) - 1
                self._next_check += 1
                decision = self._decide(final=final)
            if decision == "open":
                burst = b"".join(self._held)
                self._held = []
                self.state = "open"
                self._since_check = 0.0
                if ended:
                    self._trusted_until = self._clock + self.TRUST
                    self._finish()
                    return burst, "open+end"
                return burst, "open"
            if decision == "closed":
                self._held = []
                self.state = "closed"
                if ended:
                    self._finish()
                    return b"", "closed+end"
                return b"", "closed"
            return b"", None

        if self.state == "open":
            if speaking:
                self._since_check += seconds
            if self._since_check >= self.RECHECK and not ended:
                # Someone else may have carried straight on after the user.
                self._since_check = 0.0
                recent = to_float(b"".join(self._utterance)[-int(self.RECHECK * RATE * 2):])
                score = self.lock.score(recent)
                self.last_score = score
                if score < self.lock.threshold(self.RECHECK) - 0.1:
                    self.state = "closed"
                    self._trusted_until = 0.0
                    return b"\x00" * len(pcm), "closed"
            if ended:
                self._trusted_until = self._clock + self.TRUST
                self._finish()
                return pcm, "end"
            return pcm, None

        # closed: the other person's words become silence, so Gemini's own
        # voice detection hears the utterance end rather than hanging open.
        if ended:
            self._finish()
            return b"\x00" * len(pcm), "end"
        return b"\x00" * len(pcm), None

    def _finish(self) -> None:
        noise = self.noise
        self._strict = 0.0
        self._reset()
        self.noise = noise

    def begin_pending(self, pcm: bytes) -> None:
        """Hold audio already heard (an unsure "Hey Mint") as the start of an
        utterance, and decide on it together with whatever follows - on a
        higher bar than ordinary speech, since the phrase itself was doubtful."""
        self._reset()
        self._strict = 0.10
        self.state = "pending"
        self._held = [pcm]
        self._utterance = [pcm]
        loud = voiced(to_float(pcm), floor=self.speech_threshold())
        self._voiced = min(loud.size / RATE, 0.79)       # the first check comes with the next words
        self._next_check = 0

    def force_open(self) -> None:
        """The user was just verified another way (the wake phrase): let the
        rest of this utterance stream at once, re-checking as usual."""
        self._reset()
        self.state = "open"
        self._since_check = 0.0

    def cancel(self) -> None:
        """Drop whatever is held (sleep, pause, a new session)."""
        self._trusted_until = 0.0
        self._finish()


lock = VoiceLock()
