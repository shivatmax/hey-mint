"""Who the sub-agents are: agents.json next to the package.

Each agent:
    {
      "name": "Astra",
      "color": "#8B7CFF",
      "role": "research",                  one line, shown to Mint when it picks an agent
      "provider": "openai",                every agent runs on GPT-6 Luna, straight from the OpenAI API
      "models": ["openai/gpt-6-luna"],     (set "allow_other_models": true to use others)
      "thinking": "none" | "low" | "medium",   the usual reasoning effort; Mint may pick per task
      "tools": ["web_search", "fetch_url", "write_file", ...],
      "instructions": "…",                 the agent's own system prompt
      "workspace": "~/Documents/Mint/agents/astra",   where its files go
      "max_steps": 24
    }

"runner": "codex" makes an agent a front for OpenAI Codex (the CLI inside the
ChatGPT app) instead of a model loop: Codex plans, writes and runs the code
itself, on GPT-6 Luna, in the agent's workspace.

Edit the file by hand, or ask Mint ("create an agent called Nova that…").
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

from mint.core import config

log = logging.getLogger("mint.agents")

PATH = config.PROJECT_ROOT / "agents.json"
WORK_ROOT = Path.home() / "Documents" / "Mint" / "agents"      # default; new agents use config.storage("Agents")

# The user's choice: every sub-agent runs on OpenAI's GPT-6 Luna, straight from the
# OpenAI API (OPENAI_API_KEY) - never OpenRouter. Its thinking is set per task.
ONLY_PROVIDER, ONLY_MODEL = "openai", "gpt-6-luna"
CODEX_MODEL = "gpt-6-luna"

GEMINI_FAST = ["gemini-3.5-flash-lite", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"]
GEMINI_DEEP = ["gemini-3.1-pro-preview", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]

DEFAULTS = [
    {
        "name": "Astra",
        "color": "#8B7CFF",
        "role": "Research: searches the web, reads sources, and writes a sourced brief.",
        "provider": ONLY_PROVIDER,
        "models": [ONLY_MODEL],
        "thinking": "medium",
        "tools": ["web_search", "fetch_url", "write_file", "read_file", "list_files", "ask_user",
                  "report_progress"],
        "instructions": (
            "You are Astra, a meticulous research agent. Plan briefly, search, open the best "
            "sources with fetch_url (and watch_video for a talk, demo or tutorial video), and cross-check facts. Save your findings with write_file "
            "(a Markdown brief with a Sources section with URLs). Never invent facts or sources; "
            "say what you could not verify. Keep the final answer short: what you found, and "
            "where it is saved."),
        "max_steps": 24,
    },
    {
        "name": "Luna",
        "color": "#2EC4B6",
        "role": "Builder: RL environments and code - plans, writes files, and revises on request.",
        "provider": ONLY_PROVIDER,
        "models": [ONLY_MODEL],
        "thinking": "medium",
        "tools": ["write_file", "read_file", "list_files", "web_search", "fetch_url", "ask_user",
                  "report_progress"],
        "instructions": (
            "You are Luna, a builder agent for reinforcement-learning environments and code. "
            "Follow the instructions you are given exactly; they come from Mint, who tracks what "
            "the user wants and may send you updated instructions mid-task - when one arrives, "
            "adapt the plan at once and say what changed. Work in your workspace: write complete, "
            "runnable files (no placeholders). If a requirement is ambiguous and matters, ask with "
            "ask_user instead of guessing. Finish with a short summary of the files and how to run them."),
        "max_steps": 30,
    },
    {
        "name": "Codex",
        "color": "#10A37F",
        "role": ("Coding in OpenAI Codex (GPT-6 Luna): builds websites, apps and scripts in a project "
                 "folder, runs and checks them, and takes follow-up changes."),
        "runner": "codex",
        "provider": "codex",
        "models": ["gpt-6-luna"],
        "thinking": "low",
        "tools": [],
        "instructions": "",
        "max_steps": 1,
        "builtin": True,
    },
    {
        "name": "Sage",
        "color": "#FFB547",
        "role": "Writer: long documents, reports and PDFs from notes or research.",
        "provider": ONLY_PROVIDER,
        "models": [ONLY_MODEL],
        "thinking": "low",
        "tools": ["write_file", "read_file", "list_files", "create_pdf", "ask_user", "report_progress"],
        "instructions": (
            "You are Sage, a writing agent. Produce well-structured, complete documents: clear "
            "headings, no filler. Save drafts with write_file (Markdown) and make a PDF with "
            "create_pdf when asked. Ask with ask_user only if the audience or purpose is unclear."),
        "max_steps": 20,
    },
]

_lock = threading.Lock()


def _normal(agent: dict) -> dict:
    agent = dict(agent)
    agent["name"] = str(agent.get("name", "Agent")).strip() or "Agent"
    agent.setdefault("color", "#7FD1FF")
    agent.setdefault("role", "")
    if agent.get("runner") == "codex":
        # Codex signs in with the user's ChatGPT account; OpenRouter ids do not apply.
        agent["provider"] = "codex"
        agent["models"] = [m.split("/", 1)[-1] for m in (agent.get("models") or [CODEX_MODEL])]
    elif not agent.get("allow_other_models"):
        # Only GPT-6 Luna from OpenAI, even for agents made before that choice (they said OpenRouter).
        agent["provider"], agent["models"] = ONLY_PROVIDER, [ONLY_MODEL]
        agent.pop("fallback", None)
    agent.setdefault("provider", ONLY_PROVIDER)
    agent.setdefault("models", [ONLY_MODEL])
    if agent.get("thinking") not in ("none", "low", "medium"):
        preset = next((d for d in DEFAULTS if d["name"].lower() == agent["name"].lower()), None)
        agent["thinking"] = preset["thinking"] if preset else "low"
    agent.setdefault("tools", ["web_search", "fetch_url", "write_file", "read_file", "ask_user",
                               "report_progress"])
    if "web_search" in agent["tools"] and "watch_video" not in agent["tools"]:
        # Any agent that reads the web can watch a video on it (saved agents too).
        agent["tools"] = [*agent["tools"], "watch_video"]
    agent.setdefault("instructions", f"You are {agent['name']}, a helpful agent.")
    agent.setdefault("max_steps", 24)
    slug = re.sub(r"[^a-z0-9]+", "-", agent["name"].lower()).strip("-") or "agent"
    agent.setdefault("workspace", str(config.storage("Agents") / slug))
    return agent


def load() -> list[dict]:
    with _lock:
        if not PATH.exists():
            _write(DEFAULTS)
        try:
            data = json.loads(PATH.read_text())
            agents = data.get("agents", data) if isinstance(data, dict) else data
            removed = {str(n).lower() for n in (data.get("removed", []) if isinstance(data, dict) else [])}
        except (OSError, json.JSONDecodeError) as error:
            log.warning("agents.json unreadable (%s); using the defaults", error)
            agents, removed = DEFAULTS, set()
    agents = [a for a in agents if isinstance(a, dict)]
    # Built-in agents added after agents.json was written (Codex) appear too,
    # unless the user removed them.
    have = {str(a.get("name", "")).lower() for a in agents}
    agents += [d for d in DEFAULTS if d.get("builtin") and d["name"].lower() not in have | removed]
    return [_normal(a) for a in agents]


def _write(agents: list[dict], removed: list[str] | None = None) -> None:
    data = {"agents": agents}
    if removed:
        data["removed"] = sorted(set(removed))
    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(PATH, 0o600)


def _removed() -> list[str]:
    try:
        data = json.loads(PATH.read_text())
        return list(data.get("removed", [])) if isinstance(data, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def get(name: str) -> dict | None:
    wanted = name.strip().lower()
    agents = load()
    exact = next((a for a in agents if a["name"].lower() == wanted), None)
    if exact:
        return exact
    return next((a for a in agents if wanted and wanted in a["name"].lower()), None)


def save(agent: dict) -> dict:
    agent = _normal(agent)
    agents = [a for a in load() if a["name"].lower() != agent["name"].lower()]
    agents.append({k: v for k, v in agent.items()})
    removed = [n for n in _removed() if n.lower() != agent["name"].lower()]
    with _lock:
        _write(agents, removed)
    return agent


def remove(name: str) -> bool:
    agents = load()
    kept = [a for a in agents if a["name"].lower() != name.strip().lower()]
    if len(kept) == len(agents):
        return False
    with _lock:
        _write(kept, _removed() + [name.strip().lower()])
    return True


def color_rgb(agent: dict) -> tuple:
    value = str(agent.get("color", "#7FD1FF")).lstrip("#")
    try:
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return (0.5, 0.82, 1.0)


PALETTE = ["#8B7CFF", "#2EC4B6", "#FFB547", "#FF6B9A", "#4DA3FF", "#7BE07B", "#FF8A4C", "#C77DFF"]


def next_color() -> str:
    used = {a["color"].upper() for a in load()}
    return next((c for c in PALETTE if c.upper() not in used), PALETTE[len(used) % len(PALETTE)])
