"""The journal: what happened, and when - episodic memory, next to the memory bank's facts.

The memory bank knows lasting facts ("the user's manager is Meera"). The journal
answers questions about the past: "what did we do yesterday?", "when did I ask
you about flights?", "what was that site you found last week?", "what did Astra
find on Monday?", "which video did I have you watch on Friday?".

It reads what Mint already keeps - every turn of every conversation with its
time (history.jsonl and its rotated-out parts in history-archive/: what the user
said, what Mint did and answered, what the agents reported), the tasks, the
automations' runs and the videos watched - narrows it to the days asked about,
and a Flash Lite model answers from that, with dates and times. Nothing new is
recorded for it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time

from mint.core import config

log = logging.getLogger("mint.knowledge.journal")

HISTORY = config.PROJECT_ROOT / "history.jsonl"
MAX_CHARS = 90_000
SMALL = 20_000        # a time range with less history than this goes to the model whole
FAST_CHARS = 32_000   # what the index's hits (and the lines around them) may add up to
WINDOW = 8            # lines either side of the best hit
AROUND = 12           # lines either side for around= (scrolling)
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "a": 1, "couple": 2,
         "few": 3}


def when_range(text: str, now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime, str]:
    """'yesterday', 'last week', 'on monday', '3 days ago', 'this morning', '20 sep',
    '2026-09-20', 'past 2 weeks', '' (the last 30 days) -> (start, end, label)."""
    now = now or dt.datetime.now()
    today = dt.datetime.combine(now.date(), dt.time.min)
    t = str(text or "").strip().lower()
    since = t.startswith("since ")
    t = re.sub(r"^(on|in|during|from|since|over)\s+", "", t)
    try:
        start, end, label = _range(t, now, today)
    except ValueError:                    # "31 sep", "29 feb" in a short year
        return today - dt.timedelta(days=30), now, "the last 30 days"
    if since:
        return start, now, f"since {label}"
    if start > now:                       # "tonight" asked at 3 pm: so far today
        return today, now, "today"
    return start, min(end, now) if end > start else now, label


def _range(t: str, now: dt.datetime, today: dt.datetime) -> tuple[dt.datetime, dt.datetime, str]:
    if not t or t in ("ever", "all", "anytime", "any time", "recently", "lately"):
        return today - dt.timedelta(days=30), now, "the last 30 days"
    parts = {"this morning": (0, 12), "this afternoon": (12, 18), "this evening": (17, 24), "tonight": (17, 24)}
    if t in parts:
        a, b = parts[t]
        return today + dt.timedelta(hours=a), min(now, today + dt.timedelta(hours=b)), t
    if t in ("today", "now"):
        return today, now, "today"
    if t == "yesterday":
        return today - dt.timedelta(days=1), today, "yesterday"
    if t in ("last night", "yesterday evening", "yesterday night"):
        return today - dt.timedelta(hours=7), today + dt.timedelta(hours=4), "last night"
    if t in ("yesterday morning", "yesterday afternoon"):
        a, b = (0, 12) if "morning" in t else (12, 18)
        return today - dt.timedelta(days=1) + dt.timedelta(hours=a), today - dt.timedelta(days=1) + dt.timedelta(hours=b), t
    if t in ("day before yesterday", "the day before yesterday"):
        return today - dt.timedelta(days=2), today - dt.timedelta(days=1), "the day before yesterday"
    monday = today - dt.timedelta(days=today.weekday())
    if t == "this week":
        return monday, now, "this week"
    if t == "last week":
        return monday - dt.timedelta(days=7), monday, "last week"
    if t == "this month":
        return today.replace(day=1), now, "this month"
    if t == "last month":
        first = today.replace(day=1)
        return (first - dt.timedelta(days=1)).replace(day=1), first, "last month"
    m = re.fullmatch(r"(\w+)\s+(day|days|week|weeks|month|months)\s+ago", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else WORDS.get(m.group(1), 1)
        unit = {"day": 1, "week": 7, "month": 30}[m.group(2).rstrip("s")]
        start = today - dt.timedelta(days=n * unit)
        return start, start + dt.timedelta(days=unit), t
    m = re.fullmatch(r"(?:the\s+)?(?:past|last)\s+(\w+)\s+(day|days|week|weeks|month|months|hours?)", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else WORDS.get(m.group(1), 1)
        unit = m.group(2).rstrip("s")
        span = dt.timedelta(hours=n) if unit == "hour" else dt.timedelta(days=n * {"day": 1, "week": 7, "month": 30}[unit])
        return now - span, now, t
    m = re.fullmatch(r"(last\s+)?(" + "|".join(DAYS) + r")(\s+(morning|afternoon|evening|night))?", t)
    if m:
        back = (today.weekday() - DAYS.index(m.group(2))) % 7
        if m.group(1) and back == 0:
            back = 7                       # "last tuesday" on a Tuesday is a week ago
        day = today - dt.timedelta(days=back)
        a, b = {"morning": (0, 12), "afternoon": (12, 18), "evening": (17, 24), "night": (17, 28)}.get(
            m.group(4) or "", (0, 24))
        return day + dt.timedelta(hours=a), min(now, day + dt.timedelta(hours=b)), t
    try:
        day = dt.datetime.fromisoformat(t)
        return day, day + dt.timedelta(days=1), day.strftime("%a %d %b")
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)|([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?", t)
    if m:
        num, name = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        month = next((i for i, n in enumerate(MONTHS, 1) if n.startswith(name[:3])), None)
        if month:
            day = dt.datetime(now.year, month, int(num))
            if day > now:
                day = day.replace(year=now.year - 1)
            return day, day + dt.timedelta(days=1), day.strftime("%a %d %b")
    month = next((i for i, n in enumerate(MONTHS, 1) if t[:3] == n[:3] and len(t) >= 3), None)
    if month:
        start = dt.datetime(now.year if month <= now.month else now.year - 1, month, 1)
        end = (start + dt.timedelta(days=32)).replace(day=1)
        return start, end, start.strftime("%B")
    return today - dt.timedelta(days=30), now, "the last 30 days"


def _history_files(start: dt.datetime) -> list:
    """history.jsonl, after the rotated-out parts that may hold lines from `start` on
    (history-archive/history-YYYYMMDD.jsonl holds lines up to that day)."""
    files = []
    for path in sorted((HISTORY.parent / "history-archive").glob("history-*.jsonl")):
        try:
            rotated = dt.datetime.strptime(path.stem.split("-", 1)[1][:8], "%Y%m%d")
        except ValueError:
            rotated = None
        if rotated is None or rotated.date() >= start.date():
            files.append(path)
    return files + [HISTORY]


def _history(start: dt.datetime, end: dt.datetime) -> list[str]:
    a, b = start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")
    lines = []
    for path in _history_files(start):
        try:
            with path.open() as f:
                for raw in f:
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        continue
                    when = str(row.get("t", ""))
                    if not (a <= when <= b) or row.get("role") == "marker":
                        continue
                    who = {"user": "User", "mint": "Mint", "jarvis": "Mint", "tool": "Did", "agent": "Agent"}.get(
                        row.get("role"), str(row.get("role")))
                    text = " ".join(str(row.get("text", "")).split())
                    try:
                        shown = dt.datetime.strptime(when, "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %a %H:%M")
                    except ValueError:
                        shown = when
                    lines.append(f"{shown} {who}: {text[:400 if who == 'Did' else 700]}")
        except OSError:
            pass
    return lines


def _extras(start: dt.datetime, end: dt.datetime) -> list[str]:
    """Tasks, automation runs and videos in the range (they are not all in the history)."""
    lines = []
    a, b = start.timestamp(), end.timestamp()

    def stamp(ts: float) -> str:
        return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %a %H:%M")
    try:
        from mint.app import tasks
        for r in tasks._load():
            if a <= r.get("updated", 0) <= b or a <= r.get("created", 0) <= b:
                done, total = tasks.progress(r)
                lines.append(f"{stamp(r['created'])} Task ({r['state']}, {done}/{total} steps): {r['goal']}")
    except Exception:
        pass
    try:
        from mint.tools import automations
        for r in automations.load():
            for run in r.get("runs") or []:
                if a <= run["at"] <= b:
                    lines.append(f"{stamp(run['at'])} Automation '{r['name']}' ran: {run['result'][:120]}")
    except Exception:
        pass
    try:
        from mint.knowledge import timeline
        lines += timeline.lines(a, b)
        spent = timeline.time_spent(a, b)
        if spent:
            lines.append(f"{stamp(b)} Time on screen in this period, by app: {spent}")
    except Exception:
        pass
    try:
        from mint.tools import video
        for r in video._index():
            if a <= r.get("when", 0) <= b:
                lines.append(f"{stamp(r['when'])} Watched video: {r.get('title')} ({r.get('source')})")
    except Exception:
        pass
    return lines


STOP = {"what", "when", "where", "which", "did", "does", "about", "that", "this", "with", "have", "your", "from",
        "were", "there", "they", "them", "then", "tell", "ask", "asked", "you", "the", "and", "for", "was", "mint"}


def _narrow(lines: list[str], question: str, budget: int = MAX_CHARS) -> list[str]:
    """Too much for one request: the lines that share the question's rarer words (a word
    in few lines counts for more than "open"), with their neighbours, then the most
    recent of the rest - about `budget` characters."""
    import math
    tokens = [set(re.findall(r"[a-z0-9]+", line.lower())) for line in lines]
    words = {w for w in re.findall(r"[a-z0-9]+", question.lower()) if len(w) > 2 and w not in STOP}
    df = {w: sum(1 for t in tokens if w in t) for w in words}
    weight = {w: math.log((1 + len(lines)) / (1 + n)) for w, n in df.items() if n}
    scored = sorted(((sum(weight.get(w, 0) for w in words & t), i) for i, t in enumerate(tokens)), reverse=True)
    chosen: set[int] = set()
    size = 0
    for score, i in scored:
        if score <= 0:
            break
        for j in range(max(0, i - 2), min(len(lines), i + 3)):
            if j not in chosen:
                chosen.add(j)
                size += len(lines[j]) + 1
        if size > budget * 0.7:
            break
    for i in range(len(lines) - 1, -1, -1):
        if size > budget:
            break
        if i not in chosen:
            chosen.add(i)
            size += len(lines[i]) + 1
    return [lines[i] for i in sorted(chosen)]


def _by_time(lines: list[str]) -> list[str]:
    """Oldest first; lines of the same minute keep their order (the user's words before the answer)."""
    return sorted(lines, key=lambda line: line[:20])


def _from_index(question: str, start: dt.datetime, end: dt.datetime):
    """The fast path: the history index instead of reading every file. -> (lines, best hit or
    None), or None when there is no index (then the files are read, as before)."""
    from mint.knowledge import history_index
    index = history_index.get(HISTORY)
    if index is None:
        return None
    try:
        index.ingest()
        rows, chars = index.size(start, end)
        extras = _extras(start, end)
        if chars + sum(len(x) + 1 for x in extras) <= SMALL:     # little enough: all of it
            return _by_time([r.line() for r in index.between(start, end)] + extras), None
        found = index.find(question, start, end, window=WINDOW)
        if not found.hits:                       # nothing to search for ("what did we do?"): as before
            return _by_time([r.line() for r in index.between(start, end)] + extras), None
        picked = {r.id: r for r in found.top}    # the best hit with its conversation around it
        size = sum(len(r.line()) + 1 for r in found.top)
        # Then the other hits with a line or two either side: the best and the newest in turn ("which
        # one am I watching" is about the latest mention as much as the closest match).
        newest = sorted(found.hits[1:], key=lambda h: -h.id)
        for hit in (h for pair in zip(found.hits[1:], newest) for h in pair):
            if size > FAST_CHARS:
                break
            if hit.id in picked:
                continue
            for r in index.around(hit.id, 2, 2):
                if r.id not in picked:
                    picked[r.id] = r
                    size += len(r.line()) + 1
        if sum(len(x) + 1 for x in extras) > FAST_CHARS // 3:
            extras = _narrow(extras, question, FAST_CHARS // 3)
        lines = [picked[i].line() for i in sorted(picked)]
        return _by_time(lines + extras), found.hits[0]
    except Exception as error:
        log.warning("history index failed, reading the files: %s", str(error)[:160])
        return None


def _around(ref: str) -> str:
    """recall_history(around=...): the conversation around one line, as it was (no summary)."""
    from mint.knowledge import history_index
    digits = re.sub(r"\D", "", str(ref))
    index = history_index.get(HISTORY)
    if not digits or index is None:
        return ("FAILED: 'around' takes a line number from an earlier recall_history answer (like '1234'); "
                "ask with a question instead.")
    try:
        index.ingest()
        rows = index.around(int(digits), AROUND, AROUND, same_conversation=False)
    except Exception as error:
        return f"FAILED: could not read the history: {str(error)[:160]}"
    if not rows:
        return f"There is no history line #{digits}."
    text = "\n".join(r.line(ref=True, full=True) for r in rows)
    return (f"The history around #{digits}, as it was:\n{text}\n(Earlier: around='{rows[0].id}'; "
            f"later: around='{rows[-1].id}'.)")


def recall_history(question: str, when: str = "", around: str = "") -> str:
    if str(around or "").strip():
        return _around(str(around))
    from mint.core import llm
    start, end, label = when_range(when)
    picked = _from_index(question, start, end)
    if picked is None:
        lines, best = _by_time(_history(start, end) + _extras(start, end)), None
    else:
        lines, best = picked
    if not lines:
        return f"Nothing happened with Mint {label} (no conversations or actions recorded then)."
    if sum(len(x) + 1 for x in lines) > MAX_CHARS:
        lines = _narrow(lines, question)
    log_text = "\n".join(lines)
    now = dt.datetime.now()
    prompt = (f"Below is the log of a voice assistant (Mint) on the user's Mac, {label} "
              f"({start:%a %d %b %H:%M} to {end:%a %d %b %H:%M}). 'User' is what the user said, 'Mint' what Mint "
              "answered, 'Did' the actions Mint took and their results, 'Agent' what sub-agents reported, 'On screen' "
              "which app / window / page was in front (the activity timeline; give the address when asked for a "
              "page). Times use the "
              f"24-hour clock (01:20 is 1:20 AM, 13:20 is 1:20 PM); today is {now:%A %d %B %Y}."
              + (" These are the parts of the log that match the question, not all of it." if best else "")
              + f"\n\nAnswer the question from it: {question or 'What happened?'}\n"
              "Be specific (names, files, sites, results) and say when (day and time). If it is not in the log, "
              "say so plainly. At most 120 words; plain sentences to be spoken.\n\nLOG:\n" + log_text)
    try:
        text, _model = llm.generate(prompt)
    except Exception as error:
        return f"FAILED: could not read the history: {error}"
    more = (f"\n(Best match: line #{best.id}, {time.strftime('%a %d %b %H:%M', time.localtime(best.ts))}. "
            f"To read more of that conversation, call recall_history with around='{best.id}'.)" if best else "")
    return f"({label}, {len(lines)} entries) {text.strip()}{more}"


PROMPT = """The past: recall is for lasting facts about the user, recall_history for events ("what did we do yesterday", \
"what did Astra find on Monday", "how long was I in Slack"; around=<line> reads that part word for word)."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="recall_history",
        description=("Look back at what happened: past conversations, what Mint did and found, agents' results, "
                     "tasks, automations and videos watched, by date. For 'what did we do yesterday', 'when did I "
                     "ask you about X', 'what was that site you found last week', 'what did Astra find on Monday'."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "question": types.Schema(type=S, description="what the user wants to know about the past"),
            "when": types.Schema(type=S, description=("optional: today, this morning, yesterday, last night, "
                                                      "monday, last week, 3 days ago, past 2 weeks, 20 sep, "
                                                      "september; empty = the last 30 days")),
            "around": types.Schema(type=S, description=("optional: a line number from an earlier recall_history "
                                                        "answer ('Best match: line #1234') to read that part of the "
                                                        "conversation word for word; the question is then ignored"))},
            required=["question"]))]


HANDLERS = {"recall_history": lambda a: recall_history(str(a.get("question") or ""), str(a.get("when") or ""),
                                                       str(a.get("around") or ""))}
