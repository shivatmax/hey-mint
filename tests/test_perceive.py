"""Perception (cua-style): a bounded, flattened accessibility walk that indexes only usable controls,
AXChildren + AXWindows at the app root, no sibling window's tree when the asked-for window is gone,
snapshot tokens that go stale, +/~/- diffs, Chromium switch-on cached per process lifetime, and
window pictures that must be a 1x/2x rendering of what they claim to show. All with fake AX trees."""
import types

import pytest

try:
    from mint.screen import axkit, ground
except ImportError:
    from mint import axkit, ground
try:
    from mint.screen import capture
except ImportError:
    from mint import capture


# --- a fake accessibility tree --------------------------------------------------------------------

class Node:
    def __init__(self, role, title="", box=(0, 0, 10, 10), kids=(), value=None, desc="", enabled=True,
                 actions=("AXPress",), settable=False, subrole="", wid=None, placeholder=""):
        self.role, self.title, self.box, self.kids = role, title, box, list(kids)
        self.value, self.desc, self.enabled, self.actions = value, desc, enabled, actions
        self.settable, self.subrole, self.wid, self.placeholder = settable, subrole, wid, placeholder

    def attrs(self):
        return {"AXRole": self.role, "AXSubrole": self.subrole, "AXTitle": self.title,
                "AXDescription": self.desc, "AXValue": self.value, "AXPlaceholderValue": self.placeholder,
                "AXHelp": None, "AXEnabled": self.enabled, "AXFocused": False, "AXChildren": self.kids,
                "AXRoleDescription": "", "_box": self.box}


class App:
    def __init__(self, name="Fake", pid=4242):
        self.name, self.pid = name, pid

    def processIdentifier(self):  # noqa: N802 - AppKit's name
        return self.pid

    def localizedName(self):  # noqa: N802
        return self.name


@pytest.fixture
def tree(monkeypatch):
    """Install a fake AX world: returns a function (window, other windows...) -> app."""
    world = {"focused": None, "windows": [], "root_kids": []}
    reads = []

    def values(node):
        reads.append(node)
        return node.attrs()
    monkeypatch.setattr(ground, "_values", values)
    monkeypatch.setattr(ground, "_box", lambda v: v.get("_box"))
    monkeypatch.setattr(ground, "_actions", lambda n: list(n.actions) if n.actions is not None else None)
    monkeypatch.setattr(ground, "_settable", lambda n, name="AXValue": n.settable)
    monkeypatch.setattr(ground, "is_own", lambda app: False)
    monkeypatch.setattr(ground, "AX", types.SimpleNamespace(AXUIElementCreateApplication=lambda pid: "APP"))
    monkeypatch.setattr(axkit, "unlock", lambda app=None, wait=0.6: False)
    monkeypatch.setattr(axkit, "bound", lambda e, seconds=2.0: e)
    monkeypatch.setattr(axkit, "attr", lambda n, name: (world["root_kids"] if n == "APP" and name == "AXChildren"
                                                        else n.attrs().get(name) if isinstance(n, Node) else None))
    monkeypatch.setattr(axkit, "frame", lambda n: n.box)
    monkeypatch.setattr(axkit, "focused_window", lambda pid: world["focused"])
    monkeypatch.setattr(axkit, "window_id", lambda w: getattr(w, "wid", None))
    monkeypatch.setattr(axkit, "window_by_id", lambda pid, wid: next(
        (w for w in world["windows"] if w.wid == wid), None))

    def install(window, *others):
        world["focused"] = window
        world["windows"] = [window, *others]
        return App()
    install.reads = reads
    install.world = world
    return install


def _window(*kids, wid=7, title="Main"):
    return Node("AXWindow", title, (0, 0, 800, 600), kids, wid=wid)


# --- 1. the walk -------------------------------------------------------------------------------------

def test_layout_wrappers_do_not_spend_depth(tree):
    deep = Node("AXButton", "Deep Install", (100, 100, 60, 20))
    for _ in range(ground.WALK_DEPTH + 15):            # deeper than the depth cap, but only wrappers
        deep = Node("AXGroup", "", (0, 0, 800, 600), [deep])
    app = tree(_window(deep))
    inv = ground.inventory(app)
    assert [e["label"] for e in inv["elements"]] == ["Deep Install"]
    assert not inv["truncated"] and not inv["degraded"]


def test_named_nesting_still_has_a_depth_cap(tree):
    deep = Node("AXButton", "Too Deep", (100, 100, 60, 20))
    for i in range(ground.WALK_DEPTH + 5):
        deep = Node("AXGroup", f"level {i}", (0, 0, 800, 600), [deep])
    inv = ground.inventory(tree(_window(deep)))
    assert "Too Deep" not in [e["label"] for e in inv["elements"]]


def test_only_usable_controls_are_candidates(tree):
    kids = [Node("AXButton", "Press me", (10, 10, 80, 20)),
            Node("AXButton", "Decoration", (10, 40, 80, 20), actions=()),          # no actions at all
            Node("AXTextField", "Name", (10, 70, 200, 20), actions=(), settable=True),
            Node("AXButton", "Greyed", (10, 100, 80, 20), enabled=False),
            Node("AXButton", "Unknown", (10, 130, 80, 20), actions=None),          # could not ask: kept
            Node("AXStaticText", "", (10, 160, 200, 20), value="Just some context", actions=())]
    inv = ground.inventory(tree(_window(*kids)))
    by = {e["label"]: e for e in inv["elements"]}
    assert by["Press me"]["actionable"] and by["Name"]["actionable"] and by["Unknown"]["actionable"]
    assert by["Decoration"]["actionable"] is False and by["Greyed"]["actionable"] is False
    assert by["Just some context"]["actionable"] is False                # context, kept for reading
    pool = [e["label"] for e in ground.candidates(inv, "click")]
    assert "Press me" in pool and "Just some context" in pool
    assert "Decoration" not in pool and "Greyed" not in pool
    assert [e["label"] for e in ground.candidates(inv, "type")] == ["Name"]


