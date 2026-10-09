"""Grounding: find the on-screen element the user means, and act on it the way a
person would.

Why this exists. In testing on the ChatGPT app ("create a project called Mint
test"), every older route failed:
  * the desktop tool pressed buttons through Accessibility (AXPress); the
    Chromium-based app ignored it - "no visible effect", three times;
  * click_text found "Create project" twice (the dialog's title and its
    button) and could not tell which one is the button;
  * click_at - the Live model pointing at a screenshot - landed about 40 px
    off, repeatedly;
  * type_text reported "no text field has focus" with the name field open.

What OSWorld-style agents do instead, and what this does:
  1. Inventory. Read every visible element of the front window from the
     accessibility tree (Chromium apps unlocked first, see axkit): role,
     label, exact box, enabled, focused, and whether it sits in an open dialog.
     A dialog, when open, is the only thing the user can use - so it wins.
  2. Choose. Plain label match first (a button called exactly "Create
     project" is not ambiguous with a heading of the same text: roles
     decide). Then Jev over the list. Then, for anything still unclear or with
     no label (icons), a vision model looks at a screenshot with every
     candidate drawn as a numbered box and picks a number - it never invents
     coordinates.
  3. Act like a person. Move the pointer there, pause, press and release a
     real mouse button (Chromium listens to real clicks, not AXPress). For a
     text field: click it, check it has focus, paste, check the value.
  4. Verify. Compare the window before and after, and say what changed - or
     FAILED if nothing did.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import threading
import time

import AppKit
import ApplicationServices as AX
import Quartz

from mint.screen import axkit
from mint.core import config

log = logging.getLogger("mint.screen.ground")

# Measured on this key (one marked ChatGPT screenshot, "which box is Choose
# project?"): 3.5-flash-lite 1.3s right, robotics-er 2.4s right, 3.5-flash
# 2.9s right, 3.6-flash 3.2s right, 3.1-flash-lite 5.8s right; 3-flash-preview,
# 3.8-flash, flash-latest and 3.1-pro were out of quota (429) - and the SDK's
# retries on a 429 hung the first benchmark run for ten minutes. So: a chain of
# models that answer, a hard timeout, and no retries.
# Flash models also have a small daily cap on the free tier (~20; all were
# used up by evening on 24 Sep), so after the lite model come the ones with room.
VISION_MODELS = [m for m in (os.environ.get("MINT_GROUND_MODEL"), "gemini-3.5-flash-lite",
                             "gemini-robotics-er-2-preview", "gemini-3.7-flash", "gemini-3.5-flash",
                             "gemini-3.6-flash", "gemini-3.1-flash-lite") if m]
VISION_MODEL = VISION_MODELS[0]
_dead: dict[str, float] = {}
# Set by chat.py while Mint's own chat panel is open: ui_act then looks there first.
OWN_CHAT_OPEN = False


def _own_panel(pid: int):
    """Mint's own windows are the orb, the bubble, full-screen effect overlays and
    the chat; the focused one is often an overlay. The chat is the one with buttons."""
    best, best_count = None, 0
    for window in axkit.attr(AX.AXUIElementCreateApplication(pid), "AXWindows") or []:
        count = sum(1 for node in axkit.walk(window, limit=300) if axkit.attr(node, "AXRole") in
                    ("AXButton", "AXTextField", "AXTextArea"))
        if count > best_count:
            best, best_count = window, count
    return best or axkit.focused_window(pid)


OWN_CHAT = None      # the ChatPanel, registered by chat.py


def _on_main(fn, timeout: float = 3.0):
    """Run fn on the main thread and wait for its result (AppKit objects live there)."""
    import threading
    from PyObjCTools import AppHelper
    if threading.current_thread() is threading.main_thread():
        return fn()
    box, done = {}, threading.Event()

    def run():
        try:
            box["value"] = fn()
        except Exception as error:
            box["error"] = error
        done.set()
    AppHelper.callAfter(run)
    if not done.wait(timeout):
        raise TimeoutError("the main thread did not answer")
    if "error" in box:
        raise box["error"]
    return box["value"]


def own_controls() -> list[dict]:
    """Mint's own chat controls, read straight from the chat panel - asking
    Accessibility about Mint's own process from inside it found nothing."""
    chat = OWN_CHAT
    if chat is None or chat.window is None:
        return []

    def collect():
        out = []
        stack = [chat.window.contentView()]
        while stack:
            view = stack.pop()
            stack.extend(view.subviews())
            if isinstance(view, AppKit.NSButton) and not view.isHidden():
                label = str(view.toolTip() or view.title() or "")
                if label and label.lower() != "button":
                    out.append({"label": label, "view": view, "kind": "button"})
        return out
    try:
        return _on_main(collect)
    except Exception:
        return []


def own_act(action: str, target: str, text: str = "") -> str:
    chat = OWN_CHAT
    if chat is None or not chat.is_open:
        from mint.core import prefs
        return f"FAILED: {prefs.name()}'s chat is not open; open it first (show_chat)."
    if action == "type":
        def put():
            chat._field.setStringValue_(text)
            return True
        _on_main(put)
        return f"Typed '{text[:60]}' into Mint's chat box."
    controls = own_controls()
    wanted = [w for w in _words(target) if w not in _FILLER and w not in {"mint", "chat", "window", "your"}]
    best, best_score = None, 0.0
    for c in controls:
        have = set(_words(c["label"]))
        score = sum(1 for w in wanted if w in have) / max(len(wanted), 1)
        if score > best_score:
            best, best_score = c, score
    if best is None or best_score < 0.5:
        names = ", ".join(c["label"] for c in controls)
        return f"FAILED: Mint's chat has no '{target}'. Its buttons: {names}."
    _on_main(lambda: best["view"].performClick_(None))
    from mint.core import prefs
    return f"Clicked {prefs.name()}'s '{best['label']}' button."


def own_app():
    return AppKit.NSRunningApplication.currentApplication()


def is_own(app) -> bool:
    return app is not None and app.processIdentifier() == own_app().processIdentifier()      # model -> when it last said 429/503/404


def _client():
    from google import genai
    from google.genai import types
    from mint.core import gemini_keys
    return gemini_keys.client(http_options=types.HttpOptions(timeout=12000,
                                                             retry_options=types.HttpRetryOptions(attempts=1)))


def _generate(contents, models=None, json_mode=True):
    """First model in the chain that answers. -> (text, model)."""
    from google.genai import types
    client = _client()
    last = None
    for model in models or VISION_MODELS:
        if time.monotonic() < _dead.get(model, -1e9):
            continue
        try:
            reply = client.models.generate_content(
                model=model, contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0, **({"response_mime_type": "application/json"} if json_mode else {})))
            return str(reply.text or ""), model
        except Exception as error:
            last = error
            text = str(error)
            if "PerDay" in text:            # the day's quota is gone: do not retry for an hour
                _dead[model] = time.monotonic() + 3600
            elif any(code in text for code in ("429", "503", "404", "RESOURCE_EXHAUSTED", "UNAVAILABLE")):
                _dead[model] = time.monotonic() + 300
            _debug(f"{model} failed: {str(error)[:80]}")
    raise RuntimeError(f"no vision model answered: {str(last)[:120]}")


def _json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    return json.loads(text)

INTERACTIVE = {
    "AXButton", "AXLink", "AXMenuButton", "AXPopUpButton", "AXCheckBox", "AXRadioButton",
    "AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXMenuItem", "AXMenuBarItem",
    "AXDisclosureTriangle", "AXSlider", "AXIncrementor", "AXSwitch", "AXTab", "AXCell", "AXRow",
    "AXColorWell", "AXDockItem",
}
TEXT_INPUT = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
DIALOG_SUBROLES = {"AXDialog", "AXSystemDialog", "AXApplicationDialog", "AXFloatingWindow"}
ROLE_WORDS = {
    "AXButton": "button", "AXLink": "link", "AXMenuButton": "menu button", "AXPopUpButton": "pop-up menu",
    "AXCheckBox": "checkbox", "AXRadioButton": "option", "AXTextField": "text field",
    "AXTextArea": "text area", "AXComboBox": "combo box", "AXSearchField": "search field",
    "AXMenuItem": "menu item", "AXMenuBarItem": "menu", "AXTab": "tab", "AXCell": "cell", "AXRow": "row",
    "AXSwitch": "switch", "AXSlider": "slider", "AXDisclosureTriangle": "disclosure",
    "AXStaticText": "text", "AXImage": "image", "AXGroup": "group", "AXHeading": "heading",
}
_ATTRS = ["AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue",
          "AXHelp", "AXPosition", "AXSize", "AXEnabled", "AXFocused", "AXChildren", "AXRoleDescription"]


def _debug(message: str) -> None:
    if os.environ.get("MINT_DEBUG"):
        print(f"   · ground: {message}", flush=True)


# --- 1. inventory ---------------------------------------------------------------------

def _values(element) -> dict:
    """Many attributes in one round trip - a window has hundreds of elements."""
    try:
        err, values = AX.AXUIElementCopyMultipleAttributeValues(element, _ATTRS, 0, None)
    except Exception:
        err, values = -1, None
    if err != 0 or values is None:
        return {name: axkit.attr(element, name) for name in _ATTRS}
    out = {}
    for name, value in zip(_ATTRS, values):
        # Missing attributes come back as AXValue error objects.
        if value is not None and type(value).__name__ == "AXValueRef":
            try:
                kind = AX.AXValueGetType(value)
            except Exception:
                kind = None
            if kind == AX.kAXValueAXErrorType:
                value = None
        out[name] = value
    return out


def _box(values) -> tuple | None:
    pos, size = values.get("AXPosition"), values.get("AXSize")
    if pos is None or size is None:
        return None
    try:
        p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)[1]
        s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)[1]
        return (float(p.x), float(p.y), float(s.width), float(s.height))
    except Exception:
        return None


def _text(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def _inside(box, area, slack=2) -> bool:
    if area is None:
        return True
    x, y, w, h = box
    ax_, ay, aw, ah = area
    cx, cy = x + w / 2, y + h / 2
    return ax_ - slack <= cx <= ax_ + aw + slack and ay - slack <= cy <= ay + ah + slack


def front_app():
    return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()


WALK_SECONDS = 1.5      # one inventory's time budget: a huge web page must not hold up a voice turn
WALK_DEPTH = 50
# Layout-only wrappers: walked through without counting a level (a deep Electron page is mostly these).
_WRAPPERS = {"AXGroup", "AXScrollArea", "AXSplitGroup", "AXLayoutArea"}
# Controls that can be used by setting their value even when they list no actions.
_VALUE_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXSlider", "AXIncrementor",
                "AXCheckBox", "AXRadioButton", "AXSwitch"}


def _empty(name: str = "", pid=None, degraded: str = "") -> dict:
    return {"app": name, "pid": pid, "title": "", "window": None, "window_id": None, "dialog": None,
            "dialog_title": "", "dialog_soft": False, "elements": [], "walked": 0, "offscreen": [],
            "truncated": "", "degraded": degraded, "snapshot": None, "took": 0.0}


def _actions(element) -> list | None:
    """The element's AX action names; None when it could not be asked (then it is not judged)."""
    try:
        err, names = AX.AXUIElementCopyActionNames(element, None)
    except Exception:
        return None
    if err in (-25205, -25206):            # attribute / action unsupported: no actions
        return []
    return [str(n) for n in names or []] if err == 0 else None


def _settable(element, name: str = "AXValue") -> bool:
    try:
        err, ok = AX.AXUIElementIsAttributeSettable(element, name, None)
        return err == 0 and bool(ok)
    except Exception:
        return False


def _actionable(element, role: str, enabled: bool) -> bool:
    """Can be used through Accessibility (has actions, or a value that can be set) and is enabled - what
    gets an index for choosing (cua-driver tree.rs). Rows and cells are selected by a click either way."""
    if not enabled:
        return False
    if role in ("AXRow", "AXCell"):
        return True
    actions = _actions(element)
    if actions is None or actions:
        return True
    return role in _VALUE_ROLES and _settable(element)


