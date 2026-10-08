"""Screenshots for the guide: a separate Mint UI (no voice session) on a neutral
backdrop, so nothing personal from the real screen is captured.
Run: MINT_CAPTURE=1 <runtime python> guide/make_shots.py"""
import os, sys, subprocess, threading, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MINT_CAPTURE"] = "1"
OUT = ROOT / "guide" / "shots" / "raw"
OUT.mkdir(parents=True, exist_ok=True)
os.environ["MINT_DEMO_SHOTS"] = str(OUT)

import AppKit, Quartz
from PyObjCTools import AppHelper
from mint.core import hotkeys
from mint.ui import chat
from mint.ui import presence as ui
from mint.app import demo
from mint.ui import gfx

hotkeys.HotKeys.register = lambda self, *a, **k: False          # the real Mint owns ⌘J
chat.ChatPanel._load_history = lambda self: None                  # no real conversation in shots

screen = AppKit.NSScreen.screens()[0].frame()
SW, SH = screen.size.width, screen.size.height
labels = {}


def main_sync(fn):
    done = threading.Event(); box = {}
    def run():
        try: box["v"] = fn()
        finally: done.set()
    AppHelper.callAfter(run); done.wait(5)
    return box.get("v")


def shot(name, window=None):
    path = OUT / f"{name}.png"
    if window is not None:
        subprocess.run(["screencapture", "-x", f"-l{window}", str(path)], check=False)
    else:
        subprocess.run(["screencapture", "-x", str(path)], check=False)
    print("  [shot]", name, flush=True)


def backdrop():
    w = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        screen, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
    w.setLevel_(AppKit.NSFloatingWindowLevel)
    w.setIgnoresMouseEvents_(True)
    w.setSharingType_(AppKit.NSWindowSharingReadOnly)
    w.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces)
    v = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, SW, SH))
    v.setWantsLayer_(True)
    g = Quartz.CAGradientLayer.layer()
    g.setFrame_(Quartz.CGRectMake(0, 0, SW, SH))
    g.setColors_([gfx.cg((0.86, 0.92, 0.95)), gfx.cg((0.91, 0.89, 0.98)), gfx.cg((0.97, 0.90, 0.93))])
    g.setStartPoint_(Quartz.CGPointMake(0, 0)); g.setEndPoint_(Quartz.CGPointMake(1, 1))
    v.layer().addSublayer_(g)
    # A document card to click, type into and mark up.
    cx, cy, cw, ch = SW * 0.16, SH * 0.2, SW * 0.44, SH * 0.62
    card = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(cx, cy, cw, ch))
    card.setWantsLayer_(True)
    L = card.layer()
    L.setBackgroundColor_(gfx.cg((1, 1, 1))); L.setCornerRadius_(14)
    L.setShadowOpacity_(0.18); L.setShadowRadius_(24); L.setShadowOffset_(Quartz.CGSizeMake(0, -8))
    v.addSubview_(card)
    lines = [("title", "Launch plan — Mint 2.0", 26, True),
             ("sub", "Shared with the team · updated today", 13, False),
             ("p1", "We ship the new voice experience to everyone next week.", 16, False),
             ("deadline", "The launch deadline is Friday, 3 October.", 16, True),
             ("p2", "Design review happens on Tuesday with the whole team.", 16, False),
             ("owner", "Owner: Nina handles the release notes and the demo.", 16, False),
             ("p3", "Budget for the launch event stays under $4,000.", 16, False),
             ("risk", "Biggest risk: the wake word on older Macs.", 16, False)]
    y = ch - 64
    for key, text, size, bold in lines:
        f = AppKit.NSTextField.labelWithString_(text)
        f.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, AppKit.NSFontWeightBold if bold else AppKit.NSFontWeightRegular))
        f.setTextColor_(AppKit.NSColor.colorWithRed_green_blue_alpha_(0.12, 0.14, 0.2, 1 if key != "sub" else 0.55))
        f.sizeToFit()
        fr = f.frame()
        f.setFrameOrigin_(AppKit.NSMakePoint(40, y))
        card.addSubview_(f)
        # Quartz rect (top-left origin) of this line on screen
        labels[key] = (cx + 40, SH - (cy + y + fr.size.height), fr.size.width, fr.size.height)
        y -= 34 if key in ("title",) else (48 if key == "sub" else 44)
    field = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(40, 36, cw - 80, 44))
    field.setWantsLayer_(True)
    field.layer().setBorderWidth_(1); field.layer().setBorderColor_(gfx.cg((0.8, 0.82, 0.88)))
    field.layer().setCornerRadius_(10)
    card.addSubview_(field)
    labels["field"] = (cx + 40, SH - (cy + 36 + 44), cw - 80, 44)
    w.setContentView_(v)
    w.orderFrontRegardless()
    return w


