"""The welcome window clip for the guide (guide/media/onboarding.mp4 + .jpg; the tour inside it plays
guide/media clips, so re-record those first). The real onboarding window in this process, its pages
stepped through by script, each state saved as a window-only still (no desktop, no pointer) and the
stills joined with crossfades.

Nothing is saved: settings stay in memory (a made-up name, no real "about me"), the Gemini and
TypeSafe keys are never written to .env, voice previews are silent, Allow is never clicked.

Run: <runtime python> guide/make_onboarding.py      (about 45 s; the window shows on screen)
"""
import os, subprocess, sys, threading, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MINT_CAPTURE"] = "1"                  # the window can be captured (effects.SHARING)

import AppKit
from PyObjCTools import AppHelper
from mint.core import config
from mint.core import prefs
from mint.voice import voices
from mint.ui import onboarding as onb
from mint.agents import catalog

OUT = ROOT / "guide" / "media"
RAW = Path("/tmp/mint-guide-onboarding")
RAW.mkdir(parents=True, exist_ok=True)
FAKE = {"user_name": "Alex", "assistant_name": "Mint", "about_me": "", "voice_name": "Zephyr", "notch_mode": False}

# --- never touch the real settings, keys or speakers ------------------------------------------------
prefs._ensure_loaded()
prefs._save = lambda: None
prefs._notify = lambda changes: None
prefs._values.update(FAKE)
catalog.write_key = lambda env, value: None
onb._save_jev_key = lambda key: None
voices.preview = lambda *a, **k: None
for key in (config.API_KEY_ENV, onb.JEV_ENV):        # every page in its first-run state
    os.environ.pop(key, None)

o = onb.onboarding
stills: list[tuple[Path, float]] = []             # (png, seconds on screen)


def main(fn, *args):
    """Run on the main thread and wait for it."""
    done = threading.Event()
    AppHelper.callAfter(lambda: (fn(*args), done.set()))
    done.wait(5)


def still(name: str, seconds: float, settle: float = 1.4) -> None:
    time.sleep(settle)
    path = RAW / f"{len(stills):02d}-{name}.png"
    subprocess.run(["screencapture", "-x", "-o", "-l", str(o.window.windowNumber()), str(path)], check=True)
    stills.append((path, seconds))
    print(f"  {path.name}", flush=True)


def connected() -> None:
    o.fields["gemini_key"].setStringValue_("x" * 39)        # dots on screen; never saved (see above)
    os.environ[config.API_KEY_ENV] = "x" * 39               # this process only: Skip for now -> Continue
    o._key_checked("ok", "")
    o._paint_chrome()


def script() -> None:
    try:
        main(o.show, 0)
        still("welcome", 3.6, settle=2.2)
        main(o._go, onb.PAGES.index("about"))
        main(o._role, "Developer")
        still("about", 3.4)
        main(o._go, onb.PAGES.index("connect"))
        still("connect", 2.6)
        main(connected)
        still("connected", 2.6, settle=1.0)
        main(o._go, onb.PAGES.index("jev"))
        still("jev", 2.6)
        main(o._go, onb.PAGES.index("voice"))
        still("voice", 2.8)
        main(o._go, onb.PAGES.index("permissions"))
        still("permissions", 3.0)
        main(o._go, onb.PAGES.index("shortcuts"))
        still("shortcuts", 2.8)
        main(o._go, onb.PAGES.index("tour"))
        still("tour-1", 3.6, settle=3.0)                    # the first clip under way
        main(o._show_feature, 1, True)
        still("tour-2", 3.0, settle=3.0)
        main(o._go, onb.PAGES.index("done"))
        still("done", 3.2, settle=1.0)
        main(lambda: (o._leave(), o.window.orderOut_(None)))
        encode()
    finally:
        AppHelper.callAfter(AppKit.NSApp().terminate_, None)


def encode(fade: float = 0.45) -> None:
    """Stills to one clip: scaled to the guide's 1440x968, crossfaded."""
    args, chain, offset = ["ffmpeg", "-y", "-loglevel", "error"], "", 0.0
    for path, seconds in stills:
        args += ["-loop", "1", "-t", f"{seconds:.2f}", "-framerate", "30", "-i", str(path)]
    for i in range(len(stills)):
        chain += f"[{i}:v]scale=1440:968:flags=lanczos,format=yuv420p,setsar=1[v{i}];"
    last = "v0"
    for i in range(1, len(stills)):
        offset += stills[i - 1][1] - fade
        chain += f"[{last}][v{i}]xfade=transition=fade:duration={fade}:offset={offset:.2f}[x{i}];"
        last = f"x{i}"
    mp4 = OUT / "onboarding.mp4"
    subprocess.run(args + ["-filter_complex", chain.rstrip(";"), "-map", f"[{last}]", "-r", "30",
                           "-c:v", "libx264", "-preset", "slow", "-crf", "24", "-pix_fmt", "yuv420p",
                           "-movflags", "+faststart", "-an", str(mp4)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(stills[0][0]), "-vf", "scale=1440:968:flags=lanczos",
                    "-q:v", "3", str(OUT / "onboarding.jpg")], check=True)
    print(f"  {mp4} ({sum(s for _, s in stills) - fade * (len(stills) - 1):.1f} s)", flush=True)


app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
threading.Thread(target=script, daemon=True, name="onboarding-film").start()
AppHelper.runEventLoop()
