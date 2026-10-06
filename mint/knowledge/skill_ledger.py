"""The skill ledger: every change to a skill file, by anyone, with the text before and after.

    skills/.ledger.jsonl             one line per change, oldest first
    skills/.ledger/blobs/<sha256>    the texts themselves, stored once per distinct text

An entry: {"id", "t", "action" (create | update | archive | move | rollback | ...), "skill" (file
name), "title", "actor" (user | mint | review | curator | teach | seed | learn), "reason", "path"
(relative to skills/), "to" (where the file went, for an archive or a move), "before" / "after"
(sha256 of the whole file, or null when there was none)}.

Only storage lives here; skillbook.history / skillbook.rollback give it meaning. Writing to the
ledger never raises: a skill change must not fail because its record could not be written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("mint.knowledge.skill_ledger")

KEEP_LINES = 4000          # the ledger is trimmed to this many entries past MAX_LINES
MAX_LINES = 5000
_lock = threading.Lock()


def _root() -> Path:
    from mint.knowledge import skills as skillbook
    return skillbook.ROOT


def ledger_path() -> Path:
    return _root() / ".ledger.jsonl"


def blobs_dir() -> Path:
    return _root() / ".ledger" / "blobs"


def sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


def store(text: str | None) -> str | None:
    """Keep a text; -> its sha256 (None for no text)."""
    if text is None:
        return None
    digest = sha(text)
    target = blobs_dir() / digest
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return digest


def read_blob(digest: str | None) -> str | None:
    if not digest or not all(c in "0123456789abcdef" for c in digest):
        return None
    try:
        return (blobs_dir() / digest).read_text()
    except OSError:
        return None


def record(action: str, skill: str, path: str, before: str | None, after: str | None, actor: str,
           reason: str = "", title: str = "", to: str = "", **extra) -> str | None:
    """Append one entry -> its id, or None when it could not be written (never raises)."""
    try:
        entry = {"id": uuid.uuid4().hex[:10], "t": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action,
                 "skill": skill, "title": title, "actor": actor or "?", "reason": " ".join(str(reason).split())[:300],
                 "path": path, "to": to, "before": store(before), "after": store(after)}
        entry.update({k: v for k, v in extra.items() if v not in (None, "")})
        with _lock:
            path_ = ledger_path()
            path_.parent.mkdir(parents=True, exist_ok=True)
            with open(path_, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _trim(path_)
        return entry["id"]
    except Exception as error:
        log.warning("skill ledger: could not record %s of %s: %s", action, skill, error)
        return None


def _trim(path: Path) -> None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    if len(lines) > MAX_LINES:
        path.write_text("\n".join(lines[-KEEP_LINES:]) + "\n")


def entries(skill: str = "", limit: int | None = None) -> list[dict]:
    """Newest first; only this skill's when `skill` is given. Bad lines are skipped."""
    try:
        lines = ledger_path().read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and (not skill or row.get("skill") == skill):
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
    return rows


def get(entry_id: str) -> dict | None:
    entry_id = str(entry_id or "").strip()
    return next((r for r in entries() if r.get("id") == entry_id), None) if entry_id else None


def describe(entry: dict) -> str:
    """One line for a person: when, who, what."""
    who = {"user": "you", "mint": "Mint (asked in conversation)", "review": "Mint's review after a task",
           "curator": "the tidy-up", "teach": "a lesson", "seed": "the starter set", "learn": "learn-this"}.get(
        entry.get("actor", ""), entry.get("actor", "?"))
    why = f" - {entry['reason']}" if entry.get("reason") else ""
    return f"[{entry.get('id')}] {entry.get('t', '')[:16]} {entry.get('action')} by {who}{why}"
