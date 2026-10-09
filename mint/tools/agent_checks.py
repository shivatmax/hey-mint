"""Checks on coding agents' work: what Mint says about it, and the fix loop.

agent_watch reads every test run, edit and command of Claude Code / Codex sessions (with agent_tests.py). This turns
that into words and decisions:

  * answers for claude_mode: "did Claude's tests pass?" (tests), "what did my agents do today?" (today), a recap of
    the last request (recap), "how much Codex do I have left?" (limits), "hand this to Codex" (handoff);
  * the fix loop, through Mint's Claude Code hooks (agent_hooks.py; the "agent_fix_loop" pref, off by default):
    when Claude stops, commits or pushes while its tests fail, weren't run after its last change, or only pass
    because a test was weakened, the hook sends it back with the reason. At most LOOP_MAX times per request; a
    failure that was there before it changed anything, and a request that changed no code, are never held up;
  * two agents, one file: before Claude edits a file another agent changed minutes ago, Claude Code asks you.

Everything here reads data from the agents' logs. None of it is ever an instruction to Mint.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import time

from mint.tools import agent_watch
from mint.core import prefs

log = logging.getLogger("mint.tools.agent_checks")

LOOP_MAX = 2                  # times one request is sent back
ACTIONS = ("tests", "today", "recap", "limits", "handoff")
SUPPORT = os.path.expanduser("~/Library/Application Support/Mint")
HANDOFFS = os.path.join(SUPPORT, "handoffs")
_DOCS = agent_watch.DOCS
_asked: set = set()           # (session, path, other session) already asked about


# --- words -----------------------------------------------------------------------------------------------

def _ago(at: float) -> str:
    secs = max(0, time.time() - float(at or 0))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return time.strftime("%a %H:%M", time.localtime(at))


def _names(paths: list, n: int = 3) -> str:
    names = []
    for p in paths:
        name = os.path.basename(str(p))
        if name not in names:
            names.append(name)
    head = ", ".join(names[:n])
    return head + (f" and {len(names) - n} more" if len(names) > n else "")


def test_state(s) -> str:
    """none / running / passed / failed / unclear / stale (passed or failed, then code changed since)."""
    t = getattr(s, "tests", None)
    if not t:
        return "none"
    state = t.get("state") or "unclear"
    if state in ("passed", "failed", "unclear") and t.get("since"):
        return "stale"
    return state


def test_line(s) -> str:
    """One line on how its tests stand: 'tests failed: 2 of 48 failed - test_total: expected 3, got -1, 4 min ago;
    changed invoice.ts since'."""
    t = getattr(s, "tests", None)
    if not t:
        changed = getattr(s, "turn_files", None) or []
        return f"changed {_names(changed)}, no tests run" if changed else "no tests run by the agent"
    state = t.get("state") or "unclear"
    line = t.get("line") or state
    if state == "running":
        return "running the tests now"
    text = {"passed": f"tests {line}" if line.startswith("passed") else f"tests passed ({line})",
            "failed": f"tests failed: {line}"}.get(state, f"tests unclear: {line}")
    if state == "failed" and t.get("reason"):
        text += f" - {t['reason']}"
    text += f", {_ago(t.get('at') or 0)}"
    if t.get("since"):
        text += f"; changed {_names(t['since'])} since, so the latest code isn't tested"
    return text


def check_line(s) -> str:
    """The test line plus risky steps, for alerts and status ("" when there's nothing to say)."""
    parts = []
    if getattr(s, "tests", None) or getattr(s, "turn_files", None):
        parts.append(test_line(s))
    flags = getattr(s, "flags", None) or []
    if flags:
        parts.append("worth a look: " + "; ".join(flags[-3:]))
    return ". ".join(parts)


