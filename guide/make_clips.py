"""Short video clips for the guide: the real Mint UI (a separate process, no voice
session) on a neutral backdrop, each feature recorded in its own screen region.

Run: <runtime python> guide/make_clips.py   (about 5 minutes; keep the screen quiet)
Then: <runtime python> guide/encode_clips.py
"""
import os
import subprocess
import threading
import time
from pathlib import Path

os.environ["MINT_CAPTURE"] = "1"
import make_shots as ms                       # backdrop, labels, patches (⌘J, chat history)
from PyObjCTools import AppHelper
from mint import ui, demo
from mint.activity import phrase
from mint import motion as _motion
_motion.Motion.start = lambda self: None          # no idle tricks in the middle of a clip

RAW = Path("/tmp/mint-guide-clips")
RAW.mkdir(exist_ok=True)
ONLY = set(filter(None, os.environ.get("ONLY", "").split(",")))
cx, cy = 1394, 825                            # the orb's home, in screen points (top-left origin)
R = {
    "orb": (cx - 430, cy - 150, 476, 192),
    "emote": (cx - 70, cy - 112, 118, 154),
    "doc": (200, 130, 1240, 736),
    "card": (220, 150, 670, 626),
    "crit": (cx - 470, cy - 215, 516, 257),
    "trick": (cx - 350, cy - 318, 396, 360),
    "chat": (990, 170, 450, 697),
}


def clip(name, region, seconds, action):
    """Record `region` for `seconds` while `action()` runs (from the start)."""
    if ONLY and name not in ONLY:
        return
    x, y, w, h = region
    path = RAW / f"{name}.mov"
    path.unlink(missing_ok=True)
    rec = subprocess.Popen(["screencapture", "-x", "-v", "-V", str(int(seconds + 0.99)), "-R", f"{x},{y},{w},{h}", str(path)])
    time.sleep(0.35)                          # recording starts a beat after launch
    action()
    rec.wait()
    time.sleep(0.6)
    print(f"  [clip] {name}", flush=True)


