"""verify_state: check that an action really worked by reading the Mac - never by clicking.

An action's own reply ("pressed", "typed") says what Mint did, not what the app did with it. Here the
model (or Mint's own code) states what should now be true, as up to 8 checks that must ALL hold:

    window      a window exists (app / title contains), or is gone
    front_app   the app in front is X
    element     a control with role / label exists, its value equals / contains, it is enabled, selected
    text        text is visible in the window: accessibility first, text recognition (OCR) as the fallback
    file        a file exists, changed in the last N seconds (a save, a download), or is gone

Each sample reads everything once; the checks are polled every ~150 ms until they hold in 2 samples in a row
(a dialog half-drawn, a value still being typed) or the time runs out (6 s, at most 20). The answer is
satisfied, unsatisfied (which check failed and what was there instead) or unknown (it could not read -
never counted as success). Read-only: nothing is clicked, typed, raised or activated.

Also here, for code that acts: before() / after(), a window-change detector. A snapshot of the window list,
the app in front and its focused window before an action; after it, one short line of what changed ("new
sheet 'Save' in TextEdit", "front app changed to Finder") or '' when nothing did.

API:
    until(preds, timeout=6.0, stable=2) -> Result      poll; Result.ok, .status, .text()
    check(preds) -> Result                             one sample
    before(pids=()) -> Snap;  after(snap) -> str;  diff(a, b) -> str

Ideas from Cua Driver's verify_state (bounded predicates, stable samples, unknown never implies success)
and its WindowChangeDetector (MIT); the code is Mint's own.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mint.screen.verify")

SATISFIED, UNSATISFIED, UNKNOWN = "satisfied", "unsatisfied", "unknown"
CHECKS = ("window", "front_app", "element", "text", "file")
MAX_PREDICATES = 8
DEFAULT_TIMEOUT = 6.0
MAX_TIMEOUT = 20.0
INTERVAL = 0.15            # between samples
STABLE = 2                 # passing samples in a row that count as done
NODES = 3000               # elements read per app per sample ...
WALK_SECONDS = 1.2         # ... and the time that may take
AX_TIMEOUT = 1.0           # one hung app answers within this, not the default 6 s
SEEN = 60                  # characters of one thing seen, in a result


# --- what one sample reads -----------------------------------------------------------------------

class Reader:
    """Reads the Mac for one sample. Every method returns None when it could not read (no permission,
    a hung app): the checks then say unknown, never pass. Replaced by a fake in tests."""

    def front(self) -> dict | None:
        """{pid, name, bundle} of the app in front."""
        import AppKit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return _app_info(app) if app is not None else None

    def apps(self) -> list[dict]:
        """Running apps with a Dock presence or menus: [{pid, name, bundle}]."""
        import AppKit
        out = []
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            if app.activationPolicy() != AppKit.NSApplicationActivationPolicyProhibited and not app.isTerminated():
                out.append(_app_info(app))
        return out

    def windows(self) -> list[dict] | None:
        """On-screen windows front to back: [{id, pid, app, title, layer, w, h}]."""
        return _cg_windows()

    def ax_titles(self, pid: int) -> list[str] | None:
        """Titles of the app's windows through Accessibility (also minimised ones)."""
        if pid == os.getpid():
            return None
        try:
            from mint.screen import axkit
            windows = axkit.windows_front_to_back(pid)
            if not windows and not _ax_readable(pid):
                return None             # no Accessibility permission, or the app does not answer
            return [str(axkit.attr(w, "AXTitle") or "") for w in windows]
        except Exception:
            log.debug("no AX windows for %s", pid, exc_info=True)
            return None

    def elements(self, pid: int) -> tuple[list[dict], bool] | None:
        """(elements of the app's windows, read to the end?) - see _read_elements."""
        if pid == os.getpid():
            return None             # Mint's own windows: AppKit answers in-process, only safe on the main thread
        try:
            return _read_elements(pid)
        except Exception:
            log.debug("could not read the elements of %s", pid, exc_info=True)
            return None

    def web_text(self, pid: int, text: str) -> bool | None:
        """Is `text` visible in a web page of the app (AX search, for pages too big to walk)?"""
        if pid == os.getpid():
            return None
        try:
            return _web_text(pid, text)
        except Exception:
            return None

    def ocr(self, pid: int) -> list[str] | None:
        """Lines of text read off the app's front window (only that window, even when covered)."""
        try:
            return _ocr_window(pid)
        except Exception:
            log.debug("ocr failed for %s", pid, exc_info=True)
            return None

    def stat(self, path: str) -> tuple[bool, float] | None:
        """(exists, mtime) of a file; None when it can't be looked at."""
        try:
            info = os.stat(path)
            return True, float(info.st_mtime)
        except FileNotFoundError:
            return False, 0.0
        except OSError:
            return None

    def wall(self) -> float:
        return time.time()


def _app_info(app) -> dict:
    return {"pid": int(app.processIdentifier()), "name": str(app.localizedName() or ""),
            "bundle": str(app.bundleIdentifier() or "")}


class _Sample:
    """One sample: each reading done at most once, however many checks need it."""

    def __init__(self, reader: Reader) -> None:
        self.reader = reader
        self._memo: dict = {}

    def get(self, name: str, *args):
        key = (name, *args)
        if key not in self._memo:
            self._memo[key] = getattr(self.reader, name)(*args)
        return self._memo[key]


