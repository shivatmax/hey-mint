"""The tool diet (tool_diet.py): a fixed core set per voice session, the rest found with find_tools and run
with use_tool through the very same checks; plus the loop detector, outage captions and big-result spill."""
import asyncio
import json
import sys
import types as pytypes

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mint's tools import macOS frameworks")

try:
    from mint.app import session
    from mint.core import guard
    from mint.tools import diet as tool_diet
    from mint.tools import harness as harness_tools
    from mint.tools import registry as tools
except ImportError:
    from mint import guard, harness_tools, session, tool_diet, tools

from google.genai import types


@pytest.fixture(autouse=True)
def _diet_on():
    tool_diet.reset()
    tool_diet._on = True
    yield
    tool_diet.reset()


@pytest.fixture(scope="module")
def full():
    return tools.tools()


def _names(declared):
    return [d.name for t in declared for d in t.function_declarations or []]


# --- the declared list ---------------------------------------------------------------------------

def test_core_set_is_stable_and_small(full):
    first = _names(tool_diet.live_tools(full))
    again = _names(tool_diet.live_tools(tools.tools()))
    assert first == again                                   # the same list every time it is built
    assert set(first) == (tool_diet.CORE & set(_names(full))) | set(tool_diet.BRIDGE)
    assert 15 <= len(first) <= 26, len(first)           # 21 (9 Oct): the rest is picked per request (router.py)
    assert len(first) < len(_names(full)) * 0.2
    for everyday in ("open_app", "type_text", "press_key", "ui_act", "click_text", "click_at", "look", "read_window",
                     "set_volume", "media_key", "stop_listening", "background_task", "plan_task", "step_done",
                     "recall"):
        assert everyday in first, everyday
    assert len(first) == len(set(first))


def test_diet_off_declares_everything(full):
    tool_diet.reset()
    tool_diet._on = False
    assert _names(tool_diet.live_tools(full)) == _names(full)
    assert tool_diet.prompt_parts(["a", "b"]) == ["a", "b"]


def test_every_tool_is_reachable(full):
    declared = set(_names(tool_diet.live_tools(full)))
    for name in _names(full):
        if name in declared:
            continue
        assert tool_diet.rank(name)[:1] == [name], (name, tool_diet.rank(name)[:3])
        found = tool_diet.find(name)
        assert f"- {name}:" in found and "args:" in found, name


@pytest.mark.parametrize("query,tool", [
    ("trim the first 10 seconds of a video", "edit_video"), ("translate a document into Hindi", "convert_document"),
    ("make a spreadsheet of my invoices", "make_spreadsheet"), ("generate an image of a fox", "make_image"),
    ("move the orb down", "move_orb"), ("record my screen", "screen_record"),
    ("directions to the airport", "maps"), ("tell me when the download finishes", "track"),
    ("zip it with Keka", "hand_to_app")])
def test_find_tools_ranks_the_right_tool_first(full, query, tool):
    tool_diet.live_tools(full)
    assert tool_diet.rank(query)[0] == tool


def test_every_hidden_tool_has_a_family(full):
    """A new tool is either everyday (CORE) or joins a family, so the prompt's line names it."""
    families = {n for f in tool_diet.FAMILIES.values() for n in f["tools"]}
    loose = [n for n in _names(full) if n not in tool_diet.CORE and n not in families]
    assert not loose, f"add these to tool_diet.CORE or a FAMILIES entry: {loose}"


def test_find_tools_stays_within_budget_and_brings_the_guidance(full):
    tool_diet.live_tools(full)
    text = tool_diet.find("add bread to my grocery note")
    assert len(text) <= tool_diet.BUDGET
    assert "- notes:" in text and "How to use them:" in text and "action=append" in text
    small = tool_diet.find("add bread to my grocery note", budget=1200)
    assert len(small) <= 1200 and "- notes:" in small
    assert "No hidden tool matches" in tool_diet.find("qwxz")


def test_descriptions_point_to_hidden_tools_through_use_tool(full):
    tool_diet.live_tools(full)
    decl = types.FunctionDeclaration(name="x", description="To change it in place use edit_selection.")
    assert "edit_selection: through use_tool" in tool_diet._with_hints(decl, {"edit_selection"}).description
    assert "through use_tool" not in decl.description                                   # the original is untouched
    # Plain words that happen to be tool names (notes, mail, track) are not tool mentions.
    plain = types.FunctionDeclaration(name="y", description="Look up saved notes and mail you track.")
    assert "through use_tool" not in tool_diet._with_hints(plain, {"notes", "mail", "track"}).description


