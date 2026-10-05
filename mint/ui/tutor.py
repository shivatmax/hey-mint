"""Tutor mode: Mint teaches a task on screen instead of doing it.

    "show me how to export a PDF in Preview"
    "teach me how to add a filter in Google Sheets"
    "walk me through turning on dark mode"

Mint makes a short plan (2-10 steps), then for each step it points at the exact
control - an arrow or box with the instruction beside it, the orb flying over
next to it - says what to do, and WAITS until the user has done it before
showing the next step. Mint never clicks or types during a lesson; the user does.

The plan
    1. A saved skill (skillbook) for the task, when one clearly fits (word match,
       no Jev: only Gemini is used for AI here) - its steps are handed to Gemini
       to rewrite as things the user does.
    2. Otherwise Gemini (flash-lite, then flash) from the task, the app, the
       app's real menu bar (titles and items, with shortcuts) and a compact list
       of the controls visible in its front window. Real names keep the plan
       grounded in what is on screen, not in what the model remembers.

Finding each step's control (never clicking it)
    menu / menu_item  the app's menu bar through Accessibility. For a menu
                      item whose menu is closed, the menu is pointed at first
                      ("Open File, then Export as PDF…"); the arrow moves to the
                      item as soon as the menu opens.
    app               the app's Dock icon.
    anything else     ground.inventory + choose_by_text (labels and roles),
                      then text recognition (pointer.match), then a Gemini
                      vision model picking a numbered box (ground.choose_by_vision),
                      then Gemini pointing (ground.point_by_vision).
    If it still cannot be found, Gemini repairs the plan once or twice with a
    fresh look at the screen (usually a missing prerequisite step, like opening
    a menu or a tab), and failing that the instruction is shown on its own.

Knowing the step was done (polled every 0.3 s)
    - a mouse-down inside the target (the system's event counters sampled
      every 40 ms with the pointer position - no event tap, no extra permission);
    - or what the step expected happened: the menu opened, a sheet or dialog or
      window appeared, the window title changed, the field got text, the app
      came to the front;
    - or the user says "next" / "done" (tool action next).
    After 2 minutes without progress the control is found again and the hint
    repeated; after five such hints the lesson ends by itself.

Research (September 2026; ideas only, no code copied - all MIT, GPL-3.0 compatible):
    farzaa/clicky (MIT) - the "buddy next to the cursor": the model answers with
        a [POINT:x,y:label] tag and a cursor overlay flies there. One answer
        per question; no step tracking.
    abdallahmagdy15/mudriknow (MIT) - "Auto-Guide": accessibility tree plus a
        gridded screenshot, one guide_step at a time, re-captured after each.
        Its mouse-hook click detection was turned off after racing bugs, so the
        user taps "done"; here clicks are sampled passively instead, and "next"
        stays as the fallback.
    Nishant8677/ClickTutor_AI (MIT) - text anchors located by OCR, with a
        bounded repair loop when an anchor is not found (borrowed: _repair).
    vaibhav0806/kairo-tutor (MIT) - guides with voice, highlight and a
        companion cursor, never clicking for the user (same rule here).
"""

from __future__ import annotations

import logging
import re
import threading
import time

log = logging.getLogger("mint.ui.tutor")

POLL = 0.3                  # completion checks
CLICK_POLL = 0.04           # mouse-down sampling
STEP_TIMEOUT = 120.0        # then re-point with a hint
MAX_HINTS = 5               # then the lesson ends by itself
MAX_REPAIRS = 2             # per step
KINDS = ("app", "menu", "menu_item", "button", "tab", "checkbox", "field", "text", "shortcut", "any")
_TYPING = re.compile(r"\b(type|enter|write|name it|fill|paste)\b", re.I)

_lock = threading.RLock()
_lesson: dict | None = None
_gen = 0                    # bumped whenever the shown step changes or the lesson ends (or is cancelled)
_planning: tuple | None = None      # (ticket, task) while start() is planning
_levels: list = []          # (overlay window, its own level) raised for a lesson - main thread only


# --- small helpers ------------------------------------------------------------------------

def _notify(text: str) -> None:
    """Tell the live assistant (it says the next instruction)."""
    try:
        from mint.tools.work import _notify_mint
        _notify_mint(text)
    except Exception:
        log.debug("could not notify Mint", exc_info=True)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[…\.]{1,3}$|&|[\"“”'‘’]", "", str(text or "")).lower().split())


def _inflate(rect, pad: float = 8.0):
    x, y, w, h = rect
    return (x - pad, y - pad, w + 2 * pad, h + 2 * pad)


def _contains(rect, point) -> bool:
    if rect is None or point is None:
        return False
    x, y, w, h = _inflate(rect)
    return x <= point[0] <= x + w and y <= point[1] <= y + h


def _moved(a, b, tol: float = 4.0) -> bool:
    if a is None or b is None:
        return a is not b
    return any(abs(p - q) > tol for p, q in zip(a, b))


def _ax():
    import ApplicationServices as AX
    return AX


def _main_display() -> tuple[float, float, float, float]:
    """The main display in Quartz points (top-left origin). CGDisplayBounds is safe on any
    thread; NSScreen belongs to the main thread and lessons find controls on worker threads."""
    import Quartz
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return (float(b.origin.x), float(b.origin.y), float(b.size.width), float(b.size.height))


def _attr(element, name):
    from mint.screen.axkit import attr
    return attr(element, name) if element is not None else None


def _frame(element):
    from mint.screen.axkit import frame
    return frame(element) if element is not None else None


# --- clicks and keys (no event tap needed) -------------------------------------------------

class _Input:
    """Samples the system's mouse-down and key-down counters. The counters and the
    pointer position are readable without Input Monitoring; a 40 ms sample puts a
    click within a few points of where it happened."""

    def __init__(self) -> None:
        self.clicks: list[tuple[float, float, float]] = []    # (time, x, y), newest last
        self.last_key = 0.0
        self.keys = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True, name="tutor-input")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, stop: threading.Event) -> None:
        import Quartz
        state = Quartz.kCGEventSourceStateCombinedSessionState
        kinds = (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown, Quartz.kCGEventOtherMouseDown)

        def mouse():
            return sum(Quartz.CGEventSourceCounterForEventType(state, k) for k in kinds)

        def keys():
            return Quartz.CGEventSourceCounterForEventType(state, Quartz.kCGEventKeyDown)

        last_mouse, last_keys = mouse(), keys()
        while not stop.wait(CLICK_POLL):
            try:
                m, k = mouse(), keys()
                if m != last_mouse:
                    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
                    self.clicks.append((time.monotonic(), float(p.x), float(p.y)))
                    del self.clicks[:-20]
                if k != last_keys:
                    self.keys += max(1, k - last_keys)
                    self.last_key = time.monotonic()
                last_mouse, last_keys = m, k
            except Exception:
                log.debug("input sample failed", exc_info=True)

    def clicks_since(self, t: float) -> list[tuple[float, float]]:
        return [(x, y) for when, x, y in list(self.clicks) if when >= t]


