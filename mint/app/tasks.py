"""Tasks: multi-step work that is tracked, can grow sub-steps, and survives restarts.

plan_task starts one: a goal and its ordered steps. While it runs:

* step_done marks a step (or a sub-step like "3.1") done or failed, with what was
  checked. A step whose sub-steps are all finished finishes by itself.
* task add_steps adds steps - at the end, after a step, or as sub-steps under one
  ("3" becomes 3.1, 3.2...) when a step turns out to be bigger than planned.
* task replan replaces the steps not done yet when the plan stops fitting.
* task note keeps a finding for later steps (a file path, a name, a number).

Everything is saved in tasks.json, so a task is still there after Mint unloads,
restarts or the user says stop: "stop" pauses the task, and "continue" /
"where were we" resumes it (task resume). Unfinished tasks are listed in every
new session's instructions. The idea of keeping the plan in a file and reciting
the next step after every action is from Manus's and Anthropic's notes on
long-running agents.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid

from mint.core import config

log = logging.getLogger("mint.app.tasks")

STORE = config.PROJECT_ROOT / "tasks.json"
KEEP_FINISHED = 20
SHOW_FOR = 3 * 86400          # unfinished tasks older than this are not brought up any more
_lock = threading.RLock()


# --- Storage ------------------------------------------------------------------------------

def _load() -> list[dict]:
    try:
        rows = json.loads(STORE.read_text())
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _save(rows: list[dict]) -> None:
    open_rows = [r for r in rows if r["state"] in ("active", "paused")]
    closed = sorted((r for r in rows if r["state"] not in ("active", "paused")), key=lambda r: r["updated"])
    rows = open_rows + closed[-KEEP_FINISHED:]
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(STORE)


def save(task: dict) -> None:
    with _lock:
        task["updated"] = time.time()
        rows = [r for r in _load() if r["id"] != task["id"]]
        rows.append(task)
        _save(rows)


# --- Steps --------------------------------------------------------------------------------

def _step(n: str, text: str) -> dict:
    return {"n": n, "text": str(text).strip(), "status": "todo", "result": ""}


def _children(task: dict, n: str) -> list[dict]:
    return [s for s in task["steps"] if s["n"].startswith(n + ".") and s["n"].count(".") == n.count(".") + 1]


def _key(n: str) -> tuple:
    return tuple(int(p) for p in n.split("."))


def _sort(task: dict) -> None:
    task["steps"].sort(key=lambda s: _key(s["n"]))


def leaves(task: dict) -> list[dict]:
    """The steps that are actually done one by one (a step with sub-steps is done through them)."""
    return [s for s in task["steps"] if not _children(task, s["n"])]


def remaining(task: dict) -> list[dict]:
    return [s for s in leaves(task) if s["status"] == "todo"]


def current(task: dict) -> dict | None:
    rest = remaining(task)
    return rest[0] if rest else None


def progress(task: dict) -> tuple[int, int]:
    all_leaves = leaves(task)
    return sum(1 for s in all_leaves if s["status"] != "todo"), len(all_leaves)


def _roll_up(task: dict) -> None:
    """A step whose sub-steps are all finished is finished (failed if any failed)."""
    for s in sorted(task["steps"], key=lambda s: -s["n"].count(".")):
        kids = _children(task, s["n"])
        if kids and all(k["status"] != "todo" for k in kids):
            s["status"] = "failed" if any(k["status"] == "failed" for k in kids) else "done"
            s["result"] = s["result"] or "; ".join(k["result"] for k in kids if k["result"])[:200]
        elif kids:
            s["status"] = "todo"


def label(step: dict) -> str:
    return f"{step['n']}. {step['text']}"


def outline(task: dict, results: bool = True) -> str:
    marks = {"todo": "[ ]", "done": "[x]", "failed": "[!]", "skipped": "[-]"}
    lines = []
    for s in task["steps"]:
        indent = "  " * s["n"].count(".")
        tail = f" - {s['result']}" if results and s["result"] else ""
        lines.append(f"{indent}{marks.get(s['status'], '[ ]')} {label(s)}{tail}")
    return "\n".join(lines)


# --- Starting and changing ----------------------------------------------------------------

def start(goal: str, steps: list[str]) -> tuple[dict, dict | None]:
    """A new active task. -> (task, the task it paused or None)."""
    paused = None
    with _lock:
        for r in _load():
            if r["state"] == "active":
                if remaining(r):
                    r["state"] = "paused"
                    paused = r
                else:
                    r["state"] = "done"
                save(r)
    task = {"id": uuid.uuid4().hex[:8], "goal": goal.strip(), "state": "active", "created": time.time(),
            "updated": time.time(), "steps": [_step(str(i), s) for i, s in enumerate(steps, 1) if str(s).strip()],
            "notes": []}
    save(task)
    return task, paused


def mark(task: dict, n: str, result: str, failed: bool = False) -> str:
    n = str(n).strip().rstrip(".")
    step = next((s for s in task["steps"] if s["n"] == n), None)
    if step is None:
        return f"There is no step {n}. Steps:\n{outline(task, results=False)}"
    kids = _children(task, n)
    if kids:
        todo = [k for k in kids if k["status"] == "todo"]
        if todo:
            # Marking a parent done while its sub-steps are open: they were covered by it.
            for k in todo:
                k["status"], k["result"] = ("failed" if failed else "skipped"), "covered by step " + n
    step["status"] = "failed" if failed else "done"
    step["result"] = result.strip()[:300]
    _roll_up(task)
    if not remaining(task):
        task["state"] = "done"
    save(task)
    return ""


def add_steps(task: dict, steps: list[str], under: str = "", after: str = "") -> str:
    steps = [str(s).strip() for s in steps if str(s).strip()]
    if not steps:
        return "FAILED: no steps given."
    if under:
        parent = next((s for s in task["steps"] if s["n"] == str(under).strip()), None)
        if parent is None:
            return f"FAILED: no step {under}."
        kids = _children(task, parent["n"])
        start_at = max((_key(k["n"])[-1] for k in kids), default=0) + 1
        for i, text in enumerate(steps):
            task["steps"].append(_step(f"{parent['n']}.{start_at + i}", text))
        parent["status"] = "todo"
        where = f"as sub-steps of step {parent['n']}"
    else:
        # Top level: renumber the steps after `after` (and their sub-steps) to make room.
        tops = [s for s in task["steps"] if "." not in s["n"]]
        pos = int(str(after).split(".")[0]) if after else (max((int(s["n"]) for s in tops), default=0))
        shift = len(steps)
        for s in task["steps"]:
            parts = s["n"].split(".")
            if int(parts[0]) > pos:
                parts[0] = str(int(parts[0]) + shift)
                s["n"] = ".".join(parts)
        for i, text in enumerate(steps, 1):
            task["steps"].append(_step(str(pos + i), text))
        where = f"after step {pos}" if after else "at the end"
    _sort(task)
    task["state"] = "active"
    save(task)
    return f"Added {len(steps)} step(s) {where}."


def replan(task: dict, steps: list[str]) -> str:
    """Replace every step not started yet; finished ones stay as they are."""
    kept = [s for s in task["steps"] if s["status"] != "todo" or any(k["status"] != "todo"
                                                                      for k in _children(task, s["n"]))]
    kept_tops = [int(s["n"].split(".")[0]) for s in kept]
    base = max(kept_tops, default=0)
    dropped = {s["n"] for s in task["steps"]} - {s["n"] for s in kept}
    task["steps"] = kept + [_step(str(base + i), t) for i, t in enumerate(steps, 1) if str(t).strip()]
    _roll_up(task)
    for s in kept:
        # A half-done step whose other sub-steps were replanned is closed, and says so.
        if any(d.startswith(s["n"] + ".") for d in dropped) and "rest replanned" not in s["result"]:
            s["result"] = (s["result"] + " (rest replanned)").strip()
    _sort(task)
    save(task)
    return f"New plan: {len(steps)} step(s) from step {base + 1}."


def note(task: dict, text: str) -> str:
    from mint.knowledge.skills import has_secret
    if has_secret(text):
        return "Refused: notes are shown in later sessions, so they never hold passwords, keys or card numbers."
    task.setdefault("notes", []).append({"at": time.time(), "text": text.strip()[:400]})
    task["notes"] = task["notes"][-30:]
    save(task)
    return "Noted."


# --- Open tasks ---------------------------------------------------------------------------

def open_tasks() -> list[dict]:
    now = time.time()
    return sorted((r for r in _load() if r["state"] in ("active", "paused") and now - r["updated"] < SHOW_FOR),
                  key=lambda r: -r["updated"])


def find(what: str = "") -> dict | None:
    rows = open_tasks()
    if not rows:
        return None
    what = str(what or "").strip().lower()
    if not what:
        return rows[0]
    for r in rows:
        if r["id"] == what or what in r["goal"].lower():
            return r
    words = set(re.findall(r"\w+", what))
    scored = sorted(rows, key=lambda r: -len(words & set(re.findall(r"\w+", r["goal"].lower()))))
    return scored[0] if words & set(re.findall(r"\w+", scored[0]["goal"].lower())) else None


def pause(task: dict, why: str = "") -> None:
    if task["state"] == "active" and remaining(task):
        task["state"] = "paused"
        if why:
            task.setdefault("notes", []).append({"at": time.time(), "text": f"paused: {why}"})
        save(task)


def _ago(seconds: float) -> str:
    seconds = max(0, seconds)
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 86400:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / 86400:.0f} days ago"


def brief(task: dict) -> str:
    done, total = progress(task)
    step = current(task)
    nxt = f"; next: {label(step)}" if step else ""
    return f"'{task['goal']}' - {done}/{total} steps done{nxt} ({task['state']}, {_ago(time.time() - task['updated'])})"


def resume_text(task: dict) -> str:
    notes = "\n".join(f"- {n['text']}" for n in task.get("notes", [])[-8:])
    step = current(task)
    return (f"Resumed the task '{task['goal']}':\n{outline(task)}"
            + (f"\nNotes so far:\n{notes}" if notes else "")
            + (f"\nCheck where things stand (the window, the files) before redoing anything, then do step "
               f"{label(step)}. Call step_done after each step." if step else "\nAll steps are finished."))


def prompt_text() -> str:
    rows = open_tasks()
    if not rows:
        return ""
    lines = "\n".join(f"- {brief(r)}" for r in rows[:4])
    return ("Unfinished tasks (saved; they survive restarts). If the user says continue, resume, keep going or "
            "'where were we', call task action=resume (with words from the goal if there are several); "
            f"otherwise do not bring them up unless relevant:\n{lines}")


PROMPT = """Long tasks: before step_done, check the step really happened (window, files, look) and say what you saw in \
`result`; if not, failed=true and try another way. A step bigger than planned: task action=add_steps \
under=<step>; plan no longer fits: task action=replan; facts later steps need: task action=note. A stopped \
task is paused: "continue" resumes it."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="task",
        description=("Manage the current multi-step task (started with plan_task): add_steps (new steps at the end, "
                     "after a step, or as sub-steps `under` a step that turned out bigger), replan (replace the steps "
                     "not done yet), note (keep a finding for later steps), pause, resume (a paused or unfinished "
                     "task - after a restart, a stop, or 'where were we'), list (unfinished tasks), abandon."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["add_steps", "replan", "note", "pause", "resume", "list",
                                                 "abandon"]),
            "steps": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=S),
                                  description="add_steps / replan: short imperative steps in order"),
            "under": types.Schema(type=S, description="add_steps: make them sub-steps of this step, e.g. '3'"),
            "after": types.Schema(type=S, description="add_steps: insert after this top-level step"),
            "text": types.Schema(type=S, description="note: the finding; pause: why"),
            "which": types.Schema(type=S, description="resume/abandon: words from the task's goal (default the "
                                                      "latest)")},
            required=["action"]))]
