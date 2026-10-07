"""Mint for other AI tools: a small read-only MCP server about what coding agents really did.

Claude Code, Codex, Cursor or any MCP client can ask it, before an agent says "done", for the evidence Mint reads
from the agents' own logs (agent_watch + agent_tests): how the tests stood after the last change, what changed,
risky steps, a recap, today's work, usage limits, a hand-off note. Nothing here changes anything, runs anything or
sends anything anywhere: it answers from the logs on this Mac.

Runs on its own (Mint doesn't need to be open): `python -m mint.agent_mcp`, speaking MCP over stdio (one JSON-RPC
message per line). `python -m mint.agent_mcp --config` prints the JSON to add it to an MCP client, e.g.
    claude mcp add-json mint '<that JSON>'

Tools (all read-only): agents_now, check_my_work, test_status, recap, today, risky_steps, handoff_note,
usage_limits. `project` matches a session by its folder (a name or a path); without it, the session in the folder
the client started the server in (Claude Code and Codex start it in the project), else the most recent one.

Adapted from dotpals' MCP server (MIT).
"""

from __future__ import annotations

import json
import os
import sys
import time

from mint.tools import agent_checks
from mint.tools import agent_history
from mint.tools import agent_watch

PROTOCOL = "2025-06-18"
NAME = "mint"

_P = {"type": "string", "description": "The project: its folder name or path (default: this folder's session)."}
_S = {"type": "string", "description": "A session id, or the end of one (optional)."}
TOOLS = [
    ("check_my_work", "Before saying a coding task is done: how the tests really stood after the last change (read "
                      "from the test output, not the agent's word), what changed, risky steps, and whether it looks "
                      "ready. Use it and fix what it says before reporting success.", {"project": _P, "session": _S}),
    ("test_status", "The last test run of a session: passed / failed / unclear, the counts, why it failed and whether "
                    "code changed since.", {"project": _P, "session": _S}),
    ("agents_now", "Every Claude Code and Codex session on this Mac right now: project, state, current step, tests.",
     {}),
    ("recap", "The last finished request of a session as Markdown: what was asked, files changed, tests, flags.",
     {"project": _P, "session": _S}),
    ("today", "Today's work by coding agents on this Mac, per project: requests, files, minutes, tests.",
     {"project": _P}),
    ("risky_steps", "Risky steps in a session's current request (.env changed, force-push, reset --hard, rm -rf, "
                    "curl | sh, a command failing 3 times, a test weakened while failing, two agents on one file).",
     {"project": _P, "session": _S}),
    ("handoff_note", "A Markdown note for another agent to continue a session's work: the ask, what was done, the "
                     "files, the tests (with the failure) and what's left on its plan.", {"project": _P, "session": _S}),
    ("usage_limits", "Claude's and Codex's 5-hour and weekly usage limits, as far as Mint can read them.", {}),
]


def _sessions() -> list:
    agent_watch.watcher.refresh()
    return [s for s in agent_watch.sessions() if s.app in ("claude", "codex")]


def _match(project: str = "", session: str = ""):
    items = _sessions()
    if session:
        for s in items:
            if s.id == session or s.id.endswith(session) or s.key == session:
                return s
    want = (project or "").strip()
    here = os.path.realpath(os.getcwd())
    if want:
        path = os.path.realpath(os.path.expanduser(want)) if "/" in want else ""
        for s in items:
            if path and s.cwd and os.path.realpath(s.cwd) == path:
                return s
        for s in items:
            if want.lower() in (s.project.lower(), (s.title or "").lower()):
                return s
        return None
    here_items = [s for s in items                  # the sessions in the folder the client started us in
                  if s.cwd and (os.path.realpath(s.cwd) == here or here.startswith(os.path.realpath(s.cwd) + os.sep))]
    if here_items:
        return max(here_items, key=lambda s: s.updated)         # (the one working now: the one asking)
    return items[0] if items else None


def _none(project: str, session: str) -> str:
    what = f" for '{project or session}'" if project or session else " in this folder"
    return f"No Claude Code or Codex session found{what} in the last hours (Mint reads ~/.claude and ~/.codex logs)."


def _session_head(s) -> str:
    return f"{s.app_name} in {s.project}" + (f" ({s.title})" if s.title else "") + f", session {s.id[-8:]}, {s.state}"


