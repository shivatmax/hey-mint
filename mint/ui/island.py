"""Mint's island: the orb turns into whatever is going on, like the Dynamic Island.

When something is happening that you can act on, the orb fades into a small black capsule that
springs out of its spot. A tiny live Mint - the same swirl and eyes, blinking, following the
pointer - stays at the end where the orb was, so it is still Mint, only reshaped. When it is over
the capsule springs back into the orb.

    meeting, on a call   [• ● rec | 🎙 | 🎥 | ×]         record; mic and video on or off
    meeting, recording   [• ● 12:34 ▁▃▅▃▂ ■]            the face bobs with the call's sound
    meeting, after       [• ◌ Notes 3/10] -> [• ✓ Notes ready ↗]
    screen recording     [• ● 0:12 · screen ■]  /  [• Record this area? ✓ ✕]
    teaching a skill     [• ● 0:42 (4 steps) ⏸ ✓ ✕] -> Saving the skill -> ✓ Learned · <title>
    watching a video     a card: the title, keyframes popping in under a sweeping scan line, the step
    a lesson (tutor)     [• Step 2 of 5 · Click Export  → ✕]
    briefing, calendar   a card: the day, the weather, a timeline of today (now lit, past dimmed),
                         reminders, headlines

Each scene is a small object (size, build, tick, mood); providers look at Mint's state ten times
a second and the highest-priority scene wins. Scenes that are pushed (the schedule card) expire by
themselves. Every scene gets a small × (brighter while the pointer is on the island): it only hides
the scene - a pushed card goes, a live one (a recording, a lesson) keeps running out of sight until it
changes. The island never shows in screenshots or screen shares, and is click-through everywhere but
the capsule itself. Main thread only, except the show_* entry points.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx

log = logging.getLogger("mint.ui.island")

ROW = 40                   # a compact capsule's height
FACE = 24                  # the little Mint's diameter
ORB = 44                   # the real orb, what the island grows out of
CLOSE_SLOT = 34            # the × at a compact capsule's far end (the face's end holds the face)
CARD_RADIUS = 24
INK = (1.0, 1.0, 1.0)
DIM = (0.62, 0.63, 0.68)
RED = (1.0, 0.27, 0.23)
GREEN = (0.19, 0.82, 0.35)
BLUE = (0.04, 0.52, 1.0)
ORANGE = (1.0, 0.62, 0.04)
PURPLE = (0.75, 0.36, 0.95)
TEAL = (0.25, 0.8, 0.85)
SURFACE = (0.035, 0.035, 0.045)


# --- small helpers ------------------------------------------------------------------------------

def _cg(rgb, alpha: float = 1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _ns(rgb, alpha: float = 1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _font(size: float, weight: str = "semibold", mono: bool = False, rounded: bool = False):
    w = {"regular": AppKit.NSFontWeightRegular, "medium": AppKit.NSFontWeightMedium,
         "semibold": AppKit.NSFontWeightSemibold, "bold": AppKit.NSFontWeightBold}[weight]
    if mono:
        return AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, w)
    font = AppKit.NSFont.systemFontOfSize_weight_(size, w)
    if rounded:
        descriptor = font.fontDescriptor().fontDescriptorWithDesign_(AppKit.NSFontDescriptorSystemDesignRounded)
        if descriptor is not None:
            font = AppKit.NSFont.fontWithDescriptor_size_(descriptor, size) or font
    return font


def _stamp(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _notify_mint(text: str) -> None:
    try:
        from mint.tools.work import _notify_mint as tell
        tell(text)
    except Exception as error:
        log.info("notify: %s", error)


def _later(fn) -> None:
    """Run slow work (starting a recorder, saving a skill) off the main thread."""
    threading.Thread(target=fn, daemon=True, name="island-action").start()


class _IslandAct(AppKit.NSObject):
    """An NSButton target that calls a Python function."""

    def initWithFn_(self, fn):
        self = objc.super(_IslandAct, self).init()
        if self is None:
            return None
        self.fn = fn
        return self

    def fire_(self, sender):
        try:
            self.fn()
        except Exception:
            log.exception("island button")


class _IslandButton(AppKit.NSButton):
    """Takes the first click: the island never becomes the key window."""

    def acceptsFirstMouse_(self, event):
        return True


class _IslandRoot(AppKit.NSView):
    """The island's root view: files, pictures or text dropped on the capsule go to Mint."""

    def draggingEntered_(self, sender):
        island.drop_hover(True)
        return AppKit.NSDragOperationCopy

    def draggingExited_(self, sender):
        island.drop_hover(False)

    def prepareForDragOperation_(self, sender):
        return island.rect is not None and island._inside(AppKit.NSEvent.mouseLocation())

    def performDragOperation_(self, sender):
        board = sender.draggingPasteboard()
        urls = board.readObjectsForClasses_options_([AppKit.NSURL],
                                                    {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}) or []
        paths = [str(u.path()) for u in urls if u.path()]
        text = "" if paths else str(board.stringForType_(AppKit.NSPasteboardTypeString) or "")
        island.dropped(paths, text)
        return bool(paths or text.strip())


class _IslandTicker(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(_IslandTicker, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def tick_(self, timer):
        self.owner.tick()


# --- scenes -------------------------------------------------------------------------------------

class Scene:
    """One thing the island can be. Subclasses set key/priority and fill in size/build/tick."""

    key = "scene"
    priority = 0
    card = False                 # a card: rounded rectangle, the face in its top corner

    def __init__(self) -> None:
        self._acts: list = []

    def signature(self) -> str:
        """When this changes the island rebuilds (and reshapes) the scene."""
        return self.key

    def size(self) -> tuple[float, float]:
        return (160, ROW)

    def build(self, view, width: float, height: float) -> None:
        """Add the scene's views to `view` (bottom-left origin). Compact scenes get the capsule minus
        the face's end; cards get the whole card and keep clear of the face's top corner."""

    def tick(self, now: float) -> None:
        pass

    def mood(self) -> str:
        return "awake"

    def level(self) -> float:
        return 0.0

    def clicked(self) -> None:
        """A click on the capsule, not on a button."""

    def closable(self) -> bool:
        """Whether the island adds its × (see Island.close_scene). False where the scene's own ✕
        already is the way out."""
        return True

    # building blocks
    def label(self, view, text, x, y, w, size=13, weight="semibold", rgb=INK, alpha=1.0, mono=False,
              rounded=False, h=None):
        field = AppKit.NSTextField.labelWithString_(text)
        field.setFont_(_font(size, weight, mono, rounded))
        field.setTextColor_(_ns(rgb, alpha))
        field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        field.setFrame_(AppKit.NSMakeRect(x, y, w, h or size + 5))
        view.addSubview_(field)
        return field

    def button(self, view, symbol, fn, x, y, rgb=INK, size=28, filled=False, tip="", symbol_size=12,
               background=None):
        act = _IslandAct.alloc().initWithFn_(fn)
        self._acts.append(act)
        button = _IslandButton.buttonWithImage_target_action_(gfx.symbol(symbol, symbol_size, "bold"), act, "fire:")
        button.setBordered_(False)
        button.setFrame_(AppKit.NSMakeRect(x, y, size, size))
        button.setToolTip_(tip)
        button.setWantsLayer_(True)
        button.layer().setCornerRadius_(size / 2)
        self.paint(button, rgb, filled, background)
        view.addSubview_(button)
        return button

    @staticmethod
    def paint(button, rgb, filled: bool, background=None) -> None:
        if filled:
            button.layer().setBackgroundColor_(_cg(rgb))
            button.setContentTintColor_(_ns(INK))
        else:
            button.layer().setBackgroundColor_(_cg(background or (1, 1, 1), 0.12 if background is None else 0.22))
            button.setContentTintColor_(_ns(rgb))

    @staticmethod
    def dot(view, x, y, d, rgb, blink: bool = False):
        layer = Quartz.CALayer.layer()
        layer.setFrame_(Quartz.CGRectMake(x, y, d, d))
        layer.setCornerRadius_(d / 2)
        layer.setBackgroundColor_(_cg(rgb))
        view.layer().addSublayer_(layer)
        if blink:
            anim = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            anim.setFromValue_(1.0)
            anim.setToValue_(0.2)
            anim.setDuration_(0.75)
            anim.setAutoreverses_(True)
            anim.setRepeatCount_(1e9)
            layer.addAnimation_forKey_(anim, "blink")
        return layer

    @staticmethod
    def spinner(view, x, y, rgb=INK):
        """A white arc turning (the system spinner is too dark on black)."""
        arc = Quartz.CAShapeLayer.layer()
        arc.setBounds_(Quartz.CGRectMake(0, 0, 16, 16))
        arc.setPosition_(Quartz.CGPointMake(x + 8, y + 8))
        arc.setPath_(Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(1.5, 1.5, 13, 13), None))
        arc.setFillColor_(None)
        arc.setStrokeColor_(_cg(rgb, 0.9))
        arc.setLineWidth_(2.2)
        arc.setLineCap_(Quartz.kCALineCapRound)
        arc.setStrokeEnd_(0.7)
        view.layer().addSublayer_(arc)
        turn = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
        turn.setFromValue_(0.0)
        turn.setToValue_(-2 * math.pi)
        turn.setDuration_(0.9)
        turn.setRepeatCount_(1e9)
        arc.addAnimation_forKey_(turn, "spin")
        return arc

    @staticmethod
    def breathe(button, rgb) -> None:
        """A soft glow pulsing round a button: "press me"."""
        layer = button.layer()
        layer.setShadowColor_(_cg(rgb))
        layer.setShadowRadius_(7)
        layer.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        layer.setMasksToBounds_(False)
        glow = Quartz.CABasicAnimation.animationWithKeyPath_("shadowOpacity")
        glow.setFromValue_(0.15)
        glow.setToValue_(0.95)
        glow.setDuration_(1.1)
        glow.setAutoreverses_(True)
        glow.setRepeatCount_(1e9)
        layer.addAnimation_forKey_(glow, "breathe")


class _Bars:
    """Five level bars that bounce with a sound level, with a little life of their own."""

    def __init__(self, view, x, cy, rgb=RED, count=5, gap=5, height=18) -> None:
        self.cy, self.height, self.values, self.layers = cy, height, [0.0] * count, []
        for i in range(count):
            bar = Quartz.CALayer.layer()
            bar.setCornerRadius_(1.5)
            bar.setBackgroundColor_(_cg(rgb, 0.95))
            bar.setFrame_(Quartz.CGRectMake(x + i * gap, cy - 2, 3, 4))
            view.layer().addSublayer_(bar)
            self.layers.append(bar)

    def tick(self, now: float, level: float) -> None:
        target = min(1.0, math.sqrt(max(0.0, level)) * 2.6)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.1)
        for i, bar in enumerate(self.layers):
            wobble = 0.5 + 0.5 * math.sin(now * 7 + i * 1.3)
            want = 0.1 + target * (0.5 + 0.5 * wobble)
            self.values[i] += (want - self.values[i]) * 0.45
            h = 3 + self.values[i] * self.height
            frame = bar.frame()
            bar.setFrame_(Quartz.CGRectMake(frame.origin.x, self.cy - h / 2, 3, h))
        Quartz.CATransaction.commit()


