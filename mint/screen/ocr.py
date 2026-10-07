"""Click on-screen text: macOS text recognition finds it, Jev chooses it.

For apps that hide their controls from Accessibility (Slack exposed 19 and no
channel list), the desktop tool has nothing to choose from, and asking a vision
model for raw coordinates proved unreliable: shown a correct screenshot of
Slack, Gemini pointed at a channel twice as far down the sidebar as "Huddles"
and then announced that a huddle had started.

This rebuilds the missing list from pixels. Apple's on-device text recognizer
returns every visible label with an exact box; Jev picks which label the user
means from that real list; the click lands in the middle of the real box. A
target that is not on screen cannot be clicked, because it is not in the list.
"""

from __future__ import annotations

import io
import os
import re
import time

from mint.tools import fastinput
from mint.core import jev


def _monitor_for(monitors: list[dict], point) -> dict:
    """The display (mss monitor, in global points) holding `point`; the main one otherwise."""
    displays = monitors[1:] or monitors
    if point is not None:
        px, py = point
        for mon in displays:
            if mon["left"] <= px < mon["left"] + mon["width"] and mon["top"] <= py < mon["top"] + mon["height"]:
                return dict(mon)
    return dict(displays[0])


def _screen(point=None):
    """Full-resolution capture of one display, plus its area in global points.

    The display is the one holding `point`, else the one holding the front window (a window on a
    second display used to be invisible here), else the main one. The image is in pixels (2x on
    Retina); everything read from it is mapped back through `area`, which is in points - the
    units of mouse events - so the scale never enters the click maths."""
    import mss
    import PIL.Image

    if point is None:
        try:
            front = _front_window()
            if front is not None:
                point = (front["x"] + front["w"] / 2, front["y"] + front["h"] / 2)
        except Exception:
            point = None
    grabber = getattr(mss, "MSS", None) or mss.mss
    with grabber() as sct:
        area = _monitor_for(list(sct.monitors), point)
        shot = sct.grab(area)
        image = PIL.Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return image, area


def to_screen(box, area: dict) -> tuple[float, float, float, float]:
    """A Vision box (normalised 0-1, origin bottom-left, as (x, y, w, h)) of an image that showed
    `area` -> (x, y, w, h) in global screen points, origin top-left (Quartz, mouse events)."""
    nx, ny, nw, nh = box
    return (area["left"] + nx * area["width"],
            area["top"] + (1 - ny - nh) * area["height"],
            nw * area["width"], nh * area["height"])


def _vision_box(observation) -> tuple[float, float, float, float]:
    b = observation.boundingBox()
    return (b.origin.x, b.origin.y, b.size.width, b.size.height)


def recognize(image, area: dict, correct: bool = False) -> list[dict]:
    """Every line of text in a PIL image of `area`: [{text, x, y, w, h, candidate}] in screen
    points. `candidate` is Vision's, for the boxes of parts of the line (see target_box)."""
    import Quartz
    import Vision
    from Foundation import NSData

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = NSData.dataWithBytes_length_(buffer.getvalue(), len(buffer.getvalue()))
    source = Quartz.CGImageSourceCreateWithData(data, None)
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(correct)   # UI labels, not prose
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"text recognition failed: {error}")

    found = []
    for observation in request.results() or []:
        candidate = observation.topCandidates_(1)
        if not candidate:
            continue
        text = str(candidate[0].string()).strip()
        if not text:
            continue
        x, y, w, h = to_screen(_vision_box(observation), area)
        found.append({"text": text, "x": x, "y": y, "w": w, "h": h, "candidate": candidate[0], "area": area})
    return found


def read_screen() -> tuple[list[dict], dict]:
    """Every line of text on the display in use: [{text, x, y, w, h}] in screen points."""
    image, area = _screen()
    small = image.convert("L").resize((64, 40))
    low, high = small.getextrema()
    if high - low < 12:
        from mint.screen.vision import Blind
        raise Blind()
    return recognize(image, area), area


