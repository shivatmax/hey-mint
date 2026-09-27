"""Mint steps aside while another app records, and comes back after.

A stand-in "call app" (a Python process capturing the mic) starts, then stops.

    python bench/mic_share_test.py
"""
import asyncio, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent)); sys.argv = [sys.argv[0]]
from mint import audio_devices, prefs

CALL = """
import time, AVFoundation as A
e = A.AVAudioEngine.alloc().init(); n = e.inputNode()
n.installTapOnBus_bufferSize_format_block_(0, 4800, n.outputFormatForBus_(0), lambda b, w: None)
e.startAndReturnError_(None); print('call app recording', flush=True); time.sleep(8)
"""

def mint_on_mic() -> bool:
    """Asked from another process, as a call app would see Mint."""
    root = str(Path(__file__).resolve().parent.parent)
    out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {root!r}); "
                          f"from mint import audio_devices; "
                          f"print(any(u['pid'] == {os.getpid()} for u in audio_devices.mic_users()))"],
                         capture_output=True, text=True)
    return out.stdout.strip() == "True"

async def main():
    prefs._values["share_mic_apps"] = ["org.python.python"]      # the stand-in counts as a call app
    from mint.session import Mint
    mint = Mint(hands_free=True)
    task = asyncio.create_task(mint.run())
    while mint.session is None:
        await asyncio.sleep(0.2)
    await asyncio.sleep(2)
    print(f"before the call: Mint on the mic = {mint_on_mic()}", flush=True)
    call = subprocess.Popen([sys.executable, "-c", CALL])
    await asyncio.sleep(5)
    print(f"during the call: Mint on the mic = {mint_on_mic()}, lent to = {mint._mic_lent_to!r}", flush=True)
    call.wait()
    await asyncio.sleep(6)
    print(f"after the call:  Mint on the mic = {mint_on_mic()}, lent to = {mint._mic_lent_to!r}", flush=True)
    task.cancel(); mint.audio.close()

asyncio.run(main())
