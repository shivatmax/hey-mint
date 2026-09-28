"""Tidying a folder, with a preview and an undo: "clean up my Downloads", "sort my
Desktop by project", "put the invoices in Downloads into folders by month".

1. plan - reads the folder's files (name, kind, size, date, and the first lines of each
   document's text), finds exact duplicates, and Gemini Flash Lite proposes where each
   file goes (sub-folders of that folder) and a better name only when the current one
   says nothing ("document (3).pdf"). Nothing moves yet: the plan is summarised for
   the user, who can change it ("keep the screenshots where they are").
2. apply - moves the files as planned. Never overwrites (a clash gets " 2"), never
   deletes (duplicates go to a Duplicates folder), skips anything changed since the
   plan. Every move is written to a journal.
3. undo - moves everything from the last tidy-up back where it was.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("mint.tools.tidy")

JOURNALS = Path.home() / "Library" / "Application Support" / "Mint" / "tidy"
MAX_FILES = 400
DOCS = {".pdf", ".doc", ".docx", ".rtf", ".txt", ".md", ".pages", ".odt", ".csv"}
_plan: dict = {}


def _folder(text: str) -> Path | None:
    from mint.tools import harness as harness_tools
    text = str(text or "").strip() or "~/Downloads"
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        found, _why = harness_tools._resolve(text, must_exist=True)
        path = found or path
    return path if path.is_dir() else None


def _digest(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _snippet(path: Path) -> str:
    if path.suffix.lower() not in DOCS or path.stat().st_size > 30_000_000:
        return ""
    try:
        from mint.tools import harness as harness_tools
        text, _ = harness_tools._text_of(path)
        return " ".join(text.split())[:240]
    except Exception:
        return ""


def _scan(folder: Path) -> list[dict]:
    now = time.time()
    items = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file() or entry.name.startswith(".") or entry.suffix.lower() in {".part", ".crdownload",
                                                                                          ".download", ".tmp"}:
            continue
        st = entry.stat()
        if now - st.st_mtime < 120:            # still arriving
            continue
        items.append({"path": entry, "name": entry.name, "size": st.st_size, "mtime": st.st_mtime})
        if len(items) >= MAX_FILES:
            break
    with ThreadPoolExecutor(6) as pool:
        for item, snippet in zip(items, pool.map(lambda i: _snippet(i["path"]), items)):
            item["snippet"] = snippet
    # Exact duplicates: same size first, then the same bytes.
    by_size: dict[int, list[dict]] = {}
    for item in items:
        by_size.setdefault(item["size"], []).append(item)
    for group in by_size.values():
        if len(group) > 1 and group[0]["size"] > 0:
            seen: dict[str, dict] = {}
            copyish = re.compile(r"\(\d+\)|\bcopy\b| \d+$", re.I)
            # Keep the one with a real name ("report.pdf", not "report (2).pdf"), then the oldest.
            for item in sorted(group, key=lambda i: (bool(copyish.search(Path(i["name"]).stem)), i["mtime"])):
                key = _digest(item["path"])
                if key in seen:
                    item["duplicate_of"] = seen[key]["name"]
                else:
                    seen[key] = item
    return items


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def plan(folder_text: str, how: str = "") -> str:
    from mint.core import llm
    folder = _folder(folder_text)
    if folder is None:
        return f"FAILED: no folder '{folder_text}'."
    folder = folder.resolve()
    home = Path.home().resolve()
    if folder in (home, Path("/")) or str(folder).startswith(("/System", "/Library", "/Applications", "/usr",
                                                               "/private/var", "/private/etc", "/bin", "/sbin", "/opt")) \
            or folder == home / "Library" or home / "Library" in folder.parents:
        return "Refused: tidy a folder of documents (Downloads, Desktop, a project folder), not the home or a system folder."
    items = _scan(folder)
    if not items:
        return f"{folder.name} has no loose files to tidy."
    existing = [p.name for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")][:60]
    listing = "\n".join(
        f"{i}: {it['name']} | {_size(it['size'])} | {time.strftime('%Y-%m-%d', time.localtime(it['mtime']))}"
        + (f" | text: {it['snippet']}" if it.get("snippet") else "")
        for i, it in enumerate(items) if not it.get("duplicate_of"))
    prompt = (f"Organise the loose files of the folder '{folder.name}' into sub-folders. The user's wish: "
              f"{how or 'sort them sensibly by what they are'}.\n"
              f"Sub-folders that already exist (reuse them when they fit): {', '.join(existing) or 'none'}.\n"
              "Default groups when the user has no wish: Documents, PDFs by topic when clear (Invoices & Receipts, "
              "Statements, Tickets & Travel, Contracts), Images, Screenshots, Videos, Audio, Installers (.dmg .pkg), "
              "Archives (.zip), Code, Spreadsheets, Presentations. At most two levels ('Invoices/2026-09'). "
              "Suggest a new name ONLY when the current one says nothing ('document (3).pdf', 'download.pdf', "
              "'Untitled.png'), from the file's text; keep the extension; otherwise rename null. A file that should "
              "stay where it is: to = \"\".\n"
              'Return JSON: {"moves": [{"i": <number>, "to": "<sub-folder>", "rename": null}]}\n\nFILES:\n' + listing)
    answer = llm.ask_json(prompt)
    if not isinstance(answer, dict):
        return "FAILED: could not work out a plan (no answer from Gemini). Try again in a moment."
    moves = []
    for m in answer.get("moves") or []:
        try:
            item = items[int(m.get("i"))]
        except (TypeError, ValueError, IndexError):
            continue
        to = re.sub(r"[^\w &().,'/-]", "", str(m.get("to") or "")).strip("/ ").replace("..", "")
        rename = str(m.get("rename") or "").strip()
        if rename:
            rename = re.sub(r'[/:\\\\]', "-", rename)
            if Path(rename).suffix.lower() != item["path"].suffix.lower():
                rename += item["path"].suffix
            if rename == item["name"]:
                rename = ""                      # "renamed" to the same name: no rename
        if to or rename:
            moves.append({"from": str(item["path"]), "to": to, "rename": rename or None,
                          "mtime": item["mtime"]})
    dupes = [{"from": str(it["path"]), "to": "Duplicates", "rename": None, "mtime": it["mtime"],
              "duplicate_of": it["duplicate_of"]} for it in items if it.get("duplicate_of")]
    _plan.clear()
    _plan.update(folder=str(folder), moves=moves + dupes, made=time.time(), how=how)
    counts = Counter(m["to"] or "(stays, renamed)" for m in _plan["moves"])
    renames = [f"{Path(m['from']).name} → {m['rename']}" for m in moves if m["rename"]][:5]
    lines = ", ".join(f"{folder_name} {n}" for folder_name, n in counts.most_common(12))
    return (f"Plan for {folder} ({len(items)} files; nothing moved yet): {len(_plan['moves'])} to move - {lines}."
            + (f" Renames: {'; '.join(renames)}." if renames else "")
            + (f" {len(dupes)} exact duplicate(s) go to Duplicates (not deleted)." if dupes else "")
            + " Tell the user this in a sentence or two and ask whether to go ahead (tidy action=apply), or what "
              "to change (tidy action=plan again with their wish in `how`).")


def _unique(target: Path) -> Path:
    if not target.exists():
        return target
    for n in range(2, 1000):
        candidate = target.with_name(f"{target.stem} {n}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"too many files called {target.name}")


def apply() -> str:
    if not _plan.get("moves"):
        return "There is no tidy-up plan to apply. Make one first (tidy action=plan)."
    if time.time() - _plan["made"] > 3600:
        return "That plan is over an hour old; make a fresh one (tidy action=plan)."
    folder = Path(_plan["folder"])
    journal, skipped, failed = [], 0, []
    JOURNALS.mkdir(parents=True, exist_ok=True)
    path = JOURNALS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    try:
        for m in _plan["moves"]:
            source = Path(m["from"])
            try:
                if not source.exists() or abs(source.stat().st_mtime - m["mtime"]) > 1:
                    skipped += 1                 # gone or changed since the plan
                    continue
                target_dir = folder / m["to"] if m["to"] else source.parent
                target = _unique(target_dir / (m["rename"] or source.name))
                if folder.resolve() not in target.resolve().parents:
                    skipped += 1
                    continue
                target_dir.mkdir(parents=True, exist_ok=True)
                os.rename(source, target)
                journal.append({"from": str(source), "to": str(target)})
            except OSError as error:             # one file that cannot move does not stop the rest
                failed.append(f"{source.name} ({error.strerror or error})")
    finally:
        # Written whatever happened, so every move that was made can be undone.
        path.write_text(json.dumps({"folder": str(folder), "moves": journal}, indent=1, ensure_ascii=False))
        os.chmod(path, 0o600)
        _plan.clear()
    folders = Counter(Path(j["to"]).parent.name for j in journal)
    return (f"Tidied {folder.name}: moved {len(journal)} file(s) into "
            + ", ".join(f"{k} ({v})" for k, v in folders.most_common(8))
            + (f"; skipped {skipped} that changed since the plan" if skipped else "")
            + (f"; could not move {', '.join(failed[:5])}" if failed else "")
            + ". Nothing was deleted. \"Undo that\" puts everything back (tidy action=undo).")


def undo() -> str:
    journals = sorted(JOURNALS.glob("*.json")) if JOURNALS.exists() else []
    if not journals:
        return "There is no tidy-up to undo."
    last = journals[-1]
    data = json.loads(last.read_text())
    back, missing = 0, 0
    for move in reversed(data["moves"]):
        now, was = Path(move["to"]), Path(move["from"])
        if now.exists() and not was.exists():
            was.parent.mkdir(parents=True, exist_ok=True)
            os.rename(now, was)
            back += 1
        else:
            missing += 1
    for move in data["moves"]:                  # remove folders the tidy-up made and left empty
        parent = Path(move["to"]).parent
        try:
            if parent != Path(data["folder"]) and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
    last.rename(last.with_suffix(".undone"))
    return (f"Put {back} file(s) back where they were in {Path(data['folder']).name}"
            + (f"; {missing} had been moved or renamed since and were left alone" if missing else "") + ".")


PROMPT = """Tidying folders: "clean up / organise / sort my Downloads (Desktop, a folder)" -> tidy action=plan \
(with the user's wish in `how`), tell them the plan briefly and ASK before moving anything; "yes / go ahead" -> \
tidy action=apply; "undo that" -> tidy action=undo. It never deletes: duplicates go to a Duplicates folder."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="tidy",
        description=("Organise a folder's loose files into sub-folders (by kind, topic, project or month) with a "
                     "preview: plan (nothing moves), apply (after the user agrees), undo (put the last tidy-up "
                     "back). Never deletes; duplicates go to a Duplicates folder; unhelpful names can be improved."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["plan", "apply", "undo", "cancel"]),
            "folder": types.Schema(type=S, description="plan: which folder (default ~/Downloads)"),
            "how": types.Schema(type=S, description="plan: the user's wish, e.g. 'by project', 'invoices by month', "
                                                    "'leave the screenshots'")},
            required=["action"]))]


def tool(args: dict) -> str:
    action = str(args.get("action") or "plan").lower()
    if action == "apply":
        return apply()
    if action == "undo":
        return undo()
    if action == "cancel":
        _plan.clear()
        return "Dropped the plan; nothing was moved."
    return plan(str(args.get("folder") or ""), str(args.get("how") or ""))


HANDLERS = {"tidy": tool}
