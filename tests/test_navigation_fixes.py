"""Fixes from a failed "open Telegram and find BotFather" (9 Oct): Mint's own click-through glow no longer counts as a
window over the app, named clicks are allowed in chat apps, misnamed tool calls are taken for the right tool, and the
built-in skills teach the way round an app that shows Accessibility nothing."""
from pathlib import Path

try:
    from mint.app import telegram
    from mint.screen import ocr
    from mint.tools import diet as tool_diet
except ImportError:
    from mint import ocr, telegram, tool_diet


def test_click_through_windows_cover_nothing(monkeypatch):
    import Quartz
    rows = [{"kCGWindowLayer": 0, "kCGWindowAlpha": 1.0, "kCGWindowNumber": 77, "kCGWindowOwnerName": "python",
             "kCGWindowOwnerPID": 1, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 900, "Height": 700}},
            {"kCGWindowLayer": 0, "kCGWindowAlpha": 1.0, "kCGWindowNumber": 12, "kCGWindowOwnerName": "Telegram",
             "kCGWindowOwnerPID": 2, "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 900, "Height": 700}}]
    monkeypatch.setattr(Quartz, "CGWindowListCopyWindowInfo", lambda *a: rows)
    monkeypatch.setattr(ocr, "_click_through", lambda: {77})            # the glow round Telegram
    assert [r["app"] for r in ocr._layer0()] == ["Telegram"]
    assert ocr._top_at(ocr._layer0(), 300, 200)["app"] == "Telegram"
    monkeypatch.setattr(ocr, "_click_through", lambda: set())           # a real window of Mint's does cover
    assert ocr._top_at(ocr._layer0(), 300, 200)["app"] == "python"


def test_named_clicks_are_allowed_in_chat_apps(monkeypatch):
    monkeypatch.setattr(telegram, "_frontmost", lambda: "Telegram")
    guard = lambda args: telegram.send_guard("click_at", args, "open telegram and find botfather")  # noqa: E731
    assert guard({"target": "BotFather", "x": 200, "y": 150}) == ""
    assert guard({"target": "Search", "x": 180, "y": 50}) == ""
    assert guard({"x": 500, "y": 900}).startswith("REFUSED")             # unnamed: could be Send
    assert guard({"target": "Send button"}).startswith("REFUSED")
    assert telegram.send_guard("press_key", {"key": "return"}, "find botfather").startswith("REFUSED")


def test_misnamed_tool_calls_are_taken_for_the_right_tool():
    assert tool_diet._alias("pressed_key") == "press_key"
    assert tool_diet._alias("typed_text") == "type_text"
    assert tool_diet._alias("press_key") == "press_key"
    assert tool_diet._alias("set_timer") == "set_timer"                  # a real tool stays itself
    assert tool_diet.unwrap("pressed_key", {"key": "k"})[0] == "press_key"


def test_the_basics_are_built_in_not_skills():
    """9 Oct (the user): a new Mint knows how to work any app, the browser, files and chat apps out of the box - as
    built-in how-to the router hands over (guides.py), not as starter skills; skills are what it learns."""
    try:
        from mint.core import guides
    except ImportError:
        from mint import guides
    assert "type_text the name ONCE without Return" in guides.CHAT_APPS and "lookalike" in guides.CHAT_APPS
    assert "lists nothing" in guides.CLICKING and "repeat a call that failed" in guides.CLICKING
    assert "a dialog, menu or sign-in sheet" in guides.CLICKING                    # a click that does nothing
    here = Path(tool_diet.__file__).resolve().parent
    shipped = here.parent / "resources" / "skills"
    if not (here / "seed_skills").exists():                                       # the public app: nothing extra
        assert not list(shipped.rglob("*.md")) if shipped.exists() else True
    families = {g for info in tool_diet.FAMILIES.values() for g in info["prompts"] if g.startswith("guides.")}
    names = {n for n in dir(guides) if n.isupper()}
    assert {f"guides.{n}" for n in names} == families                              # every chunk can be handed over


def test_unused_starter_skills_are_refreshed_but_used_ones_kept(monkeypatch, tmp_path):
    try:
        from mint.knowledge import skills as skillbook
    except ImportError:
        from mint import skillbook
    seeds, root = tmp_path / "seeds", tmp_path / "skills"
    (seeds / "general").mkdir(parents=True)
    (root / "general").mkdir(parents=True)
    head = "---\ntitle: T\nwhen: w\napps:\nsource: seed\nuses: {uses}\nwins: 0\nfails: 0\n---\n"
    (seeds / "general" / "a.md").write_text(head.format(uses=0) + "## Steps\n1. new way\n")
    (seeds / "general" / "b.md").write_text(head.format(uses=0) + "## Steps\n1. new way\n")
    (root / "general" / "a.md").write_text(head.format(uses=0).replace("source: seed", "source: seed\ncreated_by: seed")
                                           + "## Steps\n1. old way\n")
    (root / "general" / "b.md").write_text(head.format(uses=3).replace("source: seed", "source: seed\ncreated_by: seed")
                                           + "## Steps\n1. old way, used\n")
    (root / ".seeded").write_text("general/a.md\ngeneral/b.md\n")
    monkeypatch.setattr(skillbook, "SEEDS", seeds)
    monkeypatch.setattr(skillbook, "ROOT", root)
    monkeypatch.setattr(skillbook, "_seeded_once", False)
    skillbook._seed()
    assert "new way" in (root / "general" / "a.md").read_text()
    assert "old way, used" in (root / "general" / "b.md").read_text()


def test_after_two_misses_the_next_way_to_try_is_given():
    try:
        from mint.app import session
    except ImportError:
        from mint import session
    mint = object.__new__(session.Mint)
    mint._asked_at = 5.0
    assert session._ladder(mint, "click_text", "CONFIRMED: clicked") == ""
    assert session._ladder(mint, "ui_act", "FAILED: could not find 'Search box'") == ""
    hint = session._ladder(mint, "click_text", "No text is visible in Telegram's window.")
    assert "2 tries" in hint and "open_app it again" in hint and "click_text" in hint
    assert session._ladder(mint, "remember", "FAILED") == ""                       # not a screen action
    mint._asked_at = 9.0                                                 # a new request starts afresh
    assert session._ladder(mint, "ui_act", "FAILED: x") == ""


def test_a_turn_with_many_tool_steps_is_not_mistaken_for_a_full_context():
    from types import SimpleNamespace
    try:
        from mint.app import session
    except ImportError:
        from mint import session
    mint = object.__new__(session.Mint)
    mint._watch_context(SimpleNamespace(total_token_count=321_569), steps=12)
    assert mint._context_tokens < 30_000
    assert not getattr(mint, "_auto_compacting", False)


def test_starter_skills_that_no_longer_ship_leave_unless_used(monkeypatch, tmp_path):
    try:
        from mint.knowledge import skills as skillbook
    except ImportError:
        from mint import skillbook
    seeds, root = tmp_path / "seeds", tmp_path / "skills"
    (root / "general").mkdir(parents=True)
    head = "---\ntitle: {t}\nwhen: w\napps:\nsource: {src}\ncreated_by: seed\nuses: {uses}\nwins: 0\nfails: 0\n---\n"
    (root / "general" / "a.md").write_text(head.format(t="A", src="seed", uses=0) + "## Steps\n1. basic\n")
    (root / "general" / "b.md").write_text(head.format(t="B", src="seed", uses=4) + "## Steps\n1. used\n")
    (root / "general" / "c.md").write_text(head.format(t="C", src="seed+edited", uses=0) + "## Steps\n1. edited\n")
    (root / ".seeded").write_text("general/a.md\ngeneral/b.md\ngeneral/c.md\n")
    monkeypatch.setattr(skillbook, "SEEDS", seeds)                  # nothing ships any more
    monkeypatch.setattr(skillbook, "ROOT", root)
    monkeypatch.setattr(skillbook, "_seeded_once", False)
    skillbook._seed()
    assert not (root / "general" / "a.md").exists() and list((root / skillbook.ARCHIVE).rglob("a.md"))
    assert (root / "general" / "b.md").exists() and (root / "general" / "c.md").exists()


def test_a_mid_job_model_switch_carries_the_job_on():
    import time
    try:
        from mint.app import autopilot, session
    except ImportError:
        from mint import autopilot, session
    mint = object.__new__(session.Mint)
    mint.task = None
    mint._user_lines = [(time.time(), "don't search with @, the chat is already there")]
    autopilot._reset("open telegram, find botfather and copy the token")
    note = session._carry_on_note(mint)
    assert note.startswith("(Mint - not the user") and "Carry on" in note
    assert "find botfather" in note and "don't search with @" in note


def test_something_asked_for_on_screen_is_done_in_front_not_in_the_background(monkeypatch):
    try:
        from mint.app import background
    except ImportError:
        from mint import background
    monkeypatch.setattr(background, "running_note", lambda: "")
    asked = "open the Telegram then search the text bot father, don't use @"
    task = 'Open Telegram, search for "botfather" in the search bar, and select the BotFather contact.'
    assert background.keep_in_front(task, asked).startswith("NOT STARTED in the background")
    assert background.keep_in_front("Research the best laptops and put them in a note", "research laptops") == ""
    assert background.keep_in_front("Open Telegram and search BotFather", "do it in the background") == ""
    monkeypatch.setattr(background, "running_note", lambda: "Background jobs still running: task-2")
    assert background.keep_in_front(task, asked) == ""                 # another job runs: this one may queue


def test_a_window_that_gives_no_picture_at_first_is_tried_again(monkeypatch):
    try:
        from mint.screen import capture
    except ImportError:
        from mint import capture
    tries = []
    monkeypatch.setattr(capture, "window_bounds", lambda wid: (0, 0, 400, 300))
    monkeypatch.setattr(capture, "_cg", lambda ids, area: tries.append("cg") or (None if len(tries) < 2 else "img"))
    monkeypatch.setattr(capture, "_sck", lambda wid: None)
    monkeypatch.setattr(capture, "_screencapture", lambda wid: None)
    monkeypatch.setattr(capture, "_shot", lambda image, bounds, wid, method: {"image": image, "method": method})
    monkeypatch.setattr(capture.time, "sleep", lambda s: None)
    assert capture.window(3783)["image"] == "img" and len(tries) == 2
