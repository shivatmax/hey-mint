"""The audio engine on this Mac: defaults, suspend/resume, device choice, echo modes.

    python bench/audio_engine_test.py
"""
import asyncio, os, subprocess, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mint import audio_devices
from mint.audio_vp import VoiceAudio

def using_mic() -> bool:
    """Ask from a separate process, as another app would see us."""
    out = subprocess.run([sys.executable, "-c", f"""
import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
from mint import audio_devices
print(any(u['pid'] == {os.getpid()} for u in audio_devices.mic_users()))"""], capture_output=True, text=True)
    return out.stdout.strip() == "True"

async def listen_for(audio, seconds=1.5):
    levels = []
    async def handler(pcm):
        levels.append(float(np.sqrt(np.mean(np.frombuffer(pcm, np.int16).astype(np.float32) ** 2))))
    task = asyncio.create_task(audio.listen(handler))
    await asyncio.sleep(seconds)
    task.cancel()
    return len(levels), (max(levels) if levels else 0.0)

async def main():
    for label, kwargs in [("defaults, echo auto", {}), ("echo off", {"echo": "off"}),
                          ("mic = BlackHole (virtual, silent)", {"input_uid": "BlackHole2ch_UID"}),
                          ("mic = BlackHole, echo off", {"input_uid": "BlackHole2ch_UID", "echo": "off"})]:
        a = VoiceAudio(**kwargs)
        frames, peak = await listen_for(a)
        print(f"{label:36} -> {a.status}\n{'':36}    frames {frames}, peak level {peak:.0f}, others see us on the mic: {using_mic()}")
        if label.startswith("defaults"):
            a.suspend("test")
            await asyncio.sleep(0.8)
            print(f"{'':36}    suspended: others see us on the mic: {using_mic()}")
            a.resume()
            frames, peak = await listen_for(a)
            print(f"{'':36}    resumed: frames {frames}, on the mic: {using_mic()}")
        a.close()
        await asyncio.sleep(0.5)

asyncio.run(main())
