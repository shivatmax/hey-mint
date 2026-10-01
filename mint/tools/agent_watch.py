"""Coding agents on this Mac, live: Claude Code and Codex sessions, read from their own session logs.

Both write every session as it happens, one JSON object per line:

    Claude Code   ~/.claude/projects/<project>/<session>.jsonl      (the CLI, the IDE extensions and the
                                                                     Claude desktop app's Code tab)
    Codex         ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl      (the CLI and the Codex app)

So Mint needs nothing installed to see them: a background thread looks at the files written in the last
minutes, reads what was appended since last time (from the end of a file the first time) and keeps a
small picture of each session - the project, what it is doing right now, its last steps (Read, Edit,
Run...) each with a running / done / failed mark, something to show for the current step (a few lines
of the file it read, the diff it made, the command and its output), the question it is asking, and
the summary it ended its turn with. notch_agents draws that ("Claude mode").

Approvals (Allow / Deny from the notch) need Claude Code's PermissionRequest hook: agent_hooks.py feeds
those in through ingest_hook().

Everything read from the logs is data shown to the user; nothing in it is ever an instruction to Mint.

API (any thread; listeners are called on the main thread):

    start()                      begin watching (idempotent)
    sessions()                   [Session] copies, the most pressing first
    get(key)                     one Session copy or None
    on_change(fn)                fn() whenever anything changed
    on_event(fn)                 fn(kind, session) for "started", "waiting", "asking", "finished", "failed"
    ingest_hook(payload)         an event from agent_hooks (Claude Code hook JSON + terminal info)
"""

from __future__ import annotations

import copy
import glob
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("mint.tools.agent_watch")

CLAUDE_DIR = os.path.expanduser("~/.claude/projects")
CODEX_DIR = os.path.expanduser("~/.codex/sessions")
POLL = 0.6               # seconds between looks at the files already followed
SCAN = 4.0               # seconds between looks for new session files
FRESH = 20 * 60          # a file written within this long is followed
KEEP = 45 * 60           # a quiet session that just finished stays near the top this long
KEEP_IDLE = 8 * 3600     # ... and stays listed (idle) this long: the app you're in can still show it
RECENT_READ = 600_000    # bytes read from the end of an older (not fresh) file seen at launch
STALE = 12 * 60          # "working" with no new line for this long reads as idle (the process went away)
FIRST_READ = 3_000_000   # bytes read from the end of a file the first time it is seen
MAX_LINE = 8_000_000     # a longer line (a huge tool result) is skipped
MAX_STEPS = 24

CLAUDE_TOOLS = {
    "Read": "Read", "Edit": "Edit", "MultiEdit": "Edit", "Write": "Write", "NotebookEdit": "Edit",
    "Bash": "Run", "BashOutput": "Run", "KillShell": "Stop", "Grep": "Search", "Glob": "Find", "LS": "List",
    "WebFetch": "Fetch", "WebSearch": "Web", "Task": "Agent", "Agent": "Agent", "TodoWrite": "Plan",
    "AskUserQuestion": "Ask", "ExitPlanMode": "Plan", "Skill": "Skill", "ToolSearch": "Tools",
}


@dataclass
class Step:
    id: str
    verb: str                   # Read / Edit / Run / Search ... (shown)
    target: str                 # invoice.ts, npm test, "TODO" ...
    status: str = "run"         # run / ok / fail
    started: float = 0.0
    detail: dict | None = None  # what the detail card shows for this step (see _detail_* below)


@dataclass
class Session:
    key: str                    # "claude:<id>" / "codex:<id>"
    app: str                    # claude / codex
    id: str
    path: str = ""
    cwd: str = ""
    title: str = ""             # the session's own name when it has one
    where: str = ""             # cli / claude-desktop / vscode ... (which app or terminal runs it)
    state: str = "idle"         # thinking / working / waiting / asking / done / failed / idle
    since: float = 0.0          # when the state began
    updated: float = 0.0        # the last line's time (wall clock)
    prompt: str = ""            # what the user asked this turn
    summary: str = ""           # the agent's last words (its answer when done)
    steps: list = field(default_factory=list)
    turn_steps: int = 0         # steps taken this turn (steps keeps only the last MAX_STEPS)
    question: dict | None = None    # {"text", "options": [..]} while it asks the user something
    approval: dict | None = None    # {"id", "tool", "verb", "target", "detail", "always"} from the hook
    term: dict = field(default_factory=dict)     # term_program, bundle_id, tty ... (from the hook)
    edits: int = 0
    runs: int = 0
    turn_started: float = 0.0

    @property
    def project(self) -> str:
        name = os.path.basename(self.cwd.rstrip("/")) if self.cwd else ""
        return name or ("Claude Code" if self.app == "claude" else "Codex")

    @property
    def app_name(self) -> str:
        return "Claude Code" if self.app == "claude" else "Codex"

    @property
    def busy(self) -> bool:
        return self.state in ("thinking", "working", "waiting", "asking")

    @property
    def current(self):
        return self.steps[-1] if self.steps else None


