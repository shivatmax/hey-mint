"""What a UI action really did: one honest verdict for every click, type and scroll.

(Not to be confused with effects.py, the sparks and glows drawn on screen.)

Borrowed from Cua Driver's action-result contract (MIT; docs/action-result-contract.md). The model
used to get prose like "Clicked X" whether or not anything happened, and then guessed. Now every
ui_act, click_text, click_at, type_text and scroll_to result starts with one of five words:

  CONFIRMED        the target itself read back the change (a field's value, a toggle's state, the
                   keyboard focus) or the window visibly changed - evidence, not the API saying "ok"
  PARTIAL          some of it landed (typing: n of m characters) - don't repeat the whole thing
  UNVERIFIED       it was delivered, but nothing could prove the effect - check before relying on it
  SUSPECTED NO-OP  delivered, and nothing changed - probably the wrong control or a dead click
  FAILED           refused before anything was sent (not found, covered, password field)

plus how it was delivered - `accessibility` (AXPress / AXSelectedText: no cursor, works on a window
in the background), `synthetic_events` (key events posted to the app's process), `global_input`
(the real pointer and keyboard: the app must be in front), `dom` (a web page's own script) - and,
when it is not CONFIRMED, the next thing to try.

Two rules from Cua's driver that matter in practice:
  * Only a read-back from the exact target confirms. "AXUIElementSetAttributeValue returned 0" does
    not: Chromium echoes an AXValue write in its accessibility tree while the page never sees it.
    So under an AXWebArea an accessibility write is never attempted (and never trusted).
  * Typing reports complete / partial(n) / unchanged, so a half-landed text is not typed twice.

Also here: the window-change detector (Cua's window_change_detector.rs) - after an action, which
new windows, sheets or dialogs appeared and whether another app came to the front - and the
accessibility deliveries shared by ground.act, ocr.click_text and vision.click_at.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("mint.screen.effect")

CONFIRMED, PARTIAL, UNVERIFIABLE, NOOP, REFUSED = "confirmed", "partial", "unverifiable", "suspected_noop", "refused"
KINDS = (CONFIRMED, PARTIAL, UNVERIFIABLE, NOOP, REFUSED)
ROUTES = ("accessibility", "synthetic_events", "global_input", "dom")
DELIVERIES = ("background", "foreground")

_HEADS = {CONFIRMED: "CONFIRMED", PARTIAL: "PARTIAL", UNVERIFIABLE: "UNVERIFIED", NOOP: "SUSPECTED NO-OP",
          REFUSED: "FAILED"}
_ROUTE_WORDS = {"accessibility": "accessibility", "synthetic_events": "key events sent to the app",
                "global_input": "the real mouse/keyboard", "dom": "the page's script"}
# Results that already say they failed, written by older code paths.
_FAIL_WORDS = ("FAILED", "REFUSED", "STOPPED", "NOT ", "Cannot", "Could not", "Nothing")


@dataclass
class Effect:
    """One action's verdict. str(effect) is the line the model reads."""
    kind: str
    summary: str
    route: str = ""
    delivery: str = ""
    evidence: list[str] = field(default_factory=list)
    escalation: str = ""          # the next step to try, when not CONFIRMED
    delivered: int | None = None  # characters that landed, for PARTIAL typing

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown effect {self.kind!r}")
        if self.route and self.route not in ROUTES:
            raise ValueError(f"unknown route {self.route!r}")
        if self.delivery and self.delivery not in DELIVERIES:
            raise ValueError(f"unknown delivery {self.delivery!r}")
        self.evidence = [e for e in self.evidence if e]
        if self.kind == CONFIRMED and not self.evidence:
            # The contract: confirmed needs a read-back or a window change behind it.
            self.kind = UNVERIFIABLE
        if self.kind == REFUSED:
            self.route, self.delivery, self.evidence = "", "", []

    @property
    def ok(self) -> bool:
        """Delivered and not shown to be a dud (the model may go on)."""
        return self.kind in (CONFIRMED, PARTIAL, UNVERIFIABLE)

    def render(self) -> str:
        summary = self.summary.strip().rstrip(".")
        if self.kind == REFUSED:
            line = summary if summary.startswith(_FAIL_WORDS) else f"FAILED: {summary}"
            return line + "." + (f" Try: {self.escalation.rstrip('.')}." if self.escalation else "")
        how = _ROUTE_WORDS.get(self.route, "")
        head = _HEADS[self.kind] + (f" via {how}" if how else "") + (f" ({self.delivery})" if self.delivery else "")
        line = f"{head}: {summary}"
        if self.evidence:
            line += ". Evidence: " + "; ".join(e.strip().rstrip(".") for e in self.evidence)
        line += "."
        if self.escalation and self.kind != CONFIRMED:
            line += f" Try: {self.escalation.rstrip('.')}."
        return line

    __str__ = render

    def to_dict(self) -> dict:
        out = {"effect": self.kind, "route": self.route, "delivery": self.delivery,
               "evidence": list(self.evidence), "summary": self.summary}
        if self.escalation:
            out["escalation"] = self.escalation
        if self.delivered is not None:
            out["delivered"] = self.delivered
        return out


