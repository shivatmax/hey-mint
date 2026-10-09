"""Music by voice: Spotify (and Apple Music when it is used), plus what is playing now for the mini player.

    "play Blinding Lights by the Weeknd"    play kind=track   -> finds the song, plays it in Spotify
    "play some jazz" / "play lo-fi"          play kind=genre   -> a public playlist for it
    "play my Liked Songs"                    play kind=liked   -> opens Liked Songs in Spotify (see below)
    "next song", "pause the music"           next / pause
    "what's playing?"                        now_playing
    "turn the music down", "volume 30"       volume (the app's own volume, not the Mac's)
    "shuffle on", "repeat off"               shuffle / repeat
    "skip to 1:30", "forward 30 seconds"     seek

Spotify is driven through its AppleScript dictionary (Spotify.sdef: play track "<uri>", playpause,
next/previous track, player position, sound volume, shuffling, repeating, current track's name,
artist, album, duration, artwork url, spotify url). Finding music needs no Spotify login or keys,
cheapest first:
  1. the cache (music_search.json, 30 days);
  2. moods and genres ("jazz", "lo-fi", "focus", "workout"...) and Spotify's big playlists by name
     (RapCaviar, Peaceful Piano...): a built-in table of Spotify's editorial playlists (GENRES);
  3. a web search restricted to open.spotify.com - DuckDuckGo's HTML page, else Yahoo (kept session:
     Yahoo's bot check sets a cookie first) - whose links https://open.spotify.com/track/<id> become
     spotify:track:<id>; the result titles ("Blinding Lights - song and lyrics by The Weeknd |
     Spotify") pick the best match, and remixes, live and karaoke versions lose unless asked for;
  4. Wikidata's Spotify ids (P2207 track, P2205 album, P1902 artist; asked first for artists).
Search engines rate-limit scrapers (in testing both refused after ~5 quick searches for a few
minutes): a refusing engine rests 10 minutes. With nothing found, Spotify's own search opens
(spotify:search:<words>); Mint presses its top result's Play button when it has Accessibility, else
the user picks - and the model may look the link up itself and pass it as the query.

Liked Songs: `play track` refuses the Liked Songs collection and ignores `in context` (tested:
playback falls back to radio), so Mint opens it (open location "spotify:collection:tracks") and
presses the page's big Play button through Accessibility when it can; otherwise it asks the user
to press it. Liking a song: Spotify's dictionary has no way to do it (`starred` is read-only), so
Mint says how (the heart, or Option-Shift-B in Spotify). Apple Music can (`favorited`).

Apple Music (the Music app) works from the library by name - songs, artists, albums, playlists,
genres - with the same controls. It is written defensively and UNTESTED (not used on the
development Mac).

now_playing() is cached and cheap: a background watcher asks the running player every second
while something plays and the mini player is on screen (3 s otherwise, 6 s when paused), wakes at
once on Spotify's / Music's own "playback changed" notifications, and never launches an app just
to look (pgrep first; every script is wrapped in "if application ... is running"). One reading is
one osascript (~0.15 s wall, ~0.04 s CPU). Spotify Free plays ads: they read as "Advertisement".
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

log = logging.getLogger("mint.tools.music")

SPOTIFY, MUSIC = "Spotify", "Music"
BUNDLES = {SPOTIFY: "com.spotify.client", MUSIC: "com.apple.Music"}
SUPPORT = Path.home() / "Library" / "Application Support" / "Mint"
CACHE_FILE = SUPPORT / "music_search.json"
ART_DIR = Path.home() / "Library" / "Caches" / "Mint" / "music-art"
CACHE_DAYS = 30
SEP = "␞"                 # between fields in AppleScript answers (a symbol no title uses)
VOLUME_STEP = 15
_SPOTIFY_LINK = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?(track|album|artist|playlist)/([A-Za-z0-9]{22})")
_VERSIONS = ("remix", "live", "karaoke", "instrumental", "cover", "tribute", "sped up", "slowed", "acoustic",
             "reverb", "8d", "version", "edit", "mix", "remaster", "demo", "piano", "lullaby", "made famous")


# --- the apps -----------------------------------------------------------------------------------

def _running_apps() -> set[str]:
    """Which players run now - asks the system (pgrep, ~5 ms), never the apps: asking would launch them.
    (NSRunningApplication's list only refreshes on the main run loop, so the watcher thread can't use it.)"""
    try:
        done = subprocess.run(["pgrep", "-lx", "|".join(BUNDLES)], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names = {line.split(None, 1)[1].strip() for line in done.stdout.splitlines() if " " in line.strip()}
    return {name for name in BUNDLES if name in names}


def _installed(app: str) -> bool:
    try:
        import AppKit
        return AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(BUNDLES[app]) is not None
    except Exception:
        return Path(f"/Applications/{app}.app").exists() or Path(f"/System/Applications/{app}.app").exists()


def _app_name(word: str) -> str:
    word = str(word or "").strip().lower()
    if word in ("spotify",):
        return SPOTIFY
    if word in ("music", "apple music", "itunes", "apple"):
        return MUSIC
    return ""


def pick_app(asked: str = "") -> str:
    """The player to use: the one asked for; else the one playing; else the running one (Spotify
    first); else the one set in prefs (music_app); else Spotify when installed, else Music."""
    named = _app_name(asked)
    if named:
        return named
    running = _running_apps()
    if len(running) > 1:
        playing = [app for app in (SPOTIFY, MUSIC) if app in running and _state_only(app) == "playing"]
        if playing:
            return playing[0]
    for app in (SPOTIFY, MUSIC):
        if app in running:
            return app
    try:
        from mint.core import prefs
        preferred = _app_name(prefs.get("music_app") or "")
        if preferred:
            return preferred
    except Exception:
        pass
    return SPOTIFY if _installed(SPOTIFY) else MUSIC


def _q(text: str) -> str:
    """A string for inside AppleScript quotes."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _osa(script: str, timeout: float = 10) -> tuple[bool, str]:
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "the app did not answer in time"
    return done.returncode == 0, (done.stdout.strip() if done.returncode == 0 else done.stderr.strip())


def _tell(app: str, body: str, timeout: float = 10, launch: bool = False) -> tuple[bool, str]:
    """Run `body` inside tell application; unless `launch`, only when the app already runs."""
    script = f'tell application id "{BUNDLES[app]}"\n{body}\nend tell'
    if not launch:
        script = f'if application id "{BUNDLES[app]}" is running then\n{script}\nend if'
    return _osa(script, timeout)


def _launch(app: str, wait: float = 15.0) -> bool:
    """Start the player in the background and wait until it answers scripts."""
    if app in _running_apps():
        return True
    subprocess.run(["open", "-g", "-b", BUNDLES[app]], capture_output=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.6)
        if app in _running_apps():
            ok, out = _tell(app, "return player state as text", timeout=4)
            if ok and out:
                time.sleep(0.8 if app == SPOTIFY else 0.3)      # Spotify answers before it can play
                return True
    return app in _running_apps()


