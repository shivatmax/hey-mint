"""The action path: one honest effect per UI action (confirmed / partial / unverified / suspected
no-op / failed), Accessibility before the pointer, typing without the clipboard, and window-change
evidence. Hermetic: every Accessibility call goes to a fake; nothing touches the real screen."""
import types

import pytest

try:
    from mint.screen import axkit, effect, ground
    from mint.tools import everyday as skills
    from mint.tools import fastinput
    from mint.tools import harness as harness_tools
    from mint.ui import effects as fx_mod
except ImportError:
    from mint import axkit, effect, fastinput, ground, harness_tools, skills
    from mint import effects as fx_mod


# --- a fake accessibility tree ---------------------------------------------------------------------

class Node:
    def __init__(self, role, parent=None, actions=(), pid=100, frame=None, **attrs):
        self.attrs = {"AXRole": role, "AXParent": parent, **attrs}
        self.acts, self.pid, self.box = list(actions), pid, frame
        self.press_error = 0
        self.max_len = None            # a field that keeps only this many characters
        self.refuse_write = False      # an app that rejects accessibility writes
        self.selection = None          # (location, length)
        self.on_press = None

    def get(self, name):
        return self.attrs.get(name)


class FakeAX:
    """The methods effect.py asks of Accessibility, over Node objects."""

    def __init__(self, at=None, chromium=False):
        self.at, self.is_chromium = at, chromium
        self.writes, self.presses, self.done = [], [], []

    def attr(self, el, name):
        if el is None or (name == "AXValue" and getattr(el, "hide_value", False)):
            return None
        return el.get(name)

    def set(self, el, name, value):
        self.writes.append((name, value))
        if el.refuse_write and name in ("AXSelectedText", "AXValue"):
            return -25205
        if name == "AXSelectedTextRange":
            el.selection = value
        elif name == "AXSelectedText":
            old = el.attrs.get("AXValue") or ""
            loc, length = el.selection if el.selection is not None else (len(old), 0)
            new = old[:loc] + value + old[loc + length:]
            if el.max_len is not None:
                new = new[:el.max_len]
            el.attrs["AXValue"] = new
            el.selection = (loc + len(value), 0)
        else:
            el.attrs[name] = value
        return 0

    def perform(self, el, action):
        self.presses.append((el.get("AXTitle"), action))
        if el.on_press:
            el.on_press()
        if el.press_error == 0:
            self.done.append((el.get("AXTitle"), action))
        return el.press_error

    def actions(self, el):
        return el.acts

    def pid(self, el):
        return el.pid

    def element_at(self, x, y):
        return self.at

    def frame(self, el):
        return el.box if el is not None else None

    def text_range(self, location, length):
        return (location, length)

    def chromium(self, pid):
        return self.is_chromium


@pytest.fixture
def quick(monkeypatch):
    """No real waiting in the modules under test."""
    clock = {"t": 1000.0}

    def sleep(s):
        clock["t"] += s

    fake_time = types.SimpleNamespace(sleep=sleep, monotonic=lambda: clock["t"], time=lambda: clock["t"])
    monkeypatch.setattr(effect, "time", fake_time)
    monkeypatch.setattr(ground, "time", fake_time)
    monkeypatch.setattr(skills, "time", fake_time)
    return clock


# --- the effect line ------------------------------------------------------------------------------

def test_confirmed_renders_route_delivery_and_evidence():
    line = effect.Effect(effect.CONFIRMED, "Pressed the button '5'", "accessibility", "background",
                         ["the display now reads 5"]).render()
    assert line == ("CONFIRMED via accessibility (background): Pressed the button '5'. "
                    "Evidence: the display now reads 5.")


def test_confirmed_without_evidence_is_only_unverified():
    # The API saying OK is not proof: the contract needs a read-back or a window change.
    e = effect.Effect(effect.CONFIRMED, "Clicked it", "global_input", "foreground", escalation="look")
    assert e.kind == effect.UNVERIFIABLE
    assert e.render().startswith("UNVERIFIED via the real mouse/keyboard (foreground): Clicked it.")
    assert e.render().endswith("Try: look.")


def test_noop_and_refused():
    noop = effect.Effect(effect.NOOP, "Clicked X; nothing changed", "global_input", "foreground",
                         escalation="call ui_elements").render()
    assert noop.startswith("SUSPECTED NO-OP via the real mouse/keyboard (foreground): ")
    assert noop.endswith("Try: call ui_elements.")
    assert effect.is_noop(noop)
    gone = effect.Effect(effect.REFUSED, "could not find 'Save'", "accessibility", "background", ["x"])
    assert gone.route == "" and gone.evidence == [] and not gone.ok
    assert gone.render() == "FAILED: could not find 'Save'."
    # an older FAILED/REFUSED text is kept as it is
    assert effect.refused("FAILED: covered by Finder") == "FAILED: covered by Finder."
    with pytest.raises(ValueError):
        effect.Effect("maybe", "x")


def test_the_loop_guard_counts_a_suspected_noop_as_a_failure():
    assert harness_tools._failed("SUSPECTED NO-OP via the real mouse/keyboard (foreground): Clicked X")
    assert not harness_tools._failed("UNVERIFIED via accessibility (background): Pressed X")
    assert not harness_tools._failed("CONFIRMED via accessibility (background): Pressed X. Evidence: y.")


def test_partial_carries_the_count():
    e = effect.Effect(effect.PARTIAL, "Typed 'hello world'", "accessibility", "background", delivered=5)
    assert e.ok and e.to_dict()["delivered"] == 5 and e.render().startswith("PARTIAL via accessibility")


# --- typing progress --------------------------------------------------------------------------------

@pytest.mark.parametrize("before,after,text,want", [
    ("", "hello", "hello", ("complete", 5)),
    ("Dear ", "Dear Alex", "Alex", ("complete", 4)),
    ("ab", "abhel", "hello", ("partial", 3)),
    ("ab", "ab", "hello", ("unchanged", 0)),
    ("ab", None, "hello", ("unverifiable", 0)),             # the field does not report its text
    ("hi", "hi", "hi", ("unchanged", 0)),                   # already there is not "landed"
    ("", "x", "a\nb", ("unverifiable", 0)),                 # Return submits / moves on
    ("abc", "ab", "xyz", ("unchanged", 0)),                 # shorter is not "some of it landed"
])
def test_typed_progress(before, after, text, want):
    assert effect.typed_progress(before, after, text) == want


def test_replaced_progress():
    assert effect.replaced_progress("old", "new text", "new text") == ("complete", 8)
    assert effect.replaced_progress("old", "old", "new") == ("unchanged", 0)
    assert effect.replaced_progress("old", None, "new") == ("unverifiable", 0)


# --- deciding: accessibility or the pointer ------------------------------------------------------------

