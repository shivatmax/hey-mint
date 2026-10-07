"""Choosing the target: a closed set for Jev, a zoomed second look, exact coordinate maths.

Three things grounding needs that are easy to get subtly wrong, kept here so they can be
tested without a screen:

  1. Jev as a strict chooser. Jev is given at most CAP fully specified actions ("click button
     'Save' in 'Note editor'"), plus "reobserve" and "abstain", and a summary of the screen
     (which fields are empty, which dialog is open, what has focus, whether Save is enabled).
     Ids come from role + label + where the control sits, never from a list position, so the
     same control keeps its id between looks. Deleting, sending, buying and closing are left
     out: those go through the guard and the explicit paths. An answer that is not in the
     table, ties two lookalikes, or is below FLOOR is not used - the next chooser gets a turn
     instead of a guess. (The idea is Cua's jev-use recipe, rewritten for Mint's inventory.)
  2. Zoom. A small or doubtful pick from the vision model gets a second look: the same capture
     cropped around it with 20% padding, at most ZOOM_WIDTH px wide, and the answer mapped
     back to screen points exactly.
  3. Normalised coordinates. Gemini points in 0-999 across the image it was sent, so the image
     is downscaled here (the scale recorded) and v/1000*size is rounded and clamped.

And locate(): click_at names its target; this finds it - Accessibility by name, then the
screen's text near the hint, then the vision model - and remembers the answer for the same
screenshot, so asking twice clicks the same place.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("mint.screen.choose")

CAP = 24            # action candidates Jev is shown (Cua measured 1,680/1,680 correct at 4-24)
FLOOR = 0.55        # Jev confidence below this is not acted on
TIE = 0.15          # two lookalikes this close in probability: neither is used
RESERVED = {
    "reobserve": "The screen may still be changing (loading, a menu or dialog opening) and the "
                 "target is not listed yet: look again before acting.",
    "abstain": "None of the listed actions does what the request asks. Choose this rather than "
               "a lookalike: 'Save draft' is not 'Save', 'New folder' is not 'New file'.",
}

# Left out of Jev's table whatever the request says: these go through the guard (delete),
# ui_act's send check (send), or an explicit request (buying, closing).
RISKY = re.compile(
    r"\b(delete|remove|erase|trash|bin|discard|uninstall|wipe|revoke|deactivate|empty|"
    r"send|send now|reply all|post|publish|"
    r"buy|purchase|pay|checkout|check out|place order|order now|subscribe|upgrade plan|"
    r"close|quit|exit|log out|logout|sign out|end call|leave)\b", re.I)

_ROLE_CLASS = {
    "AXButton": "button", "AXLink": "link", "AXMenuButton": "popup", "AXPopUpButton": "popup",
    "AXCheckBox": "checkbox", "AXSwitch": "toggle", "AXRadioButton": "radio", "AXTab": "tab",
    "AXMenuItem": "menu_item", "AXMenuBarItem": "menu_item", "AXTextField": "text_input",
    "AXTextArea": "text_input", "AXComboBox": "text_input", "AXSearchField": "text_input",
    "AXCell": "row", "AXRow": "row", "AXDisclosureTriangle": "disclosure", "AXSlider": "slider",
    "AXIncrementor": "stepper", "AXStaticText": "text", "AXImage": "image", "AXGroup": "group",
    "AXHeading": "heading", "AXDockItem": "dock_item", "AXColorWell": "color_well",
}
_VERB = {"text_input": "type into", "checkbox": "toggle", "toggle": "toggle", "popup": "open",
         "disclosure": "expand", "slider": "set"}
_KIND = {"text_input": "text field", "menu_item": "menu item", "popup": "pop-up menu",
         "dock_item": "Dock item", "color_well": "colour well"}
_SUBMIT = {"submit", "save", "create", "done", "ok", "continue", "next", "apply", "confirm", "add",
           "send", "search", "go", "sign", "log", "upload", "install", "update", "finish"}
_STOP = {"the", "a", "an", "on", "in", "into", "at", "of", "to", "and", "for", "with", "my", "this",
         "that", "please", "click", "press", "tap", "select", "choose", "open", "button", "link",
         "field", "box", "icon", "menu", "item", "tab", "option", "it", "is", "then", "type"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def slug(text: str, limit: int = 40) -> str:
    return "-".join(_words(text))[:limit].strip("-") or "unnamed"


def role_class(role: str) -> str:
    return _ROLE_CLASS.get(role or "", (role or "").removeprefix("AX").lower() or "control")


def risky(label: str) -> bool:
    return bool(RISKY.search(label or ""))


def _where(box, area) -> str:
    if not area or not box:
        return ""
    x, y, w, h = box
    ax, ay, aw, ah = area
    cx, cy = (x + w / 2 - ax) / max(aw, 1), (y + h / 2 - ay) / max(ah, 1)
    v = "top" if cy < 0.33 else ("bottom" if cy > 0.66 else "middle")
    hz = "left" if cx < 0.33 else ("right" if cx > 0.66 else "centre")
    return f"{v} {hz}"


# --- 1. the closed set ----------------------------------------------------------------------

@dataclass
class Candidate:
    id: str
    action: str                  # click / type into / toggle / open / expand / set
    description: str
    element: dict = field(repr=False, compare=False)


@dataclass
class Table:
    candidates: list[Candidate]
    dropped: int = 0             # eligible but over CAP
    excluded: dict = field(default_factory=dict)    # reason -> count

    def by_id(self) -> dict[str, Candidate]:
        return {c.id: c for c in self.candidates}

    def criteria(self) -> dict[str, str]:
        out = {c.id: c.description for c in self.candidates}
        out.update(RESERVED)
        return out


def _label(e: dict) -> str:
    return " ".join((e.get("label") or e.get("hint") or "").split())


def _describe(e: dict, klass: str, area) -> str:
    """What the action does, value-free: a field says empty or has text, never what it holds."""
    label = _label(e)
    verb = _VERB.get(klass, "click")
    text = f"{verb} {_KIND.get(klass, klass.replace('_', ' '))}" + (f" '{label[:60]}'" if label else " (no label)")
    if e.get("hint") and e.get("hint") != label:
        text += f" (shows '{e['hint'][:40]}')"
    if e.get("within"):
        text += f" in '{e['within'][:50]}'"
    extra = [_where(e.get("box"), area)]
    if klass == "text_input":
        extra.append("has text" if e.get("value") else "empty")
    if e.get("in_dialog"):
        extra.append("in the open dialog")
    if e.get("focused"):
        extra.append("focused")
    return text + f" ({', '.join(x for x in extra if x)})"


def _path(e: dict, area) -> tuple[str, ...]:
    """Where a control sits, outermost first: the dialog, the named group around it, the
    region of the window. Stands in for the ancestor path Cua's driver reports."""
    dialog = e.get("dialog") or {}
    return (str(dialog.get("title") or "") if e.get("in_dialog") else "",
            str(e.get("within") or ""), _where(e.get("box"), area))