# --- the watcher ----------------------------------------------------------------------------------------

class _File:
    __slots__ = ("path", "app", "offset", "inode", "carry", "key", "mtime")

    def __init__(self, path, app):
        self.path, self.app = path, app
        self.offset, self.inode, self.carry, self.key, self.mtime = -1, 0, b"", "", 0.0


class Watcher:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._files: dict[str, _File] = {}
        self._change_fns: list = []
        self._event_fns: list = []
        self._thread = None
        self._dirty = False
        self._events: list = []
        self._last_scan = 0.0
        self._first_scan = True
        self._wake = threading.Event()

    # --- public -----------------------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="mint-agent-watch", daemon=True)
            self._thread.start()

    def on_change(self, fn) -> None:
        self._change_fns.append(fn)

    def on_event(self, fn) -> None:
        self._event_fns.append(fn)

    def sessions(self) -> list[Session]:
        now = time.time()
        with self._lock:
            items = [copy.deepcopy(s) for s in self._sessions.values()
                     if s.busy or s.approval or now - s.updated < KEEP_IDLE]
        rank = {"waiting": 0, "asking": 0, "failed": 2, "working": 1, "thinking": 1, "done": 3, "idle": 4}
        # (a session quiet for a while ranks as idle, whatever it said last)
        items.sort(key=lambda s: (0 if s.approval else rank.get(s.state, 5) if s.busy or now - s.updated < KEEP
                                  else 4, -s.updated))
        return items

    def get(self, key: str) -> Session | None:
        with self._lock:
            s = self._sessions.get(key)
            return copy.deepcopy(s) if s is not None else None

    def poke(self) -> None:
        """Look again now (a hook said something happened)."""
        self._wake.set()

    # --- the thread -------------------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                self._pass()
            except Exception:
                log.exception("agent watch pass failed")
            self._wake.wait(POLL)
            self._wake.clear()

    def _pass(self) -> None:
        now = time.time()
        if now - self._last_scan > SCAN:
            self._last_scan = now
            self._scan(now)
        for f in list(self._files.values()):
            self._read(f)
        self._codex_titles()
        self._expire(now)
        self._flush()

    def _scan(self, now: float) -> None:
        found = []
        if os.path.isdir(CLAUDE_DIR):
            found += [(p, "claude") for p in glob.glob(os.path.join(CLAUDE_DIR, "*", "*.jsonl"))]
        if os.path.isdir(CODEX_DIR):
            for days_ago in (0, 1):
                day = time.strftime("%Y/%m/%d", time.localtime(now - days_ago * 86400))
                found += [(p, "codex") for p in glob.glob(os.path.join(CODEX_DIR, day, "*.jsonl"))]
        for path, app in found:
            if path in self._files:
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if now - mtime < FRESH or (self._first_scan and now - mtime < KEEP_IDLE):
                f = _File(path, app)
                if now - mtime >= FRESH:
                    f.carry = b"__recent__"                    # read only the end of it (see _read)
                self._files[path] = f
        self._first_scan = False
        for path, f in list(self._files.items()):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                del self._files[path]
                continue
            if now - mtime > FRESH and f.offset >= 0 and not self._busy_file(f):
                del self._files[path]          # (its session stays listed; a new line brings the file back)

    def _busy_file(self, f: _File) -> bool:
        s = self._sessions.get(f.key)
        return bool(s is not None and (s.busy or s.approval))

    def _read(self, f: _File) -> None:
        try:
            st = os.stat(f.path)
        except OSError:
            return
        if f.offset < 0 or st.st_ino != f.inode or st.st_size < f.offset:
            f.inode = st.st_ino
            f.offset = max(0, st.st_size - (RECENT_READ if f.carry == b"__recent__" else FIRST_READ))
            f.carry = b""
            skip_first = f.offset > 0
        else:
            skip_first = False
        if st.st_size == f.offset:
            return
        try:
            with open(f.path, "rb") as fh:
                fh.seek(f.offset)
                data = fh.read(st.st_size - f.offset)
        except OSError:
            return
        f.offset += len(data)
        f.mtime = st.st_mtime
        data = f.carry + data
        lines = data.split(b"\n")
        f.carry = lines.pop()                      # an unfinished last line waits for the rest
        if len(f.carry) > MAX_LINE:
            f.carry = b""
        if skip_first and lines:
            lines.pop(0)                           # we started mid-line
            if f.app == "codex":
                lines.insert(0, _first_raw_line(f.path))   # its session_meta: the app and folder it runs in
        for raw in lines:
            if not raw.strip() or len(raw) > MAX_LINE:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            try:
                if f.app == "claude":
                    self._claude(f, d)
                else:
                    self._codex(f, d)
            except Exception:
                log.debug("could not read a line of %s", f.path, exc_info=True)

    def _codex_titles(self) -> None:
        """Codex keeps its threads' names in ~/.codex/session_index.jsonl (the end of it is enough)."""
        path = os.path.join(os.path.dirname(CODEX_DIR), "session_index.jsonl")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if mtime == getattr(self, "_titles_mtime", 0):
            return
        self._titles_mtime = mtime
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, os.path.getsize(path) - 200_000))
                rows = fh.read().split(b"\n")[1:]
        except OSError:
            return
        with self._lock:
            for raw in rows:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                s = self._sessions.get(f"codex:{d.get('id')}")
                if s is not None and d.get("thread_name") and s.title != d["thread_name"]:
                    s.title = _clip(d["thread_name"], 60)
                    self._dirty = True

    def _expire(self, now: float) -> None:
        with self._lock:
            for key, s in list(self._sessions.items()):
                quiet = now - s.updated
                if s.state in ("thinking", "working") and quiet > STALE and not s.approval:
                    self._set_state(s, "idle", now)
                if quiet > KEEP_IDLE and not s.busy and not s.approval:
                    del self._sessions[key]
                    self._dirty = True

    def _flush(self) -> None:
        if not self._dirty and not self._events:
            return
        events, self._events = self._events, []
        self._dirty = False
        try:
            from PyObjCTools import AppHelper
        except Exception:
            return

        def deliver():
            for kind, session in events:
                for fn in list(self._event_fns):
                    try:
                        fn(kind, session)
                    except Exception:
                        log.exception("agent event listener failed")
            for fn in list(self._change_fns):
                try:
                    fn()
                except Exception:
                    log.exception("agent change listener failed")
        AppHelper.callAfter(deliver)

    # --- shared bookkeeping -----------------------------------------------------------------------

    def _session(self, f: _File, app: str, sid: str) -> Session:
        key = f"{app}:{sid}"
        s = self._sessions.get(key)
        if s is None:
            s = Session(key=key, app=app, id=sid, path=f.path)
            self._sessions[key] = s
        f.key = key
        return s

    def _set_state(self, s: Session, state: str, when: float) -> None:
        if s.state == state:
            return
        old = s.state
        s.state, s.since = state, when
        self._dirty = True
        live = time.time() - when < 30          # only fresh changes animate (not the history read at start)
        if not live:
            return
        if state in ("thinking", "working") and old in ("idle", "done", "failed"):
            self._events.append(("started", copy.deepcopy(s)))
        elif state in ("waiting", "asking", "done", "failed"):
            self._events.append(({"done": "finished"}.get(state, state), copy.deepcopy(s)))

    def _add_step(self, s: Session, step: Step) -> None:
        if not step.target:
            step.target = {"Run": "a command", "Edit": "a file", "Read": "a file", "Write": "a file"}.get(step.verb, "")
        for old in s.steps:
            if old.id == step.id and step.id:
                return
        s.steps.append(step)
        s.turn_steps += 1
        if len(s.steps) > MAX_STEPS:
            del s.steps[0]
        if step.verb in ("Edit", "Write"):
            s.edits += 1
        elif step.verb == "Run":
            s.runs += 1
        self._dirty = True

    def _step(self, s: Session, sid: str):
        for step in reversed(s.steps):
            if step.id == sid:
                return step
        return None

    def _new_turn(self, s: Session, prompt: str, when: float) -> None:
        s.prompt = _clip(_user_words(prompt), 400)
        s.summary = ""
        s.question = None
        s.turn_steps = 0
        s.turn_started = when
        s.steps = [st for st in s.steps if st.status == "run"]    # (none, normally)
        self._set_state(s, "thinking", when)

    # --- Claude Code ------------------------------------------------------------------------------

    def _claude(self, f: _File, d: dict) -> None:
        sid = d.get("sessionId") or os.path.splitext(os.path.basename(f.path))[0]
        kind = d.get("type")
        with self._lock:
            s = self._session(f, "claude", sid)
            if kind in ("custom-title", "agent-name"):
                title = d.get("customTitle") or d.get("agentName") or ""
                if title and title != s.title:
                    s.title = _clip(title, 60)
                    self._dirty = True
                return
            if kind == "last-prompt" and not s.prompt:
                s.prompt = _clip(d.get("lastPrompt") or "", 400)
                return
            if kind not in ("user", "assistant", "system"):
                return
            when = _when(d.get("timestamp"))
            if when:
                s.updated = max(s.updated, when)
            if d.get("cwd"):
                s.cwd = d["cwd"]
            if d.get("entrypoint"):
                s.where = d["entrypoint"]
            if d.get("isSidechain"):
                if s.state not in ("waiting", "asking") and when:
                    self._set_state(s, "working", when)   # a sub-agent working for it
                return
            message = d.get("message") or {}
            if kind == "assistant":
                self._claude_assistant(s, message, when or time.time())
            elif kind == "user":
                self._claude_user(s, d, message, when or time.time())
            elif kind == "system" and "error" in str(d.get("subtype") or d.get("level") or "").lower():
                s.summary = _clip(str(d.get("content") or "Something went wrong."), 300)
                self._set_state(s, "failed", when or time.time())

    def _claude_assistant(self, s: Session, message: dict, when: float) -> None:
        content = message.get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        tools = False
        for block in content:
            t = block.get("type")
            if t == "tool_use":
                tools = True
                self._claude_tool(s, block, when)
            elif t == "text" and (block.get("text") or "").strip():
                s.summary = _clip(block["text"].strip(), 600)
                self._dirty = True
        stop = message.get("stop_reason")
        if stop in ("end_turn", "stop_sequence", "max_tokens") and not tools:
            pending = [st for st in s.steps if st.status == "run"]
            for st in pending:
                st.status = "ok"
            s.question = None
            s.approval = None
            self._set_state(s, "done", when)
        elif tools or stop == "tool_use":
            if s.state not in ("waiting", "asking"):
                self._set_state(s, "working", when)
        elif s.state in ("idle", "done", "failed"):
            self._set_state(s, "thinking", when)

    def _claude_tool(self, s: Session, block: dict, when: float) -> None:
        name = block.get("name") or "Tool"
        args = block.get("input") or {}
        verb, target = _claude_label(name, args)
        step = Step(id=block.get("id") or "", verb=verb, target=target, started=when,
                    detail=_claude_detail_before(name, args))
        self._add_step(s, step)
        if name == "AskUserQuestion":
            qs = args.get("questions") or []
            q = qs[0] if qs and isinstance(qs[0], dict) else {}
            s.question = {"text": _clip(q.get("question") or "Claude has a question.", 240),
                          "options": [_clip(o.get("label") or "", 40) for o in (q.get("options") or [])
                                      if isinstance(o, dict)][:4],
                          "step": step.id}
            self._set_state(s, "asking", when)
        elif name == "ExitPlanMode":
            s.question = {"text": "Claude has a plan ready for you to approve.", "options": [], "step": step.id,
                          "plan": _clip(str(args.get("plan") or ""), 1200)}
            self._set_state(s, "asking", when)

    def _claude_user(self, s: Session, d: dict, message: dict, when: float) -> None:
        content = message.get("content")
        if isinstance(content, str):
            self._claude_prompt(s, d, content, when)
            return
        for block in content or []:
            t = block.get("type")
            if t == "tool_result":
                step = self._step(s, block.get("tool_use_id") or "")
                if step is None:
                    continue
                failed = bool(block.get("is_error"))
                step.status = "fail" if failed else "ok"
                step.detail = _claude_detail_after(step, d.get("toolUseResult"), block, failed) or step.detail
                if s.question and s.question.get("step") == step.id:
                    s.question = None
                if s.approval and s.approval.get("tool_use_id") in (step.id, None):
                    s.approval = None
                self._dirty = True
                if s.state in ("waiting", "asking") and not s.question and not s.approval:
                    self._set_state(s, "working", when)
            elif t == "text":
                self._claude_prompt(s, d, block.get("text") or "", when)

    def _claude_prompt(self, s: Session, d: dict, text: str, when: float) -> None:
        text = text.strip()
        if not text or d.get("isMeta") or d.get("isCompactSummary"):
            return
        if text.startswith("[Request interrupted"):
            for st in s.steps:
                if st.status == "run":
                    st.status = "stop"
            s.question = s.approval = None
            s.summary = "Stopped."
            self._set_state(s, "idle", when)
            return
        if text.startswith("<") and re.match(r"<(command-|local-command|bash-|system-reminder|task-notification)",
                                               text):
            return
        self._new_turn(s, text, when)

    # --- Codex ------------------------------------------------------------------------------------

    def _codex(self, f: _File, d: dict) -> None:
        kind = d.get("type")
        p = d.get("payload") or {}
        when = _when(d.get("timestamp")) or time.time()
        with self._lock:
            sid = f.key.split(":", 1)[1] if f.key else ""
            if kind == "session_meta":
                sid = p.get("id") or p.get("session_id") or sid
            if not sid:
                m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", f.path)
                sid = m.group(1) if m else os.path.basename(f.path)
            s = self._session(f, "codex", sid)
            s.updated = max(s.updated, when)
            if kind == "session_meta":
                s.cwd = p.get("cwd") or s.cwd
                s.where = p.get("originator") or p.get("source") or s.where
                return
            if kind == "turn_context":
                s.cwd = p.get("cwd") or s.cwd
                return
            t = p.get("type")
            if kind == "event_msg":
                if t == "task_started":
                    if s.state not in ("thinking", "working"):
                        self._new_turn(s, s.prompt, when)
                elif t == "user_message":
                    self._new_turn(s, str(p.get("message") or ""), when)
                elif t == "agent_message":
                    s.summary = _clip(str(p.get("message") or ""), 600)
                    self._dirty = True
                elif t == "task_complete":
                    for st in s.steps:
                        if st.status == "run":
                            st.status = "ok"
                    if p.get("last_agent_message"):
                        s.summary = _clip(str(p["last_agent_message"]), 600)
                    s.approval = s.question = None
                    self._set_state(s, "done", when)
                elif t == "turn_aborted":
                    for st in s.steps:
                        if st.status == "run":
                            st.status = "stop"
                    s.summary = "Stopped."
                    self._set_state(s, "idle", when)
                elif t in ("exec_approval_request", "apply_patch_approval_request", "request_user_input"):
                    cmd = p.get("command")
                    target = _clip(" ".join(cmd) if isinstance(cmd, list) else str(cmd or p.get("reason") or ""), 80)
                    s.question = {"text": "Codex is waiting for your approval" + (f": {target}" if target else "."),
                                  "options": [], "step": p.get("call_id") or ""}
                    self._set_state(s, "asking", when)
                elif t == "error":
                    s.summary = _clip(str(p.get("message") or "Something went wrong."), 300)
                    self._set_state(s, "failed", when)
                elif t == "item_completed":
                    self._codex_item(s, p.get("item") or {}, when)
            elif kind == "response_item":
                if t in ("function_call", "custom_tool_call", "local_shell_call"):
                    self._codex_call(s, p, when)
                elif t in ("function_call_output", "custom_tool_call_output"):
                    step = self._step(s, p.get("call_id") or "")
                    if step is not None and step.status == "run":
                        text = _codex_text(p.get("output"))
                        step.status = "fail" if re.search(r"(?i)\b(error|failed|exit code [1-9])", text[:200]) \
                            and "Script completed" not in text[:60] else "ok"
                        if step.detail and step.detail.get("kind") == "bash" and text:
                            step.detail["out"] = _tail_lines(text, 6)
                        self._dirty = True
                elif t == "message" and p.get("role") == "assistant":
                    text = _codex_text(p.get("content"))
                    if text.strip():
                        s.summary = _clip(text.strip(), 600)
                        self._dirty = True

    def _codex_call(self, s: Session, p: dict, when: float) -> None:
        name = p.get("name") or "tool"
        raw = p.get("arguments") if p.get("arguments") is not None else p.get("input")
        args = raw
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except ValueError:
                args = raw
        verb, target, detail = _codex_label(name, args)
        self._add_step(s, Step(id=p.get("call_id") or p.get("id") or "", verb=verb, target=target,
                               started=when, detail=detail))
        if s.state not in ("waiting", "asking"):
            self._set_state(s, "working", when)

    def _codex_item(self, s: Session, item: dict, when: float) -> None:
        t = item.get("type")
        if t == "UserMessage":
            text = _codex_text(item.get("content"))
            words = _clip(_user_words(text), 400)
            if words and words != s.prompt:
                s.prompt = words
                self._dirty = True
            return
        if t == "AgentMessage":
            text = _codex_text(item.get("content"))
            if text.strip():
                s.summary = _clip(text.strip(), 600)
                self._dirty = True
            return
        ident = item.get("id") or ""
        if t == "CommandExecution":
            cmd = item.get("command")
            if isinstance(cmd, list):
                cmd = cmd[-1] if len(cmd) >= 3 and cmd[1] in ("-lc", "-c") else " ".join(cmd)
            code = str(item.get("exit_code"))
            ok = code in ("0", "None") and item.get("status") != "failed"
            stopped = code in ("-1", "130", "137", "143", "-9", "-15")     # a server or watcher that was stopped
            detail = {"kind": "bash", "cmd": _clip(str(cmd or ""), 300), "ok": ok, "stopped": stopped,
                      "out": _tail_lines(str(item.get("aggregated_output") or item.get("stdout") or ""), 6)}
            verb, target = "Run", _clip(_first_line(str(cmd or "")), 70)
        elif t == "FileChange":
            changes = item.get("changes") or {}
            path = next(iter(changes), "")
            ch = changes.get(path) or {}
            detail = _diff_from_unified(path, ch.get("unified_diff") or "") if ch.get("unified_diff") else \
                _detail_code(path, ch.get("content") or "", 1, added=True)
            verb = {"add": "Write", "delete": "Delete"}.get(ch.get("type"), "Edit")
            target = os.path.basename(path) + (f" +{len(changes) - 1}" if len(changes) > 1 else "")
            ok = item.get("status") != "failed"
        elif t == "McpToolCall":
            args = item.get("arguments") or {}
            title = args.get("title") if isinstance(args, dict) else ""
            verb, target = _pretty_tool(item.get("tool") or "tool"), _clip(title or item.get("server") or "", 70)
            detail = {"kind": "text", "text": _clip(title or json.dumps(args)[:300], 300)}
            ok = item.get("status") != "failed"
        elif t == "Extension" and item.get("kind") == "web.search":
            verb, target = "Web", _clip(item.get("query") or "", 70)
            detail = {"kind": "search", "query": target, "hits": []}
            ok = True
        else:
            return
        step = self._step(s, ident)
        if step is None:
            step = Step(id=ident, verb=verb, target=target, started=when, detail=detail)
            self._add_step(s, step)
        step.status = "ok" if ok else "stop" if (detail or {}).get("stopped") else "fail"
        step.verb, step.target, step.detail = verb, target or step.target, detail or step.detail
        self._dirty = True

    # --- hooks (agent_hooks.py) -------------------------------------------------------------------

    def ingest_hook(self, payload: dict) -> None:
        """A Claude Code hook event (agent_hooks): instant state, the terminal it runs in, approvals."""
        event = payload.get("hook_event_name") or ""
        sid = payload.get("session_id") or ""
        if not sid:
            return
        now = time.time()
        with self._lock:
            key = f"claude:{sid}"
            s = self._sessions.get(key)
            if s is None:
                s = Session(key=key, app="claude", id=sid, path=payload.get("transcript_path") or "")
                self._sessions[key] = s
            s.updated = now
            if payload.get("cwd"):
                s.cwd = payload["cwd"]
            term = {k: payload.get(k) for k in ("term_program", "bundle_id", "tty", "iterm_session_id",
                                                "term_session_id", "pid") if payload.get(k)}
            if term:
                s.term.update(term)
            path = payload.get("transcript_path")
            if path and path not in self._files and os.path.exists(path):
                self._files[path] = _File(path, "claude")
            if event == "UserPromptSubmit":
                self._new_turn(s, str(payload.get("prompt") or ""), now)
            elif event == "PermissionRequest":
                tool = payload.get("tool_name") or "Tool"
                args = payload.get("tool_input") or {}
                verb, target = _claude_label(tool, args)
                s.approval = {"id": payload.get("_mint_id") or "", "tool": tool, "verb": verb, "target": target,
                              "detail": _claude_detail_before(tool, args),
                              "always": bool(payload.get("permission_suggestions")),
                              "tool_use_id": payload.get("tool_use_id")}
                self._dirty = True
                if s.state == "waiting":
                    self._events.append(("waiting", copy.deepcopy(s)))   # a second request while waiting
                else:
                    self._set_state(s, "waiting", now)
            elif event == "Notification":
                text = str(payload.get("message") or "")
                ntype = str(payload.get("notification_type") or "")
                if "permission" in (ntype + text).lower() and not s.approval:
                    s.question = {"text": _clip(text or "Claude needs your permission.", 200), "options": [],
                                  "step": ""}
                    self._set_state(s, "asking", now)
                elif ntype == "idle_prompt" or "waiting for your input" in text.lower():
                    if s.state not in ("done", "idle"):
                        self._set_state(s, "done", now)
            elif event == "Stop":
                s.approval = s.question = None
                last = payload.get("last_assistant_message")
                if isinstance(last, str) and last.strip():
                    s.summary = _clip(last.strip(), 600)
                for st in s.steps:
                    if st.status == "run":
                        st.status = "ok"
                self._set_state(s, "done", now)
            elif event == "SessionEnd":
                s.approval = s.question = None
                if s.busy:
                    self._set_state(s, "idle", now)
            elif event == "SessionStart":
                self._dirty = True
        self._wake.set()

    def approval_closed(self, session_key: str, approval_id: str, decision: str) -> None:
        """agent_hooks answered (or gave up on) an approval."""
        with self._lock:
            s = self._sessions.get(session_key)
            if s is None or not s.approval or s.approval.get("id") != approval_id:
                return
            s.approval = None
            if s.state == "waiting":
                self._set_state(s, "working" if decision in ("allow", "always") else "thinking", time.time())
            self._dirty = True
        self._wake.set()