class MeetingScene(Scene):
    """On a call: record (mic and video switchable). Then the recording, the notes, "Notes ready"."""

    key = "meeting"
    DONE_FOR = 8.0

    def __init__(self) -> None:
        super().__init__()
        self.mode = ""
        self.mic, self.video = True, False
        self.dismissed_call = None
        self.done_until, self.done_folder = 0.0, ""
        self.starting_until = 0.0
        self.last_mode = ""

    # which state the meeting is in
    def current(self, now: float) -> str:
        from mint.tools import meetings
        from mint.core import prefs
        phase = meetings.phase()
        if phase:
            return phase
        if now < self.starting_until:
            return "starting"
        if meetings.processing():
            return "writing"
        if now < self.done_until:
            return "done"
        call = meetings.on_call()
        if call and call.get("since") != self.dismissed_call and prefs.get("meeting_offer"):
            return "offer"
        return ""

    def provide(self, now: float):
        mode = self.current(now)
        if mode != self.last_mode:
            from mint.tools import meetings
            log.debug("meeting scene %s -> %s (phase %r, processing %s, done in %.1f)", self.last_mode, mode,
                        meetings.phase(), list(meetings.processing().values()), self.done_until - now)
        if self.last_mode in ("writing", "stopping", "recording") and mode in ("", "offer"):
            self._notes_ready()
            mode = "done"
        self.last_mode = mode
        self.mode = mode
        self.priority = {"recording": 90, "starting": 88, "stopping": 88, "writing": 60, "done": 45,
                         "offer": 50}.get(mode, 0)
        return self if mode else None

    def _notes_ready(self) -> None:
        from mint.tools import meetings
        latest = meetings.list_meetings(1)
        self.done_folder = str(latest[0].get("folder") or "") if latest else ""
        self.done_until = time.time() + self.DONE_FOR

    def signature(self) -> str:
        if self.mode == "recording":
            from mint.tools import meetings
            return f"meeting:recording:{'video' if meetings.has_video() else 'audio'}"
        return f"meeting:{self.mode}"

    def size(self):
        return {"offer": (40 + 150, ROW), "recording": (40 + 154, ROW), "starting": (40 + 124, ROW),
                "stopping": (40 + 124, ROW), "writing": (40 + 140, ROW), "done": (40 + 146, ROW)}[self.mode]

    def mood(self) -> str:
        return {"writing": "thinking", "starting": "thinking", "stopping": "thinking"}.get(self.mode, "awake")

    def closable(self) -> bool:
        return self.mode != "offer"               # its own × ("Not this call") already closes it

    def level(self) -> float:
        if self.mode != "recording":
            return 0.0
        from mint.tools import meetings
        lv = meetings.levels()
        return min(0.5, max(float(lv.get("others") or 0), float(lv.get("you") or 0)) * 2)

    def build(self, view, width, height):
        from mint.tools import meetings
        mid = height / 2
        if self.mode == "offer":
            rec = self.button(view, "record.circle.fill", self.record, 6, mid - 14, RED, filled=True, size=28,
                              symbol_size=13, tip="Record this meeting: a transcript and notes after")
            self.breathe(rec, RED)
            self.mic_button = self.button(view, "mic.fill", self.toggle_mic, 44, mid - 13, size=26,
                                          tip="Your microphone")
            self.video_button = self.button(view, "video.fill", self.toggle_video, 76, mid - 13, size=26,
                                            tip="Also a video of the call's window, with sound")
            self._paint_toggles()
            self.button(view, "xmark", self.dismiss, 116, mid - 11, DIM, size=22, symbol_size=9, tip="Not this call")
        elif self.mode == "recording":
            self.dot(view, 8, mid - 4, 8, RED, blink=True)
            self.clock = self.label(view, _stamp(meetings.elapsed()), 22, mid - 9, 52, size=14, mono=True)
            self.bars = _Bars(view, 76, mid, RED)
            if meetings.has_video():
                icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("video.fill", 10))
                icon.setContentTintColor_(_ns(DIM))
                icon.setFrame_(AppKit.NSMakeRect(102, mid - 7, 16, 14))
                icon.setToolTip_("Recording video too")
                view.addSubview_(icon)
            self.button(view, "stop.fill", self.stop, 122, mid - 14, RED, filled=True, size=28, symbol_size=11,
                        tip="Stop and write the notes")
        elif self.mode in ("starting", "stopping"):
            self.spinner(view, 8, mid - 8)
            self.label(view, "Starting…" if self.mode == "starting" else "Saving…", 30, mid - 9, 90, size=13)
        elif self.mode == "writing":
            self.spinner(view, 8, mid - 8)
            self.progress = self.label(view, "Writing notes", 30, mid - 9, 108, size=13)
        elif self.mode == "done":
            self.label(view, "✓  Notes ready", 10, mid - 9, 100, size=13, rgb=GREEN)
            self.button(view, "arrow.up.forward", self.open_notes, 112, mid - 13, size=26, symbol_size=10,
                        tip="Open the notes")

    def tick(self, now):
        from mint.tools import meetings
        if self.mode == "recording" and hasattr(self, "clock"):
            self.clock.setStringValue_(_stamp(meetings.elapsed()))
            lv = meetings.levels()
            self.bars.tick(now, max(float(lv.get("others") or 0), float(lv.get("you") or 0)))
        elif self.mode == "writing" and hasattr(self, "progress"):
            what = next(iter(meetings.processing().values()), "")
            found = [p for p in what.split() if "/" in p]
            self.progress.setStringValue_(f"Notes · {found[0]}" if found else "Writing notes")

    def clicked(self) -> None:
        if self.mode == "done":
            self.open_notes()

    # actions
    def _paint_toggles(self) -> None:
        self.mic_button.setImage_(gfx.symbol("mic.fill" if self.mic else "mic.slash.fill", 11, "bold"))
        self.paint(self.mic_button, INK if self.mic else DIM, False, None if self.mic else (0.3, 0.3, 0.32))
        self.video_button.setImage_(gfx.symbol("video.fill" if self.video else "video.slash.fill", 11, "bold"))
        self.paint(self.video_button, BLUE if self.video else DIM, False, BLUE if self.video else (0.3, 0.3, 0.32))
        self.mic_button.setToolTip_("Your microphone: on" if self.mic else "Your microphone: off (only the call)")
        self.video_button.setToolTip_("Video: on (the call's window, with sound)" if self.video else "Video: off")

    def toggle_mic(self) -> None:
        self.mic = not self.mic
        self._paint_toggles()

    def toggle_video(self) -> None:
        self.video = not self.video
        self._paint_toggles()

    def record(self) -> None:
        from mint.tools import meetings
        self.starting_until = time.time() + 12
        video, mic = self.video, self.mic

        def run():
            result = meetings.start(video=video, mic=mic)
            print(f"  [meeting start] {result}", flush=True)
            self.starting_until = 0.0
        _later(run)

    def stop(self) -> None:
        from mint.tools import meetings

        def run():
            print(f"  [meeting stop] {meetings.stop()}", flush=True)
        _later(run)

    def dismiss(self) -> None:
        from mint.tools import meetings
        self.dismissed_call = meetings.on_call().get("since")

    def open_notes(self) -> None:
        folder = Path(self.done_folder) if self.done_folder else None
        if folder is not None:
            target = folder / "notes.md" if (folder / "notes.md").exists() else folder
            subprocess.run(["open", str(target)], check=False)
        self.done_until = 0.0


class ScreenScene(Scene):
    """A screen recording, or an area waiting for a yes."""

    key = "screen"

    def provide(self, now):
        from mint.tools import meetings
        from mint.tools import screenrec
        snap = screenrec.snapshot()
        if not snap or (snap.get("file") and meetings.has_video()):
            return None                                   # the meeting's own video: the meeting scene shows it
        self.snap = snap
        self.mode = "pending" if snap.get("pending") else "recording"
        self.priority = 85 if self.mode == "recording" else 70
        return self

    def signature(self):
        return f"screen:{self.mode}"

    def closable(self) -> bool:
        return self.mode != "pending"             # "Record this area?" is a question: its ✕ is the no

    def size(self):
        return (40 + 140, ROW) if self.mode == "recording" else (40 + 196, ROW)

    def build(self, view, width, height):
        mid = height / 2
        if self.mode == "recording":
            self.dot(view, 8, mid - 4, 8, RED, blink=True)
            self.clock = self.label(view, _stamp(self.snap.get("seconds", 0)), 22, mid - 9, 50, size=14, mono=True)
            icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("rectangle.dashed.badge.record", 12))
            icon.setContentTintColor_(_ns(DIM))
            icon.setFrame_(AppKit.NSMakeRect(74, mid - 8, 22, 16))
            icon.setToolTip_(self.snap.get("what", ""))
            view.addSubview_(icon)
            self.button(view, "stop.fill", self.stop, 110, mid - 14, RED, filled=True, size=28, symbol_size=11,
                        tip="Stop and save the recording")
        else:
            self.label(view, "Record this area?", 10, mid - 9, 124, size=13)
            self.button(view, "checkmark", self.confirm, 138, mid - 13, GREEN, filled=True, size=26, symbol_size=11,
                        tip="Yes, record it")
            self.button(view, "xmark", self.cancel, 170, mid - 13, DIM, size=26, symbol_size=10, tip="No")

    def tick(self, now):
        if self.mode == "recording" and hasattr(self, "clock"):
            self.clock.setStringValue_(_stamp(self.snap.get("seconds", 0)))

    def stop(self):
        from mint.tools import screenrec
        _later(lambda: _notify_mint("(The user clicked stop on the screen recording.) " + screenrec.stop()))

    def confirm(self):
        from mint.tools import screenrec
        _later(lambda: _notify_mint("(The user clicked yes on the area shown.) " + screenrec.confirm()))

    def cancel(self):
        from mint.tools import screenrec
        _later(lambda: _notify_mint("(The user clicked no on the area shown.) " + screenrec.cancel()))