def refused(summary: str, escalation: str = "") -> str:
    return Effect(REFUSED, summary, escalation=escalation).render()


def is_noop(result: str) -> bool:
    return (result or "").startswith(_HEADS[NOOP])


# --- typing progress -----------------------------------------------------------------------------

def typed_progress(before: str | None, after: str | None, text: str) -> tuple[str, int]:
    """How much of `text`, inserted at the caret, landed: ("complete"|"partial"|"unchanged"|
    "unverifiable", characters). From Cua's typed_progress, plus: text that was already there before
    does not count as landed (typing "hi" into a field that says "hi" proves nothing)."""
    if not text:
        return "complete", 0
    if after is None:
        return "unverifiable", 0
    if (before is None and text in after) or (before is not None and after.count(text) > before.count(text)):
        return "complete", len(text)
    # Return and Tab submit, move focus or get eaten: the read-back says nothing about the rest.
    if any(c in text for c in "\n\r\t") or before is None:
        return "unverifiable", 0
    delivered = min(max(0, len(after) - len(before)), len(text))
    if after == before or delivered == 0:
        return "unchanged", 0
    return "partial", delivered


def replaced_progress(before: str | None, after: str | None, text: str) -> tuple[str, int]:
    """The same for replacing a field's whole value with `text`."""
    if after is None:
        return "unverifiable", 0
    if " ".join(after.split()) == " ".join(text.split()) or (text and text in after and after != before):
        return "complete", len(text)
    if after == (before or ""):
        return "unchanged", 0
    return "unverifiable", 0


# --- accessibility: the calls, swappable for a fake in tests ----------------------------------------

class _Live:
    """The real Accessibility calls. Tests hand the functions below a fake with the same methods."""

    def attr(self, element, name):
        from mint.screen import axkit
        return axkit.attr(element, name)

    def set(self, element, name, value) -> int:
        import ApplicationServices as AX
        try:
            return int(AX.AXUIElementSetAttributeValue(element, name, value))
        except Exception:
            return -1

    def perform(self, element, action: str) -> int:
        import ApplicationServices as AX
        try:
            return int(AX.AXUIElementPerformAction(element, action))
        except Exception:
            return -1

    def actions(self, element) -> list[str]:
        import ApplicationServices as AX
        try:
            err, names = AX.AXUIElementCopyActionNames(element, None)
        except Exception:
            return []
        return [str(n) for n in names or []] if err == 0 else []

    def pid(self, element) -> int | None:
        import ApplicationServices as AX
        try:
            err, pid = AX.AXUIElementGetPid(element, None)
        except Exception:
            return None
        return int(pid) if err == 0 else None

    def element_at(self, x: float, y: float):
        from mint.screen import axkit
        return axkit.element_at(x, y)          # never asks Mint's own windows (see axkit)

    def frame(self, element):
        from mint.screen import axkit
        return axkit.frame(element)

    def text_range(self, location: int, length: int):
        import ApplicationServices as AX
        return AX.AXValueCreate(AX.kAXValueCFRangeType, (location, length))

    def chromium(self, pid: int | None) -> bool:
        import AppKit

        from mint.screen import axkit
        if not pid:
            return False
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return app is not None and (axkit.is_chromium_app(app) or app.bundleIdentifier() in axkit._BROWSERS)

    def app(self, pid: int):
        import ApplicationServices as AX
        return AX.AXUIElementCreateApplication(pid)

    def perform_menu(self, element, action: str) -> int:
        """The press that opens a menu: the app runs its menu loop inside this call, so it is cut short
        (MENU_PRESS_TIMEOUT) instead of blocking until the menu closes."""
        import ApplicationServices as AX
        try:
            AX.AXUIElementSetMessagingTimeout(element, MENU_PRESS_TIMEOUT)
            return int(AX.AXUIElementPerformAction(element, action))
        except Exception:
            return -1
        finally:
            try:
                AX.AXUIElementSetMessagingTimeout(element, 0.0)
            except Exception:
                pass

    def escape(self, pid: int) -> None:
        from mint.tools import fastinput
        fastinput.press_key("escape", pid=pid)       # to that app only, whatever is in front


