"""Record the guide's scenes: the real Mint UI (a separate process, no voice session) on a
neutral desktop, each scene recorded full screen at full resolution. encode_scenes.py then
cuts sharp crops out of them for the site.

The "agent" scene is a real agent run: Astra researches and writes a brief through Mint's
agent hub (it needs the agent keys Mint already uses), and the critters show what it
really does.

Run: <runtime python> guide/make_scenes.py            (about 8 minutes; keep the screen quiet)
     ONLY=talk,marks <runtime python> guide/make_scenes.py
     NOTCH=1 <runtime python> guide/make_scenes.py   (notch mode: the notch and notch-hover scenes)
Then: <runtime python> guide/encode_scenes.py
"""
import os
import subprocess
import threading
import time
from pathlib import Path

os.environ["MINT_CAPTURE"] = "1"
os.environ["MINT_CAPTURE_CLEAN"] = "1"
import make_shots as ms                       # backdrop and document card, labels, main_sync
from PyObjCTools import AppHelper
from mint.ui import presence as ui
from mint.app import demo
from mint.ui.activity import phrase
from mint.ui import motion as _motion
_motion.Motion.start = lambda self: None          # no idle tricks in the middle of a scene

RAW = Path("/tmp/mint-guide-scenes")
RAW.mkdir(exist_ok=True)
ONLY = set(filter(None, os.environ.get("ONLY", "").split(",")))
say = demo._say




class Recording:
    """Full-screen recording with macOS's own screencapture: it leaves out the pointer and any
    window hidden from screen sharing (the real Mint), unlike ffmpeg's screen capture."""

    def __init__(self, name, seconds):
        self.name, self.seconds = name, seconds

    def __enter__(self):
        if PEEK:
            self.stop = threading.Event()
            self.thread = threading.Thread(target=_peek_shots, args=(self.name, self.stop), daemon=True)
            self.thread.start()
            return self
        path = RAW / f"{self.name}.mov"
        path.unlink(missing_ok=True)
        self.proc = subprocess.Popen(["screencapture", "-x", "-v", "-V", str(int(self.seconds + 0.99)), str(path)])
        time.sleep(0.5)                           # recording starts a beat after launch
        return self

    def __exit__(self, *exc):
        if PEEK:
            self.stop.set()
            self.thread.join()
            print(f"  [peek] {self.name}", flush=True)
            return
        self.proc.wait()
        time.sleep(0.5)
        print(f"  [scene] {self.name}", flush=True)


PEEK = bool(os.environ.get("PEEK"))      # testing: window-only stills of this process's notch, no recording


def _peek_shots(name, stop):
    """Every second, a still of this process's own notch window only (nothing else on screen)."""
    import Quartz
    out = RAW / "peek"
    out.mkdir(exist_ok=True)
    for old in out.glob(f"{name}-*.png"):
        old.unlink()
    i = 0
    while not stop.wait(1.0):
        for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0):
            if w.get("kCGWindowOwnerPID") == os.getpid() and w.get("kCGWindowLayer") == 27:
                subprocess.run(["screencapture", "-x", "-o", "-l", str(w["kCGWindowNumber"]),
                                str(out / f"{name}-{i:02d}.png")])
                break
        i += 1


class Chunks:
    """Back-to-back fixed-length recordings until stopped: for a scene of unknown length.
    Stitched (and the middle sped up) by encode_scenes.py."""

    def __init__(self, name, chunk=20):
        self.name, self.chunk, self.stop, self.marks = name, chunk, threading.Event(), {}
        for old in RAW.glob(f"{name}-*.mov"):
            old.unlink()

    def mark(self, label):
        self.marks[label] = time.time()

    def run(self):
        i = 0
        while not self.stop.is_set():
            path = RAW / f"{self.name}-{i:02d}.mov"
            self.marks[f"chunk{i:02d}"] = time.time() + 0.5
            subprocess.run(["screencapture", "-x", "-v", "-V", str(self.chunk), str(path)])
            i += 1

    def __enter__(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        time.sleep(0.8)
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        import json
        (RAW / f"{self.name}.json").write_text(json.dumps(self.marks))
        print(f"  [scene] {self.name} ({len([k for k in self.marks if k.startswith('chunk')])} chunks)", flush=True)


def scene(name, seconds):
    def wrap(fn):
        def run(p):
            if ONLY and name not in ONLY:
                return
            with Recording(name, seconds):
                fn(p)
            time.sleep(1.2)
        return run
    return wrap


@scene("talk", 10)
def talk(p):
    say(p, "user_said", "open Slack and go to the on-call channel", gap=0.13)
    time.sleep(0.4); p.set_state("thinking"); time.sleep(0.9)
    p.set_state("working", "Opening Slack"); p.activity_start("open_slack", {"channel": "oncall-support"})
    time.sleep(1.6); p.activity_end("open_slack", True); time.sleep(0.9)
    p.set_state("speaking")
    say(p, "assistant_said", "Done. Slack is open on on-call support.", gap=0.12)
    time.sleep(1.4); p.set_state("awake")


@scene("doing", 12)
def doing(p):
    from mint.ui.effects import fx
    l = ms.labels
    say(p, "user_said", "click Design review, then type looks good to me", gap=0.1)
    time.sleep(0.3); p.set_state("thinking"); time.sleep(0.7)
    p.set_state("working", phrase("click_text", {"text": "Design review"}))
    p.activity_start("click_text", {"text": "Design review"})
    fx.click(l["p2"][0] + 150, l["p2"][1] + 10, "Design review"); time.sleep(1.3)
    p.activity_end("click_text", True); time.sleep(0.6)
    p.set_state("working", phrase("type_text", {"text": "Looks good to me"}))
    p.activity_start("type_text", {"text": "Looks good to me"})
    fx.highlight(*l["field"], seconds=2.0, label="Typing"); time.sleep(2.2)
    p.activity_end("type_text", True); time.sleep(0.8)
    p.set_state("speaking"); say(p, "assistant_said", "Clicked it and typed your note.", gap=0.1)
    time.sleep(1.2); p.set_state("awake")


@scene("work", 17)
def work(p):
    for name, args in (("web_search", {"query": "latest stable Python"}), ("read_file", {"path": "~/Documents/plan.md"}),
                       ("menu", {"path": "Go > Applications"}), ("read_window", {})):
        p.set_state("working", phrase(name, args)); p.activity_start(name, args)
        time.sleep(1.3); p.activity_end(name, True); time.sleep(0.7)
    p.set_state("working"); p.activity_start("type_text", {"text": "Looking into it"}); time.sleep(1.0)
    p.activity_end("type_text", False); time.sleep(1.6)
    p.set_state("working", "Planning")
    for i, label in enumerate(("Step 1/3: read the calendar", "Step 2/3: write the summary", "Step 3/3: export the PDF")):
        p.progress(i, 3, label); time.sleep(1.1)
    p.progress(3, 3, "✓ task complete"); p.celebrate(); time.sleep(1.8); p.progress(0, 0); p.set_state("awake")


@scene("marks", 32)
def marks_scene(p):
    from mint.ui.marks import marks
    from mint.ui.motion import motion
    l = ms.labels
    for question, style, key, note, answer in (
            ("where's the deadline?", "box", "deadline", "the deadline", "Friday, 3 October. Boxed it."),
            ("highlight the budget", "highlight", "p3", "budget cap", "Under four thousand dollars."),
            ("point at the review day", "arrow", "p2", "review day", "Tuesday, with the whole team.")):
        say(p, "user_said", question, gap=0.12)
        time.sleep(0.3); p.set_state("thinking"); time.sleep(0.8)
        p.set_state("working", "Finding it")
        marks.show([l[key]], style, note, 4.2)
        motion.visit_quartz(l[key], stay=2.6)
        p.set_state("speaking"); say(p, "assistant_said", answer, gap=0.1)
        time.sleep(3.4)
        ms.main_sync(marks.clear); p.set_state("awake"); time.sleep(0.8)


def agent(p):
    """A real run: Astra researches and writes a brief, and the critters show it. Recorded in
    chunks for as long as it takes; encode_scenes.py speeds up the middle."""
    if ONLY and "agent" not in ONLY:
        return
    with Chunks("agent") as rec:
        _agent_run(p, rec)
    time.sleep(1.2)


def _agent_run(p, rec):
    import asyncio
    from mint.agents.runtime import hub
    from mint.ui import agent_view
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True, name="agents").start()
    hub.loop = loop
    agent_view.attach(hub)
    finished = threading.Event()

    def watch(event):
        if event["kind"] == "ask":
            threading.Timer(3.0, lambda: hub.reply(event["run"], "Yes, three picks is perfect.")).start()
        if event["kind"] in ("done", "failed", "stopped") and not event.get("parent"):
            finished.set()
    hub.on(watch)
    say(p, "user_said", "have Astra research the best mechanical keyboards under 100 dollars and write a short brief",
        gap=0.1)
    time.sleep(0.4); p.set_state("thinking"); time.sleep(0.9)
    started = hub.delegate(
        "Astra",
        "Research the three best mechanical keyboards under 100 US dollars that are sold today. For each: "
        "name, price, switch type, one line on why it stands out, and a source link. Write it as a short "
        "Markdown brief named keyboards-brief.md. Keep it under 250 words. Use at most 6 web searches.",
        why="Multi-source research with a written deliverable.", helpers=[])
    rec.mark("started")
    print("  [agent]", started[:160], flush=True)
    p.set_state("speaking"); say(p, "assistant_said", "Astra's on it. I'll tell you when the brief is ready.", gap=0.1)
    time.sleep(1.0); p.set_state("awake")
    finished.wait(420)
    rec.mark("finished")
    time.sleep(4.5)                      # the walk home and the hop into the box
    p.set_state("speaking"); say(p, "assistant_said", "Astra's brief is ready: three keyboards, with sources.", gap=0.1)
    time.sleep(2.0); p.set_state("awake")


