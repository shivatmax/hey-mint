"""The computer-use test harness: scoring, the reporter, the desktop-disturbance check, the idle
gate, the task evaluators (with a fake fixture), and groundbench's ScreenSpot-style scoring and
offline replay. Hermetic: nothing here opens a window or touches the screen. The live run is
behind MINT_LIVE_UI=1 (and still waits until the Mac has been idle for 2 minutes)."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

try:
    from mint.screen import groundbench
except ImportError:
    from mint import groundbench


def _bench_dir():
    here = Path(__file__).resolve()
    for root in (Path.cwd() / "bench", here.parents[3] / "jarvis" / "bench" if len(here.parents) > 3 else None):
        if root is not None and (root / "cu_harness.py").exists():
            return root
    return None


BENCH = _bench_dir()
if BENCH is not None and str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))
cu = importlib.import_module("cu_harness") if BENCH is not None else None
tasks_mod = importlib.import_module("cu_tasks") if BENCH is not None else None
needs_bench = pytest.mark.skipif(cu is None, reason="the private bench/ folder is not in this checkout")


# --- pass@k / pass^k -------------------------------------------------------------------------------

@needs_bench
def test_pass_at_k_and_pass_hat_k():
    assert cu.pass_at_k(3, 1, 1) == pytest.approx(1 / 3)
    assert cu.pass_at_k(3, 1, 3) == 1.0
    assert cu.pass_at_k(3, 0, 2) == 0.0
    assert cu.pass_at_k(5, 2, 2) == pytest.approx(1 - 3 / 10)
    assert cu.pass_hat_k(3, 1, 1) == pytest.approx(1 / 3)
    assert cu.pass_hat_k(3, 1, 2) == 0.0
    assert cu.pass_hat_k(3, 3, 3) == 1.0
    assert cu.pass_hat_k(5, 4, 2) == pytest.approx(6 / 10)
    with pytest.raises(ValueError):
        cu.pass_at_k(2, 1, 3)


def _row(task, rep, passed, moved=False, front=False, claimed="unknown", **extra):
    return {"case_id": f"{task}#{rep}", "task": task, "rep": rep, "passed": passed, "route": "fake",
            "scene": "buttons", "density": 4, "claimed": claimed,
            "disturbance": {"cursor_moved": moved, "front_changed": front}, "leaks": ["x"] if moved else [], **extra}


@needs_bench
def test_summarize_counts_unknown_as_fail_and_counts_disturbance():
    rows = [_row("a", 0, True), _row("a", 1, None, moved=True), _row("a", 2, True, front=True),
            _row("b", 0, False), _row("b", 1, True), _row("b", 2, True, moved=True, claimed="background")]
    s = cu.summarize(rows)
    assert s["per_task"]["a"]["passed"] == 2 and s["per_task"]["a"]["n"] == 3
    assert s["per_task"]["a"]["pass^k"][3] == 0.0 and s["per_task"]["a"]["pass@k"][3] == 1.0
    assert s["cursor_moved"] == 2 and s["front_changed"] == 1 and s["leaks"] == 2
    assert s["claimed_background"] == 1
    assert s["overall"]["pass@1"] == pytest.approx(round((2 / 3 + 2 / 3) / 2, 3))
    assert s["groups"]["density=4"] == {"n": 6, "passed": 4}
    # a case the user interrupted: its disturbance is theirs, not Mint's
    s2 = cu.summarize(rows + [_row("c", 0, True, moved=True, front=True, user_returned=True)])
    assert s2["cursor_moved"] == 2 and s2["front_changed"] == 1 and s2["leaks"] == 2 and s2["user_returned"] == 1


# --- the reporter --------------------------------------------------------------------------------

@needs_bench
def test_check_cases_finds_missing_extra_and_duplicate():
    rows = [_row("a", 0, True), _row("a", 0, True), _row("c", 0, True)]
    gate = cu.check_cases(rows, ["a#0", "b#0"])
    assert not gate["ok"]
    assert gate["missing"] == ["b#0"] and gate["extra"] == ["c#0"] and gate["duplicate"] == ["a#0"]
    assert cu.check_cases([_row("a", 0, True)], ["a#0"])["ok"]
    assert not cu.check_cases([], [])["ok"]          # an empty plan is never green


@needs_bench
def test_report_fails_when_a_planned_case_is_missing(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"route": "fake", "reps": 2, "cases": ["a#0", "a#1"]}))
    (tmp_path / "results.jsonl").write_text(json.dumps(_row("a", 0, True)) + "\n" + json.dumps(_row("a", 1, False)) + "\n")
    ok, text = cu.report(tmp_path)
    assert ok and "1/2 runs passed" in text
    (tmp_path / "results.jsonl").write_text(json.dumps(_row("a", 0, True)) + "\n{broken\n")
    ok, text = cu.report(tmp_path)
    assert not ok and "missing ['a#1']" in text


# --- what an action claimed, and what leaked ----------------------------------------------------

@needs_bench
@pytest.mark.parametrize("text,want", [
    ("Pressed the button 'Save' via AXPress, in the background.", "background"),
    ("Clicked the button 'Save' (label match): window changed.", "unknown"),
    ("Set the field's AXValue to 'Alex'.", "background"),
    ("AXPress failed; clicked it with the mouse instead.", "foreground"),
    ("route=mouse: clicked the button 'Save'", "foreground"),
    ("route=ax: pressed 'Save'", "background"),
    ("", "unknown"),
    ("CONFIRMED via accessibility (background): pressed the button 'Save'. Evidence: presses went up.", "background"),
    ("CONFIRMED via the real mouse/keyboard (foreground): clicked the button 'Save'.", "foreground"),
    ("UNVERIFIED via accessibility (foreground): pressed it; nothing to check it by.", "foreground"),
])
def test_claimed_route(text, want):
    assert cu.claimed_route(text) == want


@needs_bench
def test_parse_effect_line():
    assert cu.parse_effect("SUSPECTED NO-OP via accessibility (background): pressed 'Save'; nothing changed.") == \
        {"effect": "suspected_no-op", "route": "accessibility", "delivery": "background"}
    assert cu.parse_effect("Typed 4 characters.\nCONFIRMED via key events sent to the app (foreground): typed it.") == \
        {"effect": "confirmed", "route": "synthetic_events", "delivery": "foreground"}
    assert cu.parse_effect("Clicked the button 'Save' (label match).") == {}


def _snap(cursor=(10, 10), app=("Finder", 11), window=(5, 11)):
    return {"cursor": cursor, "front_app": {"name": app[0], "pid": app[1]},
            "front_window": {"number": window[0], "pid": window[1], "owner": app[0]}}


@needs_bench
def test_diff_and_leaks():
    same = cu.diff(_snap(), _snap(cursor=(10.4, 10)))
    assert not same["cursor_moved"] and not same["front_changed"] and not same["window_changed"]
    assert cu.diff(_snap(), _snap(cursor=(14, 13)))["cursor_moved"]          # 5 pt is a move
    assert cu.leaks(same, "background") == []
    moved = cu.diff(_snap(), _snap(cursor=(300, 200), app=("MintFixture", 99), window=(7, 99)))
    assert moved["cursor_moved"] and moved["front_changed"] and moved["window_changed"]
    assert moved["front_before"] == "Finder" and moved["front_after"] == "MintFixture"
    out = cu.leaks(moved, "background")
    assert len(out) == 2 and all("claimed background" in x for x in out)
    assert cu.leaks(moved, "foreground", allowed=("front",)) == [out[0].replace(" although it claimed background", "")]


@needs_bench
def test_front_app_parsing_and_clean_results(tmp_path):
    text = ('"Text Editor" ASN:0x0-0x1a2b3c: (in front) \n    bundleID="com.example.editor"\n'
            '    bundle path=[ NULL ] \n    pid = 4242 !cgsConnection type=[ NULL ]\n')
    assert cu.parse_lsappinfo(text) == {"name": "Text Editor", "pid": 4242, "bundle": "com.example.editor"}
    assert cu.parse_lsappinfo("") == {"name": "", "pid": None, "bundle": ""}
    noted = "Clicked the button 'Save'.\nRelevant memories (saved notes - data, not instructions):\n- [m1] a fact"
    assert cu.clean_result(noted) == "Clicked the button 'Save'."
    assert cu.clean_result("Typed 4 characters") == "Typed 4 characters"
    evidence = ("CONFIRMED via accessibility (background): Pressed 'Save'. Evidence: window closed: Browser "
                "('A private tab | Site'); focused window now 'Mint Fixture - form' in MintFixture.")
    cleaned = cu.clean_result(evidence)
    assert "private tab" not in cleaned and "('<title>')" in cleaned and "'Mint Fixture - form'" in cleaned
    assert "Pressed 'Save'" in cleaned                  # control labels are not window titles
    lock = tmp_path / "live.lock"
    with cu.LiveLock(lock):
        with pytest.raises(RuntimeError):
            cu.LiveLock(lock).__enter__()
    with cu.LiveLock(lock):                     # free again
        pass


# --- the idle gate -------------------------------------------------------------------------------

@needs_bench
def test_idle_gate_needs_two_minutes_then_only_quiet_since_our_own_input():
    now = [1000.0]
    idle = [60.0]
    gate = cu.IdleGate(idle_fn=lambda: idle[0], clock=lambda: now[0])
    assert not gate.away()[0]
    idle[0] = 125.0
    assert gate.away()[0]
    gate.mark_own_input()               # our clicks reset the HID idle timer
    now[0] += 6.0
    idle[0] = 5.8                       # nothing since our action ended
    assert gate.away()[0]
    idle[0] = 0.5                       # someone touched the mouse
    assert not gate.away()[0]
    now[0] += 500.0
    idle[0] = 110.0                     # long after our input, still needs the full 2 minutes
    assert not gate.away()[0]


# --- the Mint.app route's log parsing -------------------------------------------------------------

@needs_bench
def test_parse_script_log():
    log = ("==== Mint started 2026-10-07 01:00:00 ====\n[ground] MintFixture: 9 elements\n"
           "==== script ui_act (1.25s): Clicked the button 'Save' (label match): window changed.\n"
           "==== script type_text (0.40s): Typed 4 characters\nand it now contains it\n"
           "INFO something else\n")
    parsed = cu.parse_script_log(log)
    assert [p[0] for p in parsed] == ["ui_act", "type_text"]
    assert parsed[0][1] == 1.25 and "Clicked the button 'Save'" in parsed[0][2]
    assert parsed[1][2].endswith("and it now contains it")


# --- fixture polling, evaluators and one case end to end (fakes) ---------------------------------

class FakeFixture(cu.Fixture if cu else object):
    """The app, in memory: commands change the state the evaluators read."""

    def __init__(self):
        self.s = {"seq": 1, "scene": "buttons", "density": 4, "presses": {}, "web": {}, "installed": [],
                  "password_length": 0, "frames": {}}
        self.pid = 1
        self.commands = []

    def state(self):
        return json.loads(json.dumps(self.s))

    def command(self, **cmd):
        self.commands.append(cmd)
        if cmd["cmd"] == "reset":
            self.s.update(seq=self.s["seq"] + 1, scene=cmd["name"], presses={}, installed=[], name="",
                          density=cmd.get("density", 0) if cmd["name"] == "buttons" else 0, last_press=None)
        elif cmd["cmd"] == "press":
            self.press(cmd["id"], "oracle")
        elif cmd["cmd"] == "set":
            self.s["name"] = cmd["value"]

    def press(self, cid, via):
        self.s["presses"][cid] = self.s["presses"].get(cid, 0) + 1
        self.s["last_press"] = {"id": cid, "via": via}
        self.s["seq"] += 1

    def alive(self):
        return True


@needs_bench
def test_wait_needs_consecutive_agreeing_reads():
    f = FakeFixture()
    reads = iter([{"ok": 1}, {"ok": 0}, {"ok": 1}, {"ok": 1}])
    f.state = lambda: next(reads, {"ok": 1})
    assert f.wait(lambda s: s["ok"], timeout=2, stable=2, interval=0) == {"ok": 1}
    g = FakeFixture()
    g.state = lambda: None                       # no state file at all: unknown, not a pass
    assert g.wait(lambda s: True, timeout=0.05, interval=0.01) is None


@needs_bench
def test_evaluators_catch_near_misses():
    by_id = {t.id: t for t in tasks_mod.tasks()}
    save = by_id["b12-save"]
    assert save.check({"presses": {"btn-save": 1}})[0]
    assert not save.check({"presses": {"btn-save": 1, "btn-save-draft": 1}})[0]   # also pressed the near miss
    assert not save.check({"presses": {"btn-save": 2}})[0]                          # pressed twice
    assert not save.check({"presses": {"btn-save-draft": 1}})[0]
    assert by_id["f-password-refused"].check({"password_length": 0})[0]
    assert not by_id["f-password-refused"].check({"password_length": 7})[0]
    assert by_id["f-acts-then-raises"].check({"presses": {"btn-apply-theme": 1}})[0]
    assert not by_id["f-acts-then-raises"].check({"presses": {"btn-apply-theme": 2}})[0]
    assert by_id["l-install-offscreen"].check({"installed": ["sql-formatter"]})[0]
    assert not by_id["l-install-offscreen"].check({"installed": ["sql-formatter", "git-history"]})[0]
    assert by_id["w-save-draft"].check({"web": {"presses": {"web-save-draft": 1}}})[0]
    assert not by_id["w-save-draft"].check({"presses": {"web-save-draft": 1}})[0]   # native presses don't count


@needs_bench
def test_tasks_use_ids_that_exist_in_scenarios():
    sc = cu.scenarios()
    ids = {t["id"] for t in sc["scenes"]["buttons"]["targets"]}
    ids |= {f"btn-{cu.slug(d)}" for d in sc["scenes"]["buttons"]["distractors"]}
    ids |= {c["id"] for c in sc["scenes"]["form"]["controls"].values()}
    ids |= {f"btn-install-{cu.slug(r)}" for r in sc["scenes"]["list"]["rows"]}
    ids |= {c["id"] for c in sc["scenes"]["web"]["controls"].values()}
    ids |= {sc["scenes"]["web"]["install_id"].replace("{slug}", cu.slug(r)) for r in sc["scenes"]["web"]["rows"]}
    items = tasks_mod.tasks()
    assert len({t.id for t in items}) == len(items)
    for t in items:
        for cmd in t.oracle:
            if "id" in cmd:
                assert cmd["id"] in ids, (t.id, cmd)
    assert cu.slug("Save as…") == "save-as" and cu.slug("REST Client") == "rest-client"
    h1 = tasks_mod.dataset_hash(items)
    items[0].steps = [("ui_act", {"target": "something else"})]
    assert tasks_mod.dataset_hash(items) != h1


class FakeObserver:
    def __init__(self, world):
        self.world = world
        self.restored = []

    def snapshot(self):
        return _snap(cursor=self.world["cursor"], app=self.world["front"])

    def restore(self, before, after):
        self.restored.append((before, after))
        return ["cursor"]


class FakeRoute:
    """Pretends to be Mint: presses through the 'mouse', moves the cursor, says it was background."""
    name = "fake"

    def __init__(self, fixture, world, press="btn-save", claim="Pressed 'Save' via AXPress in the background."):
        self.fixture, self.world, self.press, self.claim = fixture, world, press, claim

    def run(self, steps):
        self.fixture.press(self.press, "mouse")
        self.world["cursor"] = (500, 500)
        return [cu.StepResult("ui_act", dict(steps[-1][1]), self.claim, 0.1)]


@needs_bench
def test_run_case_scores_on_state_and_flags_a_lying_background_claim():
    task = {t.id: t for t in tasks_mod.tasks()}["b4-save"]
    fixture, world = FakeFixture(), {"cursor": (10, 10), "front": ("Finder", 11)}
    observer = FakeObserver(world)
    row = tasks_mod.run_case(task, 0, FakeRoute(fixture, world), fixture, observer, None)
    assert row["passed"] is True and row["observed"] == {"presses": {"btn-save": 1}}
    assert row["claimed"] == "background" and row["press_via"] == "mouse"
    assert row["disturbance"]["cursor_moved"] and row["leaks"] and "claimed background" in row["leaks"][0]
    assert observer.restored                                   # the cursor is put back
    # the reply says it worked, the state says the near miss was pressed: FAIL
    fixture2, world2 = FakeFixture(), {"cursor": (10, 10), "front": ("Finder", 11)}
    row2 = tasks_mod.run_case(task, 1, FakeRoute(fixture2, world2, press="btn-save-draft", claim="Clicked 'Save'."),
                              fixture2, FakeObserver(world2), None)
    assert row2["passed"] is False and row2["observed"] == {"presses": {"btn-save-draft": 1}}


class BackGate:
    """The user touched the mouse during the case."""

    def away(self):
        return False, "idle 0s"

    def mark_own_input(self):
        pass


@needs_bench
def test_run_case_never_warps_the_cursor_back_under_a_returning_user():
    task = {t.id: t for t in tasks_mod.tasks()}["b4-save"]
    fixture, world = FakeFixture(), {"cursor": (10, 10), "front": ("Finder", 11)}
    observer = FakeObserver(world)
    row = tasks_mod.run_case(task, 0, FakeRoute(fixture, world), fixture, observer, BackGate())
    assert row["user_returned"] is True and not observer.restored and "restored" not in row


class RetryRoute:
    """Pretends to be Mint on 'Refresh feed': each step's result in turn; the pointer press on `press_on`."""
    name = "fake"

    def __init__(self, fixture, results, press_on=2):
        self.fixture, self.results, self.press_on = fixture, results, press_on

    def run(self, steps):
        calls = [s for s in steps if s[0] == "ui_act"]
        out = []
        for i, (step, text) in enumerate(zip(calls, self.results), 1):
            if i == self.press_on:
                self.fixture.press("btn-refresh-feed", "mouse")
            out.append(cu.StepResult("ui_act", dict(step[1]), text, 0.1))
        return out


