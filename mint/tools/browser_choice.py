"""Which browser a web address opens in, and opening it there.

The user keeps some browsers for some things ("entertainment always in Brave,
work in Chrome"). Before this, open_url sent everything to Chrome whenever a
Chrome window was open, so a skill that opened Brave and then the site ended up
with the site in Chrome and both browsers in front of the user.

Order of choice, first that applies:
  1. a browser the user named in this request ("on this Chrome only", "in Brave")
  2. a rule: browser_rules.json, and remembered preferences that name a browser
     ("Brave is the default for entertainment ... YouTube, anime")
  3. the browser the model asked for
  4. the browser in front, then the one Mint last worked in, then the Mac's default
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from mint.core import config

log = logging.getLogger("mint.browsers")

RULES = config.PROJECT_ROOT / "browser_rules.json"

# Bundle id -> (AppleScript name, family). Chromium browsers share one dictionary.
BROWSERS = {"com.google.Chrome": ("Google Chrome", "chrome"), "com.apple.Safari": ("Safari", "safari"),
            "company.thebrowser.Browser": ("Arc", "chrome"), "com.brave.Browser": ("Brave Browser", "chrome"),
            "com.microsoft.edgemac": ("Microsoft Edge", "chrome")}
_SPOKEN = {"chrome": "com.google.Chrome", "google chrome": "com.google.Chrome", "safari": "com.apple.Safari",
           "arc": "company.thebrowser.Browser", "brave": "com.brave.Browser", "edge": "com.microsoft.edgemac"}

# What a topic word in a rule covers: words in the request, and pieces of a site's address.
TOPICS = {
    "entertainment": {
        "words": {"entertainment", "anime", "youtube", "netflix", "movie", "movies", "film", "films", "series",
                  "music", "song", "songs", "spotify", "twitch", "crunchyroll", "hotstar", "cartoon", "cartoons",
                  "manga", "trailer", "trailers", "episode", "episodes", "prime video", "jiocinema"},
        "sites": {"youtube", "youtu.be", "netflix", "anime", "twitch", "crunchyroll", "hotstar", "primevideo",
                  "spotify", "music.", "jiocinema", "disneyplus", "hulu", "soundcloud", "manga", "aniwatch",
                  "9anime", "zoro", "gogoanime"},
    },
    "anime": {"words": {"anime", "manga", "crunchyroll"},
              "sites": {"anime", "crunchyroll", "aniwatch", "9anime", "zoro", "manga"}},
    "youtube": {"words": {"youtube"}, "sites": {"youtube", "youtu.be"}},
    "music": {"words": {"music", "song", "songs", "spotify", "playlist"},
              "sites": {"spotify", "music.", "soundcloud"}},
    "video": {"words": {"movie", "movies", "film", "films", "series", "netflix", "episode", "episodes"},
              "sites": {"netflix", "primevideo", "hotstar", "jiocinema", "disneyplus", "hulu"}},
}

_NAMED = re.compile(
    r"\b(?:in|on|with|using|use|via|into|inside|through|from)\s+(?:the\s+|my\s+|this\s+|that\s+)?"
    r"(google chrome|chrome|safari|arc|brave|brief|edge)\b"
    r"|\b(google chrome|chrome|safari|brave|brief)\s+(?:only|browser|window|tab)\b"
    r"|\b(?:open|launch|start)\s+(?:up\s+)?(?:the\s+|my\s+)?(google chrome|chrome|safari|brave|brief|arc|edge)\b", re.I)


# --- which browser ---------------------------------------------------------------------------

def named_in(request: str) -> str:
    """The browser the user named in their words ("open anime on this Chrome only"), as a bundle id."""
    found = _NAMED.search(request or "")
    if not found:
        return ""
    word = (found.group(1) or found.group(2) or found.group(3) or "").lower()
    if word == "brief":     # speech recognition hears "Brave" as "brief"; "in brief" is also plain English
        if not re.search(r"\bbrief\s+(browser|window|tab)\b|\b(on|using|with|open|launch)\s+(my\s+|the\s+)?brief\b",
                         request, re.I):
            return ""
        word = "brave"
    return _SPOKEN.get(word, "")


def bundle_for(name: str) -> str:
    key = (name or "").lower().replace("browser", "").strip()
    if not key:
        return ""
    if key in BROWSERS:
        return key
    if key in _SPOKEN:
        return _SPOKEN[key]
    for bundle, (full, _) in BROWSERS.items():
        if key in full.lower():
            return bundle
    return ""


def _load_rules() -> list[dict]:
    try:
        rules = json.loads(RULES.read_text())
        return [r for r in rules.get("rules", []) if isinstance(r, dict) and r.get("browser")]
    except (OSError, ValueError, AttributeError):
        return []


def _save_rules(rules: list[dict]) -> None:
    RULES.parent.mkdir(parents=True, exist_ok=True)
    RULES.write_text(json.dumps({"rules": rules}, indent=1, ensure_ascii=False))


_memory_cache: tuple[float, list[dict]] = (0.0, [])


def _memory_rules() -> list[dict]:
    """Rules read from remembered preferences that name a browser and what it is for."""
    global _memory_cache
    try:
        from mint.knowledge import memory as membank
        stamp = membank.BANK.stat().st_mtime
    except Exception:
        return []
    if stamp == _memory_cache[0]:
        return _memory_cache[1]
    rules = []
    try:
        blocks = membank.blocks()
    except Exception:
        blocks = []
    for block in blocks:
        text = str(block.get("text") or "")
        rule = rule_from_text(text)
        if rule:
            rule["from"] = f"memory {block.get('id', '')}".strip()
            rules.append(rule)
    _memory_cache = (stamp, rules)
    return rules


def rule_from_text(text: str) -> dict | None:
    """'Brave is the default for entertainment and YouTube' -> {browser, topics, sites}. None when not a rule."""
    low = text.lower()
    if not re.search(r"\b(default|always|use|uses|open|opens|prefer|prefers|preferred|only)\b", low):
        return None
    browsers = [b for word, b in _SPOKEN.items() if re.search(rf"\b{re.escape(word)}\b", low)]
    browsers = list(dict.fromkeys(browsers))
    if len(browsers) != 1:          # "not Chrome, use Brave" names two: too unclear to act on
        if len(browsers) == 2 and re.search(r"\b(instead of|not|never|rather than)\b", low):
            chosen = [b for b in browsers if not any(re.search(
                rf"\b(instead of|not|never|rather than)\s+(the\s+|my\s+|in\s+|on\s+|use\s+)?{re.escape(word)}\b", low)
                for word, spoken in _SPOKEN.items() if spoken == b)]
            if len(chosen) != 1:
                return None
            browsers = chosen
        else:
            return None
    sites = sorted(set(re.findall(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|tv|to|app|co|in|me))\b", low)))
    # A topic counts only when named ("entertainment", "anime"); a single service named in passing
    # ("Netflix") covers just that service, not everything in its topic.
    topics = sorted(t for t in TOPICS if re.search(rf"\b{t}\b", low))
    covered = set().union(*(TOPICS[t]["words"] for t in topics)) if topics else set()
    site_words = {site.split(".")[0] for site in sites}
    words = sorted({w for spec in TOPICS.values() for w in spec["words"]
                    if w not in covered and w not in site_words and re.search(rf"\b{re.escape(w)}\b", low)})
    if not topics and not sites and not words:
        return None
    return {"browser": BROWSERS[browsers[0]][0], "topics": topics, "sites": sites, "words": words}


def rules() -> list[dict]:
    """Explicit rules first (the user set them through Mint), then remembered ones."""
    return _load_rules() + _memory_rules()


def _host(url: str) -> str:
    try:
        return (urlparse(url if "://" in url else "https://" + url).netloc or "").lower()
    except ValueError:
        return ""


def rule_for(url: str = "", request: str = "") -> dict | None:
    """The first rule covering this address or these words."""
    host = _host(url) if url else ""
    words = " " + " ".join(re.findall(r"[a-z0-9.]+", (request or "").lower())) + " "
    for rule in rules():
        for site in rule.get("sites", []):
            if host and (host == site or host.endswith("." + site) or site in host):
                return rule
        for word in rule.get("words", []):
            if f" {word} " in words or (host and word.replace(" ", "") in host):
                return rule
        for topic in rule.get("topics", []):
            spec = TOPICS.get(topic, {"words": {topic}, "sites": {topic}})
            if host and any(piece in host for piece in spec["sites"]):
                return rule
            if any(f" {w} " in words for w in spec["words"]):
                return rule
    return None


def default_browser() -> str:
    try:
        import AppKit
        url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(
            AppKit.NSURL.URLWithString_("https://example.com"))
        bundle = AppKit.NSBundle.bundleWithURL_(url).bundleIdentifier() if url else ""
        return bundle if bundle in BROWSERS else ""
    except Exception:
        return ""


def installed(bundle: str) -> bool:
    try:
        import AppKit
        return AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle) is not None
    except Exception:
        return False


def _running(bundle: str):
    import AppKit
    for app in AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle) or []:
        if not app.isTerminated():
            return app
    return None


def choose(url: str = "", asked: str = "", request: str | None = None) -> tuple[str, str]:
    """(bundle id, why) for opening `url`. `asked` is the browser the model passed, if any."""
    if request is None:
        try:
            from mint.app import live
            request = live.request() or ""
        except Exception:
            request = ""
    named = named_in(request)
    if named and installed(named):
        return named, "you named it"
    rule = rule_for(url, request)
    if rule:
        bundle = bundle_for(rule["browser"])
        if bundle and installed(bundle):
            what = ", ".join(rule.get("topics") or rule.get("sites") or [])
            return bundle, f"your rule: {what} in {rule['browser']}"
    wanted = bundle_for(asked)
    if wanted and installed(wanted):
        return wanted, "asked for"
    import AppKit
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is not None and front.bundleIdentifier() in BROWSERS:
        return front.bundleIdentifier(), "in front"
    try:
        from mint.tools import extra as extra_tools
        last = extra_tools._target.get("app")
        if last is not None and not last.isTerminated() and last.bundleIdentifier() in BROWSERS:
            return last.bundleIdentifier(), "the one Mint was using"
    except Exception:
        pass
    default = default_browser()
    if default:
        return default, "the Mac's default browser"
    for bundle in BROWSERS:
        if _running(bundle) is not None:
            return bundle, "the one open"
    return "com.google.Chrome", "fallback"


def conflicts(app_name: str, request: str | None = None) -> str:
    """When the model is about to open a browser the user's rule does not want for this request,
    the browser the rule wants (name). '' when fine."""
    bundle = bundle_for(app_name)
    if bundle not in BROWSERS:
        return ""
    if request is None:
        from mint.app import live
        request = live.request() or ""
    if named_in(request):
        return ""
    rule = rule_for("", request)
    if not rule:
        return ""
    wanted = bundle_for(rule["browser"])
    return rule["browser"] if wanted and wanted != bundle else ""


# --- opening ------------------------------------------------------------------------------------

def _osascript(script: str, timeout: float = 8) -> tuple[bool, str]:
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    return done.returncode == 0, (done.stdout or done.stderr or "").strip()


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _same_page(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    host_a, host_b = pa.netloc.lower().removeprefix("www."), pb.netloc.lower().removeprefix("www.")
    if host_a != host_b:
        return False
    path_a, path_b = pa.path.rstrip("/") or "/", pb.path.rstrip("/") or "/"
    return path_a == path_b or (path_a in {"/", "/home"} and path_b in {"/", "/home"})


def _is_blank(url: str) -> bool:
    return (url or "").rstrip("/") in {"", "about:blank", "chrome://newtab", "chrome://new-tab-page", "brave://newtab",
                                       "edge://newtab", "arc://newtab"}


def open_in(url: str, bundle: str, why: str = "") -> str:
    """Open `url` in that browser: reuse a tab already showing it, else a new tab in its front window."""
    name, family = BROWSERS[bundle]
    app = _running(bundle)
    reason = f" ({why})" if why else ""
    if app is not None:
        from mint.tools import harness as h
        try:
            tabs = h._tabs(app)
        except Exception:
            tabs = []
        same = next((t for t in tabs if _same_page(t["url"], url)), None)
        if same is not None:
            w, t = same["window"], same["tab"]
            if family == "chrome":
                script = f'tell application "{name}"\n set active tab index of window {w} to {t}\n set index of window {w} to 1\nend tell'
            else:
                script = f'tell application "{name}"\n set current tab of window {w} to tab {t} of window {w}\n set index of window {w} to 1\nend tell'
            ok, _ = _osascript(script, 6)
            if ok:
                _front(app)
                return f"Switched to your already-open {name} tab '{same['title'][:60]}' - reused it{reason}."
        blank = next((t for t in tabs if t["window"] == 1 and t["active"] and _is_blank(t["url"])), None)
        if family == "chrome" and blank is not None:
            # A fresh window's empty New Tab takes the page instead of leaving a blank tab beside it.
            script = f'tell application "{name}" to set URL of active tab of front window to {_q(url)}'
        elif family == "chrome":
            script = (f'tell application "{name}"\n if (count of windows) = 0 then make new window\n'
                      f' tell front window to make new tab with properties {{URL:{_q(url)}}}\nend tell')
        else:
            script = (f'tell application "{name}"\n if (count of windows) = 0 then make new document\n'
                      f' tell front window to set current tab to (make new tab with properties {{URL:{_q(url)}}})\nend tell')
        ok, output = _osascript(script, 8)
        if ok:
            _front(app)
            return f"Opened {url} in a new {name} tab{reason}."
        log.info("new tab in %s failed: %s", name, output[:200])
    if app is None:
        # Not running: start it, let it open its window, then put the page in that window's tab -
        # handing the address to `open` made a second window beside the startup one in testing.
        subprocess.run(["open", "-b", bundle], capture_output=True, check=False)
        for _ in range(50):
            app = _running(bundle)
            if app is not None and app.isFinishedLaunching():
                break
            time.sleep(0.2)
        if app is not None:
            for _ in range(25):
                ok, count = _osascript(f'tell application "{name}" to return count of windows', 3)
                if ok and count.strip() not in {"", "0"}:
                    break
                time.sleep(0.2)
            time.sleep(0.3)
            return open_in(url, bundle, why)
    done = subprocess.run(["open", "-b", bundle, url], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        return f"Could not open {url} in {name}. {(done.stderr or '').strip()}"
    if app is not None:
        _front(app)
    return f"Opened {url} in {name}{reason}."


def _front(app) -> None:
    try:
        from mint.screen.ground import bring_forward
        bring_forward(app, 2.0)
    except Exception:
        try:
            import AppKit
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        except Exception:
            pass


def open_url(url: str, browser: str = "") -> str:
    url = (url or "").strip()
    if not url:
        return "No address given."
    if "://" not in url:
        url = "https://" + url
    if not url.startswith(("http://", "https://")):
        return "Only http and https addresses can be opened."
    bundle, why = choose(url, browser)
    return open_in(url, bundle, why)


# --- rules the user sets --------------------------------------------------------------------------

def set_rule(what: str, browser: str) -> str:
    bundle = bundle_for(browser)
    if bundle not in BROWSERS:
        return f"FAILED: '{browser}' is not a browser Mint knows (Chrome, Brave, Safari, Arc, Edge)."
    if not installed(bundle):
        return f"FAILED: {BROWSERS[bundle][0]} is not installed."
    items = [w.strip().lower() for w in re.split(r",|\band\b|/", what or "") if w.strip()]
    if not items:
        return "FAILED: say what the rule covers, e.g. 'entertainment' or 'youtube.com, netflix'."
    topics = [i for i in items if i in TOPICS]
    sites = [_host(i).removeprefix("www.") for i in items if i not in TOPICS and "." in i]
    words = [i for i in items if i not in TOPICS and "." not in i]
    covers = set(topics) | set(sites) | set(words)
    current = [r for r in _load_rules()
               if not covers & set(r.get("topics", []) + r.get("sites", []) + r.get("words", []))]
    rule = {"browser": BROWSERS[bundle][0], "topics": topics, "sites": sites, "words": words,
            "set": time.strftime("%Y-%m-%d %H:%M")}
    _save_rules([rule] + current)
    return f"Rule saved: {', '.join(topics + sites + words)} open in {BROWSERS[bundle][0]} from now on."


def remove_rule(what: str) -> str:
    what = (what or "").strip().lower()
    kept = [r for r in _load_rules() if what not in r.get("topics", []) + r.get("sites", []) + r.get("words", [])]
    removed = len(_load_rules()) - len(kept)
    _save_rules(kept)
    note = ""
    if any(what in r.get("topics", []) + r.get("sites", []) + r.get("words", []) for r in _memory_rules()):
        note = " A remembered preference still says so; forget that memory to drop it completely."
    return (f"Removed {removed} rule(s) for '{what}'." if removed else f"No saved rule for '{what}'.") + note


def describe_rules() -> str:
    rows = [f"- {', '.join(r.get('topics', []) + r.get('sites', []) + r.get('words', []))} -> {r['browser']}"
            + (f"  (from {r['from']})" if r.get("from") else "") for r in rules()]
    default = default_browser()
    tail = f"\nEverything else: the browser in front, else {BROWSERS[default][0] if default else 'the default'}."
    return ("Browser rules:\n" + "\n".join(rows) if rows else "No browser rules yet.") + tail
