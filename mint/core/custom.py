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

from mint.core import config

log = logging.getLogger("mint.core.custom")

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


PERSONALITY_LIMIT = 100


def prompt_addendum() -> str:
    """What Gemini should know about this particular user."""
    from mint.core import prefs
    settings = get()
    parts = []
    if user := str(prefs.get("user_name") or "").strip():
        parts.append(f"The user's name is {user}; use it now and then, naturally.")
    if personality := " ".join(str(prefs.get("personality") or "").split())[:PERSONALITY_LIMIT]:
        # Tone and nature only: every rule above (tools, safety, short replies,
        # language, goodbyes) still applies.
        parts.append(f"Your personality, chosen by the user: {personality}. Let it show in how you speak and "
                     "react - wording, humour, attitude - while you still do what is asked, follow every rule "
                     "above, and keep replies as short as before.")
    from mint.tools.diet import clip
    for about in (str(prefs.get("about_me") or "").strip(), settings.get("about_me")):
        if about:
            parts.append(f"About the user: {clip(str(about), 400, 'recall has more')}")
    # Each list is capped: the tools resolve the user's names themselves (jev.resolve), so the prompt only needs
    # the gist (9 Oct: the fixed prompt stays small however much is set up).
    if accounts := settings.get("accounts"):
        parts.append(clip("The user's accounts, by the names they use: " +
                          "; ".join(f"'{k}' = {v}" for k, v in accounts.items()) +
                          ". Pass the name or the address to open_chrome as `account`.", 300))
    if workspaces := settings.get("slack_workspaces"):
        parts.append(clip("Slack workspaces by the user's names: " +
                          "; ".join(f"'{k}' = {v}" for k, v in workspaces.items()) + ".", 250))
    if aliases := settings.get("aliases"):
        parts.append(clip("The user's own words: " +
                          "; ".join(f"'{k}' means {v}" for k, v in aliases.items()) + ".", 250))
    if found := routines():
        parts.append(clip("Routines (use_tool run_routine when the user asks for one by name or clearly means "
                          "it): " + "; ".join(f"'{n}'" + (f" - {s['description']}" if s.get("description") else "")
                                             for n, s in found.items()) + ".", 300))
    if extra := settings.get("instructions"):
        parts.append(f"The user's standing instructions: {clip(str(extra), 500)}")
    try:
        from mint.tools import extra as extra_tools
        parts.append(extra_tools.prompt_text())      # skills, memories, reachable apps
    except Exception as error:
        print(f"  [skills/memories unavailable: {error}]", file=sys.stderr, flush=True)
    try:
        from mint.tools import diet as tool_diet
        from mint.agents import orchestrator
        if not tool_diet.enabled():                  # with the diet on, find_tools serves the roster and rules
            parts += [orchestrator.prompt_text(), orchestrator.PROMPT]
    except Exception:
        pass
    return "\n".join(parts)
