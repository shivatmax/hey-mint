"""A "Set up" chip clicked in the open notch: it waits (spinner, the notch held open, the Mint pane says what to
do in macOS's box), then lands as a green tick that stays a moment before it shrinks away - and a click between
the chips never folds the notch."""
import time
import types

try:
    from mint.ui import notch as notch_mod
    from mint.ui import notch_tips
except ImportError:
    from mint import notch as notch_mod
    from mint import notch_tips


class _Orb:
    def __init__(self):
        self.hops = 0

    def hop(self):
        self.hops += 1

    def burst(self, *a, **k):
        pass


class _Strip:
    def __init__(self):
        self.calls = []

    def busy(self, key, on):
        self.calls.append(("busy", key, on))

    def done(self, key):
        self.calls.append(("done", key))

    def nope(self, key):
        self.calls.append(("nope", key))


def _notch(monkeypatch):
    n = notch_mod.Notch()
    strip = _Strip()
    n.home = {"tips": strip}
    n.orb = _Orb()
    n._mod = lambda name: notch_tips
    n._tips_facts = {"reminders": "ask", "calendar": "allowed"}
    n._cursor_back = lambda: None
    n._side_to_calendar = lambda: None
    n._tip_poll = lambda: None
    n._tips_check = lambda now, force=False: None
    asked = []
    monkeypatch.setattr(notch_tips, "ask", lambda key, done=None: asked.append(key))
    monkeypatch.setattr(notch_tips, "will_prompt", lambda key: True)
    monkeypatch.setattr(notch_mod, "_sfx", lambda name: None)
    later = []
    monkeypatch.setattr(notch_mod.AppHelper, "callLater", lambda delay, fn, *a: later.append(fn))
    return n, strip, asked, later


def test_click_waits_and_holds_the_notch_open(monkeypatch):
    n, strip, asked, later = _notch(monkeypatch)
    n._tip_clicked("reminders")
    assert ("busy", "reminders", True) in strip.calls
    assert "reminders" in n._tip_wait and "reminders" in n._tip_prompted
    assert n._holding()                                     # it doesn't fold while you answer macOS
    title, words = n._tip_words(time.monotonic())
    assert (title, words) == notch_tips.waiting_note("reminders", True)
    for fn in later:                                        # macOS is asked a beat later, not inside the click
        fn()
    assert asked == ["reminders"]
    n._tip_clicked("reminders")                             # a second click while waiting does nothing
    assert asked == ["reminders"]


def test_allowed_lands_as_a_tick_that_lingers(monkeypatch):
    n, strip, asked, later = _notch(monkeypatch)
    n._tip_clicked("reminders")
    n._tip_success("reminders")
    assert ("done", "reminders") in strip.calls and n.orb.hops == 1
    assert "reminders" not in n._tip_wait
    assert n._tips_facts["reminders"] == "allowed"          # the chip won't come back before the next read
    assert n._tip_party["reminders"] > time.monotonic()     # green for a moment before it goes
    assert n._tip_words(time.monotonic()) == notch_tips.done_note("reminders")
    assert n._holding()


def test_denied_shakes_and_says_how_to_turn_it_on(monkeypatch):
    n, strip, asked, later = _notch(monkeypatch)
    n._tip_clicked("reminders")
    n._tip_failed("reminders")
    assert ("nope", "reminders") in strip.calls
    assert n._tip_words(time.monotonic()) == notch_tips.DENIED


def test_hover_explains_the_chip(monkeypatch):
    n, strip, asked, later = _notch(monkeypatch)
    n._tip_hovered("calendar")
    assert n._tip_words(time.monotonic()) == (None, notch_tips.tip("calendar")[4])
    n._tip_hovered(None)
    assert n._tip_words(time.monotonic()) is None


def test_closing_a_waiting_chip_stops_waiting(monkeypatch):
    n, strip, asked, later = _notch(monkeypatch)
    store = {}
    monkeypatch.setattr(notch_tips.prefs, "get", lambda key: store.get(key, []))
    monkeypatch.setattr(notch_tips.prefs, "set", lambda key, value: store.__setitem__(key, value))
    n._tip_clicked("reminders")
    n._tip_closed("reminders")
    assert "reminders" not in n._tip_wait and store[notch_tips.PREF] == ["reminders"]


def test_no_box_from_macos_opens_system_settings_and_keeps_waiting(monkeypatch):
    n, strip, asked, later = _notch(monkeypatch)
    try:
        from mint.core import permissions
    except ImportError:
        from mint import permissions
    panes = []
    monkeypatch.setattr(permissions, "status", lambda kind, asked=(): "ask")
    monkeypatch.setattr(permissions, "open_pane", panes.append)
    n._tip_clicked("reminders")
    n._tip_answer("reminders", False)
    assert panes == ["reminders"] and "reminders" in n._tip_wait
    assert n._tip_words(time.monotonic())[1] == notch_tips.SETTINGS_WORDS
    assert "reminders" not in n._tip_prompted       # (from now on only the switch says yes)
    n._tip_answer("reminders", True)
    assert ("done", "reminders") in strip.calls


def test_a_no_in_the_box_is_a_no(monkeypatch):
    n, strip, asked, later = _notch(monkeypatch)
    try:
        from mint.core import permissions
    except ImportError:
        from mint import permissions
    monkeypatch.setattr(permissions, "status", lambda kind, asked=(): "denied")
    n._tip_clicked("reminders")
    n._tip_answer("reminders", False)
    assert ("nope", "reminders") in strip.calls and "reminders" not in n._tip_wait


def test_set_up_pill_brings_the_chips_over_a_song(monkeypatch):
    n, strip, asked, later = _notch(monkeypatch)
    n.st.keep = lambda now: None
    assert n._setup_until < time.monotonic()
    n._show_setup()                                          # the header's "Set up · 2" over the player
    assert n._setup_until > time.monotonic() + 15
    n._setup_until = 0.0
    n._tip_hovered("calendar")                               # on a chip: it stays up
    assert n._setup_until > time.monotonic() + 15