_input = _Input()


# --- the app and what it offers ------------------------------------------------------------

def _running(name: str):
    import AppKit
    want = (name or "").lower()
    return next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                 if (a.localizedName() or "").lower() == want), None)


def _front_app():
    """The app the user is working in: the front one, never Mint itself."""
    import AppKit
    import Quartz

    from mint.screen import ground
    app = ground.front_app()
    if app is not None and not ground.is_own(app):
        return app
    try:
        from mint.tools import extra as extra_tools
        target = extra_tools._target.get("app")
        if target is not None and not target.isTerminated():
            return target
    except Exception:
        pass
    own = ground.own_app().processIdentifier()
    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, 0) or []
    for window in info:                                   # front to back
        pid = window.get("kCGWindowOwnerPID")
        if window.get("kCGWindowLayer") == 0 and pid and pid != own:
            found = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if found is not None:
                return found
    return app


def _menu_tops(pid: int) -> list:
    """Top menu-bar items, the Apple menu first."""
    AX = _ax()
    bar = _attr(AX.AXUIElementCreateApplication(pid), "AXMenuBar")
    return [t for t in (_attr(bar, "AXChildren") or []) if _attr(t, "AXTitle")]


def _menu_outline(pid: int, budget: int = 3500) -> str:
    """'File: New ⌘N, Open… ⌘O, Export › …' for every menu - the real command names."""
    from mint.tools.harness import _menu_children, _shortcut
    lines, used = [], 0
    for top in _menu_tops(pid)[1:]:
        items = []
        for item in _menu_children(top)[:40]:
            title = _attr(item, "AXTitle") or ""
            if not title:
                continue
            keys = _shortcut(item)
            sub = " ›" if _menu_children(item) else ""
            items.append(f"{title}{' ' + keys if keys else ''}{sub}")
        line = f"{_attr(top, 'AXTitle')}: " + ", ".join(items)
        if used + len(line) > budget:
            line = line[:max(0, budget - used)]
        lines.append(line)
        used += len(line)
        if used >= budget:
            break
    return "\n".join(lines)


def _controls(app, limit: int = 70) -> tuple[str, str]:
    """(window title, compact list of the visible controls of the app's front window)."""
    from mint.screen import ground
    try:
        inv = ground.inventory(app, limit=3000)
    except Exception as error:
        return "", f"(could not read the window: {error})"
    pool = ground.candidates(inv, "click")
    rows = [ground.describe(e, inv["window"]) for e in pool[:limit]]
    head = f"a dialog is open: '{inv['dialog_title']}'\n" if inv["dialog"] else ""
    return inv.get("title", ""), head + "\n".join(rows)


def context(app=None) -> dict:
    """What Gemini is told about the screen when planning or repairing."""
    from mint.tools.fastinput import has_accessibility
    app = app or _front_app()
    ctx = {"app": app.localizedName() if app is not None else "", "title": "", "menus": "", "controls": "",
           "ax": has_accessibility()}
    if app is None or not ctx["ax"]:
        return ctx
    try:
        ctx["menus"] = _menu_outline(app.processIdentifier())
    except Exception:
        log.debug("menu outline failed", exc_info=True)
    ctx["title"], ctx["controls"] = _controls(app)
    return ctx


# --- the plan -------------------------------------------------------------------------------

_RULES = """Rules for the steps:
- Each step is ONE physical action on ONE control: open the app, open a menu, choose a menu item,
  click a button/tab/checkbox/option, type into a field, or press a keyboard shortcut.
- Use the real names from the lists above, spelled exactly (keep "…").
- A menu-bar command is two steps: open the menu (kind "menu", target = the menu's title, e.g. "File"),
  then choose the item (kind "menu_item", target = the item, path = "File > Export as PDF…").
  A submenu adds one more menu_item step (path "File > Export > PDF"). The Apple menu's title is "Apple".
- If the app is not open, the first step opens it (kind "app", target = the app's name).
- A Mac-wide setting (dark mode, Wi-Fi, wallpaper, sound, notifications…) is taught in System Settings,
  not in the app in front, unless the task names that app.
- A pop-up menu or dropdown in a window is kind "button"; its choice is kind "any".
- "say": what the user does, at most 12 words, friendly, imperative ("Click the File menu at the top").
- "target": the exact visible label of the control ("" only for shortcut steps).
- "kind": one of app, menu, menu_item, button, tab, checkbox, field, text, shortcut, any.
- "keys": for shortcut steps, like "⌘⇧S"; otherwise "".
- "expect": what visibly happens when it is done, at most 8 words ("the File menu opens").
- 2 to 10 steps; stop when the task is done. The user does every step themselves."""


def _skill_text(task: str, app_name: str) -> tuple[str, str]:
    """(skill name, its steps) for a saved skill that clearly fits - word match only."""
    try:
        from mint.knowledge import skills as skillbook
        skills = skillbook.all_skills()
        if not skills:
            return "", ""
        # Only a skill for this app (or for no particular app): a word match alone put a
        # Google Docs skill on "export a PDF in Preview" in testing.
        mine = skillbook.for_app(app_name) if app_name else []
        general = [s for s in skills if not s["meta"].get("apps", "").strip() and s["category"] != "browser"
                   and not s["category"].startswith(("apps/", "coding/"))]
        best, _ = skillbook._lexical(task, mine) if mine else (None, "")
        if best is None:
            best, _ = skillbook._lexical(task, general) if general else (None, "")
        if best is None or skillbook.app_conflict(best, task, app_name, skills):
            return "", ""
        return best["name"], f"{best['title']}\n{best['body'][:1500]}"
    except Exception:
        log.debug("skill lookup failed", exc_info=True)
        return "", ""


