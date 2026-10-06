"""Conversation history and a rolling summary of it.

Every turn is appended to history.jsonl. Once enough new conversation has
built up, a fast Flash model folds it into summary.md in the background - never
on the voice path - and writes a few journal lines (episodes) and durable facts
into the memory bank. Each new Live session starts with that summary in its
instructions, so Mint remembers across reconnects, restarts and long days.

Crash-safe: memory/state.json keeps a cursor - how far into history.jsonl the
summary has got. Lines after it that were never summarised (Mint quit or crashed
first) are picked up again at the next start (the last day, at most ~30k
characters). checkpoint() summarises what is pending right away, for the session
to call before compacting, starting a new session or quitting.

history.jsonl is rotated past 4 MB: all but the newest ~1 MB goes to
history-archive/history-YYYYMMDD.jsonl (same format; journal.py reads both).

Gemini Live separately compresses its own context within a session (sliding
window); this is the layer that survives beyond one session.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from mint.core import config

log = logging.getLogger("mint.knowledge.conversation")

HISTORY = config.PROJECT_ROOT / "history.jsonl"
SUMMARY = config.PROJECT_ROOT / "summary.md"
# Tried in order until one answers. On this free key, single models were
# often out: gemini-3-flash-preview hit its quota (429) and gemini-3.5-flash
# was overloaded (503) at the same moment, while the lite models answered.
# On the free tier the Flash models allow ~20 requests a day each and ran out
# (AI Studio, 24 Sep: 3/3.5/3.6/3.8 Flash all at their daily cap) while the
# lite models had room (3.5 Flash Lite: 15 a minute) - so lite first.
MODELS = [m for m in (os.environ.get("MINT_SUMMARY_MODEL"), "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                      "gemini-flash-lite-latest", "gemini-3.7-flash", "gemini-3.5-flash",
                      "gemini-flash-latest") if m]

# Summarise once this much new conversation has accumulated (~2k tokens). Tool
# lines count a quarter: they are long and mostly mechanics.
SUMMARIZE_AFTER_CHARS = 8000
TOOL_WEIGHT = 0.25
KEEP_SUMMARY_CHARS = 2500   # small on purpose: facts live in the memory bank
CATCH_UP_HOURS = 24
CATCH_UP_CHARS = 30_000
ROTATE_AT = 4_000_000
KEEP_BYTES = 1_000_000
ROLES = ("user", "mint", "tool", "agent", "jarvis")

_PROMPT = """You keep the running notes of Mint, a voice assistant that operates the user's Mac. Today is \
{today}. Merge the new conversation into the existing notes.

The notes are about the CONVERSATION and the WORK: what the user has been asking for, what was done and \
where (which app, doc, tab, project, with exact names), what is unfinished or promised, and decisions made. \
Durable facts about the user (people, accounts, preferences) are kept elsewhere - do not repeat them here.

Sections, as short Markdown bullets:
### In progress - ONLY things that are not finished yet. Drop an item as soon as the newer lines show it was \
done, abandoned or replaced, and drop anything that has been "in progress" for more than 7 days.
### Done recently - finished work worth knowing about, newest first; only the last few days.
### Open / promised - what Mint promised or the user still wants done.
Start every bullet with the day it was last touched, like "(Oct 6)". Small UI chores (opening or switching apps, \
moving the orb, changing modes, volume) and look-ups of news, weather or the time are not worth a line. Never \
invent anything. At most {limit} characters.

Also write 0-3 "episodes": one line each for today's journal about something meaningful the user did with \
Mint, e.g. "Worked with Mint on installing a VS Code extension" or "Planned a weekend trek". Not chores, not \
look-ups, not failures of Mint. Usually 0 or 1.

EXISTING NOTES:
{summary}

NEW CONVERSATION (tool lines are shortened to name -> outcome):
{turns}