# --- what is playing ----------------------------------------------------------------------------

_SPOTIFY_NOW = f"""set s to player state as text
if s is "stopped" then return s
set t to current track
set out to s & "{SEP}"
try
set out to out & (name of t)
end try
set out to out & "{SEP}"
try
set out to out & (artist of t)
end try
set out to out & "{SEP}"
try
set out to out & (album of t)
end try
set out to out & "{SEP}" & (player position as text) & "{SEP}"
try
set out to out & ((duration of t) as text)
end try
set out to out & "{SEP}"
try
set out to out & (artwork url of t)
end try
set out to out & "{SEP}"
try
set out to out & (spotify url of t)
end try
return out & "{SEP}" & (sound volume as text) & "{SEP}" & (shuffling as text) & "{SEP}" & (repeating as text)"""

_MUSIC_NOW = f"""set s to player state as text
if s is "stopped" then return s
set t to current track
set out to s & "{SEP}"
try
set out to out & (name of t)
end try
set out to out & "{SEP}"
try
set out to out & (artist of t)
end try
set out to out & "{SEP}"
try
set out to out & (album of t)
end try
set out to out & "{SEP}" & (player position as text) & "{SEP}"
try
set out to out & ((duration of t) as text)
end try
set out to out & "{SEP}{SEP}"
try
set out to out & (persistent ID of t)
end try
set r to "off"
try
set r to (song repeat as text)
end try
return out & "{SEP}" & (sound volume as text) & "{SEP}" & (shuffle enabled as text) & "{SEP}" & r"""


def _number(text: str) -> float:
    try:
        return float(str(text).strip().replace(",", "."))      # some locales write 6,347
    except ValueError:
        return 0.0


def _empty(app: str = "") -> dict:
    return {"app": app, "state": "stopped", "title": "", "artist": "", "album": "", "position": 0.0,
            "duration": 0.0, "artwork": "", "artwork_url": "", "uri": "", "volume": None, "shuffle": None,
            "repeat": None, "running": bool(app)}


def _read(app: str) -> dict:
    """Ask one running player what it is doing (one osascript, ~0.15 s)."""
    ok, out = _tell(app, _SPOTIFY_NOW if app == SPOTIFY else _MUSIC_NOW, timeout=6)
    info = _empty(app)
    if not ok or not out:
        info["running"] = app in _running_apps()
        return info
    parts = out.split(SEP)
    state = parts[0].strip()
    info["state"] = {"fast forwarding": "playing", "rewinding": "playing"}.get(state, state)
    if len(parts) < 11:
        return info
    duration = _number(parts[5])
    if app == SPOTIFY and duration > 7200:             # Spotify reports milliseconds (its sdef says seconds)
        duration /= 1000.0
    uri = parts[7].strip()
    info.update(title=parts[1].strip(), artist=parts[2].strip(), album=parts[3].strip(),
                position=_number(parts[4]), duration=duration, artwork_url=parts[6].strip(), uri=uri,
                volume=int(_number(parts[8])), shuffle=parts[9].strip() == "true",
                repeat=parts[10].strip() not in ("false", "off", ""))
    if uri.startswith("spotify:ad:") or (app == SPOTIFY and not info["title"] and info["state"] == "playing"):
        info.update(title=info["title"] or "Advertisement", artist=info["artist"] or "Spotify", ad=True)
    return info


def _state_only(app: str) -> str:
    ok, out = _tell(app, "return player state as text", timeout=4)
    return out if ok else ""


def _art_path(key: str, suffix: str = ".jpg") -> Path:
    return ART_DIR / (hashlib.sha1(key.encode()).hexdigest()[:20] + suffix)


def _prune_art(keep: int = 80) -> None:
    try:
        files = sorted(ART_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _artwork(info: dict) -> str:
    """A local copy of the cover (cached by track), or ''."""
    app = info.get("app")
    try:
        ART_DIR.mkdir(parents=True, exist_ok=True)
        if app == SPOTIFY and info.get("artwork_url", "").startswith("http"):
            url = info["artwork_url"]
            path = _art_path(url)
            if not path.exists():
                import httpx
                small = url.replace("ab67616d0000b273", "ab67616d00001e02")      # 300 px, not 640
                for candidate in dict.fromkeys((small, url)):
                    response = httpx.get(candidate, timeout=8, follow_redirects=True)
                    if response.status_code == 200 and response.content:
                        path.write_bytes(response.content)
                        _prune_art()
                        break
            return str(path) if path.exists() else ""
        if app == MUSIC and info.get("uri"):
            path = _art_path("music:" + info["uri"], ".img")
            if not path.exists():
                ok, _ = _tell(MUSIC, f"""try
set d to data of artwork 1 of current track
set f to open for access (POSIX file "{_q(path)}") with write permission
set eof f to 0
write d to f
close access f
end try""", timeout=6)
                if path.exists() and path.stat().st_size < 100:
                    path.unlink(missing_ok=True)
                _prune_art()
            return str(path) if path.exists() else ""
    except Exception as error:
        log.debug("artwork: %s", error)
    return ""


_lock = threading.RLock()
_now: dict = _empty()
_stamp = 0.0                    # time.monotonic() of the last reading
_listeners: list = []
_wake = threading.Event()
_fast_until = [0.0]
_thread: list = [None]


def _refresh() -> dict:
    global _now, _stamp
    running = _running_apps()
    info = None
    order = [app for app in (SPOTIFY, MUSIC) if app in running]
    for app in order:
        found = _read(app)
        if found["state"] == "playing":
            info = found
            break
        if info is None or (info["state"] == "stopped" and found["state"] != "stopped"):
            info = found
    if info is None:
        info = _empty()
    with _lock:
        before = dict(_now)
    if info.get("title") and info.get("artwork_url") == before.get("artwork_url") and before.get("artwork") \
            and info.get("app") == before.get("app"):
        info["artwork"] = before["artwork"]
    elif info.get("title"):
        info["artwork"] = _artwork(info)
    with _lock:
        _now, _stamp = info, time.monotonic()
    if (before.get("state"), before.get("uri"), before.get("title"), before.get("app")) != \
            (info["state"], info["uri"], info["title"], info["app"]):
        for listener in list(_listeners):
            try:
                listener(before, dict(info))
            except Exception:
                log.exception("music listener failed")
    return dict(info)


def cached() -> dict:
    """The last reading, position moved on by the time since (never blocks - fine on the main thread)."""
    with _lock:
        info, stamp = dict(_now), _stamp
    if info.get("state") == "playing" and stamp:
        position = info.get("position", 0.0) + (time.monotonic() - stamp)
        info["position"] = min(position, info.get("duration") or position)
    return info


def now_playing(max_age: float = 1.0) -> dict:
    """{app, state, title, artist, album, position, duration, artwork (a local image path), uri, volume,
    shuffle, repeat}. Re-read when older than `max_age` seconds; don't call from the main thread."""
    if time.monotonic() - _stamp > max_age:
        return _refresh()
    return cached()


def on_change(listener) -> None:
    """listener(before, after) on the watcher's thread whenever the track, state or app changes."""
    if listener not in _listeners:
        _listeners.append(listener)


def want_fast(seconds: float = 2.5) -> None:
    """The mini player is on screen: poll every second for the next `seconds`."""
    _fast_until[0] = max(_fast_until[0], time.monotonic() + seconds)


def poke() -> None:
    """Something changed (a button, a voice command): read the player again now."""
    _wake.set()


def _interval() -> float:
    with _lock:
        state, app = _now.get("state"), _now.get("app")
    if not app and not _running_apps():
        return 6.0
    if state == "playing":
        return 1.0 if time.monotonic() < _fast_until[0] else 3.0
    return 6.0


def _loop() -> None:
    while True:
        try:
            _refresh()
        except Exception:
            log.exception("music watcher")
        _wake.wait(_interval())
        _wake.clear()


def _observe_notifications() -> None:
    """Spotify and Music announce every play/pause/track change: wake the watcher at once (main thread)."""
    try:
        import AppKit
        import objc

        class _MusicPlayerNotificationSink(AppKit.NSObject):
            def changed_(self, note):
                _wake.set()

        sink = _MusicPlayerNotificationSink.alloc().init()
        center = AppKit.NSDistributedNotificationCenter.defaultCenter()
        for name in ("com.spotify.client.PlaybackStateChanged", "com.apple.Music.playerInfo"):
            center.addObserver_selector_name_object_(sink, objc.selector(sink.changed_, signature=b"v@:@"), name, None)
        _thread.append(sink)                 # keep it alive
    except Exception:
        log.debug("music notifications", exc_info=True)


def start_service() -> None:
    """Start the watcher (idempotent). Call once at startup; tools and the player call it too."""
    if _thread[0] is not None:
        return
    _thread[0] = threading.Thread(target=_loop, daemon=True, name="music")
    _thread[0].start()
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(_observe_notifications)
        AppHelper.callAfter(_attach_player)
    except Exception:
        pass


def _attach_player() -> None:
    """Main thread: the mini player listens from now on (it shows itself when music starts)."""
    try:
        from mint.ui import music_player
        music_player.attach()
    except Exception:
        log.debug("music player attach", exc_info=True)


# --- finding music on Spotify without its Web API -------------------------------------------------

_bench: dict[str, float] = {}          # engine -> monotonic time it may be tried again


def _strip(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment or "")).strip()


