"""What the user is asking for right now, for code that runs mid-turn.

Gemini Live finishes a user turn in the transcript only when the whole
exchange ends - after the tools have run - so tool hooks cannot learn the
request from memory. The session calls heard() as transcription streams in and
typed() for typed requests; context_request() then gives the request in
progress (falling back to the last finished one).
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import time

_lock = threading.Lock()
_text = ""
_at = 0.0
_claimed_at = -1.0     # when the current request's context pack was delivered
_typed = False         # the current request was typed, not spoken
# A background job (background.py) has its own request: its tools see the job, not the conversation.
_job: contextvars.ContextVar = contextvars.ContextVar("mint_live_job", default=None)


@contextlib.contextmanager
def background(text: str, state: dict | None = None):
    """Code run for a background job sees `text` as the request, and claims its context pack once
    (`state` is kept by the job across its calls)."""
    if state is None:
        state = {}
    state.setdefault("text", " ".join(str(text).split()))
    state.setdefault("claimed", False)
    token = _job.set(state)
    try:
        yield
    finally:
        _job.reset(token)


def heard(chunk: str, new_turn: bool = False) -> None:
    global _text, _at, _typed
    with _lock:
        if new_turn or time.monotonic() - _at > 20:
            _text = ""
            _typed = False
        _text = (_text + chunk) if _text else chunk.lstrip()
        _at = time.monotonic()


def typed(text: str) -> None:
    global _text, _at, _typed
    with _lock:
        _text, _at, _typed = text, time.monotonic(), True


def was_typed() -> bool:
    """The request in progress was typed (so nothing in it was misheard)."""
    if _job.get() is not None:
        return True
    with _lock:
        return _typed


def request(max_age: float = 180.0) -> str:
    job = _job.get()
    if job is not None:
        return job["text"]
    with _lock:
        if _text and time.monotonic() - _at <= max_age:
            return " ".join(_text.split())
    from mint.knowledge.learner import learner
    return learner.last_request()


def claim_context(max_age: float = 180.0) -> str:
    """The current request, once: '' if its context was already delivered."""
    global _claimed_at
    job = _job.get()
    if job is not None:
        if job["claimed"]:
            return ""
        job["claimed"] = True
        return job["text"]
    text = request(max_age)
    with _lock:
        if not text or _claimed_at == _at:
            return ""
        _claimed_at = _at
    return text
