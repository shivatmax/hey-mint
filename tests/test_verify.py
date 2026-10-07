"""verify_state, the window-change detector and the focus guard - all with fake readers, clocks and apps."""
import os

import pytest

try:
    from mint.screen import focus_guard, verify
except ImportError:
    from mint import focus_guard, verify


# --- fakes -------------------------------------------------------------------------------------------

TEXTEDIT, FINDER, NOTES = 501, 502, 503


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeReader(verify.Reader):
    def __init__(self, front=TEXTEDIT, windows=None, elements=None, ocr=None, ax_titles=None, files=None,
                 web=None, wall=1000.0):
        self._front = front
        self._windows = windows if windows is not None else [
            {"id": 1, "pid": TEXTEDIT, "app": "TextEdit", "title": "Untitled", "layer": 0, "w": 600, "h": 400},
            {"id": 2, "pid": FINDER, "app": "Finder", "title": "Downloads", "layer": 0, "w": 800, "h": 500}]
        self._elements = elements or {}
        self._ocr = ocr or {}
        self._ax = ax_titles or {}
        self._files = files or {}
        self._web = web or {}
        self._wall = wall
        self.calls = []

    _APPS = {TEXTEDIT: ("TextEdit", "com.apple.TextEdit"), FINDER: ("Finder", "com.apple.finder"),
             NOTES: ("Notes", "com.apple.Notes")}

    def front(self):
        self.calls.append("front")
        pid = self._front() if callable(self._front) else self._front
        if pid is None:
            return None
        name, bundle = self._APPS.get(pid, ("Mint", "ai.mint"))
        return {"pid": pid, "name": name, "bundle": bundle}

    def apps(self):
        return [{"pid": p, "name": n, "bundle": b} for p, (n, b) in self._APPS.items()]

    def windows(self):
        return self._windows() if callable(self._windows) else self._windows

    def ax_titles(self, pid):
        return self._ax.get(pid)

    def elements(self, pid):
        self.calls.append(("elements", pid))
        value = self._elements.get(pid)
        return value() if callable(value) else value

    def web_text(self, pid, text):
        return self._web.get(pid)

    def ocr(self, pid):
        self.calls.append(("ocr", pid))
        return self._ocr.get(pid)

    def stat(self, path):
        return self._files.get(path, (False, 0.0))

    def wall(self):
        return self._wall


def el(role, label="", value=None, enabled=True, selected=None, roledesc=""):
    return {"role": role, "subrole": "", "roledesc": roledesc, "label": label, "ident": "", "value": value,
            "enabled": enabled, "selected": selected}


def once(preds, reader):
    return verify.check(preds, reader=reader)


# --- checks one at a time ----------------------------------------------------------------------------------

def test_bad_checks_are_refused_not_guessed():
    for preds, why in [([], "no checks"), ([{"check": "window"}] , "needs app or title"),
                       ([{"check": "element", "value_equals": "x"}], "needs role or label"),
                       ([{"check": "teleport", "app": "x"}], "is not one of"),
                       ([{"check": "front_app", "app": "TextEdit"}] * 9, "at most 8"),
                       ([{"check": "element", "label": "Save", "gone": True, "enabled": True}], "gone has no value")]:
        with pytest.raises(ValueError, match=why):
            verify.until(preds, reader=FakeReader())
    assert verify.tool({"expect": []}).startswith("FAILED")
    assert verify.tool({"expect": "not json"}).startswith("FAILED")
    # kind guessed from the fields; a single check given flat works too
    assert verify.normalize([{"path": "~/a.txt"}])[0][0]["check"] == "file"


