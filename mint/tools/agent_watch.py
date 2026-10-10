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
CODEX_APPS = ("/Applications/Codex.app", os.path.expanduser("~/Applications/Codex.app"))
_installed = {"at": -1e9, "apps": {}}


def installed() -> dict:
    """{"claude": bool, "codex": bool}: is Claude Code / Codex on this Mac at all (it has a sessions folder - the
    Claude app's Code tab and the CLI both write one - or its command or app is there)? Checked once a minute."""
    now = time.monotonic()
    if now - _installed["at"] > 60:
        import shutil
        _installed["at"] = now
        _installed["apps"] = {
            "claude": os.path.isdir(os.path.dirname(CLAUDE_DIR)) and os.path.isdir(CLAUDE_DIR)
            or bool(shutil.which("claude")),
            "codex": os.path.isdir(os.path.dirname(CODEX_DIR)) or bool(shutil.which("codex"))
            or any(os.path.isdir(p) for p in CODEX_APPS)}
    return dict(_installed["apps"])
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
    # Checks on its work (agent_tests.py; the "agent_checks" pref):
    tests: dict | None = None   # the last test run: agent_tests.verdict() + at, cmd, since (files changed after), tail
    tests_before: str = ""      # this turn's first test verdict before it changed anything (old failures aren't its)
    flags: list = field(default_factory=list)       # this turn's risky steps ("changed .env", "force-push" ...)
    files: dict = field(default_factory=dict)       # path -> time it changed it (this session, newest 80)
    turn_files: list = field(default_factory=list)  # paths it changed this turn
    fails: dict = field(default_factory=dict)       # command -> failed tries in a row this turn
    weakened: str = ""          # a test edited while failing: "removed an assertion in test_x.py" (a faked pass?)
    sent_back: int = 0          # times the fix loop sent it back this turn
    plan: tuple | None = None   # (done, total) of its to-do list
    plan_items: list = field(default_factory=list)  # [(status, text)]
    context: float | None = None    # its context window's fill, 0..1 (Codex's own count; Claude from its status line)
    context_tokens: int = 0     # tokens in its context at the last answer

    @property
    def project(self) -> str:
        if self.app == "mint":
            return self.title or "Background job"
        name = os.path.basename(self.cwd.rstrip("/")) if self.cwd else ""
        return name or ("Claude Code" if self.app == "claude" else "Codex")

    @property
    def app_name(self) -> str:
        if self.app == "mint":
            return "Mint"
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
        self._gen = 0                   # bumped by every change (_dirty = True): sessions() is cached against it
        self._snap: tuple = (None, [])
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
        self._passed = threading.Event()            # set after each look (refresh() waits for one)

    # --- public -----------------------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="mint-agent-watch", daemon=True)
            self._thread.start()

    @property
    def _dirty(self) -> bool:
        return self._dirty_flag

    @_dirty.setter
    def _dirty(self, value: bool) -> None:
        self._dirty_flag = value
        if value:
            self._gen += 1

    def on_change(self, fn) -> None:
        self._change_fns.append(fn)

    def on_event(self, fn) -> None:
        self._event_fns.append(fn)

    def sessions(self) -> list[Session]:
        """The sessions to show, best first: copies, made again only when something changed (or a second passed,
        for the time limits). The notch asks several times a frame; deep-copying every time cost ~4 ms a frame -
        14% of a core with Mint idle (6 Oct)."""
        now = time.time()
        key = (self._gen, int(now))
        with self._lock:
            if self._snap[0] == key:
                return [copy.copy(s) for s in self._snap[1]]
            items = [copy.deepcopy(s) for s in self._sessions.values()
                     if s.busy or s.approval or now - s.updated < KEEP_IDLE]
            rank = {"waiting": 0, "asking": 0, "failed": 2, "working": 1, "thinking": 1, "done": 3, "idle": 4}
            # (a session quiet for a while ranks as idle, whatever it said last)
            items.sort(key=lambda s: (0 if s.approval else rank.get(s.state, 5) if s.busy or now - s.updated < KEEP
                                      else 4, -s.updated))
            self._snap = (key, items)
            return [copy.copy(s) for s in items]

    def get(self, key: str) -> Session | None:
        with self._lock:
            s = self._sessions.get(key)
            return copy.deepcopy(s) if s is not None else None

    def poke(self) -> None:
        """Look again now (a hook said something happened)."""
        self._wake.set()

    def refresh(self, timeout: float = 3.0) -> None:
        """Look again now and wait for it (an answer about the sessions should be up to date). The watcher's own
        thread does the reading: reading the same files from two threads at once would mix up their places."""
        self.start()
        self._passed.clear()
        self._wake.set()
        self._passed.wait(timeout)

    def put(self, session: Session, event: str = "") -> None:
        """A session Mint runs itself (a background job, background.py): shown like the others."""
        with self._lock:
            old = self._sessions.get(session.key)
            self._sessions[session.key] = session
            self._dirty = True
            if event and (old is None or old.state != session.state or event == "asking"):
                self._events.append((event, copy.deepcopy(session)))
        self.start()
        self.poke()

    # --- the thread -------------------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                self._pass()
            except Exception:
                log.exception("agent watch pass failed")
            self._passed.set()
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
                if s.app == "mint":
                    if not s.busy and quiet > KEEP:          # a finished job leaves the list after a while
                        del self._sessions[key]
                        self._dirty = True
                    continue
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
        if state in ("done", "failed") and s.app != "mint" and (s.turn_steps or s.prompt):
            try:
                from mint.tools import agent_history
                agent_history.record(s, when)
            except Exception:
                log.debug("agent history not saved", exc_info=True)

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
        s.flags, s.turn_files, s.fails, s.tests_before, s.weakened, s.sent_back = [], [], {}, "", "", 0
        if s.tests and s.tests.get("state") == "running":
            s.tests = {**s.tests, "state": s.tests.get("prev") or "unclear",
                       "line": s.tests.get("prev_line") or "the last test run didn't finish"}
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
        usage = message.get("usage") or {}
        if usage:
            s.context_tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                                    "cache_creation_input_tokens"))
            s.context = _claude_context(s)
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
        if name == "TodoWrite":
            _set_plan(s, [(t.get("status") or "", t.get("content") or "") for t in (args.get("todos") or [])
                          if isinstance(t, dict)])
        elif name == "Bash":
            _run_started(s, str(args.get("command") or ""), when, step.id)
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
                result = d.get("toolUseResult")
                before = step.detail or {}
                step.detail = _claude_detail_after(step, result, block, failed) or step.detail
                try:
                    _claude_checks(self, s, step, before, result, block, failed, when)
                except Exception:
                    log.debug("agent checks failed", exc_info=True)
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
                elif t == "token_count":
                    _codex_tokens(s, p, when)
                elif t == "patch_apply_end" and p.get("success", True):
                    try:
                        for path, ch in (p.get("changes") or {}).items():
                            lines = _diff_from_unified(path, (ch or {}).get("unified_diff") or "", cap=False)["lines"]
                            _edited(self, s, path, lines, when, deleted=(ch or {}).get("type") == "delete")
                    except Exception:
                        log.debug("agent checks failed", exc_info=True)
            elif kind == "response_item":
                if t in ("function_call", "custom_tool_call", "local_shell_call"):
                    self._codex_call(s, p, when)
                elif t in ("function_call_output", "custom_tool_call_output"):
                    step = self._step(s, p.get("call_id") or "")
                    if step is not None and step.status == "run":
                        text = _codex_text(p.get("output"))
                        code, out = _codex_exit(text)
                        if code is not None:
                            step.status = "ok" if code == 0 else "fail"
                        else:
                            step.status = "fail" if re.search(r"(?i)\b(error|failed|exit code [1-9])", text[:200]) \
                                and "Script completed" not in text[:60] else "ok"
                        if step.detail and step.detail.get("kind") == "bash" and text:
                            step.detail["out"] = _tail_lines(out if code is not None else text, 6)
                        try:
                            _codex_output(self, s, step, text, when)
                        except Exception:
                            log.debug("agent checks failed", exc_info=True)
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
        if name == "update_plan" and isinstance(args, dict):
            _set_plan(s, [(x.get("status") or "", x.get("step") or "") for x in (args.get("plan") or [])
                          if isinstance(x, dict)])
        elif verb == "Run" and detail and detail.get("cmd"):
            _run_started(s, detail["cmd"], when, p.get("call_id") or p.get("id") or "")
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
        try:
            if t == "CommandExecution":
                code = item.get("exit_code")
                _run_done(self, s, step, str(cmd or ""), str(item.get("aggregated_output") or item.get("stdout") or ""),
                          int(code) if isinstance(code, int) else None, bool(detail.get("stopped")), when)
            elif t == "FileChange" and ok:
                for path, ch in (item.get("changes") or {}).items():
                    lines = _diff_from_unified(path, (ch or {}).get("unified_diff") or "", cap=False)["lines"]
                    _edited(self, s, path, lines, when, deleted=(ch or {}).get("type") == "delete")
        except Exception:
            log.debug("agent checks failed", exc_info=True)
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
                if tool == "AskUserQuestion":            # a question: answerable from the notch (agent_hooks.answer)
                    qs = args.get("questions") or []
                    q = qs[0] if qs and isinstance(qs[0], dict) else {}
                    s.question = {"text": _clip(q.get("question") or "Claude has a question.", 240),
                                  "options": [_clip(o.get("label") or "", 40) for o in (q.get("options") or [])
                                              if isinstance(o, dict)][:4], "step": payload.get("tool_use_id") or ""}
                    if s.state != "asking":
                        self._set_state(s, "asking", now)
                elif s.state == "waiting":
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
            asked = s.approval.get("tool") == "AskUserQuestion"
            s.approval = None
            if asked and decision:
                s.question = None                    # answered (or turned down) from the notch
            if s.state == "waiting" or asked and decision and s.state == "asking":
                self._set_state(s, "working" if decision in ("allow", "always", "answer") else "thinking", time.time())
            self._dirty = True
        self._wake.set()