# --- labels and details ---------------------------------------------------------------------------------

def _claude_label(name: str, args: dict) -> tuple[str, str]:
    verb = CLAUDE_TOOLS.get(name) or _pretty_tool(name)
    a = args if isinstance(args, dict) else {}
    path = a.get("file_path") or a.get("notebook_path") or a.get("path") or ""
    if name in ("Bash", "BashOutput"):
        target = a.get("description") or _first_line(a.get("command") or "")
    elif name in ("Grep", "Glob"):
        target = a.get("pattern") or ""
    elif name in ("WebFetch",):
        target = re.sub(r"^https?://(www\.)?", "", a.get("url") or "")
    elif name == "WebSearch":
        target = a.get("query") or ""
    elif name in ("Task", "Agent"):
        target = a.get("description") or a.get("subagent_type") or ""
    elif name == "TodoWrite":
        todos = a.get("todos") or []
        doing = [t.get("activeForm") or t.get("content") for t in todos
                 if isinstance(t, dict) and t.get("status") == "in_progress"]
        target = doing[0] if doing else f"{len(todos)} to-dos"
    elif name == "AskUserQuestion":
        qs = a.get("questions") or []
        target = (qs[0].get("header") or qs[0].get("question") or "") if qs and isinstance(qs[0], dict) else ""
    elif name == "Skill":
        target = a.get("skill") or ""
    elif path:
        target = os.path.basename(str(path))
    else:
        target = next((str(v) for v in a.values() if isinstance(v, str) and v.strip()), "")
    return verb, _clip(_first_line(str(target)), 70)