def read_area(x: float, y: float, w: float, h: float) -> list[dict]:
    """The text lines in one rectangle of the screen (points), clipped to its display. Small and
    fast (~50 ms): for naming the spot the user clicked in an app that hides its controls."""
    import mss
    import PIL.Image

    grabber = getattr(mss, "MSS", None) or mss.mss
    with grabber() as sct:
        mon = _monitor_for(list(sct.monitors), (x + w / 2, y + h / 2))
        left, top = max(x, mon["left"]), max(y, mon["top"])
        right = min(x + w, mon["left"] + mon["width"])
        bottom = min(y + h, mon["top"] + mon["height"])
        if right - left < 4 or bottom - top < 4:
            return []
        area = {"left": int(left), "top": int(top), "width": int(right - left), "height": int(bottom - top)}
        shot = sct.grab(area)
        image = PIL.Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    small = image.convert("L").resize((32, 12))
    low, high = small.getextrema()
    if high - low < 6:
        return []                                  # flat: nothing there, or no Screen Recording
    return recognize(image, area)


_TOKEN = re.compile(r"[A-Za-z0-9]+")


def _same_word(a: str, b: str) -> bool:
    """OCR drops or adds a letter at a box edge ('earch' for 'Search', an icon read as 'v')."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return a.endswith(b) or b.endswith(a) or a.startswith(b) or b.startswith(a)


def target_span(text: str, target: str) -> tuple[int, int] | None:
    """Character span [start, end) of the words of `target` inside a recognised line, or None
    when the line is just the target, or the target's words are not all in it, in order."""
    tokens = [(m.group().lower(), m.start(), m.end()) for m in _TOKEN.finditer(text)]
    wanted = [w for w in _words(target).split() if w]
    if not tokens or not wanted:
        return None
    best = None
    for i in range(len(tokens)):
        run = 0
        while i + run < len(tokens) and run < len(wanted) and _same_word(tokens[i + run][0], wanted[run]):
            run += 1
        if run and (best is None or run > best[1]):
            best = (i, run)
    if best is None or best[1] < len(wanted):
        return None                                 # all of the target's words, in order
    i, run = best
    if run >= len(tokens):
        return None                                 # the whole line is the target
    return tokens[i][1], tokens[i + run - 1][2]


def target_box(item: dict, target: str = "") -> tuple[float, float, float, float]:
    """The box (screen points) to click for `target` in a recognised line: just the target's words
    when the line holds more ('Microsoft microsoft.com 52,882,693', 'Uninstall v' with its chevron),
    else the whole line. Clicking the middle of the whole line hit the neighbouring words."""
    whole = (item["x"], item["y"], item["w"], item["h"])
    span = target_span(item["text"], target) if target else None
    candidate, area = item.get("candidate"), item.get("area")
    if span is None or candidate is None or area is None:
        return whole
    try:
        result = candidate.boundingBoxForRange_error_((span[0], span[1] - span[0]), None)
        observation = result[0] if isinstance(result, tuple) else result
        if observation is None:
            return whole
        box = to_screen(_vision_box(observation), area)
    except Exception:
        return whole
    # A sub-box must lie inside the line it came from; anything else is a Vision oddity.
    if box[2] < 2 or box[3] < 2 or not contains(whole, center(box), slack=2):
        return whole
    return box


def center(box) -> tuple[float, float]:
    x, y, w, h = box
    return x + w / 2, y + h / 2


def contains(box, point, slack: float = 0.0) -> bool:
    x, y, w, h = box
    px, py = point
    return x - slack <= px <= x + w + slack and y - slack <= py <= y + h + slack


def click_point(item: dict, target: str = "") -> tuple[tuple[float, float], tuple]:
    """(the screen point to click, the box it is the centre of) for a recognised line."""
    box = target_box(item, target)
    return center(box), box


def _where(item: dict, area: dict) -> str:
    """A rough place on screen, so Jev can tell two identical labels apart."""
    cx = (item["x"] + item["w"] / 2 - area["left"]) / area["width"]
    cy = (item["y"] + item["h"] / 2 - area["top"]) / area["height"]
    horizontal = "left" if cx < 0.33 else "right" if cx > 0.66 else "middle"
    vertical = "top" if cy < 0.2 else "bottom" if cy > 0.8 else "upper" if cy < 0.5 else "lower"
    return f"{vertical} {horizontal}"


