"""The voice models Mint can talk through, and which one to use right now.

Gemini has several Live models, each with its own quota (65K input tokens a minute on the user's key, 6 Oct). Every
turn costs the whole conversation so far - about 23K tokens of instructions and tools before anyone speaks - so
a few quick turns fill a model's minute, and then it answers slowly (1-4 s, stalls of 15-17 s) or not at all.
6 Oct evening: gemini-3.8-live at 78K/65K was stalling while gemini-3.8-live-extended-thinking (0.7-1.3 s) and
gemini-3.1-flash-live (0.55-0.85 s) were idle and quick.

So Mint keeps a pool, best first, and benches a model that misbehaves:

    strike(model, why)   a stall, slow answers, a server error, deafness: benched for 5 min, then 20 min, 1 h,
                         5 h, a day, a week as the strikes add up. Good answers and quiet days forgive them.
    tokens(model, n)     a turn's tokens; near the per-minute limit the model rests a minute (not a strike).
    choose(current)      the model to use: the current one while it's fine, else the best one not benched.

State is kept in voice-models.json (Mint's data folder), so a restart doesn't forget a bad model.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

from mint.core import config

log = logging.getLogger("mint.voice.live_models")

# Best first: the smartest, the fastest, the smartest's thinking twin (its own quota; unreliable with tools). Until
# discover() has asked Google which Live models this key has (then the newest come first on their own).
POOL = ("models/gemini-3.8-live", "models/gemini-3.1-flash-live-preview",
        "models/gemini-3.8-live-extended-thinking")
THINKING_LEVEL = "LOW"        # a thinking model refuses a session without a level (and MINIMAL, 6 Oct)
# Live models that are not for a conversation (or, 2.5 native audio, too old for Mint's tools and prompt).
NOT_FOR_TALK = ("transcribe", "translate", "robotics", "native-audio", "tts", "embedding", "image")
DISCOVER_EVERY = 24 * 3600
BENCH = (5 * 60, 20 * 60, 3600, 5 * 3600, 24 * 3600, 7 * 24 * 3600)
FORGIVE_AFTER = 24 * 3600     # a day without a strike clears a model's record
GOOD_TO_FORGIVE = 12          # this many good answers in a row take one strike off
TPM_LIMIT = 65_000            # input tokens a minute per model (the key's limit; pref live_tpm_limit overrides)
TPM_SHARE = 0.85              # past this share of the minute's budget, rest the model for a minute
REST = 60.0

STATE = config.PROJECT_ROOT / "voice-models.json"
_lock = threading.Lock()
_state: dict[str, dict] | None = None
_turns: dict[str, deque] = {}


def _short(model: str) -> str:
    return str(model).removeprefix("models/")


def pool() -> list[str]:
    """The models in order of preference. MINT_MODEL (or --model) goes first; pref voice_models replaces the
    list (an empty list = this pool)."""
    try:
        from mint.core import prefs
        chosen = [m if str(m).startswith("models/") else f"models/{m}" for m in (prefs.get("voice_models") or [])]
    except Exception:
        chosen = []
    found = _load().get("_found", {}).get("models") if chosen == [] else None
    models = chosen or (rank(found) if found else list(POOL))     # ranked again: the order may have changed
    first = os.environ.get("MINT_MODEL") or ""
    if first and first not in models:
        models.insert(0, first)
    elif first:
        models.remove(first)
        models.insert(0, first)
    return models


def _load() -> dict[str, dict]:
    global _state
    if _state is None:
        try:
            _state = json.loads(STATE.read_text())
            if not isinstance(_state, dict):
                _state = {}
        except (OSError, ValueError):
            _state = {}
    return _state


def _save() -> None:
    try:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state or {}, indent=1))
        os.replace(tmp, STATE)
    except OSError:
        log.debug("could not save %s", STATE, exc_info=True)


def _record(model: str, now: float) -> dict:
    rec = _load().setdefault(model, {"strikes": 0, "until": 0.0, "last": 0.0, "good": 0, "why": ""})
    if rec.get("strikes") and now - float(rec.get("last") or 0) > FORGIVE_AFTER:
        rec.update(strikes=0, good=0)
    return rec


def benched(model: str, now: float | None = None) -> float:
    """Seconds left on the bench (0 when the model may be used)."""
    now = time.time() if now is None else now
    with _lock:
        return max(0.0, float(_record(model, now).get("until") or 0) - now)


def strike(model: str, why: str, now: float | None = None) -> float:
    """The model misbehaved: bench it, longer each time. Returns the seconds benched."""
    now = time.time() if now is None else now
    with _lock:
        rec = _record(model, now)
        if float(rec.get("until") or 0) > now and now - float(rec.get("last") or 0) < 30:
            return float(rec["until"]) - now          # the same trouble reported twice: one strike
        seconds = BENCH[min(int(rec.get("strikes") or 0), len(BENCH) - 1)]
        rec.update(strikes=int(rec.get("strikes") or 0) + 1, until=now + seconds, last=now, good=0, why=why[:120])
        _save()
    log.warning("voice model %s benched for %s (%s)", _short(model), _span(seconds), why)
    return seconds


def retire(model: str, why: str, seconds: float = BENCH[-1]) -> None:
    """The model refuses Mint's session outright (not found, settings refused): set it aside for a week."""
    now = time.time()
    with _lock:
        rec = _record(model, now)
        rec.update(strikes=len(BENCH), until=now + seconds, last=now, good=0, why=why[:120])
        _save()
    log.warning("voice model %s set aside for %s (%s)", _short(model), _span(seconds), why)