def _race(prompt: str, valid, wait: float = 35.0) -> tuple[object, str]:
    """Ask two fast models at once (a third after a few seconds) and take the first valid
    JSON answer: on this key one flash-lite model answers in 2 s while another takes 15-40 s,
    and which one is slow changes by the hour. -> (answer, model) or (None, why)."""
    import queue

    from mint.core import llm
    lite = [m for m in llm.LITE if "lite" in m]
    first = [m for m in ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite") if m in lite] or lite[:2]
    later = [m for m in llm.LITE if m not in first][:2]
    answers: queue.Queue = queue.Queue()

    def ask(model: str) -> None:
        try:
            text, used = llm.generate(prompt, [model], json_mode=True)
            answers.put((llm.parse_json(text), used))
        except Exception as error:
            answers.put((None, f"{model}: {str(error)[:80]}"))

    for model in first:
        threading.Thread(target=ask, args=(model,), daemon=True, name="tutor-ask").start()
    pending, started, errors, launched = len(first), time.monotonic(), [], False
    while pending and time.monotonic() - started < wait:
        if not launched and time.monotonic() - started > 6.0:
            launched = True
            for model in later:
                threading.Thread(target=ask, args=(model,), daemon=True, name="tutor-ask").start()
            pending += len(later)
        try:
            answer, used = answers.get(timeout=0.25)
        except queue.Empty:
            continue
        pending -= 1
        if answer is not None and valid(answer):
            return answer, used
        errors.append(str(used) if answer is None else f"{used}: unusable answer")
        if not pending and not launched:
            launched = True
            for model in later:
                threading.Thread(target=ask, args=(model,), daemon=True, name="tutor-ask").start()
            pending += len(later)
    return None, "; ".join(errors) or f"no answer in {wait:.0f}s"


def _clean_steps(raw) -> list[dict]:
    steps = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "any").strip().lower().replace(" ", "_").replace("-", "_")
        kind = kind if kind in KINDS else "any"
        step = {"say": " ".join(str(item.get("say") or "").split())[:140],
                "target": " ".join(str(item.get("target") or "").split())[:80],
                "kind": kind,
                "path": " ".join(str(item.get("path") or "").split())[:120],
                "keys": str(item.get("keys") or "").strip()[:20],
                "expect": " ".join(str(item.get("expect") or "").split())[:80]}
        if kind == "menu_item" and not step["path"] and step["target"]:
            step["path"] = step["target"]
        if not step["say"]:
            continue
        steps.append(step)
    return steps[:10]


def plan(task: str, app_name: str = "", ctx: dict | None = None) -> dict:
    """-> {"steps": [...], "app": name, "source": how, "intro": text}. No UI."""
    ctx = ctx if ctx is not None else context()
    front = ctx.get("app", "")
    skill_name, skill = _skill_text(task, app_name or front)
    running = bool(app_name) and _running(app_name) is not None
    if app_name:
        where = f"App: {app_name}" + (" (open, in front)" if app_name == front else
                                      (" (open, not in front)" if running else " (NOT open yet)"))
    else:
        where = (f"App: not named. The app in front is {front or 'unknown'}: teach it there unless the task "
                 "is about another app or a Mac-wide setting.")
    parts = [
        "You are Mint, a friendly Mac tutor. The user wants to LEARN to do a task themselves. You will "
        "point at one control at a time on their screen and wait until they have used it.",
        f"Task: {task}", where]
    if front and app_name and front != app_name:
        parts.append(f"The app in front now is {front}.")
    app_name = app_name or front
    if ctx.get("title"):
        parts.append(f"Its window: '{ctx['title']}'")
    if ctx.get("menus") and app_name == front:
        parts.append(f"Menu bar of {front} (menu: items, with shortcuts; › = submenu):\n{ctx['menus']}")
    if ctx.get("controls") and app_name == front:
        parts.append(f"Controls visible in the front window:\n{ctx['controls']}")
    if skill:
        parts.append("A saved how-to for this task (written as tool calls for an assistant; turn it into "
                     f"things the user does):\n{skill}")
    parts.append(_RULES)
    parts.append('Answer only JSON: {"app": "<app name>", "intro": "<one short sentence to say first>", '
                 '"steps": [{"say": "", "target": "", "kind": "", "path": "", "keys": "", "expect": ""}]}')
    prompt = "\n\n".join(parts)
    started = time.monotonic()
    answer, model = _race(prompt, lambda a: bool(_clean_steps((a if isinstance(a, dict) else {}).get("steps"))))
    if answer is None:
        return {"steps": [], "app": app_name, "source": f"Gemini failed: {model}", "intro": ""}
    steps = _clean_steps(answer.get("steps"))
    source = f"{model} in {time.monotonic() - started:.1f}s" + (f", from saved skill '{skill_name}'" if skill else "")
    return {"steps": steps, "app": str(answer.get("app") or app_name), "source": source,
            "intro": " ".join(str(answer.get("intro") or "").split())[:160]}


def _repair(lesson: dict, step: dict, why: str) -> dict | None:
    """The step's control is not on screen: one fresh look, and Gemini says what the user
    must do now - usually a missing prerequisite (open a menu, a tab, a panel)."""
    ctx = context(_running(lesson["app"]) or _front_app())
    listing = "\n".join(f"{i + 1}. {s['say']} [{s['kind']}: {s['target']}]" for i, s in enumerate(lesson["steps"]))
    prompt = "\n\n".join(p for p in [
        "You are Mint, a Mac tutor guiding the user one control at a time. The control for the current "
        "step is NOT on screen right now.",
        f"Task: {lesson['task']}\nPlan:\n{listing}\nCurrent step: {lesson['index'] + 1} - {step['say']} "
        f"(looking for {step['kind']} '{step['target']}'; {why})",
        f"App in front: {ctx['app']}; window '{ctx['title']}'",
        f"Menu bar:\n{ctx['menus']}" if ctx["menus"] else "",
        f"Controls visible now:\n{ctx['controls']}" if ctx["controls"] else "",
        "What should the user do NOW? Either a step to insert before the current one (a prerequisite, "
        "e.g. open the tab or menu that holds it), or a replacement for the current step using the real "
        "name of the control that is on screen, or skip if the step is already done.",
        _RULES,
        'Answer only JSON: {"action": "insert|replace|skip", "step": {"say": "", "target": "", "kind": "", '
        '"path": "", "keys": "", "expect": ""}}'] if p)
    answer, _ = _race(prompt, lambda a: isinstance(a, dict) and bool(a.get("action")), wait=20.0)
    if not isinstance(answer, dict):
        return None
    action = str(answer.get("action") or "").lower()
    fixed = _clean_steps([answer.get("step")]) if action in ("insert", "replace") else []
    if action == "skip":
        return {"action": "skip"}
    if not fixed:
        return None
    return {"action": action, "step": fixed[0]}


# --- finding a step's control (no clicking) ------------------------------------------------