# --- checks on the work (agent_tests.py) --------------------------------------------------------------------
# What a test run really said, risky steps, files two agents changed, retries, its plan, context and usage limits.
# Read from the same logs; shown and spoken, never acted on by Mint itself (the fix loop is agent_checks + hooks).

CONFLICT = 10 * 60           # another session changed the same file this recently: worth a flag
DOCS = (".md", ".mdx", ".txt", ".rst", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".csv")
SUPPORT = os.path.expanduser("~/Library/Application Support/Mint")
CLAUDE_LIMITS = (os.path.join(SUPPORT, "claude-limits.json"),           # Mint's status line (agent_hooks)
                 os.path.expanduser("~/.dotpals/claude-limits.json"))   # or dotpals', when that's installed
LIMITS: dict = {}            # "codex" -> {"5h", "5h_resets", "week", "week_resets", "at"} (percent used, epoch s)
_claude_file = {"mtime": 0.0, "data": {}}


def _checks() -> bool:
    try:
        from mint.core import prefs
        value = prefs.get("agent_checks")
        return True if value is None else bool(value)
    except Exception:
        return True


def _flag(s: Session, text: str) -> None:
    if text and text not in s.flags:
        s.flags.append(_clip(text, 120))
        del s.flags[:-8]


def _norm(cmd: str) -> str:
    cmd = re.sub(r"\s+", " ", str(cmd or "")).strip()
    return re.sub(r"\s*(2>&1|\|\s*(tail|head|grep|less|cat|tee)\b.*)$", "", cmd)


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    return "just now" if minutes < 1 else f"{minutes} min ago"