@needs_bench
def test_axpress_raises_is_the_documented_retry_judged_on_both_answers():
    task = {t.id: t for t in tasks_mod.tasks()}["f-axpress-raises"]
    calls = [a for n, a in task.steps if n == "ui_act"]
    assert len(calls) == 2 and calls[0] == calls[1]            # the identical retry the result asks for
    first = "UNVERIFIED via accessibility (background): Pressed the button 'Refresh feed'; not pressed a second time."
    second = ("UNVERIFIED via the real mouse/keyboard (foreground): Clicked the button 'Refresh feed'; "
              "Finder is back in front, as the user had it.")
    fixture, world = FakeFixture(), {"cursor": (10, 10), "front": ("Finder", 11)}
    row = tasks_mod.run_case(task, 0, RetryRoute(fixture, [first, second]), fixture, FakeObserver(world), None)
    assert row["passed"] is True and row["route_detail"] == ["background", "foreground"], row
    # pressed on the first call (an automatic second press after an error), saying background: FAIL
    fixture2, world2 = FakeFixture(), {"cursor": (10, 10), "front": ("Finder", 11)}
    row2 = tasks_mod.run_case(task, 0, RetryRoute(fixture2, ["CONFIRMED via the real mouse/keyboard (background): "
                                                            "Clicked 'Refresh feed'.", second], press_on=1),
                              fixture2, FakeObserver(world2), None)
    assert row2["passed"] is False and row2["observed"]["presses"] == {"btn-refresh-feed": 1}
    assert row2["observed"]["claims"] == ["step 1: wanted unverified (background), got confirmed (background)"]
    # the retry that never says it came forward is a fail too
    fixture3, world3 = FakeFixture(), {"cursor": (10, 10), "front": ("Finder", 11)}
    row3 = tasks_mod.run_case(task, 0, RetryRoute(fixture3, [first, second.replace("(foreground)", "(background)")]),
                              fixture3, FakeObserver(world3), None)
    assert row3["passed"] is False and "step 2" in row3["observed"]["claims"][0]


