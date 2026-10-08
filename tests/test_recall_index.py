"""The history search index (history_index.py) and recall_history on top of it: incremental ingest,
history rotation, query sanitising, parts of words, time bounds, conversations and the lines around
a hit, scrolling, and the file scan when there is no FTS5. Temporary files only; the LLM is faked."""
import datetime as dt
import json
import os
import time

import pytest

try:
    from mint.core import llm
    from mint.knowledge import conversation as memory_mod
    from mint.knowledge import history_index, journal
except ImportError:
    from mint import history_index, journal, llm
    from mint import memory as memory_mod


def _t(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M")


NOW = dt.datetime.now().replace(second=0, microsecond=0)
# "Earlier today" that is still today just after midnight (30 minutes ago at 00:20 was yesterday).
TODAY_AGO = min(30, (NOW - NOW.replace(hour=0, minute=0)).total_seconds() / 60 / 2)


def row(minutes_ago: float, role: str, text: str) -> dict:
    return {"t": _t(NOW - dt.timedelta(minutes=minutes_ago)), "role": role, "text": text}


def write(path, rows, mode="w"):
    with open(path, mode) as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MINT_NO_LEARNING", "1")
    monkeypatch.setattr(memory_mod, "HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(memory_mod, "SUMMARY", tmp_path / "summary.md")
    monkeypatch.setattr(journal, "HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(journal, "_extras", lambda start, end: [])
    monkeypatch.setattr(history_index, "_indexes", {})
    monkeypatch.setattr(history_index, "_unavailable", set())
    (tmp_path / "memory").mkdir()
    return tmp_path


def index(home):
    return history_index.HistoryIndex(home / "memory" / "history.db", home / "history.jsonl",
                                      home / "history-archive")


def texts(rows):
    return [r.text for r in rows]


# --- ingest ---------------------------------------------------------------------------------------

def test_ingest_is_incremental_and_waits_for_half_written_lines(home):
    write(home / "history.jsonl", [row(50, "user", "open the quarterly report"), row(49, "mint", "Opened it.")])
    idx = index(home)
    assert idx.ingest() == 2
    assert idx.ingest() == 0, "nothing new: nothing read again"
    write(home / "history.jsonl", [row(48, "user", "make the chart blue")], "a")
    with open(home / "history.jsonl", "a") as f:
        f.write('{"t": "2026-01-01 10:00", "role": "user", "text": "half')       # still being written
    assert idx.ingest() == 1
    assert texts(idx.between()) == ["open the quarterly report", "Opened it.", "make the chart blue"]
    with open(home / "history.jsonl", "a") as f:
        f.write(' a line"}\n')
    assert idx.ingest() == 1
    assert len(idx.between()) == 4
    assert oct(os.stat(home / "memory" / "history.db").st_mode & 0o777) == "0o600"


def test_tool_lines_are_cut_and_markers_split_conversations(home):
    write(home / "history.jsonl", [row(20, "user", "find the invoice"), row(20, "tool", "find_files() -> " + "x" * 3000),
                                   {"t": _t(NOW - dt.timedelta(minutes=19)), "role": "marker", "text": "new session"},
                                   row(19, "jarvis", "old role name"), row(18, "user", "next")])
    idx = index(home)
    idx.ingest()
    rows = idx.between()
    assert len(rows[1].text) == history_index.TOOL_CHARS
    assert [r.role for r in rows] == ["user", "tool", "mint", "user"]
    assert rows[0].conv == rows[1].conv != rows[2].conv == rows[3].conv


def test_rotation_neither_loses_nor_repeats_lines(home):
    rows = [row(300 - i, "user", f"line number {i} about topic{i}") for i in range(60)]
    write(home / "history.jsonl", rows)
    mem = memory_mod.Memory()
    mem._write_state(base=0, cursor=(home / "history.jsonl").stat().st_size)    # all summarised
    idx = index(home)
    idx.io_lock = mem._io
    assert idx.ingest() == 60
    assert mem.rotate(max_bytes=1000, keep_bytes=600)
    assert list((home / "history-archive").glob("history-*.jsonl"))
    assert idx.ingest() == 0, "rotation only moved lines: nothing new"
    write(home / "history.jsonl", [row(1, "user", "after the rotation")], "a")
    assert idx.ingest() == 1
    assert texts(idx.between()) == [r["text"] for r in rows] + ["after the rotation"]
    # A second rotation into the same day's archive, and a fresh index built from scratch.
    write(home / "history.jsonl", [row(0, "user", "y" * 200) for _ in range(10)], "a")
    assert mem.rotate(max_bytes=1000, keep_bytes=300)
    assert idx.ingest() == 10
    fresh = history_index.HistoryIndex(home / "memory" / "other.db", home / "history.jsonl", home / "history-archive")
    fresh.ingest()
    assert texts(fresh.between()) == texts(idx.between())
    assert len(idx.between()) == 71


def test_a_replaced_history_is_indexed_again(home):
    write(home / "history.jsonl", [row(10, "user", "the old blue kayak")])
    idx = index(home)
    idx.ingest()
    write(home / "history.jsonl", [row(5, "user", "a different red canoe"), row(4, "user", "and its paddle")])
    idx.ingest()
    assert texts(idx.between()) == ["a different red canoe", "and its paddle"]


# --- search ---------------------------------------------------------------------------------------

def test_terms_drop_syntax_and_filler():
    assert history_index.terms("Which anime am I watching?") == ["anime", "watching"]
    assert history_index.terms('what about "blue kayak" and TODO: fix') == ["blue kayak", "todo", "fix"]
    assert history_index.terms("AND OR NOT * 😀 :: ()") == []
    query = history_index.match_query(["todo", 'say "hi"', "extensions"])
    assert query == '"todo"* AND "say ""hi"""' + ' AND "extension"*'


@pytest.mark.parametrize("question", ['the "report', "report: quarterly", "NOT report", "report AND OR",
                                      "report* -chart", "😀 report 🎉", "col:umn(report)", '"""', "NEAR(report)"])
def test_odd_queries_never_break_the_search(home, question):
    write(home / "history.jsonl", [row(5, "user", "open the quarterly report please")])
    idx = index(home)
    idx.ingest()
    hits, _how, _terms = idx.search(question)
    if "report" in question.lower():
        assert hits and hits[0].text == "open the quarterly report please"


def test_words_match_by_stem_and_parts_of_words_by_trigram(home):
    write(home / "history.jsonl", [row(9, "user", "install the vscode extensions for python"),
                                   row(8, "user", "we watched three episodes of that anime")])
    idx = index(home)
    idx.ingest()
    hits, how, _ = idx.search("which anime was I watching")
    assert hits[0].text.startswith("we watched"), "watching -> watch* finds watched"
    hits, how, _ = idx.search("code")
    assert how == "part of a word" and hits[0].text.startswith("install the vscode")


def test_all_words_rank_above_some_words_and_recent_above_old(home):
    write(home / "history.jsonl", [row(30, "user", "the budget meeting"),
                                   row(20, "user", "the budget spreadsheet for april"),
                                   row(60 * 24 * 20, "user", "the budget spreadsheet for march")])    # found later
    idx = index(home)
    idx.ingest()
    hits, how, _ = idx.search("budget spreadsheet")
    assert how == "all words"
    assert texts(hits)[:2] == ["the budget spreadsheet for april", "the budget spreadsheet for march"]
    assert hits[2].text == "the budget meeting"


def test_time_bounds(home):
    write(home / "history.jsonl", [row(60 * 24 * 3, "user", "kayak trip planning"), row(10, "user", "kayak rental")])
    idx = index(home)
    idx.ingest()
    start, end, _ = journal.when_range("today")
    hits, _, _ = idx.search("kayak", start, end)
    assert texts(hits) == ["kayak rental"]
    start, end, _ = journal.when_range("3 days ago")
    assert texts(idx.search("kayak", start, end)[0]) == ["kayak trip planning"]


def test_hits_grouped_by_conversation_with_the_lines_around_the_best(home):
    early = [row(600, "user", "first talk about the kayak"), row(599, "mint", "noted")]
    talk = [row(100 + 10 - i, "user" if i % 2 == 0 else "mint", f"filler {i}") for i in range(10)]
    talk[5] = row(95, "user", "which kayak paddle did we pick")
    talk[8] = row(93, "mint", "The kayak paddle is the blue one.")             # another hit, same conversation
    talk.append(row(99.5, "user", "which kayak paddle did we pick"))          # the same words again
    write(home / "history.jsonl", early + talk[:6] + talk[10:] + talk[6:10])
    idx = index(home)
    idx.ingest()
    found = idx.find("kayak paddle", window=2)
    top = texts(found.top)
    assert "which kayak paddle did we pick" in top and len(top) == 5
    assert "noted" not in top, "the window stays in its own conversation"
    assert "The kayak paddle is the blue one." in [h.text for h in found.hits]
    assert [h.text for h in found.others] == ["first talk about the kayak"], "one line per other conversation"
    assert texts(found.hits).count("which kayak paddle did we pick") == 1
    more = idx.around(found.hits[0].id, 50, 50)
    assert "first talk about the kayak" not in texts(more)
    assert "first talk about the kayak" in texts(idx.around(found.hits[0].id, 50, 50, same_conversation=False))


# --- recall_history -------------------------------------------------------------------------------

def _big_history(home):
    rows = [row(2000 - i, "tool", f"browser(page {i}) -> " + "lorem ipsum dolor " * 25) for i in range(150)]
    rows.insert(80, row(1920, "user", "remind me which VS Code extension we installed"))
    rows.insert(81, row(1920, "mint", "We installed the Prettier extension in VS Code."))
    write(home / "history.jsonl", rows)


def test_recall_history_sends_only_the_matching_part(home, monkeypatch):
    _big_history(home)
    prompts = []
    monkeypatch.setattr(llm, "generate", lambda prompt, *a, **k: (prompts.append(prompt) or "Prettier.", "m"))
    answer = journal.recall_history("what did I ask about the VS Code extension")
    assert "Prettier extension" in prompts[0]
    assert len(prompts[0]) < 15_000, "only the hits and the lines around them, not the whole log"
    assert "around='" in answer
    ref = answer.split("around='")[1].split("'")[0]
    scrolled = journal.recall_history("", around=ref)
    assert "We installed the Prettier extension in VS Code." in scrolled and "#" in scrolled
    assert journal.recall_history("", around="#999999").startswith("There is no history line")


def test_small_ranges_go_whole_and_vague_questions_still_work(home, monkeypatch):
    write(home / "history.jsonl", [row(TODAY_AGO, "user", "open the calendar"), row(TODAY_AGO, "mint", "Opened it.")])
    prompts = []
    monkeypatch.setattr(llm, "generate", lambda prompt, *a, **k: (prompts.append(prompt) or "You opened it.", "m"))
    answer = journal.recall_history("what did we do", "today")
    assert "open the calendar" in prompts[0] and "Opened it." in prompts[0]
    assert "around=" not in answer
    assert prompts[0].index("open the calendar") < prompts[0].index("Opened it."), "same minute: order kept"


def test_without_fts5_the_files_are_read(home, monkeypatch):
    _big_history(home)
    monkeypatch.setattr(history_index, "_SCHEMA", "CREATE VIRTUAL TABLE IF NOT EXISTS x USING fts9(text);")
    assert history_index.get(home / "history.jsonl") is None
    prompts = []
    monkeypatch.setattr(llm, "generate", lambda prompt, *a, **k: (prompts.append(prompt) or "Prettier.", "m"))
    answer = journal.recall_history("which VS Code extension")
    assert "Prettier extension" in prompts[0] and "Prettier" in answer
    assert len(prompts[0]) > 30_000, "the old path: the whole range"
    assert journal.recall_history("", around="12").startswith("FAILED")


def test_a_broken_index_falls_back_to_the_files(home, monkeypatch):
    _big_history(home)
    idx = history_index.get(home / "history.jsonl")

    def broken(*a, **k):
        raise history_index.sqlite3.DatabaseError("database disk image is malformed")
    monkeypatch.setattr(idx, "ingest", broken)
    prompts = []
    monkeypatch.setattr(llm, "generate", lambda prompt, *a, **k: (prompts.append(prompt) or "Prettier.", "m"))
    journal.recall_history("which VS Code extension")
    assert "Prettier extension" in prompts[0]


def test_ingest_later_never_blocks(home, monkeypatch):
    write(home / "history.jsonl", [row(3, "user", "hello there")])
    monkeypatch.setattr(history_index, "_running", history_index.threading.Event())
    started = time.monotonic()
    history_index.ingest_later()
    assert time.monotonic() - started < 0.2
    for _ in range(50):
        if not history_index._running.is_set():
            break
        time.sleep(0.05)
    assert texts(history_index.get(home / "history.jsonl").between()) == ["hello there"]