@pytest.mark.parametrize("role,acts,kw,want", [
    ("AXButton", ["AXPress"], {}, "AXPress"),
    ("AXButton", ["AXPress"], {"chromium": True, "web": True}, ""),        # Chromium ignores AXPress
    ("AXButton", ["AXPress"], {"chromium": True}, ""),                     # only its native menus
    ("AXMenuItem", ["AXPress"], {"chromium": True}, "AXPress"),
    ("AXButton", ["AXPress"], {"action": "double_click"}, ""),
    ("AXButton", ["AXPress", "AXShowMenu"], {"action": "right_click"}, "AXShowMenu"),
    ("AXButton", ["AXPress"], {"action": "right_click"}, ""),
    ("AXTextField", [], {}, "focus"),
    ("AXRow", [], {}, "select"),
    ("AXTextArea", [], {"hidden": True}, ""),                              # Electron's 1x1 inputs
    ("AXButton", ["AXPress"], {"quiet_before": True}, ""),                 # pressed with no effect just now
    ("AXGroup", ["AXPress"], {"web": True}, ""),                           # web: real controls only
    ("AXLink", ["AXPress"], {"web": True}, "AXPress"),                     # WebKit presses links
    ("AXStaticText", ["AXConfirm"], {}, "AXConfirm"),
    ("AXStaticText", [], {}, ""),
])
def test_ax_plan(role, acts, kw, want):
    assert effect.ax_plan(role, acts, **kw) == want


def test_in_web_area():
    page = Node("AXWebArea")
    field = Node("AXTextField", parent=Node("AXGroup", parent=page))
    toolbar = Node("AXTextField", parent=Node("AXToolbar"))
    ax = FakeAX()
    assert effect.in_web_area(field, ax) and not effect.in_web_area(toolbar, ax)


# --- typing through accessibility ---------------------------------------------------------------------

def test_ax_insert_types_at_the_end_and_reads_back(quick):
    field = Node("AXTextField", AXValue="Hello", AXFocused=False)
    ax = FakeAX()
    assert effect.ax_insert(field, " there", at="end", ax=ax) == ("complete", 6, "")
    assert field.get("AXValue") == "Hello there" and field.get("AXFocused") is True
    assert ("AXSelectedTextRange", (5, 0)) in ax.writes


def test_ax_insert_replaces_all():
    field = Node("AXTextField", AXValue="old name", AXFocused=True)
    progress, _, _ = effect.ax_insert(field, "Alex", at="all", ax=FakeAX(), settle=0)
    assert progress == "complete" and field.get("AXValue") == "Alex"


def test_ax_insert_never_writes_into_web_content_or_passwords():
    ax = FakeAX()
    web_field = Node("AXTextField", parent=Node("AXWebArea"), AXValue="")
    assert effect.ax_insert(web_field, "hi there", ax=ax, settle=0)[0] == "skipped"
    secret = Node("AXTextField", AXSubrole="AXSecureTextField", AXValue="")
    assert effect.ax_insert(secret, "hunter2", ax=ax, settle=0)[0] == "skipped"
    assert ax.writes == []                    # an echo there would prove nothing; nothing was sent


def test_ax_insert_reports_partial_and_rejected():
    short = Node("AXTextField", AXValue="", AXFocused=True)
    short.max_len = 5
    assert effect.ax_insert(short, "hello world", ax=FakeAX(), settle=0)[:2] == ("partial", 5)
    stubborn = Node("AXTextArea", AXValue="x", AXFocused=True)
    stubborn.refuse_write = True
    assert effect.ax_insert(stubborn, "abc", ax=FakeAX(), settle=0)[0] == "rejected"
    mute = Node("AXTextArea", AXFocused=True)                       # no readable AXValue
    mute.hide_value = True
    assert effect.ax_insert(mute, "abc", ax=FakeAX(), settle=0)[0] == "unverifiable"


# --- pressing through accessibility ---------------------------------------------------------------------

def test_ax_deliver_toggle_reads_its_value_back(quick):
    box = Node("AXCheckBox", actions=["AXPress"], AXValue=0)
    box.on_press = lambda: box.attrs.update(AXValue=1)
    ok, evidence, _ = effect.ax_deliver(box, "AXPress", FakeAX())
    assert ok and "now 1 (was 0" in evidence[0]


def test_ax_deliver_error_falls_back_and_selects_rows(quick):
    button = Node("AXButton", actions=["AXPress"])
    button.press_error = -25204
    assert effect.ax_deliver(button, "AXPress", FakeAX())[0] is False
    row = Node("AXRow", AXSelected=False)
    cell = Node("AXCell", parent=row)
    ok, evidence, _ = effect.ax_deliver(cell, "select", FakeAX())
    assert ok and row.get("AXSelected") is True and "selected" in evidence[0]


# --- a pixel target: hit-test, then press ---------------------------------------------------------------

def _window_with_button(pid=100):
    window = Node("AXWindow", pid=pid, frame=(0, 0, 800, 600))
    button = Node("AXButton", parent=window, actions=["AXPress"], pid=pid, frame=(100, 100, 80, 30),
                  AXWindow=window, AXTitle="Save")
    label = Node("AXStaticText", parent=button, pid=pid, frame=(110, 105, 60, 20), AXWindow=window)
    return window, button, label


def test_hit_test_resolves_a_label_to_its_button():
    _, button, label = _window_with_button()
    assert effect.press_target_at(120, 110, 100, ax=FakeAX(at=label)) is button


def test_hit_test_refuses_another_app_chromium_and_outside_points():
    _, button, label = _window_with_button(pid=200)
    assert effect.press_target_at(120, 110, 100, ax=FakeAX(at=label)) is None        # another app on top
    _, button, label = _window_with_button()
    assert effect.press_target_at(120, 110, 100, ax=FakeAX(at=label, chromium=True)) is None
    assert effect.press_target_at(900, 110, 100, ax=FakeAX(at=label)) is None        # not in its box
    plain = Node("AXStaticText", parent=Node("AXGroup"), frame=(0, 0, 50, 20))
    assert effect.press_target_at(10, 10, 100, ax=FakeAX(at=plain)) is None          # nothing pressable


# --- window changes ---------------------------------------------------------------------------------------

def test_window_changes_only_count_the_app_acted_on():
    """A z-order or Space change moves other apps' windows in and out of the list: not evidence, and their
    titles stay out of the result."""
    before = {"windows": {1: (100, "Fixture", "Form"), 5: (300, "Browser", "Bank statement")},
              "front": (300, "Browser")}
    after = {"windows": {1: (100, "Fixture", "Form"), 2: (100, "Fixture", "Options"), 6: (400, "Chat", "Private")},
             "front": (300, "Browser")}
    notes = effect.window_changes(before, after, own_pid=7, pids=(100,))
    assert notes == ["new window: Fixture ('Options')"]
    raised = dict(after, front=(100, "Fixture"))           # Mint brought it forward itself: not the click's doing
    assert effect.window_changes(before, raised, own_pid=7, pids=(100,), raised=100) == notes
    assert effect.window_changes(before, raised, own_pid=7, pids=(100,))[-1] == "Fixture is now in front (was Browser)"