def _ua() -> str:
    try:
        from mint.agents import tools as agent_tools
        return agent_tools.UA
    except Exception:
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15"


def _duckduckgo(query: str) -> list[tuple[str, str, str]]:
    import httpx
    response = httpx.get("https://html.duckduckgo.com/html/", params={"q": query}, timeout=12,
                         headers={"User-Agent": _ua()}, follow_redirects=True)
    if response.status_code != 200 or "anomaly" in response.text[:20000] and "result__a" not in response.text:
        raise RuntimeError(f"DuckDuckGo refused ({response.status_code})")
    rows = []
    for match in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)'
                             r'(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)', response.text, re.S):
        href, title, _, snippet = match.groups()
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        rows.append((_strip(title), html.unescape(href), _strip(snippet)))
    return rows


_yahoo_session: list = []


def _yahoo(query: str) -> list[tuple[str, str, str]]:
    """Yahoo first sends new visitors through a check that sets a YBV cookie (a 307 then a 500); a kept
    session with that cookie gets real results, so ask once more after it."""
    import httpx
    response = None
    for fresh in (False, True):                  # a stale check cookie: start a new session once
        if fresh or not _yahoo_session:
            if _yahoo_session:
                _yahoo_session.pop().close()
            _yahoo_session.append(httpx.Client(headers={"User-Agent": _ua(), "Accept-Language": "en-US,en;q=0.9",
                                                        "Accept": "text/html,application/xhtml+xml"},
                                               follow_redirects=True, timeout=12))
        client = _yahoo_session[0]
        response = client.get("https://search.yahoo.com/search", params={"p": query})
        if response.status_code != 200 and "YBV" in client.cookies:
            response = client.get("https://search.yahoo.com/search", params={"p": query})
        if response.status_code == 200:
            break
    if response.status_code != 200:
        raise RuntimeError(f"Yahoo refused ({response.status_code})")
    rows = []
    for chunk in response.text.split('<div class="dd ')[1:]:
        link = re.search(r"RU=([^/]+)/RK=", chunk) or re.search(r'href="(https?://open\.spotify\.com[^"]+)"', chunk)
        title = re.search(r"<h3[^>]*>(.*?)</h3>", chunk, re.S)
        snippet = re.search(r'<div class="compText[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        if link and title:
            rows.append((_strip(title.group(1)), urllib.parse.unquote(link.group(1)),
                         _strip(snippet.group(1)) if snippet else ""))
    return rows


def _web(query: str) -> tuple[list[tuple[str, str, str]], str]:
    """Search results from the first engine that answers; an engine that refuses rests 15 minutes."""
    errors = []
    for name, engine in (("DuckDuckGo", _duckduckgo), ("Yahoo", _yahoo)):
        if _bench.get(name, 0) > time.monotonic():
            continue
        try:
            rows = engine(query)
            if any(_SPOTIFY_LINK.search(url) for _, url, _ in rows):
                return rows, name
            errors.append(f"{name}: no Spotify links in {len(rows)} results")
        except Exception as error:
            _bench[name] = time.monotonic() + 600       # refused (a captcha, 5xx): let it rest
            errors.append(f"{name}: {error}")
    log.info("music search %r: %s", query, "; ".join(errors))
    return [], ""


def _words(text: str) -> list[str]:
    text = re.sub(r"[’']", "", str(text or "").lower())
    return [w for w in re.findall(r"[^\W_]+", text) if w not in {"the", "a", "an", "by", "and", "feat", "ft", "of"}]


def _parse_title(title: str) -> tuple[str, str, str]:
    """ "Blinding Lights - song and lyrics by The Weeknd | Spotify" -> (name, by, what)."""
    title = re.sub(r"\s*(\||-)\s*Spotify.*$", "", title.strip())
    title = re.sub(r"\s*\.\.\.$|…$", "", title)
    match = re.match(r"^(.*?)\s+-\s+(song and lyrics by|song by|single by|ep by|album by|playlist by|"
                     r"compilation by|podcast by)\s+(.*)$", title, re.I)
    if match:
        return match.group(1).strip(), match.group(3).strip(), match.group(2).lower().split()[0]
    match = re.match(r"^(.*?)\s*\|\s*Spotify\s*(Playlist|Album)?", title, re.I)
    if match:
        return match.group(1).strip(), "", (match.group(2) or "").lower()
    return title.strip(), "", ""


def _score(kind: str, want: str, name: str, by: str, snippet: str, words: list[str], artist: list[str],
           rank: int, ident: str) -> float:
    name_words = _words(name)
    found = _words(name + " " + by + " " + snippet)
    score = -0.15 * rank
    if want in ("track", "album", "artist", "playlist"):
        score += 3.0 if kind == want else -2.5
    elif want == "genre":
        score += 3.0 if kind == "playlist" else -2.0
        if ident.startswith("37i9dQZF1D"):             # Spotify's own editorial playlists
            score += 1.0
    if words:
        have = sum(1 for w in words if w in name_words or w in found)
        score += 4.0 * have / len(words)
        if kind in ("track", "album") and name_words and all(w in words + artist for w in name_words):
            score += 1.5                                # the name is exactly the asked-for words
    if artist:
        who = _words(by + " " + snippet)
        score += 3.0 * sum(1 for w in artist if w in who) / len(artist)
    asked = " ".join(words + artist)
    for word in _VERSIONS:
        if word in name.lower() and word not in asked:
            score -= 1.5
    return score


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        SUPPORT.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - CACHE_DAYS * 86400
        cache = {k: v for k, v in cache.items() if v.get("t", 0) > cutoff}
        CACHE_FILE.write_text(json.dumps(dict(list(cache.items())[-400:]), indent=1, ensure_ascii=False))
    except OSError:
        pass


def _cache_key(query: str, kind: str, artist: str) -> str:
    return f"{kind}|{' '.join(_words(query))}|{' '.join(_words(artist))}"


# Spotify's own editorial playlists for moods and genres (ids checked against Spotify's public oEmbed
# titles, 2026-09-30): "play some jazz" needs no search at all. Longest matching words win.
GENRES = {
    "lofi": ("37i9dQZF1DWWQRwui0ExPn", "lofi beats"), "lo fi": ("37i9dQZF1DWWQRwui0ExPn", "lofi beats"),
    "lofi house": ("37i9dQZF1DXbXD9pMSZomS", "Lo-Fi House"), "lo fi house": ("37i9dQZF1DXbXD9pMSZomS", "Lo-Fi House"),
    "study": ("37i9dQZF1DX8Uebhn9wzrS", "chill lofi study beats"),
    "studying": ("37i9dQZF1DX8Uebhn9wzrS", "chill lofi study beats"),
    "instrumental": ("37i9dQZF1DX9sIqqvKsjG8", "Instrumental Study"),
    "jazz": ("37i9dQZF1DXbITWG1ZJKYt", "Jazz Classics"),
    "background jazz": ("37i9dQZF1DWV7EzJMK2FUI", "Jazz in the Background"),
    "coffee jazz": ("37i9dQZF1DWVqfgj8NZEp1", "Coffee Table Jazz"),
    "piano": ("37i9dQZF1DX4sWSpwq3LiO", "Peaceful Piano"), "peaceful": ("37i9dQZF1DX4sWSpwq3LiO", "Peaceful Piano"),
    "focus": ("37i9dQZF1DWZeKCadgRdKQ", "Deep Focus"), "concentration": ("37i9dQZF1DWZeKCadgRdKQ", "Deep Focus"),
    "work": ("37i9dQZF1DWZeKCadgRdKQ", "Deep Focus"), "brain food": ("37i9dQZF1DWXLeA8Omikj7", "Brain Food"),
    "coding": ("37i9dQZF1DX5trt9i14X7j", "Coding Mode"), "programming": ("37i9dQZF1DX5trt9i14X7j", "Coding Mode"),
    "pop": ("37i9dQZF1DXcBWIGoYBM5M", "Today's Top Hits"), "hits": ("37i9dQZF1DXcBWIGoYBM5M", "Today's Top Hits"),
    "top hits": ("37i9dQZF1DXcBWIGoYBM5M", "Today's Top Hits"), "popular": ("37i9dQZF1DXcBWIGoYBM5M", "Today's Top Hits"),
    "hip hop": ("37i9dQZF1DX0XUsuxWHRQd", "RapCaviar"), "rap": ("37i9dQZF1DX0XUsuxWHRQd", "RapCaviar"),
    "rock": ("37i9dQZF1DWXRqgorJj26U", "Rock Classics"), "classic rock": ("37i9dQZF1DWXRqgorJj26U", "Rock Classics"),
    "indie": ("37i9dQZF1DX2sUQwD7tbmL", "Feel-Good Indie Rock"),
    "80s": ("37i9dQZF1DX4UtSsGT1Sbe", "All Out 80s"), "eighties": ("37i9dQZF1DX4UtSsGT1Sbe", "All Out 80s"),
    "70s": ("37i9dQZF1DWTJ7xPn4vNaz", "All Out 70s"), "seventies": ("37i9dQZF1DWTJ7xPn4vNaz", "All Out 70s"),
    "r b": ("37i9dQZF1DWYmmr74INQlb", "I Love My '00s R&B"), "rnb": ("37i9dQZF1DWYmmr74INQlb", "I Love My '00s R&B"),
    "chill": ("37i9dQZF1DX889U0CL85jj", "Chill Vibes"), "chilled": ("37i9dQZF1DX889U0CL85jj", "Chill Vibes"),
    "relax": ("37i9dQZF1DX1s9knjP51Oa", "calm vibes"), "relaxing": ("37i9dQZF1DX1s9knjP51Oa", "calm vibes"),
    "calm": ("37i9dQZF1DX1s9knjP51Oa", "calm vibes"), "chill hits": ("37i9dQZF1DX4WYpdgoIcn6", "Chill Hits"),
    "workout": ("37i9dQZF1DX76Wlfdnj7AP", "Beast Mode"), "gym": ("37i9dQZF1DX76Wlfdnj7AP", "Beast Mode"),
    "running": ("37i9dQZF1DX76Wlfdnj7AP", "Beast Mode"), "sleep": ("37i9dQZF1DWZd79rJ6a7lp", "Sleep"),
    "classical": ("37i9dQZF1DWWEJlAGA9gs0", "Classical Essentials"),
    "road trip": ("37i9dQZF1DWWMOmoXKqHTD", "Songs to Sing in the Car"),
    "car": ("37i9dQZF1DWWMOmoXKqHTD", "Songs to Sing in the Car"),
    "sing along": ("37i9dQZF1DWSqmBTGDYngZ", "Songs to Sing in the Shower"),
    "country": ("37i9dQZF1DX1lVhptIYRda", "Hot Country"), "latin": ("37i9dQZF1DX10zKzsJ2jva", "Viva Latino"),
    "reggaeton": ("37i9dQZF1DX10zKzsJ2jva", "Viva Latino"),
    "happy": ("37i9dQZF1DX3rxVfibe1L0", "Mood Booster"), "upbeat": ("37i9dQZF1DX3rxVfibe1L0", "Mood Booster"),
    "feel good": ("37i9dQZF1DX3rxVfibe1L0", "Mood Booster"), "good mood": ("37i9dQZF1DX3rxVfibe1L0", "Mood Booster"),
    "party": ("37i9dQZF1DXa2PvUpywmrr", "Party Hits"), "dance": ("37i9dQZF1DXa2PvUpywmrr", "Party Hits"),
    "hindi": ("37i9dQZF1DX0XUfTFmNBRM", "Hot Hits Hindi"), "bollywood": ("37i9dQZF1DX0XUfTFmNBRM", "Hot Hits Hindi"),
    "acoustic": ("37i9dQZF1DX6ziVCJnEm59", "Your Favorite Coffeehouse"),
    "coffeehouse": ("37i9dQZF1DX6ziVCJnEm59", "Your Favorite Coffeehouse"),
}
_FILLER = {"some", "music", "songs", "song", "playlist", "mix", "tunes", "beats", "vibes", "something", "stuff",
           "me", "play", "please", "for", "to", "while", "i", "good", "nice", "radio"}


def _genre(query: str) -> tuple[str, str] | None:
    """ "some lo-fi", "jazz", "music to focus" -> Spotify's playlist for it, if the words are a known mood/genre
    (every word that isn't filler must belong to the matched key); "RapCaviar", "Peaceful Piano" by name."""
    named = " ".join(_words(query))
    for ident, name in GENRES.values():
        if named and named == " ".join(_words(name)):
            return ident, name
    words = [w for w in _words(query) if w not in _FILLER]
    text = " ".join(words)
    if not words:
        return None
    for key in sorted(GENRES, key=len, reverse=True):
        if re.search(rf"(^| ){re.escape(key)}( |$)", text) and len(set(words) - set(key.split())) <= 1:
            return GENRES[key]
    return None


def _wikidata(query: str, kind: str, artist: str) -> list[dict]:
    """Spotify ids recorded on Wikidata (P2207 track, P2205 album, P1902 artist): free, no key, never a
    captcha. Good for artists and albums, fair for well-known songs."""
    import httpx
    if _bench.get("Wikidata", 0) > time.monotonic():
        return []
    headers = {"User-Agent": "Mint/1.0 (https://github.com/shivatmax/hey-mint; macOS voice assistant)"}
    api = "https://www.wikidata.org/w/api.php"

    def ask(**params):
        response = httpx.get(api, params={**params, "format": "json"}, headers=headers, timeout=10)
        if response.status_code != 200:
            _bench["Wikidata"] = time.monotonic() + 600
            raise RuntimeError(f"Wikidata refused ({response.status_code})")
        return response.json()
    found = ask(action="wbsearchentities", search=query, language="en", limit=12, type="item").get("search", [])
    if not found:
        return []
    ids = [row["id"] for row in found]
    entities = ask(action="wbgetentities", ids="|".join(ids), props="claims").get("entities", {})
    words, artist_words = _words(query), _words(artist)
    out = []
    for rank, row in enumerate(found):
        claims = entities.get(row["id"], {}).get("claims", {})
        description = row.get("description") or ""
        for prop, item_kind in (("P2207", "track"), ("P2205", "album"), ("P1902", "artist")):
            values = sorted(claims.get(prop, []), key=lambda c: c.get("rank") != "preferred")
            ident = next((c["mainsnak"].get("datavalue", {}).get("value") for c in values
                          if isinstance(c["mainsnak"].get("datavalue", {}).get("value"), str)), None)
            if not ident or not re.fullmatch(r"[A-Za-z0-9]{22}", ident):
                continue
            score = _score(item_kind, kind, row.get("label") or "", description, "", words, artist_words, rank,
                           ident)
            if kind == "track" and item_kind == "album" and "single" in description.lower():
                score += 3.0                            # the song's single: it plays the song first
            who = re.search(r"\bby (.+?)(?:;|\(|$)", description) if item_kind != "artist" else None
            out.append({"uri": f"spotify:{item_kind}:{ident}", "kind": item_kind, "name": row.get("label") or "",
                        "by": who.group(1).strip() if who else "", "url": f"https://open.spotify.com/{item_kind}/{ident}",
                        "score": round(score, 2), "snippet": description[:120], "engine": "Wikidata"})
    return out


def _from_web(query: str, kind: str, artist: str) -> list[dict]:
    words, artist_words = _words(query), _words(artist)
    label = {"track": "song", "album": "album", "artist": "artist", "playlist": "playlist",
             "genre": "playlist"}.get(kind, "")
    text = " ".join(x for x in (query, artist) if x)
    searches = [re.sub(r"\s+", " ", f"site:open.spotify.com {label} {text}")]
    if kind == "genre":
        searches.append(f"site:open.spotify.com {query} mix")
    elif kind != "auto":
        searches.append(f"site:open.spotify.com {text}")
    candidates: list[dict] = []
    for search in searches:
        rows, engine = _web(search)
        seen = {c["uri"] for c in candidates}
        for rank, (title, url, snippet) in enumerate(rows):
            match = _SPOTIFY_LINK.search(url)
            if not match:
                continue
            item_kind, ident = match.group(1), match.group(2)
            if f"spotify:{item_kind}:{ident}" in seen:
                continue
            seen.add(f"spotify:{item_kind}:{ident}")
            name, by, _what = _parse_title(title)
            score = _score(item_kind, kind, name, by, snippet, words, artist_words, rank, ident)
            candidates.append({"uri": f"spotify:{item_kind}:{ident}", "kind": item_kind, "name": name, "by": by,
                               "url": f"https://open.spotify.com/{item_kind}/{ident}", "score": round(score, 2),
                               "snippet": snippet[:120], "engine": engine})
        candidates.sort(key=lambda c: -c["score"])
        if candidates and candidates[0]["score"] >= 3.0 and (kind in ("auto", candidates[0]["kind"]) or
                                                              (kind == "genre" and candidates[0]["kind"] == "playlist")):
            break
        if not rows:
            break                                        # the engines are resting: don't ask twice
    return candidates


def find(query: str, kind: str = "auto", artist: str = "") -> dict:
    """The best Spotify item for the words: {uri, kind, name, by, url, engine, seconds, candidates}, or
    {candidates, seconds} when nothing is good enough. Order: the cache; Spotify's own mood/genre
    playlists; a web search on open.spotify.com (DuckDuckGo, else Yahoo); Wikidata's Spotify ids
    (first for artists)."""
    kind = (kind or "auto").lower()
    kind = {"song": "track", "songs": "track", "band": "artist", "singer": "artist", "mood": "genre",
            "station": "genre"}.get(kind, kind)
    key = _cache_key(query, kind, artist)
    cache = _load_cache()
    if key in cache and cache[key].get("uri"):
        return {**cache[key], "cached": True, "engine": "cache", "seconds": 0.0}
    started = time.monotonic()
    genre = _genre(query) if kind in ("genre", "playlist", "auto") and not artist else None
    if genre is not None and (kind == "genre" or _words(query) and len(_words(query)) <= 3):
        return {"uri": f"spotify:playlist:{genre[0]}", "kind": "playlist", "name": genre[1], "by": "Spotify",
                "url": f"https://open.spotify.com/playlist/{genre[0]}", "engine": "genres", "seconds": 0.0}
    order = [_from_web, _wikidata]
    if kind == "artist":
        order = [_wikidata, _from_web]
    elif kind in ("genre", "playlist"):
        order = [_from_web]
    candidates: list[dict] = []
    for source in order:
        try:
            candidates += source(query, kind, artist)
        except Exception as error:
            log.info("music find %s: %s", source.__name__, error)
        candidates.sort(key=lambda c: -c["score"])
        if candidates and candidates[0]["score"] >= 4.0:
            break
    best = candidates[0] if candidates else None
    seconds = round(time.monotonic() - started, 2)
    if best is None or best["score"] < 2.5:
        return {"candidates": candidates[:5], "seconds": seconds}
    result = {k: best[k] for k in ("uri", "kind", "name", "by", "url", "engine")}
    result.update(seconds=seconds, t=time.time())
    cache[key] = result
    _save_cache(cache)
    return {**result, "candidates": candidates[:5]}


# --- actions ---------------------------------------------------------------------------------------

def _describe(info: dict) -> str:
    if not info.get("title"):
        return ""
    by = f" by {info['artist']}" if info.get("artist") else ""
    return f"{info['title']}{by}"


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02d}"


