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


def inventory(app=None, limit: int = 5000) -> dict:
    """Everything visible and usable in the front window.

    -> {app, title, window: box, dialog: box|None, dialog_title, elements: [...]}
    Each element: {id, role, kind, label, value, box, enabled, focused, in_dialog, ref}.
    """
    app = app or front_app()
    if app is None:
        return {"app": "", "title": "", "window": None, "dialog": None, "elements": []}
    axkit.unlock(app)
    pid = app.processIdentifier()
    window = _own_panel(pid) if is_own(app) else axkit.focused_window(pid)
    if window is None:
        return {"app": app.localizedName() or "", "title": "", "window": None, "dialog": None, "elements": []}
    wvals = _values(window)
    wbox = _box(wvals)
    title = _text(wvals.get("AXTitle"))

    elements, dialogs, offscreen = [], [], []
    # Depth-first, keeping the chain of ancestors' dialog-ness and label.
    stack = [(window, 0, None)]
    # A native menu that is open hangs off the app, not the window; while it is
    # open it is the only thing a click can reach, like a dialog.
    for child in axkit.attr(AX.AXUIElementCreateApplication(pid), "AXChildren") or []:
        if axkit.attr(child, "AXRole") == "AXMenu":
            box = axkit.frame(child)
            if box and box[2] > 10 and box[3] > 10:
                menu = {"box": box, "title": "menu"}
                dialogs.append(menu)
                stack.append((child, 1, menu))
    seen = 0
    while stack and seen < limit:
        node, depth, dialog = stack.pop()
        seen += 1
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
        for child in reversed(list(children)):
            stack.append((child, depth + 1, dialog))
        if node is window or box is None or box[2] < 3 or box[3] < 3:
            continue
        if not _inside(box, wbox) and not (dialog and dialog.get("title") == "menu"):
            if role in INTERACTIVE and len(offscreen) < 400:
                name = (_text(v.get("AXTitle")) or _text(v.get("AXDescription"))
                        or (_child_text(children, budget=2) if role in ("AXButton", "AXLink", "AXRow", "AXCell") else ""))
                if name:
                    offscreen.append({"label": name, "role": role, "ref": node, "box": box})
            continue
        label = (_text(v.get("AXTitle")) or _text(v.get("AXDescription"))
                 or _text(v.get("AXPlaceholderValue")) or _text(v.get("AXHelp")))
        value = _text(v.get("AXValue"))
        interactive = role in INTERACTIVE
        if role == "AXStaticText":
            label = label or value
        elif role in ("AXGroup", "AXImage", "AXHeading") and not label:
            continue
        if not interactive and role not in ("AXStaticText", "AXImage", "AXHeading", "AXGroup"):
            continue
        if not interactive and not label:
            continue
        if interactive and not label and role not in TEXT_INPUT:
            label = _child_text(children)
        hint = _text(v.get("AXPlaceholderValue"))
        elements.append({
            "hint": hint[:60] if hint and hint != label else "",
            "role": role, "subrole": subrole, "kind": ROLE_WORDS.get(role, role.removeprefix("AX").lower()),
            "label": label[:90], "value": value[:80] if role in TEXT_INPUT else "",
            "box": box, "enabled": v.get("AXEnabled") is not False, "focused": bool(v.get("AXFocused")),
            "interactive": interactive, "dialog": dialog, "ref": node, "depth": depth,
        })

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
    for i, e in enumerate(unique, 1):
        e["id"] = i
    return {"app": app.localizedName() or "", "pid": pid, "title": title, "window": wbox,
            "dialog": active_dialog["box"] if active_dialog else None,
            "dialog_title": active_dialog["title"] if active_dialog else "",
            "dialog_soft": bool(active_dialog and active_dialog.get("soft")),
            "elements": unique, "walked": seen, "offscreen": offscreen}


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


