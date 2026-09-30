"""Agents working together: missions, the team board, and what agents remember.

A mission is everything that grows out of one task Mint hands out. The agent
Mint gave it to leads; it may hand well-scoped pieces to teammates (ask_agent,
or ask_agents for several in parallel) - Sage writing a report asks Astra for
the research, Luna asks Codex to build the page. Helpers may recruit helpers of
their own, within limits. Only the lead reports back to Mint.

Built on the supervisor pattern with a shared blackboard, the common advice for
agent teams (and what Anthropic's research system does: a lead that writes
detailed briefs, 1-5 subagents in parallel, effort scaled to the question):

* Folder. The lead's workspace is the mission root; each helper works in
  helpers/<name>/ below it and can read everything in the mission - so a
  helper's files are one read_file away, and nobody overwrites anybody.
* Board. share_note puts a finding on the mission's board (board.md, append
  only - no write conflicts) and tells the teammates working right now;
  read_board shows it all. A living record the lead writes its answer from.
* Limits. At most MAX_DEPTH levels below the lead, TEAM_CAP agents at once, no
  asking an agent that is already up your own chain (no loops), and a helper
  that takes longer than HELPER_TIMEOUT is stopped. Stopping the lead stops
  its helpers.
* Memory. Every finished run is logged (experience.jsonl); a new run gets the
  few most relevant past results of the whole team, plus what Mint knows about
  the user - so the team builds on earlier work instead of starting cold.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
import re
import threading
import time
from pathlib import Path

from mint.core import config

log = logging.getLogger("mint.agents")

MAX_DEPTH = 2              # lead = 0; its helpers 1; theirs 2 (no further)
TEAM_CAP = 6               # agents working at once in one mission
HELPER_TIMEOUT = 20 * 60   # seconds a helper may take
EXPERIENCE = config.PROJECT_ROOT / "agents-experience.jsonl"
_ids = itertools.count(1)
_lock = threading.Lock()

TEAM_TOOLS = ("ask_agent", "ask_agents", "share_note", "read_board", "list_team")

SCHEMAS = {
    "ask_agent": {
        "description": (
            "Hand a well-scoped piece of your task to a teammate and wait for its result (e.g. Sage asks "
            "Astra for the research, Luna asks Codex to build a page). Only when their specialty clearly "
            "helps; do small things yourself. Write a complete brief: objective, what to return and in "
            "what format, sources/constraints, and what NOT to do (so work is not duplicated)."),
        "parameters": {"type": "object", "properties": {
            "agent": {"type": "string", "description": "Teammate's name (see list_team)."},
            "task": {"type": "string", "description": "The full brief."},
            "thinking": {"type": "string", "enum": ["none", "low", "medium"],
                         "description": "How hard it should think. Default: chosen from the task."}},
            "required": ["agent", "task"]}},
    "ask_agents": {
        "description": ("Hand several independent pieces to teammates at once - they work in parallel and you "
                        "get all results together (faster than one by one). Same rules as ask_agent; give each "
                        "a distinct part so they do not overlap."),
        "parameters": {"type": "object", "properties": {
            "jobs": {"type": "array", "items": {"type": "object", "properties": {
                "agent": {"type": "string"}, "task": {"type": "string"},
                "thinking": {"type": "string", "enum": ["none", "low", "medium"]}},
                "required": ["agent", "task"]}}},
            "required": ["jobs"]}},
    "share_note": {
        "description": ("Put a key finding, decision or file on the team board so teammates can use it (they "
                        "get it at their next step). Short and factual; include sources or file paths."),
        "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
    "read_board": {
        "description": "Read the team board: what every agent in this mission has shared so far.",
        "parameters": {"type": "object", "properties": {}}},
    "list_team": {
        "description": "Your teammates: who is good at what, and who is working on this mission right now.",
        "parameters": {"type": "object", "properties": {}}},
}


class Mission:
    def __init__(self, root: Path, lead: str, allowed: list[str] | None = None) -> None:
        self.id = f"m{next(_ids)}"
        self.root = root
        self.lead = lead
        # None = every agent may help; [] = no helpers; else these names only.
        self.allowed = [a.lower() for a in allowed] if allowed is not None else None
        self.board: list[tuple[str, str, float]] = []
        self.runs: list = []

    def active(self) -> list:
        return [r for r in self.runs if r.active]

    def may_use(self, name: str) -> bool:
        return self.allowed is None or name.lower() in self.allowed

    def note(self, agent: str, text: str) -> None:
        self.board.append((agent, text.strip(), time.time()))
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.root / "board.md", "a") as f:
                f.write(f"- **{agent}** ({dt.datetime.now():%H:%M}): {text.strip()}\n")
        except OSError:
            pass

    def board_text(self, limit: int = 12000) -> str:
        if not self.board:
            return "The team board is empty."
        lines = [f"- {a}: {t}" for a, t, _ in self.board]
        text = "\n".join(lines)
        return text[-limit:]


def helper_folder(mission: Mission, agent: dict, run_id: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", agent["name"].lower()).strip("-") or "agent"
    folder = mission.root / "helpers" / slug
    if any(r.active and Path(r.agent["workspace"]) == folder for r in mission.runs):
        folder = mission.root / "helpers" / f"{slug}-{run_id.rsplit('-', 1)[-1]}"
    return folder


def chain(run) -> list[str]:
    names, node = [], run
    while node is not None:
        names.append(node.name.lower())
        node = getattr(node, "parent", None)
    return names


def roster_text(agents: list[dict], mission: Mission | None, me: str) -> str:
    lines = []
    for a in agents:
        if a["name"].lower() == me.lower():
            continue
        if mission is not None and not mission.may_use(a["name"]):
            continue
        busy = [r for r in (mission.active() if mission else []) if r.name == a["name"]]
        lines.append(f"- {a['name']}: {a['role']}" + (" (working on this mission now)" if busy else ""))
    return "\n".join(lines) or "(no teammates available)"


def team_prompt(run, agents: list[dict]) -> str:
    mission = run.mission
    if mission is None:
        return ""
    can_delegate = run.depth < MAX_DEPTH and mission.allowed != []
    roster = roster_text(agents, mission, run.name)
    lead = "You lead this mission." if run.parent is None else (
        f"You are helping {run.parent.name}, who gave you this piece; your final answer goes back to it.")
    text = (f"\n\nTEAM. {lead} Mission folder: {mission.root} (everyone can read it; you write in your own "
            "workspace; helpers' files are under helpers/<name>/ - read them with read_file).")
    if can_delegate:
        text += (f"\nTeammates you can call:\n{roster}\n"
                 "Use a teammate only when their specialty clearly helps (research -> the researcher, building "
                 "code or a site -> the builder/Codex, a long document -> the writer); do the rest yourself. "
                 "Scale it to the task: usually none or one helper; 2-4 in parallel (ask_agents) for "
                 "independent parts. Give each a complete brief (objective, what to return, boundaries). "
                 "Never re-delegate what a helper returned - use it and finish.")
    else:
        text += "\nYou cannot call further teammates; do the work yourself."
    text += ("\nShare key findings with share_note (sources, numbers, file paths); read_board to see what "
             "others found. Your final answer should say what you did and, if teammates helped, what each "
             "contributed.")
    return text


# --- memory ----------------------------------------------------------------------------

def remember(run, status: str, result: str) -> None:
    if status != "done":
        return
    entry = {"at": dt.datetime.now().isoformat(timespec="minutes"), "agent": run.name,
             "task": run.task[:400], "result": result[:900],
             "files": [str(Path(run.agent["workspace"]) / f) for f in run.files[-8:]],
             "lead": run.parent.name if run.parent else None}
    try:
        with _lock, open(EXPERIENCE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        log.debug("could not log experience", exc_info=True)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", text.lower())}


def recall(task: str, limit: int = 3) -> str:
    """The team's most relevant earlier results, and what Mint knows about the user."""
    parts = []
    try:
        lines = EXPERIENCE.read_text().splitlines()[-400:]
        wanted = _words(task)
        scored = []
        for i, line in enumerate(lines):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            overlap = len(wanted & _words(e.get("task", "") + " " + e.get("result", "")))
            if overlap >= 2:
                scored.append((overlap + i / max(1, len(lines)), e))    # recency breaks ties
        for _, e in sorted(scored, key=lambda s: -s[0])[:limit]:
            files = f" Files: {', '.join(e['files'][:4])}." if e.get("files") else ""
            parts.append(f"- {e['at']} {e['agent']} did \"{e['task'][:160]}\": {e['result'][:400]}{files}")
    except OSError:
        pass
    text = ""
    if parts:
        text += ("\n\nEarlier work by the team that may help (may be outdated - check before relying on it):\n"
                 + "\n".join(parts))
    try:
        from mint.knowledge import memory as membank
        facts = [b.get("text", "") for b in membank.relevant(task, limit=4)]
        facts = [f for f in facts if f]
        if facts:
            text += "\n\nWhat Mint knows about the user that may matter:\n" + "\n".join(f"- {f}" for f in facts)
    except Exception:
        log.debug("membank recall failed", exc_info=True)
    return text


def trim(messages: list[dict], budget: int = 120_000, keep: int = 6) -> None:
    """Keep a long run inside its context: shorten old tool results once the
    conversation passes `budget` characters (the newest `keep` stay whole)."""
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= budget:
        return
    tools = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tools[:-keep]:
        content = str(messages[i].get("content") or "")
        if len(content) > 1200:
            messages[i]["content"] = content[:1000] + f"\n…(older result shortened; {len(content)} chars)"
            total -= len(content) - 1100
            # Claude rejects thinking blocks once the history before them was edited ("preserved
            # thinking"): the Claude turns after this result go back in the plain form from now on.
            for later in messages[i + 1:]:
                later.pop("_anthropic", None)
            if total <= budget:
                break