@scene("moods", 38)
def moods(p):
    from mint.ui.critters import stage

    def ev(**e):
        AppHelper.callAfter(lambda: stage._handle_safe(e))
    run = stage.demo("Sage", "#FFB547", step=2.2)
    time.sleep(2.6)
    ev(kind="progress", run=run, agent="Sage", color="#FFB547", text="asked Astra")
    ev(kind="start", run="helper-astra", agent="Astra", color="#8B7CFF", text="find sources", parent=run)
    time.sleep(2.0)
    ev(kind="tool", run="helper-astra", agent="Astra", color="#8B7CFF", text="searching", tool="web_search")
    time.sleep(2.4)
    ev(kind="done", run="helper-astra", agent="Astra", color="#8B7CFF", text="sources")
    time.sleep(12)
    stage.demo("Luna", "#2EC4B6", step=1.1, outcome="failed")
    time.sleep(0.4)
    stage.demo("Codex", "#10A37F", step=1.2, outcome="stopped")
    time.sleep(12)


@scene("show", 29)
def show(p):
    from mint.ui.critters import stage
    say(p, "user_said", "put on a show", gap=0.12)
    time.sleep(0.6)
    stage.perform()
    time.sleep(26)


@scene("tricks", 33)
def tricks(p):
    from mint.ui.motion import motion
    for name, secs in (("loop", 4.2), ("figure8", 5.2), ("chase", 4.4), ("bee", 6.2), ("spin", 2.8)):
        say(p, "user_said", {"loop": "do a loop the loop", "figure8": "a figure eight", "chase": "chase a star",
                             "bee": "zigzag like a bee", "spin": "spin"}[name], gap=0.08)
        motion.trick(name)
        time.sleep(secs + 1.0)


@scene("faces", 34)
def faces(p):
    from mint.ui.emotes import emotes
    for name, secs in (("love", 3.4), ("laugh", 3.0), ("cool", 3.4), ("surprised", 2.6), ("cry", 3.6),
                       ("sleepy", 3.4), ("dance", 5.0), ("wave", 2.8)):
        emotes.play(name)
        time.sleep(secs + 0.6)


@scene("chat", 6)
def chat(p):
    p.fire("console"); time.sleep(3.5)
    p.fire("console"); time.sleep(0.8)


# --- The island (mint/ui/island.py): Mint's real island and teach effects, fed made-up state -------------

F = {"phase": "", "call": {}, "proc": {}, "video_on": False, "levels": {}, "screen": {}, "teach": {},
     "tutor": {}, "vid": {}}
POINTER = [720.0, 466.0]                      # the scripted pointer (Cocoa points) for the teach scene
VIDEO_FRAMES = sorted(Path.home().glob("Library/Application Support/Mint/videos/bd94f99867c7ea/frame-*.jpg"))


def _fakes():
    """The island reads Mint's state through these; here they return the scene's script."""
    import math as _m
    from mint.tools import meetings
    from mint.tools import screenrec
    from mint.knowledge import teach
    from mint.ui import tutor
    from mint.tools import video
    from mint.ui import teach_fx
    meetings.phase = lambda: F["phase"]
    meetings.on_call = lambda: dict(F["call"])
    meetings.processing = lambda: dict(F["proc"])
    meetings.has_video = lambda: F["video_on"]
    meetings.current_title = lambda: "Design review"
    meetings.list_meetings = lambda n=1: [{"folder": str(RAW)}]
    t0 = time.time()
    meetings.elapsed = lambda: 754 + time.time() - t0
    meetings.levels = lambda: {"others": 0.03 + 0.05 * abs(_m.sin(time.time() * 2.3)), "you": 0.01}
    screenrec.snapshot = lambda: dict(F["screen"])
    teach.snapshot = lambda: dict(F["teach"])
    tutor.snapshot = lambda: dict(F["tutor"])
    video.snapshot = lambda: dict(F["vid"])

    class _Event:                             # teach_fx follows this pointer, not the real one
        @staticmethod
        def mouseLocation():
            import AppKit as _AK
            return _AK.NSMakePoint(*POINTER)

        @staticmethod
        def addGlobalMonitorForEventsMatchingMask_handler_(mask, handler):
            return None

        @staticmethod
        def removeMonitor_(monitor):
            pass

    class _AppKitProxy:
        NSEvent = _Event

        def __getattr__(self, name):
            import AppKit as _AK
            return getattr(_AK, name)
    teach_fx.AppKit = _AppKitProxy()


def _island():
    from mint.ui.island import island
    return island


def _meeting_scene():
    return _island().providers[0].__self__


@scene("island-meeting", 17)
def island_meeting(p):
    F["call"] = {"app": "Google Chrome", "since": 1, "title": "Design review"}
    time.sleep(2.2)
    ms.main_sync(_meeting_scene().toggle_video); time.sleep(1.2)
    F["phase"] = "starting"; time.sleep(0.9)
    F["phase"], F["video_on"] = "recording", True; time.sleep(4.2)
    F["phase"] = "stopping"; time.sleep(0.6)
    F["phase"], F["proc"] = "", {"/x": "transcribing 3/10"}; time.sleep(1.4)
    F["proc"] = {"/x": "transcribing 9/10"}; time.sleep(1.2)
    F["proc"] = {"/x": "writing notes"}; time.sleep(1.0)
    F["proc"], F["video_on"] = {}, False; time.sleep(2.6)
    F["call"] = {}; ms.main_sync(lambda: setattr(_meeting_scene(), "done_until", 0.0)); time.sleep(1.4)


def _glide(to, seconds=0.7):
    """Move the scripted pointer to `to` (Quartz point) along an eased curve."""
    import math as _m
    start = (POINTER[0], POINTER[1])
    end = (to[0], ms.SH - to[1])
    steps = int(seconds * 60)
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        lift = _m.sin(_m.pi * t) * 30
        POINTER[0] = start[0] + (end[0] - start[0]) * e
        POINTER[1] = start[1] + (end[1] - start[1]) * e + lift
        time.sleep(1 / 60)


