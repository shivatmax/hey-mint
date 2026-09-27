"""Typed requests around a reconnect, through the real session (no mic, no sound).

1. Mint is awake and hears speech that is cut off mid-sentence; the mic is
   then paused (as when the user clicks mic off).
2. The session reconnects with its resumption handle (as a voice change does);
   a request typed during the reconnect must be kept and sent afterwards.
3. Another typed request right after. Both must be answered promptly.

    python bench/typed_after_reconnect_test.py
"""

import asyncio
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.argv = [sys.argv[0]]


def say(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["say", "-v", "Rishi", "-o", f"{tmp}/a.aiff", text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", f"{tmp}/a.aiff",
                        f"{tmp}/a.wav"], check=True)
        w = wave.open(f"{tmp}/a.wav")
        return w.readframes(w.getnframes())


async def main() -> None:
    from mint.session import Mint
    mint = Mint(text_mode=True, hands_free=True, sleep_after=120)
    mint.voice_on = False
    mint._gate = None
    events: list[tuple[float, str]] = []
    original = mint._print

    def capture(text: str) -> None:
        events.append((time.monotonic(), text))
        original(text)
    mint._print = capture
    task = asyncio.create_task(mint.run())
    while mint.session is None:
        await asyncio.sleep(0.1)
    await mint.wake_up("test")
    await asyncio.sleep(1)
    pcm = say("So I want to tell you about my day, first I went to the")
    for k in range(0, len(pcm), 3200):                   # cut off: no trailing silence
        await mint._on_audio(pcm[k:k + 3200])
        await asyncio.sleep(0.1)
    mint.set_paused(True)
    await asyncio.sleep(2)

    old = mint.session
    await old.close()                                     # reconnect, as for a new voice
    while mint.session is old:
        await asyncio.sleep(0.01)
    t1 = time.monotonic()
    await mint.inject_text("What is 12 times 12? Answer in a few words.")
    while mint.session is None:
        await asyncio.sleep(0.1)
    await asyncio.sleep(12)
    t2 = time.monotonic()
    await mint.inject_text("What is the capital of France? Answer in one word.")
    await asyncio.sleep(12)

    def answered(since: float) -> str:
        for t, text in events:
            if t > since and (text.startswith("mint:") or (text.startswith("[") and "] " in text
                                                            and not text.startswith(("[typed", "[paused")))):
                return f"answered after {t - since:.1f}s: {text[:70]}"
        return "NO ANSWER"
    print("\n during reconnect:", answered(t1))
    print(" right after     :", answered(t2))
    task.cancel()
    mint.audio.close()


if __name__ == "__main__":
    asyncio.run(main())