def read_window(app) -> tuple[list[dict], dict, dict]:
    """The text lines of one app's window, read from a picture of that window alone (with its own
    sheets and popovers; capture.area), never from the screen rectangle: a window lying over it or
    beside it is not in the picture, so its text cannot be credited to the app. The picture passes
    capture's 1x/2x pixel-frame check, and the boxes map back through the window's own frame.
    -> (items, area, window {x, y, w, h, app, pid, window_id}). Raises LookupError (no window),
    capture.CaptureError (no picture, or a mismatched one) or vision.Blind."""
    from mint.screen import axkit
    from mint.screen import capture
    pid = int(app.processIdentifier())
    name = app.localizedName() or "the app"
    ax_window = axkit.focused_window(pid)
    wid = axkit.window_id(ax_window) if ax_window is not None else None
    if not wid:
        shown = axkit.server_windows(pid)
        wid = shown[0] if shown else None
    bounds = capture.window_bounds(wid) if wid else None
    if bounds is None:
        raise LookupError(f"{name} has no window on screen")
    shot = capture.area(bounds, wid)
    if shot.get("method") == "region":
        shot = capture.window(wid)           # never the screen grab: it shows whatever lies on top
    x, y, w, h = shot["bounds"]
    area = {"left": x, "top": y, "width": w, "height": h}
    small = shot["image"].convert("L").resize((64, 40))
    low, high = small.getextrema()
    if high - low < 12:
        from mint.screen.vision import Blind
        raise Blind()
    window = {"x": x, "y": y, "w": w, "h": h, "app": name, "pid": pid, "window_id": wid}
    items = recognize(shot["image"], area)
    for item in items:
        item["window_id"] = wid
    return items, area, window


def _running(app):
    """An NSRunningApplication for an app object or a name ("Visual Studio Code" runs as "Code")."""
    if app is None or hasattr(app, "processIdentifier"):
        return app
    from mint.screen import ground
    return ground.running_app(str(app))


