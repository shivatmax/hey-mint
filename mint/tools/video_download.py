"""Download a video: from a link, or from the page in front.

1. A link yt-dlp knows (YouTube and about 1,800 other sites - Vimeo, X, Instagram, TikTok, Loom, Reddit...): yt-dlp
   fetches it in the best quality QuickTime plays (H.264 + AAC in an .mp4, up to 1080p; "4K" asks for any codec).
   YouTube's player needs a JavaScript runtime for every quality: Mint finds node, deno or bun on this Mac (the
   yt-dlp-ejs package brings the solver, so nothing is downloaded at run time).
2. Any other page - a landing page, a course, a blog with an embedded player: Mint opens it in its own background
   Chrome (no window), starts the player muted, and watches what the page itself loads, as a browser's video-grabber
   extension does: HLS (.m3u8) and DASH (.mpd) playlists, plain video files, and players from other sites in frames
   (Vimeo, Wistia, JW Player, Brightcove...), plus the addresses written in the page's HTML and scripts. The best
   find is downloaded with the page as its referrer (and the cookies the page set), HLS and DASH through ffmpeg.
3. Videos locked with DRM (Netflix, Prime, Disney+, most paid streaming) are never downloaded: Mint says so.

"script": the address Mint found and a ready-to-run command (yt-dlp with the same headers) are saved next to the
video as a .command file, for when the user wants to see how, or download it again later.

For the user's own, personal use. Long downloads continue in the background and Mint says when they are done.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shlex
import shutil
import threading
import time
import urllib.parse
from pathlib import Path

log = logging.getLogger("mint.tools.video_download")

SYNC_WAIT = 25.0                  # seconds the tool waits before the job goes on in the background
SNIFF_SECONDS = 14.0              # how long a page may take to start its video
MAX_BYTES = 8_000_000_000
FOLDER = Path.home() / "Downloads"
MEDIA_EXT = re.compile(r"\.(m3u8|mpd|mp4|m4v|mov|webm|mkv|flv)(?:$|[?#])", re.I)
SEGMENT = re.compile(r"\.(ts|m4s|aac|m4a|vtt|webvtt|key|jpg|jpeg|png|gif|webp)(?:$|[?#])|/(init|seg|segment|chunk|"
                     r"frag)[-_]?\d|[?&](range|bytes)=\d", re.I)
ADS = re.compile(r"doubleclick|googlesyndication|googleads|imasdk|adservice|adnxs|moatads|scorecardresearch|"
                 r"pubmatic|rubiconproject|taboola|outbrain|teads|spotx|springserve|freewheel|fwmrm|innovid|"
                 r"/ads?/|[?&]ad_?type=|vast|vpaid|preroll", re.I)
PLAYER_HOSTS = re.compile(r"(youtube(-nocookie)?\.com/embed|youtu\.be|player\.vimeo\.com|vimeo\.com/\d|"
                          r"fast\.wistia\.(net|com)|wistia\.com/medias|wi\.st|loom\.com/(embed|share)|"
                          r"cdn\.jwplayer\.com|content\.jwplatform\.com|players\.brightcove\.net|"
                          r"play\.vidyard\.com|dailymotion\.com/embed|streamable\.com|player\.twitch\.tv|"
                          r"facebook\.com/plugins/video|tiktok\.com/embed|cdnapisec\.kaltura\.com|"
                          r"iframe\.mediadelivery\.net|videodelivery\.net|bunny|vidcaster|sproutvideo\.com|"
                          r"player\.mux\.com|stream\.mux\.com)", re.I)
QUALITIES = {"4k": 2160, "2160": 2160, "1440": 1440, "1080": 1080, "720": 720, "480": 480, "360": 360}

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


class DRMProtected(Exception):
    pass


# --- helpers ------------------------------------------------------------------------------------------------

def _bin(name: str) -> str | None:
    for candidate in (shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def js_runtime() -> dict:
    """yt-dlp's js_runtimes option: Mint's own Deno (the `deno` package, installed with Mint), else deno, node (22+)
    or bun on this Mac ({} if none). The app isn't started from a shell, so nvm/volta/fnm/asdf folders count too."""
    try:
        import deno
        path = deno.find_deno_bin()
        if path and os.access(path, os.X_OK):
            return {"deno": {"path": path}}
    except Exception:
        pass
    home = str(Path.home())
    for name in ("deno", "node", "bun"):
        found = [_bin(name), f"{home}/.deno/bin/{name}", f"{home}/.bun/bin/{name}", f"{home}/.volta/bin/{name}"]
        if name == "node":
            nvm = sorted(glob.glob(f"{home}/.nvm/versions/node/v*/bin/node"),
                         key=lambda p: [int(x) for x in re.findall(r"\d+", p.split("/v")[-1])[:3]], reverse=True)
            found += nvm + sorted(glob.glob(f"{home}/.local/share/fnm/node-versions/*/installation/bin/node")) + \
                sorted(glob.glob(f"{home}/.asdf/installs/nodejs/*/bin/node"))
        for path in found:
            if path and os.access(path, os.X_OK):
                if name == "node" and not _node_ok(path):
                    continue
                return {name: {"path": path}}
    return {}


def _node_ok(path: str) -> bool:
    m = re.search(r"/v(\d+)\.", path)
    if m:
        return int(m.group(1)) >= 22
    try:
        import subprocess
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5).stdout
        return int(re.findall(r"\d+", out)[0]) >= 22
    except Exception:
        return False


