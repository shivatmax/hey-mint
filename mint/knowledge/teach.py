"""Teach by showing: the user does a task once while Mint watches, and Mint writes it up as a skill.

    "watch me do this once"        -> teach start
    (the user works in any app, optionally talking about what they are doing)
    "done" / "that's how you do it" -> teach stop  -> a skill in skills/<category>/<name>.md

What is recorded, in memory only, while the recording is on:
    clicks        the control under the pointer, read from the accessibility tree (role, title,
                  description, value, menu path, container, window, app) - names, not pixels
    typing        runs of typed text per field; never anything typed into a password field
                  (AXSecureTextField, or macOS secure input on) and nothing that looks like a key
                  or goes into a field labelled password / PIN / code / token
    shortcuts     cmd+S, cmd+shift+T ... ; Return / Tab / Escape (no clipboard text that is marked
                  concealed or looks like a secret)
    password managers  nothing at all - just "used a password manager"
    scrolling     merged into one entry per burst
    app switches  and browser page changes (the address of the front tab)
    screenshots   at most 12 small ones (768 px) at clicks, to help the model read the screen

When the user says done, the log is compressed into a short numbered action list (typing merged,
flickers and duplicates dropped), and Gemini writes the skill from it - generalised, with the
things that change each time as <parameters>, and phrased in Mint's own tools (ui_act, menu,
browser, press_key ...) - which is then saved through skillbook.create. Nothing is written to
disk except that skill. The recording stops by itself after ten minutes.

Capture is a listen-only CGEventTap on its own thread and CFRunLoop: it can't change or delay
input. macOS asks for Input Monitoring for it (System Settings > Privacy & Security > Input
Monitoring); the control names need Accessibility, which Mint already has.

Ideas borrowed (MIT licensed, GPL-3.0 compatible; no code copied):
- microsoft/skill-recorder: the timeline -> intent + ordered steps -> generalised skill flow,
  dropping recorder bracketing / focus flickers / tracking parameters, narration as the lead
  signal for intent, native tools preferred over replaying clicks, fixed vs varying values.
- OpenAdaptAI/OpenAdapt (legacy events.py): merging consecutive key presses into typed runs,
  scroll bursts and repeated clicks into single actions before a model sees them.
- OpenAdaptAI/openadapt-capture: CGPreflightListenEventAccess as the permission check, and
  re-enabling a tap macOS disabled after a timeout.
"""

from __future__ import annotations

import ctypes
import io
import logging
import os
import queue
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger("mint.knowledge.teach")

MAX_SECONDS = 10 * 60          # recordings stop by themselves after this
MAX_EVENTS = 4000              # raw events kept (a runaway key repeat must not eat memory)
MAX_SHOTS = 12                 # screenshots kept for the model
SHOT_EDGE = 768
MAX_LINES = 160                # actions shown to the model
SENSITIVE_SECONDS = 30         # typing this soon after a password / code field (or a password manager) is hidden

SETTINGS_HINT = ("Open System Settings > Privacy & Security > Input Monitoring and turn Mint on "
                 "(then quit and reopen Mint).")

# Other processes whose synthetic input must not be taken for the user's (Mint's screen engine,
# say). Mint's own pid is always ignored.
IGNORE_PIDS: set[int] = set()

_lock = threading.RLock()
_rec: "_Recording | None" = None
_listeners: list = []
_last_result = ""


def on_change(callback) -> None:
    """callback(state, detail) on 'recording', 'auto_stopped', 'stopped', 'saved', 'cancelled',
    'failed' - so the main session can show and hide its recording indicator."""
    _listeners.append(callback)


def _emit(state: str, detail: str = "") -> None:
    for callback in list(_listeners):
        try:
            callback(state, detail)
        except Exception:
            log.exception("teach listener failed")


# --- keys ---------------------------------------------------------------------------------

_SPECIAL = {36: "Return", 76: "Enter", 48: "Tab", 49: "Space", 51: "Delete", 117: "ForwardDelete",
            53: "Escape", 123: "Left", 124: "Right", 125: "Down", 126: "Up", 115: "Home", 119: "End",
            116: "PageUp", 121: "PageDown", 122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5",
            97: "F6", 98: "F7", 100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12"}
_US = dict(zip([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24,
                25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 50],
               "asdfhgzxcvbqweryt123465=97-80]ou[ip" "lj'k;\\,/nm.`"))
_CMD, _SHIFT, _CTRL, _ALT = 1 << 20, 1 << 17, 1 << 18, 1 << 19
_EDIT_KEYS = {"Delete", "ForwardDelete", "Left", "Right", "Up", "Down", "Home", "End"}


def _mods(flags: int) -> list[str]:
    return [name for bit, name in ((_CTRL, "ctrl"), (_ALT, "option"), (_SHIFT, "shift"), (_CMD, "cmd"))
            if flags & bit]


def _secure_input_on() -> bool:
    """macOS 'secure event input' (a password field is focused somewhere)."""
    global _carbon
    try:
        if _carbon is None:
            _carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
            _carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool
        return bool(_carbon.IsSecureEventInputEnabled())
    except Exception:
        return False


_carbon = None
AX_QUICK = 0.05                            # seconds an app may take to answer the tap's focus check


def _focus_secure(pid: int) -> bool | None:
    """Is the focused control of app `pid` a password field? Asked from the tap thread, so every
    Accessibility message has a short timeout of its own (a busy app must not stall the tap).
    None when it can't be told."""
    if not pid:
        return None
    try:
        import ApplicationServices as AX
        app = AX.AXUIElementCreateApplication(pid)
        AX.AXUIElementSetMessagingTimeout(app, AX_QUICK)
        err, focused = AX.AXUIElementCopyAttributeValue(app, "AXFocusedUIElement", None)
        if err or focused is None:
            return None
        AX.AXUIElementSetMessagingTimeout(focused, AX_QUICK)
        for name in ("AXSubrole", "AXRole"):
            err, value = AX.AXUIElementCopyAttributeValue(focused, name, None)
            if not err and value == "AXSecureTextField":
                return True
        return False
    except Exception:
        return None


def _secret(text: str) -> bool:
    from mint.knowledge.skills import has_secret
    return has_secret(text)


_KEY_PREFIX = re.compile(
    r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{8,}|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bxox[abpr]-[A-Za-z0-9-]{6,}"
    r"|\bsk-[A-Za-z0-9_\-]{6,}|\b[sr]k_(?:live|test)_[A-Za-z0-9]{6,}|\bpk_live_[A-Za-z0-9]{6,}"
    r"|\bAIza[0-9A-Za-z_\-]{10,}|\bya29\.[A-Za-z0-9_\-]{6,}|\bglpat-[A-Za-z0-9_\-]{6,}|-----BEGIN")


def _high_entropy(token: str) -> bool:
    """A long run of mixed letters and digits with no spaces: a key, token or generated password -
    but not an address, a path, an email or hyphenated words."""
    token = token.strip("'\"()[]{}<>,;:!?")
    if len(token) < 20 or not re.search(r"[A-Za-z]", token) or not re.search(r"\d", token):
        return False
    if "://" in token or "@" in token or token.startswith(("/", "~", "www.")):
        return False
    words = [p for p in re.split(r"[-_.]", token) if p.isalpha() and len(p) >= 3]
    return not (len(words) >= 2 and sum(map(len, words)) * 2 >= len(token))


def _secret_like(text: str) -> bool:
    """The stricter check for what the user typed or copied: skillbook's patterns, well-known key
    prefixes, and anything that looks generated."""
    text = text or ""
    return _secret(text) or bool(_KEY_PREFIX.search(text)) or any(_high_entropy(t) for t in text.split())


_SENSITIVE_LABEL = re.compile(r"pass(?:word|code|phrase)|\bpins?\b|\botp\b|\b2fa\b|\bmfa\b|two[- ]factor|one[- ]time"
                              r"|verification code|security code|secret|token|\bcvv\b|\bcvc\b", re.I)


def _sensitive_label(label: str) -> bool:
    return bool(label and _SENSITIVE_LABEL.search(label))


# Password managers: nothing done in them is recorded - no clicks, typing, screenshots or clipboard.
PASSWORD_MANAGERS = {
    "com.1password.1password", "com.agilebits.onepassword7", "com.agilebits.onepassword-osx",
    "com.agilebits.onepassword4", "com.apple.Passwords", "com.apple.keychainaccess", "com.bitwarden.desktop",
    "com.lastpass.LastPass", "com.lastpass.lastpassmacdesktop", "com.dashlane.dashlanephonefinal",
    "com.dashlane.Dashlane", "org.keepassxc.keepassxc", "in.sinew.Enpass-Desktop", "in.sinew.Enpass-Desktop.App",
    "com.markmcguill.strongbox.mac", "me.proton.pass.electron"}
_PM_PREFIXES = ("com.1password.", "com.agilebits.onepassword", "com.lastpass.", "com.dashlane.", "in.sinew.enpass",
                "com.bitwarden.")
_PM_NAMES = {"1password", "1password 7", "passwords", "keychain access", "bitwarden", "lastpass", "dashlane",
             "keepassxc", "enpass", "strongbox", "proton pass"}
PM_NOTE = "used a password manager - not recorded"


def _is_password_manager(bundle: str = "", app: str = "") -> bool:
    bundle = bundle or ""
    return (bundle in PASSWORD_MANAGERS or bundle.lower().startswith(_PM_PREFIXES)
            or (app or "").strip().lower() in _PM_NAMES)


def _pm_on_screen() -> bool:
    """A password manager has a normal window on screen (a screenshot now could show a secret)."""
    try:
        import AppKit
        import Quartz
        pids = {int(a.processIdentifier()) for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                if _is_password_manager(str(a.bundleIdentifier() or ""), str(a.localizedName() or ""))}
        if not pids:
            return False
        windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly
                                                    | Quartz.kCGWindowListExcludeDesktopElements,
                                                    Quartz.kCGNullWindowID) or []
        return any(int(w.get("kCGWindowOwnerPID", 0)) in pids and int(w.get("kCGWindowLayer", 0)) < 25
                   for w in windows)
    except Exception:
        return True                        # can't tell: no screenshot is the safe answer


