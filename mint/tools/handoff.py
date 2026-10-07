"""Hand files to an app: "compress it with Keka", "open these in Preview", "drop it into Keka".

Two ways, as a person would do it:
- open (the default): Finder's Open With. The app gets the files as if they were dropped on its Dock
  icon; a sandboxed app (Keka) gets access to exactly those files this way. Keka compresses what it is
  given, Preview shows it, Music adds it.
- drag: Mint really drags them. The files are shown in the notch as tiles (found files already are),
  the pointer glides to one, picks it up and carries it onto the app's window, then goes back where it
  was. For apps that only take files dropped into a window.

Then whatever the app asks next is looked at. A save-location question for the job the user asked for
(Keka's "Compress "build.zip" in zip to…" when it may not write next to the folder) is answered with its
default button, which keeps the place the app suggests; anything else is described so Mint can ask.
New files next to the originals (build.zip) are reported by name.
"""

from __future__ import annotations

import difflib
import logging
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("mint.tools.handoff")

HOME = str(Path.home())

# What the user asked the app to do -> the words its questions and buttons use for it.
_JOBS = {
    "compress": ("compress", "zip", "archive", "pack"),
    "extract": ("extract", "unzip", "unpack", "decompress", "unarchive", "expand"),
    "convert": ("convert", "export", "save as"),
}
_PRESSABLE = {"compress", "extract", "save", "export", "convert", "create", "continue", "ok", "choose"}


def _short(path: str) -> str:
    return "~" + path[len(HOME):] if path.startswith(HOME + "/") else path


# --- the app and the files -------------------------------------------------------------------------------

def find_app(name: str) -> dict | None:
    """An installed app by the user's (possibly misheard) name: "Kika" is Keka."""
    from mint.tools import connectors
    app = connectors.find_app(name)
    if app is not None:
        return app
    wanted = " ".join(re.sub(r"[^a-z0-9+ ]", " ", name.lower()).split())
    apps = list(connectors.installed_apps().values())
    close = difflib.get_close_matches(wanted, [a["name"].lower() for a in apps], n=1, cutoff=0.7)
    return next((a for a in apps if a["name"].lower() == close[0]), None) if close else None


def _paths(raw) -> tuple[list[str], str]:
    """The files to hand over (full paths), and why not if none can be used."""
    from mint.tools.harness import _resolve
    if isinstance(raw, str):
        text = raw.strip()
        items = [p for p in re.split(r"\n|;|\|", text) if p.strip()]
        if text.startswith("["):
            import json
            try:
                items = [str(p) for p in json.loads(text)]
            except ValueError:
                pass
    else:
        items = [str(p) for p in raw or []]
    found, missing = [], []
    for item in items:
        path, why = _resolve(item.strip().strip("\"'"))
        if path is None or not path.exists():
            missing.append(item.strip())
        else:
            found.append(str(path))
    if not found:
        return [], ("none of these exist: " + ", ".join(missing[:4])) if missing else "no files were given"
    return found, ""


def _job(words: str) -> str:
    low = words.lower()
    return next((job for job, verbs in _JOBS.items() if any(v in low for v in verbs)), "")


def _request() -> str:
    try:
        from mint.app import live
        return live.request() or ""
    except Exception:
        return ""


# --- what the app asks next --------------------------------------------------------------------------------

def _pid(app: dict) -> int | None:
    import AppKit
    for running in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if str(running.bundleIdentifier() or "") == app.get("bundle_id"):
            return int(running.processIdentifier())
    return None


def _texts(element, depth: int = 0, out: list | None = None) -> list:
    from mint.screen.axkit import attr
    out = [] if out is None else out
    if depth > 7 or len(out) > 40:
        return out
    role = attr(element, "AXRole")
    if role == "AXStaticText":
        value = str(attr(element, "AXValue") or "").strip()
        if value:
            out.append(value)
    for child in attr(element, "AXChildren") or []:
        _texts(child, depth + 1, out)
    return out


def _buttons(element, depth: int = 0, out: list | None = None) -> list:
    from mint.screen.axkit import attr
    out = [] if out is None else out
    if depth > 7:
        return out
    if attr(element, "AXRole") == "AXButton":
        title = str(attr(element, "AXTitle") or "").strip()
        if title:
            out.append((title, element))
    for child in attr(element, "AXChildren") or []:
        _buttons(child, depth + 1, out)
    return out


