"""Choosing the target: Jev's closed table (ids, cap, ranking, risky actions left out, the screen summary),
checking Jev's answer (floor, unknown ids, lookalike ties, abstain), zoom maths, 0-999 coordinates, and
click_at finding what it names (the ladder and the per-screenshot cache). No screen, no network."""
import pytest

try:
    from mint.screen import choose, ground, vision
except ImportError:
    from mint import choose, ground, vision

PIL = pytest.importorskip("PIL.Image")

WINDOW = (0.0, 0.0, 1000.0, 600.0)


def _el(role, label, box, n=0, **kw):
    e = {"id": n, "role": role, "label": label, "box": box, "kind": ground.ROLE_WORDS.get(role, "x"), "value": "",
         "hint": "", "enabled": True, "focused": False, "in_dialog": False, "dialog": None, "ref": None,
         "depth": 3, "interactive": role in ground.INTERACTIVE, "subrole": ""}
    e.update(kw)
    return e


def _inv(elements, **kw):
    inv = {"app": "Notes", "pid": None, "title": "Note", "window": WINDOW, "dialog": None, "dialog_title": "",
           "dialog_soft": False, "elements": elements, "walked": 0, "offscreen": []}
    inv.update(kw)
    return inv


def _form():
    return [_el("AXTextField", "Note", (200, 200, 300, 22), 1), _el("AXButton", "Save", (520, 200, 60, 22), 2),
            _el("AXButton", "Save draft", (600, 200, 80, 22), 3), _el("AXCheckBox", "I agree", (200, 260, 80, 20), 4)]


# --- the table -------------------------------------------------------------------------------------

def test_ids_come_from_role_and_label_not_the_list_position():
    table = choose.build(_form(), "save the note", WINDOW)
    ids = [c.id for c in table.candidates]
    assert ids == ["text_input:note", "button:save", "button:save-draft", "checkbox:i-agree"]
    # same controls, another order and other list numbers: the same ids
    shuffled = list(reversed(_form()))
    for i, e in enumerate(shuffled):
        e["id"] = 40 + i
    assert sorted(c.id for c in choose.build(shuffled, "", WINDOW).candidates) == sorted(ids)
    assert table.candidates[0].action == "type into" and table.candidates[3].action == "toggle"
    assert set(table.criteria()) == set(ids) | {"reobserve", "abstain"}


def test_twin_labels_are_told_apart_by_row_then_place_then_a_stable_hash():
    rows = [_el("AXButton", "Install", (250, 130, 50, 20), 1, within="Rainbow CSV"),
            _el("AXButton", "Install", (250, 200, 50, 20), 2, within="CSV Colorful Table")]
    ids = [c.id for c in choose.build(rows, "", WINDOW).candidates]
    assert ids == ["button:install@rainbow-csv", "button:install@csv-colorful-table"]
    places = [_el("AXButton", "Pin chat", (10, 10, 50, 20), 1), _el("AXButton", "Pin chat", (10, 550, 50, 20), 2)]
    assert [c.id for c in choose.build(places, "", WINDOW).candidates] == ["button:pin-chat@top-left",
                                                                           "button:pin-chat@bottom-left"]
    same = [_el("AXButton", "Pin chat", (10, 10 + 20 * i, 50, 20), i) for i in range(3)]
    first = [c.id for c in choose.build(same, "", WINDOW).candidates]
    assert len(set(first)) == 3 and all(i.startswith("button:pin-chat@") for i in first)
    for i, e in enumerate(same):
        e["id"] = 90 + i                                  # renumbered: ids do not move
    assert [c.id for c in choose.build(same, "", WINDOW).candidates] == first


def test_risky_actions_are_left_out_of_the_table():
    pool = _form() + [_el("AXButton", label, (10, 400 + 25 * i, 80, 20), 10 + i) for i, label in enumerate(
        ["Delete note", "Send", "Buy now", "Close", "Sign out", "Move to Trash", "Share"])]
    table = choose.build(pool, "delete the note", WINDOW)
    labels = {c.element["label"] for c in table.candidates}
    assert labels == {"Note", "Save", "Save draft", "I agree", "Share"}
    assert table.excluded["risky"] == 6
    assert len(choose.build(pool, "", WINDOW, allow_risky=True).candidates) == len(pool)


