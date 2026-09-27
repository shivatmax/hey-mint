"""Train the shipped "Hey Mint" wake word model, and the data "Train my voice" reuses.

    python bench/train_hey_mint.py /path/to/LibriSpeech/dev-clean-2

Positives: "Hey Mint" spoken by the Mac's own voices (Indian, US, UK, Irish,
Australian English) at four speeds. Negatives: everyday sentences and
lookalikes ("hey man", "peppermint", "hey Mindy", "minted") in the same voices,
plus real speech from half the LibriSpeech mini speakers. The other half is
held out to count false wakes on people the model never heard.

Writes models/hey_mint.json (the classifier, ~40 KB) and
models/hey_mint_data.npz (the training windows, float16, reused when the user
adds their own recordings). LibriSpeech is CC BY 4.0 (openslr.org/31).
"""

import glob
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mint import wake  # noqa: E402

VOICES = ["Rishi", "Aman", "Daniel", "Samantha", "Karen", "Moira", "Tessa", "Eddy (English (US))",
          "Flo (English (UK))", "Reed (English (US))", "Sandy (English (US))", "Shelley (English (UK))"]
RATES = [150, 175, 200, 225]
SENTENCES = [
    "Can you send the report to the team by five.", "I think we should meet on Monday morning instead.",
    "The weather has been really nice this week.", "Please open the document and check the numbers.",
    "My phone battery died in the middle of the call.", "Let's grab lunch after the meeting today.",
    "He mentioned the payment would arrive tomorrow.", "Hey, how was your weekend trip to the mountains?",
    "We need more milk, eggs and some bread.", "The train was delayed by almost twenty minutes.",
    "Hey man, are you coming to the party tonight?", "I had peppermint tea and a mint chocolate.",
    "Hey Mindy, did you see my keys anywhere?", "The coin was minted in nineteen ninety.",
    "Hey, mind the gap between the train and the platform.", "Hey Minh, are you still there?",
    "Give me a minute, I'm almost done.", "Hey, meet me near the main gate.",
    "That's a mint condition bicycle.", "Hey Mike, can you hear me?",
]
HELD_OUT = ["Hey man, how are you doing today?", "Let me think about the payment for a minute.",
            "Hey Mindy, can you come here for a second?", "This old car is in mint condition."]


def tts(voice: str, text: str, rate: int | None, folder: Path, name: str) -> np.ndarray:
    aiff, wav = folder / f"{name}.aiff", folder / f"{name}.wav"
    cmd = ["say", "-v", voice, "-o", str(aiff)] + (["-r", str(rate)] if rate else []) + [text]
    subprocess.run(cmd, check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True)
    w = wave.open(str(wav))
    return np.frombuffer(w.readframes(w.getnframes()), np.int16)


def load_flac(path: str) -> np.ndarray:
    out = tempfile.mktemp(suffix=".wav")
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", path, out], check=True)
    w = wave.open(out)
    data = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    os.unlink(out)
    return data


def main(libri: str) -> None:
    from openwakeword.utils import AudioFeatures

    features = AudioFeatures(inference_framework="onnx")
    tmp = Path(tempfile.mkdtemp())
    P, N = [], []
    seed = 0
    for v, voice in enumerate(VOICES):
        for rate in RATES:
            clip = tts(voice, "Hey Mint", rate, tmp, f"p{v}{rate}")
            for noise in wake.NOISE_LEVELS:
                seed += 1
                P += wake.clip_windows(features, clip, True, noise=noise, seed=seed)
        clip = tts(voice, "Hey Mint, open Slack", None, tmp, f"c{v}")
        for noise in wake.NOISE_LEVELS:
            seed += 1
            P += wake.clip_windows(features, clip, True, noise=noise, seed=seed)
        for s, text in enumerate(SENTENCES):
            seed += 1
            N += wake.clip_windows(features, tts(voice, text, None, tmp, f"n{v}_{s}"), False, stride=3,
                                   noise=wake.NOISE_LEVELS[seed % len(wake.NOISE_LEVELS)], seed=seed)
    speakers = sorted(os.listdir(libri))
    train_speakers, test_speakers = speakers[::2], speakers[1::2]
    for spk in train_speakers:
        for f in sorted(glob.glob(f"{libri}/{spk}/*/*.flac"))[:8]:
            seed += 1
            N += wake.clip_windows(features, load_flac(f), False, stride=4,
                                   noise=wake.NOISE_LEVELS[seed % len(wake.NOISE_LEVELS)], seed=seed)
    # Room tone alone, at every level, must never wake it.
    for noise in wake.NOISE_LEVELS[1:] + (800,):
        seed += 1
        N += wake.clip_windows(features, np.zeros(16000 * 4, np.int16), False, stride=2, noise=noise, seed=seed)
    print(f"windows: {len(P)} positive, {len(N)} negative")

    model = wake.train(P, N, threshold=0.85)
    (ROOT / "models").mkdir(exist_ok=True)
    (ROOT / "models" / "hey_mint.json").write_text(json.dumps(model))
    # Negatives are subsampled to keep the file small; personal training adds the user's own speech.
    rng = np.random.default_rng(0)
    keep = rng.choice(len(N), size=min(len(N), 3000), replace=False)
    np.savez_compressed(ROOT / "models" / "hey_mint_data.npz",
                        positives=np.vstack(P).astype(np.float16),
                        negatives=np.vstack([N[i] for i in keep]).astype(np.float16))

    # Evaluate the shipped model the way it runs.
    detector = wake.MintWake()
    detector.PERSONAL = Path("/nonexistent")
    detector.load()

    def fires(audio: np.ndarray, noise: float = 60.0) -> bool:
        """As on a real mic: two seconds of room tone first, hiss throughout."""
        detector.reset()
        detector._last_fire = 0.0
        x = np.concatenate([np.zeros(32000), audio.astype(np.float32), np.zeros(16000)])
        x = x + np.random.default_rng(len(audio)).normal(0, noise, x.size)
        pcm = np.clip(x, -32768, 32767).astype(np.int16).tobytes()
        return any(detector.heard(pcm[i:i + 3200]) for i in range(0, len(pcm), 3200))

    held = [tts(v, "Hey Mint", 190, tmp, f"h{i}") for i, v in enumerate(["Rishi", "Aman", "Samantha", "Daniel"])]
    held += [tts(v, "Hey Mint, what's on my calendar", 165, tmp, f"hc{i}") for i, v in enumerate(["Rishi", "Aman"])]
    look = [tts(v, t, None, tmp, f"l{i}{j}") for i, v in enumerate(["Rishi", "Aman", "Samantha"])
            for j, t in enumerate(HELD_OUT)]
    minutes, false_wakes = 0.0, 0
    for spk in test_speakers:
        for f in sorted(glob.glob(f"{libri}/{spk}/*/*.flac"))[:16]:
            audio = load_flac(f)
            minutes += audio.size / 16000 / 60
            false_wakes += fires(audio)
    for noise in (0, 60, 300):
        caught = sum(fires(a, noise) for a in held)
        looked = sum(fires(a, noise) for a in look)
        print(f"hiss {noise:>3}: held-out Hey Mint {caught}/{len(held)}; lookalikes {looked}/{len(look)}")
    print(f"false wakes: {false_wakes} in {minutes:.0f} min of unseen speakers (hiss 60)")


if __name__ == "__main__":
    main(sys.argv[1])
