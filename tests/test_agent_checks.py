"""Checks on coding agents' work: agent_watch reads test runs, edits and risky steps from Claude Code / Codex logs,
agent_checks turns them into answers and the fix loop's decisions, agent_history keeps a week of requests."""
import json
import time

import pytest

try:
    from mint.tools import agent_checks, agent_history, agent_watch
except ImportError:
    from mint import agent_checks, agent_history, agent_watch


PYTEST_FAIL = """============================= test session starts ==============================
collected 3 items

tests/test_math.py .F.                                                   [100%]

=================================== FAILURES ===================================
___________________________________ test_add ___________________________________
    def test_add():
>       assert add(1, 2) == 3
E       assert -1 == 3
tests/test_math.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_math.py::test_add - assert -1 == 3
========================= 1 failed, 2 passed in 0.03s ==========================
"""
PYTEST_PASS = "tests/test_math.py ...  [100%]\n============================== 3 passed in 0.02s ===============================\n"


class _Prefs:
    def __init__(self, **values):
        self.values = {"agent_checks": True, "agent_fix_loop": True, **values}

    def get(self, key):
        return self.values.get(key)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_history, "PATH", str(tmp_path / "agent-history.json"))
    monkeypatch.setattr(agent_history, "_items", None)
    monkeypatch.setattr(agent_watch, "CLAUDE_LIMITS", (str(tmp_path / "claude-limits.json"),))
    prefs = _Prefs()
    monkeypatch.setattr(agent_checks, "prefs", prefs)
    monkeypatch.setattr(agent_watch, "_checks", lambda: prefs.get("agent_checks") is not False)
    monkeypatch.setattr(agent_checks, "_settled", lambda path, wait=1.5: None)
    w = agent_watch.Watcher()
    monkeypatch.setattr(agent_watch, "watcher", w)
    f = agent_watch._File(str(tmp_path / "s1.jsonl"), "claude")
    return {"w": w, "f": f, "prefs": prefs, "tmp": tmp_path, "n": [0]}


def _stamp(offset=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + offset)) + ".000Z"


def _line(world, d):
    world["w"]._claude(world["f"], {"sessionId": "s1", "cwd": "/repo", "timestamp": _stamp(), **d})


def _prompt(world, text):
    _line(world, {"type": "user", "message": {"role": "user", "content": text}})


def _tool(world, name, args):
    world["n"][0] += 1
    tid = f"t{world['n'][0]}"
    _line(world, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name,
                                                                 "input": args}], "stop_reason": "tool_use",
                                                     "usage": {"input_tokens": 10, "cache_read_input_tokens": 50_000}}})
    return tid