def inventory(app=None, limit: int = 5000, window_id: int | None = None,
              budget: float = WALK_SECONDS) -> dict:
    """Everything visible and usable in the front window (or the window with server id `window_id`).

    -> {app, pid, title, window: box, window_id, dialog: box|None, dialog_title, elements: [...],
        snapshot: "s12", truncated: "" | why the walk stopped early, degraded: "" | why it is empty}
    Each element: {id, token ("s12:7"), role, kind, label, value, box, enabled, focused, in_dialog,
    actionable, ref}. A window that cannot be found gives an empty inventory marked degraded - never
    another window's controls.
    """
    started = time.monotonic()
    app = app or front_app()
    if app is None:
        return _empty()
    name = app.localizedName() or ""
    axkit.unlock(app)
    pid = app.processIdentifier()
    if window_id:
        window = axkit.window_by_id(pid, window_id)
        if window is None:
            return _empty(name, pid, f"window {window_id} of {name} is not in its accessibility tree "
                                     "(closed, on another Space, or not answering)")
    else:
        window = _own_panel(pid) if is_own(app) else axkit.focused_window(pid)
        if window is None:
            return _empty(name, pid)
        window_id = axkit.window_id(window)
    axkit.bound(window)
    wvals = _values(window)
    wbox = _box(wvals)
    title = _text(wvals.get("AXTitle"))

    elements, dialogs, offscreen, hidden = [], [], [], []
    # Depth-first, keeping the chain of ancestors' dialog-ness and a context: the visible part of the
    # window the node is drawn in (narrowed by every scroll area around it), whether it is web
    # content, and the name of the nearest named group or row around it.
    stack = [(window, 0, None, (wbox, False, ""))]
    # A native menu that is open hangs off the app, not the window; while it is
    # open it is the only thing a click can reach, like a dialog.
    for child in axkit.children(axkit.bound(AX.AXUIElementCreateApplication(pid))):
        if axkit.attr(axkit.bound(child), "AXRole") == "AXMenu":
            box = axkit.frame(child)
            if box and box[2] > 10 and box[3] > 10:
                menu = {"box": box, "title": "menu"}
                dialogs.append(menu)
                stack.append((child, 1, menu, (None, False, "")))
    seen, truncated, pending = 0, "", []
    deadline = time.monotonic() + max(budget, 0.05)    # from here: switching Electron on may have waited
    while stack:
        if seen >= limit:
            truncated = f"stopped at {limit} elements"
            break
        if time.monotonic() > deadline:
            truncated = f"stopped after {budget:g} s"
            break
        node, depth, dialog, (clip, web, row) = stack.pop()
        seen += 1
        axkit.bound(node)
        v = _values(node)
        role = v.get("AXRole") or ""
        subrole = v.get("AXSubrole") or ""
        box = _box(v)
        roledesc = _text(v.get("AXRoleDescription")).lower()
        if node is not window and (role == "AXSheet" or subrole in DIALOG_SUBROLES or roledesc in ("dialog", "alert")):
            if box and box[2] > 40 and box[3] > 30 and _inside(box, wbox, slack=40):
                dialog = {"box": box, "title": _text(v.get("AXTitle")) or _text(v.get("AXDescription"))}
                dialogs.append(dialog)
        elif node is not window and role == "AXMenu" and dialog is None:
            # A web menu drawn inside the window (ChatGPT's model picker).
            if box and box[2] > 20 and box[3] > 20:
                dialog = {"box": box, "title": "menu", "soft": True}
                dialogs.append(dialog)
        children = v.get("AXChildren") or []
        named = _text(v.get("AXTitle")) or _text(v.get("AXDescription"))
        # Unnamed layout wrappers are walked through without spending a level of depth.
        below = depth if role in _WRAPPERS and not named else depth + 1
        inner = (_clip_to(clip, box) if role == "AXScrollArea" and node is not window and box else clip,
                 web or role == "AXWebArea",
                 named[:70] if named and role in ("AXGroup", "AXRow", "AXCell") and node is not window else row)
        if not children and node is not window and len(pending) < 4 and (
                role == "AXWebArea" or (role == "AXGroup" and not named and not v.get("AXValue")
                                        and box and box[2] >= 100 and box[3] >= 80)):
            # WebKit builds a web view's tree on first asking (MintFixture's page, 7 Oct: the web view's
            # host group had no children on the first read and the whole page 0.25 s later). An empty
            # web area, or a big unnamed group with nothing in it, is looked at again soon (ground).
            pending.append(node)
        if below <= WALK_DEPTH:
            for child in reversed(list(children)):
                stack.append((child, below, dialog, inner))
        # What can be seen of the window here: a scroll area shows only its own frame of what it holds.
        seen_area = clip if clip is not None else (None if dialog and dialog.get("title") == "menu" else wbox)
        if node is not window and box is not None and (box[2] < 3 or box[3] < 3) and role in TEXT_INPUT \
                and _inside(box, seen_area):
            # Electron/web editors (VS Code's search boxes, Monaco) type into a 1x1 hidden textarea under a
            # drawn placeholder: keep it (named below from the text drawn over it), or nothing can be typed.
            enabled = v.get("AXEnabled") is not False
            hidden.append({"role": role, "subrole": subrole, "kind": ROLE_WORDS.get(role, "text field"),
                           "label": (_text(v.get("AXTitle")) or _text(v.get("AXDescription"))
                                     or _text(v.get("AXPlaceholderValue")))[:90],
                           "value": _text(v.get("AXValue"))[:80], "hint": "", "box": box,
                           "enabled": enabled, "focused": bool(v.get("AXFocused")),
                           "interactive": True, "actionable": enabled, "dialog": dialog, "ref": node,
                           "depth": depth, "hidden": True, "in_web_content": web})
            continue
        if node is window or box is None or box[2] < 3 or box[3] < 3:
            continue
        if not _inside(box, seen_area):
            # Scrolled out of view (or outside the window): not something to click or mark on a
            # picture - listed apart, to be scrolled to (AXScrollToVisible) when it is asked for.
            if role in INTERACTIVE and len(offscreen) < 400:
                name_ = (_text(v.get("AXTitle")) or _text(v.get("AXDescription"))
                         or (_child_text(children, budget=2) if role in ("AXButton", "AXLink", "AXRow", "AXCell") else ""))
                if name_:
                    offscreen.append({"label": name_, "role": role, "ref": node, "box": box, "within": row,
                                      "kind": ROLE_WORDS.get(role, role.removeprefix("AX").lower())})
            continue
        if seen_area is not None:
            box = _clip_to(seen_area, box) or box     # half scrolled away: the part that can be seen
        help_ = _text(v.get("AXHelp"))
        label = _text(v.get("AXTitle")) or _text(v.get("AXDescription")) or _text(v.get("AXPlaceholderValue"))
        value = _text(v.get("AXValue"))
        interactive = role in INTERACTIVE
        if interactive and not label:
            label = _control_name(node, role, value, children, web, help_)
        label = label or help_
        if role == "AXStaticText":
            label = label or value
        elif role in ("AXGroup", "AXImage", "AXHeading") and not label:
            continue
        if not interactive and role not in ("AXStaticText", "AXImage", "AXHeading", "AXGroup"):
            continue
        if not interactive and not label:
            continue
        hint = _text(v.get("AXPlaceholderValue"))
        enabled = v.get("AXEnabled") is not False
        elements.append({
            "hint": hint[:60] if hint and hint != label else "",
            "role": role, "subrole": subrole, "kind": ROLE_WORDS.get(role, role.removeprefix("AX").lower()),
            "label": label[:90], "value": value[:80] if role in TEXT_INPUT else "",
            "help": help_[:80] if help_ and help_ != label else "", "in_web_content": web,
            "box": box, "enabled": enabled, "focused": bool(v.get("AXFocused")),
            "interactive": interactive,
            # Only controls are asked for their actions; text, images and named groups are context.
            "actionable": interactive and _actionable(node, role, enabled),
            "dialog": dialog, "ref": node, "depth": depth,
        })

    _name_hidden(hidden, elements)
    elements.extend(hidden)
    # The top-most open dialog is what the user can reach; everything behind is not.
    active_dialog = dialogs[-1] if dialogs else None
    for e in elements:
        e["in_dialog"] = active_dialog is not None and e["dialog"] is active_dialog
    # Drop exact duplicates (a button and its own text child share a box and label).
    unique, keys = [], set()
    for e in sorted(elements, key=lambda e: (not e["interactive"], e["depth"])):
        key = (e["label"].lower(), tuple(round(c) for c in e["box"]))
        text_of_button = (not e["interactive"] and any(
            u["interactive"] and u["label"].lower() == e["label"].lower() and _overlap(u["box"], e["box"]) > 0.5
            for u in unique))
        if key in keys or text_of_button:
            continue
        keys.add(key)
        unique.append(e)
    unique.sort(key=lambda e: (round(e["box"][1] / 8), e["box"][0]))
    _containers(unique)
    for i, e in enumerate(unique, 1):
        e["id"] = i
    if truncated:
        log.info("inventory of %s: %s (%d read)", name, truncated, seen)
    inv = {"app": name, "pid": pid, "title": title, "window": wbox, "window_id": window_id,
           "dialog": active_dialog["box"] if active_dialog else None,
           "dialog_title": active_dialog["title"] if active_dialog else "",
           "dialog_soft": bool(active_dialog and active_dialog.get("soft")),
           "elements": unique, "walked": seen, "offscreen": offscreen, "truncated": truncated,
           "degraded": "", "web_pending": bool(pending), "pending": pending,
           "took": round(time.monotonic() - started, 3)}
    remember(inv)
    return inv


def _name_hidden(hidden: list, elements: list) -> None:
    """A hidden input gets the name of the text drawn over or right beside it (its placeholder), and that
    text's box, so a click lands where a person would click."""
    for h in hidden:
        hx, hy = h["box"][0], h["box"][1]
        best, gap = None, 1e9
        for e in elements:
            if e["role"] != "AXStaticText" or not e["label"]:
                continue
            x, y, w, ht = e["box"]
            d = abs(hy - (y + ht / 2)) + max(0.0, x - hx, hx - (x + w))
            if d < gap:
                best, gap = e, d
        if best is not None and gap < 40:
            h["label"] = h["label"] or best["label"]
            h["box"] = best["box"]
            h["placeholder"] = best["label"]


def _containers(elements: list) -> None:
    """e["within"]: the label of the smallest named group around an element (a list row's name for its
    Install button), so the model can tell "Install" buttons apart and name one by its row."""
    groups = [g for g in elements if g["role"] in ("AXGroup", "AXRow", "AXCell") and g["label"]]
    for e in elements:
        if e in groups or not e["interactive"]:
            continue
        x, y, w, h = e["box"]
        cx, cy = x + w / 2, y + h / 2
        best = None
        for g in groups:
            gx, gy, gw, gh = g["box"]
            if gx <= cx <= gx + gw and gy <= cy <= gy + gh and gw * gh > w * h * 1.5:
                if best is None or gw * gh < best["box"][2] * best["box"][3]:
                    best = g
        if best is not None and best["label"].lower() != e["label"].lower():
            e["within"] = best["label"][:70]