def _ffmpeg() -> str | None:
    """Homebrew's ffmpeg if there is one, else the static one that comes with Mint (imageio-ffmpeg)."""
    found = _bin("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        return path if path and os.access(path, os.X_OK) else None
    except Exception:
        return None


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")


def _safe_name(text: str, fallback: str = "video") -> str:
    text = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", str(text or "")).strip(" .")
    text = re.sub(r"\s+", " ", text)
    return (text[:120].rstrip() or fallback)


def _free(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 200):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem} {int(time.time())}{path.suffix}")


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _clock(seconds) -> str:
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _format(quality: str, has_ffmpeg: bool) -> str:
    """The yt-dlp format: what QuickTime and the Photos app play (H.264 + AAC) first; "4k" takes any codec."""
    q = str(quality or "best").lower().replace("p", "").strip()
    if q in ("audio", "sound", "mp3", "m4a", "music"):
        return "ba[ext=m4a]/ba/b"
    if not has_ffmpeg:                          # no merging: one file with both picture and sound
        h = QUALITIES.get(q)
        return f"b[ext=mp4][height<={h}]/b[height<={h}]/b" if h else "b[ext=mp4]/b"
    h = QUALITIES.get(q)
    if q in ("4k", "2160", "1440") or q in ("max", "highest"):
        cap = f"[height<={h}]" if h else ""
        return f"bv*{cap}+ba[ext=m4a]/bv*{cap}+ba/b{cap}/b"
    cap = f"[height<={h or 1080}]"
    return (f"bv*{cap}[vcodec^=avc1]+ba[acodec^=mp4a]/bv*{cap}[ext=mp4]+ba[ext=m4a]/bv*{cap}+ba/"
            f"b{cap}[ext=mp4]/b{cap}/bv*+ba/b")


# --- yt-dlp --------------------------------------------------------------------------------------------------

def _ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except ImportError:
        return None


def _cookie_file(cookies: list[dict]) -> str:
    """Cookies the page set (from Chrome), as a Netscape cookies.txt only Mint can read; deleted after the job.
    (A Cookie header would be dropped by yt-dlp.)"""
    import tempfile
    fd, path = tempfile.mkstemp(prefix="mint-cookies-", suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            domain = str(c.get("domain") or "")
            if not domain or not c.get("name"):
                continue
            expires = int(c.get("expires") or 0)
            fh.write("\t".join([domain, "TRUE" if domain.startswith(".") else "FALSE", str(c.get("path") or "/"),
                                "TRUE" if c.get("secure") else "FALSE", str(max(expires, 0)), str(c["name"]),
                                str(c.get("value") or "")]) + "\n")
    os.chmod(path, 0o600)
    return path


def _options(folder: Path, quality: str, state: dict, headers: dict | None = None, name: str = "",
             cookiefile: str = "") -> dict:
    ffmpeg = _ffmpeg()
    template = (_safe_name(name) + ".%(ext)s") if name else "%(title).110B.%(ext)s"

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                state["percent"] = round(100 * done / total)
            elif d.get("fragment_count"):
                state["percent"] = round(100 * (d.get("fragment_index") or 0) / d["fragment_count"])
            speed = d.get("speed")
            state["step"] = (f"Downloading {state.get('percent', 0)}%" + (f" ({_size(speed)}/s)" if speed else ""))
        elif d.get("status") == "finished":
            state["step"] = "Putting it together"
        if state.get("cancel"):
            raise KeyboardInterrupt("stopped")

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True, "socket_timeout": 25,
            "retries": 5, "fragment_retries": 10, "concurrent_fragment_downloads": 4, "continuedl": True,
            "format": _format(quality, bool(ffmpeg)), "merge_output_format": "mp4", "max_filesize": MAX_BYTES,
            "outtmpl": str(folder / template), "windowsfilenames": True, "trim_file_name": 150,
            "progress_hooks": [hook], "overwrites": False, "logger": _Quiet()}
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg          # (HLS stays with yt-dlp's own downloader: AES keys, retries)
    runtime = js_runtime()
    if runtime:
        opts["js_runtimes"] = runtime
    if headers:
        opts["http_headers"] = {k: v for k, v in headers.items() if v and k.lower() != "cookie"}
        # Signed CDN links: the playlist's ?token=... goes on its variants, segments and keys too.
        opts["extractor_args"] = {"generic": {"fragment_query": [""], "variant_query": [""], "key_query": [""]}}
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


