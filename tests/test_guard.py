"""The guard: what counts as deleting or changing, the levels, asking and answering, and Claude Code's hook."""
import asyncio
import json
import subprocess
import sys
import threading
import time

import pytest

try:
    from mint.core import guard
    from mint.tools import agent_hooks
except ImportError:
    from mint import agent_hooks, guard


@pytest.fixture(autouse=True)
def _no_ui(monkeypatch):
    monkeypatch.setattr(guard, "_card_show", lambda p: None)
    monkeypatch.setattr(guard, "_card_done", lambda p: None)
    monkeypatch.setattr(guard, "_telegram_ask", lambda p: None)
    monkeypatch.setattr(guard, "_from_phone", lambda: False)
    monkeypatch.setattr(guard, "_away", lambda: False)
    monkeypatch.setattr(guard, "_audit", lambda *a: None)
    monkeypatch.setattr(guard, "level", lambda: "all")


def test_what_is_dangerous(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"x" * 2048)
    d = guard.assess("file_action", {"action": "trash", "path": str(f)})
    assert d.kind == "delete" and "Trash" in d.title and "report.pdf" in d.lines[0] and "2.0 KB" in d.lines[0]
    assert guard.assess("write_file", {"path": str(f), "mode": "overwrite"}).kind == "change"
    assert guard.assess("write_file", {"path": str(tmp_path / "new.md"), "mode": "overwrite"}) is None
    assert guard.assess("write_file", {"path": str(f), "mode": "append"}) is None
    assert guard.assess("file_action", {"action": "open", "path": str(f)}) is None
    assert guard.assess("file_action", {"action": "rename", "path": str(f), "to": "b.pdf"}).kind == "change"
    assert guard.assess("notes", {"action": "delete", "note": "Groceries"}).kind == "delete"
    assert guard.assess("reminders_manage", {"action": "complete"}) is None
    assert guard.assess("run_applescript", {"script": 'do shell script "rm -rf ~/build"'}).kind == "delete"
    assert guard.assess("run_applescript", {"script": 'tell application "Finder" to empty trash'}).kind == "delete"
    assert guard.assess("run_applescript", {"script": 'display dialog "hi"'}) is None
    assert guard.assess("press_key", {"key": "delete", "modifiers": ["cmd"]}).kind == "delete"
    assert guard.assess("press_key", {"key": "delete"}) is None
    assert guard.assess("ui_act", {"action": "click", "target": "Delete Message", "app": "Mail"}).kind == "delete"
    assert guard.assess("ui_act", {"action": "click", "target": "Open"}) is None
    assert guard.assess("menu", {"path": "Finder > Empty Trash…"}).kind == "delete"
    assert guard.assess("mac", {"control": "wifi", "value": "off"}).kind == "system"
    assert guard.assess("open_app", {"name": "Notes"}) is None


@pytest.mark.parametrize("command,kind", [
    ("rm -rf build", "delete"), ("git reset --hard HEAD~1", "delete"), ("git push --force origin main", "delete"),
    ("sudo launchctl list", "system"), ("find . -name '*.tmp' -delete", "delete"), ("mv a.txt b.txt", "change"),
    ("echo hi > notes.txt", "change"), ("diskutil eraseDisk APFS X disk4", "system"), ("brew uninstall node", "delete"),
    ("npm test", ""), ("git push origin main", ""), ("ls -la ~/Downloads", ""), ("echo 2>&1 | grep x", ""),
    ("cat a > /dev/null", "change")])
def test_shell_commands(command, kind):
    assert guard.shell_danger(command)[1] == kind


def test_levels(monkeypatch):
    change, delete = guard.Danger("change", "x"), guard.Danger("delete", "y")
    assert guard.wanted(change) and guard.wanted(delete)
    monkeypatch.setattr(guard, "level", lambda: "delete")
    assert not guard.wanted(change) and guard.wanted(delete)
    monkeypatch.setattr(guard, "level", lambda: "off")
    assert not guard.wanted(delete)


def _answer_soon(fn, *args):
    def later():
        for _ in range(100):
            p = guard.current()
            if p is not None:
                fn(p, *args)
                return
            time.sleep(0.02)
    threading.Thread(target=later, daemon=True).start()


def test_ask_waits_for_the_answer():
    d = guard.Danger("delete", "move this to the Trash", ["~/a.pdf"])
    _answer_soon(lambda p: guard.answer(p.ident, True))
    assert guard.ask(d, timeout=5) is True
    _answer_soon(lambda p: guard.answer(p.ident, False))
    assert guard.ask(d, timeout=5) is False
    _answer_soon(lambda p: guard.heard("yes, go ahead"))
    assert guard.ask(d, timeout=5) is True
    _answer_soon(lambda p: guard.heard("no don't"))
    assert guard.ask(d, timeout=5) is False
    assert guard.ask(d, timeout=0.3) is False                     # no answer: not done
    assert guard.current() is None


def test_check_refuses_on_no(monkeypatch, tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("x")
    monkeypatch.setattr(guard, "ask", lambda d, who="Mint", timeout=None: False)
    said = asyncio.run(guard.check("file_action", {"action": "trash", "path": str(f)}))
    assert said.startswith("NOT DONE")
    monkeypatch.setattr(guard, "ask", lambda d, who="Mint", timeout=None: True)
    assert asyncio.run(guard.check("file_action", {"action": "trash", "path": str(f)})) == ""
    assert asyncio.run(guard.check("open_app", {"name": "Notes"})) == ""


def test_telegram_buttons():
    d = guard.Danger("delete", "delete a note", ["Groceries"])
    result = {}

    def press(p):
        toast, act, used = guard.telegram_plan(f"{p.ident}:y")
        result["toast"], result["used"] = toast, used
        act()
    _answer_soon(press)
    assert guard.ask(d, timeout=5) is True and result["used"] == "✅ Allowed"
    assert guard.telegram_plan("zz:y")[1] is None                  # closed question: nothing to do


def test_claude_code_hook_guard(tmp_path):
    settings = tmp_path / "settings.json"
    script = tmp_path / "hook.py"
    script.write_text(agent_hooks.HOOK_SCRIPT.replace(
        'os.path.expanduser("~/Library/Application Support/Mint/settings.json")', repr(str(settings))))

    def run(command):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
        out = subprocess.run([sys.executable, str(script)], input=json.dumps(payload), capture_output=True,
                             text=True, timeout=10).stdout.strip()
        return json.loads(out)["hookSpecificOutput"] if out else None
    settings.write_text(json.dumps({"guard": "all"}))
    asked = run("rm -rf node_modules")
    assert asked["permissionDecision"] == "ask" and "deletes files" in asked["permissionDecisionReason"]
    assert run("npm test") is None
    assert run("mv a b")["permissionDecision"] == "ask"
    settings.write_text(json.dumps({"guard": "delete"}))
    assert run("mv a b") is None and run("rm x") is not None
    settings.write_text(json.dumps({"guard": "off"}))
    assert run("rm -rf /") is None
    groups = agent_hooks._merged({}, True)["hooks"]["PreToolUse"]
    assert groups[0]["matcher"] == "Bash"