def _clip_to(area, box):
    """The part of `box` inside `area` (None: no area), or None when they do not meet."""
    if area is None:
        return box
    x0, y0 = max(area[0], box[0]), max(area[1], box[1])
    x1, y1 = min(area[0] + area[2], box[0] + box[2]), min(area[1] + area[3], box[1] + box[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def _humanize(identifier: str) -> str:
    """A DOM id as words ("btn-install-docker-helper" -> "install docker helper")."""
    words = re.findall(r"[A-Za-z][a-z]*|\d+", identifier or "")
    words = [w.lower() for w in words if w.lower() not in ("btn", "button", "web", "input", "txt", "id")]
    return " ".join(words)[:60]


def _control_name(node, role: str, value: str, children, web: bool, help_: str = "") -> str:
    """A name for a control that has no title or description. A web page (WKWebView, Chromium)
    often names a button only by its text children, its value or a tooltip; a labelled field by a
    separate label element (AXTitleUIElement). The page's DOM id is the last resort."""
    if role not in TEXT_INPUT:
        if web and role in ("AXButton", "AXLink") and value:
            return value                    # WebKit gives some buttons their text as the value
        name = _child_text(children)
        if name:
            return name
    title_el = axkit.attr(node, "AXTitleUIElement")
    if title_el is not None:
        name = _text(axkit.attr(title_el, "AXValue")) or _text(axkit.attr(title_el, "AXTitle"))
        if name:
            return name
    if help_:
        return help_
    if web:
        return _humanize(str(axkit.attr(node, "AXDOMIdentifier") or ""))
    return ""


def _child_text(children, budget: int = 3) -> str:
    """A web button's name often lives in its text children."""
    parts, queue = [], [(c, 0) for c in children or []]
    while queue and len(parts) < 4:
        node, level = queue.pop(0)
        value = axkit.attr(node, "AXValue") if axkit.attr(node, "AXRole") == "AXStaticText" else None
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif level < budget:
            queue.extend((c, level + 1) for c in axkit.attr(node, "AXChildren") or [])
    return " ".join(parts)[:90]


def _overlap(a, b) -> float:
    ax_, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax_ + aw, bx + bw) - max(ax_, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    smaller = min(aw * ah, bw * bh) or 1.0
    return ix * iy / smaller


def _where(box, area) -> str:
    if not area:
        return ""
    x, y, w, h = box
    ax_, ay, aw, ah = area
    cx, cy = (x + w / 2 - ax_) / max(aw, 1), (y + h / 2 - ay) / max(ah, 1)
    v = "top" if cy < 0.33 else ("bottom" if cy > 0.66 else "middle")
    hz = "left" if cx < 0.33 else ("right" if cx > 0.66 else "centre")
    return f"{v} {hz}"


def describe(e: dict, area=None, token: bool = False) -> str:
    """One line per element. With `token`, its snapshot token ("[s12:7]") instead of the bare number,
    and nothing in brackets for text that is only context (it is not a control to use)."""
    if token and e.get("token"):
        head = f"[{e['token']}] " if _indexed(e) else "  "
    else:
        head = f"[{e['id']}] "
    parts = [head + e["kind"]]
    if e["label"]:
        parts.append(f"'{e['label']}'")
    if e.get("hint"):
        parts.append(f"(shows '{e['hint']}')")
    if e["value"]:
        parts.append(f"= '{e['value']}'")
    if e.get("within"):
        parts.append(f"in '{e['within'][:50]}'")
    extra = [_where(e["box"], area)]
    if e["in_dialog"]:
        extra.append("in the open dialog")
    if not e["enabled"]:
        extra.append("disabled")
    if e["focused"]:
        extra.append("focused")
    extra = [x for x in extra if x]
    return " ".join(parts) + (f" ({', '.join(extra)})" if extra else "")


def _indexed(e: dict) -> bool:
    """A control that can be used (enabled, with actions or a settable value), or a text box."""
    return bool(e.get("actionable") or (e.get("actionable") is None and e.get("interactive"))
                or (e["role"] in TEXT_INPUT and e["enabled"]))


# --- snapshots: tokens that name one element of one reading, and what changed since ---------------
#
# Every inventory is a snapshot ("s12"); its elements are "s12:1", "s12:2"... A newer reading of the
# same window makes the older tokens stale: they are refused (with the element's new token when it is
# still there) rather than acted on, because the list may have shifted (cua-driver element_token.rs).
# Plain numbers still work and mean the newest reading.

KEEP_SNAPSHOTS = 8
_snap_lock = threading.Lock()
_snap_count = [0]
_snaps: dict[int, dict] = {}            # number -> inventory (the newest KEEP_SNAPSHOTS)
_newest: dict[tuple, int] = {}          # (pid, window id) -> newest snapshot number
_last = [0]                             # the newest snapshot of any window


def _window_key(inv: dict) -> tuple:
    return (inv.get("pid"), inv.get("window_id") or "?")


def remember(inv: dict) -> str:
    """Give an inventory its snapshot id and its elements their tokens; it becomes the newest view of
    its window."""
    with _snap_lock:
        _snap_count[0] += 1
        number = _snap_count[0]
        _snaps[number] = inv
        _newest[_window_key(inv)] = number
        _last[0] = number
        for old in sorted(_snaps)[:-KEEP_SNAPSHOTS]:
            _snaps.pop(old, None)
    inv["snapshot"] = f"s{number}"
    for e in inv["elements"]:
        e["token"] = f"s{number}:{e['id']}"
    return inv["snapshot"]


def _parse_token(token) -> tuple[int | None, int] | None:
    match = re.fullmatch(r"\s*\[?\s*(?:s(\d+):)?(\d+)\s*\]?\s*", str(token))
    if not match:
        return None
    return (int(match.group(1)) if match.group(1) else None), int(match.group(2))


def is_token(text) -> bool:
    return _parse_token(text) is not None


def snapshot(snap) -> dict | None:
    """The remembered inventory for "s12" (or 12), or None."""
    match = re.fullmatch(r"\s*s?(\d+)\s*", str(snap or ""))
    with _snap_lock:
        return _snaps.get(int(match.group(1))) if match else None


def _identity(e: dict) -> tuple:
    return (e["role"], e["label"].lower(), (e.get("within") or "").lower(), bool(e.get("in_dialog")))


def resolve(token, inv: dict | None = None) -> tuple[dict | None, str]:
    """An element from a token ("s12:7", "[s12:7]") or a bare number ("7": the newest reading, or `inv`).
    -> (element, "") or (None, why it was refused)."""
    parsed = _parse_token(token)
    if parsed is None:
        return None, f"'{token}' is not an element id (like s12:7)"
    number, index = parsed
    with _snap_lock:
        if number is None:
            source = inv if inv is not None else _snaps.get(_last[0])
            number = int(str(source["snapshot"])[1:]) if source and source.get("snapshot") else None
        else:
            source = _snaps.get(number)
        newest = _newest.get(_window_key(source)) if source else None
        current = _snaps.get(newest) if newest else None
    if source is None:
        return None, (f"{token} is from a reading that is no longer kept; call ui_elements again"
                      if number else "nothing has been read yet; call ui_elements first")
    element = next((e for e in source["elements"] if e["id"] == index), None)
    if element is None:
        return None, f"there is no element {index} in s{number}"
    if newest != number and current is not None:
        what = f"{element['kind']} '{element['label'][:40]}'"
        same = [e for e in current["elements"] if _identity(e) == _identity(element)]
        hint = (f"; it is now {same[0]['token']}" if len(same) == 1
                else "; it is no longer there" if not same else "")
        return None, (f"{token} ({what}) is stale: {source['app']} was read again since "
                      f"({current['snapshot']}){hint}. Use the new id or call ui_elements again")
    return element, ""


def _rows(inv: dict) -> list[dict]:
    """What a change list compares: things with a name, and text boxes."""
    return [e for e in inv["elements"] if e["label"] or e["role"] in TEXT_INPUT]


def diff(before: dict, after: dict) -> dict:
    """What changed between two readings of a window.
    -> {"added": [e], "changed": [(old, new, "what")], "removed": [e], "same_window": bool}.
    Elements are matched by role, label and row (repeats in reading order), so a list that only
    scrolled reports nothing; "changed" is the value, enabled, focused or selected-dialog state."""
    old_by, new_by = {}, {}
    for e in _rows(before):
        old_by.setdefault(_identity(e), []).append(e)
    for e in _rows(after):
        new_by.setdefault(_identity(e), []).append(e)
    added, changed, removed = [], [], []
    for key in set(old_by) | set(new_by):
        olds, news = old_by.get(key, []), new_by.get(key, [])
        for old, new in zip(olds, news):
            what = []
            if old["value"] != new["value"]:
                what.append(f"was '{old['value'][:40]}'")
            if old["enabled"] != new["enabled"]:
                what.append("now enabled" if new["enabled"] else "now disabled")
            if old["focused"] != new["focused"]:
                what.append("now focused" if new["focused"] else "lost focus")
            if what:
                changed.append((old, new, ", ".join(what)))
        added.extend(news[len(olds):])
        removed.extend(olds[len(news):])
    order = lambda e: (round(e["box"][1] / 8), e["box"][0])       # noqa: E731
    return {"added": sorted(added, key=order), "changed": sorted(changed, key=lambda t: order(t[1])),
            "removed": sorted(removed, key=order), "same_window": _window_key(before) == _window_key(after)}


def diff_text(before: dict, after: dict, limit: int = 40) -> str:
    """`diff` as the model reads it: "+" new rows (new tokens), "~" changed, "-" gone (old tokens)."""
    d = diff(before, after)
    old_id, new_id = before.get("snapshot") or "before", after.get("snapshot") or "now"
    if not (d["added"] or d["changed"] or d["removed"]):
        return f"no change since {old_id} (now {new_id})"
    area = after.get("window")
    lines = [f"since {old_id} (now {new_id}): {len(d['added'])} added, {len(d['changed'])} changed, "
             f"{len(d['removed'])} removed"]
    rows = ([("+", e, "") for e in d["added"]] + [("~", new, what) for _, new, what in d["changed"]]
            + [("-", e, "") for e in d["removed"]])
    for mark, e, what in rows[:limit]:
        body = describe(e, area, token=True).strip()
        if not body.startswith("["):                  # a changed row is named by its id even if disabled
            body = f"[{e.get('token') or e['id']}] {body}"
        lines.append(f"{mark} {body}" + (f" [{what}]" if what else ""))
    if len(rows) > limit:
        lines.append(f"... and {len(rows) - limit} more")
    return "\n".join(lines)


def since(snap, inv: dict | None = None) -> str | None:
    """What changed in the window since snapshot `snap` (now: `inv`, or a fresh reading of the front
    window). None when `snap` is unknown or was of another window - read everything instead."""
    before = snapshot(snap)
    if before is None:
        return None
    after = inv if inv is not None else inventory()
    if _window_key(before) != _window_key(after):
        return None
    return diff_text(before, after)


changes_since = since      # for callers whose own argument is called `since`


# --- 2. choosing ---------------------------------------------------------------------------

_KIND_HINTS = {
    "button": {"AXButton", "AXMenuButton", "AXPopUpButton"}, "link": {"AXLink"},
    "field": TEXT_INPUT, "box": TEXT_INPUT, "input": TEXT_INPUT, "textbox": TEXT_INPUT,
    "tab": {"AXTab", "AXRadioButton"}, "checkbox": {"AXCheckBox", "AXSwitch"},
    "toggle": {"AXCheckBox", "AXSwitch"}, "menu": {"AXMenuItem", "AXMenuButton", "AXPopUpButton", "AXMenuBarItem"},
    "option": {"AXRadioButton", "AXMenuItem", "AXCell", "AXRow"},
}
_FILLER = {"sidebar", "left", "right", "top", "bottom", "toolbar", "header", "footer", "side", "bar",
           "the", "a", "an", "on", "in", "into", "at", "of", "to", "button", "link", "field", "box", "input",
           "textbox", "tab", "click", "press", "tap", "select", "choose", "open", "dialog", "popup", "pop-up",
           "menu", "option", "item", "icon", "that", "this", "please", "named", "called", "labelled", "labeled"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _core(target: str) -> str:
    quoted = re.findall(r"[\"“'‘]([^\"”'’]+)[\"”'’]", target)
    if quoted:
        return " ".join(_words(quoted[0]))
    from mint.screen import choose
    target = choose._DESCRIPTOR_PHRASES.sub(" ", target)      # "the Size pop-up": 'pop', 'up' are no label
    return " ".join(w for w in _words(target) if w not in _FILLER)


_FIELD_WORDS = {"box", "field", "input", "prompt", "composer", "textbox", "textarea", "message", "chat",
                "type", "typing", "search", "bar", "editor"}


def candidates(inv: dict, action: str) -> list[dict]:
    """What the action can apply to: interactive things, the open dialog first."""
    items = [e for e in inv["elements"] if e["enabled"]]
    if action == "type":
        pool = [e for e in items if e["role"] in TEXT_INPUT]
    else:
        pool = [e for e in items if e["interactive"] or e["role"] in ("AXStaticText", "AXImage", "AXGroup")]
        # A control that Accessibility says can do nothing (no actions, no settable value) is decoration.
        pool = [e for e in pool if not (e["interactive"] and e.get("actionable") is False)]
    if inv["dialog"] is not None:
        # With a dialog open, only the dialog can be clicked.
        in_dialog = [e for e in pool if e["in_dialog"]]
        pool = in_dialog or pool
    return pool


_PLACES = {"sidebar": "left", "left": "left", "right": "right", "top": "top", "toolbar": "top",
           "header": "top", "bottom": "bottom", "footer": "bottom"}


def _place_ok(e, target_words, area) -> float:
    """+ if the element is where the user said ("in the sidebar"), - if not."""
    if not area:
        return 0.0
    x, y, w, h = e["box"]
    ax_, ay, aw, ah = area
    cx, cy = (x + w / 2 - ax_) / max(aw, 1), (y + h / 2 - ay) / max(ah, 1)
    score = 0.0
    for word in target_words:
        side = _PLACES.get(word)
        if side is None:
            continue
        inside = {"left": cx < 0.3, "right": cx > 0.7, "top": cy < 0.2, "bottom": cy > 0.8}[side]
        score += 0.3 if inside else -0.3
    return score


def buttonish_target(target: str) -> bool:
    return bool(set(_words(target)) & {"button", "link", "icon", "item", "tab", "menu", "option", "checkbox",
                                       "toggle", "project", "chat"})


def choose_by_text(target: str, pool: list[dict], area=None) -> tuple[dict | None, str]:
    """Label match, with roles and the named place deciding between equal labels."""
    from mint.screen import choose
    core = _core(target)
    # The words as said, less the article: a label made of filler words ("Item actions") is still exact.
    said = " ".join(w for w in _words(target) if w not in ("the", "a", "an"))
    place_words = [w for w in _words(target) if w in _PLACES]
    wanted_roles = set()
    for word in _words(target):
        wanted_roles |= _KIND_HINTS.get(word, set())
    if re.search(r"\b(pop[\s-]?up|drop[\s-]?down)\b", target, re.I):
        wanted_roles |= {"AXPopUpButton", "AXMenuButton", "AXComboBox"}
    if not core:
        return None, "no label words"

    def score(e):
        label = " ".join(_words(e["label"]))
        if not label:
            return 0.0
        if label == core or label == said:
            s = 1.0
        elif core in label.split(" ") or label.startswith(core + " ") or f" {core} " in f" {label} ":
            s = 0.75 if len(core) > 3 else 0.4
        elif label in core and len(label) > 3:
            s = 0.55
            rest = set(core.replace(label, " ").split()) - _FILLER - choose._PLAIN     # "... for YAML Support"
            within = set(_words(e.get("within", "")))
            if rest and within and rest <= within:
                s = 1.0                  # "Install CSV Colorful Table": the Install button in that row
        else:
            return 0.0
        if e["interactive"]:
            s += 0.2                     # a button beats a heading with the same words
        if wanted_roles and e["role"] in wanted_roles:
            s += 0.2
        if e["in_dialog"]:
            s += 0.1
        if place_words:
            s += _place_ok(e, place_words, area)
        # A pop-up whose label merely contains the words ("Change project:
        # data-labeling-portal") is weaker than an item that IS that name.
        if e["role"] in ("AXPopUpButton", "AXMenuButton") and label != core:
            s -= 0.15
        return s

    scored = sorted(((score(e), e) for e in pool), key=lambda t: -t[0])
    scored = [t for t in scored if t[0] > 0]
    if not scored:
        return None, "no label matches"
    best, runner = scored[0], (scored[1] if len(scored) > 1 else (0.0, None))
    if best[0] >= 1.0 and best[0] - runner[0] >= 0.15:
        return best[1], f"label match ({best[0]:.2f})"
    return None, "ambiguous: " + "; ".join(f"{e['label'][:30]} ({s:.2f})" for s, e in scored[:4])


JEV_LOOK_AGAIN = "Jev: look again"     # ground() takes one fresh inventory when Jev says this


def _jev_prerank(goal: str, options: dict[str, str]) -> dict[str, float]:
    """More controls than Jev's table holds: one ranking call decides which make the cut."""
    from mint.core import jev
    ranked, _ = jev.rank(goal, options, "Which listed controls could `request` be asking for? A control "
                         "whose label means the same thing counts ('attach a file' - 'Add files').", timeout=4.0)
    return dict(ranked or [])


def choose_by_jev(target: str, pool: list[dict], inv: dict, plan: list[str] | None = None) -> tuple[dict | None, str]:
    """Jev over a closed table of actions (choose.py): at most 24, ids from role + label + where,
    deleting/sending/buying/closing left out, plus "reobserve" and "abstain", with a summary of
    the screen. An unsure, unknown or split answer is not used: the vision model gets a turn.
    `plan`: the steps of the task under way, whose controls are kept first when there are too many."""
    from mint.screen import choose
    labelled = [e for e in pool if e["label"] or e["role"] in TEXT_INPUT]
    if not labelled:
        return None, "nothing labelled"
    table = choose.build(labelled, target, inv.get("window"), plan=plan, prerank=_jev_prerank)
    if not table.candidates:
        return None, "Jev: nothing here it may choose (delete, send, buy and close go through ui_act's checks)"
    choice = choose.ask_jev(target, table, choose.state_summary(inv))
    _debug(f"jev {choice.kind} in {choice.seconds:.2f}s over {len(table.candidates)} "
           f"(+{table.dropped} dropped, {table.excluded} excluded): {choice.why}")
    closest = choice.closest(table)
    if choice.kind == "reobserve":
        return None, f"{JEV_LOOK_AGAIN}; the screen may still be changing" + (f". Closest: {closest}" if closest else "")
    if choice.kind == "abstain":
        return None, ("Jev: not sure it is on screen" + (f". Closest: {closest}" if closest else "")
                      + ". Name it as it is labelled, or look")
    if choice.kind != "act" or choice.candidate is None:
        return None, f"Jev: {choice.why}; not using it" + (f". Closest: {closest}" if closest else "")
    pick = choice.candidate.element
    why = choice.why
    place_words = [w for w in _words(target) if w in _PLACES]
    if place_words and _place_ok(pick, place_words, inv["window"]) < 0:
        return None, (f"Jev's pick '{pick['label'][:40]}' is not where the user said "
                      f"({' '.join(place_words)}); not using it")
    # A target naming something the pick does not mention is a substitution, not a match:
    # "Start new chat in data-labeling-portal" (a sidebar button shown only on hover) came back
    # as plain "New chat" - with 0.85 confidence, so confidence alone cannot catch it.
    missing = choose.missing_names(target, pick)
    if missing:
        return None, (f"Jev's best guess '{pick['label'][:40]}' does not mention '{missing}' from '{target}'; "
                      "the control may not be visible (some appear only on hover)")
    return pick, f"Jev: {why}"


def screenshot(area, window_id: int | None = None) -> tuple:
    """(PIL image of the area, scale from points to pixels). With the target's `window_id`, the picture
    is of that app's own window(s), so a window lying on top - Mint's notch included - is not in it
    (capture.area; falls back to the screen grab)."""
    from mint.screen import capture
    shot = capture.area(area, window_id)
    return shot["image"], shot["scale"]


def marked(inv: dict, pool: list[dict], area=None):
    """A screenshot with every candidate drawn as a numbered box (set-of-marks)."""
    import PIL.ImageDraw
    import PIL.ImageFont

    area = area or inv["dialog"] or inv["window"]
    image, scale = screenshot(area, inv.get("window_id"))
    draw = PIL.ImageDraw.Draw(image)
    try:
        font = PIL.ImageFont.truetype("/System/Library/Fonts/SFNSMono.ttf", int(11 * scale))
    except Exception:
        font = PIL.ImageFont.load_default()
    palette = [(255, 59, 48), (0, 122, 255), (52, 199, 89), (255, 149, 0), (175, 82, 222), (255, 45, 85)]
    ax_, ay = area[0], area[1]
    for e in pool:
        x, y, w, h = e["box"]
        box = [(x - ax_) * scale, (y - ay) * scale, (x - ax_ + w) * scale, (y - ay + h) * scale]
        color = palette[e["id"] % len(palette)]
        draw.rectangle(box, outline=color, width=max(1, int(scale)))
        tag = str(e["id"])
        tw = draw.textlength(tag, font=font)
        draw.rectangle([box[0], box[1], box[0] + tw + 4 * scale, box[1] + 13 * scale], fill=color)
        draw.text((box[0] + 2 * scale, box[1]), tag, fill=(255, 255, 255), font=font)
    return image, area


def _png(image, max_side: int = 1600) -> bytes:
    from mint.screen import choose
    image, _ = choose.prepare(image, max_side)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _vision_id(answer) -> int | None:
    pick = answer.get("id") if isinstance(answer, dict) else None
    try:
        return int(pick) if pick is not None else None
    except (TypeError, ValueError):
        return None


def _vision_confidence(answer) -> float | None:
    try:
        value = float(answer.get("confidence")) if isinstance(answer, dict) else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and 0 <= value <= 1 else None


def _zoom_check(target: str, chosen: dict, pool: list[dict], image, area, model: str) -> tuple[dict | None, str]:
    """A second look at a small or doubtful pick: the same capture cropped around it (20% padding,
    at most 500 px wide), the boxes inside it listed again, and the model's answer - a box, or a
    point mapped back to screen points - checked against them. -> (element or None, note)."""
    from google.genai import types
    from mint.screen import choose
    scale = image.size[0] / max(area[2], 1)
    zoom = choose.plan_zoom(choose.zoom_region(chosen["box"], area), area, scale, image.size)
    local = [e for e in pool if zoom.contains(_center(e))]
    crop = choose.zoom_image(image, zoom)
    listing = "\n".join(describe(e, area) for e in local[:40])
    prompt = (
        f"This is a zoomed-in part of the same app screenshot, {zoom.size[0]}x{zoom.size[1]} pixels, with the "
        "same numbered boxes. Which numbered box is the target? Also point at the target's centre with "
        "[y, x] normalised to 0-999 across this image (0 = top/left edge, 999 = bottom/right edge). If the "
        "target is not in this image, answer null for both.\n"
        f"Target: {target}\n\nBoxes here:\n{listing}\n\n"
        'Answer only JSON: {"id": <number or null>, "point": [y, x] or null, "why": "<few words>"}')
    try:
        text, _ = _generate([types.Part.from_bytes(data=_png(crop), mime_type="image/png"), prompt],
                            models=[model] + [m for m in VISION_MODELS if m != model])
        answer = _json(text)
    except Exception as error:
        return chosen, f"zoom unavailable ({str(error)[:60]}), first pick kept"
    zid = _vision_id(answer)
    hit = next((e for e in local if e["id"] == zid), None)
    if hit is not None:
        return hit, ("zoom confirmed it" if hit is chosen else f"zoom moved it to [{hit['id']}]")
    point = answer.get("point") if isinstance(answer, dict) else None
    if isinstance(point, list) and len(point) == 2 and all(isinstance(v, (int, float)) for v in point):
        x, y = zoom.to_screen(point[1], point[0])
        inside = [e for e in local if _inside((x, y, 0, 0), e["box"], slack=1)]
        if inside:
            best = min(inside, key=lambda e: e["box"][2] * e["box"][3])
            return best, f"zoom pointed at [{best['id']}]"
    return None, "zoom did not find it near the first pick"


def choose_by_vision(target: str, pool: list[dict], inv: dict, model: str | None = None,
                     zoom: bool = True) -> tuple[dict | None, str]:
    """A vision model picks a numbered box; it never invents coordinates. A pick smaller than
    choose.SMALL points, or one the model is unsure of, gets a zoomed second look."""
    from google.genai import types
    from mint.screen import choose

    image, area = marked(inv, pool)
    sent, _ = choose.prepare(image, 1600)
    listing = "\n".join(describe(e, inv["window"]) for e in pool[:150])
    hidden = offscreen_text(inv, target)
    if hidden:
        # Scrolled out of view: no box on the picture. A visible lookalike in another row is not it.
        listing += f"\n\n{hidden} - these are NOT in the picture; if the target is one of them, answer null."
    prompt = (
        "You are operating a Mac app. The screenshot "
        f"({sent.size[0]}x{sent.size[1]} pixels) shows the app with numbered boxes drawn "
        "around every control. Pick the ONE box that is the target below. It must be the control "
        "a person would click or type into to do it: a button, not a heading with the same words; "
        "the field itself, not its label. If a dialog is open, only the dialog can be used.\n"
        f"Target: {target}\n\nControls:\n{listing}\n\n"
        'Answer only JSON: {"id": <number or null>, "confidence": <0 to 1>, "why": "<few words>"}')
    started = time.monotonic()
    try:
        text, used = _generate([types.Part.from_bytes(data=_png(sent), mime_type="image/png"), prompt],
                               models=[model] if model else None)
    except Exception as error:
        return None, f"vision: {str(error)[:120]}"
    try:
        answer = _json(text)
    except Exception:
        return None, f"vision: unreadable answer {text[:80]}"
    pick = _vision_id(answer)
    chosen = next((e for e in pool if e["id"] == pick), None)
    note = answer.get("why", "") if isinstance(answer, dict) else ""
    if chosen is not None and zoom and choose.needs_zoom(chosen["box"], _vision_confidence(answer)):
        chosen, zoomed = _zoom_check(target, chosen, pool, image, area, used)
        note = f"{note}; {zoomed}"
    return chosen, f"vision {used} ({time.monotonic() - started:.1f}s): {note}"


def point_by_vision(target: str, area, model: str = "gemini-robotics-er-2-preview",
                    zoom: bool = True, window_id: int | None = None) -> tuple[tuple | None, str]:
    """For things with no accessible element at all (icons on a canvas): a pointing model returns
    a point on a screenshot of the area - normalised to 0-1000 across the image it was sent, which
    is downscaled here so its size is known - then points again on a zoomed crop around it."""
    from google.genai import types
    from mint.screen import choose

    image, scale = screenshot(area, window_id)
    started = time.monotonic()

    def ask(picture) -> tuple[tuple | None, str]:
        prompt = (f"The image is {picture.size[0]}x{picture.size[1]} pixels. Point to the {target}. The answer "
                  'follows the format: [{"point": [y, x], "label": "<label>"}] with coordinates normalized '
                  "to 0-1000.")
        text, used = _generate([types.Part.from_bytes(data=_png(picture), mime_type="image/png"), prompt],
                               models=[model], json_mode=False)
        match = re.search(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", text)
        return ((float(match.group(2)), float(match.group(1))) if match else None), (text if not match else used)

    sent, ratio = choose.prepare(image, 1600)
    try:
        found, used = ask(sent)
    except Exception as error:
        return None, f"point: {str(error)[:120]}"
    if found is None:
        return None, f"point: no point in {used[:80]}"
    # Pixels of the image sent -> pixels of the capture -> screen points.
    px = (choose.denorm(found[0], sent.size[0]) + 0.5) / ratio
    py = (choose.denorm(found[1], sent.size[1]) + 0.5) / ratio
    point = (area[0] + px / scale, area[1] + py / scale)
    note = ""
    if zoom:
        plan = choose.plan_zoom(choose.zoom_region((point[0] - 1, point[1] - 1, 2, 2), area, minimum=(160.0, 100.0)),
                                area, scale, image.size)
        try:
            again, _ = ask(choose.zoom_image(image, plan))
        except Exception:
            again = None
        if again is not None:
            point, note = plan.to_screen(*again), ", zoomed"
    return point, f"point {used} ({time.monotonic() - started:.1f}s{note})"


def _scroll_region_search(target: str, action: str, inv: dict):
    """The user said where ("in the sidebar") and it is not visible there:
    scroll that region and look again, like a person. Lists such as
    ChatGPT's sidebar only build the rows on screen, so there is nothing
    off-screen to ask Accessibility about."""
    place_words = [w for w in _words(target) if w in _PLACES]
    sides = {_PLACES[w] for w in place_words} & {"left", "right"}
    if not sides or inv["dialog"] or not inv["window"]:
        return None
    wx, wy, ww, wh = inv["window"]
    x = wx + ww * (0.12 if "left" in sides else 0.88)
    point = Quartz.CGPointMake(x, wy + wh * 0.6)
    from mint.app import control
    for direction in (-1, 1):                      # down first, then back up
        for _ in range(8):
            if control.stopped():
                return None
            event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, 6 * direction)
            Quartz.CGEventSetLocation(event, point)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.35)
            fresh = inventory()
            pool = [e for e in candidates(fresh, action) if _place_ok(e, place_words, fresh["window"]) > 0]
            hit, why = choose_by_text(target, pool, fresh["window"])
            if hit is not None:
                return hit, fresh, why + " (scrolled the " + " ".join(place_words) + " to find it)"
        if direction == -1:
            continue
    return None


def _scroll_to_named(target: str, action: str, inv: dict):
    """The control the user named may exist but be scrolled out of view (a
    sidebar project below the fold) while a namesake is visible elsewhere (the
    composer's 'Change project: …' chip, in testing). A control counts as named
    when every word of its name is in the request; if the request says where
    ("in the sidebar"), it must be there. When the request names more than the
    control ("Install SQL Formatter"), the rest must be its row's name: another
    row's Install is not it."""
    if inv["dialog"] or not inv.get("offscreen"):
        return None
    wanted = set(_words(target))
    place_words = [w for w in wanted if w in _PLACES]
    scored = []
    for o in inv["offscreen"]:
        name = _words(o["label"])
        if len(name) == 0 or len(" ".join(name)) < 3 or not set(name) <= wanted:
            continue
        if place_words and o.get("box") and _place_ok({"box": o["box"]}, place_words, inv["window"]) < 0:
            continue
        rest = wanted - set(name) - _FILLER - set(place_words)
        within = set(_words(o.get("within", "")))
        if rest and within and not rest & within:
            continue                           # the same label in another row
        scored.append(((len(name), len(rest & within)), o))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    best = scored[0][1]
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None                            # two of them named alike: scrolling to one is a guess
    how = scroll_into_view(best["ref"])
    if not how:
        return None
    time.sleep(0.25)
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(inv["pid"]) if inv.get("pid") else None
    fresh = inventory(app, window_id=inv.get("window_id")) if app is not None else inventory()
    pool = candidates(fresh, action)
    exact = [e for e in pool if " ".join(_words(e["label"])) == " ".join(_words(best["label"]))]
    same = [e for e in exact if e.get("ref") is not None and e["ref"] == best["ref"]]
    if same:
        exact = same
    elif best.get("within"):
        exact = [e for e in exact if _words(e.get("within", "")) == _words(best["within"])]
    elif len(exact) > 1:
        return None                            # several alike now in view, and nothing says which it was
    if place_words:
        exact.sort(key=lambda e: -_place_ok(e, place_words, fresh["window"]))
    if exact:
        row = f" in '{best['within'][:40]}'" if best.get("within") else ""
        chosen = dict(exact[0], scrolled=f"scrolled {best['kind'] if best.get('kind') else 'it'} "
                                         f"'{best['label'][:40]}'{row} into view first ({how})")
        return chosen, fresh, f"'{best['label'][:40]}'{row} was out of view; scrolled it into view"
    return None


def _scroll_parents(ref, levels: int = 8) -> tuple[list, object]:
    """The element's ancestors below its scroll area, and that scroll area (None: not in one)."""
    chain, node = [], ref
    for _ in range(levels):
        node = axkit.attr(node, "AXParent")
        role = axkit.attr(node, "AXRole") if node is not None else None
        if role is None or role in ("AXWindow", "AXApplication"):
            return chain, None
        if role == "AXScrollArea":
            return chain, node
        chain.append(node)
    return chain, None


def _in_view(ref, area) -> bool | None:
    """Is the element's centre inside its scroll area's frame? None when either cannot be read."""
    box = axkit.frame(ref)
    seen = axkit.frame(area) if area is not None else None
    if not box or not seen:
        return None
    return _inside(box, seen)


def _bar_value(ref, area) -> float | None:
    """The vertical scroll bar value (0 = top, 1 = bottom) that puts the element in the middle of its
    scroll area: the content's extent is the union of the area's children (an AppKit scroll view lists
    its rows, a table view its table - both moved by scrolling)."""
    box, seen = axkit.frame(ref), axkit.frame(area)
    spans = [axkit.frame(k) for k in axkit.attr(area, "AXChildren") or []
             if axkit.attr(k, "AXRole") not in ("AXScrollBar", None)]
    spans = [b for b in spans if b]
    if not box or not seen or not spans:
        return None
    top = min(b[1] for b in spans)
    height = max(b[1] + b[3] for b in spans) - top
    if height <= seen[3]:
        return None
    return max(0.0, min(1.0, (box[1] - top + box[3] / 2 - seen[3] / 2) / (height - seen[3])))


def scroll_into_view(ref) -> str:
    """Bring a control that is scrolled out of view into view, through Accessibility only: no scroll
    wheel, no pointer, the app not activated. In order: AXScrollToVisible on the control or a parent
    that offers it (WebKit pages, tables); the scroll area's vertical scroll bar value; its scroll
    actions, a page at a time (an AppKit list's Install button offers only AXPress, and its rows
    nothing - MintFixture, 7 Oct). -> how it was done, '' when it could not be."""
    chain, area = _scroll_parents(ref)
    for node in [ref, *chain]:
        try:
            err = AX.AXUIElementPerformAction(node, "AXScrollToVisible")
        except Exception:
            err = -1
        if err == 0:
            time.sleep(0.15)
            if _in_view(ref, area) is not False:
                return "Accessibility's scroll-to-visible"
    if area is None:
        return ""
    bar = axkit.attr(area, "AXVerticalScrollBar")
    value = _bar_value(ref, area) if bar is not None and _settable(bar) else None
    if value is not None:
        try:
            AX.AXUIElementSetAttributeValue(bar, "AXValue", value)
        except Exception:
            pass
        time.sleep(0.15)
        if _in_view(ref, area):
            return "its scroll bar, set through Accessibility"
    for _ in range(40):                        # a page at a time, while it moves
        box, seen = axkit.frame(ref), axkit.frame(area)
        if not box or not seen:
            return ""
        if _inside(box, seen):
            return "Accessibility's page scrolling"
        cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
        step = ("AXScrollDownByPage" if cy > seen[1] + seen[3] else "AXScrollUpByPage" if cy < seen[1]
                else "AXScrollRightByPage" if cx > seen[0] + seen[2] else "AXScrollLeftByPage")
        try:
            if AX.AXUIElementPerformAction(area, step) != 0:
                return ""
        except Exception:
            return ""
        time.sleep(0.05)
        if axkit.frame(ref) == box:
            return ""                          # it did not move: the end of the list, or not scrollable
    return ""


def offscreen_text(inv: dict, target: str = "", limit: int = 12) -> str:
    """The controls scrolled out of view, as one line - with `target`, only those sharing a word
    with it. They are not on the picture and cannot be clicked until scrolled to."""
    wanted = set(_words(target)) - _FILLER
    items = []
    for o in inv.get("offscreen") or []:
        words = set(_words(o["label"] + " " + (o.get("within") or "")))
        if target and not words & wanted:
            continue
        items.append(f"{o.get('kind') or ROLE_WORDS.get(o['role'], 'control')} '{o['label'][:40]}'"
                     + (f" in '{o['within'][:40]}'" if o.get("within") else ""))
        if len(items) >= limit:
            break
    if not items:
        return ""
    more = len(inv.get("offscreen") or []) - len(items)
    return "Off screen (scroll to it): " + "; ".join(items) + (f"; and {more} more" if more > 0 and not target else "")


PENDING_WAIT = 1.2          # seconds ground waits for a web view's page to appear in the tree
_pending_waited: dict[tuple, float] = {}       # (pid, window id) -> when such a wait found nothing


def settle_pending(inv: dict) -> dict | None:
    """The inventory found a web view whose page is not in the tree yet (`pending`, see inventory):
    poll those elements until the page is there - up to PENDING_WAIT - and read the same window again.
    -> the fresh inventory, or None when nothing came (then that window is not waited for again for two
    minutes: a group that is simply empty costs one wait, not one per action)."""
    hosts = inv.get("pending") or []
    if not hosts or not inv.get("pid"):
        return None
    key = _window_key(inv)
    if time.monotonic() - _pending_waited.get(key, -1e9) < 120.0:
        return None
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(inv["pid"])
    if app is None:
        return None
    fresh, deadline = None, time.monotonic() + PENDING_WAIT
    while hosts and time.monotonic() < deadline:
        time.sleep(0.15)
        if any(axkit.attr(h, "AXChildren") for h in hosts):
            time.sleep(0.1)                     # the rest of the page follows within a moment
            fresh = inventory(app, window_id=inv.get("window_id"))
            hosts = fresh.get("pending") or []
    if fresh is None:
        _pending_waited[key] = time.monotonic()
    else:
        _debug(f"web content came: {len(inv['elements'])} -> {len(fresh['elements'])} elements")
    return fresh


def ground(target: str, action: str = "click", inv: dict | None = None,
           use_vision: bool = True) -> tuple[dict | None, dict, str]:
    """-> (element or None, inventory, how it was chosen)."""
    inv = inv or inventory()
    inv = settle_pending(inv) or inv
    pool = candidates(inv, action)
    if len(pool) < 3 and inv.get("pid"):
        # Electron rebuilds its tree in bursts: VS Code listed 4 controls at 21:10:53 on 1 Oct and
        # the search box 4 s later. Give a thin tree one more look before deciding.
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(inv["pid"])
        if app is not None and axkit.is_chromium_app(app):
            time.sleep(0.6)
            again = inventory(app)
            if len(candidates(again, action)) > len(pool):
                inv, pool = again, candidates(again, action)
    _debug(f"{inv['app']} '{inv['title'][:40]}': {len(inv['elements'])} elements, {len(pool)} candidates"
           + (f", dialog '{inv['dialog_title']}'" if inv["dialog"] else ""))
    if not pool:
        return None, inv, "nothing usable on screen"
    fields = [e for e in pool if e["role"] in TEXT_INPUT]
    words = set(_words(target))
    area = inv["window"]
    if inv["dialog"] and not inv.get("dialog_soft"):
        # With a dialog open, "the chat message box" must not become the
        # dialog's search field just because it is the only field there (seen
        # in testing: text meant for ChatGPT's composer went into a project
        # picker's search). If the target is outside the dialog, say so.
        outside = [e for e in inv["elements"] if e["enabled"] and not e["in_dialog"] and
                   (e["role"] in TEXT_INPUT if action == "type" else e["interactive"])]
        inside_hit, _ = choose_by_text(target, pool, area)
        outside_hit, _ = choose_by_text(target, outside, area)
        field_named = any(set(_words(f["label"] + " " + f.get("hint", ""))) &
                          (words - {"the", "a", "an", "box", "field", "input"}) for f in fields)
        outside_field = any(e["role"] in TEXT_INPUT for e in outside)
        if outside_hit is not None and inside_hit is None:
            return None, inv, (f"a dialog '{inv['dialog_title'] or 'dialog'}' is open and covers the "
                               f"{outside_hit['kind']} '{outside_hit['label'][:40]}'; close the dialog first "
                               "(ui_act action dismiss) or pick something in it")
        # Modal web dialogs hide the page behind them from Accessibility (ChatGPT's
        # command menu left 44 nodes), so "is there a field outside?" cannot be
        # asked. Refuse whenever the target names something the field is not.
        specific = words - {"the", "a", "an", "box", "field", "input", "text", "textbox", "this", "that", "here",
                            "in", "into", "it", "type"}
        if (fields and not field_named and (outside_field or specific)
                and (action == "type" or (words & _FIELD_WORDS and not buttonish_target(target)))):
            return None, inv, (f"a dialog '{inv['dialog_title'] or 'dialog'}' is open; its only field is "
                               f"'{fields[0]['label'][:40]}', which does not look like '{target}'. Close the "
                               "dialog (ui_act action dismiss) or name that field")
    buttonish = action != "type" and bool(words & {"button", "link", "icon", "item", "tab", "menu", "option",
                                                        "checkbox", "toggle", "project", "chat"})
    if (len(fields) == 1 and not buttonish and (action == "type" or words & _FIELD_WORDS)
            and not choose_by_text(target, pool, area)[0]):
        # "the message box", "the prompt", "where I type": with one text field
        # on screen there is nothing to decide - and vision took 8 s to say so.
        return fields[0], inv, "the only text field"
    if action == "type" and len(pool) == 1:
        # One place to type: "the message box", "the prompt field" can only mean it.
        return pool[0], inv, "the only text field"
    chosen, why = choose_by_text(target, pool, area)
    _debug(f"text: {why}")
    if chosen is not None:
        return chosen, inv, why
    scrolled = _scroll_to_named(target, action, inv) or _scroll_region_search(target, action, inv)
    if scrolled is not None:
        return scrolled
    if inv.get("dialog_soft"):
        # An open menu, not a dialog: the target may be outside it (clicking
        # elsewhere just closes the menu), so look at the whole window too.
        wide = [e for e in inv["elements"] if e["enabled"] and (
            e["role"] in TEXT_INPUT if action == "type" else e["interactive"])]
        chosen, why = choose_by_text(target, wide)
        if chosen is not None:
            return chosen, inv, why + " (outside the open menu)"
    chosen, why2 = choose_by_jev(target, pool, inv)
    _debug(f"jev: {why2}")
    if why2.startswith(JEV_LOOK_AGAIN) and inv.get("pid"):
        # Jev thinks the screen is still changing (a menu opening, a list loading): one fresh look.
        time.sleep(0.4)
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(inv["pid"])
        fresh = inventory(app) if app is not None else None
        if fresh and candidates(fresh, action):
            inv, pool = fresh, candidates(fresh, action)
            chosen, why = choose_by_text(target, pool, inv["window"])
            if chosen is not None:
                return chosen, inv, why + " (after a second look)"
            chosen, why2 = choose_by_jev(target, pool, inv)
    if chosen is not None:
        return chosen, inv, why2
    if use_vision:
        chosen, why3 = choose_by_vision(target, pool, inv)
        _debug(why3)
        place_words = [w for w in _words(target) if w in _PLACES]
        if chosen is not None and place_words and _place_ok(chosen, place_words, inv["window"]) < 0:
            why3 = f"vision picked '{chosen['label'][:40]}', which is not {' '.join(place_words)}"
            chosen = None
        if chosen is not None:
            return chosen, inv, why3
        hidden = offscreen_text(inv, target)
        return None, inv, f"{why}; {why2}; {why3}" + (f". {hidden}" if hidden else "")
    hidden = offscreen_text(inv, target)
    return None, inv, f"{why}; {why2}" + (f". {hidden}" if hidden else "")


# --- 3. acting --------------------------------------------------------------------------------

def _post(kind, point, button=Quartz.kCGMouseButtonLeft, clicks=1):
    from mint.app import control
    if control.stopped():
        return
    event = Quartz.CGEventCreateMouseEvent(None, kind, point, button)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def mouse_click(x: float, y: float, button: str = "left", double: bool = False, label: str = "",
                spark: bool = True, pid: int | None = None) -> str:
    """Click at (x, y) - in app `pid`'s window there when given, else the window on top there. First with
    Mint's own pointer (bg_pointer: events posted to that window only - the user's cursor does not move, the
    window is not raised); when that can't be used, or the window does not change, with the user's real
    pointer, which then goes back where they had it, unless they moved it meanwhile.
    -> the route used: bg_pointer.BACKGROUND ("background_pointer") or bg_pointer.REAL ("global_input")."""
    from mint.screen import bg_pointer
    from mint.screen import effect
    from mint.ui.effects import fx

    if spark:
        time.sleep(fx.click(x, y, label[:40]))
    count = 2 if double else 1
    mine = bg_pointer.click(x, y, button=button, count=count, pid=pid)
    if mine.landed:
        axkit.note_click(mine.target.pid, x, y, label)
        _debug(f"background pointer click at ({int(x)}, {int(y)}) in {mine.target.app} "
               f"({'window changed' if mine.checked else 'not checked'})")
        return bg_pointer.BACKGROUND
    if mine.why:
        _debug(f"background pointer not used at ({int(x)}, {int(y)}): {mine.why}")
    was = effect.pointer_at()
    point = Quartz.CGPointMake(x, y)
    down, up, which = (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft)
    if button == "right":
        down, up, which = (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight)
    # Enter first, then press: Chromium ignores a press with no hover before it.
    _post(Quartz.kCGEventMouseMoved, point, which)
    time.sleep(0.09)
    for n in range(count):
        _post(down, point, which, n + 1)
        time.sleep(0.045)
        _post(up, point, which, n + 1)
        time.sleep(0.06)
    axkit.note_click(owner_at(x, y), x, y, label)
    if button != "right":          # an open context menu follows the pointer; leave it
        time.sleep(0.05)
        effect.give_pointer_back(was, (x, y))
    _learn(mine)
    return bg_pointer.REAL


def _learn(mine) -> None:
    """Mint's pointer was sent and its window did not change: when the real pointer's click does change it,
    that app is one Mint's pointer does not reach (for this run - next time straight to the real pointer)."""
    from mint.screen import bg_pointer
    if mine.sent and mine.checked and mine.target is not None:
        if bg_pointer.wait_for_change(mine.target, mine.before, 0.5):
            bg_pointer.mark_no_reach(mine.target.pid)
            _debug(f"background pointer does not reach {mine.target.app}; real pointer from now on")


def mouse_drag(x0: float, y0: float, x1: float, y1: float, seconds: float = 0.6, pid: int | None = None) -> str:
    """Press at (x0, y0), move to (x1, y1) over `seconds`, release. With Mint's own pointer when both ends are
    in one window (bg_pointer.drag), else with the user's pointer, which goes back where they had it
    afterwards (unless they moved it meanwhile). -> the route used, as mouse_click."""
    from mint.screen import bg_pointer
    from mint.app import control
    from mint.screen import effect
    mine = bg_pointer.drag(x0, y0, x1, y1, seconds, pid=pid)
    if mine.landed:
        _debug(f"background pointer drag in {mine.target.app}")
        return bg_pointer.BACKGROUND
    if mine.why:
        _debug(f"background pointer not used for the drag: {mine.why}")
    was = effect.pointer_at()
    start = Quartz.CGPointMake(x0, y0)
    _post(Quartz.kCGEventMouseMoved, start)
    time.sleep(0.08)
    _post(Quartz.kCGEventLeftMouseDown, start)
    time.sleep(0.12)                        # a held press: lists and canvases tell a drag from a click by it
    steps = max(12, int(seconds * 60))
    for i in range(1, steps + 1):
        if control.stopped():
            break
        t = i / steps
        t = t * t * (3 - 2 * t)             # ease in and out, like a hand
        _post(Quartz.kCGEventLeftMouseDragged, Quartz.CGPointMake(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
        time.sleep(seconds / steps)
    time.sleep(0.08)
    _post(Quartz.kCGEventLeftMouseUp, Quartz.CGPointMake(x1, y1))
    time.sleep(0.1)
    effect.give_pointer_back(was, (x1, y1))
    _learn(mine)
    return bg_pointer.REAL


def same_app(name: str, app) -> bool:
    """Is `app` (an NSRunningApplication) the app called `name`? Its menu-bar name, its .app file name and the
    usual other names all count: VS Code runs as "Code" but is "Visual Studio Code.app" and "VS Code"."""
    from mint.knowledge.skills import app_key
    if app is None or not name:
        return False
    names = [app.localizedName() or ""]
    url = app.bundleURL()
    if url is not None:
        names.append(str(url.lastPathComponent() or "").removesuffix(".app"))
    want = app_key(name)
    return any(n and (n.lower() == name.lower() or app_key(n) == want) for n in names)


def running_app(name: str):
    """The running app called `name` (see same_app), or None."""
    apps = list(AppKit.NSWorkspace.sharedWorkspace().runningApplications())
    exact = next((a for a in apps if (a.localizedName() or "").lower() == (name or "").lower()), None)
    return exact or next((a for a in apps if a.activationPolicy() == 0 and same_app(name, a)), None)


def bring_forward(app_or_name, wait: float = 2.0) -> bool:
    """Bring an app to the front, and check that it really is.

    Measured from inside Mint (a background app) with Claude in front: macOS's
    cooperative activation refused NSRunningApplication.activate, the
    AXFrontmost attribute, `open -a`, an osascript subprocess and pressing the
    Dock icon through Accessibility - all left Claude in front. An AppleScript
    `activate` sent from Mint's own process worked 5/5 (ChatGPT, Finder,
    Slack). So that first, the others as a fallback.
    """
    ws = AppKit.NSWorkspace.sharedWorkspace()
    me = AppKit.NSRunningApplication.currentApplication()
    from mint.core import prefs
    own = (isinstance(app_or_name, str) and app_or_name.strip().lower() in ("mint", "jarvis", prefs.name().lower())) or \
        (not isinstance(app_or_name, str) and app_or_name.processIdentifier() == me.processIdentifier())
    if own:
        # Mint itself: the visible process is this Python child, not the
        # launcher bundle - an AppleScript "activate" to "Mint" waited 127 s for
        # a launcher that never answers.
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        AX.AXUIElementSetAttributeValue(AX.AXUIElementCreateApplication(me.processIdentifier()), "AXFrontmost", True)
        time.sleep(0.2)
        front = ws.frontmostApplication()
        return front is not None and front.processIdentifier() == me.processIdentifier()
    if isinstance(app_or_name, str):
        name = app_or_name
        app = running_app(name)
    else:
        app, name = app_or_name, app_or_name.localizedName() or ""
    pid = app.processIdentifier() if app is not None else None

    def in_front() -> bool:
        front = ws.frontmostApplication()
        if front is None:
            return False
        if pid is not None:
            return front.processIdentifier() == pid
        return (front.localizedName() or "").lower() == name.lower()

    if in_front() and (app is None or show_window(app)):
        return True
    quoted = name.replace("\\", "\\\\").replace('"', '\\"')
    script = AppKit.NSAppleScript.alloc().initWithSource_(
        f'with timeout of 3 seconds\ntell application "{quoted}" to activate\nend timeout')
    script.executeAndReturnError_(None)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if in_front():
            break
        time.sleep(0.1)
    if not in_front() and app is not None:
        app.activateWithOptions_(AppKit.NSApplicationActivateAllWindows)
        axkit.bring_to_front(app, timeout=1.0)
    if app is None:
        app = next((a for a in ws.runningApplications()
                    if (a.localizedName() or "").lower() == name.lower()), None)
    return in_front() and (app is None or show_window(app))


def visible_window(pid: int):
    """Box of the app's frontmost ordinary window actually drawn on this
    screen, or None. Being the active app is not enough: in testing ChatGPT was
    'in front' while its window was not on screen at all (the user was on the
    desktop), and Accessibility still reported every button's box."""
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    for w in windows:
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer", 0) == 0 and w.get("kCGWindowAlpha", 1) > 0:
            b = w.get("kCGWindowBounds") or {}
            if b.get("Width", 0) > 100 and b.get("Height", 0) > 100:
                return (b["X"], b["Y"], b["Width"], b["Height"])
    return None


def show_window(app, wait: float = 2.0, activate: bool = True) -> bool:
    """Make sure the app has a window on screen: un-minimise, raise, or ask
    the app to reopen one (what clicking its Dock icon does).

    activate=False (an accessibility action, which reaches a window behind others or on another
    Space): only check that Accessibility has an ordinary window to work in - never raise, reopen or
    activate. Raising one in a harness run switched the user out of their full-screen Space."""
    pid = app.processIdentifier()
    if visible_window(pid):
        return True
    element = AX.AXUIElementCreateApplication(pid)
    if not activate:
        return any(not axkit.attr(window, "AXMinimized") for window in axkit.attr(element, "AXWindows") or [])
    for window in axkit.attr(element, "AXWindows") or []:
        if axkit.attr(window, "AXMinimized"):
            AX.AXUIElementSetAttributeValue(window, "AXMinimized", False)
        AX.AXUIElementPerformAction(window, "AXRaise")
        break
    if app.isHidden():
        app.unhide()
    deadline = time.monotonic() + wait / 2
    while time.monotonic() < deadline:
        if visible_window(pid):
            return True
        time.sleep(0.1)
    name = (app.localizedName() or "").replace('"', '\\"')
    script = AppKit.NSAppleScript.alloc().initWithSource_(
        f'with timeout of 3 seconds\ntell application "{name}"\nreopen\nactivate\nend tell\nend timeout')
    script.executeAndReturnError_(None)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if visible_window(pid):
            return True
        time.sleep(0.1)
    return False


def owner_at(x: float, y: float) -> int | None:
    """pid of the ordinary window on top at a screen point (Quartz coordinates).

    Accessibility happily reports the boxes of a window that is buried under
    another app: in testing, ChatGPT's controls were listed while the Claude
    app covered them, and a click there would have landed in Claude."""
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID) or []
    for w in windows:
        if w.get("kCGWindowLayer", 0) != 0 or w.get("kCGWindowAlpha", 1) == 0:
            continue
        if w.get("kCGWindowOwnerPID") == os.getpid():     # Mint's own glow round the target, not a window
            continue
        b = w.get("kCGWindowBounds") or {}
        if b and b["X"] <= x <= b["X"] + b["Width"] and b["Y"] <= y <= b["Y"] + b["Height"]:
            return w.get("kCGWindowOwnerPID")
    return None


def ensure_on_top(inv: dict, x: float, y: float) -> str:
    """'' if the point belongs to the app we inventoried; else try to bring it
    forward once, and explain if that fails."""
    pid = inv.get("pid")
    if pid is None or pid == os.getpid() or owner_at(x, y) == pid:   # owner_at skips Mint's own windows
        return ""
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is not None:
        bring_forward(app)
        show_window(app)
        time.sleep(0.25)
        if owner_at(x, y) == pid:
            return ""
    covering = owner_at(x, y)
    other = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(covering) if covering else None
    return (f"FAILED: {inv['app']}'s control is covered by {other.localizedName() if other else 'another window'} "
            "at that spot, so nothing was clicked. Bring the app to the front first.")


def _center(e) -> tuple[float, float]:
    x, y, w, h = e["box"]
    return x + w / 2, y + h / 2


def _fingerprint(inv: dict) -> set:
    # Enabled state counts: in testing, clicking ChatGPT's "Create project"
    # first only greyed the dialog out while it worked.
    return {(e["kind"], e["label"].lower(), e["value"].lower(), e["enabled"])
            for e in inv["elements"] if e["label"] or e["value"]}


def _focused(inv: dict) -> str:
    for e in inv["elements"]:
        if e["focused"]:
            return f"{e['kind']} '{e['label'][:40]}'"
    return ""


def _changes(before: dict, after: dict) -> str:
    notes = []
    if _focused(after) and _focused(after) != _focused(before):
        notes.append(f"focus moved to the {_focused(after)}")
    if before["dialog"] and not after["dialog"]:
        notes.append(f"the dialog '{before['dialog_title'] or 'dialog'}' closed")
    elif after["dialog"] and not before["dialog"]:
        notes.append(f"a dialog opened: '{after['dialog_title'] or 'untitled'}'")
    if before["title"] != after["title"]:
        notes.append(f"window title is now '{after['title'][:60]}'")
    if before["app"] != after["app"]:
        notes.append(f"{after['app']} is now in front")
    appeared = [f"{k} '{l[:40]}'" + ("" if on else " (disabled)")
                for k, l, _, on in sorted(_fingerprint(after) - _fingerprint(before),
                                          key=lambda t: -len(t[1]))[:6]]
    if appeared:
        notes.append("new on screen: " + ", ".join(appeared))
    return "; ".join(notes)


def dismiss() -> str:
    """Close the open dialog, menu or popover like a person: Escape, then its
    Close/Cancel button, then a click on the empty backdrop beside it. Some
    popups ignore Escape (ChatGPT's project picker and chat search did)."""
    from mint.tools import fastinput
    inv = inventory()
    if not inv["dialog"]:
        return "Nothing to close: no dialog or menu is open."
    title = inv["dialog_title"] or "dialog"
    fastinput.press_key("escape")
    time.sleep(0.5)
    now = inventory()
    if not now["dialog"]:
        return f"Closed the {title} (Escape)."
    closer = next((e for e in now["elements"] if e["in_dialog"] and e["role"] == "AXButton" and
                   " ".join(_words(e["label"])) in ("close", "close dialog", "cancel", "dismiss", "done", "not now")), None)
    if closer is not None:
        _press(closer, now, is_own(front_app()))       # its accessibility press first: no cursor
        time.sleep(0.5)
        if not inventory()["dialog"]:
            return f"Closed the {title} with its '{closer['label']}' button."
    # The backdrop: a spot in the window, outside the dialog, with no control on it.
    dx, dy, dw, dh = now["dialog"]
    wx, wy, ww, wh = now["window"]
    for px, py in ((dx + dw / 2, dy + dh + 40), (dx + dw / 2, dy - 40), (dx - 40, dy + dh / 2), (dx + dw + 40, dy + dh / 2)):
        if not (wx + 10 < px < wx + ww - 10 and wy + 40 < py < wy + wh - 10):
            continue
        # Only real controls are obstacles; a container group spans the whole window.
        if any(_inside((px - 1, py - 1, 2, 2), e["box"], slack=0) for e in now["elements"]
               if not e["in_dialog"] and e["interactive"] and e["box"][2] * e["box"][3] < 0.2 * ww * wh):
            continue
        mouse_click(px, py, label="")
        time.sleep(0.5)
        if not inventory()["dialog"]:
            return f"Closed the {title} by clicking beside it."
    return f"FAILED: the {title} is still open after Escape, its buttons and a click beside it."


SEND_LABELS = ("send", "send message", "send prompt", "submit", "send now", "send reply")


def _send_button(inv, x: float, y: float):
    """The Send button of a chat box: a button labelled send/submit, nearest the field."""
    buttons = [e for e in candidates(inv, "click")
               if e["role"] == "AXButton" and e["label"].strip().lower() in SEND_LABELS]
    if not buttons:
        return None
    return min(buttons, key=lambda e: (_center(e)[0] - x) ** 2 + (_center(e)[1] - y) ** 2)


def _sent_check(app, text: str) -> str:
    """After sending in a chat: did it go? A reply starting (a Stop button) or the
    message showing in the conversation says yes."""
    from mint.tools import work as work_tools
    window = work_tools._front_window(app or front_app())
    probe = " ".join(text.split())[:40].lower()
    for _ in range(4):
        _, busy, seen = work_tools._window_state(window)
        if busy or probe in " ".join(seen.split()).lower():
            return " - it was sent."
        time.sleep(0.5)
    return (" WARNING: the box emptied but nothing shows the message was sent (no reply started and it is "
            "not in the conversation). Check with look; if it was lost, type it again.")


def _would_wipe(chosen: dict, text: str) -> str:
    """Typing selects everything in the field first. In a document's text area (TextEdit, Notes, a mail
    body) that replaces the whole document with `text` - on 29 Sep "change 'Dear Sir' to 'Dear Ms. Rao'"
    left a letter holding just "Dear Ms. Rao". Say why not ("" = fine): a short field, or `text` that is a
    rewrite of the whole thing, is typed as usual."""
    if chosen["role"] != "AXTextArea":
        return ""
    raw = axkit.attr(chosen["ref"], "AXValue")
    if not isinstance(raw, str):
        return ""
    old, new = _text(raw), _text(text)
    lines = sum(1 for line in raw.splitlines() if line.strip())
    if (len(old) < 120 and lines < 3) or len(new) >= 0.6 * len(old):
        return ""
    return (f"FAILED: nothing was typed. The {chosen['kind']} holds a whole document ({len(old)} characters, "
            f"{lines} lines) and typing replaces ALL of it with '{new[:40]}'. To change a word or phrase: for a "
            "saved file, write_file mode=replace (find = the old words; keeps .rtf formatting and reloads TextEdit), "
            "or in the app menu Edit > Find > Find and Replace…, then File > Save. To rewrite the whole text, "
            "give all of the new text.")


# A snapshot id as ui_elements lists it: "s12:7" or "[s12:7]".
_ID_TARGET = re.compile(r"\s*\[?\s*s\d+:\d+\s*\]?\s*")
# Accessibility presses that showed no effect. Asked for again soon after, the same control gets the
# real pointer instead - the "try again" the UNVERIFIED result promises - because pressing twice
# automatically could send a message twice or flip a toggle back.
_QUIET_FOR = 120.0
_ax_quiet: dict = {}
# When Mint brought an app forward for a pointer click and then gave the front back: the time it
# registered that app as the one being worked in, so the next screen tool brings it back first.
_handed_back: dict = {"pid": 0, "at": -1.0}
# Menus Mint read without leaving them open (a pop-up button's choices, a context menu), by pid: the
# next click on one of their items, soon after, picks it through Accessibility (effect.pick_from_menu).
_MENU_FOR = 120.0
_menus: dict = {}
# An app Mint had to bring forward to open a menu, by pid: {"prior", "at"}. It stays in front while the
# menu is open; the next action in it gives the front back to `prior`.
_menu_front: dict = {}
_MENU_FILLER = {"the", "a", "in", "on", "from", "of", "menu", "pop", "up", "popup", "option", "choice", "item",
                "choose", "select", "pick", "set", "to", "list", "dropdown", "drop", "down", "button"}


def _quiet_key(inv: dict, e: dict) -> tuple:
    return (inv.get("pid"), e["role"], e["label"].lower(), tuple(round(c / 4) for c in e["box"]))


def _user_wants(app) -> bool:
    """Did the user ask for this app just now (Mint opened or switched to it)? Then it stays in front."""
    try:
        from mint.tools import extra as extra_tools
        t = extra_tools._target
        return (t["app"] is not None and t["app"].processIdentifier() == app.processIdentifier()
                and time.monotonic() - t["at"] < extra_tools.TARGET_FOR and t["at"] != _handed_back["at"])
    except Exception:
        return False


def _register_target(app) -> None:
    """The next type_text / ui_act must still go to `app`, not to the app given the front back:
    extra_tools brings the app it holds forward before every screen tool."""
    try:
        from mint.tools import extra as extra_tools
        now = time.monotonic()
        extra_tools._target["app"], extra_tools._target["at"] = app, now
        _handed_back.update(pid=app.processIdentifier(), at=now)
    except Exception:
        log.debug("register target", exc_info=True)


class _Front:
    """Activation only when it is needed. Accessibility presses and writes reach an app behind other
    windows, so the target is brought forward only for the pointer or the keyboard - and then the app
    the user was in gets the front back afterwards, unless they asked for this one."""

    def __init__(self, app, own: bool):
        self.app, self.own, self.raised = app, own, False
        self.prior = front_app()

    def in_front(self) -> bool:
        front = front_app()
        return self.own or self.app is None or (
            front is not None and front.processIdentifier() == self.app.processIdentifier())

    def need(self) -> bool:
        """Bring the target forward (once); False if it would not come."""
        if self.in_front():
            return True
        self.raised = bring_forward(self.app)
        return self.raised

    def give_back(self) -> str:
        prior = self.prior
        if (not self.raised or prior is None or self.app is None or _user_wants(self.app)
                or prior.processIdentifier() in (self.app.processIdentifier(), os.getpid())):
            return ""
        _register_target(self.app)
        if bring_forward(prior, wait=1.0):
            return f"{prior.localizedName()} is back in front, as the user had it"
        return ""


def _covered(pid: int) -> bool:
    """Is another app's window over part of this app's window?"""
    box = visible_window(pid)
    if box is None:
        return True
    x, y, w, h = box
    return any(owner_at(x + w * fx_, y + h * fy) != pid
               for fx_, fy in ((0.5, 0.5), (0.15, 0.15), (0.85, 0.15), (0.15, 0.85), (0.85, 0.85)))


def _ax_plan_for(e: dict, inv: dict, action: str = "click", quiet: bool = False) -> str:
    from mint.screen import effect
    try:
        return effect.ax_plan(e["role"], effect.LIVE.actions(e["ref"]), action, web=effect.in_web_area(e["ref"]),
                              chromium=effect.LIVE.chromium(inv.get("pid")), hidden=bool(e.get("hidden")),
                              quiet_before=quiet)
    except Exception:
        log.debug("ax plan", exc_info=True)
        return ""


def _press(e: dict, inv: dict, own: bool = False, front: "_Front | None" = None) -> str:
    """Click one control (a dialog's Close, a chat's Send): its accessibility press when that works,
    else the pointer (bringing the app forward first when `front` is given). -> the route used, ''
    when the pointer could not reach it."""
    from mint.screen import effect
    plan = "" if own else _ax_plan_for(e, inv)
    if plan in ("AXPress", "AXConfirm", "AXPick"):
        delivered, _, problem = effect.ax_deliver(e["ref"], plan)
        if delivered or effect.maybe_acted(problem):
            # An error after the app may already have acted (sent the message): never press again.
            return "accessibility"
    if front is not None and not own:
        x, y = _center(e)
        if not front.need() or ensure_on_top(inv, x, y):
            return ""
    return mouse_click(*_center(e), label=e["label"], pid=inv.get("pid"))


def _took_anyway(chosen: dict, values: tuple, before: dict, app, windows_before: dict, pid,
                 seconds: float = 1.2) -> list[str]:
    """After an accessibility press answered with an error: evidence that it acted all the same - its
    own value or title changed, the window's controls changed, a window or sheet of the app came or
    went. [] when nothing shows it."""
    from mint.screen import effect
    evidence = []
    now = (axkit.attr(chosen["ref"], "AXValue"), axkit.attr(chosen["ref"], "AXTitle"))
    if now != values:
        evidence.append(f"its value/title changed to {now[0] if now[0] != values[0] else now[1]!r} (read back)")
    changed, deadline = "", time.monotonic() + seconds
    while not changed and time.monotonic() < deadline:
        time.sleep(0.3)
        changed = _changes(before, inventory(app))
    evidence += ([changed] if changed else []) + effect.window_changes(windows_before, effect.window_snapshot(),
                                                                      pids=(pid,))
    return evidence


def _option_named(target: str, held: dict) -> str:
    """The item of a remembered menu that `target` names ("Large", "Large in the Size menu"), or ''."""
    from mint.screen import effect
    want = effect.option_key(target)
    exact = [t for t in held["titles"] if effect.option_key(t) == want]
    if len(exact) == 1:
        return exact[0]
    words, label = set(want.split()), set(effect.option_key(held["label"]).split())
    loose = [t for t in held["titles"] if set(effect.option_key(t).split()) <= words
             and words - set(effect.option_key(t).split()) <= label | _MENU_FILLER]
    return loose[0] if len(loose) == 1 else ""


def _settle_front(app, prior) -> str:
    """After a menu was used in the background: if the app pushed itself in front meanwhile, give the
    front back to the user's app. -> a note for the result ('' when nothing happened)."""
    now = front_app()
    if (app is None or prior is None or now is None or now.processIdentifier() != app.processIdentifier()
            or prior.processIdentifier() in (app.processIdentifier(), os.getpid()) or _user_wants(app)):
        return ""
    if bring_forward(prior, wait=1.0):
        return f"{app.localizedName()} came forward by itself while its menu was open; {prior.localizedName()} " \
               "is back in front"
    return ""


def _pick_remembered(target: str, app, started: float) -> str:
    """'click Large' just after Mint read the Size pop-up's choices: choose it in that menu through
    Accessibility - open, press, closed again, the app left where it was. '' when this is not that."""
    from mint.screen import effect
    held = _menus.get(app.processIdentifier()) if app is not None else None
    if not held or time.monotonic() - held["at"] > _MENU_FOR:
        return ""
    title = _option_named(target, held)
    if not title:
        return ""
    prior, pid = front_app(), app.processIdentifier()
    before = inventory(app)
    windows_before = effect.window_snapshot()
    try:
        pick = effect.pick_from_menu(held["ref"], held["action"], title, pid)
    except Exception:
        log.debug("pick from a remembered menu", exc_info=True)
        return ""
    if not pick.ok and not pick.titles:
        _menus.pop(pid, None)            # the button is gone or its menu no longer opens: choose as usual
        return ""
    if pick.ok:
        _menus.pop(pid, None)            # used: a later "Save" means the window's own button again
    else:
        held.update(at=time.monotonic(), titles=pick.titles or held["titles"])
    note = _settle_front(app, prior)
    where = f"the {held['name']}"
    summary = f"Chose '{pick.picked or title}' in {where} (its menu was opened and closed again through Accessibility)"
    summary += (f"; {note}" if note else "") + f" ({time.monotonic() - started:.1f}s)"
    if not pick.ok:
        if effect.maybe_acted(pick.problem):
            return effect.Effect(effect.UNVERIFIABLE, summary + f"; the app answered with an error ({pick.problem}), "
                                 "but it may have taken", "accessibility", "background",
                                 escalation=f"check {where} with ui_elements before choosing again").render()
        return effect.refused(f"could not choose '{title}' in {where}: {pick.problem}, so nothing was chosen",
                              "say which of these: " + ", ".join(f"'{t}'" for t in pick.titles[:20]))
    evidence = list(pick.evidence)
    if not evidence:
        changed = _changes(before, inventory(app))
        evidence += ([changed] if changed else []) + effect.window_changes(windows_before, effect.window_snapshot(),
                                                                          pids=(pid,))
    if evidence:
        return effect.Effect(effect.CONFIRMED, summary, "accessibility", "background", evidence).render()
    return effect.Effect(effect.UNVERIFIABLE, summary + "; nothing visibly changed", "accessibility", "background",
                         escalation="check with ui_elements or verify_state before choosing again").render()


def _read_menu(chosen: dict, name: str, why: str, plan: str, inv: dict, app, front, started: float) -> str:
    """A click that opens a menu, in an app behind the user's window: read its items while it is open
    for a moment, close it, and remember them, so the next click on one of them picks it. '' when the
    menu listed nothing (then it is opened the ordinary way, in front)."""
    from mint.screen import effect
    pid = inv.get("pid")
    try:
        listed = effect.list_menu(chosen["ref"], plan, pid)
    except Exception:
        log.debug("read a menu", exc_info=True)
        return ""
    if not listed.titles:
        return ""
    _menus[pid] = {"ref": chosen["ref"], "action": plan, "label": chosen["label"], "name": name,
                   "titles": listed.titles, "at": time.monotonic()}
    note = _settle_front(app or front.app, front.prior)
    choices = ", ".join(f"'{t}'" + (" (selected)" if t == listed.selected else "") for t in listed.titles[:30])
    example = next((t for t in listed.titles if t != listed.selected), listed.titles[0])
    summary = (f"Opened the {name} ({why}) and read its choices: {choices}. Its menu was closed again so "
               f"{inv['app']} could stay where it was - to choose one, call ui_act click with its name (e.g. "
               f"target='{example}') and Mint picks it through Accessibility" + (f"; {note}" if note else "")
               + f" ({time.monotonic() - started:.1f}s)")
    return effect.Effect(effect.CONFIRMED, summary, "accessibility", "background",
                         [f"its menu listed {len(listed.titles)} choices (read while it was open)"]).render()


def act(action: str, target: str, text: str = "", press_return: bool = False,
        replace: bool = True, app=None) -> str:
    """The whole thing: find it, do it, check it happened - and say so as an effect (effect.py).

    Doing it: through Accessibility first (AXPress, AXSelected, AXFocused, AXSelectedText): the
    user's cursor stays where it is and the app may stay behind their windows. The real pointer and
    keyboard only when that is not possible (Chromium web content, double clicks, a write the app
    refused), and then the app the user was in gets the front back afterwards."""
    from mint.app import control
    from mint.screen import effect
    if control.stopped():
        return "STOPPED by the user; nothing was done."
    action = (action or "click").lower().replace(" ", "_")
    if action in ("dismiss", "close_dialog", "close"):
        return dismiss()
    if action in ("fill", "enter", "input", "type_into"):
        action = "type"
    started = time.monotonic()
    by_id = None
    if _ID_TARGET.fullmatch(target or ""):
        # An id from ui_elements ("s12:7"). Resolved before anything is read again: a new reading of
        # the window makes it stale. (A bare "7" stays a label - Calculator has a button called 7.)
        by_id, problem = resolve(target)
        if by_id is None:
            return effect.refused(problem)
        source = snapshot(target.strip(" []").split(":")[0])
        if app is None and source and source.get("pid"):
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(source["pid"])
    own = is_own(app)
    target_app = app or front_app()
    front = _Front(None if own else target_app, own)      # who is in front before anything happens
    held = _menu_front.pop(target_app.processIdentifier(), None) if not own and target_app is not None else None
    if held and time.monotonic() - held["at"] < _MENU_FOR and front.in_front():
        front.prior, front.raised = held["prior"], True   # Mint brought it forward for its menu: give it back after
    # Accessibility reaches a window behind others (or on another Space), so it is not raised for
    # choosing either: the pixels choosing may look at are taken from the window itself (capture.area).
    if not own and target_app is not None and not show_window(target_app, activate=False) and not front.need():
        return effect.refused(f"{target_app.localizedName()} has no window on screen (minimised, closed or on "
                              "another Space) and it could not be shown, so nothing was done")
    if action == "click" and by_id is None and not own:
        picked = _pick_remembered(target, target_app, started)
        if picked:
            back = front.give_back()
            return picked + (f" ({back}.)" if back else "")
    before = inventory(app)
    if by_id is not None:
        chosen, inv, why = dict(by_id), before, f"by id {target.strip(' []')}"
        box = axkit.frame(chosen["ref"])            # where it is now, for the pointer
        if box and box[2] >= 1 and box[3] >= 1:
            chosen["box"] = tuple(float(c) for c in box)
    else:
        chosen, inv, why = ground(target, "type" if action == "type" else "click", before)
    if chosen is not None:
        before = inv                    # ground may have taken a fresher look (Electron)
    if chosen is None and not own and front.need():
        by_text = _by_screen_text(action, target, text, press_return, inv)   # reads the front window
        if by_text:
            if front.raised:            # it was brought forward to be read: that is not a background action
                by_text = by_text.replace(" (background):", " (foreground):", 1)
            back = front.give_back()
            return by_text + (f" ({back}.)" if back else "")
    if chosen is None:
        back = front.give_back()
        where = f" (a dialog '{inv['dialog_title']}' is open; only it can be used)" if inv["dialog"] else ""
        options = "; ".join(describe(e, inv["window"]) for e in candidates(inv, action)[:12])
        return effect.refused(f"could not find '{target}' in {inv['app']}{where}, so nothing was done. {why}. "
                              f"Things you could mean: {options}" + (f" ({back})" if back else ""))
    name = f"{chosen['kind']} '{chosen['label'][:50]}'" if chosen["label"] else chosen["kind"]
    windows_before = effect.window_snapshot()
    if action == "type":
        return _act_type(chosen, name, why, text, press_return, replace, inv, before, app, own, front,
                         started, windows_before)
    return _act_click(action, chosen, name, why, inv, before, app, own, front, started, windows_before)


def _show_on_page(ref) -> bool:
    """A web page's control may sit in a scrolled box that Accessibility does not clip (MintFixture's
    extension list: rows below the box's edge kept their full frames): AXScrollToVisible shows it before
    it is pressed, as a person would see it. WebKit scrolls only when needed. -> True when it moved."""
    box = axkit.frame(ref)
    try:
        AX.AXUIElementPerformAction(ref, "AXScrollToVisible")
    except Exception:
        return False
    return bool(box) and axkit.frame(ref) not in (None, box)


def _act_click(action, chosen, name, why, inv, before, app, own, front, started, windows_before) -> str:
    from mint.screen import effect
    from mint.ui.effects import fx
    x, y = _center(chosen)
    key = _quiet_key(inv, chosen)
    quiet = time.monotonic() - _ax_quiet.get(key, -1e9) < _QUIET_FOR
    plan = "" if own else _ax_plan_for(chosen, inv, action, quiet)
    route, delivery, evidence, notes, watched, keep_front = "", "", [], [], False, False
    pid = inv.get("pid")
    time.sleep(fx.click(x, y, chosen["label"][:40]))
    opens_menu = plan == "AXShowMenu" or (action == "click" and plan == "AXPress"
                                          and chosen["role"] in effect.MENU_OPENERS)
    if opens_menu and not front.in_front():
        listed = _read_menu(chosen, name, why, plan, inv, app, front, started)
        if listed:
            back = front.give_back()
            return listed + (f" ({back}.)" if back else "")
        # It listed nothing that way: open it in front, as a click does, and leave the app there while
        # the menu is open - giving the front back would close it. The next action gives it back.
        keep_front = front.need()
    if plan == "AXPress" and chosen.get("in_web_content") and not chosen.get("scrolled") \
            and _show_on_page(chosen["ref"]):
        time.sleep(0.1)
        before = inventory(app, window_id=inv.get("window_id"))   # the scroll is not the press's effect
        chosen = dict(chosen, scrolled="scrolled it into view on the page first")
    if plan:
        values = (axkit.attr(chosen["ref"], "AXValue"), axkit.attr(chosen["ref"], "AXTitle"))
        delivered, evidence, problem = effect.ax_deliver(chosen["ref"], plan)
        if delivered:
            route = "accessibility"
            delivery = "foreground" if front.raised else "background"
        else:
            # Some apps act and still answer with an error ("Apply theme" in the test app; Finder's view
            # switcher in Cua's notes). Look before pressing again: a second press may do it twice.
            took = _took_anyway(chosen, values, before, app, windows_before, pid)
            if opens_menu and not took:
                listed = effect.menu_items(chosen["ref"], pid)      # the call timed out with its menu open
                took = ["its menu is open: " + ", ".join(f"'{t}'" for _, t, _ in listed[:20])] if listed else []
            if took:
                route, delivery, evidence, watched = "accessibility", "foreground" if front.raised else "background", \
                    took, True
                notes.append(f"the app answered with an error ({problem}), but it took")
            elif effect.maybe_acted(problem):
                _ax_quiet[key] = time.monotonic()
                back = front.give_back()
                summary = (f"Pressed the {name} ({why}); the app answered with an error ({problem}) and nothing "
                           "visibly changed - it may have acted anyway, so it was not pressed a second time"
                           + (f"; {back}" if back else "") + f" ({time.monotonic() - started:.1f}s)")
                return effect.Effect(effect.UNVERIFIABLE, summary, "accessibility",
                                     "foreground" if front.raised else "background",
                                     escalation="check whether it happened (ui_elements, verify_state); if it really "
                                     "did nothing, call ui_act again with the same target: the next try uses the "
                                     "real pointer").render()
            else:
                notes.append(f"accessibility could not do it ({problem}), so the pointer was used")
        if route and opens_menu:
            items = effect.menu_items(chosen["ref"], pid)
            if items:
                _menus[pid] = {"ref": chosen["ref"], "action": plan, "label": chosen["label"], "name": name,
                               "titles": [t for _, t, _ in items], "at": time.monotonic()}
    elif quiet:
        notes.append("its accessibility press showed nothing last time, so the real pointer was used")
    if not route:
        if not own:
            if not front.need():
                return effect.refused(f"could not bring {inv['app']} to the front for the click, so nothing was "
                                      "clicked", "switch_to the app, then try again")
            covered = ensure_on_top(inv, x, y)
            if covered:
                return effect.refused(covered)
        route = mouse_click(x, y, button="right" if action == "right_click" else "left",
                            double=action == "double_click", label=chosen["label"], spark=False, pid=pid)
        delivery = "background" if route == "background_pointer" else "foreground"
        # A menu opened with the pointer closes too when the front goes back: keep it until the next action.
        keep_front = keep_front or action == "right_click" or chosen["role"] in effect.MENU_OPENERS
    # Watch for the effect for a while; apps take their time (a dialog that
    # greys out, then closes a second later).
    changed, deadline = "", time.monotonic() + (0 if watched else 2.5)
    while not changed and time.monotonic() < deadline:
        time.sleep(0.35)
        after = inventory(app)
        changed = _changes(before, after)
    if changed and after["dialog"] and after["dialog"] == before["dialog"]:
        time.sleep(0.8)          # the dialog may still be closing
        later = inventory(app)
        changed = _changes(before, later) or changed
    if not watched:
        # Only this app's windows count, and not its coming forward when Mint brought it there.
        evidence = evidence + ([changed] if changed else []) + effect.window_changes(
            windows_before, effect.window_snapshot(), pids=(pid,), raised=pid if front.raised else None)
    took = time.monotonic() - started
    verb = {"double_click": "Double-clicked", "right_click": "Right-clicked"}.get(action, "Clicked")
    if route == "accessibility":
        verb = {"AXShowMenu": "Opened the menu of", "select": "Selected", "focus": "Focused"}.get(plan, "Pressed")
    if not evidence and chosen["role"] in TEXT_INPUT and axkit.attr(chosen["ref"], "AXFocused"):
        evidence = ["it has the keyboard focus, ready to type"]
    if not evidence and not own and route in ("global_input", "background_pointer") and axkit.is_editable(axkit.focused_element(inv.get("pid"))):
        # a placeholder or label of a web/Electron text box (VS Code's "Search Extensions in
        # Marketplace" is static text over a hidden textarea): the click put the cursor in the box
        evidence = ["a text box now has the keyboard focus - type into it with type_text (no field= needed)"]
    if keep_front and front.raised and not _user_wants(front.app):
        _menu_front[pid] = {"prior": front.prior, "at": time.monotonic()}
        notes.append(f"{inv['app']} stays in front while its menu is open; the next action gives the front back")
    else:
        back = front.give_back()
        notes += [back] if back else []
    summary = f"{verb} the {name} ({why})" + "".join(f"; {n}" for n in notes) + f" ({took:.1f}s)"
    if evidence:
        # how it was reached belongs with what it did (no pointer, no wheel: Accessibility scrolled it)
        evidence = evidence + ([chosen["scrolled"]] if chosen.get("scrolled") else [])
        return effect.Effect(effect.CONFIRMED, summary, route, delivery, evidence).render()
    if route == "accessibility":
        _ax_quiet[key] = time.monotonic()
        return effect.Effect(effect.UNVERIFIABLE, summary + "; nothing visibly changed - it may already be in that "
                             "state", route, delivery,
                             escalation="if it really did nothing, call ui_act again with the same target: the "
                             "next try uses the real pointer").render()
    confident = any(k in why for k in ("label match", "exact", "only text field", "by id")) or \
        bool(re.search(r"\((0\.(8[5-9]|9\d)|1\.0+) confident\)", why))
    if confident:
        # Right control, no visible effect: usually it was already in that
        # state (already on a new chat) or the click only moved focus. Not
        # a failure - in testing, calling it FAILED made the model give up
        # on ui_act and guess coordinates instead.
        return effect.Effect(effect.UNVERIFIABLE, summary + "; nothing visibly changed - it may already be in that "
                             "state, or the click only moved focus", route, delivery,
                             escalation="check with ui_elements if it matters").render()
    return effect.Effect(effect.NOOP, summary + "; nothing on screen changed - it may be the wrong control",
                         route, delivery,
                         escalation="call ui_elements to see what is there and name the control exactly; do not "
                         "switch to click_at for a listed control").render()


def _act_type(chosen, name, why, text, press_return, replace, inv, before, app, own, front, started,
              windows_before) -> str:
    from mint.screen import effect
    from mint.tools import fastinput
    from mint.tools import everyday as skills
    from mint.ui.effects import fx
    wipe = _would_wipe(chosen, text) if replace else ""
    if wipe:
        return wipe
    if chosen.get("subrole") == "AXSecureTextField":
        return effect.refused("that is a password field; Mint never types passwords - ask the user to type it")
    ref, (x, y) = chosen["ref"], _center(chosen)
    pid = inv.get("pid")
    chromium = effect.LIVE.chromium(pid)
    web = effect.in_web_area(ref)
    whole = replace and bool(chosen["value"])
    raw = axkit.attr(ref, "AXValue")
    value_before = raw if isinstance(raw, str) else None
    progress, count, route, delivery, notes = "skipped", 0, "", "", []
    time.sleep(fx.click(x, y, chosen["label"][:40] or "field"))
    if not own and not chosen.get("hidden") and not chromium:
        # 1. Straight into the field through Accessibility: no keys, no clipboard, no cursor.
        progress, count, problem = effect.ax_insert(ref, text, at="all" if replace else "end")
        if progress in ("complete", "partial", "unverifiable"):
            route, delivery = "accessibility", "foreground" if front.raised else "background"
        elif problem and progress == "rejected":
            notes.append(problem)
    if not route:
        if not own:
            if not front.need():
                return effect.refused(f"could not bring {inv['app']} to the front to type, so nothing was typed",
                                      "switch_to the app, then try again")
            covered = ensure_on_top(inv, x, y)
            if covered:
                return effect.refused(covered)
        clicked = web or chromium or bool(chosen.get("hidden")) or own
        if not clicked:
            axkit.focus(ref)
            time.sleep(0.1)
            clicked = axkit.attr(ref, "AXFocused") is not True
        if clicked:
            # Electron/web editors take the keyboard only after a real click into them.
            mouse_click(x, y, label=chosen["label"] or "field", spark=False)
            time.sleep(0.15)
            if not axkit.attr(ref, "AXFocused"):
                axkit.focus(ref)
                time.sleep(0.1)
        if whole:
            fastinput.press_key("a", ["command"])
        keyed = not (web or chromium or own or chosen.get("hidden")) and len(text) <= fastinput.UNICODE_MAX
        if keyed:
            # 2. The characters as key events, to this app only. Still no clipboard.
            fastinput.type_unicode(text, pid=pid)
            time.sleep(0.15)
            raw = axkit.attr(ref, "AXValue")
            after = raw if isinstance(raw, str) else None
            progress, count = (effect.replaced_progress if whole else effect.typed_progress)(value_before, after, text)
            route, delivery = "synthetic_events", "foreground"
        if not keyed or progress == "unchanged":
            # 3. Last resort: paste, borrowing the clipboard and putting it back.
            with skills._Clipboard() as clip:
                clip.board.clearContents()
                clip.board.setString_forType_(text, AppKit.NSPasteboardTypeString)
                fastinput.press_key("v", ["command"])
                time.sleep(0.15)
            value = _text(axkit.attr(ref, "AXValue"))
            # After real keys this is the page's own state, not an echo of an accessibility write.
            progress = ("complete" if text.strip()[:30].lower() in value.lower() else "unchanged") if value else \
                "unverifiable"
            count = len(text) if progress == "complete" else 0
            route, delivery = "global_input", "foreground"
    typed_ok = {"complete": True, "partial": True, "unchanged": False}.get(progress)
    sent_note, how = "", ""
    if press_return and progress != "unchanged":
        time.sleep(0.3)             # let the editor take the text before Return
        # A chat box with a Send button: click it, as a person would. In
        # ChatGPT (24 Sep) Return emptied the box and the message vanished,
        # unsent, three times in a row, while clicking Send worked at once.
        # Its accessibility press reaches the app behind other windows; only the pointer or the
        # Return key need it in front.
        send = _send_button(inventory(app), x, y)
        if send is not None:
            _press(send, inv, own, front)
            how = " and clicked Send"
        else:
            front.need()            # Return goes to the app in front
            fastinput.press_key("return")
            how = " and pressed Return"
        time.sleep(0.7)
        remaining = _text(axkit.attr(ref, "AXValue"))
        if typed_ok and text.strip()[:30].lower() in remaining.lower():
            # Still there: it was not sent. Do what a person does.
            send = send or _send_button(inventory(app), x, y)
            if send is not None and how != " and clicked Send":
                _press(send, inv, own, front)
                sent_note = "; Return did not send it, so it clicked Send"
            else:
                sent_note = "; FAILED to send: the text is still in the field"
        elif typed_ok and send is not None:
            sent_note = "; " + _sent_check(app, text).strip(" -.")
    time.sleep(0.6)
    after_inv = inventory(app)
    changed = _changes(before, after_inv)
    evidence = []
    if progress == "complete":
        evidence.append("the field reads it back")
    evidence += ([changed] if changed else []) + effect.window_changes(
        windows_before, effect.window_snapshot(), pids=(pid,), raised=pid if front.raised else None)
    if front.raised:
        delivery = "foreground"     # brought forward on the way (Return, the pointer): not a background action
    back = front.give_back()
    notes += [back] if back else []
    summary = (f"Typed '{text[:60]}' into the {name} ({why}){how}{sent_note}" + "".join(f"; {n}" for n in notes)
               + f" ({time.monotonic() - started:.1f}s)")
    if progress == "complete" and "FAILED to send" not in sent_note:
        return effect.Effect(effect.CONFIRMED, summary, route, delivery, evidence).render()
    if progress == "partial" or (typed_ok and "FAILED to send" in sent_note):
        return effect.Effect(effect.PARTIAL, summary + (f"; only {count} of {len(text)} characters landed"
                                                        if progress == "partial" else ""),
                             route, delivery, evidence, delivered=count,
                             escalation="check the field with ui_elements, then type only what is missing"
                             if progress == "partial" else "click Send (ui_act click target='Send')").render()
    if typed_ok is False:
        return effect.Effect(effect.NOOP, summary + "; the field does not show it", route, delivery, evidence,
                             escalation="click into the field (ui_act click) and type again, or check with look"
                             ).render()
    return effect.Effect(effect.UNVERIFIABLE, summary + "; the field's text could not confirm it", route, delivery,
                         evidence, escalation="look at it before typing again - never type it twice").render()


def _shown_as(text: str) -> str:
    """A label as read off the screen, without its shortcut hint: Telegram's "Q Search (⌘K)" (read "(9K)") is
    "search"."""
    from mint.screen import ocr
    return ocr._words(re.sub(r"\([^()]{1,6}\)", " ", text))


def _by_screen_text(action: str, target: str, text: str, press_return: bool, inv: dict) -> str:
    """Accessibility doesn't list it, but its words are written on screen in the front window
    (an Electron app's search box placeholder, a button VS Code didn't expose): click those words
    where they are - and for `type`, type into what the click focused. '' when not applicable."""
    if action not in ("click", "double_click", "type") or inv.get("dialog"):
        return ""
    from mint.screen import ocr
    want = ocr._words(_core(target))
    if not want:
        return ""
    # The inventory's own app: its window alone is read (ocr.read_window), so the words of a window
    # lying over it - or of the front app, when this one is in the background - are never its.
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(inv["pid"]) if inv.get("pid") else None
    app = None if app is not None and is_own(app) else app
    try:
        if app is not None:
            items, _, front = ocr.read_window(app)
        else:
            items, _ = ocr.read_screen()
            front = ocr._front_window()
    except Exception:
        return ""
    mine = [i for i in items if front is None or ocr._inside(i, front)]
    hits = [i for i in mine if _shown_as(i["text"]) == want]
    if not hits:        # a placeholder that says more: "Search chats" for the search box
        hits = [i for i in mine if _shown_as(i["text"]).startswith(want + " ") and len(_shown_as(i["text"]).split()) <= 3]
    if len(hits) != 1:
        return ""
    clicked = ocr.click_text(hits[0]["text"], double=action == "double_click", app=app)
    note = " (Accessibility did not list it; found by its text on screen)"
    if action != "type":
        return clicked + note
    if clicked.startswith(("FAILED", "STOPPED", "Cannot", "SUSPECTED NO-OP")) and "keyboard focus" not in clicked:
        return clicked + note
    from mint.tools import everyday as skills
    typed = skills.type_text(text, press_return)
    return f"{clicked} Then: {typed}{note}"


def summary(limit: int = 140, since: str | None = None) -> str:
    """What is on screen now, as the model should see it: for ui_elements. Only things with a name (or a
    text box), and not a row's own text repeated under it - a VS Code window listed 60 mostly unnamed
    controls and its search results and Install buttons never made the list. Controls carry their
    snapshot token ("[s12:7] button 'Install' in 'Rainbow CSV'"); plain text is context, no token.
    With `since` (an earlier snapshot of the same window), only what changed: + ~ - rows."""
    inv = inventory()
    inv = settle_pending(inv) or inv
    head = f"{inv['app']} - '{inv['title'][:60]}'"
    if inv.get("snapshot"):
        head += f" [snapshot {inv['snapshot']}]"
    if inv.get("degraded"):
        return head + f"\nNothing read: {inv['degraded']}."
    if inv["dialog"]:
        head += f"; a dialog is open: '{inv['dialog_title']}' (only it can be used)"
    if inv.get("truncated"):
        head += f"; the window is large - reading {inv['truncated']}, so this list may be incomplete"
    if since:
        changes = changes_since(since, inv)
        if changes is not None:
            return head + "\n" + changes
        head += f"\n({since} is unknown or was another window; everything follows)"
    pool = [e for e in candidates(inv, "click") if e["label"] or e["role"] in TEXT_INPUT]
    groups = [g for g in pool if g["role"] == "AXGroup" and len(g["label"]) > 12]

    def repeated(e):
        if e["role"] != "AXStaticText":
            return False
        low = e["label"].lower()
        return any(low in g["label"].lower() and _overlap(g["box"], e["box"]) > 0.5 for g in groups if g is not e)
    pool = [e for e in pool if not repeated(e)]
    lines = [describe(e, inv["window"], token=True) for e in pool[:limit]]
    if not lines:       # Telegram for Mac, games, some Qt and Java apps: the window tells Accessibility nothing
        lines = ["(nothing listed: this app doesn't share its buttons and boxes with Accessibility. Read it with "
                 "read_window or look, and click by the words on screen with click_text, or ui_act with the words "
                 "you see as the target - the search box by its placeholder, e.g. 'Search'.)"]
    more = f"\n... and {len(pool) - limit} more" if len(pool) > limit else ""
    hidden = offscreen_text(inv)
    return head + "\n" + "\n".join(lines) + more + (f"\n{hidden}" if hidden else "")
