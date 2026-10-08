"""Mint's own setup (setup_guide.py): what is set up, the "what can you do" card, and open_setup - which opens
Settings at the right page and part only when the thing asked for isn't set up yet."""
import inspect
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mint's tools import macOS frameworks")

try:
    from mint.tools import diet as tool_diet
    from mint.tools import registry as tools
    from mint.tools import setup_guide
except ImportError:
    from mint import setup_guide, tool_diet, tools


@pytest.fixture
def fake(monkeypatch):
    """Every check answers 'ready' unless set(feature, state, detail) says otherwise; Settings opens nowhere."""
    states = {}

    def check_for(fid):
        return lambda: dict(states.get(fid) or {"state": "ready", "detail": "fine"})
    for feature in setup_guide.FEATURES:
        monkeypatch.setattr(feature, "check", check_for(feature.id))
    opened = []
    setup_guide.set_opener(lambda page, anchor="": opened.append((page, anchor)))
    monkeypatch.setattr(setup_guide, "_name", lambda: "Alex")
    setup_guide.refresh()

    def set_(fid, state, detail="", **more):
        states[fid] = {"state": state, "detail": detail, **more}
        setup_guide.refresh()
    yield set_, opened
    setup_guide.set_opener(None)
    setup_guide.refresh()


@pytest.mark.parametrize("words,fid", [
    ("telegram", "telegram"), ("connect my phone", "telegram"), ("Connect Telegram", "telegram"),
    ("gmail", "google"), ("set up Gmail", "google"), ("google meet", "google_meet"), ("Google Meet", "google_meet"),
    ("shortcut", "apple_shortcuts"), ("make a shortcut", "apple_shortcuts"), ("openai key", "openai_key"),
    ("add an OpenAI key", "openai_key"), ("anthropic key", "model_keys"), ("claude code", "claude_code"),
    ("calendar access", "calendar"), ("google calendar", "google"), ("train my voice", "voice_training"),
    ("email control", "email_control"), ("hotkey", "keyboard_shortcuts"), ("typesafe", "typesafe_key"),
    ("gemini key", "gemini_key"), ("screen recording", "screen"), ("telegram_bot", "telegram")])
def test_words_find_the_right_feature(words, fid):
    assert setup_guide.resolve(words).id == fid


def test_status_reads_the_check_and_says_the_name(fake):
    set_, _ = fake
    set_("telegram", "missing", "No bot token yet")
    got = setup_guide.status("telegram")
    assert got["state"] == "missing" and got["detail"] == "No bot token yet"
    assert (got["page"], got["anchor"]) == ("accounts", "telegram")
    assert "{name}" not in got["enables"] and "Alex" in got["enables"]
    assert setup_guide.status("google")["state"] == "ready"


def test_status_is_cached_for_a_few_seconds(fake, monkeypatch):
    calls = []
    feature = setup_guide.get("google_meet")
    monkeypatch.setattr(feature, "check", lambda: calls.append(1) or {"state": "ready", "detail": ""})
    setup_guide.refresh()
    setup_guide.status("google_meet")
    setup_guide.status("google_meet")
    assert len(calls) == 1
    setup_guide.status("google_meet", fresh=True)
    assert len(calls) == 2


def test_a_failing_check_is_unknown_not_an_error(fake, monkeypatch):
    monkeypatch.setattr(setup_guide.get("telegram"), "check", lambda: 1 / 0)
    setup_guide.refresh()
    assert setup_guide.status("telegram")["state"] == "unknown"
    assert len(setup_guide.summary()) == len(setup_guide.FEATURES)


def test_open_setup_refuses_when_already_set_up(fake):
    _, opened = fake
    said = setup_guide.open_setup({"feature": "telegram", "reason": "connect telegram"})
    assert said.startswith("ALREADY SET UP") and opened == []


def test_open_setup_opens_the_right_page_and_part(fake):
    set_, opened = fake
    set_("telegram", "missing", "No bot token yet")
    said = setup_guide.open_setup({"feature": "connect my phone", "reason": "connect my phone"})
    assert opened == [("accounts", "telegram")]
    assert said.startswith("OPENED") and "@BotFather" in said and "two short sentences" in said
    set_("openai_key", "missing", "No key yet")
    setup_guide.open_setup({"feature": "openai key", "reason": "add an OpenAI key"})
    set_("calendar", "missing", "Not allowed yet")
    setup_guide.open_setup({"feature": "calendar", "reason": "give you calendar access"})
    assert opened[1:] == [("models", "model_keys"), ("storage", "permissions")]


def test_open_setup_uses_the_step_for_where_it_got_to(fake):
    set_, opened = fake
    set_("telegram", "partial", "On, but no phone paired yet", step="Send your bot the pairing code.")
    said = setup_guide.open_setup({"feature": "telegram", "reason": "set up telegram"})
    assert opened == [("accounts", "telegram")] and "pairing code" in said and "@BotFather" not in said