def _click(step):
    from mint.ui.teach_fx import fx
    point = (POINTER[0] - fx.origin[0], POINTER[1] - fx.origin[1])
    AppHelper.callAfter(fx.burst, point, step)


@scene("island-teach", 16)
def island_teach(p):
    import AppKit
    import Quartz
    from mint.ui.teach_fx import fx
    # screencapture leaves the real pointer out: draw one that follows the script.
    cursor = Quartz.CALayer.layer()
    image = AppKit.NSCursor.arrowCursor().image()
    cursor.setContents_(image.CGImageForProposedRect_context_hints_(None, None, None)[0])
    cursor.setBounds_(Quartz.CGRectMake(0, 0, image.size().width, image.size().height))
    cursor.setAnchorPoint_(Quartz.CGPointMake(0.18, 0.88))

    def follow(_orig=fx.tick):
        _orig()
        Quartz.CATransaction.begin(); Quartz.CATransaction.setDisableActions_(True)
        cursor.setPosition_(Quartz.CGPointMake(POINTER[0] - fx.origin[0], POINTER[1] - fx.origin[1]))
        Quartz.CATransaction.commit()
    fx.tick = follow
    ms.main_sync(lambda: fx.root.addSublayer_(cursor))
    l = ms.labels
    t0 = time.time()
    snap = {"seconds": 0, "clicks": 0, "keys": 0, "goal": "", "auto_stopped": False, "paused": False}

    def tick_clock():
        while F["teach"] and "seconds" in F["teach"]:
            F["teach"] = dict(F["teach"], seconds=time.time() - t0)
            time.sleep(0.2)
    F["teach"] = snap
    threading.Thread(target=tick_clock, daemon=True).start()
    time.sleep(1.6)
    for n, (key, dx) in enumerate((("p2", 150), ("owner", 70), ("field", 60)), 1):
        target = (l[key][0] + dx, l[key][1] + l[key][3] / 2)
        _glide(target)
        time.sleep(0.25)
        _click(n)
        F["teach"] = dict(F["teach"], clicks=n)
        time.sleep(1.1)
    F["teach"] = dict(F["teach"], keys=12); time.sleep(0.8)
    _glide((l["deadline"][0] + 200, l["deadline"][1] + 40)); time.sleep(0.6)
    F["teach"] = {"saving": True}; time.sleep(1.8)
    F["teach"] = {"saved": "Share the launch plan with Priya"}; time.sleep(2.6)
    F["teach"] = {}; time.sleep(1.4)
    ms.main_sync(cursor.removeFromSuperlayer)


@scene("island-video", 16)
def island_video(p):
    title = "Steve Jobs' 2005 Stanford Commencement Address"
    frames = [str(f) for f in VIDEO_FRAMES]
    started = time.time()

    def vid(step, n):
        F["vid"] = {"title": title, "step": step, "frames": frames[:n], "seconds": time.time() - started,
                    "duration": 905, "key": "demo"}
    vid("Getting the video's details", 0); time.sleep(1.6)
    vid("Downloading a small copy to look at", 0); time.sleep(1.6)
    for n in range(1, len(frames) + 1):
        vid("Picking the keyframes", n); time.sleep(0.35)
    for part in range(1, 5):
        vid(f"Listening: part {part} of 4", len(frames)); time.sleep(0.9)
    vid("Writing it up", len(frames)); time.sleep(2.0)
    F["vid"] = {}; time.sleep(6.0)


@scene("island-schedule", 10)
def island_schedule(p):
    import datetime as dt
    now = dt.datetime.now().replace(second=0, microsecond=0)

    def ev(h0, h1, title, rgb):
        return {"start": now + dt.timedelta(hours=h0), "end": now + dt.timedelta(hours=h1), "title": title,
                "all_day": False, "place": "", "rgb": rgb}
    card = {"day": f"{now:%A}", "date": f"{now:%-d %B}",
            "weather": {"temp": 27, "desc": "Partly cloudy", "symbol": "cloud.sun.fill", "range": "23–31°"},
            "events": [ev(-2.5, -2, "Standup", (0.2, 0.6, 1.0)), ev(-0.4, 0.6, "Design review", (0.3, 0.85, 0.45)),
                       ev(2.1, 3, "1:1 with Priya", (1.0, 0.6, 0.1)), ev(5, 6, "Gym", (0.8, 0.4, 1.0))],
            "reminders": ["Send the invoice", "Book flights"],
            "news": ["Rate cut hopes lift markets", "New transit line opens this weekend"]}
    time.sleep(1.2)
    _island().show_schedule(card, seconds=6.5); time.sleep(8.6)


@scene("island-area", 11)
def island_area(p):
    from mint.ui.marks import marks
    l = ms.labels
    x, y = l["deadline"][0] - 14, l["deadline"][1] - 14
    marks.show([(x, y, l["deadline"][2] + 28, l["p2"][1] - y + l["p2"][3] + 14)], "box", "Record this area?", 5.0)
    F["screen"] = {"pending": "the launch deadline part"}; time.sleep(3.6)
    marks.clear()
    started = time.time()
    while time.time() - started < 4.2:
        F["screen"] = {"seconds": time.time() - started, "what": "the launch deadline part", "file": "/x.mp4"}
        time.sleep(0.2)
    F["screen"] = {}; time.sleep(1.6)


@scene("island-tutor", 12)
def island_tutor(p):
    from mint.ui.marks import marks
    l = ms.labels
    F["tutor"] = {"planning": "share the plan"}; time.sleep(1.8)
    for i, (key, say_) in enumerate((("title", "Click the title to open the plan"),
                                     ("owner", "Click Priya's line to mention her"),
                                     ("field", "Type a note in the comment box"))):
        F["tutor"] = {"task": "share the plan", "index": i, "total": 3, "say": say_}
        marks.show([l[key]], "arrow", "", 3.0); time.sleep(2.6)
    marks.clear()
    F["tutor"] = {}; time.sleep(1.4)


@scene("island-trackers", 16)
def island_trackers(p):
    from mint.tools import trackers
    T = {"start": {}, "end": {}}
    trackers.just_started = lambda seconds=3.5: dict(T["start"])
    trackers.recent = lambda seconds=8.0: dict(T["end"])
    time.sleep(1.0)
    T["start"] = {"id": "a", "label": "The download finishes", "kind": "download"}; time.sleep(3.2)
    T["start"] = {}; time.sleep(2.0)
    T["end"] = {"id": "a", "label": "The download finishes", "kind": "download", "outcome": "done",
                "open": str(Path.home() / "Downloads" / "Figma-installer.dmg")}; time.sleep(3.4)
    T["end"] = {"id": "b", "label": "Task auditor", "kind": "claude", "outcome": "done"}; time.sleep(3.2)
    T["end"] = {}; time.sleep(2.2)


@scene("island-cards", 19)
def island_cards(p):
    isl = _island()
    home = Path.home() / "Downloads"
    time.sleep(1.0)
    isl.show_card({"title": "Unread emails", "subtitle": "Inbox", "number": 12, "unit": "unread emails",
                   "icon": "envelope.fill", "tint": "blue", "more": 9,
                   "items": [{"title": "Priya Sharma", "detail": "Launch plan: final review", "trailing": "9:41am",
                              "icon": "person.crop.circle.fill"},
                             {"title": "GitHub", "detail": "[hey-mint] CI passed", "trailing": "8:02am",
                              "icon": "checkmark.seal.fill"},
                             {"title": "Figma", "detail": "A comment on Mint 2.0", "trailing": "Yesterday",
                              "icon": "bubble.left.fill"}]}, seconds=5.2)
    time.sleep(6.0)
    isl.show_card({"title": "Downloaded today", "subtitle": "Downloads", "icon": "arrow.down.circle.fill",
                   "tint": "green", "items": [
                       {"title": "Launch-deck.pdf", "detail": "PDF document", "trailing": "4.2 MB", "icon": "doc.richtext.fill"},
                       {"title": "Figma-installer.dmg", "detail": "Disk image", "trailing": "212 MB", "icon": "externaldrive.fill"},
                       {"title": "receipts-sept.zip", "detail": "Archive", "trailing": "18 MB", "icon": "doc.zipper"},
                       {"title": "team-photo.jpg", "detail": "JPEG image", "trailing": "3.1 MB", "icon": "photo.fill"}]},
                  seconds=5.2)
    time.sleep(6.0)
    isl.show_card({"title": "Automations", "number": 3, "unit": "automations", "icon": "bolt.fill", "tint": "purple",
                   "items": [{"title": "Morning briefing", "detail": "weekdays at 08:30", "trailing": "8:30am",
                              "icon": "sunrise.fill"},
                             {"title": "Tidy Downloads", "detail": "every day", "trailing": "9:00am",
                              "icon": "arrow.clockwise"},
                             {"title": "Standup prep", "detail": "10 min before standups", "trailing": "paused",
                              "icon": "calendar"}]}, seconds=5.0)
    time.sleep(6.0)
    del home