def _claude_detail_before(name: str, args: dict) -> dict | None:
    """Something to show while the tool runs (or waits for approval)."""
    a = args if isinstance(args, dict) else {}
    path = str(a.get("file_path") or a.get("notebook_path") or "")
    if name in ("Edit", "MultiEdit"):
        edits = a.get("edits") if name == "MultiEdit" else [a]
        lines = []
        for e in (edits or [])[:3]:
            if not isinstance(e, dict):
                continue
            lines += [("-", None, x) for x in str(e.get("old_string") or "").splitlines()[:6]]
            lines += [("+", None, x) for x in str(e.get("new_string") or "").splitlines()[:6]]
        return {"kind": "diff", "file": os.path.basename(path), "path": path, "lines": lines[:14]}
    if name == "Write":
        return _detail_code(path, str(a.get("content") or ""), 1, added=True)
    if name in ("Bash",):
        return {"kind": "bash", "cmd": _clip(str(a.get("command") or ""), 400), "out": [], "ok": None}
    if name == "Read":
        return {"kind": "code", "file": os.path.basename(path), "path": path, "lines": []}
    if name in ("Grep", "Glob"):
        return {"kind": "search", "query": str(a.get("pattern") or ""), "hits": []}
    if name in ("WebFetch", "WebSearch"):
        return {"kind": "search", "query": str(a.get("url") or a.get("query") or ""), "hits": []}
    if name in ("Task", "Agent"):
        return {"kind": "text", "text": _clip(str(a.get("prompt") or a.get("description") or ""), 300)}
    if name == "TodoWrite":
        todos = [t for t in (a.get("todos") or []) if isinstance(t, dict)]
        return {"kind": "todos", "items": [(t.get("status") or "", _clip(t.get("content") or "", 70))
                                           for t in todos[:7]]}
    return None