def describe(e: dict, area=None) -> str:
    parts = [f"[{e['id']}] {e['kind']}"]
    if e["label"]:
        parts.append(f"'{e['label']}'")
    if e.get("hint"):
        parts.append(f"(shows '{e['hint']}')")
    if e["value"]:
        parts.append(f"= '{e['value']}'")
    extra = [_where(e["box"], area)]
    if e["in_dialog"]:
        extra.append("in the open dialog")
    if not e["enabled"]:
        extra.append("disabled")
    if e["focused"]:
        extra.append("focused")
    return " ".join(parts) + f" ({', '.join(x for x in extra if x)})"


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
    core = _core(target)
    place_words = [w for w in _words(target) if w in _PLACES]
    wanted_roles = set()
    for word in _words(target):
        wanted_roles |= _KIND_HINTS.get(word, set())
    if not core:
        return None, "no label words"

    def score(e):
        label = " ".join(_words(e["label"]))
        if not label:
            return 0.0
        if label == core:
            s = 1.0
        elif core in label.split(" ") or label.startswith(core + " ") or f" {core} " in f" {label} ":
            s = 0.75 if len(core) > 3 else 0.4
        elif label in core and len(label) > 3:
            s = 0.55
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


def choose_by_jev(target: str, pool: list[dict], inv: dict) -> tuple[dict | None, str]:
    from mint.core import jev
    labelled = [e for e in pool if e["label"] or e["role"] in TEXT_INPUT]
    if not labelled:
        return None, "nothing labelled"
    if len(labelled) > 40:
        # Jev slows down and times out on long lists; keep the plausible ones.
        wanted = {w[:4] for w in _words(target) if w not in _FILLER and len(w) > 2}

        def plausible(e):
            have = {w[:4] for w in _words(e["label"])}
            return (len(wanted & have), e["interactive"], e["in_dialog"])
        labelled = sorted(labelled, key=plausible, reverse=True)[:40]
    options = {str(e["id"]): describe(e, inv["window"]) for e in labelled}
    chosen, why = jev.resolve(target, options, what="on-screen control")
    if chosen is None:
        return None, f"Jev: {why}"
    pick = next(e for e in labelled if str(e["id"]) == chosen)
    place_words = [w for w in _words(target) if w in _PLACES]
    if place_words and _place_ok(pick, place_words, inv["window"]) < 0:
        return None, (f"Jev's pick '{pick['label'][:40]}' is not where the user said "
                      f"({' '.join(place_words)}); not using it")
    # A long, specific target answered by a control sharing few of its words
    # is a substitution, not a match: "Start new chat in data-labeling-portal"
    # (a sidebar button shown only on hover) came back as plain "New chat".
    confidence = re.search(r"\((\d\.\d+) confident\)", why)
    sure = confidence is not None and float(confidence.group(1)) >= 0.85
    wanted = [w for w in _words(target) if w not in _FILLER]
    if len(wanted) >= 3 and not sure:
        have = {w[:4] for w in _words(pick["label"])}
        overlap = sum(1 for w in wanted if w[:4] in have) / len(wanted)
        if overlap < 0.5:
            return None, (f"Jev's best guess '{pick['label'][:40]}' shares too little with '{target}'; "
                          "the control may not be visible (some appear only on hover)")
    return pick, f"Jev: {why}"


def screenshot(area) -> tuple:
    """(PIL image of the area, scale from points to pixels)."""
    import mss
    import PIL.Image

    grabber = getattr(mss, "MSS", None) or mss.mss
    x, y, w, h = area
    with grabber() as sct:
        shot = sct.grab({"left": int(x), "top": int(y), "width": int(w), "height": int(h)})
        image = PIL.Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return image, image.size[0] / max(w, 1)


