"""Notifications: "what did I miss?", "any messages from Slack?", "open that one", "clear them".

    "what did I miss?"                   summary    grouped by app, newest first
    "read my notifications"              list       app, title, text and how long ago
    "anything from Slack?"               list       app="Slack" (or words: query="invoice")
    "open that notification"             open       the one just read out (index or words)
    "dismiss the Slack one"              dismiss    closes it in Notification Center
    "clear my notifications"             clear_all  all of them, or one app's
    "reply to it saying on my way"       reply      Messages/Slack inline reply; sends only on send=true

Where they come from:

* Notification Center itself, through Accessibility (Mint has it; nothing new to grant). Its
  panel is opened by pressing the menu bar clock - on macOS 27 the clock lives in MenuBarAgent,
  before that in Control Center - read, and closed again: a short flash at the right edge.
  Each notification is an AXGroup with subrole AXNotificationCenterBanner, its UUID as the
  AXIdentifier, "title" / "subtitle" / "body" texts and a "5m ago" text; the app's name leads
  its description. A pile from one app (…BannerStack) opens up when pressed; the actions are
  "Show" (open), "Close" (dismiss) and, for messaging apps, "Reply".
* The usernoted database, when Mint has Full Disk Access: the same notifications (same UUIDs)
  without opening the panel, with exact times. Without it the file cannot even be opened, and
  Mint quietly uses the panel.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid as uuidlib

log = logging.getLogger("mint.tools.notifications")

DB = os.path.expanduser("~/Library/Group Containers/group.com.apple.usernoted/db2/db")
NC_BUNDLE = "com.apple.notificationcenterui"
CLOCK = "com.apple.menuextra.clock"
MENU_HOSTS = ("com.apple.MenuBarAgent", "com.apple.controlcenter", "com.apple.systemuiserver")
BANNER, STACK = "AXNotificationCenterBanner", "AXNotificationCenterBannerStack"
MAC_EPOCH = 978307200                  # the database counts seconds from 2001

_lock = threading.RLock()
_last: list[dict] = []                 # what was last read out, so "open the second one" works
_times: dict[str, float] = {}          # first 8 hex of a notification's UUID -> when it was delivered
_times_until = 0.0
NO_ACCESS = ("I can't read notifications: Mint needs Accessibility (System Settings > Privacy & Security > "
             "Accessibility). Full Disk Access also works.")


# --- Accessibility plumbing ------------------------------------------------------------------------

def _ax():
    import ApplicationServices as AX
    return AX


def _attr(element, name):
    from mint.screen import axkit
    return axkit.attr(element, name)


def _children(element) -> list:
    return list(_attr(element, "AXChildren") or [])


def _actions(element) -> list[str]:
    try:
        err, names = _ax().AXUIElementCopyActionNames(element, None)
        return [str(n) for n in names or []] if err == 0 else []
    except Exception:
        return []


def _perform(element, action: str) -> bool:
    try:
        return _ax().AXUIElementPerformAction(element, action) == 0
    except Exception:
        return False


def _named_action(element, word: str) -> str:
    """Custom actions read "Name:Close\\nTarget:0x0\\nSelector:(null)"; the whole string is the action."""
    for name in _actions(element):
        if name.startswith("Name:") and name[5:].split("\n")[0].strip().lower() == word.lower():
            return name
    return ""


def _find(element, test, depth: int = 0, limit: int = 12, skip_apps: bool = False):
    if element is None or depth > limit:
        return None
    if test(element):
        return element
    for child in _children(element):
        if skip_apps and _attr(child, "AXRole") == "AXApplication":
            continue                   # MenuBarAgent mirrors every app's own menus: not there
        found = _find(child, test, depth + 1, limit, skip_apps)
        if found is not None:
            return found
    return None


def _find_all(element, test, out: list, depth: int = 0, limit: int = 14) -> list:
    if element is None or depth > limit:
        return out
    if test(element):
        out.append(element)
        return out
    for child in _children(element):
        _find_all(child, test, out, depth + 1, limit)
    return out


def _app_element(bundle: str):
    import AppKit
    for app in AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle) or []:
        return _ax().AXUIElementCreateApplication(app.processIdentifier())
    return None


def _trusted() -> bool:
    try:
        return bool(_ax().AXIsProcessTrusted())
    except Exception:
        return False


def _clock():
    """The menu bar clock: pressing it opens and closes Notification Center."""
    def is_clock(e):
        return _attr(e, "AXIdentifier") == CLOCK
    for bundle in MENU_HOSTS:
        app = _app_element(bundle)
        if app is None:
            continue
        for root in (_attr(app, "AXExtrasMenuBar"), app):
            found = _find(root, is_clock, limit=5, skip_apps=True)
            if found is not None:
                return found
    return None


def _nc_window():
    app = _app_element(NC_BUNDLE)
    windows = list(_attr(app, "AXWindows") or []) if app is not None else []
    return windows[0] if windows else None


def _panel_open(window) -> bool:
    """A banner shows in the same window; only the panel has the widgets' Edit button."""
    return _find(window, lambda e: _attr(e, "AXIdentifier") in ("widget-editor-button", "AXNotificationListItems"),
                 limit=6) is not None