class _Quiet:
    """yt-dlp's messages go to Mint's log, not the terminal (its errors still come back as exceptions)."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.debug("yt-dlp: %s", msg)

    def error(self, msg):
        log.info("yt-dlp: %s", str(msg)[:300])


def _ytdlp_fetch(url: str, folder: Path, quality: str, state: dict, headers: dict | None = None,
                 name: str = "", cookiefile: str = "") -> dict:
    """Download with yt-dlp -> {"path", "title", "height", "duration", "via"}. Raises with yt-dlp's reason."""
    yt_dlp = _ytdlp()
    if yt_dlp is None:
        raise RuntimeError("yt-dlp isn't installed in Mint's Python")
    folder.mkdir(parents=True, exist_ok=True)
    opts = _options(folder, quality, state, headers, name, cookiefile)
    state["step"] = "Looking at the video"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") in ("playlist", "multi_video") and info.get("entries"):
            info = next(e for e in info["entries"] if e)
        if _drm(info):
            raise DRMProtected("this video is protected (DRM)")
        if info.get("is_live") and not info.get("was_live"):
            raise RuntimeError("it's a live stream that's still on air - try again when it ends")
        state["title"] = name or state.get("title") or info.get("title") or ""
        state["step"] = "Downloading"
        info = _download_skipping_drm(ydl, info)
    paths = [d.get("filepath") for d in info.get("requested_downloads") or [] if d.get("filepath")]
    path = paths[0] if paths else info.get("filepath") or info.get("_filename")
    if not path or not os.path.exists(path):
        found = sorted(folder.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = str(found[0]) if found else ""
    if not path:
        raise RuntimeError("yt-dlp finished but no file was written")
    got = {"path": path, "title": info.get("title") or "", "height": info.get("height"),
           "duration": info.get("duration"), "via": info.get("extractor_key") or "yt-dlp",
           "url": info.get("url") or url, "vcodec": str(info.get("vcodec") or "")}
    if str(quality).lower() not in ("4k", "2160", "1440", "max", "highest", "audio"):
        got["path"] = _quicktime(got["path"], got["vcodec"], info.get("duration"), state)
    return got


QUICKTIME = ("avc1", "h264", "hvc1", "hev1", "hevc", "h265")


def _quicktime(path: str, vcodec: str, duration, state: dict) -> str:
    """A .webm / VP9 / AV1 download becomes an H.264 .mp4 that QuickTime and Photos open (up to 20 minutes;
    longer stays as it is, with a note)."""
    ffmpeg = _ffmpeg()
    p = Path(path)
    if not ffmpeg or not p.exists():
        return path
    codec = vcodec.lower()
    if p.suffix.lower() == ".mp4" and (not codec or codec.startswith(QUICKTIME) or codec == "none"):
        return path
    if p.suffix.lower() in (".m4a", ".mp3") or (duration and float(duration) > 20 * 60):
        return path
    out = _free(p.with_suffix(".mp4"))
    state["step"] = "Making it QuickTime-ready"
    import subprocess
    try:
        subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(p), "-map", "0:v:0", "-map", "0:a:0?", "-c:v",
                        "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-b:a", "160k", "-movflags", "+faststart", str(out)], check=True, capture_output=True,
                       timeout=1800)
    except Exception as error:
        log.info("quicktime copy of %s failed: %s", p.name, str(error)[:200])
        out.unlink(missing_ok=True)
        return path
    p.unlink(missing_ok=True)
    return str(out)


def _download_skipping_drm(ydl, info: dict) -> dict:
    """Some sites (Vimeo) list a protected copy next to plain ones, and it only shows when the download starts:
    drop the copy that turned out protected and take the next best, up to 4 times."""
    import copy as _copy
    dropped: set = set()
    for _ in range(5):
        trial = _copy.deepcopy(info)
        if dropped and trial.get("formats"):
            trial["formats"] = [f for f in trial["formats"] if f.get("format_id") not in dropped]
            if not trial["formats"]:
                raise DRMProtected("every copy of this video is protected (DRM)")
        try:
            return ydl.process_ie_result(trial, download=True)
        except Exception as error:
            if "drm protected" not in str(error).lower() or not trial.get("formats"):
                raise
            chosen = ydl.process_ie_result(_copy.deepcopy(trial), download=False)
            ids = {f.get("format_id") for f in chosen.get("requested_formats") or [chosen]} - {None}
            if not ids or ids <= dropped:
                raise DRMProtected("this video is protected (DRM)")
            log.info("protected copy %s: trying the next one", ",".join(sorted(ids)))
            dropped |= ids
    raise DRMProtected("this video is protected (DRM)")


def _drm(info: dict) -> bool:
    """Only DRM left: yt-dlp also sets _has_drm when it dropped some protected formats but kept playable ones."""
    formats = info.get("formats")
    if formats is None:
        return bool(info.get("has_drm") or info.get("_has_drm"))
    return not formats or all(f.get("has_drm") for f in formats)


def _why(error: Exception) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(error))
    text = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?([\w-]+:\s*)?", "", text.strip())
    low = text.lower()
    if "drm" in low:
        return "it's protected with DRM, so it can't be downloaded"
    if any(w in low for w in ("sign in", "login", "log in", "logged-in", "logged in", "private", "members-only",
                              "cookies")):
        return "the site wants you signed in to see it"
    if "not available in your country" in low or "geo" in low:
        return "the site doesn't offer it in your country"
    if "unsupported url" in low:
        return "yt-dlp doesn't know this site"
    return text[:220]


def _no_site_support(error: Exception) -> bool:
    low = str(error).lower()
    return any(w in low for w in ("unsupported url", "no video formats", "unable to extract", "no media found",
                                  "generic", "is not a valid url", "http error 404", "unable to download webpage",
                                  "requested format is not available", "403: forbidden"))


# --- finding the video in a page (Mint's own background Chrome) -------------------------------------------

HOOK = r"""
(() => {
  if (window.__mintVideo) return;
  const found = window.__mintVideo = {drm: '', urls: [], mse: 0};
  const add = (u) => { try { if (u && typeof u === 'string' && !u.startsWith('blob:') && !u.startsWith('data:'))
    found.urls.push(new URL(u, location.href).href); } catch (e) {} };
  try {
    const keys = navigator.requestMediaKeySystemAccess && navigator.requestMediaKeySystemAccess.bind(navigator);
    if (keys) navigator.requestMediaKeySystemAccess = (system, config) => { found.drm = String(system); return keys(system, config); };
  } catch (e) {}
  try {
    const create = URL.createObjectURL;
    URL.createObjectURL = function (obj) { if (window.MediaSource && obj instanceof MediaSource) found.mse++; return create.apply(this, arguments); };
  } catch (e) {}
  try {
    const open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (m, u) { if (/\.(m3u8|mpd)(\?|$)/i.test(String(u))) add(String(u)); return open.apply(this, arguments); };
    const f = window.fetch;
    if (f) window.fetch = function (input) { const u = typeof input === 'string' ? input : (input && input.url); if (/\.(m3u8|mpd)(\?|$)/i.test(String(u))) add(String(u)); return f.apply(this, arguments); };
  } catch (e) {}
})();
"""

