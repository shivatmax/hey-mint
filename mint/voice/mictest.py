"""The microphone test: is Mint hearing you, and if not, why.

Settings ▸ Microphone & voice ▸ Test microphone, and the "Say Hey Mint" step of
onboarding. For a few seconds it listens to exactly what Mint hears (the
session's own microphone stream, session._mic_probes) while the user says the
wake word, and reports in plain words:

* the level, live, and whether "Hey Mint" was caught (the wake word model's
  score against its threshold);
* with the voice lock on, whether the voice check would let that "Hey Mint" in;
* what is in the way otherwise - Mint's microphone switched off, macOS not
  allowing it, a call app holding the microphone, no sound at all, digital
  silence (a muted or taken microphone), or too quiet.

While a test runs Mint does not act on what it hears.

`diagnose(facts)` is the plain-words part, free of audio so it can be tested.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger("mint.voice.mictest")

SECONDS = 8.0
QUIET = 0.008          # rms (0..1): below this at its loudest, nobody spoke near the mic
SPEECH = 0.02          # rms: someone spoke


def permission() -> str:
    """macOS microphone permission for this app: allowed | denied | ask | unknown."""
    try:
        import AVFoundation
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
        return {3: "allowed", 2: "denied", 1: "denied", 0: "ask"}.get(int(status), "unknown")
    except Exception:
        return "unknown"


def diagnose(facts: dict) -> list[tuple[str, str]]:
    """[(kind, words)] for what the test found, most important first. kind: ok | problem | tip.

    facts: mic_on, permission, lent_to, in_call, frames, zeros, peak, speech, wake_best, wake_need,
    heard, phrase, lock_on, lock_verdict, lock_score, device."""
    found: list[tuple[str, str]] = []
    phrase = facts.get("phrase") or "Hey Mint"
    if not facts.get("mic_on", True):
        return [("problem", "Mint's microphone is switched off. Turn it on (the mic button, or the menu bar) and "
                            "test again.")]
    if facts.get("permission") == "denied":
        return [("problem", "macOS isn't letting Mint use the microphone. Allow Mint in System Settings ▸ Privacy "
                            "& Security ▸ Microphone, then test again.")]
    if facts.get("lent_to"):
        return [("problem", f"{facts['lent_to']} is using the microphone, and Mint is set to step aside during "
                            "calls. Turn on “Listen during calls” to keep the wake word working.")]
    if not facts.get("frames"):
        return [("problem", "No sound at all reached Mint. Check the microphone in Settings ▸ Microphone & voice (or System "
                            "Settings ▸ Sound ▸ Input), and that it is plugged in.")]
    if facts.get("zeros", 0) > 0.9:
        where = f" - {facts['in_call']} may have it to itself during the call" if facts.get("in_call") else ""
        return [("problem", "The microphone is giving pure silence: it is muted or taken by another app" + where +
                            ". Unmute it (a mute key or switch), or end the other app's use of it.")]
    device = f" ({facts['device']})" if facts.get("device") else ""
    if facts.get("peak", 0) < QUIET:
        found.append(("problem", f"Very quiet{device}: Mint barely heard anything. Speak closer, raise the input "
                                 "level in System Settings ▸ Sound ▸ Input, or choose another microphone in "
                                 "Settings ▸ Microphone & voice."))
    elif facts.get("heard"):
        found.append(("ok", f"Heard “{phrase}” (score {facts['wake_best']:.2f}, needs {facts['wake_need']:.2f})."))
    elif facts.get("speech"):
        found.append(("problem", f"Heard you, but not “{phrase}” (best {facts.get('wake_best', 0):.2f}, needs "
                                 f"{facts.get('wake_need', 0.85):.2f}). Say it clearly, as two words; “Record 4 takes” "
                                 "below teaches it your voice."))
    else:
        found.append(("problem", f"Didn't hear anyone speak. Say “{phrase}” while the test runs."))
    if facts.get("heard") and facts.get("lock_on"):
        verdict, score = facts.get("lock_verdict"), facts.get("lock_score", 0.0)
        if verdict == "yes":
            found.append(("ok", f"Voice lock: recognised your voice ({score:.2f})."))
        elif verdict == "maybe":
            found.append(("tip", f"Voice lock: not sure it was you ({score:.2f}) - it then checks the next second "
                                 "of speech. If it often misses you, retrain your voice or choose Relaxed."))
        else:
            found.append(("problem", f"Voice lock: did not recognise your voice ({score:.2f}), so this “{phrase}” "
                                     "would be ignored. Retrain your voice where you usually sit, choose Relaxed, or "
                                     "turn the voice lock off."))
    if facts.get("in_call") and facts.get("heard"):
        found.append(("tip", f"{facts['in_call']} is in a call: Mint listens for the wake word only, without echo "
                             "cancellation, until the call ends."))
    return found


class MicTest:
    """One test run. Feeds on the session's microphone stream; reports on the caller's callbacks
    (any thread - the UI hops to the main thread itself).

    on_level(level 0..1, wake score, threshold) about ten times a second; on_done(findings, facts)."""

    def __init__(self, session, on_level=None, on_done=None, seconds: float = SECONDS) -> None:
        self.session, self.on_level, self.on_done, self.seconds = session, on_level, on_done, seconds
        self._lock = threading.Lock()
        self._frames = 0
        self._zero_frames = 0
        self._peak = 0.0
        self._speech = False
        self._best = 0.0
        self._heard = False
        self._audio: list[bytes] = []
        self._fired_audio = b""
        self._last_report = 0.0
        self._wake = None
        self._done = threading.Event()

    def start(self) -> None:
        from mint.voice import wake
        try:
            self._wake = wake.MintWake()
        except Exception as error:
            log.warning("mic test: no wake word model (%s)", error)
        session = self.session
        if session is not None:
            session._mic_testing = True
            session._mic_probes.append(self._probe)
        threading.Thread(target=self._run, daemon=True, name="mic-test").start()

    def _probe(self, pcm: bytes) -> None:
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size == 0:
            return
        level = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)) / 32768.0)
        score = 0.0
        if self._wake is not None:
            for scores in self._wake._windows(pcm):
                score = max(score, *scores)
                for model, p in zip(self._wake._models, scores):
                    model.run = model.run + 1 if p >= model.threshold else 0
                    if model.run >= model.need and not self._heard:
                        self._heard = True
                        self._fired_audio = b"".join(self._audio[-25:]) + pcm
        with self._lock:
            self._frames += 1
            self._zero_frames += 0 if samples.any() else 1
            self._peak = max(self._peak, level)
            self._speech = self._speech or level > SPEECH
            self._best = max(self._best, score)
            self._audio.append(pcm)
            if len(self._audio) > 120:
                del self._audio[0]
        now = time.monotonic()
        if self.on_level is not None and now - self._last_report > 0.08:
            self._last_report = now
            self.on_level(min(1.0, level * 8), score, self._need())

    def _need(self) -> float:
        return float(getattr(self._wake, "threshold", 0.85) or 0.85)

    def _run(self) -> None:
        started = time.monotonic()
        while time.monotonic() - started < self.seconds and not self._done.is_set():
            time.sleep(0.1)
            if self._heard and time.monotonic() - started > 2.0:
                time.sleep(0.6)                       # the rest of the phrase, then report
                break
        self.stop()

    def stop(self) -> None:
        if self._done.is_set():
            return
        self._done.set()
        session = self.session
        if session is not None:
            try:
                session._mic_probes.remove(self._probe)
            except ValueError:
                pass
            session._mic_testing = False
        facts = self.facts()
        findings = diagnose(facts)
        log.info("mic test: %s -> %s", {k: v for k, v in facts.items() if k != "audio"}, findings)
        if self.on_done is not None:
            self.on_done(findings, facts)

    def facts(self) -> dict:
        from mint.voice import devices as audio_devices
        from mint.core import prefs
        session = self.session
        with self._lock:
            frames, zeros, peak, speech, best = self._frames, self._zero_frames, self._peak, self._speech, self._best
        facts = {
            "mic_on": bool(prefs.get("mic")) and not getattr(session, "paused", False),
            "permission": permission(),
            "lent_to": getattr(session, "_mic_lent_to", ""),
            "in_call": getattr(session, "_in_call", ""),
            "frames": frames, "zeros": zeros / frames if frames else 0.0, "peak": peak, "speech": speech,
            "wake_best": best, "wake_need": self._need(), "heard": self._heard,
            "phrase": getattr(self._wake, "phrase", "Hey Mint"),
            "lock_on": getattr(session, "_gate", None) is not None,
        }
        try:
            device = audio_devices.describe(audio_devices.default_device(output=False))
            uid = str(prefs.get("input_device") or "")
            if uid:
                device = audio_devices.device_by_uid(uid) or device
            facts["device"] = device["name"] if device else ""
        except Exception:
            facts["device"] = ""
        if self._heard and facts["lock_on"]:
            try:
                from mint.voice import voicelock
                audio = voicelock.voiced(voicelock.to_float(self._fired_audio))
                verdict, phrase_score, voice = voicelock.lock.wake_verdict(audio)
                facts.update(lock_verdict=verdict, lock_score=max(phrase_score, voice))
            except Exception:
                log.debug("mic test: voice check failed", exc_info=True)
        return facts


def run(session, on_level=None, on_done=None, seconds: float = SECONDS) -> MicTest:
    test = MicTest(session, on_level, on_done, seconds)
    test.start()
    return test
