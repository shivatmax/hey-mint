"""The router (router.py): which tool groups and how-to a request needs, decided by Jev, a cheap Gemini model or
word overlap, and handed to the voice model in find_tools' answer and the request's first tool result."""
import json
import sys
import time
import types as pytypes

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mint's tools import macOS frameworks")

try:
    from mint.core import guides
    from mint.tools import diet as tool_diet
    from mint.tools import extra as extra_tools
    from mint.tools import registry as tools
    from mint.tools import router
except ImportError:
    from mint import extra_tools, guides, router, tool_diet, tools


def _family(label: str) -> str:
    return next(f for f, info in tool_diet.FAMILIES.items() if info.get("label") == label)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    tool_diet.reset()
    tool_diet._on = True
    tool_diet.live_tools(tools.tools())
    router.reset()
    monkeypatch.setattr(router, "mode", lambda: "words")
    yield
    router.reset()
    tool_diet.reset()


@pytest.mark.parametrize("request_text,label", [
    ("set a timer for 5 minutes", "reminders, calendar, notes, email, timers"),
    ("find the PDF I downloaded yesterday", "files"),
    ("take a screenshot of this window", "screenshots and clipboard"),
    ("trim the first 10 seconds of my video", "videos"),
    ("remember my manager is Nina", "remembering and the past"),
    ("turn on dark mode", "music and Mac switches"),
])
def test_words_pick_the_right_group(request_text, label):
    assert router.route(request_text).groups[0] == _family(label)


def test_chat_needs_no_group():
    assert router.route("how are you").groups == []
    assert router.route("").groups == []


def test_jev_ids_are_checked_against_the_real_list(monkeypatch):
    monkeypatch.setattr(router, "mode", lambda: "jev")
    calls = []

    def rank(request, options, instructions, timeout=0, **_):
        calls.append(options)
        return [("g999", 0.9), ("g1", 0.5), ("g0", 0.05)], {}
    try:
        from mint.core import jev
    except ImportError:
        from mint import jev
    monkeypatch.setattr(jev, "rank", rank)
    found = router.route("open my files")
    names = list(tool_diet.FAMILIES)
    assert found.source == "jev" and found.groups == [names[1]]       # g999 does not exist; g0 is below the floor
    assert router.route("open my files") is found and len(calls) == 1     # cached
    assert all(text.startswith(tuple(tool_diet.FAMILIES)) for text in calls[0].values())


def test_gemini_answer_is_parsed_and_checked(monkeypatch):
    monkeypatch.setattr(router, "mode", lambda: "gemini")
    try:
        from mint.core import llm
    except ImportError:
        from mint import llm

    class Models:
        def generate_content(self, model, contents, config):
            assert "Request: play some jazz" in contents and config.max_output_tokens <= 100
            return pytypes.SimpleNamespace(text=json.dumps({"ids": ["g5", "bogus", "g5", "g2"]}))
    monkeypatch.setattr(llm, "client", lambda: pytypes.SimpleNamespace(models=Models()))
    monkeypatch.setattr(llm, "_busy", {})
    found = router.route("play some jazz")
    names = list(tool_diet.FAMILIES)
    assert found.source == "gemini" and found.groups == [names[5], names[2]]


def test_a_slow_model_falls_back_to_words_and_its_answer_is_kept(monkeypatch):
    monkeypatch.setattr(router, "mode", lambda: "jev")
    names = list(tool_diet.FAMILIES)
    files = names.index(_family("files"))

    def slow(request):
        time.sleep(0.4)
        return [names[files]]
    monkeypatch.setattr(router, "_by_jev", slow)
    first = router.route("find the PDF I downloaded yesterday", timeout=0.05)
    assert first.source == "words" and first.groups[0] == _family("files")
    time.sleep(0.6)
    later = router.route("find the PDF I downloaded yesterday")
    assert later.source == "jev" and later.groups == [names[files]]


def test_pack_brings_how_to_and_arguments_once_within_budget():
    groups = [_family("clicking and app windows"), _family("web, browser and websites")]
    first = tool_diet.pack(groups, "open the site and click sign in", budget=3500)
    assert len(first) <= 3700
    assert guides.CLICKING[:60] in first and "args:" in first and "use_tool" in first
    second = tool_diet.pack(groups, "open the site and click sign in", budget=3500)
    assert guides.CLICKING[:60] not in second and "Given earlier" in second     # nothing sent twice in a session
    assert len(second) < len(first)


def test_find_tools_hands_over_the_routed_groups(monkeypatch):
    files = _family("files")
    monkeypatch.setattr(router, "route", lambda request, timeout=0: router.Route([files], "jev", 0.2))
    answer = tool_diet.find_for_request("find a file", request="find the invoice PDF from yesterday")
    assert "- find_files:" in answer and guides.FILES[:50] in answer
    # Its how-to was given, so use_tool runs at once.
    assert tool_diet.unwrap("use_tool", {"name": "find_files", "args": {"query": "invoice"}})[0] == "find_files"
    assert "already one of your tools" in tool_diet.find_for_request("ui_act")       # a tool by name: its notes


def test_first_tool_result_carries_the_routed_toolkit(monkeypatch):
    try:
        from mint.knowledge import memory as membank
        from mint.knowledge import skills as skillbook
    except ImportError:
        from mint import membank, skillbook
    day = _family("reminders, calendar, notes, email, timers")
    monkeypatch.setattr(router, "route", lambda request, timeout=0: router.Route([day], "jev", 0.2))
    monkeypatch.setattr(skillbook, "all_skills", lambda: [])
    monkeypatch.setattr(membank, "relevant", lambda request: [])
    pack = extra_tools._context_pack("remind me to call mum at 6", False)
    assert pack.startswith("\n[Context for this request]") and "- create_reminder:" in pack
    assert guides.DAY[:50] in pack and len(pack) < 4300


def test_first_tool_result_does_not_repeat_what_find_tools_gave(monkeypatch):
    """9 Oct live run: find_tools gave the calendar group, then the first tool result packed the same groups'
    other tools again (~1.2k tokens on every later step)."""
    try:
        from mint.knowledge import memory as membank
        from mint.knowledge import skills as skillbook
    except ImportError:
        from mint import membank, skillbook
    day = _family("reminders, calendar, notes, email, timers")
    monkeypatch.setattr(router, "route", lambda request, timeout=0: router.Route([day], "jev", 0.2))
    monkeypatch.setattr(skillbook, "all_skills", lambda: [])
    monkeypatch.setattr(membank, "relevant", lambda request: [])
    request = "what's on my calendar today?"
    assert "- calendar_events:" in tool_diet.find_for_request("check calendar events", request=request)
    assert tool_diet.kit_given(request)
    assert "Picked for this request" not in extra_tools._context_pack(request, False)
