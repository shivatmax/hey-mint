"""A new assistant name gets its own wake word, built on this Mac.

    python bench/name_wake_test.py Nova
"""
import subprocess, sys, tempfile, time, wave
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mint import config, prefs, wake

name = sys.argv[1] if len(sys.argv) > 1 else "Nova"
config.PROJECT_ROOT = Path(tempfile.mkdtemp())            # don't touch the real models
(config.PROJECT_ROOT / "models").symlink_to(Path(__file__).resolve().parent.parent / "models")
prefs._values["assistant_name"] = name
started = time.time()
print(wake.train_name(name), f"in {time.time() - started:.0f}s")
detector = wake.MintWake()
print("loaded:", detector.phrase, "threshold", detector.threshold)

def say(voice, text, rate):
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", f"{tmp}/a.aiff", text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", f"{tmp}/a.aiff", f"{tmp}/a.wav"], check=True)
        w = wave.open(f"{tmp}/a.wav"); return np.frombuffer(w.readframes(w.getnframes()), np.int16)

def fires(audio):
    detector.reset(); detector._last_fire = 0
    x = np.concatenate([np.zeros(32000), audio.astype(np.float32), np.zeros(16000)])
    x += np.random.default_rng(len(audio)).normal(0, 60, x.size)
    pcm = np.clip(x, -32768, 32767).astype(np.int16).tobytes()
    return any(detector.heard(pcm[i:i + 3200]) for i in range(0, len(pcm), 3200))

tests = [("Rishi", f"Hey {name}", 140), ("Samantha", f"Hey {name}", 240), ("Daniel", f"Hey {name}, open Slack", 180),
         ("Aman", f"Hey {name}", 190)]
print("wake phrase:", [fires(say(v, t, r)) for v, t, r in tests])
others = [("Rishi", "Hey Mint", 180), ("Samantha", "Hey, no way!", 180), ("Daniel", "I have a novel idea for the team.", 180),
          ("Aman", "Hey man, how are you?", 180), ("Karen", "The nova was bright last night.", 180)]
print("must not wake:", [fires(say(v, t, r)) for v, t, r in others])
