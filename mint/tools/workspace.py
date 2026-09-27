"""What is open right now, and going back to it instead of opening it again.

A person who already has Gmail open clicks its tab; they do not open a second
Gmail. Without this, every step of a long task opened a fresh tab or window -
in testing they piled up across Spaces until typing went to windows nobody
could see. This reads Chrome's windows and tabs through Accessibility (each
window's title carries its profile) so a tool can reuse what is already there.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import AppKit
import ApplicationServices as AX

CHROME_ID = "com.google.Chrome"

# Site -> words that appear in its tab titles.
SITE_TITLES = {
    "mail.google.com": ("Mail", "Gmail", "Inbox"),
    "gmail.com": ("Mail", "Gmail", "Inbox"),
    "calendar.google.com": ("Google Calendar", "Calendar"),
    "docs.google.com": ("Google Docs",),
    "sheets.google.com": ("Google Sheets",),
    "drive.google.com": ("Google Drive",),
    "notion.so": ("Notion",),
    "www.notion.so": ("Notion",),
    "linear.app": ("Linear",),
    "github.com": ("GitHub",),
    "youtube.com": ("YouTube",),
    "www.youtube.com": ("YouTube",),
    "wikipedia.org": ("Wikipedia",),
    "en.wikipedia.org": ("Wikipedia",),
    "chatgpt.com": ("ChatGPT",),
}

# Addresses that always mean "make a new one", so they are never reused.
CREATE_URLS = ("docs.new", "sheets.new", "slides.new", "document/create", "compose")


def _ax(element, attribute):
    err, value = AX.AXUIElementCopyAttributeValue(element, attribute, None)
    return value if err == 0 else None


def _chrome():
    return next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                 if a.bundleIdentifier() == CHROME_ID), None)


def _tabs_in(window, depth: int = 0):
    """Tab buttons in a Chrome window's tab strip."""
    found, seen, stack = [], set(), [(window, 0)]
    while stack:
        node, level = stack.pop()
        role = _ax(node, "AXRole")
        if role == "AXTabGroup":
            for child in _ax(node, "AXChildren") or []:
                if _ax(child, "AXRole") == "AXRadioButton":
                    # The same strip can be reached by more than one path; in
                    # testing every tab was listed four times.
                    key = (_ax(child, "AXTitle"), str(_ax(child, "AXPosition")))
                    if key not in seen:
                        seen.add(key)
                        found.append(child)
            continue
        if level < 7:
            stack.extend((c, level + 1) for c in (_ax(node, "AXChildren") or []))
    return found


def chrome_windows(bring_forward: bool = True) -> list[dict]:
    """[{window, profile, title, minimized, tabs: [{element, title, selected}]}].

    Accessibility lists only windows on the current Space. With Chrome on
    another Space it reported no windows at all in testing, and every reuse
    check missed. If none are visible, bring Chrome forward (as ⌘-Tab would,
    which switches to its Space) and look again.
    """
    chrome = _chrome()
    if chrome is None:
        return []
    app = AX.AXUIElementCreateApplication(chrome.processIdentifier())
    listed = _ax(app, "AXWindows") or []
    if not listed and bring_forward:
        chrome.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        time.sleep(0.6)
        listed = _ax(app, "AXWindows") or []
    windows = []
    for window in listed:
        title = _ax(window, "AXTitle") or ""
        # "… - Google Chrome – Alex (example.com)": the profile follows the en dash.
        profile = title.rsplit(" – ", 1)[1].strip() if " – " in title else ""
        # Chrome appends " - Memory usage - 258 MB" to tab titles; drop the noise.
        tabs = [{"element": t,
                 "title": re.sub(r"\s+-\s+Memory usage\s+-\s+[\d.,]+\s*[KMG]B$", "", _ax(t, "AXTitle") or ""),
                 "selected": bool(_ax(t, "AXValue"))} for t in _tabs_in(window)]
        windows.append({"window": window, "profile": profile, "title": title,
                        "minimized": bool(_ax(window, "AXMinimized")), "tabs": tabs})
    return windows


def describe() -> str:
    """A compact picture of what is open, for the model."""
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    lines = [f"In front: {front.localizedName() if front else 'nothing'}"]
    windows = chrome_windows(bring_forward=False)   # looking must not switch Spaces
    if not windows and _chrome() is not None:
        lines.append("Chrome is running, but its windows are on another Space (not visible from here).")
    for i, w in enumerate(windows, 1):
        tabs = "; ".join(("*" if t["selected"] else "") + t["title"][:50] for t in w["tabs"]) or "(no tabs read)"
        state = " (minimized)" if w["minimized"] else ""
        lines.append(f"Chrome window {i} [{w['profile'] or 'profile unknown'}]{state}: {tabs}")
    return "\n".join(lines)


def _site(url: str) -> str:
    url = url if "://" in url else "https://" + url
    return (urlparse(url).hostname or "").lower()


