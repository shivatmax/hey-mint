"""Settings ▸ Accounts & connections, the lower half: the apps Mint works with (app_library.py over connectors.py),
and making new connectors (connector_maker.py). (It was its own page, Connectors; show("connectors") lands here.)

    Connected            what works now: compact rows, the app's icon and a ✓ badge (the first few; +N more opens
                         the app library window)
    On your Mac          installed apps Mint can work with, not connected yet: what it does, Connect… (the first
                         few; Show all opens the library at "On your Mac")
    Suggested for you    common apps that aren't on this Mac: Get it, and Use in browser when Mint can
    Accounts             Google, iCloud, Microsoft, Telegram - only while one isn't set up
    Browse all apps…     the app library window (library_window.py), with search and categories
    Your connectors      advanced: made from plain words: Test, Remove
    Connect any app      advanced: "Integrate an app, e.g. Things": Plan it (read back here), then Create

The page is built from app_library.groups(), read off the main thread by `facts` (Settings shows "Loading…" until
it is there); the scan for every scriptable app runs in the background (app_library.warm) and refreshes the page
once. Icons: the installed app's own icon, else its brand (brands.py), else a letter tile - as in the library.
"""

from __future__ import annotations

import threading
import time

import AppKit
import objc
from PyObjCTools import AppHelper

from mint.tools import connectors
from mint.core import prefs
from mint.ui import settings_art

ICON = 28
STATUS_W = 150
BUTTONS_W = 96
FIRST = 6                      # rows per group before "+N more" / Show all
# A row's state -> the colour of its status badge (settings_window.TONES).
_TONES = {"connected": "ok", "ready": "info", "setup": "warn", "get": "off", "web": "off", "missing": "off",
          "scriptable": "info"}


def _icon(card, row: dict, x: float, y: float, size: float = ICON) -> None:
    """The app's own icon when it is on this Mac, else its brand (brands.py), else a letter tile on its colour -
    the app library window's own helper, so a brand looks the same in both places."""
    try:
        from mint.ui.library_window import LibraryWindow
        LibraryWindow._icon(None, card, row, x, y, size)
        return
    except Exception:
        pass
    from mint.ui import brands
    brands.icon_tile(card, x, y, row.get("slug") or "", size)


def _scroll_view(win):
    for view in win.window.contentView().subviews() if win.window is not None else []:
        if isinstance(view, AppKit.NSScrollView):
            return view
    return None


def _refresh_keep(win, to: float | None = None) -> None:
    """Rebuild the page where the user was scrolled (Settings' refresh starts at the top), or at `to`."""
    view = _scroll_view(win)
    y = float(view.contentView().bounds().origin.y) if view is not None else 0.0
    win.refresh()
    view = _scroll_view(win)
    if view is not None:
        view.documentView().scrollPoint_(AppKit.NSMakePoint(0, y if to is None else to))


def facts(win) -> dict:
    """Settings' reader thread: the app library's groups and the connectors made here (reused for a minute or
    two, unless something was just connected or removed)."""
    from mint.tools import app_library
    state = win.__dict__.get("_connectors_ui") or {}
    fresh = bool(state.pop("fresh", False))
    if fresh:
        app_library.invalidate()
    snap = connectors.snapshot(deep=False, max_age=0 if fresh else 120)
    return {"snap": snap, "groups": app_library.groups()}


