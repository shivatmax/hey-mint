"""A search index over the conversation history, so "what did I ask about X" is quick.

history.jsonl (and its rotated-out parts in history-archive/) is the record of every
turn. Reading all of it for each question gets slow as it grows, so this keeps a
local SQLite full-text index of it: memory/history.db (only the user can read it).

- Incremental: the archive files (oldest first) and then history.jsonl are read as one
  stream. Rotation (memory.py) only moves the oldest lines of history.jsonl to the end of
  the newest archive file, so the stream never changes - the index remembers how far it
  has read and only reads what was added since. If the files change in another way
  (cleared, an archive removed) it starts over.
- Each line is a row: time, who (user / mint / tool / agent), text (tool lines only their
  first 500 characters, and not in the trigram index) and its conversation (a gap of more than 30 minutes, or a new
  session, starts the next one).
- Search: the question's words (quoted, so nothing in them is read as query syntax),
  all of them first and then any of them, ranked by BM25 with a boost for recent lines;
  a trigram index finds parts of words when nothing else matched. Time bounds come from
  journal.when_range.
- The hits are grouped by conversation: the best one comes with the lines around it, the
  others with their first line. around() reads more around any line ("scroll").

Ingesting runs in a background thread (at start and after each memory summary), never on
the voice path. Without SQLite FTS5, journal.py falls back to reading the files.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mint.knowledge.history_index")

GAP = 30 * 60                 # seconds of silence that start a new conversation
TOOL_CHARS = 500              # tool lines are long and mostly mechanics: their start is enough
TEXT_CHARS = 2000
ROLES = {"user": "user", "mint": "mint", "jarvis": "mint", "tool": "tool", "agent": "agent"}
WHO = {"user": "User", "mint": "Mint", "tool": "Did", "agent": "Agent"}
ROLE_WEIGHT = {"user": 1.0, "mint": 0.9, "agent": 0.85, "tool": 0.6}
RECENT_DAYS = 14              # recency boost: up to +50% for today, fading over ~2 weeks
MAX_TERMS = 12
TAIL = 64                     # bytes before the read position that must not change between runs

STOP = {"what", "when", "where", "which", "who", "whom", "why", "how", "did", "does", "do", "doing", "done", "about",
        "that", "this", "these", "those", "with", "have", "has", "had", "your", "you", "yours", "from", "were",
        "was", "there", "they", "them", "then", "than", "tell", "ask", "asked", "asking", "the", "and", "for",
        "mint", "are", "is", "am", "be", "been", "can", "could", "would", "should", "will", "me", "my", "mine",
        "we", "our", "us", "it", "its", "of", "to", "in", "on", "at", "a", "an", "or", "if", "so", "any", "some",
        "said", "say", "talk", "talked", "talking", "remember", "again", "earlier", "before", "last", "time",
        "ago", "yesterday", "today", "week", "please", "just", "i", "i'm", "im", "thing", "things", "something",
        "anything", "into", "out", "up", "get", "got", "not", "no", "yes", "all", "one", "lot", "also", "very",
        "currently", "recently", "lately", "still", "ever"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS lines (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, role TEXT NOT NULL,
                                  text TEXT NOT NULL, conv INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS lines_ts ON lines(ts);
CREATE INDEX IF NOT EXISTS lines_conv ON lines(conv);
CREATE VIRTUAL TABLE IF NOT EXISTS lines_fts USING fts5(text, content='lines', content_rowid='id',
                                                        tokenize='unicode61 remove_diacritics 2');
"""
_TRIGRAM = """CREATE VIRTUAL TABLE IF NOT EXISTS lines_tri USING fts5(text, content='lines', content_rowid='id',
                                                                    tokenize='trigram')"""


class Unavailable(RuntimeError):
    """This SQLite has no FTS5: search the files instead."""


@dataclass
class Row:
    id: int
    ts: int
    role: str
    text: str
    conv: int
    score: float = 0.0

    def line(self, ref: bool = False, full: bool = False) -> str:
        """'2026-10-06 Tue 14:20 User: …' - the journal's format (and its lengths, unless `full`)."""
        when = time.strftime("%Y-%m-%d %a %H:%M", time.localtime(self.ts))
        text = self.text if full else self.text[:400 if self.role == "tool" else 700]
        return f"{when}{f' #{self.id}' if ref else ''} {WHO.get(self.role, self.role)}: {text}"