@needs_bench
def test_claims_mismatch():
    assert tasks_mod.claims_mismatch([("unverified", ""), ("", "foreground")],
                                     [{"effect": "unverified", "delivery": "background"},
                                      {"effect": "confirmed", "delivery": "foreground"}]) == []
    assert tasks_mod.claims_mismatch([("", "foreground")], []) == ["step 1: wanted any (foreground), got none (none)"]


@needs_bench
def test_oracle_route_solves_every_task_on_the_fake_app():
    oracle = tasks_mod.make_route("oracle")
    for task in tasks_mod.tasks():
        if task.scene != "buttons":
            continue
        fixture, world = FakeFixture(), {"cursor": (1, 1), "front": ("Finder", 11)}
        row = tasks_mod.run_case(task, 0, oracle, fixture, FakeObserver(world), None)
        assert row["passed"] is True, task.id
        assert not row["leaks"] and row["press_via"] == ("oracle" if task.oracle else None)


@needs_bench
def test_fixture_snapshot_names_rows_and_boxes():
    state = {"scene": "list", "density": 0, "window_frame": [0, 0, 520, 448], "installed": ["git-history"],
             "frames": {"btn-install-git-history": [380, 200, 96, 28], "row-git-history": [20, 196, 440, 36],
                        "btn-install-csv-colors": [380, 20, 96, 28], "scroll-list": [16, 16, 458, 260]}}
    snap = tasks_mod.fixture_snapshot(state, cu.scenarios(), "x.png")
    by_id = {e["ax_id"]: e for e in snap["elements"]}
    assert "scroll-list" not in by_id
    assert by_id["btn-install-git-history"]["within"] == "Git History"
    assert by_id["btn-install-git-history"]["label"] == "Installed"
    assert by_id["btn-install-csv-colors"]["label"] == "Install"
    assert by_id["row-git-history"]["interactive"] is False


