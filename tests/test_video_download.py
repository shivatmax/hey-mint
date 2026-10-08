"""Downloading videos (mint/tools/video_download.py), without the network: the format yt-dlp is asked for, how
the page finder sorts what a page loads (playlists, files, segments, ads, DRM), the order it tries things in,
cookies passed as a private file, the saved command, and DRM never downloaded."""
import os
import stat
from pathlib import Path

import pytest

try:
    from mint.tools import video_download as vd
except ImportError:
    from mint import video_download as vd


def test_formats_prefer_what_quicktime_plays():
    best = vd._format("best", True)
    assert best.startswith("bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]")
    assert "[height<=720]" in vd._format("720p", True)
    assert vd._format("4k", True).startswith("bv*[height<=2160]+ba")     # any codec for 4K
    assert vd._format("audio", True).startswith("ba[ext=m4a]")
    assert "+" not in vd._format("best", False)                         # no ffmpeg: one file, nothing to merge


@pytest.mark.parametrize("url,mime,kind", [
    ("https://cdn.example.com/v/master.m3u8?token=1", "", "hls"),
    ("https://cdn.example.com/v/play", "application/vnd.apple.mpegurl", "hls"),
    ("https://cdn.example.com/v/manifest.mpd", "", "dash"),
    ("https://cdn.example.com/v/clip.mp4", "video/mp4", "file"),
    ("https://cdn.example.com/v/seg-00012.ts", "video/mp2t", ""),
    ("https://cdn.example.com/v/chunk-3.m4s", "video/iso.segment", ""),
    ("https://cdn.example.com/v/init.mp4", "video/mp4", ""),
    ("https://pubads.g.doubleclick.net/x/ad.mp4", "video/mp4", ""),
    ("https://cdn.example.com/poster.jpg", "image/jpeg", ""),
    ("blob:https://site/abc", "", ""),
])
def test_what_counts_as_a_video(url, mime, kind):
    assert vd._kind(url, mime) == kind


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
high.m3u8
"""


def test_playlists_read_for_quality_and_drm():
    info = vd._hls_info(MASTER)
    assert info["master"] and info["height"] == 1080 and not info["drm"]
    media = vd._hls_info("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"k\"\n#EXTINF:6.0,\na.ts\n#EXTINF:4.0,\nb.ts\n")
    assert not media["drm"] and media["seconds"] == 10.0                # AES-128 is not DRM: it downloads
    fairplay = vd._hls_info('#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://drm",'
                            'KEYFORMAT="com.apple.streamingkeydelivery"\n')
    assert fairplay["drm"]
    assert vd._dash_info('<MPD><ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>'
                         '</MPD>')["drm"]
    assert vd._dash_info('<MPD><Representation height="720"/></MPD>') == {"height": 720, "drm": False}


def test_ranking_master_playlist_first_then_dash_then_big_files():
    media = [{"kind": "file", "url": "small.mp4", "length": 120_000},
             {"kind": "hls", "url": "variant.m3u8", "master": False, "height": 0},
             {"kind": "file", "url": "big.mp4", "length": 40_000_000},
             {"kind": "dash", "url": "m.mpd", "height": 720},
             {"kind": "hls", "url": "master.m3u8", "master": True, "height": 1080}]
    order = [m["url"] for m in vd.rank(media)]
    assert order[0] == "master.m3u8"
    assert order.index("big.mp4") < order.index("m.mpd") < order.index("variant.m3u8")
    assert order[-1] == "small.mp4"                                     # a thumbnail-sized file comes last


def test_drm_only_when_nothing_playable_is_left():
    assert not vd._drm({"_has_drm": True, "formats": [{"has_drm": True}, {"has_drm": False}]})
    assert vd._drm({"_has_drm": True, "formats": [{"has_drm": True}]})
    assert vd._drm({"has_drm": True})
    assert not vd._drm({"formats": [{"format_id": "18"}]})


class _FakeYDL:
    """yt-dlp that says "DRM protected" for the copies listed in `protected`."""

    def __init__(self, protected):
        self.protected = set(protected)
        self.tried = []

    def process_ie_result(self, info, download=True):
        best = max(info["formats"], key=lambda f: f["q"])
        if not download:
            return {"requested_formats": [best]}
        self.tried.append(best["format_id"])
        if best["format_id"] in self.protected:
            raise RuntimeError("ERROR: This format is DRM protected; Try selecting another format")
        return {"format_id": best["format_id"]}


def test_a_protected_copy_is_skipped_for_the_next_one():
    info = {"formats": [{"format_id": "a", "q": 3}, {"format_id": "b", "q": 2}, {"format_id": "c", "q": 1}]}
    ydl = _FakeYDL({"a", "b"})
    assert vd._download_skipping_drm(ydl, info)["format_id"] == "c"
    assert ydl.tried == ["a", "b", "c"]
    with pytest.raises(vd.DRMProtected):
        vd._download_skipping_drm(_FakeYDL({"a", "b", "c"}), info)


def test_cookies_go_in_a_private_file_not_a_header(tmp_path):
    path = vd._cookie_file([{"domain": ".example.com", "path": "/", "secure": True, "expires": 2_000_000_000,
                             "name": "sid", "value": "abc"}, {"domain": "", "name": "x"}])
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        lines = Path(path).read_text().splitlines()
        assert lines[0] == "# Netscape HTTP Cookie File"
        assert lines[1].split("\t") == [".example.com", "TRUE", "/", "TRUE", "2000000000", "sid", "abc"]
        assert len(lines) == 2
    finally:
        os.unlink(path)
    opts = vd._options(tmp_path, "best", {}, headers={"Referer": "https://a.b/", "Cookie": "sid=1"},
                       cookiefile="/tmp/c.txt")
    assert "Cookie" not in opts["http_headers"] and opts["http_headers"]["Referer"] == "https://a.b/"
    assert opts["cookiefile"] == "/tmp/c.txt"
    assert opts["extractor_args"]["generic"]["fragment_query"] == [""]   # a signed playlist's token reaches its parts


def test_the_saved_command_repeats_the_download_without_cookies(tmp_path):
    video = tmp_path / "Course intro.mp4"
    video.write_bytes(b"x")
    script = vd.write_script(video, "https://cdn.example.com/master.m3u8?sig=1",
                             {"Referer": "https://site.example/page", "Cookie": "secret=1", "User-Agent": "UA x"})
    text = script.read_text()
    assert script.suffix == ".command" and os.access(script, os.X_OK)
    assert "-m yt_dlp" in text and "https://cdn.example.com/master.m3u8?sig=1" in text
    assert "Referer:https://site.example/page" in text and "secret" not in text


def _no_network(monkeypatch, fetch, sniff):
    monkeypatch.setattr(vd, "_ytdlp_fetch", fetch)
    monkeypatch.setattr(vd, "sniff", sniff)
    monkeypatch.setattr(vd, "_quicktime", lambda path, *a: path)
    monkeypatch.setattr(vd, "_user_chrome", lambda: None)              # never this Mac's real Chrome


def test_a_page_ytdlp_doesnt_know_is_searched_and_its_stream_downloaded(tmp_path, monkeypatch):
    calls = []

    def fetch(url, folder, quality, state, headers=None, name="", cookiefile=""):
        calls.append((url, headers, cookiefile, os.path.exists(cookiefile) if cookiefile else None))
        if url == "https://landing.example/watch":
            raise RuntimeError("ERROR: Unsupported URL: https://landing.example/watch")
        out = tmp_path / f"{name or 'v'}.mp4"
        out.write_bytes(b"video")
        return {"path": str(out), "title": name, "height": 720, "duration": 90}

    def sniff(url, seconds=0, browser=None):
        return {"candidates": [{"kind": "hls", "url": "https://cdn.example/master.m3u8", "master": True,
                                "headers": {"Referer": "https://landing.example/"}}],
                "frames": [], "title": "Launch film", "ua": "Mozilla/5.0 Chrome", "drm": "", "videos": [],
                "cookies": [{"domain": ".landing.example", "name": "s", "value": "1"}]}
    _no_network(monkeypatch, fetch, sniff)
    got = vd.download("https://landing.example/watch", "720", tmp_path, {})
    assert got["title"] == "Launch film" and "HLS stream" in got["note"]
    url, headers, cookiefile, existed = calls[-1]
    assert url == "https://cdn.example/master.m3u8"
    assert headers["Referer"] == "https://landing.example/" and headers["User-Agent"] == "Mozilla/5.0 Chrome"
    assert existed and not os.path.exists(cookiefile)                    # there while used, gone after


def test_drm_page_is_refused(tmp_path, monkeypatch):
    def fetch(url, *a, **k):
        raise RuntimeError("ERROR: Unsupported URL: " + url)
    _no_network(monkeypatch, fetch, lambda url, **k: {"candidates": [], "frames": [], "title": "", "ua": "",
                                                       "drm": "com.widevine.alpha", "videos": [], "cookies": []})
    with pytest.raises(vd.DRMProtected):
        vd.download("https://stream.example/film", "best", tmp_path, {})


def test_a_known_site_failing_for_a_real_reason_says_why(tmp_path, monkeypatch):
    def fetch(url, *a, **k):
        raise RuntimeError("ERROR: [youtube] abc: Private video. Sign in if you've been granted access")
    _no_network(monkeypatch, fetch, lambda *a, **k: pytest.fail("no page search for a private video"))
    monkeypatch.setattr(vd, "_user_cookies", lambda url: [])
    with pytest.raises(RuntimeError, match="signed in.*chrome://inspect"):
        vd.download("https://www.youtube.com/watch?v=abc", "best", tmp_path, {})


def test_a_sign_in_video_uses_the_users_chrome_cookies(tmp_path, monkeypatch):
    seen = []

    def fetch(url, folder, quality, state, headers=None, name="", cookiefile=""):
        if not cookiefile:
            raise RuntimeError("ERROR: [Instagram] x: Requested content is not available, rate-limit reached or "
                               "login required. Use --cookies")
        seen.append(Path(cookiefile).read_text())
        out = tmp_path / "reel.mp4"
        out.write_bytes(b"v")
        return {"path": str(out), "title": "reel"}
    _no_network(monkeypatch, fetch, lambda *a, **k: pytest.fail("no page search"))
    monkeypatch.setattr(vd, "_user_cookies", lambda url: [{"domain": ".instagram.com", "name": "sessionid",
                                                           "value": "s"}])
    got = vd.download("https://www.instagram.com/reel/x/", "best", tmp_path, {})
    assert got["note"] == "with your Chrome sign-in" and "sessionid" in seen[0]


def test_the_tool_asks_for_a_link_when_no_page_is_open(monkeypatch):
    monkeypatch.setattr(vd, "_front_url", lambda: "")
    assert "Which video?" in vd.download_video({})


def test_report_says_where_and_what(tmp_path, monkeypatch):
    monkeypatch.setattr(vd, "_ffmpeg", lambda: "/usr/bin/true")
    video = tmp_path / "Talk.mp4"
    video.write_bytes(b"x" * 2048)
    text = vd._report({"path": str(video), "title": "Talk", "height": 1080, "duration": 754})
    assert "Downloaded “Talk” (1080p, 12:34, 2 KB)" in text and str(video.name) in text


# --- playlists, subtitles, stop, disk space, Telegram ------------------------------------------------------

def test_playlist_and_subtitle_options(tmp_path):
    opts = vd._options(tmp_path, "best", {"playlist": True, "subtitles": "hi"})
    assert opts["noplaylist"] is False and opts["playlistend"] == vd.PLAYLIST_MAX
    assert "%(playlist_index|0)03d" in opts["outtmpl"]
    if vd._ffmpeg():
        assert opts["subtitleslangs"][0] == "hi" and "hi-orig" in opts["subtitleslangs"]
        assert "hi.*" not in opts["subtitleslangs"]                       # never machine translations
        assert opts["postprocessors"][0]["key"] == "FFmpegEmbedSubtitle"
    english = vd._options(tmp_path, "best", {"subtitles": "yes"})
    if vd._ffmpeg():
        assert english["subtitleslangs"][0] == "en"


def test_playlist_report():
    text = vd._report({"paths": ["/x/a.mp4", "/x/b.mp4"], "path": "/x/a.mp4", "count": 2, "failed": 1,
                       "title": "Talks", "folder": "/x"})
    assert text.startswith("Downloaded 2 videos of “Talks”") and "1 couldn't be downloaded" in text


def test_no_download_that_would_fill_the_disk(monkeypatch, tmp_path):
    import collections
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(vd.shutil, "disk_usage", lambda p: usage(1, 1, 5_000_000_000))
    vd._room(tmp_path, 1_000_000_000)                                      # fine
    with pytest.raises(RuntimeError, match="not enough free space"):
        vd._room(tmp_path, 4_000_000_000)
    monkeypatch.setattr(vd.shutil, "disk_usage", lambda p: usage(1, 1, 900_000_000))
    with pytest.raises(RuntimeError, match="not enough free space"):
        vd._room(tmp_path)


def test_stop_cancels_and_leaves_nothing_half_done(tmp_path, monkeypatch):
    import threading
    import time as _time
    monkeypatch.setattr(vd, "SYNC_WAIT", 0.2)
    monkeypatch.setattr(vd, "FOLDER", tmp_path)
    monkeypatch.setattr(vd, "_notify", lambda text: None)
    started = threading.Event()

    def slow(url, quality, folder, state, script=False):
        (folder / "clip.mp4.part").write_bytes(b"half")
        started.set()
        while not state.get("cancel"):
            _time.sleep(0.05)
        raise vd.Stopped("stopped")
    monkeypatch.setattr(vd, "download", slow)
    reply = vd.download_video({"url": "https://example.com/v"})
    assert "background" in reply
    started.wait(2)
    assert "Stopping" in vd.download_video({"stop": True})
    for _ in range(40):
        if not vd._jobs:
            break
        _time.sleep(0.05)
    assert not vd._jobs and not list(tmp_path.glob("*.part"))
    assert vd.download_video({"stop": True}) == "No video is downloading."


def test_a_long_download_from_telegram_is_sent_there(tmp_path, monkeypatch):
    sent = []

    class Bridge:
        def paired(self):
            return {"chat": 1}

        def send_file(self, path, caption=""):
            sent.append(("file", Path(path).name, caption))

        def send(self, text):
            sent.append(("text", text))
    try:
        from mint.app import telegram
    except ImportError:
        from mint import telegram
    monkeypatch.setattr(telegram, "bridge", Bridge())
    small = tmp_path / "Talk.mp4"
    small.write_bytes(b"x" * 1000)
    vd._send_to_phone(small, "Talk")
    big = tmp_path / "Film.mp4"
    with open(big, "wb") as fh:
        fh.truncate(telegram.MAX_UPLOAD + 1)
    vd._send_to_phone(big, "Film")
    assert sent[0] == ("file", "Talk.mp4", "🎬 Talk")
    assert sent[1][0] == "text" and "too big to send here" in sent[1][1]


# --- recording a page's player (MSE capture) ----------------------------------------------------------------

def _box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def test_fmp4_timestamps_are_read():
    mdhd = _box(b"mdhd", b"\x00\x00\x00\x00" + b"\x00" * 8 + (90000).to_bytes(4, "big") + b"\x00" * 8)
    assert vd._timescale(_box(b"ftyp", b"isom") + mdhd) == 90000
    tfdt0 = _box(b"tfdt", b"\x00\x00\x00\x00" + (180000).to_bytes(4, "big"))
    tfdt1 = _box(b"tfdt", b"\x01\x00\x00\x00" + (900000).to_bytes(8, "big"))
    assert vd._tfdt(_box(b"moof", tfdt0)) == 180000 and vd._tfdt(_box(b"moof", tfdt1)) == 900000
    assert vd._tfdt(b"no boxes here") is None


def _chunk(sniffer, track, data, first=True, mime=None):
    import base64
    import json as _json
    payload = {"t": track, "mime": mime} if mime else {"t": track, "n": 0, "first": first,
                                                         "b": base64.b64encode(data).decode()}
    sniffer._chunk({"name": "__mintChunk", "payload": _json.dumps(payload)}, "s1")


def test_capture_skips_resent_pieces_and_splits_on_quality_switch(tmp_path):
    s = vd.Sniffer(browser=None, capture=tmp_path)
    s.sessions.add("s1")
    init = _box(b"ftyp", b"isom") + _box(b"mdhd", b"\x00" * 12 + (1000).to_bytes(4, "big") + b"\x00" * 8)

    def frag(seconds):
        return _box(b"moof", _box(b"tfdt", b"\x00\x00\x00\x00" + (seconds * 1000).to_bytes(4, "big"))) + b"m" * 100
    _chunk(s, 1, b"", mime='video/mp4; codecs="avc1.4d401f"')
    _chunk(s, 1, init)
    _chunk(s, 1, frag(0))
    _chunk(s, 1, frag(4))
    _chunk(s, 1, frag(4))                                                  # sent again after a seek: skipped
    _chunk(s, 1, b"tail of the same append", first=False)                 # (part of a skipped append: skipped)
    _chunk(s, 1, init)                                                     # a quality switch: a new run
    _chunk(s, 1, frag(6))
    track = s.tracks[("s1", 1)]
    assert len(track["runs"]) == 2 and track["mime"].startswith("video/mp4")
    first = track["runs"][0].read_bytes()
    assert first.count(b"tfdt") == 2 and b"tail of the same" not in first
    assert track["runs"][1].read_bytes().count(b"tfdt") == 1


@pytest.mark.skipif(not vd._ffmpeg(), reason="needs ffmpeg")
def test_recorded_tracks_become_one_video(tmp_path):
    import subprocess
    ffmpeg = vd._ffmpeg()
    frag = ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"]
    v1, v2, a = tmp_path / "v1.bin", tmp_path / "v2.bin", tmp_path / "a.bin"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-t", "2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", *frag, str(v1)], check=True)
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-t", "3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", *frag, str(v2)], check=True)
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "5", "-c:a", "aac",
                    *frag, str(a)], check=True)
    tracks = {("s", 1): {"mime": "video/mp4", "runs": [v1, v2], "sizes": [v1.stat().st_size + 30_000,
                                                                          v2.stat().st_size + 30_000]},
              ("s", 2): {"mime": "audio/mp4", "runs": [a], "sizes": [a.stat().st_size + 30_000]}}
    got = vd._join_tracks(tracks, tmp_path, tmp_path / "out", {"title": "Joined", "d": 5}, ffmpeg, subprocess,
                          "https://example.com")
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", got["path"]], capture_output=True, text=True).stderr
    assert "640x360" in out and "Audio: aac" in out
    m = __import__("re").search(r"Duration: 00:00:0(\d)\.(\d\d)", out)
    assert m and 4.8 <= float(f"{m.group(1)}.{m.group(2)}") <= 5.3                  # 2 s + 3 s, in order


@pytest.mark.skipif(not os.environ.get("MINT_BROWSER_TESTS"), reason="opens Chrome: MINT_BROWSER_TESTS=1")
def test_a_page_that_only_streams_to_its_player_is_recorded(tmp_path):
    """A local page that feeds its <video> through MediaSource from files with no type or extension."""
    import http.server
    import socketserver
    import subprocess
    import threading
    ffmpeg = vd._ffmpeg()
    media = tmp_path / "site" / "media"
    media.mkdir(parents=True)
    frag = ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"]
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-t", "6",
                    "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", "30", *frag,
                    str(media / "v")], check=True)
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:a", "aac",
                    *frag, str(media / "a")], check=True)
    (tmp_path / "site" / "index.html").write_text("""<!doctype html><title>Tour</title><video id=v muted></video>
<script>(async () => { const v = document.getElementById('v'); const ms = new MediaSource();
v.src = URL.createObjectURL(ms); await new Promise(r => ms.addEventListener('sourceopen', r, {once: true}));
const vb = ms.addSourceBuffer('video/mp4; codecs="avc1.42E01E"'), ab = ms.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');
const get = async u => new Uint8Array(await (await fetch(u)).arrayBuffer());
const feed = async (sb, b) => { sb.appendBuffer(b); await new Promise(r => sb.addEventListener('updateend', r, {once: true})); };
const [x, y] = await Promise.all([get('/media/v'), get('/media/a')]); await Promise.all([feed(vb, x), feed(ab, y)]);
ms.endOfStream(); })();</script>""")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path / "site"), **k)

        def guess_type(self, path):
            return "application/octet-stream" if "/media/" in path else "text/html"

        def log_message(self, *a):
            pass
    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        got = vd.download(f"http://127.0.0.1:{server.server_address[1]}/", "best", tmp_path / "out", {})
    finally:
        server.shutdown()
    assert "recorded from the page's player" in got["note"]
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", got["path"]], capture_output=True, text=True).stderr
    assert "640x360" in out and "Audio: aac" in out
