"""8 Oct: a quieter Mint. Coding agents pop up only when one asks you something (Allow / Deny, or pick the answer to
its question right there) or finishes - nothing while they work. A hidden Mint that comes out for "Hey Mint" hides
again when it goes back to sleep. Little sounds are off until turned on. Claude's usage limits are read from the
Claude app's own samples (a status line never runs there)."""
import json
import os
import subprocess
import sys
import threading
import time

try:                                    # the published layout
    from mint.core import prefs
    from mint.tools import agent_checks, agent_hooks, agent_watch
except ImportError:                     # the private working copy
    from mint import agent_checks, agent_hooks, agent_watch, prefs


QUESTION = {"questions": [{"question": "Which database?", "header": "DB", "multiSelect": False,
                           "options": [{"label": "Postgres", "description": "a"}, {"label": "SQLite", "description": "b"}]}]}


# --- answering a question from the pop-up ---------------------------------------------------------------------

def _serve(tmp_path, monkeypatch):
    sock = f"/tmp/mint-q-{os.getpid()}.sock"
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
    return sock, script


def _ask(script, request):
    proc = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    proc.stdin.write(json.dumps(request))
    proc.stdin.close()
    key = "claude:" + request["session_id"]
    for _ in range(60):
        if agent_hooks.pending(key):
            break
        time.sleep(0.05)
    return proc, key


def test_a_question_is_answered_with_the_option_clicked(tmp_path, monkeypatch):
    sock, script = _serve(tmp_path, monkeypatch)
    request = {"hook_event_name": "PermissionRequest", "session_id": "q1", "tool_name": "AskUserQuestion",
               "tool_input": QUESTION, "tool_use_id": "tq1"}
    proc, key = _ask(script, request)
    assert agent_hooks.answerable(key) == ["Postgres", "SQLite"]
    assert not agent_hooks.decide(key, "allow")             # an Allow without an answer would deny it
    assert not agent_hooks.answer(key, "MySQL")              # not one of its options
    threading.Timer(0.05, agent_hooks.answer, args=(key, "SQLite")).start()
    out = json.loads(proc.stdout.read())["hookSpecificOutput"]["decision"]
    proc.wait(timeout=5)
    assert out["behavior"] == "allow"
    assert out["updatedInput"]["answers"] == {"Which database?": "SQLite"}
    assert out["updatedInput"]["questions"] == QUESTION["questions"]       # the rest of its input as it was
    os.unlink(sock)


def test_questions_the_pop_up_cannot_answer_go_to_the_window(tmp_path, monkeypatch):
    sock, script = _serve(tmp_path, monkeypatch)
    multi = {"questions": [{**QUESTION["questions"][0], "multiSelect": True}]}
    two = {"questions": QUESTION["questions"] + [{**QUESTION["questions"][0], "question": "Which ORM?"}]}
    for sid, tool_input in (("m1", multi), ("m2", two)):
        proc, key = _ask(script, {"hook_event_name": "PermissionRequest", "session_id": sid,
                                  "tool_name": "AskUserQuestion", "tool_input": tool_input})
        assert agent_hooks.answerable(key) == []
        assert not agent_hooks.answer(key, "Postgres")
        agent_hooks.decide(key, "deny")                      # Deny still works
        assert json.loads(proc.stdout.read())["hookSpecificOutput"]["decision"]["behavior"] == "deny"
        proc.wait(timeout=5)
    os.unlink(sock)


def test_a_held_question_reads_as_asking_and_answering_resumes_the_work():
    w = agent_watch.Watcher()
    w.ingest_hook({"hook_event_name": "UserPromptSubmit", "session_id": "h2", "cwd": "/a/b", "prompt": "go"})
    w.ingest_hook({"hook_event_name": "PermissionRequest", "session_id": "h2", "_mint_id": "q9",
                   "tool_name": "AskUserQuestion", "tool_input": QUESTION, "tool_use_id": "tq9"})
    s = w.get("claude:h2")
    assert s.state == "asking" and s.question["text"] == "Which database?"
    assert s.question["options"] == ["Postgres", "SQLite"] and s.approval["tool"] == "AskUserQuestion"
    w.approval_closed("claude:h2", "q9", "answer")
    s = w.get("claude:h2")
    assert s.approval is None and s.state == "working"


# --- Claude's usage limits from the Claude app ----------------------------------------------------------------

