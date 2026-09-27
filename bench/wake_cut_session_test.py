"""Does Gemini still hear "Hey Mint" (as "payment")? Through the real session
and real Gemini, with the user's own enrolment recordings: a "Hey Mint" clip,
a short pause, then one of their sentences, streamed in at real-time pace with
the voice lock on. Run with the phrase cut off and on; prints what Gemini
transcribed. No microphone, no sound.

    python bench/wake_cut_session_test.py
"""

import asyncio
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.argv = [sys.argv[0]]
RUNTIME = Path.home() / "Library/Application Support/Mint"

from mint import voicelock, wake  # noqa: E402

voicelock.PROFILE = RUNTIME / "voiceprint.json"
voicelock.RECORDINGS = RUNTIME / "voice" / "enrolment.npz"
voicelock.lock = voicelock.VoiceLock()


def _paths() -> None:
    wake.MintWake.GENERIC = ROOT / "models" / "hey_mint.json"
    wake.MintWake.PERSONAL = RUNTIME / "hey_mint_personal.json"


wake._paths = _paths
TRIALS = [(0, 0), (3, 2), (5, 5), (7, 1)] if "--quick" not in sys.argv[1:] else [(1, 3), (6, 4)]      # (Hey Mint clip, sentence)


async def main() -> None:
    from mint import session as S
    from mint.session import Mint
    sentences, wakes = voicelock.load_recordings()
    mint = Mint(text_mode=True, hands_free=True, sleep_after=60)
    mint.voice_on = False
    heard: list[tuple[float, str]] = []
    original = mint._print

    def capture(text: str) -> None:
        heard.append((time.monotonic(), text))
        original(text)
    mint._print = capture
    mint.refresh_voice_lock()
    task = asyncio.create_task(mint.run())
    while mint.session is None:
        await asyncio.sleep(0.1)
    rng = np.random.default_rng(0)
    real_cut = S.wake_phrase_cut

    async def stream(audio: np.ndarray) -> None:
        pcm = (np.clip(audio + rng.normal(0, 0.002, audio.size), -1, 1) * 32767).astype(np.int16).tobytes()
        for k in range(0, len(pcm), 3200):
            await mint._on_audio(pcm[k:k + 3200])
            await asyncio.sleep(0.1)

    results = []
    for cut_on in (False, True):
        S.wake_phrase_cut = real_cut if cut_on else (lambda pcm, lag: 0)
        for w, s in TRIALS:
            if not mint.asleep:
                mint.go_to_sleep("test")
            await asyncio.sleep(1.5)
            start = time.monotonic()
            await stream(np.concatenate([np.zeros(24000, np.float32), wakes[w], np.zeros(4800, np.float32),
                                         sentences[s], np.zeros(24000, np.float32)]))
            await asyncio.sleep(9)
            said = [t for at, t in heard if at > start and t.startswith(("you:", "(not for me", "[awake",
                                                                          "[Hey Mint"))]
            results.append((cut_on, w, s, said))
    print("\n\n=== what Gemini heard ===")
    for cut_on, w, s, said in results:
        print(f"cut {'ON ' if cut_on else 'OFF'} clip {w} sentence {s}: " + " | ".join(said))
    task.cancel()
    mint.audio.close()


if __name__ == "__main__":
    asyncio.run(main())