class TeachScene(Scene):
    """Mint learning a task by watching: a small recorder - time, steps so far, pause, done, cancel -
    then "Saving the skill" and "Learned · <title>"."""

    key = "teach"
    priority = 80

    def provide(self, now):
        from mint.knowledge import teach
        self.snap = teach.snapshot()
        if not self.snap:
            return None
        self.mode = ("saved" if self.snap.get("saved") else "saving" if self.snap.get("saving")
                     else "paused" if self.snap.get("paused") else "recording")
        return self

    def signature(self):
        return f"teach:{self.mode}"

    def size(self):
        if self.mode == "saving":
            return (40 + 150, ROW)
        if self.mode == "saved":
            title = self.snap.get("saved", "")
            return (40 + min(250, 92 + len(title) * 7), ROW)
        return (40 + 232, ROW)

    def mood(self):
        return {"saving": "thinking", "recording": "working"}.get(self.mode, "awake")

    def build(self, view, width, height):
        from mint.ui import gfx as _gfx
        mid = height / 2
        mint = tuple(_gfx.accent())
        if self.mode == "saving":
            self.spinner(view, 8, mid - 8, mint)
            self.label(view, "Saving the skill…", 30, mid - 9, 118, size=13)
            return
        if self.mode == "saved":
            self.label(view, "✓  Learned", 10, mid + 1, 90, size=12, rgb=GREEN)
            self.label(view, self.snap.get("saved", ""), 10, mid - 14, width - 20, size=11, weight="medium", rgb=DIM)
            return
        recording = self.mode == "recording"
        self.dot(view, 9, mid - 4, 8, mint if recording else DIM, blink=recording)
        self.clock = self.label(view, "", 23, mid - 9, 48, size=14, mono=True, rgb=INK if recording else DIM)
        # The steps so far, in a mint pill that pops each time one is added.
        self.pill = Quartz.CALayer.layer()
        self.pill.setFrame_(Quartz.CGRectMake(74, mid - 11, 62, 22))
        self.pill.setCornerRadius_(11)
        self.pill.setBackgroundColor_(_cg(mint, 0.22 if recording else 0.08))
        view.layer().addSublayer_(self.pill)
        self.steps = self.label(view, "", 74, mid - 8, 62, size=11, weight="bold", rgb=mint if recording else DIM)
        self.steps.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.counted = -1
        self.button(view, "pause.fill" if recording else "play.fill", self.pause_or_play, 142, mid - 13, INK,
                    size=26, symbol_size=10, tip="Pause (nothing is recorded)" if recording else "Carry on")
        self.button(view, "checkmark", self.done, 172, mid - 14, GREEN, filled=True, size=28, symbol_size=11,
                    tip="Done - save it as a skill")
        self.button(view, "trash", self.cancel, 206, mid - 13, DIM, size=26, symbol_size=10,
                    tip="Cancel - throw the demonstration away")      # not an ×: the island's × only hides
        self.tick(time.time())

    def tick(self, now):
        from mint.knowledge import teach
        if self.mode not in ("recording", "paused") or not hasattr(self, "clock"):
            return
        snap = teach.snapshot() or self.snap
        self.clock.setStringValue_(_stamp(snap.get("seconds", 0)))
        steps = int(snap.get("clicks", 0)) + (1 if snap.get("keys") else 0)
        if steps != self.counted:
            grew = self.counted >= 0 and steps > self.counted
            self.counted = steps
            self.steps.setStringValue_("Paused" if self.mode == "paused" else f"{steps} step{'s' * (steps != 1)}")
            if grew:
                pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
                pop.setFromValue_(1.35)
                pop.setToValue_(1.0)
                pop.setDamping_(10)
                pop.setStiffness_(260)
                pop.setDuration_(pop.settlingDuration())
                self.pill.addAnimation_forKey_(pop, "pop")

    def pause_or_play(self):
        from mint.knowledge import teach
        if self.mode == "recording":
            teach.pause()
        else:
            teach.resume()

    def done(self):
        from mint.knowledge import teach
        _later(lambda: _notify_mint("(The user pressed done on the island - save what was shown.) " + teach.stop()))

    def cancel(self):
        from mint.knowledge import teach
        _later(lambda: _notify_mint("(The user cancelled the demonstration on the island.) " + teach.cancel()))


class VideoScene(Scene):
    """Mint watching a video: the title, the keyframes popping in as they are picked, a scan line
    sweeping over them, and what it is doing (listening, writing it up). Then "Watched"."""

    key = "video"
    priority = 55
    WIDTH = 312

    @property
    def card(self) -> bool:
        """A card while watching; "Watched" afterwards is a plain capsule."""
        return getattr(self, "mode", "watching") == "watching"
    SLOTS = 6

    def __init__(self) -> None:
        super().__init__()
        self.snap: dict = {}
        self.done_until, self.done_title, self.seen = 0.0, "", False

    def provide(self, now):
        from mint.tools import video
        snap = video.snapshot()
        if snap:
            self.snap, self.seen = snap, True
            self.mode = "watching"
            return self
        if self.seen:                                   # just finished
            self.seen = False
            self.done_until, self.done_title = now + 5.0, self.snap.get("title", "")
        if now < self.done_until:
            self.mode = "done"
            return self
        return None

    def signature(self):
        return f"video:{self.mode}"

    def size(self):
        return (self.WIDTH, 104) if self.mode == "watching" else (40 + 200, ROW)

    def mood(self):
        return "working" if self.mode == "watching" else "awake"

    def build(self, view, width, height):
        from mint.ui import gfx as _gfx
        mint = tuple(_gfx.accent())
        if self.mode == "done":
            self.label(view, "✓  Watched", 10, height / 2 + 1, 180, size=12, rgb=GREEN)
            self.label(view, self.done_title, 10, height / 2 - 14, 186, size=11, weight="medium", rgb=DIM)
            return
        top = height - 12
        badge = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(14, top - 30, 30, 30))
        badge.setWantsLayer_(True)
        badge.layer().setCornerRadius_(9)
        badge.layer().setBackgroundColor_(_cg(mint, 0.18))
        view.addSubview_(badge)
        icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("film.stack", 14))
        icon.setContentTintColor_(_ns(mint))
        icon.setFrame_(AppKit.NSMakeRect(5, 6, 20, 18))
        badge.addSubview_(icon)
        right = island.right_edge(width)
        self.title = self.label(view, self.snap.get("title", ""), 52, top - 14, right - 52, size=13)
        self.step = self.label(view, "", 52, top - 30, right - 52 - 40, size=11, weight="medium", rgb=DIM)
        self.clock = self.label(view, "", right - 40, top - 30, 40, size=11, weight="medium", rgb=DIM, mono=True)
        self.clock.setAlignment_(AppKit.NSTextAlignmentRight)
        # The keyframe strip: placeholders first, pictures popping in as they are picked.
        gap, slot_w = 6.0, (width - 28 - 5 * 6.0) / self.SLOTS
        slot_h = slot_w * 9 / 16
        self.slots, self.shown_frames = [], []
        y = 14
        for i in range(self.SLOTS):
            cell = Quartz.CALayer.layer()
            cell.setFrame_(Quartz.CGRectMake(14 + i * (slot_w + gap), y, slot_w, slot_h))
            cell.setCornerRadius_(5)
            cell.setMasksToBounds_(True)
            cell.setBackgroundColor_(_cg((1, 1, 1), 0.07))
            view.layer().addSublayer_(cell)
            self.slots.append(cell)
        scan = Quartz.CALayer.layer()
        scan.setBounds_(Quartz.CGRectMake(0, 0, 2, slot_h + 8))
        scan.setBackgroundColor_(_cg(mint))
        scan.setShadowColor_(_cg(mint))
        scan.setShadowRadius_(6)
        scan.setShadowOpacity_(1.0)
        scan.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        scan.setPosition_(Quartz.CGPointMake(14, y + slot_h / 2))
        view.layer().addSublayer_(scan)
        sweep = Quartz.CABasicAnimation.animationWithKeyPath_("position.x")
        sweep.setFromValue_(14)
        sweep.setToValue_(width - 14)
        sweep.setDuration_(1.8)
        sweep.setAutoreverses_(True)
        sweep.setRepeatCount_(1e9)
        sweep.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
            Quartz.kCAMediaTimingFunctionEaseInEaseOut))
        scan.addAnimation_forKey_(sweep, "sweep")
        self.tick(time.time())

    def tick(self, now):
        from mint.tools import video
        if self.mode != "watching" or not hasattr(self, "slots"):
            return
        snap = video.snapshot() or self.snap
        self.snap = snap
        if snap.get("title") and snap["title"] != self.title.stringValue():
            self.title.setStringValue_(snap["title"])
        self.step.setStringValue_(snap.get("step", ""))
        self.clock.setStringValue_(_stamp(snap.get("seconds", 0)))
        frames = snap.get("frames") or []
        if len(frames) > len(self.shown_frames):
            # Spread over the strip: with more keyframes than slots, every n-th one.
            want = frames if len(frames) <= self.SLOTS else [
                frames[round(i * (len(frames) - 1) / (self.SLOTS - 1))] for i in range(self.SLOTS)]
            for i, path in enumerate(want[:self.SLOTS]):
                if i < len(self.shown_frames) and self.shown_frames[i] == path:
                    continue
                image = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
                picture = image.CGImageForProposedRect_context_hints_(None, None, None)[0] if image else None
                if picture is None:
                    continue
                cell = self.slots[i]
                cell.setContents_(picture)
                cell.setContentsGravity_(Quartz.kCAGravityResizeAspectFill)
                pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
                pop.setFromValue_(0.55)
                pop.setToValue_(1.0)
                pop.setDamping_(11)
                pop.setStiffness_(220)
                pop.setDuration_(pop.settlingDuration())
                cell.addAnimation_forKey_(pop, "pop")
                if i < len(self.shown_frames):
                    self.shown_frames[i] = path
                else:
                    self.shown_frames.append(path)


class TutorScene(Scene):
    """A lesson on screen: which step, what to do, next and stop."""

    key = "tutor"
    priority = 75

    def provide(self, now):
        from mint.ui import tutor
        self.snap = tutor.snapshot()
        return self if self.snap else None

    def signature(self):
        return "tutor:planning" if self.snap.get("planning") else "tutor"

    def size(self):
        return (40 + 150, ROW) if self.snap.get("planning") else (40 + 290, 48)

    def mood(self):
        return "thinking" if self.snap.get("planning") else "awake"

    def build(self, view, width, height):
        mid = height / 2
        if self.snap.get("planning"):
            self.spinner(view, 8, mid - 8)
            self.label(view, "Planning the lesson", 30, mid - 9, 116, size=13)
            return
        self.step = self.label(view, "", 10, mid + 2, 214, size=10, weight="semibold", rgb=TEAL)
        self.say = self.label(view, "", 10, mid - 15, 214, size=13)
        self.button(view, "arrow.right", self.next, 228, mid - 14, TEAL, filled=True, size=28, symbol_size=11,
                    tip="Next step")
        self.button(view, "stop.fill", self.stop, 262, mid - 13, DIM, size=26, symbol_size=10,
                    tip="End the lesson")                              # not an ×: the island's × only hides
        self.tick(time.time())

    def tick(self, now):
        from mint.ui import tutor
        snap = tutor.snapshot()
        if snap and not snap.get("planning") and hasattr(self, "say"):
            self.step.setStringValue_(f"STEP {snap['index'] + 1} OF {snap['total']}")
            self.say.setStringValue_(snap["say"])
            self.say.setToolTip_(snap["say"])

    def next(self):
        from mint.ui import tutor
        _later(tutor.next_step)

    def stop(self):
        from mint.ui import tutor
        _later(lambda: _notify_mint("(The user ended the lesson on the island.) " + tutor.stop()))