# --- the checks ------------------------------------------------------------------------------------

@dataclass
class Outcome:
    index: int
    what: str                   # the check in words: "window 'Save' in TextEdit"
    status: str
    seen: str = ""              # what was there instead / why unknown


@dataclass
class Result:
    status: str
    outcomes: list[Outcome] = field(default_factory=list)
    samples: int = 0
    elapsed: float = 0.0
    stable: bool = False
    stopped: bool = False

    @property
    def ok(self) -> bool:
        return self.status == SATISFIED

    def failing(self) -> list[Outcome]:
        worst = UNSATISFIED if any(o.status == UNSATISFIED for o in self.outcomes) else UNKNOWN
        return [o for o in self.outcomes if o.status == worst]

    def text(self) -> str:
        took = f"{self.samples} sample{'s' if self.samples != 1 else ''}, {self.elapsed:.1f}s"
        if self.stopped:
            return "STOPPED by the user before the check finished."
        if self.ok:
            return f"SATISFIED ({took}): " + "; ".join(o.what for o in self.outcomes) + "."
        bad = self.failing()
        parts = [f"#{o.index + 1} {o.what}" + (f" - {o.seen}" if o.seen else "") for o in bad]
        held = [f"#{o.index + 1}" for o in self.outcomes if o.status == SATISFIED]
        tail = f" Held: {', '.join(held)}." if held else ""
        if self.status == UNSATISFIED:
            return f"UNSATISFIED after {took}: " + "; ".join(parts) + "." + tail
        return (f"UNKNOWN after {took} (could not confirm - this is NOT a success): " + "; ".join(parts) + "."
                + tail + " Look at the screen before saying it is done.")


def normalize(predicates) -> tuple[list[dict], str]:
    """(clean predicates, '') or ([], why they can't be checked)."""
    if isinstance(predicates, dict):
        predicates = [predicates]
    if not isinstance(predicates, (list, tuple)) or not predicates:
        return [], "no checks given (expect: a list of 1-8 checks)."
    if len(predicates) > MAX_PREDICATES:
        return [], f"{len(predicates)} checks: at most {MAX_PREDICATES}."
    out = []
    for n, raw in enumerate(predicates, 1):
        if not isinstance(raw, dict):
            return [], f"check {n} is not an object."
        pred = {k: v for k, v in raw.items() if v not in (None, "")}
        kind = str(pred.get("check") or _guess_kind(pred)).strip().lower().replace(" ", "_")
        kind = {"front": "front_app", "app": "front_app", "control": "element", "file_saved": "file"}.get(kind, kind)
        pred["check"] = kind
        if kind not in CHECKS:
            return [], f"check {n}: '{kind}' is not one of {', '.join(CHECKS)}."
        need = {"window": ("app", "title"), "front_app": ("app",), "element": ("role", "label"),
                "text": ("text",), "file": ("path",)}[kind]
        if not any(str(pred.get(k) or "").strip() for k in need):
            return [], f"check {n} ({kind}) needs {' or '.join(need)}."
        if kind == "element" and pred.get("gone") and any(
                k in pred for k in ("value_equals", "value_contains", "enabled", "selected")):
            return [], f"check {n}: an element that is gone has no value or state to check."
        out.append(pred)
    return out, ""


def _guess_kind(pred: dict) -> str:
    if "path" in pred:
        return "file"
    if "text" in pred:
        return "text"
    if any(k in pred for k in ("role", "label", "value_equals", "value_contains", "enabled", "selected")):
        return "element"
    if "title" in pred:
        return "window"
    return "front_app" if "app" in pred else ""


def _norm(text) -> str:
    return " ".join(str(text or "").split()).lower()


def _seen(text) -> str:
    text = " ".join(str(text or "").split())
    try:
        from mint.core import untrusted
        text = untrusted.clean(text)
    except Exception:
        pass
    return text if len(text) <= SEEN else text[: SEEN - 1] + "…"


def _app_matches(want: str, info: dict) -> bool:
    want = _norm(want)
    return bool(want) and (want in _norm(info.get("name")) or want == _norm(info.get("bundle"))
                           or want == _norm(info.get("app")))


def _describe(pred: dict) -> str:
    kind, gone = pred["check"], bool(pred.get("gone"))
    app = f" in {pred['app']}" if pred.get("app") else ""
    if kind == "window":
        what = f"window '{pred['title']}'{app}" if pred.get("title") else f"a window of {pred['app']}"
        return what + (" gone" if gone else "")
    if kind == "front_app":
        return f"{pred['app']} {'not ' if gone else ''}in front"
    if kind == "text":
        return f"text '{pred['text']}'{app} {'gone' if gone else 'visible'}"
    if kind == "file":
        what = f"file {pred['path']}"
        if gone:
            return what + " gone"
        if pred.get("newer_than_seconds") is not None:
            return what + f" changed in the last {float(pred['newer_than_seconds']):g}s"
        return what + (" changed" if pred.get("since") is not None else " exists")
    parts = [str(pred.get("role") or "element"), f"'{pred['label']}'" if pred.get("label") else ""]
    for key in ("value_equals", "value_contains"):
        if key in pred:
            parts.append(f"{key.replace('_', ' ')} '{pred[key]}'")
    for key in ("enabled", "selected"):
        if key in pred:
            parts.append(key if _truthy(pred[key]) else f"not {key}")
    return " ".join(p for p in parts if p) + app + (" gone" if gone else "")


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off", "")
    return bool(value)


