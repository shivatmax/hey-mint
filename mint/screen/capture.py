"""Screenshots of one app's window, not of whatever happens to be on top of it.

Grounding used to grab a screen rectangle (mss), so a window lying over the target - another app, a
notification, Mint's own notch or orb - ended up in the picture the vision model numbered. Here the
picture is made from the target's own windows by their window-server ids (cua-driver's capture.rs):

  1. In process, CGWindowListCreateImageFromArray over the target window plus the same app's windows
     in front of it (an open sheet, popover or menu is a window of its own). Other apps are left out.
  2. ScreenCaptureKit (SCScreenshotManager) when its Python bridge is installed - single window only.
  3. `screencapture -l <id> -x -o` into a private temp file - single window only.
  4. The old screen-rectangle grab, marked method "region" (overlapping windows may show).

Every window picture passes a pixel-frame check before anyone maps a pixel back to the screen: its
size divided by the area it claims to show must be ~1x or ~2x (+-0.03) on both axes, else it is
refused as a capture/window mismatch (cua's px_frame.rs). A shot is a dict:
  {"image": PIL RGB, "scale": px per point, "bounds": (x, y, w, h) in screen points,
   "window_id": int|None, "method": "window"|"sck"|"screencapture"|"region"}
`to_points` maps a pixel of it back to the screen; `crop` and `fit` make zoomed / smaller shots
that still map back (for re-grounding a small target).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

log = logging.getLogger("mint.screen.capture")

SCALE_SLACK = 0.03


class CaptureError(RuntimeError):
    """No usable picture of the window (closed, off screen, or refused)."""


class FrameMismatch(CaptureError):
    """The picture is not a 1x/2x rendering of the area it claims to show."""


def frame_scale(px_w: int, px_h: int, bounds) -> float:
    """Pixels per point of a picture of `bounds`: 1.0 or 2.0, or FrameMismatch."""
    _, _, w, h = bounds
    if w <= 0 or h <= 0 or px_w <= 0 or px_h <= 0:
        raise FrameMismatch(f"capture/window mismatch: empty frame {px_w}x{px_h} px for {w}x{h} pt")
    sx, sy = px_w / w, px_h / h
    nearest = 1.0 if abs(sx - 1.0) <= abs(sx - 2.0) else 2.0
    if abs(sx - sy) > SCALE_SLACK or abs(sx - nearest) > SCALE_SLACK or abs(sy - nearest) > SCALE_SLACK:
        raise FrameMismatch(f"capture/window mismatch: {px_w}x{px_h} px for {w:g}x{h:g} pt "
                            f"(scale {sx:.3f} x {sy:.3f}, expected 1x or 2x)")
    return nearest


def _shot(image, bounds, window_id, method) -> dict:
    scale = frame_scale(image.size[0], image.size[1], bounds)
    return {"image": image, "scale": scale, "bounds": tuple(float(v) for v in bounds),
            "window_id": window_id, "method": method}


def to_points(shot: dict, px: float, py: float) -> tuple[float, float]:
    """A pixel of the shot's image -> Quartz screen points."""
    x, y, _, _ = shot["bounds"]
    return x + px / shot["scale"], y + py / shot["scale"]


def to_pixels(shot: dict, x: float, y: float) -> tuple[float, float]:
    bx, by, _, _ = shot["bounds"]
    return (x - bx) * shot["scale"], (y - by) * shot["scale"]


def crop(shot: dict, box, pad: float = 0.2) -> dict:
    """The part of a shot around `box` (screen points), padded by `pad` of its size on each side."""
    x, y, w, h = box
    bx, by, bw, bh = shot["bounds"]
    left, top = max(bx, x - w * pad), max(by, y - h * pad)
    right, bottom = min(bx + bw, x + w * (1 + pad)), min(by + bh, y + h * (1 + pad))
    if right <= left or bottom <= top:
        raise CaptureError("the box is outside the picture")
    s = shot["scale"]
    px = [round((left - bx) * s), round((top - by) * s), round((right - bx) * s), round((bottom - by) * s)]
    image = shot["image"].crop(px)
    bounds = (bx + px[0] / s, by + px[1] / s, (px[2] - px[0]) / s, (px[3] - px[1]) / s)
    return {**shot, "image": image, "bounds": bounds}