def _set_plan(s: Session, items: list) -> None:
    s.plan_items = [(str(st), _clip(text, 120)) for st, text in items if str(text).strip()][:20]
    done = sum(1 for st, _ in s.plan_items if st == "completed")
    s.plan = (done, len(s.plan_items)) if s.plan_items else None


def _run_started(s: Session, cmd: str, when: float, step_id: str = "") -> None:
    if not cmd or not _checks():
        return
    from mint.tools import agent_tests
    if agent_tests.is_test(cmd):
        prev = s.tests or {}
        s.tests = {**prev, "state": "running", "line": "running the tests", "cmd": _clip(_first_line(cmd), 160),
                   "started": when, "step": step_id,
                   "prev": prev.get("prev", "") if prev.get("state") == "running" else prev.get("state", ""),
                   "prev_line": prev.get("prev_line", "") if prev.get("state") == "running" else prev.get("line", "")}


def _run_unread(s: Session, step: Step, why: str) -> None:
    """A test run whose result never comes to the log (sent to the background, stopped, refused): back to what the
    tests said before, not "running" forever."""
    t = s.tests
    if t and t.get("state") == "running" and t.get("step") == step.id:
        s.tests = {**t, "state": t.get("prev") or "unclear", "line": t.get("prev_line") or why}