def test_cap_keeps_plan_words_then_request_words_then_preranked_in_screen_order():
    pool = [_el("AXButton", f"Tool {i}", (10 + 30 * i, 40, 25, 20), i) for i in range(40)]
    pool.insert(30, _el("AXButton", "Export PDF", (500, 300, 80, 20), 99))
    pool.insert(35, _el("AXButton", "Add files and more", (500, 350, 80, 20), 98))
    pool.insert(38, _el("AXButton", "Page settings", (500, 380, 80, 20), 97))
    seen = {}

    def prerank(goal, options):
        seen.update(options)
        return {next(i for i, d in options.items() if "Add files" in d): 0.9}

    table = choose.build(pool, "attach a file and export it", WINDOW, plan=["open page settings"],
                         prerank=prerank)
    labels = [c.element["label"] for c in table.candidates]
    assert len(labels) == choose.CAP and table.dropped == len(pool) - choose.CAP
    assert {"Export PDF", "Add files and more", "Page settings"} <= set(labels)
    assert labels.index("Export PDF") < labels.index("Add files and more") < labels.index("Page settings")
    assert len(seen) <= choose.PRERANK_MAX
    # without a pre-rank, a synonym with no shared word is cut, but plan and request words stay
    plain = [c.element["label"] for c in choose.build(pool, "export it", WINDOW, plan=["page settings"]).candidates]
    assert "Export PDF" in plain and "Page settings" in plain and "Add files and more" not in plain
    # a failing pre-rank costs nothing
    broken = choose.build(pool, "export", WINDOW, prerank=lambda g, o: 1 / 0)
    assert len(broken.candidates) == choose.CAP


def test_state_summary_says_what_is_empty_open_focused_and_enabled_without_values():
    elements = [_el("AXTextField", "Project name", (100, 100, 200, 22), 1, value="secret plan", in_dialog=True,
                    focused=True),
                _el("AXTextField", "Description", (100, 140, 200, 22), 2, in_dialog=True),
                _el("AXButton", "Create project", (100, 180, 100, 22), 3, enabled=False, in_dialog=True),
                _el("AXButton", "Cancel", (210, 180, 60, 22), 4, in_dialog=True)]
    summary = choose.state_summary(_inv(elements, dialog=(80, 80, 300, 200), dialog_title="Create project"))
    assert summary["open"].startswith("dialog 'Create project'")
    assert summary["fields"] == {"Project name": "filled", "Description": "empty"}
    assert summary["submit_buttons"] == {"Create project": "disabled"}
    assert summary["focused"] == "text field 'Project name'"
    assert "secret" not in repr(summary)
    table = choose.build(elements, "", WINDOW)
    assert "secret" not in repr(table.criteria()) and "has text" in table.criteria()["text_input:project-name"]


# --- Jev's answer ------------------------------------------------------------------------------------

def _answer(choice, confidence, **probs):
    return {"choice": choice, "confidence": confidence, "probabilities": probs or {choice: confidence}}


def test_answers_are_checked_against_the_table():
    table = choose.build(_form(), "", WINDOW)
    ok = choose.decide(_answer("button:save", 0.9), table)
    assert ok.kind == "act" and ok.candidate.element["label"] == "Save"
    assert choose.decide(_answer("button:print", 0.99), table).kind == "rejected"          # not offered
    assert choose.decide(_answer("2", 0.99), table).kind == "rejected"                     # a list number
    low = choose.decide(_answer("button:save", choose.FLOOR - 0.01), table)
    assert low.kind == "rejected" and "only" in low.why
    assert choose.decide(_answer("button:save", choose.FLOOR), table).kind == "act"
    assert choose.decide(_answer("abstain", 0.8), table).kind == "abstain"
    assert choose.decide(_answer("reobserve", 0.8), table).kind == "reobserve"
    assert choose.decide(None, table).kind == "unavailable"
    assert choose.decide({"confidence": 1}, table).kind == "unavailable"


