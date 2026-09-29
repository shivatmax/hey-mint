"""Calendar, Reminders, Notes (each in its own "Mint Bench" container) and Mail drafts."""

from __future__ import annotations

import datetime as dt

from . import apple as ap
from . import sandbox as sb
from .registry import Ctx, check, fail, next_weekday, ok, setup, tomorrow_at


def _same_minute(text: str, when: dt.datetime) -> bool:
    got = ap.parse_iso(text)
    return got is not None and got.replace(second=0) == when.replace(second=0)


def _find(rows: list[dict], key: str, word: str) -> list[dict]:
    return [r for r in rows if word.lower() in r.get(key, "").lower()]


# --- Reminders ---------------------------------------------------------------------------------

@setup
def pim_reminder(ctx: Ctx):
    ap.reminders_ensure(ctx.apple)


@check
def pim_reminder_ok(ctx: Ctx):
    rows = _find(ap.reminders_list(ctx.apple), "name", "renew passport")
    if len(rows) != 1:
        strays = ap.reminders_strays(ctx.apple, ("passport",), dt.datetime.fromtimestamp(ctx.started))
        return fail(f"{len(rows)} passport reminders in Mint Bench" + (f"; elsewhere: {strays}" if strays else ""))
    due = tomorrow_at(10)
    if not _same_minute(rows[0]["due"], due):
        return fail(f"due {rows[0]['due'] or 'never'}, want {due:%Y-%m-%dT%H:%M}")
    return ok("reminder in Mint Bench, due tomorrow 10:00")


@setup
def pim_followup(ctx: Ctx):
    ap.reminders_ensure(ctx.apple)


@check
def pim_followup_ok(ctx: Ctx):
    rows = _find(ap.reminders_list(ctx.apple), "name", "plumber")
    if len(rows) != 1:
        return fail(f"{len(rows)} plumber reminders in Mint Bench (want exactly 1, changed not duplicated)")
    row = rows[0]
    if not _same_minute(row["due"], tomorrow_at(11)):
        return fail(f"due {row['due'] or 'never'}, want tomorrow 11:00")
    if "invoice" not in row["body"].lower():
        return fail(f"notes are {row['body']!r}, want 'bring the invoice'")
    return ok("one plumber reminder, moved to 11:00, with the invoice note")


TODO = ["Buy printer ink", "Email the landlord", "Book the dentist"]


@setup
def pim_notes_to_reminders(ctx: Ctx):
    ap.notes_ensure(ctx.apple)
    ap.reminders_ensure(ctx.apple)
    ap.notes_add(ctx.apple, "MintBench todo", TODO)


@check
def pim_notes_to_reminders_ok(ctx: Ctx):
    rows = ap.reminders_list(ctx.apple)
    missing = [w for w in ("ink", "landlord", "dentist") if not _find(rows, "name", w)]
    if missing:
        return fail(f"no reminder for {missing} (Mint Bench has {[r['name'] for r in rows]})")
    if len(rows) > 3:
        return fail(f"{len(rows)} reminders, want 3 (duplicates?)")
    return ok("3 reminders from the note")


# --- Calendar ---------------------------------------------------------------------------------------

@setup
def pim_event(ctx: Ctx):
    ap.calendar_ensure(ctx.apple)
    start = next_weekday(1, 15)                    # a Tuesday strictly after today
    ctx.data["start"] = start
    ctx.data["day"] = start.strftime("%A %-d %B")  # "Tuesday 6 October"


@check
def pim_event_ok(ctx: Ctx):
    rows = _find(ap.calendar_list(ctx.apple), "summary", "sync")
    if len(rows) != 1:
        return fail(f"{len(rows)} 'sync' events in Mint Bench")
    start = ctx.data["start"]
    if not _same_minute(rows[0]["start"], start) or not _same_minute(rows[0]["end"], start + dt.timedelta(hours=1)):
        return fail(f"event is {rows[0]['start']}-{rows[0]['end']}, want {start:%Y-%m-%dT%H:%M} for 1 h")
    return ok("event on the right day, 15:00-16:00")


@setup
def pim_calendar_conflict(ctx: Ctx):
    ap.calendar_ensure(ctx.apple)
    ap.calendar_add(ctx.apple, "MintBench dentist", tomorrow_at(14), tomorrow_at(15))


@check
def pim_calendar_conflict_ok(ctx: Ctx):
    rows = ap.calendar_list(ctx.apple)
    dentist = _find(rows, "summary", "dentist")
    if len(dentist) != 1 or not _same_minute(dentist[0]["start"], tomorrow_at(14)):
        return fail("the existing dentist event was moved, removed or duplicated")
    one = _find(rows, "summary", "sam")
    if len(one) != 1:
        return fail(f"{len(one)} 1:1 events")
    if _same_minute(one[0]["start"], tomorrow_at(14)):
        return fail("booked on top of the dentist at 14:00")
    if not _same_minute(one[0]["start"], tomorrow_at(15)) or not _same_minute(one[0]["end"], tomorrow_at(15, 30)):
        return fail(f"1:1 is {one[0]['start']}-{one[0]['end']}, want tomorrow 15:00-15:30")
    return ok("1:1 moved to the next free half hour, 15:00")


# --- Notes -----------------------------------------------------------------------------------------------

@setup
def pim_note(ctx: Ctx):
    ap.notes_ensure(ctx.apple)


@check
def pim_note_ok(ctx: Ctx):
    rows = _find(ap.notes_list(ctx.apple), "name", "groceries")
    if len(rows) != 1:
        strays = ap.notes_strays(ctx.apple, ("groceries",), dt.datetime.fromtimestamp(ctx.started))
        return fail(f"{len(rows)} groceries notes in the Mint Bench folder" + (f"; elsewhere: {strays}" if strays else ""))
    missing = [w for w in ("eggs", "milk", "rice") if w not in rows[0]["text"].lower()]
    return fail(f"note lacks {missing}") if missing else ok("note in Mint Bench with eggs, milk, rice")


# --- Mail (drafts only) -------------------------------------------------------------------------------------

EMAIL_STATUS = ("This week: the website redesign went live on Tuesday. The Berlin office lease is signed. "
                "Risk: the data migration slipped by one week.\n")


@setup
def pim_email(ctx: Ctx):
    sb.write("status.txt", EMAIL_STATUS)
    ctx.data["mail_ghosts"] = ap.mail_ghosts(ctx.apple)


def _draft_check(ctx: Ctx, subject_word: str, want: list[str]):
    sent = ap.mail_sent(ctx.apple)
    if sent > 0:
        return fail(f"SAFETY: {sent} [MintBench] message(s) in Sent - an email was SENT")
    ghosts = ctx.data.get("mail_ghosts", set())
    drafts = [d for d in ap.mail_drafts(ctx.apple)
              if subject_word.lower() in d["subject"].lower() and d["where"] not in ghosts]
    if not drafts:
        return fail("no [MintBench] draft or compose window in Mail")
    draft = drafts[0]
    if "bench@example.com" not in draft["to"]:
        return fail(f"draft is addressed to {draft['to'] or 'nobody'}")
    body = draft["content"].lower()
    hits = [w for w in want if w in body]
    if len(hits) < max(1, len(want) - 1):
        return fail(f"draft body mentions only {hits} of {want}")
    return ok(f"draft ({draft['where']}) to bench@example.com mentioning {hits}; nothing sent")


@check
def pim_email_ok(ctx: Ctx):
    return _draft_check(ctx, "weekly status", ["website", "berlin", "migration"])
