"""Cut the guide's videos out of the full-screen scenes from make_scenes.py.

Each output is a crop (in screen points, recorded at 2x) of one scene, kept sharp: H.264 at
full resolution (at most MAX_W pixels wide), 60 fps, no audio, plus a poster. The agent scene
is stitched from its chunks: real time while Astra pops out and while it finishes, the long
middle sped up (the site says by how much).

Run: <runtime python> guide/encode_scenes.py [name ...]
"""
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageStat

RAW = Path("/tmp/mint-guide-scenes")
OUT = Path(__file__).resolve().parent / "media"
OUT.mkdir(exist_ok=True)
MAX_W = 1440

# name: (scene, x, y, w, h in points, start s, length s or None)
CUTS = {
    "talk": ("talk", 1000, 690, 440, 203, 0, None),
    "doing":    ("doing", 220, 150, 660, 640, 0, None),
    "work":     ("work", 980, 660, 460, 240, 0, None),
    "marks":    ("marks", 230, 150, 650, 360, 0, None),
    "mark-box": ("marks", 230, 150, 650, 300, 0, 10.5),
    "agent": ("agent", 1000, 690, 440, 203, 0, None),
    "moods": ("moods", 1000, 690, 440, 203, 0, None),
    "show": ("show", 1000, 690, 440, 203, 0, None),
    "tricks":   ("tricks", 900, 400, 540, 532, 0, None),
    "faces":    ("faces", 1300, 690, 140, 196, 0, None),
    "chat":     ("chat", 980, 150, 460, 740, 0, None),
}

MIDDLE_SECONDS = 12            # the agent's middle, whatever its real length


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def stitch_agent():
    """agent-NN.mov chunks -> agent.mov: real start, sped-up middle, real finish. Returns the speed-up."""
    chunks = sorted(RAW.glob("agent-[0-9][0-9].mov"))
    marks = json.loads((RAW / "agent.json").read_text())
    listing = RAW / "agent-list.txt"
    listing.write_text("".join(f"file '{c}'\n" for c in chunks))
    whole = RAW / "agent-whole.mov"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                    str(whole)], check=True)
    # Wall-clock marks -> timeline seconds, chunk by chunk.
    offsets, t = [], 0.0
    for i, c in enumerate(chunks):
        offsets.append((marks[f"chunk{i:02d}"], t, probe(c)))
        t += offsets[-1][2]

    def at(wall):
        for start, base, length in offsets:
            if start <= wall <= start + length:
                return base + wall - start
        return t
    total = t
    started, finished = at(marks["started"]), at(marks["finished"])
    a_end = min(started + 12, finished)                  # pop out, first thoughts and tools
    c_start = max(a_end, finished - 3)                    # the last step, finishing, home, the reply
    c_end = min(total, finished + 12)
    speed = max(1.0, (c_start - a_end) / MIDDLE_SECONDS)
    graph = (f"[0:v]trim={max(0, started - 3.5):.2f}:{a_end:.2f},setpts=PTS-STARTPTS[a];"
             f"[0:v]trim={a_end:.2f}:{c_start:.2f},setpts=(PTS-STARTPTS)/{speed:.3f},fps=60[b];"
             f"[0:v]trim={c_start:.2f}:{c_end:.2f},setpts=PTS-STARTPTS[c];[a][b][c]concat=n=3:v=1[v]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(whole), "-filter_complex", graph, "-map", "[v]",
                    "-c:v", "h264_videotoolbox", "-b:v", "60M", str(RAW / "agent.mov")], check=True)
    info = {"speed": round(speed), "real_seconds": round(c_end - max(0, started - 3.5))}
    (OUT / "agent.json").write_text(json.dumps(info))
    print(f"  agent: {info['real_seconds']}s of real run, middle x{info['speed']}")
    return speed


def encode(name, scene, x, y, w, h, start, length):
    src = RAW / f"{scene}.mov"
    px, py, pw, ph = (int(v * 2) for v in (x, y, w, h))
    pw, ph = pw - pw % 2, ph - ph % 2
    scale = f",scale={MAX_W}:-2:flags=lanczos" if pw > MAX_W else ""
    mp4 = OUT / f"{name}.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(src)]
    if length:
        cmd += ["-t", str(length)]
    cmd += ["-vf", f"crop={pw}:{ph}:{px}:{py}{scale},fps=60,format=yuv420p", "-c:v", "libx264", "-crf", "21",
            "-preset", "slow", "-tune", "animation", "-movflags", "+faststart", "-an", str(mp4)]
    subprocess.run(cmd, check=True)
    dur = probe(mp4)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{dur * 0.6:.2f}", "-i", str(mp4), "-frames:v", "1",
                    "-q:v", "2", str(OUT / f"{name}.jpg")], check=True)
    # Anything other than the pastel desktop in the corners means the screen was in use.
    bad = 0
    for k in range(8):
        frame = Path("/tmp/_scene_frame.png")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{dur * (k + 0.5) / 8:.2f}", "-i", str(mp4),
                        "-frames:v", "1", str(frame)], check=True)
        im = Image.open(frame).convert("RGB")
        W, H = im.size
        for x0, y0 in ((0, 0), (W - 24, 0), (0, H - 24), (W - 24, H - 24)):
            if min(ImageStat.Stat(im.crop((x0, y0, x0 + 24, y0 + 24))).mean) < 150:
                bad += 1
    size = Image.open(OUT / f"{name}.jpg").size
    print(f"  {name:9s} {size[0]}x{size[1]}  {dur:5.1f}s  {mp4.stat().st_size // 1024:6d} KB"
          + (f"  CHECK: {bad} odd corners" if bad else ""))


def main():
    only = set(sys.argv[1:])
    if (not only or "agent" in only) and (RAW / "agent.json").exists():
        stitch_agent()
    for name, cut in CUTS.items():
        if only and name not in only:
            continue
        if not (RAW / f"{cut[0]}.mov").exists():
            print(f"  {name}: no recording of scene {cut[0]}")
            continue
        encode(name, *cut)


main()