def test_window_and_front_app():
    r = FakeReader(ax_titles={TEXTEDIT: ["Untitled"], NOTES: []})
    assert once([{"check": "window", "app": "TextEdit", "title": "untitled"}], r).ok
    assert once([{"check": "window", "title": "Downloads"}], r).ok
    miss = once([{"check": "window", "app": "TextEdit", "title": "Save"}], r)
    assert miss.status == verify.UNSATISFIED and "'Untitled'" in miss.text()
    assert once([{"check": "window", "app": "TextEdit", "title": "Save", "gone": True}], r).ok
    assert once([{"check": "window", "app": "Notes"}], r).status == verify.UNSATISFIED      # running, no window
    assert once([{"check": "window", "app": "Calculator", "gone": True}], r).ok              # not running
    # without Accessibility, a window not on screen may be minimised or on another Space: unknown
    no_ax = FakeReader()
    assert once([{"check": "window", "app": "Notes"}], no_ax).status == verify.UNKNOWN
    assert once([{"check": "window", "app": "TextEdit", "title": "Save"}], no_ax).status == verify.UNKNOWN
    assert once([{"check": "window", "app": "TextEdit", "title": "Untitled"}], no_ax).ok    # on screen: enough
    assert once([{"check": "window", "app": "TextEdit"}], no_ax).ok
    # a minimised window is still found through Accessibility
    r2 = FakeReader(ax_titles={NOTES: ["Groceries"]})
    assert once([{"check": "window", "app": "Notes", "title": "groceries"}], r2).ok
    # no title anywhere (Screen Recording off): can't tell, never a pass
    blind = FakeReader(windows=[{"id": 1, "pid": TEXTEDIT, "app": "TextEdit", "title": "", "layer": 0,
                                 "w": 600, "h": 400}])
    assert once([{"check": "window", "app": "TextEdit", "title": "Save"}], blind).status == verify.UNKNOWN
    assert once([{"check": "front_app", "app": "textedit"}], r).ok
    assert once([{"check": "front_app", "app": "com.apple.TextEdit"}], r).ok
    no = once([{"check": "front_app", "app": "Finder"}], r)
    assert no.status == verify.UNSATISFIED and "TextEdit is in front" in no.text()
    assert once([{"check": "front_app", "app": "Finder", "gone": True}], r).ok
    assert once([{"check": "front_app", "app": "Finder"}], FakeReader(front=None)).status == verify.UNKNOWN
    assert once([{"check": "window", "title": "x"}], FakeReader(windows=lambda: None)).status == verify.UNKNOWN


def test_elements_roles_labels_values_and_state():
    controls = [el("AXButton", "Save As…"), el("AXButton", "Save", enabled=False),
                el("AXTextField", "Name", value="report  draft"), el("AXCheckBox", "Bold", value="1", selected=True),
                el("AXStaticText", "Ready", value="Ready", enabled=None)]
    r = FakeReader(elements={TEXTEDIT: (controls, True)})
    # "Save" means the button called exactly Save, not "Save As…"
    off = once([{"check": "element", "role": "button", "label": "Save", "enabled": True}], r)
    assert off.status == verify.UNSATISFIED and "not enabled" in off.text()
    assert once([{"check": "element", "role": "button", "label": "Save", "enabled": False}], r).ok
    assert once([{"check": "element", "role": "text field", "label": "name", "value_equals": "report draft"}], r).ok
    wrong = once([{"check": "element", "role": "textfield", "label": "Name", "value_equals": "Report"}], r)
    assert wrong.status == verify.UNSATISFIED and "'report draft'" in wrong.text()
    assert once([{"check": "element", "label": "Name", "value_contains": "DRAFT"}], r).ok
    assert once([{"check": "element", "role": "checkbox", "label": "Bold", "selected": True}], r).ok
    assert once([{"check": "element", "role": "AXCheckBox", "label": "Bold", "selected": "false"}], r).status \
        == verify.UNSATISFIED
    # a property the element does not report: unknown, not a pass
    assert once([{"check": "element", "label": "Ready", "enabled": True}], r).status == verify.UNKNOWN
    # missing from a complete walk: unsatisfied, naming what is there; gone holds
    missing = once([{"check": "element", "role": "button", "label": "Replace"}], r)
    assert missing.status == verify.UNSATISFIED and "'Save'" in missing.text()
    assert once([{"check": "element", "role": "button", "label": "Replace", "gone": True}], r).ok
    assert once([{"check": "element", "label": "Save", "gone": True}], r).status == verify.UNSATISFIED
    # missing from a cut-short walk proves nothing either way
    cut = FakeReader(elements={TEXTEDIT: (controls, False)})
    assert once([{"check": "element", "label": "Replace"}], cut).status == verify.UNKNOWN
    assert once([{"check": "element", "label": "Replace", "gone": True}], cut).status == verify.UNKNOWN
    # several matches that disagree: unknown; that all agree: fine
    two = FakeReader(elements={TEXTEDIT: ([el("AXButton", "OK"), el("AXButton", "OK", enabled=False)], True)})
    assert once([{"check": "element", "label": "OK", "enabled": True}], two).status == verify.UNKNOWN
    assert once([{"check": "element", "label": "OK"}], two).ok
    # unreadable app
    assert once([{"check": "element", "label": "OK"}], FakeReader()).status == verify.UNKNOWN