def test_window_changes():
    before = {"windows": {1: (100, "TextEdit", "Notes.txt"), 2: (7, "Mint", "")}, "front": (100, "TextEdit")}
    after = {"windows": {1: (100, "TextEdit", "Notes.txt"), 3: (100, "TextEdit", "Save As"),
                         4: (7, "Mint", "spark")}, "front": (300, "Finder")}
    notes = effect.window_changes(before, after, own_pid=7)
    assert notes[0] == "new window: TextEdit ('Save As')"
    assert "window closed" not in " ".join(notes)            # Mint's own overlay came and went
    assert notes[-1] == "Finder is now in front (was TextEdit)"
    assert effect.window_changes(before, before, own_pid=7) == []
    assert effect.window_changes({}, after) == []


def test_pointer_goes_back_only_if_the_user_left_it(monkeypatch):
    import Quartz
    warped = []
    monkeypatch.setattr(Quartz, "CGWarpMouseCursorPosition", lambda p: warped.append((p.x, p.y)))
    monkeypatch.setattr(Quartz, "CGAssociateMouseAndMouseCursorPosition", lambda on: None)
    monkeypatch.setattr(effect, "pointer_at", lambda: (300.0, 200.0))
    assert effect.give_pointer_back((10.0, 10.0), (300.0, 200.0)) and warped == [(10.0, 10.0)]
    monkeypatch.setattr(effect, "pointer_at", lambda: (500.0, 500.0))      # the user moved it since
    assert not effect.give_pointer_back((10.0, 10.0), (300.0, 200.0)) and len(warped) == 1


# --- ground.act end to end, with a fake app ----------------------------------------------------------------

class App:
    def __init__(self, pid, name):
        self.pid, self.name = pid, name

    def processIdentifier(self):  # noqa: N802 - AppKit's name
        return self.pid

    def localizedName(self):  # noqa: N802
        return self.name

    def isTerminated(self):  # noqa: N802
        return False


def _inv(elements, pid=100, app="Notes"):
    return {"app": app, "pid": pid, "title": "Doc", "window": (0, 0, 800, 600), "dialog": None,
            "dialog_title": "", "elements": elements, "walked": 10, "offscreen": []}


def _el(node, role, label, value=""):
    return {"id": 1, "role": role, "subrole": node.get("AXSubrole") or "", "kind": ground.ROLE_WORDS.get(role, "x"),
            "label": label, "value": value, "box": (100, 100, 80, 30), "enabled": True, "focused": False,
            "interactive": True, "dialog": None, "ref": node, "depth": 3, "in_dialog": False, "hint": ""}


@pytest.fixture
def world(monkeypatch, quick):
    """ground.act against fakes: what was clicked with the pointer, typed, brought forward."""
    ax = FakeAX()
    w = types.SimpleNamespace(ax=ax, clicks=[], raised=[], keys=[], unicode=[], pasted=[], registered=[],
                              front=App(100, "Notes"), after=None, chosen=None, shown=[], acted=False)
    monkeypatch.setattr(effect, "LIVE", ax)
    monkeypatch.setattr(effect, "window_snapshot", lambda: {})
    monkeypatch.setattr(fx_mod.fx, "click", lambda *a, **k: 0)
    monkeypatch.setattr(ground, "front_app", lambda: w.front)
    monkeypatch.setattr(ground, "is_own", lambda app: False)
    monkeypatch.setattr(ground, "show_window", lambda app, wait=2.0, activate=True: w.shown.append(activate) or True)
    monkeypatch.setattr(ground, "_covered", lambda pid: False)
    monkeypatch.setattr(ground, "_menus", {})
    monkeypatch.setattr(ground, "_menu_front", {})
    monkeypatch.setattr(ground, "ensure_on_top", lambda inv, x, y: "")
    monkeypatch.setattr(ground, "_user_wants", lambda app: False)
    monkeypatch.setattr(ground, "_register_target", lambda app: w.registered.append(app.name))
    monkeypatch.setattr(ground, "_ax_quiet", {})

    def bring_forward(app, wait=2.0):
        w.raised.append(app.localizedName())
        w.front = app
        return True

    monkeypatch.setattr(ground, "bring_forward", bring_forward)
    monkeypatch.setattr(ground, "mouse_click", lambda x, y, **k: w.clicks.append((x, y, k.get("double", False))))
    monkeypatch.setattr(ground, "ground", lambda target, action, inv: (w.chosen, inv, "label match"))

    def inventory(app=None):
        # the window as it is after something took: a press the app accepted (or that acted anyway), a click
        return w.after if (w.after is not None and (ax.done or w.acted or w.clicks or w.pasted or w.unicode)) \
            else _inv([w.chosen])

    monkeypatch.setattr(ground, "inventory", inventory)
    monkeypatch.setattr(axkit, "attr", ax.attr)
    monkeypatch.setattr(axkit, "frame", ax.frame)
    monkeypatch.setattr(axkit, "focused_element", lambda pid=None: None)
    monkeypatch.setattr(axkit, "focus", lambda el: ax.set(el, "AXFocused", True) == 0)
    monkeypatch.setattr(fastinput, "press_key", lambda key, mods=None, times=1, pid=None: w.keys.append(key))

    def type_unicode(text, pid=None, gap=0):
        w.unicode.append(text)
        field = w.chosen["ref"]
        field.attrs["AXValue"] = (field.attrs.get("AXValue") or "") + text
        return len(text)

    monkeypatch.setattr(fastinput, "type_unicode", type_unicode)

    class Board:
        def clearContents(self):  # noqa: N802
            pass

        def setString_forType_(self, text, kind):  # noqa: N802
            w.pasted.append(text)
            w.chosen["ref"].attrs["AXValue"] = text

    class Clip:
        def __enter__(self):
            self.board = Board()
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(skills, "_Clipboard", Clip)
    return w


def test_native_button_is_pressed_through_accessibility_without_the_cursor(world):
    button = Node("AXButton", actions=["AXPress"], AXTitle="New Note")
    world.chosen = _el(button, "AXButton", "New Note")
    world.after = _inv([world.chosen, _el(Node("AXTextArea"), "AXTextArea", "note body")])
    out = ground.act("click", "New Note")
    assert out.startswith("CONFIRMED via accessibility (background): Pressed the button 'New Note'"), out
    assert "new on screen" in out
    assert world.clicks == [] and world.raised == []


def test_chromium_content_gets_the_real_pointer(world):
    world.ax.is_chromium = True
    button = Node("AXButton", parent=Node("AXWebArea"), actions=["AXPress"], AXTitle="New chat")
    world.chosen = _el(button, "AXButton", "New chat")
    world.after = _inv([world.chosen, _el(Node("AXStaticText"), "AXStaticText", "What can I help with?")])
    out = ground.act("click", "New chat")
    assert out.startswith("CONFIRMED via the real mouse/keyboard (foreground): Clicked the button 'New chat'"), out
    assert world.ax.presses == [] and len(world.clicks) == 1


def test_a_silent_press_is_unverified_and_the_next_try_uses_the_pointer(world):
    button = Node("AXButton", actions=["AXPress"], AXTitle="Refresh")
    world.chosen = _el(button, "AXButton", "Refresh")
    first = ground.act("click", "Refresh")
    assert first.startswith("UNVERIFIED via accessibility (background)"), first
    assert "next try uses the real pointer" in first and world.clicks == []
    second = ground.act("click", "Refresh")
    assert len(world.clicks) == 1 and "real pointer was used" in second
    assert len(world.ax.presses) == 1                  # never pressed twice automatically