LIVE = _Live()


def in_web_area(element, ax=None, depth: int = 40) -> bool:
    """Is `element` web content (it or an ancestor is an AXWebArea)? A browser's own toolbar is not."""
    ax = ax or LIVE
    node = element
    for _ in range(depth):
        if node is None:
            return False
        if ax.attr(node, "AXRole") == "AXWebArea":
            return True
        node = ax.attr(node, "AXParent")
    return False


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2         # accessibility ranges count UTF-16 units


def ax_insert(element, text: str, at: str = "caret", ax=None, settle: float = 0.08) -> tuple[str, int, str]:
    """Type `text` into a native text field through Accessibility - no keys, no clipboard, no cursor,
    and the app may stay in the background. `at`: "caret" (where the cursor is, like typing), "end"
    (after what is there) or "all" (replace the whole value).

    -> (progress, characters, note). progress: complete | partial | unchanged | unverifiable, or
    "skipped" (web content / password: not tried) or "rejected" (the app refused the write)."""
    ax = ax or LIVE
    if ax.attr(element, "AXSubrole") == "AXSecureTextField":
        return "skipped", 0, "a password field"
    if in_web_area(element, ax):
        # Chromium echoes an accessibility write in its tree and the page never gets it.
        return "skipped", 0, "web content (an accessibility write there is only echoed)"
    raw = ax.attr(element, "AXValue")
    before = raw if isinstance(raw, str) else None
    if ax.attr(element, "AXFocused") is not True:
        ax.set(element, "AXFocused", True)
        time.sleep(settle)
    if at in ("end", "all") and before is not None:
        # Focusing a field selects all of it: say where the text goes.
        whole = _utf16(before)
        ax.set(element, "AXSelectedTextRange", ax.text_range(0, whole) if at == "all" else ax.text_range(whole, 0))
    err = ax.set(element, "AXSelectedText", text)
    if err != 0 and at == "all":
        err = ax.set(element, "AXValue", text)
    if err != 0:
        return "rejected", 0, f"the app refused the accessibility write (error {err})"
    time.sleep(settle)
    raw = ax.attr(element, "AXValue")
    after = raw if isinstance(raw, str) else None
    # Replacing an empty field is just typing into it: then a shortfall is an exact partial count.
    progress, n = (replaced_progress if at == "all" and before else typed_progress)(before, after, text)
    return progress, n, ""


# Controls whose accessibility press is what a click does.
PRESSABLE = {"AXButton", "AXCheckBox", "AXRadioButton", "AXMenuItem", "AXMenuBarItem", "AXMenuButton",
             "AXPopUpButton", "AXDisclosureTriangle", "AXLink", "AXTab", "AXSwitch", "AXDockItem", "AXColorWell",
             "AXIncrementor", "AXImage"}
SELECTABLE = {"AXRow", "AXCell", "AXOutlineRow"}
TEXT_INPUT = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
# Opening these shows a menu that closes at once if the app is in the background.
NEEDS_FRONT = {"AXPopUpButton", "AXMenuButton", "AXComboBox"}
# A press on these opens a menu: Mint reads its items and picks one through Accessibility instead
# (pick_from_menu), so the app can stay behind the user's window.
MENU_OPENERS = {"AXPopUpButton", "AXMenuButton"}

# Accessibility errors that prove the action never ran (the element does not do it, the call was
# malformed, Accessibility is off): the pointer may try at once. Any other error - kAXErrorFailure
# (-25200) when the app's handler threw, kAXErrorCannotComplete (-25204) when it was busy, an element
# that vanished (-25202) - can come AFTER the app acted (a button whose handler acts, then raises; a
# press that opened a modal dialog), so pressing again would do it twice.
NOT_DELIVERED = {-1, -25201, -25205, -25206, -25207, -25208, -25211}