def _result(world, tid, text, error=False, result=None):
    _line(world, {"type": "user", "toolUseResult": result, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": error}]}})


def _bash(world, cmd, out, code=0):
    tid = _tool(world, "Bash", {"command": cmd})
    text = (f"Exit code {code}\n" if code else "") + out
    _result(world, tid, text, error=bool(code), result={"stdout": out, "stderr": "", "interrupted": False})


def _edit(world, path, old="a = 1", new="a = 2"):
    tid = _tool(world, "Edit", {"file_path": path, "old_string": old, "new_string": new})
    patch = [{"oldStart": 1, "newStart": 1, "lines": [f"-{x}" for x in old.splitlines()] +
              [f"+{x}" for x in new.splitlines()]}]
    _result(world, tid, "ok", result={"filePath": path, "structuredPatch": patch})


def _done(world, text="Done."):
    _line(world, {"type": "assistant", "message": {"content": [{"type": "text", "text": text}],
                                                    "stop_reason": "end_turn"}})


def _s(world):
    return world["w"]._sessions["claude:s1"]


def test_failed_run_is_read_from_the_output(world):
    _prompt(world, "fix add")
    _edit(world, "/repo/src/math.py")
    _bash(world, "pytest -q", PYTEST_FAIL, code=1)
    s = _s(world)
    assert s.tests["state"] == "failed"
    assert s.tests["failed"] == 1 and s.tests["total"] == 3
    assert agent_checks.test_state(s) == "failed"
    assert "tests failed" in agent_checks.test_line(s)
    assert "/repo/src/math.py" in s.turn_files


def test_change_after_a_pass_is_stale(world):
    _prompt(world, "fix add")
    _bash(world, "pytest -q", PYTEST_PASS)
    _edit(world, "/repo/src/math.py")
    s = _s(world)
    assert agent_checks.test_state(s) == "stale"
    assert "math.py" in agent_checks.test_line(s)


def test_retry_is_linked(world):
    _prompt(world, "fix add")
    _edit(world, "/repo/src/math.py")
    _bash(world, "pytest -q", PYTEST_FAIL, code=1)
    _edit(world, "/repo/src/math.py", "a = 2", "a = 3")
    _bash(world, "pytest -q", PYTEST_PASS)
    s = _s(world)
    assert s.tests["state"] == "passed"
    assert "after 1 failed run" in s.tests["line"]
    assert s.steps[-1].detail.get("note") == "fixed on try 2"


def test_risky_steps_are_flagged(world):
    _prompt(world, "ship it")
    _edit(world, "/repo/.env", "KEY=1", "KEY=2")
    _bash(world, "git push --force origin main", "")
    flags = " ".join(_s(world).flags).lower()
    assert ".env" in flags
    assert "force" in flags


def test_plan_and_context(world):
    _prompt(world, "do things")
    _tool(world, "TodoWrite", {"todos": [{"content": "a", "status": "completed"},
                                         {"content": "b", "status": "in_progress"},
                                         {"content": "c", "status": "pending"}]})
    s = _s(world)
    assert s.plan == (1, 3)
    assert s.context_tokens == 50_010
    assert s.context is None                     # no status line: the window size isn't known
    (world["tmp"] / "claude-limits.json").write_text(json.dumps({"sizes": {"s1": 200_000}}))
    agent_watch._claude_file.update(mtime=0.0, data={})
    assert agent_watch._claude_context(s) == pytest.approx(0.25, abs=0.001)


def test_fix_loop_sends_back_at_most_twice(world):
    _prompt(world, "fix add")
    _edit(world, "/repo/src/math.py")
    _bash(world, "pytest -q", PYTEST_FAIL, code=1)
    payload = {"hook_event_name": "Stop", "session_id": "s1"}
    first = agent_checks.answer(payload)
    assert first["decision"] == "block" and "tests fail" in first["reason"]
    assert agent_checks.answer(payload)["decision"] == "block"
    assert agent_checks.answer(payload) == {}            # twice is enough: the user is told instead
    assert any("still not done" in f for f in _s(world).flags)


def test_fix_loop_off_by_pref(world):
    world["prefs"].values["agent_fix_loop"] = False
    _prompt(world, "fix add")
    _edit(world, "/repo/src/math.py")
    _bash(world, "pytest -q", PYTEST_FAIL, code=1)
    assert agent_checks.answer({"hook_event_name": "Stop", "session_id": "s1"}) == {}


def test_old_failures_and_no_code_changes_are_not_held_up(world):
    _prompt(world, "run the tests and tell me")
    _bash(world, "pytest -q", PYTEST_FAIL, code=1)
    assert agent_checks.answer({"hook_event_name": "Stop", "session_id": "s1"}) == {}
    _edit(world, "/repo/src/math.py")                    # it was already failing before this change
    _bash(world, "pytest -q", PYTEST_FAIL, code=1)
    assert agent_checks.answer({"hook_event_name": "Stop", "session_id": "s1"}) == {}


def test_untested_change_is_sent_back(world):
    _prompt(world, "fix add")
    _bash(world, "pytest -q", PYTEST_PASS)
    _edit(world, "/repo/src/math.py")
    out = agent_checks.answer({"hook_event_name": "Stop", "session_id": "s1"})
    assert out["decision"] == "block" and "after the last test run" in out["reason"]


def test_commit_with_failing_tests_is_denied(world):
    _prompt(world, "fix add")
    _edit(world, "/repo/src/math.py")
    _bash(world, "pytest -q", PYTEST_FAIL, code=1)
    deny = agent_checks.answer({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                                "tool_input": {"command": "git commit -am fix"}})
    assert "deny" in deny
    ok = agent_checks.answer({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                              "tool_input": {"command": "pytest -q && git commit -am fix"}})
    assert ok == {}


def test_two_agents_one_file(world):
    _prompt(world, "edit")
    _edit(world, "/repo/src/billing.py")
    ask = agent_checks.answer({"hook_event_name": "PreToolUse", "session_id": "other", "tool_name": "Edit",
                               "tool_input": {"file_path": "/repo/src/billing.py"}})
    assert "billing.py" in ask["ask"]
    again = agent_checks.answer({"hook_event_name": "PreToolUse", "session_id": "other", "tool_name": "Edit",
                                 "tool_input": {"file_path": "/repo/src/billing.py"}})
    assert again == {}                                   # asked once per file and pair


def test_finished_request_is_recorded_and_reported(world):
    _prompt(world, "fix add")
    _edit(world, "/repo/src/math.py")
    _bash(world, "pytest -q", PYTEST_PASS)
    _done(world, "Fixed add.")
    rows = agent_history.items()
    assert len(rows) == 1
    assert rows[0]["files"] == ["/repo/src/math.py"] and rows[0]["tests"]["state"] == "passed"
    agent_history.flush()
    saved = json.loads(open(agent_history.PATH).read())
    assert saved["items"][0]["prompt"] == "fix add"
    today = agent_checks._today("")
    assert "1 request" in today and "1 file changed" in today
    md = agent_checks.recap_markdown(rows[0])
    assert "✅ Tests passed" in md


def test_codex_limits_and_context(world):
    s = agent_watch.Session(key="codex:x", app="codex", id="x")
    reset = time.time() + 3600
    agent_watch._codex_tokens(s, {"info": {"last_token_usage": {"input_tokens": 129_200},
                                           "model_context_window": 258_400},
                                  "rate_limits": {"primary": {"used_percent": 32.0, "resets_at": reset},
                                                  "secondary": {"used_percent": 13.0, "resets_at": reset}}},
                              time.time())
    assert s.context == pytest.approx(0.5)
    lim = agent_watch.limits()["codex"]
    assert lim["5h"] == 32.0 and lim["week"] == 13.0
    assert "68% of the 5-hour limit left" in agent_checks._limits()


def test_handoff_note(world):
    _prompt(world, "fix add")
    _edit(world, "/repo/src/math.py")
    _bash(world, "pytest -q", PYTEST_FAIL, code=1)
    note = agent_checks.handoff_note(_s(world))
    assert note.startswith("# Hand-off from Claude Code")
    assert "src/math.py" in note and "## Tests" in note and "assert -1 == 3" in note


def test_hook_script_end_to_end(world, tmp_path):
    """The real hook script, against a socket like Mint's: Stop is blocked, a commit denied, an edit asked."""
    import os
    import socket
    import subprocess
    import sys
    import threading
    try:
        from mint.tools import agent_hooks
    except ImportError:
        from mint import agent_hooks
    import tempfile
    sock = os.path.join(tempfile.mkdtemp(prefix="mh", dir="/tmp"), "a.sock")     # (a socket path must be short)
    settings = str(tmp_path / "settings.json")
    open(settings, "w").write(json.dumps({"agent_fix_loop": True}))
    script = agent_hooks.HOOK_SCRIPT.replace(
        'os.path.expanduser("~/Library/Application Support/Mint/agents.sock")', repr(sock)).replace(
        'os.path.expanduser("~/Library/Application Support/Mint/settings.json")', repr(settings))
    assert repr(sock) in script and repr(settings) in script
    (tmp_path / "hook.py").write_text(script)
    server = socket.socket(socket.AF_UNIX)
    server.bind(sock)
    server.listen(4)

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=agent_hooks._client, args=(conn,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()

    def run(payload):
        out = subprocess.run([sys.executable, str(tmp_path / "hook.py")], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=15)
        return json.loads(out.stdout) if out.stdout.strip() else {}

    _prompt(world, "fix add")
    _edit(world, "/repo/src/math.py")
    _bash(world, "pytest -q", PYTEST_FAIL, code=1)
    try:
        assert run({"hook_event_name": "Stop", "session_id": "s1"})["decision"] == "block"
        deny = run({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                    "tool_input": {"command": "git push"}})
        assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert run({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                    "tool_input": {"command": "ls"}}) == {}
    finally:
        server.close()
        os.unlink(sock)


def test_statusline_saves_limits_and_keeps_yours(tmp_path, monkeypatch):
    import subprocess
    import sys
    try:
        from mint.tools import agent_hooks
    except ImportError:
        from mint import agent_hooks
    script = agent_hooks.STATUS_SCRIPT.replace('os.path.expanduser("~/Library/Application Support/Mint")',
                                               repr(str(tmp_path)))
    (tmp_path / "sl.py").write_text(script)
    data = {"session_id": "s1", "rate_limits": {"five_hour": {"used_percentage": 41.2}},
            "context_window": {"used_percentage": 33, "context_window_size": 1_000_000}}
    out = subprocess.run([sys.executable, str(tmp_path / "sl.py")], input=json.dumps(data), capture_output=True,
                         text=True, timeout=10).stdout
    assert "5h 41%" in out
    saved = json.loads((tmp_path / "claude-limits.json").read_text())
    assert saved["sizes"] == {"s1": 1_000_000}
    for name, value in (("SETTINGS", tmp_path / "settings.json"), ("STATUS_STATE", tmp_path / "state.json"),
                        ("HOOK_DIR", tmp_path / "hooks"), ("STATUS_PY", tmp_path / "hooks" / "a.py"),
                        ("STATUS_SH", tmp_path / "hooks" / "mint-statusline")):
        monkeypatch.setattr(agent_hooks, name, str(value))
    (tmp_path / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "mine.sh"}}))
    agent_hooks.install_statusline()
    assert agent_hooks.statusline_installed()
    agent_hooks.uninstall_statusline()
    assert json.loads((tmp_path / "settings.json").read_text())["statusLine"]["command"] == "mine.sh"