def emoji_line(s) -> str:
    """Telegram's line under a finished request: '✅ tests passed (48 passed)' / '⚠️ not tested since …'."""
    state = test_state(s)
    t = getattr(s, "tests", None) or {}
    out = []
    if state == "passed":
        line = t.get("line") or "passed"
        out.append(f"✅ tests {line}" if line.startswith("passed") else f"✅ tests passed ({line})")
    elif state == "failed":
        out.append(f"❌ tests failed: {t.get('line') or ''}" + (f" - {t['reason']}" if t.get("reason") else ""))
    elif state == "unclear":
        out.append(f"❔ tests unclear: {t.get('line') or ''}")
    elif state == "stale":
        out.append(f"⚠️ not tested since the last change ({_names(t.get('since') or [])})")
    elif getattr(s, "turn_files", None):
        out.append(f"⚪ changed {_names(s.turn_files)}, no tests run")
    for flag in (getattr(s, "flags", None) or [])[-3:]:
        out.append(f"🚩 {flag}")
    return "\n".join(out)


# --- claude_mode actions ---------------------------------------------------------------------------------

def report(action: str, args: dict) -> str:
    action = action.lower()
    session = str(args.get("session") or "")
    agent_watch.watcher.refresh()
    if action == "tests":
        return _tests(session)
    if action == "today":
        return _today(session)
    if action == "recap":
        return _recap(session)
    if action == "limits":
        return _limits()
    if action == "handoff":
        return handoff(session, str(args.get("to") or args.get("text") or "codex"))
    return f"Unknown action {action}."


def _pick(session: str):
    from mint.tools import agent_remote
    if session:
        return agent_remote.find(session)
    items = agent_watch.sessions()
    with_tests = [s for s in items if s.tests]
    return (max(with_tests, key=lambda s: s.tests.get("at") or 0) if with_tests else
            next((s for s in items if s.turn_files), items[0] if items else None))


def _tests(session: str) -> str:
    s = _pick(session)
    if s is None:
        return (f"No session matches '{session}'." if session else "No Claude Code or Codex session is running.")
    text = f"{s.app_name} in {s.project}: {test_line(s)}."
    if s.flags:
        text += " Worth a look: " + "; ".join(s.flags[-3:]) + "."
    if s.weakened:
        text += f" Careful: it {s.weakened} while the tests were failing, so a pass may be faked."
    if (s.tests or {}).get("source") == "exit code":
        text += " (Read from the exit code only: the output had no test summary.)"
    return text + " (Only test runs the agent did itself count; this is read from its log.)"


def _today(project: str) -> str:
    from mint.tools import agent_history
    rows = agent_history.items(since=agent_history.day_start(), project=project)
    if not rows:
        return "No coding agent finished a request today" + (f" in {project}." if project else ".")
    groups: dict = {}
    for r in rows:
        groups.setdefault((r.get("app"), r.get("project")), []).append(r)
    parts = []
    for (app, proj), rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        name = "Claude Code" if app == "claude" else "Codex" if app == "codex" else str(app)
        files = {f for r in rs for f in r.get("files") or []}
        worked = sum(min(2 * 3600, max(0.0, float(r.get("ended") or 0) - float(r.get("started") or 0))) for r in rs)
        states = [((r.get("tests") or {}).get("state")) for r in rs if r.get("tests")]
        line = f"{name} in {proj}: {len(rs)} request{'s' if len(rs) != 1 else ''}"
        if files:
            line += f", {len(files)} file{'s' if len(files) != 1 else ''} changed"
        if worked >= 60:
            line += f", about {int(worked // 60)} min of work"
        if states:
            line += f", tests last {states[-1]}"
        flags = [f for r in rs for f in r.get("flags") or []]
        if flags:
            line += f", {len(flags)} thing{'s' if len(flags) != 1 else ''} worth a look ({flags[-1]})"
        last = (rs[-1].get("prompt") or "").strip()
        if last:
            line += f'; last asked "{last[:90]}"'
        parts.append(line)
    return "Today: " + " | ".join(parts[:6]) + ". (From the agents' own logs.)"


def recap_markdown(row: dict) -> str:
    out = [f"**{row.get('prompt') or 'Request'}**", ""]
    if row.get("summary"):
        out += [row["summary"].strip(), ""]
    files = row.get("files") or []
    if files:
        out.append(f"- Changed {len(files)} file{'s' if len(files) != 1 else ''}: {_names(files, 8)}")
    t = row.get("tests")
    if t:
        mark = {"passed": "✅", "failed": "❌"}.get(t.get("state"), "❔")
        out.append(f"- {mark} Tests {t.get('state')}: {t.get('line') or ''} (`{t.get('cmd') or ''}`)"
                   + (f", changed {_names(t['since'])} since" if t.get("since") else ""))
    elif files:
        out.append("- ⚪ No tests run")
    for flag in row.get("flags") or []:
        out.append(f"- 🚩 {flag}")
    return "\n".join(out).strip()