def test_element_info_from_raw_fields():
    box = verify.element_info({"AXRole": "AXCheckBox", "AXTitle": "Wrap", "AXValue": 1, "AXEnabled": True})
    assert box["selected"] is True and box["value"] == "1" and box["label"] == "Wrap"
    text = verify.element_info({"AXRole": "AXStaticText", "AXValue": "Export complete"})
    assert text["label"] == "Export complete" and text["enabled"] is None
    field = verify.element_info({"AXRole": "AXTextField", "AXPlaceholderValue": "Search", "AXValue": "cats"},
                                title_text="Find")
    assert "Search" in field["label"] and "Find" in field["label"] and field["value"] == "cats"


def test_text_ax_first_then_ocr():
    words = ([el("AXStaticText", "Export complete", value="Export complete")], True)
    r = FakeReader(elements={TEXTEDIT: words}, ocr={TEXTEDIT: ["never read"]})
    assert once([{"check": "text", "text": "export   complete"}], r).ok
    assert ("ocr", TEXTEDIT) not in r.calls                       # accessibility had it: no OCR
    drawn = FakeReader(elements={TEXTEDIT: ([el("AXGroup")], True)}, ocr={TEXTEDIT: ["Exp0rt", "Export complete"]})
    assert once([{"check": "text", "text": "Export complete"}], drawn).ok
    not_there = FakeReader(elements={TEXTEDIT: ([], True)}, ocr={TEXTEDIT: ["Exporting 40%"]})
    res = once([{"check": "text", "text": "Export complete"}], not_there)
    assert res.status == verify.UNSATISFIED and "Exporting 40%" in res.text()
    assert once([{"check": "text", "text": "Export complete", "gone": True}], not_there).ok
    assert once([{"check": "text", "text": "export complete", "gone": True}], r).status == verify.UNSATISFIED
    blind = FakeReader(elements={TEXTEDIT: ([], True)})
    assert once([{"check": "text", "text": "Export complete"}], blind).status == verify.UNKNOWN
    assert once([{"check": "text", "text": "Export complete", "gone": True}], blind).status == verify.UNKNOWN
    # in another app, by name
    other = FakeReader(elements={FINDER: ([el("AXStaticText", "a.pdf", value="a.pdf")], True)})
    assert once([{"check": "text", "app": "Finder", "text": "a.pdf"}], other).ok
    assert once([{"check": "text", "app": "Calculator", "text": "4"}], other).status == verify.UNSATISFIED


def test_mints_own_window_is_never_read_off_the_main_thread(monkeypatch):
    monkeypatch.setattr(verify, "_mint_target", lambda: None)
    monkeypatch.setattr(verify, "_read_elements", lambda pid: pytest.fail("read Mint's own AX off the main thread"))
    monkeypatch.setattr(verify, "_web_text", lambda pid, t: pytest.fail("read Mint's own AX off the main thread"))
    r = verify.Reader()
    assert r.web_text(os.getpid(), "x") is None
    assert r.elements(os.getpid()) is None and r.ax_titles(os.getpid()) is None
    mine = FakeReader(front=os.getpid())
    assert once([{"check": "element", "label": "Send"}], mine).status == verify.UNKNOWN
    # Mint in front while working on TextEdit: look there
    monkeypatch.setattr(verify, "_mint_target", lambda: TEXTEDIT)
    working = FakeReader(front=os.getpid(), elements={TEXTEDIT: ([el("AXButton", "Send")], True)})
    assert once([{"check": "element", "label": "Send"}], working).ok


