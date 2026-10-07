"""The notch's behaviour, without the drawing: which alert it shows, and when a notch you opened closes by itself.

Pure Python (no AppKit, no timers), so it is unit-tested. notch.py feeds it what happened with the time
(time.monotonic seconds), asks what to show, and `next_wake(now)` says when the answer changes by itself
(a "done" runs out, the countdown starts, it closes) - a deadline instead of polling.

    st = NotchState()
    st.pointer(inside, now, (x, y))     # every look at the pointer: over the notch or not, and where
    st.click(now)                       # a click on the notch: opens it, or folds it ("opened" / "closed")
    st.open_by("hover", now)            # opened another way: resting on it ("hover"), the sparkles button or voice
    st.keep(now)                        # something opened from it is up (a menu, Quick Look, typing): don't close
    st.snooze(now)                      # Esc: closes, news goes, needs-you alerts wait until you open it
    st.idle(seconds)                    # the computer's idle time: >= 180 s you're away and news waits
    st.alert(id, kind, now, session=)   # "need" | "done" | "error" (ignored if that id is already queued)
    st.resolve(now, id= | session=, kind=)   # an alert is over (answered, the agent moved on)
    st.sync_needs(keys, now)            # the sessions that need you right now: adds and resolves "need" alerts
    st.tick(now)                        # time passed (see next_wake)
    st.shown() / st.queued() / st.countdown(now) / st.peeking(now) / st.is_open

Rules (dotpals' notch, in Mint's units):
- Alerts show one at a time: needs-you first, and they stay until answered; then news ("done", "error") in the
  order it came. A "done" shows 5 s, an "error" 8 s, each timed from when it actually shows (news pushed aside
  by a needs-you alert starts over when it shows again). News waits while you're away and is dropped after 15 min.
  Moving the pointer over news makes it yours: it stays open like a notch you opened.
- Opened by you (a click, resting on it, the sparkles button), it closes 8 s after the pointer leaves, or after
  60 s with the pointer resting on it untouched - never while someone needs you. The last 3 s show as a
  shrinking line (countdown).
- Once it closes, the pointer still on it doesn't open it again (no peek, no rest-to-open) until it leaves (armed).
- Resting means resting: the peek's dwell starts over each time the pointer wanders more than 6 pt, so sliding
  along the top edge to a browser tab never opens it.

Ported from dotpals' bridge/ui/notch-state.js (github.com/Rikinshah787/dotpals @ 5ef4dcb), MIT License,
Copyright (c) dotpals contributors. Permission is hereby granted, free of charge, to any person obtaining a copy
of this software, to deal in it without restriction, provided the copyright notice and this permission notice
are included in all copies or substantial portions of the software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT
WARRANTY OF ANY KIND.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

TIMING = {
    "peek": 0.3,            # resting on the notch this long shows the small row
    "restless": 6.0,        # the pointer moving farther than this (points) starts the rest over
    "leave_grace": 0.1,     # away this long, the pointer has left (crossing a gap doesn't count)
    "done": 5.0,            # a "done" alert shows this long
    "error": 8.0,           # an "error" alert shows this long
    "auto_close": 60.0,     # opened, the pointer resting on it untouched this long -> it closes
    "after_leave": 8.0,     # opened, the pointer left this long ago -> it closes (it covers tabs and title bars)
    "countdown": 3.0,       # the last part of either, shown as a shrinking line
    "away": 180.0,          # no input on the computer this long -> you're away, news waits
    "stale_news": 900.0,    # news older than this isn't worth showing
}

NEED, DONE, ERROR = "need", "done", "error"
KINDS = (NEED, DONE, ERROR)


@dataclass
class Alert:
    id: str
    kind: str                       # need / done / error
    session: str | None = None
    text: str | None = None
    at: float = 0.0                 # when it came
    shown_at: float | None = None   # when it (last) started showing: news runs out from here
    snoozed: bool = False           # Esc: a needs-you alert waits until you open the notch

    @property
    def news(self) -> bool:
        return self.kind != NEED


@dataclass
class Opened:
    by: str                         # click / hover / voice
    at: float
    active_at: float                # the last time you did something on it
    left_at: float | None = None    # when the pointer left it (None: it's on it)


@dataclass
class NotchState:
    timing: dict = field(default_factory=dict)
    hover: bool = False             # the pointer is over the notch
    hover_at: float = 0.0           # the peek's dwell started here
    still_at: float = 0.0           # the pointer last wandered (more than "restless") here
    inside_at: float = 0.0          # the last look that found the pointer over it
    armed: bool = True              # hovering may peek / open it (False after it closed under the pointer)
    away: bool = False
    open: Opened | None = None
    alerts: list = field(default_factory=list)
    _anchor: tuple | None = None    # where the current rest started
    _last: tuple | None = None      # the pointer at the last look

    def __post_init__(self) -> None:
        self.T = {**TIMING, **(self.timing or {})}

    # --- what to show ------------------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.open is not None

    def _ok(self, a: Alert) -> bool:
        return (not a.snoozed or self.open is not None) and not (self.away and a.news)

    def shown(self) -> Alert | None:
        """The alert on show: needs-you first, then news, each in arrival order."""
        for a in self.alerts:
            if a.kind == NEED and self._ok(a):
                return a
        for a in self.alerts:
            if a.news and self._ok(a):
                return a
        return None

    def queued(self) -> int:
        """Alerts waiting behind the one on show ("+N waiting")."""
        on = self.shown()
        return sum(1 for a in self.alerts if a is not on and not (self.away and a.news))

    def lasts(self, a: Alert) -> float:
        return self.T["error"] if a.kind == ERROR else self.T["done"]

    def peeking(self, now: float) -> bool:
        """The pointer has rested on the closed notch long enough to show the small row."""
        return self.hover and self.armed and self.open is None and now - self.hover_at >= self.T["peek"]

    def rested(self, now: float) -> float:
        """How long the pointer has stayed put (within "restless") on the notch."""
        return now - self.still_at if self.hover else 0.0

    def closes_at(self) -> float | None:
        """When a notch you opened closes by itself (None: not open, or someone needs you)."""
        if self.open is None:
            return None
        on = self.shown()
        if on is not None and on.kind == NEED:
            return None
        quiet = self.open.active_at + self.T["auto_close"]
        if not self.hover and self.open.left_at is not None:
            return min(quiet, self.open.left_at + self.T["after_leave"])
        return quiet

    def countdown(self, now: float) -> tuple | None:
        """(start, end) of the shrinking line while it is due: the last seconds of a notch you opened, or of
        news on show."""
        cd = self.T["countdown"]
        end = None
        if self.open is not None:
            end = self.closes_at()
        else:
            on = self.shown()
            if on is not None and on.news and on.shown_at is not None:
                end = on.shown_at + self.lasts(on)
        if end is None or now < end - cd:
            return None
        return (end - cd, end)

    def derive(self, now: float) -> dict:
        on = self.shown()
        return {"open": self.open is not None or on is not None, "by": self.open.by if self.open else
                ("alert" if on else None), "alert": on, "queued": self.queued(), "countdown": self.countdown(now),
                "peek": self.peeking(now)}

    # --- events -----------------------------------------------------------------------------------------

    def pointer(self, inside: bool, now: float, point: tuple | None = None) -> None:
        moved = point is not None and self._last is not None and \
            math.hypot(point[0] - self._last[0], point[1] - self._last[1]) > 0.5
        if point is not None:
            self._last = (float(point[0]), float(point[1]))
        if moved:
            self.away = False                       # it moved: you're here
        if inside:
            if not self.hover:
                self.hover, self.hover_at, self.still_at = True, now, now
                self._anchor = self._last
                if self.open is not None:
                    self.open.left_at = None
            elif self._anchor is not None and self._last is not None and \
                    math.hypot(self._last[0] - self._anchor[0], self._last[1] - self._anchor[1]) > self.T["restless"]:
                # Wandering, not resting: the rest starts over here. Before the small row shows, so does the
                # peek (sliding along the top edge to a tab never opens it); once it shows, moving to its
                # buttons keeps it.
                self._anchor, self.still_at = self._last, now
                if self.open is None and now - self.hover_at < self.T["peek"]:
                    self.hover_at = now
            self.inside_at = now
            if self.open is not None:
                self.open.left_at = None
                if moved:
                    self.open.active_at = now
            elif moved:
                # Moving over news ("done", "error") makes it yours: it stays open like one you opened.
                # (A needs-you alert closes once it's answered.)
                on = self.shown()
                if on is not None and on.news:
                    self.alerts.remove(on)
                    self.open = Opened("hover", now, now)
        elif self.hover and now - self.inside_at >= self.T["leave_grace"]:
            self.hover, self._anchor = False, None
            self.armed = True
            if self.open is not None:
                self.open.left_at = self.inside_at
        self.tick(now)

    def click(self, now: float) -> str:
        """A click on the notch: open it, or fold it back. Returns "opened" or "closed"."""
        self.away = False
        if self.open is not None:
            self.open = None
            self.armed = not self.hover            # folded under the pointer: no peek until it leaves
            self.tick(now)
            return "closed"
        self.open = Opened("click", now, now, None if self.hover else now)
        self.tick(now)
        return "opened"

    def open_by(self, by: str, now: float) -> None:
        """Opened by resting on it ("hover"), the sparkles button or voice ("voice"). Asked from elsewhere (the
        pointer isn't on it), it closes after_leave seconds later unless the pointer comes."""
        if self.open is None:
            self.open = Opened(by, now, now, None if self.hover else now)
        else:
            self.open.active_at = now
            if not self.hover:
                self.open.left_at = now
        self.tick(now)

    def close(self, now: float) -> None:
        """Folded by you (not Esc): the alerts stay."""
        if self.open is not None:
            self.open = None
            self.armed = not self.hover
        self.tick(now)

    def keep(self, now: float) -> None:
        """Something opened from the notch is up (a menu, Quick Look, a share sheet, typing in Search)."""
        if self.open is not None:
            self.open.active_at, self.open.left_at = now, None

    def snooze(self, now: float) -> None:
        """Esc: it closes at once; news on show goes, needs-you alerts wait until you open it."""
        was = self.open is not None or self.shown() is not None
        self.open = None
        self.alerts = [a for a in self.alerts if not a.news]
        for a in self.alerts:
            a.snoozed = True
        if was:
            self.armed = not self.hover
        self.tick(now)

    def idle(self, seconds: float) -> None:
        self.away = float(seconds) >= self.T["away"]

    def alert(self, id: str, kind: str, now: float, session: str | None = None, text: str | None = None) -> bool:
        """Queue an alert. False if one with this id is already queued."""
        if id is None or any(a.id == id for a in self.alerts):
            return False
        self.alerts.append(Alert(str(id), kind if kind in KINDS else NEED, session, text, now))
        self.tick(now)
        return True

    def resolve(self, now: float, id: str | None = None, session: str | None = None, kind: str | None = None) -> None:
        """An alert is over: by id, or every alert of a session (of one kind)."""
        if id is not None:
            self.alerts = [a for a in self.alerts if a.id != id]
        else:
            self.alerts = [a for a in self.alerts if not (a.session == session and (kind is None or a.kind == kind))]
        self.tick(now)

    def sync_needs(self, keys, now: float) -> None:
        """The sessions that need you right now: a "need" alert for each new one (also those waiting from
        before Mint started), and the ones answered are resolved."""
        keys = set(keys or ())
        before = len(self.alerts)
        self.alerts = [a for a in self.alerts if a.kind != NEED or a.session in keys]
        have = {a.session for a in self.alerts if a.kind == NEED}
        for key in keys:
            if key not in have:
                self.alerts.append(Alert("need:" + key, NEED, key, None, now))
        if len(self.alerts) != before or keys - have:
            self.tick(now)

    def tick(self, now: float) -> None:
        """Apply what time decides: news runs out, a notch you opened closes by itself."""
        was = self.open is not None or self.shown() is not None
        self.alerts = [a for a in self.alerts if not a.news or now - a.at < self.T["stale_news"]]
        for _guard in range(50):
            on = self.shown()
            for a in self.alerts:
                if a is not on and a.shown_at is not None:
                    a.shown_at = None               # pushed aside: its time starts over when it shows again
            if on is None or not on.news:
                if on is not None and on.shown_at is None:
                    on.shown_at = now
                break
            if on.shown_at is None:
                on.shown_at = now
                break
            if now - on.shown_at < self.lasts(on):
                break
            self.alerts.remove(on)
        end = self.closes_at()
        if end is not None and now >= end:
            self.open = None
        if was and self.open is None and self.shown() is None:
            self.armed = not self.hover             # it closed under the pointer: no reopening until it leaves

    def next_wake(self, now: float) -> float:
        """Seconds until the state should be looked at again (math.inf: only events change it)."""
        at = []
        on = self.shown()
        cd = self.T["countdown"]
        if self.hover and self.armed and self.open is None and on is None:
            at.append(self.hover_at + self.T["peek"])
        if on is not None and on.news and on.shown_at is not None:
            end = on.shown_at + self.lasts(on)
            at += [end - cd, end]
        end = self.closes_at()
        if end is not None:
            at += [end - cd, end]
        for a in self.alerts:
            if a.news:
                at.append(a.at + self.T["stale_news"])
        future = [t for t in at if t > now]
        return max(0.0, min(future) - now) if future else math.inf
