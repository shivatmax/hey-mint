"""Where a file Mint makes should go: where the user said, else Mint's own folder.

make_spreadsheet, data_to_sheet and create_pdf used to always save in Mint's storage, even when the request
named a place ("put them in ~/Shop/cheapest.xlsx", "save it in ~/Reports") - bench: four tasks failed on
that. `destination` takes an explicit `save_to` (a file or a folder), else a path with the right suffix in the
request, else a folder the request says to save into, else the default; never overwrites; refuses places Mint
must not write to.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_PATH = re.compile(r"(~/[^\s'\"“”,;]+|/Users/[^\s'\"“”,;]+)")
_SAVE_INTO = re.compile(r"\b(save|put|store|keep|write|place|export)\b[^.]*?\b(in|into|to|under)\s+(the\s+)?(folder\s+)?"
                        r"(~/[^\s'\"“”,;]+|/Users/[^\s'\"“”,;]+)", re.I)


def _clean(raw: str) -> Path:
    return Path(os.path.expanduser(raw.strip().rstrip(".)"))).resolve()


def _free(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 100):
        other = path.with_name(f"{path.stem} {n}{path.suffix}")
        if not other.exists():
            return other
    return path


def requested(suffix: str) -> str:
    """The place the user's request names for a new `suffix` file: a full path, a folder, or ''."""
    from mint.app import live
    request = live.request() or ""
    named = [p for p in _PATH.findall(request) if p.lower().rstrip(".)").endswith(suffix)]
    if named:
        return str(_clean(named[-1]))
    into = _SAVE_INTO.findall(request)
    if into:
        folder = _clean(into[-1][4])
        if not folder.suffix:
            return str(folder)
    return ""


def destination(default: Path, suffix: str, save_to: str = "") -> tuple[Path, str]:
    """(where to save, a note for the reply). `default` already has the file name and suffix."""
    from mint.tools import harness as harness_tools
    save_to = (save_to or "").strip() or requested(suffix)
    if not save_to:
        return default, ""
    raw = _clean(save_to)
    target = raw if raw.suffix.lower() == suffix else raw / default.name
    why = harness_tools._blocked(target, write=True)
    if why:
        return default, f"(Not saved where asked - {why} - so it is in Mint's folder.)"
    target.parent.mkdir(parents=True, exist_ok=True)
    return _free(target), ""
