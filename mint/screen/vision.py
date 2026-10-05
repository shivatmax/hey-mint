"""Tier 2: screen capture, for when the model needs to actually see something.

Deliberately on demand only. Streaming frames continuously costs latency and
tokens, and the desktop tool already reads the screen through the accessibility
tree, which is cheaper and more precise for acting on controls.
"""

from __future__ import annotations

import base64
import io
import re

import mss
import mss.tools
import PIL.Image

# Large enough to read UI text, small enough to stay cheap.
MAX_EDGE = 1400


# The screen area the last screenshot covered, in global screen points - the
# units mouse events use. click_at maps the model's coordinates through this.
_last_area: dict | None = None


class Blind(Exception):
    """The capture came back empty: Mint lacks Screen Recording permission."""


def _check_not_blank(image) -> None:
    """Refuse an empty capture instead of showing it to the model.

    Without Screen Recording permission macOS returns a flat grey frame, not an
    error. In testing the model was given one, "found" the requested control
    anyway, clicked where it guessed, and reported success. A model shown
    nothing must be told it can see nothing.
    """
    global _last_area
    small = image.convert("L").resize((64, 40))
    low, high = small.getextrema()
    if high - low < 12:
        _last_area = None   # and click_at will refuse until a real look
        try:
            import Quartz
            Quartz.CGRequestScreenCaptureAccess()   # puts Mint in the list and prompts
        except Exception:
            pass
        raise Blind()

_MSS = getattr(mss, "MSS", None) or mss.mss


def grab_screen(display: int = 0) -> dict[str, str]:
    """Capture a display and return an inline image part for the Live API."""
    global _last_area
    with _MSS() as sct:
        monitors = sct.monitors
        # monitors[0] is the union of every display; 1..n are the individual ones.
        index = display if 0 <= display < len(monitors) else 0
        _last_area = dict(monitors[index])
        shot = sct.grab(monitors[index])
        image = PIL.Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    image.thumbnail((MAX_EDGE, MAX_EDGE))
    _check_not_blank(image)
    buffer = io.BytesIO()
    image.save(buffer, format="jpeg", quality=80)
    # Keep the last one for diagnosis ("what did Mint actually see?"). Without
    # Screen Recording permission macOS returns only the wallpaper, which this
    # makes obvious at a glance.
    try:
        from pathlib import Path
        folder = Path.home() / "Library" / "Logs" / "Mint"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "last_look.jpg").write_bytes(buffer.getvalue())
    except OSError:
        pass
    return {
        "mime_type": "image/jpeg",
        "data": base64.b64encode(buffer.getvalue()).decode(),
    }


def display_count() -> int:
    with _MSS() as sct:
        return max(0, len(sct.monitors) - 1)


def to_point(x: float, y: float, area: dict) -> tuple[float, float]:
    """0-1000 coordinates of a screenshot of `area` -> global screen points (Quartz, top-left).
    The screenshot is scaled to at most MAX_EDGE px and Retina-doubled before that, but the
    0-1000 convention is relative to the image, so only the area (in points) matters."""
    return (area["left"] + x / 1000 * area["width"], area["top"] + y / 1000 * area["height"])


SNAP_X, SNAP_Y = 160.0, 70.0    # how far from the model's point the named text may be (points)


# describe_point naming a definite control (not a big unnamed area, which is all Electron shows)
_SPECIFIC = re.compile(r"^the (button|link|checkbox|tab or option|tab|menu item|pop-up menu|menu button|text field) '")


def _label_matches(label: str, target: str) -> bool:
    from mint.screen.ocr import _words
    have, want = set(_words(label).split()), set(_words(target).split())
    return bool(want) and want <= have