def test_two_lookalikes_nearly_tied_are_not_a_choice():
    twins = [_el("AXButton", "Pin chat", (10, 10, 50, 20), 1), _el("AXButton", "Pin chat", (10, 550, 50, 20), 2)]
    table = choose.build(twins, "", WINDOW)
    a, b = (c.id for c in table.candidates)
    tied = choose.decide({"choice": a, "confidence": 0.6, "probabilities": {a: 0.5, b: 0.42}}, table)
    assert tied.kind == "rejected" and "split" in tied.why
    clear = choose.decide({"choice": a, "confidence": 0.9, "probabilities": {a: 0.9, b: 0.05}}, table)
    assert clear.kind == "act"
    # a near tie with a DIFFERENT label is Jev's call to make
    table = choose.build(_form(), "", WINDOW)
    near = choose.decide({"choice": "button:save", "confidence": 0.6,
                          "probabilities": {"button:save": 0.5, "button:save-draft": 0.45}}, table)
    assert near.kind == "act"


def test_ask_jev_sends_a_closed_table_and_the_screen():
    sent = {}

    def fake_ask(state, questions, timeout=6.0):
        sent.update(state=state, questions=questions)
        return {"pick": _answer("button:save-draft", 0.97)}

    inv = _inv(_form())
    table = choose.build(inv["elements"], "save it as a draft", WINDOW)
    choice = choose.ask_jev("save it as a draft", table, choose.state_summary(inv), ask=fake_ask)
    assert choice.kind == "act" and choice.candidate.element["label"] == "Save draft"
    criteria = sent["questions"]["pick"]["criteria"]
    assert "reobserve" in criteria and "abstain" in criteria and "none" not in criteria
    assert len(criteria) <= choose.CAP + 2
    assert sent["state"]["request"] == "save it as a draft" and sent["state"]["screen"]["fields"] == {"Note": "empty"}
    assert choose.ask_jev("x", choose.Table([]), ask=fake_ask).kind == "abstain"
    assert choose.ask_jev("x", table, ask=lambda *a, **k: None).kind == "unavailable"


@pytest.fixture
def fake_jev(monkeypatch):
    """choose_by_jev with Jev answering from a script."""
    replies = []
    seen = []

    def ask(state, questions, timeout=6.0, retries=0):
        seen.append(questions)
        return {"pick": replies.pop(0)} if replies else None
    try:
        from mint.core import jev
    except ImportError:
        from mint import jev
    monkeypatch.setattr(jev, "available", lambda: True)
    monkeypatch.setattr(jev, "ask", ask)
    monkeypatch.setattr(ground, "_jev_prerank", lambda goal, options: {})
    return replies, seen


def test_choose_by_jev_acts_only_on_a_sure_answer(fake_jev):
    replies, _ = fake_jev
    inv = _inv(_form())
    replies.append(_answer("button:save", 0.95))
    pick, why = ground.choose_by_jev("save the note", inv["elements"], inv)
    assert pick["label"] == "Save" and "0.95" in why
    replies.append(_answer("button:save", 0.3, **{"button:save": 0.3, "button:save-draft": 0.2,
                                                    "checkbox:i-agree": 0.1}))
    pick, why = ground.choose_by_jev("save the note", inv["elements"], inv)
    assert pick is None and "not using it" in why and "Save draft" in why
    replies.append(_answer("abstain", 0.9, **{"abstain": 0.9, "button:save": 0.05, "button:save-draft": 0.03,
                                              "text_input:note": 0.01, "checkbox:i-agree": 0.0}))
    pick, why = ground.choose_by_jev("print it", inv["elements"], inv)
    assert pick is None and why.startswith("Jev: not sure") and why.count("(0.") == 3       # the top 3
    replies.append(_answer("reobserve", 0.8))
    pick, why = ground.choose_by_jev("the new tab", inv["elements"], inv)
    assert pick is None and why.startswith(ground.JEV_LOOK_AGAIN)


def test_a_pick_that_leaves_out_a_named_thing_is_a_substitution(fake_jev):
    replies, _ = fake_jev
    inv = _inv([_el("AXButton", "New chat", (10, 100, 120, 22), 1), _el("AXButton", "Search", (10, 130, 120, 22), 2)])
    replies.append(_answer("button:new-chat", 0.85))
    pick, why = ground.choose_by_jev("Start new chat in garden-planner", inv["elements"], inv)
    assert pick is None and "garden-planner" in why
    replies.append(_answer("button:new-chat", 0.8))
    pick, _ = ground.choose_by_jev("start a new conversation", inv["elements"], inv)
    assert pick is not None
    assert choose.names("switch the model to Luna") == ["Luna"]
    assert choose.names("the prompt field where I type") == []
    assert choose.missing_names("Install CSV Colorful Table",
                                {"label": "Install", "within": "CSV Colorful Table, 0.0.3"}) == ""