def _recap(session: str) -> str:
    from mint.tools import agent_history
    s = _pick(session)
    rows = agent_history.items(key=s.key) if s is not None else agent_history.items(project=session)
    if not rows:
        return "Nothing recorded for that session yet (a recap is kept once it finishes a request)."
    r = rows[-1]
    name = "Claude Code" if r.get("app") == "claude" else "Codex"
    text = f'{name} in {r.get("project")}, asked "{(r.get("prompt") or "")[:160]}"'
    files = r.get("files") or []
    text += f": changed {_names(files)}" if files else ": changed no files"
    if r.get("tests"):
        t = r["tests"]
        text += f"; tests {t.get('state')} ({t.get('line') or ''})"
    if r.get("flags"):
        text += "; worth a look: " + "; ".join(r["flags"][-3:])
    if r.get("summary"):
        text += f'. It said: "{r["summary"][:300]}"'
    return text + ". (Read from its log.)"


def limits_line() -> str:
    """One short line for the notch, the menu and Settings: "Claude 5h 3% · week 36%   Codex 5h 12%" (used), or ""."""
    lim = agent_watch.limits()
    parts = []
    for app, name in (("claude", "Claude"), ("codex", "Codex")):
        x = lim.get(app) or {}
        bits = [f"{label} {round(x[k])}%" for k, label in (("5h", "5h"), ("week", "week")) if k in x]
        if bits:
            parts.append(f"{name} " + " · ".join(bits))
    return "   ".join(parts)


def _limits() -> str:
    agent_watch.refresh_live(block=True)          # (a question: off the main thread, worth a second for live figures)
    lim = agent_watch.limits(live=False)
    parts = []
    for app, name in (("claude", "Claude"), ("codex", "Codex")):
        x = lim.get(app)
        if not x or ("5h" not in x and "week" not in x):
            continue
        bits = []
        for k, label in (("5h", "5-hour"), ("week", "weekly")):
            if k in x:
                left = max(0, 100 - round(x[k]))
                reset = x.get(k + "_resets")
                when = f", resets {time.strftime('%a %H:%M', time.localtime(reset))}" if reset else ""
                bits.append(f"{left}% of the {label} limit left{when}")
        parts.append(f"{name}: " + "; ".join(bits) + (" (live)" if x.get("live") else f" (as of {_ago(x.get('at') or 0)})"))
    if "claude" not in lim:
        parts.append("Claude's limits aren't known right now: Claude Code passes them to Mint while it runs (Settings "
                     "▸ Models & agents ▸ Coding agents), and the Claude app notes them while it's open")
    return ". ".join(parts) + "." if parts else "No usage limits known right now."


# --- hand-off ----------------------------------------------------------------------------------------------