def evaluate(preds: list[dict], sample: _Sample) -> list[Outcome]:
    out = []
    for n, pred in enumerate(preds):
        try:
            status, seen = _CHECK[pred["check"]](pred, sample)
        except Exception as error:
            log.debug("check %s failed", pred, exc_info=True)
            status, seen = UNKNOWN, f"could not read ({type(error).__name__})"
        out.append(Outcome(n, _describe(pred), status, seen))
    return out


def aggregate(outcomes: list[Outcome]) -> str:
    if any(o.status == UNSATISFIED for o in outcomes):
        return UNSATISFIED
    if any(o.status == UNKNOWN for o in outcomes):
        return UNKNOWN
    return SATISFIED


def _flip(found: bool, gone: bool, yes: str, no: str) -> tuple[str, str]:
    """found/not found -> the outcome of a check that may be negated (gone=true)."""
    if found != gone:
        return SATISFIED, ""
    return UNSATISFIED, (yes if found else no)


def _check_front(pred: dict, sample: _Sample) -> tuple[str, str]:
    front = sample.get("front")
    if not front:
        return UNKNOWN, "could not tell which app is in front"
    return _flip(_app_matches(pred["app"], front), bool(pred.get("gone")),
                 f"{front['name']} is in front", f"{front['name'] or 'another app'} is in front")


def _check_window(pred: dict, sample: _Sample) -> tuple[str, str]:
    gone, title = bool(pred.get("gone")), _norm(pred.get("title"))
    windows = sample.get("windows")
    if windows is None:
        return UNKNOWN, "could not read the window list"
    wins = [w for w in windows if w.get("layer", 0) == 0 and w.get("w", 100) > 60 and w.get("h", 100) > 40]
    titles_known = any(w.get("title") for w in wins)
    if pred.get("app"):
        apps = [a for a in sample.get("apps") if _app_matches(pred["app"], a)]
        if not apps:
            return _flip(False, gone, "", f"{pred['app']} is not running")
        pids = {a["pid"] for a in apps}
        wins = [w for w in wins if w.get("pid") in pids]
        titles = [str(w.get("title") or "") for w in wins]
        ax_lists = [ax for pid in sorted(pids) if (ax := sample.get("ax_titles", pid)) is not None]
        for ax in ax_lists:
            titles += ax
        # Only Accessibility sees the app's minimised windows and those on other Spaces: without it, a
        # window not on screen is not proof that there is none.
        titles_known = bool(ax_lists)
        if not title:
            found = bool(wins) or any(ax_lists)
            if not found and not ax_lists:
                return UNKNOWN, f"no {pred['app']} window on screen; its other windows can't be read"
            return _flip(found, gone, f"{len(wins) or 'a'} window(s) open",
                         f"{pred['app']} has no window open")
    else:
        titles = [str(w.get("title") or "") for w in wins]
        front = sample.get("front")
        if front:
            ax = sample.get("ax_titles", front["pid"])
            if ax is not None:
                titles += ax
                titles_known = True
    found = any(title in _norm(t) for t in titles)
    if not found and not titles_known:
        return UNKNOWN, "window titles can't be read (Screen Recording off?)"
    named = [t for t in dict.fromkeys(titles) if t][:4]
    return _flip(found, gone, "it is still open",
                 "windows: " + (", ".join(f"'{_seen(t)}'" for t in named) if named else "none with a title"))


def _scope(pred: dict, sample: _Sample) -> tuple[int | None, str]:
    """(pid of the app a check looks in, '') or (None, why not)."""
    if pred.get("app"):
        apps = [a for a in sample.get("apps") if _app_matches(pred["app"], a)]
        if not apps:
            return None, f"{pred['app']} is not running"
        front = sample.get("front") or {}
        apps.sort(key=lambda a: (a["pid"] != front.get("pid"), _norm(a["name"]) != _norm(pred["app"])))
        return apps[0]["pid"], ""
    front = sample.get("front")
    if not front:
        return None, "could not tell which app is in front"
    if front["pid"] == os.getpid():
        target = _mint_target()
        if target is not None:
            return target, ""
    return front["pid"], ""


def _mint_target() -> int | None:
    """When Mint's own window is in front, the app Mint is working on (read only, nothing raised)."""
    try:
        from mint.tools import extra as extra_tools
        app = extra_tools._target.get("app")
        if app is not None and not app.isTerminated():
            return int(app.processIdentifier())
    except Exception:
        pass
    return None


def _not_running(pred: dict, why: str) -> tuple[str, str]:
    if "not running" in why:
        return _flip(False, bool(pred.get("gone")), "", why)
    return UNKNOWN, why


def _role_matches(want: str, element: dict) -> bool:
    key = _role_key(want)
    return key in (_role_key(element.get("role")), _role_key(element.get("roledesc")),
                   _role_key(element.get("subrole")))