def _show_player() -> None:
    try:
        from mint.ui import music_player
        music_player.show(reason="asked")
    except Exception as error:
        log.debug("music player: %s", error)


def _after_change(wait: float = 0.9) -> dict:
    time.sleep(wait)
    poke()
    return now_playing(max_age=0)


def _spotify_play_uri(uri: str) -> tuple[bool, str]:
    ok, out = _tell(SPOTIFY, f'play track "{_q(uri)}"', launch=True, timeout=12)
    if not ok and "-600" in out:                      # not ready yet after a launch
        time.sleep(1.5)
        ok, out = _tell(SPOTIFY, f'play track "{_q(uri)}"', launch=True, timeout=12)
    return ok, out


def _press_play_button(words: list[str] | None = None) -> bool:
    """On the Spotify page in front (Liked Songs, a search): press its Play button through Accessibility -
    the one naming `words` when given, else the biggest (the page's green one, not the player bar's).
    True when music started or changed. Needs Mint's Accessibility permission; UNTESTED on the dev Mac
    (the test shell had no Accessibility access), so every failure just returns False."""
    try:
        import AppKit
        import ApplicationServices as AX
        from mint.screen import axkit
        if not AX.AXIsProcessTrusted():
            return False
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLES[SPOTIFY])
        if not apps:
            return False
        app = apps[0]
        axkit.unlock(app, wait=1.0)
        window = axkit.focused_window(app.processIdentifier())
        if window is None:
            return False
        best, best_score = None, 0.0
        for element in axkit.walk(window, limit=6000):
            if axkit.attr(element, "AXRole") != "AXButton":
                continue
            label = f"{axkit.attr(element, 'AXDescription') or ''} {axkit.attr(element, 'AXTitle') or ''}".strip()
            if not re.match(r"^play\b", label, re.I):
                continue
            box = axkit.frame(element)
            if box is None or box[2] * box[3] <= 0:
                continue
            if words:
                have = _words(label)
                score = sum(1 for w in words if w in have) / len(words)
                if score < 0.5:
                    continue
                score += box[2] * box[3] / 1e6
            else:
                score = box[2] * box[3] + (1e9 if "liked" in label.lower() else 0)
            if score > best_score:
                best, best_score = element, score
        if best is None:
            return False
        before = _read(SPOTIFY)
        AX.AXUIElementPerformAction(best, "AXPress")
        time.sleep(1.2)
        after = _read(SPOTIFY)
        return after["state"] == "playing" and (before["state"] != "playing" or after["uri"] != before["uri"])
    except Exception as error:
        log.info("spotify play button: %s", error)
        return False