class _Panel:
    """Opens Notification Center if it is closed, and closes it again afterwards (unless kept)."""

    def __init__(self):
        self.opened = False
        self.keep = False
        self.window = None

    def __enter__(self):
        window = _nc_window()
        if window is not None and _panel_open(window):
            self.window = window
            return self
        clock = _clock()
        if clock is None or not _perform(clock, "AXPress"):
            raise RuntimeError("I couldn't open Notification Center (the menu bar clock wasn't found).")
        self.opened = True
        for _ in range(30):
            time.sleep(0.05)
            window = _nc_window()
            if window is not None and _panel_open(window):
                break
        time.sleep(0.25)               # the list animates in
        self.window = _nc_window()
        return self

    def __exit__(self, *exc):
        if self.opened and not self.keep:
            close()
        return False


def close() -> None:
    """Leave Notification Center closed."""
    window = _nc_window()
    if window is None or not _panel_open(window):
        return
    clock = _clock()
    if clock is not None:
        _perform(clock, "AXPress")
    for _ in range(20):
        time.sleep(0.05)
        window = _nc_window()
        if window is None or not _panel_open(window):
            return
    _escape()


def _escape() -> None:
    import Quartz
    for down in (True, False):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateKeyboardEvent(None, 53, down))


# --- reading the panel ------------------------------------------------------------------------------

def _texts(element) -> dict:
    found = {"title": "", "subtitle": "", "body": "", "when": ""}
    for child in _children(element):
        if _attr(child, "AXRole") != "AXStaticText":
            continue
        ident, value = _attr(child, "AXIdentifier") or "", str(_attr(child, "AXValue") or "")
        if ident in found:
            found[ident] = value
        elif not found["when"] and value:
            found["when"] = value      # "5m ago", "Yesterday", "Mon"
    return found


def _app_name(description: str, texts: dict) -> str:
    """The description is "<app>, <title>, <subtitle>, <body>[, stacked]"; app names may hold commas."""
    description = re.sub(r",\s*stacked$", "", description or "")
    for part in (texts["title"], texts["subtitle"], texts["body"]):
        if part:
            at = description.find(", " + part)
            if at > 0:
                return description[:at]
    return description.split(",")[0].strip()


def _item(element) -> dict:
    texts = _texts(element)
    return {"id": str(_attr(element, "AXIdentifier") or ""),
            "app": _app_name(str(_attr(element, "AXDescription") or ""), texts),
            "title": texts["title"], "subtitle": texts["subtitle"], "body": texts["body"],
            "when": texts["when"], "stacked": _attr(element, "AXSubrole") == STACK, "element": element}


def _banners(window, expand: bool = True) -> list:
    """Every notification in the panel; piles are pressed open first so each shows on its own."""
    def is_note(e):
        return _attr(e, "AXSubrole") in (BANNER, STACK)
    notes = _find_all(window, is_note, [])
    if expand:
        pressed = set()
        for _ in range(12):
            stack = next((n for n in notes if _attr(n, "AXSubrole") == STACK
                          and _attr(n, "AXIdentifier") not in pressed), None)
            if stack is None:
                break
            pressed.add(_attr(stack, "AXIdentifier"))
            _perform(stack, "AXPress")
            time.sleep(0.35)
            notes = _find_all(window, is_note, [])
    return notes