def _hit_is(element) -> bool:
    """Is `element` really drawn where it says (an open menu's item, not a closed one)?"""
    from mint.screen.axkit import element_at
    box = _frame(element)
    if not box or box[2] < 3 or box[3] < 3:
        return False
    # never the raw system-wide hit test: on Mint's own arrow or card it is answered in process,
    # off the main thread (the teach crash of 1 Oct)
    hit = element_at(box[0] + box[2] / 2, box[1] + box[3] / 2)
    if hit is None:
        return False
    role, title = _attr(element, "AXRole"), _attr(element, "AXTitle")
    node = hit
    for _ in range(4):
        if node is None:
            break
        if _attr(node, "AXRole") == role and _attr(node, "AXTitle") == title and not _moved(_frame(node), box, 2):
            return True
        node = _attr(node, "AXParent")
    return False


def _menu_open(top) -> bool:
    if _attr(top, "AXSelected"):
        return True
    from mint.tools.harness import _menu_children
    first = next((i for i in _menu_children(top) if _attr(i, "AXTitle")), None)
    return first is not None and _hit_is(first)


def _match(items: list, want: str):
    from mint.tools.harness import _match as match
    if _norm(want) == "apple" and items and _attr(items[0], "AXTitle") == "Apple":
        return items[0]
    return match(items, want)


def _menu_chain(pid: int, step: dict) -> list:
    """[top, item, subitem…] along the step's menu path (as far as it resolves)."""
    from mint.tools.harness import _find_anywhere, _menu_children
    tops = _menu_tops(pid)
    if not tops:
        return []
    path = [p.strip() for p in re.split(r"\s*(?:>|›|→|»)\s*", step.get("path") or step["target"]) if p.strip()]
    if step["kind"] == "menu" or len(path) == 1 and _match(tops, path[0]) is not None:
        top = _match(tops, path[0] if path else step["target"])
        return [top] if top is not None else []
    chain = []
    top = _match(tops, path[0]) if len(path) > 1 else None
    if top is not None:
        chain, node = [top], top
        for part in path[1:]:
            node = _match(_menu_children(node), part)
            if node is None:
                break
            chain.append(node)
        if len(chain) == len(path):
            return chain
    # A partial or wrong path: find the item by its title anywhere, then rebuild the chain.
    item, trail = _find_anywhere(tops[1:], path[-1] if path else step["target"])
    if item is None:
        return chain
    chain, node = [], None
    for n, part in enumerate(trail):
        node = _match(tops, part) if n == 0 else _match(_menu_children(node), part)
        if node is None:
            return []
        chain.append(node)
    return chain


def _dock_rect(name: str):
    AX = _ax()
    dock = _running("Dock")
    if dock is None:
        return None, None
    want = _norm(name)
    for lst in _attr(AX.AXUIElementCreateApplication(dock.processIdentifier()), "AXChildren") or []:
        for item in _attr(lst, "AXChildren") or []:
            if _norm(_attr(item, "AXTitle")) == want:
                return _frame(item), item
    return None, None


def locate(step: dict, app=None, allow_vision: bool = True) -> dict:
    """-> {"rect": (x, y, w, h) Quartz points or None, "how": text, "ref": AX element or None,
           "note": text to show}. Reads only; never clicks."""
    from mint.tools.fastinput import has_accessibility
    kind, target = step["kind"], step["target"]
    out = {"rect": None, "how": "", "ref": None, "note": step["say"]}
    app = app or _front_app()
    ax = has_accessibility()

    if kind == "app":
        front = _front_app()
        if front is not None and _norm(front.localizedName()) == _norm(target):
            out["how"] = "already in front"
            return out
        rect, ref = _dock_rect(target) if ax else (None, None)
        out.update(rect=rect, ref=ref, how="Dock icon" if rect else ("not in the Dock" if ax else "no Accessibility"))
        return out

    if kind in ("menu", "menu_item") and ax and app is not None:
        chain = _menu_chain(app.processIdentifier(), step)
        if chain:
            # Point at the deepest part of the path the user can see right now.
            shown = chain[0]
            for node in chain[1:]:
                if not _hit_is(node):
                    break
                shown = node
            out.update(rect=_frame(shown), ref=shown)
            if shown is chain[-1]:
                out["how"] = "menu bar" if len(chain) == 1 else "open menu"
            else:
                first = _attr(shown, "AXTitle") or ""
                rest = " › ".join(_attr(n, "AXTitle") or "" for n in chain[chain.index(shown) + 1:])
                out["how"] = f"menu closed: pointing at '{first}' first"
                out["note"] = f"Open {'the Apple menu' if first == 'Apple' else first}, then choose {rest}"
            return out

    if not target:
        out["how"] = "nothing to point at"
        return out

    from mint.screen import ground
    inv, pool = None, []
    if ax and app is not None:
        try:
            inv = ground.inventory(app, limit=3000)
            pool = ground.candidates(inv, "type" if kind == "field" else "click")
            role_word = {"button": " button", "field": " field", "tab": " tab", "checkbox": " checkbox",
                         "menu_item": " menu"}.get(kind, "")
            hit, why = ground.choose_by_text(f'"{target}"{role_word}', pool, inv["window"])
            if hit is None and role_word:
                hit, why = ground.choose_by_text(f'"{target}"', pool, inv["window"])
            if hit is not None:
                out.update(rect=hit["box"], ref=hit["ref"], how=f"accessibility {why}")
                return out
        except Exception:
            log.debug("inventory failed", exc_info=True)
    # Text recognition: the words themselves, inside the app's window.
    try:
        from mint.screen import pointer
        lines, area = pointer._read(correct=False)
        window = inv["window"] if inv and inv.get("window") else None
        if window:
            scoped = [l for l in lines if pointer._inside(l["box"], {"x": window[0], "y": window[1],
                                                                       "w": window[2], "h": window[3]})]
            lines = scoped or lines
        boxes, how = pointer.match(target, lines, area, use_jev=False)
        if boxes:
            out.update(rect=boxes[0], how=f"text recognition ({how})")
            return out
    except Exception as error:
        log.debug("OCR failed: %s", error)
    if not allow_vision:
        out["how"] = "not found (no vision)"
        return out
    try:
        if inv is not None and pool:
            hit, why = ground.choose_by_vision(f"{target} - for this step: {step['say']}", pool, inv)
            if hit is not None:
                out.update(rect=hit["box"], ref=hit["ref"], how=why)
                return out
        area = (inv or {}).get("window")
        if area is None:
            area = _main_display()
        point, why = ground.point_by_vision(target, area)
        if point is not None:
            out.update(rect=(point[0] - 16, point[1] - 12, 32, 24), how=why)
            return out
        out["how"] = f"not found ({why})"
    except Exception as error:
        out["how"] = f"not found ({str(error)[:80]})"
    return out


