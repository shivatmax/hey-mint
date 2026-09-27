"""Whose "Hey Mint" was it? The voice check on the wake phrase, offline.

The Mac's Rishi voice plays the enrolled user: it reads the enrolment
sentences and says "Hey Mint" eight times. Then every other installed English
voice says "Hey Mint" (and "Hey Mint, what's the weather?") as an impostor,
and Rishi says held-out phrases. Reports how each is judged: yes / maybe / no.

    python bench/wake_check_bench.py
"""

import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mint import enroll, voicelock  # noqa: E402

USER = "Rishi"
OTHERS = ["Aman", "Daniel", "Samantha", "Karen", "Moira", "Tessa", "Eddy (English (US))",
          "Flo (English (UK))", "Reed (English (US))", "Sandy (English (US))", "Shelley (English (UK))"]


def tts(voice: str, text: str, rate: int | None = None) -> np.ndarray:
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = f"{tmp}/a.aiff", f"{tmp}/a.wav"
        subprocess.run(["say", "-v", voice, "-o", aiff] + (["-r", str(rate)] if rate else []) + [text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav], check=True)
        w = wave.open(wav)
        return voicelock.to_float(w.readframes(w.getnframes()))


def main() -> None:
    voicelock.PROFILE = Path(tempfile.mktemp(suffix=".json"))
    lock = voicelock.VoiceLock()
    # The user is one of the Mac's voices here, so leave that voice out of the
    # impostors the calibration uses - a real user is not a system voice.
    everyone = voicelock.impostor_wakes()
    voicelock._IMPOSTORS = everyone[2:]                      # Rishi's two are first
    voicelock._SENTENCES = voicelock.impostor_sentences()[1:]
    sentences = [tts(USER, s) for s in enroll.SENTENCES]
    wakes = [tts(USER, "Hey Mint", r) for r in (150, 160, 170, 180, 190, 200, 210, 220)]
    report = lock.enroll(sentences, wakes)
    print({k: v for k, v in report.items() if k != "thresholds"})

    def judge(audio):
        verdict, phrase, voice = lock.wake_verdict(voicelock.voiced(audio))
        return f"{verdict:5} (phrase {phrase:.2f}, voice {voice:.2f})"

    print("\nThe user (held out):")
    for rate, text in ((145, "Hey Mint"), (225, "Hey Mint"), (185, "Hey Mint, open Slack please"),
                       (175, "Hey Mint, what's on my calendar")):
        print(f"  {text!r:38} {judge(tts(USER, text, rate))}")
    print("\nOther people:")
    counts = {"yes": 0, "maybe": 0, "no": 0}
    for voice in OTHERS:
        for text in ("Hey Mint", "Hey Mint, what's the weather like today?"):
            line = judge(tts(voice, text))
            counts[line.split()[0]] += 1
            print(f"  {voice[:22]:22} {text[:28]!r:30} {line}")
    print(f"\nother people: {counts}  ('maybe' then needs the next second of speech to pass the voice lock)")
    print("\nOrdinary speech through the voice lock (2.4 s, threshold "
          f"{lock.threshold(2.4):.2f}; an unsure wake needs +0.10):")
    for voice in [USER] + OTHERS:
        clip = voicelock.voiced(tts(voice, "Please check my email and then open the report for this week."))
        crop = clip[:int(2.4 * 16000)]
        score = lock.score(crop)
        print(f"  {voice[:22]:22} {score:.2f} {'PASSES' if score >= lock.threshold(2.4) else 'blocked'}")


if __name__ == "__main__":
    main()
