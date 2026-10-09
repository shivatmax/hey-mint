"""The prompt Gemini Live re-reads on every tool step stays small however much the user has: thousands of skills,
long memory notes, many accounts, a long conversation (9 Oct, the user: "even with hundreds of thousands of
skills, the agent's initial window should be under 5k-7k tokens"). Fixed part: test_tool_diet's budgets."""
import json
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mint's tools import macOS frameworks")

try:
    from mint.app import background, session, tasks
    from mint.core import custom, prefs
    from mint.knowledge import conversation
    from mint.knowledge import memory as membank
    from mint.knowledge import skills as skillbook
    from mint.tools import app_library, connector_maker
    from mint.tools import diet as tool_diet
    from mint.tools import extra as extra_tools
    from mint.voice import hearing, vocab
except ImportError:
    from mint import (app_library, background, connector_maker, custom, extra_tools, hearing, membank, prefs, session,
                      skillbook, tasks, tool_diet, vocab)
    from mint import memory as conversation

HEAVY_BUDGET = 26_000          # characters, ~6.5k tokens: instruction + declared tools for the heaviest user
WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa ".split()


def _long(n: int, line: int = 90) -> str:
    out, i = [], 0
    while sum(len(x) + 1 for x in out) < n:
        out.append(" ".join(WORDS[(i + k) % len(WORDS)] for k in range(line // 6)))
        i += 1
    return "\n".join(out)[:n]


@pytest.fixture
def heavy(monkeypatch):
    tool_diet.reset()
    tool_diet._on = True
    settings = {"about_me": _long(4000), "instructions": _long(4000),
                "accounts": {f"account {i}": f"person{i}@example.com" for i in range(80)},
                "slack_workspaces": {f"space {i}": f"Workspace {i}" for i in range(40)},
                "aliases": {f"thing {i}": f"the long name of thing {i}" for i in range(60)}}
    monkeypatch.setattr(custom, "get", lambda: settings)
    monkeypatch.setattr(custom, "routines", lambda: {f"routine {i}": {"description": _long(120)} for i in range(40)})
    real_get = prefs.get
    extra = {"about_me": _long(3000), "personality": _long(400), "speaking_style": _long(800), "user_name": "Sam"}
    monkeypatch.setattr(prefs, "get", lambda key, *a, **k: extra[key] if key in extra else real_get(key, *a, **k))
    monkeypatch.setattr(vocab, "terms", lambda: [f"Name{i}" for i in range(300)])
    monkeypatch.setattr(vocab, "corrections", lambda: [(f"heard {i}", f"meant {i}") for i in range(200)])
    monkeypatch.setattr(hearing, "fixes", lambda: [{"heard": f"h{i}", "meant": f"m{i}"} for i in range(200)])
    monkeypatch.setattr(conversation.memory, "summary", lambda: _long(12_000))
    monkeypatch.setattr(membank, "core_text", lambda limit=5000, *a, **k: _long(limit))
    monkeypatch.setattr(extra_tools, "recent_conversation", lambda *a, max_chars=1800, **k: _long(max_chars))
    monkeypatch.setattr(skillbook, "index_text", lambda *a, **k: "SKILL INDEX " + _long(300_000))
    monkeypatch.setattr(tasks, "prompt_text", lambda: _long(900))
    monkeypatch.setattr(connector_maker, "prompt_addendum", lambda: _long(5000))
    monkeypatch.setattr(app_library, "prompt_text", lambda: _long(5000))
    monkeypatch.setattr(background, "running_note", lambda: _long(400))
    monkeypatch.setattr(session, "_carry_over", _long(20_000))
    yield
    tool_diet.reset()


def test_the_heaviest_user_still_gets_a_small_prompt(heavy):
    config = session._live_config()
    instruction = config.system_instruction.parts[0].text
    declared = [d for t in config.tools for d in t.function_declarations or []]
    tools_chars = len(json.dumps([d.model_dump(exclude_none=True, mode="json") for d in declared],
                                 ensure_ascii=False, separators=(",", ":")))
    assert "SKILL INDEX" not in instruction                  # skills are picked per request, never listed
    assert len(declared) <= 26
    assert len(instruction) + tools_chars <= HEAVY_BUDGET, (len(instruction), tools_chars)


def test_a_big_skill_library_is_shortlisted_and_gemini_chooses_without_jev(monkeypatch, tmp_path):
    """Thousands of learned skills: the chooser sees the best word matches (not the first N), and with no TypeSafe
    key - most users - a cheap Gemini model chooses."""
    import time
    import types as pytypes
    try:
        from mint.core import jev, llm
    except ImportError:
        from mint import jev, llm
    root = tmp_path / "skills"
    head = "---\ntitle: {t}\nwhen: {w}\napps: {a}\nsource: auto\ncreated_by: auto\nuses: {u}\nwins: 0\nfails: 0\n---\n"
    apps = ["Notion", "Figma", "Excel", "Zoom", "Safari", "Xcode"]
    for i in range(3000):
        app = apps[i % len(apps)]
        folder = root / "apps" / app.lower()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"s{i}.md").write_text(head.format(t=f"Export page {i} in {app}", w=f"export a page in {app}",
                                                     a=app, u=i % 9) + "## Steps\n1. x\n")
    (root / "apps" / "slack").mkdir(parents=True)
    (root / "apps" / "slack" / "open-a-channel.md").write_text(
        head.format(t="Open a Slack channel or DM", w="a Slack channel or DM by name", a="Slack", u=0)
        + "## Steps\n1. open_slack\n")
    monkeypatch.setattr(skillbook, "ROOT", root)
    monkeypatch.setattr(skillbook, "_seeded_once", True)
    monkeypatch.setattr(skillbook, "_snapshot", {"root": None, "at": 0.0, "skills": None})
    monkeypatch.setattr(jev, "available", lambda: False)
    seen = {}

    class Models:
        def generate_content(self, model, contents, config):
            seen["prompt"] = contents
            pick = next(line.split(":")[0] for line in contents.splitlines() if "Slack channel" in line)
            return pytypes.SimpleNamespace(text=json.dumps({"id": pick}))
    monkeypatch.setattr(llm, "client", lambda: pytypes.SimpleNamespace(models=Models()))
    started = time.monotonic()
    skill, why = skillbook.find("open the on-call channel in Slack", "Slack")
    assert skill is not None and skill["title"] == "Open a Slack channel or DM" and "Gemini" in why
    assert seen["prompt"].count("\n") < skillbook.SHORTLIST + 20         # a shortlist, not 3001 lines
    assert time.monotonic() - started < 10
    assert len(skillbook.all_skills()) == 3001 and skillbook._snapshot["skills"] is not None   # listed once