def test_a_test_run_sent_to_the_background_isnt_running_forever(world):
    _prompt(world, "fix add")
    _bash(world, "pytest -q", PYTEST_PASS)
    tid = _tool(world, "Bash", {"command": "pytest -q", "run_in_background": True})
    assert _s(world).tests["state"] == "running"
    _result(world, tid, "Command running in background with ID: b1", result={"backgroundTaskId": "b1"})
    t = _s(world).tests
    assert t["state"] == "passed" and t["line"].startswith("3 passed")
    tid = _tool(world, "Bash", {"command": "pytest -q"})
    _result(world, tid, "Error: timed out", error=True)              # no exit code: it never finished
    assert _s(world).tests["state"] == "passed"


def test_a_long_command_is_judged_whole(world):
    _prompt(world, "fix add")
    _bash(world, "python3 - <<'EOF'\n" + "x = 1\n" * 120 + "EOF\npytest -q", PYTEST_FAIL, code=1)
    assert _s(world).tests["state"] == "failed"


def test_a_finished_agent_is_not_another_agent_on_the_file(world):
    _prompt(world, "edit")
    _edit(world, "/repo/src/billing.py")
    _done(world)
    ask = agent_checks.answer({"hook_event_name": "PreToolUse", "session_id": "other", "tool_name": "Edit",
                               "tool_input": {"file_path": "/repo/src/billing.py"}})
    assert ask == {}


def test_a_skip_put_back_clears_the_warning(world):
    _prompt(world, "fix add")
    _edit(world, "/repo/src/math.py")
    _bash(world, "pytest -q", PYTEST_FAIL, code=1)
    _edit(world, "/repo/tests/test_math.py", "def test_add():", "@pytest.mark.skip\ndef test_add():")
    assert _s(world).weakened
    _edit(world, "/repo/tests/test_math.py", "@pytest.mark.skip\ndef test_add():", "def test_add():")
    assert not _s(world).weakened
    assert not any("while the tests were failing" in f for f in _s(world).flags)
