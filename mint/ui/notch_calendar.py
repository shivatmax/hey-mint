"""The calendar, for the notch: a week strip and the day's events (a tab of the open notch).

    ┌───────────────────────────────────┐
    │ Sep    M   T  (W)  T   F   S   S   │     month and year of the selected day on the left;
    │ 2026   28  29 (30)  1   2   3   4   │     today's number in an accent circle, the selected
    │ ▌ Design review            9:30 AM │     day in a soft accent pill (click a day; scroll the
    │ ▌ Room 4                  10:00 AM │     strip sideways for other days)
    │ ▌ Lunch with Sam          12:30 PM │     a click on an event opens it in Calendar
    └───────────────────────────────────┘

Modelled on boring.notch's CalendarView (GPL-3.0, like Mint): the same type sizes (title3 month,
caption weekdays, body day numbers, callout titles, caption times), the white 0.65 greys, the 3 pt
calendar-coloured bar, the calendar-with-check empty state. Re-implemented in AppKit.

For today it lists what is still to come (in progress, upcoming, all-day); for another day, all of
it. Three rows fit in 150 pt; more scroll.

The API the notch uses (notch.py, another session's file):

    view, update = view(width, height)   # about 200-260 x 130-150 (made for 220 x 150); clear background
    available()                          # Calendars access is already granted (never asks)

The view refreshes itself: every 60 s while visible, at once when EventKit says the store changed,
when the day rolls over, and when it comes back on screen (it then selects today again, as
boring.notch does). update() re-reads and repaints now. EventKit reads run on a background thread;
building and painting are main-thread only.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import urllib.parse

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx

log = logging.getLogger("mint.ui.notch_calendar")

HEADER = 50                # the month + week strip (boring.notch's wheel is 50 tall)
ROW = 30                   # one event
DAYS_BEFORE = 2            # days shown before the selected one (boring.notch's offset)
STALE = 60.0               # seconds before a visible calendar re-reads
INK = (1.0, 1.0, 1.0)
DIM = (0.65, 0.65, 0.65)   # boring.notch's Color(white: 0.65)
FAINT = (0.5, 0.5, 0.5)

# Tests (and demos) may set this to fn(first: date, days: int) -> {date: [row, ...]} to skip EventKit.
_source = None


def _cg(rgb, alpha=1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _ns(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _font(size, weight=AppKit.NSFontWeightRegular, digits=False):
    if digits:
        return AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)
    return AppKit.NSFont.systemFontOfSize_weight_(size, weight)


def _label(size, weight=AppKit.NSFontWeightRegular, rgb=INK, alpha=1.0, digits=False):
    field = AppKit.NSTextField.labelWithString_("")
    field.setFont_(_font(size, weight, digits))
    field.setTextColor_(_ns(rgb, alpha))
    field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    field.setAllowsDefaultTighteningForTruncation_(True)
    return field


def _accent() -> tuple:
    return tuple(gfx.accent())


def _on_accent() -> tuple:
    """Ink that reads on the accent: black on a light accent (Mint's mint), white on a dark one."""
    r, g, b = _accent()
    return (0.0, 0.0, 0.0) if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.6 else INK


_formatter: list = []


def _time(when: dt.datetime) -> str:
    """The user's short time format ("9:30 AM" / "09:30")."""
    if not _formatter:
        f = AppKit.NSDateFormatter.alloc().init()
        f.setDateStyle_(AppKit.NSDateFormatterNoStyle)
        f.setTimeStyle_(AppKit.NSDateFormatterShortStyle)
        _formatter.append(f)
    return str(_formatter[0].stringFromDate_(AppKit.NSDate.dateWithTimeIntervalSince1970_(when.timestamp())))


# --- EventKit (background threads) -------------------------------------------------------------------

def available() -> bool:
    """Calendars access is granted (full access). Never prompts."""
    try:
        import EventKit
        return EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent) == \
            EventKit.EKAuthorizationStatusFullAccess
    except Exception:
        return False


