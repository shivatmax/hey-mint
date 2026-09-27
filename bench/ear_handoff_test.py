"""The Python side of the Mint Ear hand-over, with a stand-in Ear.

A socket server plays the launcher: it sends the header and the user's own
"Hey Mint" (from their enrolment recordings) as the 3 s pre-roll, then one of
their sentences as "live" audio at real-time pace, then silence, and records
what Mint sends back (READY is main.py's; STOP must come from the session).
The real session and real Gemini run in text mode with sound off; the voice
lock uses the user's voiceprint. Two runs: Mint's own detector catches the
replayed phrase ("normal"), and a pre-roll too short for it, so the Ear's
word is taken ("fallback").

    python bench/ear_handoff_test.py
"""

import asyncio
import os
import socket
import sys
import tempfile
import threading
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
wake._paths = lambda: (setattr(wake.MintWake, "GENERIC", ROOT / "models" / "hey_mint.json"),
                       setattr(wake.MintWake, "PERSONAL", RUNTIME / "hey_mint_personal.json"))


def pcm(audio: np.ndarray) -> bytes:
    noise = np.random.default_rng(0).normal(0, 0.002, audio.size)
    return (np.clip(audio + noise, -1, 1) * 32767).astype(np.int16).tobytes()


def fake_ear(path: str, pre: bytes, live: bytes, lag: int, got: list) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    conn, _ = server.accept()
    conn.sendall(f"START reason=wake lag={lag} pre={len(pre)}\n".encode() + pre)

    def reader():
        data = b""
        while True:
            chunk = conn.recv(64)
            if not chunk:
                break
            data += chunk
            got.append((time.monotonic(), data.decode().split()))
    threading.Thread(target=reader, daemon=True).start()
    for k in range(0, len(live), 3200):              # real time, 100 ms chunks
        try:
            conn.sendall(live[k:k + 3200])
        except OSError:
            break
        time.sleep(0.1)
        if got and "STOP" in got[-1][1]:
            break
    conn.shutdown(socket.SHUT_WR)                    # no microphone here: the Ear stops sending


async def trial(label: str, pre_audio: np.ndarray, live_audio: np.ndarray, lag: int) -> None:
    from mint import ear
    from mint.session import Mint
    path = tempfile.mktemp(suffix=".sock")
    got: list = []
    threading.Thread(target=fake_ear, args=(path, pcm(pre_audio), pcm(live_audio), lag, got), daemon=True).start()
    time.sleep(0.3)
    os.environ["MINT_EAR_SOCKET"] = path
    mint = Mint(text_mode=True, hands_free=True, sleep_after=60)
    mint.voice_on = False
    lines: list = []
    original = mint._print
    mint._print = lambda text: (lines.append((time.monotonic(), text)), original(text))
    mint.refresh_voice_lock()
    start = time.monotonic()
    mint.ear = ear.connect()
    task = asyncio.create_task(mint.run())
    await asyncio.sleep(14)
    said = [t for _, t in lines if t.startswith(("you:", "[awake", "[Hey Mint"))]
    stop = next((t - start for t, words in got if "STOP" in words), None)
    print(f"\n=== {label}: " + " | ".join(said) + (f"   (STOP after {stop:.1f} s)" if stop else "   (no STOP)"))
    task.cancel()
    mint.audio.close()
    del os.environ["MINT_EAR_SOCKET"]


async def main() -> None:
    sentences, wakes = voicelock.load_recordings()
    silence = np.zeros(8000, np.float32)
    phrase = wakes[2]
    pre = np.concatenate([np.zeros(48000 - phrase.size, np.float32), phrase])      # 3 s ending at the fire
    live = np.concatenate([silence[:3200], sentences[3], np.zeros(32000, np.float32)])
    await trial("normal (3 s pre-roll)", pre, live, lag=int(0.25 * 16000))
    short = phrase[-int(0.9 * 16000):]                                               # too short for our detector
    await trial("fallback (0.9 s pre-roll)", short, live, lag=int(0.25 * 16000))


if __name__ == "__main__":
    asyncio.run(main())
