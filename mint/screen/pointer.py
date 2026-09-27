"""Find something on screen and show it: scroll to it, then box, underline,
highlight, circle or point at it - in any app (a PDF, a web page, a chat, a file).

Finding is by the words themselves. Apple's text recognizer reads the screen
and, crucially, gives the box of any part of a line - so "the deadline" is
boxed as those two words, not the whole paragraph. A phrase that wraps onto
the next line is found across lines and each piece is marked. If the exact
words are not there, a line covering most of them is accepted, and failing
that Jev picks the line that means it from the real list (or says none).

If it is not visible, the front window is scrolled a screen at a time, reading
after each step; at the bottom it turns round and searches upwards.
"""

from __future__ import annotations

import io
import logging
import re
import time

from mint.tools import fastinput
from mint.core import jev
from mint.screen import ocr

log = logging.getLogger("mint.screen.pointer")

MAX_READS = 16


def _read(correct: bool = True):
    """Every line on the main display: [{text, candidate, box (Quartz points)}], area."""
    import Quartz
    import Vision
    from Foundation import NSData

    image, area = ocr._screen()
    small = image.convert("L").resize((64, 40))
    low, high = small.getextrema()
    if high - low < 12:
        from mint.screen.vision import Blind
        raise Blind()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = NSData.dataWithBytes_length_(buffer.getvalue(), len(buffer.getvalue()))
    cg = Quartz.CGImageSourceCreateImageAtIndex(Quartz.CGImageSourceCreateWithData(data, None), 0, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(correct)       # prose: documents and pages
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"text recognition failed: {error}")
    lines = []
    for observation in request.results() or []:
        top = observation.topCandidates_(1)
        if not top:
            continue
        text = str(top[0].string())
        if not text.strip():
            continue
        lines.append({"text": text, "candidate": top[0], "box": _to_points(observation.boundingBox(), area)})
    lines.sort(key=lambda l: (round(l["box"][1] / 6), l["box"][0]))
    return lines, area


def _to_points(box, area):
    return (area["left"] + box.origin.x * area["width"],
            area["top"] + (1 - box.origin.y - box.size.height) * area["height"],
            box.size.width * area["width"], box.size.height * area["height"])


def _range_box(line: dict, start: int, end: int, area) -> tuple:
    """The box of characters [start, end) of a line; the whole line if unavailable."""
    try:
        result = line["candidate"].boundingBoxForRange_error_((start, max(1, end - start)), None)
        observation = result[0] if isinstance(result, tuple) else result
        if observation is not None:
            return _to_points(observation.boundingBox(), area)
    except Exception:
        log.debug("sub-line box unavailable", exc_info=True)
    return line["box"]


def _inside(box, window) -> bool:
    if window is None:
        return True
    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    return window["x"] <= cx <= window["x"] + window["w"] and window["y"] <= cy <= window["y"] + window["h"]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def match(query: str, lines: list[dict], area, use_jev: bool = True) -> tuple[list[tuple], str]:
    """-> (boxes to mark, how it was found) or ([], '')."""
    wanted = _norm(query).strip(" .,:;\"'")
    if not wanted or not lines:
        return [], ""
    # 1. Exact words, possibly running across consecutive lines.
    for size in range(1, 5):
        for i in range(len(lines) - size + 1):
            group = lines[i:i + size]
            joined, spans, pos = "", [], 0
            for line in group:
                text = line["text"].lower()
                spans.append((pos, pos + len(text)))
                joined += text + " "
                pos += len(text) + 1
            at = joined.find(wanted)            # positions line up with `spans`
            if at < 0:
                continue
            if size == 1:
                return [_range_box(group[0], at, at + len(wanted), area)], "exact words"
            # Map the match back onto each line it touches.
            end = at + len(wanted)
            boxes = []
            for line, (a, b) in zip(group, spans):
                lo, hi = max(at, a), min(end, b)
                if lo < hi:
                    boxes.append(_range_box(line, lo - a, hi - a, area))
            if boxes:
                return boxes, f"exact words across {len(boxes)} lines"
    # 2. Most of the words, in one or a few lines.
    words = [w for w in re.findall(r"[a-z0-9]+", wanted) if len(w) > 2] or re.findall(r"[a-z0-9]+", wanted)
    best, best_score = None, 0.0
    for size in range(1, 4):
        for i in range(len(lines) - size + 1):
            blob = " ".join(l["text"].lower() for l in lines[i:i + size])
            have = set(re.findall(r"[a-z0-9]+", blob))
            # Stems count: "refund" matches "refunds", "explain" matches "explained".
            score = sum(1 for w in words if w in have or (len(w) >= 4 and any(
                h.startswith(w) or (len(h) >= 4 and w.startswith(h)) for h in have))) / len(words) - 0.05 * (size - 1)
            if score > best_score:
                best, best_score = lines[i:i + size], score
    if best and best_score >= 0.75:
        return [l["box"] for l in best], f"most of the words ({best_score:.2f})"
    # 3. Meaning: Jev picks the line from the real list, or none.
    if use_jev and len(words) >= 2:
        options = {str(i): l["text"][:200] for i, l in enumerate(lines[:250])}
        pick = jev.choose(query, options, instructions=(
            "Which line of text on the user's screen is the thing they asked to be shown "
            "(`request`) - the line that states it or answers it? Choose none if none does."))
        if pick is not None and pick.sure:
            return [lines[int(pick.id)]["box"]], f"Jev matched the meaning ({pick.confidence:.2f})"
    return [], ""


