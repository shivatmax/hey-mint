"""Hold to talk (10 Oct): hold fn+⌃, talk - no "Hey Mint" - let go and Mint works on it. Hold fn alone to dictate.
The two share fn: fn held, then ⌃ joins = talking to Mint, so the dictation that began is cancelled, not typed."""
import asyncio

try:
    from mint.core import hotkeys, prefs
except ImportError:
    from mint import hotkeys, prefs

FN, CTRL = 1 << 23, 1 << 18


class _Event:
    def __init__(self, code, flags):
        self.code, self.flags = code, flags

    def keyCode(self):
        return self.code

    def modifierFlags(self):
        return self.flags


def test_defaults_are_fn_for_dictation_and_fn_ctrl_to_talk():
    keys = prefs.DEFAULTS["shortcuts"]
    assert keys["dictate"] == "fn" and keys["talk_hold"] == "fn+ctrl"
    assert hotkeys.is_chord("fn+ctrl") and not hotkeys.is_chord("ctrl+x") and not hotkeys.is_chord("fn")
    assert hotkeys.display("fn+ctrl") == "fn ⌃"


def test_chord_holds_while_both_are_down_and_releases_when_one_lets_go():
    calls = []
    chord = hotkeys.ModifierChord("fn+ctrl", lambda: calls.append("hold"), lambda: calls.append("release"))
    pending = []
    later = lambda delay, fn: pending.append(fn)                            # noqa: E731
    chord.flags(_Event(63, FN), later)                                    # fn down: not yet
    assert pending == [] and calls == []
    chord.flags(_Event(59, FN | CTRL), later)                             # ⌃ joins: the chord
    pending.pop()()
    assert calls == ["hold"]
    chord.flags(_Event(59, FN), later)                                    # ⌃ up: let go
    assert calls == ["hold", "release"]


def test_a_key_before_the_hold_is_a_shortcut_not_talk():
    calls = []
    chord = hotkeys.ModifierChord("fn+ctrl", lambda: calls.append("hold"), lambda: calls.append("release"))
    pending = []
    chord.flags(_Event(59, FN | CTRL), lambda d, fn: pending.append(fn))
    chord.key(_Event(123, FN | CTRL))                                     # ⌃fn-← quickly
    pending.pop()()
    chord.flags(_Event(59, 0), None)
    assert calls == []


def test_fn_dictation_is_cancelled_when_ctrl_joins(monkeypatch):
    calls = []
    hold = hotkeys.ModifierHold("fn", lambda: calls.append("dictate"), lambda: calls.append("type it"),
                                on_cancel=lambda: calls.append("cancel"))
    try:
        from PyObjCTools import AppHelper
    except ImportError:
        return
    monkeypatch.setattr(AppHelper, "callLater", lambda delay, fn: fn())    # the hold begins at once
    hold._flags(_Event(63, FN))
    assert calls == ["dictate"]
    hold._flags(_Event(59, FN | CTRL))                                    # ⌃ joins: talk to Mint instead
    hold._flags(_Event(59, FN))
    hold._flags(_Event(63, 0))                                            # fn up: nothing is typed
    assert calls == ["dictate", "cancel"]


def test_ctrl_then_fn_never_starts_dictation(monkeypatch):
    calls = []
    hold = hotkeys.ModifierHold("fn", lambda: calls.append("dictate"), lambda: calls.append("type it"))
    try:
        from PyObjCTools import AppHelper
    except ImportError:
        return
    monkeypatch.setattr(AppHelper, "callLater", lambda delay, fn: fn())
    hold._flags(_Event(63, FN | CTRL))                                    # fn pressed with ⌃ already down
    hold._flags(_Event(63, CTRL))
    assert calls == []


def test_holding_wakes_mint_even_paused_and_letting_go_ends_the_words():
    try:
        from mint.app import session
        from mint.voice import listening
    except ImportError:
        from mint import session, listening

    class Audio:
        playing = False

    class Queue:
        def empty(self):
            return True

    m = session.Mint.__new__(session.Mint)
    log = []
    m.paused, m.asleep, m.hands_free = True, True, True
    m.audio, m.audio_in, m._gate = Audio(), Queue(), None
    m._window = listening.Window()
    m._print = log.append
    m._state = lambda *a: None

    def set_paused(value):
        m.paused = value
        if value:
            m.asleep = True
        log.append(f"paused={value}")
    m.set_paused = set_paused

    async def wake_up(reason):
        m.asleep = False
        log.append(f"wake:{reason}")
    m.wake_up = wake_up

    async def close():
        log.append("end of words")
    m._close_user_audio = close
    asyncio.run(m.hold_to_talk(True))
    assert "paused=False" in log and "wake:shortcut hold" in log
    assert m._window.until == float("inf")
    asyncio.run(m.hold_to_talk(False))
    assert log[-1] == "end of words" and m._window.until != float("inf")
    m._wake, m.meet, m.loop, m.session = None, None, None, None
    m.go_to_sleep("done")                                                 # it was paused: paused again
    assert m.paused
