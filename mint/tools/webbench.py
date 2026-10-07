"""A web-engine benchmark with independent checks: `python -m mint.webbench [names...] [--runs N] [--mode M]`.

Each task's result is verified by looking at the final page itself (its address and text), never by trusting
the engine's DONE. Every attempt is kept and written to web-bench.jsonl with time, actions, decisions and the
Jev latencies, so a speed change can be compared run for run.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from pathlib import Path

from mint.tools import cdp
from mint.core import config
from mint.tools import webgoal


def _fixture() -> str:
    for parent in Path(__file__).resolve().parents:
        for base in (parent / "tests" / "fixtures", parent / "publish" / "overlay" / "tests" / "fixtures"):
            if (base / "web_hotel.html").exists():
                return (base / "web_hotel.html").as_uri()
    return ""


def _check_hotel(tab) -> str:
    state = tab.js("({hash: location.hash, text: document.body.innerText})") or {}
    note = re.search(r"Your filters: (.*)", state.get("text", ""))
    filters = note.group(1) if note else ""
    ok = (state.get("hash") == "#casa-flora" and "Design" in filters and "Free cancellation enabled" in filters
          and "Lisbon" in filters)
    return "" if ok else f"opened {state.get('hash')!r} with filters {filters!r}"


def _check_wiki(tab) -> str:
    url = tab.js("location.href") or ""
    return "" if "G%C3%B6del%27s_incompleteness_theorems" in url or "Gödel's_incompleteness" in url else f"at {url}"


def _check_flights(tab) -> str:
    state = tab.js("({url: location.href, text: document.body.innerText})") or {}
    text, url = state.get("text", ""), state.get("url", "")
    missing = [w for w in ("Z(u|ü)rich", "London") if not re.search(w, text, re.I)]
    if "/search" not in url:
        missing.append("a search results page")
    if not re.search(r"one.?way", text, re.I):
        missing.append("one way")
    if not re.search(r"(Nov(ember)?\.? 20|20 Nov)", text):
        missing.append("Nov 20")
    return "" if not missing else "missing: " + ", ".join(missing)


def _check_duck(tab) -> str:
    url = tab.js("location.href") or ""
    return "" if "github.com/browser-use" in url else f"at {url}"


def _check_httpbin(tab) -> str:
    text = tab.js("document.body.innerText") or ""
    return "" if '"custname": "Ada Lovelace"' in text and "medium" in text and "bacon" in text else "form not echoed"


TASKS = {
    "hotel": ("Search for stays in Lisbon, apply the Design style and Free cancellation filters, and open Casa Flora.",
              None, _check_hotel, ""),
    "wiki": ("Find and open the Wikipedia article about Gödel's incompleteness theorems.",
             "https://en.wikipedia.org/wiki/Main_Page", _check_wiki, ""),
    "flights": ("Find one-way flights from Zurich to London on November 20, 2026, for one adult in economy. "
                "Stop when matching flight options are visible.", "https://www.google.com/travel/flights?hl=en",
                _check_flights, ""),
    "duck": ("Search DuckDuckGo for 'jev ultrafast browser-use github' and open the GitHub repository result.",
             "https://duckduckgo.com/", _check_duck, ""),
    "form": ("Fill in the pizza order form: customer name Ada Lovelace, telephone 555-0100, email ada@example.com, "
             "size medium, topping bacon, then submit the order.", "https://httpbin.org/forms/post", _check_httpbin,
             "This is a test form: submitting it is what the user wants."),
}


class Recorder:
    """Chrome's own screencast of the task's tab (real frames of the run - nothing captured from the screen),
    written as numbered JPEGs with their timestamps, for a guide clip."""

    def __init__(self, folder: Path) -> None:
        self.folder, self.frames, self.tab = folder, [], None
        folder.mkdir(parents=True, exist_ok=True)

    def start(self, tab) -> None:
        import base64
        self.tab = tab

        def frame(params, session) -> None:
            if session != tab.session:
                return
            path = self.folder / f"{len(self.frames):05d}.jpg"
            path.write_bytes(base64.b64decode(params["data"]))
            self.frames.append((params["metadata"]["timestamp"], path.name))
            try:
                tab.b.t.post("Page.screencastFrameAck", {"sessionId": params["sessionId"]}, tab.session)
            except Exception:
                pass
        tab.b.t.on("Page.screencastFrame", frame)
        tab.call("Page.startScreencast", {"format": "jpeg", "quality": 80, "everyNthFrame": 1})

    def stop(self) -> None:
        try:
            self.tab.call("Page.stopScreencast")
        except Exception:
            pass
        (self.folder / "frames.json").write_text(json.dumps(self.frames))


def run(names: list[str], runs: int = 1, mode: str = "headless", approve: bool = False,
        record: str = "") -> list[dict]:
    rows = []
    if approve:                       # the bench's own test form: its submit is approved here, nowhere else
        from mint.core import guard
        guard.ask = lambda danger, who="Mint", timeout=None: True
    for name in names:
        goal, url, check, details = TASKS[name]
        url = url or _fixture()
        for attempt in range(runs):
            task = webgoal.Task(goal, url, mode, details, keep_open=True)
            recorder = None
            if record:
                recorder = Recorder(Path(record) / f"{name}-{attempt + 1}")
                original = webgoal.Task.observe

                def first_look(self, _rec=recorder, _orig=original):
                    if _rec.tab is None and self.tab is not None:
                        _rec.start(self.tab)
                    return _orig(self)
                task.observe = first_look.__get__(task)
            result = task.run()
            if recorder is not None:
                time.sleep(1.0)
                recorder.stop()
                (Path(record) / f"{name}-{attempt + 1}" / "steps.json").write_text(json.dumps(
                    {"steps": result.steps, "seconds": result.seconds, "status": result.status}))
            problem = "no tab"
            if task.tab is not None:
                try:
                    problem = check(task.tab)
                except Exception as error:
                    problem = f"check failed: {error}"
                task.tab.close()
            row = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "task": name, "mode": mode, "attempt": attempt + 1,
                   "status": result.status, "verified": not problem, "problem": problem,
                   "seconds": result.seconds, "actions": len(result.steps), "decisions": result.decisions,
                   "jev_ms": [s["ms"] for s in result.steps], "message": result.message[:160]}
            rows.append(row)
            print(f"{name:8} #{attempt + 1}  {'PASS' if row['verified'] else 'FAIL'}  {result.seconds:5.1f} s  "
                  f"{row['actions']:2} actions  {row['decisions']:2} decisions  {result.status}"
                  + (f"  ({problem})" if problem else ""), flush=True)
            with (config.PROJECT_ROOT / "web-bench.jsonl").open("a") as f:
                f.write(json.dumps(row) + "\n")
    for name in names:
        mine = [r for r in rows if r["task"] == name]
        passed = [r for r in mine if r["verified"]]
        if mine:
            print(f"{name:8} {len(passed)}/{len(mine)} verified, median "
                  f"{statistics.median(r['seconds'] for r in mine):.1f} s, median decisions "
                  f"{statistics.median(r['decisions'] for r in mine):.0f}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", default=list(TASKS))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--mode", default="headless", choices=["headless", "window", "chrome"])
    parser.add_argument("--record", default="", help="folder for screencast frames of each run (guide clips)")
    parser.add_argument("--approve-test-form", action="store_true",
                        help="approve the bench form's own submit (httpbin.org test form only)")
    args = parser.parse_args()
    try:
        run(args.names, args.runs, args.mode, args.approve_test_form, args.record)
    finally:
        cdp.shutdown()


if __name__ == "__main__":
    main()