def handoff_note(s) -> str:
    from mint.tools import agent_history
    rows = agent_history.items(key=s.key)[-5:]
    rel = (lambda p: os.path.relpath(p, s.cwd) if s.cwd and str(p).startswith(s.cwd) else str(p))
    ask = (rows[0].get("prompt") if rows else "") or s.prompt
    last = s.prompt or (rows[-1].get("prompt") if rows else "")
    out = [f"# Hand-off from {s.app_name}" + (f" · {s.title}" if s.title else ""), "",
           "You're continuing work another agent started. Read this, check the current state (the files below, "
           "`git status`, the tests), then continue" + (f": “{last[:200]}”." if last else "."), ""]
    if ask:
        out += ["## The original ask", "", "> " + ask[:2000].replace("\n", "\n> "), ""]
    if last and last != ask:
        out += ["## The last request", "", "> " + last[:2000].replace("\n", "\n> "), ""]
    if rows:
        out += ["## What was done", ""]
        for r in rows:
            out += [recap_markdown(r), ""]
    files = sorted(s.files, key=s.files.get)[-30:]
    if files:
        out += ["## Files changed in this session", ""] + [f"- `{rel(p)}`" for p in files] + [""]
    if s.tests:
        out += ["## Tests", "", test_line(s) + ". (Only tests the agent ran: run them again to be sure.)"]
        tail = s.tests.get("tail") or []
        if s.tests.get("state") == "failed" and tail:
            out += ["", f"The last run, `{s.tests.get('cmd') or ''}`, failed:", "", "```",
                    "\n".join(tail).replace("```", "'''"), "```"]
        out.append("")
    todo = [(st, tx) for st, tx in s.plan_items if st != "completed"]
    if todo:
        done, total = s.plan or (0, 0)
        out += [f"## Still on its plan ({done} of {total} done)", ""]
        out += [f"- [ ] {tx}" + (" (it was on this one)" if st == "in_progress" else "") for st, tx in todo[:12]]
        out.append("")
    if s.flags:
        out += ["## Worth a second look", ""] + [f"- {f}" for f in s.flags] + [""]
    if s.summary:
        out += [f"## {s.app_name}'s last message", "", "> " + s.summary[:2500].replace("\n", "\n> "), ""]
    out += ["---", f"_Written by Mint from {s.app_name}'s session {s.id[-8:]}" + (f" in {s.cwd}" if s.cwd else "")
            + f", {time.strftime('%Y-%m-%d %H:%M')}. Mint only sees what the agent did through its tools: check the "
              "files for how things really stand._"]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


_CLI = {"codex": "codex", "claude": "claude", "claude code": "claude", "gemini": "gemini", "gemini cli": "gemini"}