def _app_usage(tmp_path, monkeypatch, samples):
    path = tmp_path / "plan-usage-history.json"
    path.write_text(json.dumps({"version": 2, "samples": samples}))
    monkeypatch.setattr(agent_watch, "CLAUDE_APP_USAGE", str(path))
    monkeypatch.setattr(agent_watch, "CLAUDE_LIMITS", (str(tmp_path / "none.json"),))
    monkeypatch.setattr(agent_watch, "LIMITS", {})
    monkeypatch.setattr(agent_watch, "_app_usage", {"mtime": 0.0, "data": {}})
    return path


def test_claude_limits_come_from_the_claude_apps_newest_sample(tmp_path, monkeypatch):
    now = time.time() * 1000
    _app_usage(tmp_path, monkeypatch, [{"t": now - 3_600_000, "org": "o", "u": {"fh": 40, "sd": 30}},
                                       {"t": now - 60_000, "org": "o", "u": {"fh": 3, "sd": 36}}])
    claude = agent_watch.limits()["claude"]
    assert claude["5h"] == 3 and claude["week"] == 36
    assert agent_checks.limits_line() == "Claude 5h 3% · week 36%"
    assert "97% of the 5-hour limit left" in agent_checks._limits()


def test_an_old_sample_says_nothing_about_the_five_hours(tmp_path, monkeypatch):
    now = time.time() * 1000
    _app_usage(tmp_path, monkeypatch, [{"t": now - 6 * 3_600_000, "org": "o", "u": {"fh": 80, "sd": 50}}])
    assert agent_watch.limits()["claude"] == {"at": (now - 6 * 3_600_000) / 1000, "source": "app", "week": 50}
    _app_usage(tmp_path, monkeypatch, [{"t": now - 8 * 86_400_000, "org": "o", "u": {"fh": 80, "sd": 50}}])
    assert "claude" not in agent_watch.limits()
    assert agent_checks.limits_line() == ""


def test_a_newer_status_line_wins(tmp_path, monkeypatch):
    now = time.time()
    _app_usage(tmp_path, monkeypatch, [{"t": (now - 600) * 1000, "org": "o", "u": {"fh": 3, "sd": 36}}])
    line = tmp_path / "claude-limits.json"
    line.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 12}, "seven_day": {"used_percentage": 40}},
                                "updatedAt": int(now * 1000)}))
    monkeypatch.setattr(agent_watch, "CLAUDE_LIMITS", (str(line),))
    monkeypatch.setattr(agent_watch, "_claude_file", {"mtime": 0.0, "data": {}})
    claude = agent_watch.limits()["claude"]
    assert claude["5h"] == 12 and claude["week"] == 40


def test_no_claude_app_no_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_watch, "CLAUDE_APP_USAGE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(agent_watch, "CLAUDE_LIMITS", (str(tmp_path / "none.json"),))
    monkeypatch.setattr(agent_watch, "LIMITS", {})
    assert agent_watch.limits() == {} and agent_checks.limits_line() == ""


# --- the new defaults, and moving an existing settings file to them ---------------------------------------------

def _settings(tmp_path, monkeypatch, stored):
    path = tmp_path / "settings.json"
    if stored is not None:
        path.write_text(json.dumps(stored))
    monkeypatch.setattr(prefs, "PATH", path)
    monkeypatch.setattr(prefs, "_values", {})
    monkeypatch.setattr(prefs, "_mtime", None)
    return path


def test_defaults_are_quiet():
    assert prefs.DEFAULTS["agent_mode"] == "quiet"
    assert prefs.DEFAULTS["ui_sounds"] is False
    assert prefs.DEFAULTS["agent_approvals"] is True and prefs.DEFAULTS["agent_open_on_done"] is True


def test_an_old_settings_file_moves_to_pop_ups_once(tmp_path, monkeypatch):
    path = _settings(tmp_path, monkeypatch, {"agent_mode": "auto", "agent_open_on_done": False, "ui_sounds": True})
    assert prefs.get("agent_mode") == "quiet" and prefs.get("agent_open_on_done") is True
    assert prefs.get("ui_sounds") is True                      # a choice the user made stays
    saved = json.loads(path.read_text())
    assert saved["display_version"] == prefs.DISPLAY_VERSION and "agent_open_on_done" not in saved
    prefs.set("agent_mode", "auto")                            # chosen again after the move: kept
    monkeypatch.setattr(prefs, "_values", {})
    monkeypatch.setattr(prefs, "_mtime", None)
    assert prefs.get("agent_mode") == "auto"