# --- reading the screen -------------------------------------------------------------------

def _attr(element, name):
    from mint.screen.axkit import attr
    return attr(element, name)


def _text(value, limit: int = 60) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value[:limit] + ("…" if len(value) > limit else "")


def _is_secure(element) -> bool:
    return (_attr(element, "AXSubrole") == "AXSecureTextField"
            or _attr(element, "AXRole") == "AXSecureTextField")


def _label(element) -> str:
    parts = []
    for name in ("AXTitle", "AXDescription", "AXPlaceholderValue", "AXHelp"):
        value = _text(_attr(element, name), 70)
        if value and value.lower() not in (p.lower() for p in parts):
            parts.append(value)
    title_element = _attr(element, "AXTitleUIElement")
    if title_element is not None:
        value = _text(_attr(title_element, "AXValue") or _attr(title_element, "AXTitle"), 70)
        if value and value.lower() not in (p.lower() for p in parts):
            parts.append(value)
    return " / ".join(parts)


def _child_text(element, limit: int = 30) -> str:
    """The first words inside a row, cell or group (a list row's own title)."""
    from mint.screen.axkit import walk
    for node in walk(element, limit=limit, depth=4):
        if node is element:
            continue
        if _attr(node, "AXRole") in ("AXStaticText", "AXTextField") and not _is_secure(node):
            value = _text(_attr(node, "AXValue"), 60)
            if value:
                return value
    return ""


def _meaningful(element, hops: int = 4):
    """The element, or the nearest ancestor with a name or an action (the icon inside a button)."""
    import ApplicationServices as AX
    node = element
    for _ in range(hops):
        if node is None:
            break
        try:
            _err, actions = AX.AXUIElementCopyActionNames(node, None)
        except Exception:
            actions = None
        pressable = any(a in (actions or []) for a in ("AXPress", "AXConfirm", "AXPick", "AXOpen"))
        role = _attr(node, "AXRole")
        if pressable or (_label(node) and role not in {"AXGroup", "AXImage", "AXUnknown"}):
            return node
        node = _attr(node, "AXParent")
    return element


_CONTAINERS = {"AXSheet": "dialog", "AXToolbar": "toolbar", "AXPopover": "popover", "AXTabGroup": "tabs",
               "AXOutline": "sidebar/outline", "AXTable": "table", "AXList": "list", "AXMenu": "menu",
               "AXMenuBar": "menu bar", "AXWebArea": "web page", "AXBrowser": "column browser"}


def _context(element) -> tuple[str, bool, str]:
    """(container words, inside a web page, window title)."""
    container, web, node = "", False, _attr(element, "AXParent")
    for _ in range(30):
        if node is None:
            break
        role = _attr(node, "AXRole")
        if role == "AXWebArea":
            web = True
        if not container:
            if role in _CONTAINERS:
                container = _CONTAINERS[role]
                name = _text(_attr(node, "AXTitle") or _attr(node, "AXDescription"), 40)
                if name and role not in ("AXWebArea", "AXMenuBar"):
                    container += f" '{name}'"
            elif _attr(node, "AXSubrole") in ("AXDialog", "AXSystemDialog"):
                container = "dialog"
        if role == "AXWindow":
            break
        node = _attr(node, "AXParent")
    window = _attr(element, "AXWindow")
    title = _text(_attr(window, "AXTitle"), 80) if window is not None else ""
    return container, web, title


def _menu_path(element) -> str:
    """'File > Export > PDF…' for a menu item or menu-bar item."""
    names, node = [], element
    for _ in range(12):
        if node is None:
            break
        role = _attr(node, "AXRole")
        if role in ("AXMenuItem", "AXMenuBarItem"):
            title = _text(_attr(node, "AXTitle"), 50)
            if title:
                names.append(title)
            if role == "AXMenuBarItem":
                break
        elif role not in ("AXMenu",):
            break
        node = _attr(node, "AXParent")
    return " > ".join(reversed(names))


def describe_element(element, x: float | None = None, y: float | None = None) -> dict:
    """What the user clicked, in the words Mint's tools use to find it again."""
    node = _meaningful(element)
    role = _attr(node, "AXRole") or "?"
    subrole = _attr(node, "AXSubrole") or ""
    secure = _is_secure(node) or _is_secure(element)
    label = _label(node)
    if not label and role in ("AXRow", "AXCell", "AXGroup", "AXOutlineRow", "AXLink"):
        label = _child_text(node)
    if not label and role == "AXStaticText":
        label = _text(_attr(node, "AXValue"), 60)
    if not label and x is not None:
        try:                              # Chromium's hit test can stop at a big unnamed group
            from mint.tools.harness import _deepest_at, _label as ht_label
            deeper = _deepest_at(element, x, y)
            if deeper is not None:
                node, role = deeper, _attr(deeper, "AXRole") or role
                label = _text(ht_label(deeper), 70)
        except Exception:
            pass
    value = ""
    if not secure and role in ("AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXCheckBox",
                               "AXRadioButton", "AXPopUpButton", "AXSlider", "AXTab"):
        raw = _attr(node, "AXValue")
        value = _text(raw if isinstance(raw, str) else (str(raw) if isinstance(raw, (int, float)) else ""), 40)
        if value and (_secret_like(value) or _sensitive_label(label)):
            value = ""
    kind = str(_attr(node, "AXRoleDescription") or role.removeprefix("AX")).lower()
    container, web, window = _context(node)
    menu = _menu_path(node) if role in ("AXMenuItem", "AXMenuBarItem") else ""
    return {"role": role, "subrole": subrole, "kind": kind, "label": label, "value": value, "secure": secure,
            "container": container, "web": web, "window": window, "menu": menu,
            "ident": _text(_attr(node, "AXIdentifier"), 40)}


