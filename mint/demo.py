"""A tour of the HUD and effects, with no Gemini session: `mint --demo`.

Plays each state and animation in turn - waking, word-by-word captions, the
activity badges, a click spark, an app opening, a finished task, the console -
so they can be previewed or checked. With MINT_DEMO_SHOTS=<dir> it also
takes a screenshot at each step (run with MINT_CAPTURE=1 so the HUD is
visible to screen capture).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from . import ui


def _shot(name: str) -> None:
    folder = os.environ.get("MINT_DEMO_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        subprocess.run(["screencapture", "-x", str(Path(folder) / f"{name}.png")], check=False)
        print(f"  [shot {name}]", flush=True)


def _say(presence, method, text, gap=0.05):
    """Stream text in small pieces, the way transcripts arrive."""
    for i, word in enumerate(text.split(" ")):
        getattr(presence, method)(("" if i == 0 else " ") + word, new_turn=(i == 0))
        time.sleep(gap)


def run(presence: "ui.Presence", quit_after: bool = True) -> None:
    from .effects import fx
    from .activity import phrase as activity_phrase
    import AppKit

    screen = AppKit.NSScreen.screens()[0].frame().size
    p = presence
    time.sleep(1.0)

    p.set_state("awake")
    time.sleep(0.5)
    _say(p, "user_said", "open Slack and go to the on-call channel", gap=0.12)
    time.sleep(0.12)
    _shot("01-listening-words")
    time.sleep(0.6)

    p.set_state("thinking")
    time.sleep(0.6)
    _shot("02-thinking")

    p.set_state("working", "Opening Slack")
    p.activity_start("open_slack", {"channel": "oncall-support"})
    time.sleep(0.75)
    _shot("03-opening-morph")
    time.sleep(1.0)
    p.activity_end("open_slack", True)
    time.sleep(0.3)
    _shot("04-done-check")
    time.sleep(1.0)

    p.activity_start("click_text", {"text": "Threads"})
    time.sleep(0.2)
    fx.click(screen.width * 0.35, screen.height * 0.45, "Threads")
    time.sleep(0.2)
    _shot("05-click-flight")
    time.sleep(0.25)
    _shot("06-click-ripple")
    time.sleep(0.6)
    p.activity_end("click_text", True)
    time.sleep(0.8)

    p.set_state("thinking", "Reading")
    p.activity_start("read_window", {})
    time.sleep(0.6)
    _shot("07-reading-scan")
    time.sleep(0.8)
    p.activity_end("read_window", True)

    p.set_state("working")
    p.activity_start("type_text", {"text": "Looking into it now"})
    fx.highlight(screen.width * 0.3, screen.height * 0.7, 520, 44, seconds=2.0, label="Typing")
    time.sleep(0.5)
    _shot("08-typing-highlight")
    time.sleep(0.8)
    p.activity_end("type_text", False)
    time.sleep(0.25)
    _shot("09-failed-shake")
    time.sleep(1.0)

    fx.scroll(screen.width * 0.5, screen.height * 0.5, "down")
    time.sleep(0.25)
    _shot("10-scroll")
    time.sleep(0.6)

    # The orb becomes the task: a magnifier for a search, a folder for files, a tile for a menu.
    for name, args, shot in (("web_search", {"query": "latest stable Python"}, "10b-orb-search"),
                             ("read_file", {"path": "~/Documents/plan.md"}, "10c-orb-file"),
                             ("menu", {"path": "Go > Applications"}, "10d-orb-menu")):
        p.set_state("working", activity_phrase(name, args))
        p.activity_start(name, args)
        time.sleep(0.9)
        _shot(shot)
        time.sleep(0.5)
        p.activity_end(name, True)
        time.sleep(1.4)

    p.set_state("speaking")
    _say(p, "assistant_said",
         "Done. Slack is open on on-call support, and I typed your reply in the thread.", gap=0.09)
    time.sleep(0.1)
    _shot("11-speaking-words")
    time.sleep(0.8)

    p.progress(0, 3, "Step 1/3: read the calendar")
    time.sleep(0.6)
    p.progress(2, 3, "Step 3/3: export the PDF")
    time.sleep(0.6)
    _shot("12-progress")
    p.progress(3, 3, "✓ task complete")
    p.celebrate()
    time.sleep(0.25)
    _shot("13-celebrate")
    time.sleep(1.5)
    p.progress(0, 0)

    p.set_state("working")
    p.activity_start("click_text", {"text": "Send"})
    time.sleep(0.5)
    _say(p, "user_said", "stop", gap=0.05)
    p.stopped()
    time.sleep(0.3)
    _shot("14-stopped")
    time.sleep(1.5)

    p.set_state("awake")
    p.fire("console")
    time.sleep(0.9)
    _shot("15-chat")
    time.sleep(1.0)
    p.fire("console")
    time.sleep(0.5)

    p.set_state("sleeping")
    time.sleep(0.6)
    _shot("16-asleep")
    time.sleep(2.5)
    if quit_after:
        ui.quit_app()


def main() -> int:
    presence = ui.Presence(hands_free=True, show_hud=True)
    presence.on("quit", ui.quit_app)

    def build():
        presence.build()
        threading.Thread(target=run, args=(presence,), daemon=True).start()

    ui.run_cocoa(build)
    return 0