def _play_liked(app: str) -> str:
    if app == MUSIC:
        for name in ("Favorite Songs", "Favourite Songs", "Loved"):
            ok, out = _tell(MUSIC, f'play playlist "{_q(name)}"', launch=True)
            if ok:
                _show_player()
                return f"Playing your {name} in Music."
        return "FAILED: Music has no Favorite Songs playlist to play."
    if not _launch(SPOTIFY):
        return "FAILED: Spotify did not start."
    ok, out = _tell(SPOTIFY, 'open location "spotify:collection:tracks"', launch=True)
    if not ok:
        return f"FAILED: Spotify would not open Liked Songs: {out[:120]}"
    time.sleep(1.5)
    if _press_play_button():
        info = _after_change(0.3)
        _show_player()
        return f"Playing your Liked Songs on Spotify{': ' + _describe(info) if _describe(info) else ''}."
    return ("Opened your Liked Songs in Spotify. Spotify doesn't let other apps start that list, so the user "
            "presses the green Play button once (tell them in a few words).")


def _music_find_and_play(query: str, kind: str, artist: str) -> str:
    """Apple Music: play from the library by name (UNTESTED here - Music isn't used on the dev Mac)."""
    q, a = _q(query), _q(artist)
    if kind == "playlist" or kind == "genre":
        ok, out = _tell(MUSIC, f"""set found to (every user playlist whose name contains "{q}")
if found is not {{}} then
play item 1 of found
return "playlist" & "{SEP}" & (name of item 1 of found)
end if
return "none" """, launch=True, timeout=15)
        if ok and out.startswith("playlist"):
            return f"Playing the playlist {out.split(SEP)[1]} in Music."
        if kind == "genre":
            ok, out = _tell(MUSIC, f"""set found to (every track of library playlist 1 whose genre contains "{q}")
if found is {{}} then return "none"
set shuffle enabled to true
play some item of found
return "genre" """, launch=True, timeout=20)
            if ok and out == "genre":
                return f"Playing {query} from your Music library, shuffled."
    field = {"artist": "artist", "album": "album"}.get(kind, "name")
    condition = f'{field} contains "{q}"' + (f' and artist contains "{a}"' if a and field != "artist" else "")
    ok, out = _tell(MUSIC, f"""set found to (every track of library playlist 1 whose {condition})
if found is {{}} then
set found to (search library playlist 1 for "{q}{(' ' + a) if a else ''}")
end if
if found is {{}} then return "none"
play item 1 of found
return (name of item 1 of found) & "{SEP}" & (artist of item 1 of found)""", launch=True, timeout=20)
    if ok and out and out != "none":
        name, _, by = out.partition(SEP)
        return f"Playing {name}{' by ' + by if by else ''} from your Music library."
    return (f"FAILED: nothing called '{query}' is in the Music library. Music can only play the user's library "
            "by voice; songs from the Apple Music catalogue have to be added first.")