def test_hidden_inputs_and_rows_survive_the_new_walk(tree):
    row = Node("AXGroup", "Rainbow CSV, 3.1.0", (10, 200, 300, 60),
               [Node("AXButton", "Install", (250, 230, 50, 20))])
    search = Node("AXGroup", "", (10, 90, 300, 40),
                  [Node("AXStaticText", "", (60, 100, 220, 20), value="Search Extensions in Marketplace",
                        actions=()),
                   Node("AXTextArea", "", (64, 108, 1, 1), actions=())])
    inv = ground.inventory(tree(_window(search, row)))
    hidden = next(e for e in inv["elements"] if e.get("hidden"))
    assert hidden["label"] == "Search Extensions in Marketplace" and hidden["box"] == (60, 100, 220, 20)
    install = next(e for e in inv["elements"] if e["label"] == "Install")
    assert install["within"].startswith("Rainbow CSV")


def test_node_and_time_budgets_stop_the_walk_and_say_so(tree, monkeypatch):
    kids = [Node("AXButton", f"b{i}", (10, 10 + i * 4, 40, 3.5)) for i in range(60)]
    app = tree(_window(*kids))
    inv = ground.inventory(app, limit=10)
    assert inv["walked"] == 10 and "10 elements" in inv["truncated"]
    clock = iter(range(0, 10_000))
    monkeypatch.setattr(ground.time, "monotonic", lambda: next(clock) * 0.1)   # every read takes 0.1 s
    inv = ground.inventory(app, budget=1.0)
    assert inv["walked"] < 20 and "stopped after 1 s" in inv["truncated"]