def tour(presence):
    time.sleep(1.5)
    demo.run(presence, quit_after=False)          # 01-listening … 16-asleep
    p = presence
    from mint.ui.emotes import emotes
    from mint.ui.motion import motion
    from mint.ui.marks import marks
    from mint.ui.critters import stage
    from mint.ui.effects import fx
    p.set_state("awake"); time.sleep(1.0)

    # Expressions on the orb
    for name, wait in (("smile", 0.9), ("love", 1.2), ("laugh", 0.9), ("cry", 1.6), ("cool", 1.6),
                       ("surprised", 0.7), ("sleepy", 1.6), ("dizzy", 1.2), ("wave", 1.0), ("clap", 1.0),
                       ("praise", 1.2), ("dance", 1.6), ("angry", 1.0), ("blush", 1.0)):
        emotes.play(name); time.sleep(wait); shot(f"emote-{name}"); time.sleep(2.2)

    # Marks on the document
    for style, key, note in (("box", "deadline", "the deadline"), ("underline", "owner", ""),
                             ("highlight", "p3", "budget cap"), ("circle", "risk", ""), ("arrow", "p2", "review day")):
        marks.show([labels[key]], style, note, 6.0)
        time.sleep(1.4); shot(f"mark-{style}")
        main_sync(marks.clear); time.sleep(0.4)

    # Paw print + ripple on a click, typing highlight
    fx.click(labels["p1"][0] + 120, labels["p1"][1] + 10, "")
    time.sleep(0.25); shot("fx-paw"); time.sleep(1.0)

    # Tricks: a strip of frames for each
    for trick, frames, gap in (("loop", 12, 0.16), ("bounce", 10, 0.2), ("hops", 12, 0.25), ("figure8", 12, 0.25)):
        motion.trick(trick)
        time.sleep(0.15)
        for i in range(frames):
            shot(f"trick-{trick}-{i:02d}"); time.sleep(gap)
        time.sleep(3.5)

    # Critters
    stage.demo("Astra", "#8B7CFF", step=1.5)
    time.sleep(0.5); stage.demo("Luna", "#2EC4B6", step=1.6, outcome="failed")
    time.sleep(0.5); stage.demo("Codex", "#10A37F", step=1.4)
    time.sleep(0.3); shot("critter-popout")
    time.sleep(2.0); shot("critter-working")
    time.sleep(3.3); shot("critter-ask")
    time.sleep(2.5)
    AppHelper.callAfter(lambda: stage._handle_safe({"kind": "start", "run": "helper1", "agent": "Nova",
                                                    "color": "#FF6FB5", "text": "check facts", "parent": next(
                                                        (r for r, c in stage.critters.items() if c.name == "Astra"), None)}))
    time.sleep(1.0); shot("critter-helper")
    time.sleep(3.0); shot("critter-done")
    time.sleep(8)
    stage.perform()
    for t, name in ((3.2, "show-out"), (2.4, "show-wave"), (2.3, "show-solo"), (5.0, "show-dance"), (3.4, "show-finale")):
        time.sleep(t); shot(name)
    time.sleep(8)

    # Windows: Settings (safe tabs) and Skills
    from mint.ui.settings import SettingsWindow
    sw = main_sync(lambda: SettingsWindow({"train_voice": lambda: None, "forget_voice": lambda: None,
                                          "audio_status": lambda: "MacBook Air Microphone · echo cancellation on"}))
    main_sync(sw.show); time.sleep(1.0)
    for ident, name in (("Your voice", "settings-yourvoice"), ("Mint's voice", "settings-mintvoice"),
                        ("Audio", "settings-audio"), ("Appearance", "settings-appearance")):
        main_sync(lambda ident=ident: sw.tabs.selectTabViewItemWithIdentifier_(ident)); time.sleep(0.6)
        shot(name, window=main_sync(lambda: sw.window.windowNumber()))
    main_sync(lambda: sw.window.orderOut_(None))
    from mint.ui import brain as brain_window
    brain_window.open_window("skills"); time.sleep(2.5)
    win = main_sync(lambda: brain_window._window.window.windowNumber())
    shot("brain-skills", window=win)
    main_sync(lambda: brain_window._window.window.orderOut_(None))
    time.sleep(0.5)
    print("  [done]", flush=True)
    os._exit(0)


def retake(presence):
    """Only the frames that caught something else on screen."""
    time.sleep(1.5)
    p = presence
    from mint.ui.emotes import emotes
    from mint.ui.motion import motion
    from mint.ui.marks import marks
    from mint.ui.activity import phrase
    p.set_state("awake"); time.sleep(1.0)
    for name, args, shot_name in (("open_app", {"name": "Slack"}, "03-opening-morph"),
                                  ("menu", {"path": "Go > Applications"}, "10d-orb-menu")):
        p.set_state("working", phrase(name, args)); p.activity_start(name, args)
        time.sleep(0.9); shot(shot_name); time.sleep(0.4); p.activity_end(name, True); time.sleep(1.6)
    p.set_state("awake"); time.sleep(1.0)
    for name, wait in (("love", 1.2), ("cool", 1.6), ("surprised", 0.7), ("sleepy", 1.6), ("dizzy", 1.2),
                       ("praise", 1.2), ("angry", 1.0), ("blush", 1.0)):
        emotes.play(name); time.sleep(wait); shot(f"emote-{name}"); time.sleep(2.2)
    marks.show([labels["p2"]], "arrow", "review day", 6.0); time.sleep(1.4); shot("mark-arrow")
    main_sync(marks.clear); time.sleep(0.5)
    for trick, frames, gap in (("loop", 12, 0.16), ("bounce", 10, 0.2)):
        motion.trick(trick); time.sleep(0.15)
        for i in range(frames):
            shot(f"trick-{trick}-{i:02d}"); time.sleep(gap)
        time.sleep(3.5)
    print("  [done]", flush=True)
    os._exit(0)


def main():
    presence = ui.Presence(hands_free=True, show_hud=True)
    presence.on("quit", lambda: os._exit(0))

    def build():
        backdrop()
        presence.build()
        job = retake if os.environ.get("RETAKE") else tour
        threading.Thread(target=job, args=(presence,), daemon=True).start()
    ui.run_cocoa(build)


if __name__ == "__main__":
    main()
