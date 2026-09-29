"""Where the user asked for an agent's result, and getting it there.

An agent works in its own workspace (~/Documents/Mint/agents/<name>/...), which
stays its scratch space. When the user names a destination ("save it as
~/Notes/x.md", "put it on my Desktop"), that destination is:

* found - from delegate_task's save_to, or else from a path in the task text;
* checked - inside the home folder and not a private, system or hidden place
  (Mint's own write rules, harness_tools._blocked);
* writable by the agent - write_file may use its full path (the only place
  outside the workspace it may write);
* delivered at the end - if the agent left the result in its workspace, the
  hub copies it there. An existing file is backed up before it is replaced.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger("mint.agents")

HOME = Path.home()
_TEXT = {".md", ".markdown", ".txt", ".csv", ".json", ".html", ".htm", ".yaml", ".yml", ".py", ".js", ".ts",
         ".css", ".tex", ".rst", ".xml", ".sh", ".toml", ".org"}
_PLACES = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads"}
_VERB = r"(?:save|saving|saved|write|writing|put|putting|store|deliver|export|place|drop|output|keep)"
# "save it as a Markdown file at ~/x/y.md", "write it to ~/Documents/Notes/", "put it in ~/Desktop"
_PATH_AFTER_VERB = re.compile(
    _VERB + r"\b[^\n]{0,80}?\b(?:as|to|in|into|at|under)\s+(?:the\s+)?(?:folder\s+|file\s+|path\s+)?"
    r"[`\"'“]?((?:~|/Users/|/Volumes/)[^\s`\"'”,;]*)", re.I)
# "save it to my Desktop", "put it in the Downloads folder"
_PLACE_AFTER_VERB = re.compile(
    _VERB + r"\b[^\n]{0,60}?\b(?:to|in|into|on|at)\s+(?:my|the)\s+(desktop|documents|downloads)\b", re.I)


def find(task: str, save_to: str = "") -> str:
    """The destination the user named, as text ('' if none): save_to wins, else the task text."""
    if (save_to or "").strip():
        return save_to.strip()
    match = _PATH_AFTER_VERB.search(task or "")
    if match:
        return match.group(1).rstrip(".)!?")
    match = _PLACE_AFTER_VERB.search(task or "")
    return f"~/{_PLACES[match.group(1).lower()]}/" if match else ""


def _short(path: Path) -> str:
    text = str(path)
    return "~" + text[len(str(HOME)):] if text.startswith(str(HOME)) else text


def _blocked(path: Path) -> str:
    """Why the agent may not write `path`, or '' - Mint's own rules, plus home-folder only."""
    try:
        real = path.resolve()
    except OSError:
        real = path
    if HOME not in real.parents:
        return f"{_short(real)} is outside the home folder"
    from mint.tools.harness import _blocked as mint_blocked
    return mint_blocked(real, write=True)


class Destination:
    """A file (has a suffix, e.g. ~/x/notes.md) or a folder (~/x/ or an existing folder)."""

    def __init__(self, path: Path, folder: bool) -> None:
        self.path = path
        self.folder = folder
        self.written: list[Path] = []        # what the agent wrote here itself

    def __str__(self) -> str:
        return _short(self.path) + ("/" if self.folder else "")

    @classmethod
    def parse(cls, raw: str) -> tuple["Destination | None", str]:
        """(destination, '') or (None, why it cannot be used)."""
        text = (raw or "").strip().strip("\"'`“”")
        if not text:
            return None, ""
        first, _, rest = text.partition("/")
        if first.lower() in _PLACES:                         # "Desktop/report.md"
            text = f"~/{_PLACES[first.lower()]}/{rest}"
        if not text.startswith(("~", "/")):
            return None, f"'{raw}' is not a full path (start it with ~/ or /Users/)"
        path = Path(os.path.expanduser(text))
        folder = text.endswith("/") or path.is_dir() or not path.suffix
        why = _blocked(path)
        if why:
            return None, why
        return cls(path, folder), ""

    def covers(self, path: Path) -> bool:
        """May the agent write `path` directly?"""
        try:
            path = Path(os.path.expanduser(str(path))).resolve()
            mine = self.path.resolve()
        except OSError:
            return False
        return path == mine if not self.folder else mine in path.parents

    def note(self) -> str:
        """For the agent's system prompt."""
        if self.folder:
            return (f"\nDESTINATION. The user wants the result in the folder {self.path}/. Save the finished "
                    f"deliverable(s) there with write_file, using full paths inside it (e.g. "
                    f"'{self.path / 'result.md'}') - the only place outside your workspace you may write. Keep "
                    "notes and drafts in your workspace. In your final answer, give the full path(s).")
        return (f"\nDESTINATION. The user wants the result saved as {self.path}. Save the finished deliverable with "
                f"write_file using exactly that path - the only place outside your workspace you may write "
                "(an existing file there is backed up first). Keep notes and drafts in your workspace. In your "
                "final answer, give that full path.")