@scene("translate", 14)
def translate_scene(p):
    """A Japanese note on the desk, translated in place by the real pipeline (Gemini)."""
    import AppKit
    import Quartz
    from mint.tools import translate as TR
    from mint.screen import ocr

    def window():
        w = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(930, 300, 440, 300), AppKit.NSWindowStyleMaskBorderless, 2, False)
        w.setLevel_(AppKit.NSFloatingWindowLevel + 1)
        w.setSharingType_(AppKit.NSWindowSharingReadOnly)
        v = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 440, 300))
        v.setWantsLayer_(True)
        v.layer().setBackgroundColor_(Quartz.CGColorCreateGenericRGB(0.12, 0.12, 0.14, 1))
        v.layer().setCornerRadius_(14)
        w.setContentView_(v)
        w.setOpaque_(False)
        w.setBackgroundColor_(AppKit.NSColor.clearColor())
        y = 250
        for text, size, bold in (("チームへのお知らせ", 21, True), ("来週、新しい音声アシスタントを全員に公開します。", 14, False),
                                 ("締め切りは10月3日の金曜日です。質問があれば、チャンネルに書いてください。", 14, False),
                                 ("予算はイベント全体で4,000ドル以内に抑えます。", 14, False)):
            f = AppKit.NSTextField.wrappingLabelWithString_(text)
            f.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, AppKit.NSFontWeightBold if bold else AppKit.NSFontWeightRegular))
            f.setTextColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.9, 0.9, 0.92, 1))
            f.setDrawsBackground_(False)
            f.setFrame_(AppKit.NSMakeRect(26, 0, 390, 60))
            f.sizeToFit()
            fr = f.frame()
            f.setFrameOrigin_(AppKit.NSMakePoint(26, y - fr.size.height))
            v.addSubview_(f)
            y -= fr.size.height + 20
        w.orderFrontRegardless()
        return w
    win = ms.main_sync(window)
    f = win.frame()
    rect = (f.origin.x, ms.SH - f.origin.y - f.size.height, f.size.width, f.size.height)

    def capture():
        image, area = ocr._screen()
        k = image.width / area["width"]
        return image.crop(tuple(int(v * k) for v in (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]))), rect
    TR._capture = capture
    time.sleep(2.0)
    print("  [translate]", TR.translate_screen("English", True)[:120].replace("\n", " "), flush=True)
    time.sleep(6.5)
    from mint.ui.marks import marks
    marks.clear()
    time.sleep(0.8)
    ms.main_sync(win.orderOut_)


@scene("dictation", 12)
def dictation_scene(p):
    """Hold Right Option, talk, let go: the words land in the note's comment box."""
    import math as _m

    import AppKit
    from mint.voice import dictation
    D = {"s": {}}
    dictation.snapshot = lambda: dict(D["s"])
    l = ms.labels
    fx, fy, fw, fh = l["field"]
    box = {}

    def make_text():
        field = AppKit.NSTextField.labelWithString_("")
        field.setFont_(AppKit.NSFont.systemFontOfSize_(15))
        field.setTextColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.12, 0.14, 0.2, 1))
        field.setFrame_(AppKit.NSMakeRect(fx + 14, ms.SH - fy - fh + 12, fw - 28, 22))
        win = AppKit.NSApp().windows()[0] if AppKit.NSApp().windows() else None
        for w in AppKit.NSApp().windows():
            if w.frame().size.width >= ms.SW - 1 and w.level() == AppKit.NSFloatingWindowLevel:
                win = w
        win.contentView().addSubview_(field)
        box["field"] = field
    ms.main_sync(make_text)
    time.sleep(1.2)
    t0 = time.time()
    while time.time() - t0 < 3.4:
        k = time.time() - t0
        levels = [abs(_m.sin(k * 9 + i * 0.7)) * 0.06 * (0.6 + 0.4 * _m.sin(k * 3 + i)) for i in range(40)]
        D["s"] = {"mode": "recording", "seconds": k, "levels": levels, "hands_free": False}
        time.sleep(0.08)
    D["s"] = {"mode": "writing", "seconds": 3.5}
    time.sleep(1.3)
    text = "Looks good to me. Let's ship it on Thursday."
    AppHelper.callAfter(lambda: box["field"].setStringValue_(text))
    D["s"] = {"mode": "done", "text": text}
    time.sleep(2.4)
    D["s"] = {}
    time.sleep(1.8)
    AppHelper.callAfter(box["field"].removeFromSuperview)


@scene("drop", 13)
def drop_scene(p):
    """A PDF dragged towards Mint: the drop target, then what to do with it."""
    isl = _island()
    isl._watch_drag = lambda: None
    time.sleep(1.0)
    isl.dragging = True
    time.sleep(1.6)
    isl.drop_hot = True
    time.sleep(1.2)
    report = Path("/tmp/mint-guide-scenes/Quarterly report.pdf")
    report.write_bytes(b"%PDF-1.4")
    ms.main_sync(lambda: isl.dropped([str(report)], ""))
    time.sleep(4.5)
    isl.hud.fire = lambda *a: print("  [drop chose]", a[:2], flush=True)
    ms.main_sync(lambda: isl.drop.choose("Translate it into Hindi, as a Word doc", False))
    time.sleep(2.2)


@scene("convert", 14)
def convert_scene(p):
    """A PDF translated into a Hindi Word document: the progress on the island, then the file."""
    from mint.tools import convert
    from mint.tools import video_edit
    C = {"s": {}}
    convert.snapshot = lambda: dict(C["s"])
    video_edit.snapshot = lambda: {}
    isl = _island()
    time.sleep(1.0)
    started = time.time()
    for done in range(0, 14):
        C["s"] = {"kind": "convert", "title": "Quarterly report.pdf", "step": "Translating into Hindi",
                  "done": done, "total": 13, "seconds": time.time() - started}
        time.sleep(0.45)
    C["s"] = {"kind": "convert", "title": "Quarterly report.pdf", "step": "Writing the Word document", "done": 13,
              "total": 13, "seconds": time.time() - started}
    time.sleep(1.0)
    C["s"] = {}
    out = Path("/tmp/mint-guide-scenes/Quarterly report (Hindi).docx")
    out.write_bytes(b"PK")
    isl.show_card({"title": "Translated into Hindi", "subtitle": "21 pages · 42 s", "icon": "doc.text.fill",
                   "tint": "blue", "items": [{"title": out.name, "detail": "Word document · headings, lists and 16 "
                                              "tables kept", "trailing": "1.2 MB", "path": str(out)}]},
                  seconds=4.5)
    time.sleep(5.5)


