"""Open the Skills & Memory window on a COPY of the installed skills and memory,
and screenshot both tabs (visual check; nothing real is edited).

    python bench/brain_window_shots.py /tmp/brain-shots
"""
import shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent)); sys.argv = sys.argv[:2]
import AppKit
from PyObjCTools import AppHelper
from mint import brain_window, membank, skillbook

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/brain-shots"); out.mkdir(parents=True, exist_ok=True)
runtime = Path.home() / "Library" / "Application Support" / "Mint"
tmp = Path(tempfile.mkdtemp(prefix="brain-shots-"))
for name in ("skills", "memory"):
    if (runtime / name).exists():
        shutil.copytree(runtime / name, tmp / name)
skillbook.ROOT = tmp / "skills"
membank.ROOT = tmp / "memory"; membank.BANK = membank.ROOT / "bank.json"; membank.VIEW = membank.ROOT / "MEMORY.md"
membank.LEGACY = tmp / "none.md"
if not membank.blocks():
    for text, group, fixed in [("I'm Alex, a developer at Acme", "core", True),
                               ("My manager is Meera", "people", False),
                               ("The on-call Slack channel is oncall-support", "work", False)]:
        membank.add_exact(text, group, fixed)

def shot(i, name):
    """Render the window's own views to PNG - no screen capture, so nothing else
    on screen (another app, another Space) can end up in the picture."""
    import threading as _t
    done = _t.Event()

    def render():
        import Quartz
        window = brain_window._window.window
        window.displayIfNeeded()
        # Just this window, by its number: nothing else on screen is captured.
        image = Quartz.CGWindowListCreateImage(Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
                                               window.windowNumber(), Quartz.kCGWindowImageBoundsIgnoreFraming)
        if image is not None:
            rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
        else:   # fall back to drawing the selected tab's view
            view = brain_window._window.tabs.selectedTabViewItem().view()
            rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
            view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
        data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
        data.writeToFile_atomically_(str(out / f"{i}-{name}.png"), True)
        done.set()
    AppHelper.callAfter(render)
    done.wait(5)

def run():
    time.sleep(1.2); shot(0, "skills")
    AppHelper.callAfter(brain_window._window.show, "memory"); time.sleep(0.8); shot(1, "memory")
    AppHelper.callAfter(lambda: AppKit.NSApp.terminate_(None))

app = AppKit.NSApplication.sharedApplication(); app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
brain_window.open_window("skills")
AppHelper.callAfter(lambda: threading.Thread(target=run, daemon=True).start())
AppHelper.runEventLoop()
