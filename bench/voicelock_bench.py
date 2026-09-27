"""Voice lock on real speech: enrol one LibriSpeech speaker, then stream a
conversation mixing them with others through the Gate, frame by frame, the way
the microphone feeds it. Reports what reached Gemini and how fast.

    python bench/voicelock_bench.py /path/to/LibriSpeech/dev-clean-2
"""
import glob, os, random, subprocess, sys, tempfile, time, wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mint import voicelock  # noqa: E402

RATE = 16000


def load_flac(path):
    out = tempfile.mktemp(suffix=".wav")
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", path, out], check=True)
    w = wave.open(out)
    data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    os.unlink(out)
    return data


def main(root):
    random.seed(7)
    speakers = sorted(os.listdir(root))
    lock = voicelock.VoiceLock.__new__(voicelock.VoiceLock)
    voicelock.VoiceLock.__init__(lock)
    voicelock.PROFILE = Path(tempfile.mktemp(suffix=".json"))    # never touch the real voiceprint
    results = {"user_ok": 0, "user_total": 0, "other_leaked": 0, "other_total": 0,
               "short_ok": 0, "short_total": 0, "takeover_cut": 0, "takeover_total": 0}
    latencies = []
    for user in speakers[:8]:
        files = sorted(glob.glob(f"{root}/{user}/*/*.flac"))
        enrol = [load_flac(f) for f in files[:6]]
        lock.enroll(enrol, [])
        tests = [load_flac(f) for f in files[6:12]]
        others = [load_flac(random.choice(glob.glob(f"{root}/{o}/*/*.flac")))
                  for o in random.sample([s for s in speakers if s != user], 6)]
        gate = voicelock.Gate(lock)
        silence = np.zeros(int(0.8 * RATE), np.float32)

        def run(clip):
            """Stream a clip; return (fraction of its speech sent as real audio, decision latency)."""
            audio = np.concatenate([silence, clip, silence])
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
            sent_real, first_open = 0, None
            step = 1600 * 2                               # 100 ms frames, like the mic tap
            for i in range(0, len(pcm), step):
                frame = pcm[i:i + step]
                level = float(np.sqrt(np.mean(np.frombuffer(frame, np.int16).astype(np.float32) ** 2)) / 32768)
                out, event = gate.feed(frame, level)
                if event and event.startswith("open") and first_open is None:
                    first_open = i / 2 / RATE - 0.8      # seconds after the clip began
                if out and any(out):
                    sent_real += len(out)
            return sent_real / max(1, len(clip) * 2), first_open

        for clip in tests:
            frac, opened = run(clip)
            if frac <= 0.8 and os.environ.get("DEBUG"):
                crops = {k: round(lock.score(clip[int(0.2*RATE):int((0.2+k)*RATE)]), 2) for k in (0.8, 1.2, 1.6, 2.4)}
                print(f"  MISS user {user}: sent {frac:.2f}, scores {crops}, thresholds "
                      f"{ {k: round(lock.threshold(k), 2) for k in (0.8, 1.2, 1.6)} }")
            results["user_total"] += 1
            results["user_ok"] += frac > 0.8
            if opened is not None:
                latencies.append(opened)
        for clip in others:
            frac, _ = run(clip)
            results["other_total"] += 1
            results["other_leaked"] += frac > 0.05
        for clip in tests[:4]:                         # a short command: 0.6 s
            frac, _ = run(clip[int(0.5 * RATE):int(1.1 * RATE)])
            results["short_total"] += 1
            results["short_ok"] += frac > 0.5
        for clip, other in zip(tests[:3], others[:3]):   # user, then someone else with no pause
            mixed = np.concatenate([clip[:3 * RATE], other[:5 * RATE]])
            gate2 = voicelock.Gate(lock)
            gate = gate2
            frac_total, _ = run(mixed)
            # sent fraction should be about the user's part only
            results["takeover_total"] += 1
            results["takeover_cut"] += frac_total < (3 + 2.5) / 8
    print(f"your speech let through:     {results['user_ok']}/{results['user_total']}")
    print(f"other speakers leaked:       {results['other_leaked']}/{results['other_total']}")
    print(f"short commands (0.6 s) through: {results['short_ok']}/{results['short_total']}")
    print(f"someone talking over the end cut off: {results['takeover_cut']}/{results['takeover_total']}")
    if latencies:
        print(f"verified after (s of speech): median {np.median(latencies):.2f}, max {max(latencies):.2f}")


if __name__ == "__main__":
    main(sys.argv[1])
