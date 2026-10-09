"""Token usage per model, per day: what Settings ▸ Usage shows.

Every Gemini request goes through google-genai, so `install()` wraps its generate_content (sync
and async) once and every module is counted without changes; the Live session reports its own
usage on each server message (session.py calls `live`), and the OpenAI agents report theirs
(`record`). Only counts are kept - tokens and requests - never what was said.

Saved in ~/Library/Application Support/Mint/usage.json as {day: {model: {requests, input,
output, thinking}}}, the last 90 days.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("mint.core.usage")

PATH = Path.home() / "Library" / "Application Support" / "Mint" / "usage.json"
KEEP_DAYS = 90

_lock = threading.Lock()
_days: dict | None = None
_dirty = [0.0]


def _load() -> dict:
    global _days
    if _days is None:
        try:
            _days = json.loads(PATH.read_text())
        except (OSError, ValueError):
            _days = {}
    return _days


def _save_soon() -> None:
    """Write at most every few seconds: the Live session reports on many messages."""
    if _dirty[0]:
        return
    _dirty[0] = time.monotonic()

    def later():
        time.sleep(5)
        with _lock:
            _dirty[0] = 0.0
            days = _load()
            cutoff = (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).isoformat()
            for day in [d for d in days if d < cutoff]:
                days.pop(day)
            try:
                PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(days))
                tmp.replace(PATH)
            except OSError:
                log.debug("usage save", exc_info=True)
    threading.Thread(target=later, daemon=True, name="usage-save").start()


def _short(model: str) -> str:
    return str(model or "unknown").removeprefix("models/")


def record(model: str, input_tokens: int = 0, output_tokens: int = 0, thinking: int = 0,
           requests: int = 1) -> None:
    """Count one request (or a Live turn) for `model` today."""
    try:
        with _lock:
            day = _load().setdefault(datetime.date.today().isoformat(), {})
            row = day.setdefault(_short(model), {"requests": 0, "input": 0, "output": 0, "thinking": 0})
            row["requests"] += int(requests)
            row["input"] += int(input_tokens or 0)
            row["output"] += int(output_tokens or 0)
            row["thinking"] += int(thinking or 0)
        _save_soon()
    except Exception:
        log.debug("usage record", exc_info=True)


def _from_gemini(model: str, meta) -> None:
    if meta is None:
        record(model)
        return
    record(model, getattr(meta, "prompt_token_count", 0) or 0,
           (getattr(meta, "candidates_token_count", None) or getattr(meta, "response_token_count", 0) or 0),
           getattr(meta, "thoughts_token_count", 0) or 0)


def live(model: str, meta) -> None:
    """A Live server message's usage_metadata: one per model turn, with the turn's totals."""
    if meta is not None and (getattr(meta, "total_token_count", 0) or 0):
        # One line per turn in mint.log: the prompt tokens summed over the turn's tool steps (what the
        # tokens-a-minute limit counts), to compare with the fixed prompt (bench/prompt_size.py).
        print(f"  [live turn: {getattr(meta, 'prompt_token_count', None)} prompt tokens, "
              f"{getattr(meta, 'total_token_count', None)} total]", flush=True)
        _from_gemini(model, meta)


def install() -> None:
    """Count every generate_content call made through google-genai, once per process."""
    from google.genai import models
    if getattr(models.Models.generate_content, "_mint_counted", False):
        return
    plain, later = models.Models.generate_content, models.AsyncModels.generate_content

    def counted(self, *args, **kwargs):
        reply = plain(self, *args, **kwargs)
        _from_gemini(kwargs.get("model", args[0] if args else ""), getattr(reply, "usage_metadata", None))
        return reply

    async def counted_async(self, *args, **kwargs):
        reply = await later(self, *args, **kwargs)
        _from_gemini(kwargs.get("model", args[0] if args else ""), getattr(reply, "usage_metadata", None))
        return reply
    counted._mint_counted = True
    models.Models.generate_content = counted
    models.AsyncModels.generate_content = counted_async


def totals(days: int = 1) -> list[dict]:
    """[{model, requests, input, output, thinking}] over the last `days` days, busiest first."""
    since = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
    merged: dict[str, dict] = {}
    with _lock:
        for day, rows in _load().items():
            if day < since:
                continue
            for model, row in rows.items():
                into = merged.setdefault(model, {"model": model, "requests": 0, "input": 0, "output": 0,
                                                 "thinking": 0})
                for key in ("requests", "input", "output", "thinking"):
                    into[key] += row.get(key, 0)
    return sorted(merged.values(), key=lambda r: -(r["input"] + r["output"]))


def daily(days: int = 14) -> list[tuple[str, int]]:
    """[(day, tokens)] for the last `days` days, oldest first (the little chart)."""
    with _lock:
        stored = _load()
        out = []
        for back in range(days - 1, -1, -1):
            day = (datetime.date.today() - datetime.timedelta(days=back)).isoformat()
            out.append((day, sum(r.get("input", 0) + r.get("output", 0) for r in stored.get(day, {}).values())))
    return out


def clear() -> None:
    global _days
    with _lock:
        _days = {}
    PATH.unlink(missing_ok=True)