def error_code(problem: str) -> int | None:
    """The AXError in an ax_deliver problem ("AXPress returned error -25200"), or None."""
    found = re.search(r"returned error (-?\d+)", problem or "")
    return int(found.group(1)) if found else None


def maybe_acted(problem: str) -> bool:
    """Did a press that returned an error perhaps act anyway? Then it must not be repeated
    automatically - only a read-back or a visible change can say whether it took."""
    code = error_code(problem)
    return code is not None and code not in NOT_DELIVERED


def ax_plan(role: str, advertised: list[str], action: str = "click", web: bool = False,
            chromium: bool = False, hidden: bool = False, quiet_before: bool = False) -> str:
    """The accessibility action that does this click, or '' to use the real pointer.

    '' for: double clicks (no accessibility equivalent), Chromium/Electron web content (it ignores
    AXPress - ChatGPT's app showed "no visible effect" three times), the 1x1 hidden inputs of web
    editors, and an element whose accessibility press already showed nothing last time.
    "focus" (a text field), "select" (a list row) or an AX action name otherwise."""
    if action == "double_click" or hidden or quiet_before:
        return ""
    if chromium and (web or role not in ("AXMenuItem", "AXMenuBarItem")):
        return ""                  # only native menus are safe to press in a Chromium app
    if action == "right_click":
        return "AXShowMenu" if "AXShowMenu" in advertised else ""
    if role in TEXT_INPUT:
        return "focus"
    if role in SELECTABLE and "AXPress" not in advertised:
        return "select"
    if "AXPress" in advertised and (role in PRESSABLE or role in SELECTABLE or not web):
        return "AXPress"
    for alt in ("AXConfirm", "AXPick"):
        if alt in advertised:
            return alt
    return ""


def ax_deliver(element, plan: str, ax=None, settle: float = 0.15) -> tuple[bool, list[str], str]:
    """Do `plan` (from ax_plan) on the element and read it back.

    -> (delivered, evidence, problem). delivered=False means fall back to the pointer: the app refused
    the call (an AXError), or nothing showed that it took. Evidence is only what the element itself
    reads back (its value, selection or focus); the caller adds window changes."""
    ax = ax or LIVE
    role = ax.attr(element, "AXRole") or ""
    if plan == "focus":
        if ax.set(element, "AXFocused", True) != 0 and ax.attr(element, "AXFocused") is not True:
            return False, [], "the field refused the accessibility focus"
        time.sleep(settle)
        if ax.attr(element, "AXFocused") is True:
            return True, ["it has the keyboard focus (read back)"], ""
        return False, [], "the focus did not stick"
    if plan == "select":
        node = element
        for _ in range(4):           # a cell's row, or the row itself
            if node is None:
                break
            if ax.attr(node, "AXSelected") is not None:
                if ax.attr(node, "AXSelected") is True:
                    return True, ["it is selected (read back; it already was)"], ""
                ax.set(node, "AXSelected", True)
                time.sleep(settle)
                if ax.attr(node, "AXSelected") is True:
                    return True, ["it is now selected (read back)"], ""
                return False, [], "the row refused the selection"
            node = ax.attr(node, "AXParent")
        return False, [], "nothing selectable there"
    toggle = role in ("AXCheckBox", "AXRadioButton", "AXSwitch")
    value_before = ax.attr(element, "AXValue") if toggle else None
    err, note = _perform_guarded(ax, element, plan)
    time.sleep(settle)
    if toggle:
        now = ax.attr(element, "AXValue")
        if now is not None and now != value_before and (role != "AXRadioButton" or now in (1, True, "1")):
            return True, [f"its value is now {now} (was {value_before}, read back)"], ""
    if err != 0:
        # Some apps act and still return an error (Finder's view switcher, in Cua's notes) - only a
        # read-back says so, and that was checked above.
        return False, [], f"{plan} returned error {err}"
    return True, [note.strip(" ()")] if note else [], ""


