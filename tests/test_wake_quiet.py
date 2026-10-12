"""12 Oct (battery): the wake word's features ran on every 80 ms of sound, ~5% of a core all day. After 1.3 s of
near-silence the model's whole context is silence, so more silence is no longer computed. A "Hey Mint" after a
quiet spell must fire exactly as before."""
import shutil
import subprocess
import wave

import numpy as np
import pytest

try:
    from mint.voice import wake
except ImportError:
    from mint import wake


def _say(tmp_path, voice):
    if not shutil.which("say"):
        pytest.skip("macOS voices needed")
    path = tmp_path / f"{voice}.wav"
    subprocess.run(["say", "-v", voice, "-o", str(path), "--data-format=LEI16@16000", "Hey Mint"], check=True,
                   capture_output=True, timeout=30)
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def _run(audio, gated):
    w = wake.MintWake()
    if not gated:
        w._quiet = lambda frame: False
    fires, peak = [], 0.0
    for i in range(0, len(audio), 1280):
        if w.heard(audio[i:i + 1280].tobytes()):
            fires.append(i)
        peak = max(peak, w.last_peak)
    return fires, peak, getattr(w, "skipped", 0)


@pytest.mark.parametrize("voice", ["Samantha", "Daniel"])
def test_hey_mint_after_silence_fires_the_same_with_silence_skipped(tmp_path, voice):
    phrase = _say(tmp_path, voice)
    rng = np.random.default_rng(1)
    hiss = lambda seconds: rng.normal(0, 8, int(16000 * seconds)).astype(np.int16)     # a quiet room
    audio = np.concatenate([hiss(4), phrase, hiss(2)])
    fires, peak, skipped = _run(audio, gated=True)
    fires_all, peak_all, _ = _run(audio, gated=False)
    assert fires == fires_all and len(fires) == 1
    assert abs(peak - peak_all) < 0.02
    assert skipped > 20                                            # the quiet seconds were not computed


def test_a_noisy_room_is_never_skipped():
    w = wake.MintWake()
    rng = np.random.default_rng(2)
    noise = rng.normal(0, 600, 16000 * 4).astype(np.int16)         # a fan, a video: well above near-silence
    for i in range(0, len(noise), 1280):
        w.heard(noise[i:i + 1280].tobytes())
    assert getattr(w, "skipped", 0) == 0
