"""Train my voice: a guided enrolment for the voice lock and the wake word.

Two minutes, once: say "Hey Mint" eight times, then read eight short
sentences. From that Mint builds

* your voiceprint (voicelock.py), with thresholds calibrated on your own
  held-out speech, and
* "Hey Mint" retrained on your own recordings (wake.MintWake), with every
  wake voice-checked against the voiceprint.

Then it listens for a few seconds and shows, for each thing you say, whether
it would have let it through - so you can see the lock work before relying
on it.

The microphone is the session's: while enrolling, its audio comes here and
goes nowhere else (not to Gemini, not to the wake word).
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from mint.voice import voicelock

log = logging.getLogger("mint.voice.enroll")

RATE = 16000
WAKES = 8

SENTENCES = [
    "Open Slack and take me to the on-call support channel.",
    "What's on my calendar tomorrow, and do I have anything before ten?",
    "Draft an email to Aman saying the report will be ready by Friday evening.",
    "Scroll down a little, then click the second result on the page.",
    "Remind me to call the bank at four thirty, and turn the volume down.",
    "Switch to my Acme Chrome and check the latest emails.",
    "Export this document as a PDF and save it in my Documents folder.",
    "The quick brown fox jumps over the lazy dog near the riverbank.",
]


def _name() -> str:
    """The name in the wake phrase the user trains with: from a custom "Hey <name>" phrase when one
    is set (Settings ▸ Microphone & voice), else the assistant's name."""
    from mint.core import prefs
    from mint.voice import wake
    phrase = wake.active_phrases()[0]
    if phrase.lower().startswith("hey ") and wake.ready(phrase[4:].strip()):
        return phrase[4:].strip()
    return prefs.name() if wake.ready(prefs.name()) else "Mint"


class Enroller:
    """Collects the recordings from the live microphone. Audio thread safe:
    feed() only appends and counts; the heavy work runs on its own thread."""

    def __init__(self, on_update, on_done, on_level=None, test_seconds: float = 25.0) -> None:
        self.on_update = on_update          # (step, index, total, prompt, hint, level)
        self.on_level = on_level            # (level), about ten times a second
        self.on_done = on_done              # (ok, message)
        self.test_seconds = test_seconds
        self.step = "wake"                  # wake | read | training | test | done
        self.wakes: list[np.ndarray] = []
        self.sentences: list[np.ndarray] = []
        self.cancelled = False
        self._noise = 0.004
        self._segment: list[bytes] = []
        self._voiced = 0.0
        self._quiet = 0.0
        self._loud = 0
        self._pre: list[bytes] = []
        self._test_until = 0.0
        self._level_seen = 0.0
        self._level_pushed = 0.0
        self.results: list[tuple[float, bool]] = []

    # --- prompts ------------------------------------------------------------------

    def prompt(self) -> tuple[str, int, int, str]:
        if self.step == "wake":
            return "wake", len(self.wakes), WAKES, f"Say “Hey {_name()}”"
        if self.step == "read":
            index = len(self.sentences)
            return "read", index, len(SENTENCES), SENTENCES[min(index, len(SENTENCES) - 1)]
        if self.step == "training":
            return "training", 0, 0, "Learning your voice…"
        if self.step == "test":
            return "test", len(self.results), 0, "Now just talk - or get someone else to."
        return "done", 0, 0, ""

    def _update(self, hint: str = "") -> None:
        step, index, total, text = self.prompt()
        try:
            self.on_update(step, index, total, text, hint, self._level_seen)
        except Exception:
            log.exception("enrolment UI update failed")

    def start(self) -> None:
        self._update()

    def cancel(self) -> None:
        self.cancelled = True
        self.step = "done"

    # --- audio (session loop thread) ------------------------------------------------

    def feed(self, pcm: bytes, level: float) -> None:
        if self.step in ("training", "done") or self.cancelled:
            return
        self._level_seen = level
        if self.on_level is not None and time.monotonic() - self._level_pushed > 0.08:
            self._level_pushed = time.monotonic()
            self.on_level(level)
        speaking = level > max(0.012, self._noise * 3.5)
        if not self._segment:
            if not speaking:
                self._noise = 0.97 * self._noise + 0.03 * min(level, 0.03)
                self._pre = (self._pre + [pcm])[-3:]
                self._loud = 0
                return
            self._loud += 1
            self._pre = (self._pre + [pcm])[-4:]
            if self._loud < 2:
                return
            self._segment = list(self._pre)
            self._voiced = 0.2
            self._quiet = 0.0
            return
        self._segment.append(pcm)
        seconds = len(pcm) / 2 / RATE
        if speaking:
            self._voiced += seconds
            self._quiet = 0.0
        else:
            self._quiet += seconds
        # Wake phrases are short and end quickly; sentences may pause for breath.
        hangover = 0.5 if self.step in ("wake", "test") else 0.9
        if self._quiet >= hangover:
            audio = voicelock.to_float(b"".join(self._segment))
            voiced = self._voiced
            self._segment, self._pre, self._loud = [], [], 0
            self._finished(audio, voiced)
        if self.step == "test" and time.monotonic() > self._test_until:
            self.step = "done"
            self.on_done(True, self._summary())

    def _finished(self, audio: np.ndarray, voiced: float) -> None:
        if self.step == "wake":
            if 0.35 <= voiced <= 2.5:
                self.wakes.append(audio)
                if len(self.wakes) >= WAKES:
                    self.step = "read"
                self._update("✓")
            else:
                self._update("That was too long - just the wake phrase." if voiced > 2.5 else "")
        elif self.step == "read":
            if voiced >= 1.6:
                self.sentences.append(audio)
                if len(self.sentences) >= len(SENTENCES):
                    self.step = "training"
                    self._update()
                    threading.Thread(target=self._train, name="mint-enrol", daemon=True).start()
                    return
                self._update("✓")
            else:
                self._update("Read the whole sentence, please.")
        elif self.step == "test":
            lock = voicelock.lock
            score = lock.score(audio)
            passed = score >= lock.threshold(min(voiced, 2.4))
            self.results.append((score, passed))
            self._update(("✓ That's you" if passed else "✗ Not you - ignored") + f"  ({score:.2f})")

    # --- training (its own thread) -----------------------------------------------------

    def _train(self) -> None:
        try:
            voicelock.save_recordings(self.sentences, self.wakes)
            report = voicelock.lock.enroll(self.sentences, self.wakes)
            report["hey_mint"] = train_personal_wake(self.wakes, self.sentences)
            log.info("enrolled: %s", report)
            print(f"  [voice lock: enrolled {report['seconds']} s of speech, consistency "
                  f"{report['consistency']}, thresholds {report['thresholds']}, "
                  f"wake check {{k: v for k, v in report.items() if 'template' in k or 'wake' in k or 'impostor' in k}}, "
                  f"Hey Mint {report['hey_mint']}]", flush=True)
            self.report = report
            if self.cancelled:
                return
            self.step = "test"
            self._test_until = time.monotonic() + self.test_seconds
            self._update("Voice lock is on.")
        except Exception as error:
            log.exception("enrolment failed")
            self.step = "done"
            self.on_done(False, f"Could not learn your voice: {error}")

    def _summary(self) -> str:
        mine = sum(1 for _, passed in self.results if passed)
        return (f"Voice lock is on. In the test, {mine} of {len(self.results)} things said "
                "were let through." if self.results else "Voice lock is on.")