def test_a_refused_press_falls_back_to_the_pointer_in_the_same_call(world):
    button = Node("AXButton", actions=["AXPress"], AXTitle="Go")
    button.press_error = -25206                        # kAXErrorActionUnsupported: it never ran
    world.chosen = _el(button, "AXButton", "Go")
    world.after = _inv([world.chosen, _el(Node("AXStaticText"), "AXStaticText", "Done")])
    out = ground.act("click", "Go")
    assert len(world.clicks) == 1 and "accessibility could not do it" in out and "via the real mouse" in out


def test_background_app_stays_behind_for_accessibility(world):
    world.front = App(300, "Editor")                           # the user is working elsewhere
    button = Node("AXButton", actions=["AXPress"], AXTitle="Play")
    world.chosen = _el(button, "AXButton", "Play")
    world.after = _inv([world.chosen, _el(Node("AXButton"), "AXButton", "Pause")])
    out = ground.act("click", "Play", app=App(100, "Music"))
    assert out.startswith("CONFIRMED via accessibility (background)") and world.raised == []


def test_pointer_fallback_brings_the_app_forward_and_gives_the_front_back(world):
    world.front = App(300, "Editor")
    world.ax.is_chromium = True
    button = Node("AXButton", parent=Node("AXWebArea"), actions=["AXPress"], AXTitle="New chat")
    world.chosen = _el(button, "AXButton", "New chat")
    world.after = _inv([world.chosen, _el(Node("AXStaticText"), "AXStaticText", "Hello")])
    out = ground.act("click", "New chat", app=App(100, "Chat"))
    assert world.raised == ["Chat", "Editor"], world.raised
    assert world.registered == ["Chat"]            # the next screen tool brings Chat back first
    assert "Editor is back in front" in out


def test_typing_goes_through_accessibility_without_keys_or_clipboard(world):
    field = Node("AXTextField", AXValue="", AXFocused=False)
    world.chosen = _el(field, "AXTextField", "Title")
    out = ground.act("type", "Title", text="Shopping list")
    assert out.startswith("CONFIRMED via accessibility (background): Typed 'Shopping list'"), out
    assert field.get("AXValue") == "Shopping list"
    assert world.pasted == [] and world.unicode == [] and world.keys == [] and world.clicks == []


def test_typing_partial_is_reported_not_repeated(world):
    field = Node("AXTextField", AXValue="", AXFocused=True)
    field.max_len = 5
    world.chosen = _el(field, "AXTextField", "Code")
    out = ground.act("type", "Code", text="hello world")
    assert out.startswith("PARTIAL via accessibility") and "only 5 of 11 characters landed" in out, out
    assert world.pasted == [] and world.unicode == []


def test_a_refused_write_falls_to_key_events_then_never_the_clipboard(world):
    field = Node("AXTextField", AXValue="", AXFocused=True)
    field.refuse_write = True
    world.chosen = _el(field, "AXTextField", "Search")
    out = ground.act("type", "Search", text="weather")
    assert world.unicode == ["weather"] and world.pasted == []
    assert out.startswith("CONFIRMED via key events sent to the app (foreground)"), out


def test_web_fields_are_never_written_through_accessibility(world):
    field = Node("AXTextField", parent=Node("AXWebArea"), AXValue="")
    world.chosen = _el(field, "AXTextField", "Search the web")
    out = ground.act("type", "Search the web", text="weather today")
    assert not any(name == "AXSelectedText" for name, _ in world.ax.writes)
    assert world.pasted == ["weather today"] and len(world.clicks) == 1    # focus-click, then type
    assert out.startswith("CONFIRMED via the real mouse/keyboard (foreground)"), out


def test_chromium_native_fields_are_not_written_through_accessibility_either(world):
    world.ax.is_chromium = True                    # Electron: even outside a web area, paste
    field = Node("AXTextArea", AXValue="")
    world.chosen = _el(field, "AXTextArea", "Message")
    ground.act("type", "Message", text="hi there")
    assert not any(name == "AXSelectedText" for name, _ in world.ax.writes)
    assert world.pasted == ["hi there"] and world.unicode == []


def test_password_fields_are_refused(world):
    field = Node("AXTextField", AXSubrole="AXSecureTextField", AXValue="")
    world.chosen = _el(field, "AXTextField", "Password")
    out = ground.act("type", "Password", text="hunter2")
    assert out.startswith("FAILED") and "password" in out
    assert world.ax.writes == [] and world.pasted == [] and world.unicode == []


def test_an_id_from_ui_elements_is_resolved_before_reading_again(world, monkeypatch):
    button = Node("AXButton", actions=["AXPress"], AXTitle="Save")
    world.chosen = _el(button, "AXButton", "Save")
    listed = ground.remember(_inv([world.chosen]))           # what ui_elements showed the model
    monkeypatch.setattr(ground, "ground", lambda *a: pytest.fail("an id needs no choosing"))
    world.after = _inv([world.chosen, _el(Node("AXStaticText"), "AXStaticText", "Saved")])
    out = ground.act("click", f"[{listed}:1]")
    assert out.startswith("CONFIRMED via accessibility (background): Pressed the button 'Save' (by id"), out


def test_a_stale_id_is_refused_and_a_bare_number_is_a_label(world, monkeypatch):
    button = Node("AXButton", actions=["AXPress"], AXTitle="2")
    world.chosen = _el(button, "AXButton", "2")
    old = ground.remember(_inv([world.chosen]))
    ground.remember(_inv([world.chosen]))                    # the window was read again since
    out = ground.act("click", f"{old}:1")
    assert out.startswith("FAILED") and "stale" in out and world.ax.presses == [] and world.clicks == []
    asked = []
    monkeypatch.setattr(ground, "ground", lambda target, action, inv: asked.append(target) or (None, inv, "x"))
    monkeypatch.setattr(ground, "_by_screen_text", lambda *a: "")
    ground.act("click", "2")                                 # Calculator's button, not element 2
    assert asked == ["2"]


# --- an accessibility error is not proof that nothing happened ---------------------------------------------

@pytest.mark.parametrize("problem,want", [
    ("AXPress returned error -25200", True),     # the app's handler threw - maybe after acting
    ("AXPress returned error -25204", True),     # busy (a modal dialog it opened): it may act yet
    ("AXPress returned error -25202", True),     # the element went away - perhaps because it acted
    ("AXPress returned error -25206", False),    # action unsupported: it never ran
    ("AXPress returned error -1", False),        # the call itself failed
    ("the field refused the accessibility focus", False),
])
def test_maybe_acted(problem, want):
    assert effect.maybe_acted(problem) is want


def test_an_error_after_the_app_acted_is_never_pressed_twice(world):
    """The test app's "Apply theme" acts, then its AXPress raises: the pointer must not press it again."""
    button = Node("AXButton", actions=["AXPress"], AXTitle="Apply theme")
    button.press_error = -25200
    world.chosen = _el(button, "AXButton", "Apply theme")
    out = ground.act("click", "Apply theme")
    assert out.startswith("UNVERIFIED via accessibility (background)"), out
    assert "not pressed a second time" in out and "next try uses the real pointer" in out
    assert world.clicks == [] and len(world.ax.presses) == 1
    again = ground.act("click", "Apply theme")             # asked again: the real pointer, once
    assert len(world.clicks) == 1 and len(world.ax.presses) == 1, again