def test_off_stays_off_and_a_new_user_is_never_moved(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, {"agent_mode": "off"})
    assert prefs.get("agent_mode") == "off"
    (tmp_path / "new").mkdir()
    path = _settings(tmp_path / "new", monkeypatch, None)
    assert prefs.get("agent_mode") == "quiet" and not path.exists()      # nothing written just by reading
    prefs.set("agent_mode", "auto")
    assert json.loads(path.read_text())["display_version"] == prefs.DISPLAY_VERSION
    monkeypatch.setattr(prefs, "_values", {})
    monkeypatch.setattr(prefs, "_mtime", None)
    assert prefs.get("agent_mode") == "auto"


# --- what the closed notch shows: only asks and finishes ---------------------------------------------------------

def _agents(monkeypatch, sessions, **settings):
    try:
        from mint.ui import notch_agents
    except ImportError:
        from mint import notch_agents
    values = {"agent_mode": "quiet", "agent_approvals": True, "agent_open_on_done": True, **settings}
    monkeypatch.setattr(notch_agents.prefs, "get", lambda key: values.get(key, prefs.DEFAULTS.get(key)))
    monkeypatch.setattr(notch_agents, "ordered", lambda items=None: list(sessions))
    monkeypatch.setattr(notch_agents, "pal_of", lambda key: ("cat", (1.0, 1.0, 1.0)))
    # As on a Mac with Claude Code: without it Claude mode is off by itself (mode()), which is what the CI runner
    # has - these tests are about what shows, not about detecting the apps.
    monkeypatch.setattr(agent_watch, "installed", lambda: {"claude": True, "codex": True})
    return notch_agents


def _s(key, state, busy=False, approval=None, since=None):
    s = agent_watch.Session(key=key, app="claude", id=key, cwd="/srv/x", where="cli", state=state,
                            updated=time.time(), since=since or time.time())
    s.approval = approval
    if busy:
        s.state = "working"
    return s


def test_quiet_shows_nothing_while_they_work(monkeypatch):
    working = _s("claude:w", "working", busy=True)
    na = _agents(monkeypatch, [working])
    assert working.busy and na.wing() is None and na._wing_items() == []
    assert not na.live()
    na = _agents(monkeypatch, [working], agent_mode="auto")
    assert na.live() and na.wing()["state"] == "working"


def test_quiet_pops_up_when_one_asks_or_finishes(monkeypatch):
    asking = _s("claude:a", "waiting", approval={"id": "1", "tool": "Bash", "verb": "Run", "target": "npm test"})
    done = _s("claude:d", "done")
    na = _agents(monkeypatch, [asking, done])
    assert [s.key for s in na._wing_items()] == ["claude:a", "claude:d"]
    na = _agents(monkeypatch, [asking, done], agent_open_on_done=False)
    assert [s.key for s in na._wing_items()] == ["claude:a"]
    na = _agents(monkeypatch, [asking, done], agent_approvals=False, agent_open_on_done=False)
    assert na._wing_items() == [] and na.wing() is None
    old = _s("claude:o", "done", since=time.time() - 60)
    assert _agents(monkeypatch, [old]).wing() is None          # long finished: nothing to pop up for


# --- Codex on and off (Settings ▸ Models & agents) ----------------------------------------------------------------

def _registry(tmp_path, monkeypatch, problem=""):
    from mint.agents import codex, registry
    monkeypatch.setattr(registry, "PATH", tmp_path / "agents.json")
    monkeypatch.setattr(codex, "problem", lambda: problem)
    return registry