def _run_done(w, s: Session, step: Step, cmd: str, out: str, code, interrupted: bool, when: float) -> None:
    if not _checks() or (step.detail or {}).get("checked"):
        return
    if step.detail is not None:
        step.detail["checked"] = True           # (Codex can report one command twice)
    from mint.tools import agent_tests
    flag = agent_tests.risk("Run", step.target, cmd=cmd)
    if flag:
        _flag(s, flag)
    key = _norm(cmd)
    tries = 0
    if code not in (0, None) and not interrupted:
        s.fails[key] = s.fails.get(key, 0) + 1
        if s.fails[key] == 3:
            _flag(s, f"{_clip(_first_line(cmd), 50)} failed 3 times")
    elif code == 0 and s.fails.get(key):
        tries = s.fails.pop(key) + 1
        if step.detail is not None:
            step.detail["note"] = f"fixed on try {tries}"
    started = (s.tests or {}).get("state") == "running" and (s.tests or {}).get("step") == step.id
    if not started and not agent_tests.is_test(cmd):
        return                                  # (a long command's card shows it clipped: the start knew better)
    v = agent_tests.verdict(cmd, out, code, interrupted)
    if tries and v.get("state") == "passed":
        v["line"] = f"{v.get('line') or 'passed'}, after {tries - 1} failed run{'s' if tries > 2 else ''}"
    v.update(at=when, cmd=_clip(_first_line(cmd), 160), since=[], tail=_tail_lines(out, 25),
             cut=agent_tests.piped(cmd))
    if not s.turn_files and not s.tests_before:
        s.tests_before = v.get("state", "")
    s.tests = v
    w._dirty = True


def _edited(w, s: Session, path: str, lines: list, when: float, deleted: bool = False) -> None:
    if not path or not _checks():
        return
    from mint.tools import agent_tests
    if not os.path.isabs(path) and s.cwd:
        path = os.path.normpath(os.path.join(s.cwd, path))
    name = os.path.basename(path)
    s.files[path] = when
    if len(s.files) > 80:
        del s.files[min(s.files, key=s.files.get)]
    if path not in s.turn_files:
        s.turn_files.append(path)
    flag = agent_tests.risk("Delete" if deleted else "Edit", name, path=path)
    if flag:
        _flag(s, flag)
    t = s.tests
    failing = bool(t) and (t.get("state") == "failed" or (t.get("state") == "running" and t.get("prev") == "failed"))
    code = not name.lower().endswith(DOCS)          # notes and images don't make the tests out of date
    if code and t and t.get("state") != "running" and float(t.get("at") or 0) <= when and \
            name not in t.setdefault("since", []):
        t["since"] = (t["since"] + [name])[-6:]
    if agent_tests.is_test_file(path):
        why = agent_tests.weakened(lines, path) if failing else ""
        if why:
            s.weakened = f"{why} in {name}"
            _flag(s, f"{why} in {name} while the tests were failing")
        elif s.weakened.endswith(f" in {name}") and agent_tests.weakened(
                [({"+": "-", "-": "+"}.get(sign, sign), n, text) for sign, n, text in lines], path):
            s.weakened = ""                     # it put the test back (the skip it added is gone again)
            s.flags = [f for f in s.flags if not f.endswith(f" in {name} while the tests were failing")]
    for other in w._sessions.values():
        if other is s or other.app == "mint":
            continue
        at = other.files.get(path)
        if at and 0 <= when - at < CONFLICT and (other.busy or other.approval):   # (a finished one isn't "another agent")
            _flag(s, f"{other.app_name} ({other.project}) also changed {name} {_ago(when - at)}")
            break
    w._dirty = True


