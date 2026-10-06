"""The memory bank v2 and the conversation memory: migration, the prompt core, hybrid search, the write path
(duplicates, supersede), auto extraction, the crash-safe summary cursor, checkpoint, history rotation and
consolidation. Everything runs on temporary files; no network (embeddings and the LLM are faked)."""
import json
import time

import pytest

try:
    from mint.core import llm
    from mint.knowledge import conversation as memory_mod
    from mint.knowledge import journal
    from mint.knowledge import memory as membank
except ImportError:
    from mint import journal, llm, membank
    from mint import memory as memory_mod


def _no_network(*args, **kwargs):
    raise RuntimeError("no network in tests")


@pytest.fixture(autouse=True)
def bank(tmp_path, monkeypatch):
    monkeypatch.setenv("MINT_NO_LEARNING", "1")
    monkeypatch.setattr(membank, "BANK", tmp_path / "memory" / "bank.json")
    monkeypatch.setattr(membank, "LEGACY", tmp_path / "memories.md")
    monkeypatch.setattr(membank, "AUTO_EMBED", False)
    monkeypatch.setattr(membank, "_embed", _no_network)
    monkeypatch.setattr(membank, "_query_cache", {})
    monkeypatch.setattr(membank, "_embedding", {"running": False, "down_until": 0.0, "model": None})
    monkeypatch.setattr(membank, "_vec_cache", {"mtime": None, "data": None, "path": None})
    monkeypatch.setattr(membank, "_used", [])
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: None)
    monkeypatch.setattr(llm, "generate", _no_network)
    monkeypatch.setattr(memory_mod, "HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(memory_mod, "SUMMARY", tmp_path / "summary.md")
    monkeypatch.setattr(journal, "HISTORY", tmp_path / "history.jsonl")
    (tmp_path / "memory").mkdir()
    return tmp_path


def _ago(days: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - days * 86400))


def mk(n: int, text: str, group: str = "misc", **kw) -> dict:
    block = membank._block(n, text, group, kw.pop("pinned", False), kind=kw.pop("kind", "fact"),
                           origin=kw.pop("origin", "auto"))
    block.update(kw)
    return block


def save(blocks: list[dict]) -> None:
    membank.BANK.write_text(json.dumps(blocks))


# --- migration ---------------------------------------------------------------------------------------