def _role_key(role) -> str:
    key = re.sub(r"[^a-z]", "", str(role or "").lower())
    key = key[2:] if key.startswith("ax") else key
    return {"checkbox": "checkbox", "check": "checkbox", "toggle": "checkbox", "switch": "checkbox",
            "field": "textfield", "textarea": "textfield", "input": "textfield", "textbox": "textfield", "searchfield": "textfield",
            "edit": "textfield", "radio": "radiobutton", "popup": "popupbutton", "dropdown": "popupbutton",
            "menubutton": "popupbutton", "statictext": "text", "label": "text", "tabbutton": "tab",
            "radiotab": "tab", "dialog": "sheet"}.get(key, key)


def match_elements(pred: dict, elements: list[dict]) -> list[dict]:
    label = _norm(pred.get("label"))
    found = [e for e in elements
             if (not pred.get("role") or _role_matches(pred["role"], e))
             and (not label or label in _norm(e.get("label")) or label == _norm(e.get("ident")))]
    exact = [e for e in found if label and _norm(e.get("label")) == label]
    return exact or found


def check_element(pred: dict, read: tuple[list[dict], bool] | None) -> tuple[str, str]:
    """The element check against one reading of the app's elements (pure: tested with fakes)."""
    if read is None:
        return UNKNOWN, "could not read the app's controls"
    elements, complete = read
    gone = bool(pred.get("gone"))
    found = match_elements(pred, elements)
    if not found:
        if not complete:
            return UNKNOWN, f"not among the first {len(elements)} controls (the window has more)"
        near = [e for e in elements if e.get("label") and (not pred.get("role") or _role_matches(pred["role"], e))]
        hint = ", ".join(f"'{_seen(e['label'])}'" for e in near[:4])
        return _flip(False, gone, "", "no such control" + (f"; there are {hint}" if hint else ""))
    if gone:
        return UNSATISFIED, f"still there: {_brief(found[0])}"
    results = [_element_state(pred, e) for e in found]
    if all(r[0] == SATISFIED for r in results):
        return SATISFIED, ""
    if len(found) > 1 and any(r[0] == SATISFIED for r in results):
        return UNKNOWN, f"{len(found)} controls match and they differ - name it more exactly"
    bad = next((r for r in results if r[0] == UNSATISFIED), None) or results[0]
    return bad


def _element_state(pred: dict, element: dict) -> tuple[str, str]:
    if "value_equals" in pred or "value_contains" in pred:
        value = element.get("value")
        if value is None:
            return UNKNOWN, f"{_brief(element)} has no readable value"
        if "value_equals" in pred and " ".join(str(value).split()) != " ".join(str(pred["value_equals"]).split()):
            return UNSATISFIED, f"value is '{_seen(value)}'"
        if "value_contains" in pred and _norm(pred["value_contains"]) not in _norm(value):
            return UNSATISFIED, f"value is '{_seen(value)}'"
    for key in ("enabled", "selected"):
        if key in pred:
            have = element.get(key)
            if have is None:
                return UNKNOWN, f"{_brief(element)} does not say whether it is {key}"
            if bool(have) != _truthy(pred[key]):
                return UNSATISFIED, f"it is {'' if have else 'not '}{key}"
    return SATISFIED, ""


def _brief(element: dict) -> str:
    role = _role_key(element.get("role")) or "element"
    return f"{role} '{_seen(element.get('label'))}'" if element.get("label") else role


def _check_element(pred: dict, sample: _Sample) -> tuple[str, str]:
    pid, why = _scope(pred, sample)
    if pid is None:
        return _not_running(pred, why)
    read = sample.get("elements", pid)
    if read is None and pid == os.getpid():
        return UNKNOWN, "that is Mint's own window, which can't be read from here"
    return check_element(pred, read)


def text_in(text: str, read: tuple[list[dict], bool] | None) -> bool | None:
    """Is `text` in the labels / values of the elements? None when unreadable."""
    if read is None:
        return None
    needle = _norm(text)
    return any(needle in _norm(e.get("label")) or needle in _norm(e.get("value")) for e in read[0])


def check_text(pred: dict, ax: bool | None, ocr: list[str] | None) -> tuple[str, str]:
    """Accessibility first; OCR when accessibility does not have it (pure: tested with fakes)."""
    gone = bool(pred.get("gone"))
    if ax:
        return _flip(True, gone, "still in the window", "")
    if ocr is None:
        return UNKNOWN, ("not in the window's accessibility text, and the window could not be read "
                         "as an image (Screen Recording off?)")
    needle = _norm(pred["text"])
    squeezed = needle.replace(" ", "")
    found = any(needle in _norm(line) or (len(squeezed) > 3 and squeezed in _norm(line).replace(" ", ""))
                for line in ocr)
    lines = [_seen(line) for line in ocr if line.strip()][:4]
    return _flip(found, gone, "still on screen",
                 "the window shows: " + (" | ".join(f"'{t}'" for t in lines) if lines else "no text"))


def _check_text(pred: dict, sample: _Sample) -> tuple[str, str]:
    pid, why = _scope(pred, sample)
    if pid is None:
        return _not_running(pred, why)
    ax = text_in(pred["text"], sample.get("elements", pid))
    if not ax:
        read = sample.get("elements", pid)
        if read is not None and not read[1]:
            ax = sample.get("web_text", pid, pred["text"]) or ax
    if ax:
        return check_text(pred, True, None)
    return check_text(pred, ax, sample.get("ocr", pid))