def _patch_lines(result) -> list:
    """Every line of a Claude Edit / Write result (not just the 14 the card shows)."""
    if not isinstance(result, dict):
        return []
    lines = []
    for hunk in result.get("structuredPatch") or []:
        n = int(hunk.get("newStart") or 1)
        for raw in hunk.get("lines") or []:
            sign = raw[:1] if raw[:1] in "+- " else " "
            lines.append((sign, n, raw[1:] if raw[:1] in "+- " else raw))
            if sign != "-":
                n += 1
    if not lines and result.get("type") == "create" and result.get("content"):
        lines = [("+", i, x) for i, x in enumerate(str(result["content"]).splitlines()[:400], 1)]
    return lines


def _claude_checks(w, s: Session, step: Step, before: dict, result, block: dict, failed: bool, when: float) -> None:
    if not _checks():
        return
    if step.verb == "Run":
        text = _codex_text(block.get("content"))
        if text.startswith("Command running in background") or (
                isinstance(result, dict) and (result.get("backgroundTaskId") or result.get("backgroundedByUser"))):
            _run_unread(s, step, "the tests run in the background")
            return
        m = re.match(r"(?:Error: )?Exit code (-?\d+)", text)
        if failed and not m:
            _run_unread(s, step, "the test run didn't finish")
            return                              # it never ran (denied, timed out): nothing to judge
        out, interrupted = "", False
        if isinstance(result, dict):
            out = (result.get("stdout") or "") + ("\n" + result["stderr"] if result.get("stderr") else "")
            interrupted = bool(result.get("interrupted"))
        _run_done(w, s, step, str(before.get("cmd") or step.target), out or text,
                  int(m.group(1)) if m else 0, interrupted, when)
    elif step.verb in ("Edit", "Write") and not failed and before.get("path"):
        _edited(w, s, str(before["path"]), _patch_lines(result) or list(before.get("lines") or []), when)


def _codex_exit(text: str):
    """(exit code or None, output) from a Codex tool output: JSON with metadata, or "Exit code: N ... Output:"."""
    text = str(text or "")
    if text[:1] == "{":
        try:
            d = json.loads(text)
            meta = d.get("metadata") or {}
            code = meta.get("exit_code")
            return (int(code) if isinstance(code, int) else None), str(d.get("output") or "")
        except (ValueError, AttributeError):
            pass
    m = re.search(r"(?:Process exited with code|Exit code:?)\s*(-?\d+)", text[:400])
    out = text.split("Output:\n", 1)[1] if "Output:\n" in text else text
    if not m and out.lstrip()[:1] == "{":           # newer Codex: {"exit_code": 1, "output": "..."} after "Output:"
        try:
            d = json.loads(out.strip())
            if isinstance(d, dict) and ("exit_code" in d or "output" in d):
                code = d.get("exit_code")
                return (int(code) if isinstance(code, int) else None), str(d.get("output") or "")
        except ValueError:
            pass
    return (int(m.group(1)) if m else None), out


def _codex_output(w, s: Session, step: Step, text: str, when: float) -> None:
    if step.verb == "Run":
        code, out = _codex_exit(text)
        _run_done(w, s, step, str((step.detail or {}).get("cmd") or step.target), out, code, False, when)
    elif step.verb == "Edit" and step.status == "ok" and (step.detail or {}).get("path"):
        _edited(w, s, step.detail["path"], step.detail.get("lines") or [], when)


def _codex_tokens(s: Session, p: dict, when: float) -> None:
    info = p.get("info") or {}
    last = info.get("last_token_usage") or {}
    window = info.get("model_context_window")
    if last and window:
        s.context_tokens = int(last.get("input_tokens") or 0)
        s.context = max(0.0, min(1.0, s.context_tokens / float(window)))
    rl = p.get("rate_limits") or {}
    if rl and when >= (LIMITS.get("codex") or {}).get("at", 0):
        LIMITS["codex"] = _limit_pair(rl.get("primary"), rl.get("secondary"), "used_percent", when)