def test_ground_falls_through_to_vision_when_jev_is_unsure(fake_jev, monkeypatch):
    replies, _ = fake_jev
    looked = []
    monkeypatch.setattr(ground, "choose_by_vision",
                        lambda target, pool, inv, model=None, zoom=True: (looked.append(target) or pool[0], "vision"))
    inv = _inv(_form())
    replies.append(_answer("button:save-draft", 0.4))
    chosen, _, why = ground.ground("keep my changes", "click", inv)
    assert looked == ["keep my changes"] and why == "vision"


# --- zoom and coordinates ------------------------------------------------------------------------------

def test_denorm_rounds_and_clamps_0_999():
    assert choose.denorm(0, 1000) == 0
    assert choose.denorm(500, 1000) == 500
    assert choose.denorm(999, 1000) == 999
    assert choose.denorm(1000, 1000) == 999            # clamped to the last pixel
    assert choose.denorm(-20, 640) == 0
    assert choose.denorm(250, 640) == 160
    assert choose.denorm("x", 640) == 0


def test_prepare_downscales_and_records_the_ratio():
    image, ratio = choose.prepare(PIL.new("RGB", (3200, 1800)), 1600)
    assert image.size == (1600, 900) and ratio == 0.5
    same, ratio = choose.prepare(PIL.new("RGB", (800, 600)), 1600)
    assert same.size == (800, 600) and ratio == 1.0


def test_zoom_region_pads_20_percent_keeps_a_minimum_and_stays_inside():
    x, y, w, h = choose.zoom_region((400, 300, 200, 100), WINDOW)
    assert (w, h) == pytest.approx((280, 140)) and (x + w / 2, y + h / 2) == pytest.approx((500, 350))
    tiny = choose.zoom_region((500, 300, 16, 16), WINDOW)
    assert tiny[2] == pytest.approx(choose.ZOOM_MIN[0] * 1.4) and tiny[3] == pytest.approx(choose.ZOOM_MIN[1] * 1.4)
    corner = choose.zoom_region((0, 0, 10, 10), WINDOW)
    assert corner[0] == 0 and corner[1] == 0
    edge = choose.zoom_region((990, 590, 10, 10), WINDOW)
    assert edge[0] + edge[2] == pytest.approx(1000) and edge[1] + edge[3] == pytest.approx(600)


@pytest.mark.parametrize("scale", [1.0, 2.0])
def test_zoom_maps_back_exactly_on_retina_and_not(scale):
    area = (100.0, 50.0, 800.0, 600.0)                 # a window at (100, 50) in screen points
    image = (int(800 * scale), int(600 * scale))
    target = (480.0, 330.0, 16.0, 16.0)                # a 16 pt icon
    zoom = choose.plan_zoom(choose.zoom_region(target, area), area, scale, image)
    assert zoom.size[0] <= choose.ZOOM_WIDTH
    x0, y0, x1, y1 = zoom.crop
    assert 0 <= x0 < x1 <= image[0] and 0 <= y0 < y1 <= image[1]
    # the model points at the icon's centre on the zoom image (0-999): it maps into the icon
    cx = ((target[0] + 8 - area[0]) * scale - x0) / (x1 - x0) * 1000
    cy = ((target[1] + 8 - area[1]) * scale - y0) / (y1 - y0) * 1000
    px, py = zoom.to_screen(cx, cy)
    assert px == pytest.approx(488, abs=1.5) and py == pytest.approx(338, abs=1.5)
    # the corners of the zoom image are the corners of the crop
    assert zoom.to_screen(0, 0)[0] == pytest.approx(area[0] + x0 / scale, abs=1.0 / scale * (x1 - x0) / zoom.size[0])
    far = zoom.to_screen(999, 999)
    assert far[0] <= area[0] + x1 / scale and far[1] <= area[1] + y1 / scale
    assert zoom.contains((488, 338)) and not zoom.contains((120, 60))


def test_needs_zoom_for_small_or_doubtful_picks():
    assert choose.needs_zoom((0, 0, 16, 16), 0.95)
    assert not choose.needs_zoom((0, 0, 80, 22), 0.95)
    assert choose.needs_zoom((0, 0, 80, 22), 0.4)
    assert not choose.needs_zoom((0, 0, 80, 22), None)