# --- writing into the destination ---------------------------------------------------------------

def put(target: Path, data: bytes | str) -> str:
    """Write `data` at `target` with Mint's rules: allowed place, no secrets, backup before replacing.
    Returns 'Wrote …' or 'FAILED: …'."""
    from mint.tools.harness import BACKUPS, _remember_made
    why = _blocked(target)
    if why:
        return f"FAILED: {why}."
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if target.suffix.lower() in _TEXT or isinstance(data, str):
        from mint.knowledge.skills import has_secret
        if has_secret(raw.decode("utf-8", errors="ignore")):
            return "FAILED: the text looks like it holds a password, key or card number; not written."
    if target.is_dir():
        return f"FAILED: {_short(target)} is a folder."
    backup = None
    existed = target.exists()
    if existed:
        try:
            if target.read_bytes() == raw:
                return f"Wrote {_short(target)} (already there, unchanged)."
        except OSError:
            pass
        BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = f"{dt.datetime.now():%Y%m%d-%H%M%S}"
        backup = BACKUPS / f"{stamp}-{target.name}"
        n = 1
        while backup.exists():                   # two backups in one second must not clobber each other
            n += 1
            backup = BACKUPS / f"{stamp}-{n}-{target.name}"
        shutil.copy2(target, backup)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    except OSError as error:
        return f"FAILED: could not write {_short(target)}: {error.strerror or error}"
    try:
        _remember_made(target)
        from mint.tools import undo
        undo.record("file", f"{'replacing' if existed else 'creating'} {_short(target)} (agent result)",
                    {"kind": "file_restore", "path": str(target), "backup": str(backup),
                     "mtime": target.stat().st_mtime} if backup else
                    {"kind": "file_trash", "path": str(target), "mtime": target.stat().st_mtime})
    except Exception:
        log.debug("could not note the delivered file", exc_info=True)
    return f"Wrote {_short(target)} ({len(raw)} bytes)." + (
        f" The previous version is saved at {_short(backup)}." if backup else "")


# --- the end of a run ---------------------------------------------------------------------------

def _outputs(files: list[str], workspace: Path) -> list[Path]:
    """The files the run made, oldest first, that still exist (not the team board)."""
    found = []
    for name in dict.fromkeys(files):
        path = Path(name) if os.path.isabs(name) else workspace / name
        if path.is_file() and path.name != "board.md" and path not in found:
            found.append(path)
    return found


def deliver(dest: Destination, files: list[str], workspace: Path, result: str) -> tuple[list[Path], str]:
    """Make sure the result is at `dest`. Returns (paths now there, a problem or '')."""
    if dest.written and all(p.exists() for p in dest.written):
        return list(dest.written), ""
    outputs = [p for p in _outputs(files, workspace) if not dest.covers(p)]
    if not dest.folder:
        pick = (next((p for p in reversed(outputs) if p.name == dest.path.name), None)
                or next((p for p in reversed(outputs) if p.suffix.lower() == dest.path.suffix.lower()), None)
                or next((p for p in reversed(outputs) if p.suffix.lower() in _TEXT), None))
        if pick is not None:
            said = put(dest.path, pick.read_bytes())
        elif result.strip() and dest.path.suffix.lower() in _TEXT:
            said = put(dest.path, result.strip() + "\n")     # it answered in text only
        else:
            return [], "the agent made no file to put there"
        return ([dest.path], "") if said.startswith("Wrote") else ([], said.removeprefix("FAILED: "))
    placed, problems = [], []
    for path in outputs[:200]:
        try:
            relative = path.relative_to(workspace)
        except ValueError:
            relative = Path(path.name)
        said = put(dest.path / relative, path.read_bytes())
        if said.startswith("Wrote"):
            placed.append(dest.path / relative)
        else:
            problems.append(said.removeprefix("FAILED: "))
    if not outputs:
        return [], "the agent made no file to put there"
    return placed, "; ".join(problems[:3])