def _limit_pair(five, week, used_key: str, when: float) -> dict:
    out = {"at": when}
    for name, w in (("5h", five), ("week", week)):
        if isinstance(w, dict) and isinstance(w.get(used_key), (int, float)):
            resets = w.get("resets_at")
            resets = float(resets) / (1000 if resets and resets > 1e12 else 1) if isinstance(resets, (int, float)) \
                else 0.0
            if resets and resets < time.time():
                continue                 # that window has started over since: how much of the new one is used isn't known
            out[name], out[name + "_resets"] = float(w[used_key]), resets
    return out


def _claude_saved() -> dict:
    """What a Claude Code status line saved (Mint's, or dotpals'): usage limits and context window sizes."""
    best, mtime = None, 0.0
    for path in CLAUDE_LIMITS:
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        if m > mtime:
            best, mtime = path, m
    if best is None:
        return {}
    if mtime != _claude_file["mtime"]:
        try:
            with open(best, encoding="utf-8") as fh:
                data = json.load(fh)
            _claude_file.update(mtime=mtime, data=data if isinstance(data, dict) else {})
        except (OSError, ValueError):
            return _claude_file["data"]
    return _claude_file["data"]


def _claude_context(s: Session) -> float | None:
    size = (_claude_saved().get("sizes") or {}).get(s.id)
    return max(0.0, min(1.0, s.context_tokens / float(size))) if size and s.context_tokens else None


# The Claude app (Claude Code in its Code tab, and chat) samples the plan's usage about every 15 minutes while it
# runs: {"samples": [{"t": ms, "org": ..., "u": {"fh": 5-hour %, "sd": 7-day %}}]}. A status line never runs there.
CLAUDE_APP_USAGE = os.path.expanduser("~/Library/Application Support/Claude/plan-usage-history.json")
_app_usage = {"mtime": 0.0, "data": {}}


def _claude_app_usage() -> dict:
    """Claude's limits from the Claude app's own samples ({} when it isn't installed or has none). A 5-hour figure
    older than 5 hours says nothing about now, so it is left out; a sample older than a week is ignored."""
    try:
        mtime = os.path.getmtime(CLAUDE_APP_USAGE)
    except OSError:
        return {}
    if mtime != _app_usage["mtime"]:
        data = {}
        try:
            with open(CLAUDE_APP_USAGE, encoding="utf-8") as fh:
                samples = (json.load(fh) or {}).get("samples") or []
            last = max((x for x in samples if isinstance(x, dict) and isinstance(x.get("u"), dict)),
                       key=lambda x: float(x.get("t") or 0), default=None)
            if last is not None:
                data = {"at": float(last.get("t") or 0) / 1000, **{k: last["u"].get(k) for k in ("fh", "sd")}}
        except (OSError, ValueError, TypeError, AttributeError):
            data = {}
        _app_usage.update(mtime=mtime, data=data)
    data, now = _app_usage["data"], time.time()
    if not data or now - data["at"] > 7 * 86400:
        return {}
    out = {"at": data["at"], "source": "app"}
    if isinstance(data.get("fh"), (int, float)) and now - data["at"] < 5 * 3600:
        out["5h"] = float(data["fh"])
    if isinstance(data.get("sd"), (int, float)):
        out["week"] = float(data["sd"])
    return out if ("5h" in out or "week" in out) else {}


# --- usage limits: only numbers that are true now ----------------------------------------------------------
# Codex: asked live (`codex app-server`, JSON-RPC account/rateLimits/read - the numbers Codex itself shows), at most
# every LIVE_EVERY seconds while someone looks (a Settings page, the notch, a question); its session logs only
# when that can't run. Claude: what Claude Code passed to Mint's status line, or the Claude app's own samples.
# A figure older than LIMITS_FRESH (per window), or from a window that has reset since, is not shown at all.
LIMITS_FRESH = {"5h": 20 * 60, "week": 60 * 60}      # (the Claude app notes them every ~15 min)
# Claude has no live reading: the app notes its numbers only now and then, so under the rule above they were never
# shown (10 Oct: 2% / 18% noted at 11:06, nothing on screen all afternoon). A Claude figure stays until its window
# could have started over, and is shown "As of <time>" (the "at" field) - an old number, said to be old.
CLAUDE_FRESH = {"5h": 5 * 3600, "week": 24 * 3600}
LIVE_EVERY = 120.0
LIVE: dict = {}              # "codex" -> the live reading ({"5h", "5h_resets", "week", "week_resets", "at", "live"})
_live = {"wanted": 0.0, "at": -1e9, "busy": False, "failed": -1e9}