def find_tab(url: str, profile: str = "") -> tuple[dict, dict] | None:
    """An open tab showing `url`'s site, preferring the given profile."""
    if any(marker in url for marker in CREATE_URLS):
        return None
    words = SITE_TITLES.get(_site(url)) or SITE_TITLES.get(_site(url).removeprefix("www."))
    if not words:
        return None
    candidates = []
    for w in chrome_windows():
        if profile and profile.lower() not in w["profile"].lower():
            continue
        for tab in w["tabs"]:
            if any(word.lower() in tab["title"].lower() for word in words):
                candidates.append((w, tab))
    if not candidates:
        return None
    # Prefer the tab already selected in its window, then the first found.
    candidates.sort(key=lambda pair: not pair[1]["selected"])
    return candidates[0]


def focus(window: dict, tab: dict | None = None) -> None:
    """Bring a Chrome window (and one of its tabs) to the front, from any Space."""
    chrome = _chrome()
    if chrome is None:
        return
    element = window["window"]
    if window.get("minimized"):
        AX.AXUIElementSetAttributeValue(element, "AXMinimized", False)
    if tab is not None:
        AX.AXUIElementPerformAction(tab["element"], "AXPress")
    AX.AXUIElementPerformAction(element, "AXRaise")
    AX.AXUIElementSetAttributeValue(element, "AXMain", True)
    chrome.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    time.sleep(0.4)


def switch_to(what: str) -> str:
    """Bring an open tab or window to the front by words from its title."""
    wanted = [w for w in re.findall(r"[a-z0-9@.]+", what.lower()) if len(w) > 1]
    scored = []
    for w in chrome_windows():
        for tab in w["tabs"]:
            haystack = (tab["title"] + " " + w["profile"]).lower()
            score = sum(1 for word in wanted if word in haystack)
            if score:
                scored.append((score, w, tab))
    if not scored:
        return f"FAILED: no open tab matches '{what}'. Open tabs:\n{describe()}"
    scored.sort(key=lambda item: -item[0])
    top = [item for item in scored if item[0] == scored[0][0]]
    distinct = {item[2]["title"] for item in top}
    if len(top) > 1:
        # Several tabs match equally well - e.g. many "Untitled document"s. In
        # testing, picking the first one edited the wrong document. Ask instead.
        names = "; ".join(sorted(distinct))
        return (f"FAILED: '{what}' matches {len(top)} open tabs equally ({names}). "
                "Ask the user which one, or use a more specific name.")
    focus(top[0][1], top[0][2])
    return f"Switched to the open tab '{top[0][2]['title']}' ({top[0][1]['profile']})."


def new_tab_here(url: str) -> str:
    """Open `url` in a new tab of the Chrome window in front - the way a person does.

    Handing a URL to macOS (`open URL`) lets Chrome pick the window, and in
    testing it picked a different window from the one Mint was working in:
    the new doc opened elsewhere, typing went to an old doc in front, and the
    export exported the wrong document. ⌘T in the working window keeps
    everything together.
    """
    from mint.tools import fastinput
    from mint.tools import everyday as skills

    chrome = _chrome()
    if chrome is None:
        return ""
    chrome.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    time.sleep(0.3)
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None or front.bundleIdentifier() != CHROME_ID:
        return ""
    fastinput.press_key("t", ["command"])
    # Paste only into the address bar, verified. In testing the new tab did not
    # take the address, and the window ended up showing one of the user's work
    # pages; an unverified paste + Return would have gone into that page.
    app = AX.AXUIElementCreateApplication(chrome.processIdentifier())
    focused = None
    for _ in range(10):
        time.sleep(0.15)
        focused = _ax(app, "AXFocusedUIElement")
        if focused is not None and "address" in str(_ax(focused, "AXDescription") or "").lower():
            break
        focused = None
    if focused is None:
        return ""   # the caller falls back to handing the address to macOS
    board = AppKit.NSPasteboard.generalPasteboard()
    with skills._Clipboard():
        board.clearContents()
        board.setString_forType_(url, AppKit.NSPasteboardTypeString)
        fastinput.press_key("v", ["command"])
        time.sleep(0.15)
    # Still the address bar? Only then press Return.
    still = _ax(app, "AXFocusedUIElement")
    if still is None or "address" not in str(_ax(still, "AXDescription") or "").lower():
        return ""
    fastinput.press_key("return")
    time.sleep(0.8)
    return url


def front_tab_url() -> str:
    """The address of the front Chrome tab, read from its address bar."""
    chrome = _chrome()
    if chrome is None:
        return ""
    app = AX.AXUIElementCreateApplication(chrome.processIdentifier())
    window = _ax(app, "AXFocusedWindow") or _ax(app, "AXMainWindow")
    stack, seen = [window], 0
    while stack and seen < 800:
        node = stack.pop()
        seen += 1
        if _ax(node, "AXRole") == "AXTextField" and "address" in str(_ax(node, "AXDescription") or "").lower():
            value = str(_ax(node, "AXValue") or "")
            return value if "://" in value else "https://" + value
        stack.extend(_ax(node, "AXChildren") or [])
    return ""