@dataclass
class Found:
    hits: list[Row] = field(default_factory=list)        # ranked, best first, deduplicated
    top: list[Row] = field(default_factory=list)         # the best hit with the lines around it
    others: list[Row] = field(default_factory=list)      # the first hit of each other conversation
    terms: list[str] = field(default_factory=list)
    how: str = ""                                        # "all words", "any word", "part of a word", ""


# --- query -----------------------------------------------------------------------------------------

def terms(question: str) -> list[str]:
    """The words worth searching for: 'Which anime am I watching?' -> ['anime', 'watching'].
    Quoted phrases stay together ('"blue kayak"' -> ['blue kayak']). Punctuation, colons, emoji
    and query operators are dropped - every term is quoted later, so nothing is query syntax."""
    question = str(question or "")[:500]
    found: list[str] = []
    for phrase in re.findall(r'"([^"]+)"', question):
        words = re.findall(r"\w+", phrase.lower())
        if words:
            found.append(" ".join(words))
    rest = re.sub(r'"[^"]*"', " ", question).lower()
    for word in re.findall(r"\w+(?:['’]\w+)?", rest):
        word = word.replace("’", "'")
        if word in STOP or (len(word) < 3 and not word.isdigit()) or word.replace("_", "") == "":
            continue
        found.append(word.split("'")[0] if word.endswith(("'s", "'t")) else word)
    seen, out = set(), []
    for term in found:
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out[:MAX_TERMS]


def _stem(word: str) -> str:
    """A crude stem for prefix search: 'watching' -> 'watch', 'extensions' -> 'extension'."""
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def match_query(words: list[str], any_word: bool = False, prefix: bool = True) -> str:
    """An FTS5 MATCH expression from search terms: every term quoted (so ':', '-', 'AND', '*'
    inside it mean nothing), single words also as a prefix of their stem."""
    parts = []
    for term in words:
        if " " in term or not prefix or term.isdigit():
            parts.append(_quote(term))
        else:
            stem = _stem(term)
            parts.append(f"{_quote(stem)}*" if len(stem) >= 3 else _quote(term))
    return (" OR " if any_word else " AND ").join(parts)


# --- the index ---------------------------------------------------------------------------------------

