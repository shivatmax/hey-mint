"""Finding a screenshot by what is in it: "find the screenshot with the invoice number",
"the screenshot of the flight booking from last week", "show me that error screenshot".

Spotlight knows which files are screenshots (kMDItemIsScreenCapture), wherever they were
saved. Their text is read on the Mac with Apple's Vision framework - nothing is uploaded
for that - and kept in an index (only new or changed screenshots are read again). A
search matches the words first; only when that finds nothing, Gemini Flash Lite picks
from the best candidates' text. The index stays on this Mac (mode 600).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("mint.tools.screenshots")

INDEX = Path.home() / "Library" / "Application Support" / "Mint" / "screenshots.json"
MAX_SHOTS = 800
_lock = threading.Lock()
_indexing = {"running": False, "done": 0, "total": 0}


def _all() -> list[Path]:
    query = ('kMDItemIsScreenCapture == 1 || kMDItemFSName == "Screenshot*"cd || kMDItemFSName == "Screen Shot*"cd '
             '|| kMDItemFSName == "CleanShot*"cd || kMDItemFSName == "Shottr*"cd')
    done = subprocess.run(["mdfind", query], capture_output=True, text=True, timeout=20)
    paths = [Path(p) for p in done.stdout.splitlines() if p.lower().endswith((".png", ".jpg", ".jpeg", ".heic"))]
    paths = [p for p in paths if p.exists()]
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_SHOTS]


def read_text(path: Path) -> str:
    """The text in an image, read on the Mac (Vision, accurate)."""
    import Vision
    from Foundation import NSURL
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(path)), None)
    ok, _error = handler.performRequests_error_([request], None)
    if not ok:
        return ""
    lines = []
    for observation in request.results() or []:
        best = observation.topCandidates_(1)
        if best:
            lines.append(str(best[0].string()))
    return "\n".join(lines)


def _load() -> dict:
    try:
        return json.loads(INDEX.read_text())
    except (OSError, ValueError):
        return {}


def _save(index: dict) -> None:
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(index, f)
    tmp.replace(INDEX)


def update(limit_seconds: float = 25.0) -> tuple[dict, int]:
    """Read the screenshots not in the index yet (newest first) for up to `limit_seconds`;
    the rest continue in the background. -> (index, how many are still waiting)."""
    with _lock:
        index = _load()
        shots = _all()
        known = {str(p) for p in shots}
        index = {k: v for k, v in index.items() if k in known}          # deleted ones go
        todo = [p for p in shots if index.get(str(p), {}).get("mtime") != int(p.stat().st_mtime)]
        started = time.monotonic()
        for n, path in enumerate(todo):
            if time.monotonic() - started > limit_seconds:
                _save(index)
                rest = todo[n:]
                if rest and not _indexing["running"]:
                    threading.Thread(target=_background, args=(rest,), daemon=True, name="screenshot-index").start()
                return index, len(rest)
            try:
                index[str(path)] = {"mtime": int(path.stat().st_mtime), "text": read_text(path)[:4000]}
            except Exception as error:
                log.info("%s: %s", path.name, error)
        _save(index)
        return index, 0


def _background(paths: list[Path]) -> None:
    _indexing.update(running=True, done=0, total=len(paths))
    try:
        for path in paths:
            try:
                text = read_text(path)[:4000]
            except Exception:
                text = ""
            with _lock:
                index = _load()
                index[str(path)] = {"mtime": int(path.stat().st_mtime), "text": text}
                _save(index)
            _indexing["done"] += 1
    finally:
        _indexing["running"] = False


def _score(query: str, path: str, text: str) -> float:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2 and w not in {
        "the", "screenshot", "screenshots", "with", "from", "find", "show", "that", "of", "and", "last", "my"}]
    hay = (Path(path).name + " " + text).lower()
    return sum(1.0 + (0.5 if re.search(rf"\b{re.escape(w)}\b", hay) else 0) for w in words if w in hay) / max(1, len(words))


found = threading.local()          # this thread's last hits, for the notch (tools.py)


def search(query: str, open_it: bool = False) -> str:
    found.last = None
    index, waiting = update()
    if not index:
        return "There are no screenshots on this Mac (Spotlight finds none)."
    ranked = sorted(((_score(query, p, e["text"]), p) for p, e in index.items()), reverse=True)
    hits = [p for s, p in ranked if s >= 0.5][:5]
    how = "matched words"
    if not hits:
        from mint.core import llm
        pool = sorted(index.items(), key=lambda kv: kv[1]["mtime"], reverse=True)[:60]
        listing = "\n".join(f"{i}: {Path(p).name} | {e['text'][:300]!r}" for i, (p, e) in enumerate(pool))
        answer = llm.ask_json(f"Which of these screenshots (file name | the text in it) match: {query!r}? "
                              f'Return JSON {{"ids": [...]}}, best first, at most 5, [] if none.\n{listing}')
        ids = answer.get("ids") if isinstance(answer, dict) else []
        hits = [pool[int(i)][0] for i in ids or [] if str(i).isdigit() and int(i) < len(pool)]
        how = "picked by meaning"
    tail = f" ({waiting} newer screenshots are still being read; ask again in a minute for those.)" if waiting else ""
    if not hits:
        return f"No screenshot matches '{query}' among {len(index)}.{tail}"
    found.last = (list(hits), query, f"Screenshots: “{query}”")
    lines = []
    for p in hits:
        when = dt.datetime.fromtimestamp(index[p]["mtime"]).strftime("%a %d %b %H:%M")
        snippet = " ".join(index[p]["text"].split())[:140]
        lines.append(f"- {p} ({when}): {snippet}")
    if open_it:
        subprocess.run(["open", hits[0]], check=False)
    return (f"Screenshots matching '{query}' ({how}, from {len(index)}):\n" + "\n".join(lines)
            + ("\nOpened the first one." if open_it else "\nOffer to open or reveal one (file_action).") + tail)


PROMPT = """Screenshots: "find the screenshot with …", "that screenshot of the error / booking / invoice" -> \
find_screenshot (it reads the text in every screenshot on the Mac, on the Mac)."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="find_screenshot",
        description=("Find screenshots by the text or things in them (an invoice number, an error, a booking, a "
                     "chat), wherever they are saved. The text is read on this Mac."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "query": types.Schema(type=S, description="what is in it, in the user's words"),
            "open": types.Schema(type=types.Type.BOOLEAN, description="open the best match (default false)")},
            required=["query"]))]


HANDLERS = {"find_screenshot": lambda a: search(str(a.get("query") or ""), bool(a.get("open")))}