def _front() -> tuple[int, str, str, str]:
    """(pid, app name, bundle id, window title) of the app with keyboard focus."""
    import AppKit
    import ApplicationServices as AX
    pid = 0
    try:
        app_el = _attr(AX.AXUIElementCreateSystemWide(), "AXFocusedApplication")
        if app_el is not None:
            err, pid = AX.AXUIElementGetPid(app_el, None)
            pid = 0 if err else pid
    except Exception:
        pid = 0
    app = (AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid
           else AppKit.NSWorkspace.sharedWorkspace().frontmostApplication())
    if app is None:
        return 0, "", "", ""
    pid = app.processIdentifier()
    title = ""
    try:
        from mint.screen.axkit import focused_window
        window = focused_window(pid)
        title = _text(_attr(window, "AXTitle"), 80) if window is not None else ""
    except Exception:
        pass
    return pid, str(app.localizedName() or ""), str(app.bundleIdentifier() or ""), title


_TRACKING = re.compile(r"^(utm_|gclid$|gad_|fbclid$|mc_|ref_src$|igshid$|si$|_hs|yclid$|msclkid$)", re.I)


def clean_url(url: str) -> str:
    """No tracking parameters, and no query at all when it looks like it carries a secret."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url[:200]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    text = urlencode(query)
    if _secret(text) or any(k.lower() in ("token", "code", "key", "auth", "password", "sig", "signature",
                                          "access_token", "id_token", "session") for k, _ in query):
        text = ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, text, ""))[:200]


# --- the recording --------------------------------------------------------------------------

class _Recording:
    def __init__(self, goal: str) -> None:
        self.goal = goal.strip()
        self.started = time.time()
        self.events: list[dict] = []
        self.narration: list[tuple[float, str]] = []
        self.shots: list[dict] = []          # {"id": n, "jpeg": bytes, "t": ...}
        self.shot_stride, self.shot_counter, self.shot_seq = 1, 0, 0
        self.screens_ok = True
        self.keyboard = True
        self.accessibility = True
        self.raw: queue.Queue = queue.Queue(maxsize=5000)
        self.stopping = threading.Event()
        self.starting = True                 # between start() claiming the slot and the tap being live
        self.capturing = False
        self.auto_stopped = False
        self.dropped = 0
        self.tap = None
        self.ready = threading.Event()
        self.tap_error = ""
        self.own_pid = os.getpid()
        self.last_front: tuple = ()
        self.last_url = ""
        self.last_url_check = 0.0
        self.down: dict | None = None
        self.threads: list[threading.Thread] = []
        self.front_pid = 0                   # the app in front, for the tap's password-field check
        self.pm_used = False                 # a password manager was used: its copies must not show
        self.clip_count = -1                 # pasteboard changeCount after the user's last seen copy

    def t(self) -> float:
        return round(time.time() - self.started, 2)

    def add(self, event: dict) -> None:
        with _lock:
            if len(self.events) >= MAX_EVENTS:
                self.events, lost = make_room(self.events)
                self.dropped += lost
            event.setdefault("t", self.t())
            self.events.append(event)

    def _pm_note(self, app: str, t: float | None = None) -> None:
        """One placeholder for a stretch of password-manager use; nothing of what was done in it."""
        self.pm_used = True
        self.down = None
        with _lock:
            if self.events and self.events[-1]["type"] == "pm":
                return
            self.add({"type": "pm", "app": app, **({"t": t} if t is not None else {})})

    # -- the tap thread: grab the plain facts and hand them on, nothing slow here --

    def _callback(self, proxy, kind, event, refcon):
        import Quartz
        try:
            if kind in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                if self.tap is not None and not self.stopping.is_set():
                    Quartz.CGEventTapEnable(self.tap, True)
                return event
            source = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID)
            if source and (source == self.own_pid or source in IGNORE_PIDS):
                return event                       # Mint's own clicks and typing are not the user's
            loc = Quartz.CGEventGetLocation(event)
            item = {"kind": int(kind), "x": float(loc.x), "y": float(loc.y), "t": self.t(),
                    "flags": int(Quartz.CGEventGetFlags(event))}
            if kind == Quartz.kCGEventKeyDown:
                item["code"] = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
                item["repeat"] = bool(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat))
                try:
                    _n, chars = Quartz.CGEventKeyboardGetUnicodeString(event, 8, None, None)
                except Exception:
                    chars = ""
                item["chars"] = chars or ""
                # Is this key going into a password field? Decided now, not when the worker gets to it
                # (by then the focus may have moved on and the key would be recorded as plain text).
                item["secure_input"] = _secure_input_on()
                target = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventTargetUnixProcessID))
                item["focus_secure"] = _focus_secure(target or self.front_pid)
            elif kind == Quartz.kCGEventScrollWheel:
                item["dy"] = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis1))
                item["dx"] = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGScrollWheelEventDeltaAxis2))
            else:
                item["clicks"] = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventClickState))
            self.raw.put_nowait(item)
        except queue.Full:
            pass
        except Exception:
            log.debug("tap callback", exc_info=True)
        return event

    def _tap_thread(self) -> None:
        import Quartz
        mouse = [Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGEventRightMouseDown,
                 Quartz.kCGEventScrollWheel]
        keys = [Quartz.kCGEventKeyDown]

        def mask(kinds):
            value = 0
            for k in kinds:
                value |= Quartz.CGEventMaskBit(k)
            return value

        _secure_input_on()                        # load Carbon now, not inside the first key press
        tap = None
        for kinds, keyboard in ((mouse + keys, True), (mouse, False)):
            tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGTailAppendEventTap,
                                          Quartz.kCGEventTapOptionListenOnly, mask(kinds), self._callback, None)
            if tap is not None:
                self.keyboard = keyboard
                break
        if tap is None:
            self.tap_error = "tap"
            self.ready.set()
            return
        self.tap = tap
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopDefaultMode)
        Quartz.CGEventTapEnable(tap, True)
        self.capturing = True
        self.ready.set()
        try:
            while not self.stopping.is_set():
                Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.25, False)
        finally:
            Quartz.CGEventTapEnable(tap, False)
            Quartz.CFRunLoopRemoveSource(loop, source, Quartz.kCFRunLoopDefaultMode)
            Quartz.CFMachPortInvalidate(tap)
            self.tap = None
            self.capturing = False

    # -- the worker: names for clicks, typed runs, screenshots --

    def _worker(self) -> None:
        import Quartz
        while not (self.stopping.is_set() and self.raw.empty()):
            try:
                item = self.raw.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                kind = item["kind"]
                if kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown):
                    self._click(item, right=kind == Quartz.kCGEventRightMouseDown)
                elif kind == Quartz.kCGEventLeftMouseUp:
                    self._mouse_up(item)
                elif kind == Quartz.kCGEventKeyDown:
                    self._key(item)
                elif kind == Quartz.kCGEventScrollWheel:
                    self._scroll(item)
            except Exception:
                log.debug("teach worker", exc_info=True)

    def _app_of(self, element):
        import AppKit
        import ApplicationServices as AX
        try:
            err, pid = AX.AXUIElementGetPid(element, None)
        except Exception:
            return 0, "", ""
        if err:
            return 0, "", ""
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return pid, str(app.localizedName() or "") if app else "", str(app.bundleIdentifier() or "") if app else ""

    def _click(self, item: dict, right: bool) -> None:
        import ApplicationServices as AX
        x, y = item["x"], item["y"]
        err, element = AX.AXUIElementCopyElementAtPosition(AX.AXUIElementCreateSystemWide(), x, y, None)
        if err or element is None:
            if err == -25211:                          # kAXErrorAPIDisabled: no Accessibility
                self.accessibility = False
            pid, app, bundle, window = _front()        # still say where, and keep the screenshot
            if not pid or pid == self.own_pid or pid in IGNORE_PIDS:
                self.down = None
                return
            if _is_password_manager(bundle, app):
                self._pm_note(app, item["t"])
                return
            info = {"kind": "spot", "label": "", "window": window}
        else:
            pid, app, bundle = self._app_of(element)
            if pid == self.own_pid or pid in IGNORE_PIDS:
                self.down = None
                return
            if _is_password_manager(bundle, app):
                self._pm_note(app, item["t"])
                return
            info = describe_element(element, x, y)
        event = {"type": "click", "t": item["t"], "button": "right" if right else "left",
                 "count": max(1, item.get("clicks", 1)), "app": app, "bundle": bundle, "pid": pid,
                 "x": round(x), "y": round(y), "mods": _mods(item["flags"]), **info}
        if not right and not info.get("secure"):
            event["shot"] = self._shot(x, y)
        self.add(event)
        self.down = event if not right else None

    def _mouse_up(self, item: dict) -> None:
        down, self.down = self.down, None
        if down is None:
            return
        if abs(item["x"] - down["x"]) + abs(item["y"] - down["y"]) < 14:
            return
        import ApplicationServices as AX
        err, element = AX.AXUIElementCopyElementAtPosition(AX.AXUIElementCreateSystemWide(),
                                                          item["x"], item["y"], None)
        target = {}
        if not err and element is not None:
            _pid, app, bundle = self._app_of(element)
            target = ({"label": "a password manager"} if _is_password_manager(bundle, app)
                      else describe_element(element))
        with _lock:
            down["type"] = "drag"
            down["to"] = target.get("label") or target.get("kind") or "another place"
            down["to_container"] = target.get("container", "")

    def _key(self, item: dict) -> None:
        import ApplicationServices as AX
        code, flags, chars = item["code"], item["flags"], item.get("chars", "")
        focused = _attr(AX.AXUIElementCreateSystemWide(), "AXFocusedUIElement")
        pid, app, bundle = self._app_of(focused) if focused is not None else (0, "", "")
        if not pid:
            pid, app, bundle, _ = _front()
        if pid == self.own_pid or pid in IGNORE_PIDS:
            return
        if _is_password_manager(bundle, app):
            self._pm_note(app, item["t"])
            return
        # the password-field facts were taken in the tap as the key went by; only ask again when
        # the tap couldn't tell (older items, or the app didn't answer in time)
        secure = bool(item.get("secure_input")) or bool(item.get("focus_secure"))
        if "secure_input" not in item or item.get("focus_secure") is None:
            secure = secure or (focused is not None and _is_secure(focused))
            if "secure_input" not in item:
                secure = secure or _secure_input_on()
        mods = _mods(flags)
        field = {}
        if focused is not None and not secure:
            field = {"role": _attr(focused, "AXRole") or "", "label": _label(focused)}
            if not field["label"] and field["role"] in ("AXTextArea", "AXTextField", "AXWebArea", "AXGroup"):
                container, web, _w = _context(focused)
                field["label"] = container
        base = {"t": item["t"], "app": app, "bundle": bundle, "pid": pid,
                "field": f"{field.get('label', '')}".strip(), "field_role": field.get("role", "")}
        if secure:
            self.add({**base, "type": "type", "text": "", "secure": True})
            return
        special = _SPECIAL.get(code)
        if "cmd" in mods or "ctrl" in mods:
            key = special or _US.get(code) or (chars if chars and chars.isprintable() else f"key{code}")
            event = {**base, "type": "shortcut", "keys": "+".join(mods + [key.lower() if len(key) == 1 else key])}
            if key.lower() in ("c", "x", "v") and mods == ["cmd"]:
                if key.lower() in ("c", "x"):
                    time.sleep(0.15)                    # let the app put it on the clipboard
                clip, count = _clipboard_preview()      # clip None: withheld on purpose
                if key.lower() in ("c", "x"):
                    self.clip_count = count             # the clipboard now holds what we saw copied
                elif self.pm_used and count != self.clip_count:
                    clip = None                         # it may have come from the password manager
                if _sensitive_label(base["field"]):
                    clip = None                         # pasted into a password / code field
                event["clip"] = clip or ""
                if clip is None:
                    event["clip_hidden"] = True
            self.add(event)
            return
        if item.get("repeat") and special not in ("Delete", "ForwardDelete"):
            return
        if special and special not in ("Space",):
            self.add({**base, "type": "key", "key": special, "mods": mods})
            return
        text = " " if special == "Space" else chars
        if text and text.isprintable():
            self.add({**base, "type": "char", "text": text})

    def _scroll(self, item: dict) -> None:
        dy, dx = item.get("dy", 0), item.get("dx", 0)
        if not dy and not dx:
            return
        with _lock:
            last = self.events[-1] if self.events else None
            if last and last["type"] == "scroll" and item["t"] - last["t_end"] < 1.5:
                last["dy"] += dy
                last["dx"] += dx
                last["t_end"] = item["t"]
                return
        import ApplicationServices as AX
        err, element = AX.AXUIElementCopyElementAtPosition(AX.AXUIElementCreateSystemWide(),
                                                          item["x"], item["y"], None)
        where, app, pid = "", "", 0
        if not err and element is not None:
            pid, app, bundle = self._app_of(element)
            if pid == self.own_pid or pid in IGNORE_PIDS or _is_password_manager(bundle, app):
                return
            info = describe_element(element)
            where = info.get("container") or info.get("label") or ""
        self.add({"type": "scroll", "t": item["t"], "t_end": item["t"], "dy": dy, "dx": dx, "app": app,
                  "where": where})

    # -- screenshots: few, small, in memory --

    def _shot(self, x: float, y: float) -> int | None:
        if not self.screens_ok:
            return None
        self.shot_counter += 1
        if (self.shot_counter - 1) % self.shot_stride:
            return None
        if _pm_on_screen():
            return None
        try:
            import mss
            import PIL.Image
            import PIL.ImageDraw
            maker = getattr(mss, "MSS", None) or mss.mss
            with maker() as sct:
                monitors = sct.monitors[1:] or sct.monitors
                mon = next((m for m in monitors if m["left"] <= x < m["left"] + m["width"]
                            and m["top"] <= y < m["top"] + m["height"]), monitors[0])
                grab = sct.grab(mon)
                image = PIL.Image.frombytes("RGB", grab.size, grab.bgra, "raw", "BGRX")
            image.thumbnail((SHOT_EDGE, SHOT_EDGE))
            small = image.convert("L").resize((64, 40))
            low, high = small.getextrema()
            if high - low < 12:                 # no Screen Recording permission: a blank frame
                self.screens_ok = False
                return None
            px = (x - mon["left"]) / mon["width"] * image.width
            py = (y - mon["top"]) / mon["height"] * image.height
            draw = PIL.ImageDraw.Draw(image)
            for r, width in ((16, 4), (19, 2)):
                draw.ellipse((px - r, py - r, px + r, py + r), outline=(255, 30, 30), width=width)
            buffer = io.BytesIO()
            image.save(buffer, format="jpeg", quality=70)
        except Exception:
            log.debug("teach screenshot", exc_info=True)
            self.screens_ok = False
            return None
        with _lock:
            self.shot_seq += 1
            self.shots.append({"id": self.shot_seq, "jpeg": buffer.getvalue()})
            if len(self.shots) > MAX_SHOTS:           # keep an even spread over the whole recording
                self.shots = self.shots[::2]
                self.shot_stride *= 2
            return self.shot_seq

    # -- app switches and browser pages --

    def _watch_front(self) -> None:
        while not self.stopping.wait(0.5):
            if time.time() - self.started > MAX_SECONDS:
                self.auto_stopped = True
                self.stopping.set()
                _emit("auto_stopped", "Recording stopped after 10 minutes.")
                break
            try:
                self._check_front()
            except Exception:
                log.debug("teach front", exc_info=True)

    def _check_front(self) -> None:
        pid, app, bundle, title = _front()
        self.front_pid = pid
        if not pid or pid == self.own_pid or pid in IGNORE_PIDS:
            return
        if _is_password_manager(bundle, app):     # not even its window title (a vault or item name)
            if (pid, app) != self.last_front[:2]:
                self.last_front = (pid, app, "")
                self._pm_note(app)
            return
        if (pid, app) != self.last_front[:2]:
            self.last_front = (pid, app, title)
            self.add({"type": "app", "app": app, "bundle": bundle, "window": title})
            self.last_url_check = 0.0
        elif title and title != self.last_front[2]:
            self.last_front = (pid, app, title)
            self.add({"type": "title", "app": app, "window": title})
        try:
            from mint.tools.harness import _BROWSERS
        except Exception:
            return
        if bundle in _BROWSERS and time.time() - self.last_url_check > 1.5:
            self.last_url_check = time.time()
            import AppKit
            from mint.tools.harness import _tab_info
            running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            url, page = _tab_info(running) if running is not None else ("", "")
            url = clean_url(url) if url else ""
            if url and url != self.last_url:
                self.last_url = url
                self.add({"type": "url", "app": app, "url": url, "page": _text(page, 80)})


# Marked by password managers and generators (nspasteboard.org): never read these.
CONCEALED_TYPES = {"org.nspasteboard.ConcealedType", "org.nspasteboard.TransientType",
                   "org.nspasteboard.AutoGeneratedType"}


def _clipboard_preview() -> tuple[str | None, int]:
    """(the start of the clipboard's text, its changeCount). The text is None when it is withheld:
    marked concealed / transient / generated, or it looks like a secret; "" when there is none."""
    try:
        import AppKit
        board = AppKit.NSPasteboard.generalPasteboard()
        count = int(board.changeCount())
        if {str(t) for t in (board.types() or [])} & CONCEALED_TYPES:
            return None, count
        text = board.stringForType_(AppKit.NSPasteboardTypeString)
    except Exception:
        return "", -1
    return _clip_text(text), count


def _clip_text(text) -> str | None:
    raw = str(text or "")
    if not raw.strip():
        return ""
    return None if _secret_like(raw) else _text(raw, 60)


# --- compressing the log into actions --------------------------------------------------------

def _clock(t: float) -> str:
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def _target(e: dict) -> str:
    kind = e.get("kind") or e.get("role", "").removeprefix("AX").lower() or "spot"
    if e.get("secure"):
        return "password field"
    label = e.get("label", "")
    if kind == "spot" and not label:
        return f"a spot at ({e.get('x')}, {e.get('y')}) that has no accessible name"
    words = f"{kind} '{label}'" if label else f"{kind} (no name)"
    if e.get("value") and e.get("role") not in ("AXStaticText",):
        words += f" [value '{e['value']}']"
    if e.get("container"):
        words += f" in {e['container']}"
    if e.get("ident") and not label:
        words += f" (id {e['ident']})"
    return words


_TYPING = ("char", "type")
_TIER = {"title": 0, "scroll": 0, "char": 1, "type": 1, "key": 1, "app": 2, "url": 2}   # the rest: 3


def merge_typing(events: list[dict]) -> list[dict]:
    """Consecutive keystrokes into one field -> one event holding the typed run (as compress
    would read them), so a long recording keeps its clicks and shortcuts instead of every key."""
    out: list[dict] = []
    group: dict | None = None
    for e in events:
        kind = e["type"]
        key = (e.get("pid"), e.get("field"), e.get("field_role"))
        joins = group is not None and group["_key"] == key and (
            kind in _TYPING or (kind == "key" and e.get("key") in _EDIT_KEYS))
        if not joins and kind not in _TYPING:
            group = None
            out.append(e)
            continue
        if not joins:
            group = {**e, "_key": key, "text": e.get("text", "") if kind == "char" else "",
                     "edited": bool(e.get("edited"))}
            group["type"] = "type" if e.get("secure") else "char"
            out.append(group)
            continue
        if kind == "type" and e.get("secure"):
            group.update(type="type", secure=True, text="")
        elif group["type"] == "type" and group.get("secure"):
            pass                                  # a password run stays a password run
        elif kind == "char":
            group["text"] += e["text"]
            group["edited"] = group["edited"] or bool(e.get("edited"))
        elif e["key"] == "Delete":
            group["text"], group["edited"] = group["text"][:-1], True
        else:
            group["edited"] = True
    for e in out:
        e.pop("_key", None)
    return out


def make_room(events: list[dict], limit: int | None = None) -> tuple[list[dict], int]:
    """The event list is full: merge keystrokes into typed runs first; only if that is not enough,
    drop the oldest of the least useful events (window titles and scrolling, then typing, then app
    and page changes; clicks, shortcuts and menu choices last). -> (events, how many were lost)."""
    limit = limit or MAX_EVENTS
    events = merge_typing(events)
    if len(events) < limit * 0.9:
        return events, 0
    target, lost = int(limit * 0.75), 0
    for tier in (0, 1, 2, 3):
        excess = len(events) - target
        if excess <= 0:
            break
        drop = set()
        for n, e in enumerate(events):
            if len(drop) >= excess:
                break
            if _TIER.get(e["type"], 3) == tier:
                drop.add(n)
        lost += sum(1 for n in drop if events[n]["type"] != "title")
        events = [e for n, e in enumerate(events) if n not in drop]
    return events, lost


def compress(events: list[dict]) -> list[dict]:
    """Raw events -> actions: [{"t", "app", "text", "shot"}], typing merged, noise dropped."""
    events = sorted(events, key=lambda e: e.get("t", 0))
    actions: list[dict] = []
    run: dict | None = None               # the typed run being built
    sensitive_t: float | None = None      # last time a password / code field was clicked or typed in

    def armed(t) -> bool:
        return sensitive_t is not None and t - sensitive_t <= SENSITIVE_SECONDS

    def emit(t, app, text, shot=None, window=""):
        if actions and actions[-1]["text"] == text and actions[-1]["app"] == app:
            actions[-1]["times"] = actions[-1].get("times", 1) + 1
            return actions[-1]
        actions.append({"t": t, "app": app, "text": text, "shot": shot, "window": window})
        return actions[-1]

    def flush(ending: str = ""):
        nonlocal run
        if run is None:
            return
        text = run["text"]
        where = f" into {run['field']}" if run["field"] else ""
        if run["secure"]:
            line = f"typed [password - not recorded]{where}"
        elif not text.strip():
            line = None
        elif run["hidden"] or _secret_like(text):
            line = f"typed [secret-like text - not recorded]{where}"
        else:
            shown = text if len(text) <= 140 else text[:140] + "…"
            line = f"typed '{shown}'{where}" + (" (with corrections)" if run["edited"] else "")
        if line and ending:
            line += f", then pressed {ending}"
        elif not line and ending:
            line = f"pressed {ending}"
        if line:
            action = emit(run["t"], run["app"], line)
            if not run["secure"] and text.strip():
                action["_run"] = (run["key"], text, run["field"])
        run = None

    for i, e in enumerate(events):
        kind = e["type"]
        if kind in ("char", "type") or (kind == "key" and e["key"] in _EDIT_KEYS and run is not None):
            key = (e.get("pid"), e.get("field"), e.get("field_role"))
            if run is not None and run["key"] != key:
                flush()
            if run is None:
                run = {"key": key, "t": e["t"], "app": e["app"], "field": _field_words(e), "text": "",
                       "secure": False, "edited": False,
                       "hidden": _sensitive_label(e.get("field", "")) or armed(e["t"])}
            if run["hidden"]:
                sensitive_t = e["t"]              # a code typed box by box stays hidden box by box
            if kind == "type" and e.get("secure"):
                run["secure"] = True
            elif kind == "char":
                run["text"] += e["text"]
                run["edited"] = run["edited"] or bool(e.get("edited"))
            elif e["key"] == "Delete":
                run["text"], run["edited"] = run["text"][:-1], True
            else:
                run["edited"] = True
            continue
        if kind == "key":
            label = "+".join(e.get("mods", []) + [e["key"]])
            if run is not None and e["key"] in ("Return", "Enter", "Tab"):
                flush(label)
            else:
                flush()
                emit(e["t"], e["app"], f"pressed {label}" + (f" in {_field_words(e)}" if _field_words(e) else ""))
            continue
        if kind == "shortcut":
            clip = e.get("clip") or ""
            if clip and e["keys"] == "cmd+v" and (_sensitive_label(e.get("field", "")) or armed(e["t"])):
                clip = ""                         # pasted into a password / code field
            if e["keys"] == "cmd+v" and run is not None and clip and not run["secure"]:
                run["text"] += f"[pasted '{clip}']"
                continue
            flush()
            extra = ""
            if clip:
                extra = f" (clipboard: '{clip}')"
            elif e.get("clip_hidden") or e.get("clip"):
                extra = " (clipboard not recorded)"
            emit(e["t"], e["app"], f"pressed {e['keys']}{extra}")
            continue
        flush()
        if kind == "pm":
            sensitive_t = e["t"]                  # what comes next may be a password from it
            if not (actions and actions[-1]["text"] == PM_NOTE):
                emit(e["t"], e.get("app", ""), PM_NOTE)
            continue
        if kind in ("click", "drag") and e.get("label"):
            sensitive_t = e["t"] if _sensitive_label(e["label"]) else None
        if kind == "app":
            later = events[i + 1] if i + 1 < len(events) else None
            if later and later["type"] == "app" and later["t"] - e["t"] < 1.0:
                continue                          # a flicker on the way to another app
            if actions and actions[-1].get("switch") == e["app"]:
                continue
            emit(e["t"], e["app"], f"switched to {e['app']}" + (f" (window '{e['window']}')" if e.get("window") else ""))
            actions[-1]["switch"] = e["app"]
        elif kind == "title":
            continue                              # carried as context on the next action instead
        elif kind == "url":
            later = next((x for x in events[i + 1:] if x["type"] == "url"), None)
            if later and later["t"] - e["t"] < 1.6:
                continue                          # a redirect hop
            emit(e["t"], e["app"], f"page is now {e['url']}" + (f" ('{e['page']}')" if e.get("page") else ""))
        elif kind == "scroll":
            dy, dx = e.get("dy", 0), e.get("dx", 0)
            way = ("down" if dy < 0 else "up") if abs(dy) >= abs(dx) else ("right" if dx < 0 else "left")
            emit(e["t"], e["app"], f"scrolled {way}" + (f" in {e['where']}" if e.get("where") else ""))
        elif kind in ("click", "drag"):
            if not e.get("app"):
                continue
            nxt = events[i + 1] if i + 1 < len(events) else None
            next_click = next((x for x in events[i + 1:] if x["type"] in ("click", "drag")), None)
            if (e.get("role") == "AXMenuBarItem" and next_click and next_click.get("role") == "AXMenuItem"
                    and next_click["t"] - e["t"] < 15):
                continue                          # opening the menu: the item click says it all
            if (nxt and nxt["type"] == "click" and nxt.get("count", 1) >= 2 and nxt.get("label") == e.get("label")
                    and nxt.get("role") == e.get("role") and nxt["t"] - e["t"] < 0.8):
                continue                          # first half of a double-click
            if e.get("role") in ("AXTextField", "AXTextArea", "AXSearchField", "AXComboBox") and nxt is not None \
                    and nxt["type"] in ("char", "type") and nxt.get("pid") == e.get("pid"):
                continue                          # clicking into the field; the typing names it
            if kind == "drag":
                text = f"dragged {_target(e)} to {e.get('to')}" + (f" in {e['to_container']}" if e.get("to_container") else "")
            elif e.get("menu"):
                prefix = "chose from the context menu" if e.get("container") == "menu" and "> " not in e["menu"] \
                    else "chose menu"
                text = f"{prefix} {e['menu']}"
            else:
                verb = {"right": "right-clicked"}.get(e["button"], "double-clicked" if e.get("count", 1) >= 2 else "clicked")
                mods = f" with {'+'.join(e['mods'])} held" if e.get("mods") else ""
                text = f"{verb} {_target(e)}{mods}" + (" (web page)" if e.get("web") else "")
            emit(e["t"], e["app"], text, e.get("shot"), e.get("window", ""))
    flush()
    _redact_split_secrets(actions)
    return actions


def _redact_split_secrets(actions: list[dict]) -> None:
    """A key typed in two goes (a click in between) must not leak as two harmless-looking halves:
    check each field's consecutive runs together, and blank every run that joins into a secret."""
    by_field: dict = {}
    for action in actions:
        if "_run" in action:
            by_field.setdefault(action["_run"][0], []).append(action)
    for runs in by_field.values():
        bad = set()
        for i in range(len(runs)):
            for j in range(i + 1, min(i + 4, len(runs)) + 1):
                if _secret_like("".join(r["_run"][1] for r in runs[i:j])):
                    bad.update(range(i, j))
        for i in bad:
            field = runs[i]["_run"][2]
            runs[i]["text"] = "typed [secret-like text - not recorded]" + (f" into {field}" if field else "")
    for action in actions:
        action.pop("_run", None)


def _field_words(e: dict) -> str:
    role = {"AXTextField": "text field", "AXTextArea": "text area", "AXSearchField": "search field",
            "AXComboBox": "combo box", "AXWebArea": "web page", "AXGroup": "editor"}.get(e.get("field_role", ""), "")
    label = e.get("field", "")
    if label and role:
        return f"{role} '{label}'"
    return f"'{label}'" if label else role


def action_lines(actions: list[dict]) -> list[str]:
    lines, last_app, last_window = [], None, None
    for n, a in enumerate(actions, 1):
        head = f"{n}. [{_clock(a['t'])}]"
        if a["app"] and a["app"] != last_app:
            head += f" ({a['app']})"
            last_app = a["app"]
        text = a["text"] + (f" x{a['times']}" if a.get("times", 1) > 1 else "")
        if a.get("window") and a["window"] != last_window and not text.startswith("switched"):
            text += f" - window '{a['window']}'"
            last_window = a["window"]
        if a.get("shot"):
            text += f" [screenshot S{a['shot']}]"
        lines.append(f"{head} {text}")
    if len(lines) > MAX_LINES:
        keep = MAX_LINES // 2
        lines = lines[:keep] + [f"... ({len(lines) - 2 * keep} actions left out) ..."] + lines[-keep:]
    return lines


# --- writing the skill ---------------------------------------------------------------------

WRITER_MODELS = [m for m in (os.environ.get("MINT_TEACH_MODEL"), "gemini-3.7-flash", "gemini-3.5-flash",
                             "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite") if m]

_TOOLS_TEXT = """\
- open_app <App> - launch or switch to an app.
- ui_act click|double_click|right_click|type|dismiss target="<the control in plain words, e.g. 'New Note \
button in the toolbar', 'To field', 'the message box'>" text="..." press_return=true - THE way to click or \
type into anything in an app's window; it finds controls by their accessible name and role.
- menu "File > Export as PDF…" - any menu-bar command of the app in front (surer than clicking menus).
- press_key <key> modifiers=cmd,shift - a keyboard shortcut, or Return / Escape / Tab.
- type_text "<text>" press_return=... - type into the field that already has focus.
- open_url <url> - open a web address in a new tab.
- browser <action> target text - in the browser's page: click (link/button text), fill (field label + text), \
select (dropdown + option), go (URL in this tab), switch (to a tab), read, wait (page loaded), back.
- switch_to <words> - bring an open browser tab or window to the front.
- scroll_to target="<text to bring into view>" | where="<pane>" direction; wait_for_text "<text>" (gone=true \
for a spinner going away); read_window (read the window's text); look (screenshot, when names are not enough).
- clipboard, file_action, find_files, compose_email, create_note, create_reminder, run_applescript - native \
tools, only when they do exactly what the user did, more surely than clicking."""


def _prompt(goal: str, lines: list[str], narration: list[str], title: str, shot_ids: list[int]) -> str:
    said = "\n".join(f"- {n}" for n in narration) or "(the user said nothing while doing it)"
    return f"""You turn a recorded demonstration into a reusable SKILL for Mint, a voice assistant that \
operates the user's Mac through tools. The user did the task once while Mint watched.
{f'What the user said the task is: {goal}' if goal else ''}
{f'Title the user asked for: {title}' if title else ''}

RECORDED ACTIONS (times are mm:ss from the start; controls are named from the accessibility tree - \
their real on-screen names and roles):
{chr(10).join(lines)}

WHAT THE USER SAID WHILE DOING IT (their own words - the best evidence of intent and of what changes \
from one time to the next):
{said}

{f"SCREENSHOTS {', '.join(f'S{i}' for i in shot_ids)} follow, each taken at the click that names it; a red ring marks where they clicked." if shot_ids else "No screenshots were taken."}

MINT'S TOOLS - write every step in terms of these:
{_TOOLS_TEXT}

RULES
1. Generalise. This was ONE example. Anything that would differ next time - a person, a message, a \
file or note name, a search term, a date, an amount, a specific item's URL - becomes a parameter in \
angle brackets, e.g. ui_act type target="To field" text="<recipient>". Keep what is fixed literal: app \
names, button and field labels, menu paths, a site's address.
2. Name controls, never coordinates or pixel positions. Say where a control is when that helps find it \
("in the sidebar", "in the dialog", "at the top of the page").
3. Merge low-level actions into meaningful steps. Drop noise: typos the user corrected, things undone, \
detours that do not serve the goal, idle scrolling, switching to or from Mint itself. Keep prerequisites \
(opening, selecting, waiting for a page or dialog).
4. Prefer the surest tool: menu for a menu command (also when the user pressed its shortcut - say both, \
e.g. menu "File > Save" (cmd+S)); browser click/fill for web pages; open_url for a known address.
5. The step that sends, submits, posts, buys or deletes (also by shortcut, e.g. cmd+shift+D sends in Mail) \
MUST end with "(confirm with the user first unless they clearly asked for it)".
6. "[password - not recorded]" means the user typed a password: write a step asking the user to type it \
themselves. Never write passwords, keys, card numbers or private message text into the skill.
7. ALWAYS end with a check step: what should be on screen when it worked (wait_for_text "<something \
that appears>" or read_window and look for it).
8. Steps are plain strings without numbers, each starting with the tool name, using double quotes for \
text: ui_act click target="New Note button in the toolbar". 3-12 steps.
9. Parameters: every <name> used in the steps, listed once with what it means.

Write JSON only:
{{"title": "short imperative title, e.g. Add a note in Notes",
  "when": "when to use it, in terms of what the user would ask",
  "apps": ["app or site names"],
  "category": "folder: apps/<app> or browser/<site> or general/<topic>, lowercase",
  "parameters": [{{"name": "<note title>", "meaning": "what it is and where it comes from"}}],
  "steps": ["open_app Notes.", "..."],
  "notes": ["gotchas worth knowing next time: waits, where a control hides, what the shortcut is"]}}"""


def write_skill(actions: list[dict], narration: list[str], shots: list[dict], goal: str = "",
                title: str = "") -> dict | None:
    """Gemini drafts the skill from the compressed actions (+ narration and screenshots)."""
    from google.genai import types

    from mint.core import llm
    lines = action_lines(actions)
    used = {a.get("shot") for a in actions if a.get("shot")}
    shots = [s for s in shots if s["id"] in used][:MAX_SHOTS]
    contents: list = [_prompt(goal, lines, narration, title, [s["id"] for s in shots])]
    for shot in shots:
        contents.append(f"Screenshot S{shot['id']}:")
        contents.append(types.Part.from_bytes(data=shot["jpeg"], mime_type="image/jpeg"))
    try:
        text, model = llm.generate(contents, WRITER_MODELS, json_mode=True)
        data = llm.parse_json(text)
        log.info("teach: skill drafted by %s", model)
    except Exception as error:
        log.info("teach: drafting failed: %s", str(error)[:200])
        return None
    if isinstance(data, list) and data:
        data = data[0]
    return data if isinstance(data, dict) else None


def _save(draft: dict, title: str = "") -> tuple[dict | None, str]:
    from mint.knowledge import skills as skillbook
    steps = [re.sub(r"^\s*\d+[.)]\s*", "", str(s)).strip() for s in draft.get("steps") or [] if str(s).strip()]
    notes = [str(n).strip() for n in draft.get("notes") or [] if str(n).strip()]
    params = [p for p in draft.get("parameters") or [] if isinstance(p, dict) and p.get("name")]
    for p in params:                      # the steps say <note title>; the list must match
        p["name"] = "<" + str(p["name"]).strip("<> ") + ">"
    if params:
        inputs = "; ".join(f"{p['name']}" + (f" - {p['meaning']}" if p.get("meaning") else "") for p in params)
        notes.insert(0, f"Inputs: {inputs}")
    risky = re.compile(r"\b(send|sends|sent|submit|post|publish|delete|buy|purchase|pay|order)\b", re.I)
    if risky.search(" ".join(steps + notes + [str(draft.get("title") or "")])) and \
            not any("confirm" in s.lower() for s in steps):
        notes.insert(0, "Before the step that sends, submits, posts, buys or deletes, confirm with the user "
                        "unless they clearly asked for it.")
    notes.append(f"Taught by demonstration on {time.strftime('%Y-%m-%d')}.")
    apps = draft.get("apps") or []
    apps = ", ".join(str(a) for a in apps) if isinstance(apps, list) else str(apps)
    return skillbook.create(title.strip() or str(draft.get("title") or "").strip(), str(draft.get("when") or ""),
                            steps, notes, category=str(draft.get("category") or ""), apps=apps, source="taught")


# --- the public API --------------------------------------------------------------------------

def _permission_problem(ask: bool) -> str:
    import Quartz
    try:
        allowed = bool(Quartz.CGPreflightListenEventAccess())
    except Exception:
        allowed = True
    if not allowed and ask:
        try:
            Quartz.CGRequestListenEventAccess()     # adds Mint to the list and shows macOS's own prompt
        except Exception:
            pass
    return ("I can't watch the keyboard and mouse: macOS hasn't given Mint Input Monitoring. " + SETTINGS_HINT)


def start(goal: str = "", ask: bool = True) -> str:
    global _rec
    with _lock:
        # the slot is taken before the tap exists, so a second start while this one waits for the
        # tap sees it (and stop / cancel can reach it) instead of making a second, unreachable tap
        if _rec is not None and not _rec.stopping.is_set() and (_rec.starting or _rec.capturing):
            return f"Already watching ({int(_rec.t())} s so far). Say done when you've finished."
        rec = _rec = _Recording(goal)
        tap_thread = threading.Thread(target=rec._tap_thread, name="teach-tap", daemon=True)
        rec.threads.append(tap_thread)
        tap_thread.start()
    rec.ready.wait(3.0)
    with _lock:
        rec.starting = False
        if _rec is not rec or rec.stopping.is_set():      # stopped or cancelled while starting
            rec.stopping.set()
            return "Stopped watching before it began."
        failed = bool(rec.tap_error) or not rec.capturing
        if failed:
            rec.stopping.set()
            _rec = None
        else:
            for target, name in ((rec._worker, "teach-worker"), (rec._watch_front, "teach-front")):
                thread = threading.Thread(target=target, name=name, daemon=True)
                thread.start()
                rec.threads.append(thread)
    if failed:
        _emit("failed", "no event tap")
        return _permission_problem(ask)
    try:
        rec._check_front()
    except Exception:
        pass
    import ApplicationServices as AX
    warn = []
    if not rec.keyboard:
        warn.append("I can see clicks but not typing - for typing, Mint needs Input Monitoring. " + SETTINGS_HINT)
    if not AX.AXIsProcessTrusted():
        rec.accessibility = False
        warn.append("Without Accessibility permission I can't read the names of what you click.")
    _emit("recording", goal)
    return ("Watching now - go ahead and do it once, and talk me through it if you like. Passwords are "
            "never recorded. Say 'done' when you've finished, or 'cancel' to throw it away. It stops by "
            "itself after 10 minutes." + (" " + " ".join(warn) if warn else ""))


def add_narration(text: str) -> None:
    """What the user said while recording (the main session passes it in as it hears it)."""
    text = " ".join(str(text or "").split())
    with _lock:
        if _rec is not None and text and not _secret(text):
            _rec.narration.append((_rec.t(), text))


def _halt(rec: "_Recording") -> None:
    rec.stopping.set()
    for thread in rec.threads:
        if thread is not threading.current_thread():
            thread.join(timeout=3.0)


def cancel() -> str:
    global _rec
    with _lock:
        rec, _rec = _rec, None
    if rec is None:
        return "I wasn't watching anything."
    _halt(rec)
    rec.events.clear()
    rec.shots.clear()
    _emit("cancelled")
    return "Stopped watching and threw the recording away."


def stop(title: str = "", narration=None) -> str:
    """Stop watching and turn the recording into a skill. Returns what was saved."""
    global _rec, _last_result
    with _lock:
        rec, _rec = _rec, None
    if rec is None:
        return _last_result or "I wasn't watching anything. Say 'watch me do this' first."
    _halt(rec)
    _emit("stopped")
    said = [f"[{_clock(t)}] {text}" for t, text in rec.narration]
    if isinstance(narration, str):
        narration = [narration]
    for line in narration or []:
        line = " ".join(str(line).split())
        if line and not _secret(line):
            said.append(line)
    with _lock:
        events = list(rec.events)
        shots = list(rec.shots)
    rec.events.clear()
    rec.shots.clear()
    actions = compress(events)
    real = [a for a in actions if not a["text"].startswith(("switched to", "page is now"))]
    if not real:
        _emit("failed", "nothing recorded")
        why = "" if rec.keyboard and rec.accessibility else " (permissions were missing - see what I said at the start)"
        return f"I didn't see you do anything I could learn from{why}. Say 'watch me' to try again."
    result = finish(actions, said, shots, goal=rec.goal, title=title)
    if rec.dropped:
        result += "\n(The recording was long; some typing was summarised.)"
    _last_result = result
    return result


def finish(actions: list[dict], narration: list[str], shots: list[dict], goal: str = "", title: str = "") -> str:
    """Write and save the skill for already-compressed actions (split out for testing)."""
    draft = write_skill(actions, narration, shots, goal=goal, title=title)
    if not draft or not draft.get("steps"):
        _emit("failed", "drafting failed")
        return ("I watched, but couldn't write the skill just now (Gemini didn't answer). Here is what I saw:\n"
                + "\n".join(action_lines(actions)[:25]))
    skill, message = _save(draft, title)
    if skill is None:
        _emit("failed", message)
        return message
    _emit("saved", skill["title"])
    return (f"{message} ({skill['category']}/{skill['name']}), from {len(actions)} recorded actions. "
            f"When: {skill['meta'].get('when', '')}\n{skill['body']}\n"
            "Read the steps back briefly and ask if anything should change (update_skill).")


def status() -> str:
    with _lock:
        rec = _rec
        if rec is None:
            return "Not watching. " + (f"Last time: {_last_result.splitlines()[0]}" if _last_result else "")
        clicks = sum(1 for e in rec.events if e["type"] in ("click", "drag"))
        keys = sum(1 for e in rec.events if e["type"] in ("char", "shortcut", "key"))
        state = "stopped after 10 minutes - say done to save it" if rec.auto_stopped else "watching"
        return (f"{state}: {int(rec.t())} s, {clicks} clicks, {keys} key presses, {len(rec.shots)} screenshots"
                + (f", goal: {rec.goal}" if rec.goal else "") + ".")


def recording() -> bool:
    with _lock:
        return _rec is not None and not _rec.stopping.is_set()


# --- the tool --------------------------------------------------------------------------------

PROMPT = """Teach by showing: when the user wants to SHOW you how to do something - "watch me do this", \
"learn how I do this", "I'll show you how", "record a skill" - call teach action=start (goal = the task in \
their words, if they said it), say one short line, then stay quiet and do NOT use screen tools while they \
work (your own clicks would be recorded). When they say "done", "that's it", "that's how you do it", call \
teach action=stop with title if they named it and narration = what they said while showing you, as short \
lines. Then read back the saved skill's steps briefly. "cancel"/"never mind" -> action=cancel."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="teach",
        description=("Learn a task by watching the user do it once, then save it as a skill Mint can follow "
                     "later. start: begin watching clicks, typing and pages (never passwords); stop: finish "
                     "and write the skill; cancel: throw the recording away; status: is it recording."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["start", "stop", "cancel", "status"]),
            "goal": types.Schema(type=S, description="start: the task in the user's words, e.g. 'file an expense in Expensify'"),
            "title": types.Schema(type=S, description="stop: a title for the skill, if the user gave one"),
            "narration": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=S),
                                      description="stop: what the user said while showing you, in order")},
            required=["action"]))]


def tool(args: dict) -> str:
    action = str(args.get("action") or "status").lower()
    if action == "start":
        return start(str(args.get("goal") or ""))
    if action == "stop":
        return stop(str(args.get("title") or ""), args.get("narration"))
    if action == "cancel":
        return cancel()
    return status()


HANDLERS = {"teach": tool}
