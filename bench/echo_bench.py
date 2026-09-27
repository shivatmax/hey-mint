"""Does macOS voice processing remove Mint's own voice from the microphone?

Plays speech through the speakers while recording the mic, twice: with
AVAudioEngine voice processing off, then on. Reports how loud the mic signal is
during playback relative to the room's silence before it. Without echo
cancellation the assistant's voice dominates the mic; with it, the mic should
stay close to room level - which is what lets the user interrupt by talking.

Usage: python bench/echo_bench.py /tmp/speech24.wav
"""

from __future__ import annotations

import sys
import threading
import time
import wave

import AVFoundation as A
import numpy as np


def run(path: str, voice_processing: bool) -> tuple[float, float]:
    with wave.open(path) as f:
        rate = f.getframerate()
        speech = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768

    engine = A.AVAudioEngine.alloc().init()
    mic = engine.inputNode()
    player = A.AVAudioPlayerNode.alloc().init()
    engine.attachNode_(player)
    fmt = A.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(rate, 1)
    engine.connect_to_format_(player, engine.mainMixerNode(), fmt)
    # Enable voice processing only after the output side is connected: enabled
    # first, the engine failed to initialise its output (-10875).
    if voice_processing:
        ok, error = mic.setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise RuntimeError(f"voice processing unavailable: {error}")

    captured: list[tuple[float, float]] = []   # (time, rms)
    lock = threading.Lock()

    def tap(buffer, when):
        n = buffer.frameLength()
        data = np.frombuffer(buffer.floatChannelData()[0].as_buffer(n), dtype=np.float32)
        with lock:
            captured.append((time.monotonic(), float(np.sqrt(np.mean(data ** 2)))))

    mic.installTapOnBus_bufferSize_format_block_(0, 4800, mic.outputFormatForBus_(0), tap)
    ok, error = engine.startAndReturnError_(None)
    if not ok:
        raise RuntimeError(f"engine failed to start: {error}")

    time.sleep(1.5)                      # room level, nothing playing
    buf = A.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(fmt, len(speech))
    buf.setFrameLength_(len(speech))
    np.frombuffer(buf.floatChannelData()[0].as_buffer(len(speech)), dtype=np.float32)[:] = speech
    play_start = time.monotonic()
    player.scheduleBuffer_completionHandler_(buf, None)
    player.play()
    time.sleep(len(speech) / rate + 0.3)
    play_end = time.monotonic()

    mic.removeTapOnBus_(0)
    engine.stop()
    with lock:
        quiet = [r for t, r in captured if t < play_start - 0.2]
        loud = [r for t, r in captured if play_start + 0.3 < t < play_end - 0.3]
    return float(np.median(quiet)), float(np.median(loud))


if __name__ == "__main__":
    # One engine per process: a second voice-processing engine in the same
    # process fails to initialise (-10875).
    path, vp = sys.argv[1], sys.argv[2] == "on"
    quiet, during = run(path, vp)
    ratio = during / quiet if quiet else float("inf")
    print(f"  voice processing {'ON ' if vp else 'OFF'}: room {quiet:.4f}  during playback {during:.4f}  "
          f"-> {20 * np.log10(ratio):+.1f} dB above room")