def test_vision_zooms_on_a_small_pick_and_maps_its_point(monkeypatch):
    area = (0.0, 0.0, 400.0, 300.0)
    pool = [_el("AXButton", "", (200, 150, 14, 14), 7), _el("AXButton", "", (220, 150, 14, 14), 8),
            _el("AXButton", "Search", (20, 20, 80, 22), 9)]
    inv = _inv(pool, window=area)
    monkeypatch.setattr(ground, "marked", lambda inv, pool, area=None: (PIL.new("RGB", (800, 600)), (0.0, 0.0, 400.0, 300.0)))
    asked = []

    def fake_generate(contents, models=None, json_mode=True):
        asked.append(contents[1])
        if len(asked) == 1:
            assert "800x600 pixels" in contents[1]
            return '{"id": 7, "confidence": 0.9, "why": "gear"}', "m"
        assert "[8]" in contents[1] and "[9]" not in contents[1]          # only the boxes inside the crop
        # the zoom shows 140x80 pt (+20%) around icon 7 at 2x; icon 8's centre (227, 157) is at
        # x 602, y 500 of that image in 0-999
        return '{"id": null, "point": [500, 602], "why": "right icon"}', "m"
    monkeypatch.setattr(ground, "_generate", fake_generate)
    chosen, why = ground.choose_by_vision("the settings icon", pool, inv)
    assert len(asked) == 2 and chosen is pool[1] and "zoom pointed at [8]" in why
    # the zoom saying it is not there drops the first pick rather than clicking it
    asked.clear()
    monkeypatch.setattr(ground, "_generate", lambda contents, models=None, json_mode=True: (
        asked.append(1) or ('{"id": 7, "confidence": 0.9}' if len(asked) == 1 else '{"id": null, "point": null}'), "m"))
    chosen, why = ground.choose_by_vision("the settings icon", pool, inv)
    assert chosen is None and "did not find" in why


def test_vision_keeps_a_large_sure_pick_without_zooming(monkeypatch):
    pool = [_el("AXButton", "Save", (200, 150, 80, 22), 3)]
    monkeypatch.setattr(ground, "marked", lambda inv, pool, area=None: (PIL.new("RGB", (800, 600)), (0.0, 0.0, 400.0, 300.0)))
    calls = []
    monkeypatch.setattr(ground, "_generate",
                        lambda contents, models=None, json_mode=True: (calls.append(1) or '{"id": 3, "confidence": 0.9}', "m"))
    chosen, _ = ground.choose_by_vision("Save", pool, _inv(pool))
    assert chosen is pool[0] and len(calls) == 1


def test_point_by_vision_converts_through_the_image_it_sent(monkeypatch):
    area = (100.0, 50.0, 1000.0, 500.0)
    monkeypatch.setattr(ground, "screenshot", lambda area, window_id=None: (PIL.new("RGB", (4000, 2000)), 4.0))
    answers = ['[{"point": [500, 250], "label": "x"}]', '[{"point": [500, 500], "label": "x"}]']
    prompts = []

    def fake_generate(contents, models=None, json_mode=True):
        prompts.append(contents[1])
        return answers[len(prompts) - 1], "robot"
    monkeypatch.setattr(ground, "_generate", fake_generate)
    point, why = ground.point_by_vision("the gear", area, zoom=False)
    assert "1600x800 pixels" in prompts[0]                        # pre-downscaled, size stated
    assert point == pytest.approx((100 + (400 + 0.5) / 0.4 / 4, 50 + (400 + 0.5) / 0.4 / 4))   # px 400 of 1600 and of 800
    prompts.clear()
    point, why = ground.point_by_vision("the gear", area)
    assert len(prompts) == 2 and "zoomed" in why
    assert point == pytest.approx((350, 300), abs=1.0)            # the zoom's centre is the first point


# --- click_at: find what it names -----------------------------------------------------------------------

