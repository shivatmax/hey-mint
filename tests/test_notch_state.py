"""The notch's alert queue and timing (notch_state, ported from dotpals' notch-state.js): needs-you first and
held until answered, news timed from when it shows and held back while you're away, Esc snoozes, a notch you
opened closes 8 s after the pointer leaves or after a quiet minute, `armed`, the restless peek, next_wake()."""
import math

try:
    from mint.ui import notch_state
except ImportError:
    from mint import notch_state

NotchState = notch_state.NotchState
IN, OUT = True, False


def test_needs_you_first_and_held_until_answered():
    st = NotchState()
    st.alert("done:a", "done", 0.0, session="a")
    st.alert("need:b", "need", 1.0, session="b")
    assert st.shown().id == "need:b"
    assert st.queued() == 1
    st.tick(500.0)                                      # minutes later: still there
    assert st.shown().id == "need:b"
    st.resolve(501.0, id="need:b")
    assert st.shown().id == "done:a"                    # the news shows now, timed from now
    assert st.shown().shown_at == 501.0
    st.tick(505.9)
    assert st.shown().id == "done:a"
    st.tick(506.0)
    assert st.shown() is None


def test_done_5s_error_8s_from_when_shown():
    st = NotchState()
    st.alert("e", "error", 0.0)
    st.alert("d", "done", 0.0)
    assert st.shown().id == "e"
    st.tick(7.9)
    assert st.shown().id == "e"
    st.tick(8.0)
    assert st.shown().id == "d" and st.shown().shown_at == 8.0     # its 5 s start when it shows
    st.tick(12.9)
    assert st.shown().id == "d"
    st.tick(13.0)
    assert st.shown() is None


def test_news_pushed_aside_starts_over():
    st = NotchState()
    st.alert("d", "done", 0.0)
    st.tick(3.0)
    st.alert("n", "need", 3.0, session="x")
    assert st.shown().id == "n"
    assert next(a for a in st.alerts if a.id == "d").shown_at is None
    st.resolve(10.0, session="x")
    assert st.shown().id == "d" and st.shown().shown_at == 10.0


def test_duplicate_ids_ignored_and_unknown_kind_is_need():
    st = NotchState()
    assert st.alert("a", "done", 0.0)
    assert not st.alert("a", "done", 1.0)
    st.alert("b", "weird", 0.0)
    assert next(a for a in st.alerts if a.id == "b").kind == "need"


def test_news_waits_while_away_and_goes_stale():
    st = NotchState()
    st.idle(200)
    assert st.away
    st.alert("d", "done", 0.0)
    st.alert("n", "need", 0.0, session="s")
    assert st.shown().id == "n"                         # needs-you still shows while away
    st.resolve(1.0, id="n")
    assert st.shown() is None and st.queued() == 0      # news held back
    st.idle(2)
    st.tick(60.0)
    assert st.shown().id == "d" and st.shown().shown_at == 60.0
    st2 = NotchState()
    st2.idle(500)
    st2.alert("d", "done", 0.0)
    st2.tick(899.0)
    assert len(st2.alerts) == 1
    st2.tick(900.0)
    assert st2.alerts == []                             # dropped after 15 min


def test_pointer_movement_means_youre_back():
    st = NotchState()
    st.idle(500)
    st.pointer(OUT, 0.0, (10, 10))
    st.pointer(OUT, 0.1, (40, 10))
    assert not st.away


def test_esc_snoozes():
    st = NotchState()
    st.alert("n", "need", 0.0, session="s")
    st.alert("d", "done", 0.0)
    st.snooze(1.0)
    assert st.shown() is None
    assert [a.id for a in st.alerts] == ["n"]           # the news went, the need waits
    assert st.queued() == 1                             # "+1 waiting"
    st.click(2.0)                                       # opening the notch shows it again
    assert st.shown().id == "n"
    st.click(3.0)
    assert st.shown() is None


def test_opened_closes_8s_after_leaving():
    st = NotchState()
    st.pointer(IN, 0.0, (0, 0))
    assert st.click(0.5) == "opened"
    st.pointer(IN, 1.0, (1, 1))
    st.pointer(OUT, 1.05, (300, 300))                   # within the grace: still "over"
    assert st.hover
    st.pointer(OUT, 1.2, (300, 300))
    assert not st.hover and st.open.left_at == 1.0
    st.tick(8.9)
    assert st.is_open
    assert st.countdown(5.9) is None
    assert st.countdown(6.0) == (6.0, 9.0)             # the last 3 s show as a shrinking line
    st.tick(9.0)
    assert not st.is_open
    assert st.armed                                     # it closed with the pointer away


def test_coming_back_cancels_the_close():
    st = NotchState()
    st.pointer(IN, 0.0, (0, 0))
    st.click(0.1)
    st.pointer(OUT, 1.0, (500, 0))
    st.pointer(IN, 7.0, (0, 0))
    st.tick(20.0)
    assert st.is_open and st.countdown(20.0) is None