def handoff(session: str, to: str) -> str:
    """Write the hand-off note and start the other agent on it, in a new Terminal window in the same folder."""
    from mint.tools import agent_remote
    s = agent_remote.find(session) if session else _pick("")
    if s is None:
        return f"No session matches '{session}'." if session else "No Claude Code or Codex session to hand off."
    cli = _CLI.get(to.lower().strip(), "")
    if not cli:
        return "Hand off to Codex, Claude Code or Gemini CLI."
    os.makedirs(HANDOFFS, exist_ok=True)
    path = os.path.join(HANDOFFS, f"{re.sub(r'[^A-Za-z0-9._-]+', '-', s.project)[:40]}-{time.strftime('%Y%m%d-%H%M%S')}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(handoff_note(s))
    folder = s.cwd if s.cwd and os.path.isdir(s.cwd) else os.path.expanduser("~")
    command = f"cd {shlex.quote(folder)} && {cli} {shlex.quote(f'Read {path} and continue the work it describes.')}"
    script = f'tell application "Terminal"\n activate\n do script {agent_remote._as_string(command)}\n return "ok"\nend tell'
    ok, why = agent_remote._osascript(script)
    name = {"codex": "Codex", "claude": "Claude Code", "gemini": "Gemini CLI"}[cli]
    if not ok:
        return f"Wrote the hand-off note ({path}) but couldn't open Terminal: {why[:120]}"
    return (f"Handed {s.app_name}'s work in {s.project} to {name}: a new Terminal window in {folder} started "
            f"{cli} with the note ({os.path.basename(path)}).")


# --- the fix loop and conflicts (agent_hooks asks; answers go back to Claude Code) -------------------------

def answer(payload: dict) -> dict:
    """The hook asks before Stop, a shipping command (commit / push / publish) or an edit. {} = nothing to say."""
    try:
        event = payload.get("hook_event_name") or ""
        if event == "Stop":
            return _stop(payload)
        if event == "PreToolUse":
            tool = payload.get("tool_name") or ""
            if tool == "Bash":
                return _ship(payload)
            if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                return _edit(payload)
    except Exception:
        log.exception("agent check failed")
    return {}


def _settled(path: str, wait: float = 1.5) -> None:
    """Let agent_watch read the transcript up to now (the hook can arrive before the watcher's next look)."""
    agent_watch.watcher.poke()
    end = time.time() + wait
    while time.time() < end:
        f = agent_watch.watcher._files.get(path)
        try:
            if f is not None and f.offset >= os.path.getsize(path):
                return
        except OSError:
            return
        time.sleep(0.05)
        agent_watch.watcher.poke()


def _session(payload: dict):
    path = payload.get("transcript_path") or ""
    if path:
        _settled(path)
    return agent_watch.watcher._sessions.get("claude:" + str(payload.get("session_id") or ""))


def _code_changed(s) -> list:
    return [p for p in s.turn_files if not str(p).lower().endswith(_DOCS)]


def _why_not_done(s) -> str:
    """Why this request isn't finished, or ""."""
    changed = _code_changed(s)
    t = s.tests
    if not changed or not t:
        return ""                                # no code changed, or a project it never tested: never held up
    state = t.get("state")
    if s.weakened and state == "passed":
        return (f"the tests only pass because a test was weakened ({s.weakened}). Put the test back as it was and "
                f"fix the code instead, then run the tests again")
    if state == "failed" and s.tests_before != "failed":
        why = t.get("line") or "they failed"
        return (f"the tests fail ({why}{' - ' + t['reason'] if t.get('reason') else ''}). Fix the code so they pass "
                f"(don't change or skip tests to make them pass), run them again, then finish")
    if state in ("passed", "failed", "unclear") and t.get("since"):
        return (f"you changed {_names(t['since'])} after the last test run (`{t.get('cmd') or 'tests'}`), so the latest "
                f"code isn't tested. Run the tests again before finishing")
    if state == "unclear" and t.get("cut"):
        return (f"the last test run couldn't be read ({t.get('line') or 'output cut'}). Run the tests again without "
                f"cutting the output (no | tail or | head), then finish")
    return ""


def _stop(payload: dict) -> dict:
    if not prefs.get("agent_fix_loop"):
        return {}
    s = _session(payload)
    if s is None:
        return {}
    with agent_watch.watcher._lock:
        why = _why_not_done(s)
        if not why:
            return {}
        if s.sent_back >= LOOP_MAX:
            agent_watch._flag(s, f"sent back {LOOP_MAX} times, still not done: {why.split('.')[0]}")
            agent_watch.watcher._dirty = True
            return {}
        s.sent_back += 1
        agent_watch._flag(s, "Mint sent it back: " + why.split(".")[0])
        agent_watch.watcher._dirty = True
    print(f"  [agents: sent Claude back ({s.sent_back}/{LOOP_MAX}): {why[:80]}]", flush=True)
    return {"decision": "block", "reason": "Mint checked your work before you finish: " + why + "."}


def _ship(payload: dict) -> dict:
    if not prefs.get("agent_fix_loop"):
        return {}
    from mint.tools import agent_tests
    cmd = str((payload.get("tool_input") or {}).get("command") or "")
    if not agent_tests.ships(cmd):
        return {}
    head = re.split(r"\bgit\s+(commit|push)\b|\bgh\s+pr\b|\bnpm\s+publish\b", cmd)[0]
    if agent_tests.is_test(head):
        return {}                                # "npm test && git commit": the tests run first
    s = _session(payload)
    if s is None:
        return {}
    with agent_watch.watcher._lock:
        why = _why_not_done(s)
    if not why:
        return {}
    return {"deny": "Mint checked your work: " + why.replace("then finish", "then commit").replace(
        "before finishing", "before committing") + "."}


def _edit(payload: dict) -> dict:
    if prefs.get("agent_checks") is False:
        return {}
    path = str((payload.get("tool_input") or {}).get("file_path") or (payload.get("tool_input") or {}).get(
        "notebook_path") or "")
    if not path:
        return {}
    me = "claude:" + str(payload.get("session_id") or "")
    now = time.time()
    with agent_watch.watcher._lock:
        for other in agent_watch.watcher._sessions.values():
            if other.key == me or other.app == "mint":
                continue
            at = other.files.get(path)
            if at and now - at < agent_watch.CONFLICT and (other.busy or other.approval) and \
                    (me, path, other.key) not in _asked:
                _asked.add((me, path, other.key))
                return {"ask": f"{other.app_name} ({other.project}) changed {os.path.basename(path)} "
                               f"{_ago(at)}. Edit anyway? (Mint's check: two agents on one file.)"}
    return {}