def train_personal_wake(wakes: list[np.ndarray], sentences: list[np.ndarray]) -> dict:
    """Retrain "Hey Mint" with the user's own recordings, on top of the shipped
    data (12 voices, lookalikes, real speakers). Writes hey_mint_personal.json."""
    import json

    from mint.voice.features import Features

    from mint.voice import wake

    name = _name()
    wake._set_paths(wake.slug(name))          # the personal model is saved under this name
    data = np.load(wake.generic_data_path(name))
    positives = list(data["positives"].astype(np.float32))
    negatives = list(data["negatives"].astype(np.float32))
    features = Features()
    mine = []
    seed = 0
    for clip in wakes:
        pcm = (np.clip(clip, -1, 1) * 32767).astype(np.int16)
        for gain in (1.0, 0.5, 1.6):      # quieter and louder, as from across the desk
            for noise in (0, 150):
                seed += 1
                mine += wake.clip_windows(features, (pcm * gain).clip(-32768, 32767).astype(np.int16), True,
                                          noise=noise, seed=seed)
    # The user's other speech is exactly what must NOT wake it.
    for sentence in sentences:
        pcm = (np.clip(sentence, -1, 1) * 32767).astype(np.int16)
        seed += 1
        negatives += wake.clip_windows(features, pcm, False, stride=1, noise=40, seed=seed)
    model = wake.train(positives + mine * 3, negatives, threshold=0.85)

    # Check it on the user's own phrases; lower the bar a little only if needed.
    detector = wake.MintWake.__new__(wake.MintWake)
    detector._mean, detector._scale = np.asarray(model["mean"], np.float32), np.asarray(model["scale"], np.float32)
    detector._coef, detector._bias = np.asarray(model["coef"], np.float32), model["intercept"]
    peaks = []
    for clip in wakes:
        pcm = (np.clip(clip, -1, 1) * 32767).astype(np.int16)
        windows = wake.clip_windows(features, pcm, True)
        peaks.append(max((detector._probability(w) for w in windows), default=0.0))
    peaks.sort()
    if peaks and peaks[len(peaks) // 8] < model["threshold"]:
        model["threshold"] = round(max(0.6, peaks[len(peaks) // 8] - 0.05), 2)
    wake.MintWake.PERSONAL.write_text(json.dumps(model))
    wake.MintWake.PERSONAL.chmod(0o600)
    return {"threshold": model["threshold"], "your_scores": [round(float(p), 2) for p in peaks]}


# --- the window (main thread) ----------------------------------------------------------

class EnrollWindow:
    """A small guided window. Click-through apart from its buttons; it never
    takes the keyboard."""

    W, H = 480, 250

    def __init__(self, on_cancel) -> None:
        self.on_cancel = on_cancel
        self.panel = None

    def show(self) -> None:
        import AppKit
        import objc

        screen = AppKit.NSScreen.screens()[0].visibleFrame()
        frame = AppKit.NSMakeRect(screen.origin.x + (screen.size.width - self.W) / 2,
                                  screen.origin.y + screen.size.height * 0.62 - self.H / 2, self.W, self.H)
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setMovableByWindowBackground_(True)
        panel.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        from mint.ui import look
        look.follow_system(panel)                   # light or dark with the system
        # System glass rounded by a mask image (a layer cornerRadius left a square blur patch).
        blur = look.glass(AppKit.NSMakeRect(0, 0, self.W, self.H), radius=20)
        panel.setContentView_(blur)

        def label(y, h, size, weight, alpha, lines=1):
            field = AppKit.NSTextField.labelWithString_("")
            field.setFrame_(AppKit.NSMakeRect(28, y, self.W - 56, h))
            field.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, weight))
            field.setTextColor_(AppKit.NSColor.labelColor() if alpha > 0.9 else AppKit.NSColor.secondaryLabelColor())
            field.setMaximumNumberOfLines_(lines)
            field.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
            blur.addSubview_(field)
            return field

        self.title = label(206, 22, 15, AppKit.NSFontWeightSemibold, 0.95)
        self.title.setStringValue_("Train my voice")
        self.step = label(184, 18, 12, AppKit.NSFontWeightMedium, 0.55)
        self.prompt = label(104, 70, 20, AppKit.NSFontWeightSemibold, 0.97, lines=3)
        self.hint = label(76, 18, 12, AppKit.NSFontWeightRegular, 0.7)

        self.track = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(28, 60, self.W - 56, 4))
        self.track.setWantsLayer_(True)
        self.track.layer().setCornerRadius_(2)
        self.track.layer().setBackgroundColor_(look.cg(AppKit.NSColor.quaternaryLabelColor(), blur))
        blur.addSubview_(self.track)
        self.meter = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(28, 60, 0, 4))
        self.meter.setWantsLayer_(True)
        self.meter.layer().setCornerRadius_(2)
        self.meter.layer().setBackgroundColor_(
            AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.35, 0.78, 1.0, 1.0).CGColor())
        blur.addSubview_(self.meter)

        self.handler = _ButtonTarget.alloc().initWithCallback_(self._button)
        button = AppKit.NSButton.buttonWithTitle_target_action_("Cancel", self.handler, "press:")
        button.setBezelStyle_(AppKit.NSBezelStyleRounded)
        button.setFrame_(AppKit.NSMakeRect(self.W - 28 - 96, 16, 96, 30))
        blur.addSubview_(button)
        self.button = button
        self.panel = panel
        panel.orderFrontRegardless()
        panel.invalidateShadow()                    # shadow follows the rounded glass
        del objc

    def _button(self) -> None:
        if self.button.title() == "Done":
            self.close()
        else:
            self.on_cancel()
            self.close()

    def update(self, step, index, total, text, hint, level) -> None:
        import AppKit

        if self.panel is None:
            return
        steps = {"wake": f"Step 1 of 2 · wake word · {index}/{total}",
                 "read": f"Step 2 of 2 · read aloud · sentence {min(index + 1, total)} of {total}",
                 "training": "Learning your voice…",
                 "test": "Try it: speak, or have someone else speak"}
        self.step.setStringValue_(steps.get(step, ""))
        self.prompt.setStringValue_(text)
        self.hint.setStringValue_(hint)
        width = (self.W - 56) * max(0.0, min(1.0, level * 9))
        self.meter.setFrame_(AppKit.NSMakeRect(28, 60, width, 4))

    def level(self, level: float) -> None:
        import AppKit
        if self.panel is not None:
            self.meter.setFrame_(AppKit.NSMakeRect(28, 60, (self.W - 56) * max(0.0, min(1.0, level * 9)), 4))

    def done(self, ok: bool, message: str) -> None:
        if self.panel is None:
            return
        self.step.setStringValue_("Done" if ok else "Something went wrong")
        self.prompt.setStringValue_(message)
        self.hint.setStringValue_("Turn it off any time: menu bar ▸ Voice lock." if ok else "")
        self.button.setTitle_("Done")

    def close(self) -> None:
        if self.panel is not None:
            self.panel.orderOut_(None)
            self.panel = None


def _make_target():
    import AppKit
    import objc

    class _ButtonTarget(AppKit.NSObject):
        def initWithCallback_(self, callback):
            self = objc.super(_ButtonTarget, self).init()
            if self is None:
                return None
            self.callback = callback
            return self

        def press_(self, sender):
            self.callback()

    return _ButtonTarget


_ButtonTarget = _make_target()