def fit(shot: dict, max_side: int) -> dict:
    """The shot resized so its longer side is at most (or, for zooming, exactly) `max_side` pixels;
    "scale" follows, so to_points still lands on the same screen point."""
    image = shot["image"]
    ratio = max_side / max(image.size)
    if abs(ratio - 1) < 1e-3:
        return shot
    size = (max(1, round(image.size[0] * ratio)), max(1, round(image.size[1] * ratio)))
    return {**shot, "image": image.resize(size), "scale": shot["scale"] * size[0] / image.size[0]}


# --- the window server's view -------------------------------------------------------------------

def window_info(window_id: int) -> dict | None:
    import Quartz
    try:
        rows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, int(window_id)) or []
    except Exception:
        return None
    return dict(rows[0]) if rows else None


def window_bounds(window_id: int) -> tuple | None:
    info = window_info(window_id)
    b = (info or {}).get("kCGWindowBounds") or {}
    if not b or b.get("Width", 0) <= 0 or b.get("Height", 0) <= 0:
        return None
    return (float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"]))


def _rows_front_to_back() -> list[dict]:
    import Quartz
    try:
        return list(Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or [])
    except Exception:
        return []


def stack_for(window_id: int, area=None, rows: list[dict] | None = None) -> list[int]:
    """The window plus the same app's on-screen windows in front of it that overlap `area` (its sheets,
    popovers and menus), front to back. Other apps' windows - Mint's included - never."""
    rows = _rows_front_to_back() if rows is None else rows
    index = next((i for i, r in enumerate(rows) if int(r.get("kCGWindowNumber", 0)) == int(window_id)), None)
    if index is None:
        return [int(window_id)]
    pid = int(rows[index].get("kCGWindowOwnerPID", -1))
    area = area or window_bounds(window_id)
    ids = []
    for r in rows[:index]:
        b = r.get("kCGWindowBounds") or {}
        if int(r.get("kCGWindowOwnerPID", -2)) != pid or not b or float(r.get("kCGWindowAlpha", 1) or 0) <= 0:
            continue
        if area and not _overlaps((b["X"], b["Y"], b["Width"], b["Height"]), area):
            continue
        ids.append(int(r["kCGWindowNumber"]))
    return ids + [int(window_id)]


def _overlaps(a, b) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


# --- the ways to take the picture ---------------------------------------------------------------

def _from_cgimage(image):
    """A CGImage (32-bit BGRA, as the window server makes them) -> PIL RGB, or None when it is empty or
    fully transparent (what a window we may not record looks like)."""
    import PIL.Image
    import Quartz
    if image is None:
        return None
    w, h = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
    if not w or not h or Quartz.CGImageGetBitsPerPixel(image) != 32:
        return None
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
    rgba = PIL.Image.frombuffer("RGBA", (w, h), bytes(data), "raw", "BGRA",
                                Quartz.CGImageGetBytesPerRow(image), 1)
    if rgba.getchannel("A").getbbox() is None:
        return None
    return rgba.convert("RGB")


def _cg(ids: list[int], area):
    import Quartz
    rect = Quartz.CGRectMake(*area)
    image = Quartz.CGWindowListCreateImageFromArray(
        rect, ids, Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution)
    return _from_cgimage(image)


def _sck(window_id: int, timeout: float = 3.0):
    """ScreenCaptureKit, when pyobjc-framework-ScreenCaptureKit is installed; else None."""
    try:
        import ScreenCaptureKit as SCK
    except ImportError:
        return None
    if threading.current_thread() is threading.main_thread():
        return None          # the completion handlers are waited for; never block the UI thread
    box, done = {}, threading.Event()

    def got_content(content, error):
        box["content"] = content
        done.set()
    SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        True, False, got_content)
    if not done.wait(timeout) or box.get("content") is None:
        return None
    window = next((w for w in box["content"].windows() if int(w.windowID()) == int(window_id)), None)
    if window is None:
        return None
    flt = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
    scale = float(flt.pointPixelScale()) if hasattr(flt, "pointPixelScale") else 2.0
    frame = window.frame()
    config = SCK.SCStreamConfiguration.alloc().init()
    config.setWidth_(round(frame.size.width * scale))
    config.setHeight_(round(frame.size.height * scale))
    config.setShowsCursor_(False)
    box.clear()
    done.clear()

    def got_image(image, error):
        box["image"] = image
        done.set()
    SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(flt, config, got_image)
    if not done.wait(timeout):
        return None
    return _from_cgimage(box.get("image"))


