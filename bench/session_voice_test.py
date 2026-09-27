"""End to end through the real session and real Gemini, no microphone.

The Mac's Rishi voice is the enrolled user; other system voices are other
people. Their speech is streamed into Mint's audio input at real-time pace,
with faint hiss, the way the microphone delivers it. Prints what Mint did.

    python bench/session_voice_test.py
"""

import asyncio
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.argv = [sys.argv[0]]
from mint import enroll, voicelock  # noqa: E402

voicelock.PROFILE = Path(tempfile.mktemp(suffix=".json"))
voicelock.RECORDINGS = Path(tempfile.mktemp(suffix=".npz"))


def say(voice: str, text: str, rate: int | None = None) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = f"{tmp}/a.aiff", f"{tmp}/a.wav"
        subprocess.run(["say", "-v", voice, "-o", aiff] + (["-r", str(rate)] if rate else []) + [text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav], check=True)
        w = wave.open(wav)
        return w.readframes(w.getnframes())


STEPS = [
    ("Aman (similar voice): Hey Mint + request", "Aman", "Hey Mint, what's the weather like today?", 4),
    ("Daniel: Hey Mint", "Daniel", "Hey Mint, tell me a joke.", 4),
    ("you: bare Hey Mint", "Rishi", "Hey Mint.", 5),
    ("you: a question", "Rishi", "What is the date today?", 9),
    ("Aman, while Mint is awake", "Aman", "Tell me a joke about cats, a really funny one please.", 7),
    ("you: bye", "Rishi", "Bye Mint.", 6),
]


async def main() -> None:
    lock = voicelock.lock = voicelock.VoiceLock()
    voicelock._IMPOSTORS = voicelock.impostor_wakes()[2:]          # the test user is a system voice
    voicelock._SENTENCES = voicelock.impostor_sentences()[1:]
    f32 = voicelock.to_float
    lock.enroll([f32(say("Rishi", s)) for s in enroll.SENTENCES],
                [f32(say("Rishi", "Hey Mint", r)) for r in (150, 160, 170, 180, 190, 200, 210, 220)])
    from mint.session import Mint

    mint = Mint(text_mode=True, hands_free=True, sleep_after=30)
    mint.refresh_voice_lock()
    task = asyncio.create_task(mint.run())
    while mint.session is None:
        await asyncio.sleep(0.1)
    rng = np.random.default_rng(0)

    async def stream(pcm: bytes) -> None:
        for k in range(0, len(pcm), 3200):
            frame = np.frombuffer(pcm[k:k + 3200], np.int16).astype(np.float32)
            frame += rng.normal(0, 40, frame.size)
            await mint._on_audio(np.clip(frame, -32768, 32767).astype(np.int16).tobytes())
            await asyncio.sleep(0.1)

    await stream(b"\x00\x00" * 32000)                       # room tone, as at start-up
    for label, voice, text, wait in STEPS:
        print(f"\n=== {label}   (asleep={mint.asleep})", flush=True)
        await stream(say(voice, text) + b"\x00\x00" * 24000)
        await asyncio.sleep(wait)
    print(f"\nend: asleep={mint.asleep}")
    task.cancel()
    mint.audio.close()


if __name__ == "__main__":
    asyncio.run(main())
