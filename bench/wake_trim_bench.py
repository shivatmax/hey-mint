"""Where does "Hey Mint" end? Checks the wake detector's estimate
(MintWake.phrase_end_lag) against the true end, on the user's own enrolment
recordings: each "Hey Mint" clip followed by one of their sentences, with no
pause, a short one and a long one, fed in 100 ms chunks as the mic does.

    python bench/wake_trim_bench.py [runtime_dir]
"""

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mint import config, voicelock, wake  # noqa: E402

RUNTIME = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Library/Application Support/Mint"
voicelock.RECORDINGS = RUNTIME / "voice" / "enrolment.npz"
wake._paths = lambda: None
wake.MintWake.GENERIC = ROOT / "models" / "hey_mint.json"
personal = RUNTIME / "hey_mint_personal.json"
wake.MintWake.PERSONAL = personal if personal.exists() else ROOT / "models" / "missing.json"


def edges(clip: np.ndarray) -> tuple[int, int]:
    env = np.array([np.sqrt(np.mean(clip[i:i + 160] ** 2)) for i in range(0, len(clip) - 160, 160)])
    on = np.where(env > 0.08 * env.max())[0]
    return on[0] * 160, (on[-1] + 1) * 160


PHRASE = 0.55


def main() -> None:
    global PHRASE
    sentences, wakes = voicelock.load_recordings()
    PHRASE = float(np.median([np.subtract(*edges(c)[::-1]) / 16000 for c in wakes]))
    print(f"your usual Hey Mint: {PHRASE:.2f} s")
    detector = wake.MintWake()
    print("model:", "personal" if detector.personal else "generic", "threshold", detector.threshold)
    errors, missed = [], 0
    detector.reset()
    for i, clip in enumerate(wakes):
        start, end = edges(clip)
        phrase = clip[start:end]
        for gap in (0.0, 0.08, 0.15, 0.3, 0.5):
            sentence = sentences[i % len(sentences)]
            s0, _ = edges(sentence)
            audio = np.concatenate([np.zeros(8000), phrase, np.zeros(int(gap * 16000)), sentence[s0:],
                                    np.zeros(8000)]).astype(np.float32)
            audio = audio + np.random.default_rng(i).normal(0, 0.002, audio.size).astype(np.float32)
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            true_end = 8000 + len(phrase)
            detector.reset()
            detector._last_fire = 0.0
            fed, estimate = 0, None
            for k in range(0, len(pcm), 1600):
                chunk = pcm[k:k + 1600]
                fed += len(chunk)
                if detector.heard(chunk.tobytes()):
                    estimate = wake.phrase_cut(pcm[:fed].tobytes(), detector.phrase_end_lag) // 2
                    break
            if estimate is None:
                missed += 1
                print(f"  clip {i} gap {gap:.2f}s: not detected")
                continue
            errors.append((estimate - true_end) / 16)
            print(f"  clip {i} gap {gap:.2f}s: cut {errors[-1]:+5.0f} ms from the true end")
    e = np.array(errors)
    print(f"\ndetected {len(e)}/{len(e) + missed}; error ms: median {np.median(e):+.0f}, "
          f"min {e.min():+.0f}, max {e.max():+.0f}")


if __name__ == "__main__":
    main()