class DictationScene(Scene):
    """Dictation (dictation.py): a live waveform while you talk, "Writing…", then "✓ Pasted"."""

    key = "dictation"
    priority = 96
    BARS = 18

    def provide(self, now):
        from mint.voice import dictation
        self.snap = dictation.snapshot()
        if not self.snap:
            return None
        self.mode = self.snap["mode"]
        return self

    def signature(self):
        return f"dictation:{self.mode}"

    def size(self):
        if self.mode == "recording":
            return (40 + 214, ROW)
        if self.mode == "done":
            return (40 + 200, ROW)
        return (40 + 156, ROW)

    def mood(self):
        return "thinking" if self.mode == "writing" else "awake"

    def level(self):
        levels = self.snap.get("levels") or [0.0]
        return min(0.5, levels[-1] * 3) if self.mode == "recording" else 0.0

    def build(self, view, width, height):
        from mint.ui import gfx as _gfx
        mid = height / 2
        mint = tuple(_gfx.accent())
        if self.mode == "recording":
            badge = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(7, mid - 13, 26, 26))
            badge.setWantsLayer_(True)
            badge.layer().setCornerRadius_(13)
            badge.layer().setBackgroundColor_(_cg(mint, 0.22))
            view.addSubview_(badge)
            icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("mic.fill", 11, "bold"))
            icon.setContentTintColor_(_ns(mint))
            icon.setFrame_(AppKit.NSMakeRect(5, 5, 16, 16))
            badge.addSubview_(icon)
            self.breathe_layer = badge.layer()
            glow = Quartz.CABasicAnimation.animationWithKeyPath_("backgroundColor")
            glow.setFromValue_(_cg(mint, 0.15))
            glow.setToValue_(_cg(mint, 0.45))
            glow.setDuration_(0.8)
            glow.setAutoreverses_(True)
            glow.setRepeatCount_(1e9)
            badge.layer().addAnimation_forKey_(glow, "glow")
            self.wave = []
            for i in range(self.BARS):
                bar = Quartz.CALayer.layer()
                bar.setCornerRadius_(1.25)
                bar.setBackgroundColor_(_cg(mint, 0.95))
                bar.setFrame_(Quartz.CGRectMake(42 + i * 5, mid - 1.5, 2.5, 3))
                view.layer().addSublayer_(bar)
                self.wave.append(bar)
            self.clock = self.label(view, "0:00", 136, mid - 8, 40, size=12, mono=True, rgb=DIM)
            self.button(view, "trash", self.cancel, 180, mid - 12, DIM, size=24, symbol_size=9,
                        tip="Throw it away")                       # not an ×: the island's × only hides
            self.tick(time.time())
        elif self.mode == "writing":
            self.spinner(view, 9, mid - 8, mint)
            self.label(view, "Writing…", 32, mid - 9, 120, size=13)
        elif self.mode == "done":
            self.label(view, "✓  Pasted", 10, mid + 1, 180, size=12, rgb=GREEN)
            self.label(view, " ".join(self.snap.get("text", "").split())[:60], 10, mid - 14, width - 16, size=10.5,
                       weight="medium", rgb=DIM)
        else:
            self.label(view, self.snap.get("error") or "Didn't catch that", 10, mid - 9, width - 16, size=12,
                       rgb=ORANGE)

    def tick(self, now):
        from mint.voice import dictation
        if self.mode != "recording" or not hasattr(self, "wave"):
            return
        snap = dictation.snapshot() or self.snap
        self.clock.setStringValue_(_stamp(snap.get("seconds", 0)))
        levels = (snap.get("levels") or [])[-self.BARS:]
        levels = [0.0] * (self.BARS - len(levels)) + levels
        mid = ROW / 2
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.08)
        for bar, level in zip(self.wave, levels):
            h = 3 + min(1.0, math.sqrt(level) * 3.2) * 22
            frame = bar.frame()
            bar.setFrame_(Quartz.CGRectMake(frame.origin.x, mid - h / 2, 2.5, h))
        Quartz.CATransaction.commit()

    def cancel(self):
        from mint.voice import dictation
        dictation.cancel()


DROP_ACTIONS = {
    "document": [("text.alignleft", "Summarize it"), ("character.bubble", "Translate it into Hindi, as a Word doc"),
                 ("tablecells", "Make a spreadsheet of its tables"), ("doc.on.clipboard", "Copy all its text")],
    "image": [("text.viewfinder", "Copy the text in it"), ("eye", "Describe it"),
              ("tablecells", "Make a spreadsheet of the table in it")],
    "video": [("sparkles.tv", "Watch it and tell me what it's about"), ("captions.bubble", "Add captions to it"),
              ("rectangle.portrait", "Make it vertical for Reels"), ("arrow.down.right.and.arrow.up.left", "Compress it")],
    "audio": [("waveform", "Transcribe it"), ("text.alignleft", "Summarize it")],
    "sheet": [("chart.bar", "Summarize the data"), ("text.magnifyingglass", "Find anything unusual in it")],
    "folder": [("sparkles", "Tidy it up"), ("list.bullet", "What's in it?")],
    "many": [("tablecells", "Make one spreadsheet of all of them"), ("text.alignleft", "Summarize them"),
             ("folder.badge.plus", "Put them in one folder")],
    "text": [("text.alignleft", "Summarize it"), ("character.bubble", "Translate it"), ("pencil", "Make it more formal"),
             ("doc.on.clipboard", "Copy it")],
}
KINDS = {"document": (".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".pages", ".html", ".key", ".pptx"),
         "image": (".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp", ".tiff", ".bmp"),
         "video": (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"),
         "audio": (".mp3", ".m4a", ".wav", ".aiff", ".aac", ".flac", ".ogg"),
         "sheet": (".csv", ".xlsx", ".xls", ".numbers", ".tsv")}


def drop_kind(paths: list[str], text: str) -> str:
    if not paths:
        return "text"
    if len(paths) > 1:
        return "many"
    if os.path.isdir(paths[0]):
        return "folder"
    ext = os.path.splitext(paths[0])[1].lower()
    return next((kind for kind, exts in KINDS.items() if ext in exts), "document")


class DropScene(Scene):
    """While a file is being dragged anywhere: a capsule to drop it on ("Drop here for Mint"); after the
    drop, a card of things to do with it that fit what it is - or just tell Mint."""

    key = "drop"

    def __init__(self) -> None:
        super().__init__()
        self.mode, self.dropped_at = "", 0.0
        self.paths: list[str] = []
        self.text = ""

    def provide(self, now):
        if island.dragging:
            self.mode, self.priority = "target", 97
            return self
        if self.mode == "actions" and now - self.dropped_at < 30:
            self.priority = 94
            return self
        self.mode = ""
        return None

    def signature(self):
        return f"drop:{self.mode}:{'hot' if island.drop_hot else ''}:{self.dropped_at}"

    @property
    def card(self) -> bool:
        return self.mode == "actions"

    def closable(self) -> bool:
        return self.mode == "actions"             # the drop target lasts only while something is dragged

    def size(self):
        if self.mode == "target":
            return (40 + 196, ROW)
        rows = len(DROP_ACTIONS[drop_kind(self.paths, self.text)]) + 1
        return (320, 62 + rows * 36 + 10)

    def mood(self):
        return "awake"

    def build(self, view, width, height):
        from mint.ui import gfx as _gfx
        mint = tuple(_gfx.accent())
        if self.mode == "target":
            mid = height / 2
            ring = Quartz.CAShapeLayer.layer()
            ring.setFrame_(Quartz.CGRectMake(4, 4, width - 6, height - 8))
            ring.setPath_(Quartz.CGPathCreateWithRoundedRect(Quartz.CGRectMake(0, 0, width - 6, height - 8),
                                                            (height - 8) / 2, (height - 8) / 2, None))
            ring.setFillColor_(_cg(mint, 0.18 if island.drop_hot else 0.06))
            ring.setStrokeColor_(_cg(mint, 0.9))
            ring.setLineWidth_(1.6)
            ring.setLineDashPattern_([5, 4])
            view.layer().addSublayer_(ring)
            march = Quartz.CABasicAnimation.animationWithKeyPath_("lineDashPhase")
            march.setFromValue_(0)
            march.setToValue_(-18)
            march.setDuration_(0.8)
            march.setRepeatCount_(1e9)
            ring.addAnimation_forKey_(march, "march")
            icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("tray.and.arrow.down.fill", 14))
            icon.setContentTintColor_(_ns(mint))
            icon.setFrame_(AppKit.NSMakeRect(18, mid - 9, 22, 18))
            view.addSubview_(icon)
            self.label(view, "Let go to hand it over" if island.drop_hot else "Drop here for Mint", 46, mid - 9,
                       width - 52, size=13)
            return
        top = height - 14
        right = island.right_edge(width)
        first = self.paths[0] if self.paths else ""
        if first:
            picture = AppKit.NSWorkspace.sharedWorkspace().iconForFile_(first)
            image = AppKit.NSImageView.imageViewWithImage_(picture)
            image.setFrame_(AppKit.NSMakeRect(14, top - 34, 34, 34))
            view.addSubview_(image)
        else:
            badge = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("text.quote", 16))
            badge.setContentTintColor_(_ns(mint))
            badge.setFrame_(AppKit.NSMakeRect(18, top - 30, 26, 24))
            view.addSubview_(badge)
        name = (os.path.basename(first) if first else " ".join(self.text.split())[:40] or "Text")
        more = f"  +{len(self.paths) - 1} more" if len(self.paths) > 1 else ""
        self.label(view, name + more, 56, top - 15, right - 56, size=14, weight="bold")
        self.label(view, "What should I do with it?", 56, top - 32, right - 56, size=11, weight="medium", rgb=DIM)
        y = top - 50
        actions = DROP_ACTIONS[drop_kind(self.paths, self.text)] + [("mic.fill", "Tell Mint what to do")]
        for n, (symbol, words) in enumerate(actions):
            y -= 36
            last = n == len(actions) - 1
            act = _IslandAct.alloc().initWithFn_(lambda w=words, lst=last: self.choose(w, lst))
            self._acts.append(act)
            row = _IslandButton.alloc().initWithFrame_(AppKit.NSMakeRect(10, y + 1, width - 20, 32))
            row.setBordered_(False)
            row.setTitle_("")
            row.setTarget_(act)
            row.setAction_("fire:")
            row.setWantsLayer_(True)
            row.layer().setCornerRadius_(9)
            row.layer().setBackgroundColor_(_cg(mint if last else (1, 1, 1), 0.16 if last else 0.06))
            view.addSubview_(row)
            icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol(symbol, 12))
            icon.setContentTintColor_(_ns(mint if last else INK))
            icon.setFrame_(AppKit.NSMakeRect(22, y + 8, 20, 18))
            view.addSubview_(icon)
            self.label(view, words, 50, y + 8, width - 70, size=13, weight="semibold", rgb=mint if last else INK)

    def choose(self, words: str, tell: bool) -> None:
        what = ", ".join(self.paths) if self.paths else ""
        if tell:
            request = (f"(The user dropped {'these files' if len(self.paths) > 1 else 'this file' if self.paths else 'this text'} "
                       f"on you: {what or self.text[:2000]}) Ask in a few words what they'd like done with it, then wait.")
            island.hud._fire("wake")
        elif self.paths:
            request = f"{words}: {what}"
        else:
            request = f"{words}:\n{self.text[:6000]}"
        island.hud._fire("submit", request)
        self.mode = ""
        print(f"  {time.strftime('%H:%M:%S')} [dropped -> {words}]", flush=True)