def _claude_detail_after(step: Step, result, block: dict, failed: bool) -> dict | None:
    detail = dict(step.detail or {})
    kind = detail.get("kind")
    text = _codex_text(block.get("content"))
    if kind == "bash" or step.verb == "Run":
        out = ""
        if isinstance(result, dict):
            out = (result.get("stdout") or "") + ("\n" + result["stderr"] if result.get("stderr") else "")
        out = out or text
        detail.update({"kind": "bash", "out": _tail_lines(out, 6), "ok": not failed})
        detail.setdefault("cmd", step.target)
        return detail
    if kind == "code" and isinstance(result, dict) and isinstance(result.get("file"), dict):
        f = result["file"]
        return _detail_code(f.get("filePath") or detail.get("path") or "", f.get("content") or "",
                            int(f.get("startLine") or 1))
    if kind == "diff" and isinstance(result, dict) and result.get("structuredPatch"):
        lines = []
        for hunk in result["structuredPatch"][:3]:
            old_n, new_n = int(hunk.get("oldStart") or 1), int(hunk.get("newStart") or 1)
            for raw in hunk.get("lines") or []:
                sign, body = (raw[:1], raw[1:]) if raw[:1] in "+- " else (" ", raw)
                if sign == "-":
                    lines.append(("-", old_n, body))
                    old_n += 1
                elif sign == "+":
                    lines.append(("+", new_n, body))
                    new_n += 1
                else:
                    lines.append((" ", new_n, body))
                    old_n += 1
                    new_n += 1
        detail["lines"] = _focus_diff(lines)
        return detail
    if kind == "search":
        detail["hits"] = _tail_lines(text, 5, head=True)
        return detail
    if failed and text:
        return {"kind": "text", "text": _clip(text, 300), "error": True}
    return None