def marked(inv: dict, pool: list[dict], area=None):
    """A screenshot with every candidate drawn as a numbered box (set-of-marks)."""
    import PIL.ImageDraw
    import PIL.ImageFont

    area = area or inv["dialog"] or inv["window"]
    image, scale = screenshot(area)
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
    if max(image.size) > max_side:
        ratio = max_side / max(image.size)
        image = image.resize((int(image.size[0] * ratio), int(image.size[1] * ratio)))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def choose_by_vision(target: str, pool: list[dict], inv: dict, model: str | None = None) -> tuple[dict | None, str]:
    """A vision model picks a numbered box; it never invents coordinates."""
    from google import genai
    from google.genai import types

    image, _ = marked(inv, pool)
    listing = "\n".join(describe(e, inv["window"]) for e in pool[:150])
    prompt = (
        "You are operating a Mac app. The screenshot shows the app with numbered boxes drawn "
        "around every control. Pick the ONE box that is the target below. It must be the control "
        "a person would click or type into to do it: a button, not a heading with the same words; "
        "the field itself, not its label. If a dialog is open, only the dialog can be used.\n"
        f"Target: {target}\n\nControls:\n{listing}\n\n"
        'Answer only JSON: {"id": <number or null>, "why": "<few words>"}')
    started = time.monotonic()
    try:
        text, used = _generate([types.Part.from_bytes(data=_png(image), mime_type="image/png"), prompt],
                               models=[model] if model else None)
    except Exception as error:
        return None, f"vision: {str(error)[:120]}"
    try:
        answer = _json(text)
    except Exception:
        return None, f"vision: unreadable answer {text[:80]}"
    pick = answer.get("id") if isinstance(answer, dict) else None
    try:
        pick = int(pick) if pick is not None else None
    except (TypeError, ValueError):
        pick = None
    chosen = next((e for e in pool if e["id"] == pick), None)
    return chosen, f"vision {used} ({time.monotonic() - started:.1f}s): {answer.get('why', '') if isinstance(answer, dict) else ''}"


