"""Settings ▸ Accounts & connections, the lower half: the apps and services Mint works with (connectors.py),
and making new ones (connector_maker.py). (It was its own page, Connectors; show("connectors") still lands here.)

    Connected apps          ready now: Test (a read-only check), or where its settings live
    Available on this Mac   installed but not set up: Connect (asks macOS / opens the right settings), Make…
                            for apps a connector can be made for; advanced: every other scriptable app
    Your connectors         made from plain words: Test, Remove (advanced, or when there are some)
    Connect any app         advanced: "Integrate an app, e.g. Things": Plan it (read back here), then Create
    Not on this Mac         advanced: not installed: Get… opens where to get it

The page is built from a quick snapshot (bundle ids, permission states that never prompt), read off
the main thread by `facts` (Settings shows "Loading…" until it is there); a fuller check (Mail's
accounts, the scriptable-app scan) runs in the background and refreshes the page once.
"""

from __future__ import annotations

import threading
import time

import AppKit
import objc
from PyObjCTools import AppHelper

from mint.tools import connectors
from mint.core import prefs

ICON = 28
STATUS_W = 170
BUTTONS_W = 96
FIRST_SCRIPTABLE = 6
_COLORS = {"connected": AppKit.NSColor.systemGreenColor, "setup": AppKit.NSColor.systemOrangeColor,
           "missing": AppKit.NSColor.tertiaryLabelColor, "scriptable": AppKit.NSColor.systemBlueColor}


def _text_width(text: str, size: float) -> float:
    font = AppKit.NSFont.systemFontOfSize_(size)
    return float(AppKit.NSString.stringWithString_(text).sizeWithAttributes_({AppKit.NSFontAttributeName: font}).width)


def _icon(card, row: dict, x: float, y: float) -> None:
    """The app's own icon when it is installed, else the connector's SF Symbol on a soft accent tile."""
    if row.get("path"):
        image = AppKit.NSWorkspace.sharedWorkspace().iconForFile_(row["path"])
        view = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(x - 2, y - 2, ICON + 4, ICON + 4))
        view.setImage_(image)
        view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        card.addSubview_(view)
        return
    from mint.ui.settings import _cgc
    accent = AppKit.NSColor.controlAccentColor()
    tile = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, ICON, ICON))
    tile.setWantsLayer_(True)
    tile.layer().setCornerRadius_(7)
    tile.layer().setBackgroundColor_(_cgc(accent, 0.14))
    card.addSubview_(tile)
    image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(row.get("icon") or
                                                                               "puzzlepiece.extension", None)
    if image is None:
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("puzzlepiece.extension", None)
    config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(14, AppKit.NSFontWeightMedium)
    glyph = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(x + 4, y + 4, ICON - 8, ICON - 8))
    glyph.setImage_(image.imageWithSymbolConfiguration_(config) if image is not None else None)
    glyph.setContentTintColor_(accent)
    card.addSubview_(glyph)


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
    """Settings' reader thread: the snapshot (reused for 2 minutes, unless a connector was just removed)."""
    state = win.__dict__.get("_connectors_ui") or {}
    fresh = bool(state.pop("fresh", False))
    return {"snap": connectors.snapshot(deep=False, max_age=0 if fresh else 120)}