class VideoEditScene(Scene):
    """A video being edited (video_edit.py): what it is doing and how far it has got."""

    key = "video_edit"
    priority = 57

    def provide(self, now):
        from mint.tools import video_edit
        self.snap = video_edit.snapshot()
        return self if self.snap else None

    def size(self):
        return (40 + 236, ROW)

    def mood(self):
        return "working"

    def build(self, view, width, height):
        from mint.ui import gfx as _gfx
        mint = tuple(_gfx.accent())
        mid = height / 2
        icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("scissors", 12, "bold"))
        icon.setContentTintColor_(_ns(mint))
        icon.setFrame_(AppKit.NSMakeRect(10, mid - 8, 18, 16))
        view.addSubview_(icon)
        self.step = self.label(view, "", 34, mid, 160, size=12)
        self.track = Quartz.CALayer.layer()
        self.track.setFrame_(Quartz.CGRectMake(34, mid - 9, 190, 4))
        self.track.setCornerRadius_(2)
        self.track.setBackgroundColor_(_cg((1, 1, 1), 0.12))
        view.layer().addSublayer_(self.track)
        self.fill = Quartz.CALayer.layer()
        self.fill.setFrame_(Quartz.CGRectMake(34, mid - 9, 2, 4))
        self.fill.setCornerRadius_(2)
        self.fill.setBackgroundColor_(_cg(mint))
        view.layer().addSublayer_(self.fill)
        self.percent = self.label(view, "", 196, mid, 34, size=11, mono=True, rgb=DIM)
        self.percent.setAlignment_(AppKit.NSTextAlignmentRight)
        self.tick(time.time())

    def tick(self, now):
        from mint.tools import video_edit
        snap = video_edit.snapshot() or self.snap
        if not hasattr(self, "fill"):
            return
        percent = max(0.0, min(100.0, float(snap.get("percent") or 0)))
        self.step.setStringValue_(str(snap.get("step") or "Editing the video"))
        self.percent.setStringValue_(f"{percent:.0f}%" if percent else "")
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.3)
        self.fill.setFrame_(Quartz.CGRectMake(34, ROW / 2 - 9, max(2.0, 190 * percent / 100), 4))
        Quartz.CATransaction.commit()


class ConvertScene(VideoEditScene):
    """A document being converted or translated (convert.py): the step and its parts done."""

    key = "convert"
    priority = 56

    def provide(self, now):
        from mint.tools import convert
        self.snap = convert.snapshot()
        return self if self.snap else None

    def build(self, view, width, height):
        super().build(view, width, height)
        icon = next(v for v in view.subviews() if isinstance(v, AppKit.NSImageView))
        icon.setImage_(gfx.symbol("doc.text.fill", 12, "bold"))

    def tick(self, now):
        from mint.tools import convert
        snap = convert.snapshot() or self.snap
        if not hasattr(self, "fill"):
            return
        total, done = int(snap.get("total") or 0), int(snap.get("done") or 0)
        percent = 100.0 * done / total if total else 0.0
        self.step.setStringValue_(str(snap.get("step") or snap.get("title") or "Converting"))
        self.percent.setStringValue_(f"{done}/{total}" if total else "")
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.3)
        self.fill.setFrame_(Quartz.CGRectMake(34, ROW / 2 - 9, max(2.0, 190 * percent / 100), 4))
        Quartz.CATransaction.commit()


class TrackerScene(Scene):
    """A tracker: "Tracking · <what>" for a moment when it starts; when it ends, "✓ Download finished"
    (click: open it / bring the app forward) - or "Needs you" when a Claude session waits for approval."""

    key = "tracker"
    ICONS = {"download": "arrow.down.circle.fill", "claude": "sparkles", "terminal": "terminal.fill",
             "window": "macwindow", "file": "doc.fill"}

    def provide(self, now):
        from mint.tools import trackers
        ended = trackers.recent(8.0)
        if ended:
            self.item, self.mode = ended, ended["outcome"]
            self.priority = 40
            return self
        started = trackers.just_started(3.5)
        if started:
            self.item, self.mode = started, "started"
            self.priority = 25
            return self
        return None

    def signature(self):
        return f"tracker:{self.mode}:{self.item.get('id')}"

    def size(self):
        attrs = {AppKit.NSFontAttributeName: _font(13, "semibold")}
        measured = AppKit.NSString.stringWithString_(self._title()).sizeWithAttributes_(attrs).width
        return (40 + min(320.0, 42 + measured + 14), ROW)

    def mood(self):
        return "awake"

    def _title(self) -> str:
        kind, label = self.item.get("kind", ""), self.item.get("label", "")
        if self.mode == "started":
            return f"Tracking · {label}"
        if self.mode == "waiting":
            return "Needs you · " + label
        if self.mode == "failed":
            return "Didn't finish · " + label
        opened = Path(self.item.get("open") or "").name
        if kind == "download":
            return f"Downloaded · {opened}" if opened else "Download finished"
        if kind == "file":
            return f"Ready · {opened}" if opened else "File ready"
        return {"claude": "Claude is done · ", "terminal": "Command finished · "}.get(kind, "Done · ") + label

    def build(self, view, width, height):
        from mint.ui import gfx as _gfx
        mid = height / 2
        rgb = {"done": GREEN, "failed": ORANGE, "waiting": ORANGE}.get(self.mode, tuple(_gfx.accent()))
        badge = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(8, mid - 13, 26, 26))
        badge.setWantsLayer_(True)
        badge.layer().setCornerRadius_(13)
        badge.layer().setBackgroundColor_(_cg(rgb, 0.2))
        view.addSubview_(badge)
        symbol = "checkmark" if self.mode == "done" else self.ICONS.get(self.item.get("kind"), "eye.fill")
        icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol(symbol, 11, "bold"))
        icon.setContentTintColor_(_ns(rgb))
        icon.setFrame_(AppKit.NSMakeRect(5, 5, 16, 16))
        badge.addSubview_(icon)
        if self.mode == "done":
            pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
            pop.setFromValue_(0.3)
            pop.setToValue_(1.0)
            pop.setDamping_(9)
            pop.setStiffness_(240)
            pop.setDuration_(pop.settlingDuration())
            badge.layer().addAnimation_forKey_(pop, "pop")
        self.label(view, self._title(), 42, mid - 9, width - 50, size=13, rgb=INK)

    def clicked(self) -> None:
        """Open what finished: the downloaded file, the app, or Claude."""
        item = self.item
        if item.get("open"):
            subprocess.run(["open", item["open"]], check=False)
        elif item.get("kind") == "claude":
            subprocess.run(["open", "-a", "Claude"], check=False)
        elif item.get("app"):
            subprocess.run(["open", "-a", item["app"]], check=False)


TINTS = {"mint": None, "blue": BLUE, "orange": ORANGE, "green": GREEN, "purple": PURPLE, "red": RED, "teal": TEAL}