def click_text(target: str, double: bool = False, app=None) -> str:
    """Click the on-screen text that best matches `target`. With `app`, only that app's window is
    read (a picture of it alone), so another window lying on top or beside cannot lend its text."""
    import Quartz

    if not fastinput.has_accessibility():
        return "Cannot click: Mint lacks Accessibility permission."
    running = _running(app)
    if app is not None and running is None:
        return f"FAILED: {app} is not running, so nothing was clicked; open it first."
    if running is not None and int(running.processIdentifier()) == os.getpid():
        running = None                     # Mint's own window: read off the screen as before
    try:
        if running is not None:
            items, area, front = read_window(running)
            rows = None
        else:
            items, area = read_screen()
            front, rows = _front_window(), _layer0()
    except Exception as error:
        if type(error).__name__ == "Blind":
            return ("Cannot see the screen: Mint lacks Screen Recording permission. Do not "
                    "guess; tell the user to allow it in System Settings.")
        if isinstance(error, LookupError):
            return f"FAILED: {error}, so nothing was clicked."
        return f"Could not read the screen: {error}"
    if not items:
        return f"No text is visible in {front['app']}'s window." if running is not None else "No text is visible on screen."
    seen = (lambda item: _seen_in(item, front, rows)) if front is not None else (lambda item: True)
    of = "of the window" if running is not None else "of the screen"

    # Exact label first: the common case ("Huddles") needs no model at all.
    # Icons next to a label are read as stray characters ("6 Huddles|" for a
    # headphone icon), so compare words only.
    wanted = _words(target)
    exact = [i for i, item in enumerate(items) if _words(item["text"]) == wanted]
    # The same label can appear in two windows - in testing, "Threads" was both
    # Slack's sidebar item and a word in a chat window behind it. The user means
    # the app in front - where its window can be seen, not where another lies over it.
    if len(exact) > 1 and front is not None:
        in_front = [i for i in exact if seen(items[i])]
        exact = in_front or exact
    if not exact:
        # The words inside a longer line ("microsoft.com" in "Microsoft microsoft.com 52,882,693"):
        # one such line in the front window needs no model either.
        within = [i for i, item in enumerate(items) if wanted and target_span(item["text"], target) is not None
                  and seen(item)]
        if len(within) == 1:
            exact = within
    if len(exact) == 1:
        index, why = exact[0], "exact text"
    else:
        pool = exact or range(len(items))
        if front is not None:          # the front window's text first (the list is cut at 250)
            pool = sorted(pool, key=lambda i: not seen(items[i]))
        options = {str(i): f"'{items[i]['text']}' ({_where(items[i], area)} {of})"
                   for i in list(pool)[:250]}   # TypeSafe allows 255 options
        chosen, why = jev.resolve(target, options, what="on-screen text")
        if chosen is None:
            where = f"in {front['app']}'s window" if running is not None else "on screen"
            return f"FAILED: could not find '{target}' {where}, so nothing was clicked. {why}"
        index = int(chosen)

    item = items[index]
    (px, py), box = click_point(item, target)
    # Self-check before anything moves: the point must be inside the box of the text that was
    # chosen, on the display (or window) that was read, and - for an app's own window - that spot
    # must not be covered by another app's window.
    line_box = (item["x"], item["y"], item["w"], item["h"])
    if not (contains(box, (px, py)) and contains(line_box, (px, py), slack=2)
            and contains((area["left"], area["top"], area["width"], area["height"]), (px, py))):
        return (f"FAILED: the point worked out for '{item['text'][:40]}' ({int(px)}, {int(py)}) is not inside its "
                f"box {tuple(int(v) for v in box)}, so nothing was clicked.")
    point = Quartz.CGPointMake(px, py)
    window = _window_at(point)
    if running is not None:
        if window is not None and window.get("pid") and window["pid"] != front["pid"]:
            return (f"FAILED: '{item['text'][:40]}' is in {front['app']}'s window, but {window['app']} covers that "
                    f"spot, so nothing was clicked. Bring {front['app']} forward first, or use ui_act (no pointer).")
        window = front
    # Without an app the text was read off the screen as drawn, so it belongs to the window on top
    # at that spot (`window`) - even when the front app's window lies under it.
    before = _texts_in(items, window)
    focus_before = _focus_role(window)

    # Show where: a spark flies from the orb and lands as the click does.
    from mint.ui.effects import fx
    time.sleep(fx.click(point.x, point.y, _words(item["text"])[:40]))
    from mint.app import control
    if control.stopped():
        return "STOPPED by the user before clicking; nothing was clicked."

    # An accessibility press on the control under the text first: no cursor, no activation. Only
    # for native controls that advertise it (effect.press_target_at) - Chromium ignores AXPress, so
    # Slack's "Huddles" still gets the pointer, moved there first: in testing, a press at exactly
    # the right spot did nothing until the pointer had entered it.
    from mint.screen import axkit
    from mint.screen import effect
    windows_before = effect.window_snapshot()
    quiet_key = ((window or {}).get("pid"), _words(item["text"]))
    pressed, evidence = None, []
    if not double and time.monotonic() - _ax_quiet.get(quiet_key, -1e9) > 120:
        try:
            pressed = effect.press_target_at(px, py, (window or {}).get("pid"))
            if pressed is not None:
                done, evidence, problem = effect.ax_deliver(pressed, "AXPress")
                # An error after the app may already have acted: never click it again with the pointer.
                pressed = pressed if done or effect.maybe_acted(problem) else None
        except Exception:
            pressed = None
    if pressed is not None:
        axkit.note_click((window or {}).get("pid"), px, py, item["text"])
        route, delivery, verb = "accessibility", "background", "Pressed"
    else:
        from mint.screen import ground
        ground.mouse_click(px, py, double=double, label=item["text"], spark=False)
        route, delivery, verb = "global_input", "foreground", "Double-clicked" if double else "Clicked"

    # Read the screen again: a warm read takes about 0.1s, so there is no
    # excuse for reporting a click as a result. In testing the vision route
    # announced "Huddle started" after clicking something else entirely.
    time.sleep(0.8)
    try:
        after_items = read_window(running)[0] if running is not None else read_screen()[0]
        # Only the clicked window counts. Comparing the whole screen let a
        # change in some other window pass for success in testing.
        after = _texts_in(after_items, window)
        changed = len(before ^ after)
    except Exception:
        changed = -1
    evidence += effect.window_changes(windows_before, effect.window_snapshot(), pids=((window or {}).get("pid"),))
    where = f" in {window['app']}" if window and window.get("app") else ""
    label = f"{verb} '{_words(item['text'])}'{where} at ({int(point.x)}, {int(point.y)}) ({why})"
    # A couple of labels can differ between two reads of an unchanged window.
    threshold = max(3, len(before) // 12)
    if changed >= threshold:
        appeared = sorted(after - before, key=len, reverse=True)[:5]
        evidence.insert(0, f"the window changed; new text includes: {appeared}")
    if 0 <= changed < threshold and not evidence:
        # A click into a text box changes nothing you can see until you type (VS Code's
        # "Search Extensions in Marketplace" on 1 Oct: the click was right, the report said FAILED,
        # and the model gave up on it). The keyboard focus says whether it went in.
        focus_after = _focus_role(window)
        if focus_after == "input":
            evidence = ["the text box now has the keyboard focus - type into it with type_text (no field= needed)"]
        elif _PLACEHOLDER.match(_words(item["text"])) and focus_after != focus_before:
            return effect.Effect(effect.UNVERIFIABLE, label + "; nothing else changed, but the keyboard focus moved - "
                                 "it looks like a text box's placeholder, so the cursor is probably in it now",
                                 route, delivery, escalation="type with type_text, then check").render()
        elif route == "accessibility":
            _ax_quiet[quiet_key] = time.monotonic()
            return effect.Effect(effect.UNVERIFIABLE, label + "; the window did not change afterwards", route,
                                 delivery, escalation="if it really did nothing, call click_text again: the next "
                                 "try uses the real pointer").render()
        else:
            return effect.Effect(effect.NOOP, label + "; the window did NOT change afterwards, so the click probably "
                                 "had no effect - something may be covering it, such as a dialog", route, delivery,
                                 escalation="look at what is in front (ui_elements or look) before clicking again"
                                 ).render()
    if evidence:
        return effect.Effect(effect.CONFIRMED, label, route, delivery, evidence).render()
    return effect.Effect(effect.UNVERIFIABLE, label + "; the screen could not be read again to check", route,
                         delivery, escalation="look to check").render()


# Text whose accessibility press showed nothing: clicked with the pointer if asked again soon.
_ax_quiet: dict = {}

_PLACEHOLDER = re.compile(r"^(s?earch|type|enter|filter|find|ask|message|write|add|go to|reply|new|name|"
                          r"what|where|email|url|address)\b")


def _focus_role(window: dict | None) -> str:
    """'input' when the app that owns `window` has a text box focused, else a fingerprint of what
    is focused ('' when unknown) - so a click that only moved the focus can be told apart."""
    try:
        from mint.screen import axkit
        pid = (window or {}).get("pid")
        focused = axkit.focused_element(pid)
        if focused is None:
            return ""
        if axkit.is_editable(focused):
            return "input"
        return f"{axkit.attr(focused, 'AXRole')}:{axkit.frame(focused)}"
    except Exception:
        return ""


def _inside(item: dict, window: dict) -> bool:
    cx, cy = item["x"] + item["w"] / 2, item["y"] + item["h"] / 2
    return (window["x"] <= cx <= window["x"] + window["w"]
            and window["y"] <= cy <= window["y"] + window["h"])


def _front_window() -> dict | None:
    """Bounds of the window the user is actually looking at.

    Asked of the app through Accessibility (its focused window). Taking the
    app's first window from the system window list was wrong with several
    Chrome windows open: verification then read the wrong window and reported
    a successful paste as failed.
    """
    import AppKit
    import ApplicationServices as AX
    import Quartz

    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return None
    try:
        if front.processIdentifier() == os.getpid():
            raise LookupError("Mint itself: read only from its main thread, so use the window list")
        app = AX.AXUIElementCreateApplication(front.processIdentifier())
        err, window = AX.AXUIElementCopyAttributeValue(app, "AXFocusedWindow", None)
        if err == 0 and window is not None:
            _, pos = AX.AXUIElementCopyAttributeValue(window, "AXPosition", None)
            _, size = AX.AXUIElementCopyAttributeValue(window, "AXSize", None)
            p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)[1]
            s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)[1]
            if s.width > 100:
                return {"x": p.x, "y": p.y, "w": s.width, "h": s.height,
                        "app": front.localizedName() or "", "pid": int(front.processIdentifier())}
    except Exception:
        pass
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    for window in windows:
        if window.get("kCGWindowOwnerPID") == front.processIdentifier() and window.get("kCGWindowLayer", 0) == 0:
            b = window.get("kCGWindowBounds") or {}
            if b.get("Width", 0) > 100:
                return {"x": b["X"], "y": b["Y"], "w": b["Width"], "h": b["Height"],
                        "app": front.localizedName() or "", "pid": int(front.processIdentifier())}
    return None