# --- drawing (through the marks overlay, on the main thread) --------------------------------

def _draw(gen: int, rect, note: str, window=None, orb: bool = True) -> None:
    """Arrow or box at `rect` with the note where it fits, kept until cleared; the orb flies
    over beside it. With no rect, the note alone near the top of the window. Drawn only while
    `gen` is still the lesson's generation (not after a stop, or once the step changed)."""
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(_draw_main, rect, note, window, gen)
    except Exception:
        log.debug("could not draw", exc_info=True)
        return
    if rect is not None and orb and gen == _gen:
        try:
            from mint.ui.motion import motion
            motion.visit_quartz(rect, stay=20.0)
        except Exception:
            log.debug("orb visit failed", exc_info=True)


def _draw_main(rect, note, window, gen: int) -> None:
    if gen != _gen:
        return                            # queued before a stop or a step change: stale
    import Quartz

    from mint.ui.marks import marks
    marks._clear()
    marks._token += 1                     # no fade is scheduled: the step's mark stays until cleared
    marks._ensure()
    for _, overlay, _, _ in marks._overlays:
        # Menus open at the pop-up menu level, in front of an overlay at that same level: a
        # lesson points INTO open menus, so its marks sit one level higher (still click-through).
        # The overlays are shared: _clear_main puts them back when the lesson ends.
        if overlay.level() < Quartz.kCGOverlayWindowLevel:
            _levels.append((overlay, overlay.level()))
            overlay.setLevel_(Quartz.kCGOverlayWindowLevel)
    _paint(marks, rect, note, window)


def _paint(marks, rect, note, window) -> None:
    import AppKit
    import Quartz

    from mint.ui import gfx
    font = AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightSemibold)
    width = min(AppKit.NSAttributedString.alloc().initWithString_attributes_(
        note, {AppKit.NSFontAttributeName: font}).size().width + 24, 420) if note else 0

    def note_at(root, left: float, bottom: float) -> None:
        # marks._note puts the pill 12 pt above a rect's top, 6 pt left of its x.
        if not note:
            return
        bounds = root.bounds()
        left = max(8.0, min(left, bounds.size.width - width - 8.0))
        bottom = max(8.0, min(bottom, bounds.size.height - 34.0))
        marks._note(root, Quartz.CGRectMake(left + 6, bottom - 12, 1, 0), note, "box")

    if rect is None:
        area = window
        if area is None:
            size = AppKit.NSScreen.screens()[0].frame().size
            area = (0.0, 0.0, float(size.width), float(size.height))
        x, y, w, h = area
        root, local, _ = marks._local(x, y, w, h)
        note_at(root, local.origin.x + local.size.width / 2 - width / 2, local.origin.y + local.size.height - 90)
        return
    x, y, w, h = rect
    root, local, _ = marks._local(x, y, w, h)
    bounds = root.bounds()
    ink = gfx.accent()
    top_room = bounds.size.height - (local.origin.y + local.size.height)     # space above the target
    if local.origin.x > 130 and top_room > 110:
        marks._arrow(root, local, ink)
        # The arrow's tail is 90 pt left and 70 pt above the target's middle: the note sits on it.
        tail_x, tail_y = local.origin.x - 96, local.origin.y + local.size.height / 2 + 70
        note_at(root, tail_x - width / 2, tail_y + 8)
    else:
        marks._box(root, local, ink)
        if top_room > 60:
            note_at(root, local.origin.x - 6, local.origin.y + local.size.height + 12)
        else:                                   # the menu bar: below it
            note_at(root, local.origin.x - 6, local.origin.y - 42)


def _clear() -> None:
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(_clear_main)
    except Exception:
        log.debug("could not clear marks", exc_info=True)


def _clear_main() -> None:
    """Remove the lesson's marks and put the shared overlays back at their own level."""
    try:
        from mint.ui.marks import marks
        marks._clear()
    except Exception:
        log.debug("could not clear marks", exc_info=True)
    while _levels:
        overlay, level = _levels.pop()
        try:
            overlay.setLevel_(level)
        except Exception:
            log.debug("could not restore the overlay level", exc_info=True)


# --- state and completion ------------------------------------------------------------------

def _snapshot(pid: int | None) -> dict:
    """What a finished step usually changes: menus, windows, sheets, focus, titles."""
    AX = _ax()
    front = _front_app()
    snap = {"front": front.localizedName() if front is not None else "", "title": "", "windows": 0,
            "sheet": False, "dialog": False, "menu": "", "focus": "", "value": ""}
    if not pid:
        return snap
    app = AX.AXUIElementCreateApplication(pid)
    window = _attr(app, "AXFocusedWindow")
    snap["title"] = str(_attr(window, "AXTitle") or "")
    snap["windows"] = len(_attr(app, "AXWindows") or [])
    snap["dialog"] = str(_attr(window, "AXSubrole") or "") in ("AXDialog", "AXSystemDialog", "AXFloatingWindow")
    snap["sheet"] = any(_attr(c, "AXRole") == "AXSheet" for c in (_attr(window, "AXChildren") or [])[:60])
    for child in _attr(app, "AXChildren") or []:
        if _attr(child, "AXRole") == "AXMenu":
            snap["menu"] = "context menu"
    if not snap["menu"]:
        for top in _menu_tops(pid):
            if _attr(top, "AXSelected"):
                snap["menu"] = str(_attr(top, "AXTitle") or "")
                break
    focus = _attr(app, "AXFocusedUIElement")
    if focus is not None:
        snap["focus"] = f"{_attr(focus, 'AXRole')}:{_attr(focus, 'AXTitle') or _attr(focus, 'AXDescription') or ''}"
        value = _attr(focus, "AXValue")
        snap["value"] = value[:200] if isinstance(value, str) else ""
    return snap


def _structural(a: dict, b: dict) -> bool:
    return any(a[k] != b[k] for k in ("front", "title", "windows", "sheet", "dialog", "menu"))


