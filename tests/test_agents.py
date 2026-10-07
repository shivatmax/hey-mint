"""Claude mode: reading Claude Code / Codex session logs (agent_watch) and the approval hook (agent_hooks).

Stdlib only (no AppKit): fake session logs in a temporary folder, a temporary settings.json, and a real
round trip through the hook script and Mint's socket on a temporary path.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

try:                                    # the published layout
    from mint.tools import agent_hooks, agent_watch
except ImportError:                     # the private working copy
    from mint import agent_hooks, agent_watch


def _stamp(offset=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + offset)) + ".000Z"


def _watcher(tmp_path, monkeypatch):
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    claude.mkdir()
    codex.mkdir()
    monkeypatch.setattr(agent_watch, "CLAUDE_DIR", str(claude))
    monkeypatch.setattr(agent_watch, "CODEX_DIR", str(codex))
    return agent_watch.Watcher(), claude, codex


def _write(path, rows):
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _claude_rows(sid="s1", cwd="/srv/code/korus"):
    base = {"sessionId": sid, "cwd": cwd, "entrypoint": "cli", "isSidechain": False}
    return [
        {**base, "type": "user", "timestamp": _stamp(-20), "message": {"role": "user", "content": "round the totals"}},
        {**base, "type": "assistant", "timestamp": _stamp(-19), "message": {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": cwd + "/src/invoice.ts"}}]}},
        {**base, "type": "user", "timestamp": _stamp(-18),
         "toolUseResult": {"type": "text", "file": {"filePath": cwd + "/src/invoice.ts", "content": "const TVA = 0.196\n",
                                                    "startLine": 12}},
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "12→const TVA = 0.196"}]}},
        {**base, "type": "assistant", "timestamp": _stamp(-17), "message": {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t2", "name": "Edit",
             "input": {"file_path": cwd + "/src/invoice.ts", "old_string": "0.196", "new_string": "0.20"}}]}},
        {**base, "type": "user", "timestamp": _stamp(-16),
         "toolUseResult": {"structuredPatch": [{"oldStart": 12, "oldLines": 1, "newStart": 12, "newLines": 1,
                                                "lines": ["-const TVA = 0.196", "+const TVA = 0.20"]}]},
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
        {**base, "type": "assistant", "timestamp": _stamp(-15), "message": {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "npm test", "description": "Run tests"}}]}},
        {**base, "type": "user", "timestamp": _stamp(-14),
         "toolUseResult": {"stdout": "PASS tests/invoice.test.ts\nTests: 48 passed", "stderr": ""},
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "content": "PASS"}]}},
        {"type": "custom-title", "customTitle": "TVA fix", "sessionId": sid},
        {**base, "type": "assistant", "timestamp": _stamp(-13), "message": {"stop_reason": "end_turn", "content": [
            {"type": "text", "text": "Done: TVA is 20% and all 48 tests pass."}]}},
    ]


def test_claude_session_from_its_log(tmp_path, monkeypatch):
    w, claude, _ = _watcher(tmp_path, monkeypatch)
    (claude / "proj").mkdir()
    _write(claude / "proj" / "s1.jsonl", _claude_rows())
    w._pass()
    [s] = w.sessions()
    assert (s.app, s.project, s.title, s.state, s.where) == ("claude", "korus", "TVA fix", "done", "cli")
    assert s.prompt == "round the totals"
    assert [(st.verb, st.target, st.status) for st in s.steps] == [
        ("Read", "invoice.ts", "ok"), ("Edit", "invoice.ts", "ok"), ("Run", "Run tests", "ok")]
    assert s.steps[0].detail["kind"] == "code" and s.steps[0].detail["lines"][0][1] == 12
    assert ("-", 12, "const TVA = 0.196") in s.steps[1].detail["lines"]
    assert ("+", 12, "const TVA = 0.20") in s.steps[1].detail["lines"]
    assert s.steps[2].detail["out"][0].startswith("PASS")
    assert "48 tests pass" in s.summary


def test_claude_log_is_read_incrementally_and_a_half_line_waits(tmp_path, monkeypatch):
    w, claude, _ = _watcher(tmp_path, monkeypatch)
    (claude / "proj").mkdir()
    path = claude / "proj" / "s2.jsonl"
    rows = _claude_rows("s2")
    _write(path, rows[:2])
    w._pass()
    s = w.get("claude:s2")
    assert s.state == "working" and s.steps[-1].status == "run"
    line = json.dumps(rows[2])
    with open(path, "a") as fh:
        fh.write(line[:40])                      # the writer is mid-line
    w._pass()
    assert w.get("claude:s2").steps[-1].status == "run"
    with open(path, "a") as fh:
        fh.write(line[40:] + "\n")
    w._pass()
    assert w.get("claude:s2").steps[-1].status == "ok"


def test_interrupt_and_question(tmp_path, monkeypatch):
    w, claude, _ = _watcher(tmp_path, monkeypatch)
    (claude / "p").mkdir()
    base = {"sessionId": "s3", "cwd": "/x/app"}
    _write(claude / "p" / "s3.jsonl", [
        {**base, "type": "user", "timestamp": _stamp(-5), "message": {"content": "<system-reminder>x</system-reminder>"}},
        {**base, "type": "user", "timestamp": _stamp(-4), "message": {"content": "pick a colour"}},
        {**base, "type": "assistant", "timestamp": _stamp(-3), "message": {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {"questions": [
                {"question": "Which colour?", "header": "Colour", "options": [{"label": "Red"}, {"label": "Blue"}]}]}}]}},
    ])
    w._pass()
    s = w.get("claude:s3")
    assert s.prompt == "pick a colour"
    assert s.state == "asking" and s.question["options"] == ["Red", "Blue"]
    _write(claude / "p" / "s3.jsonl", [{**base, "type": "user", "timestamp": _stamp(-1),
                                        "message": {"content": [{"type": "text",
                                                                 "text": "[Request interrupted by user]"}]}}])
    w._pass()
    s = w.get("claude:s3")
    assert s.state == "idle" and s.question is None and s.summary == "Stopped."


def test_codex_session_from_its_rollout(tmp_path, monkeypatch):
    w, _, codex = _watcher(tmp_path, monkeypatch)
    day = codex / time.strftime("%Y/%m/%d")
    day.mkdir(parents=True)
    sid = "01a0f676-8f5d-7232-91d0-877846158718"
    _write(day / f"rollout-2026-10-01T10-00-00-{sid}.jsonl", [
        {"type": "session_meta", "timestamp": _stamp(-30), "payload": {"id": sid, "cwd": "/srv/atlas",
                                                                      "originator": "codex_cli"}},
        {"type": "event_msg", "timestamp": _stamp(-29), "payload": {"type": "task_started"}},
        {"type": "event_msg", "timestamp": _stamp(-29), "payload": {
            "type": "user_message", "message": "<ctx a='1'>noise</ctx>\nfix the timer"}},
        {"type": "response_item", "timestamp": _stamp(-28), "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "c1",
            "arguments": json.dumps({"cmd": "npm test -- timer"})}},
        {"type": "response_item", "timestamp": _stamp(-27), "payload": {
            "type": "function_call_output", "call_id": "c1", "output": "Tests: 18 passed"}},
        {"type": "event_msg", "timestamp": _stamp(-26), "payload": {"type": "item_completed", "item": {
            "type": "FileChange", "id": "f1", "status": "completed", "changes": {"/srv/atlas/src/timer.ts": {
                "type": "update", "unified_diff": "@@ -3,1 +3,1 @@\n-const LIMIT = 5\n+const LIMIT = 10\n"}}}}},
        {"type": "event_msg", "timestamp": _stamp(-25), "payload": {
            "type": "task_complete", "last_agent_message": "The timer now waits 10 s."}},
    ])
    w._pass()
    [s] = w.sessions()
    assert (s.app, s.project, s.state, s.prompt) == ("codex", "atlas", "done", "fix the timer")
    assert [(st.verb, st.status) for st in s.steps] == [("Run", "ok"), ("Edit", "ok")]
    assert s.steps[0].detail["out"] == ["Tests: 18 passed"]
    assert ("+", 3, "const LIMIT = 10") in s.steps[1].detail["lines"]
    assert s.summary == "The timer now waits 10 s."


def test_hook_events_drive_state_and_approvals():
    w = agent_watch.Watcher()
    w.ingest_hook({"hook_event_name": "UserPromptSubmit", "session_id": "h1", "cwd": "/a/b", "prompt": "go",
                   "term_program": "Apple_Terminal", "tty": "/dev/ttys004"})
    s = w.get("claude:h1")
    assert s.state == "thinking" and s.term["tty"] == "/dev/ttys004"
    w.ingest_hook({"hook_event_name": "PermissionRequest", "session_id": "h1", "_mint_id": "a1", "tool_name": "Bash",
                   "tool_input": {"command": "rm -rf build"}, "tool_use_id": "tu1",
                   "permission_suggestions": [{"rule": "Bash(rm:*)"}]})
    s = w.get("claude:h1")
    assert s.state == "waiting" and s.approval["detail"]["cmd"] == "rm -rf build" and s.approval["always"]
    w.approval_closed("claude:h1", "a1", "allow")
    s = w.get("claude:h1")
    assert s.approval is None and s.state == "working"
    w.ingest_hook({"hook_event_name": "Stop", "session_id": "h1", "last_assistant_message": "All done."})
    s = w.get("claude:h1")
    assert s.state == "done" and s.summary == "All done."


def test_user_words_drop_context_blocks():
    assert agent_watch._user_words('<in-app-browser-context source="x">\nTab\n</in-app-browser-context>\nfix it') \
        == "fix it"
    assert agent_watch._user_words("# Files mentioned by the user:\n\n## a.png: /t/a.png\n\n## My request for "
                                   "Codex:\nmake it blue\n") == "make it blue"


# --- settings.json: only Mint's own entries, a backup first -----------------------------------------------

def test_settings_merge_keeps_everything_else(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    other = {"type": "command", "command": "node other-hook.js", "timeout": 5}
    original = {"model": "opus", "hooks": {"Stop": [{"hooks": [other]}], "PreToolUse": [{"hooks": [other]}]}}
    settings.write_text(json.dumps(original))
    monkeypatch.setattr(agent_hooks, "SETTINGS", str(settings))
    monkeypatch.setattr(agent_hooks, "HOOK_DIR", str(tmp_path / "hooks"))
    monkeypatch.setattr(agent_hooks, "HOOK_SH", str(tmp_path / "hooks" / "mint-agent-hook"))
    monkeypatch.setattr(agent_hooks, "HOOK_PY", str(tmp_path / "hooks" / "mint_agent_hook.py"))
    assert not agent_hooks.installed()
    said = agent_hooks.install()
    assert "Connected" in said and "backup" in said
    assert agent_hooks.installed()
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"
    assert data["hooks"]["PreToolUse"][0] == {"hooks": [other]}           # theirs first, untouched
    assert "Bash" in data["hooks"]["PreToolUse"][1]["matcher"].split("|")  # Mint's guard (shell commands)
    assert data["hooks"]["Stop"][0] == {"hooks": [other]} and len(data["hooks"]["Stop"]) == 2
    assert data["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] >= 120
    assert agent_hooks.install() == "Already connected."                 # idempotent
    assert len(json.loads(settings.read_text())["hooks"]["Stop"]) == 2
    assert list(tmp_path.glob("settings.json.mint-backup-*"))
    assert os.access(tmp_path / "hooks" / "mint-agent-hook", os.X_OK)
    agent_hooks.uninstall()
    assert json.loads(settings.read_text()) == original


def test_settings_left_alone_when_not_json(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json")
    monkeypatch.setattr(agent_hooks, "SETTINGS", str(settings))
    assert "isn't valid JSON" in agent_hooks.install()
    assert settings.read_text() == "{ not json"


# --- a real round trip: the hook script, the socket, a click ---------------------------------------------

@pytest.mark.skipif(sys.platform != "darwin", reason="getpeereid / AF_UNIX path on macOS")
def test_hook_script_round_trip(tmp_path, monkeypatch):
    sock = f"/tmp/mint-test-{os.getpid()}.sock"
    monkeypatch.setattr(agent_hooks, "SUPPORT", str(tmp_path))
    monkeypatch.setattr(agent_hooks, "SOCK", sock)
    monkeypatch.setattr(agent_hooks, "_started", False)
    monkeypatch.setattr(agent_hooks, "installed", lambda: False)
    script = tmp_path / "hook.py"
    script.write_text(agent_hooks.HOOK_SCRIPT.replace(
        'os.path.expanduser("~/Library/Application Support/Mint/agents.sock")', repr(sock)))
    agent_hooks.start()
    for _ in range(50):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    request = {"hook_event_name": "PermissionRequest", "session_id": "rt1", "tool_name": "Bash",
               "tool_input": {"command": "npm test"}, "tool_use_id": "tu9",
               "permission_suggestions": [{"rule": "Bash(npm test:*)", "description": "npm test"}]}
    results = {}
    for decision in ("allow", "always", "deny"):
        proc = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                text=True)
        proc.stdin.write(json.dumps(request))
        proc.stdin.close()
        for _ in range(60):
            if agent_hooks.pending("claude:rt1"):
                break
            time.sleep(0.05)
        assert agent_hooks.rule_for("claude:rt1") == "Bash(npm test:*)"
        threading.Timer(0.05, agent_hooks.decide, args=("claude:rt1", decision)).start()
        results[decision] = json.loads(proc.stdout.read())["hookSpecificOutput"]["decision"]
        proc.wait(timeout=5)
    assert results["allow"] == {"behavior": "allow"}
    assert results["always"]["updatedPermissions"] == {"add": ["Bash(npm test:*)"], "remove": []}
    assert results["deny"]["behavior"] == "deny"
    os.unlink(sock)
    quiet = subprocess.run([sys.executable, str(script)], input=json.dumps(request), capture_output=True, text=True,
                           timeout=5)
    assert quiet.returncode == 0 and quiet.stdout == ""              # no Mint: no answer, Claude Code asks itself


# --- remote control: typing into sessions, Telegram alerts and answers -----------------------------------

try:
    from mint.tools import agent_remote
except ImportError:
    from mint import agent_remote


def _session(**kw):
    base = dict(key="claude:r1", app="claude", id="r1", cwd="/srv/code/korus", where="cli", state="done",
                updated=time.time(), since=time.time())
    base.update(kw)
    return agent_watch.Session(**base)


def test_route_by_where_it_runs(monkeypatch):
    monkeypatch.setattr(agent_remote, "_terminal_of", lambda s: dict(s.term))
    assert agent_remote.route(_session(where="claude-desktop")) == "claude-app"
    assert agent_remote.route(_session(app="codex", where="codex_work_desktop")) == "codex-app"
    assert agent_remote.route(_session(term={"tty": "/dev/ttys004", "term_program": "Apple_Terminal"})) == "terminal"
    assert agent_remote.route(_session(term={"tty": "/dev/ttys009", "term_program": "iTerm.app"})) == "iterm"
    assert agent_remote.route(_session(term={"term_program": "vscode"})) == ""


def test_applescript_strings_are_escaped():
    script = agent_remote.terminal_script("/dev/ttys004", 'say "hi" \\ $HOME')
    assert 'do script "say \\"hi\\" \\\\ $HOME" in t' in script
    assert 'if tty of t is "/dev/ttys004"' in script
    assert 'write text "x\\"y"' in agent_remote.iterm_script("/dev/ttys1", 'x"y')


def test_send_refuses_secrets_and_unknown_hosts(monkeypatch):
    s = _session(term={"term_program": "WarpTerminal"})
    monkeypatch.setattr(agent_watch, "get", lambda key: s)
    monkeypatch.setattr(agent_remote, "_terminal_of", lambda s: dict(s.term))
    assert "can't type" in agent_remote.send("claude:r1", "push")
    assert agent_remote.send("claude:r1", "") == "Nothing to send."


class _FakeBridge:
    def __init__(self, read_only=False):
        self.sent, self.audits, self.read_only = [], [], read_only
        self.agent_msgs, self.agent_btns, self.agent_follow = {}, {}, {}
        self._state = {"user_id": 1}
        self.bot = object()
        self._n = 0

    def setting(self, key):
        return {"telegram_read_only": self.read_only, "agent_telegram": "always"}.get(key)

    def _keep(self, table, value):
        self._n += 1
        table[str(self._n)] = value
        return str(self._n)

    def html(self, text, markup=None, **extra):
        self.sent.append((text, markup))
        return {"message_id": 100 + len(self.sent)}

    def reply(self, text):
        self.sent.append((text, None))

    def audit(self, *args):
        self.audits.append(args)

    def _action(self, *_):
        pass


def test_telegram_alert_and_allow(monkeypatch):
    try:
        from mint.app import telegram_agents
    except ImportError:
        from mint import telegram_agents
    bridge = _FakeBridge()
    s = _session(state="waiting", approval={"id": "a9", "tool": "Bash", "verb": "Run", "target": "Run tests",
                                            "detail": {"kind": "bash", "cmd": "npm test"}})
    monkeypatch.setattr(agent_hooks, "rule_for", lambda key: "Bash(npm test:*)")
    monkeypatch.setattr(agent_remote, "route", lambda s: "terminal")
    telegram_agents.alert(bridge, "waiting", s)
    text, markup = bridge.sent[-1]
    assert "wants to run" in text and "npm test" in text
    labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
    assert labels[:3] == ["✅ Allow", "♾ Always", "⛔ Deny"] and "💬 Reply" in labels
    assert bridge.agent_msgs[101] == "claude:r1"
    data = markup["inline_keyboard"][0][0]["callback_data"]
    decided = []
    monkeypatch.setattr(agent_watch, "get", lambda key: s)
    monkeypatch.setattr(agent_hooks, "pending", lambda key: True)
    monkeypatch.setattr(agent_hooks, "decide", lambda key, d: decided.append((key, d)) or True)
    name, _, arg = data.partition(":")
    toast, act, used = telegram_agents.plan(bridge, name, arg, {})
    act()
    assert decided == [("claude:r1", "allow")] and toast == "✅ Allowed"
    ro = _FakeBridge(read_only=True)
    ro.agent_btns = bridge.agent_btns
    assert telegram_agents.plan(ro, name, arg, {})[1] is None          # read-only: never approves


def test_telegram_reply_is_typed_into_the_session(monkeypatch):
    try:
        from mint.app import telegram_agents
    except ImportError:
        from mint import telegram_agents
    bridge = _FakeBridge()
    bridge.agent_msgs[55] = "claude:r1"
    typed = []
    monkeypatch.setattr(agent_remote, "send", lambda key, text: typed.append((key, text)) or "Sent to it.")
    monkeypatch.setattr(agent_watch, "get", lambda key: _session())
    runs = []
    monkeypatch.setattr(sys.modules[telegram_agents.__name__.replace("telegram_agents", "telegram")], "_later",
                        lambda fn, *a: runs.append(fn(*a)))
    assert telegram_agents.on_reply(bridge, {"reply_to_message": {"message_id": 55}}, "push", 3.0)
    assert typed == [("claude:r1", "push")]
    assert not telegram_agents.on_reply(bridge, {"reply_to_message": {"message_id": 9}}, "push", 3.0)
    ro = _FakeBridge(read_only=True)
    ro.agent_msgs[55] = "claude:r1"
    assert telegram_agents.on_reply(ro, {"reply_to_message": {"message_id": 55}}, "push", 3.0)
    assert typed == [("claude:r1", "push")] and "Read-only" in ro.sent[-1][0]
