"""Record the guide's scenes: the real Mint UI (a separate process, no voice session) on a
neutral desktop, each scene recorded full screen at full resolution. encode_scenes.py then
cuts sharp crops out of them for the site.

The "agent" scene is a real agent run: Astra researches and writes a brief through Mint's
agent hub (it needs the agent keys Mint already uses), and the critters show what it
really does.

Run: <runtime python> guide/make_scenes.py            (about 8 minutes; keep the screen quiet)
     ONLY=talk,marks <runtime python> guide/make_scenes.py
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
        path = RAW / f"{self.name}.mov"
        path.unlink(missing_ok=True)
        self.proc = subprocess.Popen(["screencapture", "-x", "-v", "-V", str(int(self.seconds + 0.99)), str(path)])
        time.sleep(0.5)                           # recording starts a beat after launch
        return self

    def __exit__(self, *exc):
        self.proc.wait()
        time.sleep(0.5)
        print(f"  [scene] {self.name}", flush=True)


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


def tour(p):
    try:
        time.sleep(1.8)
        p.set_state("awake"); time.sleep(1.2)
        for step in (talk, doing, work, marks_scene, show, tricks, faces, moods, agent, chat):
            step(p)
        print("  [done]", flush=True)
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        from mint.core import prefs
        prefs.set("origin", None); prefs.set("position", "bottom-right")
        os._exit(0)


def main():
    presence = ui.Presence(hands_free=True, show_hud=True)
    presence.on("quit", lambda: os._exit(0))

    def build():
        ms.backdrop()
        presence.build()
        threading.Thread(target=tour, args=(presence,), daemon=True).start()
    ui.run_cocoa(build)


main()