def play(query: str = "", kind: str = "auto", artist: str = "", app: str = "") -> str:
    player = pick_app(app)
    kind = (kind or "auto").lower()
    start_service()
    if not query and kind != "liked":
        return resume(player)
    if kind == "liked" or re.search(r"\b(liked|saved) songs\b|\bmy likes\b", query, re.I):
        return _play_liked(player)
    if player == MUSIC:
        result = _music_find_and_play(query, kind, artist)
        if not result.startswith("FAILED"):
            _after_change()
            _show_player()
        return result
    personal = re.search(r"\b(discover weekly|release radar|daily mix( \d)?|on repeat|repeat rewind|time capsule|"
                         r"daylist|your top songs)\b", query, re.I)
    direct = _SPOTIFY_LINK.search(query) or re.match(r"^spotify:(track|album|artist|playlist):([A-Za-z0-9]{22})$", query)
    if direct:
        found = {"uri": f"spotify:{direct.group(1)}:{direct.group(2)}", "kind": direct.group(1), "name": "", "by": ""}
    elif personal:
        found = {}                                    # made for this user: only Spotify itself knows the link
    else:
        found = find(query, kind, artist)
    if not found.get("uri"):
        _launch(SPOTIFY)
        words = " ".join(x for x in (query, artist) if x)
        _tell(SPOTIFY, f'open location "spotify:search:{_q(urllib.parse.quote(words))}"', launch=True)
        subprocess.run(["open", "-b", BUNDLES[SPOTIFY]], capture_output=True)
        time.sleep(2.0)
        if kind != "genre" and _press_play_button(_words(words)):
            info = _after_change(0.2)
            _show_player()
            return f"Playing {_describe(info) or words} on Spotify (its top search result)."
        return (f"NOT FOUND: the web search found no Spotify link for '{words}' (the search engines may be busy), "
                "so Spotify's own search is open with those words for the user to pick one - tell them in a few "
                "words. If you can search the web yourself, find the open.spotify.com link and call music "
                "action=play with query=<that link>: it plays at once.")
    if not _launch(SPOTIFY):
        return "FAILED: Spotify did not start."
    ok, out = _spotify_play_uri(found["uri"])
    if not ok:
        return f"FAILED: Spotify would not play {found['uri']}: {out[:160]}"
    info = _after_change(1.2)
    _show_player()
    named, by, now = found.get("name") or "", found.get("by") or "", _describe(info)
    if found["kind"] == "track" or (kind == "track" and now):      # a song's single plays the song
        said = now or f"{named}{' by ' + by if by else ''}"
    elif found["kind"] == "album":
        album = named or info.get("album") or ""
        said = f"the album {album}".strip() + (f" by {by}" if by and len(by) < 40 else "")
    elif found["kind"] == "artist":
        said = f"{named or artist or info.get('artist') or query}'s songs"
    else:
        said = f"the playlist {named}" if named else "the playlist"
    tail = f" (now: {now})" if now and found["kind"] != "track" else ""
    if info.get("state") != "playing":
        return f"Asked Spotify to play {said}, but it isn't playing yet (state: {info.get('state')})."
    return f"Playing {said} on Spotify{tail}."