def test_locate_prefers_the_accessibility_name_nearest_the_hint():
    elements = [_el("AXButton", "Uninstall", (600, 210, 70, 20), 1), _el("AXButton", "Disable", (550, 210, 46, 20), 2),
                _el("AXButton", "Uninstall", (600, 500, 70, 20), 3)]
    calls = []
    found = choose.locate("Uninstall", (576, 221), None, inventory=lambda: (calls.append("ax") or _inv(elements)),
                          read_text=lambda h, t: (calls.append("ocr") or ((0, 0), "")), vision=None)
    assert found.how == "accessibility" and found.point == (635, 220) and calls == ["ax"]
    # two equally near: the hint cannot decide, so the next rung is asked
    twins = [_el("AXButton", "OK", (100, 100, 40, 20), 1), _el("AXButton", "OK", (100, 140, 40, 20), 2)]
    found = choose.locate("OK", (120, 130), None, inventory=lambda: _inv(twins), read_text=lambda h, t: ((121, 131), "OK"))
    assert found.how == "screen text"
    # too far from the hint: not that one
    assert choose.by_name("Uninstall", elements[:1], hint=(100, 100)) is None


def test_locate_ladder_screen_text_then_vision_then_nothing():
    nothing = _inv([_el("AXButton", "", (10, 10, 50, 20), 1)])
    found = choose.locate("general", (100, 300), None, inventory=lambda: _inv([_el("AXGroup", "Sidebar", (0, 0, 200, 600), 1)]),
                          read_text=lambda h, t: ((95, 305), "general"), vision=lambda t, inv: (None, "no"))
    assert found.how == "screen text" and found.point == (95, 305)
    seen = _el("AXButton", "", (300, 300, 16, 16), 5)
    found = choose.locate("the gear icon", (310, 290), None, inventory=lambda: _inv([seen]),
                          read_text=lambda h, t: (h, ""), vision=lambda t, inv: (seen, "vision"))
    assert found.how == "vision" and found.point == (308, 308)
    assert choose.locate("the gear icon", (310, 290), None, inventory=lambda: nothing,
                         read_text=lambda h, t: (h, ""), vision=lambda t, inv: (None, "no")) is None
    assert choose.locate("   ", (1, 1), None) is None


def test_the_same_description_on_the_same_screenshot_clicks_the_same_place(monkeypatch):
    choose.forget()
    calls = []
    elements = [_el("AXButton", "Run", (100, 100, 40, 20), 1)]

    def inventory():
        calls.append(1)
        return _inv(elements)
    first = choose.locate("Run", None, ("look-1", 42), inventory=inventory)
    elements[0]["box"] = (300, 300, 40, 20)                            # the window moved things
    again = choose.locate("run ", None, ("look-1", 42), inventory=inventory)
    assert again.how == "cache" and again.point == first.point and len(calls) == 1
    fresh = choose.locate("Run", None, ("look-2", 42), inventory=inventory)   # a new look: asked again
    assert fresh.point == (320, 310) and len(calls) == 2
    monkeypatch.setattr(choose, "CACHE_SECONDS", -1)
    assert choose.locate("Run", None, ("look-2", 42), inventory=inventory).how == "accessibility"
    choose.forget()


def test_click_at_needs_a_target_or_a_point(monkeypatch):
    try:
        from mint.tools import fastinput
    except ImportError:
        from mint import fastinput
    monkeypatch.setattr(fastinput, "has_accessibility", lambda: True)
    assert vision.click_at().startswith("Say what to click")
    monkeypatch.setattr(vision, "_last_area", None)
    assert vision.click_at(500, 500).startswith("Call look first")
    assert "between 0 and 1000" in vision.click_at(1500, 5, target="x")
    monkeypatch.setattr(vision, "find_target", lambda target, pointed=None: None)
    assert vision.click_at(target="the gear").startswith("NOT CLICKED")


def test_click_at_declaration_makes_the_target_the_point():
    try:
        from mint.tools import registry as tools
    except ImportError:
        from mint import tools
    assert tools._number(None) is None and tools._number("12.5") == 12.5 and tools._number("x") is None


# --- fix-see: near misses, and what a request calls a control -------------------------------------

def test_what_a_request_calls_a_control_is_not_a_name():
    assert choose.names("the Size pop-up") == ["Size"]
    assert choose.names("open the Theme drop-down menu") == ["Theme"]
    assert choose.names("tick the Remember me check box") == ["Remember"]
    assert choose.names("the 'pop-up' next to Size") == ["Size"]
    assert choose.names("the Pop-Up Blocker switch") == ["Blocker"]
    assert choose.names("Start new chat in garden-planner") == ["garden-planner"]       # a real name stays
    assert choose.missing_names("the Size pop-up", {"label": "Size"}) == ""
    assert choose.content_words("click the Size pop-up button in the sidebar") == ["size"]
    assert choose.content_words("the Remember me check box") == ["remember"]
    assert choose._has("draft", {"drafts"}) and choose._has("colors", {"color"})
    assert not choose._has("install", {"uninstall"}) and not choose._has("2", {"12"})
    assert not choose._has("2026", {"20261"}) and choose._has("2026", {"2026"})