def _done(lesson: dict) -> str:
    """Why the shown step counts as done, or ''."""
    step = lesson["steps"][lesson["index"]]
    kind, shown = step["kind"], lesson["shown_at"]
    now = _snapshot(lesson["pid"])
    before = lesson["before"]
    clicks = _input.clicks_since(shown + 0.15)
    rect = lesson["rect"]
    expect = (step.get("expect") or "").lower()
    typing = kind == "field" or (kind in ("any", "text") and bool(_TYPING.search(step["say"])))

    if kind == "app":
        return "the app is in front" if _norm(now["front"]) == _norm(step["target"]) else ""
    # Ahead of the lesson: pointed at a menu, and the user (or a shortcut) already ran the command in
    # it - a sheet, dialog or window appeared. Skip the "choose the item" step too.
    following = lesson["steps"][lesson["index"] + 1] if lesson["index"] + 1 < len(lesson["steps"]) else None
    if kind == "menu" and following is not None and following["kind"] == "menu_item" and not now["menu"] and (
            (now["sheet"] and not before["sheet"]) or (now["dialog"] and not before["dialog"])
            or now["windows"] > before["windows"]):
        return "skip:the command in that menu already ran"
    if typing:
        if _input.keys > lesson["keys_at"] and time.monotonic() - _input.last_key > 2.0 and (
                now["value"] != before["value"] or _structural(before, now)):
            return "the field has the text"
        if kind == "field" and any(_contains(rect, c) for c in clicks) and not _TYPING.search(step["say"]):
            return "clicked the field"
        return ""
    if kind == "menu" and lesson["how"] == "menu bar":
        if now["menu"] and _norm(now["menu"]) == _norm(step["target"]):
            return "the menu is open"
        if lesson["ref"] is not None and _menu_open(lesson["ref"]):
            return "the menu is open"
        if any(_contains(rect, c) for c in clicks) and now["menu"]:
            return "the menu is open"
        return ""
    if kind == "menu_item" and lesson.get("pointing_at_parent"):
        return ""                               # still opening the menu that holds it
    if any(_contains(rect, c) for c in clicks):
        return "clicked it"
    if kind == "shortcut":
        return "the shortcut did its thing" if _structural(before, now) else ""
    if clicks or kind == "menu_item":
        # Clicked slightly off the mark (or used the keyboard) and what was expected happened.
        if ("menu" in expect or "list" in expect) and now["menu"] and not before["menu"]:
            return "a menu opened"
        if any(w in expect for w in ("dialog", "sheet", "window", "panel", "save", "opens", "appears")) and (
                (now["sheet"] and not before["sheet"]) or (now["dialog"] and not before["dialog"])
                or now["windows"] > before["windows"]):
            return "a window or dialog appeared"
        if clicks and ((before["sheet"] and not now["sheet"]) or (before["dialog"] and not now["dialog"])
                       or now["windows"] < before["windows"]):
            return "the dialog closed"               # Save / OK / Done in a sheet
        if kind == "menu_item" and before["menu"] and not now["menu"] and _structural(
                {**before, "menu": ""}, {**now, "menu": ""}):
            return "the menu command ran"
        if clicks and now["title"] != before["title"] and before["title"]:
            return "the window changed"
    return ""


# --- running a lesson ------------------------------------------------------------------------

def _app_of(lesson: dict, step: dict | None = None):
    """The app a step happens in. The menu bar on screen is always the front app's."""
    if step is not None and step["kind"] in ("menu", "menu_item", "app"):
        return _front_app()
    return (_running(lesson["app"]) if lesson.get("app") else None) or _front_app()


def _begin(lesson: dict) -> dict:
    """A new generation for the lesson's current step (the caller holds _lock) -> the view
    _show works from without the lock: its own copy of the plan, so repairs stay private
    until they are applied."""
    global _gen
    _gen += 1
    lesson["gen"] = _gen
    return {"lesson": lesson, "gen": _gen, "task": lesson["task"], "app": lesson["app"],
            "index": lesson["index"], "steps": [dict(s) for s in lesson["steps"]]}


def _find(step: dict, app) -> dict:
    try:
        return locate(step, app)
    except Exception as error:
        log.debug("locate failed", exc_info=True)
        return {"rect": None, "how": f"not found ({str(error)[:80]})", "ref": None, "note": step["say"]}


def _show(view: dict, announce: bool) -> str:
    """Find and mark the view's step. -> the sentence for the assistant; "" when the lesson was
    stopped or moved on meanwhile; a FAILED sentence (and the lesson ends) when it broke."""
    try:
        return _present(view, announce)
    except Exception as error:
        log.exception("tutor could not show a step")
        lesson = view["lesson"]
        with _lock:
            if _lesson is not lesson or _gen != view["gen"]:
                return ""
        _end(lesson)
        text = (f"FAILED: the lesson stopped - step {view['index'] + 1} could not be shown "
                f"({str(error)[:80]}). Explain the rest in words instead.")
        if announce:
            _notify("(Tutor) " + text)
        return text


def _present(view: dict, announce: bool) -> str:
    lesson, gen, steps = view["lesson"], view["gen"], view["steps"]
    # The slow part - Accessibility, text recognition, vision, Gemini repairs - runs WITHOUT
    # the lock, so next/back/stop answer at once; the result is applied only if nothing moved.
    while True:
        step = steps[view["index"]]
        app = _app_of(view, step)
        pid = app.processIdentifier() if app is not None else None
        found = _find(step, app)
        repairs, again = 0, False
        while found["rect"] is None and step["kind"] not in ("shortcut", "app") and repairs < MAX_REPAIRS \
                and step.get("repairs", 0) < MAX_REPAIRS:
            if _gen != gen:
                return ""                           # stopped or moved on: no more Gemini calls
            repairs += 1
            step["repairs"] = step.get("repairs", 0) + 1
            fix = _repair(view, step, found["how"])
            if fix is None:
                break
            if fix["action"] == "skip":
                if view["index"] + 1 < len(steps):
                    view["index"] += 1
                    again = True
                break
            new = fix["step"]
            new["repairs"] = MAX_REPAIRS if fix["action"] == "insert" else step["repairs"]
            if fix["action"] == "insert":
                steps.insert(view["index"], new)
            else:
                steps[view["index"]] = new
            del steps[12:]
            step = new
            app = _app_of(view, step)
            pid = app.processIdentifier() if app is not None else None
            found = _find(step, app)
        if again:
            continue
        if step["kind"] == "app" and found["how"] == "already in front" and view["index"] + 1 < len(steps):
            view["index"] += 1
            continue
        break
    before = _snapshot(pid)
    n, total = view["index"] + 1, len(steps)
    note = f"{n}/{total} · {found['note']}"
    if step["kind"] == "shortcut" and step.get("keys"):
        note += f"  ({step['keys']})"
    window = None
    if found["rect"] is None:
        try:
            from mint.screen import ocr
            w = ocr._front_window()
            window = (w["x"], w["y"], w["w"], w["h"]) if w and w["h"] > 100 else None
        except Exception:
            pass
    with _lock:
        if _lesson is not lesson or _gen != gen:
            return ""                               # stopped, or next/back moved on meanwhile
        lesson.update(steps=steps, index=view["index"], pid=pid, rect=found["rect"], ref=found["ref"],
                      how=found["how"], shown_at=time.monotonic(), before=before, keys_at=_input.keys,
                      pointing_at_parent=found["how"].startswith("menu closed"), last_track=time.monotonic(),
                      shown=gen)
    _draw(gen, found["rect"], note, window)
    where = "" if found["rect"] is not None else " (Mint could not find the control on screen, so only the " \
                                                 "instruction is shown - describe where it usually is)"
    text = (f"(Tutor) Step {n} of {total} is on screen: '{found['note']}'"
            + (f" - shortcut {step['keys']}" if step.get("keys") else "") + f"{where}. "
            "Say it to the user in one short sentence, then wait; Mint moves on by itself when they do it. "
            "If they say 'next', 'done' or 'skip' -> tutor action=next; 'go back' -> tutor action=back; "
            "'stop the lesson' -> tutor action=stop.")
    log.info("tutor step %d/%d %s -> %s", n, total, step, found["how"])
    if announce:
        _notify(text)
    return text