def _refresh_times() -> None:
    """The panel shows "5m ago" only on some notifications. usernoted logs each delivery - app and
    the UUID's first 8 hex digits, never the text - so the log fills in the rest (last 3 hours)."""
    global _times_until
    now = time.time()
    start = max(_times_until - 60, now - 3 * 3600)
    try:
        out = subprocess.run(["/usr/bin/log", "show", "--start", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start)),
                              "--style", "compact", "--predicate",
                              'process == "usernoted" AND eventMessage BEGINSWITH "Delivering"'],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError) as error:
        log.info("notification times: %s", error)
        return
    for m in re.finditer(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ .*? uuid:"([0-9A-F]{8})"', out, re.M):
        _times[m[2]] = time.mktime(time.strptime(m[1], "%Y-%m-%d %H:%M:%S"))
    _times_until = now


def _from_panel(expand: bool = True) -> list[dict]:
    lookup = threading.Thread(target=_refresh_times, daemon=True)
    lookup.start()                     # alongside the panel, which takes about as long
    with _Panel() as panel:
        items = [_item(e) for e in _banners(panel.window, expand)]
    if any(not i["when"] for i in items):
        lookup.join(timeout=3)
        for item in items:
            at = _times.get(item["id"][:8])
            if not item["when"] and at:
                item["when"] = _ago(time.time() - at)
    return items


# --- reading the database (Full Disk Access) ---------------------------------------------------------

def _ago(seconds: float) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return "now"
    for size, unit in ((86400 * 7, "w"), (86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "now"


def _app_display(bundle: str) -> str:
    try:
        import AppKit
        url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle)
        if url is not None:
            return str(AppKit.NSFileManager.defaultManager().displayNameAtPath_(url.path())).removesuffix(".app")
    except Exception:
        pass
    return bundle.split(".")[-1] if bundle else "an app"


def _from_db(path: str = DB, limit: int = 200) -> list[dict] | None:
    """None when the database cannot be read (no Full Disk Access, or a format we don't know)."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return None
    try:
        rows = con.execute("SELECT app.identifier, record.uuid, record.data, record.delivered_date "
                           "FROM record JOIN app ON app.app_id = record.app_id "
                           "ORDER BY record.delivered_date DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error as error:
        log.info("notifications db: %s", error)
        return None
    finally:
        con.close()
    now, names, items = time.time(), {}, []
    for bundle, raw_uuid, data, delivered in rows:
        try:
            plist = plistlib.loads(data) if data else {}
        except Exception:
            plist = {}
        req = plist.get("req") or {}
        if bundle not in names:
            names[bundle] = _app_display(bundle or plist.get("app") or "")
        try:
            ident = str(uuidlib.UUID(bytes=bytes(raw_uuid))).upper() if raw_uuid and len(raw_uuid) == 16 else ""
        except (TypeError, ValueError):
            ident = ""
        at = (delivered or plist.get("date") or 0) + MAC_EPOCH
        items.append({"id": ident, "app": names[bundle], "bundle": bundle or "",
                      "title": str(req.get("titl") or ""), "subtitle": str(req.get("subt") or ""),
                      "body": str(req.get("body") or ""), "when": _ago(now - at) if at > MAC_EPOCH else "",
                      "at": at, "stacked": False, "element": None})
    return items


# --- the actions --------------------------------------------------------------------------------------

def _read(expand: bool = True) -> list[dict]:
    """Newest first. The database when it can be read, else the panel."""
    items = _from_db()
    if items is not None:
        return items
    if not _trusted():
        raise PermissionError(NO_ACCESS)
    return _from_panel(expand)


def _matches(item: dict, app: str, query: str) -> bool:
    if app and app.lower() not in item["app"].lower() and app.lower() not in item.get("bundle", "").lower():
        return False
    if query:
        text = " ".join((item["app"], item["title"], item["subtitle"], item["body"])).lower()
        return all(word in text for word in query.lower().split())
    return True


def _line(n: int, item: dict) -> str:
    head = item["title"] or item["subtitle"]
    if item["subtitle"] and item["title"] and item["subtitle"] != item["title"]:
        head = f"{item['title']} - {item['subtitle']}"
    body = re.sub(r"\s+", " ", item["body"]).strip()
    if len(body) > 160:
        body = body[:157] + "..."
    when = f" ({item['when']})" if item["when"] else ""
    return f"{n}. {item['app']}: {head}{': ' + body if body and head else body}{when}"


def _remember(items: list[dict]) -> None:
    global _last
    _last = [{k: v for k, v in item.items() if k != "element"} for item in items]


def _card(title: str, items: list[dict], more: int = 0) -> None:
    """A card on the island - only inside Mint (tests never import the island)."""
    if "mint.ui.island" not in sys.modules:
        return
    from mint.tools import cards
    cards.show(title, items=[{"title": f"{i['app']}: {i['title'] or i['subtitle']}"[:60],
                              "detail": (i["body"] or i["subtitle"])[:80], "trailing": i["when"],
                              "icon": "bell.fill"} for i in items[:8]],
               icon="bell.badge.fill", tint="orange", more=more)


def listing(app: str = "", query: str = "", limit: int = 8) -> str:
    items = [i for i in _read() if _matches(i, app, query)]
    what = " ".join(x for x in (f"from {app}" if app else "", f"about '{query}'" if query else "") if x)
    if not items:
        return f"No notifications {what}.".replace("  ", " ").replace(" .", ".")
    shown = items[:max(1, limit)]
    _remember(shown)
    _card("Notifications" + (f" · {app}" if app else ""), shown, len(items) - len(shown))
    head = f"{len(items)} notification{'s' if len(items) != 1 else ''}{' ' + what if what else ''}"
    more = f"\n(and {len(items) - len(shown)} more)" if len(items) > len(shown) else ""
    return head + ", newest first:\n" + "\n".join(_line(n, i) for n, i in enumerate(shown, 1)) + more


def summary() -> str:
    items = _read()
    if not items:
        return "No notifications - you haven't missed anything."
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["app"], []).append(item)
    order = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    lines = []
    for name, group in order:
        latest = group[0]
        what = ": ".join(x for x in (latest["title"] or latest["subtitle"], latest["body"][:60]) if x)
        when = f", {latest['when']}" if latest["when"] else ""
        lines.append(f"- {name}: {len(group)} (latest: {what}{when})")
    _remember(items[:20])
    _card("You missed", [g[0] for _, g in order], max(0, len(order) - 8))
    return (f"{len(items)} notification{'s' if len(items) != 1 else ''} from {len(groups)} "
            f"app{'s' if len(groups) != 1 else ''}:\n" + "\n".join(lines) +
            "\nSay the gist in a sentence; offer to read any app's in full.")


def _pick(items: list[dict], index, match: str, app: str) -> dict | None:
    """By number from the last list read out, by words, or the newest (of an app)."""
    if index not in (None, ""):
        try:
            n = int(float(index))
        except (TypeError, ValueError):
            n = 0
        if 1 <= n <= len(_last):
            want = _last[n - 1]
            return next((i for i in items if want["id"] and i["id"] == want["id"]), None) or \
                next((i for i in items if (i["app"], i["title"], i["body"]) ==
                      (want["app"], want["title"], want["body"])), None)
        return None
    pool = [i for i in items if _matches(i, app, match)]
    if not match and not app and len(_last) == 1:
        pool = [i for i in items if i["id"] == _last[0]["id"]] or pool
    return pool[0] if pool else None


def _describe(item: dict) -> str:
    words = ": ".join(x for x in (item["title"] or item["subtitle"], item["body"][:40]) if x)
    return f"the {item['app']} notification '{words}'"


def _act(action: str, index=None, match: str = "", app: str = "", text: str = "", send: bool = False) -> str:
    if not _trusted():
        return NO_ACCESS if action != "reply" else NO_ACCESS.split(" Full Disk")[0]
    with _Panel() as panel:
        items = [_item(e) for e in _banners(panel.window)]
        if not items:
            return "There are no notifications."
        item = _pick(items, index, match, app)
        if item is None:
            return "I couldn't find that notification - it may have been cleared. Ask me to list them again."
        element = item["element"]
        if action == "open":
            name = _named_action(element, "Show") or "AXPress"
            ok = _perform(element, name)
            return f"Opened {_describe(item)}." if ok else f"Couldn't open {_describe(item)}."
        if action == "dismiss":
            name = _named_action(element, "Close") or _named_action(element, "Clear All")
            ok = bool(name) and _perform(element, name)
            _forget(item["id"])
            return f"Dismissed {_describe(item)}." if ok else f"Couldn't dismiss {_describe(item)}."
        if action == "reply":
            return _reply(panel, item, text, send)
    return "Unknown action."


def _forget(ident: str) -> None:
    global _last
    _last = [i for i in _last if i["id"] != ident]


def _reply(panel: _Panel, item: dict, text: str, send: bool) -> str:
    """Types into the notification's inline reply box. Sends only when asked to (send=true)."""
    element = item["element"]
    name = _named_action(element, "Reply")
    if not name:
        return f"The {_describe(item)[4:]} has no reply box - open it and reply in the app instead."
    if not text.strip():
        return "What should the reply say?"
    _perform(element, name)
    field = None
    for _ in range(20):
        time.sleep(0.1)
        field = _find(panel.window, lambda e: _attr(e, "AXRole") in ("AXTextField", "AXTextArea"), limit=14)
        if field is not None:
            break
    if field is None:
        return "The reply box didn't open."
    _ax().AXUIElementSetAttributeValue(field, "AXFocused", True)
    if _ax().AXUIElementSetAttributeValue(field, "AXValue", text) != 0:
        return "Couldn't type into the reply box."
    if not send:
        panel.keep = True              # the typed reply stays on screen for the user to check
        return (f"Typed the reply to {_describe(item)} but did NOT send it: \"{text}\". "
                "Ask the user; if they say send, call reply again with send=true.")
    button = _find(panel.window, lambda e: _attr(e, "AXRole") == "AXButton"
                   and str(_attr(e, "AXDescription") or _attr(e, "AXTitle") or "").lower() in ("send", "reply"),
                   limit=14)
    ok = _perform(button, "AXPress") if button is not None else _perform(field, "AXConfirm")
    return f"Sent the reply to {_describe(item)}." if ok else "Couldn't send the reply; it is still typed there."


def clear_all(app: str = "", query: str = "") -> str:
    """Closes each notification (of one app, or matching words) - never touches anything else."""
    if not _trusted():
        return NO_ACCESS.split(" Full Disk")[0]
    closed: set[str] = set()
    failed = 0
    with _Panel() as panel:
        for _ in range(200):
            # A closed one stays in the tree while it animates away: never count it twice.
            items = [i for i in (_item(e) for e in _banners(panel.window))
                     if i["id"] not in closed and _matches(i, app, query)]
            if not items:
                break
            name = _named_action(items[0]["element"], "Close")
            if not name or not _perform(items[0]["element"], name):
                failed += 1
                if failed > 3:
                    break
                continue
            closed.add(items[0]["id"])
            time.sleep(0.15)
    closed_count = len(closed)
    _remember([])
    scope = " ".join(x for x in (f"from {app}" if app else "", f"about '{query}'" if query else "") if x)
    if not closed_count:
        return f"There were no notifications{' ' + scope if scope else ''} to clear."
    rest = " Some wouldn't close." if failed else ""
    return f"Cleared {closed_count} notification{'s' if closed_count != 1 else ''}{' ' + scope if scope else ''}.{rest}"


# --- the tool ------------------------------------------------------------------------------------------

PROMPT = """Notifications: "what did I miss?" / "any notifications?" -> notifications action=summary; "read my \
notifications" -> action=list; "anything from Slack?" -> action=list app=Slack (query= for words in them). Say the \
gist briefly - don't read every line unless asked. "open that one" / "open the second one" -> action=open with \
index (from the list you just read) or match (words). "dismiss it" -> action=dismiss; "clear my notifications" -> \
action=clear_all (app= to clear only one app's). Reply only when the user asks to reply: action=reply with text; it \
types but does not send - read it back and call again with send=true only after they say send."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="notifications",
        description=("The user's macOS notifications (Notification Center): what they missed, read them, filter by "
                     "app or words, open one, dismiss one, clear them, or reply inline (Messages/Slack). Actions: "
                     "summary (grouped by app - 'what did I miss'), list, open, dismiss, clear_all, reply."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["summary", "list", "open", "dismiss", "clear_all", "reply"]),
            "app": types.Schema(type=S, description="only this app's, e.g. 'Slack', 'Messages', 'Mail'"),
            "query": types.Schema(type=S, description="list/clear_all: words that must be in the notification"),
            "index": types.Schema(type=types.Type.NUMBER,
                                  description="open/dismiss/reply: its number in the list you last read out"),
            "match": types.Schema(type=S, description="open/dismiss/reply: words from it, e.g. the sender"),
            "text": types.Schema(type=S, description="reply: what to reply"),
            "send": types.Schema(type=types.Type.BOOLEAN,
                                 description="reply: true only after the user confirmed the typed reply"),
            "limit": types.Schema(type=types.Type.NUMBER, description="list: how many (default 8)")},
            required=["action"]))]


def tool(args: dict) -> str:
    action = str(args.get("action") or "summary").lower()
    app, query = str(args.get("app") or "").strip(), str(args.get("query") or "").strip()
    try:
        with _lock:
            if action == "list":
                return listing(app, query, int(args.get("limit") or 8))
            if action in ("open", "dismiss", "reply"):
                return _act(action, args.get("index"), str(args.get("match") or query), app,
                            str(args.get("text") or ""), bool(args.get("send")))
            if action == "clear_all":
                return clear_all(app, query)
            return summary()
    except PermissionError as error:
        return str(error)
    except RuntimeError as error:
        return str(error)
    except Exception as error:
        log.exception("notifications")
        try:
            close()
        except Exception:
            pass
        return f"Notifications failed: {error}"


HANDLERS = {"notifications": tool}