def _perform_guarded(ax, element, plan: str) -> tuple[int, str]:
    """Press in the background without letting the app push itself in front of the user's window: a
    focus_guard lease when the target is not the front app (live only; the fake AX in tests has no pid)."""
    pid = ax.pid(element) if ax is LIVE else None
    if plan == "AXShowMenu" or (plan == "AXPress" and ax.attr(element, "AXRole") in MENU_OPENERS):
        # A menu that opens: putting the user's app back in front would close it again at once. And the
        # app answers only when its menu closes, so the call is cut short (the menu stays open).
        return getattr(ax, "perform_menu", ax.perform)(element, plan), ""
    front = None
    if pid:
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            front = int(app.processIdentifier()) if app is not None else None
        except Exception:
            front = None
    if not pid or pid == front:
        return ax.perform(element, plan), ""
    try:
        from mint.screen import focus_guard
        guarding = focus_guard.lease(pid, only_target=True, seconds=2.0)
        held = guarding.__enter__()
    except Exception:
        log.debug("could not open a focus guard lease", exc_info=True)
        return ax.perform(element, plan), ""
    try:
        err = ax.perform(element, plan)          # exactly once, guard or no guard
    finally:
        try:
            guarding.__exit__(None, None, None)
        except Exception:
            log.debug("closing the focus guard lease failed", exc_info=True)
    return err, held.note


def press_target_at(x: float, y: float, pid: int | None, button: str = "left", ax=None):
    """The control under a screen point that an accessibility press can click instead of the pointer,
    or None. It must belong to `pid` (the app whose window is on top there), sit in that app's window
    at the point, and advertise the press - a label inside a button resolves to the button."""
    ax = ax or LIVE
    if not pid:
        return None
    element = ax.element_at(x, y)
    if element is None or ax.pid(element) != pid or ax.chromium(pid):
        return None
    wanted = "AXShowMenu" if button == "right" else "AXPress"
    node = element
    for _ in range(4):
        if node is None:
            return None
        role = ax.attr(node, "AXRole") or ""
        if role in ("AXWindow", "AXApplication", "AXWebArea", "AXScrollArea", "AXSplitGroup"):
            return None
        if (role in PRESSABLE or role in SELECTABLE) and wanted in ax.actions(node):
            box = ax.frame(node)
            if box is not None and not (box[0] - 2 <= x <= box[0] + box[2] + 2 and box[1] - 2 <= y <= box[1] + box[3] + 2):
                return None
            window = ax.attr(node, "AXWindow")
            wbox = ax.frame(window) if window is not None else None
            if wbox is not None and not (wbox[0] <= x <= wbox[0] + wbox[2] and wbox[1] <= y <= wbox[1] + wbox[3]):
                return None
            return node
        node = ax.attr(node, "AXParent")
    return None


# --- menus: choose an item without leaving a menu open -----------------------------------------------
#
# A pop-up button's menu (Size: Small / Medium / Large) closes the moment its app is not in front, so
# "click Size, then click Large" used to need the app in front for both steps - and when the user's app
# was put back between them, the menu closed and "Large" was gone. After Cua Driver's set_value (MIT):
# open the menu with its accessibility press, read the items as soon as they are listed, press the one
# wanted (or close it again), all within a second, and read the button's value back.

MENU_OPEN_WAIT = 2.5       # for an opened menu to list its items
MENU_POLL = 0.05
MENU_STABLE = 2            # unchanged polls that mean the list is complete
MENU_PRESS_TIMEOUT = 0.5   # accessibility messaging timeout for the press that opens a menu
MENU_CLOSE_WAIT = 0.3


def option_key(text) -> str:
    """A menu item's words for matching: case, '…' and punctuation don't matter ("Save as…" = "save as")."""
    t = str(text or "").replace("…", " ").replace("...", " ").lower()
    return " ".join(re.sub(r"[^\w&+'-]+", " ", t).split())


def _menus_open(opener, pid, ax) -> list:
    """The open menus of `opener`: its own AXMenu child once it lists items (a pop-up button), else a menu
    the app shows by itself (a context menu: a child of the application, not of the menu bar)."""
    menus = [c for c in ax.attr(opener, "AXChildren") or []
             if ax.attr(c, "AXRole") == "AXMenu" and ax.attr(c, "AXChildren")]
    if not menus and pid:
        app = getattr(ax, "app", lambda pid: None)(pid)
        menus = [c for c in (ax.attr(app, "AXChildren") or [] if app is not None else [])
                 if ax.attr(c, "AXRole") == "AXMenu" and ax.attr(c, "AXChildren")]
    return menus