class HistoryIndex:
    def __init__(self, db: Path, history: Path, archive: Path, io_lock=None) -> None:
        self.db = Path(db)
        self.history = Path(history)
        self.archive = Path(archive)
        self.io_lock = io_lock                 # memory.py's lock: no rotation half-way through a read
        self._lock = threading.RLock()
        self.trigram = False
        self.ready = False

    # --- connection --------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        if not self.db.exists():
            os.close(os.open(self.db, os.O_CREAT | os.O_WRONLY, 0o600))
        conn = sqlite3.connect(self.db, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not self.ready:
            try:
                os.chmod(self.db, 0o600)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
            except sqlite3.OperationalError as error:
                conn.close()
                if "fts5" in str(error).lower() or "no such module" in str(error).lower():
                    raise Unavailable(str(error)) from error
                raise
            try:
                conn.execute(_TRIGRAM)
                self.trigram = True
            except sqlite3.OperationalError:
                self.trigram = False           # SQLite before 3.34: no parts of words
            conn.commit()
            self.ready = True
        return conn

    def _meta(self, conn: sqlite3.Connection) -> dict:
        row = conn.execute("SELECT value FROM meta WHERE key='state'").fetchone()
        try:
            state = json.loads(row["value"]) if row else {}
        except ValueError:
            state = {}
        return state if isinstance(state, dict) else {}

    # --- ingest ----------------------------------------------------------------------------------

    def _files(self) -> list[tuple[Path, int]]:
        """The stream: archive files oldest first, then history.jsonl, with their sizes."""
        files = []
        for path in sorted(self.archive.glob("history-*.jsonl")) if self.archive.is_dir() else []:
            try:
                files.append((path, path.stat().st_size))
            except OSError:
                pass
        try:
            files.append((self.history, self.history.stat().st_size))
        except OSError:
            files.append((self.history, 0))
        return files

    def _read(self, files: list[tuple[Path, int]], start: int, end: int) -> bytes:
        """Bytes [start, end) of the stream."""
        out, offset = [], 0
        for path, size in files:
            lo, hi = max(start, offset), min(end, offset + size)
            if lo < hi:
                try:
                    with open(path, "rb") as f:
                        f.seek(lo - offset)
                        out.append(f.read(hi - lo))
                except OSError:
                    pass
            offset += size
        return b"".join(out)

    def _consistent(self, state: dict, files: list[tuple[Path, int]]) -> bool:
        """The stream still starts with what was indexed: archives only grew at the end, nothing
        before the read position changed."""
        done = int(state.get("done", 0))
        if done == 0:
            return True
        known = state.get("archives") or []
        current = [(p.name, s) for p, s in files[:-1]]
        if len(current) < len(known):
            return False
        for (name, size), (now_name, now_size) in zip(known, current):
            if name != now_name or now_size < size:
                return False
        if sum(s for _, s in files) < done:
            return False
        tail = self._read(files, max(0, done - TAIL), done)
        return tail.hex() == state.get("tail", "")

    def ingest(self) -> int:
        """Index what was added since the last time. Returns the number of new rows."""
        with self._lock:
            conn = self._connect()
            try:
                state = self._meta(conn)
                lock = self.io_lock
                if lock is not None:
                    lock.acquire()
                try:
                    files = self._files()
                    if not self._consistent(state, files):
                        log.info("history index: the history files changed; indexing them again")
                        self._clear(conn)
                        state = {}
                    done = int(state.get("done", 0))
                    total = sum(s for _, s in files)
                    data = self._read(files, done, total) if total > done else b""
                    archives = [(p.name, s) for p, s in files[:-1]]
                finally:
                    if lock is not None:
                        lock.release()
                cut = data.rfind(b"\n") + 1            # a half-written last line waits for next time
                if cut <= 0:
                    if archives != state.get("archives"):
                        state["archives"] = archives
                        self._save(conn, state)
                        conn.commit()
                    return 0
                added = self._add(conn, data[:cut], state)
                state["tail"] = (bytes.fromhex(state.get("tail", "")) + data[:cut])[-TAIL:].hex()
                state["done"] = done + cut
                state["archives"] = archives
                self._save(conn, state)
                conn.commit()
                return added
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _add(self, conn: sqlite3.Connection, data: bytes, state: dict) -> int:
        conv, last = int(state.get("conv", 0)), int(state.get("last", 0))
        first = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM lines").fetchone()[0]) + 1
        rows = []
        for raw in data.splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            try:
                ts = int(time.mktime(time.strptime(str(entry.get("t", ""))[:16], "%Y-%m-%d %H:%M")))
            except (ValueError, OverflowError):
                continue
            role = entry.get("role")
            if role == "marker":                       # a new session or a compaction
                last = 0
                continue
            role = ROLES.get(str(role))
            text = " ".join(str(entry.get("text", "")).split())
            if not role or not text:
                continue
            if not last or ts - last > GAP:
                conv += 1
            last = ts
            rows.append((ts, role, text[:TOOL_CHARS if role == "tool" else TEXT_CHARS], conv))
        if rows:
            conn.executemany("INSERT INTO lines(ts, role, text, conv) VALUES (?, ?, ?, ?)", rows)
            conn.execute("INSERT INTO lines_fts(rowid, text) SELECT id, text FROM lines WHERE id >= ?", (first,))
            if self.trigram:
                # Not tool lines: they would double the index for parts of paths and JSON.
                conn.execute("INSERT INTO lines_tri(rowid, text) SELECT id, text FROM lines "
                             "WHERE id >= ? AND role != 'tool'", (first,))
        state["conv"], state["last"] = conv, last
        return len(rows)

    def _save(self, conn: sqlite3.Connection, state: dict) -> None:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('state', ?)", (json.dumps(state),))

    def _clear(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM lines")
        conn.execute("INSERT INTO lines_fts(lines_fts) VALUES ('delete-all')")
        if self.trigram:
            conn.execute("INSERT INTO lines_tri(lines_tri) VALUES ('delete-all')")
        conn.execute("DELETE FROM meta")

    # --- reading ----------------------------------------------------------------------------------

    @staticmethod
    def _bounds(start: dt.datetime | None, end: dt.datetime | None) -> tuple[int, int]:
        return (int(start.timestamp()) if start else 0, int(end.timestamp()) if end else 2 ** 40)

    def between(self, start: dt.datetime | None = None, end: dt.datetime | None = None) -> list[Row]:
        """Every row in the time range, oldest first."""
        a, b = self._bounds(start, end)
        conn = self._connect()
        try:
            return [Row(r["id"], r["ts"], r["role"], r["text"], r["conv"]) for r in conn.execute(
                "SELECT id, ts, role, text, conv FROM lines WHERE ts BETWEEN ? AND ? ORDER BY id", (a, b))]
        finally:
            conn.close()

    def size(self, start: dt.datetime | None = None, end: dt.datetime | None = None) -> tuple[int, int]:
        """(rows, characters) in the time range."""
        a, b = self._bounds(start, end)
        conn = self._connect()
        try:
            n, chars = conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(text)), 0) FROM lines "
                                    "WHERE ts BETWEEN ? AND ?", (a, b)).fetchone()
            return int(n), int(chars)
        finally:
            conn.close()

    def around(self, row_id: int, before: int = 10, after: int = 10, same_conversation: bool = True) -> list[Row]:
        """The lines around one row (the scroll), oldest first."""
        conn = self._connect()
        try:
            anchor = conn.execute("SELECT conv FROM lines WHERE id=?", (int(row_id),)).fetchone()
            if anchor is None:
                return []
            where, extra = ("AND conv = ?", (anchor["conv"],)) if same_conversation else ("", ())
            older = conn.execute(f"SELECT id, ts, role, text, conv FROM lines WHERE id < ? {where} "
                                 "ORDER BY id DESC LIMIT ?", (int(row_id), *extra, max(0, before))).fetchall()
            newer = conn.execute(f"SELECT id, ts, role, text, conv FROM lines WHERE id >= ? {where} "
                                 "ORDER BY id LIMIT ?", (int(row_id), *extra, max(1, after + 1))).fetchall()
            return [Row(r["id"], r["ts"], r["role"], r["text"], r["conv"]) for r in list(reversed(older)) + newer]
        finally:
            conn.close()

    def _match(self, conn: sqlite3.Connection, table: str, query: str, a: int, b: int, limit: int) -> list[Row]:
        try:
            rows = conn.execute(
                f"SELECT l.id, l.ts, l.role, l.text, l.conv, bm25({table}) AS rank FROM {table} "
                f"JOIN lines l ON l.id = {table}.rowid WHERE {table} MATCH ? AND l.ts BETWEEN ? AND ? "
                "ORDER BY rank LIMIT ?", (query, a, b, limit)).fetchall()
        except sqlite3.OperationalError as error:       # a query FTS5 still cannot parse: no hits
            log.debug("history search %r failed: %s", query, error)
            return []
        return [Row(r["id"], r["ts"], r["role"], r["text"], r["conv"], -float(r["rank"])) for r in rows]

    def search(self, question: str, start: dt.datetime | None = None, end: dt.datetime | None = None,
               limit: int = 60, now: float | None = None) -> tuple[list[Row], str, list[str]]:
        """Ranked hits (best first, the same text only once), how they were found, the terms."""
        words = terms(question)
        if not words:
            return [], "", []
        a, b = self._bounds(start, end)
        conn = self._connect()
        try:
            how = "all words"
            hits = self._match(conn, "lines_fts", match_query(words), a, b, limit * 3)
            if len(hits) < limit and len(words) > 1:
                seen = {h.id for h in hits}
                for h in hits:
                    h.score *= 1.5                      # every word matched: ahead of lines with only some
                more = [h for h in self._match(conn, "lines_fts", match_query(words, any_word=True), a, b, limit * 3)
                        if h.id not in seen]
                how = "any word" if not hits else how
                hits += more
            if not hits and self.trigram:
                parts = [_quote(w) for w in words if len(w) >= 3]
                if parts:
                    hits = self._match(conn, "lines_tri", " OR ".join(parts), a, b, limit * 3)
                    how = "part of a word"
        finally:
            conn.close()
        if not hits:
            return [], "", words
        now = now if now is not None else time.time()
        best = max(h.score for h in hits) or 1.0
        for h in hits:
            age = max(0.0, (now - h.ts) / 86400)
            h.score = (h.score / best) * ROLE_WEIGHT.get(h.role, 0.7) * (1 + 0.5 * math.exp(-age / RECENT_DAYS))
        hits.sort(key=lambda h: (-h.score, -h.id))
        seen_text, ranked = set(), []
        for h in hits:
            key = h.text.lower()[:200]
            if key in seen_text:
                continue                                # the same request asked again: once is enough
            seen_text.add(key)
            ranked.append(h)
        return ranked[:limit], how, words

    def find(self, question: str, start: dt.datetime | None = None, end: dt.datetime | None = None,
             window: int = 6, groups: int = 8, now: float | None = None) -> Found:
        """Search, grouped by conversation: the best hit with `window` lines either side of it,
        and the first hit of each of up to `groups` other conversations."""
        hits, how, words = self.search(question, start, end, now=now)
        found = Found(hits=hits, terms=words, how=how)
        if not hits:
            return found
        top = hits[0]
        found.top = self.around(top.id, window, window)
        shown = {top.conv}
        for h in hits[1:]:
            if h.conv in shown:
                continue
            shown.add(h.conv)
            found.others.append(h)
            if len(found.others) >= groups:
                break
        return found

    def stats(self) -> dict:
        conn = self._connect()
        try:
            n = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
            convs = conn.execute("SELECT COUNT(DISTINCT conv) FROM lines").fetchone()[0]
        finally:
            conn.close()
        size = sum(p.stat().st_size for p in self.db.parent.glob(self.db.name + "*") if p.exists())
        return {"rows": n, "conversations": convs, "bytes": size, "trigram": self.trigram}


