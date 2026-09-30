"""The harness: the everyday tools an assistant on a Mac needs beyond clicking.

* Files     - read_file, write_file, find_files, file_action. Anything in the
              home folder except secrets (keys, keychains, browser profiles,
              .env files); writes never touch hidden or Library folders, an
              overwritten file is backed up first, and "delete" means the Trash.
* Web       - web_search (Exa or Tavily when a key is set, DuckDuckGo without
              one) and read_url, shared with the sub-agents (agents/tools.py).
* Browser   - one `browser` tool: read the page, list links, find text, click,
              fill a field, scroll, back/forward/reload, run JavaScript. It
              uses page JavaScript through AppleScript when the browser allows
              it, and the page's accessibility tree when it does not (Chrome
              ships with "Allow JavaScript from Apple Events" off).
* Screen    - scroll_to (bring a named thing into view, scrolling a list
              until it appears; or scroll one named pane), menu (click or list
              any menu-bar command by its path, with its keyboard shortcut),
              wait_for_text.
* Scripting - run_applescript, for apps with a scripting dictionary (Finder,
              Music, Notes, Calendar...). Shell escapes are refused; commands
              that send, delete or quit need the user to have asked for that.

`guard` wraps every tool call (not just these): it refuses to type into a
password field, and warns the model when it repeats the same call with the same
result, or when several steps in a row have failed, so it changes approach
instead of looping.

Ideas from the reference harnesses in ref/ - aura (browser JS over AppleScript,
menu-bar paths, password fields, the tool-choice ladder), browser-use's
macos-harness and computer-harness (AX search predicates, wheel events aimed at
one element), MacOS-Use (loop detection) and Mark-XXXV (file tools that keep to
safe places); the code is Mint's own.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections import deque
from pathlib import Path

from google.genai import types

log = logging.getLogger("mint.harness")

HOME = Path.home()
MINT_FILES = HOME / "Documents" / "Mint"      # the default; config.storage() is the real one (Settings > Storage)
BACKUPS = HOME / "Library" / "Application Support" / "Mint" / "backups"

STRING = {"type": types.Type.STRING}
INTEGER = {"type": types.Type.INTEGER}
BOOLEAN = {"type": types.Type.BOOLEAN}


def _fn(name, description, properties, required=None):
    return types.FunctionDeclaration(
        name=name, description=description,
        parameters=types.Schema(type=types.Type.OBJECT,
                                properties={k: types.Schema(**v) for k, v in properties.items()},
                                required=required or []))


def _enum(values, description):
    return {"type": types.Type.STRING, "enum": list(values), "description": description}


def declarations() -> list[types.FunctionDeclaration]:
    from mint.tools import clipboard as clip_tools
    clip_tools.start_watching()
    return clip_tools.declarations() + [
        _fn("read_file",
            "Read a file on this Mac: text, code, Markdown, CSV, JSON, PDF, Word/RTF/Pages-exported docs. "
            "`path` can be absolute, start with ~, or be relative to Desktop/Documents/Downloads; a bare "
            "name is looked up with Spotlight. A folder path lists the folder. Long files come in parts: "
            "call again with start_line to continue.",
            {"path": STRING, "start_line": {**INTEGER, "description": "1-based line to start from"},
             "max_chars": {**INTEGER, "description": "default 8000, at most 30000"}},
            ["path"]),
        _fn("write_file",
            "Create or change a text file (notes, code, Markdown, CSV, HTML...). mode: create (new file; "
            "fails if it exists), overwrite (replace all, only when the user asked to replace what is in it; the "
            "old version is backed up first), append, or "
            "replace (swap the exact text `find` for `content`; `find` must occur once; also in .rtf/.rtfd "
            "documents, keeping their formatting). A bare file name "
            "goes to Mint's Documents folder (Settings > Storage). Never put passwords or keys in a file.",
            {"path": STRING, "content": STRING,
             "mode": _enum(("create", "overwrite", "append", "replace"), "default create"),
             "find": {**STRING, "description": "for mode=replace: the exact text to replace"}},
            ["path", "content"]),
        _fn("find_files",
            "Find files and folders by name or content with Spotlight, newest first. With no query it "
            "lists `folder` (or, with no folder either, what changed recently on Desktop, Documents and "
            "Downloads). Each result shows its size and the date it was last modified (saved). kind narrows it: "
            "pdf, image, doc, text (.txt/.md/.csv...), sheet, slides, code, video, audio, folder, app. "
            "days=7 keeps only what was modified in the last 7 days; modified_from / modified_to "
            "(YYYY-MM-DD, or YYYY-MM for a whole month, inclusive) keep a date range - use these for "
            "'from last week' / 'saved in September'. A date printed INSIDE a document (an invoice date) is "
            "not its modified date: read the files for that.",
            {"query": STRING, "folder": STRING,
             "kind": _enum(("any", "pdf", "image", "doc", "text", "sheet", "slides", "code", "video", "audio",
                            "folder", "app"), "default any"),
             "content": {**BOOLEAN, "description": "also match text inside files (default true)"},
             "days": {**INTEGER, "description": "only items modified within the last this many days"},
             "modified_from": {**STRING, "description": "only items modified on or after this date (YYYY-MM-DD or YYYY-MM)"},
             "modified_to": {**STRING, "description": "only items modified on or before this date (YYYY-MM-DD or YYYY-MM)"},
             "sort": _enum(("newest", "oldest", "biggest", "smallest", "name"), "default newest"),
             "limit": INTEGER}),
        _fn("file_action",
            "Do something with a file or folder: open (in its default app), reveal (in Finder), info, "
            "move / copy / rename (to `to`), make_folder, trash (moves it to the Trash - only when the "
            "user asked to delete it; it can be restored from there).",
            {"action": _enum(("open", "reveal", "info", "move", "copy", "rename", "make_folder", "trash"),
                             "what to do"),
             "path": STRING, "to": {**STRING, "description": "destination folder or new name"}},
            ["action", "path"]),
        _fn("web_search",
            "Search the web and get titles, links and snippets - for facts, news, prices, docs, anything "
            "current. Much faster than opening a browser. Follow up with read_url to read a result.",
            {"query": STRING, "max_results": INTEGER}, ["query"]),
        _fn("read_url",
            "Fetch a web page or text URL and get its readable text, without opening it on screen. For a "
            "page the user has open in their browser (logged-in pages), use browser action=read instead.",
            {"url": STRING, "max_chars": INTEGER}, ["url"]),
        _fn("browser",
            "Full control of the web browser (Chrome, Safari, Brave, Arc, Edge) - faster and surer than "
            "clicking pixels. Pages: read (page text), links (links with URLs), url (address and title), find "
            "(is `target` on the page?), click (the link/button whose text is `target`), fill (type `text` "
            "into the field labelled `target`; empty target = the focused field), select (choose option "
            "`text` in the dropdown labelled `target`), scroll (target = down/up/top/bottom), wait (until the "
            "page has loaded), js (run JavaScript `text`; needs the browser's 'Allow JavaScript from Apple "
            "Events'). Navigation: go (load URL `target` in the CURRENT tab, replacing its page), back, forward, "
            "reload. When the user says 'new tab', or the current tab is theirs, use new_tab instead. Tabs: tabs (list "
            "every tab in every window), switch (to the tab whose title or address contains `target`, or its "
            "number from tabs), new_tab (URL `target`), close_tab (the tab matching `target`, or the current "
            "one - only when the user asked), group (Chrome/Brave/Edge tab group named `target` holding the tabs "
            "in `text`, comma-separated titles or sites, e.g. target='Entertainment' text='YouTube, anime'; tabs "
            "from other windows are moved in), ungroup (take the tabs out of group `target`; a group that is only "
            "saved in the bookmarks bar is deleted, when the user asked). Browser rules: rule (target = what it covers: a topic like "
            "entertainment/anime/youtube/music/video or sites; text = the browser, or 'remove'; both empty = list "
            "the rules) - save one when the user says which browser to always use for something. Which browser "
            "new_tab uses follows the user's words and rules by itself. toolbar: press the browser's own "
            "button called `target` - an extension (opens its pop-up), 'Extensions', a bookmark, the profile. "
            "Never fills password fields.",
            {"action": _enum(("read", "links", "url", "find", "click", "fill", "select", "scroll", "wait", "js",
                              "go", "back", "forward", "reload", "tabs", "switch", "new_tab", "close_tab", "group",
                              "rule", "toolbar", "ungroup"),
                             "what to do"),
             "target": STRING, "text": STRING,
             "browser": {**STRING, "description": "only when the user named one: Chrome, Brave, Safari..."}},
            ["action"]),
        _fn("scroll_to",
            "Scroll so something is visible, in any app: `target` is text or a control to bring into view "
            "(scrolls a long list or page until it appears). Or scroll one particular pane: `where` "
            "describes it (sidebar, message list, left, right, the settings list...) with direction and "
            "amount. Better than scroll when the thing is off screen or the window has several panes.",
            {"target": STRING, "where": STRING,
             "direction": _enum(("down", "up", "left", "right"), "for where=, default down"),
             "amount": {**INTEGER, "description": "for where=, roughly lines x5, default 3"}}),
        _fn("menu",
            "Use the app's menu bar - every command an app has is there, with its keyboard shortcut. "
            "action=click runs the command at `path` like 'File > Export as PDF…' or 'View > Show Sidebar' "
            "(the top menu can be left out: 'Export as PDF'). action=list shows the menus, or the items of "
            "`path`'s menu with their shortcuts. `app` defaults to the app in front.",
            {"path": STRING, "action": _enum(("click", "list"), "default click"), "app": STRING}),
        _fn("wait_for_text",
            "Short wait (seconds, at most 2 minutes) for `text` to appear in the front window - or to go "
            "away, with gone=true (a 'Loading…' or 'Exporting…' label, a dialog closing). For a dialog, page "
            "or export after a click. For an AI chat still writing its answer, or anything that takes "
            "minutes, use wait_until_done instead.",
            {"text": STRING, "seconds": {**INTEGER, "description": "give up after this long, default 15"},
             "gone": BOOLEAN}, ["text"]),
        _fn("pointer",
            "The user's mouse pointer: where (what is under it - the control, its app, nearby text) or click / "
            "double_click / right_click right where it is. Use when the user says 'this', 'here', 'what I'm "
            "pointing at', 'where my mouse is', 'click this'.",
            {"action": _enum(("where", "click", "double_click", "right_click"), "default where")}),
        _fn("quit_mint",
            "Quit Mint itself completely - the assistant (you), everything you started (sub-agents, Codex, "
            "preview servers) and the screen-control engine - when the user says 'quit Mint', 'turn yourself "
            "off', 'shut down Mint', 'close Mint'. Say a short goodbye first; you stop a few seconds after. "
            "For 'go to sleep' / 'stop listening' use stop_listening instead: that keeps Mint running.",
            {}),
        _fn("run_applescript",
            "Run an AppleScript for apps with a scripting dictionary: Finder, Music, Notes, Reminders, "
            "Calendar, Mail drafts, Safari/Chrome tabs, System Events UI scripting. Returns its result. "
            "`do shell script` is not allowed; sending, deleting or quitting only when the user asked.",
            {"script": STRING}, ["script"]),
    ]


NAMES = ("read_file", "write_file", "find_files", "file_action", "web_search", "read_url", "browser",
         "scroll_to", "menu", "wait_for_text", "run_applescript", "screenshot", "clipboard", "quit_mint",
         "pointer")

PROMPT = """
# Harness: how to act on this Mac
Work in the background first; move the pointer only when nothing else can do it. Never tell the user you
can't do something on the Mac before trying the tools below - try, and if it fails, try the next rung.
Background (no pointer, nothing needs to be in front):
1. Facts, news, docs, prices: web_search, then read_url on the best result. Don't open a browser to look
   something up unless the user wants to see it.
2. Files: find_files to locate. For a file you don't know the name of ("my grocery list", "the PDF from
   yesterday"), call find_files with NO query first: it lists files Mint made and recently changed documents.
   Then one content word. After three searches, ask the user where it is. read_file to read, write_file to create or edit (mode=replace for a small change), file_action to
   open, reveal, move, rename or trash. Say where you saved things.
