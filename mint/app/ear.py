"""Mint Ear: the hand-over from Mint.app's launcher, and unloading when idle.

Mint.app's main executable (launcher/main.swift) is Mint Ear. It starts this
app as a child, and when this app has been asleep and idle for a while
(Settings: unload_after_minutes, default 20) this app unloads itself: it takes a
picture of the orb for the Ear to show, folds the conversation into memory and
exits with UNLOADED. Idle, only the Ear runs (~30 MB against ~260 MB): it
listens for "Hey Mint" with the same detector and keeps the menu-bar glyph, the
orb and ⌘J.

When the Ear starts this app again it hands over over a Unix socket
($MINT_EAR_SOCKET): a header line, the last 3 s of microphone audio before the
wake word ("pre" bytes, 16 kHz int16 mono), then live audio until this app's own
microphone has caught up. The session replays it through the normal asleep
path - the same wake word model, the voice lock, the phrase cut - so "Hey Mint,
open Slack" said while the app is starting is heard in full.

    START reason=wake lag=4160 pre=96000\\n<pre bytes><live bytes>...
    reasons: launch (app opened), wake, console (⌘J / orb / menu), settings

This app answers READY (its orb and menu are up: the Ear hides its own) and
STOP (its microphone runs: the Ear stops listening).

The heartbeat: while this app runs, a timer on the MAIN thread writes
"<pid> <count> <allow>" to $MINT_HEARTBEAT every 2 s. Mint is an accessory app
(no Dock icon, not in Force Quit), so when its main thread hung nothing could
stop it but restarting the Mac. Now the Ear watches the count: no change for
10 s and it shows a "Mint isn't responding" menu-bar item (Restart / Quit);
none for `allow` seconds (30) and it asks for every thread's stack (SIGUSR1,
into mint.log), then stops this process and starts it again. In-process,
`watch_main_thread` logs the main thread's stack once it has been stuck for
10 s, so even a hang that clears up on its own says where it was.
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import threading

log = logging.getLogger("mint.app.ear")

UNLOADED = 75


def managed() -> bool:
    """Started by Mint Ear (Mint.app), so unloading hands back to it."""
    return bool(os.environ.get("MINT_EAR_SOCKET"))


class Link:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self.reason, self.lag, self.pre = "none", None, 0
        self.chunks: queue.Queue = queue.Queue()      # ("pre" | "live", bytes); None at the end
        self.closed = False
        header = self._line()
        for field in header.split()[1:]:
            key, _, value = field.partition("=")
            if key == "reason":
                self.reason = value
            elif key == "lag":
                self.lag = int(value) if value.lstrip("-").isdigit() and int(value) >= 0 else None
            elif key == "pre":
                self.pre = int(value) if value.isdigit() else 0
        threading.Thread(target=self._read, name="mint-ear", daemon=True).start()

    def _line(self) -> str:
        data = b""
        while not data.endswith(b"\n") and len(data) < 256:
            byte = self._sock.recv(1)
            if not byte:
                break
            data += byte
        return data.decode("utf-8", "replace").strip()

    def _read(self) -> None:
        left, carry = self.pre, b""
        try:
            while True:
                data = self._sock.recv(65536)
                if not data:
                    break
                data = carry + data
                even = len(data) - len(data) % 2
                data, carry = data[:even], data[even:]
                if left > 0:
                    head, data = data[:left], data[left:]
                    left -= len(head)
                    if head:
                        self.chunks.put(("pre", head))
                if data:
                    self.chunks.put(("live", data))
        except OSError:
            pass
        self.closed = True
        self.chunks.put(None)

    def send(self, message: str) -> None:
        try:
            self._sock.sendall((message + "\n").encode())
        except OSError:
            log.debug("the Ear is gone; %s not sent", message)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


_link: "Link | None" = None


def defer_audio() -> bool:
    """A wake word hand-over is under way: keep the microphone free for the Ear
    until the user pauses (see VoiceAudio's `defer`)."""
    return _link is not None and _link.reason == "wake"


def connect() -> Link | None:
    global _link
    path = os.environ.get("MINT_EAR_SOCKET")
    if not path:
        return None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(path)
        link = Link(sock)
        sock.settimeout(None)
        _link = link
        log.info("Mint Ear hand-over: %s (pre-roll %.1f s)", link.reason, link.pre / 32000)
        return link
    except OSError as error:
        log.info("no Mint Ear hand-over: %s", error)
        return None


# --- the heartbeat: the Ear's watchdog -----------------------------------------------

BEAT_EVERY = 2.0          # seconds between beats (a main-thread timer)
ALLOW = 30                # no beat for this long: the Ear calls Mint hung (the default it is told)
STUCK_LOG = 10.0          # the in-process watcher logs the main thread's stack after this long

_beat = {"fd": None, "count": 0, "allow": ALLOW, "at": 0.0, "timer": None, "target": None}


def _write_beat() -> None:
    fd = _beat["fd"]
    if fd is None:
        return
    line = f"{os.getpid()} {_beat['count']} {int(_beat['allow'])}\n".ljust(40).encode()
    try:
        os.pwrite(fd, line, 0)            # same bytes, same place: no new file, nothing to tidy
    except OSError:
        log.debug("heartbeat write failed", exc_info=True)


def beat() -> None:
    """Main thread (the timer): the main thread is alive."""
    import time
    _beat["count"] += 1
    _beat["at"] = time.monotonic()
    _write_beat()


def grace(seconds: float) -> None:
    """About to keep the main thread busy on purpose (quitting folds the conversation into memory):
    let the Ear wait up to `seconds` before calling Mint hung. Any thread."""
    _beat["allow"] = max(ALLOW, int(seconds))
    _beat["count"] += 1
    _write_beat()


def start_heartbeat() -> bool:
    """Main thread, at the end of start-up (the run loop starts next). The file is written only for
    the Ear that asked for it ($MINT_HEARTBEAT, set by Mint.app); a run from a terminal has no
    watchdog, only the stuck log. -> True when the Ear gets beats."""
    import time
    if _beat["timer"] is not None:
        return _beat["fd"] is not None
    path = os.environ.get("MINT_HEARTBEAT")
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _beat["fd"] = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError as error:
            log.warning("heartbeat: %s", error)
    import AppKit

    class _MintHeartbeat(AppKit.NSObject):
        def tick_(self, _timer):
            beat()

    target = _MintHeartbeat.alloc().init()
    timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        BEAT_EVERY, target, "tick:", None, True)
    timer.setTolerance_(0.5)
    # Common modes: it beats during a menu, a drag or an alert too - only a blocked main thread stops it.
    AppKit.NSRunLoop.mainRunLoop().addTimer_forMode_(timer, AppKit.NSRunLoopCommonModes)
    _beat["timer"], _beat["target"] = timer, target
    _beat["at"] = time.monotonic()
    beat()
    watch_main_thread()
    if _beat["fd"] is not None:
        log.info("heartbeat: %s every %.0f s", path, BEAT_EVERY)
    return _beat["fd"] is not None


