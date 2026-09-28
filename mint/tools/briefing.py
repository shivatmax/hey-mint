"""The daily briefing: "brief me", "what's my day like?", "good morning".

One spoken summary of the day, from what is on this Mac and a little from the web:

* today's calendar and the reminders due today (EventKit),
* the newest mail in Mail.app's inbox (which unread, from whom),
* unfinished tasks and today's meeting notes (tasks.py, meetings.py),
* the weather where the Mac is (wttr.in, no key) and a few headlines (Google News RSS,
  no key) - both skipped quietly when offline.

Everything is gathered in parallel (a few seconds), then Gemini Flash Lite writes it as
something to be SAID: about a minute, the important things first. As an automation
("every weekday at 8:30, brief me") it runs by itself.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

log = logging.getLogger("mint.tools.briefing")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Mint"}


def _calendar() -> str:
    from mint.tools import everyday as skills
    return skills.calendar_events(1)


def _reminders() -> str:
    """Incomplete reminders due by the end of today (and overdue ones)."""
    import threading
    import EventKit
    from Foundation import NSDate
    from mint.tools import everyday as skills
    store, problem = skills._event_store(EventKit.EKEntityTypeReminder)
    if problem:
        return ""
    end = dt.datetime.combine(dt.date.today(), dt.time.max)
    predicate = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
        None, NSDate.dateWithTimeIntervalSince1970_(end.timestamp()), None)
    found: list = []
    done = threading.Event()

    def got(items):
        found.extend(items or [])
        done.set()
    store.fetchRemindersMatchingPredicate_completion_(predicate, got)
    done.wait(8)
    return "; ".join(str(r.title()) for r in found[:12])


def _mail() -> str:
    from mint.tools import everyday as skills
    out = skills.list_emails(10)
    return "" if out.startswith("Could not") else out


def _tasks() -> str:
    from mint.app import tasks
    return "; ".join(tasks.brief(t) for t in tasks.open_tasks()[:3])


def _meetings() -> str:
    try:
        from mint.tools import meetings
        today = dt.date.today().strftime("%Y-%m-%d")
        rows = [m for m in meetings.list_meetings()[:10] if str(m.get("started", "")).startswith(today)]
        return "; ".join(str(m.get("title")) for m in rows)
    except Exception:
        return ""


def _weather() -> str:
    data = json.loads(urllib.request.urlopen(urllib.request.Request("https://wttr.in/?format=j1", headers=UA),
                                             timeout=6).read())
    now = data["current_condition"][0]
    today = data["weather"][0]
    place = data.get("nearest_area", [{}])[0].get("areaName", [{}])[0].get("value", "")
    rain = max(int(h.get("chanceofrain", 0)) for h in today.get("hourly", [{"chanceofrain": 0}]))
    return (f"{place}: now {now['temp_C']}°C, {now['weatherDesc'][0]['value'].strip()}; "
            f"today {today['mintempC']}-{today['maxtempC']}°C, rain chance up to {rain}%")


def _news(topic: str = "") -> str:
    import urllib.parse
    region = "hl=en-IN&gl=IN&ceid=IN:en"
    url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(topic)}&{region}" if topic
           else f"https://news.google.com/rss?{region}")
    root = ElementTree.fromstring(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=6).read())
    titles = [re.sub(r"\s+-\s+[^-]+$", "", item.findtext("title") or "") for item in root.iter("item")]
    return "; ".join(t for t in titles[:6] if t)


def gather(news_topic: str = "") -> dict:
    jobs = {"calendar": _calendar, "reminders": _reminders, "mail": _mail, "tasks": _tasks,
            "meetings": _meetings, "weather": _weather, "news": lambda: _news(news_topic)}
    out: dict = {}
    with ThreadPoolExecutor(len(jobs)) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        for name, future in futures.items():
            try:
                out[name] = future.result(timeout=25) or ""
            except Exception as error:
                log.info("briefing %s: %s", name, str(error)[:120])
                out[name] = ""
    return out


def brief(focus: str = "", news_topic: str = "") -> str:
    from mint.core import llm
    from mint.core import prefs
    parts = gather(news_topic)
    now = dt.datetime.now()
    name = prefs.get("user_name") or ""
    facts = "\n".join(f"{k.upper()}: {v}" for k, v in parts.items() if v)
    if not facts:
        return "FAILED: could not read the calendar, mail or the web just now."
    prompt = (f"It is {now:%A %d %B, %-I:%M %p}. Write a spoken briefing for {name or 'the user'} from the facts below"
              + (f", focusing on: {focus}" if focus else "") + ". About 45-70 seconds when read aloud. Order: "
              "a one-line greeting with the weather; what is on the calendar (times in 12-hour form, next thing "
              "first); reminders due; which emails look like they need them (unread ones from people, not "
              "newsletters); unfinished tasks; two or three headlines. Skip any section with nothing in it - "
              "never say 'no data'. Plain sentences, no lists, no Markdown.\n\n" + facts)
    try:
        text, _ = llm.generate(prompt)
    except Exception as error:
        return f"FAILED: could not write the briefing: {error}"
    return ("Say this briefing to the user, as it is:\n" + text.strip()
            + "\n\n(Sources read: " + ", ".join(k for k, v in parts.items() if v) + ".)")


PROMPT = """Briefing: "brief me", "what's my day like?", "good morning, what's up today?" -> briefing (one call \
reads the calendar, reminders, mail, tasks, weather and headlines; then say what it returns). For every \
morning, make an automation whose `do` is "give me my daily briefing"."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="briefing",
        description=("The user's day in one spoken summary: today's calendar, reminders due, the newest mail "
                     "(what needs them), unfinished tasks, today's meetings, the weather and a few headlines."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "focus": types.Schema(type=S, description="optional: what to focus on, e.g. 'just work'"),
            "news_topic": types.Schema(type=S, description="optional: headlines about this, e.g. 'AI'")}))]


HANDLERS = {"briefing": lambda a: brief(str(a.get("focus") or ""), str(a.get("news_topic") or ""))}
