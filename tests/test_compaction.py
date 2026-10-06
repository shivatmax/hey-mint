"""Compaction (compaction.py + the session's compaction hooks): exact names taken from the tool lines, the
user's own words kept, the sectioned prompt, updating the previous summary, when an automatic compaction
starts (fake usage numbers) and that it waits for a quiet moment. No network: the LLM is faked."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

try:
    from mint.app import compaction, session
    from mint.core import llm, prefs
    from mint.knowledge import conversation as memory_mod
except ImportError:
    from mint import compaction, llm, prefs, session
    from mint import memory as memory_mod


def e(role: str, text: str, t: str = "2026-10-06 10:00") -> dict:
    return {"t": t, "role": role, "text": text}


CONVO = [
    e("user", "Always save the exports to the Reports folder, never the Desktop."),
    e("tool", "open_app(open app · Numbers) -> Opened Numbers [Now in front: Numbers]"),
    e("tool", "write_file(write file · /srv/reports/q3-sales.csv) -> Saved 4.2 MB"),
    e("tool", "open_url(open url · https://example.com/pricing?plan=pro) -> Opened https://example.com/pricing?plan=pro"),
    e("mint", "Saved q3-sales.csv to Reports."),
    e("user", "Send it to alex@example.org at 4:30 pm, total was $1,240.50"),
    e("tool", "find_files(find files · invoice) -> FAILED: not found ~/Documents/invoice-0423.pdf"),
    e("user", "ok"),
]


# --- anchors and the user's words ----------------------------------------------------------------------

def test_anchors_are_taken_exactly_from_the_tool_lines():
    found = compaction.anchors(CONVO)
    assert "/srv/reports/q3-sales.csv" in found["files"]
    assert "~/Documents/invoice-0423.pdf" in found["files"]
    assert "q3-sales.csv" not in found["files"], "a file name already in a path is not listed twice"
    assert found["links"] == ["https://example.com/pricing?plan=pro"]
    assert found["apps"] == ["Numbers"]
    assert "alex@example.org" in found["emails"]
    assert {"4.2 MB", "$1,240.50", "4:30 pm"} <= set(found["numbers"])
    text = compaction.anchor_text(found)
    assert text.startswith("- Files: ") and "- Apps: Numbers" in text


def test_the_users_words_are_kept_verbatim_latest_last():
    words = compaction.user_words(CONVO)
    assert words == ["Always save the exports to the Reports folder, never the Desktop.",
                     "Send it to alex@example.org at 4:30 pm, total was $1,240.50"], "fillers like 'ok' dropped"
    many = [e("user", f"request number {i}") for i in range(30)] + [e("user", "request number 29")]
    kept = compaction.user_words(many, keep=15)
    assert kept[0] == "request number 15" and kept[-1] == "request number 29" and len(kept) == 15
    long = compaction.user_words([e("user", "word " * 200)], each=50)
    assert len(long[0]) == 50 and long[0].endswith("…")


def test_a_long_conversation_keeps_the_early_instructions():
    items = [e("user", "Rule: never email Sam without asking me first.")]
    items += [e("tool", f"browser(page {i}) -> " + "z" * 400) for i in range(400)]
    items += [e("mint", "Done with the research.")]
    text = compaction.transcript(items, limit=20_000)
    assert len(text) <= 21_000
    assert "never email Sam without asking me first" in text
    assert text.rstrip().endswith("Done with the research.")


# --- the prompt and the summary -----------------------------------------------------------------------------

def test_the_prompt_has_every_section_the_anchors_and_the_plan():
    prompt = compaction.build_prompt(CONVO, plan="1. export [done]\n2. email it [next]", today="Monday Oct 6 2026")
    for name in compaction.SECTIONS:
        assert f"## {name}" in prompt
    assert "QUOTED word for word" in prompt
    assert "/srv/reports/q3-sales.csv" in prompt.split("NAMES FROM THE TOOL LINES")[1]
    assert "2. email it [next]" in prompt
    assert "PREVIOUS SUMMARY" not in prompt
    assert "Always save the exports to the Reports folder" in prompt


def test_summary_adds_names_and_words_and_the_next_one_updates_it(monkeypatch):
    prompts = []

    def fake(prompt, *a, **k):
        prompts.append(prompt)
        return (f"## Goal - what the user is trying to get done\n- export sales (round {len(prompts)})\n"
                "## Constraints & preferences\n- \"Always save…\""), "m"
    monkeypatch.setattr(llm, "generate", fake)
    first = compaction.summarize(CONVO)
    assert first.startswith("## Goal\n- export sales (round 1)")
    assert compaction.ANCHOR_HEADING in first and compaction.WORDS_HEADING in first
    assert "> Always save the exports to the Reports folder, never the Desktop." in first

    later = [e("user", "Now make a chart of it in Keynote"),
             e("tool", "open_app(open app · Keynote) -> Opened Keynote")]
    second = compaction.summarize(later, previous=first)
    assert "PREVIOUS SUMMARY:\n## Goal\n- export sales (round 1)" in prompts[1]
    assert compaction.WORDS_HEADING not in prompts[1].split("NEW CONVERSATION")[0], "only the model's part is updated"
    body, anchor_lines, words = compaction.split(second)
    assert body.startswith("## Goal\n- export sales (round 2)")
    assert words[0] == "Always save the exports to the Reports folder, never the Desktop.", "earlier words survive"
    assert words[-1] == "Now make a chart of it in Keynote"
    apps = next(line for line in anchor_lines if line.startswith("- Apps: "))
    assert "Numbers" in apps and "Keynote" in apps
    assert any("q3-sales.csv" in line for line in anchor_lines)


def test_an_empty_or_failed_summary_raises(monkeypatch):
    monkeypatch.setattr(llm, "generate", lambda *a, **k: ("", "m"))
    with pytest.raises(RuntimeError):
        compaction.summarize(CONVO)
    with pytest.raises(ValueError):
        compaction.summarize([])


def test_the_new_session_gets_it_as_reference_only():
    framed = compaction.framed("## Goal\n- x")
    assert framed.startswith("REFERENCE ONLY") and "latest request" in framed and "authoritative" in framed
    assert compaction.framed("") == ""


def test_entries_start_after_the_last_marker_and_the_session_start(tmp_path):
    path = tmp_path / "history.jsonl"
    rows = [e("user", "before the marker", "2026-10-06 09:00"), {"t": "2026-10-06 09:05", "role": "marker",
                                                                "text": "compacted"},
            e("user", "old run", "2026-10-06 09:10"), e("user", "this run", "2026-10-06 11:00"),
            e("tool", "x() -> y", "2026-10-06 11:01")]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert [x["text"] for x in compaction.entries(path)] == ["old run", "this run", "x() -> y"]
    since = time.mktime(time.strptime("2026-10-06 10:00", "%Y-%m-%d %H:%M"))
    assert [x["text"] for x in compaction.entries(path, since)] == ["this run", "x() -> y"]


# --- when to compact on our own -----------------------------------------------------------------------------

def test_due_at_eighty_percent_of_the_trigger():
    assert not compaction.due(0)
    assert not compaction.due(80_000)
    assert compaction.due(84_000)
    assert compaction.due(120_000)
    assert compaction.context_tokens(SimpleNamespace(total_token_count=2313, prompt_token_count=2289)) == 2313
    assert compaction.context_tokens(SimpleNamespace(total_token_count=None, prompt_token_count=100,
                                                     response_token_count=5)) == 105


class _Queue:
    def __init__(self, empty=True):
        self._empty = empty

    def empty(self):
        return self._empty


def _mint(**kw):
    base = dict(audio=SimpleNamespace(playing=False), audio_in=_Queue(), _turn_open=False, _heard="",
                _last_voice=0.0, _busy=False, _batch=[], _tool_task=None, meet=None, task=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_only_a_quiet_moment_counts():
    now = 1000.0
    assert compaction.busy_reason(_mint(), now) == ""
    assert compaction.busy_reason(_mint(audio=SimpleNamespace(playing=True)), now) == "speaking"
    assert compaction.busy_reason(_mint(audio_in=_Queue(False)), now) == "audio queued"
    assert compaction.busy_reason(_mint(_heard="so can you"), now) == "the user is talking"
    assert compaction.busy_reason(_mint(_last_voice=now - 1), now) == "the user just spoke"
    assert compaction.busy_reason(_mint(_busy=True), now) == "a tool is running"
    running = SimpleNamespace(done=lambda: False)
    assert compaction.busy_reason(_mint(_tool_task=running), now) == "a tool is running"
    assert compaction.busy_reason(_mint(meet=object()), now) == "in a call"
    plan = {"goal": "g", "steps": [{"n": "1", "text": "a", "status": "todo"}], "updated": time.time()}
    assert compaction.busy_reason(_mint(task=plan), now) == "a plan step is under way"
    plan["updated"] = time.time() - 3600
    assert compaction.busy_reason(_mint(task=plan), now) == "", "a plan left alone for an hour does not hold it up"


class _Fake:
    """Just enough of session.Mint for its compaction hooks."""
    _watch_context = session.Mint._watch_context
    _auto_compact = session.Mint._auto_compact
    _quiet_for = session.Mint._quiet_for

    def __init__(self):
        self.audio = SimpleNamespace(playing=False)
        self.audio_in = _Queue()
        self._turn_open, self._heard, self._last_voice, self._busy = False, "", 0.0, False
        self._batch, self._tool_task, self.meet, self.task = [], None, None, None
        self.printed, self.started, self.summaries = [], [], 0

    def _print(self, text):
        self.printed.append(text)

    async def _compaction_summary(self, source):
        self.summaries += 1
        return "## Goal\n- keep going"

    async def new_session(self, source, carry=""):
        self.started.append((source, carry))


def test_usage_numbers_start_one_automatic_compaction_at_a_quiet_moment(monkeypatch):
    monkeypatch.setattr(compaction, "QUIET_FOR", 0.2)
    monkeypatch.setattr(memory_mod.memory, "checkpoint", lambda timeout=20: None)
    monkeypatch.setattr(prefs, "get", lambda key, default=None: True if key == "auto_compact" else default)
    fake = _Fake()

    async def go():
        fake._watch_context(SimpleNamespace(total_token_count=50_000))
        await asyncio.sleep(0.05)
        assert not getattr(fake, "_auto_compacting", False) and fake._context_tokens == 50_000
        fake.audio.playing = True                         # Mint is speaking: it waits
        fake._watch_context(SimpleNamespace(total_token_count=90_000))
        fake._watch_context(SimpleNamespace(total_token_count=91_000))    # only one at a time
        await asyncio.sleep(1.2)
        assert fake.started == [] and fake._auto_compacting
        fake.audio.playing = False
        for _ in range(40):
            if fake.started:
                break
            await asyncio.sleep(0.1)
    asyncio.run(go())
    assert fake.started == [("auto", "## Goal\n- keep going")]
    assert fake.summaries == 1 and not fake._auto_compacting
    assert any("compacting at the next quiet moment" in p for p in fake.printed)


def test_no_automatic_compaction_when_the_pref_is_off(monkeypatch):
    monkeypatch.setattr(prefs, "get", lambda key, default=None: False if key == "auto_compact" else default)
    fake = _Fake()

    async def go():
        fake._watch_context(SimpleNamespace(total_token_count=100_000))
        assert not getattr(fake, "_auto_compacting", False) and fake.printed == []
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert fake._context_tokens == 100_000 and fake.started == []


def test_a_failed_summary_backs_off(monkeypatch):
    monkeypatch.setattr(compaction, "QUIET_FOR", 0.1)
    monkeypatch.setattr(memory_mod.memory, "checkpoint", lambda timeout=20: None)
    monkeypatch.setattr(prefs, "get", lambda key, default=None: True if key == "auto_compact" else default)
    fake = _Fake()

    async def failing(source):
        fake.summaries += 1
        return "Could not summarise right now: 503"
    fake._compaction_summary = failing

    async def go():
        fake._watch_context(SimpleNamespace(total_token_count=95_000))
        for _ in range(30):
            if fake.summaries:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.1)
        fake._watch_context(SimpleNamespace(total_token_count=96_000))     # right after: not again
        await asyncio.sleep(0.8)
    asyncio.run(go())
    assert fake.started == [] and fake.summaries == 1


class _UI:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append(name)


def test_a_compaction_keeps_the_plan_and_a_new_session_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_mod, "HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(memory_mod.memory, "checkpoint", lambda timeout=20: None)
    paused = []
    try:
        from mint.app import tasks
    except ImportError:
        from mint import tasks
    monkeypatch.setattr(tasks, "pause", lambda task, why: paused.append(why))
    try:
        from mint.app import background
    except ImportError:
        from mint import background
    stopped = []
    monkeypatch.setattr(background, "on_stop", lambda *a, **k: stopped.append(a))

    class Fake:
        new_session = session.Mint.new_session

        def __init__(self):
            self.task = {"id": "t1", "goal": "export"}
            self.ui, self.session, self._recent_calls = _UI(), None, {"x": 1}

        def _print(self, text):
            pass

        def _flush_playback(self):
            pass

    fake = Fake()
    asyncio.run(fake.new_session("auto", carry="## Goal\n- export"))
    assert fake.task == {"id": "t1", "goal": "export"} and paused == []
    assert stopped == [], "background jobs are outside the Live session: a compaction leaves them running"
    assert session._carry_over == "## Goal\n- export"
    assert "chat_note" in fake.ui.calls and "chat_reset" not in fake.ui.calls, "auto: the chat is not wiped"
    assert "compacted" in (tmp_path / "history.jsonl").read_text()
    assert "REFERENCE ONLY" in compaction.framed(session._carry_over)
    asyncio.run(fake.new_session("button"))
    assert fake.task is None and paused == ["a new conversation was started"]
    assert session._carry_over == ""
