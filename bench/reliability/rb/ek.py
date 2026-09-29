"""Reminders and Calendar fixtures through EventKit, when this process already has full access.

Reminders' AppleScript took ~110 s for one `exists list` on a Mac with big iCloud lists (29 Sep),
so EventKit is preferred. A fresh store per call: Mint changes the same database from its own process.
"""

from __future__ import annotations

import datetime as dt
import threading

TITLE = "Mint Bench"


def _ek():
    import EventKit
    return EventKit


def authorized(kind: str) -> bool:
    """Full access already granted (no prompt is ever shown from here)."""
    E = _ek()
    entity = E.EKEntityTypeReminder if kind == "reminders" else E.EKEntityTypeEvent
    return E.EKEventStore.authorizationStatusForEntityType_(entity) == getattr(E, "EKAuthorizationStatusFullAccess", 3)


def request(kind: str, wait: float = 120.0) -> bool:
    """Ask macOS for full access (shows the system prompt once, if never answered). True if granted.
    Only when access was never decided: a denial stays denied and is not asked again."""
    E = _ek()
    entity = E.EKEntityTypeReminder if kind == "reminders" else E.EKEntityTypeEvent
    if E.EKEventStore.authorizationStatusForEntityType_(entity) != getattr(E, "EKAuthorizationStatusNotDetermined", 0):
        return authorized(kind)
    done = threading.Event()
    result = {"ok": False}

    def answered(ok, error):
        result["ok"] = bool(ok)
        done.set()

    store = _store()
    if kind == "reminders":
        store.requestFullAccessToRemindersWithCompletion_(answered)
    else:
        store.requestFullAccessToEventsWithCompletion_(answered)
    done.wait(wait)
    return result["ok"]


def _store():
    return _ek().EKEventStore.alloc().init()


def _nsdate(when: dt.datetime):
    from Foundation import NSDate
    return NSDate.dateWithTimeIntervalSince1970_(when.timestamp())


def _py(nsdate) -> dt.datetime | None:
    return dt.datetime.fromtimestamp(nsdate.timeIntervalSince1970()) if nsdate is not None else None


def _components(when: dt.datetime):
    from Foundation import NSDateComponents
    c = NSDateComponents.alloc().init()
    c.setYear_(when.year)
    c.setMonth_(when.month)
    c.setDay_(when.day)
    c.setHour_(when.hour)
    c.setMinute_(when.minute)
    return c


def _from_components(c) -> dt.datetime | None:
    if c is None:
        return None
    big = 2 ** 62                                   # NSDateComponentUndefined
    hour = c.hour() if c.hour() < big else 0
    minute = c.minute() if c.minute() < big else 0
    try:
        return dt.datetime(c.year(), c.month(), c.day(), hour, minute)
    except ValueError:
        return None


def _calendar(store, kind: str):
    E = _ek()
    entity = E.EKEntityTypeReminder if kind == "reminders" else E.EKEntityTypeEvent
    return next((c for c in store.calendarsForEntityType_(entity) if c.title() == TITLE), None)


def ensure(kind: str) -> None:
    E = _ek()
    store = _store()
    if _calendar(store, kind) is not None:
        return
    entity = E.EKEntityTypeReminder if kind == "reminders" else E.EKEntityTypeEvent
    calendar = E.EKCalendar.calendarForEntityType_eventStore_(entity, store)
    calendar.setTitle_(TITLE)
    default = store.defaultCalendarForNewReminders() if kind == "reminders" else store.defaultCalendarForNewEvents()
    calendar.setSource_(default.source())
    ok, error = store.saveCalendar_commit_error_(calendar, True, None)
    if not ok:
        raise RuntimeError(f"could not make the {TITLE} {kind} calendar: {error}")


def exists(kind: str) -> bool:
    return _calendar(_store(), kind) is not None


def remove(kind: str) -> None:
    store = _store()
    calendar = _calendar(store, kind)
    if calendar is not None:
        ok, error = store.removeCalendar_commit_error_(calendar, True, None)
        if not ok:
            raise RuntimeError(f"could not remove the {TITLE} {kind} calendar: {error}")