def test_an_error_with_a_visible_change_is_confirmed_without_the_pointer(world):
    button = Node("AXButton", actions=["AXPress"], AXTitle="Apply theme")
    button.press_error = -25200
    button.on_press = lambda: setattr(world, "acted", True)
    world.chosen = _el(button, "AXButton", "Apply theme")
    world.after = _inv([world.chosen, _el(Node("AXStaticText"), "AXStaticText", "Theme applied")])
    out = ground.act("click", "Apply theme")
    assert out.startswith("CONFIRMED via accessibility (background)"), out
    assert "answered with an error" in out and "Theme applied".lower() in out.lower()
    assert world.clicks == [] and len(world.ax.presses) == 1
    # its own title changing is evidence too
    titled = Node("AXButton", actions=["AXPress"], AXTitle="Sync")
    titled.press_error = -25204
    titled.on_press = lambda: titled.attrs.update(AXTitle="Syncing…")
    world.chosen, world.after = _el(titled, "AXButton", "Sync"), None
    out = ground.act("click", "Sync")
    assert out.startswith("CONFIRMED via accessibility") and "Syncing" in out and world.clicks == [], out


def test_send_after_an_error_is_not_clicked_again(world):
    send = Node("AXButton", actions=["AXPress"], AXTitle="Send")
    send.press_error = -25200
    element = _el(send, "AXButton", "Send")
    assert ground._press(element, _inv([element])) == "accessibility" and world.clicks == []
    send.press_error = -25206
    assert ground._press(element, _inv([element])) == "global_input" and len(world.clicks) == 1


# --- the user's front app: an accessibility action never activates the target -------------------------------

def test_a_covered_window_is_not_raised_for_an_accessibility_press(world, monkeypatch):
    monkeypatch.setattr(ground, "_covered", lambda pid: True)        # the user's window lies over it
    world.front = App(300, "Editor")
    button = Node("AXButton", actions=["AXPress"], AXTitle="Play")
    world.chosen = _el(button, "AXButton", "Play")
    world.after = _inv([world.chosen, _el(Node("AXButton"), "AXButton", "Pause")])
    out = ground.act("click", "Play", app=App(100, "Music"))
    assert out.startswith("CONFIRMED via accessibility (background)"), out
    assert world.raised == [] and world.shown == [False]              # never "reopen + activate"


def test_show_window_without_activating_never_raises_or_reopens(monkeypatch):
    """A window on another Space is not on screen, but Accessibility still reaches it: no AXRaise, no
    AppleScript "reopen/activate" (in a live run that switched the user out of a full-screen Space)."""
    done = []
    windows = [Node("AXWindow", AXMinimized=False)]
    monkeypatch.setattr(ground, "visible_window", lambda pid: None)
    monkeypatch.setattr(ground, "AX", types.SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: Node("AXApplication", AXWindows=windows),
        AXUIElementPerformAction=lambda el, a: done.append(a),
        AXUIElementSetAttributeValue=lambda el, n, v: done.append(n)))
    monkeypatch.setattr(ground, "AppKit", types.SimpleNamespace(NSAppleScript=None))      # would fail if used
    monkeypatch.setattr(axkit, "attr", lambda el, name: el.get(name) if el is not None else None)
    app = App(100, "Fixture")
    app.isHidden = lambda: False
    assert ground.show_window(app, activate=False) is True and done == []
    windows[0].attrs["AXMinimized"] = True                 # only minimised: that needs the app shown
    assert ground.show_window(app, activate=False) is False and done == []


def test_an_app_without_a_usable_window_is_shown_in_front_and_given_back(world, monkeypatch):
    monkeypatch.setattr(ground, "show_window", lambda app, wait=2.0, activate=True: activate)
    world.front = App(300, "Editor")
    button = Node("AXButton", actions=["AXPress"], AXTitle="Play")
    world.chosen = _el(button, "AXButton", "Play")
    world.after = _inv([world.chosen, _el(Node("AXButton"), "AXButton", "Pause")])
    out = ground.act("click", "Play", app=App(100, "Music"))
    assert world.raised == ["Music", "Editor"], world.raised
    assert out.startswith("CONFIRMED via accessibility (foreground)") and "Editor is back in front" in out, out


def test_found_by_screen_text_says_foreground_and_gives_the_front_back(world, monkeypatch):
    world.front = App(300, "Editor")
    monkeypatch.setattr(ground, "_by_screen_text",
                        lambda *a: "CONFIRMED via accessibility (background): Pressed 'Go'. Evidence: x.")
    out = ground.act("click", "Go", app=App(100, "Music"))
    assert out.startswith("CONFIRMED via accessibility (foreground)"), out
    assert world.raised == ["Music", "Editor"] and "Editor is back in front" in out


def test_send_is_pressed_in_the_background_but_return_needs_the_front(world, monkeypatch):
    world.front = App(300, "Editor")
    field = Node("AXTextField", AXValue="", AXFocused=False)
    send = Node("AXButton", actions=["AXPress"], AXTitle="Send")
    send.on_press = lambda: field.attrs.update(AXValue="")
    world.chosen = _el(field, "AXTextField", "Message")
    monkeypatch.setattr(ground, "_send_button", lambda inv, x, y: _el(send, "AXButton", "Send"))
    monkeypatch.setattr(ground, "_sent_check", lambda app, text: " - it was sent.")
    out = ground.act("type", "Message", text="hello", press_return=True, app=App(100, "Chat"))
    assert world.raised == [] and world.keys == [] and world.clicks == [], out
    assert out.startswith("CONFIRMED via accessibility (background)") and "clicked Send" in out, out
    monkeypatch.setattr(ground, "_send_button", lambda inv, x, y: None)
    field.attrs["AXValue"] = ""
    out = ground.act("type", "Message", text="hello", press_return=True, app=App(100, "Chat"))
    assert world.keys == ["return"] and world.raised == ["Chat", "Editor"], world.raised
    assert out.split(":")[0].endswith("(foreground)"), out          # Return needed the app in front


def test_menu_openers_are_not_guarded(monkeypatch):
    """The focus guard putting the user's app back closes a just-opened menu (the Size pop-up, live)."""
    from contextlib import contextmanager
    try:
        from mint.screen import focus_guard
    except ImportError:
        from mint import focus_guard
    ax = FakeAX()
    monkeypatch.setattr(effect, "LIVE", ax)
    leases = []

    @contextmanager
    def lease(*a, **k):
        leases.append(k)
        yield types.SimpleNamespace(note="")

    monkeypatch.setattr(focus_guard, "lease", lease)
    effect._perform_guarded(ax, Node("AXPopUpButton", actions=["AXPress"], pid=4242), "AXPress")
    effect._perform_guarded(ax, Node("AXButton", actions=["AXShowMenu"], pid=4242), "AXShowMenu")
    assert leases == []
    effect._perform_guarded(ax, Node("AXButton", actions=["AXPress"], pid=4242), "AXPress")
    assert leases and leases[0].get("only_target") is True


