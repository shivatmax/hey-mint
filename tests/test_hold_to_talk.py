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


def _dictation():
    try:
        from mint.voice import dictation
    except ImportError:
        from mint import dictation
    return dictation


def _voice(seconds=1.5, loud=6000):
    import numpy as np
    t = np.arange(int(16000 * seconds)) / 16000
    return (np.sin(2 * np.pi * 220 * t) * loud * (np.sin(2 * np.pi * 2 * t) > 0)).astype(np.int16).tobytes()


def test_capture_keeps_the_moment_before_the_key_and_skips_engine_zeros(monkeypatch):
    d = _dictation()
    monkeypatch.setattr(d.Capture, "_plain_mic_if_needed", lambda self: None)     # no real microphone in tests
    d._ring.clear()
    d.ring(b"\x01\x00" * 1600)                                               # heard just before the key
    cap = d.Capture("t")
    assert cap.begin() and not cap.begin()
    cap.feed(bytes(3200))                                                    # the engine still starting: zeros
    cap.feed(b"\x02\x00" * 1600)
    pcm = cap.end()
    assert pcm == b"\x01\x00" * 1600 + b"\x02\x00" * 1600 and not cap.on


def test_holding_records_and_letting_go_wakes_mint_and_sends_the_words(monkeypatch):
    try:
        from mint.app import session
        from mint.voice import listening
    except ImportError:
        from mint import session, listening
    d = _dictation()
    monkeypatch.setattr(d.Capture, "_plain_mic_if_needed", lambda self: None)
    d._ring.clear()

    class Audio:
        playing = False

    class Queue:
        def empty(self):
            return True

    class Live:
        def __init__(self):
            self.chunks = 0

        async def send_realtime_input(self, audio=None, **_):
            self.chunks += 1

    m = session.Mint.__new__(session.Mint)
    log = []
    m.paused, m.asleep, m.hands_free = True, True, True
    m.audio, m.audio_in, m._gate, m.session = Audio(), Queue(), None, Live()
    m._window = listening.Window()
    m._print = log.append
    m._state = lambda *a: None
    m._idle_state = lambda: "paused"

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
    assert m.paused and "paused=False" not in log                       # nothing restarts while the keys are down
    d.talk.feed(_voice())                                                 # the user talks
    asyncio.run(m.hold_to_talk(False))
    assert "paused=False" in log and "wake:shortcut hold" in log
    assert m.session.chunks == 15 and log[-1] == "end of words"           # 1.5 s in 0.1 s chunks, then the end
    m._wake, m.meet, m.loop, m.session = None, None, None, None
    m.go_to_sleep("done")                                                 # it was paused: paused again
    assert m.paused


def test_holding_without_words_does_nothing(monkeypatch):
    try:
        from mint.app import session
    except ImportError:
        from mint import session
    d = _dictation()
    monkeypatch.setattr(d.Capture, "_plain_mic_if_needed", lambda self: None)
    d._ring.clear()
    m = session.Mint.__new__(session.Mint)
    log = []

    class Audio:
        playing = False

    class Queue:
        def empty(self):
            return True
    m.audio, m.audio_in, m.paused = Audio(), Queue(), True
    m._print, m._state, m._idle_state = log.append, (lambda *a: None), (lambda: "paused")
    asyncio.run(m.hold_to_talk(True))
    asyncio.run(m.hold_to_talk(False))
    assert m.paused and "nothing heard" in log[-1]


def test_dictation_key_down_records_before_the_hold_is_sure(monkeypatch):
    d = _dictation()
    monkeypatch.setattr(d.Capture, "_plain_mic_if_needed", lambda self: None)
    d._ring.clear()
    d._state.clear()
    d._state["mode"] = "idle"
    d.prime()                                                             # fn down
    d.feed(b"\x03\x00" * 1600)                                         # "Hey..." said at once
    d.unprime()                                                           # it was only a tap
    assert not d.capturing()
    d.prime()
    d.feed(b"\x03\x00" * 1600)
    d.start()                                                             # the hold is sure 0.28 s later
    d.feed(b"\x04\x00" * 1600)
    assert d._rec.seconds() == 0.2
    d.cancel()


def test_dictation_tells_a_silent_mic_from_a_quiet_room(capsys):
    try:
        from mint.voice import dictation
    except ImportError:
        from mint import dictation
    dictation._write(bytes(16000 * 2 * 3))                               # 3 s of digital zeros
    assert "only silence" in capsys.readouterr().out and "still starting" in dictation._state["error"]
    import numpy as np
    hiss = (np.random.default_rng(1).normal(0, 30, 16000 * 3)).astype(np.int16).tobytes()
    dictation._write(hiss)                                               # a quiet room: nothing said
    assert "too quiet" in capsys.readouterr().out and dictation._state["error"] == "Didn't hear anything"