def test_waiting_for_electron_does_not_eat_the_walk_budget(tree, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(ground.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(axkit, "unlock", lambda app=None, wait=0.6: now.__setitem__(0, now[0] + 4.5))
    inv = ground.inventory(tree(_window(Node("AXButton", "Run", (5, 5, 50, 20)))))
    assert [e["label"] for e in inv["elements"]] == ["Run"] and not inv["truncated"]


# --- 2. which window -------------------------------------------------------------------------------

def test_app_root_unions_children_and_windows(monkeypatch):
    class Elem:
        def __init__(self, name):
            self.name = name

        def __eq__(self, other):              # CFEqual: another proxy of the same element
            return isinstance(other, Elem) and other.name == self.name

        __hash__ = None
    menubar, main, background = Elem("menubar"), Elem("main"), Elem("background")
    values = {"AXChildren": [menubar, Elem("main")], "AXWindows": [main, background]}
    monkeypatch.setattr(axkit, "AX", types.SimpleNamespace(AXUIElementCreateApplication=lambda pid: "APP",
                                                           AXUIElementSetMessagingTimeout=lambda e, s: 0))
    monkeypatch.setattr(axkit, "attr", lambda e, name: values.get(name) if e == "APP" else
                        ("AXWindow" if name == "AXRole" and e.name != "menubar" else None))
    top = axkit.top_level(1)
    assert [e.name for e in top] == ["menubar", "main", "background"]       # no duplicate "main"
    monkeypatch.setattr(axkit, "window_id", lambda e: {"main": 11, "background": 12}.get(e.name))
    assert axkit.window_by_id(1, 12) is background
    assert axkit.window_by_id(1, 99) is None


def test_missing_window_is_empty_and_degraded_not_another_window(tree):
    front = _window(Node("AXButton", "Delete everything", (10, 10, 80, 20)), wid=7)
    app = tree(front)
    inv = ground.inventory(app, window_id=99)
    assert inv["elements"] == [] and "window 99" in inv["degraded"]
    assert not tree.reads                                  # the front window was never even read
    assert "Nothing read" in _summary_of(inv)
    ok = ground.inventory(app, window_id=7)
    assert ok["window_id"] == 7 and ok["elements"][0]["label"] == "Delete everything"


def _summary_of(inv):
    original = ground.inventory
    try:
        ground.inventory = lambda app=None: inv
        return ground.summary()
    finally:
        ground.inventory = original


# --- 3. snapshot tokens and diffs ------------------------------------------------------------------

def test_tokens_resolve_and_go_stale(tree):
    install = Node("AXButton", "Install", (250, 230, 50, 20))
    app = tree(_window(install, Node("AXButton", "Cancel", (10, 10, 60, 20))))
    first = ground.inventory(app)
    tok = next(e["token"] for e in first["elements"] if e["label"] == "Install")
    assert tok.startswith(first["snapshot"] + ":")
    e, why = ground.resolve(tok)
    assert e is not None and e["label"] == "Install" and not why
    assert ground.resolve(f"[{tok}]")[0] is e
    second = ground.inventory(app)
    e, why = ground.resolve(tok)
    assert e is None and "stale" in why
    new_tok = next(x["token"] for x in second["elements"] if x["label"] == "Install")
    assert f"now {new_tok}" in why                         # tells the model the fresh id
    plain, _ = ground.resolve(new_tok.split(":")[1])       # a bare number means the newest reading
    assert plain is not None and plain["label"] == "Install"
    assert ground.resolve("click the thing")[0] is None
    # Reading ANOTHER window does not make this window's tokens stale.
    tree.world["windows"].append(_window(Node("AXButton", "Other", (5, 5, 50, 20)), wid=8))
    ground.inventory(app, window_id=8)
    assert ground.resolve(new_tok)[0] is not None


def test_old_snapshots_are_forgotten(tree):
    app = tree(_window(Node("AXButton", "A", (1, 1, 20, 20))))
    first = ground.inventory(app)
    for _ in range(ground.KEEP_SNAPSHOTS + 1):
        ground.inventory(app)
    e, why = ground.resolve(first["elements"][0]["token"])
    assert e is None and "no longer kept" in why


def test_diff_reports_added_changed_removed(tree):
    field = Node("AXTextField", "Search", (10, 10, 200, 20), value="", actions=(), settable=True)
    cancel = Node("AXButton", "Cancel", (10, 40, 60, 20))
    window = _window(field, cancel)
    app = tree(window)
    before = ground.inventory(app)
    assert ground.diff_text(before, ground.inventory(app)).startswith("no change since")
    before = ground.inventory(app)
    field.value = "csv"
    window.kids.remove(cancel)
    window.kids.append(Node("AXButton", "Install", (10, 70, 60, 20)))
    after = ground.inventory(app)
    d = ground.diff(before, after)
    assert [e["label"] for e in d["added"]] == ["Install"]
    assert [e["label"] for e in d["removed"]] == ["Cancel"]
    assert [(n["label"], what) for _, n, what in d["changed"]] == [("Search", "was ''")]
    text = ground.diff_text(before, after)
    install_tok = next(e["token"] for e in after["elements"] if e["label"] == "Install")
    assert f"+ [{install_tok}] button 'Install'" in text
    assert "~ [" in text and "text field 'Search' = 'csv'" in text
    assert "- [" + before["snapshot"] + ":" in text                       # gone rows keep their old id
    assert "1 added, 1 changed, 1 removed" in text


def test_ui_elements_since(tree, monkeypatch):
    button = Node("AXButton", "Install", (250, 230, 50, 20))
    window = _window(button)
    app = tree(window)
    monkeypatch.setattr(ground, "front_app", lambda: app)
    full = ground.summary()
    snap = full.split("[snapshot ")[1].split("]")[0]
    assert f"[{snap}:1] button 'Install'" in full
    button.enabled = False
    changed = ground.summary(since=snap)
    assert "~ [" in changed and "now disabled" in changed and "1 changed" in changed
    button.enabled = True
    unknown = ground.summary(since="s999999")
    assert "unknown" in unknown and "button 'Install'" in unknown


def test_summary_lines_are_compact_tokens():
    e = {"role": "AXButton", "kind": "button", "label": "Install", "value": "", "hint": "", "box": (0, 0, 9, 9),
         "enabled": True, "focused": False, "in_dialog": False, "within": "Rainbow CSV", "id": 7,
         "token": "s12:7", "actionable": True, "interactive": True}
    assert ground.describe(e, token=True) == "[s12:7] button 'Install' in 'Rainbow CSV'"
    assert ground.describe(e) == "[7] button 'Install' in 'Rainbow CSV'"     # vision's numbered marks
    text = {**e, "role": "AXStaticText", "kind": "text", "within": "", "actionable": False, "interactive": False}
    assert ground.describe(text, token=True) == "  text 'Install'"           # context: no id to act on


# --- 4. Chromium switch-on, once per process lifetime ---------------------------------------------

def test_enablement_cache_is_keyed_by_process_start():
    now = 1000.0
    done = {"stamp": (100, 1), "done": True, "attempts": 0, "at": now}
    assert axkit._next_attempt(done, (100, 1), now + 3600) is None           # same process: skip
    assert axkit._next_attempt(done, (200, 5), now) == 0                     # pid reused: switch on again
    assert axkit._next_attempt(done, None, now) == 0                         # unreadable: never trusted
    assert axkit._next_attempt(None, (100, 1), now) == 0
    timed_out = {"stamp": (100, 1), "done": False, "attempts": 1, "at": now}
    assert axkit._next_attempt(timed_out, (100, 1), now + 1) is None         # backing off
    assert axkit._next_attempt(timed_out, (100, 1), now + axkit.RETRY_AFTER) == 1
    spent = {**timed_out, "attempts": axkit.MAX_ATTEMPTS}
    assert axkit._next_attempt(spent, (100, 1), now + 1e6) is None


def test_tree_is_polled_reasserted_and_bounded():
    log = []
    answers = iter([False, False, True])
    assert axkit._await_tree(lambda: log.append("probe") or next(answers),
                             lambda: log.append("reassert"), lambda s: log.append(s))
    assert log == ["probe", axkit.MATERIALIZE_POLL, "probe", axkit.MATERIALIZE_POLL, "probe", "reassert",
                   axkit.SETTLE]
    log.clear()
    assert not axkit._await_tree(lambda: log.append("probe") or False, lambda: log.append("reassert"),
                                 lambda s: log.append(s))
    assert log.count("reassert") == 1                                       # once, halfway
    assert round(sum(x for x in log if isinstance(x, float)), 6) == axkit.MATERIALIZE_TIMEOUT


def test_unlock_switches_on_once_per_lifetime(monkeypatch):
    sets, stamp = [], [(100, 1)]
    monkeypatch.setattr(axkit, "_unlocked", {})
    monkeypatch.setattr(axkit, "is_chromium_app", lambda app: True)
    monkeypatch.setattr(axkit, "process_start", lambda pid: stamp[0])
    monkeypatch.setattr(axkit, "has_web_content", lambda e: True)
    monkeypatch.setattr(axkit.time, "sleep", lambda s: None)
    monkeypatch.setattr(axkit, "AX", types.SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: "APP", AXUIElementSetMessagingTimeout=lambda e, s: 0,
        AXUIElementSetAttributeValue=lambda e, name, v: sets.append(name) or 0))
    app = App()
    assert axkit.unlock(app) is True
    assert axkit.unlock(app) is False and sets.count("AXManualAccessibility") == 2   # flip + one re-assert
    stamp[0] = (300, 9)                                                    # same pid, new process
    assert axkit.unlock(app) is True


def test_older_electron_gets_the_enhanced_switch(monkeypatch):
    sets = []

    def set_attr(e, name, value):
        sets.append(name)
        return -25205 if name == "AXManualAccessibility" else 0           # attribute unsupported
    monkeypatch.setattr(axkit, "AX", types.SimpleNamespace(AXUIElementSetAttributeValue=set_attr))
    assert axkit._switch_on("APP") == "enhanced" and sets == ["AXManualAccessibility", "AXEnhancedUserInterface"]
    sets.clear()
    monkeypatch.setattr(axkit, "AX", types.SimpleNamespace(
        AXUIElementSetAttributeValue=lambda e, n, v: sets.append(n) or -25204))   # busy: no fallback
    assert axkit._switch_on("APP") == "" and sets == ["AXManualAccessibility"]


def test_process_start_reads_this_process():
    import os
    assert axkit.process_start(os.getpid()) is not None
    assert axkit.process_start(0) is None


# --- 5. window pictures --------------------------------------------------------------------------

@pytest.mark.parametrize("px,bounds,scale", [((1600, 1200), (0, 0, 800, 600), 2.0),
                                             ((800, 600), (5, 5, 800, 600), 1.0),
                                             ((1601, 1199), (0, 0, 800, 600), 2.0)])
def test_px_frame_accepts_1x_and_2x(px, bounds, scale):
    assert capture.frame_scale(*px, bounds) == scale


@pytest.mark.parametrize("px,bounds", [((1760, 1320), (0, 0, 800, 600)),     # 2.2x: a parent window's picture
                                       ((1600, 600), (0, 0, 800, 600)),      # 2x wide but 1x tall
                                       ((1650, 1238), (0, 0, 800, 600)),     # 2.06x: just past the slack
                                       ((2400, 1800), (0, 0, 800, 600)),     # 3x: not a Mac window picture
                                       ((1623, 1183), (0, 0, 800, 600)),     # each near 2x, but not alike
                                       ((0, 0), (0, 0, 800, 600)), ((800, 600), (0, 0, 0, 600))])
def test_px_frame_refuses_mismatch(px, bounds):
    with pytest.raises(capture.FrameMismatch, match="capture/window mismatch"):
        capture.frame_scale(*px, bounds)


def test_shot_maps_pixels_back_through_crop_and_fit():
    PIL = pytest.importorskip("PIL.Image")
    shot = capture._shot(PIL.new("RGB", (1600, 1200)), (100, 50, 800, 600), 7, "window")
    assert capture.to_points(shot, 200, 100) == (200, 100)
    zoom = capture.crop(shot, (300, 250, 40, 20), pad=0.5)
    assert zoom["bounds"] == (280, 240, 80, 40) and zoom["image"].size == (160, 80)
    assert capture.to_points(zoom, 40, 20) == (300, 250)
    big = capture.fit(zoom, 480)
    assert big["image"].size == (480, 240) and capture.to_points(big, 120, 60) == (300, 250)
    with pytest.raises(capture.CaptureError):
        capture.crop(shot, (5000, 5000, 10, 10))


def test_window_stack_keeps_the_apps_sheets_and_drops_other_windows():
    def row(n, pid, x, y, w, h, alpha=1.0):
        return {"kCGWindowNumber": n, "kCGWindowOwnerPID": pid, "kCGWindowAlpha": alpha,
                "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}
    rows = [row(1, 99, 0, 0, 1440, 900),          # Mint's overlay, on top of everything: never
            row(2, 10, 200, 150, 300, 200),       # the app's sheet over its window: yes
            row(3, 10, 2000, 0, 100, 100),        # the app's window elsewhere: no
            row(4, 10, 0, 0, 100, 100, alpha=0),  # invisible: no
            row(5, 20, 100, 100, 200, 200),       # another app on top: no
            row(6, 10, 100, 100, 800, 600),       # the target
            row(7, 10, 100, 100, 800, 600)]       # behind the target: no
    assert capture.stack_for(6, (100, 100, 800, 600), rows) == [2, 6]
    assert capture.stack_for(42, None, rows) == [42]


def test_area_falls_back_to_the_screen_grab(monkeypatch):
    PIL = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(capture, "stack_for", lambda wid, area=None, rows=None: [wid])
    monkeypatch.setattr(capture, "window_bounds", lambda wid: (0, 0, 800, 600))
    region = {"image": None, "scale": 1.0, "bounds": (0, 0, 800, 600), "window_id": None, "method": "region"}
    monkeypatch.setattr(capture, "_region", lambda area: region)
    monkeypatch.setattr(capture, "_cg", lambda ids, area: PIL.new("RGB", (1760, 1320)))   # 2.2x: refused
    monkeypatch.setattr(capture, "_sck", lambda wid: None)
    monkeypatch.setattr(capture, "_screencapture", lambda wid: None)
    assert capture.area((0, 0, 800, 600), 5)["method"] == "region"
    monkeypatch.setattr(capture, "_cg", lambda ids, area: PIL.new("RGB", (1600, 1200)))
    shot = capture.area((0, 0, 800, 600), 5)
    assert shot["method"] == "window" and shot["scale"] == 2.0 and shot["window_id"] == 5
    assert capture.area((0, 0, 800, 600), None)["method"] == "region"


# --- fix-see: the window of a never-active app, scrolled-out rows, web names, window-only text ------

class _Elem:
    """An AX element whose attributes are a dict (CFEqual by name)."""

    def __init__(self, name, **values):
        self.name, self.values = name, values

    def __eq__(self, other):
        return isinstance(other, _Elem) and other.name == self.name

    __hash__ = None


def _app_world(monkeypatch, app_values):
    monkeypatch.setattr(axkit, "AX", types.SimpleNamespace(AXUIElementCreateApplication=lambda pid: "APP",
                                                           AXUIElementSetMessagingTimeout=lambda e, s: 0))
    monkeypatch.setattr(axkit, "attr", lambda e, name: app_values.get(name) if e == "APP" else e.values.get(name))


def test_never_active_app_window_main_then_first_window(monkeypatch):
    main, other = _Elem("main", AXRole="AXWindow"), _Elem("other", AXRole="AXWindow")
    _app_world(monkeypatch, {"AXFocusedWindow": None, "AXMainWindow": main, "AXWindows": [other]})
    assert axkit.focused_window(4242) is main
    # Opened in the background and never active: no focused or main window - its first shown window.
    mini = _Elem("mini", AXRole="AXWindow", AXMinimized=True)
    menubar = _Elem("menubar", AXRole="AXMenuBar")
    _app_world(monkeypatch, {"AXWindows": [mini, other], "AXChildren": [menubar]})
    monkeypatch.setattr(axkit, "server_windows", lambda pid: pytest.fail("not needed"))
    assert axkit.focused_window(4242) is other


def test_never_active_app_window_from_the_window_server_only_its_own(monkeypatch):
    _app_world(monkeypatch, {"AXWindows": [], "AXChildren": []})
    found = _Elem("found", AXRole="AXWindow")
    asked = []
    monkeypatch.setattr(axkit, "server_windows", lambda pid: asked.append(("list", pid)) or [55, 56])
    monkeypatch.setattr(axkit, "window_by_id", lambda pid, wid: asked.append((pid, wid)) or (found if wid == 56 else None))
    assert axkit.focused_window(4242) is found
    assert asked == [("list", 4242), (4242, 55), (4242, 56)]
    monkeypatch.setattr(axkit, "server_windows", lambda pid: [])
    assert axkit.focused_window(4242) is None


def test_window_by_id_probes_only_a_window_the_server_gives_that_app(monkeypatch):
    _app_world(monkeypatch, {"AXWindows": [], "AXChildren": []})
    probed = []
    monkeypatch.setattr(axkit, "_token_window", lambda pid, wid: probed.append((pid, wid)) or "W")
    monkeypatch.setattr(axkit, "server_owner", lambda wid: 999)           # another app's window
    assert axkit.window_by_id(4242, 55) is None and probed == []
    monkeypatch.setattr(axkit, "server_owner", lambda wid: 4242)
    assert axkit.window_by_id(4242, 55) == "W" and probed == [(4242, 55)]
    assert axkit.window_by_id(4242, 55, probe=False) is None


def _list_window():
    def row(title, y):
        return Node("AXGroup", "", (0, y, 400, 36), [Node("AXButton", "Install", (300, y + 4, 80, 28))],
                    desc=title)
    scroll = Node("AXScrollArea", "", (0, 100, 400, 200), [
        row("CSV Colors", 100), row("Half Seen", 276), row("Live Server", 290), row("SQL Formatter", 400)])
    return _window(scroll, Node("AXButton", "Done", (500, 500, 60, 24)))


def test_rows_scrolled_out_of_their_list_are_off_screen_not_candidates(tree):
    inv = ground.inventory(tree(_list_window()))
    installs = [e for e in inv["elements"] if e["label"] == "Install"]
    assert sorted(e["within"] for e in installs) == ["CSV Colors", "Half Seen"]
    half = next(e for e in installs if e["within"] == "Half Seen")
    assert half["box"] == (300, 280, 80, 20)                # only the part that can be seen
    hidden = {o["within"] for o in inv["offscreen"]}
    assert hidden == {"Live Server", "SQL Formatter"}      # centre below the list's bottom edge
    pool = ground.candidates(inv, "click")
    assert all(e.get("within") not in hidden for e in pool)
    text = _summary_of(inv)
    assert "Off screen (scroll to it):" in text and "'Install' in 'SQL Formatter'" in text
    assert "SQL Formatter" in ground.offscreen_text(inv, "Install SQL Formatter")
    assert ground.offscreen_text(inv, "Apply theme") == ""


def test_scrolling_to_a_named_row_takes_that_rows_control(monkeypatch):
    sql, live = object(), object()
    inv = {"dialog": None, "window": (0, 0, 800, 600), "pid": None, "window_id": None, "offscreen": [
        {"label": "Install", "role": "AXButton", "ref": live, "box": (300, 294, 80, 28), "within": "Live Server"},
        {"label": "Install", "role": "AXButton", "ref": sql, "box": (300, 404, 80, 28), "within": "SQL Formatter"}]}
    pressed = []
    monkeypatch.setattr(ground, "AX", types.SimpleNamespace(
        AXUIElementPerformAction=lambda ref, action: pressed.append((ref, action)) or 0))
    monkeypatch.setattr(ground.time, "sleep", lambda s: None)
    fresh_els = [{"label": "Install", "within": w, "role": "AXButton", "box": (300, 120 + 36 * i, 80, 28),
                  "enabled": True, "interactive": True, "actionable": True, "in_dialog": False}
                 for i, w in enumerate(["Live Server", "SQL Formatter"])]
    fresh = {"dialog": None, "window": (0, 0, 800, 600), "elements": fresh_els}
    monkeypatch.setattr(ground, "inventory", lambda *a, **k: fresh)
    hit, _, why = ground._scroll_to_named("Install SQL Formatter", "click", inv)
    assert pressed == [(sql, "AXScrollToVisible")] and hit["within"] == "SQL Formatter" and "scrolled" in why
    pressed.clear()
    assert ground._scroll_to_named("Install", "click", inv) is None and pressed == []   # two alike: no guess
    assert ground._scroll_to_named("Install Git History", "click", inv) is None and pressed == []
    inv["offscreen"] = inv["offscreen"][:1]                 # only another row's Install is out of view
    assert ground._scroll_to_named("Install SQL Formatter", "click", inv) is None and pressed == []


class WebNode(Node):
    def __init__(self, role, title="", box=(0, 0, 10, 10), kids=(), extra=None, **kw):
        super().__init__(role, title, box, kids, **kw)
        self.extra = extra or {}

    def attrs(self):
        return {**super().attrs(), **self.extra}


def test_web_controls_are_named_from_text_value_label_or_dom_id_and_flagged(tree):
    label = Node("AXStaticText", "", (10, 300, 40, 20), value="Email", actions=())
    page = WebNode("AXWebArea", "", (0, 0, 800, 600), [
        WebNode("AXButton", "", (10, 100, 80, 24), [
            Node("AXGroup", "", (10, 100, 80, 24), [Node("AXStaticText", "", (12, 102, 60, 20), value="Install",
                                                         actions=())])]),
        WebNode("AXButton", "", (100, 100, 80, 24), value="Save draft"),
        WebNode("AXButton", "", (200, 100, 80, 24), extra={"AXDOMIdentifier": "btn-export-csv"}),
        WebNode("AXTextField", "", (60, 300, 200, 22), extra={"AXTitleUIElement": label}, settable=True),
        WebNode("AXLink", "", (300, 100, 80, 24), extra={"AXHelp": "Open the changelog"}),
    ])
    native = Node("AXButton", "", (500, 500, 60, 24), [Node("AXStaticText", "", (505, 505, 40, 14),
                                                            value="Quit", actions=())])
    inv = ground.inventory(tree(_window(Node("AXScrollArea", "", (0, 0, 800, 600), [page]), native)))
    by = {e["label"]: e for e in inv["elements"] if e["interactive"]}
    assert {"Install", "Save draft", "export csv", "Email", "Open the changelog", "Quit"} <= set(by)
    assert all(by[n]["in_web_content"] for n in ("Install", "Save draft", "export csv", "Email",
                                                 "Open the changelog"))
    assert by["Quit"]["in_web_content"] is False
    assert by["Email"]["role"] == "AXTextField" and by["Email"]["value"] == ""
    assert not inv["web_pending"]
    empty = ground.inventory(tree(_window(WebNode("AXWebArea", "", (0, 0, 800, 600), []))))
    assert empty["web_pending"]                        # WebKit's tree not built yet: ground looks again


# --- fix-see: on-screen text belongs to the window that draws it ------------------------------------

try:
    from mint.screen import ocr
except ImportError:
    from mint import ocr


def test_screen_text_under_another_apps_window_is_not_the_front_apps():
    front = {"x": 0, "y": 0, "w": 1000, "h": 800, "app": "Browser", "pid": 1}
    rows = [{"x": 600, "y": 400, "w": 300, "h": 200, "app": "MintFixture", "pid": 2},   # raised, not active
            {"x": 0, "y": 0, "w": 1000, "h": 800, "app": "Browser", "pid": 1}]
    covered = {"text": "Save as…", "x": 650, "y": 450, "w": 60, "h": 20}
    plain = {"text": "Bookmarks", "x": 50, "y": 50, "w": 80, "h": 20}
    assert not ocr._seen_in(covered, front, rows) and ocr._seen_in(plain, front, rows)
    assert ocr._top_at(rows, 660, 460)["app"] == "MintFixture"
    assert ocr._seen_in(covered, front, None)           # a window-only read: bounds alone decide


class _RunningApp:
    def __init__(self, pid=4242, name="MintFixture"):
        self.pid, self.name = pid, name

    def processIdentifier(self):  # noqa: N802
        return self.pid

    def localizedName(self):  # noqa: N802
        return self.name


def test_read_window_reads_the_apps_own_window_and_maps_through_its_frame(monkeypatch):
    PIL = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(axkit, "focused_window", lambda pid: "W")
    monkeypatch.setattr(axkit, "window_id", lambda w: 77)
    monkeypatch.setattr(capture, "window_bounds", lambda wid: (600.0, 400.0, 300.0, 200.0))
    picture = PIL.new("RGB", (600, 400), "white")
    picture.paste((0, 0, 0), (100, 100, 200, 140))
    asked = []

    def area(a, wid):
        asked.append((a, wid))
        return {"image": picture, "scale": 2.0, "bounds": a, "window_id": wid, "method": "window"}
    monkeypatch.setattr(capture, "area", area)
    monkeypatch.setattr(capture, "window", lambda wid: pytest.fail("the window picture was fine"))
    seen = {}

    def recognize(image, where, correct=False):
        seen.update(image=image, area=where)
        return [{"text": "Save as…", **dict(zip("xywh", ocr.to_screen((0.1, 0.5, 0.2, 0.1), where)))}]
    monkeypatch.setattr(ocr, "recognize", recognize)
    items, where, window = ocr.read_window(_RunningApp())
    assert asked == [((600.0, 400.0, 300.0, 200.0), 77)] and seen["image"] is picture
    assert where == {"left": 600.0, "top": 400.0, "width": 300.0, "height": 200.0}
    assert window["pid"] == 4242 and window["window_id"] == 77 and window["app"] == "MintFixture"
    assert items[0]["window_id"] == 77 and items[0]["x"] == pytest.approx(630.0) and items[0]["y"] == pytest.approx(480.0)
    # The window picture failed and the screen grab came back: never used - the window alone, or nothing.
    monkeypatch.setattr(capture, "area", lambda a, wid: {**area(a, wid), "method": "region"})
    monkeypatch.setattr(capture, "window", lambda wid: (_ for _ in ()).throw(capture.FrameMismatch("mismatch")))
    with pytest.raises(capture.FrameMismatch):
        ocr.read_window(_RunningApp())
    monkeypatch.setattr(capture, "window_bounds", lambda wid: None)
    with pytest.raises(LookupError):
        ocr.read_window(_RunningApp())


def test_click_text_in_an_app_refuses_a_spot_another_app_covers(monkeypatch):
    try:
        from mint.tools import fastinput
    except ImportError:
        from mint import fastinput
    monkeypatch.setattr(fastinput, "has_accessibility", lambda: True)
    monkeypatch.setattr(ocr, "_running", lambda app: _RunningApp())
    window = {"x": 600, "y": 400, "w": 300, "h": 200, "app": "MintFixture", "pid": 4242, "window_id": 77}
    item = {"text": "Save as…", "x": 650, "y": 450, "w": 60, "h": 20}
    monkeypatch.setattr(ocr, "read_window", lambda app: ([item], {"left": 600, "top": 400, "width": 300,
                                                                   "height": 200}, window))
    monkeypatch.setattr(ocr, "read_screen", lambda: pytest.fail("the screen must not be read for an app"))
    monkeypatch.setattr(ocr, "_window_at", lambda point: {"app": "Browser", "pid": 1})
    result = ocr.click_text("Save as…", app="MintFixture")
    assert result.startswith("FAILED") and "Browser covers" in result and "nothing was clicked" in result
    monkeypatch.setattr(ocr, "read_window", lambda app: (_ for _ in ()).throw(capture.FrameMismatch("capture/window mismatch")))
    assert "mismatch" in ocr.click_text("Save as…", app="MintFixture")


# --- fix-live: a web view's page that is not in the tree yet; lists scrolled through Accessibility -----

def _clock(monkeypatch, on_sleep=None):
    """ground's time, faked: sleeping advances the clock (and may change the world)."""
    clock = {"t": 1000.0, "slept": 0}

    def sleep(s):
        clock["t"] += s
        clock["slept"] += 1
        assert clock["slept"] < 500, "waiting without a bound"
        if on_sleep:
            on_sleep(clock["slept"])
    monkeypatch.setattr(ground, "time", types.SimpleNamespace(sleep=sleep, monotonic=lambda: clock["t"],
                                                              time=lambda: clock["t"]))
    return clock


def _web_page():
    def row(title, y):
        return WebNode("AXGroup", title, (20, y, 700, 36), [
            Node("AXStaticText", "", (30, y + 8, 100, 16), value=title, actions=()),
            WebNode("AXButton", "Install", (600, y + 4, 60, 22), extra={"AXDOMIdentifier": f"web-install-{y}"})])
    page = WebNode("AXWebArea", "", (0, 40, 800, 560), [
        WebNode("AXButton", "Save", (20, 50, 50, 22)), row("YAML Support", 200), row("Docker Helper", 236)])
    return Node("AXGroup", "", (0, 40, 800, 560), [Node("AXScrollArea", "", (0, 40, 800, 560), [page])])


def _running(monkeypatch, app):
    monkeypatch.setattr(ground, "AppKit", types.SimpleNamespace(NSRunningApplication=types.SimpleNamespace(
        runningApplicationWithProcessIdentifier_=lambda pid: app)))


def test_an_empty_web_view_host_is_pending_but_small_or_named_empty_groups_are_not(tree):
    host = Node("AXGroup", "", (0, 40, 800, 560), [])          # WKWebView before WebKit built its page
    window = _window(host, Node("AXGroup", "", (5, 5, 14, 14), []), Node("AXGroup", "Toolbar", (0, 0, 800, 40), []),
                     Node("AXButton", "Back", (10, 10, 40, 20)))
    inv = ground.inventory(tree(window))
    assert inv["web_pending"] and inv["pending"] == [host]
    full = ground.inventory(tree(_window(_web_page(), Node("AXButton", "Back", (10, 10, 40, 20)))))
    assert not full["web_pending"] and full["pending"] == []


def test_a_pending_web_view_is_read_again_once_its_page_arrives(tree, monkeypatch):
    host = Node("AXGroup", "", (0, 40, 800, 560), [])
    app = tree(_window(host, Node("AXButton", "Back", (10, 10, 40, 20)), wid=7))
    page = _web_page()

    def arrive(n):
        if n == 2:                                            # the page appears after the second poll
            host.kids = page.kids
    _clock(monkeypatch, arrive)
    _running(monkeypatch, app)
    monkeypatch.setattr(ground, "_pending_waited", {})
    monkeypatch.setattr(ground, "choose_by_jev", lambda *a, **k: (None, "Jev is not asked in tests"))
    monkeypatch.setattr(axkit, "is_chromium_app", lambda app: False)
    reads = []
    real = ground.inventory
    monkeypatch.setattr(ground, "inventory", lambda a=None, **kw: reads.append(kw) or real(a, **kw))
    inv = real(app)
    assert inv["pending"] == [host]
    hit, fresh, why = ground.ground("Install Docker Helper", "click", inv, use_vision=False)
    assert reads == [{"window_id": 7}]                       # the same window, read once more
    assert hit is not None and hit["label"] == "Install" and hit["within"] == "Docker Helper", why
    assert hit["in_web_content"] and not fresh["pending"]


def test_a_group_that_stays_empty_costs_one_bounded_wait(tree, monkeypatch):
    app = tree(_window(Node("AXGroup", "", (0, 40, 800, 560), []), Node("AXButton", "Back", (10, 10, 40, 20))))
    clock = _clock(monkeypatch)
    _running(monkeypatch, app)
    monkeypatch.setattr(ground, "_pending_waited", {})
    inv = ground.inventory(app)
    started = clock["t"]
    assert ground.settle_pending(inv) is None
    assert ground.PENDING_WAIT <= clock["t"] - started < ground.PENDING_WAIT + 0.5
    again = clock["slept"]
    assert ground.settle_pending(ground.inventory(app)) is None and clock["slept"] == again   # remembered


class _ListModel:
    """An AppKit list in a scroll area (0, 100, 400 x 260): 30 rows of 36 pt, each with an Install button
    that offers only AXPress, moved by a scroll offset (0..820), like MintFixture's list."""

    def __init__(self, to_visible=(), bar_settable=True, pages=True, offset=0.0):
        self.offset, self.calls = offset, []
        self.to_visible, self.bar_settable, self.pages = set(to_visible), bar_settable, pages

    def _clamp(self, v):
        return max(0.0, min(820.0, v))

    def frame(self, n):
        if n in ("area", "window"):
            return (0, 100, 400, 260) if n == "area" else (0, 0, 800, 600)
        if n == "bar":
            return (385, 100, 15, 260)
        y = 100 + 36 * int(n[3:]) - self.offset
        return (0, y, 380, 36) if n.startswith("row") else (300, y + 4, 80, 28)

    def attr(self, n, name):
        if name == "AXParent":
            return n.replace("btn", "row") if n.startswith("btn") else "area" if n.startswith("row") \
                else "window" if n == "area" else None
        if name == "AXRole":
            return {"area": "AXScrollArea", "bar": "AXScrollBar", "window": "AXWindow"}.get(n) or \
                ("AXButton" if n.startswith("btn") else "AXGroup")
        if name == "AXChildren":
            return [f"row{i}" for i in range(30)] + ["bar"] if n == "area" else []
        if name == "AXVerticalScrollBar":
            return "bar" if n == "area" else None
        return None

    def perform(self, n, action):
        self.calls.append((n, action))
        if action == "AXScrollToVisible" and n in self.to_visible:
            self.offset = self._clamp(36 * int(n[3:]) - 100)
            return 0
        if action in ("AXScrollDownByPage", "AXScrollUpByPage") and self.pages and n == "area":
            self.offset = self._clamp(self.offset + (240 if action.endswith("DownByPage") else -240))
            return 0
        return -25206

    def set(self, n, name, value):
        self.calls.append((n, name, round(value, 3)))
        if n == "bar" and name == "AXValue" and self.bar_settable:
            self.offset = self._clamp(value * 820)
            return 0
        return -25200

    def seen(self, n):
        x, y, w, h = self.frame(n)
        return 100 <= y + h / 2 <= 360


@pytest.fixture
def listing(monkeypatch):
    def install(**kw):
        model = _ListModel(**kw)
        monkeypatch.setattr(axkit, "attr", model.attr)
        monkeypatch.setattr(axkit, "frame", model.frame)
        monkeypatch.setattr(ground, "_settable", lambda n, name="AXValue": n == "bar" and model.bar_settable)
        monkeypatch.setattr(ground, "AX", types.SimpleNamespace(AXUIElementPerformAction=model.perform,
                                                               AXUIElementSetAttributeValue=model.set))
        _clock(monkeypatch)
        return model
    return install


def test_scroll_to_visible_is_used_where_offered(listing):
    m = listing(to_visible={"btn25"})
    assert "scroll-to-visible" in ground.scroll_into_view("btn25") and m.seen("btn25")
    assert m.calls == [("btn25", "AXScrollToVisible")]


def test_a_list_without_scroll_to_visible_is_moved_by_its_scroll_bar_to_put_the_row_in_the_middle(listing):
    m = listing()
    how = ground.scroll_into_view("btn25")
    assert how == "its scroll bar, set through Accessibility" and m.seen("btn25")
    assert m.calls[:2] == [("btn25", "AXScrollToVisible"), ("row25", "AXScrollToVisible")]
    assert m.calls[2] == ("bar", "AXValue", 0.961)              # (904 + 14 - 130) / 820
    assert not any(c[1].endswith("ByPage") for c in m.calls)


def test_without_a_settable_scroll_bar_it_pages_both_ways_and_stops_when_nothing_moves(listing):
    m = listing(bar_settable=False)
    assert ground.scroll_into_view("btn25") == "Accessibility's page scrolling" and m.seen("btn25")
    assert not any(c[1] == "AXValue" for c in m.calls)
    m2 = listing(bar_settable=False, offset=820.0)
    assert ground.scroll_into_view("btn0") == "Accessibility's page scrolling" and m2.seen("btn0")
    assert ("area", "AXScrollUpByPage") in m2.calls
    m3 = listing(bar_settable=False, pages=False)
    assert ground.scroll_into_view("btn25") == "" and m3.offset == 0.0
    m4 = listing(bar_settable=False, offset=820.0)          # at the end already: one try, then it stops
    assert ground.scroll_into_view("btn40") == ""
    assert m4.calls.count(("area", "AXScrollDownByPage")) == 1


def test_scrolling_to_a_named_row_reports_how_and_never_guesses_after_the_scroll(monkeypatch):
    sql = object()
    inv = {"dialog": None, "window": (0, 0, 800, 600), "pid": None, "window_id": None, "offscreen": [
        {"label": "Install", "role": "AXButton", "kind": "button", "ref": sql, "box": (300, 404, 80, 28),
         "within": "SQL Formatter"},
        {"label": "Jarvis", "role": "AXLink", "kind": "link", "ref": object(), "box": (10, 700, 80, 20), "within": ""}]}
    monkeypatch.setattr(ground.time, "sleep", lambda s: None)
    monkeypatch.setattr(ground, "scroll_into_view", lambda ref: "its scroll bar, set through Accessibility")
    row = {"enabled": True, "interactive": True, "actionable": True, "in_dialog": False, "role": "AXButton"}
    fresh = {"dialog": None, "window": (0, 0, 800, 600), "elements": [
        dict(row, label="Install", within="Live Server", box=(300, 120, 80, 28)),
        dict(row, label="Install", within="SQL Formatter", box=(300, 156, 80, 28)),
        dict(row, label="Jarvis", within="", role="AXLink", box=(10, 200, 80, 20)),
        dict(row, label="Jarvis", within="", role="AXLink", box=(10, 230, 80, 20))]}
    monkeypatch.setattr(ground, "inventory", lambda *a, **k: fresh)
    hit, _, why = ground._scroll_to_named("Install SQL Formatter", "click", inv)
    assert hit["within"] == "SQL Formatter" and "out of view" in why
    assert hit["scrolled"] == ("scrolled button 'Install' in 'SQL Formatter' into view first "
                               "(its scroll bar, set through Accessibility)")
    assert "scrolled" not in fresh["elements"][1]          # the inventory's own element is left as it was
    # no row name, and two alike in view after scrolling: which one it was is not known - no guess
    assert ground._scroll_to_named("the Jarvis project", "click", inv) is None
    monkeypatch.setattr(ground, "scroll_into_view", lambda ref: "")
    assert ground._scroll_to_named("Install SQL Formatter", "click", inv) is None   # could not scroll: nothing