# --- reminders ------------------------------------------------------------------------------------------

def _fetch(store, calendars) -> list:
    done = threading.Event()
    found: list = []

    def got(reminders):
        found.extend(reminders or [])
        done.set()

    store.fetchRemindersMatchingPredicate_completion_(store.predicateForRemindersInCalendars_(calendars), got)
    done.wait(60)
    return found


def reminder_add(name: str, due: dt.datetime | None = None, body: str = "") -> None:
    E = _ek()
    store = _store()
    reminder = E.EKReminder.reminderWithEventStore_(store)
    reminder.setTitle_(name)
    reminder.setCalendar_(_calendar(store, "reminders"))
    if body:
        reminder.setNotes_(body)
    if due is not None:
        reminder.setDueDateComponents_(_components(due))
    ok, error = store.saveReminder_commit_error_(reminder, True, None)
    if not ok:
        raise RuntimeError(f"could not save the reminder: {error}")


def reminders() -> list[dict]:
    store = _store()
    calendar = _calendar(store, "reminders")
    if calendar is None:
        return []
    rows = []
    for r in _fetch(store, [calendar]):
        due = _from_components(r.dueDateComponents())
        rows.append({"name": str(r.title() or ""), "due": due.isoformat(timespec="minutes") if due else "",
                     "body": str(r.notes() or ""), "done": str(bool(r.isCompleted())).lower()})
    return rows


def _marked(title: str, markers: tuple[str, ...]) -> bool:
    return any(m.lower() in title.lower() for m in markers)


def reminder_strays(markers: tuple[str, ...], since: dt.datetime, delete: bool = False) -> list[str]:
    store = _store()
    found = []
    for r in _fetch(store, None):
        calendar = r.calendar()
        if calendar is not None and calendar.title() == TITLE:
            continue
        made = _py(r.creationDate())
        if made is not None and made >= since and _marked(str(r.title() or ""), markers):
            found.append(f"{calendar.title() if calendar else '?'}: {r.title()}")
            if delete:
                store.removeReminder_commit_error_(r, True, None)
    return found


# --- events -------------------------------------------------------------------------------------------------

def event_add(summary: str, start: dt.datetime, end: dt.datetime) -> None:
    E = _ek()
    store = _store()
    event = E.EKEvent.eventWithEventStore_(store)
    event.setTitle_(summary)
    event.setStartDate_(_nsdate(start))
    event.setEndDate_(_nsdate(end))
    event.setCalendar_(_calendar(store, "calendar"))
    ok, error = store.saveEvent_span_commit_error_(event, E.EKSpanThisEvent, True, None)
    if not ok:
        raise RuntimeError(f"could not save the event: {error}")


def _events(store, calendars, since: dt.datetime) -> list:
    start = since - dt.timedelta(days=2)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        _nsdate(start), _nsdate(start + dt.timedelta(days=60)), calendars)
    return list(store.eventsMatchingPredicate_(predicate) or [])


def events() -> list[dict]:
    store = _store()
    calendar = _calendar(store, "calendar")
    if calendar is None:
        return []
    fmt = "%Y-%m-%dT%H:%M"
    return [{"summary": str(e.title() or ""), "start": _py(e.startDate()).strftime(fmt),
             "end": _py(e.endDate()).strftime(fmt), "notes": str(e.notes() or "")}
            for e in _events(store, [calendar], dt.datetime.now())]


def event_strays(markers: tuple[str, ...], since: dt.datetime, delete: bool = False) -> list[str]:
    E = _ek()
    store = _store()
    found = []
    for e in _events(store, None, since):
        calendar = e.calendar()
        if calendar is not None and calendar.title() == TITLE:
            continue
        if _marked(str(e.title() or ""), markers):
            found.append(f"{calendar.title() if calendar else '?'}: {e.title()}")
            if delete:
                store.removeEvent_span_commit_error_(e, E.EKSpanThisEvent, True, None)
    return found
