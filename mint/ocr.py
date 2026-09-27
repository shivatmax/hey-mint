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
import re
import time

from . import fastinput, jev


def _screen():
    """Full-resolution capture of the main display, plus its size in points."""
    import mss
    import PIL.Image

    grabber = getattr(mss, "MSS", None) or mss.mss
    with grabber() as sct:
        area = dict(sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0])
        shot = sct.grab(area)
        image = PIL.Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return image, area


def read_screen() -> tuple[list[dict], dict]:
    """Every line of text on the main display: [{text, x, y, w, h}] in screen points."""
    import Quartz
    import Vision
    from Foundation import NSData

    image, area = _screen()
    small = image.convert("L").resize((64, 40))
    low, high = small.getextrema()
    if high - low < 12:
        from .vision import Blind
        raise Blind()

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = NSData.dataWithBytes_length_(buffer.getvalue(), len(buffer.getvalue()))
    source = Quartz.CGImageSourceCreateWithData(data, None)
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)   # UI labels, not prose
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
        box = observation.boundingBox()   # normalised, origin bottom-left
        found.append({
            "text": text,
            "x": area["left"] + box.origin.x * area["width"],
            "y": area["top"] + (1 - box.origin.y - box.size.height) * area["height"],
            "w": box.size.width * area["width"],
            "h": box.size.height * area["height"],
        })
    return found, area


def _where(item: dict, area: dict) -> str:
    """A rough place on screen, so Jev can tell two identical labels apart."""
    cx = (item["x"] + item["w"] / 2 - area["left"]) / area["width"]
    cy = (item["y"] + item["h"] / 2 - area["top"]) / area["height"]
    horizontal = "left" if cx < 0.33 else "right" if cx > 0.66 else "middle"
    vertical = "top" if cy < 0.2 else "bottom" if cy > 0.8 else "upper" if cy < 0.5 else "lower"
    return f"{vertical} {horizontal}"


def click_text(target: str, double: bool = False) -> str:
    """Click the on-screen text that best matches `target`."""
    import Quartz

    if not fastinput.has_accessibility():
        return "Cannot click: Mint lacks Accessibility permission."
    try:
        items, area = read_screen()
    except Exception as error:
        if type(error).__name__ == "Blind":
            return ("Cannot see the screen: Mint lacks Screen Recording permission. Do not "
                    "guess; tell the user to allow it in System Settings.")
        return f"Could not read the screen: {error}"
    if not items:
        return "No text is visible on screen."

    # Exact label first: the common case ("Huddles") needs no model at all.
    # Icons next to a label are read as stray characters ("6 Huddles|" for a
    # headphone icon), so compare words only.
    wanted = _words(target)
    exact = [i for i, item in enumerate(items) if _words(item["text"]) == wanted]
    # The same label can appear in two windows - in testing, "Threads" was both
    # Slack's sidebar item and a word in a chat window behind it. The user means
    # the app in front.
    front = _front_window()
    if len(exact) > 1 and front is not None:
        in_front = [i for i in exact if _inside(items[i], front)]
        exact = in_front or exact
    if len(exact) == 1:
        index, why = exact[0], "exact text"
    else:
        pool = exact or range(len(items))
        options = {str(i): f"'{items[i]['text']}' ({_where(items[i], area)} of the screen)"
                   for i in list(pool)[:250]}   # TypeSafe allows 255 options
        chosen, why = jev.resolve(target, options, what="on-screen text")
        if chosen is None:
            return f"FAILED: could not find '{target}' on screen, so nothing was clicked. {why}"
        index = int(chosen)

    item = items[index]
    point = Quartz.CGPointMake(item["x"] + item["w"] / 2, item["y"] + item["h"] / 2)
    window = _window_at(point)
    before = _texts_in(items, window)

    # Show where: a spark flies from the orb and lands as the click does.
    from .effects import fx
    time.sleep(fx.click(point.x, point.y, _words(item["text"])[:40]))
    from . import control
    if control.stopped():
        return "STOPPED by the user before clicking; nothing was clicked."

    # Move there first. Chromium-based apps (Slack, Chrome, Electron) ignore a
    # press that arrives without the pointer having entered the element: in
    # testing, a click at exactly the right spot on Slack's "Huddles" did
    # nothing until the pointer was moved there first.
    move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.08)
    for click in range(2 if double else 1):
        for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            event = Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, click + 1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.03)

    # Read the screen again: a warm read takes about 0.1s, so there is no
    # excuse for reporting a click as a result. In testing the vision route
    # announced "Huddle started" after clicking something else entirely.
    time.sleep(0.8)
    try:
        after_items, _ = read_screen()
        # Only the clicked window counts. Comparing the whole screen let a
        # change in some other window pass for success in testing.
        after = _texts_in(after_items, window)
        changed = len(before ^ after)
    except Exception:
        changed = -1
    where = f" in {window['app']}" if window and window.get("app") else ""
    label = f"Clicked '{_words(item['text'])}'{where} at ({int(point.x)}, {int(point.y)}) ({why})"
    # A couple of labels can differ between two reads of an unchanged window.
    threshold = max(3, len(before) // 12)
    if 0 <= changed < threshold:
        return ("FAILED: " + label + ", but the window did NOT change afterwards, so the click "
                "probably had no effect - something may be covering it, such as a dialog.")
    if changed >= threshold:
        appeared = sorted(after - before, key=len, reverse=True)[:5]
        return label + f". The window changed; new text includes: {appeared}."
    return label + "."


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
        app = AX.AXUIElementCreateApplication(front.processIdentifier())
        err, window = AX.AXUIElementCopyAttributeValue(app, "AXFocusedWindow", None)
        if err == 0 and window is not None:
            _, pos = AX.AXUIElementCopyAttributeValue(window, "AXPosition", None)
            _, size = AX.AXUIElementCopyAttributeValue(window, "AXSize", None)
            p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)[1]
            s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)[1]
            if s.width > 100:
                return {"x": p.x, "y": p.y, "w": s.width, "h": s.height,
                        "app": front.localizedName() or ""}
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
                        "app": front.localizedName() or ""}
    return None


def _window_at(point) -> dict | None:
    """Bounds of the ordinary window under a screen point, front-most first."""
    import Quartz

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    for window in windows:
        if window.get("kCGWindowLayer", 0) != 0:
            continue
        b = window.get("kCGWindowBounds") or {}
        if b and b["X"] <= point.x <= b["X"] + b["Width"] and b["Y"] <= point.y <= b["Y"] + b["Height"]:
            return {"x": b["X"], "y": b["Y"], "w": b["Width"], "h": b["Height"],
                    "app": window.get("kCGWindowOwnerName", "")}
    return None


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
