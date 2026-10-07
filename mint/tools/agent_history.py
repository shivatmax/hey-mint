"""What coding agents did, kept for a week: one small record per finished request.

agent_watch keeps only the live picture (the last steps, for hours). This keeps, for each request a Claude Code
or Codex session finished: what was asked, what it said at the end, the files it changed, how the tests stood,
risky steps, how long it worked. "What did my agents do today?", a recap of a request, and the hand-off note
read it.

Stored in ~/Library/Application Support/Mint/agent-history.json (this Mac only). Writes are batched (2 s) and
atomic (tmp + rename). Records older than DAYS or beyond MAX are dropped.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("mint.tools.agent_history")

PATH = os.path.expanduser("~/Library/Application Support/Mint/agent-history.json")
DAYS = 7
MAX = 3000
DEBOUNCE = 2.0

_lock = threading.Lock()
_items: list | None = None
_mtime = 0.0                  # the file as we last read or wrote it: changed by someone else -> read again
_timer: threading.Timer | None = None


def _load() -> list:
    global _items, _mtime
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        mtime = 0.0
    if _items is not None and mtime and mtime != _mtime and _timer is None:
        _items = None                           # changed on disk (another Mint, or cleaned by hand)
    if _items is None:
        _mtime = mtime
        try:
            with open(PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            _items = [x for x in data.get("items", []) if isinstance(x, dict)] if isinstance(data, dict) else []
        except (OSError, ValueError):
            _items = []
    return _items


def _save() -> None:
    global _timer, _mtime
    with _lock:
        items = list(_load())                   # (before the timer is cleared: a pending record is never dropped)
        _timer = None
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "items": items}, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, PATH)
        _mtime = os.path.getmtime(PATH)
    except OSError:
        log.debug("agent history not written", exc_info=True)


def _later() -> None:
    global _timer
    if _timer is None:
        _timer = threading.Timer(DEBOUNCE, _save)
        _timer.daemon = True
        _timer.start()


def record(s, when: float | None = None) -> None:
    """A session finished a request (agent_watch, on done / failed)."""
    when = when or time.time()
    tests = dict(s.tests) if s.tests else None
    if tests:
        if tests.get("state") == "running":           # still running when it stopped (a background run)
            tests.update(state=tests.get("prev") or "unclear",
                         line=tests.get("prev_line") or "the last test run hadn't finished")
        for key in ("tail", "prev", "prev_line", "step"):
            tests.pop(key, None)
    item = {
        "key": s.key, "app": s.app, "project": s.project, "title": s.title, "cwd": s.cwd,
        "prompt": (s.prompt or "")[:400], "summary": (s.summary or "")[:800], "state": s.state,
        "started": s.turn_started or when, "ended": when, "steps": s.turn_steps,
        "files": list(s.turn_files)[:40], "tests": tests, "flags": list(s.flags),
        "plan": list(s.plan) if s.plan else None, "sent_back": s.sent_back,
    }
    with _lock:
        items = _load()
        if items and items[-1].get("key") == s.key and items[-1].get("started") == item["started"]:
            items[-1] = item                    # the same request again (a Stop hook and the log both said done)
        else:
            items.append(item)
        cutoff = time.time() - DAYS * 86400
        keep = [x for x in items if float(x.get("ended") or 0) >= cutoff][-MAX:]
        items[:] = keep
        _later()


def items(since: float = 0.0, key: str = "", project: str = "") -> list:
    with _lock:
        out = [dict(x) for x in _load() if float(x.get("ended") or 0) >= since]
    if key:
        out = [x for x in out if x.get("key") == key]
    if project:
        p = project.lower()
        out = [x for x in out if p in str(x.get("project") or "").lower() or p in str(x.get("title") or "").lower()]
    return out


def day_start(now: float | None = None) -> float:
    t = time.localtime(now or time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def flush() -> None:
    """Write now (tests; quitting)."""
    global _timer
    with _lock:
        timer, _timer = _timer, None
    if timer is not None:
        timer.cancel()
    _save()