def test_files_saved_and_downloaded(tmp_path):
    r = FakeReader(files={"/srv/a.pdf": (True, 990.0), "/srv/old.pdf": (True, 100.0), "/srv/locked": None},
                   wall=1000.0)
    assert once([{"check": "file", "path": "/srv/a.pdf", "newer_than_seconds": 30}], r).ok
    stale = once([{"check": "file", "path": "/srv/old.pdf", "newer_than_seconds": 30}], r)
    assert stale.status == verify.UNSATISFIED and "15 min ago" in stale.text()
    r50 = FakeReader(files={"/srv/b.pdf": (True, 950.0)}, wall=1000.0)          # 50 s old
    assert once([{"check": "file", "path": "/srv/b.pdf", "newer_than_seconds": 30}], r50).status == verify.UNSATISFIED
    assert once([{"check": "file", "path": "/srv/b.pdf", "newer_than_seconds": 60}], r50).ok
    assert once([{"check": "file", "path": "/srv/a.pdf", "since": 980.0}], r).ok
    assert once([{"check": "file", "path": "/srv/a.pdf", "since": 995.0}], r).status == verify.UNSATISFIED
    assert once([{"check": "file", "path": "/srv/none.pdf"}], r).status == verify.UNSATISFIED
    assert once([{"check": "file", "path": "/srv/none.pdf", "gone": True}], r).ok
    assert once([{"check": "file", "path": "/srv/locked"}], r).status == verify.UNKNOWN
    # the real reader
    f = tmp_path / "x.txt"
    f.write_text("hi")
    assert verify.Reader().stat(str(f))[0] is True and verify.Reader().stat(str(tmp_path / "no")) == (False, 0.0)


def test_and_of_checks_unsatisfied_beats_unknown():
    r = FakeReader()
    res = once([{"check": "front_app", "app": "TextEdit"}, {"check": "element", "label": "x"},
                {"check": "front_app", "app": "Finder"}], r)
    assert res.status == verify.UNSATISFIED and res.failing()[0].index == 2 and "Held: #1" in res.text()
    res = once([{"check": "front_app", "app": "TextEdit"}, {"check": "element", "label": "x"}], r)
    assert res.status == verify.UNKNOWN and not res.ok and "NOT a success" in res.text()


def test_one_sample_reads_each_thing_once():
    r = FakeReader(elements={TEXTEDIT: ([el("AXButton", "A"), el("AXButton", "B")], True)})
    assert once([{"check": "element", "label": "A"}, {"check": "element", "label": "B"},
                 {"check": "front_app", "app": "TextEdit"}], r).ok
    assert r.calls.count(("elements", TEXTEDIT)) == 1 and r.calls.count("front") == 1


# --- polling and stability --------------------------------------------------------------------------------

def _poll(fronts, timeout=6.0, stable=2, stopped=lambda: False):
    clock = Clock()
    seq = iter(fronts)
    last = [fronts[-1]]

    def front():
        last[0] = next(seq, last[0])
        return last[0]
    r = FakeReader(front=front)
    res = verify.until([{"check": "front_app", "app": "Finder"}], timeout=timeout, stable=stable, reader=r,
                       clock=clock, sleep=clock.sleep, stopped=stopped)
    return res, clock


def test_needs_two_passing_samples_in_a_row():
    res, clock = _poll([TEXTEDIT, FINDER, TEXTEDIT, FINDER, FINDER])
    assert res.ok and res.samples == 5 and res.stable
    assert abs(clock.now - 100.0 - 4 * verify.INTERVAL) < 1e-6        # polled every INTERVAL
    res, _ = _poll([FINDER, FINDER])
    assert res.ok and res.samples == 2
    res, _ = _poll([FINDER], stable=3)
    assert res.ok and res.samples == 3