def test_codex_can_be_turned_off_and_on(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    assert "Codex" in [a["name"] for a in registry.active()]
    registry.set_on("Codex", False)
    assert "Codex" not in [a["name"] for a in registry.active()]
    assert "turned off" in registry.unusable(registry.get("Codex"))
    assert registry.get("Codex")["runner"] == "codex"            # still itself, only off
    registry.set_on("Codex", True)
    assert "Codex" in [a["name"] for a in registry.active()] and not registry.get("Codex").get("off")


def test_without_codex_on_this_mac_it_is_never_offered(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, problem="Codex is not installed (it comes with the ChatGPT app).")
    assert [a["name"] for a in registry.active()] == ["Astra", "Luna", "Sage"]
    from mint.agents import orchestrator
    text = orchestrator.prompt_text()
    assert "give it to Luna" in text and "Codex -" not in text
    from mint.agents import runtime
    said = runtime.hub.delegate("Codex", "build a landing page for the bakery with a menu and an order form")
    assert said.startswith("NOT STARTED: Codex is not installed") and "Luna" in said


# --- no Claude Code or Codex on this Mac: Claude mode is off by itself ---------------------------------------------

def _mode(monkeypatch, apps, stored=None):
    na = _agents(monkeypatch, [], **({"agent_mode": stored} if stored else {}))
    monkeypatch.setattr(agent_watch, "installed", lambda: dict(apps))
    monkeypatch.setattr(na.prefs, "is_set", lambda key: stored is not None and key == "agent_mode")
    return na


def test_without_claude_or_codex_claude_mode_is_off(monkeypatch):
    na = _mode(monkeypatch, {"claude": False, "codex": False})
    assert na.mode() == "off" and not na.available() and na.wing() is None
    hooked = []
    monkeypatch.setattr(na, "_hook", lambda: hooked.append(1))
    monkeypatch.setattr(na, "_event_fns", [])
    na.on_event(lambda kind, s: None)
    assert hooked == [] and len(na._event_fns) == 1          # listening waits; nothing is watched
    for apps in ({"claude": True, "codex": False}, {"claude": False, "codex": True}):
        assert _mode(monkeypatch, apps).mode() == "quiet"
    assert _mode(monkeypatch, {"claude": False, "codex": False}, stored="auto").mode() == "auto"   # chosen: kept


def test_installed_reads_the_sessions_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_watch, "CLAUDE_DIR", str(tmp_path / ".claude" / "projects"))
    monkeypatch.setattr(agent_watch, "CODEX_DIR", str(tmp_path / ".codex" / "sessions"))
    monkeypatch.setattr(agent_watch, "CODEX_APPS", (str(tmp_path / "Codex.app"),))
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(agent_watch, "_installed", {"at": -1e9, "apps": {}})
    assert agent_watch.installed() == {"claude": False, "codex": False}
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    (tmp_path / ".codex").mkdir()
    monkeypatch.setattr(agent_watch, "_installed", {"at": -1e9, "apps": {}})
    assert agent_watch.installed() == {"claude": True, "codex": True}


# --- Codex's model: Auto (what Codex is set to) or one Codex offers -------------------------------------------------

def _codex_home(tmp_path, monkeypatch):
    from mint.agents import codex
    home = tmp_path / ".codex"
    home.mkdir(exist_ok=True)
    (home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "visibility": "list", "priority": 4},
        {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list", "priority": 0},
        {"slug": "codex-auto-review", "display_name": "Codex Auto Review", "visibility": "hide", "priority": 43}]}))
    (home / "config.toml").write_text('model = "gpt-6.1-sol"\nmodel_reasoning_effort = "xhigh"\n')
    monkeypatch.setattr(codex, "CODEX_HOME", home)
    return codex, home


def test_codex_models_come_from_codex_itself(tmp_path, monkeypatch):
    codex, home = _codex_home(tmp_path, monkeypatch)
    assert codex.models() == [("gpt-6.1-sol", "GPT-6.1-Sol"), ("gpt-6-luna", "GPT-6-Luna")]   # best first, no hidden
    assert codex.selected() == "gpt-6.1-sol"
    (home / "models_cache.json").unlink()
    (home / "config.toml").unlink()
    assert codex.models() == [] and codex.selected() == ""


def _codex_args(tmp_path, monkeypatch, model):
    import asyncio
    import types
    codex, _ = _codex_home(tmp_path, monkeypatch)
    monkeypatch.setattr(codex, "binary", lambda: "/usr/bin/true")
    monkeypatch.setattr(codex, "problem", lambda: "")
    seen = []

    class Stop(Exception):
        pass

    async def spawn(*args, **kw):
        seen.append(list(args))
        raise Stop
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    run = types.SimpleNamespace(agent={"models": [model]}, task="build a page", context="", inbox=[], effort="low",
                                id="t1", thread_id=None, model="")
    try:
        asyncio.run(codex.run(None, run, tmp_path / "out"))
    except Stop:
        pass
    return seen[0], run


def test_auto_lets_codex_use_its_own_model(tmp_path, monkeypatch):
    args, run = _codex_args(tmp_path, monkeypatch, "auto")
    assert "-m" not in args and run.model == "codex/gpt-6.1-sol"
    args, run = _codex_args(tmp_path, monkeypatch, "gpt-6-luna")
    assert args[args.index("-m") + 1] == "gpt-6-luna" and run.model == "codex/gpt-6-luna"


def test_choosing_codexs_model_is_kept(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    assert registry.get("Codex")["models"] == ["auto"]                  # new installs follow Codex
    registry.set_model("Codex", "gpt-6-luna")
    assert registry.get("Codex")["models"] == ["gpt-6-luna"]
    registry.save({**registry.get("Codex"), "role": "Coding in OpenAI Codex (GPT-6 Luna): builds websites"})
    assert registry.get("Codex")["role"] == "Coding in OpenAI Codex: builds websites"   # the model shows on its own