def fetch(first: dt.date, days: int) -> dict:
    """{date: [row, ...]} for `days` days from `first` (any thread; {} without access). A row:
    {id, title, start, end (datetimes), all_day, place, rgb, recurring}. An event spanning days is
    listed on each of them."""
    if _source is not None:
        return _source(first, days)
    if not available():
        return {}
    import EventKit
    from Foundation import NSDate

    from mint.tools.everyday import _event_store
    store, problem = _event_store(EventKit.EKEntityTypeEvent)          # authorised: never prompts
    if problem or store is None:
        return {}
    start = dt.datetime.combine(first, dt.time.min)
    end = start + dt.timedelta(days=days)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        NSDate.dateWithTimeIntervalSince1970_(start.timestamp()),
        NSDate.dateWithTimeIntervalSince1970_(end.timestamp()), None)
    out: dict = {first + dt.timedelta(days=i): [] for i in range(days)}
    for event in store.eventsMatchingPredicate_(predicate) or []:
        try:
            begins = dt.datetime.fromtimestamp(event.startDate().timeIntervalSince1970())
            ends = dt.datetime.fromtimestamp(event.endDate().timeIntervalSince1970())
            rgb = (0.04, 0.52, 1.0)
            try:
                colour = event.calendar().color().colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
                rgb = (colour.redComponent(), colour.greenComponent(), colour.blueComponent())
            except Exception:
                pass
            row = {"id": str(event.calendarItemIdentifier() or ""), "title": str(event.title() or "(untitled)"),
                   "start": begins, "end": ends, "all_day": bool(event.isAllDay()),
                   "place": str(event.location() or "").split("\n")[0], "rgb": rgb,
                   "recurring": bool(event.hasRecurrenceRules())}
        except Exception:
            log.debug("calendar event", exc_info=True)
            continue
        day = max(first, begins.date())
        last = ends.date() if (ends.time() != dt.time.min or ends.date() == begins.date()) \
            else ends.date() - dt.timedelta(days=1)
        while day <= last and day in out:
            out[day].append(row)
            day += dt.timedelta(days=1)
    for rows in out.values():
        rows.sort(key=lambda r: (not r["all_day"], r["start"], r["title"]))
    return out


def open_event(row: dict) -> None:
    """Open the event in Calendar (the ical:// link boring.notch uses)."""
    ident = urllib.parse.quote(row.get("id") or "", safe="/:@!$&'()*+,;=-._~")
    if not ident:
        calendar = AppKit.NSURL.fileURLWithPath_("/System/Applications/Calendar.app")
        AppKit.NSWorkspace.sharedWorkspace().openURL_(calendar)
        return
    date = ""
    if row.get("recurring"):
        start = row["start"]
        if row.get("all_day"):
            date = "/" + start.strftime("%Y-%m-%dT%H:%M:%S") + start.astimezone().strftime("%z")
        else:
            date = "/" + start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    url = AppKit.NSURL.URLWithString_(f"ical://ekevent{date}/{ident}?method=show&options=more")
    if url is not None:
        AppKit.NSWorkspace.sharedWorkspace().openURL_(url)


# --- small views ---------------------------------------------------------------------------------------

class _NotchCalRoot(AppKit.NSView):
    def isFlipped(self):
        return False


class _NotchCalDay(AppKit.NSView):
    """One day of the strip: a click selects it."""

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        pass

    def mouseUp_(self, event):
        owner = getattr(self, "owner", None)
        if owner is not None and getattr(self, "day", None) is not None:
            owner.select(self.day)

    def scrollWheel_(self, event):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.wheel(event)


class _NotchCalStrip(AppKit.NSView):
    def scrollWheel_(self, event):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.wheel(event)


class _NotchCalList(AppKit.NSView):
    def isFlipped(self):
        return True


class _NotchCalRow(AppKit.NSView):
    """One event: hover lightens it, a click opens it in Calendar."""

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def updateTrackingAreas(self):
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        options = (AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways
                   | AppKit.NSTrackingInVisibleRect)
        self.addTrackingArea_(AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None))
        objc.super(_NotchCalRow, self).updateTrackingAreas()

    def mouseEntered_(self, event):
        self.layer().setBackgroundColor_(_cg(INK, 0.07))

    def mouseExited_(self, event):
        self.layer().setBackgroundColor_(_cg(INK, 0.0))

    def mouseDown_(self, event):
        pass

    def mouseUp_(self, event):
        row = getattr(self, "row", None)
        if row is not None and AppKit.NSPointInRect(
                self.convertPoint_fromView_(event.locationInWindow(), None), self.bounds()):
            open_event(row)

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.pointingHandCursor())