def test_a_word_stuck_on_the_picks_name_that_nothing_has_is_a_near_miss(fake_jev):
    replies, _ = fake_jev
    inv = _inv(_form())
    replies.append(_answer("button:save", 0.95))
    pick, why = ground.choose_by_jev("Save all", inv["elements"], inv)
    assert pick is None and "only part of the request" in why and "'all'" in why
    replies.append(_answer("button:save", 0.97))
    pick, _ = ground.choose_by_jev("the Save As button", inv["elements"], inv)
    assert pick is None
    replies.append(_answer("button:save", 0.9))
    pick, _ = ground.choose_by_jev("click Save", inv["elements"], inv)
    assert pick["label"] == "Save"
    # The same words spread through a sentence describe the control: Jev's call.
    replies.append(_answer("button:save-draft", 0.9))
    pick, _ = ground.choose_by_jev("save it as a draft", inv["elements"], inv)
    assert pick["label"] == "Save draft"
    mic = _inv([_el("AXButton", "Dictate", (900, 500, 30, 30), 1), _el("AXButton", "Send", (950, 500, 30, 30), 2)])
    replies.append(_answer("button:dictate", 0.8))
    pick, _ = ground.choose_by_jev("the microphone to dictate", mic["elements"], mic)
    assert pick["label"] == "Dictate"
    # A word the row gives it is not missing; another row's word is.
    rows = _inv([_el("AXButton", "Install", (250, 130, 50, 20), 1, within="YAML Support"),
                 _el("AXButton", "Install", (250, 200, 50, 20), 2, within="Git History")])
    replies.append(_answer("button:install@yaml-support", 0.9))
    pick, _ = ground.choose_by_jev("Install YAML Support", rows["elements"], rows)
    assert pick["within"] == "YAML Support"
    replies.append(_answer("button:install@git-history", 0.9))
    pick, why = ground.choose_by_jev("Install SQL Formatter", rows["elements"], rows)
    assert pick is None and "SQL" in why
    # A word another listed control has is context Jev weighed ("Size" is the pop-up this menu belongs to).
    menu = _inv([_el("AXPopUpButton", "Size", (200, 300, 90, 22), 1), _el("AXMenuItem", "Large", (200, 330, 90, 22), 2)])
    replies.append(_answer("menu_item:large", 0.9))
    pick, _ = ground.choose_by_jev("Size Large", menu["elements"], menu)
    assert pick["label"] == "Large"


def test_jev_takes_the_size_pop_up():
    table = choose.build([_el("AXPopUpButton", "Size", (200, 300, 90, 22), 1), _el("AXButton", "Apply", (300, 300, 60, 22), 2)],
                         "the Size pop-up", WINDOW)
    choice = choose.ask_jev("the Size pop-up", table, ask=lambda *a, **k: {"pick": _answer("popup:size", 0.9)})
    assert choice.kind == "act" and choice.candidate.element["label"] == "Size"


def test_text_chooser_reads_descriptor_phrases_and_filler_labels():
    pool = [_el("AXPopUpButton", "Size", (200, 300, 90, 22), 1), _el("AXButton", "Apply", (300, 300, 60, 22), 2),
            _el("AXButton", "Item actions", (200, 340, 90, 22), 3), _el("AXStaticText", "Actions", (200, 380, 90, 22), 4)]
    assert ground.choose_by_text("the Size pop-up", pool, WINDOW)[0]["label"] == "Size"
    assert ground.choose_by_text("the Size drop-down", pool, WINDOW)[0]["label"] == "Size"
    assert ground.choose_by_text("Item actions", pool, WINDOW)[0]["label"] == "Item actions"
    assert ground.choose_by_text("Save all", pool, WINDOW)[0] is None
    rows = [_el("AXButton", "Install", (250, 130 + 40 * i, 50, 20), i, within=w)
            for i, w in enumerate(["CSV Colors", "YAML Support", "Git History"])]
    assert ground.choose_by_text("the Install button for YAML Support", rows, WINDOW)[0]["within"] == "YAML Support"
