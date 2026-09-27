"""Grounding benchmark: which way of finding a control actually finds it?

Run through the app, so it has Accessibility and Screen Recording:

    open -n -W ~/Applications/Mint.app --args --tool ground_bench '{"mode": "eval", "case": "chatgpt"}'

Nothing is clicked. For each query in tests/cases/<case>.json whose `state`
matches what is on screen, every grounder picks a control and is scored
against the real element (found by exact role + label in the accessibility
tree): a hit if the point it would click lies inside that element's box.

Modes:
    snapshot  save the marked screenshot and the element list of the front window
    eval      score the grounders on the queries for the current state
Results go to ~/Library/Logs/Mint/ground/.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from mint.core import config
from mint.screen import ground

OUT = Path.home() / "Library" / "Logs" / "Mint" / "ground"
CASES = config.PROJECT_ROOT / "bench" / "cases"


def _inside(point, box) -> bool:
    x, y, w, h = box
    return x - 1 <= point[0] <= x + w + 1 and y - 1 <= point[1] <= y + h + 1


def _expected(inv, expect: dict) -> list[dict]:
    label = expect.get("label", "").lower()
    roles = set(expect.get("roles", []))
    contains = expect.get("contains", False)
    found = []
    for e in inv["elements"]:
        text = e["label"].lower()
        if (text == label or (contains and label in text)) and (not roles or e["role"] in roles):
            if expect.get("in_dialog") is not None and e["in_dialog"] != expect["in_dialog"]:
                continue
            found.append(e)
    return found


def _front(args: dict) -> str:
    """Bring the named app forward and check it is really in front."""
    name = args.get("app")
    if not name:
        return ""
    import AppKit
    from mint.screen import axkit
    app = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                if (a.localizedName() or "").lower() == name.lower()), None)
    if app is None:
        return f"{name} is not running"
    ok = ground.bring_forward(app)
    time.sleep(0.4)
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return "" if ok and front and front.processIdentifier() == app.processIdentifier() else \
        f"could not bring {name} to the front (front is {front.localizedName() if front else '?'})"


def snapshot(args: dict) -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    problem = _front(args)
    if problem:
        return problem
    name = args.get("name", "snapshot")
    inv = ground.inventory()
    pool = ground.candidates(inv, "click")
    image, area = ground.marked(inv, pool, area=inv["window"])
    image.save(OUT / f"{name}-marked.png")
    rows = [ground.describe(e, inv["window"]) for e in inv["elements"]]
    rows += [f"OFFSCREEN {o['role']} '{o['label']}' box={tuple(round(c) for c in o['box'])}" for o in inv.get("offscreen", [])]
    (OUT / f"{name}-elements.txt").write_text(
        f"{inv['app']} - {inv['title']} - dialog: {inv['dialog_title'] if inv['dialog'] else 'none'}"
        f" - walked {inv['walked']}\n" + "\n".join(rows) + "\n")
    return (f"{inv['app']} '{inv['title'][:50]}': walked {inv['walked']} nodes, kept {len(inv['elements'])} "
            f"elements, {len(pool)} clickable; dialog: {inv['dialog_title'] or bool(inv['dialog'])} box={inv['dialog']} window={inv['window']}. "
            f"Saved {OUT / (name + '-marked.png')}")


def evaluate(args: dict) -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    case = args.get("case", "chatgpt")
    queries = json.loads((CASES / f"{case}.json").read_text())
    state = args.get("state")
    problem = _front(args)
    if problem:
        return problem
    grounders = args.get("grounders") or ["text", "jev", "text+jev", "vision:gemini-3.5-flash-lite",
                                          "vision:gemini-3.5-flash", "vision:gemini-robotics-er-2-preview",
                                          "point:gemini-robotics-er-2-preview", "full"]
    started = time.monotonic()
    inv = ground.inventory()
    inv_time = time.monotonic() - started
    results, table = [], {g: [0, 0, 0.0] for g in grounders}
    for q in queries:
        if state and q.get("state") != state:
            continue
        truth = _expected(inv, q["expect"])
        if not truth:
            results.append({"query": q["target"], "error": "expected element not on screen"})
            continue
        action = q.get("action", "click")
        pool = ground.candidates(inv, action)
        for g in grounders:
            t0 = time.monotonic()
            pick, why, point = None, "", None
            try:
                if g == "text":
                    pick, why = ground.choose_by_text(q["target"], pool)
                elif g == "jev":
                    pick, why = ground.choose_by_jev(q["target"], pool, inv)
                elif g == "text+jev":
                    pick, why = ground.choose_by_text(q["target"], pool)
                    if pick is None:
                        pick, why = ground.choose_by_jev(q["target"], pool, inv)
                elif g.startswith("vision:"):
                    pick, why = ground.choose_by_vision(q["target"], pool, inv, model=g.split(":", 1)[1])
                elif g.startswith("point:"):
                    point, why = ground.point_by_vision(q["target"], inv["dialog"] or inv["window"],
                                                        model=g.split(":", 1)[1])
                elif g == "full":
                    pick, _, why = ground.ground(q["target"], action, inv)
            except Exception as error:
                why = f"error: {str(error)[:100]}"
            took = time.monotonic() - t0
            if pick is not None:
                point = ground._center(pick)
            hit = point is not None and any(_inside(point, e["box"]) for e in truth)
            answered = point is not None
            table[g][0] += int(hit)
            table[g][1] += 1
            table[g][2] += took
            results.append({"query": q["target"], "grounder": g, "hit": hit, "answered": answered,
                            "picked": (ground.describe(pick, inv["window"]) if pick else
                                       (f"point {tuple(round(c) for c in point)}" if point else None)),
                            "why": why[:160], "seconds": round(took, 2)})
    (OUT / f"{case}-{state or 'all'}-results.json").write_text(json.dumps(results, indent=1))
    lines = [f"{inv['app']} '{inv['title'][:40]}' state={state} dialog={inv['dialog_title'] or bool(inv['dialog'])}"
             f" inventory {inv_time:.2f}s ({inv['walked']} nodes, {len(inv['elements'])} kept)"]
    for g, (hits, n, secs) in table.items():
        if n:
            lines.append(f"  {g:38s} {hits}/{n} hits   mean {secs / n:.2f}s")
    misses = [r for r in results if r.get("hit") is False and r.get("grounder") in ("full", "text+jev")]
    for r in misses[:12]:
        lines.append(f"  MISS [{r['grounder']}] {r['query']!r} -> {r['picked']} ({r['why'][:90]})")
    errors = [r for r in results if r.get("error")]
    for r in errors:
        lines.append(f"  NOT ON SCREEN: {r['query']!r}")
    return "\n".join(lines)


def front_test(args: dict) -> str:
    """Which way of bringing an app forward does macOS honour from Mint?"""
    import subprocess

    import AppKit
    import ApplicationServices as AX
    name, method = args.get("app", "ChatGPT"), args.get("method", "nsapp")
    ws = AppKit.NSWorkspace.sharedWorkspace()
    app = next((a for a in ws.runningApplications() if (a.localizedName() or "") == name), None)
    before = ws.frontmostApplication().localizedName()
    if method == "nsapp":
        app.activateWithOptions_(AppKit.NSApplicationActivateAllWindows)
    elif method == "ax":
        el = AX.AXUIElementCreateApplication(app.processIdentifier())
        AX.AXUIElementSetAttributeValue(el, "AXFrontmost", True)
    elif method == "applescript":
        script = AppKit.NSAppleScript.alloc().initWithSource_(f'tell application "{name}" to activate')
        script.executeAndReturnError_(None)
    elif method == "osascript":
        subprocess.run(["osascript", "-e", f'tell application "{name}" to activate'], check=False)
    elif method == "open":
        subprocess.run(["open", "-a", name], check=False)
    elif method == "dock":
        dock = next(a for a in ws.runningApplications() if a.bundleIdentifier() == "com.apple.dock")
        from mint.screen import axkit
        root = AX.AXUIElementCreateApplication(dock.processIdentifier())
        for node in axkit.walk(root, limit=400):
            if axkit.attr(node, "AXRole") == "AXDockItem" and axkit.attr(node, "AXTitle") == name:
                AX.AXUIElementPerformAction(node, "AXPress")
                break
    time.sleep(0.8)
    return f"{method}: before={before} after={ws.frontmostApplication().localizedName()}"


def act_step(args: dict) -> str:
    """One real action through ground.act, then a snapshot of what is on screen."""
    problem = _front(args)
    if problem:
        return problem
    import os
    os.environ["MINT_DEBUG"] = "1"
    result = ground.act(args.get("action", "click"), args.get("target", ""), args.get("text", ""),
                        bool(args.get("press_return", False)))
    time.sleep(0.5)
    shot = snapshot({"name": args.get("name", "step")})
    return f"{result}\n  after: {shot}"


def windows(args: dict) -> str:
    """List an app's windows; with fix=true, leave full screen and close floating panels."""
    import AppKit
    import ApplicationServices as AX
    from mint.screen import axkit
    name = args.get("app", "Finder")
    app = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                if (a.localizedName() or "") == name), None)
    if app is None:
        return f"{name} is not running"
    lines = []
    for w in axkit.attr(AX.AXUIElementCreateApplication(app.processIdentifier()), "AXWindows") or []:
        title, full, sub = axkit.attr(w, "AXTitle"), axkit.attr(w, "AXFullScreen"), axkit.attr(w, "AXSubrole")
        lines.append(f"'{title}' fullscreen={full} subrole={sub}")
        if args.get("fix"):
            if full:
                AX.AXUIElementSetAttributeValue(w, "AXFullScreen", False)
                lines.append("  -> left full screen")
            if sub in ("AXFloatingWindow", "AXSystemFloatingWindow"):
                close = axkit.attr(w, "AXCloseButton")
                if close is not None:
                    AX.AXUIElementPerformAction(close, "AXPress")
                    lines.append("  -> closed it")
    return "\n".join(lines) or "no windows"


def run(args: dict) -> str:
    mode = args.get("mode", "snapshot")
    if mode == "windows":
        return windows(args)
    if mode == "act":
        return act_step(args)
    if mode == "front":
        return front_test(args)
    if mode == "snapshot":
        return snapshot(args)
    if mode == "eval":
        return evaluate(args)
    return f"unknown mode {mode}"