@scene("video-edit", 13)
def video_edit_scene(p):
    """"Make it vertical for Reels and add captions": the edit's progress, then the QA."""
    from mint.tools import convert
    from mint.tools import video_edit
    V = {"s": {}}
    video_edit.snapshot = lambda: dict(V["s"])
    convert.snapshot = lambda: {}
    isl = _island()
    time.sleep(1.0)
    V["s"] = {"step": "Writing the captions", "percent": 0, "summary": "", "seconds": 0}
    time.sleep(1.4)
    for percent in range(0, 101, 6):
        V["s"] = {"step": "Rendering", "percent": percent, "summary": "", "seconds": 1}
        time.sleep(0.22)
    V["s"] = {"step": "Checking it (QA)", "percent": 100, "summary": "", "seconds": 5}
    time.sleep(1.0)
    V["s"] = {}
    isl.show_card({"title": "Edited · launch-demo (edited).mp4", "subtitle": "vertical 9:16 · captions burned in",
                   "icon": "scissors", "tint": "green",
                   "items": [{"title": "Length", "detail": "0:45, as planned", "trailing": "✓", "icon": "clock.fill"},
                             {"title": "Size", "detail": "1080 x 1920 (9:16)", "trailing": "✓",
                              "icon": "rectangle.portrait"},
                             {"title": "Captions", "detail": "18 lines, checked on 3 frames", "trailing": "✓",
                              "icon": "captions.bubble"}]}, seconds=4.5)
    time.sleep(5.5)


@scene("clipboard", 13)
def clipboard_scene(p):
    """"Show my screenshots", then "paste the last three into the chat"."""
    import AppKit
    isl = _island()
    media = Path(__file__).resolve().parent / "media"
    shots = [media / f"{n}.jpg" for n in ("island-schedule", "island-cards", "translate", "drop", "convert")]
    l = ms.labels
    fx, fy, fw, fh = l["field"]
    time.sleep(1.0)
    isl.show_card({"title": "Screenshots", "subtitle": "newest first · say “paste number 2”", "icon": "doc.on.clipboard.fill",
                   "tint": "teal", "items": [{"title": f"screenshot {5 - i} · {time.strftime('%H:%M')}",
                                              "detail": "screenshot", "trailing": f"#{i + 1}", "path": str(shot)}
                                             for i, shot in enumerate(shots)]}, seconds=4.4)
    time.sleep(5.2)
    box = {}

    def thumbs():
        views = []
        win = next(w for w in AppKit.NSApp().windows()
                   if w.frame().size.width >= ms.SW - 1 and w.level() == AppKit.NSFloatingWindowLevel)
        for i, shot in enumerate(shots[:3]):
            image = AppKit.NSImageView.imageViewWithImage_(AppKit.NSImage.alloc().initWithContentsOfFile_(str(shot)))
            image.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            image.setFrame_(AppKit.NSMakeRect(fx + 10 + i * 70, ms.SH - fy - fh + 6, 62, 32))
            image.setWantsLayer_(True)
            image.layer().setCornerRadius_(5)
            image.setAlphaValue_(0.0)
            win.contentView().addSubview_(image)
            views.append(image)
        box["views"] = views
    ms.main_sync(thumbs)
    for view in box["views"]:
        AppHelper.callAfter(view.setAlphaValue_, 1.0)
        time.sleep(0.45)
    isl.show_card({"title": "Pasted 3 screenshots", "subtitle": "into the chat, oldest first · not sent",
                   "icon": "checkmark.circle.fill", "tint": "green"}, seconds=3.0)
    time.sleep(3.6)
    for view in box["views"]:
        AppHelper.callAfter(view.removeFromSuperview)


@scene("clipboard-window", 15)
def clipboard_window_scene(p):
    """Mint turns into the clipboard: everything copied, three screenshots picked in order, the Mint and Pinned tabs."""
    import tempfile
    from mint.tools import clipboard as C
    from mint.ui import clipboard_window as CW
    media = Path(__file__).resolve().parent / "media"
    C.STORE = Path(tempfile.mkdtemp()) / "clipboard"
    C._loaded = True
    now = time.time()

    def pic(name):
        return C._png_file(C._as_png(media / name))
    C.HISTORY[:] = [
        {"id": "a1", "kind": "text", "text": "Looks good to me. Let's ship it on Thursday.", "label": "Looks good",
         "source": "you", "at": now - 40},
        {"id": "a2", "kind": "image", "image": pic("island-schedule.jpg"), "label": "screenshot 5 · screen",
         "source": "screenshot", "at": now - 120},
        {"id": "a3", "kind": "image", "image": pic("island-cards.jpg"), "label": "screenshot 4 · screen",
         "source": "screenshot", "at": now - 300},
        {"id": "a4", "kind": "text", "text": "https://hey-mint.pages.dev/docs#shortcuts", "label": "link",
         "source": "mint", "at": now - 900},
        {"id": "a6", "kind": "image", "image": pic("convert.jpg"), "label": "screenshot 3 · window",
         "source": "screenshot", "at": now - 4000},
        {"id": "a7", "kind": "text", "text": "Invoice #INV-2044 - due 12 October", "label": "Invoice",
         "source": "mint", "at": now - 8000}]
    C.VERSION[0] += 1
    w = CW.window
    C._save_pins({"invoice": dict(C.HISTORY[5], name="Invoice #INV-2044", at=now),
                  "ship note": dict(C.HISTORY[0], name="Ship note", at=now - 60)})
    time.sleep(0.8)
    ms.main_sync(w.show)
    time.sleep(0.6)
    print("  clipboard frame", tuple(w.panel.frame().origin), tuple(w.panel.frame().size), flush=True)
    time.sleep(1.8)
    for chosen in ("a2", "a3", "a6"):
        ms.main_sync(lambda c=chosen: w.click(c, c != "a2", False))
        time.sleep(0.8)
    time.sleep(1.4)
    ms.main_sync(lambda: w.set_filter("mint"))
    time.sleep(1.8)
    ms.main_sync(lambda: w.set_filter("pinned"))
    time.sleep(1.8)
    ms.main_sync(w.close)
    time.sleep(1.2)


@scene("image-card", 44)
def image_card_scene(p):
    """A real picture from Image Playground on the card, then a typed change redraws it."""
    import threading
    from mint.ui import image_card
    from mint.tools import imagegen
    made_before = set(imagegen.images_folder().glob("*.png"))
    time.sleep(1.2)
    threading.Thread(target=lambda: imagegen.create("a lighthouse on a cliff at sunset", "illustration"),
                     daemon=True).start()
    for _ in range(80):                                   # drawn (about 7-15 s)
        time.sleep(0.5)
        if image_card.current() is not None and not image_card.card.busy:
            break
    print("  image card frame", tuple(image_card.card.panel.frame().origin), tuple(image_card.card.panel.frame().size),
          flush=True)
    time.sleep(2.0)
    change = "add a small sailing boat on the sea"
    for i in range(1, len(change) + 1):                  # typed into the card
        ms.main_sync(lambda t=change[:i]: image_card.card.field.setStringValue_(t))
        time.sleep(0.045)
    time.sleep(0.5)
    ms.main_sync(image_card.card.submit)
    time.sleep(1.0)
    for _ in range(80):
        time.sleep(0.5)
        if not image_card.card.busy:
            break
    time.sleep(3.0)
    ms.main_sync(image_card.close)
    time.sleep(1.5)
    import AppKit
    for path in set(imagegen.images_folder().glob("*.png")) - made_before:     # the scene's own pictures
        AppKit.NSWorkspace.sharedWorkspace().recycleURLs_completionHandler_([AppKit.NSURL.fileURLWithPath_(str(path))],
                                                                            None)


# --- Notch mode (mint/ui/notch.py): Mint in the camera notch. A run of its own (NOTCH=1): the HUD is
# built once, as the orb or as the notch.

NOTCH = bool(os.environ.get("NOTCH"))
NOTCH_PREFS = {"notch_mode": True, "mic": True, "voice": True}
if os.environ.get("AGENTS_MINI"):
    NOTCH_PREFS["agent_compact"] = True       # the Agents tab minimized to one line


def _talk_level(seconds):
    """A voice-like level for the sound bars."""
    import math as _m
    end = time.time() + seconds
    return lambda: min(0.9, 0.25 + 0.35 * abs(_m.sin(time.time() * 5.1)) * abs(_m.sin(time.time() * 1.7))) \
        if time.time() < end else 0.0


