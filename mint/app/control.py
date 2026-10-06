"""The stop switch.

When the user says "stop", the running tool is abandoned - but its thread may
still be mid-way through a click or a paste, and threads cannot be killed. So
every synthetic key, click and scroll checks this switch first: once stopped,
Mint posts no more input until the next request starts.
"""

from __future__ import annotations

import re
import threading

_stopped = threading.Event()
_generation = 0          # bumped by every stop; long background waits compare it
_stopped_at = 0.0        # time.monotonic() of the last stop


def stop() -> None:
    global _generation, _stopped_at
    import time
    _generation += 1
    _stopped_at = time.monotonic()
    _stopped.set()


def stopped_at() -> float:
    return _stopped_at


def generation() -> int:
    return _generation


def resume() -> None:
    _stopped.clear()


def stopped() -> bool:
    return _stopped.is_set()


DEFAULT_WORDS = ["stop", "stop it", "stop everything", "cancel", "cancel that", "abort",
                 "enough", "hold on", "never mind", "nevermind", "shut up"]
_NEGATIONS = {"don't", "dont", "do", "not", "never", "no", "won't", "can't"}
# "stop recording", "stop the screen recording", "stop the music": a request about one thing, for the
# model - not the stop switch (which would leave that very recording running).
_OBJECTS = {"recording", "record", "recordings", "screen", "video", "meeting", "call", "music", "song", "playing",
            "playback", "sharing", "share", "timer", "alarm", "caffeinate", "keeping", "watching", "tutor", "lesson",
            "teaching", "dictation", "notes", "automation", "reminder", "focus"}
_DETERMINERS = {"the", "my", "this", "that", "our"}


def _about_something(words: list[str], after: int) -> bool:
    """The words right after "stop" name a thing to stop ("stop recording", "stop the music")."""
    rest = words[after:after + 3]
    while rest and rest[0] in _DETERMINERS:
        rest = rest[1:]
    return bool(rest) and rest[0] in _OBJECTS


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", text.lower())


def is_stop(text: str, phrases: list[str] | None = None) -> bool:
    """Is this utterance the user telling Mint to stop?

    Only when it is short and plainly a command: "stop", "Mint, stop",
    "stop, open Slack instead". Not "the bus stop" in a long sentence, and not
    "don't stop".
    """
    words = [w for w in _words(text) if w not in {"mint", "hey", "please", "okay", "ok", "now"}]
    if not words:
        return False
    for phrase in phrases or DEFAULT_WORDS:
        target = _words(phrase)
        n = len(target)
        if not n:
            continue
        for i in range(len(words) - n + 1):
            if words[i:i + n] != target:
                continue
            if i > 0 and words[i - 1] in _NEGATIONS:
                continue
            if target[-1] == "stop" and _about_something(words, i + n):
                continue
            # A command: the whole utterance is short, or it leads with the phrase.
            if len(words) <= 5 or i == 0:
                return True
    return False