def question(app: dict) -> dict | None:
    """The app's open question, if any: {title, texts, buttons, default (title), press (the default button)}.
    A dialog window, a sheet on a window, or a save panel."""
    import ApplicationServices as AX
    from mint.screen.axkit import attr
    pid = _pid(app)
    if pid is None:
        return None
    root = AX.AXUIElementCreateApplication(pid)
    for window in attr(root, "AXWindows") or []:
        candidates = [window] + [c for c in attr(window, "AXChildren") or [] if attr(c, "AXRole") == "AXSheet"]
        for part in candidates:
            subrole = str(attr(part, "AXSubrole") or "")
            role = str(attr(part, "AXRole") or "")
            default = attr(part, "AXDefaultButton")
            if role != "AXSheet" and subrole not in ("AXDialog", "AXSystemDialog") and default is None:
                continue
            buttons = _buttons(part)
            if not buttons:
                continue
            title = str(attr(part, "AXTitle") or "").strip()
            chosen = str(attr(default, "AXTitle") or "") if default is not None else ""
            return {"title": title, "texts": _texts(part)[:8], "buttons": [b for b, _ in buttons][:8],
                    "default": chosen, "press": default}
    # A sandboxed app's save panel is drawn by another process and may not show up above: its window title
    # still does ("Compress "build.zip" in zip to...").
    import Quartz
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0):
        name = str(w.get("kCGWindowName") or "")
        if w.get("kCGWindowOwnerPID") == pid and re.search(r"\bto(\.\.\.|…)\s*$|^save|^export", name, re.I):
            verb = name.split()[0].strip("\"“").title() if name else "Save"
            return {"title": name, "texts": [], "buttons": [], "default": verb, "press": None, "keys": True}
    log.info("no question from %s: %s", app.get("name"), [(str(attr(w, "AXTitle")), str(attr(w, "AXSubrole")))
                                                          for w in attr(root, "AXWindows") or []])
    return None


def _answer(app: dict, job: str, wait: float = 6.0) -> tuple[str, bool]:
    """Wait a moment for the app's question; press its default button when it is the job the user asked
    for (a save-location question for "compress"). (what happened, pressed?)."""
    import ApplicationServices as AX
    end = time.monotonic() + wait
    asked = None
    while time.monotonic() < end:
        asked = question(app)
        if asked:
            break
        time.sleep(0.3)
    if not asked:
        return "", False
    said = " ".join([asked["title"]] + asked["texts"]).lower()
    button = asked["default"].lower()
    fits = bool(job) and (any(v in said for v in _JOBS.get(job, ())) or any(v in button for v in _JOBS.get(job, ())))
    if fits and button in _PRESSABLE and asked.get("keys"):
        from mint.tools.fastinput import press_key
        from mint.screen.ground import bring_forward
        bring_forward(app["name"], wait=2.0)
        time.sleep(0.4)
        press_key("return")                      # the panel's default button
        time.sleep(1.0)
        if question(app) is None:
            return (f"{app['name']} asked “{asked['title']}” - pressed {asked['default']}, keeping the place it "
                    "suggested."), True
        return (f"{app['name']} is asking “{asked['title']}” (where to save) and pressing Return didn't answer it. "
                "Look at the screen and press its default button with ui_act."), False
    if fits and button in _PRESSABLE and asked["press"] is not None:
        time.sleep(0.4)                          # let it settle: a sheet still sliding in ignores a press
        if AX.AXUIElementPerformAction(asked["press"], "AXPress") == 0:
            label = asked["title"] or (asked["texts"][0] if asked["texts"] else "where to save")
            return (f"{app['name']} asked “{label}” - pressed {asked['default']}, keeping the place it "
                    "suggested."), True
    words = "; ".join(t for t in asked["texts"][:4])
    return (f"{app['name']} is asking: “{asked['title']}” {words} - buttons: {', '.join(asked['buttons'])}"
            f"{' (default ' + asked['default'] + ')' if asked['default'] else ''}. Ask the user which, then press it "
            "with ui_act."), False


def _listing(folders: set[str]) -> dict[str, float]:
    seen = {}
    for folder in folders:
        try:
            for entry in os.scandir(folder):
                if not entry.name.startswith("."):
                    seen[entry.path] = entry.stat().st_mtime
        except OSError:
            pass
    return seen


def _made(before: dict, folders: set[str], since: float, wait: float) -> list[str]:
    """New files the app wrote next to the originals, once their size stops changing."""
    end = time.monotonic() + wait
    sizes: dict[str, int] = {}
    while time.monotonic() < end:
        now = _listing(folders)
        fresh = [p for p, m in now.items() if p not in before and m >= since - 2]
        if fresh:
            stable = True
            for path in fresh:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = -1
                if sizes.get(path) != size or size <= 0:
                    stable = False
                sizes[path] = size
            if stable:
                return fresh
        time.sleep(0.5)
    return [p for p in sizes if os.path.exists(p)]


# --- the ways ------------------------------------------------------------------------------------------------