def _speak_bars(p, seconds):
    level = _talk_level(seconds)
    while (v := level()) > 0:
        p.set_level(v); time.sleep(1 / 20)
    p.set_level(0.0)


@scene("notch", 22)
def notch_talk(p):
    time.sleep(1.4)
    threading.Thread(target=_speak_bars, args=(p, 2.4), daemon=True).start()
    say(p, "user_said", "what's on my calendar today?", gap=0.16)
    time.sleep(0.5); p.set_state("thinking"); time.sleep(1.1)
    p.set_state("working", phrase("calendar_events", {})); p.activity_start("calendar_events", {})
    for i, label in enumerate(("Step 1/3: read the calendar", "Step 2/3: check the invites", "Step 3/3: sum it up")):
        p.progress(i, 3, label); time.sleep(1.2)
    p.progress(3, 3, "✓ done"); p.activity_end("calendar_events", True); time.sleep(0.5); p.progress(0, 0)
    p.set_state("speaking")
    threading.Thread(target=_speak_bars, args=(p, 3.6), daemon=True).start()
    say(p, "assistant_said", "Three meetings. The design review at eleven is the big one.", gap=0.2)
    time.sleep(1.6); p.set_state("awake"); time.sleep(4.5)


@scene("notch-hover", 13)
def notch_hover(p):
    from mint.ui import notch
    time.sleep(1.2)
    F["mouse"] = (notch.notch.cx + 10, notch.notch.top - 12)          # the pointer over the notch
    time.sleep(3.4)
    F["mouse"] = (notch.notch.cx, notch.notch.top - 400)
    time.sleep(1.4)
    p.set_state("sleeping"); time.sleep(2.4)
    NOTCH_PREFS["mic"] = False; p.set_state("paused"); time.sleep(2.4)
    NOTCH_PREFS["mic"] = True; p.set_state("awake"); time.sleep(1.6)


@scene("notch-home", 17)
def notch_home(p):
    """Hover: the open notch (Mint + this week's calendar); then files land on the shelf."""
    from mint.ui import notch
    from mint.ui import notch_shelf
    time.sleep(1.2)
    F["mouse"] = (notch.notch.cx - 40, notch.notch.top - 12)
    time.sleep(5.2)
    F["mouse"] = (notch.notch.cx, notch.notch.top - 400)
    time.sleep(1.6)
    notch_shelf.add_paths([str(path) for path in _shelf_files()])

    def dropped():
        notch.notch.tab, notch.notch.drag_until = "shelf", time.monotonic() + 3.0
    AppHelper.callAfter(dropped)
    time.sleep(1.5)
    F["mouse"] = (notch.notch.cx + 60, notch.notch.top - 60)      # stays open while the pointer is on it
    time.sleep(4.4)
    F["mouse"] = (notch.notch.cx, notch.notch.top - 400)
    time.sleep(1.6)


@scene("notch-music", 17)
def notch_music(p):
    """The charger goes in (the battery peeks from the wings), then a song starts: the notch is the player."""
    from mint.ui import notch
    time.sleep(1.2)
    F["battery"] = dict(F["battery"], plugged=True, charging=True, source="AC Power", time_to_full=65,
                        time_to_empty=None)
    time.sleep(5.0)                               # noticed within 2 s, shown for 3 s
    F["music"] = dict(F["song"], state="playing", position=48.0)
    from mint.tools import music
    music._refresh()                              # the watcher would only notice on its next pass
    AppHelper.callAfter(notch.notch.peek_music, 4.5)
    time.sleep(5.2)
    F["mouse"] = (notch.notch.cx - 40, notch.notch.top - 12)
    time.sleep(4.2)
    F["mouse"] = (notch.notch.cx, notch.notch.top - 400)
    time.sleep(1.4)


def _shelf_files():
    """A few everyday files for the shelf (made once, in /tmp: the real shelf is never touched)."""
    from PIL import Image, ImageDraw
    root = Path("/tmp/Launch")                   # a short path for the hover line
    root.mkdir(exist_ok=True)
    photo = root / "Sunset.jpg"
    if not photo.exists():
        img = Image.new("RGB", (640, 420))
        draw = ImageDraw.Draw(img)
        for y in range(420):
            t = y / 419
            draw.line([(0, y), (640, y)], fill=(int(250 - 90 * t), int(150 - 80 * t), int(90 + 60 * t)))
        draw.ellipse((250, 230, 390, 370), fill=(255, 214, 140))
        draw.rectangle((0, 330, 640, 420), fill=(40, 34, 70))
        img.save(photo, quality=90)
    plan = root / "Launch plan.pdf"
    if not plan.exists():
        page = Image.new("RGB", (620, 800), "white")
        draw = ImageDraw.Draw(page)
        draw.rectangle((50, 60, 420, 90), fill=(30, 30, 30))
        for i in range(14):
            draw.rectangle((50, 130 + i * 34, 570 - (i % 4) * 60, 144 + i * 34), fill=(190, 190, 190))
        page.save(plan)
    folder = root / "Brand assets"
    folder.mkdir(exist_ok=True)
    notes = root / "Interview notes.txt"
    if not notes.exists():
        notes.write_text("Interview notes\n\n- Ask about the on-call rota\n- Demo the notch shelf\n")
    return [photo, plan, folder, notes]


def _song_art() -> str:
    from PIL import Image, ImageDraw
    path = Path("/tmp/mint-guide-shelf/cover.png")
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        img = Image.new("RGB", (300, 300))
        draw = ImageDraw.Draw(img)
        for y in range(300):
            draw.line([(0, y), (300, y)], fill=(int(200 - 150 * y / 299), int(30 + 20 * y / 299), int(60 + 140 * y / 299)))
        draw.ellipse((70, 70, 230, 230), outline=(255, 235, 245), width=10)
        img.save(path)
    return str(path)


def _notch_fakes():
    """The open notch's panes, scripted: a made-up week in the calendar, a battery that can be
    plugged in, a song that can start, and a shelf kept in /tmp (the real ones are never read or written)."""
    import datetime as dt
    from mint.tools import music
    from mint.ui import music_player
    from mint.ui import notch_battery
    from mint.ui import notch_calendar
    from mint.ui import notch_shelf  # noqa: F401 (the notch reads the player)
    notch_shelf.STORE = Path("/tmp/mint-guide-shelf/shelf.json")
    notch_shelf.STORE.unlink(missing_ok=True)
    today, now = dt.date.today(), dt.datetime.now().replace(second=0, microsecond=0)
    at = lambda day, h, m=0: dt.datetime.combine(day, dt.time(h, m))
    ev = lambda i, title, start, end, rgb, place="", all_day=False: {
        "id": f"demo-{i}", "title": title, "start": start, "end": end, "all_day": all_day, "place": place,
        "rgb": rgb, "recurring": False}
    blue, orange, green, red = (0.04, 0.52, 1.0), (1.0, 0.58, 0.0), (0.2, 0.78, 0.35), (1, 0.23, 0.19)
    base = now.replace(minute=0)
    week = {today: [ev(1, "Design review", base - dt.timedelta(minutes=15), base + dt.timedelta(minutes=45), blue, "Room 4"),
                    ev(2, "Lunch with Priya", base + dt.timedelta(hours=2), base + dt.timedelta(hours=3), orange),
                    ev(3, "Ship the beta build", base + dt.timedelta(hours=4), base + dt.timedelta(hours=5), green, "Zoom")],
            today + dt.timedelta(days=1): [ev(4, "Dentist", at(today + dt.timedelta(days=1), 9, 30),
                                              at(today + dt.timedelta(days=1), 10, 15), red, "12 High St")]}
    notch_calendar._source = lambda first, days: {first + dt.timedelta(days=i): list(week.get(first + dt.timedelta(days=i), []))
                                                  for i in range(days)}
    notch_calendar.available = lambda: True
    F["battery"] = dict(notch_battery.read(), present=True, percent=64, plugged=False, charging=False, charged=False,
                        low_power=False, source="Battery Power", time_to_full=None, time_to_empty=182, calculating=False)
    notch_battery.read = lambda: dict(F["battery"])
    F["song"] = {"app": "Spotify", "state": "stopped", "title": "Midnight City", "artist": "M83",
                 "album": "Hurry Up, We're Dreaming", "position": 0.0, "duration": 243.0, "artwork": _song_art(),
                 "artwork_url": "", "uri": "", "volume": 60, "shuffle": False, "repeat": False, "running": True}
    F["music"] = dict(F["song"], title="")

    def refresh():
        with music._lock:
            music._now = dict(F["music"])
            music._stamp = time.monotonic()
        return dict(music._now)
    music._refresh = refresh
    music._observe_notifications = lambda: None