def rest(model: str, seconds: float = REST, why: str = "near its per-minute limit") -> None:
    """A short pause that is not the model's fault (its minute's tokens are nearly used up)."""
    now = time.time()
    with _lock:
        rec = _record(model, now)
        if float(rec.get("until") or 0) < now + seconds:
            rec.update(until=now + seconds, why=why)
            _save()


def good(model: str) -> None:
    """An answer that came quickly: enough of them in a row forgive a strike."""
    now = time.time()
    with _lock:
        rec = _record(model, now)
        if not rec.get("strikes"):
            return
        rec["good"] = int(rec.get("good") or 0) + 1
        if rec["good"] >= GOOD_TO_FORGIVE:
            rec.update(strikes=int(rec["strikes"]) - 1, good=0)
            _save()


def tokens(model: str, count: int, now: float | None = None) -> bool:
    """A turn's input (prompt) tokens. True when the model is near its per-minute budget (it has been rested a minute)."""
    now = time.monotonic() if now is None else now
    turns = _turns.setdefault(model, deque())
    turns.append((now, int(count or 0)))
    while turns and now - turns[0][0] > 60:
        turns.popleft()
    if sum(n for _, n in turns) <= TPM_SHARE * _tpm_limit():
        return False
    rest(model)
    turns.clear()
    return True


def used(model: str, now: float | None = None) -> int:
    """Tokens this model used in the last minute (as counted here)."""
    now = time.monotonic() if now is None else now
    return sum(n for t, n in _turns.get(model, ()) if now - t <= 60)


def _tpm_limit() -> int:
    try:
        from mint.core import prefs
        return int(prefs.get("live_tpm_limit") or TPM_LIMIT)
    except Exception:
        return TPM_LIMIT


def choose(current: str = "", now: float | None = None) -> str:
    """The model to use: the best one not benched. When every one is benched, the one back soonest."""
    now = time.time() if now is None else now
    models = pool()
    ready = [m for m in models if benched(m, now) <= 0]
    if ready:
        return ready[0]
    return min(models, key=lambda m: benched(m, now)) if models else (current or config.MODEL)


def better(current: str, now: float | None = None) -> str:
    """A model ahead of `current` in the pool that is ready again ('' if none) - to move back to it."""
    now = time.time() if now is None else now
    models = pool()
    if current not in models:
        return ""
    for m in models[:models.index(current)]:
        if benched(m, now) <= 0:
            return m
    return ""


def rank(names) -> list[str]:
    """Live models for a conversation, best first: the newest version, the plain model before flash (fastest),
    a released model before a preview - and any thinking model last (its own quota, but unreliable with tools)."""
    import re

    def key(name: str):
        short = _short(name).lower()
        version = re.search(r"gemini-(\d+(?:\.\d+)?)", short)
        # Thinking last: 6 Oct, 3.8-live-extended-thinking promised tool work and then didn't call (2 of 3 runs).
        return ("thinking" in short, -(float(version.group(1)) if version else 0.0), "flash" in short,
                "preview" in short, short)
    talk = [n for n in dict.fromkeys(names) if "live" in _short(n).lower()
            and not any(word in _short(n).lower() for word in NOT_FOR_TALK)]
    return sorted(talk, key=key)


def discover(force: bool = False) -> list[str]:
    """Ask Google which Live models this key can use (models that support bidiGenerateContent), at most once a
    day, and keep the ranked list for pool(). On any failure the last list (or POOL) stays."""
    now = time.time()
    found = _load().get("_found") or {}
    if not force and now - float(found.get("at") or 0) < DISCOVER_EVERY and found.get("models"):
        return list(found["models"])
    try:
        from mint.core import gemini_keys
        names = [m.name for m in gemini_keys.client().models.list()
                 if "bidiGenerateContent" in (getattr(m, "supported_actions", None) or [])]
    except Exception as error:
        log.info("could not list the Live models: %s", str(error)[:120])
        return list(found.get("models") or POOL)
    ranked = rank(names)
    if not ranked:
        return list(found.get("models") or POOL)
    with _lock:
        _load()["_found"] = {"at": now, "models": ranked}
        _save()
    if ranked != list(found.get("models") or []):
        log.warning("voice models found: %s", ", ".join(_short(m) for m in ranked))
    return ranked


def discover_later() -> None:
    """discover() off the main thread (at start, then daily)."""
    threading.Thread(target=discover, name="live-models", daemon=True).start()


def tune(settings, model: str) -> None:
    """Model-specific session settings (a thinking model needs a thinking level)."""
    level = THINKING_LEVEL if "thinking" in _short(model).lower() else None
    try:
        from google.genai import types
        settings.thinking_config = types.ThinkingConfig(thinking_level=level) if level else None
    except Exception:
        log.debug("could not set the thinking level for %s", model, exc_info=True)


def status() -> str:
    """One line per model for the log and Settings."""
    now = time.time()
    lines = []
    for m in pool():
        if m.startswith("_"):
            continue
        left = benched(m, now)
        rec = _load().get(m, {})
        lines.append(f"{_short(m)}: {'benched ' + _span(left) + ' (' + rec.get('why', '') + ')' if left else 'ready'}"
                     f"{', strikes ' + str(rec.get('strikes')) if rec.get('strikes') else ''}")
    return "\n".join(lines)


def label(model: str) -> str:
    return _short(model)


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    if seconds < 2 * 86400:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} days"


def reset_for_tests(path: Path | None = None) -> None:
    global _state, STATE
    _state = None
    _turns.clear()
    if path is not None:
        STATE = path