def test_timeout_and_a_last_moment_pass_is_not_proven():
    res, clock = _poll([TEXTEDIT], timeout=1.0)
    assert res.status == verify.UNSATISFIED and clock.now - 100.0 == pytest.approx(1.0)
    assert res.samples == 8                       # 0, .15, ... .9, then the last at 1.0
    flicker = [TEXTEDIT] * 7 + [FINDER]           # holds only in the very last sample
    res, _ = _poll(flicker, timeout=1.0)
    assert res.status == verify.UNKNOWN and "last sample" in res.text()
    res, _ = _poll([FINDER], timeout=0)           # check once
    assert res.ok and res.samples == 1
    res, clock = _poll([TEXTEDIT], timeout=99)    # capped
    assert clock.now - 100.0 == pytest.approx(verify.MAX_TIMEOUT)


def test_unknown_never_ends_as_success_and_stop_wins():
    clock = Clock()
    res = verify.until([{"check": "front_app", "app": "Finder"}], timeout=1.0, reader=FakeReader(front=None),
                       clock=clock, sleep=clock.sleep)
    assert res.status == verify.UNKNOWN and not res.ok
    res, _ = _poll([TEXTEDIT], stopped=lambda: True)
    assert res.stopped and not res.ok and res.samples == 1 and res.text().startswith("STOPPED")


def test_tool_entry(monkeypatch):
    seen = {}

    def fake_until(expect, timeout, stable):
        seen.update(expect=expect, timeout=timeout, stable=stable)
        return verify.Result(verify.SATISFIED, [verify.Outcome(0, "TextEdit in front", verify.SATISFIED)], 2, 0.2)
    monkeypatch.setattr(verify, "until", fake_until)
    assert verify.tool({"expect": [{"check": "front_app", "app": "TextEdit"}]}).startswith("SATISFIED")
    assert seen["timeout"] == verify.DEFAULT_TIMEOUT and seen["stable"] == 2
    verify.tool({"check": "window", "title": "Save", "seconds": 0})          # flat, one check, once
    assert seen["expect"] == [{"check": "window", "title": "Save"}] and seen["timeout"] == 0
    decl = verify.declarations()[0]
    assert decl.name == "verify_state" and decl.parameters.required == ["expect"]


# --- window-change detector ---------------------------------------------------------------------------------

def _snap(windows=(), front=(TEXTEDIT, "TextEdit"), focused=None, sheets=None):
    s = verify.Snap()
    for wid, pid, app, title in windows:
        s.windows[wid] = {"pid": pid, "app": app, "title": title}
        s.names[pid] = app
    s.front = {"pid": front[0], "name": front[1]} if front else None
    s.focused = dict(focused or {})
    s.sheets = dict(sheets or {})
    return s


def test_window_changes_in_one_line():
    base = [(1, TEXTEDIT, "TextEdit", "Untitled")]
    a = _snap(base, focused={TEXTEDIT: "Untitled"}, sheets={TEXTEDIT: []})
    assert verify.diff(a, _snap(base, focused={TEXTEDIT: "Untitled"}, sheets={TEXTEDIT: []})) == ""
    # a sheet: its untitled CG window is not reported a second time
    b = _snap(base + [(9, TEXTEDIT, "TextEdit", "")], focused={TEXTEDIT: "Untitled"}, sheets={TEXTEDIT: ["Save"]})
    assert verify.diff(a, b) == "new sheet 'Save' in TextEdit"
    assert verify.diff(b, a) == "sheet 'Save' closed in TextEdit"
    c = _snap(base + [(3, TEXTEDIT, "TextEdit", "Untitled 2")], focused={TEXTEDIT: "Untitled 2"},
              sheets={TEXTEDIT: []})
    assert verify.diff(a, c) == "new window 'Untitled 2' in TextEdit"    # the focus change is the same news
    d = _snap(base, front=(FINDER, "Finder"))
    assert verify.diff(a, d) == "front app changed to Finder"
    e = _snap(base, focused={TEXTEDIT: "Notes.txt"}, sheets={TEXTEDIT: []})
    assert verify.diff(a, e) == "focused window now 'Notes.txt' in TextEdit"
    f = _snap([], focused={TEXTEDIT: "Untitled"}, sheets={TEXTEDIT: []})
    assert verify.diff(a, f) == "window 'Untitled' closed in TextEdit"
    many = _snap(base + [(10 + i, FINDER, "Finder", f"w{i}") for i in range(6)], focused={TEXTEDIT: "Untitled"},
                 sheets={TEXTEDIT: []})
    assert verify.diff(a, many).endswith("2 more changes")


