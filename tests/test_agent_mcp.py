"""Mint's read-only MCP server about coding agents: the JSON-RPC handshake, the tools, and check_my_work's verdicts
(the same evidence the fix loop uses), with sessions made up in the test - never this Mac's real logs."""
import io
import json
import time

import pytest

try:
    from mint.tools import agent_checks, agent_mcp, agent_watch
except ImportError:
    from mint import agent_checks, agent_mcp, agent_watch


@pytest.fixture
def sessions(monkeypatch, tmp_path):
    items = []
    monkeypatch.setattr(agent_mcp, "_sessions", lambda: items)
    monkeypatch.setattr(agent_watch, "CLAUDE_LIMITS", (str(tmp_path / "none.json"),))
    monkeypatch.chdir(tmp_path)
    return items, tmp_path


def _session(cwd, **kw):
    now = time.time()
    s = agent_watch.Session(key=f"codex:{kw.pop('id', 'abc12345')}", app="codex", id="abc12345", cwd=str(cwd),
                            state="working", since=now, updated=now)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _call(name, **args):
    reply = agent_mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                              "params": {"name": name, "arguments": args}})
    assert reply["id"] == 7 and not reply["result"]["isError"]
    return reply["result"]["content"][0]["text"]


def test_handshake_and_tool_list():
    init = agent_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["capabilities"]["tools"] is not None
    assert init["result"]["serverInfo"]["name"] == "mint"
    assert agent_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = agent_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"check_my_work", "test_status", "agents_now", "recap", "today", "risky_steps", "handoff_note",
            "usage_limits"} <= names
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    bad = agent_mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "rm_rf"}})
    assert bad["error"]["code"] == -32602
    assert agent_mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601


def test_check_my_work_finds_this_folders_session(sessions):
    items, here = sessions
    items.append(_session("/elsewhere", id="other999"))
    items.append(_session(here, turn_files=[str(here / "calc.py")],
                          tests={"state": "failed", "line": "1 of 2 failed", "reason": "assert 0.30000000000000004 == 0.3",
                                 "at": time.time() - 30, "since": ["calc.py"], "cmd": "pytest -q"}))
    text = _call("check_my_work")
    assert "abc12345"[-8:] in text and "Not done yet" in text and "calc.py" in text


def test_check_my_work_ready_after_a_pass(sessions):
    items, here = sessions
    items.append(_session(here, turn_files=[str(here / "calc.py")],
                          tests={"state": "passed", "line": "2 passed", "at": time.time(), "since": []}))
    assert "Looks ready" in _call("check_my_work")
    assert "2 passed" in _call("test_status")


def test_no_session_and_a_named_project(sessions):
    items, here = sessions
    assert "No Claude Code or Codex session" in _call("check_my_work")
    items.append(_session("/srv/korus", flags=["force-push"]))
    assert "force-push" in _call("risky_steps", project="korus")


def test_stdio_loop():
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "not json",
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})]
    out = io.StringIO()
    agent_mcp.serve(io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert replies[1]["error"]["code"] == -32700
    assert len(replies) == 2


def test_config_points_at_this_python():
    entry = agent_mcp.config()
    assert entry["type"] == "stdio" and entry["args"][0] == "-m" and entry["args"][1].endswith("agent_mcp")