def _codex_label(name: str, args) -> tuple[str, str, dict | None]:
    a = args if isinstance(args, dict) else {}
    if name in ("shell", "exec_command", "local_shell", "container.exec") or "cmd" in a or "command" in a:
        cmd = a.get("cmd") or a.get("command") or ""
        if isinstance(cmd, list):
            cmd = cmd[-1] if len(cmd) >= 3 and cmd[1] in ("-lc", "-c") else " ".join(cmd)
        return "Run", _clip(_first_line(str(cmd)), 70), {"kind": "bash", "cmd": _clip(str(cmd), 300), "out": [],
                                                         "ok": None}
    if name == "apply_patch":
        patch = args if isinstance(args, str) else str(a.get("input") or a.get("patch") or "")
        m = re.search(r"\*\*\* (?:Update|Add|Delete) File: (.+)", patch)
        path = m.group(1).strip() if m else ""
        return "Edit", os.path.basename(path), _diff_from_unified(path, patch)
    if name == "exec" and isinstance(args, str):
        m = re.search(r"exec_command\(\{\s*cmd:\s*\"((?:[^\"\\]|\\.)*)\"", args)
        if m:
            cmd = m.group(1).encode().decode("unicode_escape", "ignore")
            return "Run", _clip(_first_line(cmd), 70), {"kind": "bash", "cmd": _clip(cmd, 300), "out": [],
                                                        "ok": None}
        return "Run", _clip(_first_line(args), 70), {"kind": "text", "text": _clip(args, 300)}
    title = a.get("title") if isinstance(a.get("title"), str) else ""
    return _pretty_tool(name), _clip(title, 70), ({"kind": "text", "text": _clip(title, 300)} if title else None)