def test_quiet_minute_with_pointer_resting_then_not_armed():
    st = NotchState()
    st.pointer(IN, 0.0, (5, 5))
    st.click(0.0)
    for t in range(1, 57):
        st.pointer(IN, float(t), (5, 5))                # resting, not moving
    assert st.countdown(57.0) == (57.0, 60.0)
    st.pointer(IN, 59.9, (5, 5))
    assert st.is_open
    st.pointer(IN, 60.0, (5, 5))
    assert not st.is_open
    assert not st.armed                                 # closed under the pointer
    st.pointer(IN, 61.0, (5, 5))
    assert not st.peeking(70.0)                         # no peek until it leaves
    st.pointer(OUT, 71.0, (900, 900))
    assert st.armed


def test_moving_keeps_it_open():
    st = NotchState()
    st.pointer(IN, 0.0, (0, 0))
    st.click(0.0)
    for t in range(1, 120):
        st.pointer(IN, float(t), (t % 2 * 3, 0))        # small movements: you're using it
    assert st.is_open


def test_keep_holds_it_open():
    st = NotchState()
    st.pointer(IN, 0.0, (0, 0))
    st.click(0.0)
    st.pointer(OUT, 1.0, (500, 500))
    for t in range(2, 30):
        st.keep(float(t))                               # a menu from the notch is up
        st.tick(float(t))
    assert st.is_open


def test_click_toggles_and_disarms():
    st = NotchState()
    st.pointer(IN, 0.0, (0, 0))
    st.click(0.5)
    assert st.click(1.0) == "closed"
    assert not st.armed and not st.peeking(5.0)
    assert st.click(1.5) == "opened"                    # a click always opens, armed or not


def test_needs_you_blocks_the_auto_close():
    st = NotchState()
    st.click(0.0)                                       # pointer elsewhere: left_at = 0
    st.alert("n", "need", 1.0, session="s")
    st.tick(100.0)
    assert st.is_open and st.closes_at() is None
    st.resolve(101.0, id="n")
    assert not st.is_open                               # past its time: closes once answered


def test_restless_pointer_restarts_the_peek():
    st = NotchState()
    x = 0.0
    t = 0.0
    st.pointer(IN, t, (x, 0))
    while t < 2.0:                                      # sliding along the top edge, 7 pt every 0.1 s
        t += 0.1
        x += 7.0
        st.pointer(IN, t, (x, 0))
        assert not st.peeking(t)
    st.pointer(IN, t + 0.31, (x + 2, 0))                # stops: the dwell runs out
    assert st.peeking(t + 0.31)
    # Once peeking, moving to the buttons keeps the small row.
    st.pointer(IN, t + 0.5, (x + 40, 0))
    assert st.peeking(t + 0.5)
    assert st.rested(t + 0.5) == 0.0                    # (but the rest-to-open starts over)


def test_hovering_news_makes_it_yours():
    st = NotchState()
    st.pointer(IN, 0.0, (0, 0))
    st.alert("d", "done", 0.0)
    st.pointer(IN, 0.5, (0, 0))                         # resting there when it came: not adopted
    assert st.shown().id == "d" and not st.is_open
    st.pointer(IN, 1.0, (3, 0))                         # moved over it
    assert st.is_open and st.open.by == "hover"
    assert st.shown() is None
    st.tick(30.0)
    assert st.is_open                                   # no 5 s timeout any more


def test_open_by_voice_from_elsewhere_closes_later():
    st = NotchState()
    st.open_by("voice", 0.0)
    assert st.open.left_at == 0.0
    st.tick(7.9)
    assert st.is_open
    st.tick(8.0)
    assert not st.is_open


def test_sync_needs():
    st = NotchState()
    st.sync_needs({"a", "b"}, 0.0)
    assert {a.id for a in st.alerts} == {"need:a", "need:b"}
    st.alert("need:a", "need", 1.0, session="a")        # the event for it: already queued
    assert len(st.alerts) == 2
    st.sync_needs({"b"}, 2.0)
    assert [a.id for a in st.alerts] == ["need:b"]
    st.sync_needs(set(), 3.0)
    assert st.alerts == []


def test_resolve_by_session_and_kind():
    st = NotchState()
    st.alert("need:a", "need", 0.0, session="a")
    st.alert("done:a", "done", 0.0, session="a")
    st.resolve(1.0, session="a", kind="done")
    assert [a.id for a in st.alerts] == ["need:a"]
    st.resolve(1.0, session="a")
    assert st.alerts == []


def test_next_wake():
    st = NotchState()
    assert st.next_wake(0.0) == math.inf
    st.alert("d", "done", 0.0)
    assert st.next_wake(0.0) == 2.0                     # the countdown starts 3 s before the 5 s are up
    assert st.next_wake(2.5) == 2.5
    st.tick(5.0)
    st.idle(0)
    assert st.next_wake(5.0) == math.inf
    st.pointer(IN, 10.0, (0, 0))
    assert abs(st.next_wake(10.0) - 0.3) < 1e-9         # the peek's dwell
    st.click(10.5)
    st.pointer(OUT, 11.0, (400, 400))
    # left at 10.0 (the last look that found it over the notch): closes at 18, the line starts at 15
    assert abs(st.next_wake(11.0) - 4.0) < 1e-9


def test_derive():
    st = NotchState()
    st.alert("n", "need", 0.0, session="s")
    st.alert("e", "error", 0.0)
    d = st.derive(0.0)
    assert d["open"] and d["by"] == "alert" and d["alert"].id == "n" and d["queued"] == 1
    assert d["countdown"] is None                       # needs-you has no countdown