def check_file(pred: dict, stat: tuple[bool, float] | None, now: float) -> tuple[str, str]:
    """(pure: tested with fakes)"""
    if stat is None:
        return UNKNOWN, "the file can't be looked at (permissions?)"
    exists, mtime = stat
    gone = bool(pred.get("gone"))
    if gone or not exists:
        return _flip(exists, gone, "it is still there", "no such file")
    if pred.get("newer_than_seconds") is not None:
        age = now - mtime
        if age > float(pred["newer_than_seconds"]):
            return UNSATISFIED, f"it exists but last changed {_ago(age)} ago"
    if pred.get("since") is not None and mtime <= float(pred["since"]):
        return UNSATISFIED, f"it exists but has not changed since (last change {_ago(now - mtime)} ago)"
    return SATISFIED, ""


def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.0f} h"


def _check_file(pred: dict, sample: _Sample) -> tuple[str, str]:
    path = os.path.expanduser(str(pred["path"]).strip())
    if not os.path.isabs(path):
        path = str(Path.home() / path)
    return check_file(pred, sample.get("stat", path), sample.reader.wall())


_CHECK = {"window": _check_window, "front_app": _check_front, "element": _check_element, "text": _check_text,
          "file": _check_file}


# --- polling ------------------------------------------------------------------------------------------

def until(predicates, timeout: float = DEFAULT_TIMEOUT, stable: int = STABLE, interval: float = INTERVAL,
          reader: Reader | None = None, clock=time.monotonic, sleep=time.sleep, stopped=None) -> Result:
    """Poll the checks until all hold in `stable` samples in a row, or `timeout` seconds pass.
    Bad checks raise ValueError (the tool turns that into a FAILED line)."""
    preds, why = normalize(predicates)
    if why:
        raise ValueError(why)
    reader = reader or Reader()
    timeout = max(0.0, min(float(timeout), MAX_TIMEOUT))
    stable = 1 if timeout == 0 else max(1, min(int(stable), 5))
    stopped = stopped or _stopped
    started = clock()
    samples = in_a_row = 0
    while True:
        samples += 1
        outcomes = evaluate(preds, _Sample(reader))
        status = aggregate(outcomes)
        in_a_row = in_a_row + 1 if status == SATISFIED else 0
        if in_a_row >= stable:
            return Result(SATISFIED, outcomes, samples, clock() - started, stable=True)
        if stopped():
            return Result(UNKNOWN, outcomes, samples, clock() - started, stopped=True)
        left = timeout - (clock() - started)
        if left <= 0.001:
            break
        sleep(min(interval, left))
    if status == SATISFIED:
        # Held only in the last sample: not proven steady.
        for outcome in outcomes:
            outcome.status, outcome.seen = UNKNOWN, "held only in the very last sample"
        status = UNKNOWN
    return Result(status, outcomes, samples, clock() - started)


def check(predicates, reader: Reader | None = None) -> Result:
    """One sample, no waiting."""
    return until(predicates, timeout=0, stable=1, reader=reader)


def _stopped() -> bool:
    try:
        from mint.app import control
        return control.stopped()
    except Exception:
        return False


# --- the tool --------------------------------------------------------------------------------------------

def declarations():
    from google.genai import types
    S, B, N = types.Type.STRING, types.Type.BOOLEAN, types.Type.NUMBER
    item = types.Schema(type=types.Type.OBJECT, properties={
        "check": types.Schema(type=S, enum=list(CHECKS), description=(
            "window (app/title), front_app (app), element (role/label + value_equals/value_contains/enabled/"
            "selected), text (text visible in the window), file (path, newer_than_seconds)")),
        "app": types.Schema(type=S, description="app name; for element/text/window: look only in this app "
                                                "(default: the app in front)"),
        "title": types.Schema(type=S, description="window: its title contains this"),
        "role": types.Schema(type=S, description="element: button, text field, checkbox, menu item, tab, link..."),
        "label": types.Schema(type=S, description="element: its label contains this"),
        "value_equals": types.Schema(type=S), "value_contains": types.Schema(type=S),
        "enabled": types.Schema(type=B), "selected": types.Schema(type=B, description="selected / checked / on"),
        "text": types.Schema(type=S, description="text: words visible in the window"),
        "path": types.Schema(type=S, description="file: its path (~ allowed)"),
        "newer_than_seconds": types.Schema(type=N, description="file: changed within this many seconds (a save, "
                                                                "a download)"),
        "gone": types.Schema(type=B, description="the opposite: the window / text / control / file is gone, or "
                                                 "the app is NOT in front")})
    return [types.FunctionDeclaration(
        name="verify_state",
        description=("Check that something really happened, without touching anything: 1-8 checks that must ALL "
                     "hold - a window (app, title), the app in front, a control (role, label; its value, enabled, "
                     "selected/checked), text visible in the window, a file saved or downloaded (path, "
                     "newer_than_seconds). Waits up to `seconds` (default 6) until they hold steadily. Answers "
                     "SATISFIED, UNSATISFIED (which check failed and what is there instead) or UNKNOWN (could not "
                     "read - not a success). Use it after an action whose result did not say CONFIRMED, before "
                     "telling the user it is done."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "expect": types.Schema(type=types.Type.ARRAY, items=item, description="the checks (all must hold)"),
            "seconds": types.Schema(type=N, description="wait at most this long, default 6, at most 20; 0 = "
                                                        "check once now"),
            "stable": types.Schema(type=types.Type.INTEGER, description="passing samples in a row, default 2")},
            required=["expect"]))]