def open_with(paths: list[str], app: dict) -> str:
    done = subprocess.run(["open", "-a", app["path"], *paths], capture_output=True, text=True, timeout=20,
                          check=False)
    if done.returncode != 0:
        return f"FAILED: {app['name']} would not take them: {(done.stderr or '').strip()[:200]}"
    return ""


def _post(kind, x: float, y: float, clicks: int = 1) -> None:
    import Quartz
    event = Quartz.CGEventCreateMouseEvent(None, kind, Quartz.CGPointMake(x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _glide(kind, start: tuple, end: tuple, seconds: float, arc: float = 0.0) -> None:
    """Move the pointer along an eased curve (a person's hand, not a teleport)."""
    steps = max(8, int(seconds * 90))
    (x0, y0), (x1, y1) = start, end
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        lift = math.sin(math.pi * e) * arc
        _post(kind, x0 + (x1 - x0) * e, y0 + (y1 - y0) * e - lift)
        time.sleep(seconds / steps)


def _on_main(fn, timeout: float = 3.0):
    from PyObjCTools import AppHelper
    box, ready = [], threading.Event()

    def run():
        try:
            box.append(fn())
        except Exception:
            log.debug("main-thread call failed", exc_info=True)
            box.append(None)
        ready.set()
    AppHelper.callAfter(run)
    ready.wait(timeout)
    return box[0] if box else None


def _tile_point(path: str):
    """Where the notch shows this file (Quartz points, top-left origin), or None."""
    import AppKit
    from mint.ui import notch_search

    def find():
        for view in list(getattr(notch_search, "_views", [])):
            rect = view.screen_rect(path)
            if rect is not None:
                return rect
        return None
    rect = _on_main(find)
    if rect is None:
        return None
    height = AppKit.NSScreen.screens()[0].frame().size.height
    return (rect.origin.x + rect.size.width / 2, height - (rect.origin.y + rect.size.height / 2))


def _window_point(app: dict):
    """The middle of the app's front window (Quartz points), or None."""
    import Quartz
    pid = _pid(app)
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly
                                               | Quartz.kCGWindowListExcludeDesktopElements, 0):
        if w.get("kCGWindowOwnerPID") != pid or w.get("kCGWindowLayer") != 0:
            continue
        b = w["kCGWindowBounds"]
        if b["Width"] * b["Height"] > 20000:
            return (b["X"] + b["Width"] / 2, b["Y"] + b["Height"] / 2)
    return None


def drag(paths: list[str], app: dict) -> str:
    """Carry the first file from the notch onto the app's window with the real pointer ('' when done)."""
    from mint.ui import notch
    try:
        from mint.ui import notch_search
    except ImportError:
        return "the notch search isn't available"
    if not notch.active():
        return "files are dragged out of the notch, and Mint isn't in the notch right now"
    from mint.screen.ground import bring_forward
    subprocess.run(["open", "-a", app["path"]], capture_output=True, timeout=15, check=False)
    bring_forward(app["name"], wait=3.0)
    target = None
    for _ in range(10):
        target = _window_point(app)
        if target:
            break
        time.sleep(0.3)
    if target is None:
        return f"{app['name']} has no window to drop into"
    __import__("mint.ui.window_glow", fromlist=["glow"]).glow(point=target, seconds=6.0)   # where the file lands
    notch_search.show(paths=paths[:1], query="", title=f"To {app['name']}")
    start = None
    for _ in range(12):                          # the notch grows and the tile lands
        time.sleep(0.25)
        start = _tile_point(paths[0])
        if start:
            break
    if start is None:
        return "the notch didn't show the file to pick up"
    import Quartz
    from mint.ui.effects import fx
    home = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    home = (float(home.x), float(home.y))
    time.sleep(0.4)                              # the tile settles under the grown notch
    start = _tile_point(paths[0]) or start
    _glide(Quartz.kCGEventMouseMoved, home, start, 0.45)
    time.sleep(0.25)
    _post(Quartz.kCGEventLeftMouseDown, *start)
    time.sleep(0.12)
    for i in range(1, 7):                        # past the drag threshold, slowly: the drag starts here
        _post(Quartz.kCGEventLeftMouseDragged, start[0], start[1] + i * 2)
        time.sleep(0.03)
    lifted = (start[0], start[1] + 12)
    picked = False
    for _ in range(15):                          # AppKit starts the drag on its own thread's time
        _post(Quartz.kCGEventLeftMouseDragged, *lifted)
        time.sleep(0.06)
        if notch_search.dragging():
            picked = True
            break
    print(f"  [drag: {_short(paths[0])} from the notch ({int(start[0])}, {int(start[1])}) to {app['name']} "
          f"({int(target[0])}, {int(target[1])}), picked up: {picked}]", flush=True)
    _glide(Quartz.kCGEventLeftMouseDragged, lifted, target, 0.9, arc=40)
    for dx in (4, -4, 0):                        # a moment over the window: it lights up for the drop
        _post(Quartz.kCGEventLeftMouseDragged, target[0] + dx, target[1])
        time.sleep(0.12)
    fx.click(target[0], target[1], f"Into {app['name']}")
    _post(Quartz.kCGEventLeftMouseUp, *target)
    __import__("mint.ui.sfx", fromlist=["play"]).play("drop")
    time.sleep(0.35)
    _glide(Quartz.kCGEventMouseMoved, target, home, 0.35)
    if not picked:
        return "the file didn't lift out of the notch"
    return ""


# --- the tool ------------------------------------------------------------------------------------------------

def hand_to_app(args: dict) -> str:
    name = str(args.get("app") or "").strip()
    if not name:
        return "FAILED: say which app (app=Keka)."
    app = find_app(name)
    if app is None:
        return f"FAILED: no app called '{name}' on this Mac. find_files kind=app can look it up."
    paths, why = _paths(args.get("paths") or args.get("path") or "")
    if not paths:
        return f"FAILED: {why}. Find them first (find_files), then hand over the full paths."
    how = str(args.get("how") or "").lower()
    if how not in ("open", "drag"):
        how = "drag" if re.search(r"\b(drag|drop)", _request().lower()) else "open"
    job = _job(str(args.get("job") or "") + " " + _request())
    folders = {os.path.dirname(p.rstrip("/")) for p in paths}
    before, since = _listing(folders), time.time()
    names = ", ".join(os.path.basename(p.rstrip("/")) for p in paths[:3]) + (f" and {len(paths) - 3} more"
                                                                             if len(paths) > 3 else "")
    note = ""
    if how == "drag":
        problem = drag(paths, app)
        if problem:
            note = f" (Couldn't drag: {problem}; handed them over with Open With instead.)"
            how = "open"
    if how != "drag":
        failed = open_with(paths, app)
        if failed:
            return failed
    said, pressed = _answer(app, job, wait=6.0 if job else 3.0)
    if said and not pressed:
        return f"Gave {names} to {app['name']}{note}. {said}"
    made = _made(before, folders, since, 45.0 if job else 4.0) if (job or pressed) else []
    if how == "drag" and job and not made and not pressed:
        # Dropped, but the app didn't start on it: hand it over the sure way so the job still gets done.
        note = f" ({app['name']} didn't take the drop, so Mint handed them over with Open With.)"
        failed = open_with(paths, app)
        if failed:
            return failed
        said, pressed = _answer(app, job, wait=6.0)
        if said and not pressed:
            return f"Gave {names} to {app['name']}{note}. {said}"
        made = _made(before, folders, since, 45.0)
    if made:
        listed = ", ".join(_short(p) for p in made[:4])
        return f"DONE: {app['name']} made {listed} from {names}.{note}" + (f" {said}" if said else "")
    if job:
        return (f"NOT DONE YET: gave {names} to {app['name']} ({'dragged in' if how == 'drag' else 'Open With'})"
                f"{note}, but nothing new has appeared next to it. {said + ' ' if said else ''}Don't say it's done "
                f"or compressing: look at {app['name']}'s window and tell the user what it shows.")
    return f"DONE: opened {names} in {app['name']}{note}." + (f" {said}" if said else "")


PROMPT = """Files INTO an app: "compress it with Keka", "zip the build folder with Keka", "open these in \
Preview", "add it to Music", "drop it into Keka", "extract this with The Unarchiver" -> hand_to_app with the full \
paths (from find_files or what the notch shows) and the app (misheard names are matched: Kika = Keka). It hands them \
over like Finder's Open With, answers the app's save-location question for the job asked, and reports the new \
file (build.zip). how=drag only when the user says drag / drop / move it in, or the app takes files only in its \
window: Mint then carries the file out of the notch onto the app with the pointer. Never do this by clicking and \
dragging in Finder, run_applescript, or making aliases."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="hand_to_app",
        description=("Give files or folders to an app to work on, as Finder's Open With or by really dragging them "
                     "onto its window: compress or extract with Keka, open in Preview, add to Music or Photos. "
                     "Answers the app's save-location question for that job and says which file it made."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "paths": types.Schema(type=S, description="full paths, one per line (or a JSON list)"),
            "app": types.Schema(type=S, description="the app, e.g. Keka"),
            "how": types.Schema(type=S, enum=["open", "drag"], description="open (default) or drag onto its window"),
            "job": types.Schema(type=S, description="what the app should do, in the user's words (compress, extract, "
                                                    "open)")},
            required=["paths", "app"]))]


HANDLERS = {"hand_to_app": hand_to_app}