def test_extra_tools_names_the_app_for_ui_act_and_gives_the_front_back_otherwise(monkeypatch):
    import time
    try:
        from mint.tools import extra as extra_tools
    except ImportError:
        from mint import extra_tools
    user, target, raised = App(300, "Editor"), App(100, "Chat"), []
    workspace = types.SimpleNamespace(frontmostApplication=lambda: user)
    monkeypatch.setattr(extra_tools, "AppKit", types.SimpleNamespace(
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: workspace)))
    now = time.monotonic()
    monkeypatch.setattr(extra_tools, "_target", {"app": target, "at": now})
    monkeypatch.setattr(ground, "_handed_back", {"pid": 100, "at": now})     # Mint gave the front back
    monkeypatch.setattr(ground, "bring_forward", lambda app, wait=2.0: raised.append(app.localizedName()) or True)
    args = {"action": "click", "target": "Send"}
    assert extra_tools._ensure_target_front("ui_act", args) == ("", None)
    assert args["app"] == "Chat" and raised == []                  # ui_act decides itself, in the background
    problem, prior = extra_tools._ensure_target_front("type_text", {"text": "hi"})
    assert problem == "" and prior is user and raised == ["Chat"]  # the keyboard needs the front...
    out = extra_tools._give_front_back(prior, "CONFIRMED via accessibility (background): Typed 2 characters.")
    assert out.startswith("CONFIRMED via accessibility (foreground)") and "Editor is back in front" in out
    assert raised == ["Chat", "Editor"]                             # ...and the user gets it back
    monkeypatch.setattr(ground, "_handed_back", {"pid": 0, "at": -1.0})     # an app the user asked for
    assert extra_tools._ensure_target_front("type_text", {}) == ("", None) and raised[-1] == "Chat"


# --- menus: read, then chosen through Accessibility, the app left where it was --------------------------------

def _popup(options=("Small", "Medium", "Large"), value="Small", opens=True):
    popup = Node("AXPopUpButton", actions=["AXPress"], AXTitle="Size", AXValue=value)
    menu = Node("AXMenu", parent=popup, AXChildren=[])
    items = [Node("AXMenuItem", parent=menu, actions=["AXPress"], AXTitle=t, AXEnabled=True) for t in options]

    def close():
        menu.attrs["AXChildren"] = []

    if opens:
        popup.on_press = lambda: menu.attrs.update(AXChildren=list(items))
    menu.on_press = close                                        # AXCancel
    for item in items:
        item.on_press = lambda t=item.get("AXTitle"): (popup.attrs.update(AXValue=t), close())
    popup.attrs["AXChildren"] = [menu]
    return popup, menu, items


def test_list_menu_reads_the_choices_and_closes_it_again(quick):
    popup, menu, _ = _popup()
    ax = FakeAX()
    read = effect.list_menu(popup, "AXPress", 100, ax)
    assert read.titles == ["Small", "Medium", "Large"] and read.selected == "Small"
    assert menu.get("AXChildren") == [] and popup.get("AXValue") == "Small"      # closed, nothing chosen
    assert not any(t in ("Small", "Medium", "Large") for t, _ in ax.presses)


def test_pick_from_menu_presses_the_item_and_reads_the_value_back(quick):
    popup, menu, items = _popup()
    pick = effect.pick_from_menu(popup, "AXPress", "large", 100, FakeAX())
    assert pick.ok and pick.picked == "Large" and "now reads 'Large'" in pick.evidence[0], pick
    assert menu.get("AXChildren") == []
    items[1].attrs["AXEnabled"] = False
    ax = FakeAX()
    missing = effect.pick_from_menu(popup, "AXPress", "Huge", 100, ax)
    assert not missing.ok and "no 'Huge'" in missing.problem and missing.titles == ["Small", "Medium", "Large"]
    grey = effect.pick_from_menu(popup, "AXPress", "Medium", 100, ax)
    assert not grey.ok and "greyed out" in grey.problem
    assert not any(t in ("Small", "Medium", "Large") for t, _ in ax.presses) and menu.get("AXChildren") == []


def test_a_context_menu_is_found_on_the_application(quick):
    class AppAX(FakeAX):
        def __init__(self, app_node):
            super().__init__()
            self.app_node = app_node

        def app(self, pid):
            return self.app_node

    app_node = Node("AXApplication", AXChildren=[])
    button = Node("AXButton", actions=["AXPress", "AXShowMenu"], AXTitle="Item actions")
    menu = Node("AXMenu", AXChildren=[])
    items = [Node("AXMenuItem", AXTitle=t) for t in ("Rename item", "Archive item")]
    chosen = []
    button.on_press = lambda: (menu.attrs.update(AXChildren=list(items)), app_node.attrs.update(AXChildren=[menu]))
    for item in items:
        item.on_press = lambda t=item.get("AXTitle"): (chosen.append(t), menu.attrs.update(AXChildren=[]))
    pick = effect.pick_from_menu(button, "AXShowMenu", "Archive item", 100, AppAX(app_node))
    assert pick.ok and chosen == ["Archive item"], pick


def test_a_popup_in_a_background_app_is_read_then_chosen_without_bringing_it_forward(world, monkeypatch):
    world.front = App(300, "Editor")
    popup, menu, _ = _popup()
    world.chosen = _el(popup, "AXPopUpButton", "Size")
    fixture = App(100, "Fixture")
    out = ground.act("click", "Size", app=fixture)
    assert out.startswith("CONFIRMED via accessibility (background)"), out
    assert "'Small' (selected), 'Medium', 'Large'" in out and "target='Medium'" in out
    assert menu.get("AXChildren") == [] and world.raised == [] and world.clicks == []
    monkeypatch.setattr(ground, "ground", lambda *a: pytest.fail("a remembered menu item needs no choosing"))
    out = ground.act("click", "Large", app=fixture)
    assert out.startswith("CONFIRMED via accessibility (background): Chose 'Large' in the pop-up"), out
    assert popup.get("AXValue") == "Large" and menu.get("AXChildren") == []
    assert world.raised == [] and world.clicks == []
    assert ground._menus == {}                       # used up: a later "Large" is the window's own again


def test_a_menu_named_loosely_and_one_that_is_not_there(world):
    popup, _, _ = _popup()
    held = {"titles": ["Small", "Medium", "Large"], "label": "Size"}
    assert ground._option_named("Large in the Size menu", held) == "Large"
    assert ground._option_named("size: medium", held) == "Medium"
    assert ground._option_named("Size", held) == "" and ground._option_named("Large font", held) == ""