# --- groundbench: ScreenSpot-style scoring, refusals, hash, replay ---------------------------------

def test_score_screenspot_style_and_refusals():
    box = (100, 100, 50, 20)
    assert groundbench.score((120, 110), [box])["outcome"] == "hit"
    assert groundbench.score((200, 110), [box])["outcome"] == "wrong"
    assert groundbench.score(None, [box])["outcome"] == "abstain"
    assert groundbench.score(None, [box])["hit"] is False
    assert groundbench.score(None, [], refusal=True) == {"hit": True, "answered": False, "outcome": "abstain_ok"}
    assert groundbench.score((120, 110), [], refusal=True)["hit"] is False
    assert groundbench.is_refusal({"expect": {"absent": True}})
    assert not groundbench.is_refusal({"expect": {"id": "btn-save"}})


def test_aggregate_and_density_buckets():
    rows = [{"grounder": "text", "app": "A", "hit": True, "outcome": "hit", "seconds": 1.0},
            {"grounder": "text", "app": "A", "hit": False, "outcome": "wrong", "seconds": 1.0},
            {"grounder": "text", "app": "A", "hit": True, "outcome": "abstain_ok", "refusal": True, "seconds": 1.0},
            {"grounder": "jev", "app": "B", "hit": False, "outcome": "abstain", "seconds": 3.0},
            {"query": "x", "error": "not on screen"}]
    g = {r["grounder"]: r for r in groundbench.aggregate(rows, ("grounder",))}
    assert g["text"]["accuracy"] == 0.5 and g["text"]["refusal_accuracy"] == 1.0 and g["text"]["wrong"] == 1
    assert g["jev"]["accuracy"] == 0.0 and g["jev"]["abstain"] == 1 and g["jev"]["mean_seconds"] == 3.0
    assert [groundbench.density_bucket(n) for n in (3, 8, 12, 18, 24, 0)] == ["~4", "~4", "~12", "~12", "~24", "?"]