def tool(args: dict) -> str:
    args = dict(args or {})
    expect = args.get("expect")
    if expect is None and any(k in args for k in ("check", "title", "label", "text", "path", "role")):
        expect = [{k: v for k, v in args.items() if k not in ("seconds", "stable", "expect")}]
    if isinstance(expect, str):
        import json
        try:
            expect = json.loads(expect)
        except ValueError:
            return "FAILED: `expect` must be a list of checks, e.g. [{\"check\": \"window\", \"title\": \"Save\"}]."
    try:
        seconds = float(args.get("seconds") if args.get("seconds") is not None else DEFAULT_TIMEOUT)
        result = until(expect, timeout=seconds, stable=int(args.get("stable") or STABLE))
    except ValueError as error:
        return f"FAILED: {error}"
    log.info("verify_state %s -> %s in %d samples", expect, result.status, result.samples)
    return result.text()


HANDLERS = {"verify_state": tool}


# --- reading the Mac -------------------------------------------------------------------------------------

_FIELDS = ["AXRole", "AXSubrole", "AXRoleDescription", "AXTitle", "AXDescription", "AXPlaceholderValue",
           "AXValue", "AXEnabled", "AXSelected", "AXIdentifier", "AXTitleUIElement", "AXChildren"]
_TEXT_ROLES = {"AXStaticText", "AXHeading", "AXCell"}
_TOGGLES = {"AXCheckBox", "AXRadioButton", "AXSwitch", "AXToggle"}


def _attrs(element) -> dict:
    """The fields of one element in one round trip (falls back to one call each)."""
    import ApplicationServices as AX
    try:
        err, values = AX.AXUIElementCopyMultipleAttributeValues(element, _FIELDS, 0, None)
        if err == 0 and values is not None:
            out = {}
            for name, value in zip(_FIELDS, values):
                if value is not None and not _is_error(value):
                    out[name] = value
            return out
    except Exception:
        pass
    from mint.screen.axkit import attr
    return {name: value for name in _FIELDS if (value := attr(element, name)) is not None}


def _is_error(value) -> bool:
    """CopyMultiple puts an AXValue of the error type where an attribute is missing."""
    try:
        import ApplicationServices as AX
        return type(value).__name__ == "AXValueRef" and AX.AXValueGetType(value) == AX.kAXValueAXErrorType
    except Exception:
        return False


def _text(value) -> str | None:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def element_info(fields: dict, title_text: str = "") -> dict:
    """An element's fields as the checks see them (pure)."""
    role = str(fields.get("AXRole") or "")
    raw = fields.get("AXValue")
    value = _text(raw)
    parts = [fields.get(k) for k in ("AXTitle", "AXDescription", "AXPlaceholderValue")] + [title_text]
    if role in _TEXT_ROLES and isinstance(raw, str):
        parts.append(raw)
    label = []
    for part in parts:
        part = " ".join(str(part or "").split()) if isinstance(part, str) else ""
        if part and part.lower() not in (p.lower() for p in label):
            label.append(part)
    selected = fields.get("AXSelected")
    if role in _TOGGLES or fields.get("AXSubrole") in _TOGGLES:
        if isinstance(raw, (bool, int, float)) and not isinstance(selected, bool):
            selected = bool(raw)
    return {"role": role, "subrole": str(fields.get("AXSubrole") or ""),
            "roledesc": str(fields.get("AXRoleDescription") or ""), "label": " ".join(label),
            "ident": str(fields.get("AXIdentifier") or ""), "value": value,
            "enabled": fields["AXEnabled"] if isinstance(fields.get("AXEnabled"), bool) else None,
            "selected": selected if isinstance(selected, bool) else None}


def _read_elements(pid: int, limit: int = NODES, seconds: float = WALK_SECONDS) -> tuple[list[dict], bool]:
    """Every element in the app's windows (dialogs and sheets included), breadth first, within a node and a
    time budget. The flag says whether the walk reached the end (absence is only proven then)."""
    import ApplicationServices as AX

    from mint.screen import axkit
    app = AX.AXUIElementCreateApplication(pid)
    try:
        AX.AXUIElementSetMessagingTimeout(app, AX_TIMEOUT)
    except Exception:
        pass
    roots = axkit.windows_front_to_back(pid)
    if not roots:
        if not _ax_readable(pid):
            raise PermissionError("the app's accessibility can't be read")
        return [], True
    out, queue = [], deque(roots)
    deadline = time.monotonic() + seconds
    while queue and len(out) < limit and time.monotonic() < deadline:
        node = queue.popleft()
        fields = _attrs(node)
        title_text = ""
        title_el = fields.get("AXTitleUIElement")
        if title_el is not None:
            title_text = str(axkit.attr(title_el, "AXValue") or axkit.attr(title_el, "AXTitle") or "")
        out.append(element_info(fields, title_text))
        queue.extend(fields.get("AXChildren") or [])
    return out, not queue