def menu_items(opener, pid, ax=None) -> list[tuple]:
    """[(element, title, enabled)] of the menu open for `opener` now (read only; [] when it is closed)."""
    ax = ax or LIVE
    out = []
    for menu in _menus_open(opener, pid, ax):
        for item in ax.attr(menu, "AXChildren") or []:
            if ax.attr(item, "AXRole") != "AXMenuItem":
                continue
            title = str(ax.attr(item, "AXTitle") or "").strip()
            if title:                                    # separators have none
                out.append((item, title, ax.attr(item, "AXEnabled") is not False))
    return out


def _open_and_read(opener, action: str, pid, ax, wanted: str = "", wait: float = MENU_OPEN_WAIT):
    """Open the menu (unless it already is) and wait for its items. -> (items, opened_here, thread).
    The opening press runs on its own thread: the app answers it only when its menu loop lets it."""
    items = menu_items(opener, pid, ax)
    if items:
        return items, False, None
    press = getattr(ax, "perform_menu", ax.perform)
    thread = threading.Thread(target=press, args=(opener, action), name="mint-open-menu", daemon=True)
    thread.start()
    thread.join(MENU_POLL)               # a press that returns at once (most) is done before the first read
    deadline = time.monotonic() + wait
    last, stable = None, 0
    while True:
        items = menu_items(opener, pid, ax)
        titles = [t for _, t, _ in items]
        if wanted and any(option_key(t) == option_key(wanted) for t in titles):
            break
        if items:
            stable = stable + 1 if titles == last else 0
            if stable >= MENU_STABLE:
                break
        last = titles
        if time.monotonic() >= deadline:
            break
        time.sleep(MENU_POLL)
    return items, True, thread


def close_menu(opener, pid, ax=None) -> bool:
    """Close the menu without choosing: AXCancel, then Escape to that app only. True when it closed."""
    ax = ax or LIVE
    for menu in _menus_open(opener, pid, ax):
        ax.perform(menu, "AXCancel")
    deadline = time.monotonic() + MENU_CLOSE_WAIT
    while _menus_open(opener, pid, ax):
        if time.monotonic() >= deadline:
            escape = getattr(ax, "escape", None)
            if escape is not None and pid:
                try:
                    escape(pid)
                except Exception:
                    log.debug("escape to close a menu", exc_info=True)
                time.sleep(MENU_CLOSE_WAIT)
            return not _menus_open(opener, pid, ax)
        time.sleep(MENU_POLL)
    return True


def _selected(opener, items, ax) -> str:
    value = ax.attr(opener, "AXValue")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return next((t for el, t, _ in items if ax.attr(el, "AXMenuItemMarkChar")), "")


@dataclass
class MenuRead:
    titles: list[str]
    selected: str = ""
    problem: str = ""


def list_menu(opener, action: str, pid, ax=None) -> MenuRead:
    """The items of the menu `opener` opens (AXPress for a pop-up or menu button, AXShowMenu for a
    context menu), read while it is open for a moment and closed again - nothing chosen."""
    ax = ax or LIVE
    items, opened, thread = _open_and_read(opener, action, pid, ax)
    if opened:
        close_menu(opener, pid, ax)
    if thread is not None:
        thread.join(1.0)
    if not items:
        return MenuRead([], problem="its menu listed no items through Accessibility")
    return MenuRead([t for _, t, _ in items], _selected(opener, items, ax))


@dataclass
class MenuPick:
    ok: bool
    picked: str = ""
    evidence: list[str] = field(default_factory=list)
    problem: str = ""
    titles: list[str] = field(default_factory=list)


def pick_from_menu(opener, action: str, wanted: str, pid, ax=None, settle: float = 0.15) -> MenuPick:
    """Choose `wanted` in the menu `opener` opens: open it, press the item, make sure it closed, and
    read the button's value back (a pop-up button shows the choice). Nothing is pressed when the item is
    missing or greyed out."""
    ax = ax or LIVE
    value_before = ax.attr(opener, "AXValue")
    items, opened, thread = _open_and_read(opener, action, pid, ax, wanted=wanted)
    titles = [t for _, t, _ in items]
    match = next(((el, t, on) for el, t, on in items if option_key(t) == option_key(wanted)), None)
    if match is None or not match[2]:
        if opened:
            close_menu(opener, pid, ax)
        if thread is not None:
            thread.join(1.0)
        problem = (f"'{match[1]}' is greyed out in that menu" if match is not None else
                   f"the menu has no '{wanted}'" if items else "its menu listed no items through Accessibility")
        return MenuPick(False, problem=problem, titles=titles)
    item, title, _ = match
    err = ax.perform(item, "AXPress")
    time.sleep(settle)
    if menu_items(opener, pid, ax):
        close_menu(opener, pid, ax)                 # a refused press leaves it open
    if thread is not None:
        thread.join(1.0)
    now = ax.attr(opener, "AXValue")
    evidence = []
    if isinstance(now, str) and option_key(now) == option_key(title):
        evidence.append(f"it now reads '{now}' (read back" + ("; it already did)" if now == value_before else ")"))
    if err != 0 and not evidence:
        return MenuPick(False, title, problem=f"pressing '{title}' returned error {err}", titles=titles)
    return MenuPick(True, title, evidence, titles=titles)