PLAY = r"""
(async () => {
  const out = [];
  const docs = [document];
  for (const f of document.querySelectorAll('iframe')) { try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {} }
  for (const d of docs) {
    for (const v of d.querySelectorAll('video')) {
      try { v.muted = true; v.playsInline = true; const p = v.play(); if (p && p.catch) p.catch(() => {}); out.push('video'); } catch (e) {}
    }
    const sel = ['[aria-label*="play" i]', '[title*="play" i]', '.vjs-big-play-button', '.jw-display-icon-container',
      '.ytp-large-play-button', '.plyr__control--overlaid', '.w-big-play-button', '.vp-big-play-button',
      '[class*="play-button" i]', '[class*="playButton" i]', '[class*="PlayButton" i]', 'button[class*="play" i]',
      '[data-testid*="play" i]', '.video-play', '.play-icon', '.play'];
    for (const s of sel) {
      let n = 0;
      for (const el of d.querySelectorAll(s)) {
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8 || n > 2) continue;
        try { el.click(); out.push(s); n++; } catch (e) {}
      }
    }
  }
  return out.length;
})()
"""

SCAN = r"""
(() => {
  const urls = [], frames = [], vids = [];
  const add = (list, u) => { try { if (u && !String(u).startsWith('blob:') && !String(u).startsWith('data:')) list.push(new URL(u, location.href).href); } catch (e) {} };
  const docs = [document];
  for (const f of document.querySelectorAll('iframe')) { add(frames, f.src || f.dataset.src); try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {} }
  for (const d of docs) {
    for (const v of d.querySelectorAll('video')) {
      add(urls, v.currentSrc || v.src); for (const s of v.querySelectorAll('source')) add(urls, s.src);
      vids.push({w: v.videoWidth, h: v.videoHeight, d: isFinite(v.duration) ? v.duration : 0, src: v.currentSrc || v.src || '', blob: String(v.currentSrc || v.src || '').startsWith('blob:'), playing: !v.paused});
      for (const k of ['src', 'videoSrc', 'videoUrl', 'hls', 'hlsSrc', 'mp4', 'url']) if (v.dataset && v.dataset[k]) add(urls, v.dataset[k]);
    }
  }
  for (const m of document.querySelectorAll('meta[property="og:video"], meta[property="og:video:url"], meta[property="og:video:secure_url"], meta[name="twitter:player:stream"], meta[itemprop="contentUrl"], link[itemprop="contentUrl"]'))
    add(urls, m.content || m.href);
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { const walk = (o) => { if (!o || typeof o !== 'object') return; if (Array.isArray(o)) return o.forEach(walk);
      if (o.contentUrl) add(urls, o.contentUrl); if (o.embedUrl) add(frames, o.embedUrl); Object.values(o).forEach(walk); };
      walk(JSON.parse(s.textContent)); } catch (e) {}
  }
  const html = document.documentElement.outerHTML.replace(/\\u002[Ff]/g, '/').replace(/\\\//g, '/');
  const re = /https?:\/\/[^"'\s<>()\\]+?\.(?:m3u8|mpd|mp4|m4v|webm|mov)(?:\?[^"'\s<>()\\]*)?/gi;
  let m; let n = 0; while ((m = re.exec(html)) && n++ < 200) add(urls, m[0].replace(/&amp;/g, '&'));
  const hooked = window.__mintVideo || {drm: '', urls: [], mse: 0};
  const title = (document.querySelector('meta[property="og:title"]') || {}).content || document.title || '';
  return {urls: urls.concat(hooked.urls), frames, vids, drm: hooked.drm, mse: hooked.mse, title, ua: navigator.userAgent};
})()
"""