def test_dataset_hash_changes_with_items_and_snapshots():
    items = [{"target": "Save", "expect": {"id": "btn-save"}}]
    h = groundbench.dataset_hash(items)
    assert h == groundbench.dataset_hash(json.loads(json.dumps(items)))
    assert h != groundbench.dataset_hash([{"target": "Save draft", "expect": {"id": "btn-save"}}])
    assert h != groundbench.dataset_hash(items, {"s": {"elements": [{"label": "Save"}]}})


def _write_snapshot(folder: Path):
    elements = []
    for i, (cid, label) in enumerate([("btn-cancel", "Cancel"), ("btn-save", "Save"),
                                      ("btn-save-draft", "Save draft"), ("btn-save-as", "Save as…")]):
        elements.append({"id": i + 1, "ax_id": cid, "role": "AXButton", "kind": "button", "label": label,
                         "box": [20 + i * 100, 40, 90, 24], "interactive": True})
    snap = {"name": "fx-buttons-4", "app": "MintFixture", "title": "Mint Fixture", "window": [0, 0, 520, 448],
            "dialog": None, "elements": elements}
    (folder / "fx-buttons-4.json").write_text(json.dumps(snap))
    return snap


def test_snapshot_inventory_shuffles_but_truth_follows_the_id(tmp_path):
    snap = _write_snapshot(tmp_path)
    plain = groundbench.snapshot_inventory(snap)
    mixed = groundbench.snapshot_inventory(snap, "shuffled", seed="s1")
    again = groundbench.snapshot_inventory(snap, "shuffled", seed="s1")
    assert [e["label"] for e in mixed["elements"]] == [e["label"] for e in again["elements"]]   # seeded
    assert [e["id"] for e in mixed["elements"]] == [1, 2, 3, 4]          # numbered in the new order
    assert [e["label"] for e in mixed["elements"]] != [e["label"] for e in plain["elements"]]
    assert all(e["ref"] is None for e in plain["elements"])
    assert groundbench.truth_boxes(mixed, {"id": "btn-save"}) == [(120, 40, 90, 24)]
    assert groundbench.truth_boxes(mixed, {"id": "btn-save"}, {"btn-save": [1, 2, 3, 4]}) == [(1, 2, 3, 4)]
    assert groundbench.truth_boxes(mixed, {"absent": True}) == []
    assert groundbench.truth_boxes(plain, {"label": "Save draft", "roles": ["AXButton"]}) == [(220, 40, 90, 24)]


