"""Setup, check and cleanup functions are registered by name; tasks.json refers to them.

    @setup
    def files_organize(ctx): ...          # makes the fixtures; may raise Skip("why")
    @check
    def files_organize_ok(ctx): ...       # returns ok("…") or fail("…"); READ-ONLY, may be polled
    @cleanup
    def close_textedit(ctx): ...          # extra cleanup; the standard cleanup always runs too

A check looks at the END STATE (files, apps, memory), never at Mint's words.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import sandbox
from .apple import Apple

SETUPS: dict = {}
CHECKS: dict = {}
CLEANUPS: dict = {}


def setup(fn):
    SETUPS[fn.__name__] = fn
    return fn


def check(fn):
    CHECKS[fn.__name__] = fn
    return fn


def cleanup(fn):
    CLEANUPS[fn.__name__] = fn
    return fn


class Skip(Exception):
    """The task cannot run here (a missing permission, a missing tool)."""


@dataclass
class Verdict:
    ok: bool
    reason: str


def ok(reason: str = "ok") -> Verdict:
    return Verdict(True, reason)


def fail(reason: str) -> Verdict:
    return Verdict(False, reason)


@dataclass
class Ctx:
    task: dict
    site: str                      # http://127.0.0.1:PORT
    apple: Apple
    server: object = None          # site.SiteServer
    started: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)     # setup -> check -> cleanup
    trashed: dict = field(default_factory=dict)  # fixture name -> sha256, purged from the Trash after
    apps_before: set = field(default_factory=set)
    dry: bool = False

    root: Path = sandbox.ROOT

    def p(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @property
    def markers(self) -> tuple[str, ...]:
        return tuple(self.task.get("markers", ()))


def tomorrow_at(hour: int, minute: int = 0) -> dt.datetime:
    day = dt.date.today() + dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time(hour, minute))


def next_weekday(weekday: int, hour: int, minute: int = 0) -> dt.datetime:
    """The next such weekday strictly after today (Mon=0)."""
    today = dt.date.today()
    ahead = (weekday - today.weekday()) % 7 or 7
    return dt.datetime.combine(today + dt.timedelta(days=ahead), dt.time(hour, minute))