class Sniffer:
    """A page open in Mint's own background Chrome, with its network watched (iframes too)."""

    def __init__(self, browser) -> None:
        self.b = browser
        self.sessions: set[str] = set()
        self.requests: dict[str, dict] = {}
        self.media: dict[str, dict] = {}
        self.done = False
        self.tab = None

    def open(self, url: str) -> None:
        self.tab = self.b.new_tab("about:blank")
        self.sessions.add(self.tab.session)
        t = self.b.t
        t.on("Network.requestWillBeSent", self._request)
        t.on("Network.responseReceived", self._response)
        t.on("Target.attachedToTarget", self._attached)
        self.tab.call("Network.enable", {"maxTotalBufferSize": 100_000_000, "maxResourceBufferSize": 20_000_000})
        try:
            self.tab.call("Network.setBypassServiceWorker", {"bypass": True})     # requests stay visible
        except Exception:
            pass
        self.tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK, "runImmediately": True})
        try:
            self.tab.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False,
                                                   "flatten": True})
        except Exception:
            log.debug("no auto-attach", exc_info=True)
        self.tab.go(url, wait=25)

    def _attached(self, params: dict, session) -> None:
        if self.done or session not in self.sessions:
            return
        child = params.get("sessionId")
        if child and (params.get("targetInfo") or {}).get("type") in ("iframe", "page", "worker", "service_worker"):
            self.sessions.add(child)
            self.b.t.post("Network.enable", {}, child)
            self.b.t.post("Runtime.runIfWaitingForDebugger", {}, child)

    def _request(self, params: dict, session) -> None:
        if self.done or session not in self.sessions:
            return
        req = params.get("request") or {}
        self.requests[params.get("requestId", "")] = {"url": req.get("url", ""), "headers": req.get("headers") or {}}

    def _response(self, params: dict, session) -> None:
        if self.done or session not in self.sessions:
            return
        resp = params.get("response") or {}
        url = resp.get("url") or ""
        mime = str(resp.get("mimeType") or "").lower()
        kind = _kind(url, mime)
        if not kind:
            return
        headers = {k.lower(): v for k, v in (resp.get("headers") or {}).items()}
        length = 0
        try:
            length = int(headers.get("content-length") or 0)
            rng = headers.get("content-range", "")
            if "/" in rng:
                length = int(rng.rsplit("/", 1)[1])
        except ValueError:
            pass
        req = self.requests.get(params.get("requestId", ""), {})
        key = url.split("#")[0]
        old = self.media.get(key)
        if old is None or length > old.get("length", 0):
            self.media[key] = {"url": url, "kind": kind, "mime": mime, "length": length, "status": resp.get("status"),
                               "headers": req.get("headers") or {}, "at": time.time(),
                               "request": params.get("requestId"), "session": session}

    def play_and_wait(self, seconds: float) -> dict:
        tab = self.tab
        scan = {}
        end = time.time() + seconds
        tried = 0
        last_count, quiet_since = -1, time.time()
        while time.time() < end:
            if tried < 3:
                try:
                    tab.js("window.scrollBy(0, 400)")
                    tab.js(PLAY, timeout=8, await_promise=True)
                except Exception:
                    log.debug("play", exc_info=True)
                tried += 1
            time.sleep(1.2)
            count = len(self.media)
            if count != last_count:
                last_count, quiet_since = count, time.time()
            strong = any(m["kind"] in ("hls", "dash") or m["length"] > 2_000_000 for m in self.media.values())
            if strong and time.time() - quiet_since > 2.5:
                break
        try:
            scan = tab.js(SCAN, timeout=10) or {}
        except Exception:
            log.debug("scan", exc_info=True)
        return scan

    def cookies(self, urls: list[str]) -> list[dict]:
        try:
            return self.tab.call("Network.getCookies", {"urls": urls[:20]}).get("cookies", [])
        except Exception:
            return []

    def body(self, m: dict, limit: int = 2_000_000) -> str:
        """A playlist's text: what Chrome already received for the page, else fetched again like the page did."""
        if m.get("request") and m.get("session"):
            try:
                got = self.b.call("Network.getResponseBody", {"requestId": m["request"]}, m["session"], timeout=8)
                text = got.get("body") or ""
                if got.get("base64Encoded"):
                    import base64
                    text = base64.b64decode(text).decode("utf-8", "replace")
                if text:
                    return text[:limit]
            except Exception:
                log.debug("response body", exc_info=True)
        for mode in ("same-origin", "include", "omit"):
            try:
                text = self.tab.js(f"fetch({json.dumps(m['url'])}, {{credentials: '{mode}'}}).then(r => r.ok ? "
                                   f"r.text() : '').then(t => t.slice(0, {limit})).catch(e => '')", timeout=15,
                                   await_promise=True)
                if text:
                    return str(text)
            except Exception:
                pass
        return ""

    def close(self) -> None:
        self.done = True
        if self.tab is not None:
            self.tab.close()


def _kind(url: str, mime: str = "") -> str:
    low = url.lower()
    if not low.startswith("http") or ADS.search(low):
        return ""
    if "mpegurl" in mime or re.search(r"\.m3u8(\?|$)", low):
        return "hls"
    if "dash+xml" in mime or re.search(r"\.mpd(\?|$)", low):
        return "dash"
    if SEGMENT.search(low) and not re.search(r"\.(mp4|webm|mov|m4v)(\?|$)", low.split("?")[0]):
        return ""
    if mime.startswith("video/") or re.search(r"\.(mp4|m4v|mov|webm|mkv)(\?|$)", low):
        if re.search(r"/(seg|segment|chunk|frag|range)[-_/]?\d|init\.mp4|\.m4s", low):
            return ""
        return "file"
    return ""


def _hls_info(text: str) -> dict:
    """Master or media playlist, the best resolution in it, and whether it is DRM'd."""
    info = {"master": "#EXT-X-STREAM-INF" in text, "height": 0, "bandwidth": 0, "drm": False, "seconds": 0.0}
    if re.search(r"METHOD=SAMPLE-AES|KEYFORMAT=\"com\.(apple\.streamingkeydelivery|widevine|microsoft)|"
                 r"urn:uuid:edef8ba9", text, re.I):
        info["drm"] = True
    for m in re.finditer(r"RESOLUTION=\d+x(\d+)", text):
        info["height"] = max(info["height"], int(m.group(1)))
    for m in re.finditer(r"BANDWIDTH=(\d+)", text):
        info["bandwidth"] = max(info["bandwidth"], int(m.group(1)))
    info["seconds"] = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text))
    return info


def _dash_info(text: str) -> dict:
    info = {"height": 0, "drm": False}
    if re.search(r"ContentProtection[^>]*(edef8ba9|widevine|playready|9a04f079|cenc:default_KID)", text, re.I):
        info["drm"] = True
    for m in re.finditer(r'height="(\d+)"', text):
        info["height"] = max(info["height"], int(m.group(1)))
    return info