def test_bridge_declarations_are_valid(full):
    for decl in tool_diet.declarations():
        assert decl.description and decl.parameters.required
        assert decl.name not in _names(full)


# --- use_tool -------------------------------------------------------------------------------------------

def test_use_tool_unwraps_to_the_real_tool(full):
    tool_diet.live_tools(full)
    tool_diet.find("edit a video")                       # its guidance has been read
    name, args, problem = tool_diet.unwrap("use_tool", {"name": "edit_video",
                                                        "args": json.dumps({"instruction": "mute it"})})
    assert (name, args, problem) == ("edit_video", {"instruction": "mute it"}, "")
    assert tool_diet.unwrap("open_app", {"name": "Notes"}) == ("open_app", {"name": "Notes"}, "")
    assert tool_diet.unwrap("use_tool", {"name": "edit_video", "args": {"instruction": "x"}})[0] == "edit_video"


def test_use_tool_refuses_what_it_cannot_run(full):
    tool_diet.live_tools(full)
    tool_diet.find("edit a video")
    assert "not valid JSON" in tool_diet.unwrap("use_tool", {"name": "edit_video", "args": "{instruction"})[2]
    missing = tool_diet.unwrap("use_tool", {"name": "edit_video", "args": "{}"})[2]
    assert missing.startswith("NOT RUN") and "instruction" in missing and '"properties"' in missing
    assert "Did you mean edit_video" in tool_diet.unwrap("use_tool", {"name": "edit_vidoe"})[2]
    assert tool_diet.unwrap("use_tool", {"name": "use_tool"})[2]
    assert tool_diet.unwrap("use_tool", {})[2]


def test_use_tool_without_find_tools_reads_the_rules_first(full):
    tool_diet.live_tools(full)
    call = {"name": "meeting", "args": json.dumps({"action": "start"})}
    name, _, problem = tool_diet.unwrap("use_tool", call)
    assert name == "use_tool" and problem.startswith("NOT RUN yet") and "Never start recording" in problem
    assert tool_diet.unwrap("use_tool", call)[0] == "meeting"        # read once: now it runs


def test_use_tool_goes_through_the_guard_with_the_inner_name(full, monkeypatch):
    tool_diet.live_tools(full)
    tool_diet.find("organise my downloads folder")
    seen, sent = [], []

    async def check(name, args, who=None):
        seen.append((name, dict(args)))
        return "NOT DONE: the user said no."
    monkeypatch.setattr(guard, "check", check)
    monkeypatch.setattr(session, "_trace", lambda *a: seen.append(("trace", a[0])))
    try:
        from mint.knowledge import conversation as memory_module
    except ImportError:
        from mint import memory as memory_module
    monkeypatch.setattr(memory_module.memory, "add", lambda *a, **k: None)

    class Session:
        async def send_tool_response(self, function_responses):
            sent.extend(function_responses)

    class Fake:
        _IDEMPOTENT = session.Mint._IDEMPOTENT
        _suppress_turn = _hush = False
        session = Session()
        _recent_calls: dict = {}
        _print = staticmethod(lambda *a: None)
        _verdict_pending = staticmethod(lambda: False)

        async def _run_detachable(self, name, args):
            return await session.Mint._run_one(self, name, args)

    call = types.FunctionCall(id="c1", name="use_tool",
                              args={"name": "tidy", "args": json.dumps({"action": "plan", "folder": "~/Downloads"})})
    asyncio.run(session.Mint._run_batch(Fake(), pytypes.SimpleNamespace(function_calls=[call])))
    assert seen[0] == ("tidy", {"action": "plan", "folder": "~/Downloads"})    # the guard saw the real tool
    assert ("trace", "tidy (use_tool)") in seen
    assert sent and sent[0].name == "use_tool" and sent[0].id == "c1"
    assert "NOT DONE" in sent[0].response["result"]


# --- the prompt -----------------------------------------------------------------------------------------

def test_prompt_moves_hidden_families_sections_to_find_tools():
    try:
        from mint.tools import music, video_edit
        from mint.tools import harness as harness_mod
    except ImportError:
        from mint import harness_tools as harness_mod, music, video_edit
    parts = tool_diet.prompt_parts(["the user's own note", harness_mod.PROMPT, video_edit.PROMPT, music.PROMPT])
    assert parts[0] == "the user's own note" and len(parts) == 2      # guides.py carries the harness rules now
    line = parts[-1]
    assert "find_tools" in line and "videos" in line and "use_tool" in line
    assert video_edit.PROMPT.strip()[:60] in tool_diet.find("cut a video")