def _layer0() -> list[dict]:
    """The ordinary on-screen windows, front to back: [{x, y, w, h, app, pid}]. Invisible ones
    (alpha 0) are left out: they cover nothing."""
    import Quartz

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    rows = []
    for window in windows:
        b = window.get("kCGWindowBounds") or {}
        if window.get("kCGWindowLayer", 0) != 0 or not b or float(window.get("kCGWindowAlpha", 1) or 0) <= 0:
            continue
        rows.append({"x": b["X"], "y": b["Y"], "w": b["Width"], "h": b["Height"],
                     "app": window.get("kCGWindowOwnerName", ""), "pid": int(window.get("kCGWindowOwnerPID", 0))})
    return rows


def _top_at(rows: list[dict], x: float, y: float) -> dict | None:
    return next((r for r in rows if r["x"] <= x <= r["x"] + r["w"] and r["y"] <= y <= r["y"] + r["h"]), None)


def _seen_in(item: dict, window: dict, rows: list[dict] | None) -> bool:
    """The text is drawn by `window`: inside its bounds, and no other app's window lies on top there
    (the screen picture then shows that other window's text)."""
    if not _inside(item, window):
        return False
    if rows is None or not window.get("pid"):
        return True
    top = _top_at(rows, item["x"] + item["w"] / 2, item["y"] + item["h"] / 2)
    return top is None or top["pid"] == window["pid"]