JSON only: {{"notes": "<the updated notes, Markdown>", "episodes": ["..."]}}"""


def archive_dir() -> Path:
    return HISTORY.parent / "history-archive"


def _state_path() -> Path:
    return HISTORY.parent / "memory" / "state.json"


def _weight(entry: dict) -> float:
    role = entry.get("role")
    if role == "tool":
        return len(entry.get("text", "")) * TOOL_WEIGHT
    return len(entry.get("text", "")) if role in ROLES else 0.0


_TOOL = re.compile(r"^(\w+)\((.*?)\) -> (.*)$", re.S)
_FAILED = re.compile(r"\b(FAILED|failed|error|Error|could not|couldn't|not found|refused|NOT RUN|timed out)\b")


def compress_tool(text: str) -> str:
    """'web_search(web search) -> Results (DuckDuckGo): 1. …' -> 'web_search -> Results (DuckDuckGo): 1.…'
    (short), keeping more of a failure."""
    match = _TOOL.match(text)
    if not match:
        return text[:120]
    name, args, outcome = match.groups()
    what = f"{name}({args[:50]})" if args and args.replace(" ", "_") != name else name
    if _FAILED.search(outcome[:200]):
        return f"{what} -> {outcome[:160]}"
    return f"{what} -> {outcome[:60]}"


class Memory:
    def __init__(self) -> None:
        self._pending: list[dict] = []
        self._pending_chars = 0.0
        self._lock = threading.Condition()
        self._io = threading.RLock()
        self._working = False
        self._caught_up = False

    # --- the cursor --------------------------------------------------------------------

    def _state(self) -> dict:
        try:
            state = json.loads(_state_path().read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, **changes) -> None:
        with self._io:
            state = self._state()
            state.update(changes)
            path = _state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            os.chmod(tmp, 0o600)
            tmp.replace(path)

    def _catch_up(self) -> None:
        """Once per run: lines after the cursor that never reached the summary (a crash, a quit without
        a checkpoint) become pending again - only the last day of them, at most CATCH_UP_CHARS."""
        with self._io:
            if self._caught_up:
                return
            self._caught_up = True
            try:
                size = HISTORY.stat().st_size
            except OSError:
                size = 0
            state = self._state()
            base = int(state.get("base", 0))
            if "cursor" not in state:          # first run with a cursor: start from here, not from the beginning
                self._write_state(base=base, cursor=base + size, at=time.time())
                return
            offset = int(state["cursor"]) - base
            if offset > size or offset < 0:    # the file was replaced or cleared
                offset = 0
            if offset >= size:
                return
            try:
                with open(HISTORY, "rb") as f:
                    f.seek(offset)
                    data = f.read()
            except OSError:
                return
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - CATCH_UP_HOURS * 3600))
        found, position = [], offset
        for raw in data.splitlines(keepends=True):
            position += len(raw)
            if not raw.endswith(b"\n"):
                break                          # a half-written last line: leave it for next time
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if entry.get("role") in ROLES and str(entry.get("t", "")) >= since and entry.get("text"):
                entry["_end"] = base + position
                found.append(entry)
        kept, total = [], 0
        for entry in reversed(found):
            total += len(entry["text"])
            if total > CATCH_UP_CHARS:
                break
            kept.append(entry)
        kept.reverse()
        if kept:
            log.info("memory: catching up on %d turns that were never summarised", len(kept))
            with self._lock:
                self._pending = kept + self._pending
                self._pending_chars += sum(_weight(e) for e in kept)

    # --- recording ---------------------------------------------------------------

    def add(self, role: str, text: str) -> None:
        text = " ".join(str(text).split())
        if not text:
            return
        self._catch_up()
        entry = {"t": time.strftime("%Y-%m-%d %H:%M"), "role": role, "text": text[:2000]}
        try:
            with self._io:
                with open(HISTORY, "ab") as f:
                    f.write((json.dumps(entry) + "\n").encode())
                    end = f.tell()
                os.chmod(HISTORY, 0o600)
                entry["_end"] = int(self._state().get("base", 0)) + end
                if end > ROTATE_AT:
                    self.rotate()
        except OSError:
            pass
        with self._lock:
            self._pending.append(entry)
            self._pending_chars += _weight(entry)
            due = self._pending_chars >= SUMMARIZE_AFTER_CHARS
        if due:
            self.summarize_in_background()
        # The same stream teaches skills: long or corrected tasks become how-tos.
        from mint.knowledge.learner import learner
        learner.observe(role, entry["text"])

    def rotate(self, max_bytes: int | None = None, keep_bytes: int | None = None) -> bool:
        """Past max_bytes, move all but the newest ~keep_bytes of history.jsonl (whole lines) to
        history-archive/history-YYYYMMDD.jsonl. The cursor keeps pointing at the same line."""
        max_bytes = ROTATE_AT if max_bytes is None else max_bytes
        keep_bytes = KEEP_BYTES if keep_bytes is None else keep_bytes
        with self._io:
            try:
                if HISTORY.stat().st_size <= max_bytes:
                    return False
                data = HISTORY.read_bytes()
            except OSError:
                return False
            cut = data.find(b"\n", max(0, len(data) - keep_bytes)) + 1
            state = self._state()
            unsummarised = int(state.get("cursor", -1)) - int(state.get("base", 0))
            if 0 < unsummarised < cut and len(data) - unsummarised <= max_bytes:
                cut = unsummarised             # lines not yet summarised stay where catch-up looks for them
            if cut <= 0 or cut >= len(data):
                return False
            folder = archive_dir()
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"history-{time.strftime('%Y%m%d')}.jsonl"
            with open(target, "ab") as f:
                f.write(data[:cut])
            os.chmod(target, 0o600)
            tmp = HISTORY.with_name(HISTORY.name + ".tmp")
            tmp.write_bytes(data[cut:])
            os.chmod(tmp, 0o600)
            tmp.replace(HISTORY)
            self._write_state(base=int(state.get("base", 0)) + cut)
            log.info("history rotated: %d bytes to %s", cut, target.name)
            return True

    # --- summarising -------------------------------------------------------------

    def summary(self) -> str:
        try:
            return SUMMARY.read_text().strip()
        except OSError:
            return ""

    def _take(self) -> list[dict]:
        batch, self._pending, self._pending_chars = self._pending, [], 0.0
        self._working = True
        return batch

    def summarize_in_background(self) -> None:
        with self._lock:
            if self._working or not self._pending:
                return
            batch = self._take()
        threading.Thread(target=self._summarize, args=(batch,), daemon=True, name="mint-summarize").start()

    def checkpoint(self, timeout: float = 20) -> None:
        """Summarise (and extract facts / episodes from) everything pending NOW, waiting at most
        `timeout` seconds. Returns at once when nothing is pending. A summary already running is waited
        for first."""
        self._catch_up()
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            while self._working:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._lock.wait(min(left, 0.5))
            if not self._pending:
                return
            batch = self._take()
        worker = threading.Thread(target=self._summarize, args=(batch,), daemon=True, name="mint-checkpoint")
        worker.start()
        worker.join(max(0.0, deadline - time.monotonic()))

    def summarize_now(self) -> str:
        """Fold everything pending into the summary, synchronously. Returns it."""
        self.checkpoint(timeout=120)
        return self.summary()

    def close(self, timeout: float = 8) -> None:
        """Before quitting: a checkpoint that never holds the quit up for long."""
        self.checkpoint(timeout=timeout)

    def _line(self, e: dict) -> str:
        text = compress_tool(e["text"]) if e.get("role") == "tool" else e["text"]
        return f"[{e['t']}] {e['role']}: {text}"

    def _summarize(self, batch: list[dict]) -> None:
        try:
            from mint.core import llm
            today = dt.date.today().strftime("%A %b %-d %Y")
            prompt = _PROMPT.format(today=today, limit=KEEP_SUMMARY_CHARS, summary=self.summary() or "(empty)",
                                    turns="\n".join(self._line(e) for e in batch))
            started = time.monotonic()
            text, model = llm.generate(prompt, MODELS, json_mode=True)
            notes, episodes = _parse(text)
            if not notes:
                raise RuntimeError("the summary came back empty")
            SUMMARY.write_text(notes[: KEEP_SUMMARY_CHARS + 500] + "\n")
            os.chmod(SUMMARY, 0o600)
            ends = [e["_end"] for e in batch if isinstance(e.get("_end"), int)]
            if ends:
                self._write_state(cursor=max(ends), at=time.time())
            log.info("memory summarised %d turns with %s in %.1fs", len(batch), model, time.monotonic() - started)
            print(f"  [memory: summarised {len(batch)} turns with {model}]", flush=True)
            from mint.knowledge import history_index
            history_index.ingest_later()       # the search index catches up too (its own thread)
            from mint.knowledge import memory as membank
            day = str(batch[-1].get("t", ""))[:10] or None
            for line in episodes[:3]:
                try:
                    membank.add(line, origin="auto", kind="episode", day=day)
                except Exception as error:
                    log.warning("episode not saved: %s", str(error)[:120])
            # Durable facts go to the memory bank, block by block.
            try:
                membank.extract("\n".join(f"{e['role']}: {e['text']}" for e in batch
                                          if e["role"] in ("user", "mint", "jarvis")))
            except Exception as error:
                log.warning("fact extraction failed: %s", str(error)[:120])
        except Exception as error:
            log.warning("summary failed: %s", str(error)[:160])
            with self._lock:   # keep the turns for next time
                self._pending = batch + self._pending
                self._pending_chars += sum(_weight(e) for e in batch)
        finally:
            with self._lock:
                self._working = False
                self._lock.notify_all()

    def clear(self) -> None:
        with self._lock:
            self._pending, self._pending_chars = [], 0.0
        for path in (HISTORY, SUMMARY, _state_path()):
            try:
                path.unlink()
            except OSError:
                pass
        self._caught_up = False


def _parse(text: str) -> tuple[str, list[str]]:
    """The model's JSON -> (notes, episodes). Plain Markdown (no JSON) is taken as the notes."""
    text = (text or "").strip()
    try:
        from mint.core import llm
        data = llm.parse_json(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        notes = str(data.get("notes") or "").strip()
        episodes = [" ".join(str(x).split()) for x in data.get("episodes") or [] if str(x).strip()]
        return notes, episodes
    return ("" if text.startswith("{") else text), []


def history_files(since: dt.datetime | None = None) -> list[Path]:
    """The archive files that may hold lines from `since` on, oldest first, then history.jsonl."""
    files = []
    for path in sorted(archive_dir().glob("history-*.jsonl")):
        try:
            rotated = dt.datetime.strptime(path.stem.split("-", 1)[1][:8], "%Y%m%d")
        except ValueError:
            rotated = None
        if since is None or rotated is None or rotated.date() >= since.date():
            files.append(path)
    return files + [HISTORY]


memory = Memory()


def checkpoint(timeout: float = 20) -> None:
    """Module-level: memory.checkpoint(timeout) - summarise what is pending now (blocking, bounded)."""
    memory.checkpoint(timeout)