def test_window_changes_only_for_the_app_acted_on(monkeypatch):
    base = [(1, TEXTEDIT, "TextEdit", "Untitled")]
    a = _snap(base + [(2, FINDER, "Finder", "Bank statements")], focused={TEXTEDIT: "Untitled"},
              sheets={TEXTEDIT: []})
    a.scope = (TEXTEDIT,)
    # Finder's window left the list (a Space switch), a Notes window came: not the action's doing
    b = _snap(base + [(7, NOTES, "Notes", "Private note")], focused={TEXTEDIT: "Untitled"}, sheets={TEXTEDIT: []})
    assert verify.diff(a, b) == ""
    c = _snap(base + [(3, TEXTEDIT, "TextEdit", "Untitled 2"), (8, NOTES, "Notes", "Private note")],
              focused={TEXTEDIT: "Untitled 2"}, sheets={TEXTEDIT: []})
    assert verify.diff(a, c) == "new window 'Untitled 2' in TextEdit"
    d = _snap(base, front=(FINDER, "Finder"), focused={TEXTEDIT: "Untitled"}, sheets={TEXTEDIT: []})
    assert verify.diff(a, d) == "front app changed to Finder"
    # before(): the scope is the app acted on, else the app in front
    monkeypatch.setattr(verify, "_snap", lambda pids: _snap(base, front=(FINDER, "Finder")))
    assert verify.before((TEXTEDIT,)).scope == (TEXTEDIT,) and verify.before().scope == (FINDER,)


# --- focus guard -------------------------------------------------------------------------------------------

class Mac:
    """The front app, the user's idle time and the restores, faked."""

    def __init__(self):
        self.clock = Clock()
        self.front_pid = FINDER          # the user's app
        self.idle_at_start = 30.0
        self.started = self.clock.now
        self.restores = []
        self.names = {TEXTEDIT: "TextEdit", FINDER: "Finder", NOTES: "Notes", 999: "Mint"}

    def idle(self):
        return self.idle_at_start + (self.clock.now - self.started)

    def front(self):
        return {"pid": self.front_pid, "name": self.names[self.front_pid], "bundle": "", "app": None}

    def reactivate(self, previous):
        self.restores.append(previous["name"])
        self.front_pid = previous["pid"]
        return True

    def guard(self):
        return focus_guard.Guard(clock=self.clock, idle=self.idle, front=self.front, reactivate=self.reactivate,
                                 spawn=lambda fn: fn(), own_pid=999, settle=0)


def test_a_self_activating_target_is_sent_back():
    mac = Mac()
    g = mac.guard()
    with focus_guard.lease(TEXTEDIT, guard=g) as held:
        mac.clock.sleep(0.2)
        mac.front_pid = TEXTEDIT
        assert g.activated(TEXTEDIT, "TextEdit") == "restore"
    assert mac.restores == ["Finder"] and held.restored == 1
    assert held.note == " (TextEdit took focus; put Finder back in front.)"
    assert g.activated(TEXTEDIT, "TextEdit") == "no lease"            # the lease is over


def test_allowed_front_and_third_apps():
    mac = Mac()
    g = mac.guard()
    held = g.open(TEXTEDIT, allow_front=True)
    assert g.decide(held, TEXTEDIT, mac.clock()) == "allowed"
    assert g.activated(NOTES, "Notes") == "restore"                   # a hand-off to another app is not
    assert g.activated(FINDER, "Finder") == "previous app"            # our own restore arriving
    assert g.activated(999, "Mint") == "mint"
    g.close(held)
    assert mac.restores == ["Finder"]


