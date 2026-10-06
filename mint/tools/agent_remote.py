"""Talk to a running Claude Code or Codex session from anywhere: Telegram, the notch, or by voice.

    send(session_key, text)      type `text` into that session and press Return, the way you would

Where the session lives decides how:

    a Terminal tab / an iTerm2 session  AppleScript to that exact tab (found by its tty: from the hook, or the
                                        agent's own process by its folder). No focus change, works while you
                                        are away; the screen may even be locked.
    the Claude app (Code tab)           agentapps: opens that session by its name, pastes, checks what landed,
                                        presses Send (the Mac must be awake and unlocked)
    the Codex app (ChatGPT.app)         the same, in its Codex view, by the thread's name
    anything else (VS Code's terminal,  not typed into: Mint says so (no blind keystrokes into a window it
    Ghostty, Warp...)                   can't check)

Only the user decides what is typed: a reply to an alert on Telegram (the paired account only), the claude_mode
tool when the user's own request asked to send something, or the notch. Never text that came from a session.
Permission requests are answered with agent_hooks.decide (buttons), never by typing.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

from mint.tools import agent_watch

log = logging.getLogger("mint.tools.agent_remote")

TERMINAL, ITERM = "com.apple.Terminal", "com.googlecode.iterm2"
CLAUDE_APP, CODEX_APP = "com.anthropic.claudefordesktop", "com.openai.codex"
MAX_TEXT = 4000


def _as_string(text: str) -> str:
    """An AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript(script: str, timeout: float = 8.0) -> tuple[bool, str]:
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except Exception as error:
        return False, str(error)
    return out.returncode == 0 and "ok" in out.stdout, (out.stderr or out.stdout).strip()


def terminal_script(tty: str, text: str) -> str:
    # `do script ... in <tab>` types the text into whatever runs in that tab, then Return.
    return (f'tell application "Terminal"\n repeat with w in windows\n repeat with t in tabs of w\n'
            f' if tty of t is {_as_string(tty)} then\n do script {_as_string(text)} in t\n return "ok"\n end if\n'
            f' end repeat\n end repeat\n return "none"\nend tell')


def iterm_script(tty: str, text: str) -> str:
    return (f'tell application "iTerm2"\n repeat with w in windows\n repeat with t in tabs of w\n'
            f' repeat with s in sessions of t\n if tty of s is {_as_string(tty)} then\n'
            f' tell s to write text {_as_string(text)}\n return "ok"\n end if\n end repeat\n end repeat\n'
            f' end repeat\n return "none"\nend tell')


def _terminal_of(s) -> dict:
    """{tty, term_program} for a terminal session: what the hook said, else the agent's process in its folder."""
    term = dict(s.term or {})
    if term.get("tty"):
        return term
    name = "claude" if s.app == "claude" else "codex"
    try:
        rows = subprocess.run(["ps", "-axo", "pid=,tty=,comm="], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return term
    for line in rows.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[1] in ("??", "-") or os.path.basename(parts[2]) != name:
            continue
        try:
            cwd = subprocess.run(["lsof", "-a", "-p", parts[0], "-d", "cwd", "-Fn"], capture_output=True,
                                 text=True, timeout=2).stdout
        except Exception:
            continue
        if s.cwd and any(x[1:] == s.cwd for x in cwd.splitlines() if x.startswith("n")):
            from mint.ui.notch_agents import _term_of
            term.update(tty="/dev/" + parts[1], term_program=_term_of(int(parts[0])))
            return term
    return term


def route(s) -> str:
    """How text would reach this session: terminal / iterm / claude-app / codex-app / '' (it can't)."""
    where = (s.where or "").lower()
    if s.app == "claude" and where == "claude-desktop":
        return "claude-app"
    if s.app == "codex" and "desktop" in where:
        return "codex-app"
    term = _terminal_of(s)
    program = (term.get("term_program") or "").lower()
    bundle = term.get("bundle_id") or ""
    if term.get("tty") and (program == "apple_terminal" or bundle == TERMINAL):
        return "terminal"
    if term.get("tty") and (program == "iterm.app" or bundle == ITERM):
        return "iterm"
    return ""


def where_words(s) -> str:
    return {"terminal": "its Terminal tab", "iterm": "its iTerm tab", "claude-app": "the Claude app",
            "codex-app": "the Codex app"}.get(route(s), "")


def find(name: str):
    """A session by a word of its project, title or app ("korus", "Claude", "Codex"); the most pressing one when
    the name is empty."""
    items = agent_watch.sessions()
    if not name:
        return items[0] if items else None
    want = name.lower().strip()
    for s in items:
        if want in (s.project.lower(), (s.title or "").lower()):
            return s
    for s in items:
        if want in s.project.lower() or want in (s.title or "").lower() or want in s.app_name.lower():
            return s
    return None


def send(session_key: str, text: str) -> str:
    """Type `text` into the session and press Return. Returns what happened, in words."""
    text = (text or "").strip()
    if not text:
        return "Nothing to send."
    if len(text) > MAX_TEXT:
        return f"That's too long to type into a session ({len(text)} characters; at most {MAX_TEXT})."
    try:
        from mint.knowledge.skills import has_secret
        if has_secret(text):
            return "Not sent: it looks like it holds a password, key or card number."
    except Exception:
        pass
    if session_key.startswith("mint:"):
        # One of Mint's own background jobs: the words go to it as new instructions (or the answer).
        from mint.agents.runtime import hub
        run = hub.find(session_key.split(":", 1)[1])
        if run is None:
            return "That job is gone."
        return hub.reply(run.id, text) if run.status == "asking" else hub.steer(run.id, text)
    s = agent_watch.get(session_key)
    if s is None:
        return "That session is gone."
    how = route(s)
    name = f"{s.app_name} in {s.project}"
    if how in ("terminal", "iterm"):
        tty = _terminal_of(s).get("tty", "")
        text = text.replace("\r", " ").replace("\n", " ")       # one line: Return sends it
        script = terminal_script(tty, text) if how == "terminal" else iterm_script(tty, text)
        ok, why = _osascript(script)
        if ok:
            print(f"  [agents: typed into {name} ({how})]", flush=True)
            return f"Sent to {name} in {where_words(s)}."
        return f"Couldn't type into {name}'s {how} tab: {why[:160] or 'the tab is gone'}."
    if how in ("claude-app", "codex-app"):
        from mint.tools import agentapps
        key = "claude" if how == "claude-app" else "chatgpt"
        chat = s.title or ""
        said = agentapps.ask(key, text, chat=chat, wait=False, view="code" if key == "claude" else "codex",
                             confirmed=True)
        print(f"  [agents: {said[:120]}]", flush=True)
        return said
    host = (s.term or {}).get("term_program") or s.where or "its app"
    return (f"I can't type into {name} yet: it runs in {host}, which Mint can't address safely. Claude Code and "
            f"Codex in Terminal, iTerm, the Claude app and the Codex app work.")


def describe(s) -> str:
    """One line for a session (alerts, /agents)."""
    step = s.current
    state = "waiting for your OK" if s.approval else {
        "thinking": "thinking", "working": f"{step.verb} {step.target}".strip() if step else "working",
        "asking": "asking you", "done": "done", "failed": "failed", "idle": "idle"}.get(s.state, s.state)
    return f"{s.app_name} · {s.project}" + (f" ({s.title})" if s.title and s.title != s.project else "") + \
        f" - {state}"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