class InfoCard(Scene):
    """An answer as a card: a count ("12 unread emails" - a big number) or a short list (files, emails,
    downloads, automations, trackers...) with icons; a row with a file opens it on click."""

    key = "info"
    priority = 32
    card = True
    WIDTH = 324
    ROWS = 7

    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data
        self.items = [i for i in (data.get("items") or []) if isinstance(i, dict) and i.get("title")]

    def signature(self):
        return f"info:{id(self)}"

    def _number(self) -> str:
        number = self.data.get("number")
        return "" if number in (None, "") else str(number)

    def size(self):
        h = 16 + (46 if self.data.get("subtitle") else 36)    # padding + header
        if self._number():
            h += 62
        shown = min(len(self.items), self.ROWS)
        if shown:
            h += 6 + shown * 34
        if len(self.items) > self.ROWS or self.data.get("more"):
            h += 20
        return (self.WIDTH, h + 12)

    def clicked(self) -> None:
        island.dismiss(self)

    def _tint(self):
        from mint.ui import gfx as _gfx
        return TINTS.get(str(self.data.get("tint") or "mint")) or tuple(_gfx.accent())

    def build(self, view, width, height):
        tint = self._tint()
        top = height - 14
        right = island.right_edge(width)
        badge = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(16, top - 32, 32, 32))
        badge.setWantsLayer_(True)
        badge.layer().setCornerRadius_(10)
        badge.layer().setBackgroundColor_(_cg(tint, 0.2))
        view.addSubview_(badge)
        icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol(str(self.data.get("icon") or "sparkles"), 14))
        icon.setContentTintColor_(_ns(tint))
        icon.setFrame_(AppKit.NSMakeRect(6, 6, 20, 20))
        badge.addSubview_(icon)
        self.label(view, str(self.data.get("title") or ""), 58, top - 14, right - 58, size=15, weight="bold",
                   rounded=True, h=19)
        if self.data.get("subtitle"):
            self.label(view, str(self.data["subtitle"]), 58, top - 31, right - 58, size=11, weight="medium", rgb=DIM)
        y = top - (46 if self.data.get("subtitle") else 36)
        number = self._number()
        if number:
            y -= 62
            big = self.label(view, number, 18, y + 4, 200, size=46, weight="bold", rgb=tint, rounded=True, h=56)
            big.sizeToFit()
            nw = big.frame().size.width
            if self.data.get("unit"):
                self.label(view, str(self.data["unit"]), 18 + nw + 8, y + 14, width - 44 - nw, size=15,
                           weight="semibold", rgb=INK, alpha=0.9)
            pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
            pop.setFromValue_(0.6)
            pop.setToValue_(1.0)
            pop.setDamping_(10)
            pop.setStiffness_(220)
            pop.setDuration_(pop.settlingDuration())
            big.setWantsLayer_(True)
            big.layer().addAnimation_forKey_(pop, "pop")
        if self.items:
            y -= 6
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        for n, item in enumerate(self.items[: self.ROWS]):
            y -= 34
            if n % 2 == 0:
                stripe = Quartz.CALayer.layer()
                stripe.setFrame_(Quartz.CGRectMake(10, y + 1, width - 20, 32))
                stripe.setCornerRadius_(8)
                stripe.setBackgroundColor_(_cg((1, 1, 1), 0.04))
                view.layer().addSublayer_(stripe)
            path = str(item.get("path") or "")
            if path and os.path.exists(os.path.expanduser(path)):
                full = os.path.expanduser(path)
                thumb = None
                if full.lower().endswith((".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp", ".tiff")):
                    thumb = AppKit.NSImage.alloc().initWithContentsOfFile_(full)      # the picture itself
                image = AppKit.NSImageView.imageViewWithImage_(thumb or workspace.iconForFile_(full))
                image.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
                image.setFrame_(AppKit.NSMakeRect(14, y + 4, 28, 26) if thumb else AppKit.NSMakeRect(16, y + 5, 24, 24))
                if thumb is not None:
                    image.setWantsLayer_(True)
                    image.layer().setCornerRadius_(4)
                    image.layer().setMasksToBounds_(True)
            else:
                image = AppKit.NSImageView.imageViewWithImage_(gfx.symbol(str(item.get("icon") or "circle.fill"),
                                                                          10 if not item.get("icon") else 13))
                image.setContentTintColor_(_ns(tint))
                image.setFrame_(AppKit.NSMakeRect(18, y + 8, 20, 18))
            view.addSubview_(image)
            trailing = str(item.get("trailing") or "")
            tw = 0
            if trailing:
                tw = min(96, 12 + len(trailing) * 6.6)
                tail = self.label(view, trailing, width - 16 - tw, y + 10, tw, size=11, weight="semibold", rgb=DIM,
                                  mono=bool(re.match(r"^[\d:.,% ]+[a-zA-Z]{0,3}$", trailing)))
                tail.setAlignment_(AppKit.NSTextAlignmentRight)
            detail = str(item.get("detail") or "")
            text_w = width - 50 - 18 - tw
            if detail:
                self.label(view, str(item["title"]), 48, y + 16, text_w, size=13, weight="semibold")
                self.label(view, detail, 48, y + 2, text_w, size=10.5, weight="medium", rgb=DIM)
            else:
                self.label(view, str(item["title"]), 48, y + 9, text_w, size=13, weight="semibold")
            if path:                                   # the whole row opens it
                act = _IslandAct.alloc().initWithFn_(lambda p=path: subprocess.run(["open", os.path.expanduser(p)],
                                                                                   check=False))
                self._acts.append(act)
                row = _IslandButton.alloc().initWithFrame_(AppKit.NSMakeRect(10, y + 1, width - 20, 32))
                row.setTransparent_(True)
                row.setTarget_(act)
                row.setAction_("fire:")
                row.setToolTip_(f"Open {os.path.basename(path)}")
                view.addSubview_(row)
        extra = max(0, len(self.items) - self.ROWS) + int(self.data.get("more") or 0)
        if extra:
            y -= 20
            self.label(view, f"+ {extra} more", 48, y + 2, 200, size=11, weight="semibold", rgb=DIM)