def _detail_code(path: str, content: str, start: int, added: bool = False) -> dict:
    lines = []
    for i, line in enumerate(content.splitlines()[:12]):
        m = re.match(r"^\s*(\d+)[\t→](.*)$", line)          # Read results come numbered ("  12→text")
        if m:
            lines.append(("+" if added else " ", int(m.group(1)), m.group(2)))
        else:
            lines.append(("+" if added else " ", start + i, line))
    return {"kind": "code", "file": os.path.basename(path), "path": path, "lines": lines, "added": added}


def _diff_from_unified(path: str, patch: str) -> dict:
    lines, old_n, new_n = [], 1, 1
    for raw in patch.splitlines():
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)", raw)
        if m:
            old_n, new_n = int(m.group(1)), int(m.group(2))
            continue
        if raw.startswith(("***", "---", "+++", "diff ", "index ")):
            continue
        sign, body = raw[:1], raw[1:]
        if sign == "-":
            lines.append(("-", old_n, body))
            old_n += 1
        elif sign == "+":
            lines.append(("+", new_n, body))
            new_n += 1
        elif sign == " ":
            lines.append((" ", new_n, body))
            old_n += 1
            new_n += 1
    return {"kind": "diff", "file": os.path.basename(path), "path": path, "lines": _focus_diff(lines)}