def watch_main_thread() -> None:
    """A small thread that notices when the main thread stops beating and writes its stack to the log
    (once per stall, then once more when it recovers). The main thread often only looks hung - a
    long call to another app, a lock - and the stack says which; a hard hang is the Ear's job."""
    import sys
    import time
    import traceback

    if _beat.get("watching"):
        return
    _beat["watching"] = True
    main = threading.main_thread().ident

    def run():
        stalled_since = 0.0
        while True:
            time.sleep(BEAT_EVERY)
            quiet = time.monotonic() - _beat["at"]
            if quiet >= STUCK_LOG and not stalled_since:
                stalled_since = _beat["at"]
                frame = sys._current_frames().get(main)
                stack = "".join(traceback.format_stack(frame)) if frame is not None else "  (no Python frame)\n"
                log.warning("main thread stuck for %.0f s - the windows, menu and orb don't respond. "
                            "It is here:\n%s", quiet, stack)
                print(f"  [main thread stuck for {quiet:.0f} s - stack in the log]", flush=True)
            elif stalled_since and _beat["at"] > stalled_since:
                log.warning("main thread responding again after %.0f s", _beat["at"] - stalled_since)
                stalled_since = 0.0
    threading.Thread(target=run, daemon=True, name="mint-main-watch").start()


# --- the orb, for the Ear to show while this app is unloaded ------------------------

def snapshot_orb(presence, timeout: float = 3.0) -> bool:
    """Save a picture of the orb as it is now (asleep) and where it is, to
    ear/orb.png + ear/orb.json. Any thread; the work happens on the main thread."""
    import json
    import threading as _threading

    from PyObjCTools import AppHelper

    from mint.core import config
    hud = getattr(presence, "hud", None)
    window = getattr(hud, "_orb_window", None)
    if window is None:
        return False
    folder = config.PROJECT_ROOT / "ear"
    done, result = _threading.Event(), [False]

    def work():
        try:
            png = _capture(window)
            if png:
                folder.mkdir(exist_ok=True)
                (folder / "orb.png").write_bytes(png)
                frame = window.frame()
                from mint.ui.hud import D
                (folder / "orb.json").write_text(json.dumps({
                    "x": frame.origin.x, "y": frame.origin.y, "size": frame.size.width, "diameter": D}))
                result[0] = True
        except Exception:
            log.exception("orb picture failed")
        finally:
            done.set()
    AppHelper.callAfter(work)
    done.wait(timeout)
    return result[0]


def _visible(rep) -> bool:
    """Not an empty picture: some pixel near the middle is opaque."""
    w, h = rep.pixelsWide(), rep.pixelsHigh()
    for dx, dy in ((0, 0), (0.1, 0), (-0.1, 0), (0, 0.1), (0, -0.1)):
        color = rep.colorAtX_y_(int(w * (0.5 + dx)), int(h * (0.5 + dy)))
        if color is not None and color.alphaComponent() > 0.2:
            return True
    return False


def _capture(window) -> bytes | None:
    import AppKit
    # 1. What is on screen, glow included. The orb is kept out of screen
    # captures (sharingType none); allow this one capture, then restore.
    try:
        import Quartz
        old = window.sharingType()
        window.setSharingType_(AppKit.NSWindowSharingReadOnly)
        try:
            image = Quartz.CGWindowListCreateImage(
                Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, window.windowNumber(),
                Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution)
        finally:
            window.setSharingType_(old)
        if image is not None:
            rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
            if rep is not None and _visible(rep):
                return bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}))
    except Exception:
        log.debug("window capture failed", exc_info=True)
    # 2. The orb's layers drawn into a bitmap (no blur filters, otherwise the same).
    view = window.contentView()
    layer = view.layer() if view is not None else None
    if layer is None:
        return None
    scale = window.backingScaleFactor() or 2.0
    size = int(window.frame().size.width * scale)
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    import Quartz
    cg = context.CGContext()
    Quartz.CGContextScaleCTM(cg, scale, scale)
    layer.renderInContext_(cg)
    return bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})) if _visible(rep) else None
