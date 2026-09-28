"""The activity timeline: what was in front on the Mac, and when. Off unless the
user turns it on ("remember what I work on" -> set_preference activity_timeline on).

Every 15 seconds, while Mint is running: the app in front, its window title and,
in a browser, the page address - text only, never a screenshot or what is typed.
A line is written when that changes (and every five minutes while it does not,
so time can be measured); nothing while the screen is locked or nobody has
touched the Mac for five minutes. Password managers and private / incognito
windows are never recorded (Safari cannot tell its private windows apart, so
Safari pages are never recorded, only that Safari was in front). It stays on this Mac (timeline.jsonl, mode 600) and
anything older than 14 days is deleted.

The journal (journal.py) reads it, which is what answers "what was I working on
yesterday afternoon?", "find that Airbnb tab from Tuesday", "how long was I in
Slack today?".
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time

from mint.core import config

log = logging.getLogger("mint.knowledge.timeline")

PATH = config.PROJECT_ROOT / "timeline.jsonl"
KEEP_DAYS = 14
EVERY = 15.0
HEARTBEAT = 300.0
IDLE_AFTER = 300.0
SKIP = {"com.1password.1password", "com.agilebits.onepassword7", "com.apple.keychainaccess", "com.apple.Passwords",
        "com.bitwarden.desktop", "com.lastpass.LastPass", "com.dashlane.dashlanephonefinal", "org.keepassxc.keepassxc",
        "com.apple.loginwindow", "com.apple.ScreenSaver.Engine"}
PRIVATE = ("private browsing", "incognito", "inprivate", "private window")

_thread: threading.Thread | None = None
_last = {"key": "", "at": 0.0, "url": "", "title": ""}


def enabled() -> bool:
    from mint.core import prefs
    return bool(prefs.get("timeline"))


def _idle() -> bool:
    try:
        import Quartz
        seconds = Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateCombinedSessionState,
                                                                 Quartz.kCGAnyInputEventType)
        locked = (Quartz.CGSessionCopyCurrentDictionary() or {}).get("CGSSessionScreenIsLocked")
        return bool(locked) or seconds > IDLE_AFTER
    except Exception:
        return False


def _window_name(pid: int) -> str:
    """The front window's name from the window server (Electron apps often give none over AX)."""
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
        for w in windows or []:
            if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0 and w.get("kCGWindowName"):
                return str(w["kCGWindowName"])
    except Exception:
        pass
    return ""


def _incognito(browser: str) -> bool:
    """Chrome, Brave, Edge and Arc say "incognito" as the front window's mode. When that
    cannot be read, count it as private: better a gap than a private page recorded."""
    import subprocess
    try:
        done = subprocess.run(["osascript", "-e", f'tell application "{browser}" to return mode of front window'],
                              capture_output=True, text=True, timeout=3)
        return done.returncode != 0 or done.stdout.strip().lower() != "normal"
    except Exception:
        return True


def sample() -> dict | None:
    """What is in front now: {"app", "title", "url"} / {"idle": True} / None (nothing to record)."""
    import AppKit
    if _idle():
        return {"idle": True}
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None
    bundle = str(app.bundleIdentifier() or "")
    if bundle in SKIP or app.processIdentifier() == os.getpid():
        return None
    from mint.screen import axkit
    window = axkit.focused_window(app.processIdentifier())
    title = str(axkit.attr(window, "AXTitle") or "") if window is not None else ""
    if not title:
        title = _window_name(app.processIdentifier())
    if any(p in title.lower() for p in PRIVATE):
        return {"app": str(app.localizedName() or bundle), "title": "(a private window)"}
    entry = {"app": str(app.localizedName() or bundle), "title": title[:200]}
    from mint.tools import harness as harness_tools
    if bundle == "com.apple.Safari":
        # Safari does not say whether a window is private, so no page is recorded from it at all.
        return {"app": entry["app"], "title": "(a Safari page)"}
    if bundle in harness_tools._BROWSERS:
        name, family = harness_tools._BROWSERS[bundle]
        if family == "chrome" and _incognito(name):
            return {"app": entry["app"], "title": "(a private window)"}
        if title and title == _last["title"] and _last["url"]:
            entry["url"] = _last["url"]
        else:
            try:
                url, tab = harness_tools._tab_info(app)
                if not title and tab:
                    # Chromium browsers often give no window title over AX; the tab has one.
                    if any(p in tab.lower() for p in PRIVATE):
                        return {"app": entry["app"], "title": "(a private window)"}
                    entry["title"] = tab[:200]
                if url and not url.startswith(("chrome://", "about:", "file://")):
                    entry["url"] = url[:300]
            except Exception:
                pass
    return entry


