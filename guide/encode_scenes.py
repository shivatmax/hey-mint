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
    # The island (made-up state on the real island; crops stay clear of the menu bar and the Dock).
    "island-meeting":  ("island-meeting", 1080, 762, 360, 96, 0, None),
    "island-teach":    ("island-teach", 200, 128, 1240, 738, 0, None),
    "island-video":    ("island-video", 1040, 640, 400, 226, 0, None),
    "island-schedule": ("island-schedule", 1050, 490, 390, 376, 0, None),
    "island-area":     ("island-area", 200, 128, 1240, 738, 0, None),
    "island-tutor":    ("island-tutor", 200, 128, 1240, 738, 0, None),
    "island-trackers": ("island-trackers", 990, 762, 450, 96, 0, None),
    "island-cards":    ("island-cards", 1050, 520, 390, 346, 0, None),
    "translate":       ("translate", 900, 300, 500, 360, 0, None),
    "dictation":       ("dictation", 200, 128, 1240, 738, 0, None),
    "drop":            ("drop", 1050, 470, 390, 396, 0, None),
    "convert":         ("convert", 1050, 560, 390, 306, 0, None),
    "video-edit":      ("video-edit", 1050, 520, 390, 346, 0, None),
    "clipboard":       ("clipboard", 200, 128, 1240, 738, 0, None),
    "clipboard-window": ("clipboard-window", 1028, 334, 412, 532, 0, None),
    "image-card":      ("image-card", 1028, 232, 412, 634, 0, None),
    # Notch mode: the top middle of the screen, the (demo) menu bar included.
    "notch":           ("notch", 440, 0, 560, 140, 0, None),
    "notch-hover":     ("notch-hover", 440, 0, 560, 140, 0, None),
    "notch-switch":    ("notch-switch", 440, 0, 1000, 932, 0, None),
    # The open notch (640 wide) with its shadow.
    "notch-home":      ("notch-home", 370, 0, 700, 230, 0, None),
    "notch-music":     ("notch-music", 370, 0, 700, 230, 0, None),
    "notch-words":     ("notch-words", 370, 0, 700, 230, 0, None),
    "notch-search":    ("notch-search", 370, 0, 700, 230, 0, None),
    "notch-agents":    ("notch-agents", 370, 0, 700, 272, 0, None),
    "notch-guard":     ("notch-guard", 370, 0, 700, 230, 0, None),
    "notch-meet":      ("notch-meet", 370, 0, 700, 230, 0, None),
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


PROBES = [(40, 60), (1400, 60), (40, 840), (1000, 120), (1180, 300), (140, 400)]   # never under Mint's UI
# Scenes whose own Mint UI covers a probe (the image card is tall: its top edge reaches (1180, 300)).
SKIP_PROBES = {"image-card": {(1180, 300)},
               # the open notch (640 wide, ~200 tall) covers (1000, 120)
               **{name: {(1000, 120)} for name in ("notch-home", "notch-music", "notch-words", "notch-search",
                                                         "notch-agents", "notch-guard", "notch-meet")}}


def foreign(src, start=0.0, length=None, skip=frozenset()) -> list[float]:
    """Moments (seconds) where the raw recording is not all the demo backdrop at points Mint never draws
    on - the real screen showing through (a switch to a full-screen Space, say)."""
    scan = Path("/tmp/_raw_scan")
    subprocess.run(["rm", "-rf", str(scan)], check=False)
    scan.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(src)]
    if length:
        cmd += ["-t", str(length)]
    subprocess.run(cmd + ["-vf", "fps=4,scale=720:-1", str(scan / "f%04d.png")], check=True)
    bad = []
    for frame in sorted(scan.glob("f*.png")):
        im = Image.open(frame).convert("RGB")
        k = im.width / 1440
        for px, py in (p for p in PROBES if p not in skip):
            r, g, b = im.getpixel((int(px * k), int(py * k)))
            if min(r, g, b) < 180 or max(r, g, b) > 252:
                bad.append(start + (int(frame.stem[1:]) - 1) / 4)
                break
    return bad


def encode(name, scene, x, y, w, h, start, length):
    src = RAW / f"{scene}.mov"
    leaked = foreign(src, start, length, SKIP_PROBES.get(scene, frozenset()))
    if leaked:
        print(f"  {name:9s} REJECTED: the real screen shows at {leaked[:6]} s - record the scene again")
        for old in (OUT / f"{name}.mp4", OUT / f"{name}.jpg"):
            old.unlink(missing_ok=True)
        return
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