def resume(app: str = "") -> str:
    player = pick_app(app)
    if player not in _running_apps() and not _launch(player):
        return f"FAILED: {player} did not start."
    ok, out = _tell(player, "play", launch=True)
    if not ok:
        return f"FAILED: {player} did not respond: {out[:120]}"
    info = _after_change(0.7)
    _show_player()
    return f"{player}: playing {_describe(info) or 'again'}."


def _control(app: str, body: str, done: str) -> str:
    player = pick_app(app)
    if player not in _running_apps():
        return f"{player} isn't open, so nothing is playing."
    ok, out = _tell(player, body)
    if not ok:
        return f"FAILED: {player} did not respond: {out[:120]}"
    info = _after_change(0.6)
    return done.format(app=player, now=_describe(info) or "nothing")


def pause(app: str = "") -> str:
    return _control(app, "pause", "Paused {app}.")


def toggle(app: str = "") -> str:
    return _control(app, "playpause", "{app}: play/pause.")


def next_track(app: str = "") -> str:
    return _control(app, "next track", "Next: {now}.")


def previous_track(app: str = "") -> str:
    player = pick_app(app)
    body = "previous track" if player == SPOTIFY else "back track"
    return _control(player, body, "Back: {now}.")


def set_volume(value: str, app: str = "") -> str:
    player = pick_app(app)
    if player not in _running_apps():
        return f"{player} isn't open."
    ok, out = _tell(player, "return sound volume as text")
    if not ok:
        return f"FAILED: {player} did not respond: {out[:120]}"
    current = int(_number(out))
    text = str(value or "").strip().lower().rstrip("%")
    if text in ("up", "louder", "+"):
        level = current + VOLUME_STEP
    elif text in ("down", "quieter", "softer", "-"):
        level = current - VOLUME_STEP
    elif text in ("mute", "off", "silent"):
        level = 0
    elif text in ("max", "full"):
        level = 100
    elif re.match(r"^[+-]\d+$", text):
        level = current + int(text)
    else:
        try:
            level = int(float(text))
        except ValueError:
            return f"{player}'s volume is {current}%."
    level = max(0, min(100, level))
    ok, out = _tell(player, f"set sound volume to {level}")
    if not ok:
        return f"FAILED: {out[:120]}"
    poke()
    return f"{player} volume {current}% -> {level}%."


def _switch(value: str, current: bool) -> bool:
    text = str(value or "").strip().lower()
    if text in ("on", "true", "yes", "1", "enable", "enabled"):
        return True
    if text in ("off", "false", "no", "0", "disable", "disabled"):
        return False
    return not current


def shuffle(value: str = "", app: str = "") -> str:
    player = pick_app(app)
    if player not in _running_apps():
        return f"{player} isn't open."
    prop = "shuffling" if player == SPOTIFY else "shuffle enabled"
    ok, out = _tell(player, f"return {prop} as text")
    if not ok:
        return f"FAILED: {out[:120]}"
    want = _switch(value, out == "true")
    ok, out = _tell(player, f"set {prop} to {'true' if want else 'false'}")
    poke()
    return f"Shuffle {'on' if want else 'off'} in {player}." if ok else f"FAILED: {out[:120]}"


def repeat(value: str = "", app: str = "") -> str:
    player = pick_app(app)
    if player not in _running_apps():
        return f"{player} isn't open."
    text = str(value or "").strip().lower()
    if player == SPOTIFY:
        ok, out = _tell(player, "return repeating as text")
        want = _switch(text, out == "true")
        ok, out = _tell(player, f"set repeating to {'true' if want else 'false'}")
        poke()
        return (f"Repeat {'on' if want else 'off'} in Spotify." if ok else f"FAILED: {out[:120]}")
    mode = {"one": "one", "song": "one", "track": "one", "all": "all", "on": "all", "off": "off"}.get(text)
    if mode is None:
        ok, out = _tell(player, "return song repeat as text")
        mode = "off" if ok and out != "off" else "all"
    ok, out = _tell(player, f"set song repeat to {mode}")
    poke()
    return f"Repeat {mode} in Music." if ok else f"FAILED: {out[:120]}"


def _seconds(text: str) -> float | None:
    text = str(text).strip().lower()
    match = re.match(r"^(\d+):(\d{1,2})(?::(\d{1,2}))?$", text)
    if match:
        parts = [int(x) for x in match.groups() if x is not None]
        return float(parts[0] * 60 + parts[1]) if len(parts) == 2 else float(parts[0] * 3600 + parts[1] * 60 + parts[2])
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(m|min|minutes?|s|sec|seconds?)?$", text)
    if match:
        value = float(match.group(1))
        return value * 60 if (match.group(2) or "").startswith("m") else value
    return None