def point_by_vision(target: str, area, model: str = "gemini-robotics-er-2-preview") -> tuple[tuple | None, str]:
    """For things with no accessible element at all (icons on a canvas): a
    pointing model returns a point, normalised 0-1000, on a screenshot of the area."""
    from google import genai
    from google.genai import types

    image, _ = screenshot(area)
    prompt = (f"Point to the {target}. The answer follows the format: "
              '[{"point": [y, x], "label": "<label>"}] with coordinates normalized to 0-1000.')
    started = time.monotonic()
    try:
        text, model = _generate([types.Part.from_bytes(data=_png(image), mime_type="image/png"), prompt],
                                models=[model], json_mode=False)
    except Exception as error:
        return None, f"point: {str(error)[:120]}"
    match = re.search(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", text)
    if not match:
        return None, f"point: no point in {text[:80]}"
    yn, xn = float(match.group(1)), float(match.group(2))
    x, y, w, h = area
    return (x + xn / 1000 * w, y + yn / 1000 * h), f"point {model} ({time.monotonic() - started:.1f}s)"


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
    ("in the sidebar"), it must be there."""
    if inv["dialog"] or not inv.get("offscreen"):
        return None
    wanted = set(_words(target))
    place_words = [w for w in wanted if w in _PLACES]
    best = None
    for o in inv["offscreen"]:
        name = _words(o["label"])
        if len(name) == 0 or len(" ".join(name)) < 3 or not set(name) <= wanted:
            continue
        if place_words and o.get("box") and _place_ok({"box": o["box"]}, place_words, inv["window"]) < 0:
            continue
        if best is None or len(name) > len(_words(best["label"])):
            best = o
    if best is None:
        return None
    if AX.AXUIElementPerformAction(best["ref"], "AXScrollToVisible") != 0:
        return None
    time.sleep(0.4)
    fresh = inventory()
    pool = candidates(fresh, action)
    exact = [e for e in pool if " ".join(_words(e["label"])) == " ".join(_words(best["label"]))]
    if place_words:
        exact.sort(key=lambda e: -_place_ok(e, place_words, fresh["window"]))
    if exact:
        return exact[0], fresh, f"'{best['label'][:40]}' was out of view; scrolled it into view"
    return None


def ground(target: str, action: str = "click", inv: dict | None = None,
           use_vision: bool = True) -> tuple[dict | None, dict, str]:
    """-> (element or None, inventory, how it was chosen)."""
    inv = inv or inventory()
    pool = candidates(inv, action)
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
        return None, inv, f"{why}; {why2}; {why3}"
    return None, inv, f"{why}; {why2}"


# --- 3. acting --------------------------------------------------------------------------------

def _post(kind, point, button=Quartz.kCGMouseButtonLeft, clicks=1):
    from mint.app import control
    if control.stopped():
        return
    event = Quartz.CGEventCreateMouseEvent(None, kind, point, button)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def mouse_click(x: float, y: float, button: str = "left", double: bool = False, label: str = "") -> None:
    from mint.ui.effects import fx

    time.sleep(fx.click(x, y, label[:40]))
    point = Quartz.CGPointMake(x, y)
    down, up, which = (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft)
    if button == "right":
        down, up, which = (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight)
    # Enter first, then press: Chromium ignores a press with no hover before it.
    _post(Quartz.kCGEventMouseMoved, point, which)
    time.sleep(0.09)
    for n in range(2 if double else 1):
        _post(down, point, which, n + 1)
        time.sleep(0.045)
        _post(up, point, which, n + 1)
        time.sleep(0.06)


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
        app = next((a for a in ws.runningApplications()
                    if (a.localizedName() or "").lower() == name.lower()), None)
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


def show_window(app, wait: float = 2.0) -> bool:
    """Make sure the app has a window on screen: un-minimise, raise, or ask
    the app to reopen one (what clicking its Dock icon does)."""
    pid = app.processIdentifier()
    if visible_window(pid):
        return True
    element = AX.AXUIElementCreateApplication(pid)
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
        b = w.get("kCGWindowBounds") or {}
        if b and b["X"] <= x <= b["X"] + b["Width"] and b["Y"] <= y <= b["Y"] + b["Height"]:
            return w.get("kCGWindowOwnerPID")
    return None


def ensure_on_top(inv: dict, x: float, y: float) -> str:
    """'' if the point belongs to the app we inventoried; else try to bring it
    forward once, and explain if that fails."""
    pid = inv.get("pid")
    if pid is None or owner_at(x, y) == pid:
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
        mouse_click(*_center(closer), label=closer["label"])
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


def act(action: str, target: str, text: str = "", press_return: bool = False,
        replace: bool = True, app=None) -> str:
    """The whole thing: find it, do it like a person, check it happened."""
    from mint.app import control
    if control.stopped():
        return "STOPPED by the user; nothing was done."
    action = (action or "click").lower().replace(" ", "_")
    if action in ("dismiss", "close_dialog", "close"):
        return dismiss()
    if action in ("fill", "enter", "input", "type_into"):
        action = "type"
    started = time.monotonic()
    own = is_own(app)
    front = app or front_app()
    if not own and front is not None and not show_window(front):
        return (f"FAILED: {front.localizedName()} has no window on screen (minimised, closed or on "
                "another Space) and it could not be shown, so nothing was done.")
    before = inventory(app)
    chosen, inv, why = ground(target, "type" if action == "type" else "click", before)
    if chosen is None:
        where = f" (a dialog '{inv['dialog_title']}' is open; only it can be used)" if inv["dialog"] else ""
        options = "; ".join(describe(e, inv["window"]) for e in candidates(inv, action)[:12])
        return (f"FAILED: could not find '{target}' in {inv['app']}{where}, so nothing was done. {why}. "
                f"Things you could mean: {options}")
    x, y = _center(chosen)
    name = f"{chosen['kind']} '{chosen['label'][:50]}'" if chosen["label"] else chosen["kind"]
    # Mint's own panels float above ordinary windows, so the covering check
    # (which looks at ordinary windows) does not apply to them.
    covered = "" if own else ensure_on_top(inv, x, y)
    if covered:
        return covered

    if action == "type":
        wipe = _would_wipe(chosen, text) if replace else ""
        if wipe:
            return wipe
        mouse_click(x, y, label=chosen["label"] or "field")
        time.sleep(0.15)
        if not axkit.attr(chosen["ref"], "AXFocused"):
            axkit.focus(chosen["ref"])
            time.sleep(0.1)
        from mint.tools import fastinput
        from mint.tools import everyday as skills
        if replace and chosen["value"]:
            fastinput.press_key("a", ["command"])
        with skills._Clipboard() as clip:
            clip.board.clearContents()
            clip.board.setString_forType_(text, AppKit.NSPasteboardTypeString)
            fastinput.press_key("v", ["command"])
            time.sleep(0.15)
        value = _text(axkit.attr(chosen["ref"], "AXValue"))
        typed_ok = text.strip()[:30].lower() in value.lower() if value else None
        sent_note, how = "", ""
        if press_return:
            time.sleep(0.3)             # let the editor take the paste before Return
            # A chat box with a Send button: click it, as a person would. In
            # ChatGPT (24 Sep) Return emptied the box and the message vanished,
            # unsent, three times in a row, while clicking Send worked at once.
            send = _send_button(inventory(app), x, y)
            if send is not None:
                mouse_click(*_center(send), label="Send")
                how = " and clicked Send"
            else:
                fastinput.press_key("return")
                how = " and pressed Return"
            time.sleep(0.7)
            remaining = _text(axkit.attr(chosen["ref"], "AXValue"))
            if typed_ok and text.strip()[:30].lower() in remaining.lower():
                # Still there: it was not sent. Do what a person does.
                send = send or _send_button(inventory(app), x, y)
                if send is not None and how != " and clicked Send":
                    mouse_click(*_center(send), label="Send")
                    sent_note = " Return did not send it, so it clicked Send."
                else:
                    sent_note = " FAILED to send: the text is still in the field."
            elif typed_ok and send is not None:
                sent_note = _sent_check(app, text)
        time.sleep(0.6)
        after = inventory(app)
        changed = _changes(before, after)
        verdict = ("and it now contains it" if typed_ok else
                   "but the field does not show it - check before going on" if typed_ok is False else
                   "(the field does not report its text; look if it matters)")
        prefix = "" if typed_ok is not False else "FAILED: "
        return (f"{prefix}Typed '{text[:60]}' into the {name} ({why}) {verdict}"
                + how + sent_note + (f". {changed}" if changed else "") + ".")

    mouse_click(x, y, button="right" if action == "right_click" else "left",
                double=action == "double_click", label=chosen["label"])
    # Watch for the effect for a while; apps take their time (a dialog that
    # greys out, then closes a second later).
    changed, deadline = "", time.monotonic() + 2.5
    while not changed and time.monotonic() < deadline:
        time.sleep(0.35)
        after = inventory(app)
        changed = _changes(before, after)
    if changed and after["dialog"] and after["dialog"] == before["dialog"]:
        time.sleep(0.8)          # the dialog may still be closing
        later = inventory(app)
        changed = _changes(before, later) or changed
    took = time.monotonic() - started
    if not changed and chosen["role"] in TEXT_INPUT and axkit.attr(chosen["ref"], "AXFocused"):
        return f"Clicked into the {name} ({why}); it has the keyboard focus, ready to type. ({took:.1f}s)"
    if not changed:
        confident = any(k in why for k in ("label match", "exact", "only text field")) or \
            bool(re.search(r"\((0\.(8[5-9]|9\d)|1\.0+) confident\)", why))
        if confident:
            # Right control, no visible effect: usually it was already in that
            # state (already on a new chat) or the click only moved focus. Not
            # a failure - in testing, calling it FAILED made the model give up
            # on ui_act and guess coordinates instead.
            return (f"Clicked the {name} ({why}); nothing visibly changed - it may already be in "
                    f"that state, or the click only moved focus. Check with ui_elements if it matters. ({took:.1f}s)")
        return (f"UNCERTAIN: clicked the {name} ({why}) but nothing on screen changed - it may be the "
                f"wrong control. Call ui_elements to see what is there and name the control exactly; "
                f"do not switch to click_at for a listed control. ({took:.1f}s)")
    return f"Clicked the {name} ({why}): {changed}. ({took:.1f}s)"


def summary(limit: int = 60) -> str:
    """What is on screen now, as the model should see it: for ui_elements."""
    inv = inventory()
    pool = candidates(inv, "click")
    head = f"{inv['app']} - '{inv['title'][:60]}'"
    if inv["dialog"]:
        head += f"; a dialog is open: '{inv['dialog_title']}' (only it can be used)"
    lines = [describe(e, inv["window"]) for e in pool[:limit]]
    more = f"\n... and {len(pool) - limit} more" if len(pool) > limit else ""
    return head + "\n" + "\n".join(lines) + more