def _focus_diff(lines: list) -> list:
    """At most 14 lines, around the first change."""
    if len(lines) <= 14:
        return lines
    first = next((i for i, x in enumerate(lines) if x[0] in "+-"), 0)
    start = max(0, min(first - 2, len(lines) - 14))
    return lines[start:start + 14]


def _pretty_tool(name: str) -> str:
    name = str(name)
    if name.startswith("mcp__"):
        name = name.split("__")[-1]
    name = re.sub(r"[_\-]+", " ", name).strip()
    return _clip(name[:1].upper() + name[1:], 18) if name else "Tool"


def _codex_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(str(c.get("text") or c.get("content") or "") if c.get("type") != "image" else "")
            else:
                parts.append(str(c))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


def _tail_lines(text: str, n: int, head: bool = False) -> list:
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", str(text or ""))       # no terminal colour codes
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    lines = [ln[:160] for ln in lines if ln.strip() and not re.fullmatch(r"[\r\s#=O\-]*\d*\.?\d*%?", ln)]
    return lines[:n] if head else lines[-n:]


def _user_words(text: str) -> str:
    """What the user typed, without the context blocks apps wrap around it (<in-app-browser-context ...>,
    <system-reminder>, "# Files mentioned by the user" ...)."""
    text = str(text or "")
    text = re.sub(r"<([a-zA-Z][\w\-]*)(\s[^>]*)?>.*?</\1>", " ", text, flags=re.S)
    text = re.sub(r"<[a-zA-Z][\w\-]*(\s[^>]*)?/?>", " ", text)
    text = re.sub(r"(?ms)^#+ Files mentioned by the user:.*?(?=^#+ My request|\Z)", "", text)
    text = re.sub(r"(?m)^\s*#+ My request[^\n]*\n", "", text)
    return text.strip()


def _first_raw_line(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            line = fh.readline(MAX_LINE)
        return line.rstrip(b"\n") if line.endswith(b"\n") else b""
    except OSError:
        return b""


def _first_line(text: str) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()
    return ""


def _clip(text, n: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip() if n < 200 else str(text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _when(stamp) -> float:
    if not stamp:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


watcher = Watcher()
start = watcher.start
sessions = watcher.sessions
get = watcher.get
on_change = watcher.on_change
on_event = watcher.on_event
ingest_hook = watcher.ingest_hook