def seek(value: str, app: str = "") -> str:
    player = pick_app(app)
    if player not in _running_apps():
        return f"{player} isn't open."
    info = now_playing(max_age=0)
    text = str(value or "").strip().lower()
    relative = 0
    for word, sign in (("forward", 1), ("ahead", 1), ("+", 1), ("back", -1), ("rewind", -1), ("-", -1)):
        if text.startswith(word):
            relative, text = sign, text[len(word):].strip()
            break
    if text in ("start", "beginning", "restart", "0"):
        target = 0.0
    else:
        amount = _seconds(text or "15")
        if amount is None:
            return "FAILED: say where to go, like 1:30, 90 seconds, forward 30 or back 15."
        target = info.get("position", 0.0) + relative * amount if relative else amount
    duration = info.get("duration") or 0
    target = max(0.0, min(target, duration - 1 if duration else target))
    ok, out = _tell(player, f"set player position to {target:.1f}")
    poke()
    return f"Jumped to {_clock(target)}{' of ' + _clock(duration) if duration else ''}." if ok else f"FAILED: {out[:120]}"


def like(app: str = "") -> str:
    player = pick_app(app)
    info = now_playing(max_age=0)
    if player == MUSIC:
        ok, out = _tell(MUSIC, "set favorited of current track to true")
        if not ok:
            ok, out = _tell(MUSIC, "set loved of current track to true")        # before macOS 14
        return f"Added {_describe(info) or 'the song'} to your favourites in Music." if ok else f"FAILED: {out[:120]}"
    return ("NOT POSSIBLE: Spotify doesn't let other apps like songs (its AppleScript can only read that). Tell the "
            f"user to click the + / heart next to {_describe(info) or 'the song'} in Spotify, or press "
            "Option-Shift-B there.")


def open_app(app: str = "") -> str:
    player = pick_app(app)
    done = subprocess.run(["open", "-b", BUNDLES[player]], capture_output=True, text=True)
    return f"Opened {player}." if done.returncode == 0 else f"FAILED: could not open {player}: {done.stderr[:120]}"


def which_app() -> str:
    running = _running_apps()
    rows = []
    for app in (SPOTIFY, MUSIC):
        if app in running:
            rows.append(f"{app}: open, {_state_only(app) or 'unknown state'}")
        elif _installed(app):
            rows.append(f"{app}: installed, not open")
    chosen = pick_app()
    return "; ".join(rows) + f". Music commands go to {chosen}" + \
        (" (say 'in Spotify' or 'in Apple Music' to choose)." if len(rows) > 1 else ".")


def describe_now() -> str:
    info = now_playing(max_age=0)
    if not info.get("app"):
        return "No music app is open."
    if info["state"] == "stopped" or not info.get("title"):
        return f"{info['app']} is open but nothing is playing."
    where = f"{_clock(info['position'])} of {_clock(info['duration'])}" if info.get("duration") else ""
    album = f" (from {info['album']})" if info.get("album") and info["album"] != info["title"] else ""
    state = "Playing" if info["state"] == "playing" else "Paused on"
    extras = [x for x in (where, f"volume {info['volume']}%" if info.get("volume") is not None else "",
                          "shuffle on" if info.get("shuffle") else "", "repeat on" if info.get("repeat") else "") if x]
    return f"{state} {_describe(info)}{album} in {info['app']}" + (f" - {', '.join(extras)}." if extras else ".")


# --- the tool ------------------------------------------------------------------------------------

def tool(args: dict) -> str:
    action = str(args.get("action") or "now_playing").lower().replace(" ", "_")
    app = str(args.get("app") or "")
    value = str(args.get("value") if args.get("value") is not None else "")
    start_service()
    try:
        if action == "play":
            return play(str(args.get("query") or ""), str(args.get("kind") or "auto"), str(args.get("artist") or ""), app)
        if action in ("resume", "unpause"):
            return resume(app)
        if action in ("pause", "stop"):
            return pause(app)
        if action in ("toggle", "playpause"):
            return toggle(app)
        if action in ("next", "skip"):
            return next_track(app)
        if action in ("previous", "back"):
            return previous_track(app)
        if action in ("now_playing", "what", "status"):
            return describe_now()
        if action == "volume":
            return set_volume(value, app)
        if action == "shuffle":
            return shuffle(value, app)
        if action == "repeat":
            return repeat(value, app)
        if action == "seek":
            return seek(value, app)
        if action in ("like", "love", "favorite", "favourite"):
            return like(app)
        if action in ("open_app", "open"):
            return open_app(app)
        if action == "which_app":
            return which_app()
        if action in ("show_player", "hide_player"):
            from mint.ui import music_player
            if action == "show_player":
                music_player.show(reason="asked")
                return "Showing the mini player."
            music_player.hide()
            return "Hid the mini player."
    except Exception as error:
        log.exception("music %s", action)
        return f"FAILED: {action}: {error}"
    return f"FAILED: unknown action '{action}'."


PROMPT = """Music (Spotify or Apple's Music) -> music: "play Blinding Lights by the Weeknd" = play query="Blinding \
Lights" artist="The Weeknd" kind=track; an artist, album, playlist, genre or mood, "my Liked Songs" = that \
kind; "play music" with nothing named = resume. The music app's volume is music action=volume; the Mac's is \
set_volume. Say what is playing in a few words; never read URIs out."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="music",
        description=("Play and control music in Spotify (or Apple's Music app): play a song, artist, album, "
                     "playlist, genre/mood or the user's Liked Songs by name (found on Spotify without a login), "
                     "pause, resume, next, previous, what's playing, the app's volume, shuffle, repeat, seek, like "
                     "the current song, open the app, which app is used, show/hide the mini player."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["play", "pause", "resume", "next", "previous", "now_playing",
                                                 "volume", "shuffle", "repeat", "seek", "like", "open_app",
                                                 "which_app", "show_player", "hide_player"]),
            "query": types.Schema(type=S, description="play: the song / artist / album / playlist name, or the "
                                                      "genre or mood; empty = resume"),
            "artist": types.Schema(type=S, description="play: the artist when the user named one with a song or "
                                                       "album ('by the Weeknd')"),
            "kind": types.Schema(type=S, enum=["track", "artist", "album", "playlist", "genre", "liked", "auto"],
                                 description="play: what the query is (auto when unsure)"),
            "value": types.Schema(type=S, description="volume: 0-100, up, down, mute; shuffle/repeat: on, off "
                                                      "(repeat in Music: one/all/off); seek: 1:30, 90, forward 30, "
                                                      "back 15, start"),
            "app": types.Schema(type=S, enum=["spotify", "music"],
                                description="only when the user names the app")},
            required=["action"]))]


HANDLERS = {"music": tool}