class _NotchCalTarget(AppKit.NSObject):
    def tick_(self, timer):
        try:
            _tick()
        except Exception:
            log.debug("calendar tick", exc_info=True)

    def storeChanged_(self, note):
        for item in list(_views):
            item.stamp = 0.0


# --- the view ---------------------------------------------------------------------------------------------

class CalendarView:
    def __init__(self, width: float, height: float) -> None:
        self.w, self.h = float(width), float(height)
        self.today = dt.date.today()
        self.selected = self.today
        self.first = self.today - dt.timedelta(days=DAYS_BEFORE)
        self.cache: dict = {}                 # date -> rows
        self.stamp = 0.0                      # when the cache was read (monotonic)
        self.loading = False
        self.shown = False                    # on screen at the last tick
        self.in_window = False
        self._wheel = 0.0
        self._sig = None
        view = _NotchCalRoot.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.w, self.h))
        view.setWantsLayer_(True)
        view.layer().setMasksToBounds_(True)
        view.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        self.view = view
        self._build()

    # building
    def _build(self) -> None:
        w, h = self.w, self.h
        self.month = _label(15, AppKit.NSFontWeightSemibold)
        self.year = _label(15, AppKit.NSFontWeightLight, DIM)
        self.view.addSubview_(self.month)
        self.view.addSubview_(self.year)
        self.month_w = 40.0
        strip_x = self.month_w + 6
        strip_w = w - strip_x
        self.count = max(5, min(7, int(strip_w // 26)))
        self.cell_w = strip_w / self.count
        self.strip = _NotchCalStrip.alloc().initWithFrame_(AppKit.NSMakeRect(strip_x, h - HEADER, strip_w, HEADER))
        self.strip.owner = self
        self.strip.setWantsLayer_(True)
        self.view.addSubview_(self.strip)
        fade = Quartz.CAGradientLayer.layer()                 # the strip's edges fade, like boring.notch's wheel
        fade.setFrame_(Quartz.CGRectMake(0, 0, strip_w, HEADER))
        fade.setStartPoint_(Quartz.CGPointMake(0, 0.5))
        fade.setEndPoint_(Quartz.CGPointMake(1, 0.5))
        edge = min(0.12, 10.0 / strip_w)
        fade.setColors_([_cg((0, 0, 0), 0.35), _cg((0, 0, 0), 1), _cg((0, 0, 0), 1), _cg((0, 0, 0), 0.35)])
        fade.setLocations_([0.0, edge, 1.0 - edge, 1.0])
        self.strip.layer().setMask_(fade)
        self.cells = []
        for i in range(self.count):
            cell = _NotchCalDay.alloc().initWithFrame_(
                AppKit.NSMakeRect(i * self.cell_w + 1, 2, self.cell_w - 2, HEADER - 4))
            cell.owner = self
            cell.setWantsLayer_(True)
            cell.layer().setCornerRadius_(8)
            letter = _label(10, AppKit.NSFontWeightMedium, DIM)
            letter.setAlignment_(AppKit.NSTextAlignmentCenter)
            letter.setFrame_(AppKit.NSMakeRect(0, HEADER - 4 - 17, self.cell_w - 2, 13))
            cell.addSubview_(letter)
            dot = Quartz.CALayer.layer()
            dot.setBounds_(Quartz.CGRectMake(0, 0, 22, 22))
            dot.setCornerRadius_(11)
            dot.setPosition_(Quartz.CGPointMake((self.cell_w - 2) / 2, 14))
            cell.layer().addSublayer_(dot)
            number = _label(13, AppKit.NSFontWeightMedium, DIM, digits=True)
            number.setAlignment_(AppKit.NSTextAlignmentCenter)
            number.setFrame_(AppKit.NSMakeRect(0, 5, self.cell_w - 2, 17))
            cell.addSubview_(number)
            self.strip.addSubview_(cell)
            self.cells.append((cell, letter, dot, number))

        list_h = h - HEADER - 6
        self.list_h = list_h
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, w, list_h))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(False)
        scroll.setHasHorizontalScroller_(False)
        scroll.setBorderType_(AppKit.NSNoBorder)
        scroll.setVerticalScrollElasticity_(AppKit.NSScrollElasticityAllowed)
        scroll.setHorizontalScrollElasticity_(AppKit.NSScrollElasticityNone)
        self.document = _NotchCalList.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, w, list_h))
        scroll.setDocumentView_(self.document)
        scroll.setWantsLayer_(True)
        self.list_fade = Quartz.CAGradientLayer.layer()        # more below: the last row fades out
        self.list_fade.setFrame_(Quartz.CGRectMake(0, 0, w, list_h))
        self.list_fade.setColors_([_cg((0, 0, 0), 0.0), _cg((0, 0, 0), 1.0), _cg((0, 0, 0), 1.0)])
        self.list_fade.setLocations_([0.0, min(0.5, 16.0 / list_h), 1.0])
        self.list_fade.setStartPoint_(Quartz.CGPointMake(0.5, 1.0))     # the scroll view's layer is flipped
        self.list_fade.setEndPoint_(Quartz.CGPointMake(0.5, 0.0))
        self.scroll = scroll
        self.view.addSubview_(scroll)

        self.empty = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, w, list_h))
        icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("calendar.badge.checkmark", 19, "regular"))
        icon.setContentTintColor_(_ns(DIM))
        icon.setFrame_(AppKit.NSMakeRect(0, 0, w, 26))
        self.empty_title = _label(11, AppKit.NSFontWeightMedium)
        self.empty_title.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.empty_sub = _label(10, AppKit.NSFontWeightRegular, DIM)
        self.empty_sub.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.empty_sub.setStringValue_("Enjoy your free time!")
        block = 26 + 3 + 15 + 14
        top = list_h / 2 + block / 2 + 2
        icon.setFrameOrigin_(AppKit.NSMakePoint(0, round(top - 26)))
        self.empty_title.setFrame_(AppKit.NSMakeRect(0, round(top - 26 - 3 - 15), w, 15))
        self.empty_sub.setFrame_(AppKit.NSMakeRect(0, round(top - 26 - 3 - 15 - 14), w, 14))
        for part in (icon, self.empty_title, self.empty_sub):
            self.empty.addSubview_(part)
        self.empty.setHidden_(True)
        self.view.addSubview_(self.empty)
        self._paint_header()

    # painting
    def _paint_header(self) -> None:
        month = self.selected.strftime("%b")
        self.month.setStringValue_(month)
        self.year.setStringValue_(str(self.selected.year))
        self.month.setFrame_(AppKit.NSMakeRect(0, self.h - 4 - 19, self.month_w + 4, 19))
        self.year.setFrame_(AppKit.NSMakeRect(0, self.h - 4 - 19 - 19, self.month_w + 4, 19))
        accent, ink = _accent(), _on_accent()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        for i, (cell, letter, dot, number) in enumerate(self.cells):
            day = self.first + dt.timedelta(days=i)
            cell.day = day
            is_today, is_selected = day == self.today, day == self.selected
            letter.setStringValue_(day.strftime("%a")[:1])
            number.setStringValue_(str(day.day))
            letter.setTextColor_(_ns(INK if is_selected else DIM))
            if is_today:                                   # boring.notch: white 0.9 on its blue
                number.setTextColor_(_ns(ink, 1.0 if ink != INK or is_selected else 0.9))
            else:
                number.setTextColor_(_ns(INK if is_selected else DIM))
            number.setFont_(_font(13, AppKit.NSFontWeightSemibold if is_today else AppKit.NSFontWeightMedium, True))
            dot.setBackgroundColor_(_cg(accent) if is_today else _cg(accent, 0.0))
            cell.layer().setBackgroundColor_(_cg(accent, 0.25) if is_selected else _cg(accent, 0.0))
            has = bool(self.cache.get(day))
            cell.setToolTip_(day.strftime("%A %-d %B") + (" · events" if has else ""))
        Quartz.CATransaction.commit()

    def _rows(self) -> list:
        rows = list(self.cache.get(self.selected) or [])
        if self.selected == self.today:
            now = dt.datetime.now()
            rows = [r for r in rows if r["all_day"] or r["end"] > now]
        return rows

    def _paint_list(self) -> None:
        rows = self._rows()
        had_any = bool(self.cache.get(self.selected))
        sig = (self.selected, tuple((r["id"], r["title"], r["start"], r["end"], r["place"], r["rgb"]) for r in rows),
               had_any, self.selected in self.cache)
        if sig == self._sig:
            return
        self._sig = sig
        for sub in list(self.document.subviews()):
            sub.removeFromSuperview()
        known = self.selected in self.cache
        self.empty.setHidden_(bool(rows) or not known)
        self.scroll.setHidden_(not rows)
        if not rows:
            if self.selected == self.today:
                self.empty_title.setStringValue_("No more events today" if had_any else "No events today")
            else:
                self.empty_title.setStringValue_("No events")
            return
        w = self.w
        total = max(self.list_h, len(rows) * ROW)
        self.document.setFrameSize_(AppKit.NSMakeSize(w, total))
        now = dt.datetime.now()
        for i, row in enumerate(rows):
            self.document.addSubview_(self._row_view(row, i, w, now))
        self.scroll.layer().setMask_(self.list_fade if len(rows) * ROW > self.list_h + 1 else None)
        self.scroll.contentView().scrollToPoint_(AppKit.NSMakePoint(0, 0))
        self.scroll.reflectScrolledClipView_(self.scroll.contentView())

    def _row_view(self, row: dict, i: int, w: float, now: dt.datetime):
        view = _NotchCalRow.alloc().initWithFrame_(AppKit.NSMakeRect(0, i * ROW, w, ROW))
        view.row = row
        view.setWantsLayer_(True)
        view.layer().setCornerRadius_(6)
        bar = Quartz.CALayer.layer()
        bar.setFrame_(Quartz.CGRectMake(2, 4, 3, ROW - 8))
        bar.setCornerRadius_(1.5)
        bar.setBackgroundColor_(_cg(row["rgb"]))
        view.layer().addSublayer_(bar)
        times_w = 52
        text_w = w - 11 - times_w - 4
        title = _label(12, AppKit.NSFontWeightMedium)
        title.setStringValue_(row["title"])
        place = row.get("place") or ""
        if place:
            title.setFrame_(AppKit.NSMakeRect(11, 2, text_w, 15))
            sub = _label(10, AppKit.NSFontWeightRegular, DIM)
            sub.setStringValue_(place)
            sub.setFrame_(AppKit.NSMakeRect(11, 16, text_w, 13))
            view.addSubview_(sub)
        else:
            title.setFrame_(AppKit.NSMakeRect(11, (ROW - 15) / 2, text_w, 15))
        view.addSubview_(title)
        if row["all_day"]:
            when = _label(10, AppKit.NSFontWeightMedium)
            when.setStringValue_("All-day")
            when.setAlignment_(AppKit.NSTextAlignmentRight)
            when.setFrame_(AppKit.NSMakeRect(w - times_w - 2, (ROW - 13) / 2, times_w, 13))
            view.addSubview_(when)
        else:
            for text, rgb, y in ((_time(row["start"]), INK, 3), (_time(row["end"]), DIM, 16)):
                when = _label(10, AppKit.NSFontWeightRegular, rgb, digits=True)
                when.setStringValue_(text)
                when.setAlignment_(AppKit.NSTextAlignmentRight)
                when.setFrame_(AppKit.NSMakeRect(w - times_w - 2, y, times_w, 13))
                view.addSubview_(when)
        live = not row["all_day"] and row["start"] <= now < row["end"]
        view.setToolTip_(f"{row['title']}" + (f" — {place}" if place else "") + (" (now)" if live else "")
                         + "\nClick to open in Calendar")
        if i:
            line = Quartz.CALayer.layer()
            line.setFrame_(Quartz.CGRectMake(11, 0, w - 13, 0.5))
            line.setBackgroundColor_(_cg(FAINT, 0.35))
            view.layer().addSublayer_(line)
        return view

    def paint(self) -> None:
        self._paint_header()
        self._paint_list()

    # data
    def _window(self) -> tuple:
        first = min(self.first, self.selected, self.today)
        last = max(self.first + dt.timedelta(days=self.count - 1), self.selected, self.today)
        return first, (last - first).days + 1

    def refresh(self) -> None:
        """Read the shown days in the background, then repaint (main thread)."""
        if self.loading:
            return
        self.loading = True
        first, days = self._window()
        asked = time.monotonic()

        def work():
            try:
                got = fetch(first, days)
            except Exception:
                log.exception("calendar fetch")
                got = {}
            AppHelper.callAfter(self._got, got, asked)
        threading.Thread(target=work, daemon=True, name="notch-calendar").start()

    def _got(self, got: dict, asked: float) -> None:
        self.loading = False
        self.cache = {d: rows for d, rows in self.cache.items() if abs((d - self.today).days) <= 45}
        self.cache.update(got)
        self.stamp = asked
        self.paint()

    def _need(self) -> bool:
        first, days = self._window()
        return any((first + dt.timedelta(days=i)) not in self.cache for i in range(days))

    # interaction
    def select(self, day: dt.date) -> None:
        self.selected = day
        if not (self.first <= day < self.first + dt.timedelta(days=self.count)):
            self.first = day - dt.timedelta(days=DAYS_BEFORE)
        self.paint()
        if self._need():
            self.refresh()

    def wheel(self, event) -> None:
        dx = event.scrollingDeltaX()
        dy = event.scrollingDeltaY()
        delta = dx if abs(dx) >= abs(dy) else dy
        if not event.hasPreciseScrollingDeltas():
            delta *= 12
        self._wheel += delta
        step = 0
        while self._wheel >= 22:
            self._wheel -= 22
            step -= 1
        while self._wheel <= -22:
            self._wheel += 22
            step += 1
        if step:
            self.first += dt.timedelta(days=step)
            self.paint()
            if self._need():
                self.refresh()

    def reset(self) -> None:
        """Back to today (the notch opened again, or the day rolled over)."""
        self.today = dt.date.today()
        self.selected = self.today
        self.first = self.today - dt.timedelta(days=DAYS_BEFORE)
        self._wheel = 0.0
        self.paint()

    def update(self, *_ignored) -> None:
        """Re-read now and repaint what is known at once."""
        self.stamp = 0.0
        self._sig = None
        self.paint()
        self.refresh()

    def tick(self, visible: bool) -> None:
        if not visible:
            self.shown = False
            return
        came_back = not self.shown
        self.shown = True
        if dt.date.today() != self.today or came_back:
            self.reset()
        if self.selected == self.today:
            self._paint_list()                # events end: today's list drops them as time passes
        if not self.loading and (self._need() or time.monotonic() - self.stamp > (15.0 if came_back else STALE)):
            self.refresh()


# --- keeping them fresh -------------------------------------------------------------------------------

_views: list = []
_timer: list = []


def _tick() -> None:
    for item in list(_views):
        window = item.view.window()
        if window is None:
            if item.in_window:                  # taken out of the notch: forget it
                _views.remove(item)
            continue
        item.in_window = True
        item.tick(bool(window.isVisible()) and not item.view.isHiddenOrHasHiddenAncestor())


def _attach() -> None:
    """Main thread, once: the refresh timer and EventKit's change notification."""
    if _timer:
        return
    target = _NotchCalTarget.alloc().init()
    timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0, target, "tick:", None, True)
    try:
        import EventKit
        AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            target, "storeChanged:", EventKit.EKEventStoreChangedNotification, None)
    except Exception:
        log.debug("EventKit change notification", exc_info=True)
    _timer.extend([target, timer])


def view(width: float, height: float):
    """For the notch: (NSView, update). Main thread. See the module docstring for when it refreshes."""
    _attach()
    item = CalendarView(width, height)
    _views.append(item)
    item.paint()
    item.refresh()
    return item.view, item.update
