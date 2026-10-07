"""Grounding benchmark: which way of finding a control actually finds it?

Run through the app, so it has Accessibility and Screen Recording:

    open -n -W ~/Applications/Mint.app --args --tool ground_bench '{"mode": "eval", "case": "chatgpt"}'

Nothing is clicked. For each query in tests/cases/<case>.json whose `state`
matches what is on screen, every grounder picks a control and is scored
ScreenSpot-style: a hit only if the ONE point it would click lies inside the
real element's box (found by exact role + label in the accessibility tree, or
by its stable id - `expect.id` - for the fixture app). Refusal items
(`expect.absent: true`, "not on screen") are right only when the grounder
abstains; answering anything is wrong.

Modes:
    snapshot  save the marked screenshot and the element list of the front window
    eval      score the grounders on the queries for the current state
    capture   save an offline snapshot (inventory JSON + window screenshot) of an app's window
              into bench/snapshots/, for `replay`
    tree      the raw accessibility tree of an app's window (diagnostics; optional plan of waits
              and accessibility switches between reads)
Results go to ~/Library/Logs/Mint/ground/.

Offline, with no desktop (choosers replayed over saved snapshots, the candidate
order shuffled to catch position bias):

    python -m mint.groundbench replay --cases mintfixture --grounders text,jev --order element,shuffled --reps 3

Every report carries a dataset hash (cases + snapshots), so numbers from
different case sets are never compared by mistake, and accuracy per grounder,
per app and per density (candidate-set size ~4 / ~12 / ~24).
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path

from mint.core import config
from mint.screen import ground

OUT = Path.home() / "Library" / "Logs" / "Mint" / "ground"
CASES = config.PROJECT_ROOT / "bench" / "cases"
SNAPSHOTS = config.PROJECT_ROOT / "bench" / "snapshots"
# MintFixture (bench/fixtures/MintFixture) writes every visible control's screen box here.
FIXTURE_STATE = Path.home() / "Library" / "Caches" / "MintFixture" / "state.json"


# --- scoring (pure) ------------------------------------------------------------------------------

def _inside(point, box) -> bool:
    x, y, w, h = box
    return x - 1 <= point[0] <= x + w + 1 and y - 1 <= point[1] <= y + h + 1


def is_refusal(item: dict) -> bool:
    return bool((item.get("expect") or {}).get("absent"))


def score(point, truth: list, refusal: bool = False) -> dict:
    """ScreenSpot-style: one point, inside the target's box. For a refusal item the only right
    answer is no point at all."""
    answered = point is not None
    if refusal:
        return {"hit": not answered, "answered": answered, "outcome": "wrong" if answered else "abstain_ok"}
    hit = answered and any(_inside(point, b) for b in truth)
    return {"hit": hit, "answered": answered, "outcome": "hit" if hit else ("wrong" if answered else "abstain")}


def density_bucket(n: int | None) -> str:
    """Candidate-set size, bucketed like cua's jev-use measurements."""
    if not n:
        return "?"
    return "~4" if n <= 8 else "~12" if n <= 18 else "~24"


def aggregate(results: list[dict], keys: tuple = ("grounder",)) -> list[dict]:
    """Accuracy per group (e.g. grounder x app x density), with refusal items counted apart."""
    groups: dict[tuple, dict] = {}
    for r in results:
        if r.get("error"):
            continue
        key = tuple(r.get(k, "") for k in keys)
        g = groups.setdefault(key, {"n": 0, "hits": 0, "refusal_n": 0, "refusal_ok": 0, "wrong": 0, "abstain": 0,
                                    "seconds": 0.0})
        g["seconds"] += r.get("seconds", 0.0)
        if r.get("refusal"):
            g["refusal_n"] += 1
            g["refusal_ok"] += int(r["hit"])
        else:
            g["n"] += 1
            g["hits"] += int(r["hit"])
        g["wrong"] += int(r.get("outcome") == "wrong")
        g["abstain"] += int(r.get("outcome") == "abstain")
    out = []
    for key, g in sorted(groups.items()):
        total = g["n"] + g["refusal_n"]
        out.append({**dict(zip(keys, key)), **g,
                    "accuracy": round(g["hits"] / g["n"], 3) if g["n"] else None,
                    "refusal_accuracy": round(g["refusal_ok"] / g["refusal_n"], 3) if g["refusal_n"] else None,
                    "mean_seconds": round(g["seconds"] / total, 2) if total else 0.0})
    return out