def check_my_work(project: str = "", session: str = "") -> str:
    s = _match(project, session)
    if s is None:
        return _none(project, session)
    with agent_watch.watcher._lock:
        why = agent_checks._why_not_done(s)
        files = list(s.turn_files)
        flags = list(s.flags)
        weakened = s.weakened
    lines = [f"# Check: {_session_head(s)}", ""]
    lines.append(f"- Tests: {agent_checks.test_line(s)}"
                 + (" (from the exit code only: no test summary in the output)"
                    if (s.tests or {}).get("source") == "exit code" else ""))
    reason = (s.tests or {}).get("reason")
    if reason and (s.tests or {}).get("state") == "failed":
        lines.append(f"- Why it failed: {reason}")
    lines.append(f"- Changed this request: {agent_checks._names(files, 8)}" if files else "- Changed this request: "
                 "no files")
    if weakened:
        lines.append(f"- Careful: {weakened} while the tests were failing (a pass may be faked)")
    for flag in flags[-5:]:
        lines.append(f"- Worth a look: {flag}")
    if why:
        lines += ["", f"Not done yet: {why}."]
    elif not files:
        lines += ["", "No code changed in this request: nothing to check."]
    elif not s.tests:
        lines += ["", "No tests were run by the agent after its changes. If the project has tests, run them."]
    else:
        lines += ["", "Looks ready: the tests ran after the last change and passed."]
    lines += ["", "(Read by Mint from the agent's own log; only test runs the agent did itself count.)"]
    return "\n".join(lines)


