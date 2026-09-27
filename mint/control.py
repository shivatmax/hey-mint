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


def stop() -> None:
    global _generation
    _generation += 1
    _stopped.set()


def generation() -> int:
    return _generation


def resume() -> None:
    _stopped.clear()


def stopped() -> bool:
    return _stopped.is_set()


DEFAULT_WORDS = ["stop", "stop it", "stop everything", "cancel", "cancel that", "abort",
                 "enough", "hold on", "never mind", "nevermind", "shut up"]
_NEGATIONS = {"don't", "dont", "do", "not", "never", "no", "won't", "can't"}


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
            # A command: the whole utterance is short, or it leads with the phrase.
            if len(words) <= 5 or i == 0:
                return True
    return False