def test_replay_offline_with_the_text_chooser(tmp_path, monkeypatch):
    _write_snapshot(tmp_path)
    cases = tmp_path / "cases"
    cases.mkdir()
    items = [{"snapshot": "fx-buttons-4", "target": "Save", "expect": {"id": "btn-save"}},
             {"snapshot": "fx-buttons-4", "target": "Save draft", "expect": {"id": "btn-save-draft"}},
             {"snapshot": "fx-buttons-4", "target": "Delete", "expect": {"absent": True}},
             {"snapshot": "missing-snap", "target": "Save", "expect": {"id": "btn-save"}}]
    (cases / "fx.json").write_text(json.dumps(items))
    monkeypatch.setattr(groundbench, "CASES", cases)
    text, report = groundbench.replay("fx", ["text"], ["element", "shuffled"], reps=2, folder=tmp_path)
    scored = [r for r in report["results"] if not r.get("error")]
    assert len(scored) == 3 * 2 * 2
    assert all(r["hit"] for r in scored), [r for r in scored if not r["hit"]]
    assert sum(1 for r in report["results"] if r.get("error")) == 4          # the missing snapshot, each rep/order
    g = report["by_grounder"][0]
    assert g["accuracy"] == 1.0 and g["refusal_accuracy"] == 1.0
    assert report["dataset_hash"] in text and "by density" in text