# --- what every step re-reads -----------------------------------------------------------------------------

# Gemini Live bills the instruction and the declared tools again on every function-call step. At ~26k tokens a
# step (9 Oct) two or three steps a minute hit the user's 65k tokens/min limit and the model was rested mid-task.
# These budgets (characters, ~4 per token) keep the fixed part near 3k tokens (with the session's time, permissions
# and hearing lines): past one, move the text into a guide (guides.py, a family's prompt) that router.py hands over
# when a request needs it, rather than raising the number.
TOOLS_BUDGET = 7_500
INSTRUCTION_BUDGET = 4_000
SECTIONS_BUDGET = 2_000
STEP_BUDGET = 13_000


def _fixed_sections(monkeypatch) -> str:
    """The prompt sections every session carries, without the user's own skills, memories and conversation."""
    try:
        from mint.app import tasks
        from mint.knowledge import memory as membank
        from mint.knowledge import skills as skillbook
        from mint.tools import app_library, automations, connector_maker
        from mint.tools import extra as extra_tools
    except ImportError:
        from mint import app_library, automations, connector_maker, extra_tools, membank, skillbook, tasks
    for module, name in ((skillbook, "index_text"), (tasks, "prompt_text"), (automations, "habit_note"),
                         (connector_maker, "prompt_addendum"), (app_library, "prompt_text")):
        monkeypatch.setattr(module, name, lambda: "")
    monkeypatch.setattr(membank, "core_text", lambda *a, **k: "")
    monkeypatch.setattr(extra_tools, "recent_conversation", lambda *a, **k: "")
    return extra_tools.prompt_text()


def _declared_json(declared) -> str:
    return json.dumps([d.model_dump(exclude_none=True, mode="json") for d in declared], ensure_ascii=False,
                      separators=(",", ":"))


def test_what_every_step_rereads_stays_within_budget(full, monkeypatch):
    try:
        from mint.core import config
    except ImportError:
        from mint import config
    tools_chars = len(_declared_json([d for t in tool_diet.live_tools(full) for d in t.function_declarations]))
    sections = _fixed_sections(monkeypatch)
    assert tools_chars <= TOOLS_BUDGET, tools_chars
    assert len(config.SYSTEM_INSTRUCTION) <= INSTRUCTION_BUDGET, len(config.SYSTEM_INSTRUCTION)
    assert len(sections) <= SECTIONS_BUDGET, len(sections)
    total = tools_chars + len(config.SYSTEM_INSTRUCTION) + len(sections)
    assert total <= STEP_BUDGET, total
    assert "find_tools" in sections and "Saved skills" not in sections        # the line about hidden tools is there


def _shape(schema):
    """A schema without its descriptions: names, types, enums, required."""
    if isinstance(schema, dict):
        return {k: _shape(v) for k, v in schema.items() if k != "description"}
    if isinstance(schema, list):
        return [_shape(v) for v in schema]
    return schema


def test_short_descriptions_keep_names_arguments_and_the_full_notes(full):
    originals = {d.name: d for t in full for d in t.function_declarations}
    declared = {d.name: d for t in tool_diet.live_tools(full) for d in t.function_declarations}
    for name, text in tool_diet.LIVE.items():
        assert name in tool_diet.CORE, f"{name} is not declared: drop its short text"
        assert len(text) < len(originals[name].description), name
        assert declared[name].description.startswith(text), name
    for name, notes in tool_diet.LIVE_ARGS.items():
        assert name in tool_diet.CORE, name
        for path, text in notes.items():
            found = tool_diet._arg(declared[name].parameters, path)
            assert found is not None and found.description == text, (name, path)
    for name, decl in declared.items():
        if name in originals:                       # the same arguments, types and enums as the full tool
            assert _shape(decl.parameters.model_dump(exclude_none=True, mode="json")) == \
                _shape(originals[name].parameters.model_dump(exclude_none=True, mode="json")), name
    assert len(originals["ui_act"].description) > len(tool_diet.LIVE["ui_act"])    # the original is untouched
    notes = tool_diet.find("ui_act")                 # find_tools with a declared tool's name: its full notes
    assert "already one of your tools" in notes and originals["ui_act"].description[:80] in notes


def test_moved_tools_are_found_and_run(full):
    tool_diet.live_tools(full)
    for query, tool in (("smile for me", "express"), ("ask Astra to research flights to Goa", "delegate_task"),
                        ("pause everything", "pause_everything"), ("read the ChatGPT app's latest reply", "agent_app")):
        assert tool_diet.rank(query)[0] == tool, (query, tool_diet.rank(query)[:3])
    # express has no family guide (its rules stay in the prompt), so it runs at once
    assert tool_diet.unwrap("use_tool", {"name": "express", "args": {"emotion": "smile", "requested": True}})[0] \
        == "express"
    assert "Delegating:" in tool_diet.find("delegate a job to Sage")


