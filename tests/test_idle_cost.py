"""Mint at rest costs little (6 Oct: 35-45% of a core idle): the agent list isn't deep-copied every frame, the audio
engine stops while the microphone is off, and the notch looks 10 times a second when nothing on it moves."""
import threading
import time
import types

try:
    from mint.tools import agent_watch
except ImportError:
    from mint import agent_watch


def _session(key, state="working"):
    s = agent_watch.Session(key=key, app="claude", id=key)
    s.state, s.updated = state, time.time()
    return s


def test_sessions_are_copied_only_when_something_changed(monkeypatch):
    w = agent_watch.Watcher()
    copies = []
    real = agent_watch.copy.deepcopy
    monkeypatch.setattr(agent_watch.copy, "deepcopy", lambda x, *a: copies.append(1) or real(x, *a))
    w.put(_session("a"))
    first = w.sessions()
    for _ in range(50):
        again = w.sessions()                       # (the notch asks several times a frame)
    assert len(copies) == 1 and [s.key for s in again] == [s.key for s in first] == ["a"]
    again[0].state = "changed by a caller"          # a caller's copy is its own
    assert w.sessions()[0].state == "working"
    w.put(_session("b", "waiting"))                 # a change: made again, waiting first
    assert [s.key for s in w.sessions()] == ["b", "a"] and len(copies) == 3


def _audio():
    try:
        from mint.voice import engine as audio_vp
    except ImportError:
        from mint import audio_vp
    a = audio_vp.VoiceAudio.__new__(audio_vp.VoiceAudio)
    a._build_lock, a.resting, a.suspended, a._closed, a._deferred, a.playing = threading.RLock(), False, False, \
        False, False, False
    calls = []
    a._teardown = lambda: calls.append("teardown")
    a.restart = lambda reason: calls.append("restart")
    return a, calls


def test_the_audio_engine_rests_while_the_mic_is_off():
    a, calls = _audio()
    a.rest()
    assert a.resting and calls == ["teardown"]
    a.rest()                                        # once
    a.wake()
    assert not a.resting and calls == ["teardown", "restart"]
    a.playing = True
    a.rest()                                        # never while Mint is talking
    assert not a.resting
    a.playing, a.suspended = False, True
    a.rest()                                        # a call has the mic: that's suspend's business
    assert not a.resting


def test_a_calm_notch_looks_less_often_and_wakes_at_once():
    try:
        from mint.ui import notch
    except ImportError:
        from mint import notch
    n = notch.Notch.__new__(notch.Notch)
    n.cx, n.nw, n.top, n.nh = 700.0, 200.0, 900.0, 32.0
    n.battery_peek_until = n.search_until = n.agent_until = n.drag_until = n.music_peek_until = 0.0
    hud = types.SimpleNamespace(_state="sleeping", _activity=None, _said=types.SimpleNamespace(words=None),
                                _you=types.SimpleNamespace(words=None))
    far = types.SimpleNamespace(x=100.0, y=100.0)
    near = types.SimpleNamespace(x=720.0, y=880.0)
    now = time.monotonic()
    assert not n._stirring(hud, far, now)
    assert n._stirring(hud, near, now)              # the pointer coming: full speed (hover feels instant)
    hud._state = "speaking"
    assert n._stirring(hud, far, now)
    hud._state, n.music_peek_until = "sleeping", now + 3
    assert n._stirring(hud, far, now)


def test_no_framework_is_loaded_with_objc_load_bundle():
    """objc.loadBundle() wraps every Objective-C class in the process and keeps the wrappers: the shelf's first
    thumbnail cost 232 MB for good (6 Oct). Load a framework with NSBundle and look up only the classes needed."""
    import pathlib
    import re

    import mint
    root = pathlib.Path(mint.__file__).parent
    calls = [f"{p.relative_to(root)}:{n}" for p in root.rglob("*.py")
             for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1)
             if re.search(r"\bobjc\.loadBundle\(", line.split("#", 1)[0])]
    assert calls == []
