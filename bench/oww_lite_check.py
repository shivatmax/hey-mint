"""mint.oww_lite.Features must give openWakeWord's AudioFeatures numbers.

Feeds the same audio (the user's enrolment recordings if present, else noise
and a macOS voice) through both, in the wake word's 80 ms frames and in
uneven 100 ms chunks, and compares the features and the "Hey Mint" scores.

    python bench/oww_lite_check.py
"""

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mint import oww_lite, voicelock  # noqa: E402


def audio() -> np.ndarray:
    runtime = Path.home() / "Library/Application Support/Mint/voice/enrolment.npz"
    voicelock.RECORDINGS = runtime
    recorded = voicelock.load_recordings() if runtime.exists() else None
    if recorded:
        clips = recorded[1][:4] + recorded[0][:2]
        return (np.clip(np.concatenate(clips), -1, 1) * 32767).astype(np.int16)
    return np.random.default_rng(0).normal(0, 3000, 16000 * 20).astype(np.int16)


def main() -> None:
    from openwakeword.utils import AudioFeatures
    from mint import wake
    pcm = audio()
    scorer = wake.MintWake.__new__(wake.MintWake)
    wake.MintWake.load(scorer)
    for chunk in (1280, 1600, 1000):
        np.random.seed(7)
        original = AudioFeatures(inference_framework="onnx")
        np.random.seed(7)
        lite = oww_lite.Features()
        worst, score_gap, windows = 0.0, 0.0, 0
        for k in range(0, len(pcm), chunk):
            piece = pcm[k:k + chunk]
            original(piece)
            lite(piece)
            a, b = original.get_features(16), lite.get_features(16)
            worst = max(worst, float(np.abs(a - b).max()))
            if a.shape[1] == 16:
                score_gap = max(score_gap, abs(scorer._probability(a) - scorer._probability(b)))
                windows += 1
        print(f"{len(pcm) / 16000:.0f} s in {chunk}-sample chunks: max feature difference {worst:.2e}, "
              f"max 'Hey Mint' score difference {score_gap:.2e} over {windows} windows")


if __name__ == "__main__":
    main()