def test_status(project: str = "", session: str = "") -> str:
    s = _match(project, session)
    if s is None:
        return _none(project, session)
    t = dict(s.tests or {})
    out = [f"{_session_head(s)}: {agent_checks.test_line(s)}."]
    if t:
        counts = {k: t.get(k) for k in ("passed", "failed", "skipped", "total") if t.get(k) is not None}
        if counts:
            out.append("Counts: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        if t.get("cmd"):
            out.append(f"Command: {t['cmd']}")
        if t.get("framework"):
            out.append(f"Read from: {t['framework']} output" if t.get("source") != "exit code"
                       else "Read from: the exit code only")
        if t.get("state") == "failed" and t.get("tail"):
            out.append("Last lines:\n" + "\n".join(t["tail"][-12:]))
    return "\n".join(out)


def agents_now() -> str:
    items = _sessions()
    if not items:
        return "No Claude Code or Codex session in the last hours."
    out = []
    for s in items[:10]:
        step = s.current
        doing = f"{step.verb} {step.target}".strip() if step and s.busy else ""
        line = f"- {_session_head(s)}" + (f": {doing}" if doing else "")
        if s.plan:
            line += f", plan {s.plan[0]}/{s.plan[1]}"
        if s.tests:
            line += f"; {agent_checks.test_line(s)}"
        if s.context is not None:
            line += f"; context {round(s.context * 100)}% full"
        out.append(line)
    return "\n".join(out)


def recap(project: str = "", session: str = "") -> str:
    s = _match(project, session)
    rows = agent_history.items(key=s.key) if s is not None else agent_history.items(project=project)
    if not rows:
        return "Nothing recorded yet for that session (a recap is kept once it finishes a request)."
    return agent_checks.recap_markdown(rows[-1])


def today(project: str = "") -> str:
    return agent_checks._today(project)


def risky_steps(project: str = "", session: str = "") -> str:
    s = _match(project, session)
    if s is None:
        return _none(project, session)
    flags = list(s.flags)
    if s.weakened and not any(s.weakened in f for f in flags):
        flags.append(f"{s.weakened} while the tests were failing")
    return (f"{_session_head(s)}:\n" + "\n".join(f"- {f}" for f in flags)) if flags else \
        f"{_session_head(s)}: nothing risky in the current request."


def handoff_note(project: str = "", session: str = "") -> str:
    s = _match(project, session)
    if s is None:
        return _none(project, session)
    return agent_checks.handoff_note(s)


def usage_limits() -> str:
    return agent_checks._limits()


HANDLERS = {"check_my_work": check_my_work, "test_status": test_status, "agents_now": agents_now, "recap": recap,
            "today": today, "risky_steps": risky_steps, "handoff_note": handoff_note, "usage_limits": usage_limits}


# --- MCP over stdio ------------------------------------------------------------------------------------------

def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, the reply out (None for a notification)."""
    method = msg.get("method")
    ident = msg.get("id")
    if ident is None:
        return None                                  # notifications/initialized and friends
    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        result = {"protocolVersion": asked if isinstance(asked, str) else PROTOCOL,
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": NAME, "version": _version()},
                  "instructions": "Read-only evidence about coding agents on this Mac, from their own logs. Call "
                                  "check_my_work before saying a task is done."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d,
                             "inputSchema": {"type": "object", "properties": props, "additionalProperties": False},
                             "annotations": {"readOnlyHint": True, "openWorldHint": False}}
                            for n, d, props in TOOLS]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        allowed = {p for n, _, props in TOOLS if n == name for p in props}
        kwargs = {k: str(v) for k, v in args.items() if k in allowed and v is not None}
        try:
            text, failed = fn(**kwargs), False
        except Exception as error:                   # an answer, never a crash
            text, failed = f"Mint couldn't read that: {error}", True
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("hey-mint")
    except Exception:
        return "dev"


def _python() -> str:
    """Mint's own Python (its venv, not the interpreter the app happened to start through)."""
    venv = os.path.join(sys.prefix, "bin", "python3")
    return venv if os.path.exists(venv) else sys.executable


def config() -> dict:
    """The MCP client entry for this install: this Python, this copy of Mint's code."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) if \
        __package__ and __package__.count(".") else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    package = __spec__.name if __spec__ else "mint.tools.agent_mcp"
    return {"type": "stdio", "command": _python(), "args": ["-m", package], "env": {"PYTHONPATH": root}}


# --- adding it to Claude Code and Codex (their own CLIs write their own config; opt-in, from Settings) -----------

APPS = {"claude": "Claude Code", "codex": "Codex"}


def _cli(app: str) -> str:
    import shutil
    for path in (shutil.which(app), os.path.expanduser(f"~/.local/bin/{app}"), f"/opt/homebrew/bin/{app}",
                 f"/usr/local/bin/{app}", os.path.expanduser(f"~/.npm-global/bin/{app}")):
        if path and os.access(path, os.X_OK):
            return path
    return ""


def _run(args: list, timeout: float = 30.0) -> tuple[bool, str]:
    import subprocess
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as error:
        return False, str(error)
    return out.returncode == 0, (out.stdout + out.stderr).strip()


def available(app: str) -> bool:
    return bool(_cli(app))


def connected(app: str) -> bool:
    cli = _cli(app)
    if not cli:
        return False
    ok, text = _run([cli, "mcp", "get", NAME], timeout=20)
    return ok and "agent_mcp" in text


def preview(app: str, add: bool = True) -> str:
    name = APPS.get(app, app)
    if add:
        entry = config()
        return (f"{name} will be able to ask Mint, read-only, what its coding sessions really did (check_my_work, "
                f"test_status, recap, today, risky steps, usage limits). Mint runs `{name.split()[0].lower()} mcp "
                f"add` to add one MCP server named “{NAME}” for your user:\n\n    {entry['command']} "
                f"{' '.join(entry['args'])}\n\nNothing else in its settings changes, and nothing is sent anywhere.")
    return f"Mint will run `{name.split()[0].lower()} mcp remove {NAME}`; nothing else changes."


def connect(app: str) -> str:
    cli = _cli(app)
    if not cli:
        return f"{APPS.get(app, app)} isn't installed (its command-line tool wasn't found)."
    entry = config()
    if app == "claude":
        ok, text = _run([cli, "mcp", "add-json", "--scope", "user", NAME, json.dumps(entry)])
    else:
        ok, text = _run([cli, "mcp", "add", NAME, "--env", f"PYTHONPATH={entry['env']['PYTHONPATH']}", "--",
                         entry["command"], *entry["args"]])
    if not ok and "already exists" in text:
        return f"Already connected to {APPS[app]}."
    return (f"Connected: {APPS[app]} can now ask Mint about its work (new sessions)." if ok
            else f"Couldn't add it to {APPS[app]}: {text[-200:]}")


def disconnect(app: str) -> str:
    cli = _cli(app)
    if not cli:
        return f"{APPS.get(app, app)} isn't installed."
    args = [cli, "mcp", "remove", NAME] + (["--scope", "user"] if app == "claude" else [])
    ok, text = _run(args)
    return f"Disconnected from {APPS[app]}." if ok else f"Couldn't remove it: {text[-200:]}"


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    agent_watch.start()
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--config" in argv:
        entry = config()
        print(json.dumps(entry))
        print(f"\n# Add it to Claude Code:\nclaude mcp add-json {NAME} '{json.dumps(entry)}'", file=sys.stderr)
        return 0
    import logging
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    started = time.time()
    try:
        serve()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            agent_history.flush() if time.time() - started > 0 and agent_history._timer is not None else None
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