def rank(media: list[dict]) -> list[dict]:
    """Best first: a master HLS playlist, then DASH, then the biggest plain file, then single HLS renditions."""
    def score(m):
        base = {"hls": 3 if m.get("master") else 1, "dash": 2, "file": 1.5}.get(m["kind"], 0)
        if m["kind"] == "file":
            if m.get("length", 0) and m["length"] < 300_000:
                return (-1, 0)
            base = 2.5 if m.get("length", 0) > 5_000_000 else 1.5
        return (base, m.get("height") or 0, m.get("bandwidth") or 0, m.get("length") or 0, m.get("seconds") or 0)
    return sorted(media, key=score, reverse=True)


def sniff(url: str, seconds: float = SNIFF_SECONDS, browser=None) -> dict:
    """Open the page in Mint's own background Chrome and find what it plays.
    -> {"candidates": [..best first], "frames": [player pages], "title", "ua", "drm": "", "mse": n, "page"}"""
    from mint.tools import cdp
    browser = browser or cdp.get("headless")
    s = Sniffer(browser)
    try:
        s.open(url)
        scan = s.play_and_wait(seconds)
        for u in scan.get("urls") or []:
            kind = _kind(u)
            if kind and u.split("#")[0] not in s.media:
                s.media[u.split("#")[0]] = {"url": u, "kind": kind, "mime": "", "length": 0, "headers": {},
                                            "from_page": True}
        drm = scan.get("drm") or ""
        found = []
        for m in s.media.values():
            if m["kind"] == "hls":
                text = s.body(m)
                if text and "#EXTM3U" not in text[:200]:
                    continue
                info = _hls_info(text)
                if info["drm"]:
                    drm = drm or "HLS with DRM"
                    continue
                m.update(info)
            elif m["kind"] == "dash":
                text = s.body(m)
                info = _dash_info(text)
                if info["drm"]:
                    drm = drm or "DASH with DRM"
                    continue
                m.update(info)
            found.append(m)
        candidates = rank(found)
        frames = []
        for f in (scan.get("frames") or []):
            if PLAYER_HOSTS.search(f) and f not in frames:
                frames.append(f)
        cookies = s.cookies([url] + [c["url"] for c in candidates[:3]])
        return {"candidates": candidates, "frames": frames, "title": scan.get("title") or "", "ua": scan.get("ua")
                or "", "drm": drm, "mse": scan.get("mse") or 0,
                "videos": scan.get("vids") or [], "page": url, "cookies": cookies}
    finally:
        s.close()


# --- the whole job ------------------------------------------------------------------------------------------

def _user_chrome():
    """The user's own Chrome (their logins), only when they have already let Mint use it (Chrome's remote
    debugging box ticked); None otherwise. Chrome may ask "Allow?" once if Mint isn't connected yet."""
    try:
        from mint.tools import cdp
        if not cdp.connected() and cdp.consent_state() != "ready":
            return None
        return cdp.get("chrome")
    except Exception as error:
        log.info("user's Chrome: %s", str(error)[:160])
        return None


def _user_cookies(url: str) -> list[dict]:
    """The user's Chrome cookies for this site (the profile signed in there), for a video that needs them."""
    browser = _user_chrome()
    if browser is None:
        return []
    host = _host(url)
    try:
        context = browser.profile_for(url)
        got = browser.call("Storage.getCookies", {"browserContextId": context} if context else {}).get("cookies", [])
    except Exception as error:
        log.info("cookies from the user's Chrome: %s", str(error)[:160])
        return []
    base = ".".join(host.split(".")[-2:])
    return [c for c in got if str(c.get("domain") or "").lstrip(".").endswith(base)]


SIGNED_IN = ("Open the video's page in Chrome while you're signed in and let Mint use Chrome (tick “Allow remote "
             "debugging” at chrome://inspect/#remote-debugging once), then ask again.")

def _headers_for(page: str, ua: str, extra: dict | None = None) -> dict:
    parts = urllib.parse.urlsplit(page)
    headers = {"Referer": page, "Origin": f"{parts.scheme}://{parts.netloc}" if parts.netloc else "",
               "User-Agent": ua or ""}
    for k, v in (extra or {}).items():
        if k.lower() in ("referer", "origin", "authorization") and v:
            headers[k.title()] = v
    return {k: v for k, v in headers.items() if v}