def test_never_fights_the_user():
    mac = Mac()
    g = mac.guard()
    held = g.open(TEXTEDIT)
    mac.clock.sleep(1.0)
    mac.idle_at_start = -0.7          # they clicked 0.3 s ago, during the lease
    assert g.activated(TEXTEDIT, "TextEdit") == "user"
    mac.idle_at_start = 30.0          # and it stays their choice for the rest of the lease
    assert g.activated(NOTES, "Notes") == "user" and held.user_active
    assert mac.restores == []
    # idle unreadable: treated as the user being active
    g2 = focus_guard.Guard(clock=mac.clock, idle=lambda: 1 / 0, front=mac.front, reactivate=mac.reactivate,
                           spawn=lambda fn: fn(), own_pid=999, settle=0)
    g2.open(TEXTEDIT)
    mac.clock.sleep(0.1)
    assert g2.activated(TEXTEDIT, "TextEdit") == "user"


def test_lease_expires_and_gives_up_after_two():
    mac = Mac()
    g = mac.guard()
    held = g.open(TEXTEDIT, seconds=60)                               # capped at LEASE
    assert held.until - held.started == focus_guard.LEASE
    for _ in range(3):
        g.activated(TEXTEDIT, "TextEdit")
    assert mac.restores == ["Finder", "Finder"] and g.decide(held, TEXTEDIT, mac.clock()) == "gave up"
    mac.clock.sleep(focus_guard.LEASE + 0.1)
    assert g.decide(held, NOTES, mac.clock()) == "expired"
    assert g.activated(NOTES, "Notes") == "no lease"


def test_close_catches_an_activation_nobody_announced_and_one_restore_for_two_leases():
    mac = Mac()
    g = mac.guard()
    with focus_guard.lease(TEXTEDIT, guard=g) as held:
        mac.clock.sleep(0.1)
        mac.front_pid = TEXTEDIT       # no notification (no main loop)
    assert mac.restores == ["Finder"] and held.restored == 1
    a, b = g.open(TEXTEDIT), g.open(NOTES)
    mac.clock.sleep(0.1)
    assert g.activated(TEXTEDIT, "TextEdit") == "restore"
    assert mac.restores == ["Finder", "Finder"]
    g.close(a)
    g.close(b)
    # nothing in front when the lease began: nothing to restore
    mac2 = Mac()
    g3 = focus_guard.Guard(clock=mac2.clock, idle=mac2.idle, front=lambda: None, reactivate=mac2.reactivate,
                           spawn=lambda fn: fn(), own_pid=999, settle=0)
    g3.open(TEXTEDIT)
    assert g3.activated(TEXTEDIT, "TextEdit") == "no previous app"


# --- live, read-only (MINT_LIVE_UI=1) ----------------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("MINT_LIVE_UI") != "1", reason="reads the real screen: MINT_LIVE_UI=1")
def test_live_front_app_and_its_window():
    front = verify.Reader().front()
    assert front and front["name"]
    res = verify.until([{"check": "front_app", "app": front["name"]}], timeout=2)
    assert res.ok, res.text()
    snap = verify.before()
    assert snap.front and isinstance(verify.after(snap, settle=0), str)


def test_only_target_lease_leaves_the_actions_result_alone():
    """A background press that opens a link brings the browser forward on purpose: only the pressed app
    pushing itself in front is undone (only_target)."""
    try:
        from mint.screen import focus_guard
    except ImportError:
        from mint import focus_guard
    g = focus_guard.Guard.__new__(focus_guard.Guard)
    g.own_pid, g.idle = 1, lambda: 99.0
    lease = focus_guard.Lease(target_pid=50, allow_front=False, previous={"pid": 10, "name": "Notes"},
                              started=0.0, seconds=5.0, only_target=True)
    assert g.decide(lease, 77, 1.0) == "the action's result"         # a browser opened by the press
    assert g.decide(lease, 50, 1.0) == "restore"                      # the pressed app jumped forward
