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


def test_a_search_box_takes_one_query(monkeypatch):
    try:
        from mint.screen import axkit
        from mint.tools import everyday as skills
    except ImportError:
        from mint import axkit, skills
    fields = {"tg": {"AXRole": "AXTextField", "AXPlaceholderValue": "Search (⌘K)"},
              "note": {"AXRole": "AXTextArea", "AXDescription": "Message"},
              "mac": {"AXRole": "AXTextField", "AXSubrole": "AXSearchField"}}
    monkeypatch.setattr(axkit, "attr", lambda element, key: fields[element].get(key))
    assert skills._is_search("tg") and skills._is_search("mac")
    assert not skills._is_search("note")                  # a message box keeps what is there


def test_the_voice_connection_is_renewed_while_asleep():
    import time
    try:
        from mint.app import session
    except ImportError:
        from mint import session

    class Loop:
        def __init__(self):
            self.made = []

        def create_task(self, coro):
            self.made.append(coro)
            coro.close()
    mint = object.__new__(session.Mint)
    mint.loop, mint.session, mint._busy = Loop(), object(), False
    mint._server_at = mint._tools_at = time.monotonic() - 1000
    mint._connected_at = time.monotonic() - 60                 # young: left alone
    mint._renew_if_stale()
    assert not mint.loop.made
    mint._connected_at = time.monotonic() - session.Mint.STALE_AFTER - 5
    mint._renew_if_stale()
    assert len(mint.loop.made) == 1 and mint._restarting


def test_screen_on_but_not_working_says_so(monkeypatch):
    try:
        from mint.core import permit
    except ImportError:
        from mint import permit
    opened = []
    monkeypatch.setattr(permit, "_times", {})
    import Quartz
    monkeypatch.setattr(Quartz, "CGPreflightScreenCaptureAccess", lambda: True)    # macOS says "on"...
    monkeypatch.setattr(permit.permissions, "status", lambda kind, asked=(): "denied")  # ...but it can't see
    monkeypatch.setattr(permit.permissions, "open_pane", lambda kind: opened.append(kind))
    monkeypatch.setattr(permit.permissions, "ask", lambda *a, **k: opened.append("asked"))
    said = permit.request("screen", "look")
    assert opened == ["screen"] and "switch" in said and "off and on" in said and "don't guess" in said


def test_words_already_in_the_field_are_not_typed_again():
    try:
        from mint.tools import everyday as skills
    except ImportError:
        from mint import skills
    assert skills._already_holds("BotFather", "BotFather")
    assert skills._already_holds("botfather ", "BotFather")
    assert not skills._already_holds("Hello", "BotFather")
    assert not skills._already_holds("", "BotFather")


def test_asking_how_a_job_is_going_is_recognised():
    try:
        from mint.app import session
    except ImportError:
        from mint import session
    for asked in ("what its doing", "what is it doing?", "ask for how much done because i don't see anything working",
                  "any update?", "is it still working"):
        assert session._ASKS_PROGRESS.search(asked), asked
    for other in ("open telegram", "do the thing", "make a note"):
        assert not session._ASKS_PROGRESS.search(other), other


def test_a_job_that_keeps_failing_changes_route_then_ends_honestly(monkeypatch):
    from types import SimpleNamespace
    try:
        from mint.app import background
    except ImportError:
        from mint import background
    told = []
    monkeypatch.setattr(background, "_tell_stuck", lambda run: told.append(run.id))
    run = SimpleNamespace(id="task-1", task="open the Dharamshala group", title="Telegram group")
    notes = [background._stuck_note(run, "click_text", "Could not read the screen: could not capture window 4971")
             for _ in range(background.STUCK_AFTER)]
    assert notes[-1].startswith("\n[STUCK") and "Export" in notes[-1] and told == ["task-1"]
    assert background._stuck_note(run, "read_window", "Telegram - Telegram\nDharamshala") == ""   # it worked
    for _ in range(background.GIVE_UP_AFTER - 1):
        note = background._stuck_note(run, "ui_act", "FAILED: could not find 'Search box'")
    assert background._stuck_note(run, "ui_act", "FAILED: x").startswith("\n[END THE JOB NOW")