@scene("notch-words", 16)
def notch_words(p):
    """A song in the wings; "Hey Mint" brings the little Mint back; hovering while it talks opens it all."""
    from mint.tools import music
    from mint.ui import notch
    F["music"] = dict(F["song"], state="playing", position=30.0)
    music._refresh()
    p.set_state("sleeping")
    time.sleep(2.2)                               # the artwork and bars in the wings
    p.set_state("awake")
    time.sleep(2.0)                               # listening: the little Mint is back
    say(p, "user_said", "what's the weather like this evening?", gap=0.1)
    p.set_state("speaking")
    F["mouse"] = (notch.notch.cx - 40, notch.notch.top - 12)     # hovering while it talks: its words inside
    say(p, "assistant_said", "Clear and 24 degrees this evening, dropping to 19 by midnight. No rain.", gap=0.12)
    time.sleep(2.6)
    F["mouse"] = (notch.notch.cx, notch.notch.top - 400)
    p.set_state("sleeping")
    time.sleep(3.0)


@scene("notch-search", 14)
def notch_search_scene(p):
    """Files Mint found open the notch as tiles; hovering one says where it is."""
    from mint.ui import notch_search
    time.sleep(1.2)
    files = [str(path) for path in _shelf_files()]
    say(p, "user_said", "find my launch files", gap=0.1)
    p.set_state("thinking")
    time.sleep(0.6)
    notch_search.show(paths=files, query="launch", title="4 files for “launch”")
    p.set_state("speaking")
    say(p, "assistant_said", "Four files for the launch. They're in the notch.", gap=0.1)
    time.sleep(2.2)

    def hover(on):
        for view in list(notch_search._views):
            if view.order and view._visible():
                view.hover(view.tiles[view.order[1]], on)
    AppHelper.callAfter(hover, True)
    time.sleep(3.2)
    AppHelper.callAfter(hover, False)
    p.set_state("sleeping")
    time.sleep(4.0)


@scene("notch-switch", 11)
def notch_switch(p):
    """Live switching: out of the notch as the orb (drop, bounce home), then back in (the flight)."""
    from mint.ui import notch
    time.sleep(1.2)
    AppHelper.callAfter(notch.leave, True)
    time.sleep(4.2)
    AppHelper.callAfter(notch.enter, True)
    time.sleep(4.6)


def _agents_demo():
    """Claude mode with made-up sessions only: the scene process never reads this Mac's real Claude Code or
    Codex logs (no real project or prompt can reach a public clip) and never opens the approval socket the
    real Mint owns. Returns (put, emit): put(sessions) replaces what the pane sees; emit(kind, session)
    plays an event (started / waiting / finished) as the watcher would."""
    import copy
    from mint.tools import agent_hooks
    from mint.tools import agent_watch
    from mint.ui import notch_agents
    agent_watch.watcher.start = lambda: None
    notch_agents._hooked = True
    agent_hooks.decide = lambda key, decision: True
    agent_hooks.rule_for = lambda key: "Bash(npm test:*)"
    agent_hooks.installed = lambda: True
    shown = {"items": []}
    agent_watch.sessions = lambda: copy.deepcopy(shown["items"])
    agent_watch.watcher.sessions = agent_watch.sessions

    def put(items):
        shown["items"] = list(items)
        AppHelper.callAfter(notch_agents._changed)

    def emit(kind, session):
        AppHelper.callAfter(notch_agents._event, kind, copy.deepcopy(session))
    return put, emit


@scene("notch-agents", 25)
def notch_agents_scene(p):
    """Claude mode: a Claude Code session in the notch - it reads, writes a diff, asks to run the tests (Allow
    from the notch), they pass, and it's done."""
    from mint.ui import notch
    from mint.tools.agent_watch import Session, Step
    put, emit = AGENTS
    now = time.time()
    s = Session(key="claude:demo", app="claude", id="demo", cwd="/srv/code/korus", where="cli",
                state="thinking", since=now, updated=now, turn_started=now,
                prompt="Use the 2026 TVA rate and round totals to the cent")
    other = Session(key="codex:demo", app="codex", id="demo2", cwd="/srv/code/atlas", state="done",
                    since=now - 300, updated=now - 300, summary="Renamed the API client and fixed its imports.")
    code = [(" ", 10, "import { Item } from './types'"), (" ", 11, ""), (" ", 12, "const TVA = 0.196"),
            (" ", 13, ""), (" ", 14, "export function total(items: Item[]) {"),
            (" ", 15, "  const sum = items.reduce((s, i) => s + i.price, 0)"), (" ", 16, "  return sum * (1 + TVA)"),
            (" ", 17, "}")]
    diff = [(" ", 11, ""), ("-", 12, "const TVA = 0.196"), ("+", 12, "const TVA = 0.20   // 2026"), (" ", 13, ""),
            (" ", 14, "export function total(items: Item[]) {"),
            (" ", 15, "  const sum = items.reduce((s, i) => s + i.price, 0)"),
            ("-", 16, "  return sum * (1 + TVA)"), ("+", 16, "  return Math.round(sum * (1 + TVA) * 100) / 100"),
            (" ", 17, "}")]
    path = "/srv/code/korus/src/invoice.ts"

    def step(n, verb, target, status, detail):
        return Step(id=f"t{n}", verb=verb, target=target, status=status, started=time.time(), detail=detail)
    time.sleep(1.0)
    put([s, other]); emit("started", s)
    time.sleep(1.6)                                             # the wing: Claude's sparkle turning
    s.state = "working"
    s.steps = [step(1, "Read", "invoice.ts", "run", {"kind": "code", "file": "invoice.ts", "path": path, "lines": []})]
    s.turn_steps = 1
    put([s, other])
    F["mouse"] = (notch.notch.cx + 120, notch.notch.top - 120)    # the pointer comes to the open notch
    AppHelper.callAfter(notch.notch._agents_open)
    time.sleep(1.4)
    s.steps[0].status, s.steps[0].detail["lines"] = "ok", code
    put([s, other]); time.sleep(1.8)
    s.steps.append(step(2, "Edit", "invoice.ts", "run", {"kind": "diff", "file": "invoice.ts", "path": path,
                                                          "lines": diff}))
    s.turn_steps = 2
    put([s, other]); time.sleep(2.2)
    s.steps[1].status = "ok"
    s.steps.append(step(3, "Run", "Run the tests", "run", {"kind": "bash", "cmd": "npm test", "out": [], "ok": None}))
    s.turn_steps, s.state = 3, "waiting"
    s.approval = {"id": "a1", "tool": "Bash", "verb": "Run", "target": "Run the tests",
                  "detail": {"kind": "bash", "cmd": "npm test"}, "always": True}
    put([s, other]); emit("waiting", s)
    time.sleep(2.6)
    from mint.ui import notch_agents
    AppHelper.callAfter(lambda: notch_agents._panes[0]._decide("allow"))        # Allow, from the notch
    time.sleep(0.25)
    s.approval, s.state = None, "working"
    put([s, other]); time.sleep(1.4)
    s.steps[2].status = "ok"
    s.steps[2].detail = {"kind": "bash", "cmd": "npm test", "ok": True,
                         "out": ["PASS  tests/invoice.test.ts", "  ✓ applies the 2026 TVA rate (3 ms)",
                                 "  ✓ rounds totals to the cent (1 ms)", "Tests:  48 passed, 48 total"]}
    put([s, other]); time.sleep(2.0)
    s.state, s.since = "done", time.time()
    s.summary = "Switched TVA to the 2026 rate (20%) and rounded every total to the cent. All 48 tests pass."
    put([s, other]); emit("finished", s)
    time.sleep(3.6)
    F["mouse"] = (notch.notch.cx, notch.notch.top - 400)
    time.sleep(4.5)