def test_scenarios_ids_are_unique():
    path = BENCH / "fixtures" / "MintFixture" / "scenarios.json" if BENCH else None
    if path is None or not path.exists():
        pytest.skip("the fixture app is not in this checkout")
    sc = json.loads(path.read_text())
    ids = [t["id"] for t in sc["scenes"]["buttons"]["targets"]]
    ids += [c["id"] for c in sc["scenes"]["form"]["controls"].values()]
    ids += [c["id"] for c in sc["scenes"]["web"]["controls"].values()]
    assert len(ids) == len(set(ids))


# --- live (opt-in) ------------------------------------------------------------------------------

@needs_bench
@pytest.mark.skipif(os.environ.get("MINT_LIVE_UI") != "1", reason="live UI test: set MINT_LIVE_UI=1 (needs an idle Mac)")
def test_live_oracle_route_on_the_fixture_app(tmp_path):
    away, why = cu.IdleGate().away()
    if not away:
        pytest.skip(f"the Mac is in use ({why})")
    out = tasks_mod.run("oracle", 1, r"^(b4-|f-type-name|l-install-visible|w-save-draft)", tmp_path / "run", live_out=lambda s: None)
    ok, text = cu.report(out)
    assert ok, text
    rows = cu.read_jsonl(out / "results.jsonl")
    assert all(r["passed"] for r in rows), text
    assert not any(r["leaks"] for r in rows), text           # the oracle never touches the desktop