def codex_cli() -> str:
    """The codex command: the CLI, or the one inside the Codex app."""
    try:
        from mint.tools import agent_mcp
        cli = agent_mcp._cli("codex")
    except Exception:
        cli = ""
    if cli:
        return cli
    for app in CODEX_APPS:
        inside = os.path.join(app, "Contents", "Resources", "codex")
        if os.access(inside, os.X_OK):
            return inside
    return ""


def codex_live(timeout: float = 8.0) -> dict | None:
    """Codex's limits right now, from Codex itself (its app server), or None (not installed, signed in with an API
    key, too old, offline). Blocks up to `timeout` seconds: not on the main thread."""
    cli = codex_cli()
    if not cli:
        return None
    import select
    import subprocess
    try:
        proc = subprocess.Popen([cli, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except Exception:
        log.debug("codex app-server", exc_info=True)
        return None
    try:
        for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"clientInfo": {"name": "mint", "title": "Mint", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}):
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ready, _, _ = select.select([proc.stdout], [], [], max(0.05, end - time.monotonic()))
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2:
                return _codex_reading((msg.get("result") or {}).get("rateLimits"))
    except Exception:
        log.debug("codex limits", exc_info=True)
    finally:
        try:
            proc.terminate()
            proc.wait(2)
        except Exception:
            proc.kill()
    return None


def _codex_reading(rl) -> dict | None:
    if not isinstance(rl, dict):
        return None
    now = time.time()
    out = {"at": now, "live": True, "plan": rl.get("planType") or ""}
    for name, w in (("5h", rl.get("primary")), ("week", rl.get("secondary"))):
        if isinstance(w, dict) and isinstance(w.get("usedPercent"), (int, float)):
            resets = float(w.get("resetsAt") or 0)
            if resets > 1e12:
                resets /= 1000
            mins = w.get("windowDurationMins")
            key = name if not isinstance(mins, (int, float)) else ("5h" if mins <= 24 * 60 else "week")
            out[key], out[key + "_resets"] = float(w["usedPercent"]), resets
    return out if ("5h" in out or "week" in out) else None


def refresh_live(block: bool = False, force: bool = False) -> None:
    """Ask Codex again if the last reading is older than LIVE_EVERY (on a thread unless `block`); `force`: the next
    read asks whatever the last one said (Settings' Refresh limits)."""
    now = time.monotonic()
    _live["wanted"] = now
    if force:
        _live["at"] = _live["failed"] = -1e9
        return
    if _live["busy"] or now - _live["at"] < LIVE_EVERY or now - _live["failed"] < 10 * 60:
        return
    _live["busy"] = True

    def run():
        try:
            reading = codex_live()
            if reading is not None:
                LIVE["codex"] = reading
                _live["at"] = time.monotonic()
            else:                                      # (an older reading ages out by itself: _fresh)
                _live["failed"] = time.monotonic()     # not again for a while: no CLI, an API key, offline
        finally:
            _live["busy"] = False
    if block:
        run()
    else:
        threading.Thread(target=run, daemon=True, name="codex-limits").start()


def _fresh(x, now: float | None = None, keep: dict | None = None):
    """Only the windows still true: read recently enough, and not reset since. None when nothing is left."""
    if not isinstance(x, dict):
        return None
    now = time.time() if now is None else now
    age = now - float(x.get("at") or 0)
    keep = keep or LIMITS_FRESH
    out = {k: v for k, v in x.items() if k not in ("5h", "5h_resets", "week", "week_resets")}
    for name in ("5h", "week"):
        if not isinstance(x.get(name), (int, float)) or age > keep[name] or age < -60:
            continue
        resets = float(x.get(name + "_resets") or 0)
        if resets and resets < now:
            continue
        out[name], out[name + "_resets"] = float(x[name]), resets
    out["live"] = bool(x.get("live")) and age < LIVE_EVERY + 60
    return out if ("5h" in out or "week" in out) else None


def limits(live: bool = True) -> dict:
    """Usage limits that are true now, percent used: {"codex": {...}, "claude": {...}} with "5h", "week",
    "5h_resets", "week_resets" (epoch seconds), "at" and "live"; an app is left out when nothing about it is known
    for sure. Cheap (any thread): a live Codex reading is fetched in the background (`live`)."""
    if live:
        refresh_live()
    out = {}
    codex = LIVE.get("codex") or LIMITS.get("codex")
    if LIVE.get("codex") and LIMITS.get("codex") and LIMITS["codex"].get("at", 0) > LIVE["codex"]["at"] + 5:
        codex = LIMITS["codex"]                          # a Codex turn just logged newer numbers
    claude = None
    saved = _claude_saved()
    rl = saved.get("rate_limits") or {}
    if rl:
        claude = _limit_pair(rl.get("five_hour"), rl.get("seven_day"), "used_percentage",
                             float(saved.get("updatedAt") or 0) / 1000)
        claude["live"] = True
    app = _claude_app_usage()
    if app and app["at"] > float((claude or {}).get("at") or 0):
        claude = app
    for name, x in (("codex", codex), ("claude", claude)):
        x = _fresh(x, keep=CLAUDE_FRESH if name == "claude" and not (x or {}).get("live") else None)
        if x is not None:
            out[name] = x
    return out


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
        return _codex_script(args)
    title = a.get("title") if isinstance(a.get("title"), str) else ""
    return _pretty_tool(name), _clip(title, 70), ({"kind": "text", "text": _clip(title, 300)} if title else None)


def _js_string(literal: str) -> str:
    try:
        return json.loads(literal)
    except ValueError:
        return literal[1:-1].encode().decode("unicode_escape", "ignore")


def _codex_script(script: str) -> tuple[str, str, dict | None]:
    """Newer Codex runs its tools from a small script ("exec"): `tools.exec_command({"cmd": ...})`,
    `tools.apply_patch("*** Begin Patch ...")`, `tools.mcp__server__tool({...})`."""
    m = re.search(r"exec_command\(\s*(\{.*?\})\s*\)", script, re.S)
    cmd = ""
    if m:
        try:
            cmd = str(json.loads(m.group(1)).get("cmd") or "")
        except ValueError:
            q = re.search(r"\bcmd\"?\s*:\s*(\"(?:[^\"\\]|\\.)*\")", m.group(1))
            cmd = _js_string(q.group(1)) if q else ""
    if cmd:
        return "Run", _clip(_first_line(cmd), 70), {"kind": "bash", "cmd": _clip(cmd, 300), "out": [], "ok": None}
    if "apply_patch(" in script:
        q = re.search(r"(\"(?:[^\"\\]|\\.)*\*\*\* Begin Patch(?:[^\"\\]|\\.)*\")", script)
        patch = _js_string(q.group(1)) if q else script
        f = re.search(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\"]+)", patch)
        path = f.group(1).strip() if f else ""
        return "Edit", os.path.basename(path), _diff_from_unified(path, patch)
    t = re.search(r"tools\.(\w+)\(", script)
    if t:
        return _pretty_tool(t.group(1)), "", {"kind": "text", "text": _clip(script, 300)}
    return "Run", _clip(_first_line(script), 70), {"kind": "text", "text": _clip(script, 300)}


def _detail_code(path: str, content: str, start: int, added: bool = False) -> dict:
    lines = []
    for i, line in enumerate(content.splitlines()[:12]):
        m = re.match(r"^\s*(\d+)[\t→](.*)$", line)          # Read results come numbered ("  12→text")
        if m:
            lines.append(("+" if added else " ", int(m.group(1)), m.group(2)))
        else:
            lines.append(("+" if added else " ", start + i, line))
    return {"kind": "code", "file": os.path.basename(path), "path": path, "lines": lines, "added": added}


def _diff_from_unified(path: str, patch: str, cap: bool = True) -> dict:
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
    return {"kind": "diff", "file": os.path.basename(path), "path": path, "lines": _focus_diff(lines) if cap else lines}


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