def _screencapture(window_id: int, timeout: float = 5.0):
    """The `screencapture` tool, into a private temp dir that is removed again."""
    import PIL.Image
    folder = tempfile.mkdtemp(prefix="mint-cap-")
    path = os.path.join(folder, "w.png")
    try:
        done = subprocess.run(["/usr/sbin/screencapture", "-l", str(int(window_id)), "-x", "-o", path],
                              capture_output=True, timeout=timeout)
        if done.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
            return None
        with PIL.Image.open(path) as image:
            image.load()
            if image.mode == "RGBA" and image.getchannel("A").getbbox() is None:
                return None
            return image.convert("RGB")
    except Exception:
        log.debug("screencapture", exc_info=True)
        return None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _region(area) -> dict:
    import mss
    import PIL.Image
    grabber = getattr(mss, "MSS", None) or mss.mss
    x, y, w, h = area
    with grabber() as sct:
        shot = sct.grab({"left": int(x), "top": int(y), "width": int(w), "height": int(h)})
        image = PIL.Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    bounds = (int(x), int(y), int(w), int(h))
    try:
        return _shot(image, bounds, None, "region")
    except FrameMismatch:
        # A rectangle across two displays of different scales: keep the old measured scale.
        return {"image": image, "scale": image.size[0] / max(w, 1), "bounds": bounds, "window_id": None,
                "method": "region"}


def window(window_id: int) -> dict:
    """A picture of exactly one window (no other window, Mint's notch and orb included), pixel-checked.
    Raises CaptureError / FrameMismatch."""
    last = None
    # Twice: a window that was just opened, unhidden or brought from another Space can give no picture for a moment
    # (seen 9 Oct: "could not capture window 3783" for Telegram a second after open_app, and Mint was blind).
    for attempt in range(2):
        bounds = window_bounds(window_id)
        if bounds is None:
            if attempt == 0:
                time.sleep(0.6)
                continue
            raise CaptureError(f"window {window_id} is not on screen")
        for method, take in (("window", lambda: _cg([int(window_id)], bounds)), ("sck", lambda: _sck(window_id)),
                             ("screencapture", lambda: _screencapture(window_id))):
            try:
                image = take()
            except Exception as error:
                log.debug("capture %s failed: %s", method, error)
                image = None
            if image is None:
                continue
            try:
                return _shot(image, bounds, int(window_id), method)
            except FrameMismatch as error:
                last = error
                log.info("%s (%s)", error, method)
        if attempt == 0:
            time.sleep(0.6)
    raise last or CaptureError(f"could not capture window {window_id}")


def area(area, window_id: int | None = None) -> dict:
    """A picture of a screen area as the target app draws it: its window (and that app's sheets and
    popovers in front of it) when `window_id` is known, else - or when that fails - the screen grab."""
    area = tuple(float(v) for v in area)
    if window_id:
        ids = stack_for(window_id, area)
        try:
            image = _cg(ids, area)
            if image is not None:
                return _shot(image, area, int(window_id), "window")
        except FrameMismatch as error:
            log.info("%s (window %s)", error, window_id)
        except Exception:
            log.debug("window capture", exc_info=True)
        clipped = _clip(area, window_bounds(window_id))
        if len(ids) == 1 and all(abs(a - b) <= 1 for a, b in zip(clipped, area)):
            # Nothing of the app's in front and the area is inside the window: one-window tools will do.
            try:
                return crop(window(window_id), clipped, pad=0)
            except CaptureError as error:
                log.info("window %s: %s; grabbing the screen area", window_id, error)
    return _region(area)


def _clip(box, bounds):
    if bounds is None:
        return box
    x0, y0 = max(box[0], bounds[0]), max(box[1], bounds[1])
    x1 = min(box[0] + box[2], bounds[0] + bounds[2])
    y1 = min(box[1] + box[3], bounds[1] + bounds[3])
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))