def _window_at(point) -> dict | None:
    """Bounds of the ordinary window under a screen point, front-most first."""
    return _top_at(_layer0(), point.x, point.y)


def _texts_in(items: list[dict], window: dict | None) -> set[str]:
    """Normalised labels inside a window. Normalised because two reads of an
    unchanged screen differ slightly ("6 Huddles" then "6g Huddles")."""
    inside = items if window is None else [
        i for i in items
        if window["x"] <= i["x"] + i["w"] / 2 <= window["x"] + window["w"]
        and window["y"] <= i["y"] + i["h"] / 2 <= window["y"] + window["h"]]
    return {w for w in (_words(i["text"]) for i in inside) if len(w) > 2}


def _words(text: str) -> str:
    """Just the words, for matching labels read next to icons.

    An icon beside a label is read as a stray character ("6 Huddles", "O
    Threads"), so single characters are dropped when there are real words too.
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if len(tokens) > 1:
        tokens = [t for t in tokens if len(t) > 1] or tokens
    return " ".join(tokens)


def warm_up() -> None:
    """Load the text recognizer once, in the background. The first use of the
    accurate model took 26s in testing; after that a full screen takes ~0.1s."""
    try:
        import Quartz
        import Vision

        context = Quartz.CGBitmapContextCreate(None, 64, 64, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
                                               Quartz.kCGImageAlphaPremultipliedLast)
        image = Quartz.CGBitmapContextCreateImage(context)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None) \
            .performRequests_error_([request], None)
    except Exception:
        pass