# --- loops ----------------------------------------------------------------------------------------------

def test_loop_detector_blocks_the_third_identical_call(monkeypatch):
    monkeypatch.setattr(harness_tools, "_request_now", lambda: "click the blue button")
    harness_tools._this_request.clear()
    ran = []

    async def core(name, args):
        ran.append(name)
        return "FAILED: no control called 'blue button'.", None

    async def go():
        call = ("ui_act", {"target": "blue button", "action": "click"})
        first = await harness_tools.guard(core, *call)
        second = await harness_tools.guard(core, *call)
        third = await harness_tools.guard(core, *call)
        return first, second, third
    first, second, third = asyncio.run(go())
    assert len(ran) == 2 and third[0].startswith("NOT RUN - you are looping")
    # Another request starts fresh; keys and scrolling are never blocked.
    monkeypatch.setattr(harness_tools, "_request_now", lambda: "something else")
    assert asyncio.run(harness_tools.guard(core, "ui_act", {"target": "blue button", "action": "click"}))[0] \
        .startswith("FAILED")
    for _ in range(4):
        asyncio.run(harness_tools.guard(core, "press_key", {"key": "down"}))
    assert ran.count("press_key") == 4


def test_loop_detector_lets_changing_results_run_with_a_note(monkeypatch):
    monkeypatch.setattr(harness_tools, "_request_now", lambda: "go through the setup wizard")
    harness_tools._this_request.clear()
    pages = iter(["Page 2 of 4", "Page 3 of 4", "Page 4 of 4"])

    async def core(name, args):
        return f"Clicked Next. {next(pages)}", None

    results = [asyncio.run(harness_tools.guard(core, "ui_act", {"target": "Next"}))[0] for _ in range(3)]
    assert results[2].startswith("Clicked Next. Page 4") and "[Loop warning]" in results[2]
    assert "[Loop warning]" not in results[1]


# --- outages --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("message,caption", [
    ("1011 None. You exceeded your current quota, please check your plan", "quota"),
    ("429 RESOURCE_EXHAUSTED", "quota"),
    ("received 1011 (internal error) Internal error encountered.", "down on Google's side"),
    ("503 UNAVAILABLE: The model is overloaded", "down on Google's side"),
    ("400 API key not valid. Please pass a valid API key.", "refused the API key"),
    ("PERMISSION_DENIED", "refused the API key"),
    ("[Errno 8] nodename nor servname provided, or not known", "No connection"),
    ("timed out during opening handshake", "No connection"),
    ("something odd", "dropped"),
])
def test_outage_captions(message, caption):
    assert caption in session._outage_caption(message)


def test_outage_countdown_shows_the_time_left(monkeypatch):
    lines = []

    class UI:
        def action(self, text):
            lines.append(text)

    class Fake:
        ui = UI()

        def _state(self, name, note=""):
            assert name == "offline"

    asyncio.run(session.Mint._count_down(Fake(), 2.2, "The voice service is down on Google's side"))
    assert lines[0] == "The voice service is down on Google's side - trying again in 2 seconds."
    assert any(line.endswith("in 1 second.") for line in lines[1:]) and lines[-1] == "Reconnecting…"


# --- big results ---------------------------------------------------------------------------------------

def test_big_result_is_cut_and_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_diet, "SPILL_DIR", tmp_path)
    text = "START " + "x" * 50_000 + " END"
    out = tool_diet.fit("read_window", text)
    assert len(out) < tool_diet.RESULT_CAP and out.startswith("START") and out.endswith("END")
    saved = list(tmp_path.glob("*.txt"))
    assert len(saved) == 1 and saved[0].read_text() == text
    assert saved[0].name in out and "read_file" in out
    assert tool_diet.fit("read_window", "short") == "short"
    assert tool_diet.fit("read_file", text) == text           # read_file pages itself (and reads the spill)
    paths = {tool_diet.fit("browser", text).split("saved in ")[1].split(" - ")[0] for _ in range(3)}
    assert len(paths) == 3                                     # never one over another
    for _ in range(tool_diet.SPILL_KEEP + 3):
        tool_diet.fit("browser", text)
    assert len(list(tmp_path.glob("*.txt"))) == tool_diet.SPILL_KEEP