def page(win, page, facts: dict) -> None:
    from mint.ui.settings import _text_height
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
                connectors.installed_apps(fresh=force)
                connectors.snapshot(deep=True)
            except Exception:
                pass
            state["deep_at"], state["deep_busy"] = time.time(), False
            later(refresh)
        background(run, "connectors-deep")

    snap = facts["snap"]
    advanced = win._advanced()
    deep_refresh()

    # --- one row -------------------------------------------------------------------------------------------
    def row(item: dict, buttons: list, state_key: str, detail: str) -> None:
        widths = sum(w for _, w, _ in buttons) + 8 * max(0, len(buttons) - 1)
        control_w = STATUS_W + 12 + max(widths, BUTTONS_W)        # statuses line up, with or without a button
        label_x = 16 + ICON + 12
        label_w = page.width - label_x - 16 - control_w - 12
        hint = state["msgs"].get(item["id"]) or item.get("enables") or ""
        hint_h = _text_height(hint, 11, label_w) if hint else 0      # the whole hint: never cut off with "…"
        height = max(54, 18 + hint_h + 22)
        card, top, x, h = page.row("", height=height, control_w=control_w)
        _icon(card, item, 16, top + (h - ICON) / 2)
        title_y = top + (h - (18 + (hint_h + 2 if hint else 0))) / 2
        win._label(card, item["name"], label_x, title_y, label_w, h=18, size=13)
        if hint:
            note = win._label(card, hint, label_x, title_y + 19, label_w, h=hint_h, size=11,
                              alpha=0.85 if item["id"] in state["msgs"] else 0.55, lines=0)
            if item["id"] in state["msgs"]:
                note.setTextColor_(AppKit.NSColor.controlAccentColor())
            item["_note"] = note
        # status: a coloured dot and the detail, right-aligned before the buttons
        words = detail[:40]
        status = win._label(card, words, x + 14, top + (h - 16) / 2, STATUS_W - 16, h=16, size=11, alpha=0.7)
        status.setAlignment_(AppKit.NSTextAlignmentRight)
        status.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        dot_x = max(x + 2, x + STATUS_W - 2 - _text_width(words, 11) - 16)
        dot = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(dot_x, top + (h - 7) / 2, 7, 7))
        dot.setWantsLayer_(True)
        dot.layer().setCornerRadius_(3.5)
        from mint.ui.settings import _cgc
        dot.layer().setBackgroundColor_(_cgc(_COLORS.get(state_key, AppKit.NSColor.tertiaryLabelColor)()))
        card.addSubview_(dot)
        bx = x + STATUS_W + 12 + max(widths, BUTTONS_W) - widths
        for text, w, handler in buttons:
            win._button(card, text, bx, top + (h - 28) / 2, w, handler)
            bx += w + 8

    def say(item: dict, words: str) -> None:
        state["msgs"][item["id"]] = words
        note = item.get("_note")
        if note is not None:
            AppHelper.callAfter(note.setStringValue_, words)
            AppHelper.callAfter(note.setTextColor_, AppKit.NSColor.controlAccentColor())

    def connect(item: dict) -> None:
        lib = connectors.BY_ID.get(item["id"])
        if lib is None:
            return
        if lib.settings_page and item["state"] != "missing":
            win.select(lib.settings_page, anchor=lib.id)          # that page, scrolled to its part
            return
        say(item, "Connecting…")

        def run() -> None:
            try:
                words = lib.connect()
            except Exception as error:
                words = f"Couldn't: {error}"
            say(item, words)
            deep_refresh(force=True)
        background(run, "connector-connect")

    def test(item: dict) -> None:
        say(item, "Testing…")

        def run() -> None:
            try:
                if item.get("custom"):
                    from mint.tools import connector_maker
                    found = connectors.custom(item["id"])
                    ok, out = connector_maker.test(found) if found else (False, "it was removed")
                    words = ("Works: " if ok else "Didn't work: ") + " ".join(out.split())[:160]
                else:
                    words = " ".join((connectors.BY_ID[item["id"]].test() or "No test for this one.").split())[:160]
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

    def open_app(item: dict) -> None:
        AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.fileURLWithPath_(item["path"]))

    def buttons_for(item: dict) -> list:
        lib = connectors.BY_ID.get(item["id"])
        if item["state"] == "connected":
            if item.get("testable"):
                return [("Test", 80, lambda i=item: test(i))]
            if lib and lib.settings_page:
                return [("Settings…", 96, lambda i=item: connect(i))]
            if item.get("path"):
                return [("Open", 80, lambda i=item: open_app(i))]
            return []
        if item["state"] == "setup":
            if item.get("makeable") and item.get("path"):
                return [("Make…", 80, lambda i=item: make(f"integrate {i['name']}"))]
            return [("Settings…" if lib and lib.settings_page else "Connect", 96 if lib and lib.settings_page else 88,
                     lambda i=item: connect(i))]
        if item.get("site"):
            return [("Get…", 72, lambda i=item: connectors.open_url(i["site"]))]
        return []

    library = snap["library"]
    connected = [r for r in library if r["state"] == "connected"]
    setup = [r for r in library if r["state"] == "setup"]
    missing = [r for r in library if r["state"] == "missing"]

    # --- Connected ---
    page.section("Connected apps")
    page.text(f"Apps and services {name} can use right now. Everything happens on this Mac - through macOS accounts, "
              "app scripting and links - with no server in between.", size=12, alpha=0.7)
    for item in map(dict, connected):
        row(item, buttons_for(item), "connected", item["detail"])
    page.end("Test runs a read-only check. Mint never sends a message or email, pays, or deletes anything through "
             "a connector unless you asked for exactly that.")

    # --- Available on this Mac ---
    page.section("Available on this Mac")
    scriptable = snap["scriptable"] if advanced else []
    if not setup and not scriptable:
        page.text("Everything installed is connected." if not state["deep_busy"] else "Checking your apps…",
                  size=12, alpha=0.7)
    for item in map(dict, setup):
        row(item, buttons_for(item), "setup", item["detail"])
    if scriptable:
        shown = scriptable if state["all"] else scriptable[:FIRST_SCRIPTABLE]
        for app in shown:
            item = {"id": "app:" + app["bundle_id"], "name": app["name"], "path": app["path"],
                    "enables": "Scriptable: Mint can make a connector for it."}
            row(item, [("Make…", 80, lambda a=app: make(f"integrate {a['name']}"))], "scriptable", "Scriptable")
        if len(scriptable) > FIRST_SCRIPTABLE:
            def toggle() -> None:
                state["all"] = not state["all"]
                refresh()
            win._row_buttons(page, "", [("Show fewer" if state["all"] else f"Show all {len(scriptable)}", 130,
                                         toggle)])
    elif state["deep_busy"] and advanced:
        page.text("Looking for scriptable apps…", size=11, alpha=0.55)
    page.end("Connect asks macOS for permission once, or opens the right page of System Settings.")

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
    if making or snap["custom"]:
        page.section("Your connectors")
        if not snap["custom"]:
            page.text(f"None yet. Make one below - {name} reads the app's scripting dictionary and plans a few "
                      "actions for you to check.", size=12, alpha=0.7)
        for item in snap["custom"]:
            acts = item.get("actions") or []
            item = dict(item, enables=(item.get("enables") or "") + (f" · {', '.join(acts[:4])}" if acts else ""))
            row(item, [("Test", 64, lambda i=item: test(i)), ("Remove", 84, lambda i=item: remove(i))],
                item["state"], item["detail"])
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
        plan_button = win._button(card, "Plan it", page.width - 16 - 196, top + 12, 96, lambda: plan_it())
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

    # --- Not on this Mac ---
    if missing and advanced:
        page.section("Not on this Mac")
        page.text(f"Install one and {name} picks it up.", size=12, alpha=0.7)
        for item in map(dict, missing):
            row(item, buttons_for(item), "missing", item["detail"])
        page.end()