class ScheduleCard(Scene):
    """The day at a glance: shown with a briefing or a look at today's calendar."""

    key = "schedule"
    priority = 30
    card = True
    WIDTH = 318

    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data
        self.events = [e for e in data.get("events") or []][:6]

    def signature(self):
        return f"schedule:{id(self)}"

    def size(self):
        h = 58 + max(1, len(self.events)) * 30 + 6
        if self.data.get("reminders"):
            h += 34
        if self.data.get("news"):
            h += 22 + 17 * len(self.data["news"][:3])
        return (self.WIDTH, h + 10)

    def clicked(self) -> None:
        island.dismiss(self)

    def build(self, view, width, height):
        top = height - 14
        self.label(view, self.data.get("day", ""), 18, top - 22, 170, size=19, weight="bold", rounded=True, h=24)
        self.label(view, self.data.get("date", ""), 18, top - 40, 170, size=12, weight="medium", rgb=DIM)
        weather = self.data.get("weather")
        right = island.right_edge(width)
        if weather:
            icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol(weather["symbol"], 15))
            config = AppKit.NSImageSymbolConfiguration.configurationWithPaletteColors_(
                [_ns((1.0, 0.85, 0.3)), _ns((0.85, 0.88, 0.95))])
            icon.setSymbolConfiguration_(config)
            icon.setFrame_(AppKit.NSMakeRect(right - 78, top - 26, 22, 20))
            icon.setToolTip_(weather.get("desc", ""))
            view.addSubview_(icon)
            self.label(view, f"{weather['temp']}°", right - 54, top - 26, 54, size=17, weight="semibold", rounded=True)
            if weather.get("range"):
                self.label(view, weather["range"], right - 78, top - 42, 76, size=10, weight="medium", rgb=DIM)
        y = top - 58
        now = dt.datetime.now()
        next_event = next((e for e in self.events if not e["all_day"] and e["start"] > now), None)
        if not self.events:
            self.label(view, "Nothing on the calendar today", 18, y - 20, width - 36, size=13, weight="medium",
                       rgb=DIM)
            y -= 30
        for event in self.events:
            y -= 30
            live = not event["all_day"] and event["start"] <= now < event["end"]
            past = not event["all_day"] and event["end"] <= now
            if live:
                glow = Quartz.CALayer.layer()
                glow.setFrame_(Quartz.CGRectMake(10, y + 1, width - 20, 28))
                glow.setCornerRadius_(9)
                glow.setBackgroundColor_(_cg(event["rgb"], 0.2))
                view.layer().addSublayer_(glow)
            alpha = 0.42 if past else 1.0
            when = "all day" if event["all_day"] else event["start"].strftime("%-I:%M%p").lower()
            self.label(view, when, 18, y + 7, 58, size=12, weight="semibold", rgb=INK if live else DIM,
                       alpha=alpha, mono=True)
            upcoming = not live and not past and not event["all_day"] and event is next_event
            if upcoming:
                minutes = int((event["start"] - now).total_seconds() // 60)
                soon = f"in {minutes}m" if minutes < 60 else f"in {minutes // 60}h" + (
                    f" {minutes % 60}m" if minutes % 60 and minutes < 600 else "")
                hint = self.label(view, soon, width - 70, y + 8, 56, size=10, weight="semibold", rgb=BLUE)
                hint.setAlignment_(AppKit.NSTextAlignmentRight)
            bar = Quartz.CALayer.layer()
            bar.setFrame_(Quartz.CGRectMake(78, y + 6, 3, 18))
            bar.setCornerRadius_(1.5)
            bar.setBackgroundColor_(_cg(event["rgb"], alpha))
            view.layer().addSublayer_(bar)
            title = self.label(view, event["title"], 90, y + 7, width - 90 - (60 if live or upcoming else 16), size=13,
                               weight="semibold", alpha=alpha)
            title.setToolTip_(event["title"] + (f" · {event['place']}" if event.get("place") else ""))
            if live:
                pill = self.label(view, "NOW", width - 58, y + 8, 40, size=9, weight="bold", rgb=SURFACE)
                pill.setAlignment_(AppKit.NSTextAlignmentCenter)
                pill.setWantsLayer_(True)
                pill.setDrawsBackground_(True)
                pill.setBackgroundColor_(_ns(GREEN))
                pill.layer().setCornerRadius_(6)
        reminders = self.data.get("reminders") or []
        if reminders:
            y -= 34
            x = 14
            attrs = {AppKit.NSFontAttributeName: _font(11, "semibold")}
            for text in reminders[:3]:
                measured = AppKit.NSString.stringWithString_("◯  " + text).sizeWithAttributes_(attrs).width
                chip_w = min(150, measured + 22)
                if x + chip_w > width - 12:
                    break
                chip = Quartz.CALayer.layer()
                chip.setFrame_(Quartz.CGRectMake(x, y + 4, chip_w, 24))
                chip.setCornerRadius_(12)
                chip.setBackgroundColor_(_cg(ORANGE, 0.18))
                view.layer().addSublayer_(chip)
                self.label(view, "◯  " + text, x + 9, y + 8, chip_w - 14, size=11, weight="semibold", rgb=ORANGE)
                x += chip_w + 6
        news = self.data.get("news") or []
        if news:
            y -= 22
            self.label(view, "HEADLINES", 18, y + 2, 120, size=9, weight="bold", rgb=DIM)
            for line in news[:3]:
                y -= 17
                self.label(view, "·  " + line, 18, y, width - 32, size=11, weight="medium", alpha=0.86)


# --- the island itself ----------------------------------------------------------------------------

class Island:
    def __init__(self) -> None:
        self.hud = None
        self.scene: Scene | None = None
        self.sig = ""
        self.shown = False
        self.rect = None                  # the capsule's frame, in screen points
        self.anchor = None                # where the orb was when the island opened
        self.face_right = True
        self.face_top = True
        self.pushed: list[tuple[Scene, float]] = []
        self.providers: list = []
        self.frame = 0
        self.errors: set[str] = set()
        self.mood = ""
        self.closing = False
        self.hover_since = 0.0
        self.dragging = False
        self.drop_hot = False
        self.drag_count = -1
        self.hidden: set[str] = set()     # signatures closed with the × (the work behind them goes on)
        self.close_button = None
        self._close_room = False          # while a scene builds: leave room for the ×

    # --- building --------------------------------------------------------------------------------

    def attach(self, hud) -> None:
        from mint.ui.hud import _panel
        from mint.ui.orb import Orb
        self.hud = hud
        screen = AppKit.NSScreen.mainScreen().frame()
        self.screen = screen
        self.panel = _panel(screen, click_through=True)
        root = _IslandRoot.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, screen.size.width, screen.size.height))
        root.setWantsLayer_(True)
        root.registerForDraggedTypes_([AppKit.NSPasteboardTypeFileURL, AppKit.NSPasteboardTypeString])
        self.panel.setContentView_(root)
        self.root = root

        self.blob = Quartz.CALayer.layer()
        self.blob.setBackgroundColor_(_cg(SURFACE))          # opaque: 3% see-through showed text behind
        self.blob.setBorderColor_(_cg((1, 1, 1), 0.09))
        self.blob.setBorderWidth_(0.5)
        self.blob.setShadowColor_(_cg((0, 0, 0)))
        self.blob.setShadowOpacity_(0.35)
        self.blob.setShadowRadius_(14)
        self.blob.setShadowOffset_(Quartz.CGSizeMake(0, -4))
        self.blob.setOpacity_(0.0)
        root.layer().addSublayer_(self.blob)

        self.content = None
        self.face_host = Quartz.CALayer.layer()
        self.face_host.setBounds_(Quartz.CGRectMake(0, 0, ROW, ROW))
        self.face_host.setOpacity_(0.0)
        root.layer().addSublayer_(self.face_host)
        self.face = Orb(self.face_host, (ROW / 2, ROW / 2), FACE)
        self.face.apply_state("awake", True)
        self._quiet_face()

        # A click on the little face opens the chat, like a click on the orb.
        self.face_act = _IslandAct.alloc().initWithFn_(lambda: self.hud._fire("console"))
        self.face_button = _IslandButton.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, ROW, ROW))
        self.face_button.setBordered_(False)
        self.face_button.setTitle_("")
        self.face_button.setTransparent_(True)
        self.face_button.setTarget_(self.face_act)
        self.face_button.setAction_("fire:")
        self.face_button.setHidden_(True)
        root.addSubview_(self.face_button)

        self.drop = DropScene()
        scenes = (MeetingScene(), ScreenScene(), TeachScene(), TutorScene(), VideoScene(), TrackerScene(),
                  DictationScene(), VideoEditScene(), ConvertScene(), self.drop)
        self.providers = [scene.provide for scene in scenes]
        self._hide_bubble_while_shown()

        self.ticker = _IslandTicker.alloc().initWithOwner_(self)
        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 30, self.ticker, "tick:", None, True)

    def _quiet_face(self) -> None:
        """The little Mint keeps its face and swirl, not the orb's outer pulse ring."""
        try:
            for layer in (self.face.ring, self.face.spinner):
                layer.removeAllAnimations()
                layer.setOpacity_(0.0)
        except Exception:
            pass

    def _hide_bubble_while_shown(self) -> None:
        """The word bubble beside the orb would sit on top of the island: while it shows, words stay in
        the chat only."""
        hud = self.hud
        original = hud._render_bubble

        def render(now, _orig=original):
            if self.shown:
                if hud._bubble_visible:
                    hud._hide_bubble()
                hud._bubble_dirty = False
                return
            _orig(now)
        hud._render_bubble = render

    # --- public (any thread) ---------------------------------------------------------------------

    def show_schedule(self, data: dict, seconds: float = 40.0) -> None:
        AppHelper.callAfter(self._push, ScheduleCard(data), seconds)

    def drop_hover(self, hot: bool) -> None:
        self.drop_hot = hot

    def dropped(self, paths: list[str], text: str) -> None:
        """Something was dropped on the capsule: offer what to do with it."""
        self.dragging, self.drop_hot = False, False
        drop = self.drop
        drop.paths, drop.text = paths, text
        drop.mode, drop.dropped_at = "actions", time.time()
        print(f"  {time.strftime('%H:%M:%S')} [dropped on Mint: {', '.join(os.path.basename(p) for p in paths) or 'text'}]",
              flush=True)

    def _watch_drag(self) -> None:
        """A drag (files or text) started anywhere: show the drop target until the button is let go."""
        board = AppKit.NSPasteboard.pasteboardWithName_(AppKit.NSPasteboardNameDrag)
        count = board.changeCount()
        pressed = bool(AppKit.NSEvent.pressedMouseButtons() & 1)
        if count != self.drag_count:
            self.drag_count = count
            types = [str(t) for t in (board.types() or [])]
            if pressed and any(t in types for t in (str(AppKit.NSPasteboardTypeFileURL),
                                                    str(AppKit.NSPasteboardTypeString))):
                self.dragging = True
        if self.dragging and not pressed:
            self.dragging, self.drop_hot = False, False

    def show_card(self, data: dict, seconds: float = 20.0) -> None:
        """An answer as a card (see InfoCard): a count, a list of files, emails, things."""
        AppHelper.callAfter(self._push, InfoCard(data), seconds)

    def dismiss(self, scene: Scene) -> None:
        self.pushed = [(s, until) for s, until in self.pushed if s is not scene]

    def close_scene(self, scene: Scene) -> None:
        """The ×: out of sight, nothing stopped. A pushed card goes; a live scene (a recording, a
        lesson, dictation) only hides until it changes (recording -> saving) or ends."""
        self._was_pressed = False             # the × took this click, not whatever shows next
        if any(s is scene for s, _ in self.pushed):
            self.dismiss(scene)
        else:
            self.hidden.add(scene.signature())

    def right_edge(self, width: float) -> float:
        """Where a card's header must stop: clear of the face in its top corner and of the × (which
        sits beside the face, or in that corner when the face is elsewhere)."""
        face = self.face_right and self.face_top
        right = width - (48 if face else 14)
        if self._close_room:
            right -= 20 if face else 24
        return right

    def _face_shown(self) -> bool:
        """False in notch mode: the notch has its own little Mint, so the face's end is free."""
        host = getattr(self, "face_host", None)
        return host is not None and not host.isHidden()

    def _close_slot(self, scene: Scene) -> bool:
        """A compact scene with a × and a face: the capsule grows a slot at the far end for the ×."""
        return not scene.card and self._face_shown() and scene.closable()

    def _push(self, scene: Scene, seconds: float) -> None:
        self.pushed = [(s, until) for s, until in self.pushed if s.key != scene.key]
        self.pushed.append((scene, time.time() + seconds))

    # --- choosing ---------------------------------------------------------------------------------

    def _pick(self, now: float) -> Scene | None:
        wall = time.time()
        hovering = self.shown and self.rect is not None and self._inside(AppKit.NSEvent.mouseLocation())
        if hovering:                                   # a card being read stays
            self.pushed = [(s, max(until, wall + 4)) for s, until in self.pushed]
        self.pushed = [(s, until) for s, until in self.pushed if until > wall]
        found = []
        for provide in self.providers:
            try:
                scene = provide(now)
            except Exception as error:
                self._failed(error)
                scene = None
            if scene is not None:
                found.append(scene)
        found += [s for s, _ in self.pushed]
        signatures = {s.signature() for s in found}
        self.hidden &= signatures                      # changed or over: the next one shows again
        found = [s for s in found if s.signature() not in self.hidden]
        return max(found, key=lambda s: s.priority) if found else None

    def tick(self) -> None:
        try:
            self._tick()
        except Exception as error:
            self._failed(error)

    def _failed(self, error: Exception) -> None:
        key = f"{type(error).__name__}: {error}"[:200]
        if key not in self.errors:
            self.errors.add(key)
            log.exception("island")
            print(f"  {time.strftime('%H:%M:%S')} [island failed: {key}]", flush=True)

    def _tick(self) -> None:
        now = time.time()
        self.frame += 1
        if self.frame % 3 == 0:
            self._watch_drag()
        if self.frame % 3 == 0 and not self.closing:
            scene = self._pick(now)
            sig = scene.signature() if scene is not None else ""
            if sig != self.sig:
                previous = self.sig
                self.sig = sig
                print(f"  {time.strftime('%H:%M:%S')} [island: {sig or 'orb'}]", flush=True)
                if scene is None:
                    self._close()
                elif not self.shown:
                    self._open(scene)
                else:
                    self._morph(scene, previous)
        if not self.shown or self.scene is None:
            return
        try:
            self.scene.tick(now)
        except Exception as error:
            self._failed(error)
        self._tick_face(now)
        mouse = AppKit.NSEvent.mouseLocation()
        inside = self._inside(mouse)
        self.panel.setIgnoresMouseEvents_(not inside)
        if self.close_button is not None:
            gfx.track_close(self.close_button, inside, mouse)
        pressed = bool(AppKit.NSEvent.pressedMouseButtons() & 1)
        if inside and pressed and not getattr(self, "_was_pressed", False):
            self._press_started = (mouse.x, mouse.y)
        if inside and not pressed and getattr(self, "_was_pressed", False):
            self._maybe_clicked(mouse)
        self._was_pressed = pressed

    def _maybe_clicked(self, mouse) -> None:
        """A click on the capsule that no button took (cards: dismiss; "Notes ready": open)."""
        hit = self.root.hitTest_(self.root.convertPoint_fromView_(
            self.panel.convertPointFromScreen_(mouse), None))
        if hit is None or isinstance(hit, AppKit.NSButton):
            return
        try:
            self.scene.clicked()
        except Exception as error:
            self._failed(error)

    def _inside(self, point) -> bool:
        if self.rect is None:
            return False
        x, y, w, h = self.rect
        return x <= point.x <= x + w and y <= point.y <= y + h

    def _tick_face(self, now: float) -> None:
        scene = self.scene
        mood = scene.mood() if scene is not None else "awake"
        if mood != self.mood:
            self.mood = mood
            self.face.apply_state(mood, True)
            self._quiet_face()
        mouse = AppKit.NSEvent.mouseLocation()
        fx, fy = self.face_center
        level = max(scene.level(), float(getattr(self.hud, "_level", 0.0) or 0.0))
        self.face.tick(time.monotonic(), level, (mouse.x - fx, mouse.y - fy), False, True)

    # --- geometry --------------------------------------------------------------------------------

    def _upper(self) -> bool:
        visible = AppKit.NSScreen.mainScreen().visibleFrame()
        return self.anchor[1] > visible.origin.y + visible.size.height / 2

    def _size(self, scene: Scene) -> tuple[float, float]:
        """The scene's size, plus a strip for the face at the bottom of a card that opens upwards."""
        width, height = scene.size()
        if scene.card and not self._upper():
            height += ROW - 6
        if self._close_slot(scene):
            width += CLOSE_SLOT
        return width, height

    def _layout(self, width: float, height: float):
        """The capsule's frame (screen points) and the face's centre: the face sits exactly where the
        orb is, the capsule grows towards the middle of the screen."""
        cx, cy = self.anchor
        visible = AppKit.NSScreen.mainScreen().visibleFrame()
        self.face_right = cx > visible.origin.x + visible.size.width / 2
        upper = self.face_top = cy > visible.origin.y + visible.size.height / 2
        x = cx + ROW / 2 - width if self.face_right else cx - ROW / 2
        y = cy + ROW / 2 - height if upper else cy - ROW / 2
        x = min(max(x, visible.origin.x + 6), visible.origin.x + visible.size.width - width - 6)
        y = min(max(y, visible.origin.y + 6), visible.origin.y + visible.size.height - height - 6)
        fx = x + width - ROW / 2 if self.face_right else x + ROW / 2
        fy = y + height - ROW / 2 if upper else y + ROW / 2
        return (x, y, width, height), (fx, fy)

    def _local(self, x, y):
        return x - self.screen.origin.x, y - self.screen.origin.y

    # --- motion ----------------------------------------------------------------------------------

    @staticmethod
    def _spring(layer, key, old, new, damping=20.0, stiffness=190.0) -> None:
        anim = Quartz.CASpringAnimation.animationWithKeyPath_(key)
        anim.setFromValue_(old)
        anim.setToValue_(new)
        anim.setDamping_(damping)
        anim.setStiffness_(stiffness)
        anim.setMass_(1.0)
        anim.setDuration_(anim.settlingDuration())
        layer.addAnimation_forKey_(anim, key)

    def _shape(self, rect, radius, animate: bool = True, damping=20.0) -> None:
        """Move the black capsule to `rect` (screen points), springing from wherever it is now."""
        x, y, w, h = rect
        lx, ly = self._local(x, y)
        shown = self.blob.presentationLayer() or self.blob
        old_bounds, old_position, old_radius = shown.bounds(), shown.position(), shown.cornerRadius()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.blob.setBounds_(Quartz.CGRectMake(0, 0, w, h))
        self.blob.setPosition_(Quartz.CGPointMake(lx + w / 2, ly + h / 2))
        self.blob.setCornerRadius_(radius)
        Quartz.CATransaction.commit()
        if animate:
            self._spring(self.blob, "bounds", AppKit.NSValue.valueWithRect_(old_bounds),
                         AppKit.NSValue.valueWithRect_(Quartz.CGRectMake(0, 0, w, h)), damping)
            self._spring(self.blob, "position", AppKit.NSValue.valueWithPoint_(old_position),
                         AppKit.NSValue.valueWithPoint_(Quartz.CGPointMake(lx + w / 2, ly + h / 2)), damping)
            self._spring(self.blob, "cornerRadius", old_radius, radius, damping)
        self.rect = rect

    def _place_face(self, center, animate: bool = True, scale: float = 1.0) -> None:
        fx, fy = self._local(*center)
        shown = self.face_host.presentationLayer() or self.face_host
        old = shown.position()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.face_host.setPosition_(Quartz.CGPointMake(fx, fy))
        self.face_host.setAffineTransform_(Quartz.CGAffineTransformMakeScale(scale, scale))
        Quartz.CATransaction.commit()
        if animate:
            self._spring(self.face_host, "position", AppKit.NSValue.valueWithPoint_(old),
                         AppKit.NSValue.valueWithPoint_(Quartz.CGPointMake(fx, fy)))
        self.face_center = center
        self.face_button.setFrame_(AppKit.NSMakeRect(fx - ROW / 2, fy - ROW / 2, ROW, ROW))

    def _radius(self, scene: Scene, height: float) -> float:
        return CARD_RADIUS if scene.card else height / 2

    def _fill(self, scene: Scene, rect, delay: float) -> None:
        """Build the scene's views in a fresh container over the capsule and fade them in."""
        old = self.content
        if old is not None:
            AppKit.NSAnimationContext.beginGrouping()
            AppKit.NSAnimationContext.currentContext().setDuration_(0.12)
            old.animator().setAlphaValue_(0.0)
            AppKit.NSAnimationContext.endGrouping()
            AppHelper.callLater(0.15, old.removeFromSuperview)
        x, y, w, h = rect
        lx, ly = self._local(x, y)
        closable = scene.closable()
        slot = CLOSE_SLOT if self._close_slot(scene) else 0.0
        # A holder over the whole capsule: the scene's own view inside it, and the island's × beside it.
        holder = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(lx, ly, w, h))
        holder.setWantsLayer_(True)
        holder.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        holder.setAlphaValue_(0.0)
        if scene.card:
            strip = 0.0 if self.face_top else ROW - 6         # the face's own strip at the bottom
            frame = AppKit.NSMakeRect(0, strip, w, h - strip)
        elif self.face_right:
            frame = AppKit.NSMakeRect(slot, 0, w - ROW - slot, h)
        else:
            frame = AppKit.NSMakeRect(ROW, 0, w - ROW - slot, h)
        view = AppKit.NSView.alloc().initWithFrame_(frame)
        view.setWantsLayer_(True)
        holder.addSubview_(view)
        self.root.addSubview_positioned_relativeTo_(holder, AppKit.NSWindowBelow, self.face_button)
        scene._acts = []
        self._close_room = closable
        try:
            scene.build(view, frame.size.width, frame.size.height)
        finally:
            self._close_room = False
        self.close_button = self._add_close(scene, holder, w, h) if closable else None
        self.content = holder

        def appear():
            if self.content is holder:
                AppKit.NSAnimationContext.beginGrouping()
                AppKit.NSAnimationContext.currentContext().setDuration_(0.2)
                holder.animator().setAlphaValue_(1.0)
                AppKit.NSAnimationContext.endGrouping()
        AppHelper.callLater(delay, appear)

    def _add_close(self, scene: Scene, holder, w: float, h: float):
        """The scene's × (gfx.close_button). Cards: the top corner by the face - beside it when the face
        is up there, else where it would be. Compact capsules: the slot at the far end from the face,
        or the face's own end when the notch hides the face."""
        size = gfx.CLOSE
        act = _IslandAct.alloc().initWithFn_(lambda: self.close_scene(scene))
        scene._acts.append(act)
        pushed = any(s is scene for s, _ in self.pushed)
        button = gfx.close_button(act, "fire:", "Close" if pushed else "Hide (nothing stops)")
        if scene.card:
            beside_face = self.face_right and self.face_top
            x = w - 40 - size if beside_face else w - 10 - size
            y = h - ROW / 2 - size / 2
        else:
            # Concentric with a rounded end of the capsule: the far end from the face, or the face's
            # own end when there is no face (notch mode).
            at_left = self.face_right if self._face_shown() else not self.face_right
            x = ROW / 2 - size / 2 if at_left else w - ROW / 2 - size / 2
            y = h / 2 - size / 2
        button.setFrameOrigin_(AppKit.NSMakePoint(x, y))
        holder.addSubview_(button)
        return button

    def _open(self, scene: Scene) -> None:
        """The orb becomes the capsule: it fades into a black circle the same size, which springs open."""
        self.anchor = self.hud.orb_center()
        if self.anchor is None:
            return
        self.scene, self.shown = scene, True
        self.hud.island_active = True
        try:
            self.hud._hide_bubble()              # the caption bubble would peek out above the capsule
        except Exception:
            pass
        width, height = self._size(scene)
        rect, face = self._layout(width, height)
        cx, cy = self.anchor
        self.panel.orderFrontRegardless()
        # Start as the orb: a black circle exactly over it, and the little face as big as the orb.
        self._shape((cx - ORB / 2, cy - ORB / 2, ORB, ORB), ORB / 2, animate=False)
        self._place_face((cx, cy), animate=False, scale=ORB / FACE * 0.8)
        fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
        fade.setFromValue_(0.0)
        fade.setToValue_(1.0)
        fade.setDuration_(0.18)
        for layer in (self.blob, self.face_host):
            layer.setOpacity_(1.0)
            layer.addAnimation_forKey_(fade, "in")
        self._orb_alpha(0.0, 0.16)
        self.face_button.setHidden_(False)

        def grow():
            if self.scene is not scene or not self.shown:
                return
            self._shape(rect, self._radius(scene, height), damping=19.0)      # a small bounce, not a wobble
            shown = self.face_host.presentationLayer() or self.face_host
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setDisableActions_(True)
            self.face_host.setAffineTransform_(Quartz.CGAffineTransformIdentity)
            Quartz.CATransaction.commit()
            self._spring(self.face_host, "transform", AppKit.NSValue.valueWithCATransform3D_(shown.transform()),
                         AppKit.NSValue.valueWithCATransform3D_(Quartz.CATransform3DIdentity))
            self._place_face(face)
        AppHelper.callLater(0.08, grow)
        self._fill(scene, rect, 0.24)

    def _morph(self, scene: Scene, previous: str) -> None:
        """One scene to another: the capsule reshapes, the contents cross-fade."""
        self.scene = scene
        width, height = self._size(scene)
        rect, face = self._layout(width, height)
        self._shape(rect, self._radius(scene, height))
        self._place_face(face)
        self._fill(scene, rect, 0.14)
        if previous.startswith("meeting:") and scene.signature() == "meeting:done":
            self.face.celebrate()

    def _close(self) -> None:
        """Back into the orb: the contents go, the capsule springs shut over the orb, the orb returns."""
        if not self.shown or self.closing:
            return
        self.closing = True
        if self.content is not None:
            AppKit.NSAnimationContext.beginGrouping()
            AppKit.NSAnimationContext.currentContext().setDuration_(0.12)
            self.content.animator().setAlphaValue_(0.0)
            AppKit.NSAnimationContext.endGrouping()
        cx, cy = self.hud.orb_center() or self.anchor
        self._shape((cx - ORB / 2, cy - ORB / 2, ORB, ORB), ORB / 2, damping=20.0)
        self._place_face((cx, cy), scale=1.0)
        self.face_button.setHidden_(True)

        def finish():
            fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
            fade.setFromValue_(1.0)
            fade.setToValue_(0.0)
            fade.setDuration_(0.2)
            for layer in (self.blob, self.face_host):
                layer.setOpacity_(0.0)
                layer.addAnimation_forKey_(fade, "out")
            self._orb_alpha(1.0, 0.2)

        def done():
            if self.content is not None:
                self.content.removeFromSuperview()
                self.content = None
            self.close_button = None
            self.scene, self.shown, self.rect, self.closing = None, False, None, False
            self.hud.island_active = False
            self.panel.setIgnoresMouseEvents_(True)
            self.hud._bubble_dirty = True
        AppHelper.callLater(0.3, finish)
        AppHelper.callLater(0.55, done)

    def _orb_alpha(self, alpha: float, seconds: float) -> None:
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(seconds)
        self.hud._orb_window.animator().setAlphaValue_(alpha)
        AppKit.NSAnimationContext.endGrouping()


island = Island()


def attach(hud) -> None:
    """Called by HUD.build."""
    try:
        island.attach(hud)
    except Exception:
        log.exception("the island failed to build")
        print("  [island failed to build - see the log]", flush=True)