def snap(point: tuple[float, float], target: str, lines: list[dict] | None = None) -> tuple[tuple, str]:
    """The point to click for `target` near where the model pointed: the centre of the nearest
    text that says it (its own words, not its whole line), within SNAP_X x SNAP_Y points.
    -> (point, what was found) - the model's own point and '' when nothing nearby says it.

    Why: on 1 Oct the model saw VS Code's 'Uninstall' button, pointed 67 pt to its left and
    clicked 'Disable' (then 'Enable', 'Disable', ...) five times; the button was right there."""
    from mint.screen import ocr
    px, py = point
    if lines is None:
        try:
            lines = ocr.read_area(px - SNAP_X, py - SNAP_Y, 2 * SNAP_X, 2 * SNAP_Y)
        except Exception:
            return point, ""
    want = ocr._words(target)
    if not want:
        return point, ""
    best = None
    for line in lines:
        words = ocr._words(line["text"])
        if words != want and (ocr.target_span(line["text"], target) is None
                              or len(words.split()) > len(want.split()) + 3):
            continue                   # a label or button, not the same word inside a sentence
        box = ocr.target_box(line, target)
        cx, cy = ocr.center(box)
        if abs(cx - px) > SNAP_X or abs(cy - py) > SNAP_Y:
            continue
        # already on it: keep the model's point (it may mean a spot inside a wide control)
        if ocr.contains(box, point, slack=3):
            return point, line["text"]
        distance = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
        if best is None or distance < best[0]:
            best = (distance, (cx, cy), line["text"], box)
    if best is None:
        return point, ""
    _, centre, text, box = best
    if not ocr.contains(box, centre):
        return point, ""
    return centre, text


def click_at(x: float, y: float, button: str = "left", double: bool = False, target: str = "") -> str:
    """Click a point on the latest screenshot.

    `x` and `y` are 0-1000 across the screenshot's width and height (1000 is
    the right or bottom edge), which is the convention Gemini is trained to
    point with. This is the fallback for apps whose controls are invisible to
    Accessibility - Slack exposed 19 controls and no channel list in testing -
    where the desktop tool has nothing to choose from. With `target` (what the
    model means to click), the click goes to the centre of that text if it is
    near the point: model pointing is tens of points off.
    """
    import time

    import Quartz

    from mint.tools import fastinput

    if not fastinput.has_accessibility():
        return "Cannot click: Mint lacks Accessibility permission."
    if _last_area is None:
        return "Call look first; click_at uses coordinates from the latest screenshot."
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        return "x and y must be between 0 and 1000."

    area = _last_area
    pointed = to_point(x, y, area)
    found = ""
    if target:
        (px, py), found = snap(pointed, target)
    else:
        px, py = pointed
    # Self-check: the point is on the screen the screenshot showed.
    if not (area["left"] <= px <= area["left"] + area["width"] and area["top"] <= py <= area["top"] + area["height"]):
        return f"FAILED: ({int(px)}, {int(py)}) is outside the screenshot's screen area, so nothing was clicked."
    point = Quartz.CGPointMake(px, py)
    down, up = {
        "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp),
        "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp),
    }.get(button, (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp))
    mouse = Quartz.kCGMouseButtonRight if button == "right" else Quartz.kCGMouseButtonLeft

    # Say what is at that point, so a wrong guess shows at once ("that's the address bar, not the
    # extension") instead of the same blind click four times.
    try:
        from mint.tools.harness import describe_point
        what_there = describe_point(point.x, point.y)
    except Exception:
        what_there = ""
    if target and not found and _SPECIFIC.match(what_there or "") and not _label_matches(what_there, target):
        # Named something that is neither at the point nor written near it: don't click a
        # neighbour (Disable for Uninstall); say what is really there.
        return (f"NOT CLICKED: at ({int(px)}, {int(py)}) there is {what_there}, not '{target}', and no text "
                f"'{target}' is within {int(SNAP_X)} points of it. Use ui_act or click_text with '{target}', or "
                "look again and point at it.")

    from mint.ui.effects import fx
    time.sleep(fx.click(point.x, point.y, (found or target)[:40]))
    from mint.app import control
    if control.stopped():
        return "STOPPED by the user before clicking; nothing was clicked."

    move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, mouse)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.05)
    for click in range(2 if double else 1):
        for kind in (down, up):
            event = Quartz.CGEventCreateMouseEvent(None, kind, point, mouse)
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, click + 1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.03)
    try:
        from mint.screen import axkit
        from mint.screen import ground
        axkit.note_click(ground.owner_at(px, py), px, py, found or target)
    except Exception:
        pass
    what = "Double-clicked" if double else ("Right-clicked" if button == "right" else "Clicked")
    moved = ((px - pointed[0]) ** 2 + (py - pointed[1]) ** 2) ** 0.5
    if found and moved >= 3:
        hit = (f" - the text '{found[:40]}', {int(moved)} points from where you pointed "
               f"({int(pointed[0])}, {int(pointed[1])})" + (f"; it is {what_there}" if what_there else ""))
    else:
        hit = f" - that is {what_there}" if what_there else ""
    return (f"{what} at ({int(point.x)}, {int(point.y)}) on screen{hit}. If that is not what you meant, don't "
            "click the same point again: use ui_act with its name, or look again and pick a different point.")