def _ax_readable(pid: int) -> bool:
    """Does the app answer Accessibility at all? An app with no windows still has a role; without the
    permission (or when it hangs) nothing does - and an empty answer must not read as "nothing there"."""
    import ApplicationServices as AX

    from mint.screen.axkit import attr
    return bool(attr(AX.AXUIElementCreateApplication(pid), "AXRole"))


def _web_text(pid: int, text: str) -> bool | None:
    """AX search for visible text in the app's web areas (Chrome, Safari, Electron)."""
    from mint.screen.axkit import windows_front_to_back
    from mint.tools.documents import _find_web_area
    from mint.tools.harness import _search
    seen = None
    for window in windows_front_to_back(pid)[:3]:
        web = _find_web_area(window)
        if web is None:
            continue
        import ApplicationServices as AX
        predicate = {"AXSearchKey": "AXAnyTypeSearchKey", "AXVisibleOnly": True, "AXResultsLimit": 1,
                     "AXDirection": "AXDirectionNext", "AXImmediateDescendantsOnly": False, "AXSearchText": text}
        try:
            err, values = AX.AXUIElementCopyParameterizedAttributeValue(
                web, "AXUIElementsForSearchPredicate", predicate, None)
        except Exception:
            err, values = 1, None
        if err == 0:
            if values:
                return True
            seen = False
        elif _search(web, text, limit=1):
            return True
    return seen


def _cg_windows() -> list[dict] | None:
    try:
        import Quartz
        raw = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID)
    except Exception:
        return None
    if raw is None:
        return None
    out = []
    for w in raw:
        bounds = w.get("kCGWindowBounds") or {}
        if float(w.get("kCGWindowAlpha", 1) or 0) <= 0:
            continue
        out.append({"id": int(w.get("kCGWindowNumber", 0)), "pid": int(w.get("kCGWindowOwnerPID", 0)),
                    "app": str(w.get("kCGWindowOwnerName") or ""), "title": str(w.get("kCGWindowName") or ""),
                    "layer": int(w.get("kCGWindowLayer", 0)), "x": float(bounds.get("X", 0)),
                    "y": float(bounds.get("Y", 0)), "w": float(bounds.get("Width", 0)),
                    "h": float(bounds.get("Height", 0))})
    return out


def _ocr_window(pid: int) -> list[str] | None:
    """The text of the app's front window. When that window is the top one, a quick grab of its area;
    when something covers it, `screencapture -l` takes just that window (read-only, no sound)."""
    from mint.screen import ocr
    windows = _cg_windows()
    if windows is None:
        return None
    own = os.getpid()
    ordinary = [w for w in windows if w["layer"] == 0 and w["pid"] != own and w["w"] > 60 and w["h"] > 40]
    mine = next((w for w in ordinary if w["pid"] == pid), None)
    if mine is None:
        return None
    area = {"left": int(mine["x"]), "top": int(mine["y"]), "width": int(mine["w"]), "height": int(mine["h"])}
    try:
        from mint.screen import capture
        shot = capture.window(mine["id"])        # that window alone, pixel-checked
        return [str(i.get("text", "")) for i in ocr.recognize(shot["image"], area)]
    except Exception as error:
        log.debug("window capture failed (%s); screen grab or screencapture instead", error)
    if ordinary and ordinary[0]["id"] == mine["id"]:
        items = ocr.read_area(mine["x"], mine["y"], mine["w"], mine["h"])
        return [str(i.get("text", "")) for i in items]
    with tempfile.TemporaryDirectory(prefix="mint-verify-") as folder:
        path = Path(folder) / "window.png"
        done = subprocess.run(["screencapture", "-x", "-o", "-l", str(mine["id"]), str(path)],
                              capture_output=True, timeout=5, check=False)
        if done.returncode != 0 or not path.exists():
            return None
        import PIL.Image
        with PIL.Image.open(path) as image:
            items = ocr.recognize(image.convert("RGB"), area)
    return [str(i.get("text", "")) for i in items]


# --- the window-change detector ------------------------------------------------------------------------

@dataclass
class Snap:
    windows: dict = field(default_factory=dict)      # id -> {pid, app, title, layer}
    front: dict | None = None                         # {pid, name}
    focused: dict = field(default_factory=dict)       # pid -> focused window title ('' when untitled)
    sheets: dict = field(default_factory=dict)        # pid -> [sheet titles] on its focused window
    names: dict = field(default_factory=dict)         # pid -> app name
    scope: tuple = ()                                 # the apps whose windows count (see diff)


def before(pids=()) -> Snap:
    """What is open now: the windows, the app in front and its focused window (plus `pids`' - the app being
    acted on). Cheap (~5-20 ms); call right before the action. Only the windows of `pids` (or, without
    them, of the app in front now) are reported later."""
    snap = _snap(tuple(pids))
    own = os.getpid()
    snap.scope = tuple(p for p in pids if p and p != own) or ((snap.front["pid"],) if snap.front else ())
    return snap


