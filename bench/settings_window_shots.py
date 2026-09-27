"""Open the Settings window and screenshot each tab (visual check).

    python bench/settings_window_shots.py /tmp/shots
"""
import subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent)); sys.argv = sys.argv[:2]
import AppKit
from PyObjCTools import AppHelper
from mint.settings_window import SettingsWindow

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/settings-shots"); out.mkdir(parents=True, exist_ok=True)
w = SettingsWindow({"audio_status": lambda: "mic MacBook Air Microphone, speaker MacBook Air Speakers, echo cancellation on"})

def run():
    time.sleep(1.0)
    for i in range(w.tabs.numberOfTabViewItems()):
        name = str(w.tabs.tabViewItemAtIndex_(i).identifier()).lower().replace(" ", "-").replace("'", "")
        AppHelper.callAfter(w.tabs.selectTabViewItemAtIndex_, i)
        time.sleep(0.6)
        # By window number, not screen region: a region grabs whatever is on
        # top there (it caught the user's browser when the window was on
        # another Space).
        subprocess.run(["screencapture", "-x", "-o", "-l", str(w.window.windowNumber()), str(out / f"{i}-{name}.png")])
    AppHelper.callAfter(lambda: AppKit.NSApp.terminate_(None))

app = AppKit.NSApplication.sharedApplication(); app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
AppHelper.callAfter(lambda: (w.show(), threading.Thread(target=run, daemon=True).start()))
AppHelper.runEventLoop()