def _bring(app_name: str) -> str:
    """Bring the named app forward; '' if done, else why not."""
    import AppKit

    from mint.tools import appfinder
    from mint.screen import axkit
    name, why = appfinder.resolve(app_name)
    if name is None:
        return f"FAILED: no app called '{app_name}' ({why}); nothing was marked."
    running = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                    if (a.localizedName() or "").lower() == name.lower()), None)
    if running is None:
        return f"FAILED: {name} is not open, so there is nothing in it to show; nothing was marked."
    if not axkit.bring_to_front(running):
        return f"FAILED: could not bring {name} to the front; nothing was marked."
    time.sleep(0.35)
    return ""


def show(query: str, style: str = "box", note: str = "", scroll: bool = True, seconds: float = 10.0,
         app: str = "") -> str:
    """Find `query` in the front window (or `app`'s, brought forward) and mark it."""
    from mint.ui.marks import marks

    if app:
        # In testing, "find it in this PDF" was run while another app was in front,
        # and the same words were found - and boxed - in that other app.
        problem = _bring(app)
        if problem:
            return problem
    if not fastinput.has_accessibility() and scroll:
        scroll = False
    window = ocr._front_window()
    where = f" in {window['app']}" if window and window.get("app") else ""
    moved, direction, reads, last_seen = 0, "down", 0, None
    while reads < MAX_READS:
        reads += 1
        try:
            lines, area = _read()
        except Exception as error:
            if type(error).__name__ == "Blind":
                return ("FAILED: cannot see the screen - Mint lacks Screen Recording permission. Tell the "
                        "user to allow it in System Settings.")
            return f"FAILED: could not read the screen: {error}"
        scoped = [l for l in lines if _inside(l["box"], window)] or lines
        boxes, how = match(query, scoped, area, use_jev=(reads == 1 or not scroll))
        if boxes and moved:
            # Smooth scrolling keeps gliding after the read: in testing, an
            # underline landed one line below its words. Let it settle, measure again.
            time.sleep(0.6)
            try:
                again, area = _read()
                again = [l for l in again if _inside(l["box"], window)] or again
                settled, _ = match(query, again, area, use_jev=False)
                boxes = settled or boxes
            except Exception:
                pass
        if boxes and window and scroll:
            # Found hard against an edge (under a toolbar, or cut off at the
            # bottom): bring it a little further in, then measure again.
            top_gap = boxes[0][1] - window["y"]
            bottom_gap = window["y"] + window["h"] - (boxes[-1][1] + boxes[-1][3])
            nudge = "up" if top_gap < 110 else ("down" if bottom_gap < 70 else "")
            if nudge:
                fastinput.scroll(nudge, 2)
                time.sleep(0.6)
                try:
                    again, area = _read()
                    again = [l for l in again if _inside(l["box"], window)] or again
                    boxes = match(query, again, area, use_jev=False)[0] or boxes
                except Exception:
                    pass
        if boxes:
            marks.show(boxes, style, note, seconds)
            _visit(boxes[0])
            scrolled = f", after scrolling {direction} {moved} time{'s' if moved != 1 else ''}" if moved else ""
            return (f"Showing '{query}'{where} with a {style} ({how}{scrolled})."
                    + (f" Note shown: '{note}'." if note else "") + " It stays for about 10 seconds.")
        if not scroll:
            break
        seen = tuple(l["text"] for l in scoped[:40])
        if seen == last_seen:                       # nothing moved: the end of the document
            if direction == "up":
                break
            direction, moved = "up", 0
        last_seen = seen
        fastinput.scroll(direction, 5)
        moved += 1
        time.sleep(0.55)
    visible = "; ".join(l["text"][:40] for l in scoped[:8])
    return (f"FAILED: could not find '{query}'{where}" + (" even after scrolling the whole way" if scroll else "")
            + f". Nothing was marked. Visible text includes: {visible}")


def mark_area(x: float, y: float, w: float, h: float, style: str = "box", note: str = "",
              seconds: float = 10.0) -> str:
    """Mark a region given in 0-1000 coordinates of the latest look() screenshot."""
    from mint.screen import vision
    from mint.ui.marks import marks

    area = vision._last_area
    if area is None:
        return "Call look first; mark_area uses coordinates from the latest screenshot."
    box = (area["left"] + x / 1000 * area["width"], area["top"] + y / 1000 * area["height"],
           max(8.0, w / 1000 * area["width"]), max(8.0, h / 1000 * area["height"]))
    marks.show([box], style, note, seconds)
    _visit(box)
    return f"Marked that area with a {style}" + (f" and the note '{note}'" if note else "") + "."


def _visit(box) -> None:
    """The orb flies over beside the mark, looks at it, and goes home later."""
    try:
        from mint.ui.motion import motion
        motion.visit_quartz(box)
    except Exception:
        log.debug("orb visit failed", exc_info=True)