def test_migration_upgrades_once_with_backup(bank):
    old = [
        {"id": "m1", "group": "core", "text": "The user's name is Alex.", "pinned": True, "created": _ago(40),
         "updated": _ago(40), "source": "user", "hits": 2},
        {"id": "m2", "group": "vocabulary", "text": "The user refers to 'Gemini' as 'Gemini' in voice commands.",
         "pinned": True, "created": _ago(3), "updated": _ago(3), "source": "auto", "hits": 0},
        {"id": "m3", "group": "vocabulary", "text": '"cloud code" means Claude Code.', "pinned": True,
         "created": _ago(3), "updated": _ago(3), "source": "user", "hits": 0},
        {"id": "m4", "group": "how-they-want-things-done", "text": "The user wants email drafts shown before sending.",
         "pinned": False, "created": _ago(5), "updated": _ago(5), "source": "auto", "hits": 0},
        {"id": "m5", "group": "Contacts", "text": "The user works with a person named Sam.", "pinned": False,
         "created": _ago(5), "updated": _ago(5), "source": "auto", "hits": 1},
        {"id": "m6", "group": "random-stuff", "text": "The user lives in Lisbon.", "pinned": False,
         "created": _ago(5), "updated": _ago(5), "source": "edited", "hits": 0},
    ]
    membank.BANK.write_text(json.dumps(old))
    blocks = {b["id"]: b for b in membank.all_blocks()}

    backups = list(membank.BANK.parent.glob("bank.json.pre-v2-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == old
    for b in blocks.values():
        for field in ("kind", "confirm", "last_used", "expires", "superseded_by", "origin", "about", "archived"):
            assert field in b, (b["id"], field)
        assert b["group"] in membank.GROUPS
    assert blocks["m1"]["pinned"] and blocks["m1"]["origin"] == "user"
    assert not blocks["m2"]["pinned"], "auto-extracted vocabulary must not stay pinned"
    assert blocks["m3"]["pinned"], "a correction the user gave stays pinned"
    assert blocks["m4"]["group"] == "preferences" and blocks["m4"]["kind"] == "profile"
    assert blocks["m5"]["group"] == "people" and blocks["m5"]["about"] == ["Sam"]
    assert blocks["m6"]["group"] == "places" and blocks["m6"]["origin"] == "edit"
    # Loading again changes nothing and makes no second backup.
    membank.all_blocks()
    assert len(list(membank.BANK.parent.glob("bank.json.pre-v2-*"))) == 1


# --- the prompt core -------------------------------------------------------------------------------

def _rendered(b: dict) -> str:
    return f"[{b['id']}] {b['text']}"


def test_core_text_whole_lines_profile_and_accounts_beat_vocab(bank):
    blocks = [mk(i, f"The user refers to 'thing number {i}' as 'thingy {i}' in voice commands.", "vocabulary")
              for i in range(1, 30)]
    blocks += [mk(40, "The user prefers drafts shown before anything is sent.", "preferences", kind="profile"),
               mk(41, "The user wants files copied, never moved, when archiving.", "preferences", kind="profile"),
               mk(42, "The user's work browser profile is called 'Work' and holds Slack.", "accounts", pinned=True),
               mk(43, "The user's personal browser profile holds Notion.", "accounts", pinned=True),
               mk(44, "The user's name is Alex.", "core", pinned=True, origin="user"),
               mk(45, "The user's team uses a weekly planning doc.", "work", updated=_ago(120), created=_ago(120)),
               mk(46, "Worked with Mint on the launch video.", "work", kind="episode",
                  day=time.strftime("%Y-%m-%d")),
               mk(47, "The user's old manager was Priya.", "people", superseded_by="m48"),
               mk(48, "The user's manager is Rahul.", "people", origin="user"),
               mk(49, "The user is on leave this week.", "schedule", expires=time.time() - 60),
               mk(50, "The user likes a stale thing.", "misc", archived=True)]
    save(blocks)
    by_id = {b["id"]: b for b in blocks}
    for budget in (500, 700, 900, 5000):
        core = membank.core_text(budget)
        assert len(core) <= budget
        for line in core.splitlines():
            if not line.startswith("- ["):
                continue
            b = by_id[line[3:line.index("]")]]
            if b["kind"] == "episode":
                assert line.endswith(f": {b['text']}"), f"cut line: {line!r}"
            else:
                rest = line[len(f"- {_rendered(b)}"):]
                assert line.startswith(f"- {_rendered(b)}") and (rest == "" or rest.startswith(" (noted")), \
                    f"cut or invented line: {line!r}"
        assert "[m40]" in core and "[m41]" in core and "[m44]" in core, "profile/core lines first"
        assert "[m42]" in core and "[m43]" in core, "pinned accounts beat vocabulary"
        assert "Priya" not in core and "on leave" not in core and "stale thing" not in core
    small = membank.core_text(700)
    assert "thing number" not in small.split("Lately")[0] or small.count("thing number") < 3
    assert "more saved facts" in small and "recall" in small
    full = membank.core_text(5000)
    assert "Lately:" in full and "launch video" in full
    assert "[m45] The user's team uses a weekly planning doc. (noted" in full and "may have changed" in full


def test_pinned_text_drops_lowest_whole_lines(bank):
    blocks = [mk(1, "The user's work profile is called 'Work'.", "accounts", pinned=True, origin="user",
                 created=_ago(60), updated=_ago(60))]
    blocks += [mk(i, f"The user says 'word {i}' for something else entirely in voice.", "vocabulary", pinned=True)
               for i in range(2, 40)]
    save(blocks)
    text = membank.pinned_text(400)
    assert len(text) <= 400
    lines = text.splitlines()
    assert "- (accounts) The user's work profile is called 'Work'." in lines, "the oldest line is not dropped"
    valid = {f"- ({b['group']}) {b['text']}" for b in blocks}
    assert all(line in valid for line in lines), "a line was cut"


# --- search ------------------------------------------------------------------------------------------

def test_multipliers():
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    month_ago = time.strftime("%Y-%m-%d", time.localtime(now - 30 * 86400))
    ten_days = time.strftime("%Y-%m-%d", time.localtime(now - 10 * 86400))
    assert membank.multiplier({"kind": "episode", "day": today}, now) == pytest.approx(1.0, abs=0.02)
    assert membank.multiplier({"kind": "episode", "day": ten_days}, now) == pytest.approx(0.5 ** (10 / 30), abs=0.02)
    assert membank.multiplier({"kind": "episode", "day": month_ago}, now) == pytest.approx(0.75)
    assert membank.multiplier({"kind": "fact", "confirm": 2}, now) == pytest.approx(1.3)
    assert membank.multiplier({"kind": "fact", "confirm": 20}, now) == pytest.approx(2.0)
    assert membank.multiplier({"kind": "fact", "last_used": now - 86400}, now) == pytest.approx(1.25)
    assert membank.multiplier({"kind": "fact", "last_used": now - 40 * 86400}, now) == pytest.approx(1.0)


def test_search_ranking_uses_confirmations_and_people(bank):
    save([mk(1, "The user plays tennis on Saturdays.", "schedule"),
          mk(2, "The user plays tennis on Sundays.", "schedule", confirm=4),
          mk(3, "The user's sister is named Maya.", "people", about=["Maya"]),
          mk(4, "Maya is a doctor in Porto.", "people", about=["Maya"])])
    found = membank.search("when does the user play tennis", k=2)
    assert [b["id"] for b in found] == ["m2", "m1"]
    assert {b["id"] for b in membank.search("what does maya do", k=3)} >= {"m3", "m4"}


def test_search_word_only_when_embeddings_fail(bank, monkeypatch):
    save([mk(1, "The user's car is a red hatchback.", "misc"), mk(2, "The user likes pizza.", "preferences")])
    calls = []
    monkeypatch.setattr(membank, "_embed", lambda texts: calls.append(texts) or _no_network())
    # No vectors stored: no embedding is even asked for.
    assert [b["id"] for b in membank.search("red hatchback")] == ["m1"]
    assert not calls
    # Vectors stored, but the API is down: still answers from words, and backs off.
    membank._write_vectors({"m1": {"h": membank._hash("The user's car is a red hatchback."), "m": "fake",
                                   "v": [1.0, 0.0]}})
    assert [b["id"] for b in membank.search("red hatchback")] == ["m1"]
    assert calls and membank._embedding["down_until"] > time.monotonic()


def _fake_embed(texts):
    concepts = [("car", "vehicle", "hatchback", "drive"), ("pizza", "food", "eat", "dinner"),
                ("tennis", "sport", "play")]
    out = []
    for t in texts:
        low = t.lower()
        v = [1.0 if any(w in low for w in words) else 0.0 for words in concepts] + [0.1]
        out.append(membank._unit(v))
    return "fake-embed", out


def test_search_meaning_finds_what_words_miss(bank, monkeypatch):
    monkeypatch.setattr(membank, "_embed", _fake_embed)
    save([mk(1, "The user owns a red hatchback.", "misc"), mk(2, "The user likes pizza.", "preferences"),
          mk(3, "The user plays tennis on Sundays.", "schedule")])
    assert membank.refresh_vectors() == 3
    assert json.loads(membank._p("vectors.json").read_text())["items"]["m1"]["m"] == "fake-embed"
    found = membank.search("which vehicle do I drive", k=1)
    assert [b["id"] for b in found] == ["m1"]
    assert membank.refresh_vectors() == 0, "vectors are cached by text hash"


# --- writing -----------------------------------------------------------------------------------------

def test_near_duplicates_are_confirmed_not_added(bank, monkeypatch):
    asked = []
    monkeypatch.setattr(llm, "ask_json", lambda prompt, *a, **k: asked.append(prompt) or None)
    first = membank.add("The user's manager is Priya.", "people", origin="auto")
    assert first.startswith("Remembered [m1]")
    again = membank.add("the users manager is priya", origin="user")
    assert again.startswith("Already known [m1]") and "confirmed 1x" in again
    membank.add("The user goes hiking in the Alps mountains with friends every weekend.", "schedule")
    near = membank.add("The user goes hiking in the Alps mountains with friends every single weekend.")
    assert "Already known [m2]" in near
    blocks = membank.all_blocks()
    assert len(blocks) == 2
    assert blocks[0]["confirm"] == 1 and blocks[0]["origin"] == "user", "the user saying it upgrades the origin"
    assert not asked, "duplicates never need the LLM"


def test_supersede_keeps_history_and_hides_the_old(bank, monkeypatch):
    membank.add("The user's manager at Acme is Priya.", "people")
    result = membank.add("The user's manager at Acme is Rahul.", supersedes="m1")
    assert "[m2]" in result and "replaces m1" in result
    old, new = membank.all_blocks()
    assert old["superseded_by"] == "m2" and old["text"].endswith("Priya.")
    assert new["history"][-1]["text"] == "The user's manager at Acme is Priya."
    assert "Priya" not in membank.core_text()
    assert [b["id"] for b in membank.search("manager at Acme", deep=True)] == ["m2"]
    assert "before: 'The user's manager at Acme is Priya.'" in membank.recall("who is my manager")
    # A similar note: ONE LLM call decides, here that it supersedes m2.
    prompts = []

    def decide(prompt, *a, **k):
        prompts.append(prompt)
        return {"action": "supersede", "target": "m2", "section": "people", "kind": "fact"}
    monkeypatch.setattr(llm, "ask_json", decide)
    result = membank.add("The user's manager at Acme is now Meera.", origin="auto")
    assert "replaces m2" in result and len(prompts) == 1
    assert [b["id"] for b in membank.search("manager Acme")] == ["m3"]
    assert membank.add("Something", supersedes="m99").startswith("No memory m99")


def test_add_refuses_secrets_and_never_pins_auto(bank):
    assert membank.add("my email password: hunter2-Secret!").startswith("Refused")
    membank.add("The user refers to 'cloud code' as Claude Code in voice commands.", "vocabulary", origin="auto",
                pinned=True)
    membank.add("The user's name is Alex.", "core", origin="user")
    a, b = membank.all_blocks()
    assert a["pinned"] is False and b["pinned"] is True


def test_episodes_dedupe_within_a_day_only(bank):
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    membank.add("Worked with Mint on the launch video.", origin="auto", kind="episode", day=yesterday)
    assert membank.add("Worked with Mint on the launch video.", origin="auto", kind="episode").startswith(
        "Remembered [m2]")
    assert membank.add("Worked with Mint on the launch video.", origin="auto", kind="episode").startswith(
        "Already known [m2]")


def test_expires_parsing():
    now = time.mktime(time.strptime("2026-10-06 12:00", "%Y-%m-%d %H:%M"))
    assert time.strftime("%Y-%m-%d", time.localtime(membank.parse_expires("2026-12-31", now))) == "2026-12-31"
    assert time.strftime("%Y-%m-%d", time.localtime(membank.parse_expires("in 2 weeks", now))) == "2026-10-20"
    assert time.strftime("%Y-%m-%d", time.localtime(membank.parse_expires("tomorrow", now))) == "2026-10-07"
    assert membank.parse_expires("whenever", now) is None


def test_recall_marks_used_and_forget_archives(bank):
    save([mk(1, "The user's dentist is Dr Lee.", "people"), mk(2, "The user parks on level 3.", "places")])
    out = membank.recall("dentist")
    assert "[m1]" in out and "(noted" in out
    b = membank.all_blocks()[0]
    assert b["hits"] == 1 and b["last_used"]
    assert "Relevant memories" in membank.recall_block("dentist") and membank.recall_block("zebra") == ""
    assert membank.update("[m2]", "The user parks on level 4.").startswith("Updated memory [m2]")
    gone = membank.forget("m1")
    assert "Forgotten [m1]" in gone and "for_good" in gone
    assert membank.search("dentist", deep=True) == [] and "[m1]" not in membank.core_text()
    assert membank.forget("m1", for_good=True).startswith("Deleted for good")
    assert [b["id"] for b in membank.all_blocks()] == ["m2"]


# --- learning from conversation ---------------------------------------------------------------------------

def test_extraction_keeps_durable_facts_only(bank, monkeypatch):
    facts = [{"text": "The user's sister is named Maya.", "section": "people", "kind": "fact", "about": ["Maya"]},
             {"text": "The user prefers dark mode in every app.", "section": "preferences", "kind": "profile"},
             {"text": "The user's battery is at 9%.", "section": "misc", "kind": "fact"},
             {"text": "The USD to EUR exchange rate is 0.92.", "section": "misc", "kind": "fact"},
             {"text": "The VS Code extension install is in progress.", "section": "work", "kind": "fact"},
             {"text": "Mint opened Visual Studio Code for the user.", "section": "work", "kind": "fact"},
             {"text": "The user lives in Lisbon.", "section": "places", "kind": "fact"}]   # 7th: over the cap

    def fake(prompt, *a, **k):
        return {"facts": facts} if "worth remembering" in prompt else None
    monkeypatch.setattr(llm, "ask_json", fake)
    added = membank.extract("user: hi\nmint: hello")
    texts = [b["text"] for b in membank.all_blocks()]
    assert texts == ["The user's sister is named Maya.", "The user prefers dark mode in every app."]
    assert len(added) == 2
    assert all(b["origin"] == "auto" and not b["pinned"] for b in membank.all_blocks())
    assert membank.all_blocks()[1]["kind"] == "profile"
    assert membank.transient("The user asked about AI news")
    assert not membank.transient("The user works at a design studio")


# --- the summary: cursor, catch-up, checkpoint ---------------------------------------------------------------

def _summary_llm(prompts):
    def generate(prompt, *a, **k):
        prompts.append(prompt)
        return json.dumps({"notes": "### In progress\n- (Oct 6) Plan the trip", "episodes": ["Planned a trip"]}), "m"
    return generate


def test_summary_catches_up_after_a_crash(bank, monkeypatch):
    first = memory_mod.Memory()
    first.add("user", "Let's plan the kayak trip to the lake")
    first.add("mint", "Sure, which weekend?")
    first.add("tool", "web_search(web search) -> " + "x" * 3000)
    assert first._pending_chars < memory_mod.SUMMARIZE_AFTER_CHARS, "tool lines count a quarter"
    del first                                          # "crash": nothing summarised
    # An old line after the cursor is outside the catch-up window.
    with open(memory_mod.HISTORY, "a") as f:
        f.write(json.dumps({"t": _ago(3), "role": "user", "text": "ancient line"}) + "\n")
    prompts = []
    monkeypatch.setattr(llm, "generate", _summary_llm(prompts))
    second = memory_mod.Memory()
    second.checkpoint(timeout=10)
    assert len(prompts) == 1 and "kayak trip" in prompts[0] and "ancient line" not in prompts[0]
    assert "x" * 100 not in prompts[0], "tool output is shortened for the summary"
    assert "Plan the trip" in memory_mod.SUMMARY.read_text()
    state = json.loads((memory_mod.HISTORY.parent / "memory" / "state.json").read_text())
    with open(memory_mod.HISTORY, "rb") as f:
        lines = f.read().splitlines(keepends=True)
    assert state["cursor"] == sum(len(x) for x in lines[:3]), "cursor at the last summarised line"
    episodes = [b for b in membank.all_blocks() if b["kind"] == "episode"]
    assert [b["text"] for b in episodes] == ["Planned a trip"] and episodes[0]["day"] == time.strftime("%Y-%m-%d")
    third = memory_mod.Memory()
    third.checkpoint(timeout=5)
    assert len(prompts) == 1, "nothing left to summarise after the cursor"


def test_checkpoint_keeps_turns_when_the_model_fails(bank, monkeypatch):
    mem = memory_mod.Memory()
    started = time.monotonic()
    mem.checkpoint(timeout=5)
    assert time.monotonic() - started < 0.5, "nothing pending: returns at once"
    mem.add("user", "remember the blue folder")
    mem.checkpoint(timeout=10)                         # llm.generate fails (fixture)
    assert [e["text"] for e in mem._pending] == ["remember the blue folder"]
    assert not memory_mod.SUMMARY.exists()
    prompts = []
    monkeypatch.setattr(llm, "generate", _summary_llm(prompts))
    mem.close(timeout=10)
    assert prompts and not mem._pending and memory_mod.SUMMARY.exists()


def test_compress_tool_keeps_failures():
    ok = memory_mod.compress_tool("read_url(read url · https://example.com) -> " + "page text " * 50)
    assert len(ok) < 130
    bad = memory_mod.compress_tool("open_app(Foo) -> FAILED: could not find the app Foo anywhere on this Mac, "
                                   "tried Spotlight and the Applications folder")
    assert "FAILED" in bad and "Applications folder" in bad


# --- rotation ------------------------------------------------------------------------------------------------

def test_rotation_moves_old_lines_and_recall_history_reads_them(bank, monkeypatch):
    rows = [{"t": "2026-09-01 10:00", "role": "user", "text": "we talked about the blue kayak"}]
    rows += [{"t": _ago(0.01), "role": "user", "text": f"recent line {i} " + "y" * 80} for i in range(40)]
    memory_mod.HISTORY.write_text("".join(json.dumps(r) + "\n" for r in rows))
    mem = memory_mod.Memory()
    mem._write_state(base=0, cursor=len("".join(json.dumps(r) + "\n" for r in rows[:30])))
    assert mem.rotate(max_bytes=2000, keep_bytes=1500)
    archived = list((memory_mod.HISTORY.parent / "history-archive").glob("history-*.jsonl"))
    assert len(archived) == 1
    old_lines = archived[0].read_text().splitlines()
    new_lines = memory_mod.HISTORY.read_text().splitlines()
    assert [json.loads(x) for x in old_lines + new_lines] == rows, "every line kept whole, in order"
    assert memory_mod.HISTORY.stat().st_size <= 2000, "kept from the cursor on (the unsummarised lines)"
    mem._caught_up = False
    mem._catch_up()
    assert mem._pending[0]["text"].startswith("recent line 29 "), "the cursor still points at the same line"
    prompts = []
    monkeypatch.setattr(llm, "generate", lambda prompt, *a, **k: (prompts.append(prompt) or "It was the kayak.", "m"))
    answer = journal.recall_history("what did we talk about", "2026-09-01")
    assert "blue kayak" in prompts[0] and "kayak" in answer


def test_add_rotates_past_the_limit(bank, monkeypatch):
    monkeypatch.setattr(memory_mod, "ROTATE_AT", 3000)
    monkeypatch.setattr(memory_mod, "KEEP_BYTES", 1000)
    mem = memory_mod.Memory()
    for i in range(40):
        mem.add("user", f"line {i} " + "z" * 100)
    assert memory_mod.HISTORY.stat().st_size <= 3000
    archived = "".join(p.read_text() for p in (memory_mod.HISTORY.parent / "history-archive").glob("*.jsonl"))
    texts = [json.loads(x)["text"] for x in (archived + memory_mod.HISTORY.read_text()).splitlines()]
    assert texts == [f"line {i} " + "z" * 100 for i in range(40)]


# --- consolidation -----------------------------------------------------------------------------------------

def test_consolidation_archives_merges_and_logs(bank, monkeypatch):
    save([mk(1, "The user once checked a parcel tracking page.", "misc", created=_ago(120), updated=_ago(120)),
          mk(2, "The user's gym is near the station.", "places", created=_ago(120), updated=_ago(120),
             last_used=time.time() - 86400),
          mk(3, "The user's accountant is Dana.", "people", origin="user", created=_ago(200), updated=_ago(200)),
          mk(4, "The user refers to 'Gemini' as 'Gemini' in voice commands.", "vocabulary", pinned=True),
          mk(5, "The user prefers tea over coffee.", "preferences", kind="profile"),
          mk(6, "The user likes tea more than coffee.", "preferences", kind="profile"),
          mk(7, "The current price of the plan is 20 dollars.", "misc"),
          mk(8, "The user uses a standing desk.", "misc")])
    seen = []

    def fake(prompt, *a, **k):
        seen.append(prompt)
        return {"merge": [{"ids": ["m5", "m6"], "text": "The user prefers tea over coffee."}],
                "archive": [{"id": "m7", "why": "a one-off price"}],
                "unpin": [{"id": "m4", "why": "not a correction"}],
                "promote": [{"id": "m8"}],
                "section": [{"id": "m8", "section": "preferences"}]}
    monkeypatch.setattr(llm, "ask_json", fake)
    result = membank.tidy(force=True)
    assert "archived" in result and seen
    assert "m4" in seen[0] and "Gemini" in seen[0], "pinned and vocabulary notes are reviewed too"
    blocks = {b["id"]: b for b in membank.all_blocks()}
    assert len(blocks) == 8, "nothing is deleted"
    assert blocks["m1"]["archived"], "auto, unpinned and unused for 90 days"
    assert not blocks["m2"]["archived"] and not blocks["m3"]["archived"]
    assert blocks["m7"]["archived"] and not blocks["m4"]["pinned"]
    assert blocks["m6"]["superseded_by"] == "m5" and blocks["m5"]["confirm"] >= 1
    assert blocks["m8"]["kind"] == "fact", "an unconfirmed auto note is not promoted to profile"
    assert blocks["m8"]["group"] == "preferences"
    changes = [json.loads(x) for x in membank._p("changes.jsonl").read_text().splitlines()]
    assert {c["kind"] for c in changes} >= {"archived", "merge", "unpinned", "regroup"}
    assert any(c.get("text") == "The user once checked a parcel tracking page." for c in changes)
    assert list((membank.BANK.parent / "backups").glob("bank-*.json"))
    assert membank.search("parcel tracking") == []
    assert [b["id"] for b in membank.search("parcel tracking", deep=True)] == ["m1"]
    assert membank.tidy() == "Tidied recently."


def test_consolidation_caps_changes(bank, monkeypatch):
    monkeypatch.setattr(membank, "MAX_CHANGES", 2)
    save([mk(i, f"The user once looked at thing {i}.", "misc", created=_ago(100), updated=_ago(100))
          for i in range(1, 7)])
    membank.tidy(force=True)
    assert sum(1 for b in membank.all_blocks() if b["archived"]) == 2