def test_a_menu_that_lists_nothing_in_the_background_opens_in_front_and_stays(world):
    world.front = App(300, "Editor")
    popup, menu, _ = _popup(opens=False)            # this app does not open its menu for Accessibility
    world.chosen = _el(popup, "AXPopUpButton", "Size")
    fixture = App(100, "Fixture")
    out = ground.act("click", "Size", app=fixture)
    assert world.raised == ["Fixture"], world.raised           # in front for its menu - and left there
    assert out.split(":")[0].endswith("(foreground)") and "stays in front while its menu is open" in out, out
    item = Node("AXMenuItem", actions=["AXPress"], AXTitle="Large")
    world.chosen = _el(item, "AXMenuItem", "Large")
    world.after = _inv([world.chosen, _el(Node("AXStaticText"), "AXStaticText", "Large chosen")])
    out = ground.act("click", "Large", app=fixture)
    assert world.raised == ["Fixture", "Editor"] and "Editor is back in front" in out, out


def test_a_menu_open_in_front_after_a_timed_out_press_is_used_not_clicked(world, monkeypatch):
    """Its menu opens only with the app in front, and the opening press times out (-25204) while the menu
    is open: that is the menu working, not a reason for the pointer. The item is then picked from it."""
    world.front = App(300, "Editor")
    popup, menu, items = _popup(opens=False)
    popup.press_error = -25204
    popup.on_press = lambda: menu.attrs.update(AXChildren=list(items)) if world.front.pid == 100 else None
    world.chosen = _el(popup, "AXPopUpButton", "Size")
    fixture = App(100, "Fixture")
    out = ground.act("click", "Size", app=fixture)
    assert out.startswith("CONFIRMED via accessibility (foreground)") and "its menu is open" in out, out
    assert world.clicks == [] and world.raised == ["Fixture"]
    monkeypatch.setattr(ground, "ground", lambda *a: pytest.fail("a remembered menu item needs no choosing"))
    out = ground.act("click", "Large", app=fixture)
    assert out.startswith("CONFIRMED via accessibility") and popup.get("AXValue") == "Large", out
    assert world.raised == ["Fixture", "Editor"] and world.clicks == []


def test_a_context_menu_opened_with_the_pointer_keeps_the_front_until_the_next_action(world):
    world.front = App(300, "Editor")
    world.ax.is_chromium = True                        # a web page's context menu: only the pointer opens it
    link = Node("AXLink", parent=Node("AXWebArea"), actions=["AXPress"], AXTitle="Docs")
    world.chosen = _el(link, "AXLink", "Docs")
    world.after = _inv([world.chosen, _el(Node("AXMenuItem"), "AXMenuItem", "Copy Link")])
    out = ground.act("right_click", "Docs", app=App(100, "Browser"))
    assert world.raised == ["Browser"] and "stays in front while its menu is open" in out, out


# --- fix-live: what was scrolled to reach it is part of the evidence; a page's control is shown first ----------

def test_a_row_scrolled_into_view_is_pressed_and_the_scroll_is_in_the_evidence(world):
    button = Node("AXButton", actions=["AXPress"], AXTitle="Install")
    world.chosen = dict(_el(button, "AXButton", "Install"), within="SQL Formatter",
                        scrolled="scrolled button 'Install' in 'SQL Formatter' into view first (its scroll bar, "
                                 "set through Accessibility)")
    world.after = _inv([_el(Node("AXButton"), "AXButton", "Installed")])
    out = ground.act("click", "Install SQL Formatter")
    assert out.startswith("CONFIRMED via accessibility (background)"), out
    assert "into view first (its scroll bar, set through Accessibility)" in out.split("Evidence:")[1]
    assert world.clicks == [] and world.raised == []
    # nothing visibly changed: the scroll alone is no evidence that the press did anything
    world.ax.done.clear()
    world.after = None
    quiet = ground.act("click", "Install SQL Formatter")
    assert quiet.startswith("UNVERIFIED"), quiet


def test_a_web_pages_control_is_scrolled_into_view_before_its_press(world, monkeypatch):
    shown, reads = [], []
    monkeypatch.setattr(ground, "_show_on_page", lambda ref: shown.append(ref) or True)
    plain = ground.inventory
    monkeypatch.setattr(ground, "inventory", lambda app=None, **kw: reads.append(kw) or plain(app))
    button = Node("AXButton", parent=Node("AXWebArea"), actions=["AXPress", "AXScrollToVisible"], AXTitle="Install")
    world.chosen = dict(_el(button, "AXButton", "Install"), in_web_content=True, within="Docker Helper")
    world.after = _inv([_el(Node("AXButton"), "AXButton", "Installed")])
    out = ground.act("click", "Install Docker Helper")
    assert shown == [button] and world.ax.presses == [("Install", "AXPress")] and world.clicks == []
    assert {"window_id": None} in reads                    # read again after the scroll, before the press
    assert out.startswith("CONFIRMED via accessibility (background)") and "on the page first" in out, out


def test_native_and_chromium_controls_are_not_scrolled_before_a_press(world, monkeypatch):
    monkeypatch.setattr(ground, "_show_on_page", lambda ref: pytest.fail("not web content"))
    world.chosen = _el(Node("AXButton", actions=["AXPress"], AXTitle="Save"), "AXButton", "Save")
    world.after = _inv([_el(Node("AXStaticText"), "AXStaticText", "Saved")])
    assert ground.act("click", "Save").startswith("CONFIRMED via accessibility")
    world.ax.is_chromium = True                            # Chromium: the pointer, no accessibility scroll
    button = Node("AXButton", parent=Node("AXWebArea"), actions=["AXPress"], AXTitle="Go")
    world.chosen = dict(_el(button, "AXButton", "Go"), in_web_content=True)
    assert "via the real mouse" in ground.act("click", "Go")


def test_show_on_page_says_whether_the_control_moved(monkeypatch):
    frames = {"now": (10, 700, 60, 22)}
    monkeypatch.setattr(axkit, "frame", lambda ref: frames["now"])

    def scroll(ref, action):
        frames["now"] = (10, 300, 60, 22)
        return 0
    monkeypatch.setattr(ground, "AX", types.SimpleNamespace(AXUIElementPerformAction=scroll))
    assert ground._show_on_page("el") is True
    assert ground._show_on_page("el") is False             # already in view: WebKit leaves it
    monkeypatch.setattr(ground, "AX", types.SimpleNamespace(
        AXUIElementPerformAction=lambda ref, action: (_ for _ in ()).throw(TypeError("not an element"))))
    assert ground._show_on_page("el") is False


# --- skills.type_text: the same ladder at the focused field -----------------------------------------------

