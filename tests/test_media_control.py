"""Play / pause by voice never toggles blind and never starts a player that was quit: "stop the music" pauses (it
used to press Play/Pause - playing a paused song, or relaunching a Spotify the user had just quit)."""
try:
    from mint.app import instant
    from mint.tools import fastinput, music
except ImportError:
    from mint import fastinput, instant, music


def test_stop_the_music_is_pause_and_play_is_play():
    assert instant.match("stop the music")[:2] == ("media_key", {"action": "pause"})
    assert instant.match("pause")[:2] == ("media_key", {"action": "pause"})
    assert instant.match("play music")[:2] == ("media_key", {"action": "play"})
    assert instant.match("resume the song")[:2] == ("media_key", {"action": "play"})
    assert instant.match("stop") is None or instant.match("stop")[0] != "media_key"   # a bare "stop" stops Mint


def _players(monkeypatch, running, state="playing"):
    calls = []
    monkeypatch.setattr(music, "_running_apps", lambda: set(running))
    monkeypatch.setattr(music, "pick_app", lambda app="": next(iter(running), "Spotify"))
    monkeypatch.setattr(music, "_state_only", lambda app: state)
    for name in ("pause", "resume", "next_track", "previous_track"):
        monkeypatch.setattr(music, name, lambda app="", n=name: calls.append((n, app)) or n)
    return calls


def test_nothing_open_nothing_is_started(monkeypatch):
    calls = _players(monkeypatch, [])
    sent = []
    monkeypatch.setattr(fastinput, "_post", lambda event, pid=None: sent.append(event))
    for action in ("pause", "playpause", "next", "previous", "stop"):
        assert fastinput.media_key(action).startswith("Nothing is playing")
    assert calls == [] and sent == []                 # no media key: it would have launched Spotify again
    fastinput.media_key("play")
    assert calls == [("resume", "")]                  # asked to play: the usual player starts


def test_spotify_open_is_told_directly(monkeypatch):
    calls = _players(monkeypatch, ["Spotify"], state="paused")
    fastinput.media_key("pause")
    fastinput.media_key("playpause")                  # paused: a toggle plays
    fastinput.media_key("next")
    assert calls == [("pause", "Spotify"), ("resume", "Spotify"), ("next_track", "Spotify")]
    assert fastinput.media_key("dance").startswith("Action must be")
