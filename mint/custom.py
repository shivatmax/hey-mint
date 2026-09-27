"""Personal customisation: custom.json, next to the mint package.

Edits apply without restarting: the file is re-read whenever it changes. The
session prompt (who you are, your accounts, your routines) is rebuilt on each
reconnect; aliases and routines are looked up live on every call.

    {
      "about_me": "I'm Alex, a developer at Acme. Keep answers short.",
      "accounts":        {"work": "alex@example.com"},
      "slack_workspaces": {"work": "Acme"},
      "aliases":         {"on call": "on-call"},
      "chrome_profiles": {"alex@example.com": "Profile 2"},
      "routines": {
        "start work": {
          "description": "Work Gmail and the on-call channel",
          "steps": [
            {"tool": "open_chrome", "account": "work", "url": "mail.google.com"},
            {"tool": "open_slack", "workspace": "work", "channel": "on-call"}
          ]
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from . import config

log = logging.getLogger("mint.custom")

PATH = config.PROJECT_ROOT / "custom.json"
_cache: dict = {}
_mtime: float = -1.0


def get() -> dict:
    """The current custom.json, re-read if it has changed. {} if absent or invalid."""
    global _cache, _mtime
    try:
        mtime = PATH.stat().st_mtime
    except FileNotFoundError:
        _cache, _mtime = {}, -1.0
        return _cache
    if mtime != _mtime:
        try:
            _cache = json.loads(PATH.read_text())
        except json.JSONDecodeError as error:
            # Keep the last good version rather than silently dropping everything.
            print(f"  [custom.json is not valid JSON, keeping the previous version: {error}]",
                  file=sys.stderr, flush=True)
        _mtime = mtime
    return _cache


def alias(text: str) -> str:
    """The user's own word for something, translated. Unchanged if not an alias."""
    wanted = " ".join(text.lower().split())
    for key, value in get().get("aliases", {}).items():
        if " ".join(key.lower().split()) == wanted:
            return value
    return text


def routines() -> dict[str, dict]:
    return {name: (spec if isinstance(spec, dict) else {"steps": spec})
            for name, spec in get().get("routines", {}).items()}


def prompt_addendum() -> str:
    """What Gemini should know about this particular user."""
    from . import prefs
    settings = get()
    parts = []
    if user := str(prefs.get("user_name") or "").strip():
        parts.append(f"The user's name is {user}; use it now and then, naturally.")
    for about in (str(prefs.get("about_me") or "").strip(), settings.get("about_me")):
        if about:
            parts.append(f"About the user: {about}")
    if accounts := settings.get("accounts"):
        parts.append("The user's accounts, by the names they use: " +
                     "; ".join(f"'{k}' = {v}" for k, v in accounts.items()) +
                     ". Pass the name or the address to open_chrome as `account`.")
    if workspaces := settings.get("slack_workspaces"):
        parts.append("Slack workspaces by the user's names: " +
                     "; ".join(f"'{k}' = {v}" for k, v in workspaces.items()) + ".")
    if aliases := settings.get("aliases"):
        parts.append("The user's own words: " +
                     "; ".join(f"'{k}' means {v}" for k, v in aliases.items()) + ".")
    if found := routines():
        parts.append("Routines (run with run_routine when the user asks for one by name or "
                     "clearly means it): " +
                     "; ".join(f"'{n}'" + (f" - {s['description']}" if s.get("description") else "")
                               for n, s in found.items()) + ".")
    if extra := settings.get("instructions"):
        parts.append(f"The user's standing instructions: {extra}")
    try:
        from . import extra_tools
        parts.append(extra_tools.prompt_text())      # skills, memories, reachable apps
    except Exception as error:
        print(f"  [skills/memories unavailable: {error}]", file=sys.stderr, flush=True)
    try:
        from .agents import orchestrator
        parts.append(orchestrator.prompt_text())
    except Exception:
        pass
    return "\n".join(parts)