def dataset_hash(items: list[dict], snapshots: dict | None = None) -> str:
    """sha256 over the case items (and the snapshots they are scored on), canonical JSON."""
    blob = json.dumps(items, sort_keys=True, ensure_ascii=False)
    for name in sorted(snapshots or {}):
        blob += name + json.dumps(snapshots[name].get("elements", []), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def table(rows: list[dict], keys: tuple) -> list[str]:
    lines = []
    for g in rows:
        name = " ".join(f"{g[k]}" for k in keys)
        acc = f"{g['hits']}/{g['n']} hits" if g["n"] else "-"
        ref = f"  refusals {g['refusal_ok']}/{g['refusal_n']}" if g["refusal_n"] else ""
        lines.append(f"  {name:52s} {acc:12s}{ref}  wrong {g['wrong']} abstain {g['abstain']}  mean {g['mean_seconds']:.2f}s")
    return lines


# --- the truth ------------------------------------------------------------------------------------

def _expected(inv, expect: dict) -> list[dict]:
    label = expect.get("label", "").lower()
    roles = set(expect.get("roles", []))
    contains = expect.get("contains", False)
    found = []
    for e in inv["elements"]:
        if expect.get("id"):
            if e.get("ax_id") == expect["id"]:
                found.append(e)
            continue
        text = e["label"].lower()
        if (text == label or (contains and label in text)) and (not roles or e["role"] in roles):
            if expect.get("in_dialog") is not None and e["in_dialog"] != expect["in_dialog"]:
                continue
            found.append(e)
    return found


def truth_boxes(inv: dict, expect: dict, frames: dict | None = None) -> list:
    """Boxes the click must land in. By id: the app's own frame for it (the fixture writes them),
    else an element carrying that accessibility identifier."""
    if expect.get("absent"):
        return []
    if expect.get("id") and frames and expect["id"] in frames:
        return [tuple(frames[expect["id"]])]
    return [tuple(e["box"]) for e in _expected(inv, expect)]


def _fixture_frames(app_name: str) -> dict:
    if app_name != "MintFixture":
        return {}
    try:
        return json.loads(FIXTURE_STATE.read_text()).get("frames") or {}
    except (OSError, ValueError):
        return {}


def _front(args: dict) -> str:
    """Bring the named app forward and check it is really in front."""
    name = args.get("app")
    if not name:
        return ""
    import AppKit
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


_KEEP = ("id", "role", "subrole", "kind", "label", "value", "hint", "box", "enabled", "focused", "interactive",
         "in_dialog", "within", "depth", "hidden", "in_web_content", "help")


def serialize(inv: dict) -> dict:
    """An inventory as plain JSON (no AX references), with each element's AXIdentifier when it has one."""
    from mint.screen import axkit
    elements = []
    for e in inv["elements"]:
        row = {k: (list(e[k]) if k == "box" else e[k]) for k in _KEEP if k in e}
        ref = e.get("ref")
        if ref is not None:
            ax_id = axkit.attr(ref, "AXIdentifier")
            if ax_id:
                row["ax_id"] = str(ax_id)
        elements.append(row)
    return {"source": "ax", "app": inv.get("app", ""), "title": inv.get("title", ""),
            "window": list(inv["window"]) if inv.get("window") else None,
            "dialog": list(inv["dialog"]) if inv.get("dialog") else None,
            "dialog_title": inv.get("dialog_title", ""), "dialog_soft": inv.get("dialog_soft", False),
            "walked": inv.get("walked"), "elements": elements}


def capture(args: dict) -> str:
    """Save an offline snapshot of an app's window: bench/snapshots/<name>.json + .png. The app need
    not be in front (nothing is clicked, nothing is brought forward)."""
    import subprocess

    from mint.screen import axkit
    name = args.get("name") or "snapshot"
    app = ground.running_app(args["app"]) if args.get("app") else ground.front_app()
    if app is None:
        return f"{args.get('app')} is not running"
    inv = ground.inventory(app)
    inv = ground.settle_pending(inv) or inv       # a web view's page arrives on first asking
    if not inv.get("window"):
        return f"{inv.get('app')}: no window to capture"
    snap = serialize(inv)
    snap.update(name=name, density=args.get("density"), scene=args.get("scene", ""),
                captured=time.strftime("%Y-%m-%d %H:%M:%S"))
    frames = _fixture_frames(snap["app"])
    if frames:
        snap["frames"] = frames
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    png = SNAPSHOTS / f"{name}.png"
    window_id = inv.get("window_id")
    if window_id is None and inv["elements"]:
        window_id = axkit.window_id(axkit.focused_window(inv["pid"])) if hasattr(axkit, "window_id") else None
    if window_id:
        # the window alone, even when something lies on top of it
        subprocess.run(["screencapture", "-x", "-o", f"-l{window_id}", str(png)], check=False, timeout=15)
    else:
        image, _ = ground.screenshot(inv["window"])
        image.save(png)
    snap["image"] = png.name
    (SNAPSHOTS / f"{name}.json").write_text(json.dumps(snap, indent=1))
    return f"saved {SNAPSHOTS / name}.json: {len(snap['elements'])} elements of {snap['app']} '{snap['title'][:40]}'"


_TREE_ATTRS = ("AXRole", "AXSubrole", "AXRoleDescription", "AXTitle", "AXDescription", "AXValue", "AXHelp",
               "AXPlaceholderValue", "AXIdentifier", "AXDOMIdentifier", "AXDOMClassList", "AXEnabled", "AXFocused")


def _tree_pass(window, limit: int = 1500, depth: int = 40) -> list[dict]:
    """One read of the raw accessibility tree under `window` (breadth first): every node's own
    attributes, actions, frame and child count - what an inventory is built from, unfiltered."""
    import collections

    import ApplicationServices as AX

    from mint.screen import axkit
    rows, queue = [], collections.deque([(window, 0, -1)])
    while queue and len(rows) < limit:
        node, level, parent = queue.popleft()
        axkit.bound(node)
        row = {"n": len(rows), "parent": parent, "depth": level}
        for name in _TREE_ATTRS:
            value = axkit.attr(node, name)
            if value is None:
                continue
            row[name.removeprefix("AX")] = value if isinstance(value, (bool, int, float)) else str(value)[:120]
        box = axkit.frame(node)
        if box:
            row["box"] = [round(c, 1) for c in box]
        try:
            err, names = AX.AXUIElementCopyActionNames(node, None)
            row["actions"] = [str(a) for a in names or []] if err == 0 else f"err {err}"
        except Exception as e:
            row["actions"] = f"raised {type(e).__name__}"
        kids = list(axkit.attr(node, "AXChildren") or [])
        row["children"] = len(kids)
        rows.append(row)
        if level < depth:
            queue.extend((k, level + 1, row["n"]) for k in kids)
    return rows


def tree(args: dict) -> str:
    """Diagnostics: the raw accessibility tree of an app's window, read without bringing it forward,
    one or more times (`plan`: "dump", "wait:<seconds>", "set:<AXAttribute>:app|web" - an
    accessibility switch on the app or on each AXWebArea's host - then dump again). Nothing is
    clicked. Written to ~/Library/Logs/Mint/ground/tree-<name>.json."""
    import ApplicationServices as AX

    from mint.screen import axkit
    app = ground.running_app(args.get("app", ""))
    if app is None:
        return f"{args.get('app')} is not running"
    pid = app.processIdentifier()
    window = axkit.focused_window(pid)
    if window is None:
        return f"{args.get('app')}: no window"
    passes, notes = [], []
    for step in args.get("plan") or ["dump"]:
        kind, _, rest = str(step).partition(":")
        if kind == "wait":
            time.sleep(min(float(rest or 1), 10.0))
        elif kind == "set":
            name, _, where = rest.partition(":")
            targets = [AX.AXUIElementCreateApplication(pid)] if where != "web" else [
                n for n in axkit.walk(window, limit=1500) if axkit.attr(n, "AXRole") == "AXWebArea"]
            for t in targets:
                notes.append(f"set {name} on {where or 'app'}: {AX.AXUIElementSetAttributeValue(t, name, True)}")
        elif kind == "dump":
            started = time.monotonic()
            rows = _tree_pass(window)
            passes.append({"at": step, "took": round(time.monotonic() - started, 3), "nodes": rows})
            notes.append(f"dump {len(passes)}: {len(rows)} nodes, roles "
                         + ", ".join(sorted({r.get('Role', '?') for r in rows})))
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"tree-{args.get('name') or 'snapshot'}.json"
    path.write_text(json.dumps({"app": app.localizedName(), "pid": pid, "notes": notes, "passes": passes},
                               indent=1, default=str))
    return f"saved {path}: " + "; ".join(notes)


def _pick(g: str, q: dict, pool: list[dict], inv: dict, live: bool = True):
    """(element or None, point or None, why) from one grounder."""
    pick, why, point = None, "", None
    if g == "text":
        pick, why = ground.choose_by_text(q["target"], pool, inv.get("window"))
    elif g == "jev":
        pick, why = ground.choose_by_jev(q["target"], pool, inv)
    elif g == "text+jev":
        pick, why = ground.choose_by_text(q["target"], pool, inv.get("window"))
        if pick is None:
            pick, why = ground.choose_by_jev(q["target"], pool, inv)
    elif g.startswith("vision:"):
        pick, why = ground.choose_by_vision(q["target"], pool, inv, model=g.split(":", 1)[1])
    elif g.startswith("point:"):
        point, why = ground.point_by_vision(q["target"], inv["dialog"] or inv["window"], model=g.split(":", 1)[1])
    elif g == "full" and live:
        pick, _, why = ground.ground(q["target"], q.get("action", "click"), inv)
    else:
        why = f"grounder {g} is not available here"
    if pick is not None:
        point = ground._center(pick)
    return pick, point, why


def evaluate(args: dict) -> str:
    OUT.mkdir(parents=True, exist_ok=True)
    case = args.get("case", "chatgpt")
    raw = (CASES / f"{case}.json").read_text()
    queries = json.loads(raw)
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
    frames = _fixture_frames(inv.get("app", ""))
    results = []
    for q in queries:
        if state and q.get("state") != state:
            continue
        refusal = is_refusal(q)
        truth = truth_boxes(inv, q["expect"], frames)
        if not truth and not refusal:
            results.append({"query": q["target"], "error": "expected element not on screen"})
            continue
        action = q.get("action", "click")
        pool = ground.candidates(inv, action)
        for g in grounders:
            t0 = time.monotonic()
            try:
                pick, point, why = _pick(g, q, pool, inv)
            except Exception as error:
                pick, point, why = None, None, f"error: {str(error)[:100]}"
            took = time.monotonic() - t0
            results.append({"query": q["target"], "grounder": g, "app": inv.get("app", ""), "refusal": refusal,
                            "density": density_bucket(len(pool)), "candidates": len(pool),
                            **score(point, truth, refusal),
                            "picked": (ground.describe(pick, inv["window"]) if pick else
                                       (f"point {tuple(round(c) for c in point)}" if point else None)),
                            "why": why[:160], "seconds": round(took, 2)})
    digest = dataset_hash(queries)
    (OUT / f"{case}-{state or 'all'}-results.json").write_text(
        json.dumps({"dataset_hash": digest, "results": results}, indent=1))
    lines = [f"{inv['app']} '{inv['title'][:40]}' state={state} dialog={inv['dialog_title'] or bool(inv['dialog'])}"
             f" inventory {inv_time:.2f}s ({inv['walked']} nodes, {len(inv['elements'])} kept) dataset {digest}"]
    lines += table(aggregate(results, ("grounder",)), ("grounder",))
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


# --- offline replay ---------------------------------------------------------------------------

def load_snapshot(path: Path) -> dict:
    snap = json.loads(Path(path).read_text())
    snap.setdefault("name", Path(path).stem)
    return snap


def snapshot_inventory(snap: dict, order: str = "element", seed: str = "") -> dict:
    """A saved snapshot as an inventory the choosers accept (no AX references). order="shuffled"
    permutes the elements and their numbers (seeded), so a chooser that leans on position shows it."""
    elements = []
    for e in snap.get("elements", []):
        row = {"role": "", "subrole": "", "kind": "", "label": "", "value": "", "hint": "", "enabled": True,
               "focused": False, "interactive": True, "in_dialog": False, "within": "", "depth": 0, **e}
        row["box"] = tuple(row["box"])
        row["ref"] = None
        row["dialog"] = None
        elements.append(row)
    if order == "shuffled":
        random.Random(seed or snap.get("name", "")).shuffle(elements)
    for i, e in enumerate(elements, 1):
        e["id"] = i
    return {"app": snap.get("app", ""), "pid": None, "title": snap.get("title", ""),
            "window": tuple(snap["window"]) if snap.get("window") else None, "window_id": None,
            "dialog": tuple(snap["dialog"]) if snap.get("dialog") else None,
            "dialog_title": snap.get("dialog_title", ""), "dialog_soft": snap.get("dialog_soft", False),
            "elements": elements, "walked": len(elements), "offscreen": []}


class _SavedScreen:
    """ground.screenshot (and capture.area) answered from a snapshot's saved window picture,
    so a vision chooser replays without a screen."""

    def __init__(self, snap: dict, folder: Path):
        import PIL.Image
        self.image = PIL.Image.open(folder / snap["image"]).convert("RGB")
        self.window = snap["window"]
        self.saved: list = []

    def grab(self, area, *_, **__):
        x, y, w, _h = self.window
        scale = self.image.size[0] / max(w, 1)
        ax_, ay, aw, ah = area
        box = (int((ax_ - x) * scale), int((ay - y) * scale), int((ax_ - x + aw) * scale), int((ay - y + ah) * scale))
        return self.image.crop(box), scale

    def __enter__(self):
        self.saved.append((ground, "screenshot", ground.screenshot))
        ground.screenshot = self.grab
        try:
            from mint.screen import capture
            self.saved.append((capture, "area", capture.area))

            def area(a, *rest, **kw):
                image, scale = self.grab(a)
                return {"image": image, "scale": scale}
            capture.area = area
        except ImportError:
            pass
        return self

    def __exit__(self, *exc):
        for module, name, fn in self.saved:
            setattr(module, name, fn)
        return False


def replay(cases: str = "mintfixture", grounders=("text",), orders=("element",), reps: int = 1,
           folder: Path = SNAPSHOTS) -> tuple[str, dict]:
    """Score choosers over saved snapshots: no desktop, no Accessibility, nothing clicked."""
    items = json.loads((CASES / f"{cases}.json").read_text())
    snaps = {}
    for name in sorted({q["snapshot"] for q in items if q.get("snapshot")}):
        path = folder / f"{name}.json"
        if path.exists():
            snaps[name] = load_snapshot(path)
    digest = dataset_hash(items, snaps)
    results = []
    for rep in range(reps):
        for order in orders:
            for q in items:
                snap = snaps.get(q.get("snapshot", ""))
                if snap is None:
                    results.append({"query": q["target"], "error": f"no snapshot '{q.get('snapshot')}'"})
                    continue
                inv = snapshot_inventory(snap, order, seed=f"{snap['name']}:{rep}")
                refusal = is_refusal(q)
                truth = truth_boxes(inv, q["expect"], snap.get("frames"))
                if not truth and not refusal:
                    results.append({"query": q["target"], "error": "expected element not in the snapshot"})
                    continue
                pool = ground.candidates(inv, q.get("action", "click"))
                for g in grounders:
                    t0 = time.monotonic()
                    try:
                        if g.startswith(("vision:", "point:")) and not snap.get("image"):
                            raise RuntimeError("snapshot has no picture")
                        if g.startswith(("vision:", "point:")):
                            with _SavedScreen(snap, folder):
                                pick, point, why = _pick(g, q, pool, inv, live=False)
                        else:
                            pick, point, why = _pick(g, q, pool, inv, live=False)
                    except Exception as error:
                        pick, point, why = None, None, f"error: {str(error)[:100]}"
                    results.append({"query": q["target"], "grounder": g, "app": snap.get("app", ""),
                                    "snapshot": snap["name"], "order": order, "rep": rep, "refusal": refusal,
                                    "density": density_bucket(len(pool)), "candidates": len(pool),
                                    **score(point, truth, refusal),
                                    "picked": (f"{pick['kind']} '{pick['label'][:40]}'" if pick else
                                               (f"point {tuple(round(c) for c in point)}" if point else None)),
                                    "why": why[:160], "seconds": round(time.monotonic() - t0, 2)})
    report = {"cases": cases, "dataset_hash": digest, "grounders": list(grounders), "orders": list(orders),
              "reps": reps, "snapshots": sorted(snaps), "results": results,
              "by_grounder": aggregate(results, ("grounder",)),
              "by_app": aggregate(results, ("grounder", "app")),
              "by_density": aggregate(results, ("grounder", "density")),
              "by_order": aggregate(results, ("grounder", "order"))}
    lines = [f"replay {cases}: {len(items)} items over {len(snaps)} snapshots, dataset {digest}, "
             f"orders {','.join(orders)}, reps {reps}"]
    for title, keys in (("grounder", ("grounder",)), ("app", ("grounder", "app")),
                        ("density", ("grounder", "density")), ("order", ("grounder", "order"))):
        lines.append(f" by {title}:")
        lines += table(report[f"by_{title}"], keys)
    wrong = [r for r in results if r.get("outcome") == "wrong"]
    for r in wrong[:10]:
        lines.append(f"  WRONG [{r['grounder']}/{r['order']}] {r['query']!r} in {r['snapshot']} -> {r['picked']}")
    missing = [r for r in results if r.get("error")]
    for r in missing[:5]:
        lines.append(f"  SKIPPED {r['query']!r}: {r['error']}")
    return "\n".join(lines), report


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
    if mode == "capture":
        return capture(args)
    if mode == "tree":
        return tree(args)
    if mode == "replay":
        text, _ = replay(args.get("case", "mintfixture"), args.get("grounders") or ["text"],
                         args.get("orders") or ["element"], int(args.get("reps", 1)))
        return text
    return f"unknown mode {mode}"


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python -m mint.groundbench", description="Offline grounding replay.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="score choosers over saved snapshots (bench/snapshots)")
    r.add_argument("--cases", default="mintfixture")
    r.add_argument("--grounders", default="text", help="comma list: text, jev, text+jev, vision:<model>")
    r.add_argument("--order", default="element", help="comma list: element, shuffled")
    r.add_argument("--reps", type=int, default=1)
    r.add_argument("--snapshots", type=Path, default=SNAPSHOTS)
    r.add_argument("--out", type=Path, help="write the full report (JSON) here")
    h = sub.add_parser("hash", help="print the dataset hash of a case file and its snapshots")
    h.add_argument("--cases", default="mintfixture")
    h.add_argument("--snapshots", type=Path, default=SNAPSHOTS)
    args = parser.parse_args(argv)
    if args.cmd == "hash":
        items = json.loads((CASES / f"{args.cases}.json").read_text())
        snaps = {q["snapshot"]: load_snapshot(args.snapshots / f"{q['snapshot']}.json") for q in items
                 if q.get("snapshot") and (args.snapshots / f"{q['snapshot']}.json").exists()}
        print(dataset_hash(items, snaps))
        return 0
    text, report = replay(args.cases, [g for g in args.grounders.split(",") if g],
                          [o for o in args.order.split(",") if o], args.reps, args.snapshots)
    print(text)
    out = args.out or OUT / f"replay-{args.cases}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str))
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
