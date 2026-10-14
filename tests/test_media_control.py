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


def test_asking_a_player_never_launches_it(monkeypatch):
    """12 Oct: a quit Spotify (and Music) kept coming back - `if application id ... is running` around a
    `tell application id ... player state` still compiles the tell, which can launch the app. Now the player's
    part is compiled only when it runs (run script)."""
    try:
        from mint.tools import music
    except ImportError:
        from mint import music
    seen = []
    monkeypatch.setattr(music, "_osa", lambda script, timeout=10: (seen.append(script) or True, ""))
    music._tell("Spotify", 'return player state as text')
    script = seen[0]
    assert script.startswith('if application id "com.spotify.client" is running then\nreturn run script "')
    assert '\\"com.spotify.client\\"' in script                     # the tell is text, not compiled code
    music._tell("Spotify", "play", launch=True)                      # asked to play: launching is fine
    assert seen[1].startswith('tell application id "com.spotify.client"')


def _music_module():
    try:
        from mint.tools import music
    except ImportError:
        from mint import music
    return music


def _fresh_watcher(monkeypatch, music, running=("Spotify",)):
    monkeypatch.setattr(music, "_now", music._empty())
    monkeypatch.setattr(music, "_listeners", [])
    monkeypatch.setattr(music, "_notes", [])
    monkeypatch.setattr(music, "_running_apps", lambda: set(running))
    asked = []
    monkeypatch.setattr(music, "_tell", lambda app, body, timeout=10, launch=False: asked.append(app) or (True, ""))
    return asked


SPOTIFY_NOTE = {"Player State": "Playing", "Name": "Savage Love", "Artist": "Jawsh 685", "Album": "Savage Love",
                "Track ID": "spotify:track:1xQ6trAsedVPCdbtDAmk0c", "Duration": 171375, "Playback Position": 12.5}


def test_the_watcher_reads_the_players_announcements_not_the_players(monkeypatch):
    """14 Oct: Spotify reopened 10 s after the user quit it - the watcher asked it (an Apple Event) right as it
    closed. Now the players' own announcements carry the track: no question for a pause, a stop or the same song."""
    music = _music_module()
    asked = _fresh_watcher(monkeypatch, music)
    playing = music._from_note("Spotify", SPOTIFY_NOTE)
    assert playing["title"] == "Savage Love" and playing["duration"] == 171.375 and playing["position"] == 12.5
    music._set(dict(playing, artwork="/tmp/cover.jpg"))
    music._apply_note("Spotify", music._from_note("Spotify", dict(SPOTIFY_NOTE, **{"Player State": "Paused"})))
    assert asked == [] and music.cached()["state"] == "paused" and music.cached()["artwork"] == "/tmp/cover.jpg"
    music._apply_note("Spotify", music._from_note("Spotify", {"Player State": "Stopped"}))   # what quitting says
    assert asked == [] and music.cached()["state"] == "stopped"


def test_a_new_song_is_asked_about_once_for_its_cover(monkeypatch):
    music = _music_module()
    asked = _fresh_watcher(monkeypatch, music)
    music._apply_note("Spotify", music._from_note("Spotify", SPOTIFY_NOTE))
    assert asked == ["Spotify"]                                # it just announced itself: it isn't closing


def test_a_quit_player_is_noticed_from_the_process_list_alone(monkeypatch):
    music = _music_module()
    asked = _fresh_watcher(monkeypatch, music, running=())
    music._set(music._from_note("Spotify", SPOTIFY_NOTE))
    music._check_running()
    assert asked == [] and music.cached()["state"] == "stopped" and music.cached()["app"] == ""


def test_music_announcements_are_read_too(monkeypatch):
    music = _music_module()
    _fresh_watcher(monkeypatch, music, running=("Music",))
    info = music._from_note("Music", {"Player State": "Playing", "Name": "Song", "Artist": "Band", "Album": "LP",
                                      "Total Time": 200000, "PersistentID": 1234567})
    assert (info["app"], info["state"], info["duration"], info["uri"]) == ("Music", "playing", 200.0, "1234567")
