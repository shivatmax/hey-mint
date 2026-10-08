"""The notch's "Set up" chips (notch_tips): a chip only for what isn't set up yet, most important first, at most
MAX, never one the user closed - and nothing before the statuses are known."""
try:
    from mint.ui import notch_tips
except ImportError:
    from mint import notch_tips

NOTHING = {"calendar": "ask", "accessibility": "ask", "screen": "denied", "reminders": "ask",
           "claude_installed": True, "claude_connected": False, "shelf_used": False, "remote_on": False}
EVERYTHING = {"calendar": "allowed", "accessibility": "allowed", "screen": "allowed", "reminders": "allowed",
              "claude_installed": True, "claude_connected": True, "shelf_used": True, "remote_on": True}
ORDER = [key for key, *_ in notch_tips.TIPS]


def test_nothing_set_up_shows_the_most_important_first():
    assert notch_tips.pick(NOTHING) == ORDER[:notch_tips.MAX]
    assert ORDER[0] == "calendar"
    assert notch_tips.pick(NOTHING, limit=99) == ORDER


def test_everything_set_up_shows_nothing():
    assert notch_tips.pick(EVERYTHING) == []


def test_unknown_statuses_show_nothing():
    assert notch_tips.pick({}) == []


def test_only_what_is_missing():
    facts = dict(EVERYTHING, calendar="denied", shelf_used=False)
    assert notch_tips.pick(facts) == ["calendar", "shelf"]


def test_claude_code_only_when_installed_and_not_connected():
    assert "claude" not in notch_tips.pick(dict(EVERYTHING, claude_installed=False, claude_connected=False))
    assert "claude" in notch_tips.pick(dict(EVERYTHING, claude_connected=False))
    assert "claude" not in notch_tips.pick(dict(EVERYTHING, claude_installed=True))


def test_dismissed_stays_hidden_and_the_next_one_moves_up():
    shown = notch_tips.pick(NOTHING, dismissed=["calendar"])
    assert "calendar" not in shown
    assert shown == [k for k in ORDER if k != "calendar"][:notch_tips.MAX]
    assert notch_tips.pick(dict(EVERYTHING, calendar="ask"), dismissed=("calendar",)) == []


def test_limit():
    assert len(notch_tips.pick(NOTHING, limit=2)) == 2
    assert notch_tips.pick(NOTHING, limit=0) == []


def test_dismiss_is_remembered(monkeypatch):
    store = {}
    monkeypatch.setattr(notch_tips.prefs, "get", lambda key: store.get(key, []))
    monkeypatch.setattr(notch_tips.prefs, "set", lambda key, value: store.__setitem__(key, value))
    notch_tips.dismiss("shelf")
    notch_tips.dismiss("shelf")
    notch_tips.dismiss("phone")
    assert store[notch_tips.PREF] == ["shelf", "phone"]
    assert notch_tips.pick(NOTHING, notch_tips.dismissed(), limit=99) == [
        k for k in ORDER if k not in ("shelf", "phone")]


def test_every_tip_has_an_icon_label_and_tooltip():
    for key, symbol, rgb, label, why in notch_tips.TIPS:
        assert symbol and label and why and len(rgb) == 3
        assert len(label) <= 14                       # chips sit side by side in the Mint pane
    assert set(notch_tips.PERMISSION_TIPS) <= set(ORDER)