@pytest.fixture
def typing(monkeypatch, quick):
    ax = FakeAX()
    t = types.SimpleNamespace(ax=ax, field=None, pasted=[], unicode=[], keys=[])
    monkeypatch.setattr(effect, "LIVE", ax)
    monkeypatch.setattr(effect, "window_snapshot", lambda: {})
    monkeypatch.setattr(fastinput, "has_accessibility", lambda: True)
    monkeypatch.setattr(skills, "_wait_for_page", lambda: None)
    monkeypatch.setattr(skills, "_focus_web_editor", lambda: False)
    monkeypatch.setattr(skills, "_no_text_field", lambda: None)
    monkeypatch.setattr(skills, "_visible_text", lambda target=None: "")
    monkeypatch.setattr(skills, "_verify_typed", lambda text, before, target=None: " (not verified: test).")
    monkeypatch.setattr(skills, "_name_untitled_doc", lambda text: "")
    monkeypatch.setattr(fx_mod.fx, "highlight_focused", lambda **k: None)
    monkeypatch.setattr(axkit, "attr", ax.attr)
    monkeypatch.setattr(axkit, "focused_element", lambda pid=None: t.field)
    try:
        from mint.screen import ocr
        from mint.tools import undo
    except ImportError:
        from mint import ocr, undo
    monkeypatch.setattr(ocr, "_front_window", lambda: None)
    monkeypatch.setattr(undo, "typed", lambda *a, **k: None)
    monkeypatch.setattr(fastinput, "press_key", lambda key, mods=None, times=1, pid=None: t.keys.append(key))

    def type_unicode(text, pid=None, gap=0):
        t.unicode.append(text)
        if not t.field.refuse_keys:
            t.field.attrs["AXValue"] = (t.field.attrs.get("AXValue") or "") + text
        return len(text)

    monkeypatch.setattr(fastinput, "type_unicode", type_unicode)

    class Clip:
        def __enter__(self):
            self.board = types.SimpleNamespace(clearContents=lambda: None,
                                               setString_forType_=lambda s, k: t.pasted.append(s))
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(skills, "_Clipboard", Clip)
    return t


def _focused(value="", **kw):
    node = Node("AXTextArea", AXValue=value, AXFocused=True, **kw)
    node.refuse_keys = False
    return node


def test_type_text_inserts_at_the_caret_through_accessibility(typing):
    typing.field = _focused("Dear ")
    typing.field.selection = (5, 0)
    out = skills.type_text("Alex,", press_return=False)
    assert out.startswith("CONFIRMED via accessibility (background): Typed 5 characters"), out
    assert typing.field.get("AXValue") == "Dear Alex," and typing.pasted == [] and typing.unicode == []


def test_type_text_ladder_keys_then_paste(typing):
    typing.field = _focused("")
    typing.field.refuse_write = True
    out = skills.type_text("hello there")
    assert typing.unicode == ["hello there"] and typing.pasted == []
    assert out.startswith("CONFIRMED via key events sent to the app")
    typing.field = _focused("")
    typing.field.refuse_write, typing.field.refuse_keys = True, True
    out = skills.type_text("hello again")
    assert typing.pasted == ["hello again"], out          # last resort only
    assert out.startswith("UNVERIFIED via the real mouse/keyboard")


def test_type_text_refuses_passwords_and_skips_chromium(typing):
    typing.field = _focused("", AXSubrole="AXSecureTextField")
    assert skills.type_text("hunter2").startswith("FAILED") and typing.ax.writes == []
    typing.field = _focused("")
    typing.ax.is_chromium = True
    skills.type_text("hello there")
    assert typing.pasted == ["hello there"] and typing.ax.writes == [] and typing.unicode == []


# --- live (opt-in): MINT_LIVE_UI=1, an idle Mac, a process with Accessibility -----------------------------
#
# Only apps these tests start themselves: Calculator and a new, never-saved TextEdit document. Both are
# skipped if the app is already running (the user's own windows are never touched). They check that the
# user's pointer did not move and the front app did not change.

import os  # noqa: E402
import subprocess  # noqa: E402
import time as _time  # noqa: E402

live = pytest.mark.skipif(os.environ.get("MINT_LIVE_UI") != "1", reason="live UI test: set MINT_LIVE_UI=1")


def _idle() -> int:
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            return int(int(line.split()[-1]) / 1e9)
    return 0


def _live_ready(app_name: str):
    import AppKit
    import ApplicationServices as AX
    if not AX.AXIsProcessTrusted():
        pytest.skip("this process has no Accessibility permission")
    if _idle() < 120:
        pytest.skip("the Mac is in use (idle < 120 s)")
    if any(a.localizedName() == app_name for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()):
        pytest.skip(f"{app_name} is already running - not touching the user's window")


def _running(name: str, wait: float = 6.0):
    import AppKit
    deadline = _time.monotonic() + wait
    while _time.monotonic() < deadline:
        app = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                    if a.localizedName() == name), None)
        if app is not None and axkit.focused_window(app.processIdentifier()) is not None:
            return app
        _time.sleep(0.2)
    pytest.fail(f"{name} did not open a window")


def _front_pid():
    import AppKit
    return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier()


@live
def test_live_calculator_presses_buttons_without_the_cursor():
    _live_ready("Calculator")
    subprocess.run(["open", "-g", "-a", "Calculator"], check=False)       # -g: stays in the background
    app = _running("Calculator")
    try:
        window = axkit.focused_window(app.processIdentifier())
        buttons = {}
        for node in axkit.walk(window):
            if axkit.attr(node, "AXRole") == "AXButton":
                for name in (axkit.attr(node, "AXDescription"), axkit.attr(node, "AXTitle"),
                             axkit.attr(node, "AXIdentifier")):
                    if isinstance(name, str) and name:
                        buttons.setdefault(name.lower(), node)
        sequence = [k for k in ("all clear", "clear") if k in buttons][:1] + ["2", "add", "3", "equals"]
        pointer, front, moved = effect.pointer_at(), _front_pid(), 0
        for key in sequence:
            done, _, problem = effect.ax_deliver(buttons[key], "AXPress")
            assert done, f"{key}: {problem}"
            moved += effect.pointer_at() != pointer
        _time.sleep(0.3)
        shown = [str(axkit.attr(n, "AXValue") or axkit.attr(n, "AXDescription") or "")
                 for n in axkit.walk(window) if axkit.attr(n, "AXRole") == "AXStaticText"]
        print(f"\nCalculator display read back: {shown}; pointer moves: {moved}; front unchanged: "
              f"{_front_pid() == front}")
        assert any(s.strip().replace("‎", "") == "5" for s in shown), shown
        assert moved == 0 and _front_pid() == front
    finally:
        app.terminate()


@live
def test_live_textedit_types_in_the_background_without_the_cursor():
    _live_ready("TextEdit")
    subprocess.run(["osascript", "-e", 'tell application "TextEdit" to make new document'], check=False)
    app = _running("TextEdit")
    try:
        window = axkit.focused_window(app.processIdentifier())
        area = next(n for n in axkit.walk(window) if axkit.attr(n, "AXRole") == "AXTextArea")
        sentence = "The quick brown fox types without a cursor."
        pointer, front = effect.pointer_at(), _front_pid()
        progress, count, note = effect.ax_insert(area, sentence, at="end")
        moved = effect.pointer_at() != pointer
        value = axkit.attr(area, "AXValue")
        print(f"\nTextEdit: {progress} ({count}) {note}; read back {value!r}; pointer moved: {moved}; "
              f"TextEdit in front: {_front_pid() == app.processIdentifier()}")
        assert progress == "complete" and value == sentence
        assert not moved and _front_pid() == front
        # and the whole ui_act path, with TextEdit still behind the user's windows
        out = ground.act("type", "the text area", text=" Twice.", replace=False, app=app)
        print("ui_act:", out)
        assert out.startswith("CONFIRMED via accessibility") and effect.pointer_at() == pointer
        assert _front_pid() == front
    finally:
        subprocess.run(["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
                       check=False)
        app.terminate()
