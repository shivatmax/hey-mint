"""Mint's island: the orb turns into whatever is going on, like the Dynamic Island.

When something is happening that you can act on, the orb fades into a small black capsule that
springs out of its spot. A tiny live Mint - the same swirl and eyes, blinking, following the
pointer - stays at the end where the orb was, so it is still Mint, only reshaped. When it is over
the capsule springs back into the orb.

    meeting, on a call   [• ● rec | 🎙 | 🎥 | ×]         record; mic and video on or off
    meeting, recording   [• ● 12:34 ▁▃▅▃▂ ■]            the face bobs with the call's sound
    meeting, after       [• ◌ Notes 3/10] -> [• ✓ Notes ready ↗]
    screen recording     [• ● 0:12 · screen ■]  /  [• Record this area? ✓ ✕]
    teaching a skill     [• 👁 Learning 0:42 · 12 clicks  ✓ ✕]
    a lesson (tutor)     [• Step 2 of 5 · Click Export  → ✕]
    briefing, calendar   a card: the day, the weather, a timeline of today (now lit, past dimmed),
                         reminders, headlines

Each scene is a small object (size, build, tick, mood); providers look at Mint's state ten times
a second and the highest-priority scene wins. Scenes that are pushed (the schedule card) expire by
themselves. The island never shows in screenshots or screen shares, and is click-through
everywhere but the capsule itself. Main thread only, except the show_* entry points.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
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
    """Mint watching the user do a task, to learn it as a skill."""

    key = "teach"
    priority = 80

    def provide(self, now):
        from mint.knowledge import teach
        self.snap = teach.snapshot()
        return self if self.snap else None

    def size(self):
        return (40 + 212, ROW)

    def mood(self):
        return "working"

    def build(self, view, width, height):
        mid = height / 2
        icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("eye.fill", 12))
        icon.setContentTintColor_(_ns(PURPLE))
        icon.setFrame_(AppKit.NSMakeRect(8, mid - 8, 18, 16))
        view.addSubview_(icon)
        self.title = self.label(view, "Learning", 30, mid - 1, 110, size=12)
        self.detail = self.label(view, "", 30, mid - 15, 110, size=10, weight="medium", rgb=DIM)
        self.button(view, "checkmark", self.done, 146, mid - 14, GREEN, filled=True, size=28, symbol_size=11,
                    tip="Done - save it as a skill")
        self.button(view, "xmark", self.cancel, 180, mid - 13, DIM, size=26, symbol_size=10, tip="Cancel")
        self.tick(time.time())

    def tick(self, now):
        from mint.knowledge import teach
        snap = teach.snapshot() or self.snap
        if not hasattr(self, "title"):
            return
        self.title.setStringValue_(f"Learning · {_stamp(snap.get('seconds', 0))}")
        clicks, keys = snap.get("clicks", 0), snap.get("keys", 0)
        self.detail.setStringValue_(f"{clicks} click{'s' * (clicks != 1)} · {keys} key{'s' * (keys != 1)}"
                                    if not snap.get("auto_stopped") else "Paused - press ✓ to save")

    def done(self):
        from mint.knowledge import teach
        _later(lambda: _notify_mint("(The user pressed done on the island - save what was shown.) " + teach.stop()))

    def cancel(self):
        from mint.knowledge import teach
        _later(lambda: _notify_mint("(The user cancelled the demonstration on the island.) " + teach.cancel()))


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
        self.button(view, "xmark", self.stop, 262, mid - 13, DIM, size=26, symbol_size=10, tip="End the lesson")
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
        right = width - 48 if island.face_right and island.face_top else width - 14
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

    # --- building --------------------------------------------------------------------------------

    def attach(self, hud) -> None:
        from mint.ui.hud import _panel
        from mint.ui.orb import Orb
        self.hud = hud
        screen = AppKit.NSScreen.mainScreen().frame()
        self.screen = screen
        self.panel = _panel(screen, click_through=True)
        root = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, screen.size.width, screen.size.height))
        root.setWantsLayer_(True)
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

        meeting, screen_scene, teach_scene, tutor_scene = MeetingScene(), ScreenScene(), TeachScene(), TutorScene()
        self.providers = [meeting.provide, screen_scene.provide, teach_scene.provide, tutor_scene.provide]
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

    def dismiss(self, scene: Scene) -> None:
        self.pushed = [(s, until) for s, until in self.pushed if s is not scene]

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
    def _spring(layer, key, old, new, damping=17.0, stiffness=190.0) -> None:
        anim = Quartz.CASpringAnimation.animationWithKeyPath_(key)
        anim.setFromValue_(old)
        anim.setToValue_(new)
        anim.setDamping_(damping)
        anim.setStiffness_(stiffness)
        anim.setMass_(1.0)
        anim.setDuration_(anim.settlingDuration())
        layer.addAnimation_forKey_(anim, key)

    def _shape(self, rect, radius, animate: bool = True, damping=17.0) -> None:
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
        if scene.card:
            frame = AppKit.NSMakeRect(lx, ly, w, h)
        elif self.face_right:
            frame = AppKit.NSMakeRect(lx, ly, w - ROW, h)
        else:
            frame = AppKit.NSMakeRect(lx + ROW, ly, w - ROW, h)
        view = AppKit.NSView.alloc().initWithFrame_(frame)
        view.setWantsLayer_(True)
        view.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        view.setAlphaValue_(0.0)
        self.root.addSubview_positioned_relativeTo_(view, AppKit.NSWindowBelow, self.face_button)
        scene._acts = []
        scene.build(view, frame.size.width, frame.size.height)
        self.content = view

        def appear():
            if self.content is view:
                AppKit.NSAnimationContext.beginGrouping()
                AppKit.NSAnimationContext.currentContext().setDuration_(0.2)
                view.animator().setAlphaValue_(1.0)
                AppKit.NSAnimationContext.endGrouping()
        AppHelper.callLater(delay, appear)

    def _open(self, scene: Scene) -> None:
        """The orb becomes the capsule: it fades into a black circle the same size, which springs open."""
        self.anchor = self.hud.orb_center()
        if self.anchor is None:
            return
        self.scene, self.shown = scene, True
        self.hud.island_active = True
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
            self._shape(rect, self._radius(scene, height), damping=15.0)
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