# --- window changes ---------------------------------------------------------------------------------

def window_snapshot() -> dict:
    """The on-screen windows (normal, floating and modal levels) and the front app, right now."""
    try:
        import AppKit
        import Quartz
        rows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        log.debug("window snapshot", exc_info=True)
        return {}
    windows = {}
    for w in rows:
        b = w.get("kCGWindowBounds") or {}
        if int(w.get("kCGWindowLayer", 0)) not in (0, 3, 8) or w.get("kCGWindowAlpha", 1) == 0 \
                or b.get("Width", 0) < 40 or b.get("Height", 0) < 30:
            continue
        windows[int(w.get("kCGWindowNumber", 0))] = (int(w.get("kCGWindowOwnerPID", 0)),
                                                     str(w.get("kCGWindowOwnerName") or ""),
                                                     str(w.get("kCGWindowName") or ""))
    return {"windows": windows,
            "front": (int(front.processIdentifier()), str(front.localizedName() or "")) if front is not None else None}


def window_changes(before: dict, after: dict, own_pid: int | None = None, pids=None,
                   raised: int | None = None) -> list[str]:
    """What changed between two snapshots, as evidence lines: new windows of the app acted on (sheets
    and dialogs are windows too), closed ones, another app in front.

    Only the windows of `pids` (the app acted on; by default the app that was in front) count: other
    apps' windows come and go in the window list when the stacking order or the Space changes, which is
    not the action's doing - and their titles are none of the result's business. Mint's own overlays
    don't count, nor the target coming to the front when Mint itself brought it there (`raised`)."""
    if not before or not after:
        return []
    own = os.getpid() if own_pid is None else own_pid
    scope = {int(p) for p in (pids or ()) if p}
    if not scope and before.get("front"):
        scope = {before["front"][0]}
    was, now = before.get("windows", {}), after.get("windows", {})
    notes = []

    def named(items):
        by_app: dict[str, list[str]] = {}
        for pid, app, title in items:
            if pid != own and pid in scope:
                by_app.setdefault(app or "an app", []).append(title)
        return "; ".join(app + (" (" + ", ".join(f"'{t[:50]}'" for t in titles if t) + ")" if any(titles) else "")
                         for app, titles in sorted(by_app.items()))

    opened = named(now[w] for w in sorted(now) if w not in was)
    if opened:
        notes.append(f"new window: {opened}")
    closed = named(was[w] for w in sorted(was) if w not in now)
    if closed:
        notes.append(f"window closed: {closed}")
    if before.get("front") and after.get("front") and before["front"][0] != after["front"][0] \
            and after["front"][0] not in (own, raised):
        notes.append(f"{after['front'][1] or 'another app'} is now in front (was {before['front'][1] or 'another app'})")
    return notes


# --- the user's pointer ----------------------------------------------------------------------------

def pointer_at() -> tuple[float, float] | None:
    try:
        import Quartz
        p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return float(p.x), float(p.y)
    except Exception:
        return None


def give_pointer_back(was: tuple[float, float] | None, clicked: tuple[float, float]) -> bool:
    """Put the pointer back where the user left it after a click Mint had to make with it - unless
    they have moved it since (then it is theirs). No events are posted, so nothing is hovered."""
    now = pointer_at()
    if was is None or now is None or abs(now[0] - clicked[0]) > 2 or abs(now[1] - clicked[1]) > 2:
        return False
    if abs(was[0] - clicked[0]) <= 2 and abs(was[1] - clicked[1]) <= 2:
        return False
    try:
        import Quartz
        Quartz.CGWarpMouseCursorPosition(Quartz.CGPointMake(*was))
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)   # no quarter-second freeze after the warp
        return True
    except Exception:
        return False