def page(win, page, facts: dict) -> None:
    from mint.ui.settings import _text_height, _text_width
    name = prefs.name()
    state = win.__dict__.setdefault("_connectors_ui", {
        "desc": "", "plan": None, "status": "", "msgs": {}, "deep_at": 0.0, "deep_busy": False, "all": False,
        "planning": False, "creating": False})

    def later(fn) -> None:
        def run() -> None:
            if win.showing("connectors"):
                fn()
        AppHelper.callAfter(run)

    def refresh() -> None:
        to = state.get("jump_y") if state.pop("jump", False) else None     # after Plan it: show the plan
        _refresh_keep(win, to)

    def background(fn, label: str) -> None:
        threading.Thread(target=fn, daemon=True, name=f"settings-{label}").start()

    def deep_refresh(force: bool = False) -> None:
        if state["deep_busy"] or (not force and time.time() - state["deep_at"] < 90):
            return
        state["deep_busy"] = True

        def run() -> None:
            try:
                from mint.tools import app_library
                connectors.installed_apps(fresh=force)
                app_library.all_rows(fresh=force, deep=True)
            except Exception:
                pass
            state["deep_at"], state["deep_busy"] = time.time(), False
            later(refresh)
        background(run, "connectors-deep")

    # --- one row -------------------------------------------------------------------------------------------
    def row(item: dict, buttons: list, detail: str, compact: bool = False) -> None:
        """Icon, name, what Mint does with it (unless compact), a status badge, and its buttons."""
        buttons = [(text, settings_art.button_width(text, w), *rest) for text, w, *rest in buttons]   # fit titles
        widths = sum(b[1] for b in buttons) + 8 * max(0, len(buttons) - 1)
        words = ((detail[:40] + " ✓") if item.get("state") == "connected" else detail[:40]) if detail else ""
        # the badge as wide as its words (never "Add in Internet Accou…"), at least STATUS_W
        badge_w = min(240.0, max(STATUS_W, _text_width(words, 11, bold=True) + 20)) if detail else 0
        status_w = badge_w                                       # no badge: the words get its room
        control_w = status_w + (12 if status_w and buttons else 0) + (max(widths, BUTTONS_W) if buttons else 0)
        label_x = 16 + ICON + 12
        label_w = page.width - label_x - 16 - control_w - 12
        hint = state["msgs"].get(item["id"]) or ("" if compact else item.get("what") or item.get("enables") or "")
        hint_h = _text_height(hint, 11, label_w) if hint else 0      # the whole hint: never cut off with "…"
        height = max(40 if compact and not hint else 54, 18 + hint_h + 22)
        card, top, x, h = page.row("", height=height, control_w=control_w)
        _icon(card, item, 16, top + (h - ICON) / 2)
        title_y = top + (h - (18 + (hint_h + 2 if hint else 0))) / 2
        win._label(card, item["name"], label_x, title_y, label_w, h=18, size=13).setLineBreakMode_(
            AppKit.NSLineBreakByTruncatingTail)
        if hint:
            note = win._label(card, hint, label_x, title_y + 19, label_w, h=hint_h, size=11,
                              alpha=0.85 if item["id"] in state["msgs"] else 0.55, lines=0)
            if item["id"] in state["msgs"]:
                note.setTextColor_(AppKit.NSColor.controlAccentColor())
            item["_note"] = note
        from mint.ui.settings import _badge
        if detail:
            badge = _badge(card, words, x + badge_w, top + h / 2, _TONES.get(item.get("state"), "off"),
                           max_w=badge_w)
            badge.setToolTip_(detail)
        bx = x + control_w - widths
        for text, w, handler, *style in buttons:
            win._button(card, text, bx, top + (h - 28) / 2, w, handler, primary=bool(style and style[0]))
            bx += w + 8

    def say(item: dict, words: str) -> None:
        state["msgs"][item["id"]] = words
        note = item.get("_note")
        if note is not None:
            AppHelper.callAfter(note.setStringValue_, words)
            AppHelper.callAfter(note.setTextColor_, AppKit.NSColor.controlAccentColor())

    def ask_on_main(plan: dict) -> bool:
        """app_library.connect's confirm: the plan as an alert, on the main thread (this runs on a worker)."""
        from mint.tools import app_library
        title, body = app_library.plan_summary(plan)
        done, answer = threading.Event(), [False]

        def show() -> None:
            try:
                alert = AppKit.NSAlert.alloc().init()
                alert.setMessageText_(title)
                alert.setInformativeText_(body)
                alert.addButtonWithTitle_("Connect")
                alert.addButtonWithTitle_("Cancel")
                answer[0] = alert.runModal() == AppKit.NSAlertFirstButtonReturn
            finally:
                done.set()
        AppHelper.callAfter(show)
        done.wait(600)
        return answer[0]

    def connect(item: dict) -> None:
        """Connect / Set up / Get it: app_library.connect, on a thread (it may wait for macOS or the planner)."""
        from mint.tools import app_library
        say(item, "Connecting…" if item.get("action") == "connect" else "Opening…")

        def run() -> None:
            words = app_library.connect(item["id"], confirm=ask_on_main, say=lambda w: say(item, w))
            say(item, words)
            state["fresh"] = True
            deep_refresh(force=True)
        background(run, "app-connect")

    def in_browser(item: dict) -> None:
        from mint.tools import app_library

        def run() -> None:
            say(item, app_library.use_in_browser(item["id"], confirm=ask_on_main, say=lambda w: say(item, w)))
            state["fresh"] = True
            later(refresh)
        background(run, "app-browser")

    def library(category=None) -> None:
        from mint.ui import library_window
        library_window.open_library(category)

    def test(item: dict) -> None:
        say(item, "Testing…")

        def run() -> None:
            try:
                from mint.tools import connector_maker
                found = connectors.custom(item["id"])
                ok, out = connector_maker.test(found) if found else (False, "it was removed")
                words = ("Works: " if ok else "Didn't work: ") + " ".join(out.split())[:160]
            except Exception as error:
                words = f"Didn't work: {error}"
            say(item, words)
        background(run, "connector-test")

    def remove(item: dict) -> None:
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(f"Remove the {item['name']} connector?")
        alert.setInformativeText_("Its file goes to the Trash; you can make it again any time.")
        alert.addButtonWithTitle_("Remove")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return
        connectors.remove_custom(item["id"])
        state["fresh"] = True                        # the page's next read takes a new snapshot
        refresh()

    def make(words: str) -> None:
        state["desc"] = words
        plan_it(words)

    groups = facts.get("groups") or {}

    def more_row(words: str, button: str, handler) -> None:
        win._row_buttons(page, "", [(button, max(110, 22 + 7.5 * len(button)), handler)], hint=words)

    # --- Connected ---
    connected = list(groups.get("connected") or [])
    page.section("Connected apps")
    page.text(f"Apps and services {name} can use right now - all on this Mac, through macOS accounts, app "
              "scripting and links, with no server in between.", size=12, alpha=0.7)
    def check(item: dict) -> None:
        from mint.tools import app_library
        say(item, "Testing…")
        later(refresh)                               # a compact row grows a line for what the test says

        def run() -> None:
            say(item, app_library.check(item["id"]))
            later(refresh)
        background(run, "app-check")

    for item in map(dict, connected[:FIRST]):
        row(item, [("Test", 64, lambda i=item: check(i))], item.get("detail") or "Ready", compact=True)
    if len(connected) > FIRST:
        more_row(", ".join(r["name"] for r in connected[FIRST:FIRST + 5]) + ("…" if len(connected) > FIRST + 5
                                                                               else ""),
                 f"+{len(connected) - FIRST} more", library)
    if not connected:
        page.text("Nothing yet - connect one below.", size=12, alpha=0.6)
    page.end("Mint never sends a message or email, pays, or deletes anything through an app unless you asked for "
             "exactly that.")

    # --- On your Mac ---
    on_mac = list(groups.get("on_mac") or [])
    page.section("On your Mac", icon=("desktopcomputer", (0.2, 0.6, 1.0)))
    if not on_mac:
        page.text("Everything Mint knows on this Mac is connected." if not state["deep_busy"] else
                  "Checking your apps…", size=12, alpha=0.7)
    for item in map(dict, on_mac[:FIRST]):
        row(item, [(f"{item.get('button') or 'Connect'}…", 100, lambda i=item: connect(i), True)],
            item.get("how_words") or "")
    if len(on_mac) > FIRST:
        more_row(f"{len(on_mac) - FIRST} more apps on this Mac Mint can work with.", f"Show all {len(on_mac)}",
                 lambda: library("mac"))
    page.end("Connect asks macOS once, or shows what Mint will be able to do before anything is saved.")

    # --- Suggested for you ---
    suggested = list(groups.get("suggested") or [])
    if suggested:
        page.section("Suggested for you", icon=("star.fill", (1.0, 0.7, 0.1)))
        for item in map(dict, suggested):
            if item.get("state") == "web":
                buttons = [("Use in browser", 124, lambda i=item: in_browser(i), True)]
            else:
                buttons = [("Get it…", 84, lambda i=item: connect(i), True)]
                if item.get("browser_ok"):
                    buttons.insert(0, ("Use in browser", 124, lambda i=item: in_browser(i)))
            row(item, buttons, "")
        page.end("Get it opens the App Store or the maker's page. Use in browser: Mint works in the web app, where "
                 "you are signed in.")

    # --- Accounts not set up ---
    accounts = list(groups.get("accounts") or [])
    if accounts:
        page.section("Accounts", icon=("person.crop.circle.fill", (0.55, 0.45, 0.95)))
        for item in map(dict, accounts):
            row(item, [(f"{item.get('button') or 'Set up'}…", 100, lambda i=item: connect(i), True)],
                item.get("detail") or "Not set up")
        page.end()

    page.section()
    count = sum(len(groups.get(k) or []) for k in ("connected", "on_mac", "get", "browser", "more_on_mac"))
    win._row_buttons(page, "Every app Mint can work with", [("Browse all apps…", 160, lambda: library(), True)],
                     hint=f"{count} apps, by category, with search." if count else "By category, with search.",
                     icon="library")
    page.end()

    snap = facts.get("snap") or {"custom": []}
    advanced = win._advanced()
    deep_refresh()

    # Making connectors is advanced - but a plan already under way (a Make… click) stays on screen.
    making = advanced or bool(state["plan"] or state["planning"] or state["creating"])
    views = {"field": None, "status": None}

    def plan_it(words: str | None = None) -> None:
        field, status = views["field"], views["status"]
        desc = (words if words is not None else str(field.stringValue()) if field is not None else "").strip()
        state["desc"] = desc
        if not desc:
            if status is not None:
                status.setStringValue_("Name an app first, e.g. “Things”.")
            return
        state.update(planning=True, status=f"Planning… (reading {desc.removeprefix('integrate ')})", plan=None,
                     jump=True)
        refresh()

        def run() -> None:
            from mint.tools import connector_maker
            try:
                text = desc if desc.lower().startswith(("integrate", "connect")) else f"integrate {desc}"
                state["plan"] = connector_maker.plan(text)
                state["status"] = ""
            except Exception as error:
                state["status"] = f"Couldn't plan it: {error}"
            state.update(planning=False, jump=True)
            later(refresh)
        background(run, "connector-plan")

    def create() -> None:
        plan = state["plan"]
        if not plan or not plan.get("actions"):
            return
        state.update(creating=True, status="Saving and testing one read-only action (macOS may ask once)…")
        if views["status"] is not None:
            views["status"].setStringValue_(state["status"])

        def run() -> None:
            from mint.tools import connector_maker
            said = connector_maker.create(plan["id"])
            state.update(creating=False, status=said.removeprefix("DONE: ").removeprefix("FAILED: "))
            connectors.snapshot(deep=False)
            later(refresh)
        background(run, "connector-create")

    # --- Your connectors ---
    if making:
        page.section("Your connectors")
        if not snap["custom"]:
            page.text(f"None yet. Make one below - {name} reads the app's scripting dictionary and plans a few "
                      "actions for you to check.", size=12, alpha=0.7)
        for item in snap["custom"]:
            acts = item.get("actions") or []
            item = dict(item, enables=(item.get("enables") or "") + (f" · {', '.join(acts[:4])}" if acts else ""))
            row(dict(item, state="connected"), [("Test", 64, lambda i=item: test(i)),
                                                ("Remove", 84, lambda i=item: remove(i))], item["detail"])
        page.end("Saved in ~/Library/Application Support/Mint/connectors. Say “what can you connect to?” to hear "
                 "them.")

    # --- Connect any app ---
    if making:
        state["jump_y"] = max(0.0, page.y - 40)
        page.section("Connect any app")
        page.text(f"Name an app on this Mac or a web service. {name} reads what the app can be told to do (its "
                  "scripting dictionary and links) and plans a small connector; nothing is saved until Create.",
                  size=12, alpha=0.7)
        card, top, x, h = page.row("", height=52, control_w=page.width - 32)
        field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(16, top + 14, page.width - 32 - 212, 24))
        field.setStringValue_(state["desc"])
        field.setPlaceholderString_("Integrate an app, e.g. Things")
        field.setBezelStyle_(AppKit.NSTextFieldRoundedBezel)
        field.cell().setUsesSingleLineMode_(True)
        field.cell().setScrollable_(True)
        field.setDelegate_(win.target)
        win._handlers[objc.pyobjc_id(field)] = lambda c: state.update(desc=str(c.stringValue()))
        card.addSubview_(field)
        views["field"] = field
        plan_button = win._button(card, "Plan it", page.width - 16 - 196, top + 12, 96, lambda: plan_it(), primary=True)
        result = state["plan"]
        ready = bool(result and result.get("actions") and not result.get("created"))
        create_button = win._button(card, "Create", page.width - 16 - 92, top + 12, 92, lambda: create())
        create_button.setEnabled_(ready and not state["creating"])
        plan_button.setEnabled_(not state["planning"])
        if result:
            from mint.tools import connector_maker
            words = connector_maker.plan_text(result)
            for prefix in ("FAILED: ", "REFUSED: ", "BUILT IN: "):
                words = words.removeprefix(prefix)
            if result.get("actions"):
                words = words.split("\n", 1)[-1] if "\n" in words else words
                words = f"{result['name']}: {result.get('summary') or ''}\n{words}"
            page.text(words, size=12, alpha=0.85)
        views["status"] = page.text(state["status"] or ("Plan it reads the plan back here first." if not result else
                                                         "Not saved yet." if ready else " "), size=11, alpha=0.6)
        page.end("Actions that change things only run when you ask for them. Mint leaves out anything that could "
                 "delete for good, run shell commands that change the system, or type into other apps.")