3. Web pages: open_url or browser new_tab - they pick the browser by themselves (the one the user names,
   else the user's browser rules, e.g. entertainment in Brave), so never open_app a browser first. Inside a
   page in Chrome or Brave: ALWAYS the browser tool - read / links / find to see, click / fill / select by
   the visible text or label, back, tabs, close_tab, group (a named tab group: "put YouTube and anime in a
   group called Entertainment"), toolbar (an extension's button, a bookmark). It is exact and fast. When
   the user says which browser to always use for something, save it: browser action=rule.
4. Scriptable apps (Finder, Music, Notes, Calendar, Reminders, System Events): run_applescript.
Foreground (the app comes to the front, still no guessing):
5. An app command (export, new window, show sidebar, full screen, format, sort): menu - list a menu first
   when unsure; it also tells you the keyboard shortcut for next time.
6. Something off screen: scroll_to target=... (it scrolls until it appears); one pane of several: where=.
7. A control by its name: ui_act (ui_elements lists the exact names).
8. After a click that opens a dialog or page, or starts an export: wait_for_text (seconds; gone=true for a
   'Loading…' label). An AI chat writing its answer, or minutes of work: wait_until_done.
Pointer (last):
9. "this" / "here" / "where my mouse is": pointer (where tells you what it is over; click clicks it).
10. Only for things no tool above can see (canvases, games, custom-drawn apps): look, then click_at. click_at
   says what it hit - if that is not the target, pick a different point or use ui_act; never the same point twice.
If a tool result starts with [Loop warning], do something different - never the same call a fourth time.
A request repeated after an earlier failure means: try again, differently - never answer it from the
earlier failure without a single tool call.
"""

from mint.tools import clipboard as _clip  # noqa: E402  (the screenshot and clipboard guide joins the prompt)
PROMPT += _clip.PROMPT



# --- Guard: password fields and loops, around every tool call --------------------------------

_calls: deque = deque(maxlen=40)       # (time, fingerprint, result digest)
_names: deque = deque(maxlen=12)       # tool names, most recent last
SAME_TOOL = 6      # this many calls in a row to one tool, whatever the arguments: warn
_outcomes: deque = deque(maxlen=6)     # True when a call failed
_EXEMPT = {"wait_until_done", "wait_for_text", "agent_status", "look", "read_window", "get_status", "recall",
           "list_open", "frontmost_app", "list_agents", "list_skills", "list_memories", "express",
           "step_done", "plan_task", "task", "find_skill", "set_voice", "set_preference", "automation",
           "watch_video"}
# The "N calls in a row to one tool" warning is for searching in circles (30 find_files in testing). Doing
# a list of things the user asked for - every orb trick, several sites, many files - is not a loop:
# it told the model to stop at the 6th of "do all the tricks one by one".
_STREAK_TOOLS = {"find_files", "web_search", "read_url", "recall", "find_skill", "ui_elements", "look", "click_at",
                 "click_text", "ui_act", "desktop", "scroll_to", "menu", "read_window", "list_files"}
_STREAK_BROWSER = {"find", "click", "fill", "select", "links", "read"}
REPEATS = 3        # the same call with the same result this many times: warn
FAILS = 4          # this many failed calls in a row: warn


def _fingerprint(name: str, args: dict) -> str:
    clean = {k: (" ".join(v.lower().split()) if isinstance(v, str) else v) for k, v in (args or {}).items()}
    return hashlib.sha1(json.dumps({name: clean}, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _failed(result: str) -> bool:
    head = (result or "")[:80].lower()
    return head.startswith(("failed", "could not", "cannot", "did nothing", "not found", "refused", "no ")) \
        or " failed" in head


def _password_field() -> bool:
    try:
        import ApplicationServices as AX

        from mint.screen.axkit import attr
        element = attr(AX.AXUIElementCreateSystemWide(), "AXFocusedUIElement")
        return element is not None and attr(element, "AXSubrole") == "AXSecureTextField"
    except Exception:
        return False


def check(name: str, args: dict, result: str) -> str:
    """The loop warning to add to `result`, or ''."""
    now = time.monotonic()
    if name in _EXEMPT or name in {"scroll", "press_key", "media_key", "set_volume", "music"} or (
            name == "browser" and str((args or {}).get("action")) in {"scroll", "back", "forward", "reload", "read"}):
        _outcomes.append(_failed(result))
        return ""
    key = _fingerprint(name, args)
    digest = hashlib.sha1((result or "")[:3000].encode()).hexdigest()[:12]
    same = sum(1 for at, k, d in _calls if k == key and d == digest and now - at < 180)
    _calls.append((now, key, digest))
    _outcomes.append(_failed(result))
    _names.append(name)
    streak = 0
    for previous in reversed(_names):
        if previous != name:
            break
        streak += 1
    if same + 1 >= REPEATS:
        log.info("loop warning: %s repeated %d times", name, same + 1)
        return (f"\n[Loop warning] This exact {name} call has now given the same result {same + 1} times. Do not "
                "repeat it. Change approach: menu (commands and shortcuts), browser (web pages), scroll_to, "
                "ui_elements to see the controls, look at the screen - or tell the user what is in the way.")
    searching = name in _STREAK_TOOLS or (name == "browser" and str((args or {}).get("action")) in _STREAK_BROWSER)
    if searching and streak and streak % SAME_TOOL == 0:
        log.info("loop warning: %s %d times in a row", name, streak)
        return (f"\n[Loop warning] That is {streak} {name} calls in a row. Stop trying variations. Use what "
                "you have, try a different kind of step - or ask the user one short question (for a file: "
                "its name or where it was saved).")
    if len(_outcomes) >= FAILS and all(list(_outcomes)[-FAILS:]):
        _outcomes.clear()
        return (f"\n[Loop warning] The last {FAILS} steps all failed. Stop and check where things are (look, "
                "or list_open), then try one different approach - or tell the user what is blocking it.")
    return ""


async def guard(core, name: str, args: dict):
    """Around every tool call."""
    import asyncio

    if name == "type_text" and await asyncio.to_thread(_password_field):
        return ("REFUSED: the focused field is a password field. Mint never types passwords - ask the user "
                "to type it themselves, then carry on."), None
    if name in {"open_app", "switch_to"}:
        from mint.tools import browser_choice
        wanted = await asyncio.to_thread(browser_choice.conflicts, str(args.get("name") or args.get("what") or ""))
        if wanted:
            return (f"NOT RUN: the user's browser rule puts this in {wanted}, not {args.get('name') or args.get('what')}. "
                    f"Use open_url (it opens {wanted} by itself) or browser action=new_tab - don't open another "
                    "browser first."), None
    result, image = await core(name, args)
    if isinstance(result, str):
        result += check(name, args, result)
    return result, image


# --- Asking before irreversible things ----------------------------------------------------------

# A risky word in a command or control -> words that show the user asked for it.
_RISKY = {
    "send": ("send", "email", "mail", "reply", "post", "submit"),
    "submit": ("submit", "send", "post", "apply"),
    "post": ("post", "publish", "share", "tweet", "send"),
    "publish": ("publish", "post", "release"),
    "delete": ("delete", "remove", "trash", "erase", "get rid", "clear", "clean"),
    "erase": ("erase", "delete", "wipe"),
    "empty": ("empty",),
    "trash": ("trash", "delete", "remove", "bin"),
    "discard": ("discard", "delete", "throw away", "drop"),
    "quit": ("quit", "close", "exit", "shut", "kill"),
    "close": ("close", "quit", "exit"),
    "buy": ("buy", "purchase", "order", "checkout", "pay"),
    "pay": ("pay", "purchase", "buy", "checkout"),
    "place order": ("order", "buy", "purchase"),
    "checkout": ("checkout", "buy", "purchase", "order"),
    "sign out": ("sign out", "log out", "logout", "signout"),
    "log out": ("log out", "sign out", "logout"),
    "restart": ("restart", "reboot"),
    "shut down": ("shut down", "shutdown", "power off"),
    "uninstall": ("uninstall", "remove"),
    "unsubscribe": ("unsubscribe",),
    "confirm": ("confirm", "yes", "go ahead"),
}


_NEGATED = r"(?:don'?t|do not|not|never|without|no need to|avoid)\s+(?:\w+\s+){0,2}"


def _asked(request: str, word: str) -> bool:
    """`word` is in the request and not only as "don't <word>" / "without <word>ing"."""
    hits = [m.start() for m in re.finditer(r"\b" + re.escape(word), request)]
    if not hits:
        return False
    negated = {m.end() for m in re.finditer(_NEGATED + r"(?=" + re.escape(word) + ")", request)}
    return any(h not in negated for h in hits)


def _risky(text: str, request: str | None = None) -> str:
    """The first risky word in a command or label that the user's request did not ask for, or ''."""
    if request is None:
        from mint.app import live
        request = live.request() or ""
    words = " " + " ".join(re.findall(r"[a-z]+", (text or "").lower())) + " "
    request = " ".join(request.lower().replace("’", "'").split())
    for risky, asked in _RISKY.items():
        if f" {risky} " in words and not any(_asked(request, a) for a in asked):
            return risky
    return ""


# --- Paths ----------------------------------------------------------------------------------

_SECRET_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", ".config/gh",
                ".config/gcloud", "Library/Keychains", "Library/Cookies", "Library/Messages", "Library/Mail",
                "Library/Safari", "Library/Application Support/Google/Chrome", "Library/Application Support/Firefox",
                "Library/Application Support/BraveSoftware", "Library/Application Support/Arc",
                "Library/Application Support/Microsoft Edge", "Library/Application Support/com.apple.TCC",
                "Library/Group Containers", "Library/Containers/com.apple.mail",
                "Library/Application Support/AddressBook", "Library/Accounts")
_SECRET_NAMES = re.compile(
    r"^(\.env(\..+)?|.*\.pem|.*\.key|.*\.p12|.*\.pfx|.*\.keychain(-db)?|.*\.kdbx|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|"
    r"\.netrc|\.npmrc|\.pypirc|\.git-credentials|credentials(\.json)?|secrets?\.(json|ya?ml|toml|txt)|"
    r"service[-_]account.*\.json|bank\.json|voiceprint\.json)$", re.I)
_PLACES = {"desktop": HOME / "Desktop", "documents": HOME / "Documents", "downloads": HOME / "Downloads",
           "home": HOME, "pictures": HOME / "Pictures", "movies": HOME / "Movies", "music": HOME / "Music",
           "icloud": HOME / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
           "icloud drive": HOME / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
           }
_ROOTS = (HOME, Path("/tmp"), Path("/private/tmp"), Path("/Volumes"))


def _short(path: Path) -> str:
    text = str(path)
    return "~" + text[len(str(HOME)):] if text.startswith(str(HOME)) else text


def _blocked(path: Path, write: bool = False) -> str:
    """Why `path` is off limits, or ''."""
    try:
        real = path.resolve()
    except Exception:
        real = path
    if not any(real == root or root in real.parents for root in _ROOTS):
        return f"{_short(real)} is outside the home folder; Mint only works with files in the home folder."
    relative = str(real)[len(str(HOME)) + 1:] if HOME in real.parents else ""
    for secret in _SECRET_DIRS:
        if relative == secret or relative.startswith(secret + "/"):
            return f"{_short(real)} holds private keys, passwords or app data; Mint does not open it."
    if _SECRET_NAMES.match(real.name):
        return f"{real.name} looks like a key, password or credentials file; Mint does not open it."
    if write and relative:
        parts = relative.split("/")
        icloud = relative.startswith("Library/Mobile Documents/")
        if (parts[0] == "Library" and not icloud) or any(p.startswith(".") for p in parts[:-1]) \
                or (len(parts) == 1 and parts[0].startswith(".")):
            return (f"{_short(real)} is a system or hidden location; Mint only writes to normal folders "
                    "(Desktop, Documents, Downloads, projects...).")
    return ""


def _spotlight(name: str, limit: int = 8) -> list[Path]:
    try:
        done = subprocess.run(["mdfind", "-onlyin", str(HOME), "-name", name], capture_output=True,
                              text=True, timeout=6, check=False)
    except Exception:
        return []
    found = [Path(line) for line in done.stdout.splitlines() if line.strip()]
    found = [p for p in found if not _blocked(p) and "/Library/" not in str(p) and "/." not in str(p)]
    exact = [p for p in found if p.name.lower() == name.lower()]
    ranked = exact or found
    ranked.sort(key=lambda p: _mtime(p), reverse=True)
    return ranked[:limit]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _resolve(raw: str, must_exist: bool = True) -> tuple[Path | None, str]:
    """A path from what the model said: (path, '') or (None, why not)."""
    text = (raw or "").strip().strip("\"'`")
    if not text:
        return None, "No path given."
    if text.startswith("~"):
        path = Path(os.path.expanduser(text))
    elif text.startswith("/"):
        path = Path(text)
    else:
        first, _, rest = text.partition("/")
        from mint.core import config
        place = config.storage() if first.lower() == "mint" else _PLACES.get(first.lower())
        if place is not None:
            path = place / rest if rest else place
        else:
            candidates = [base / text for base in (config.storage("Documents"), config.storage(), HOME / "Desktop",
                                                   HOME / "Documents", HOME / "Downloads", HOME)]
            existing = [p for p in candidates if p.exists()]
            if existing:
                path = existing[0]
            elif not must_exist:
                path = config.storage("Documents") / text
            else:
                matches = _spotlight(Path(text).name)
                if len(matches) == 1:
                    path = matches[0]
                elif matches:
                    listing = "\n".join(f"- {_short(p)}" for p in matches)
                    return None, f"Several files match '{text}'; say which one (full path):\n{listing}"
                else:
                    return None, f"No file called '{text}' was found. Try find_files with part of the name."
    why = _blocked(path)
    if why:
        return None, why
    if must_exist and not path.exists():
        alike = _lookalikes(path)
        if alike:
            listing = "\n".join(f"- {_short(p)}{'/' if p.is_dir() else ''}  ({why})" for p, why in alike)
            single = (" Only one is close, so it is almost certainly a typo: use it and carry on, and tell the "
                      "user which file you used." if len(alike) == 1 else "")
            return None, (f"{_short(path)} does not exist. {_short(path.parent)} has files with similar names:\n"
                          f"{listing}\nUse the real name from this list (the name may differ from what a document "
                          "or spreadsheet calls it); if none of them is the one meant, ask the user." + single)
        near = _spotlight(path.name, 3)
        hint = (" Did you mean: " + ", ".join(_short(p) for p in near) + "?") if near else ""
        return None, f"{_short(path)} does not exist.{hint}"
    return path, ""


def _lookalikes(path: Path, limit: int = 6) -> list[tuple[Path, str]]:
    """Files next to where a missing `path` would be whose names are close to it (bench: Mint moved
    "INV-102.pdf" when the file was INV-102.txt): [(path, why)], best first."""
    import difflib
    folder = path.parent
    try:
        if not folder.is_dir() or _blocked(folder):
            return []
        siblings = [Path(e.path) for e in os.scandir(folder) if not e.name.startswith(".")]
    except OSError:
        return []
    stem, name = path.stem.lower(), path.name.lower()
    scored = []
    for p in siblings:
        other = p.stem.lower() if p.is_file() else p.name.lower()
        if other == stem and p.name.lower() != name:
            scored.append((3, p, f"same name, {p.suffix or 'no extension'}" if p.is_file() else "folder"))
        elif len(stem) >= 3 and (stem in other or (len(other) >= 3 and other in stem)):
            scored.append((2, p, "name contains it" if stem in other else "name is part of it"))
        else:
            ratio = difflib.SequenceMatcher(None, stem, other).ratio()
            if ratio >= 0.75:
                scored.append((1 + ratio / 10, p, "similar name"))
    scored.sort(key=lambda s: (-s[0], s[1].name.lower()))
    return [(p, why) for _, p, why in scored[:limit]]


def _size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _ago(stamp: float) -> str:
    """How long ago, in whole days up to two months ("9 days ago", never a vague "1 week ago": bench, Mint
    counted a 9-day-old note as "from the last 7 days")."""
    seconds = time.time() - stamp
    if seconds < 90:
        return "just now"
    for size, unit in ((86400 * 365, "year"), (86400 * 60, "month"), (86400, "day"), (3600, "hour"),
                       (60, "minute")):
        if seconds >= size:
            n = int(seconds // (86400 * 30 if unit == "month" else size))
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "just now"


def _when(stamp: float) -> str:
    """A modification time for listings: the calendar date and how long ago ("20 Sep 2026, 9 days ago"),
    so "saved in September" and "in the last 7 days" can be answered from the listing itself."""
    then = datetime.datetime.fromtimestamp(stamp)
    if then.date() == datetime.date.today():
        return f"today {then:%H:%M}"
    return f"{then.day} {then:%b %Y}, {_ago(stamp)}"


# --- Files ----------------------------------------------------------------------------------

_TEXTUTIL = {".docx", ".doc", ".rtf", ".rtfd", ".odt", ".html", ".htm", ".webarchive", ".wordml"}
_IMAGES = {".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp", ".tiff", ".bmp"}
_KINDS = {
    "pdf": {".pdf"},
    "image": _IMAGES | {".svg"},
    "doc": {".docx", ".doc", ".pages", ".rtf", ".txt", ".md", ".odt"},
    "text": {".txt", ".md", ".markdown", ".rtf", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".html", ".xml"},
    "sheet": {".xlsx", ".xls", ".csv", ".numbers", ".tsv"},
    "slides": {".pptx", ".ppt", ".key"},
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".go", ".rs", ".java", ".kt", ".c", ".cpp", ".h",
             ".rb", ".php", ".html", ".css", ".json", ".yaml", ".yml", ".toml", ".sh", ".sql"},
    "video": {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"},
    "audio": {".mp3", ".m4a", ".wav", ".aiff", ".flac", ".aac"},
    "app": {".app"},
}


def _text_of(path: Path) -> tuple[str, str]:
    """(text, note) for a file, or ('', why not)."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            import Quartz
            from Foundation import NSURL
            document = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(str(path)))
            if document is None:
                return "", "The PDF could not be opened."
            pages = document.pageCount()
            text = document.string() or ""
            if not text.strip():
                return "", (f"The PDF has {pages} pages but no text layer (a scan). Open it with file_action "
                            "and use look to read it.")
            return text, f"PDF, {pages} pages"
        except Exception as error:
            return "", f"Could not read the PDF: {error}"
    if suffix in _TEXTUTIL:
        done = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True,
                              text=True, timeout=20, check=False)
        if done.returncode == 0:
            return done.stdout, suffix[1:].upper() + " document"
        return "", f"Could not convert it to text: {done.stderr.strip()[:200]}"
    if suffix in _IMAGES:
        try:
            import AppKit
            image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(path))
            size = image.size() if image else None
            dims = f"{int(size.width)}x{int(size.height)} " if size else ""
        except Exception:
            dims = ""
        return "", (f"It is a {dims}image. To see it, open it (file_action open) and use look.")
    if suffix in {".xlsx", ".xlsm"}:
        return "", (f"{suffix[1:]} files cannot be read as text here. To read or change it, use edit_spreadsheet "
                    "with the path (only the path shows what is in it); don't type into Numbers or Excel.")
    if suffix in {".pages", ".numbers", ".key", ".xls", ".pptx"}:
        return "", (f"{suffix[1:]} files cannot be read as text here. Open it (file_action open), then use "
                    "read_window, or export it as PDF or CSV and read that.")
    try:
        with open(path, "rb") as handle:
            head = handle.read(8192)
        if b"\x00" in head:
            return "", f"It is a binary file ({_size(path.stat().st_size)}), not text."
        return path.read_text(encoding="utf-8", errors="replace"), ""
    except OSError as error:
        if error.errno == 1:
            return "", ("macOS did not let Mint read it. The user can allow Mint in System Settings > "
                        "Privacy & Security > Files and Folders.")
        return "", f"Could not read it: {error.strerror or error}"


def read_file(args: dict) -> str:
    path, why = _resolve(str(args.get("path", "")))
    if path is None:
        return f"FAILED: {why}"
    if path.is_dir():
        return find_files({"folder": str(path), "query": ""})
    text, note = _text_of(path)
    if not text:
        return f"FAILED: {_short(path)}: {note}" if not note.startswith("It is a") else f"{_short(path)}: {note}"
    stat = path.stat()
    lines = text.splitlines()
    start = max(1, int(args.get("start_line") or 1))
    limit = max(500, min(int(args.get("max_chars") or 8000), 30000))
    chosen, used, end = [], 0, start - 1
    for number in range(start - 1, len(lines)):
        line = lines[number]
        if used + len(line) + 1 > limit and chosen:
            break
        chosen.append(line)
        used += len(line) + 1
        end = number + 1
    header = (f"{_short(path)} ({_size(stat.st_size)}, modified {_when(stat.st_mtime)}"
              f"{', ' + note if note else ''}; {len(lines)} lines, showing {start}-{end})")
    more = f"\n[{len(lines) - end} more lines: read_file start_line={end + 1}]" if end < len(lines) else ""
    return header + "\n" + "\n".join(chosen) + more


# Words in the request that let overwrite drop lines a file already holds ("make plan.md" does not).
_REPLACE_ASKED = ("overwrite", "replace", "rewrite", "redo", "re-do", "start over", "from scratch", "fresh",
                  "clear", "wipe", "reset", "instead", "edit", "change", "update", "fix", "correct", "modify",
                  "remove", "delete", "rename", "reword", "rephrase", "tidy", "clean", "sort", "reorder",
                  "reorganize", "reorganise", "format", "shorten", "trim", "translate", "convert", "merge",
                  "yes", "go ahead", "shorter", "longer", "condense", "summari", "simplif", "tighten")
_WROTE: dict[str, float] = {}      # path -> its mtime right after Mint last wrote it


def _would_lose(path: Path, content: str) -> str:
    """Why overwriting `path` with `content` would lose lines the user did not ask to replace, or ''."""
    try:
        if _WROTE.get(str(path)) == path.stat().st_mtime:
            return ""                  # only what Mint wrote, unchanged since: redoing its own work is fine
        old = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    new = " ".join(content.split())
    lost = [" ".join(line.split()) for line in old.splitlines() if line.strip()]
    lost = [line for line in lost if line not in new]
    if not lost:
        return ""
    from mint.app import live
    request = " ".join(live.request().lower().replace("’", "'").split())
    if not request or any(_asked(request, word) for word in _REPLACE_ASKED):
        return ""
    return (f"FAILED: {_short(path)} already exists and overwriting would lose {len(lost)} line(s) the user "
            f"did not ask to replace (e.g. {lost[0][:80]!r}). Keep them: use mode=append to add below, or "
            "replace to change one part. If they might want the file replaced, ask them first.")


def _other_format(query: str, folder: Path | None) -> str:
    """Asked for scores.pdf, and only scores.csv is there: say so (bench: Mint found nothing, then gave up)."""
    name = Path(query.strip())
    if not name.suffix or len(name.suffix) > 6 or "/" in query:
        return ""
    places = [folder] if folder is not None else [HOME / d for d in ("Desktop", "Documents", "Downloads")]
    matches = []
    for place in places:
        if place is None or not place.is_dir():
            continue
        deadline = time.monotonic() + 2
        for root, dirs, files in os.walk(place):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", ".git", ".venv"}]
            matches += [Path(root) / f for f in files if Path(f).stem.lower() == name.stem.lower()
                        and Path(f).suffix.lower() != name.suffix.lower()]
            if time.monotonic() > deadline or len(matches) > 5:
                break
    if not matches:
        return ""
    listed = "\n".join(f"- {_short(p)}" for p in matches[:5])
    return (f"There is no {name.name}" + (f" in {_short(folder)}" if folder else "") + f", but the same name in "
            f"another format:\n{listed}\nIf there is only one, use it and tell the user you used that one; if "
            "there are several, ask which.")


def _asked_fresh() -> bool:
    """The user asked for a new, empty or replaced file ("a fresh plan.md", "start over", "replace it")."""
    from mint.app import live
    request = (live.request() or "").lower()
    return any(_asked(request, w) for w in ("overwrite", "replace", "start over", "from scratch", "fresh",
                                             "new file", "wipe", "instead of"))


def _meant_existing(path: Path) -> str:
    """The user asked to add to a file that isn't there, while a file with a similar name is ("add a line to
    agenda.md" when only agenda-draft.md exists): ask, don't quietly start a new file (bench: Mint made an
    empty agenda.md, then lost track of which one it was editing)."""
    from mint.app import live
    request = " ".join((live.request() or "").lower().split())
    if not request or path.name.lower() not in request:
        return ""
    if not re.search(r"\b(add|append|insert|put)\b|\bend of\b|\bbottom of\b|\bto the\b", request) or \
            re.search(r"\b(create|make|new file|start a)\b", request):
        return ""
    import difflib
    stem = path.stem.lower()
    try:
        siblings = [p for p in path.parent.iterdir() if p.is_file() and not p.name.startswith(".")]
    except OSError:
        return ""
    close = [p for p in siblings if stem in p.stem.lower() or p.stem.lower() in stem
             or difflib.SequenceMatcher(None, stem, p.stem.lower()).ratio() >= 0.75]
    if not close:
        return ""
    names = ", ".join(p.name for p in close[:4])
    return (f"FAILED: there is no {path.name} in {_short(path.parent)}, but there is {names}. The user asked to add "
            "to an existing file: ask them whether they meant that one (don't create a new file).")


def write_file(args: dict) -> str:
    from mint.knowledge.skills import has_secret

    path, why = _resolve(str(args.get("path", "")), must_exist=False)
    if path is None:
        return f"FAILED: {why}"
    why = _blocked(path, write=True)
    if why:
        return f"FAILED: {why}"
    content = str(args.get("content", ""))
    mode = str(args.get("mode") or "create").lower()
    if has_secret(content):
        return ("REFUSED: the text looks like it holds a password, key or card number. Mint does not write "
                "secrets into files; the user can paste it in themselves.")
    if mode == "replace" and path.suffix.lower() in RICH_EDIT:
        return _replace_rich(path, str(args.get("find") or ""), content)
    if path.suffix.lower() in {".pdf", ".docx", ".pages", ".xlsx", ".numbers", ".key", ".pptx", ".png", ".jpg"}:
        return (f"FAILED: write_file writes plain text; {path.suffix} is not. For a PDF use create_pdf; "
                "for other documents write .md, .txt, .csv or .html.")
    if path.is_dir():
        return f"FAILED: {_short(path)} is a folder. Give a file name inside it."
    added_note = ""
    if mode == "create" and path.exists() and path.is_file() and not _asked_fresh():
        # "Make plan.md with a checklist" when plan.md is already there: add it below, keep what was there,
        # and say so (undo takes it out). Stopping to ask left the checklist unmade (bench err-write-conflict).
        shown = _preview(path, 80).strip()
        old = path.read_text(encoding="utf-8", errors="replace")
        if content.strip() and content.strip() in old:
            return f"{_short(path)} already has that text; nothing was changed."
        mode = "append"
        added_note = (f" {_short(path)} already existed{', starting ' + shown if shown else ''}, so the new text "
                      "was added below what was there (nothing lost). Tell the user that; 'undo that' takes it out.")
    exists = path.exists()
    size_before = path.stat().st_size if exists else 0
    mine = not exists or _WROTE.get(str(path)) == path.stat().st_mtime     # holds only text Mint wrote
    if not exists and mode in {"create", "append"}:
        lookalike = _meant_existing(path)
        if lookalike:
            return lookalike
    backup, copy = "", None
    if exists and mode == "overwrite":
        why = _would_lose(path, content)
        if why:
            return why
    if exists and mode in {"overwrite", "replace"}:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        copy = BACKUPS / f"{stamp}-{path.name}"
        shutil.copy2(path, copy)
        backup = f" The previous version is saved at {_short(copy)}."
    try:
        if mode == "create":
            if exists:
                shown = _preview(path, 160).strip()
                return (f"FAILED: {_short(path)} already exists ({_size(size_before)}"
                        f"{', starting ' + shown if shown else ''}). "
                        "Don't lose what is in it: add the new text with mode=append (it keeps what is there) and "
                        "tell the user you added it below the existing text, or use replace to change one part. "
                        "Overwrite only if the user asked for a fresh file.")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            verb = "Created"
        elif mode == "overwrite":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            verb = "Overwrote" if exists else "Created"
        elif mode == "append":
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                if exists and path.stat().st_size:
                    ends = path.read_text(encoding="utf-8", errors="replace").endswith("\n")
                    content = ("" if ends else "\n") + content.lstrip("\n")
                handle.write(content)
            verb = "Appended to" if exists else "Created"
        elif mode == "replace":
            if not exists:
                return f"FAILED: {_short(path)} does not exist, so there is nothing to replace."
            find = str(args.get("find") or "")
            if not find:
                return "FAILED: mode=replace needs `find`, the exact text to replace."
            old = path.read_text(encoding="utf-8", errors="replace")
            count = old.count(find)
            if count == 0:
                return (f"FAILED: that text is not in {path.name}. read_file it and copy the exact text "
                        "(spaces and line breaks included).")
            if count > 1:
                return f"FAILED: that text occurs {count} times in {path.name}; include more around it so it is unique."
            path.write_text(old.replace(find, content, 1), encoding="utf-8")
            verb = "Edited"
        else:
            return "FAILED: mode must be create, overwrite, append or replace."
    except OSError as error:
        return f"FAILED: could not write {_short(path)}: {error.strerror or error}"
    _remember_made(path)
    from mint.tools import undo
    stat = path.stat()
    if mine and verb in {"Created", "Overwrote"}:
        _WROTE[str(path)] = stat.st_mtime
    else:
        _WROTE.pop(str(path), None)             # the user's text is (still) in it
    doing = {"Created": "creating", "Overwrote": "overwriting", "Appended to": "appending to", "Edited": "editing"}
    undo.record("file", f"{doing.get(verb, verb.lower())} {_short(path)}",
                {"kind": "file_trash", "path": str(path), "mtime": stat.st_mtime} if not exists else
                {"kind": "file_restore", "path": str(path), "backup": str(copy), "mtime": stat.st_mtime} if copy else
                {"kind": "file_truncate", "path": str(path), "size": size_before, "after": stat.st_size})
    return f"{verb} {_short(path)} ({_size(path.stat().st_size)}).{backup}{added_note}"


RICH_EDIT = {".rtf", ".rtfd"}          # write_file mode=replace edits these through the Cocoa text system


def _replace_rich(path: Path, find: str, content: str) -> str:
    """mode=replace in an .rtf/.rtfd document. The text is changed through the Cocoa text system (as TextEdit
    saves it), so fonts, styles and pictures stay - a raw edit of the RTF source breaks on escapes and style
    runs. A TextEdit window showing the file is reloaded, so it shows the change and nothing needs saving."""
    import AppKit
    from Foundation import NSURL
    if not path.exists():
        return f"FAILED: {_short(path)} does not exist, so there is nothing to replace."
    if not find:
        return "FAILED: mode=replace needs `find`, the exact text to replace."
    if not os.access(path / "TXT.rtf" if path.is_dir() else path, os.W_OK):
        return f"FAILED: could not write {_short(path)}: it is read-only (locked)."
    open_in = _textedit_docs(path)
    if any(modified for _, modified in open_in):
        return (f"FAILED: {path.name} is open in TextEdit with unsaved changes, and changing the file would clash "
                "with them, so nothing was changed. Save or close it in TextEdit first, or make the change there "
                "(menu Edit > Find > Find and Replace…, then File > Save).")
    url = NSURL.fileURLWithPath_(str(path))
    text, attrs, error = AppKit.NSMutableAttributedString.alloc().initWithURL_options_documentAttributes_error_(
        url, {}, None, None)
    if text is None:
        return f"FAILED: could not read {path.name} as a rich text document ({error})."
    count = str(text.string()).count(find)
    if count == 0:
        return (f"FAILED: that text is not in {path.name}. read_file it and copy the exact text "
                "(spaces and line breaks included).")
    if count > 1:
        return f"FAILED: that text occurs {count} times in {path.name}; include more around it so it is unique."
    text.replaceCharactersInRange_withString_(text.string().rangeOfString_(find), content)
    rtfd = path.suffix.lower() == ".rtfd"
    keep = dict(attrs or {})
    keep[AppKit.NSDocumentTypeDocumentAttribute] = AppKit.NSRTFDTextDocumentType if rtfd else AppKit.NSRTFTextDocumentType
    whole = (0, text.length())
    BACKUPS.mkdir(parents=True, exist_ok=True)
    copy = BACKUPS / f"{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}-{path.name}"
    try:
        if rtfd:
            wrapper, error = text.fileWrapperFromRange_documentAttributes_error_(whole, keep, None)
            shutil.copytree(path, copy, dirs_exist_ok=True)
            done = wrapper is not None and wrapper.writeToURL_options_originalContentsURL_error_(url, 1, None, None)[0]
        else:
            data, error = text.dataFromRange_documentAttributes_error_(whole, keep, None)
            shutil.copy2(path, copy)
            done = data is not None
            if done:
                path.write_bytes(bytes(data))           # in place: keeps its permissions, tags and identity
    except OSError as err:
        return f"FAILED: could not write {_short(path)}: {err.strerror or err}"
    if not done:
        return f"FAILED: could not write {_short(path)} ({error})."
    _WROTE.pop(str(path), None)
    _remember_made(path)
    from mint.tools import undo
    if rtfd:
        undo.record("file", f"editing {_short(path)}", None,
                    f"it is an .rtfd package; its previous version is at {_short(copy)}.")
    else:
        undo.record("file", f"editing {_short(path)}",
                    {"kind": "file_restore", "path": str(path), "backup": str(copy), "mtime": path.stat().st_mtime})
    shown = ""
    if open_in:
        shown = (" TextEdit had it open, so its window was reloaded and shows the change (nothing to save there)."
                 if _textedit_reload(path, open_in) else
                 " TextEdit has it open and may still show the old text; close and reopen it there.")
    return (f"Edited {_short(path)}: '{find[:60]}' is now '{content[:60]}', formatting kept. The previous version "
            f"is saved at {_short(copy)}.{shown}")


def _textedit_docs(path: Path) -> list[tuple[str, bool]]:
    """(TextEdit's path, has unsaved changes) of each TextEdit document open on `path`; [] when TextEdit is not
    running (it is never started for this)."""
    # (pgrep, not NSRunningApplication: that list only refreshes with a run loop, so it can miss TextEdit.)
    if subprocess.run(["pgrep", "-xq", "TextEdit"], check=False).returncode != 0:
        return []
    ok, out = _osascript('tell application "TextEdit"\nset out to ""\nrepeat with d in documents\ntry\n'
                         'set out to out & (path of d) & tab & (modified of d as text) & linefeed\n'
                         'end try\nend repeat\nreturn out\nend tell', 8)
    if not ok:
        return []
    real = os.path.realpath(path)
    found = []
    for line in out.splitlines():
        where, _, modified = line.rpartition("\t")
        if where and os.path.realpath(where) == real:
            found.append((where, modified.strip() == "true"))
    return found


def _as_applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _textedit_reload(path: Path, open_in: list[tuple[str, bool]], front: bool = False) -> bool:
    """Close TextEdit's (unchanged) windows on `path` and open the file again, so they show what is on disk."""
    for where in {w for w, modified in open_in if not modified}:
        ok, _ = _osascript(f'tell application "TextEdit" to close (every document whose path is '
                           f'{_as_applescript(where)}) saving no', 8)
        if not ok:
            return False
    done = subprocess.run(["open", "-a", "TextEdit", *([] if front else ["-g"]), str(path)], capture_output=True,
                          timeout=15, check=False)
    return done.returncode == 0


_TEXTEDIT_KINDS = {".rtf", ".rtfd", ".txt", ".text", ".md", ".markdown", ".html", ".htm", ".doc", ".docx", ".odt",
                   ".xml", ".csv", ".log", ".json", ".webarchive"}


def _textedit_stale(path: Path) -> str:
    """Before opening a file: TextEdit may still have a window on it from before the file changed (it does not
    reload a file that was replaced), and "open" would only bring that old window forward. An unchanged one is
    closed so the file opens afresh; one with unsaved changes is left alone and mentioned."""
    if path.suffix.lower() not in _TEXTEDIT_KINDS:
        return ""
    open_in = _textedit_docs(path)
    if not open_in:
        return ""
    if any(modified for _, modified in open_in):
        return (" Note: TextEdit already had it open with unsaved changes, so that window came forward; what it "
                "shows may differ from the file on disk.")
    try:
        import AppKit
        from Foundation import NSURL
        text, _, _ = AppKit.NSAttributedString.alloc().initWithURL_options_documentAttributes_error_(
            NSURL.fileURLWithPath_(str(path)), {}, None, None)
        on_disk = str(text.string()) if text is not None else None
        ok, shown = _osascript(f'tell application "TextEdit" to get text of (first document whose path is '
                               f'{_as_applescript(open_in[0][0])})', 8)
    except Exception as error:
        log.info("textedit stale check: %s", error)
        return ""
    if on_disk is None or not ok or " ".join(shown.split()) == " ".join(on_disk.split()):
        return ""
    for where in {w for w, _ in open_in}:
        _osascript(f'tell application "TextEdit" to close (every document whose path is {_as_applescript(where)}) '
                   'saving no', 8)
    return " (TextEdit still had an old copy of it open from before the file changed; that window was replaced.)"


_JUNK = re.compile(r"/(node_modules|build|dist|out|target|\.venv|venv|env|site-packages|__pycache__|\.git|"
                   r"\.next|\.cache|cache|Caches|DerivedData|Pods|vendor|coverage|\.idea|\.vscode|logs?)/|"
                   r"\.app/|/Library/|\.(pyc|map|lock|log|tmp|o|class)$|\.bundle\.js$|\.min\.(js|css)$")
_DOCS = _KINDS["doc"] | _KINDS["pdf"] | _KINDS["sheet"] | _KINDS["slides"] | {".html", ".json", ".txt"}
MADE = BACKUPS.parent / "made_files.json"


def _remember_made(path: Path) -> None:
    """Files Mint wrote, newest first: "my grocery list" is often one of them."""
    try:
        made = json.loads(MADE.read_text()) if MADE.exists() else []
    except Exception:
        made = []
    made = [str(path)] + [m for m in made if m != str(path)]
    MADE.parent.mkdir(parents=True, exist_ok=True)
    MADE.write_text(json.dumps(made[:50]))


def _made() -> list[Path]:
    try:
        return [Path(m) for m in json.loads(MADE.read_text()) if Path(m).exists()]
    except Exception:
        return []


def _place_score(path: Path) -> int:
    """User places first: Mint's own files, then Desktop / Documents / Downloads / iCloud top levels."""
    text = str(path)
    from mint.core import config
    if text.startswith(str(config.storage())) or text.startswith(str(MINT_FILES)):
        return 3
    for base in (HOME / "Desktop", HOME / "Documents", HOME / "Downloads",
                 HOME / "Library" / "Mobile Documents" / "com~apple~CloudDocs"):
        if path.parent == base:
            return 2
        if text.startswith(str(base) + "/") and text.count("/") - str(base).count("/") <= 3:
            return 1
    return 0


def _preview(path: Path, chars: int = 90) -> str:
    if path.suffix.lower() not in {".md", ".txt", ".csv", ".json", ".html", ".py", ".js", ".yaml", ".yml"}:
        return ""
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            head = handle.read(600)
    except OSError:
        return ""
    head = " ".join(re.sub(r"<[^>]+>", " ", head).split())
    return f'  "{head[:chars]}{"…" if len(head) > chars else ""}"' if head else ""


def _listing(paths: list[Path], limit: int, preview: bool = False) -> str:
    rows = []
    for p in paths[:limit]:
        try:
            stat = p.stat()
        except OSError:
            continue
        if p.is_dir() and p.suffix != ".app":
            rows.append(f"- {_short(p)}/  (folder, modified {_when(stat.st_mtime)})")
        else:
            rows.append(f"- {_short(p)}  ({_size(stat.st_size)}, modified {_when(stat.st_mtime)})"
                        + (_preview(p) if preview else ""))
    return "\n".join(rows)


def _kind_ok(path: Path, kind: str) -> bool:
    if kind in ("", "any"):
        return True
    if kind == "folder":
        return path.is_dir() and path.suffix != ".app"
    return path.suffix.lower() in _KINDS.get(kind, set())


def _contains(path: Path, text: str) -> bool:
    try:
        return path.is_file() and path.stat().st_size < 2_000_000 and \
            text.lower() in path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _day(text: str, end: bool) -> float | None:
    """'2026-09-12' / '2026-09' (a month) / '12 Sep 2026' -> the start (or, with end, the end) of that day or month."""
    text = text.strip()
    month = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    try:
        if month:
            year, number = int(month.group(1)), int(month.group(2))
            first = datetime.datetime(year, number, 1)
            if not end:
                return first.timestamp()
            nxt = datetime.datetime(year + (number == 12), number % 12 + 1, 1)
            return nxt.timestamp() - 0.001
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%Y/%m/%d"):
            try:
                day = datetime.datetime.strptime(text.replace(",", ""), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    except ValueError:
        return None
    return (day + datetime.timedelta(days=1)).timestamp() - 0.001 if end else day.timestamp()


def _dates(args: dict) -> tuple[float | None, float | None, str, str]:
    """(lowest mtime, highest mtime, what the filter says, error) from days / modified_from / modified_to."""
    low = high = None
    said = []
    try:
        days = float(args.get("days") or 0)
    except (TypeError, ValueError):
        return None, None, "", "days must be a number."
    if days > 0:
        low = time.time() - days * 86400
        said.append(f"modified in the last {days:g} day{'s' if days != 1 else ''} "
                    f"(since {datetime.datetime.fromtimestamp(low):%-d %b %Y %H:%M})")
    for key, end in (("modified_from", False), ("modified_to", True)):
        raw = str(args.get(key) or "").strip()
        if not raw:
            continue
        stamp = _day(raw, end)
        if stamp is None:
            return None, None, "", f"{key} '{raw}' is not a date; use YYYY-MM-DD or YYYY-MM."
        if end:
            high = stamp if high is None else min(high, stamp)
            said.append(f"modified on or before {datetime.datetime.fromtimestamp(stamp):%-d %b %Y}")
        else:
            low = stamp if low is None else max(low, stamp)
            said.append(f"modified on or after {datetime.datetime.fromtimestamp(stamp):%-d %b %Y}")
    return low, high, " and ".join(said), ""


_SORTS = {"newest": (lambda p: _mtime(p), True), "oldest": (lambda p: _mtime(p), False),
          "biggest": (lambda p: _bytes(p), True), "smallest": (lambda p: _bytes(p), False),
          "name": (lambda p: p.name.lower(), False)}


def _bytes(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def find_files(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    kind = str(args.get("kind") or "any").lower()
    limit = max(1, min(int(args.get("limit") or 20), 60))
    folder_arg = str(args.get("folder") or "").strip()
    folder = None
    if folder_arg:
        folder, why = _resolve(folder_arg)
        if folder is None:
            return f"FAILED: {why}"
        if not folder.is_dir():
            return f"FAILED: {_short(folder)} is a file, not a folder."
    low, high, dated, why = _dates(args)
    if why:
        return f"FAILED: {why}"
    order = str(args.get("sort") or "newest").lower()
    if order not in _SORTS:
        order = "newest"
    if dated or order != "newest":
        return _find_filtered(args, query, kind, limit, folder, low, high, dated, order)

    if not query:
        if folder is None:
            made = [p for p in _made() if _kind_ok(p, kind)][:8]
            found: list[Path] = []
            try:
                lines = subprocess.run(
                    ["mdfind", "-onlyin", str(HOME),
                     'kMDItemFSContentChangeDate >= $time.today(-14) && kMDItemContentTypeTree == "public.content"'],
                    capture_output=True, text=True, timeout=8, check=False).stdout.splitlines()
                found = [Path(n) for n in lines if n.strip()]
            except subprocess.TimeoutExpired:
                pass
            found = [p for p in found if not _JUNK.search(str(p)) and "/." not in str(p)[len(str(HOME)):]
                     and not _blocked(p) and _kind_ok(p, kind) and p not in made
                     and (_place_score(p) > 0 or p.suffix.lower() in _DOCS)]
            found.sort(key=lambda p: (_place_score(p) > 0, _mtime(p)), reverse=True)
            parts = []
            if made:
                parts.append("Made by Mint recently (with how they start):\n" + _listing(made, 8, preview=True))
            if found:
                parts.append(f"Your recently changed documents ({len(found)}), newest first:\n" + _listing(found, limit))
            return "\n".join(parts) or "No documents changed in the last two weeks."
        try:
            entries = [Path(e.path) for e in os.scandir(folder) if not e.name.startswith(".")]
        except OSError as error:
            return f"FAILED: could not list {_short(folder)}: {error.strerror or error}"
        chosen = [p for p in entries if _kind_ok(p, kind)]
        chosen.sort(key=_mtime, reverse=True)
        more = f"\n({len(chosen) - limit} more not shown)" if len(chosen) > limit else ""
        if chosen:
            return f"{_short(folder)}: {len(chosen)} items, newest first:\n" + _listing(chosen, limit) + more
        return f"{_short(folder)} is empty" + (f" of {kind} files{_others(entries)}" if kind != "any" else ".")

    found = _search_paths(query, folder, kind, limit, args.get("content", True) is not False)
    needle = query.lower()
    other = _other_format(query, folder) if not any(p.name.lower() == needle for p in found) else ""
    if other:
        return other
    if not found:
        return f"Nothing found for '{query}'" + (f" in {_short(folder)}" if folder else "") + \
            ". Try a shorter part of the name, or another folder."
    return f"{len(found)} found for '{query}' (name matches first, newest first):\n" + _listing(found, limit)


def _others(entries: list[Path]) -> str:
    """'(it has 7 other files: .md, .txt)' when a kind filter hid everything in a folder."""
    if not entries:
        return "."
    kinds = sorted({p.suffix.lower() or ("folder" if p.is_dir() else "no extension") for p in entries})
    return f" (it has {len(entries)} other item(s): {', '.join(kinds[:8])} - list it without kind to see them)."


def _find_filtered(args: dict, query: str, kind: str, limit: int, folder: Path | None, low, high,
                   dated: str, order: str) -> str:
    """find_files with a date range and/or another order (oldest, biggest...)."""
    where = _short(folder) if folder is not None else "your documents"
    if query:
        found = _search_paths(query, folder, kind, limit * 3, args.get("content", True) is not False)
        what = f"'{query}' in {where}"
    elif folder is not None:
        try:
            found = [Path(e.path) for e in os.scandir(folder) if not e.name.startswith(".")]
        except OSError as error:
            return f"FAILED: could not list {_short(folder)}: {error.strerror or error}"
        found = [p for p in found if _kind_ok(p, kind)]
        what = where + (f" ({kind} files)" if kind not in ("", "any") else "")
    else:
        back = max(1, min(366, int((time.time() - low) // 86400) + 1)) if low else 14
        try:
            lines = subprocess.run(
                ["mdfind", "-onlyin", str(HOME),
                 f'kMDItemFSContentChangeDate >= $time.today(-{back}) && kMDItemContentTypeTree == "public.content"'],
                capture_output=True, text=True, timeout=8, check=False).stdout.splitlines()
        except subprocess.TimeoutExpired:
            lines = []
        found = [Path(n) for n in lines if n.strip()]
        found = [p for p in found if not _JUNK.search(str(p)) and "/." not in str(p)[len(str(HOME)):]
                 and not _blocked(p) and _kind_ok(p, kind) and (_place_score(p) > 0 or p.suffix.lower() in _DOCS)]
        what = "your documents"
    total = len(found)
    if low is not None:
        found = [p for p in found if _mtime(p) >= low]
    if high is not None:
        found = [p for p in found if _mtime(p) <= high]
    key, backwards = _SORTS[order]
    found.sort(key=key, reverse=backwards)
    ordered = {"newest": "newest first", "oldest": "oldest first", "biggest": "biggest first",
               "smallest": "smallest first", "name": "by name"}[order]
    if not found:
        return (f"Nothing in {what} is {dated}" if dated else f"Nothing found in {what}") + \
            (f" ({total} other item(s) are outside that range)." if total else ".")
    more = f"\n({len(found) - limit} more not shown)" if len(found) > limit else ""
    left_out = f"; {total - len(found)} other item(s) left out by the date filter" if dated and total > len(found) else ""
    return (f"{what}: {len(found)} item(s){' ' + dated if dated else ''}{left_out}, {ordered}:\n"
            + _listing(found, limit) + more)


def _search_paths(query: str, folder: Path | None, kind: str, limit: int, content: bool = True) -> list[Path]:
    """Spotlight (then a short walk) for `query` by name and, with content, inside files; best first."""
    scope = str(folder or HOME)
    found: list[Path] = []
    try:
        names = subprocess.run(["mdfind", "-onlyin", scope, "-name", query], capture_output=True, text=True,
                               timeout=8, check=False).stdout.splitlines()
        found = [Path(n) for n in names if n.strip()]
        if content and len(found) < limit:
            inside = subprocess.run(["mdfind", "-onlyin", scope, query], capture_output=True, text=True,
                                    timeout=8, check=False).stdout.splitlines()
            known = set(map(str, found))
            found += [Path(n) for n in inside if n.strip() and n not in known]
    except subprocess.TimeoutExpired:
        pass
    if not found and folder is not None:
        # Spotlight may not index this folder: walk it, briefly.
        deadline, needle = time.monotonic() + 3, query.lower()
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", ".git", ".venv"}]
            for name in dirs + files:
                if needle in name.lower():
                    found.append(Path(root) / name)
            if time.monotonic() > deadline or len(found) > limit * 3:
                break
    found += [p for p in _made() if query.lower() in p.name.lower() or _contains(p, query)]
    unique: dict[str, Path] = {}
    for p in found:
        unique.setdefault(str(p), p)
    found = [p for p in unique.values() if not _blocked(p) and not _JUNK.search(str(p))
             and "/." not in str(p)[len(str(HOME)):] and _kind_ok(p, kind)]
    from mint.app import live
    asked = (live.request() or "").lower()
    if re.search(r"\bmarkdown\b", asked) and not re.search(r"\b(text|txt) files?\b", asked):
        # "the Markdown notes": .md only (bench, a .txt note slipped into a Markdown digest).
        found = [p for p in found if p.is_dir() or p.suffix.lower() in (".md", ".markdown")]
    needle = query.lower()
    # Name matches, then documents in the user's own places, then the rest; newest first within each.
    found.sort(key=lambda p: (needle in p.name.lower(), p.suffix.lower() in _DOCS or p.is_dir(),
                              _place_score(p), _mtime(p)), reverse=True)
    return found


def file_action(args: dict) -> str:
    import AppKit
    from Foundation import NSURL

    action = str(args.get("action", "")).lower()
    target = str(args.get("to") or "").strip()
    if action == "make_folder":
        path, why = _resolve(str(args.get("path", "")), must_exist=False)
        if path is None:
            return f"FAILED: {why}"
        why = _blocked(path, write=True)
        if why:
            return f"FAILED: {why}"
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        if not existed:
            from mint.tools import undo
            undo.record("file", f"making the folder {_short(path)}", {"kind": "folder_remove", "path": str(path)})
        return f"Folder ready: {_short(path)}"
    path, why = _resolve(str(args.get("path", "")))
    if path is None:
        return f"FAILED: {why}"
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    if action == "open":
        stale = _textedit_stale(path)
        ok = workspace.openURL_(NSURL.fileURLWithPath_(str(path)))
        if not ok:
            return f"FAILED: macOS could not open {_short(path)}."
        hint = (" To change words in it, write_file mode=replace (find = the old words) edits the file itself, "
                "keeping its formatting; a TextEdit window on it is reloaded to show the change, with nothing to "
                "save." if path.suffix.lower() in RICH_EDIT and "unsaved changes" not in stale else "")
        return f"Opened {_short(path)}.{stale}{hint}"
    if action == "reveal":
        workspace.activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(str(path))])
        return f"Showing {path.name} in Finder."
    if action == "info":
        stat = path.stat()
        kind = subprocess.run(["mdls", "-raw", "-name", "kMDItemKind", str(path)], capture_output=True,
                              text=True, timeout=4, check=False).stdout.strip()
        created = datetime.datetime.fromtimestamp(stat.st_birthtime).strftime("%d %b %Y %H:%M")
        changed = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%d %b %Y %H:%M")
        size = _size(stat.st_size)
        if path.is_dir():
            count = sum(1 for _ in os.scandir(path))
            size = f"{count} items"
        return f"{_short(path)}: {kind or 'item'}, {size}, created {created}, modified {changed}."
    if action == "trash":
        if _risky("delete"):
            return ("REFUSED: the user did not ask to delete anything. Only move files to the Trash when they "
                    "ask; confirm with them first if unsure.")
        why = _blocked(path, write=True)
        if why:
            return f"FAILED: {why}"
        manager = AppKit.NSFileManager.defaultManager()
        ok, trashed, error = manager.trashItemAtURL_resultingItemURL_error_(NSURL.fileURLWithPath_(str(path)), None, None)
        if ok and trashed is not None:
            from mint.tools import undo
            undo.record("file", f"moving {_short(path)} to the Trash",
                        {"kind": "file_untrash", "trashed": str(trashed.path()), "to": str(path)})
        return f"Moved {_short(path)} to the Trash (it can be put back from there)." if ok else \
            f"FAILED: could not move it to the Trash: {error}"
    if action in {"move", "copy", "rename"}:
        if not target:
            return f"FAILED: {action} needs `to`."
        why = _blocked(path, write=True) if action != "copy" else ""
        if why:
            return f"FAILED: {why}"
        if action == "rename":
            if "/" in target:
                return "FAILED: for rename, `to` is just the new name; use move to change folders."
            destination = path.with_name(target)
        else:
            base, why = _resolve(target, must_exist=False)
            if base is None:
                return f"FAILED: {why}"
            # "to" names a folder when it is one, ends in "/", or is a new name
            # without an extension while the file has one ("move it to Archive").
            as_folder = base.is_dir() or target.endswith("/") or (
                not base.exists() and not base.suffix and bool(path.suffix))
            destination = base / path.name if as_folder else base
        why = _blocked(destination, write=True)
        if why:
            return f"FAILED: {why}"
        if destination.exists():
            return f"FAILED: {_short(destination)} already exists; Mint will not overwrite it. Pick another name."
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if action == "copy":
                (shutil.copytree if path.is_dir() else shutil.copy2)(path, destination)
            else:
                shutil.move(str(path), str(destination))
        except OSError as error:
            return f"FAILED: {error.strerror or error}"
        if action == "copy" and destination.is_file():
            _WROTE[str(destination)] = destination.stat().st_mtime   # Mint's own copy: rewriting it loses nothing
        from mint.tools import undo
        undo.record("file", f"{dict(copy='copying', move='moving', rename='renaming')[action]} {_short(path)} -> {_short(destination)}",
                    {"kind": "file_trash", "path": str(destination), "mtime": destination.stat().st_mtime}
                    if action == "copy" else {"kind": "file_move", "from": str(destination), "to": str(path)})
        return f"{action.capitalize()}d {_short(path)} -> {_short(destination)}." if action != "copy" else \
            f"Copied {_short(path)} -> {_short(destination)}."
    return "FAILED: action must be open, reveal, info, move, copy, rename, make_folder or trash."


# --- Web ------------------------------------------------------------------------------------

def web_search(args: dict) -> str:
    from mint.agents import tools as agent_tools

    query = str(args.get("query", "")).strip()
    if not query:
        return "FAILED: no query."
    try:
        return agent_tools.web_search(query, max(1, min(int(args.get("max_results") or 6), 10)))
    except Exception as error:
        return f"FAILED: web search did not work: {error}"


def read_url(args: dict) -> str:
    from mint.ui.activity import _site
    from mint.agents import tools as agent_tools

    url = str(args.get("url", "")).strip()
    if not url:
        return "FAILED: no url."
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        return agent_tools.fetch_url(url, max(1000, min(int(args.get("max_chars") or 8000), 30000)))
    except Exception as error:
        return f"FAILED: could not fetch {url}: {error}"


# --- AppleScript ----------------------------------------------------------------------------

def _osascript(script: str, timeout: float = 20.0) -> tuple[bool, str]:
    try:
        done = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s"
    if done.returncode != 0:
        return False, (done.stderr or "").strip()
    return True, (done.stdout or "").rstrip("\n")


_NEVER = re.compile(r"\b(do\s+shell\s+script|run\s+script|load\s+script|store\s+script|"
                    r"keychain|password|call\s+method|ObjC\.|current\s+application's|use\s+framework)", re.I)


def run_applescript(args: dict) -> str:
    script = str(args.get("script", ""))
    if not script.strip():
        return "FAILED: no script."
    if _NEVER.search(script):
        return ("REFUSED: scripts that run shell commands, other scripts, Objective-C, or touch passwords are "
                "not allowed. Use the dedicated tools instead.")
    risky = _risky(script)
    if risky:
        return (f"REFUSED: the script would '{risky}', which the user did not ask for. Ask them first, or "
                "leave that step to them.")
    ok, output = _osascript(script, timeout=30)
    if not ok:
        if "-1743" in output or "Not authorized" in output:
            return ("FAILED: macOS has not allowed Mint to control that app. The user can allow it in System "
                    "Settings > Privacy & Security > Automation > Mint.")
        return f"FAILED: {output[:600]}"
    output = output.strip() or "(done, no result)"
    return output[:4000] + (" … (truncated)" if len(output) > 4000 else "")


# --- Accessibility search -------------------------------------------------------------------

def _ax():
    import ApplicationServices as AX
    return AX


def _attr(element, name):
    from mint.screen.axkit import attr
    return attr(element, name)


def _frame(element):
    from mint.screen.axkit import frame
    return frame(element)


def _label(element) -> str:
    parts = []
    for name in ("AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue", "AXHelp"):
        value = _attr(element, name)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    title_element = _attr(element, "AXTitleUIElement")
    if title_element is not None:
        value = _attr(title_element, "AXValue") or _attr(title_element, "AXTitle")
        if isinstance(value, str):
            parts.append(value)
    unique = []
    for part in parts:                           # a title and description are often the same words
        part = " ".join(part.split())
        if part and part.lower() not in (u.lower() for u in unique):
            unique.append(part)
    return " ".join(unique)


def _search(root, text: str = "", key: str = "AXAnyTypeSearchKey", limit: int = 40):
    """Elements under `root` matching `text`, via the AX search predicate (web areas support it;
    it also finds rows of long lists that are not drawn yet). None when unsupported."""
    AX = _ax()
    predicate = {"AXSearchKey": key, "AXVisibleOnly": False, "AXResultsLimit": limit,
                 "AXDirection": "AXDirectionNext", "AXImmediateDescendantsOnly": False}
    if text:
        predicate["AXSearchText"] = text
    try:
        err, values = AX.AXUIElementCopyParameterizedAttributeValue(
            root, "AXUIElementsForSearchPredicate", predicate, None)
    except Exception:
        return None
    if err != 0 or values is None:
        return None
    return list(values)


def _walk_find(root, text: str, limit: int = 5000, seconds: float = 2.5) -> list:
    """Elements whose label contains `text`, breadth first (for native apps)."""
    needle = text.lower().strip()
    found, queue, seen = [], deque([root]), 0
    deadline = time.monotonic() + seconds
    while queue and seen < limit and time.monotonic() < deadline:
        node = queue.popleft()
        seen += 1
        label = _label(node).lower()
        if needle and needle in label:
            found.append(node)
        queue.extend(_attr(node, "AXChildren") or [])
    return found


_PRESSABLE = {"AXButton", "AXLink", "AXMenuItem", "AXMenuButton", "AXPopUpButton", "AXCheckBox",
              "AXRadioButton", "AXTab", "AXCell", "AXRow", "AXDisclosureTriangle", "AXComboBox", "AXTextField",
              "AXTextArea", "AXImage", "AXStaticText", "AXOutlineRow"}


def _plausible(element, text: str) -> bool:
    """A control called `text`, not a paragraph that happens to contain the word."""
    needle = " ".join(text.lower().split())
    label = _label(element).lower()
    return bool(needle) and (label == needle or label.startswith(needle) or
                             (needle in label and len(label) <= max(40, 4 * len(needle))))


def _rank(elements: list, text: str) -> list:
    """Best match first: exact label, then starts with, then contains; pressable before not."""
    needle = " ".join(text.lower().split())

    def score(element):
        label = _label(element).lower()
        role = _attr(element, "AXRole") or ""
        exact = 3 if label == needle else 2 if label.startswith(needle) else 1 if needle in label else 0
        press = 1 if role in {"AXButton", "AXLink", "AXMenuItem", "AXTab", "AXCheckBox", "AXRadioButton",
                              "AXPopUpButton"} else 0
        return (exact, press, -len(label))
    return sorted(elements, key=score, reverse=True)


def _pressable(element, depth: int = 4):
    """The link or button a piece of text belongs to; else the element itself if it can be
    pressed; else its nearest ancestor that can."""
    AX = _ax()
    if _attr(element, "AXRole") in {"AXStaticText", "AXImage", "AXGroup"}:
        node = _attr(element, "AXParent")
        for _ in range(3):
            if node is None:
                break
            if _attr(node, "AXRole") in {"AXLink", "AXButton", "AXMenuItem", "AXTab", "AXCheckBox",
                                          "AXRadioButton", "AXPopUpButton"}:
                return node
            node = _attr(node, "AXParent")
    node = element
    for _ in range(depth):
        if node is None:
            break
        try:
            err, actions = AX.AXUIElementCopyActionNames(node, None)
        except Exception:
            actions = None
        if actions and "AXPress" in actions and (_attr(node, "AXRole") or "") != "AXWebArea":
            return node
        node = _attr(node, "AXParent")
    return element


def _on_screen(element, window) -> bool:
    box, area = _frame(element), _frame(window)
    if not box or not area:
        return False
    x, y, w, h = box
    ax, ay, aw, ah = area
    return w > 0 and h > 0 and x + w > ax and x < ax + aw and y + h > ay + 20 and y < ay + ah


def _front():
    """(NSRunningApplication, AX app element, focused window) of the app in front, skipping Mint itself."""
    import AppKit
    AX = _ax()
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is not None and front.processIdentifier() in {os.getpid(), os.getppid()}:
        from mint.tools import extra as extra_tools
        target = extra_tools._target.get("app")
        if target is not None and not target.isTerminated():
            from mint.screen.ground import bring_forward
            bring_forward(target, 1.5)
            front = target
    if front is None:
        return None, None, None
    app = AX.AXUIElementCreateApplication(front.processIdentifier())
    window = _attr(app, "AXFocusedWindow") or _attr(app, "AXMainWindow")
    return front, app, window


def _highlight(element, label: str = "", seconds: float = 1.6) -> None:
    box = _frame(element)
    if box:
        try:
            from mint.ui.effects import fx
            fx.highlight(*box, seconds=seconds, label=label[:40])
        except Exception:
            pass


def _highlight_box(box, label: str = "", seconds: float = 1.6) -> None:
    try:
        from mint.ui.effects import fx
        fx.highlight(*box, seconds=seconds, label=label[:40])
    except Exception:
        pass


def _wheel(point, direction: str, amount: int) -> None:
    import Quartz

    from mint.tools.fastinput import _post
    step = 5
    dy = {"down": -step, "up": step}.get(direction, 0)
    dx = {"left": step, "right": -step}.get(direction, 0)
    for _ in range(max(1, amount)):
        event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, dy, dx)
        Quartz.CGEventSetLocation(event, Quartz.CGPointMake(*point))
        _post(event)
        time.sleep(0.02)


def _scroll_area_of(element):
    node = _attr(element, "AXParent")
    for _ in range(30):
        if node is None:
            return None
        if _attr(node, "AXRole") in {"AXScrollArea", "AXWebArea"}:
            return node
        node = _attr(node, "AXParent")
    return None


def _signature(window) -> str:
    """A cheap fingerprint of what is on screen in a window, to notice when scrolling stops moving."""
    texts = []
    for count, node in enumerate(_walk_limited(window, 400)):
        value = _attr(node, "AXValue")
        title = _attr(node, "AXTitle")
        for text in (value, title):
            if isinstance(text, str) and text:
                texts.append(text[:40])
        if count < 60:
            box = _frame(node)
            if box:
                texts.append(f"{box[0]:.0f},{box[1]:.0f}")
    return hashlib.sha1("|".join(texts).encode()).hexdigest()


def _walk_limited(root, limit: int):
    queue, seen = deque([root]), 0
    while queue and seen < limit:
        node = queue.popleft()
        seen += 1
        yield node
        queue.extend(_attr(node, "AXChildren") or [])


def _ocr_find(target: str):
    """(x, y, w, h) of `target` read on screen inside the front window, or None. For apps whose
    accessibility tree hides its text (System Settings' SwiftUI sidebar gives invalid elements)."""
    try:
        from mint.screen import ocr
        items, _ = ocr.read_screen()
        window = ocr._front_window()
    except Exception as error:
        log.info("ocr failed: %s", error)
        return None
    needle = " ".join(target.lower().split())
    best, score = None, 0
    for item in items:
        if window is not None and not ocr._inside(item, window):
            continue
        text = " ".join(str(item.get("text", "")).lower().split())
        s = 3 if text == needle else 2 if text.startswith(needle) else \
            1 if needle in text and len(text) <= max(40, 4 * len(needle)) else 0
        if s > score:
            best, score = item, s
    if best is None:
        return None
    return (best["x"], best["y"], best["w"], best["h"])


# --- scroll_to ------------------------------------------------------------------------------

_GEOMETRY = {"left", "right", "top", "bottom", "sidebar", "main", "middle", "center", "centre", "content"}


def _pane(window, where: str):
    """The scrollable pane `where` describes."""
    areas = [n for n in _walk_limited(window, 3000)
             if _attr(n, "AXRole") in {"AXScrollArea", "AXWebArea"} and _frame(n) and _frame(n)[2] > 40
             and _frame(n)[3] > 40]
    if not areas:
        return None
    words = set(re.findall(r"[a-z]+", where.lower()))
    for area in areas:                                  # a pane named like that
        label = _label(area).lower() + " " + str(_attr(area, "AXIdentifier") or "").lower()
        if label.strip() and words & set(re.findall(r"[a-z]+", label)):
            return area
    if words & _GEOMETRY:
        by_x = sorted(areas, key=lambda a: _frame(a)[0])
        if words & {"left", "sidebar"}:
            return by_x[0]
        if "right" in words:
            return by_x[-1]
        if words & {"top"}:
            return sorted(areas, key=lambda a: _frame(a)[1])[0]
        if words & {"bottom"}:
            return sorted(areas, key=lambda a: _frame(a)[1])[-1]
        return max(areas, key=lambda a: _frame(a)[2] * _frame(a)[3])
    for area in areas:                                  # a pane containing that text
        if _walk_find(area, where, limit=800, seconds=0.6):
            return area
    return max(areas, key=lambda a: _frame(a)[2] * _frame(a)[3])


def _center(box):
    x, y, w, h = box
    return x + w / 2, y + h / 2


def scroll_to(args: dict) -> str:
    from mint.tools.fastinput import has_accessibility

    if not has_accessibility():
        return "FAILED: Mint lacks Accessibility permission."
    target = str(args.get("target") or "").strip()
    where = str(args.get("where") or "").strip()
    direction = str(args.get("direction") or "down").lower()
    amount = max(1, min(int(args.get("amount") or 3), 40))
    front, app, window = _front()
    if window is None:
        return "FAILED: no window is in front."
    name = front.localizedName()

    if not target:
        if not where:
            return "FAILED: say what to bring into view (target) or which pane to scroll (where)."
        pane = _pane(window, where)
        if pane is None:
            return f"FAILED: no scrollable pane found in {name}."
        point = _center(_frame(pane))
        try:
            from mint.ui.effects import fx
            fx.scroll(point[0], point[1], direction)
        except Exception:
            pass
        _wheel(point, direction, amount)
        return f"Scrolled the {where} pane of {name} {direction}."

    def locate():
        web = None
        from mint.tools.documents import _find_web_area
        web = _find_web_area(window)
        found = (_search(web, target) if web is not None else None) or []
        if not found:
            found = _search(window, target) or []
        if not found:
            found = _walk_find(window, target, limit=4000, seconds=1.5)
        return _rank(found, target)[0] if found else None

    element = locate()
    rounds = 0
    seen = _ocr_find(target) if element is None else None
    if seen is not None:
        _highlight_box(seen, target)
        return f"'{target}' is already in view in {name} (read on screen)."
    if element is None:
        # Not in the tree yet (long lists build rows as they scroll), or its
        # text is hidden from Accessibility. Scroll each pane in turn - the
        # named one, else largest first - looking again after every step, until
        # it appears or the pane stops moving.
        if where:
            panes = [_pane(window, where)]
        else:
            panes = [n for n in _walk_limited(window, 3000)
                     if _attr(n, "AXRole") in {"AXScrollArea", "AXWebArea"} and _frame(n)
                     and _frame(n)[2] > 40 and _frame(n)[3] > 40]
            panes.sort(key=lambda a: _frame(a)[2] * _frame(a)[3], reverse=True)
            panes = [a for i, a in enumerate(panes) if _attr(a, "AXRole") != "AXWebArea"
                     or not any(_attr(b, "AXRole") == "AXWebArea" for b in panes[:i])][:4]
        panes = [p for p in panes if p is not None]
        if not panes:
            return f"FAILED: '{target}' is not in {name}, and there is nothing to scroll."
        for pane in panes:
            point = _center(_frame(pane))
            last = _signature(pane)
            for step in range(1, 16):
                _wheel(point, direction, 4)
                rounds += 1
                time.sleep(0.3)
                element = locate()
                if element is not None:
                    break
                seen = _ocr_find(target)
                if seen is not None:
                    _highlight_box(seen, target)
                    return f"'{target}' is now in view in {name} after {rounds} scroll{'s' if rounds > 1 else ''} (read on screen)."
                now = _signature(pane)
                if now == last:
                    break                  # this pane is at its end
                last = now
            if element is not None:
                break
        if element is None:
            return (f"FAILED: scrolled {direction} through {name} ({len(panes)} pane{'s' if len(panes) > 1 else ''}) "
                    f"and '{target}' never appeared. Try direction={'up' if direction == 'down' else 'down'}, "
                    "or check the name.")

    AX = _ax()
    AX.AXUIElementPerformAction(element, "AXScrollToVisible")
    time.sleep(0.15)
    if not _on_screen(element, window):
        area = _scroll_area_of(element)
        box = _frame(element)
        if area is not None and box:
            ax_, ay, aw, ah = _frame(area)
            point = _center((ax_, ay, aw, ah))
            for _ in range(20):
                box = _frame(element)
                if not box or (ay <= box[1] and box[1] + box[3] <= ay + ah):
                    break
                _wheel(point, "down" if box[1] > ay + ah else "up", 3)
                time.sleep(0.08)
    visible = _on_screen(element, window)
    label = _label(element)[:60] or target
    _highlight(element, target)
    role = (_attr(element, "AXRole") or "").removeprefix("AX")
    if visible:
        return f"'{label}' ({role}) is now in view in {name}" + (f" after {rounds} scrolls." if rounds else ".")
    return f"Found '{label}' ({role}) in {name}, but could not confirm it is on screen; look to check."


# --- menu -----------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return " ".join(re.sub(r"[…\.]{1,3}$|&", "", (text or "")).lower().split())


def _toggle(title: str) -> str:
    """'Show Path Bar' <-> 'Hide Path Bar' (menu items that flip), else ''."""
    for a, b in (("show ", "hide "), ("hide ", "show "), ("enter ", "exit "), ("exit ", "enter "),
                 ("turn on ", "turn off "), ("turn off ", "turn on ")):
        if title.lower().startswith(a):
            return b.capitalize() + title[len(a):]
    return ""


def _menu_children(element) -> list:
    """Items of a menu bar item or a menu item with a submenu."""
    items = []
    for child in _attr(element, "AXChildren") or []:
        if _attr(child, "AXRole") == "AXMenu":
            items.extend(_attr(child, "AXChildren") or [])
        else:
            items.append(child)
    return [i for i in items if _attr(i, "AXRole") in {"AXMenuItem", "AXMenuBarItem"}]


def _match(items: list, want: str):
    want = _norm(want)
    best, score = None, 0
    for item in items:
        title = _norm(_attr(item, "AXTitle") or "")
        if not title:
            continue
        s = 3 if title == want else 2 if title.startswith(want) else 1 if want in title else 0
        if s > score:
            best, score = item, s
    return best


_MODS = [(4, "⌃"), (2, "⌥"), (1, "⇧")]


_VIRTUAL = {126: "↑", 125: "↓", 123: "←", 124: "→", 51: "⌫", 117: "⌦", 36: "↩", 53: "⎋", 48: "⇥",
            49: "Space", 116: "⇞", 121: "⇟", 115: "↖", 119: "↘"}
_GLYPHS = {104: "↑", 106: "↓", 100: "←", 101: "→", 23: "⌫", 10: "⌦", 11: "↩", 27: "⎋", 2: "⇥", 9: "Space",
           98: "⇞", 107: "⇟", 102: "↖", 105: "↘", 148: "fn"}


def _shortcut(item) -> str:
    char = _attr(item, "AXMenuItemCmdChar")
    if not isinstance(char, str) or not char.strip() or not char.isprintable():
        key = _attr(item, "AXMenuItemCmdVirtualKey")
        glyph = _attr(item, "AXMenuItemCmdGlyph")
        char = _VIRTUAL.get(int(key)) if isinstance(key, int) and key in _VIRTUAL else \
            _GLYPHS.get(int(glyph)) if isinstance(glyph, int) and glyph in _GLYPHS else ""
    if not char:
        return ""
    mods = int(_attr(item, "AXMenuItemCmdModifiers") or 0)
    text = "".join(symbol for bit, symbol in _MODS if mods & bit)
    if not mods & 8:
        text += "⌘"
    return text + char.upper()


def _describe_items(items: list) -> str:
    rows = []
    for item in items:
        title = _attr(item, "AXTitle") or ""
        if not title:
            continue
        extra = []
        keys = _shortcut(item)
        if keys:
            extra.append(keys)
        if _attr(item, "AXEnabled") is False:
            extra.append("disabled")
        if _menu_children(item):
            extra.append("submenu ›")
        if _attr(item, "AXMenuItemMarkChar"):
            extra.append("✓")
        rows.append(f"- {title}" + (f"  ({', '.join(extra)})" if extra else ""))
    return "\n".join(rows)


def _find_anywhere(tops: list, want: str, depth: int = 3):
    """(item, path) for a menu item titled `want` in any menu, best match first."""
    best, best_score, best_path = None, 0, []
    want_n = _norm(want)
    stack = [(top, [_attr(top, "AXTitle") or ""], 1) for top in tops]
    while stack:
        node, path, level = stack.pop()
        for item in _menu_children(node):
            title = _norm(_attr(item, "AXTitle") or "")
            if title:
                s = 3 if title == want_n else 2 if title.startswith(want_n) else 0
                if s > best_score:
                    best, best_score, best_path = item, s, path + [_attr(item, "AXTitle")]
            if level < depth:
                stack.append((item, path + [_attr(item, "AXTitle") or ""], level + 1))
    return best, best_path


def menu(args: dict) -> str:
    import AppKit

    from mint.tools import appfinder
    from mint.tools.fastinput import has_accessibility

    if not has_accessibility():
        return "FAILED: Mint lacks Accessibility permission."
    AX = _ax()
    wanted = str(args.get("app") or "").strip()
    if wanted:
        resolved, _ = appfinder.resolve(wanted)
        running = [a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                   if (a.localizedName() or "").lower() == (resolved or wanted).lower()]
        if not running:
            return f"FAILED: {resolved or wanted} is not running. open_app it first."
        front = running[0]
        from mint.screen.ground import bring_forward
        bring_forward(front, 2.0)
        app = AX.AXUIElementCreateApplication(front.processIdentifier())
    else:
        front, app, _ = _front()
        if front is None:
            return "FAILED: no app is in front."
    name = front.localizedName()
    bar = _attr(app, "AXMenuBar")
    tops = [t for t in (_attr(bar, "AXChildren") or []) if _attr(t, "AXTitle")] if bar is not None else []
    if not tops:
        return f"FAILED: {name} has no menu bar Mint can read."
    tops_named = tops[1:] if len(tops) > 1 else tops          # the first is the Apple menu
    path = [p.strip() for p in re.split(r"\s*(?:>|›|→|»|/|\bthen\b)\s*", str(args.get("path") or "")) if p.strip()]
    action = str(args.get("action") or "click").lower()

    if action == "list":
        if not path:
            return f"{name}'s menus: " + ", ".join(_attr(t, "AXTitle") for t in tops_named) + \
                ". List one with path='<menu name>'."
        node = _match(tops_named, path[0])
        for part in path[1:]:
            if node is None:
                break
            node = _match(_menu_children(node), part)
        if node is None:
            item, trail = _find_anywhere(tops_named, path[-1])
            if item is not None:
                keys = _shortcut(item)
                return f"'{_attr(item, 'AXTitle')}' is in {name} > {' > '.join(trail)}" + \
                    (f" (shortcut {keys})." if keys else ".")
            return f"FAILED: no menu '{' > '.join(path)}' in {name}. Menus: " + \
                ", ".join(_attr(t, "AXTitle") for t in tops_named)
        items = _menu_children(node)
        _highlight(node, _attr(node, "AXTitle") or "")
        return f"{name} > {' > '.join(path)}:\n" + (_describe_items(items) or "(empty)")

    if not path:
        return "FAILED: give the menu path, like 'File > Export as PDF…'."
    item, trail = None, []
    top = _match(tops_named, path[0]) if len(path) > 1 else None
    if top is not None:
        node, trail = top, [_attr(top, "AXTitle")]
        for part in path[1:]:
            node = _match(_menu_children(node), part)
            if node is None:
                break
            trail.append(_attr(node, "AXTitle"))
        item = node
    if item is None:
        item, trail = _find_anywhere(tops_named, path[-1])
    if item is None:
        toggled = _toggle(path[-1])
        other, other_trail = _find_anywhere(tops_named, toggled) if toggled else (None, [])
        if other is not None:
            return (f"There is no '{path[-1]}' - {name} shows '{' > '.join(other_trail)}' instead, so it is "
                    f"already {'showing' if toggled.lower().startswith('hide') else 'hidden'}. Nothing to do.")
        return (f"FAILED: {name} has no menu command '{' > '.join(path)}'. Use menu action=list to see its "
                "menus: " + ", ".join(_attr(t, "AXTitle") for t in tops_named))
    title = _attr(item, "AXTitle") or path[-1]
    if _menu_children(item):
        return f"'{' > '.join(trail)}' is a submenu. Its items:\n" + _describe_items(_menu_children(item))
    if _attr(item, "AXEnabled") is False:
        return f"FAILED: '{' > '.join(trail)}' is greyed out in {name} right now (nothing selected, or not possible here)."
    risky = _risky(title)
    if risky:
        return (f"REFUSED: '{title}' would {risky}, and the user did not ask for that. Ask them first.")
    top_item = _match(tops_named, trail[0]) if trail else None
    if top_item is not None:
        _highlight(top_item, " › ".join(trail), seconds=1.2)
    saving = _norm(title) == "save"
    before = _document_state(app) if saving else None
    err = AX.AXUIElementPerformAction(item, "AXPress")
    keys = _shortcut(item)
    tip = f" (shortcut {keys})" if keys else ""
    if err != 0:
        return f"FAILED: {name} did not accept '{' > '.join(trail)}' (AX error {err})."
    after = _after_save(app, before) if saving else ""
    return f"Chose {name} > {' > '.join(trail)}{tip}.{after}"


def _document_state(app) -> tuple[str, float | None]:
    """(file path, its modification time) of the document in the app's focused window; ("", None) if none."""
    try:
        from urllib.parse import unquote, urlparse
        window = _attr(app, "AXFocusedWindow") or _attr(app, "AXMainWindow")
        url = str(_attr(window, "AXDocument") or "") if window is not None else ""
        path = unquote(urlparse(url).path) if url.startswith("file://") else url
        return path, (os.stat(path).st_mtime if path and os.path.exists(path) else None)
    except Exception:
        return "", None


def _sheet_text(app) -> str | None:
    """The words in a sheet or dialog now on the app's focused window, or None when there is none."""
    window = _attr(app, "AXFocusedWindow") or _attr(app, "AXMainWindow")
    if window is None:
        return None
    sheets = [c for c in (_attr(window, "AXChildren") or []) if _attr(c, "AXRole") == "AXSheet"]
    if not sheets and _attr(window, "AXSubrole") not in {"AXDialog", "AXSystemDialog"}:
        return None
    words = []
    for node in _walk_limited(sheets[0] if sheets else window, 300):
        if _attr(node, "AXRole") in {"AXStaticText", "AXButton"}:
            text = str(_attr(node, "AXValue") or _attr(node, "AXTitle") or "").strip()
            if text and text not in words:
                words.append(text)
    return " / ".join(words)[:300]


def _after_save(app, before: tuple[str, float | None]) -> str:
    """After File > Save: did the file on disk change? A save that opens a sheet ("the file has been changed
    by another application", where to save an untitled one) has not saved anything yet, and saying "Chose
    Save" alone made the model report a save that never happened."""
    path, was = before
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        time.sleep(0.3)
        try:
            if path and os.path.exists(path) and os.stat(path).st_mtime != was:
                return f" Saved: {_short(Path(path))} on disk changed just now."
            sheet = _sheet_text(app)
        except Exception:
            return ""
        if sheet is not None:
            return (f" NOT SAVED YET: a dialog opened instead ({sheet or 'no text'}). Read it (ui_elements) and "
                    "answer it as the user asked, then check the file.")
    if path:
        return (f" WARNING: {_short(Path(path))} on disk did not change - it may have had nothing new to save, or "
                "the save did not happen. Check before saying it is saved.")
    return ""


# --- wait_for_text --------------------------------------------------------------------------

def wait_for_text(args: dict) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        return "FAILED: no text to wait for."
    seconds = max(1, min(int(args.get("seconds") or 15), 120))
    gone = bool(args.get("gone"))
    from mint.app import control
    started = time.monotonic()
    last_ocr = 0.0
    while time.monotonic() - started < seconds:
        if control.stopped():
            return "STOPPED by the user."
        front, _, window = _front()
        present = False
        if window is not None:
            from mint.tools.documents import _find_web_area
            web = _find_web_area(window)
            hits = (_search(web, text, limit=1) if web is not None else None) or \
                _walk_find(window, text, limit=3000, seconds=0.8)
            present = bool(hits)
            if not present and time.monotonic() - last_ocr > 1.5:
                last_ocr = time.monotonic()
                present = _ocr_find(text) is not None
        if present != gone:
            took = time.monotonic() - started
            return f"'{text}' {'is gone' if gone else 'appeared'} after {took:.1f}s" + \
                (f" in {front.localizedName()}." if front else ".")
        time.sleep(0.6)
    return f"FAILED: after {seconds}s '{text}' {'is still there' if gone else 'has not appeared'}."


# --- browser --------------------------------------------------------------------------------

_BROWSERS = {"com.google.Chrome": ("Google Chrome", "chrome"), "com.apple.Safari": ("Safari", "safari"),
             "company.thebrowser.Browser": ("Arc", "chrome"), "com.brave.Browser": ("Brave Browser", "chrome"),
             "com.microsoft.edgemac": ("Microsoft Edge", "chrome")}
_JS_OFF: dict[str, float] = {}      # browser -> when JavaScript from Apple Events was found off


def _pick_browser(name: str = ""):
    import AppKit
    running = {a.bundleIdentifier(): a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
               if a.bundleIdentifier() in _BROWSERS}
    if name:
        key = name.lower().replace("google ", "")
        for bundle, app in running.items():
            if key in _BROWSERS[bundle][0].lower():
                return app
        from mint.tools import browser_choice
        bundle = browser_choice.bundle_for(name)
        if bundle in _BROWSERS and browser_choice.installed(bundle):
            # Not running yet: start it (a window comes with it) rather than using another browser.
            subprocess.run(["open", "-b", bundle], capture_output=True, check=False)
            for _ in range(40):
                started = next((a for a in AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
                                or [] if not a.isTerminated()), None)
                if started is not None and started.isFinishedLaunching():
                    time.sleep(0.8)
                    return started
                time.sleep(0.2)
        return None
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is not None and front.bundleIdentifier() in running:
        return front
    from mint.tools import extra as extra_tools
    target = extra_tools._target.get("app")
    if target is not None and target.bundleIdentifier() in running and not target.isTerminated():
        return target
    # The browser whose window is highest on screen - the one the user was last looking at.
    try:
        import Quartz
        pids = {a.processIdentifier(): a for a in running.values()}
        for info in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly
                                                      | Quartz.kCGWindowListExcludeDesktopElements,
                                                      Quartz.kCGNullWindowID) or []:
            if info.get("kCGWindowLayer", 0) == 0 and info.get("kCGWindowOwnerPID") in pids:
                return pids[info["kCGWindowOwnerPID"]]
    except Exception:
        pass
    for bundle in _BROWSERS:
        if bundle in running:
            return running[bundle]
    return None


def _browser_with_tabs(which: str):
    """The running browser that has tabs matching every item of `which` ("YouTube, anime"), or None."""
    import AppKit
    items = [w.strip() for w in re.split(r",|;|\band\b|\+", which or "") if w.strip()]
    if not items:
        return None
    front = _pick_browser()
    apps = [a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
            if a.bundleIdentifier() in _BROWSERS and _BROWSERS[a.bundleIdentifier()][1] == "chrome"]
    apps.sort(key=lambda a: front is None or a.processIdentifier() != front.processIdentifier())
    best, score = None, 0
    for app in apps:
        tabs = _tabs(app)
        hits = sum(1 for item in items if _match_tab(tabs, item) is not None)
        if hits > score:
            best, score = app, hits
        if hits == len(items):
            return app
    return best


def _as_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _wrap_js(code: str) -> str:
    """One expression that runs `code` (a function body) and always yields a string."""
    body = f"try {{ var __r = (function(){{ {code} }})(); " \
           "return (typeof __r === 'string') ? __r : JSON.stringify(__r); } catch (e) { return 'JS error: ' + e; }"
    return f"(function(){{ {body} }})()"


def _js(app, code: str) -> tuple[bool, str]:
    """Run JavaScript in the browser's current tab. (ok, result or error)."""
    name, family = _BROWSERS[app.bundleIdentifier()]
    if time.monotonic() - _JS_OFF.get(name, -1e9) < 20:
        return False, "off"
    wrapped = _wrap_js(code)
    if family == "chrome":
        script = f'tell application "{name}" to execute front window\'s active tab javascript {_as_string(wrapped)}'
    else:
        script = f'tell application "{name}" to do JavaScript {_as_string(wrapped)} in current tab of front window'
    ok, output = _osascript(script, timeout=15)
    if not ok and ("turned off" in output or "Allow JavaScript" in output or "(12)" in output):
        _JS_OFF[name] = time.monotonic()
        return False, "off"
    if not ok and ("(-1719)" in output or "Can’t get window" in output or "Can't get window" in output):
        return False, f"{name} has no window open. Open one first (browser action=new_tab)."
    if not ok and ("-1743" in output or "Not authorized" in output):
        return False, (f"macOS has not allowed Mint to control {name}. The user can allow it in System Settings > "
                       "Privacy & Security > Automation > Mint.")
    if ok:
        _JS_OFF.pop(name, None)
    return ok, output


def _js_off_note(app) -> str:
    name, family = _BROWSERS[app.bundleIdentifier()]
    menu_path = "View > Developer > Allow JavaScript from Apple Events" if family == "chrome" else \
        "Develop > Allow JavaScript from Apple Events (turn on the Develop menu in Settings > Advanced)"
    return (f"{name} does not allow JavaScript from Apple Events. The user can turn it on in {menu_path}. "
            "Don't turn it on yourself.")


def _tab_info(app) -> tuple[str, str]:
    name, family = _BROWSERS[app.bundleIdentifier()]
    if family == "chrome":
        script = (f'tell application "{name}" to return (URL of active tab of front window) & linefeed & '
                  f'(title of active tab of front window)')
    else:
        script = (f'tell application "{name}" to return (URL of current tab of front window) & linefeed & '
                  f'(name of current tab of front window)')
    ok, output = _osascript(script, timeout=6)
    if not ok:
        log.info("tab info from %s failed: %s", name, output[:200])
        return "", ""
    url, _, title = output.partition("\n")
    return url.strip(), title.strip()


_READ_JS = """
var main = document.querySelector('main, article, [role=main]');
var node = (main && main.innerText.length > 400) ? main : document.body;
var text = (node ? node.innerText : '').replace(/\\n{3,}/g, '\\n\\n');
return document.title + '\\n' + location.href + '\\n\\n' + text.slice(0, MAX);
"""

_LINKS_JS = """
var out = [], seen = {};
document.querySelectorAll('a[href]').forEach(function (a) {
  var t = (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\\s+/g, ' ').trim();
  if (!t || seen[a.href] || a.href.indexOf('javascript:') === 0) return;
  seen[a.href] = 1;
  var r = a.getBoundingClientRect();
  out.push((r.bottom > 0 && r.top < innerHeight ? '* ' : '  ') + t.slice(0, 80) + ' -> ' + a.href);
});
return out.slice(0, 150).join('\\n');
"""

_CLICK_JS = """
var want = WANT.toLowerCase().replace(/\\s+/g, ' ').trim();
function label(e) {
  return ((e.innerText || '') + ' ' + (e.getAttribute('aria-label') || '') + ' ' + (e.title || '') + ' ' +
          (e.type !== 'password' && typeof e.value === 'string' ? e.value : '') + ' ' + (e.alt || ''))
         .replace(/\\s+/g, ' ').trim().toLowerCase();
}
var sel = 'a,button,[role=button],[role=link],[role=tab],[role=menuitem],[role=option],[role=checkbox],' +
          '[role=radio],[role=switch],input[type=submit],input[type=button],input[type=checkbox],' +
          'input[type=radio],summary,label,[onclick],[tabindex]';
var best = null, bestScore = 0;
document.querySelectorAll(sel).forEach(function (e) {
  var r = e.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return;
  var l = label(e);
  if (!l) return;
  var s = l === want ? 4 : l.indexOf(want) === 0 ? 3 :
          (l.indexOf(want) >= 0 && l.length <= Math.max(40, 4 * want.length)) ? 2 - l.length / 400 : 0;
  if (s > bestScore) { bestScore = s; best = e; }
});
if (!best) return 'NOTFOUND';
if (RISKY_CHECK && /\\b(send|submit|post|publish|delete|remove|buy|pay|place order|checkout|confirm|unsubscribe)\\b/i
    .test(label(best))) return 'RISKY ' + label(best).slice(0, 80);
best.scrollIntoView({block: 'center'});
best.click();
var shown = (best.innerText || best.getAttribute('aria-label') || best.title || best.value || '').replace(/\\s+/g, ' ').trim();
return 'CLICKED ' + ({A: 'link', BUTTON: 'button', INPUT: 'button', SUMMARY: 'section', LABEL: 'option'}[best.tagName] ||
       (best.getAttribute('role') || 'element')) + ": '" + shown.slice(0, 80) + "'";
"""

_FILL_JS = """
var want = WANT.toLowerCase().trim(), value = VALUE, kind = KIND;
function lab(e) {
  var l = (e.getAttribute('aria-label') || '') + ' ' + (e.placeholder || '') + ' ' + (e.name || '') + ' ' +
          (e.id || '') + ' ' + (e.title || '');
  if (e.labels) for (var i = 0; i < e.labels.length; i++) l += ' ' + e.labels[i].innerText;
  var by = e.getAttribute('aria-labelledby');
  if (by) by.split(' ').forEach(function (id) { var x = document.getElementById(id); if (x) l += ' ' + x.innerText; });
  return l.replace(/\\s+/g, ' ').toLowerCase();
}
function words(t) { return t.toLowerCase().replace(/[^a-z0-9]+/g, ' ').split(' ').filter(function (w) { return w; }); }
function near(a, b) { return a === b || (a.length >= 4 && b.length >= 4 && a.slice(0, 4) === b.slice(0, 4)); }
function score(e) {
  var l = lab(e), ws = words(want), ls = words(l);
  if (!ws.length) return 0;
  if (l.indexOf(want) >= 0) return 2;
  return ws.filter(function (w) { return ls.some(function (x) { return near(w, x); }); }).length / ws.length;
}
var fields = Array.prototype.slice.call(document.querySelectorAll(
  'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]),' +
  'textarea,select,[contenteditable=true],[contenteditable=""],[role=textbox],[role=combobox]'))
  .filter(function (e) { var r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; })
  .filter(function (e) { return kind === 'select' ? e.tagName === 'SELECT' : e.tagName !== 'SELECT'; });
var e = null;
if (!want) {
  e = fields.indexOf(document.activeElement) >= 0 ? document.activeElement : (fields.length === 1 ? fields[0] : null);
  if (!e) return 'WHICH ' + fields.map(function (f) { return lab(f).trim().slice(0, 30); }).join(' | ');
} else {
  var best = 0;
  fields.forEach(function (f) { var s = score(f); if (s > best) { best = s; e = f; } });
  if (best < 0.5) e = fields.length === 1 ? fields[0] : null;
}
if (!e) return 'NOTFOUND ' + fields.map(function (f) { return lab(f).trim().slice(0, 30); }).join(' | ');
if (e.type === 'password' || /password|passcode|cvv|cvc|card number/i.test(lab(e))) return 'PASSWORD';
e.scrollIntoView({block: 'center'}); e.focus();
if (e.tagName === 'SELECT') {
  var opts = Array.prototype.slice.call(e.options), v = value.toLowerCase();
  var o = opts.filter(function (x) { return x.text.toLowerCase() === v; })[0] ||
          opts.filter(function (x) { return x.text.toLowerCase().indexOf(v) >= 0; })[0];
  if (!o) return 'NOOPTION ' + opts.map(function (x) { return x.text; }).slice(0, 30).join(' | ');
  e.value = o.value;
} else if (e.isContentEditable) {
  document.execCommand('selectAll', false, null); document.execCommand('insertText', false, value);
} else {
  var proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, value);
}
e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true}));
var nice = (e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.placeholder ||
           e.name || e.id || e.tagName.toLowerCase();
return 'FILLED ' + nice.replace(/\\s+/g, ' ').trim().slice(0, 60);
"""

_JS_BLOCK = re.compile(r"document\.cookie|localStorage|sessionStorage|indexedDB|\bfetch\s*\(|XMLHttpRequest|"
                       r"sendBeacon|WebSocket|\.submit\s*\(|window\.open|type\s*=\s*['\"]?password|"
                       r"navigator\.credentials|\.requestSubmit", re.I)


def _web_window(app):
    AX = _ax()
    ax_app = AX.AXUIElementCreateApplication(app.processIdentifier())
    window = _attr(ax_app, "AXFocusedWindow") or _attr(ax_app, "AXMainWindow")
    if window is None:
        return None, None
    from mint.tools.documents import _find_web_area
    return window, _find_web_area(window, limit=800)


def _url(text: str) -> str:
    text = text.strip()
    if re.match(r"^(https?|file|about|chrome|brave|edge)://", text) or text.startswith("about:"):
        return text
    if re.match(r"^(localhost|127\.0\.0\.1)(:\d+)?(/|$)", text):
        return "http://" + text
    return "https://" + text


def _tabs(app) -> list[dict]:
    """Every tab of every window: [{window, tab, active, title, url}], front window first."""
    name, family = _BROWSERS[app.bundleIdentifier()]
    current = "active tab index of w" if family == "chrome" else "index of current tab of w"
    title = "title of t" if family == "chrome" else "name of t"
    # Inside a browser's tell block `tab` is its tab object, not the tab character.
    script = f'''set sep to (ASCII character 9)
    tell application "{name}"
        set out to ""
        set wi to 0
        repeat with w in windows
            set wi to wi + 1
            try
                set act to {current}
                set ti to 0
                repeat with t in tabs of w
                    set ti to ti + 1
                    set out to out & wi & sep & ti & sep & (ti = act) & sep & ({title}) & sep & (URL of t) & linefeed
                end repeat
            end try
        end repeat
        return out
    end tell'''
    ok, output = _osascript(script, timeout=8)
    if not ok:
        log.info("tabs of %s failed: %s", name, output[:200])
        return []
    rows = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            rows.append({"window": int(parts[0]), "tab": int(parts[1]), "active": parts[2] == "true",
                         "title": parts[3], "url": "\t".join(parts[4:])})
    return rows


def _match_tab(tabs: list[dict], target: str):
    if re.fullmatch(r"\d+", target.strip()):
        n = int(target)
        return tabs[n - 1] if 0 < n <= len(tabs) else None
    needle = target.lower().strip()
    scored = []
    for t in tabs:
        title, url = t["title"].lower(), t["url"].lower()
        score = 3 if title == needle else 2 if needle in title else 1 if needle in url else 0
        if score:
            scored.append((score, t["active"], t))
    return max(scored, key=lambda s: (s[0], s[1]))[2] if scored else None


def _wait_loaded(app, seconds: float = 12.0) -> bool:
    """Until the current tab has finished loading (Chrome's `loading`, else the page's AXLoaded)."""
    name, family = _BROWSERS[app.bundleIdentifier()]
    deadline = time.monotonic() + seconds
    time.sleep(0.3)
    while time.monotonic() < deadline:
        if family == "chrome":
            ok, output = _osascript(f'tell application "{name}" to return loading of active tab of front window', 4)
            if ok and output.strip() == "false":
                return True
        else:
            _, web = _web_window(app)
            if web is not None and _attr(web, "AXLoaded") is not False:
                return True
        time.sleep(0.4)
    return False


def _browser_tabs(app, action: str, target: str) -> str:
    name, family = _BROWSERS[app.bundleIdentifier()]
    tabs = _tabs(app)
    if action == "tabs":
        if not tabs:
            return f"{name} has no open tabs Mint can read."
        rows = [f"{i}. {'▶ ' if t['active'] and t['window'] == 1 else ''}{t['title'][:70] or '(untitled)'} - "
                f"{t['url'][:90]}  (window {t['window']})" for i, t in enumerate(tabs, 1)]
        return f"{name}: {len(tabs)} tabs (▶ = the one showing):\n" + "\n".join(rows)

    if action == "new_tab":
        if not target:
            return "FAILED: new_tab needs the address in `target`."
        url = _url(target)
        from mint.tools import browser_choice
        from mint.app import live
        wants_another = re.search(r"\b(new tab|another|second|duplicate|again)\b", live.request() or "", re.I)
        same = None if wants_another else next((t for t in tabs if browser_choice._same_page(t["url"], url)), None)
        if same is not None:
            # Already open: use that tab rather than a duplicate (the user can ask for "another tab").
            w, t = same["window"], same["tab"]
            if family == "chrome":
                script = f'tell application "{name}"\n set active tab index of window {w} to {t}\n set index of window {w} to 1\nend tell'
            else:
                script = f'tell application "{name}"\n set current tab of window {w} to tab {t} of window {w}\n set index of window {w} to 1\nend tell'
            ok, _ = _osascript(script, 6)
            if ok:
                from mint.screen.ground import bring_forward
                bring_forward(app, 2.0)
                return f"Already open - switched to the {name} tab '{same['title'][:60]}' - {same['url'][:90]}."
        blank = next((t for t in tabs if t["window"] == 1 and t["active"] and browser_choice._is_blank(t["url"])), None)
        if blank is not None and family == "chrome":
            ok, output = _osascript(f'tell application "{name}" to set URL of active tab of front window to {_as_string(url)}', 8)
            if ok:
                from mint.screen.ground import bring_forward
                bring_forward(app, 2.0)
                loaded = _wait_loaded(app)
                new_url, new_title = _tab_info(app)
                return f"Opened in the empty {name} tab: {new_title or url} - {new_url or url}" + ("" if loaded else " (still loading)")
        if family == "chrome":
            script = (f'tell application "{name}"\n if (count of windows) = 0 then make new window\n'
                      f' tell front window to make new tab with properties {{URL:{_as_string(url)}}}\nend tell')
        else:
            script = (f'tell application "{name}"\n if (count of windows) = 0 then make new document\n'
                      f' tell front window to set current tab to (make new tab with properties {{URL:{_as_string(url)}}})\nend tell')
        ok, output = _osascript(script, 8)
        if not ok:
            return f"FAILED: {output[:200]}"
        from mint.screen.ground import bring_forward
        bring_forward(app, 2.0)
        loaded = _wait_loaded(app)
        new_url, new_title = _tab_info(app)
        return f"Opened a new {name} tab: {new_title or url} - {new_url or url}" + ("" if loaded else " (still loading)")

    if action == "go":
        if not target:
            return "FAILED: go needs the address in `target`."
        url = _url(target)
        tab = "active tab" if family == "chrome" else "current tab"
        ok, output = _osascript(f'tell application "{name}" to set URL of {tab} of front window to {_as_string(url)}', 8)
        if not ok:
            return f"FAILED: {output[:200]}"
        loaded = _wait_loaded(app)
        new_url, new_title = _tab_info(app)
        return f"Now on {new_title or url} - {new_url or url}" + ("" if loaded else " (still loading)")

    if action in {"switch", "close_tab"}:
        if action == "close_tab" and _risky("close"):
            return "REFUSED: the user did not ask to close a tab. Only close tabs when they ask."
        if target:
            chosen = _match_tab(tabs, target)
            if chosen is None:
                return f"FAILED: no {name} tab matches '{target}'. browser action=tabs lists them."
        else:
            if action == "switch":
                return "FAILED: say which tab (part of its title or address, or its number from tabs)."
            chosen = next((t for t in tabs if t["active"] and t["window"] == 1), None)
            if chosen is None:
                return "FAILED: no current tab found."
        w, t = chosen["window"], chosen["tab"]
        if action == "close_tab":
            ok, output = _osascript(f'tell application "{name}" to close tab {t} of window {w}', 6)
            return f"Closed the tab '{chosen['title'][:60]}'." if ok else f"FAILED: {output[:200]}"
        if family == "chrome":
            script = (f'tell application "{name}"\n set active tab index of window {w} to {t}\n'
                      f' set index of window {w} to 1\nend tell')
        else:
            script = (f'tell application "{name}"\n set current tab of window {w} to tab {t} of window {w}\n'
                      f' set index of window {w} to 1\nend tell')
        ok, output = _osascript(script, 6)
        if not ok:
            return f"FAILED: {output[:200]}"
        from mint.screen.ground import bring_forward
        bring_forward(app, 2.0)
        return f"Switched to '{chosen['title'][:70]}' - {chosen['url'][:90]}."
    return "FAILED: unknown tab action."


def _safari_page(app, what: str) -> str:
    """Safari gives a page's text and source through AppleScript even with JavaScript from Apple Events off."""
    ok, output = _osascript(f'tell application "Safari" to return {what} of current tab of front window', 10)
    return output if ok else ""


def _links_from_html(html_text: str, base: str) -> str:
    from html import unescape
    from urllib.parse import urljoin
    rows, seen = [], set()
    for href, inner in re.findall(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', html_text, re.I | re.S):
        label = " ".join(unescape(re.sub(r"<[^>]+>", " ", inner)).split())
        url = urljoin(base, unescape(href))
        if not label or url in seen or url.startswith("javascript:"):
            continue
        seen.add(url)
        rows.append(f"  {label[:80]} -> {url}")
    return "\n".join(rows[:150])


def _ax_select(app, target: str, option: str) -> str:
    """Pick `option` in the dropdown labelled `target` through Accessibility (no JavaScript)."""
    AX = _ax()
    window, web = _web_window(app)
    if web is None:
        return "FAILED: no web page found in the browser."
    boxes = [n for n in (_search(web, "", key="AXPopUpButtonSearchKey", limit=60) or [])
             if _attr(n, "AXRole") in {"AXPopUpButton", "AXComboBox"}] or \
        [n for n in _walk_limited(web, 4000) if _attr(n, "AXRole") in {"AXPopUpButton", "AXComboBox"}]
    words = [w for w in target.lower().split() if w]
    best, score = None, 0.0
    for box in boxes:
        label = (_label(box) + " " + str(_attr(_attr(box, "AXParent"), "AXTitle") or "")).lower()
        hit = sum(1 for w in words if w in label) / max(1, len(words)) if words else 0.5
        if hit > score:
            best, score = box, hit
    if best is None or (words and score < 0.5):
        return f"FAILED: no dropdown labelled '{target}' on the page."
    AX.AXUIElementPerformAction(best, "AXScrollToVisible")
    # Chromium lets the value of a closed <select> be set by its option text: no menu, no keys.
    before = _attr(best, "AXValue")
    AX.AXUIElementSetAttributeValue(best, "AXValue", option)
    time.sleep(0.2)
    now = str(_attr(best, "AXValue") or "")
    if now.lower().strip() == option.lower().strip():
        return f"Chose '{now}' in the '{target or 'dropdown'}' list. Nothing was submitted."
    # Otherwise type-ahead: focus it and type the option's name, into the browser only.
    import AppKit
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is not None and front.processIdentifier() == app.processIdentifier():
        AX.AXUIElementSetAttributeValue(best, "AXFocused", True)
        time.sleep(0.15)
        from mint.tools import everyday as skills
        skills.type_text(option, False)
        time.sleep(0.3)
        now = str(_attr(best, "AXValue") or "")
        if now.lower().startswith(option.lower()[:3]) and now != before:
            return f"Chose '{now}' in the '{target or 'dropdown'}' list. Nothing was submitted."
    AX.AXUIElementPerformAction(best, "AXPress")
    time.sleep(0.5)
    if not _attr(best, "AXExpanded"):
        AX.AXUIElementPerformAction(best, "AXShowMenu")        # WebKit ignores AXPress on a <select>
        time.sleep(0.5)

    def options():
        found = [n for n in _walk_limited(best, 400) if _attr(n, "AXRole") == "AXMenuItem"]
        if len(found) <= 1:
            # The open list is a pop-up menu of the app (never the menu bar's menus).
            ax_app = AX.AXUIElementCreateApplication(app.processIdentifier())
            for menu in (_attr(ax_app, "AXChildren") or []):
                if _attr(menu, "AXRole") == "AXMenu":
                    more = [n for n in _walk_limited(menu, 400) if _attr(n, "AXRole") == "AXMenuItem"]
                    if len(more) > len(found):
                        found = more
        return found

    items = options()
    if len(items) <= 1:
        # Still closed: a real click on it opens the menu - only with the browser in front.
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        box = _frame(best)
        if box and front is not None and front.processIdentifier() == app.processIdentifier():
            from mint.screen.ground import mouse_click
            mouse_click(*_center(box), label=target)
            time.sleep(0.6)
            items = options()
    want = option.lower().strip()
    names = [(_label(i) or "").strip() for i in items]
    pick = next((i for i, n in zip(items, names) if n.lower() == want), None) or \
        next((i for i, n in zip(items, names) if want in n.lower()), None)
    from mint.tools.fastinput import press_key
    if pick is not None:
        AX.AXUIElementPerformAction(pick, "AXPress")
        time.sleep(0.2)
    else:
        # Type-ahead inside the open list, then Return - which only picks the option.
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None and front.processIdentifier() == app.processIdentifier():
            from mint.tools import everyday as skills
            skills.type_text(option, False)
            time.sleep(0.2)
            press_key("return")
            time.sleep(0.3)
    now = str(_attr(best, "AXValue") or "")
    if now.lower().strip() == want or (pick is None and now.lower().startswith(want[:3]) and now != before):
        return f"Chose '{now}' in the '{target or 'dropdown'}' list. Nothing was submitted."
    if pick is None and _attr(best, "AXExpanded"):
        press_key("escape")
    listed = " | ".join(n for n in names if n)[:400]
    return f"FAILED: could not choose '{option}' (the list shows '{now}')." + (f" Options: {listed}" if listed else "")


# --- the pointer: what is under the mouse, and clicking there ----------------------------------------

_ROLE_WORDS = {"AXButton": "button", "AXLink": "link", "AXTextField": "text field", "AXTextArea": "text area",
               "AXCheckBox": "checkbox", "AXRadioButton": "tab or option", "AXPopUpButton": "pop-up menu",
               "AXMenuItem": "menu item", "AXMenuBarItem": "menu", "AXImage": "picture", "AXStaticText": "text",
               "AXRow": "row", "AXCell": "cell", "AXTab": "tab", "AXSlider": "slider", "AXGroup": "area",
               "AXWebArea": "web page", "AXWindow": "window", "AXToolbar": "toolbar", "AXScrollArea": "scroll area",
               "AXMenuButton": "menu button", "AXComboBox": "combo box", "AXList": "list", "AXOutline": "list"}


def mouse_position() -> tuple[float, float]:
    import Quartz
    point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return float(point.x), float(point.y)


def element_at(x: float, y: float):
    """(element, app) at a screen point (Quartz, top-left origin), skipping Mint's own overlays."""
    AX = _ax()
    import AppKit
    err, element = AX.AXUIElementCopyElementAtPosition(AX.AXUIElementCreateSystemWide(), float(x), float(y), None)
    if err or element is None:
        return None, None
    err, pid = AX.AXUIElementGetPid(element, None)
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if not err else None
    return element, app


def _deepest_at(element, x: float, y: float, limit: int = 600):
    """Chromium's hit test can stop at a big unlabelled group (the tab strip): look inside it for the
    smallest labelled element that contains the point."""
    best, best_area = None, float("inf")
    window = _attr(element, "AXWindow")
    if window is not None and _attr(element, "AXRole") != "AXWebArea":
        element = window        # the hit group can be an overlay beside the real control: search the window
    queue, seen = deque([element]), 0
    limit = max(limit, 2500)
    while queue and seen < limit:
        node = queue.popleft()
        seen += 1
        box = _frame(node)
        if box is None or not (box[0] <= x <= box[0] + box[2] and box[1] <= y <= box[1] + box[3]):
            if node is not element:
                continue
        elif node is not element and _label(node) and box[2] * box[3] < best_area:
            best, best_area = node, box[2] * box[3]
        if _attr(node, "AXRole") != "AXWebArea" or node is element:
            queue.extend(_attr(node, "AXChildren") or [])
    return best


def _meaningful(element, hops: int = 4):
    """The element itself, or the nearest ancestor with a label or an action (a picture inside a button)."""
    AX = _ax()
    node = element
    for _ in range(hops):
        if node is None:
            break
        err, actions = AX.AXUIElementCopyActionNames(node, None)
        pressable = any(a in (actions or []) for a in ("AXPress", "AXConfirm", "AXPick", "AXOpen"))
        if _label(node) and (pressable or _attr(node, "AXRole") not in {"AXGroup", "AXImage", "AXUnknown"}):
            return node
        if pressable:
            return node
        node = _attr(node, "AXParent")
    return element


def describe_point(x: float, y: float) -> str:
    """'the button 'Extensions' in Brave Browser' for what is at a screen point, or ''."""
    import os
    try:
        element, app = element_at(x, y)
    except Exception as error:
        log.info("hit test failed: %s", error)
        return ""
    if element is None:
        return ""
    if app is not None and app.processIdentifier() == os.getpid():
        return "Mint's own orb or overlay"
    node = _meaningful(element)
    if not _label(node):
        node = _deepest_at(element, x, y) or node
    role = _attr(node, "AXRole") or "?"
    kind = _ROLE_WORDS.get(role, str(_attr(node, "AXRoleDescription") or role.removeprefix("AX")).lower())
    label = _label(node)[:80]
    where = f" in {app.localizedName()}" if app is not None else ""
    return f"the {kind}{f' {label!r}' if label else ' (no label)'}{where}"


def _near_text(x: float, y: float, radius: float = 90) -> str:
    """Words read on screen right around a point, for things Accessibility can't name."""
    try:
        from mint.screen import ocr
        items, _ = ocr.read_screen()
    except Exception:
        return ""
    close = []
    for item in items:
        try:
            ix, iy, iw, ih, text = item["x"], item["y"], item["w"], item["h"], item["text"]
        except (KeyError, TypeError):
            continue
        cx, cy = ix + iw / 2, iy + ih / 2
        distance = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
        if distance <= radius + max(iw, ih) / 2:
            close.append((distance, str(text)))
    close.sort()
    return " | ".join(t for _, t in close[:6])


def pointer(args: dict) -> str:
    """Where the user's mouse pointer is and what it is over; or click right there."""
    AX = _ax()
    action = str(args.get("action") or "where").lower()
    x, y = mouse_position()
    element, app = element_at(x, y)
    what = describe_point(x, y) or "nothing Accessibility can name"
    if action == "where":
        extra = ""
        if element is None or what.endswith("(no label)") or "no label" in what:
            words = _near_text(x, y)
            extra = f" Text next to it on screen: {words}." if words else ""
        return f"The pointer is at ({int(x)}, {int(y)}), over {what}.{extra}"
    if action not in {"click", "double_click", "right_click"}:
        return "FAILED: action is where, click, double_click or right_click."
    import os
    if app is not None and app.processIdentifier() == os.getpid():
        return "FAILED: the pointer is over Mint itself. Ask the user to point at the thing to click."
    node = _meaningful(element) if element is not None else None
    if node is not None and not _label(node):
        node = _deepest_at(element, x, y) or node
    risky = _risky(_label(node)) if node is not None else ""
    if risky:
        return (f"REFUSED: the pointer is over {what}, and '{risky}' was not asked for. Ask the user to confirm "
                "first, then click with ui_act or pointer again.")
    if action == "click" and node is not None:
        err, actions = AX.AXUIElementCopyActionNames(node, None)
        if "AXPress" in (actions or []) and AX.AXUIElementPerformAction(node, "AXPress") == 0:
            return f"Pressed {what} (under the pointer at {int(x)}, {int(y)})."
    from mint.screen.ground import mouse_click
    mouse_click(x, y, button="right" if action == "right_click" else "left", double=action == "double_click",
                label=_label(node)[:30] if node is not None else "")
    verb = {"click": "Clicked", "double_click": "Double-clicked", "right_click": "Right-clicked"}[action]
    return f"{verb} {what} at the pointer ({int(x)}, {int(y)})."


def _browser_rule(what: str, browser_name: str) -> str:
    """browser action=rule: which browser a kind of site opens in."""
    from mint.tools import browser_choice
    if not what and not browser_name:
        return browser_choice.describe_rules()
    if browser_name.lower().strip() in {"remove", "none", "delete", "forget", "off", "no"}:
        return browser_choice.remove_rule(what)
    if not browser_name:
        return "FAILED: say which browser in `text` (Chrome, Brave, Safari...), or text=remove to drop the rule."
    return browser_choice.set_rule(what, browser_name)


# --- tab groups (Chromium browsers) -----------------------------------------------------------------

def _strip_walk(root, limit: int = 1500):
    """The browser's own controls, breadth first, never descending into the web page."""
    queue, seen = deque([root]), 0
    while queue and seen < limit:
        node = queue.popleft()
        seen += 1
        yield node
        if _attr(node, "AXRole") == "AXWebArea":
            continue
        queue.extend(_attr(node, "AXChildren") or [])


def _front_ax_window(app):
    AX = _ax()
    ax_app = AX.AXUIElementCreateApplication(app.processIdentifier())
    return ax_app, (_attr(ax_app, "AXMainWindow") or _attr(ax_app, "AXFocusedWindow")
                    or next(iter(_attr(ax_app, "AXWindows") or []), None))


def _strip_tabs(app) -> list:
    """The tab buttons of the browser's front window, left to right."""
    _, window = _front_ax_window(app)
    if window is None:
        return []
    found = []
    for node in _strip_walk(window):
        role = _attr(node, "AXRole")
        described = str(_attr(node, "AXRoleDescription") or "").lower()
        if role in {"AXRadioButton", "AXTab"} and described == "tab":
            found.append(node)
    found.sort(key=lambda n: (_frame(n) or (0, 0, 0, 0))[0])
    return found


def _strip_groups(app) -> dict[str, int]:
    """Tab groups in the front window, from the tabs' own labels ('… – Part of group Entertainment'):
    {group name: number of tabs}."""
    groups: dict[str, int] = {}
    for node in _strip_tabs(app):
        found = re.search(r"part of group (.+?)(?:\s+-\s+.*)?$", _label(node), re.I)
        if found:
            key = found.group(1).strip()
            groups[key] = groups.get(key, 0) + 1
    return groups


def _ax_tab_for(app, title: str, index: int | None = None, count: int | None = None):
    """The tab button for a tab: by its position when the strip shows every tab (titles repeat -
    two pages both called 'Example Domain'), else by title."""
    want = " ".join(title.lower().split())
    tabs = _strip_tabs(app)
    if index and count and len(tabs) == count and 0 < index <= len(tabs):
        node = tabs[index - 1]
        label = " ".join(_label(node).lower().split())
        if not want or label.startswith(want[:20]) or want.startswith(label[:20]):
            return node
    for node in tabs:
        if " ".join(_label(node).lower().split()) == want:
            return node
    for node in tabs:
        label = " ".join(_label(node).lower().split())
        if want and (label.startswith(want[:40]) or want.startswith(label[:40]) and label):
            return node
    return None


def _menu_at(app, points) -> object | None:
    """The open menu found by hit-testing points where it should be drawn - far faster than a walk."""
    for x, y in points or []:
        try:
            node, owner = element_at(x, y)
        except Exception:
            continue
        if node is None or owner is None or owner.processIdentifier() != app.processIdentifier():
            continue
        for _ in range(4):
            if node is None:
                break
            if _attr(node, "AXRole") == "AXMenu":
                return node
            node = _attr(node, "AXParent")
    return None


def _open_menu(app, near=None):
    """The context menu the browser has open (Chrome draws it inside the window's tree), or None."""
    menu = _menu_at(app, near)
    if menu is not None:
        return menu
    AX = _ax()
    ax_app = AX.AXUIElementCreateApplication(app.processIdentifier())
    for child in (_attr(ax_app, "AXChildren") or []):
        if _attr(child, "AXRole") == "AXMenu":
            return child
    for window in (_attr(ax_app, "AXWindows") or []):
        for node in _strip_walk(window, 4000):
            if _attr(node, "AXRole") == "AXMenu" and \
                    _attr(_attr(node, "AXParent"), "AXRole") not in {"AXMenuItem", "AXMenuBarItem", "AXMenuBar"}:
                return node
    return None


def _menu_items(menu) -> list:
    return [n for n in (_attr(menu, "AXChildren") or []) if _attr(n, "AXRole") == "AXMenuItem"]


def _popup_items(app, seconds: float = 2.0, near=None) -> list:
    """Items of the context menu the browser just opened (top level only)."""
    deadline = time.monotonic() + seconds
    walked = 0
    while time.monotonic() < deadline:
        menu = _menu_at(app, near) if near else None
        if menu is None and (not near or walked < 2 or time.monotonic() > deadline - 0.6):
            walked += 1
            menu = _open_menu(app)
        if menu is not None:
            items = _menu_items(menu)
            if items:
                return items
        time.sleep(0.1)
    return []


def _menu_pick(items, wanted: tuple[str, ...]):
    for want in wanted:
        for item in items:
            label = " ".join(_label(item).lower().split())
            if label == want or label.startswith(want):
                return item
    return None


def _submenu(item) -> list:
    """Items of a menu item's submenu (opened with AXPress when not there yet)."""
    def read():
        for child in (_attr(item, "AXChildren") or []):
            if _attr(child, "AXRole") == "AXMenu":
                found = _menu_items(child)
                if found:
                    return found
        return []
    found = read()
    if not found:
        _ax().AXUIElementPerformAction(item, "AXPress")
        time.sleep(0.4)
        found = read()
    return found


def _close_menus(app) -> None:
    from mint.tools.fastinput import press_key
    for _ in range(3):
        if _open_menu(app) is None:
            return
        press_key("escape")
        time.sleep(0.2)


def _tab_menu(app, tab_title: str, index: int | None = None, count: int | None = None) -> list:
    """Open the right-click menu of one tab; its items (empty when it did not open)."""
    AX = _ax()
    node = _ax_tab_for(app, tab_title, index, count)
    if node is None:
        return []
    import threading
    # AXShowMenu can wait (and then report -25204) while the menu is up: don't block on it.
    box = _frame(node)
    near = [(box[0] + 40, box[1] + box[3] + 14), (box[0] + 40, box[1] + box[3] + 40),
            (box[0] + 80, box[1] + box[3] + 60)] if box else None
    threading.Thread(target=AX.AXUIElementPerformAction, args=(node, "AXShowMenu"), daemon=True).start()
    items = _popup_items(app, 1.5, near)
    if not items and box:
        from mint.screen.ground import mouse_click
        x, y = _center(box)
        mouse_click(x, y, label=tab_title[:30], button="right")
        items = _popup_items(app, 1.5, [(x + 30, y + 14), (x + 30, y + 40), (x + 60, y + 60)])
    return items


def _focused_text_field(app, seconds: float = 2.0):
    AX = _ax()
    ax_app = AX.AXUIElementCreateApplication(app.processIdentifier())
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        focused = _attr(ax_app, "AXFocusedUIElement")
        if focused is not None and _attr(focused, "AXRole") in {"AXTextField", "AXTextArea"} and \
                "address" not in _label(focused).lower():
            return focused
        time.sleep(0.1)
    return None


def _toolbar_press(app, target: str) -> str:
    """Press one of the browser's own controls by name: an extension's button, the Extensions menu,
    a bookmark, the profile button... through Accessibility, no pointer."""
    AX = _ax()
    browser_name = _BROWSERS[app.bundleIdentifier()][0]
    want = " ".join(target.lower().split())
    if not want:
        return "FAILED: say which control in `target` (an extension's name, 'Extensions', a bookmark...)."
    _, window = _front_ax_window(app)
    if window is None:
        return f"FAILED: {browser_name} has no window open."

    def controls():
        found = []
        for node in _strip_walk(window, 2500):
            if _attr(node, "AXRole") in {"AXButton", "AXPopUpButton", "AXMenuButton", "AXCheckBox", "AXLink"} \
                    and str(_attr(node, "AXRoleDescription") or "").lower() != "tab":
                label = " ".join(_label(node).lower().split())
                if label:
                    found.append((label, node))
        return found

    def best(found):
        exact = [n for label, n in found if label == want]
        starts = [n for label, n in found if label.startswith(want)]
        inside = [n for label, n in found if want in label]
        return (exact or starts or inside or [None])[0]

    found = controls()
    node = best(found)
    if node is not None:
        label = _label(node)
        if _risky(label):
            return f"REFUSED: '{label}' was not asked for."
        AX.AXUIElementPerformAction(node, "AXPress")
        time.sleep(0.5)
        return f"Pressed {browser_name}'s '{label}' button."
    # Not pinned: open the Extensions menu and pick it there.
    puzzle = next((n for label, n in found if label == "extensions"), None)
    if puzzle is not None:
        AX.AXUIElementPerformAction(puzzle, "AXPress")
        time.sleep(0.7)
        ax_app = AX.AXUIElementCreateApplication(app.processIdentifier())
        pool = []
        for root in [window] + list(_attr(ax_app, "AXWindows") or []):
            for n in _strip_walk(root, 1500):
                if _attr(n, "AXRole") in {"AXButton", "AXMenuItem", "AXCheckBox"}:
                    label = " ".join(_label(n).lower().split())
                    if label and label != "extensions":
                        pool.append((label, n))
        pick = best(pool)
        if pick is not None:
            AX.AXUIElementPerformAction(pick, "AXPress")
            time.sleep(0.5)
            return f"Opened the Extensions menu and pressed '{_label(pick)}'."
        from mint.tools.fastinput import press_key
        press_key("escape")
        names = sorted({label for label, _ in pool})[:25]
        return f"FAILED: no extension called '{target}'. The Extensions menu lists: {', '.join(names)}"
    names = sorted({label for label, _ in found})[:40]
    return (f"FAILED: no {browser_name} control called '{target}'. In full screen the toolbar is hidden - leave "
            f"full screen or use menu. Its controls: {', '.join(names)}")


def _tab_ungroup(app, name: str) -> str:
    """Take the tabs out of the tab group called `name` (they stay open). A group that is only saved
    (closed, in the bookmarks bar) is deleted instead - only when the user asked to delete or remove it."""
    AX = _ax()
    browser_name, family = _BROWSERS[app.bundleIdentifier()]
    if family != "chrome" or browser_name == "Arc":
        return f"FAILED: {browser_name} has no tab groups Mint can change."
    want = " ".join(name.lower().split())
    if not want:
        return "FAILED: say which group in `target`."
    from mint.screen.ground import bring_forward
    bring_forward(app, 3.0)
    time.sleep(0.3)
    def members():
        return [n for n in _strip_tabs(app) if re.search(rf"part of group {re.escape(want)}\b", _label(n).lower())]

    freed = 0
    total = len(members())
    for _ in range(total):
        found = members()
        if not found:
            break
        member = found[0]
        box = _frame(member)
        near = [(box[0] + 40, box[1] + box[3] + 14), (box[0] + 40, box[1] + box[3] + 40)] if box else None
        import threading
        threading.Thread(target=AX.AXUIElementPerformAction, args=(member, "AXShowMenu"), daemon=True).start()
        items = _popup_items(app, 1.5, near)
        pick = _menu_pick(items, ("remove from group", "remove tab from group"))
        if pick is None:
            _close_menus(app)
            break
        AX.AXUIElementPerformAction(pick, "AXPress")
        deadline = time.monotonic() + 1.5          # the tab's label updates a moment later
        while time.monotonic() < deadline and len(members()) >= len(found):
            time.sleep(0.1)
        freed += 1
    left = len(members())
    if freed and left:
        return f"Took {freed} tab(s) out of the group '{name}', but {left} still show as in it."
    if freed:
        return f"Took {freed} tab(s) out of the group '{name}'; the tabs are still open."
    # Not open: maybe a saved group in the bookmarks bar.
    _, window = _front_ax_window(app)
    saved = None
    for node in _strip_walk(window, 2000) if window is not None else []:
        if "saved tab group" in str(_attr(node, "AXRoleDescription") or "").lower() and \
                _label(node).lower().startswith(want + " group"):
            saved = node
            break
    if saved is None:
        return f"FAILED: no tab group called '{name}' in {browser_name}'s front window."
    if _risky("delete"):
        return (f"'{name}' is only a saved group (closed, in the bookmarks bar). Deleting it needs the user to "
                "ask to delete or remove it.")
    _confirm_sheet(app, "delete tab group", ("cancel",), wait=0.1)     # a question left open blocks menus
    box = _frame(saved)
    import threading
    threading.Thread(target=AX.AXUIElementPerformAction, args=(saved, "AXShowMenu"), daemon=True).start()
    items = _popup_items(app, 1.5, [(box[0] + 20, box[1] + box[3] + 14), (box[0] + 20, box[1] + box[3] + 40)]
                         if box else None)
    pick = _menu_pick(items, ("delete group", "delete"))
    if pick is None:
        _close_menus(app)
        return f"FAILED: the saved group's menu has no Delete ({', '.join(_label(i) for i in items)[:200]})."
    AX.AXUIElementPerformAction(pick, "AXPress")
    confirmed = _confirm_sheet(app, "delete tab group", ("delete", "delete group"), wait=2.5)
    time.sleep(0.4)
    still = [n for n in _strip_walk(window, 2000)
             if "saved tab group" in str(_attr(n, "AXRoleDescription") or "").lower()
             and _label(n).lower().startswith(want + " group")]
    if still:
        return (f"FAILED: the saved group '{name}' is still there"
                + (" (Chrome's 'Delete tab group?' question was answered)" if confirmed else "") + ".")
    return f"Deleted the saved tab group '{name}' (checked: it is gone from the bookmarks bar)."


def _confirm_sheet(app, title: str, buttons: tuple[str, ...], wait: float = 2.0) -> bool:
    """Press a button in the browser's own question sheet ("Delete tab group?") - for a step the user
    asked for. True when one was pressed."""
    AX = _ax()
    ax_app = AX.AXUIElementCreateApplication(app.processIdentifier())
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        for window in list(_attr(ax_app, "AXWindows") or []):
            for sheet in (_attr(window, "AXChildren") or []):
                if _attr(sheet, "AXRole") != "AXSheet":
                    continue
                if title and title not in " ".join(_label(n).lower() for n in _walk_limited(sheet, 60)):
                    continue
                for node in _walk_limited(sheet, 200):
                    if _attr(node, "AXRole") == "AXButton" and " ".join(_label(node).lower().split()) in buttons:
                        err = AX.AXUIElementPerformAction(node, "AXPress")
                        time.sleep(0.5)
                        if _attr(sheet, "AXRole") == "AXSheet" and sheet in (_attr(window, "AXChildren") or []):
                            # Chrome's dialog buttons can ignore AXPress: a real click on it.
                            box = _frame(node)
                            log.info("sheet button ignored AXPress (%s); clicking", err)
                            if box:
                                from mint.screen.ground import mouse_click
                                mouse_click(*_center(box), label=_label(node)[:20])
                                time.sleep(0.5)
                        return True
        time.sleep(0.15)
    return False


def _is_blank_url(url: str) -> bool:
    from mint.tools.browser_choice import _is_blank
    return _is_blank(url)


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _tab_group(app, name: str, which: str) -> str:
    """Put tabs in a named tab group (Chrome, Brave, Edge): the tabs matching `which` (comma-separated
    titles or addresses; empty = the current tab), in the front window."""
    browser_name, family = _BROWSERS[app.bundleIdentifier()]
    if family != "chrome" or browser_name == "Arc":
        return f"FAILED: {browser_name} has no tab groups Mint can make. Chrome, Brave and Edge do."
    name = " ".join(name.split())
    if not name:
        return "FAILED: say the group's name in `target`, e.g. target='Entertainment'."
    started = time.monotonic()
    tabs = _tabs(app)
    if not tabs:
        return f"FAILED: {browser_name} has no tabs Mint can read."
    wanted = [w.strip() for w in re.split(r",|;|\band\b|\+", which or "") if w.strip()]
    chosen: list[dict] = []
    for want in wanted:
        pool = [t for t in tabs if t not in chosen]
        match = _match_tab(pool, want)
        if match is None:
            return f"FAILED: no {browser_name} tab matches '{want}'. browser action=tabs lists them."
        chosen.append(match)
    if not chosen:
        current = next((t for t in tabs if t["active"] and t["window"] == 1), None)
        if current is None:
            return "FAILED: no current tab found."
        chosen = [current]
    moved = []
    home = chosen[0]["window"]
    for t in chosen[1:]:
        if t["window"] == home:
            continue
        # Tabs from other windows join the first tab's window: reopened there, then closed where they were.
        # (AppleScript's `move` left a blank New Tab in testing - the page was lost.)
        ok, output = _osascript(f'tell application "{browser_name}" to tell window {home} to make new tab '
                                f'with properties {{URL:{_as_string(t["url"])}}}', 6)
        if not ok:
            return f"FAILED: could not bring '{t['title'][:50]}' into the same window: {output[:150]}"
        moved.append(t)
    ok, home_id = _osascript(f'tell application "{browser_name}" to return id of window {home}', 4)
    # Close the originals afterwards, last window and highest tab number first so the numbers stay right
    # (closing a window's last tab closes the window).
    for t in sorted(moved, key=lambda t: (-t["window"], -t["tab"])):
        _osascript(f'tell application "{browser_name}" to close tab {t["tab"]} of window {t["window"]}', 6)
    if moved:
        # Reopened pages start blank: give them a moment to take their address.
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            fresh = _tabs(app)
            if not any(_is_blank_url(t["url"]) for t in fresh if t["window"] == home):
                break
            time.sleep(0.3)
        time.sleep(0.4)
    # The window the tabs are in goes to the front, with the browser.
    if ok and home_id.strip():
        _osascript(f'tell application "{browser_name}" to set index of (first window whose id is '
                   f'{home_id.strip()}) to 1', 6)
    elif home != 1:
        _osascript(f'tell application "{browser_name}" to set index of window {home} to 1', 6)
    from mint.screen.ground import bring_forward
    if not bring_forward(app, 3.0):
        return f"FAILED: {browser_name} would not come to the front; tab groups are made in its tab strip."
    time.sleep(0.4)
    # Full screen hides the tab strip, and its menus don't open: step out, group, step back in.
    _, window = _front_ax_window(app)
    was_full = bool(window is not None and _attr(window, "AXFullScreen"))
    if was_full:
        _ax().AXUIElementSetAttributeValue(window, "AXFullScreen", False)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and _attr(window, "AXFullScreen"):
            time.sleep(0.2)
        time.sleep(0.9)                     # the zoom-out animation
    try:
        return _group_tabs(app, name, chosen, moved, browser_name, started)
    finally:
        if was_full:
            time.sleep(0.3)
            _ax().AXUIElementSetAttributeValue(window, "AXFullScreen", True)


def _group_tabs(app, name: str, chosen: list, moved: list, browser_name: str, started: float) -> str:
    # Where each chosen tab is now (moves and the window order changed the numbers): by address.
    now = [t for t in _tabs(app) if t["window"] == 1]
    placed, taken = [], set()
    for t in chosen:
        from mint.tools.browser_choice import _same_page
        spot = next((n for n in now if n["url"] == t["url"] and n["tab"] not in taken), None) or \
            next((n for n in now if _same_page(n["url"], t["url"]) and n["tab"] not in taken), None) or \
            next((n for n in now if n["title"] == t["title"] and n["tab"] not in taken), None) or \
            next((n for n in now if _host_of(n["url"]) == _host_of(t["url"]) and n["tab"] not in taken), None)
        if spot is not None:
            taken.add(spot["tab"])
            placed.append(spot)
    if len(placed) < len(chosen):
        return f"FAILED: lost track of {len(chosen) - len(placed)} tab(s) after moving them. browser action=tabs shows where they are."
    count = len(now)
    if not _strip_tabs(app):
        return (f"FAILED: can't see {browser_name}'s tab strip (full screen hides it until the pointer is at the "
                "top). Leave full screen or show the toolbar, then try again.")
    existing = [g for g in _strip_groups(app) if g.lower() == name.lower()]
    grouped, problems = [], []
    for index, spot in enumerate(placed):
        title = spot["title"]
        items = _tab_menu(app, title, spot["tab"], count)
        if not items:
            problems.append(f"no menu for '{title[:40]}'")
            continue
        if index == 0 and not existing:
            pick = _menu_pick(items, ("add tab to new group", "add tabs to new group", "add to new group"))
            if pick is None:
                # With other groups around, Chrome nests it: Add tab to group > New group.
                parent = _menu_pick(items, ("add tab to group", "add tabs to group"))
                pick = _menu_pick(_submenu(parent), ("new group", "new tab group")) if parent is not None else None
            if pick is None:
                _close_menus(app)
                labels = ", ".join(_label(i) for i in items if _label(i))[:300]
                return f"FAILED: the tab menu has no 'Add tab to new group'. It shows: {labels}"
            _ax().AXUIElementPerformAction(pick, "AXPress")
            field = _focused_text_field(app, 2.5)
            if field is None:
                problems.append("the group's name box did not open, so the group is unnamed")
            else:
                import AppKit
                from mint.tools import everyday as skills
                from mint.tools.fastinput import press_key
                with skills._Clipboard() as clip:        # paste: exact, instant, the user's clipboard put back
                    clip.board.clearContents()
                    clip.board.setString_forType_(name, AppKit.NSPasteboardTypeString)
                    press_key("a", ["command"])
                    press_key("v", ["command"])
                    time.sleep(0.15)
                press_key("return")
                time.sleep(0.3)
                if _focused_text_field(app, 0.3) is not None:
                    press_key("escape")
            grouped.append(title)
            existing = [name]
            continue
        parent = _menu_pick(items, ("add tab to group", "add tabs to group", "move tab to group"))
        if parent is None:
            _close_menus(app)
            problems.append(f"no 'Add tab to group' for '{title[:40]}'")
            continue
        sub = _submenu(parent)
        target_item = next((n for n in sub if " ".join(_label(n).lower().split()) == name.lower()), None) or \
            next((n for n in sub if name.lower() in _label(n).lower()), None)
        if target_item is None:
            _close_menus(app)
            _close_menus(app)
            problems.append(f"the group '{name}' is not in the menu for '{title[:40]}' "
                            f"(it lists: {', '.join(_label(n) for n in sub)[:120]})")
            continue
        _ax().AXUIElementPerformAction(target_item, "AXPress")
        time.sleep(0.3)
        grouped.append(title)
    _close_menus(app)
    log.info("tab group %r: %d grouped in %.1fs", name, len(grouped), time.monotonic() - started)
    after = _strip_groups(app)
    shown = next((f"{g}' with {n} tab(s)" for g, n in after.items() if g.lower() == name.lower()), "")
    summary = f"Grouped {len(grouped)} tab(s) as '{name}' in {browser_name}: " + "; ".join(t[:50] for t in grouped)
    if moved:
        summary += f". Brought over from another window first: {', '.join(t['title'][:40] for t in moved)}"
    if problems:
        summary += ". Problems: " + "; ".join(problems)
    if not grouped:
        return "FAILED: " + summary
    return summary + (f". Checked: the tab strip shows the group '{shown}." if shown else
                      ". (Could not read the group back from the tab strip - check with look.)")


def browser(args: dict) -> str:
    action = str(args.get("action") or "read").lower()
    target = str(args.get("target") or "").strip()
    text = str(args.get("text") or "")
    asked = str(args.get("browser") or "")
    if action == "rule":
        return _browser_rule(target, text)
    if action in {"new_tab", "go"} and target:
        # Which browser a page opens in follows the user's rules and words, not just whichever is in front.
        from mint.tools import browser_choice
        bundle, why = browser_choice.choose(_url(target), asked)
        front = _pick_browser(asked) if asked else _pick_browser()
        if front is None or front.bundleIdentifier() != bundle:
            if action == "go":
                action = "new_tab"       # never replace a page in a browser the rule does not want
            asked = browser_choice.BROWSERS[bundle][0]
    app = None
    if action == "group" and not asked:
        app = _browser_with_tabs(text)        # the browser that has those tabs, not just the one in front
    app = app or _pick_browser(asked)
    if app is None:
        return "FAILED: no web browser is open. open_url or open_chrome first."
    name, family = _BROWSERS[app.bundleIdentifier()]
    url, title = _tab_info(app)
    site = __import__("mint.ui.activity", fromlist=["_site"])._site(url) if url else name

    if action == "url":
        return f"{name} is showing: {title or '(untitled)'} - {url or 'unknown address'}"

    if action in {"tabs", "switch", "new_tab", "close_tab", "go"}:
        return _browser_tabs(app, action, target)

    if action == "group":
        return _tab_group(app, target, text)

    if action == "ungroup":
        return _tab_ungroup(app, target)

    if action == "toolbar":
        return _toolbar_press(app, target)

    if action == "wait":
        loaded = _wait_loaded(app, float(args.get("seconds") or 15))
        new_url, new_title = _tab_info(app)
        return (f"Loaded: {new_title} - {new_url}" if loaded else
                f"FAILED: still loading after the wait: {new_title or new_url}")

    if action in {"back", "forward", "reload"}:
        verb = {"back": "go back", "forward": "go forward", "reload": "reload"}[action]
        before = url
        # The page's own history first: Chrome's back button (and AppleScript's `go back`) skips
        # entries made by script-triggered navigation, such as a click Mint made through JavaScript.
        page = {"back": "history.back(); return 'ok';", "forward": "history.forward(); return 'ok';",
                "reload": "location.reload(); return 'ok';"}[action]
        ok, output = _js(app, page)
        if ok and output == "ok":
            pass
        elif family == "chrome":
            ok, output = _osascript(f'tell application "{name}" to tell active tab of front window to {verb}', 6)
        else:
            from mint.screen.ground import bring_forward
            from mint.tools.fastinput import press_key
            bring_forward(app, 2.0)
            press_key({"back": "[", "forward": "]", "reload": "r"}[action], ["command"])
            ok, output = True, ""
        if not ok:
            return f"FAILED: {output[:200]}"
        if action != "reload":
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and _tab_info(app)[0] == before:
                time.sleep(0.15)
        else:
            time.sleep(0.3)
        _wait_loaded(app, 8)
        new_url, new_title = _tab_info(app)
        if action != "reload" and new_url == before:
            return f"{action.capitalize()}: nothing to go {action} to - still on {new_title or new_url}."
        return f"{action.capitalize()}: now on {new_title or new_url or 'the page'}."

    needs_front = action in {"click", "fill", "scroll", "read", "links", "find"}
    if needs_front:
        import AppKit
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None or front.processIdentifier() != app.processIdentifier():
            from mint.screen.ground import bring_forward
            bring_forward(app, 2.0)

    if action == "js":
        if not text.strip():
            return "FAILED: put the JavaScript in `text`."
        if _JS_BLOCK.search(text):
            return ("REFUSED: that JavaScript reads cookies/storage/passwords, sends data out, or submits a "
                    "form. Use click or fill instead, and only what the user asked.")
        body = text if re.search(r"\breturn\b", text) else f"return ({text.strip().rstrip(';')});"
        ok, output = _js(app, body)
        if output == "off":
            return "FAILED: " + _js_off_note(app)
        return (output[:6000] or "(no result)") if ok else f"FAILED: {output[:400]}"

    if action in {"read", "links"}:
        limit = max(1000, min(int(args.get("max_chars") or 12000), 40000))
        code = (_READ_JS.replace("MAX", str(limit)) if action == "read" else _LINKS_JS)
        ok, output = _js(app, code)
        if ok and output and not output.startswith("JS error"):
            return output
        if family == "safari":
            if action == "read":
                page = _safari_page(app, "text")
                if page.strip():
                    return f"{title}\n{url}\n\n" + page[:limit]
            else:
                links = _links_from_html(_safari_page(app, "source"), url)
                if links:
                    return links
        from mint.tools.documents import read_window
        page = read_window(max_chars=limit, with_links=(action == "links"))
        return page

    if action == "find":
        if not target:
            return "FAILED: say what to find (target)."
        window, web = _web_window(app)
        if web is None:
            return f"FAILED: no web page found in {name}."
        hits = _search(web, target, limit=20) or []
        if not hits:
            return f"'{target}' is not on the page {title or url}."
        rows = []
        for hit in hits[:8]:
            role = (_attr(hit, "AXRole") or "").removeprefix("AX")
            rows.append(f"- {role}: {_label(hit)[:100]}" + ("" if _on_screen(hit, window) else " (off screen)"))
        _highlight(hits[0], target)
        return f"'{target}' is on the page ({len(hits)} match{'es' if len(hits) > 1 else ''}):\n" + "\n".join(rows)

    if action == "click":
        if not target:
            return "FAILED: say what to click (target: its visible text)."
        risky = _risky(target)
        if risky:
            return (f"REFUSED: clicking '{target}' would {risky}, and the user did not ask for that. "
                    "Ask them first.")
        code = _CLICK_JS.replace("WANT", json.dumps(target))
        ok, output = _js(app, code.replace("RISKY_CHECK", "true"))
        if ok and output.startswith("RISKY"):
            label = output[6:]
            risky = _risky(label)
            if risky:
                return (f"REFUSED: '{label}' would {risky}, and the user did not ask for that. Ask them first.")
            ok, output = _js(app, code.replace("RISKY_CHECK", "false"))
        if ok and output.startswith("CLICKED"):
            time.sleep(0.6)
            new_url, new_title = _tab_info(app)
            if new_url and new_url != url:           # it navigated: report the page once it has loaded
                _wait_loaded(app, 10)
                new_url, new_title = _tab_info(app)
            moved = f" The page is now: {new_title} - {new_url}" if new_url and new_url != url else ""
            return f"Clicked the {output[8:]}.{moved}"
        if ok and output == "NOTFOUND" and re.search(r"\s\([^)]*\)\s*$", target):
            # "Headline (site.com)" as the page lists it: the link itself is just the headline.
            return browser({**args, "target": re.sub(r"\s\([^)]*\)\s*$", "", target)})
        if ok and output == "NOTFOUND":
            pass                     # the accessibility tree may still find it (shadow DOM, frames)
        return _ax_click(app, target, url)

    if action == "fill":
        if not text:
            return "FAILED: put what to type in `text`."
        code = _FILL_JS.replace("WANT", json.dumps(target)).replace("VALUE", json.dumps(text)).replace(
            "KIND", json.dumps("fill"))
        ok, output = _js(app, code)
        if ok and output.startswith(("WHICH", "NOTFOUND")):
            fields = output.split(" ", 1)[1] if " " in output else ""
            return (f"FAILED: {'say which field' if output.startswith('WHICH') else f'no field like {target!r}'}"
                    f" - the page's fields: {fields or 'none'}.")
        if ok and output == "PASSWORD":
            return "REFUSED: that is a password or card field. Mint never fills those - the user types it."
        if ok and output.startswith("FILLED"):
            return f"Filled the '{output[7:]}' field. Nothing was submitted."
        if ok and output.startswith("NOOPTION"):
            return f"FAILED: that list has no option '{text}'. Options: {output[9:]}"
        return _ax_fill(app, target, text)

    if action == "select":
        if not text:
            return "FAILED: put the option to choose in `text`."
        code = _FILL_JS.replace("WANT", json.dumps(target)).replace("VALUE", json.dumps(text)).replace(
            "KIND", json.dumps("select"))
        ok, output = _js(app, code)
        if ok and output.startswith("FILLED"):
            return f"Chose '{text}' in the '{output[7:].strip() or 'dropdown'}' list. Nothing was submitted."
        if ok and output.startswith(("WHICH", "NOTFOUND")):
            lists = output.split(" ", 1)[1] if " " in output else ""
            return f"FAILED: no dropdown like {target!r} - the page's dropdowns: {lists or 'none'}."
        if ok and output.startswith("NOOPTION"):
            return f"FAILED: that list has no option '{text}'. Options: {output[9:]}"
        return _ax_select(app, target, text)

    if action == "scroll":
        where = (target or "down").lower()
        code = {"down": "window.scrollBy(0, innerHeight * 0.8); return String(scrollY);",
                "up": "window.scrollBy(0, -innerHeight * 0.8); return String(scrollY);",
                "top": "window.scrollTo(0, 0); return '0';",
                "bottom": "window.scrollTo(0, document.body.scrollHeight); return String(scrollY);"}.get(where)
        if code is None:
            return scroll_to({"target": target})
        ok, output = _js(app, code)
        if ok and not output.startswith("JS error"):
            return f"Scrolled to the {where}." if where in {"top", "bottom"} else f"Scrolled {where}."
        from mint.tools.fastinput import press_key, scroll
        if where in {"top", "bottom"}:
            press_key("home" if where == "top" else "end")
            return f"Scrolled to the {where}."
        return scroll(where, 4)

    return "FAILED: unknown browser action."


def _ax_click(app, target: str, url: str) -> str:
    window, web = _web_window(app)
    if web is None:
        return f"FAILED: no web page found in the browser to click '{target}' in."
    hits = _search(web, target, limit=40) or _walk_find(web, target, limit=4000)
    hits = [h for h in hits if _plausible(h, target)]
    if not hits:
        return (f"FAILED: nothing called '{target}' on the page. Use browser action=links or read to see what "
                "is there, or scroll_to it first.")
    element = _pressable(_rank(hits, target)[0])
    label = _label(element)[:80] or target
    risky = _risky(label)
    if risky:
        return f"REFUSED: '{label}' would {risky}, and the user did not ask for that. Ask them first."
    AX = _ax()
    AX.AXUIElementPerformAction(element, "AXScrollToVisible")
    time.sleep(0.2)
    before = url
    _highlight(element, label, seconds=0.9)
    err = AX.AXUIElementPerformAction(element, "AXPress")
    if err != 0:
        box = _frame(element)
        if not box:
            return f"FAILED: found '{label}' but could not press it."
        from mint.screen.ground import mouse_click
        mouse_click(*_center(box), label=label)
    time.sleep(0.7)
    new_url, new_title = _tab_info(app)
    if new_url and new_url != before:
        _wait_loaded(app, 10)
        new_url, new_title = _tab_info(app)
    moved = f" The page is now: {new_title} - {new_url}" if new_url and new_url != before else ""
    role = (_attr(element, "AXRole") or "").removeprefix("AX").lower()
    return f"Clicked the {role} '{label}'.{moved}"


def _ax_fill(app, target: str, text: str) -> str:
    AX = _ax()
    window, web = _web_window(app)
    if web is None:
        return "FAILED: no web page found in the browser."
    if target:
        fields = []
        for key in ("AXTextFieldSearchKey", "AXEditableSearchKey"):
            fields = _search(web, "", key=key, limit=80) or []
            if fields:
                break
        words = [w for w in target.lower().split() if w]
        scored = []
        for field in fields:
            label = _label(field).lower()
            hit = sum(1 for w in words if w in label) / max(1, len(words))
            if hit >= 0.5:
                scored.append((hit, field))
        if not scored:
            return (f"FAILED: no field labelled '{target}' on the page. browser action=read shows the form; or "
                    "click the field, then fill with an empty target.")
        field = max(scored, key=lambda s: s[0])[1]
        if _attr(field, "AXSubrole") == "AXSecureTextField":
            return "REFUSED: that is a password field. Mint never fills those - the user types it."
        AX.AXUIElementPerformAction(field, "AXScrollToVisible")
        AX.AXUIElementSetAttributeValue(field, "AXFocused", True)
        time.sleep(0.2)
        _highlight(field, target, seconds=1.2)
        # Set the value straight through Accessibility: no keystrokes, so nothing can land in
        # another app. Chromium turns it into a real input event for the page.
        err = AX.AXUIElementSetAttributeValue(field, "AXValue", text)
        time.sleep(0.15)
        if err == 0 and (_attr(field, "AXValue") or "") == text:
            return f"Filled the '{target}' field. Nothing was submitted."
    elif _password_field():
        return "REFUSED: the focused field is a password field. Mint never fills those."
    # Typing is the fallback, and only into the browser itself.
    import AppKit
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None or front.processIdentifier() != app.processIdentifier():
        from mint.screen.ground import bring_forward
        if not bring_forward(app, 2.0):
            return (f"FAILED: {_BROWSERS[app.bundleIdentifier()][0]} is not in front, so nothing was typed "
                    "(keys would have gone to another app).")
    from mint.tools import everyday as skills
    from mint.tools.fastinput import press_key
    press_key("a", ["command"])
    result = skills.type_text(text, False)
    return f"{result} Nothing was submitted."


HANDLERS = {"read_file": read_file, "write_file": write_file, "find_files": find_files,
            "file_action": file_action, "web_search": web_search, "read_url": read_url, "browser": browser,
            "scroll_to": scroll_to, "menu": menu, "wait_for_text": wait_for_text,
            "run_applescript": run_applescript, "pointer": pointer}


def _clip_handler(name):
    def run(args):
        from mint.tools import clipboard as clip_tools
        return clip_tools.HANDLERS[name](args)
    return run


HANDLERS.update({name: _clip_handler(name) for name in ("screenshot", "clipboard")})


_QUIT_WORDS = re.compile(r"\b(quit|exit|shut ?down|shut (?:yourself|it|mint) (?:down|off)|turn (?:yourself|it|mint)? ?off|"
                         r"switch (?:yourself|it|mint)? ?off|power (?:off|down)|close (?:mint|yourself|the app))\b", re.I)


def _asks_to_quit_mint(request: str) -> bool:
    """"quit Mint", "turn yourself off", or a bare "quit" - never "quit Spotify" or "shut down the Mac"."""
    from mint.core import prefs
    text = " ".join(re.findall(r"[a-z']+", request.lower()))
    if not _QUIT_WORDS.search(text):
        return False
    if text in {"quit", "exit", "quit please", "please quit", "exit please"}:
        return True
    names = {"mint", "yourself", "you", "assistant", (prefs.name() or "mint").lower()}
    return any(f" {n} " in f" {text} " for n in names)


def quit_mint(args: dict) -> str:
    from mint.app import live
    from mint.app import power
    request = live.request() or ""
    if not _asks_to_quit_mint(request):
        return ("REFUSED: the user did not ask to quit Mint. For 'sleep' or 'stop listening' use stop_listening; "
                "quit only when they say quit, shut down or turn off.")
    power.quit_later(4.0, reason="asked by voice or text")
    return "Quitting in 4 seconds: everything Mint started stops too. Say a short goodbye now."


HANDLERS["quit_mint"] = quit_mint


def probe(args: dict) -> str:
    """Diagnostics, not offered to the model: how `target` is found in the front window, and how fast.
    Run through the app: --script [["harness_probe", {"target": "Keyboard"}]]"""
    target = str(args.get("target") or "")
    front, _, window = _front()
    if window is None:
        return "no window"
    out = [f"app {front.localizedName()}, window {_attr(window, 'AXTitle')!r}"]
    started = time.monotonic()
    hits = _search(window, target)
    out.append(f"search predicate: {'unsupported' if hits is None else len(hits)} in {time.monotonic() - started:.2f}s")
    started = time.monotonic()
    count, roles = 0, {}
    for node in _walk_limited(window, int(args.get("limit") or 3000)):
        count += 1
        role = _attr(node, "AXRole") or "?"
        roles[role] = roles.get(role, 0) + 1
    out.append(f"walk: {count} nodes in {time.monotonic() - started:.2f}s; roles {sorted(roles.items(), key=lambda r: -r[1])[:12]}")
    started = time.monotonic()
    found = _walk_find(window, target, limit=6000, seconds=8)
    out.append(f"walk_find: {len(found)} in {time.monotonic() - started:.2f}s")
    for node in found[:5]:
        out.append(f"  {_attr(node, 'AXRole')} {_label(node)[:80]!r} frame={_frame(node)} "
                   f"parent={_attr(_attr(node, 'AXParent'), 'AXRole')}")
    return "\n".join(out)


HANDLERS["probe"] = probe


def _probe_strip(args: dict) -> str:
    """Diagnostics: the browser's own controls (tab strip, toolbar) with roles and actions, no focus change."""
    AX = _ax()
    app = _pick_browser(str(args.get("browser") or ""))
    if app is None:
        return "no browser"
    _, window = _front_ax_window(app)
    out = [f"{app.localizedName()} window {_attr(window, 'AXTitle')!r}"]
    for node in _strip_walk(window, int(args.get("limit") or 800)):
        role = _attr(node, "AXRole")
        if role in {"AXWebArea", "AXStaticText", "AXImage"} and not args.get("all"):
            continue
        err, actions = AX.AXUIElementCopyActionNames(node, None)
        out.append(f"{role}/{_attr(node, 'AXSubrole')}/{_attr(node, 'AXRoleDescription')!r} {_label(node)[:60]!r} "
                   f"frame={_frame(node)} actions={[a for a in (actions or []) if not a.startswith('AXScroll')][:6]}")
        if len(out) > int(args.get("n") or 120):
            break
    return "\n".join(out)


def _probe_menu(args: dict) -> str:
    """Diagnostics: open a tab's context menu and report where its items appear."""
    import threading
    AX = _ax()
    app = _pick_browser(str(args.get("browser") or ""))
    from mint.screen.ground import bring_forward
    bring_forward(app, 2.0)
    node = _ax_tab_for(app, str(args.get("title") or ""))
    if node is None:
        return "no tab"
    out = []
    ax_app = AX.AXUIElementCreateApplication(app.processIdentifier())
    started = time.monotonic()
    done = {}
    if args.get("mouse"):
        from mint.screen.ground import mouse_click
        box = _frame(node)
        cx, cy = _center(box)
        mouse_click(cx, cy, button="right", label="probe")
        done["err"] = "mouse"
    else:
        thread = threading.Thread(target=lambda: done.setdefault("err", AX.AXUIElementPerformAction(
            node, str(args.get("action") or "AXShowMenu"))), daemon=True)
        thread.start()
    for step in range(12):
        time.sleep(0.25)
        kids = [(_attr(k, "AXRole"), _attr(k, "AXSubrole"), _label(k)[:30]) for k in (_attr(ax_app, "AXChildren") or [])]
        wins = [(_attr(w, "AXRole"), _attr(w, "AXSubrole"), _label(w)[:30]) for w in (_attr(ax_app, "AXWindows") or [])]
        focused = _attr(ax_app, "AXFocusedUIElement")
        sys_focus = _attr(AX.AXUIElementCreateSystemWide(), "AXFocusedUIElement")
        out.append(f"t={time.monotonic() - started:.2f} action_done={'err' in done}:{done.get('err')} kids={kids} "
                   f"wins={wins} focused={(_attr(focused, 'AXRole'), _label(focused)[:40]) if focused else None} "
                   f"sysfocus={(_attr(sys_focus, 'AXRole'), _label(sys_focus)[:40]) if sys_focus else None}")
        for k in (_attr(ax_app, "AXChildren") or []):
            if _attr(k, "AXRole") == "AXMenu":
                items = [_label(i) for i in _walk_limited(k, 200) if _attr(i, "AXRole") == "AXMenuItem"]
                out.append(f"   MENU items={items[:30]}")
            if _attr(k, "AXSubrole") == "AXUnknown" or (_attr(k, "AXRole") == "AXWindow" and step == 3):
                deep = [(_attr(i, "AXRole"), _label(i)[:30]) for i in _walk_limited(k, 300)
                        if _attr(i, "AXRole") in {"AXMenu", "AXMenuItem", "AXButton"}]
                if deep and step in (3, 8):
                    out.append(f"   in {_attr(k, 'AXSubrole')} window: {deep[:30]}")
        if step == 3:
            import subprocess as sp
            sp.run(["screencapture", "-x", "/tmp/minttest/menu.png"], check=False)
    from mint.tools.fastinput import press_key
    press_key("escape")
    return "\n".join(out)


def _probe_attrs(args: dict) -> str:
    """Diagnostics: every attribute of the first few elements with `role` in the front window."""
    AX = _ax()
    role = str(args.get("role") or "AXRow")
    _, _, window = _front()
    out = []
    for node in _walk_limited(window, 3000):
        if (_attr(node, "AXRole") or "?") != role or (args.get("label") and args["label"].lower() not in _label(node).lower()):
            continue
        err, names = AX.AXUIElementCopyAttributeNames(node, None)
        if err:
            out.append(f"attribute names error {err}")
        row = []
        for name in names or []:
            value = _attr(node, name)
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                row.append(f"{name}={str(value)[:40]!r}")
        kids = [(_attr(k, "AXRole"), _label(k)[:30], [(_attr(g, "AXRole"), _label(g)[:20])
                                                        for g in (_attr(k, "AXChildren") or [])][:6])
                for k in (_attr(node, "AXChildren") or [])]
        err, actions = AX.AXUIElementCopyActionNames(node, None)
        row.append(f"actions={list(actions or [])}")
        settable = AX.AXUIElementIsAttributeSettable(node, "AXValue", None)
        row.append(f"value_settable={settable}")
        out.append(" ".join(row) + f" kids={kids}")
        if len(out) >= int(args.get("n") or 4):
            break
    return "\n".join(out) or "none"


HANDLERS["probe_attrs"] = _probe_attrs
HANDLERS["probe_strip"] = _probe_strip
HANDLERS["probe_menu"] = _probe_menu


def _probe_ocr(args: dict) -> str:
    """Diagnostics: what text recognition reads inside the front window."""
    from mint.screen import ocr
    started = time.monotonic()
    items, area = ocr.read_screen()
    window = ocr._front_window()
    inside = [i for i in items if window is None or ocr._inside(i, window)]
    took = time.monotonic() - started
    return (f"{len(items)} lines on screen, {len(inside)} in window {window} ({took:.2f}s); area {area}\n" +
            "\n".join(f"  {i['text'][:40]!r} at {i['x']:.0f},{i['y']:.0f}" for i in inside[:40]))


HANDLERS["probe_ocr"] = _probe_ocr