def download(url: str, quality: str = "best", folder: Path | None = None, state: dict | None = None,
             script: bool = False) -> dict:
    """-> {"path", "title", "via", "media", "headers", "height", "duration", "note"}. Raises with the reason."""
    state = state if state is not None else {}
    folder = folder or FOLDER
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    first_error = None
    direct = bool(MEDIA_EXT.search(url.split("?")[0]))
    if not direct:
        try:
            got = _ytdlp_fetch(url, folder, quality, state)
            got.update(media=url, headers={}, note="")
            return got
        except DRMProtected:
            raise
        except KeyboardInterrupt:
            raise RuntimeError("stopped")
        except Exception as error:
            if "drm protected" in str(error).lower():
                raise DRMProtected("this video is protected (DRM)")
            first_error = error
            log.info("yt-dlp couldn't take %s: %s", url, str(error)[:200])
            vimeo = re.match(r"https?://(?:www\.)?vimeo\.com/(\d+)", url)
            if vimeo:
                try:
                    got = _ytdlp_fetch(f"https://player.vimeo.com/video/{vimeo.group(1)}", folder, quality, state,
                                       headers={"Referer": "https://vimeo.com/"})
                    got.update(media=url, headers={}, note="")
                    return got
                except DRMProtected:
                    raise
                except Exception as again:
                    log.info("vimeo player: %s", str(again)[:160])
            if "signed in" in _why(error):        # Instagram, a private post, a members' video: the user's logins
                cookies = _user_cookies(url)
                if cookies:
                    state["step"] = "Using your sign-in"
                    cookiefile = _cookie_file(cookies)
                    try:
                        got = _ytdlp_fetch(url, folder, quality, state, cookiefile=cookiefile)
                        got.update(media=url, headers={}, note="with your Chrome sign-in")
                        return got
                    except DRMProtected:
                        raise
                    except Exception as again:
                        raise RuntimeError(_why(again))
                    finally:
                        os.unlink(cookiefile)
                raise RuntimeError(f"{_why(error)}. {SIGNED_IN}")
            if not _no_site_support(error):
                raise RuntimeError(_why(error))     # a site it knows, failing for a real reason (private, removed)
    else:
        try:                                    # a stream or file address itself: yt-dlp, never a page to open
            got = _ytdlp_fetch(url, folder, quality, state, headers={"Referer": url})
            got.update(media=url, headers={"Referer": url}, note="")
            return got
        except DRMProtected:
            raise
        except Exception as error:
            if "drm" in str(error).lower():
                raise DRMProtected("this stream is protected (DRM)")
            raise RuntimeError(_why(error))
    # Not a site yt-dlp knows: find the video in the page.
    state["step"] = "Finding the video"
    try:
        found = sniff(url)
    except Exception as error:
        raise RuntimeError(f"couldn't open the page to look for the video ({str(error)[:160]})")
    if not found["candidates"] and not found["frames"] and not found.get("drm"):
        mine = _user_chrome()                   # a page that shows its video only to a signed-in visitor
        if mine is not None:
            state["step"] = "Trying your Chrome"
            try:
                found = sniff(url, browser=mine)
            except Exception as error:
                log.info("sniff in the user's Chrome: %s", str(error)[:160])
    state["title"] = found.get("title") or state.get("title") or ""
    ua = found.get("ua") or ""
    for frame in found["frames"]:                       # a player from another site (Vimeo, Wistia...)
        state["step"] = f"Found a {_host(frame).split('.')[-2]} player"
        try:
            got = _ytdlp_fetch(frame, folder, quality, state, headers=_headers_for(url, ua))
            got.update(media=frame, headers={"Referer": url}, note=f"from the {_host(frame)} player in the page")
            return got
        except DRMProtected:
            raise
        except Exception as error:
            log.info("player %s: %s", frame, str(error)[:160])
    errors = []
    cookiefile = _cookie_file(found.get("cookies") or []) if found.get("cookies") else ""
    try:
        return _try_candidates(found, url, ua, folder, quality, state, script, cookiefile, errors)
    except _NoneWorked:
        pass
    finally:
        if cookiefile:
            try:
                os.unlink(cookiefile)
            except OSError:
                pass
    if found.get("drm"):
        raise DRMProtected(f"the page plays it with DRM ({found['drm']})")
    if any(v.get("blob") for v in found.get("videos") or []) and not found["candidates"]:
        raise RuntimeError("the page builds the video in the browser from pieces it doesn't name (no playlist or "
                           "file to fetch) - it may need you signed in, or it's protected")
    if errors:
        raise RuntimeError("found the video but the site refused the download: " + errors[0])
    if first_error is not None and not _no_site_support(first_error):
        raise RuntimeError(_why(first_error))
    raise RuntimeError("no video found on that page (it may need you signed in, or the video is behind a click "
                       "Mint can't make)")


class _NoneWorked(Exception):
    pass


def _try_candidates(found: dict, url: str, ua: str, folder: Path, quality: str, state: dict, script: bool,
                    cookiefile: str, errors: list) -> dict:
    for m in found["candidates"][:4]:
        headers = _headers_for(url, ua, m.get("headers"))
        what = {"hls": "an HLS stream", "dash": "a DASH stream", "file": "a video file"}[m["kind"]]
        state["step"] = f"Found {what}"
        try:
            got = _ytdlp_fetch(m["url"], folder, quality, state, headers=headers, name=found.get("title") or "",
                               cookiefile=cookiefile)
            got.update(media=m["url"], headers=headers, note=f"found in the page ({what})")
            if found.get("title"):
                got["title"] = found["title"]
            if script:
                got["script"] = str(write_script(Path(got["path"]), m["url"], headers, quality))
            return got
        except DRMProtected:
            raise
        except Exception as error:
            errors.append(_why(error))
            log.info("candidate %s failed: %s", m["url"][:120], str(error)[:200])
    raise _NoneWorked()


def write_script(video: Path, media: str, headers: dict, quality: str = "best") -> Path:
    """A double-clickable .command that downloads the same video again with the same headers."""
    lines = ["#!/bin/sh", f"# Downloads {video.name} again: the address Mint found in the page, with the headers "
             "the page used.", 'cd "$(dirname "$0")"']
    import sys
    python = os.path.join(sys.prefix, "bin", "python3")
    runner = [python if os.path.exists(python) else sys.executable, "-m", "yt_dlp"]    # Mint's own yt-dlp
    args = runner + ["--no-playlist", "-f", _format(quality, True), "--merge-output-format", "mp4"]
    for k, v in headers.items():
        if k.lower() == "cookie":
            continue                            # (cookies expire and are private: not written to a file)
        args += ["--add-header", f"{k}:{v}"]
    args += ["-o", video.stem + ".%(ext)s", media]
    lines.append(" ".join(shlex.quote(a) for a in args))
    path = _free(video.with_suffix(".command"))
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o755)
    return path


# --- the tool ------------------------------------------------------------------------------------------------