def _end(lesson: dict) -> bool:
    """End `lesson` if it is still the running one: marks cleared, overlays restored."""
    global _lesson, _gen
    with _lock:
        if _lesson is not lesson:
            return False
        _lesson = None
        _gen += 1
    _clear()
    _input.stop()
    return True


def _finish(lesson: dict, announce: bool) -> str:
    _end(lesson)
    text = (f"(Tutor) All {len(lesson['steps'])} steps are done - '{lesson['task']}' is finished. "
            "Tell the user in one short, warm sentence.")
    if announce:
        _notify(text)
    return text


def _go(delta: int, announce: bool, lesson: dict | None = None, gen: int | None = None) -> str:
    """Move `delta` steps. With `lesson`/`gen` (the watcher), only if nothing changed since."""
    with _lock:
        current = _lesson
        if current is None:
            return "No lesson is running. Start one with tutor action=start."
        if lesson is not None and (current is not lesson or _gen != gen):
            return ""
        index = max(0, current["index"] + delta)
        if index >= len(current["steps"]):
            return _finish(current, announce)
        current["index"] = index
        current["hints"] = 0
        view = _begin(current)
    return _show(view, announce) or "(Tutor) The lesson changed meanwhile. " + status()


def _track(lesson: dict, gen: int) -> None:
    """Keep the mark on the control: windows move, menus open under a 'menu closed' pointer.
    Called without the lock; applies what it found only if the step is unchanged."""
    now = time.monotonic()
    step = lesson["steps"][lesson["index"]]
    every = 0.5 if step["kind"] in ("menu", "menu_item") else 1.0
    if now - lesson["last_track"] < every:
        return
    lesson["last_track"] = now
    if step["kind"] == "menu_item":
        found = locate(step, _app_of(lesson, step), allow_vision=False)
        parent = found["how"].startswith("menu closed")
        with _lock:
            if _lesson is not lesson or _gen != gen:
                return
            if found["rect"] is None or not (_moved(found["rect"], lesson["rect"])
                                             or parent != lesson["pointing_at_parent"]):
                return
            orb = parent != lesson["pointing_at_parent"]
            lesson.update(rect=found["rect"], ref=found["ref"], pointing_at_parent=parent)
            n, total = lesson["index"] + 1, len(lesson["steps"])
        _draw(gen, found["rect"], f"{n}/{total} · {found['note']}", orb=orb)
        return
    ref = lesson["ref"]
    if ref is not None:
        box = _frame(ref)
        if box is not None and box[2] > 2 and _moved(box, lesson["rect"]):
            with _lock:
                if _lesson is not lesson or _gen != gen:
                    return
                lesson["rect"] = box
                n, total = lesson["index"] + 1, len(lesson["steps"])
                note = f"{n}/{total} · {lesson['steps'][lesson['index']]['say']}"
            _draw(gen, box, note, orb=False)


def _watch(lesson: dict) -> None:
    # The lock is held only to read the generation and to apply results: reading the screen
    # (and finding a control again, which can take a minute with repairs) happens without it.
    while True:
        time.sleep(POLL)
        with _lock:
            if _lesson is not lesson:
                return
            gen = _gen
            if lesson.get("shown") != gen:
                continue                             # the step is still being found
        try:
            why = _done(lesson)
            if why:
                time.sleep(0.35)                     # let the app draw what the click opened
                log.info("tutor step %d done: %s", lesson["index"] + 1, why)
                _go(+2 if why.startswith("skip:") else +1, announce=True, lesson=lesson, gen=gen)
                continue
            _track(lesson, gen)
            if time.monotonic() - lesson["shown_at"] > STEP_TIMEOUT:
                with _lock:
                    if _lesson is not lesson or _gen != gen:
                        continue
                    lesson["hints"] = lesson.get("hints", 0) + 1
                    if lesson["hints"] > MAX_HINTS:
                        _finish(lesson, announce=False)
                        _notify("(Tutor) The lesson stopped after 10 minutes without progress. Say so briefly.")
                        return
                    step = lesson["steps"][lesson["index"]]
                    view = _begin(lesson)
                if _show(view, announce=False).startswith("(Tutor)"):
                    _notify(f"(Tutor) The user has not done step {view['index'] + 1} yet: '{step['say']}'. "
                            "It is pointed at again. Give one short, friendly hint about where it is.")
        except Exception:
            log.exception("tutor watch failed")


# --- public API ------------------------------------------------------------------------------