def test_open_setup_needs_a_reason_and_a_known_feature(fake):
    set_, opened = fake
    set_("telegram", "missing")
    assert setup_guide.open_setup({"feature": "telegram"}).startswith("NOT OPENED")
    assert setup_guide.open_setup({"feature": "zzqx", "reason": "x"}).startswith("NOT OPENED")
    assert opened == []


def test_open_setup_never_opens_for_what_is_not_on_this_mac(fake):
    set_, opened = fake
    set_("claude_code", "unavailable", "Claude Code isn't installed on this Mac")
    assert setup_guide.open_setup({"feature": "claude code", "reason": "connect claude"}).startswith("NOT OPENED")
    assert opened == []


def test_open_anyway_opens_a_set_up_feature_when_asked_to_show_it(fake):
    _, opened = fake
    said = setup_guide.open_setup({"feature": "telegram", "reason": "open the telegram settings", "open_anyway": True})
    assert said.startswith("OPENED") and opened == [("accounts", "telegram")]


def test_without_an_opener_it_says_where(fake):
    set_, opened = fake
    setup_guide.set_opener(None)
    set_("google_meet", "missing", "Not signed in yet")
    said = setup_guide.open_setup({"feature": "google meet", "reason": "set up google meet"})
    assert said.startswith("NOT OPENED") and "Accounts & connections" in said and "Open sign-in" in said


def test_status_tool_one_feature_and_all(fake):
    set_, opened = fake
    set_("telegram", "missing", "No bot token yet")
    one = setup_guide.setup_status({"feature": "telegram"})
    assert "Telegram: not set up" in one and "open_setup" in one
    every = setup_guide.setup_status({})
    assert "Not set up: Telegram" in every and "Permissions allowed" in every
    assert opened == []


def test_what_can_you_do_card_marks_what_needs_setup(fake, monkeypatch):
    set_, opened = fake
    shown = []
    try:
        from mint.tools import cards
    except ImportError:
        from mint import cards
    monkeypatch.setattr(cards, "show", lambda *a, **k: shown.append((a, k)))
    set_("calendar", "missing", "Not allowed yet")
    set_("telegram", "missing", "No bot token yet")
    set_("email_control", "missing", "Off")
    said = setup_guide.setup_status({"card": True})
    assert shown and opened == []
    rows = {r["title"]: r for r in shown[0][1]["items"]}
    assert len(rows) <= 7
    assert rows["Your day"]["trailing"] == "Set up" and "Calendar" in rows["Your day"]["detail"]
    assert rows["From your phone"]["detail"] == "Connect Telegram"
    assert rows["Do things on your Mac"]["trailing"] == "Ready"
    assert "Telegram, from your phone (needs: connect Telegram)" in said
    set_("email_control", "ready", "Watching your inbox")         # one way from the phone is enough
    rows = {r["title"]: r for r in setup_guide.groups()}
    assert rows["From your phone"]["trailing"] == "Ready"


def test_tools_are_declared_core_and_findable():
    full = tools.tools()
    names = {d.name for t in full for d in t.function_declarations or []}
    assert {"setup_status", "open_setup"} <= names
    assert {"setup_status", "open_setup"} <= tool_diet.CORE
    tool_diet.reset()
    tool_diet._on = True
    try:
        declared = {d.name for t in tool_diet.live_tools(full) for d in t.function_declarations}
        assert {"setup_status", "open_setup"} <= declared
        found = tool_diet.find("connect telegram")
        assert "open_setup" in found and "Already in your tools" in found
        tool_diet.reset()
        tool_diet._on = False
        tool_diet.remember(full)
        assert tool_diet.rank("open_setup")[0] == "open_setup"
    finally:
        tool_diet.reset()


def test_pages_and_anchors_exist_in_settings():
    try:
        from mint.ui import settings as settings_window
        from mint.ui import settings_models
    except ImportError:
        from mint import settings_models, settings_window
    pages = {key for key, _, _ in settings_window.PAGES}
    source = inspect.getsource(settings_window) + inspect.getsource(settings_models)
    import re
    anchors = set(re.findall(r"_anchor\(page, \"(\w+)\"\)", source))
    for feature in setup_guide.FEATURES:
        assert settings_window.page_for(feature.page) == feature.page in pages, feature.id
        if feature.anchor:
            assert feature.anchor in anchors, (feature.id, feature.anchor)


def test_the_tour_features_named_here_are_in_the_tour():
    pytest.importorskip("AppKit")
    try:
        from mint.ui import onboarding
    except ImportError:
        from mint import onboarding
    titles = {item[2] for item in onboarding.TOUR}
    assert set(setup_guide.TOUR_NEEDS) <= titles
    for needs in setup_guide.TOUR_NEEDS.values():
        assert all(setup_guide.get(n) for n in needs)