def _front_url() -> str:
    try:
        from mint.tools import harness as harness_tools
        app = harness_tools._pick_browser()
        if app is None:
            return ""
        url, _title = harness_tools._tab_info(app)
        return url if url.startswith("http") else ""
    except Exception as error:
        log.debug("front tab: %s", error)
        return ""


def _report(got: dict) -> str:
    path = Path(got["path"])
    size = path.stat().st_size if path.exists() else 0
    bits = [b for b in (f"{got['height']}p" if got.get("height") else "", _clock(got.get("duration")) if
                        got.get("duration") else "", _size(size) if size else "") if b]
    where = str(path).replace(str(Path.home()), "~")
    text = f"Downloaded “{got.get('title') or path.stem}”" + (f" ({', '.join(bits)})" if bits else "") + f": {where}"
    if got.get("note"):
        text += f" - {got['note']}"
    if got.get("script"):
        text += f". The download command is saved next to it: {str(got['script']).replace(str(Path.home()), '~')}"
    if path.suffix.lower() not in (".mp4", ".m4a", ".mov") and path.exists():
        text += f". (It's a .{path.suffix.lstrip('.')} file: QuickTime may not open it; VLC or IINA will.)"
    if not _ffmpeg():
        text += (" (ffmpeg isn't installed, so the best single file was taken; with ffmpeg - `brew install "
                 "ffmpeg` - Mint gets higher qualities.)")
    return text


def download_video(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url or url.lower() in ("this", "this video", "front", "current"):
        url = _front_url()
        if not url:
            return "Which video? Give me its link, or open its page in the browser and ask again."
    quality = str(args.get("quality") or "best")
    script = bool(args.get("script"))
    folder = FOLDER
    if args.get("save_to"):
        try:
            from mint.tools import saveto
            target, _note = saveto.destination(FOLDER / "video.mp4", ".mp4", str(args["save_to"]))
            folder = target.parent
        except Exception:
            log.debug("save_to", exc_info=True)
    state: dict = {"step": "Starting", "started": time.time(), "url": url}
    key = f"dl-{time.time():.3f}"

    def run():
        try:
            got = download(url, quality, folder, state, script)
            if script and not got.get("script"):
                got["script"] = str(write_script(Path(got["path"]), got.get("media") or url, got.get("headers") or {},
                                                 quality))
            state["result"] = _report(got)
            state["path"] = got["path"]
            try:
                from mint.tools.harness import _remember_made
                _remember_made(Path(got["path"]))
            except Exception:
                pass
        except DRMProtected as error:
            state["result"] = (f"FAILED: not downloaded - {error}. Mint doesn't get around copy protection (Netflix, "
                               "Prime Video, paid streams).")
        except Exception as error:
            log.info("download_video %s: %s", url, error)
            state["result"] = f"FAILED: couldn't download it - {error}"
        with _lock:
            _jobs.pop(key, None)
            late = state.get("late")
        if late:
            _notify("(Mint's video downloader, not the user.) The download finished:\n" + state["result"]
                    + "\nTell the user in a sentence.")

    thread = threading.Thread(target=run, daemon=True, name="video-download")
    with _lock:
        _jobs[key] = state
    thread.start()
    thread.join(SYNC_WAIT)
    with _lock:
        if thread.is_alive():
            state["late"] = True
            step = state.get("step", "")
            title = state.get("title", "")
        else:
            step = ""
    if step:
        return (f"Downloading{(' “' + title + '”') if title else ''} - {step.lower()}. It continues in the "
                "background; a message comes when it's saved. Tell the user in a few words, then carry on.")
    return state["result"]


def _notify(text: str) -> None:
    try:
        from mint.tools.work import _notify_mint
        _notify_mint(text)
    except Exception as error:
        log.info("notify: %s", error)


def snapshot() -> dict:
    """For the island: the download running now - {"step", "percent", "seconds", "summary"} - else {}."""
    with _lock:
        states = list(_jobs.values())
    if not states:
        return {}
    s = states[0]
    return {"step": s.get("step", ""), "percent": s.get("percent", 0), "summary": s.get("title", ""),
            "seconds": time.time() - s.get("started", time.time())}


PROMPT = """Downloading videos: download_video. "download this video", "save this YouTube video", "download the \
video on this page", "get me the mp4 of <link>", "download it in 720p", "just the audio" -> download_video with url \
(the link; empty = the page open in the browser), quality (best / 4k / 1080 / 720 / 480 / audio) and save_to only if \
the user named a place (default: Downloads). It works for YouTube and ~1,800 sites, and for other pages it finds the \
video the page plays (embedded players, HLS/DASH streams). "show me how / give me the script / the command" -> \
script=true (saves a .command next to the video). It never downloads DRM-protected streams (Netflix, Prime...) - \
say so plainly. For watching/summarising a video use watch_video instead."""


def declarations():
    from google.genai import types
    return [types.FunctionDeclaration(
        name="download_video",
        description=("Download a video to the Mac: YouTube and ~1,800 sites by link, or the video embedded in any "
                     "web page (finds its stream or file like a browser video-grabber). Long downloads continue in "
                     "the background."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "url": types.Schema(type=types.Type.STRING,
                                description="The video's or page's link; empty = the page open in the browser"),
            "quality": types.Schema(type=types.Type.STRING,
                                    description="best (default), 4k, 1080, 720, 480 or audio"),
            "save_to": types.Schema(type=types.Type.STRING,
                                    description="Only if the user named a place; default Downloads"),
            "script": types.Schema(type=types.Type.BOOLEAN,
                                   description="Also save the found address and a download command next to it")})
    )]


HANDLERS = {"download_video": download_video}
