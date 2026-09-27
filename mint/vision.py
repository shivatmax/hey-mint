"""Tier 2: screen capture, for when the model needs to actually see something.

Deliberately on demand only. Streaming frames continuously costs latency and
tokens, and the desktop tool already reads the screen through the accessibility
tree, which is cheaper and more precise for acting on controls.
"""

from __future__ import annotations

import base64
import io

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


def click_at(x: float, y: float, button: str = "left", double: bool = False) -> str:
    """Click a point on the latest screenshot.

    `x` and `y` are 0-1000 across the screenshot's width and height (1000 is
    the right or bottom edge), which is the convention Gemini is trained to
    point with. This is the fallback for apps whose controls are invisible to
    Accessibility - Slack exposed 19 controls and no channel list in testing -
    where the desktop tool has nothing to choose from.
    """
    import time

    import Quartz

    from . import fastinput

    if not fastinput.has_accessibility():
        return "Cannot click: Mint lacks Accessibility permission."
    if _last_area is None:
        return "Call look first; click_at uses coordinates from the latest screenshot."
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        return "x and y must be between 0 and 1000."

    area = _last_area
    point = Quartz.CGPointMake(area["left"] + x / 1000 * area["width"],
                               area["top"] + y / 1000 * area["height"])
    down, up = {
        "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp),
        "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp),
    }.get(button, (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp))
    mouse = Quartz.kCGMouseButtonRight if button == "right" else Quartz.kCGMouseButtonLeft

    # Say what is at that point, so a wrong guess shows at once ("that's the address bar, not the
    # extension") instead of the same blind click four times.
    try:
        from .harness_tools import describe_point
        target = describe_point(point.x, point.y)
    except Exception:
        target = ""

    from .effects import fx
    time.sleep(fx.click(point.x, point.y))
    from . import control
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
    what = "Double-clicked" if double else ("Right-clicked" if button == "right" else "Clicked")
    hit = f" - that is {target}" if target else ""
    return (f"{what} at ({int(point.x)}, {int(point.y)}) on screen{hit}. If that is not what you meant, don't "
            "click the same point again: use ui_act with its name, or look again and pick a different point.")