@scene("notch-guard", 14)
def notch_guard_scene(p):
    """The guard: Mint is asked to delete old invoices; before anything moves it shows exactly what and waits for a
    yes (here, said out loud)."""
    from mint.core import guard
    guard._telegram_ask = lambda pending: None
    guard._from_phone = lambda: False
    guard._away = lambda: False
    guard._audit = lambda *a: None
    time.sleep(1.2)
    say(p, "user_said", "delete last year's invoices", gap=0.12)
    p.set_state("thinking")
    time.sleep(1.2)
    danger = guard.Danger("delete", "move 3 files to the Trash",
                          ["~/Documents/Invoices/2025-10 Acme.pdf (84.2 KB)",
                           "~/Documents/Invoices/2025-11 Acme.pdf (91.0 KB)",
                           "~/Documents/Invoices/2025-12 Northwind.pdf (77.5 KB)"])
    result = {}
    asker = threading.Thread(target=lambda: result.update(ok=guard.ask(danger, timeout=60)), daemon=True)
    asker.start()
    time.sleep(4.2)
    say(p, "user_said", "yes", gap=0.1)
    guard.heard("yes")
    asker.join(3)
    p.set_state("speaking")
    say(p, "assistant_said", "Moved the three invoices to the Trash.", gap=0.12)
    time.sleep(1.6)
    p.set_state("awake")
    time.sleep(1.8)


@scene("notch-meet", 12)
def notch_meet_scene(p):
    """A Google Meet call with Mint: "start a Google Meet", then the notch's red camera while the call is on; the
    user, on their phone in the call, asks something and Mint answers into the call."""
    import types
    from mint.app import meet_call
    time.sleep(1.0)
    say(p, "user_said", "start a Google Meet", gap=0.12)
    p.set_state("thinking")
    time.sleep(1.0)
    p.set_state("speaking")
    say(p, "assistant_said", "Starting the Google Meet. The link is on Telegram.", gap=0.1)
    time.sleep(1.0)
    meet_call._call = types.SimpleNamespace(live=True, over=threading.Event())
    p.set_state("awake")
    time.sleep(2.6)
    say(p, "user_said", "what's on my screen right now?", gap=0.1)
    p.set_state("speaking")
    say(p, "assistant_said", "Your editor, with the release notes open.", gap=0.1)
    time.sleep(1.4)
    p.set_state("awake")
    time.sleep(2.4)
    meet_call._call = None


def _notch_demo():
    """The demo Mint in the notch, without touching the real Mint: the setting is answered here,
    never saved, and the demo never restarts itself (that restart would stop the shared engine)."""
    from mint.ui import notch
    from mint.core import prefs
    real_get, real_set = prefs.get, prefs.set
    prefs.get = lambda key: NOTCH_PREFS[key] if key in NOTCH_PREFS else real_get(key)
    prefs.set = lambda key, value: None if key in NOTCH_PREFS else real_set(key, value)
    notch._watching[0] = True
    _notch_fakes()
    # The recording makes Mint visible to capture; the eye button shows what a user sees (hidden).
    import AppKit
    from mint.ui import sharing
    sharing.visible = lambda: False
    sharing._desired = lambda: AppKit.NSWindowSharingReadOnly
    F["mouse"] = (200.0, 300.0)

    class _Event:                             # the notch's hover follows this pointer, not the real one
        @staticmethod
        def mouseLocation():
            import AppKit as _AK
            return _AK.NSMakePoint(*F["mouse"])

        @staticmethod
        def pressedMouseButtons():
            return 0

    class _AppKitProxy:
        NSEvent = _Event

        def __getattr__(self, name):
            import AppKit as _AK
            return getattr(_AK, name)
    notch.AppKit = _AppKitProxy()


def _menu_bar_cover():
    """A plain menu bar over the real one (its menus and status icons stay out of the clips).
    Under the notch's black shape (main menu + 3), over the status icons."""
    import AppKit
    import Quartz
    from mint.ui import gfx
    frame = AppKit.NSScreen.screens()[0].frame()
    SW, SH, bar = frame.size.width, frame.size.height, 30       # 2 points over the real bar's bottom edge
    w = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(0, SH - bar, SW, bar), AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
    w.setLevel_(Quartz.CGWindowLevelForKey(Quartz.kCGMainMenuWindowLevelKey) + 2)
    w.setHasShadow_(False)
    w.setIgnoresMouseEvents_(True)
    w.setSharingType_(AppKit.NSWindowSharingReadOnly)
    w.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                             | AppKit.NSWindowCollectionBehaviorStationary)
    v = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, SW, bar))
    v.setWantsLayer_(True)
    g = Quartz.CAGradientLayer.layer()               # the backdrop's own gradient, so the strip lines up
    g.setFrame_(Quartz.CGRectMake(0, bar - SH, SW, SH))
    g.setColors_([gfx.cg((0.86, 0.92, 0.95)), gfx.cg((0.91, 0.89, 0.98)), gfx.cg((0.97, 0.90, 0.93))])
    g.setStartPoint_(Quartz.CGPointMake(0, 0)); g.setEndPoint_(Quartz.CGPointMake(1, 1))
    v.layer().addSublayer_(g)
    tint = Quartz.CALayer.layer()
    tint.setFrame_(Quartz.CGRectMake(0, 0, SW, bar))
    tint.setBackgroundColor_(gfx.cg((1, 1, 1), 0.35))
    v.layer().addSublayer_(tint)
    w.setContentView_(v)
    w.orderFrontRegardless()
    return w


def tour(p):
    try:
        time.sleep(1.8)
        p.set_state("awake"); time.sleep(1.2)
        _fakes()
        steps = (notch_talk, notch_hover, notch_home, notch_music, notch_words, notch_search_scene,
                 notch_agents_scene, notch_guard_scene, notch_meet_scene, notch_switch) if NOTCH else (talk, doing, work, marks_scene, show, tricks, faces, moods, agent, chat, island_meeting,
                     island_teach, island_video, island_schedule, island_area, island_tutor,
                     island_trackers, island_cards, translate_scene,
                     dictation_scene, drop_scene, convert_scene, video_edit_scene,
                     clipboard_scene, clipboard_window_scene, image_card_scene)
        for step in steps:
            step(p)
        print("  [done]", flush=True)
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        os._exit(0)


def _demo_prefs():
    """The demo's orb sits bottom-right, without touching the real Mint's settings (the same file)."""
    from mint.core import prefs
    real_get, real_set = prefs.get, prefs.set
    fixed = {"position": "bottom-right", "origin": None, "meeting_offer": True, "face": True, "notch_mode": False}
    prefs.get = lambda key: fixed[key] if key in fixed else real_get(key)
    prefs.set = lambda key, value: None if key in fixed else real_set(key, value)


def main():
    _demo_prefs()
    global AGENTS
    AGENTS = _agents_demo()                   # (always: no scene ever shows this Mac's real agent sessions)
    if NOTCH:
        _notch_demo()
    presence = ui.Presence(hands_free=True, show_hud=True)
    presence.on("quit", lambda: os._exit(0))

    def build():
        back = ms.backdrop() if not PEEK else None
        if NOTCH and back is not None:
            back.contentView().subviews()[0].setHidden_(True)      # no document card under the notch
            _menu_bar_cover()
        presence.build()
        threading.Thread(target=tour, args=(presence,), daemon=True).start()
    ui.run_cocoa(build)


main()
