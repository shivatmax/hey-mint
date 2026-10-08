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