def after(snap: Snap, settle: float = 0.35, interval: float = 0.07) -> str:
    """What changed since `before` - waiting up to `settle` s for a window or sheet to appear. '' when
    nothing did."""
    pids = tuple(snap.focused)
    deadline = time.monotonic() + max(0.0, settle)
    while True:
        line = diff(snap, _snap(pids))
        if line or time.monotonic() >= deadline:
            return line
        time.sleep(interval)


def _snap(pids: tuple) -> Snap:
    snap = Snap()
    own = os.getpid()
    for w in _cg_windows() or []:
        if w["pid"] == own or w["layer"] != 0 or w["w"] < 60 or w["h"] < 40:
            continue                                  # Mint's own overlays, tooltips, the menu bar
        snap.windows[w["id"]] = {"pid": w["pid"], "app": w["app"], "title": w["title"]}
        snap.names[w["pid"]] = w["app"]
    try:
        front = Reader().front()
    except Exception:
        front = None
    snap.front = front
    watch = set(pids)
    if front and front["pid"] != own:
        watch.add(front["pid"])
        snap.names[front["pid"]] = front["name"]
    for pid in watch:
        if pid == own:
            continue
        title, sheets = _focused(pid)
        if title is not None:
            snap.focused[pid] = title
            snap.sheets[pid] = sheets
    return snap


def _focused(pid: int) -> tuple[str | None, list[str]]:
    """(title of the app's focused window, titles of sheets on it) or (None, []) when unreadable."""
    try:
        import ApplicationServices as AX

        from mint.screen import axkit
        app = AX.AXUIElementCreateApplication(pid)
        try:
            AX.AXUIElementSetMessagingTimeout(app, 0.5)
        except Exception:
            pass
        window = axkit.attr(app, "AXFocusedWindow")
        if window is None:
            return ("" if axkit.attr(app, "AXRole") else None), []
        sheets = []
        for child in axkit.attr(window, "AXChildren") or []:
            if axkit.attr(child, "AXRole") == "AXSheet":
                sheets.append(_sheet_name(child))
        return str(axkit.attr(window, "AXTitle") or ""), sheets
    except Exception:
        return None, []


def _sheet_name(sheet) -> str:
    """A sheet has no title of its own: its first words or its default button name it ('Save')."""
    from mint.screen import axkit
    title = axkit.attr(sheet, "AXTitle") or axkit.attr(sheet, "AXDescription")
    if title:
        return str(title)
    button = axkit.attr(sheet, "AXDefaultButton")
    if button is not None and axkit.attr(button, "AXTitle"):
        return str(axkit.attr(button, "AXTitle"))
    for node in axkit.walk(sheet, limit=40, depth=3):
        if axkit.attr(node, "AXRole") == "AXStaticText" and axkit.attr(node, "AXValue"):
            return str(axkit.attr(node, "AXValue"))[:50]
    return ""


def diff(a: Snap, b: Snap) -> str:
    """One short line of what changed between two snapshots, '' when nothing did (pure).

    Windows and sheets count only for the apps in `a.scope` (when set): other apps' windows come and go
    in the list when the stacking order or the Space changes - not the action's doing - and their titles
    don't belong in a result. A change of the front app is always reported (by app name)."""
    parts: list[str] = []
    scope = set(a.scope)

    def mine(pid) -> bool:
        return not scope or pid in scope

    def name(pid) -> str:
        return b.names.get(pid) or a.names.get(pid) or "another app"

    sheet_pids = set()
    for pid, sheets in b.sheets.items():
        if pid not in a.sheets or not mine(pid):
            continue
        for title in sheets:
            if title not in a.sheets[pid]:
                parts.append(f"new sheet '{_seen(title)}' in {name(pid)}" if title else f"new sheet in {name(pid)}")
                sheet_pids.add(pid)
        for title in a.sheets[pid]:
            if title not in sheets:
                parts.append(f"sheet '{_seen(title)}' closed in {name(pid)}" if title
                             else f"a sheet closed in {name(pid)}")
                sheet_pids.add(pid)
    for wid, w in b.windows.items():
        if wid not in a.windows and mine(w["pid"]) and not (w["pid"] in sheet_pids and not w["title"]):
            parts.append(f"new window '{_seen(w['title'])}' in {w['app']}" if w["title"]
                         else f"new window in {w['app']}")
    for wid, w in a.windows.items():
        if wid not in b.windows and mine(w["pid"]) and not (w["pid"] in sheet_pids and not w["title"]):
            parts.append(f"window '{_seen(w['title'])}' closed in {w['app']}" if w["title"]
                         else f"a window closed in {w['app']}")
    if a.front and b.front and a.front["pid"] != b.front["pid"]:
        parts.append(f"front app changed to {b.front['name'] or 'another app'}")
    elif b.front and b.front["pid"] in a.focused and b.front["pid"] in b.focused and mine(b.front["pid"]):
        pid = b.front["pid"]
        was, now = a.focused[pid], b.focused[pid]
        new_titles = {w["title"] for wid, w in b.windows.items() if wid not in a.windows}
        if was != now and now not in new_titles:
            parts.append(f"focused window now '{_seen(now)}' in {name(pid)}" if now
                         else f"focused window changed in {name(pid)}")
    if len(parts) > 4:
        parts = parts[:4] + [f"{len(parts) - 4} more changes"]
    return "; ".join(parts)