def _write(entry: dict) -> None:
    fd = os.open(PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)      # private from the first byte
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"t": round(time.time()), **entry}, ensure_ascii=False) + "\n")


def _loop() -> None:
    pruned = 0.0
    while True:
        if time.time() - pruned > 6 * 3600:      # Mint can stay loaded for days
            prune()
            pruned = time.time()
        time.sleep(EVERY)
        if not enabled():
            _last.update(key="", at=0.0)
            continue
        try:
            entry = sample()
        except Exception:
            log.debug("timeline sample failed", exc_info=True)
            continue
        if entry is None:
            continue
        key = "idle" if entry.get("idle") else f"{entry['app']}|{entry['title']}"
        now = time.time()
        if key == _last["key"] and (key == "idle" or now - _last["at"] < HEARTBEAT):
            continue
        _write(entry)
        _last.update(key=key, at=now, url=entry.get("url", ""), title=entry.get("title", ""))


def start() -> None:
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_loop, daemon=True, name="timeline")
        _thread.start()


def prune() -> None:
    """Forget everything older than KEEP_DAYS."""
    cutoff = time.time() - KEEP_DAYS * 86400
    try:
        lines = PATH.read_text().splitlines()
    except OSError:
        return
    keep = [line for line in lines if _time(line) >= cutoff]
    if len(keep) != len(lines):
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text("".join(line + "\n" for line in keep))
        os.chmod(tmp, 0o600)
        tmp.replace(PATH)


def _time(line: str) -> float:
    try:
        return float(json.loads(line).get("t", 0))
    except ValueError:
        return 0.0


def clear() -> str:
    PATH.unlink(missing_ok=True)
    return "Deleted the activity timeline."


# --- Reading ------------------------------------------------------------------------------

def sessions(start: float, end: float) -> list[dict]:
    """Consecutive samples of the same window joined: [{from, to, app, title, url}]."""
    rows = []
    try:
        with PATH.open() as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    out: list[dict] = []
    for i, row in enumerate(rows):
        t = float(row.get("t", 0))
        nxt = float(rows[i + 1]["t"]) if i + 1 < len(rows) else time.time()
        stop = min(nxt, t + HEARTBEAT + EVERY * 2)      # a gap (Mint not running) is not time spent
        if stop < start or t > end or row.get("idle"):
            continue
        key = (row.get("app"), row.get("title"))
        if out and (out[-1]["app"], out[-1]["title"]) == key and t - out[-1]["to"] < EVERY * 3:
            out[-1]["to"] = stop
            continue
        out.append({"from": max(t, start), "to": min(stop, end), "app": row.get("app", ""),
                    "title": row.get("title", ""), "url": row.get("url", "")})
    return out


def _minutes(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def lines(start: float, end: float) -> list[str]:
    """For the journal: one line per stretch of at least a minute."""
    out = []
    for s in sessions(start, end):
        if s["to"] - s["from"] < 60:
            continue
        a, b = dt.datetime.fromtimestamp(s["from"]), dt.datetime.fromtimestamp(s["to"])
        where = f" <{s['url']}>" if s["url"] else ""
        out.append(f"{a:%Y-%m-%d %a %H:%M}-{b:%H:%M} On screen: {s['app']} · {s['title'][:120]}{where}")
    return out


def time_spent(start: float, end: float, top: int = 8) -> str:
    """'Google Chrome 2 h 10 min (YouTube 40 min, Gmail 25 min), Xcode 1 h 5 min, ...'"""
    by_app: dict[str, float] = {}
    by_site: dict[str, dict[str, float]] = {}
    for s in sessions(start, end):
        span = s["to"] - s["from"]
        by_app[s["app"]] = by_app.get(s["app"], 0) + span
        if s["url"]:
            from urllib.parse import urlparse
            host = urlparse(s["url"]).netloc.removeprefix("www.")
            by_site.setdefault(s["app"], {})[host] = by_site.get(s["app"], {}).get(host, 0) + span
    parts = []
    for app, total in sorted(by_app.items(), key=lambda x: -x[1])[:top]:
        if total < 60:
            continue
        sites = sorted((by_site.get(app) or {}).items(), key=lambda x: -x[1])[:3]
        detail = " (" + ", ".join(f"{h} {_minutes(v)}" for h, v in sites if v >= 60) + ")" if sites else ""
        parts.append(f"{app} {_minutes(total)}{detail if detail != ' ()' else ''}")
    return ", ".join(parts)