def _stems(text: str) -> set[str]:
    return {w[:4] for w in _words(text) if w not in _STOP and len(w) > 2}


def _rank_key(e: dict, plan_words: set, goal_words: set, score: float):
    have = _stems(_label(e) + " " + str(e.get("within") or ""))
    return (len(plan_words & have) > 0, len(goal_words & have), score, bool(e.get("in_dialog")),
            bool(e.get("interactive")))


PRERANK_MAX = 48     # options in the one ranking call made when there are more than CAP


def build(pool: list[dict], goal: str = "", area=None, plan: list[str] | None = None,
          cap: int = CAP, allow_risky: bool = False, prerank=None) -> Table:
    """The candidate table for Jev, from ground's element dicts (`pool`).

    Over `cap`: controls named by the plan's steps first, then those sharing words with `goal`,
    then - when `prerank(goal, {id: description}) -> {id: score}` is given - the ones it scores
    highest (Jev's ranking: "attach a file" shares no word with "Add files and more"), then
    screen order. The kept ones are still shown in screen order, and ids are made before the cut,
    so a control keeps its id whatever else is on screen."""
    excluded: dict[str, int] = {}

    def skip(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    eligible = []
    for e in pool:
        klass = role_class(e.get("role", ""))
        label = _label(e)
        if e.get("enabled") is False:
            skip("disabled")
        elif not label and klass != "text_input":
            skip("unlabeled")
        elif not allow_risky and risky(label):
            skip("risky")
        else:
            eligible.append((e, klass))

    bases = [f"{klass}:{slug(_label(e))}" for e, klass in eligible]
    counts: dict[str, int] = {}
    for base in bases:
        counts[base] = counts.get(base, 0) + 1
    paths = [_path(e, area) for e, _ in eligible]
    seen: dict[str, int] = {}
    out: list[Candidate] = []
    for index, ((e, klass), base) in enumerate(zip(eligible, bases)):
        cid = ""
        if counts[base] > 1:
            # Readable when the row, the dialog or the place tells them apart
            # ("button:install@csv-colorful-table"); otherwise a short hash of the path and how
            # many identical ones came before it - never the position in the list.
            group = [i for i, b in enumerate(bases) if b == base]
            for level in (1, 0, 2):                      # row, dialog, place
                suffix = slug(paths[index][level], 28) if paths[index][level] else ""
                if suffix and sum(1 for i in group if paths[i][level] and
                                  slug(paths[i][level], 28) == suffix) == 1:
                    cid = f"{base}@{suffix}"
                    break
            if not cid:
                key = repr((paths[index], base))
                ordinal = seen.get(key, 0)
                seen[key] = ordinal + 1
                cid = f"{base}@{hashlib.sha1(f'{key}|{ordinal}'.encode()).hexdigest()[:4]}"
        out.append(Candidate(cid or base, _VERB.get(klass, "click"), _describe(e, klass, area), e))
    ids = [c.id for c in out]
    if len(set(ids)) != len(ids):        # never send Jev two options with one id
        out = [c for i, c in enumerate(out) if c.id not in ids[:i]]
        skip("duplicate_id")

    dropped = 0
    if len(out) > cap:
        plan_words = {w for step in (plan or []) for w in _stems(step)}
        goal_words = _stems(goal)
        scores: dict[str, float] = {}
        if prerank is not None:
            lexical = sorted(out, key=lambda c: _rank_key(c.element, plan_words, goal_words, 0.0), reverse=True)
            try:
                scores = prerank(goal, {c.id: c.description for c in lexical[:PRERANK_MAX]}) or {}
            except Exception as error:
                log.info("jev table: pre-rank failed: %s", error)
        order = sorted(range(len(out)), reverse=True,
                       key=lambda i: _rank_key(out[i].element, plan_words, goal_words, scores.get(out[i].id, 0.0)))
        keep = set(order[:cap])
        dropped = len(out) - cap
        out = [c for i, c in enumerate(out) if i in keep]
        log.info("jev table: kept %d, dropped %d", cap, dropped)
    return Table(out, dropped, excluded)


_NAME = re.compile(r"[\"“'‘]([^\"”'’]+)[\"”'’]|([A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+|[A-Za-z]*\d[A-Za-z0-9]*)|(\b[A-Z][\w]*)")


# What a request calls a control rather than its name ("the Size pop-up", "the Remember me check
# box"): never a word the control must show. Phrases are taken out before single words.
DESCRIPTORS = ("pop-up menu", "pop up menu", "popup menu", "drop-down menu", "drop down menu", "pop-up", "pop up",
               "drop-down", "drop down", "check box", "check-box", "text field", "text box", "text area",
               "combo box", "radio button", "menu item", "menu button", "popup", "dropdown", "menu", "button",
               "checkbox", "toggle", "switch", "field", "box", "tab", "link", "icon", "slider", "item", "option",
               "input", "textbox", "textarea", "control", "image", "entry", "bar", "row", "list", "label", "area")
_DESCRIPTOR_NAMES = {" ".join(_words(d)) for d in DESCRIPTORS}
_DESCRIPTOR_PHRASES = re.compile(r"\b(" + "|".join(re.escape(d).replace(r"\ ", r"[\s-]+").replace(r"\-", r"[\s-]*")
                                                  for d in DESCRIPTORS if " " in d or "-" in d) + r")\b", re.I)
_DESCRIPTOR_WORDS = {d for d in DESCRIPTORS if " " not in d and "-" not in d}
# Words of a request that are not part of any control's name: articles, pronouns, prepositions,
# the verbs of asking, and where it is ("in the sidebar", "the first one").
_PLAIN = {"the", "a", "an", "this", "that", "these", "those", "it", "its", "my", "your", "our", "their", "them",
          "on", "in", "into", "onto", "at", "of", "to", "for", "from", "with", "by", "near", "under", "above",
          "below", "beside", "inside", "within", "and", "or", "then", "please", "there", "here", "one", "thing",
          "called", "named", "labelled", "labeled", "says", "saying", "which", "where", "is", "i", "you", "can",
          "could", "would", "will", "just", "want", "me",
          "click", "press", "tap", "hit", "push", "select", "choose", "pick", "open", "show", "tick", "untick",
          "check", "uncheck", "toggle", "turn", "enable", "disable", "use", "type", "enter", "fill", "put",
          "write", "go", "find", "start", "make", "do", "expand", "collapse", "view", "see", "set",
          "top", "bottom", "left", "right", "middle", "centre", "center", "corner", "upper", "lower", "side",
          "sidebar", "toolbar", "header", "footer", "window", "screen", "page", "panel", "pane", "dialog",
          "sheet", "app", "first", "second", "third", "last"}


def names(target: str) -> list[str]:
    """The proper names in a request - quoted text, hyphenated or numbered words, Capitalised words
    after the first - which a control that is really the target must mention. What the request
    calls the control ("pop-up", "drop-down", "check box") is not a name."""
    found = []
    for m in _NAME.finditer(target or ""):
        quoted, joined, capital = m.groups()
        if quoted or joined:
            if " ".join(_words(quoted or joined)) not in _DESCRIPTOR_NAMES:
                found.append(quoted or joined)
        elif capital and len(capital) > 1 and target[:m.start()].strip() and capital.lower() not in _STOP \
                and capital.lower() not in _DESCRIPTOR_NAMES:
            found.append(capital)
    return found


def content_words(text: str) -> list[str]:
    """The words of a request (or a label) that can name a control: without what it is called
    (button, pop-up, check box), where it is, the verb of asking, articles and prepositions."""
    plain = _DESCRIPTOR_PHRASES.sub(" ", text or "")
    return [w for w in _words(plain) if w not in _PLAIN and w not in _DESCRIPTOR_WORDS]


def _has(word: str, words: set) -> bool:
    """`word` is among `words`, or one is the other plus an ending (draft/drafts, install/installed).
    Numbers only exactly: 'Page 2' is not 'Page 12'."""
    if word in words:
        return True
    if len(word) < 4:
        return False
    return any(len(w) >= 4 and not (w.isdigit() and word.isdigit()) and (w.startswith(word) or word.startswith(w))
               for w in words)


def _known(e: dict) -> set:
    """Every word a control shows or is shown with: its label, row, hint and tooltip."""
    return set(_words(" ".join(str(e.get(k) or "") for k in ("label", "within", "hint", "help"))))


def unmatched(goal: str, pick: dict, table: Table) -> tuple[list[str], bool]:
    """The request's words that the pick leaves out AND no other listed control has either - words
    for something that is not on screen - and whether one of them is stuck onto the pick's name in
    the request: "Save all" (the pick 'Save', then 'all'), "Install SQL Formatter". A word some other
    control has ('as' in "save it as a draft", with 'Save as' listed) was Jev's to weigh."""
    want = content_words(goal)
    have = _known(pick)
    missing = [w for w in want if not _has(w, have)]
    if not missing:
        return [], False
    elsewhere = set()
    for c in table.candidates:
        elsewhere |= _known(c.element)
    absent = [w for w in missing if not _has(w, elsewhere)]
    own = content_words(_label(pick))
    said = [w for w in _words(_DESCRIPTOR_PHRASES.sub(" ", goal)) if w not in ("the", "a", "an")]
    stuck = False
    for i in range(len(said) - len(own) + 1 if own else 0):
        if all(_has(w, {x}) for w, x in zip(own, said[i:i + len(own)])):
            around = said[i - 1:i] + said[i + len(own):i + len(own) + 1]
            stuck = stuck or any(w in absent for w in around)
    return absent, stuck


def vet(goal: str, choice: "Choice", table: Table) -> "Choice":
    """Jev's pick checked against the words of the request: a pick whose name has a word stuck on it
    in the request that nothing on screen has - 'Save' for "Save all" - is a near miss, not the
    target, and is not used however sure Jev is (0.86-0.91 in the fixture bench, where every one of
    those picks was wrong). Words further off ("the microphone to dictate", "the choose project
    button under the message box") describe the control; Jev's own floor decides those - on the
    ChatGPT replay right picks of that kind came at 0.66-0.84, so no confidence bar separates them."""
    if choice.kind != "act" or choice.candidate is None:
        return choice
    absent, stuck = unmatched(goal, choice.candidate.element, table)
    if not stuck:
        return choice
    label = _label(choice.candidate.element)[:40]
    named = [n for n in names(goal) if set(_words(n)) & set(absent)]
    words = ", ".join(named[:2]) if named else " ".join(absent[:3])
    return Choice("rejected", None, choice.confidence, choice.top,
                  f"'{label}' is only part of the request: nothing listed says '{words}'", choice.seconds)


def missing_names(target: str, element: dict) -> str:
    """The first name in `target` that the element's label, row, hint and value all leave out, or ''."""
    have = set(_words(" ".join(str(element.get(k) or "") for k in ("label", "within", "hint", "value"))))
    have |= {w[:4] for w in have}
    for name in names(target):
        if not all(w in have or w[:4] in have for w in _words(name)):
            return name
    return ""


def state_summary(inv: dict, limit: int = 8) -> dict:
    """What a person would notice before acting, precomputed so Jev need not infer it."""
    elements = inv.get("elements") or []
    summary: dict = {"app": inv.get("app", ""), "window": (inv.get("title") or "")[:60]}
    if inv.get("dialog") is not None:
        kind = "menu" if inv.get("dialog_soft") else "dialog"
        summary["open"] = f"{kind} '{(inv.get('dialog_title') or kind)[:50]}' (only it can be used)"
    else:
        summary["open"] = "no dialog"
    focused = next((e for e in elements if e.get("focused")), None)
    summary["focused"] = (f"{_KIND.get(role_class(focused.get('role', '')), role_class(focused.get('role', '')))} "
                          f"'{_label(focused)[:40]}'") if focused else "nothing"
    fields, buttons = {}, {}
    ordered = sorted(elements, key=lambda e: not e.get("in_dialog"))
    for e in ordered:
        klass = role_class(e.get("role", ""))
        name = _label(e)[:40]
        if klass == "text_input" and name and len(fields) < limit:
            fields[name] = "filled" if e.get("value") else "empty"
        elif klass == "button" and name and len(buttons) < limit and set(_words(name)[:1]) & _SUBMIT:
            buttons[name] = "enabled" if e.get("enabled") is not False else "disabled"
    if fields:
        summary["fields"] = fields
    if buttons:
        summary["submit_buttons"] = buttons
    return summary


@dataclass
class Choice:
    kind: str                    # act / abstain / reobserve / rejected / unavailable
    candidate: Candidate | None
    confidence: float
    top: list                    # [(id, probability)] best first, reserved ids left out
    why: str
    seconds: float = 0.0

    def closest(self, table: Table, n: int = 3) -> str:
        by_id = table.by_id()
        return "; ".join(f"{by_id[i].description[:70]} ({p:.2f})" for i, p in self.top[:n] if i in by_id)


INSTRUCTIONS = (
    "`request` names one control the user wants used on this screen of a Mac app; `screen` "
    "summarises the screen. Each listed id is one complete action. Choose the ONE whose control "
    "is exactly what the request names or describes - match the label's meaning, its row or "
    "dialog and its place. A near-miss is wrong: 'Save draft' is not 'Save', 'Add folder' is "
    "not 'Add file'. Choose reobserve if the target is likely to appear in a moment; choose "
    "abstain if nothing listed is it.")


def decide(answer: dict | None, table: Table, floor: float = FLOOR, seconds: float = 0.0) -> Choice:
    """Check Jev's answer against the table it was given; anything doubtful is not an action."""
    if not isinstance(answer, dict) or "choice" not in answer:
        return Choice("unavailable", None, 0.0, [], "Jev did not answer", seconds)
    by_id = table.by_id()
    chosen = answer.get("choice")
    try:
        confidence = float(answer.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    probabilities = answer.get("probabilities") or {}
    top = []
    for key, value in probabilities.items() if isinstance(probabilities, dict) else []:
        try:
            if key in by_id:
                top.append((key, float(value)))
        except (TypeError, ValueError):
            continue
    top.sort(key=lambda kv: -kv[1])
    if chosen in ("abstain", "reobserve"):
        return Choice(chosen, None, confidence, top, f"Jev chose {chosen} ({confidence:.2f})", seconds)
    if chosen not in by_id:
        return Choice("rejected", None, confidence, top, f"Jev answered an id it was not given ({str(chosen)[:40]})",
                      seconds)
    if confidence < floor:
        return Choice("rejected", None, confidence, top,
                      f"Jev was only {confidence:.2f} sure of '{_label(by_id[chosen].element)[:40]}'", seconds)
    pick = by_id[chosen]
    rival = next(((i, p) for i, p in top if i != chosen), None)
    mine = dict(top).get(chosen, confidence)
    if rival is not None and mine - rival[1] < TIE and \
            _words(_label(by_id[rival[0]].element)) == _words(_label(pick.element)):
        # Two controls with the same name and Jev nearly split between them: a coin toss.
        return Choice("rejected", None, confidence, top,
                      f"Jev split between two '{_label(pick.element)[:40]}' ({mine:.2f} vs {rival[1]:.2f})", seconds)
    return Choice("act", pick, confidence, top, f"Jev picked it ({confidence:.2f} confident)", seconds)


def ask_jev(goal: str, table: Table, state: dict | None = None, timeout: float = 6.0,
            floor: float = FLOOR, ask=None) -> Choice:
    """One Jev call over the table. `ask` stands in for jev.ask in tests."""
    if not table.candidates:
        return Choice("abstain", None, 0.0, [], "nothing to choose from")
    if ask is None:
        from mint.core import jev
        if not jev.available():
            return Choice("unavailable", None, 0.0, [], "Jev has no key")
        ask = jev.ask
    started = time.monotonic()
    answers = ask({"request": goal, "screen": state or {}},
                  {"pick": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": table.criteria()}},
                  timeout=timeout)
    took = time.monotonic() - started
    choice = decide((answers or {}).get("pick") if isinstance(answers, dict) else None, table, floor, took)
    return vet(goal, choice, table)


# --- 2. zoom ---------------------------------------------------------------------------------

ZOOM_WIDTH = 500          # px, the widest a zoom image is sent
ZOOM_PAD = 0.2            # added on each side
SMALL = 24.0              # points: a pick smaller than this gets a second look
ZOOM_MIN = (140.0, 80.0)  # points: enough around a tiny icon to see what it belongs to


def denorm(v: float, size: int) -> int:
    """A Gemini coordinate (0-999 across `size` pixels) -> a pixel, clamped to the image."""
    try:
        return max(0, min(int(size) - 1, int(round(float(v) / 1000 * size))))
    except (TypeError, ValueError, OverflowError):
        return 0


def prepare(image, max_side: int = 1600):
    """Downscale before sending, so the size the model sees is known: -> (image, ratio sent/original)."""
    w, h = image.size
    if max(w, h) <= max_side:
        return image, 1.0
    ratio = max_side / max(w, h)
    return image.resize((max(1, round(w * ratio)), max(1, round(h * ratio)))), ratio


def zoom_region(box, area, pad: float = ZOOM_PAD, minimum=ZOOM_MIN) -> tuple[float, float, float, float]:
    """Points to crop around `box`: at least `minimum`, plus `pad` on each side, inside `area`."""
    x, y, w, h = box
    cx, cy = x + w / 2, y + h / 2
    w, h = max(w, minimum[0]), max(h, minimum[1])
    w, h = w * (1 + 2 * pad), h * (1 + 2 * pad)
    ax, ay, aw, ah = area
    w, h = min(w, aw), min(h, ah)
    left = min(max(cx - w / 2, ax), ax + aw - w)
    top = min(max(cy - h / 2, ay), ay + ah - h)
    return left, top, w, h


@dataclass
class Zoom:
    """A crop of a capture of `area` (taken at `scale` px per point), resized for the model."""
    area: tuple
    scale: float
    crop: tuple          # (x0, y0, x1, y1) px in the capture
    size: tuple          # (w, h) px of the image sent

    def to_screen(self, nx: float, ny: float) -> tuple[float, float]:
        """0-999 on the zoom image -> screen points."""
        x0, y0, x1, y1 = self.crop
        zw, zh = self.size
        px = x0 + (denorm(nx, zw) + 0.5) * (x1 - x0) / zw
        py = y0 + (denorm(ny, zh) + 0.5) * (y1 - y0) / zh
        return self.area[0] + px / self.scale, self.area[1] + py / self.scale

    def contains(self, point) -> bool:
        x0, y0, x1, y1 = self.crop
        px = (point[0] - self.area[0]) * self.scale
        py = (point[1] - self.area[1]) * self.scale
        return x0 <= px <= x1 and y0 <= py <= y1


def plan_zoom(region, area, scale: float, image_size, width: int = ZOOM_WIDTH) -> Zoom:
    """The pixel crop for `region` (points) in a capture of `area`, and the size to send it at
    (`width` px wide, up or down, never more than 4x up)."""
    iw, ih = image_size
    x0 = max(0, int((region[0] - area[0]) * scale))
    y0 = max(0, int((region[1] - area[1]) * scale))
    x1 = min(iw, int(-(-(region[0] + region[2] - area[0]) * scale // 1)))
    y1 = min(ih, int(-(-(region[1] + region[3] - area[1]) * scale // 1)))
    x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
    ratio = min(width / (x1 - x0), 4.0)
    return Zoom(tuple(area), scale, (x0, y0, x1, y1),
                (max(1, min(width, round((x1 - x0) * ratio))), max(1, round((y1 - y0) * ratio))))


def zoom_image(image, zoom: Zoom):
    return image.crop(zoom.crop).resize(zoom.size)


def needs_zoom(box, confidence: float | None, small: float = SMALL, sure: float = 0.7) -> bool:
    return max(box[2], box[3]) < small or (confidence is not None and confidence < sure)


# --- 3. click_at: find what it names ----------------------------------------------------------

@dataclass
class Located:
    point: tuple
    how: str              # "accessibility" / "screen text" / "vision" / "cache"
    label: str = ""
    element: dict | None = field(default=None, repr=False)


_CACHE: dict = {}
CACHE_SECONDS = 120.0


def _cache_key(snapshot, target: str):
    return (snapshot, " ".join(_words(target)))


def cached(snapshot, target: str) -> Located | None:
    entry = _CACHE.get(_cache_key(snapshot, target))
    if entry is None or time.monotonic() - entry[0] > CACHE_SECONDS:
        return None
    hit = entry[1]
    return Located(hit.point, "cache", hit.label, hit.element)


def remember(snapshot, target: str, found: Located) -> None:
    if len(_CACHE) > 200:
        _CACHE.clear()
    _CACHE[_cache_key(snapshot, target)] = (time.monotonic(), found)


def forget() -> None:
    _CACHE.clear()


def _distance(point, box) -> float:
    x, y, w, h = box
    dx = max(x - point[0], 0.0, point[0] - (x + w))
    dy = max(y - point[1], 0.0, point[1] - (y + h))
    return (dx * dx + dy * dy) ** 0.5


def by_name(target: str, elements: list[dict], hint=None, radius: float = 220.0) -> dict | None:
    """The element whose name is `target`: exactly its words, or all of them with only role
    words besides. Several: the one nearest the hint, if it is within `radius` and clearly nearer."""
    want = [w for w in _words(target) if w not in _STOP]
    if not want:
        return None
    exact, close = [], []
    for e in elements:
        if e.get("enabled") is False or not e.get("box"):
            continue
        name = _words(_label(e))
        if name == want or name == _words(target):
            exact.append(e)
        elif name and set(want) <= set(name) and len(name) <= len(want) + 1:
            close.append(e)
    found = exact or close
    if not found:
        return None
    if hint is None:
        return found[0] if len(found) == 1 else None
    ranked = sorted(found, key=lambda e: _distance(hint, e["box"]))
    if _distance(hint, ranked[0]["box"]) > radius:
        return None
    if len(ranked) > 1 and _distance(hint, ranked[1]["box"]) - _distance(hint, ranked[0]["box"]) < 24:
        return None          # two of them about as near: the hint cannot decide
    return ranked[0]


def locate(target: str, hint=None, snapshot=None, *, inventory=None, read_text=None, vision=None) -> Located | None:
    """Where to click for `target` (with `hint`, a rough point, when the model gave one).

    The ladder: the cache for this snapshot; Accessibility by name; text read on screen near the
    hint; the vision model over the window's marked controls (which zooms in on small ones).
    `inventory()` -> ground inventory, `read_text(hint, target)` -> (point, text) and
    `vision(target, inv)` -> (element|None, why) are passed in, so this runs without a screen in tests."""
    if not target.strip():
        return None
    if snapshot is not None:
        hit = cached(snapshot, target)
        if hit is not None:
            return hit
    found = None
    inv = None
    if inventory is not None:
        try:
            inv = inventory()
        except Exception as error:
            log.info("locate: inventory failed: %s", error)
    elements = (inv or {}).get("elements") or []
    if elements:
        e = by_name(target, [e for e in elements if e.get("interactive") or e.get("role") == "AXStaticText"], hint)
        if e is not None:
            x, y, w, h = e["box"]
            found = Located((x + w / 2, y + h / 2), "accessibility", _label(e), e)
    if found is None and read_text is not None and hint is not None:
        try:
            point, text = read_text(hint, target)
        except Exception as error:
            log.info("locate: screen text failed: %s", error)
            point, text = hint, ""
        if text:
            found = Located(tuple(point), "screen text", text)
    if found is None and vision is not None and elements:
        try:
            e, why = vision(target, inv)
        except Exception as error:
            e, why = None, str(error)
        if e is not None and e.get("box"):
            x, y, w, h = e.get("point_box") or e["box"]
            found = Located((x + w / 2, y + h / 2), "vision", _label(e), e)
        else:
            log.info("locate: vision found nothing: %s", why[:120])
    if found is not None and snapshot is not None:
        remember(snapshot, target, found)
    return found
