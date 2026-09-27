"""Encode /tmp/mint-guide-clips/*.mov into guide/clips/*.mp4 (H.264, no audio, web-ready)
with a poster image each, and flag clips where something other than the backdrop
showed up in the corners (the screen was in use)."""
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageStat

RAW = Path("/tmp/mint-guide-clips")
OUT = Path(__file__).resolve().parent / "clips"
OUT.mkdir(exist_ok=True)
only = set(sys.argv[1:])


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    data = json.loads(out)
    s = data["streams"][0]
    return s["width"], s["height"], float(data["format"]["duration"])


report = {}
for mov in sorted(RAW.glob("*.mov")):
    name = mov.stem
    if only and name not in only:
        continue
    w, h, dur = probe(mov)
    scale = "scale=iw/2:-2:flags=lanczos," if w > 1100 else ""
    mp4 = OUT / f"{name}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mov), "-vf", f"{scale}fps=30,format=yuv420p",
                    "-c:v", "libx264", "-crf", "25", "-preset", "slow", "-movflags", "+faststart", "-an", str(mp4)],
                   check=True)
    poster = OUT / f"{name}.jpg"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{dur * 0.55:.2f}", "-i", str(mp4), "-frames:v", "1",
                    "-q:v", "3", str(poster)], check=True)
    # Contamination: sample frames, check the four corners are the pastel backdrop.
    bad = 0
    for k in range(6):
        frame = Path(f"/tmp/_f{k}.png")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{dur * (k + 0.5) / 6:.2f}", "-i", str(mov),
                        "-frames:v", "1", str(frame)], check=True)
        im = Image.open(frame).convert("RGB")
        W, H = im.size
        for x0, y0 in ((0, 0), (W - 24, 0), (0, H - 24), (W - 24, H - 24)):
            m = ImageStat.Stat(im.crop((x0, y0, x0 + 24, y0 + 24))).mean
            if min(m) < 175:
                bad += 1
    report[name] = {"size": f"{w}x{h}", "sec": round(dur, 1), "kb": mp4.stat().st_size // 1024, "suspect": bad}
for k, v in report.items():
    print(f"{k:18s} {v}")