def tour(p):
    time.sleep(1.8)
    from mint.effects import fx
    from mint.emotes import emotes
    from mint.motion import motion
    from mint.marks import marks
    from mint.critters import stage
    say = demo._say
    p.set_state("awake"); time.sleep(1.2)

    def talk():
        say(p, "user_said", "open Slack and go to the on-call channel", gap=0.13)
        time.sleep(0.4); p.set_state("thinking"); time.sleep(0.9)
        p.set_state("working", "Opening Slack"); p.activity_start("open_slack", {"channel": "oncall-support"})
        time.sleep(1.6); p.activity_end("open_slack", True); time.sleep(0.9)
        p.set_state("speaking")
        say(p, "assistant_said", "Done. Slack is open on on-call support.", gap=0.12)
        time.sleep(1.0); p.set_state("awake")
    clip("talk", R["orb"], 9, talk)
    time.sleep(1.5)

    def morphs():
        for name, args in (("web_search", {"query": "latest stable Python"}), ("read_file", {"path": "~/Documents/plan.md"}),
                           ("menu", {"path": "Go > Applications"}), ("read_window", {})):
            p.set_state("working", phrase(name, args)); p.activity_start(name, args)
            time.sleep(1.3); p.activity_end(name, True); time.sleep(0.7)
        p.set_state("awake")
    clip("morphs", R["orb"], 9, morphs)
    time.sleep(1.2)

    def done_fail():
        p.set_state("working"); p.activity_start("click_text", {"text": "Threads"}); time.sleep(1.0)
        p.activity_end("click_text", True); time.sleep(1.6)
        p.activity_start("type_text", {"text": "Looking into it"}); time.sleep(1.0)
        p.activity_end("type_text", False); time.sleep(1.6); p.set_state("awake")
    clip("done-fail", R["orb"], 6, done_fail)
    time.sleep(1.2)

    def progress():
        p.set_state("working", "Planning")
        for i, label in enumerate(("Step 1/3: read the calendar", "Step 2/3: write the summary", "Step 3/3: export the PDF")):
            p.progress(i, 3, label); time.sleep(1.0)
        p.progress(3, 3, "✓ task complete"); p.celebrate(); time.sleep(1.6); p.progress(0, 0); p.set_state("awake")
    clip("progress", R["orb"], 6, progress)
    time.sleep(1.2)

    def stop():
        p.set_state("working"); p.activity_start("click_text", {"text": "Send"}); time.sleep(0.8)
        say(p, "user_said", "stop", gap=0.05); p.stopped(); time.sleep(1.8); p.set_state("awake")
    clip("stop", R["orb"], 4, stop)
    time.sleep(1.2)

    def click_type():
        l = ms.labels
        p.activity_start("click_text", {"text": "Design review"})
        fx.click(l["p2"][0] + 150, l["p2"][1] + 10, "Design review"); time.sleep(1.2)
        p.activity_end("click_text", True); time.sleep(0.6)
        p.activity_start("type_text", {"text": "Looks good to me"})
        fx.highlight(*l["field"], seconds=2.0, label="Typing"); time.sleep(2.2)
        p.activity_end("type_text", True)
    clip("click-type", R["card"], 5, click_type)
    time.sleep(1.2)

    def flourishes():
        for name, args in (("open_app", {"name": "Notes"}), ("open_url", {"url": "https://example.com"}),
                           ("web_search", {"query": "weather"}), ("type_text", {"text": "hello"}),
                           ("scroll", {"direction": "down"}), ("write_file", {"path": "plan.md"}),
                           ("compose_email", {"to": "sam@example.com"}), ("media_key", {"action": "play"}),
                           ("run_applescript", {"script": "..."}), ("screenshot", {})):
            p.set_state("working", phrase(name, args)); p.activity_start(name, args)
            time.sleep(1.1); p.activity_end(name, True); time.sleep(0.45)
        p.set_state("awake")
    clip("flourishes", R["orb"], 17, flourishes)
    time.sleep(1.5)

    def chat():
        p.fire("console"); time.sleep(2.6)
    clip("chat", R["chat"], 3, chat)
    if not ONLY or "chat" in ONLY:
        p.fire("console")                     # close it again
    time.sleep(1.2)

    for name, secs in (("smile", 3), ("laugh", 3), ("love", 3.5), ("blush", 3), ("cry", 4), ("angry", 3),
                       ("surprised", 2.6), ("sleepy", 3.6), ("dizzy", 3.2), ("cool", 3.4), ("wave", 2.8),
                       ("clap", 3), ("praise", 3), ("dance", 5)):
        clip(f"emote-{name}", R["emote"], secs, lambda name=name: emotes.play(name))
        time.sleep(0.8)

    l = ms.labels
    for style, key, note in (("box", "deadline", "the deadline"), ("underline", "owner", ""),
                             ("highlight", "p3", "budget cap"), ("circle", "risk", ""), ("arrow", "p2", "review day")):
        def mark(style=style, key=key, note=note):
            time.sleep(0.3)
            marks.show([l[key]], style, note, 4.0)
            motion.visit_quartz(l[key], stay=2.4)
            time.sleep(4.2)
            ms.main_sync(marks.clear)
        clip(f"mark-{style}", R["doc"], 6, mark)
        time.sleep(1.2)

    for trick, secs in (("hops", 5), ("loop", 4), ("figure8", 5), ("bounce", 3), ("bee", 6), ("chase", 4),
                        ("peek", 4), ("spin", 2.4)):
        clip(f"trick-{trick}", R["trick"], secs, lambda trick=trick: motion.trick(trick))
        time.sleep(1.0)

    # Critters
    clip("agent-life", R["crit"], 15, lambda: stage.demo("Astra", "#8B7CFF", step=1.35))
    time.sleep(3)

    def team():
        run = stage.demo("Sage", "#FFB547", step=2.2)
        time.sleep(2.6)
        AppHelper.callAfter(lambda: stage._handle_safe({"kind": "progress", "run": run, "agent": "Sage",
                                                        "color": "#FFB547", "text": "asked Astra"}))
        AppHelper.callAfter(lambda: stage._handle_safe({"kind": "start", "run": "helper-astra", "agent": "Astra",
                                                        "color": "#8B7CFF", "text": "find sources", "parent": run}))
        time.sleep(2.0)
        AppHelper.callAfter(lambda: stage._handle_safe({"kind": "tool", "run": "helper-astra", "agent": "Astra",
                                                        "color": "#8B7CFF", "text": "searching", "tool": "web_search"}))
        time.sleep(2.4)
        AppHelper.callAfter(lambda: stage._handle_safe({"kind": "done", "run": "helper-astra", "agent": "Astra",
                                                        "color": "#8B7CFF", "text": "sources"}))
    clip("agent-team", R["crit"], 9, team)
    time.sleep(14)

    def trouble():
        stage.demo("Luna", "#2EC4B6", step=1.1, outcome="failed")
        time.sleep(0.4)
        stage.demo("Codex", "#10A37F", step=1.2, outcome="stopped")
    clip("agent-trouble", R["crit"], 13, trouble)
    time.sleep(6)

    def poke():
        run = "poke-demo"
        AppHelper.callAfter(lambda: stage._handle_safe({"kind": "start", "run": run, "agent": "Mochi", "color": "#FF6FB5",
                                                        "text": "putting on a show"}))
        time.sleep(2.2)
        for i in range(4):
            AppHelper.callAfter(lambda: stage.clicked(stage.critters[run].x, 34 + 20, 1)); time.sleep(0.9)
        AppHelper.callAfter(stage._poke_box); time.sleep(1.5)
        AppHelper.callAfter(lambda: stage._handle_safe({"kind": "done", "run": run, "agent": "Mochi",
                                                        "color": "#FF6FB5", "text": "ok"}))
    clip("agent-poke", R["crit"], 9, poke)
    time.sleep(6)

    clip("showtime", R["crit"], 26, stage.perform)
    time.sleep(6)
    def nudge():
        motion.nudge("up", "medium"); time.sleep(1.4); motion.nudge("left", "medium"); time.sleep(1.4)
        motion.nudge("back"); time.sleep(1.2)
        ms.main_sync(lambda: motion._fly([motion._swoop(motion._center(), motion._home())]))
    clip("nudge", R["trick"], 5, nudge)
    time.sleep(1.5)
    from mint import prefs
    prefs.set("origin", None); prefs.set("position", "bottom-right")

    print("  [done]", flush=True)
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