def test_a_box_from_gemini_becomes_a_screen_point_on_the_matching_words(monkeypatch):
    import PIL.Image
    try:
        from mint.screen import ground
    except ImportError:
        from mint import ground
    image = PIL.Image.new("RGB", (1600, 1000))               # an 800 x 500 point window on a Retina screen
    monkeypatch.setattr(ground, "screenshot", lambda area, wid=None: (image, 2.0))
    # Gemini's box_2d for the "Dharamshala Trip" row: [ymin, xmin, ymax, xmax], 0-1000 across the picture sent.
    monkeypatch.setattr(ground, "_generate", lambda *a, **k: ('[{"box_2d": [410, 30, 530, 400], "label": "row"}]',
                                                              "gemini-test"))
    words = [{"text": "Dharamshala Trip", "x": 200, "y": 290, "w": 120, "h": 20},
             {"text": "Premium Deals", "x": 200, "y": 440, "w": 110, "h": 20}]
    point, box, why = ground.box_by_vision("the Dharamshala Trip chat", (100, 50, 800, 500), None, words)
    assert box is not None and 120 <= box[0] <= 130 and 250 <= box[1] <= 260      # x0 = 100 + 0.03*800 ...
    assert point == (260.0, 300.0) and "on the words" in why                       # snapped to the label itself
    monkeypatch.setattr(ground, "_generate", lambda *a, **k: ("[]", "gemini-test"))
    assert ground.box_by_vision("a missing thing", (0, 0, 800, 500), None, [])[0] is None


def test_screen_recording_that_shows_nothing_counts_as_off(monkeypatch):
    import Quartz
    try:
        from mint.core import permissions
        from mint.screen import vision
    except ImportError:
        from mint import permissions, vision
    monkeypatch.setattr(Quartz, "CGPreflightScreenCaptureAccess", lambda: True)
    monkeypatch.setattr(permissions, "_blind_seen", {"at": -1e9, "blind": False})
    monkeypatch.setattr(vision, "sees_other_apps", lambda: False)
    assert permissions.status("screen") == "denied"          # so the Set up chip, Settings and the ask flow show it
    monkeypatch.setattr(permissions, "_blind_seen", {"at": -1e9, "blind": False})
    monkeypatch.setattr(vision, "sees_other_apps", lambda: True)
    assert permissions.status("screen") == "allowed"


def test_mint_is_blind_when_another_apps_window_cannot_be_pictured(monkeypatch):
    import PIL.Image
    import PIL.ImageDraw
    import Quartz
    try:
        from mint.screen import vision
    except ImportError:
        from mint import vision
    telegram = [{"kCGWindowOwnerPID": 4242, "kCGWindowLayer": 0, "kCGWindowAlpha": 1.0, "kCGWindowNumber": 3783,
                 "kCGWindowBounds": {"X": 846, "Y": 29, "Width": 380, "Height": 842}}]
    monkeypatch.setattr(Quartz, "CGWindowListCopyWindowInfo", lambda *a: telegram)
    monkeypatch.setattr(Quartz, "CGImageGetWidth", lambda picture: 760)

    def probe(picture):
        vision._probe_seen.update(at=-1e9)
        return vision._probe_other_window()
    monkeypatch.setattr(Quartz, "CGWindowListCreateImage", lambda *a: None)          # macOS refuses: blind
    assert probe(None) is False and vision.blind_to_others()
    window = PIL.Image.new("RGB", (760, 1684), (30, 30, 30))
    PIL.ImageDraw.Draw(window).rectangle((40, 40, 700, 100), fill=(220, 220, 220))     # Telegram's search box
    monkeypatch.setattr(Quartz, "CGWindowListCreateImage", lambda *a: object())
    monkeypatch.setattr(vision, "_cg_to_pil", lambda picture: window)
    assert probe(None) is True and not vision.blind_to_others()
    monkeypatch.setattr(vision, "_cg_to_pil", lambda picture: PIL.Image.new("RGB", (760, 1684), (46, 46, 52)))
    assert probe(None) is False                                                        # a flat grey stand-in
    monkeypatch.setattr(Quartz, "CGWindowListCopyWindowInfo", lambda *a: [])
    assert probe(None) is None and not vision.blind_to_others()                        # nothing to try: can't tell