def start(task: str, app: str = "") -> str:
    """Plan the lesson and show step 1. -> what the assistant should say."""
    global _lesson, _planning
    task = " ".join(str(task or "").split())
    if not task:
        return "FAILED: say what to teach, e.g. 'export a PDF in Preview'."
    from mint.core import config  # noqa: F401 - loads the Gemini key
    from mint.tools.fastinput import has_accessibility
    if not has_accessibility():
        return ("FAILED: Mint lacks Accessibility permission, so it cannot see the app's controls to point "
                "at them. Tell the user to allow it in System Settings > Privacy & Security > Accessibility.")
    stop(quiet=True)
    with _lock:
        ticket = _gen                    # a stop (or another start) while planning changes it
        _planning = (ticket, task)
    try:
        from mint.tools import appfinder
        from mint.screen import axkit
        named, note = "", ""
        if app:
            name, _ = appfinder.resolve(app)
            if name:
                named = name
                running = _running(name)
                if running is not None:
                    axkit.bring_to_front(running)
                    time.sleep(0.35)
            else:
                # "in Google Sheets": a website, used in the browser in front.
                note = f"'{app}' is not an app on this Mac, so it is taught in the app in front"
                task = f"{task} (in {app}, a website)"
        front = _front_app()
        front_name = front.localizedName() if front is not None else ""
        made = plan(task, named, context(front))
        steps = made["steps"]
        if _gen != ticket:
            return f"Stopped: the lesson '{task}' was cancelled while it was being planned."
        if not steps:
            return f"FAILED: could not make a plan for '{task}' ({made['source']}). Explain it in words instead."
        lesson_app = named
        if not lesson_app and not note and made["app"] and _norm(made["app"]) != _norm(front_name):
            lesson_app = appfinder.resolve(made["app"])[0] or ""      # "dark mode" -> System Settings
        lesson_app = lesson_app or front_name
        opens_it = any(s["kind"] == "app" or _norm(lesson_app) in _norm(s["target"]) for s in steps[:3])
        if _norm(lesson_app) != _norm(front_name) and not opens_it:
            steps.insert(0, {"say": f"Open {lesson_app}", "target": lesson_app, "kind": "app", "path": "",
                             "keys": "", "expect": f"{lesson_app} comes to the front"})
        with _lock:
            if _gen != ticket:
                return f"Stopped: the lesson '{task}' was cancelled while it was being planned."
            _planning = None
            lesson = _lesson = {"task": task, "app": lesson_app, "steps": steps, "index": 0, "rect": None,
                                "ref": None, "how": "", "shown_at": time.monotonic(), "pid": None, "hints": 0,
                                "source": made["source"], "started": time.time()}
            view = _begin(lesson)
    finally:
        with _lock:
            if _planning is not None and _planning[0] == ticket:
                _planning = None
    _input.start()
    threading.Thread(target=_watch, args=(lesson,), daemon=True, name="tutor-watch").start()
    said = _show(view, announce=False)
    if said.startswith("FAILED"):
        return said
    if not said:
        if not active():
            return f"The lesson '{task}' was stopped before step 1 was shown."
        said = "(Tutor) " + status()
    intro = f"{made['intro']} " if made.get("intro") else ""
    outline = "; ".join(f"{i + 1}. {s['say']}" for i, s in enumerate(steps))
    return (f"Lesson started ({len(steps)} steps, plan by {made['source']}"
            + (f"; {note}" if note else "") + f"): {outline}\n"
            f"Say first: {intro}then step 1. " + said.removeprefix("(Tutor) "))


def next_step() -> str:
    return _go(+1, announce=False)


def back() -> str:
    return _go(-1, announce=False)


def stop(quiet: bool = False) -> str:
    global _lesson, _gen, _planning
    with _lock:                              # never held during slow work, so this is quick
        lesson, _lesson = _lesson, None
        planning, _planning = _planning, None
        _gen += 1                            # queued draws, a finding watcher and a planning start see it
    _input.stop()
    if lesson is None:
        if planning is not None and not quiet:
            return f"Cancelled the lesson '{planning[1]}' while it was being planned."
        return "" if quiet else "No lesson was running."
    _clear()
    return f"Stopped the lesson '{lesson['task']}' at step {lesson['index'] + 1} of {len(lesson['steps'])}."


def status() -> str:
    lesson = _lesson
    if lesson is None:
        return "No lesson is running."
    step = lesson["steps"][lesson["index"]]
    waited = int(time.monotonic() - lesson["shown_at"])
    rest = "; ".join(f"{i + 1}. {s['say']}" for i, s in enumerate(lesson["steps"]))
    return (f"Teaching '{lesson['task']}' in {lesson['app']}: step {lesson['index'] + 1} of "
            f"{len(lesson['steps'])} - '{step['say']}' (pointed at by {lesson['how'] or 'nothing'}; waiting "
            f"{waited}s). Plan: {rest}")


def active() -> bool:
    return _lesson is not None


def snapshot() -> dict:
    """For the island: {"task", "index", "total", "say"} during a lesson, {"planning": task} while one is
    being planned, else {}."""
    lesson, planning = _lesson, _planning
    if lesson is not None:
        step = lesson["steps"][lesson["index"]]
        return {"task": lesson["task"], "index": lesson["index"], "total": len(lesson["steps"]), "say": step["say"]}
    if planning is not None:
        return {"planning": planning[1]}
    return {}


def keep_loaded() -> bool:
    """A lesson is running: Mint must stay loaded to watch for the user's clicks."""
    return active()


# --- the tool ------------------------------------------------------------------------------

PROMPT = """Tutor mode: when the user wants to LEARN how to do something on the Mac - "show me how to…", \
"teach me how to…", "walk me through…", "how do I … in <app>?", "guide me" - call tutor action=start with \
the task (and the app if named). Mint then points at each control on screen and waits for the user to do \
it; do NOT do the steps yourself and do not click for them. Say each step in ONE short sentence. Messages \
starting "(Tutor)" come from the lesson: say the new step briefly. "next", "done", "skip" -> tutor \
action=next; "go back" -> back; "stop", "never mind", "just do it" -> stop (then, for "do it for me", \
use the normal tools). "Do it for me" / "export it" (no learning wanted) -> normal tools, not tutor."""


def declarations():
    from google.genai import types

    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="tutor",
        description=("Teach the user a task on screen instead of doing it: Mint plans the steps, points at "
                     "each control with an arrow and a note, waits until the user has done it, then shows "
                     "the next. Use for 'show me how', 'teach me', 'walk me through', 'how do I … in <app>'. "
                     "Actions: start (task, app), next (the user says next/done/skip), back, stop, status."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["start", "next", "back", "stop", "status"]),
            "task": types.Schema(type=S, description="start: what to teach, e.g. 'export a PDF in Preview'"),
            "app": types.Schema(type=S, description="start: the app it is in, if named or obvious "
                                                    "(e.g. 'Figma', 'System Settings'); empty = the app in front")},
            required=["action"]))]


def tool(args: dict) -> str:
    action = str(args.get("action") or "status").lower()
    if action == "start":
        return start(str(args.get("task") or ""), str(args.get("app") or ""))
    if action == "next":
        return next_step()
    if action == "back":
        return back()
    if action == "stop":
        return stop()
    return status()


HANDLERS = {"tutor": tool}