# --- the shared index ------------------------------------------------------------------------------

_indexes: dict[Path, HistoryIndex] = {}
_unavailable: set[Path] = set()
_guard = threading.Lock()
_running = threading.Event()


def get(history: Path | None = None) -> HistoryIndex | None:
    """The index next to `history` (default: Mint's history.jsonl), or None without FTS5."""
    if history is None:
        from mint.knowledge import journal
        history = journal.HISTORY
    history = Path(history)
    with _guard:
        if history in _unavailable:
            return None
        index = _indexes.get(history)
        if index is None:
            lock = None
            try:
                from mint.knowledge import conversation as memory
                if Path(memory.HISTORY) == history:
                    lock = memory.memory._io
            except Exception:
                lock = None
            index = HistoryIndex(history.parent / "memory" / "history.db", history,
                                 history.parent / "history-archive", lock)
            try:
                index._connect().close()
            except Unavailable as error:
                log.warning("history search without an index (no SQLite FTS5): %s", error)
                _unavailable.add(history)
                return None
            except (sqlite3.Error, OSError) as error:
                log.warning("history index unavailable: %s", str(error)[:160])
                return None
            _indexes[history] = index
        return index


def reset(history: Path | None = None) -> None:
    """Forget a broken index file: it is built again from the history on next use."""
    index = get(history)
    if index is None:
        return
    with index._lock:
        for path in index.db.parent.glob(index.db.name + "*"):
            try:
                path.unlink()
            except OSError:
                pass
        index.ready = False


def ingest_later(delay: float = 0.0) -> None:
    """Bring the index up to date in a background thread (one at a time); never blocks."""
    if _running.is_set():
        return
    _running.set()

    def work() -> None:
        try:
            if delay:
                time.sleep(delay)
            index = get()
            if index is None:
                return
            started = time.monotonic()
            try:
                added = index.ingest()
            except sqlite3.DatabaseError as error:
                log.warning("history index broken (%s); building it again", str(error)[:120])
                reset()
                added = index.ingest()
            if added:
                log.info("history index: %d new lines in %.0f ms", added, (time.monotonic() - started) * 1000)
        except Exception as error:
            log.warning("history index update failed: %s", str(error)[:160])
        finally:
            _running.clear()
    threading.Thread(target=work, daemon=True, name="mint-history-index").start()
