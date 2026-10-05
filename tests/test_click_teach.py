"""Clicking where the text is, and teaching: screen maths, snapping a pointed click to the named
text, naming clicks in apps that hide their controls, never hit-testing Mint itself, and saved
skills not being used for another app (1 Oct: VS Code, Docker extension)."""
import sys

import pytest

try:
    from mint.core import guard
    from mint.knowledge import skills as skillbook
    from mint.knowledge import teach
    from mint.screen import axkit, ocr, vision
    from mint.tools import harness as harness_tools
    from mint.ui import tutor
except ImportError:
    from mint import axkit, guard, harness_tools, ocr, skillbook, teach, tutor, vision


# --- fakes for Vision's recognised text ---------------------------------------------------------

class _P:
    def __init__(self, x, y):
        self.x, self.y = x, y


class _S:
    def __init__(self, w, h):
        self.width, self.height = w, h


class _R:
    def __init__(self, x, y, w, h):
        self.origin, self.size = _P(x, y), _S(w, h)


class _Obs:
    def __init__(self, box):
        self._box = box

    def boundingBox(self):  # noqa: N802 - Vision's name
        return _R(*self._box)


class _Candidate:
    """Characters evenly spread over the line's normalised box, like a monospaced line."""

    def __init__(self, text, box):
        self.text, self.box = text, box

    def boundingBoxForRange_error_(self, rng, error):  # noqa: N802
        start, length = rng
        x, y, w, h = self.box
        per = w / len(self.text)
        return _Obs((x + start * per, y, length * per, h)), None


def _line(text, x, y, w, h, area):
    """A recognised line at screen points (x, y, w, h) on `area`, with a fake Vision candidate."""
    nx = (x - area["left"]) / area["width"]
    nw = w / area["width"]
    nh = h / area["height"]
    ny = 1 - (y - area["top"]) / area["height"] - nh
    return {"text": text, "x": x, "y": y, "w": w, "h": h, "candidate": _Candidate(text, (nx, ny, nw, nh)),
            "area": area}


MAIN = {"left": 0, "top": 0, "width": 1440, "height": 932}           # MacBook Air, points (2880x1864 px)
SECOND = {"left": 1440, "top": -200, "width": 1920, "height": 1080}  # a display to the right, higher up


# --- screen maths ---------------------------------------------------------------------------------

def test_vision_box_to_screen_points_flips_y_and_ignores_pixels():
    # Vision: normalised, origin bottom-left. A box at the top-left corner of the image:
    assert ocr.to_screen((0.0, 0.9, 0.1, 0.1), MAIN) == pytest.approx((0, 0, 144, 93.2))
    # the middle of the main display
    x, y, w, h = ocr.to_screen((0.45, 0.45, 0.1, 0.1), MAIN)
    assert ocr.center((x, y, w, h)) == pytest.approx((720, 466))
    # a second display: its own origin, negative top, different size; the image's pixel size
    # (2x or 1x) never enters
    x, y, w, h = ocr.to_screen((0.5, 0.25, 0.1, 0.05), SECOND)
    assert (x, y) == pytest.approx((1440 + 960, -200 + 0.70 * 1080))
    assert (w, h) == pytest.approx((192, 54))


def test_vscode_window_capture_maps_to_screen():
    # VS Code's window at (0, 29) 1440x842 points, captured alone (2880x1684 px). OCR of that capture
    # read 'Enable' at normalised (0.3924, 0.7613, 0.0236, 0.0101) - measured 5 Oct; the button is drawn
    # at x 558-610, y 211-232 on screen (capture pixels / 2 + the window origin).
    window = {"left": 0, "top": 29, "width": 1440, "height": 842}
    box = ocr.to_screen((0.3924, 0.7613, 0.0236, 0.0101), window)
    assert ocr.center(box) == pytest.approx((583, 226), abs=1)


def test_target_span_inside_longer_lines():
    assert ocr.target_span("Microsoft microsoft.com 52,882,693", "microsoft.com") == (10, 23)
    assert ocr.target_span("Uninstall v", "Uninstall") == (0, 9)          # the dropdown chevron read as 'v'
    assert ocr.target_span("Uninstall", "Uninstall") is None              # the whole line: use its box
    assert ocr.target_span("You can clone a repository locally.", "Clone Repository") is None
    assert ocr.target_span("v No Folder Opened", "Open Folder") is None
    # OCR losing a letter at the box edge still matches, as the whole line
    assert ocr.target_span("earch Extensions in Marketplace", "Search Extensions in Marketplace") is None


def test_click_point_is_the_target_words_not_the_line():
    line = _line("Microsoft microsoft.com 52,882,693", 557, 156, 274, 18, MAIN)
    (x, y), box = ocr.click_point(line, "microsoft.com")
    whole_centre = (557 + 137, 165)
    assert ocr.contains(box, (x, y)) and ocr.contains((557, 156, 274, 18), (x, y))
    # characters 10-23 of 34 -> x from 557+80.6 to 557+185.4
    assert x == pytest.approx(557 + 274 * 16.5 / 34, abs=0.5) and y == pytest.approx(165)
    assert abs(x - whole_centre[0]) > 3
    # a plain label: its own centre
    label = _line("Enable", 565, 221, 36, 10, MAIN)
    assert ocr.click_point(label, "Enable")[0] == pytest.approx((583, 226))


def test_click_point_on_a_second_display():
    line = _line("Cancel Uninstall", 2440, 300, 160, 20, SECOND)
    (x, y), box = ocr.click_point(line, "Uninstall")
    assert 2440 + 160 * 7 / 16 <= x <= 2600 and y == pytest.approx(310)
    assert ocr.contains(box, (x, y))


def test_click_at_maps_0_1000_through_the_screen_area():
    assert vision.to_point(0, 0, MAIN) == (0, 0)
    assert vision.to_point(1000, 1000, MAIN) == (1440, 932)
    assert vision.to_point(400, 237, MAIN) == pytest.approx((576, 220.884))
    assert vision.to_point(500, 500, SECOND) == pytest.approx((2400, 340))


def test_snap_moves_a_pointed_click_onto_the_named_text():
    # 1 Oct: the model pointed at (576, 221) for 'Uninstall' and clicked 'Disable' five times.
    lines = [_line("Disable", 558, 216, 46, 12, MAIN), _line("Uninstall v", 622, 219, 67, 13, MAIN),
             _line("the Docker extension. If you want, you can uninstall this extension", 400, 270, 600, 15, MAIN)]
    (x, y), found = vision.snap((576, 221), "Uninstall", lines)
    assert found == "Uninstall v" and 622 <= x <= 666 and y == pytest.approx(225.5)
    # already on it: the model's point stays
    assert vision.snap((630, 224), "Uninstall", lines) == ((630, 224), "Uninstall v")
    # the word inside a sentence is not a button
    assert vision.snap((700, 278), "uninstall", [lines[2]]) == ((700, 278), "")
    # too far away: nothing to snap to
    assert vision.snap((100, 600), "Uninstall", lines) == ((100, 600), "")


# --- never asking Mint itself -----------------------------------------------------------------------

def test_hit_test_skips_mints_own_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(axkit, "own_window_at", lambda x, y: True)

    class _AX:
        @staticmethod
        def AXUIElementCopyElementAtPosition(*a):  # noqa: N802
            calls.append(a)
            raise AssertionError("must not hit-test Mint's own window")

        @staticmethod
        def AXUIElementCreateSystemWide():  # noqa: N802
            return object()

    monkeypatch.setitem(sys.modules, "ApplicationServices", _AX)
    assert teach._hit(100, 100) == ("own", None)
    monkeypatch.setattr(axkit, "AX", _AX)
    assert axkit.element_at(100, 100) is None
    assert not calls


def test_focus_secure_never_asks_mint(monkeypatch):
    import os
    assert teach._focus_secure(os.getpid()) is None


def test_recent_click_is_per_app_and_expires(monkeypatch):
    axkit.note_click(4242, 171, 114, "Search Extensions in Marketplace")
    assert axkit.recent_click(4242)["label"] == "Search Extensions in Marketplace"
    assert axkit.recent_click(1111) is None
    assert axkit.recent_click(4242, seconds=-1) is None


# --- teaching in apps that name nothing -------------------------------------------------------------

def test_pick_text_names_the_clicked_words():
    area = {"left": 0, "top": 0, "width": 1440, "height": 932}
    lines = [_line("Install", 560, 215, 40, 12, area), _line("Disable", 470, 215, 44, 12, area)]
    assert teach.pick_text(lines, 575, 221) == "Install"
    assert teach.pick_text(lines, 900, 500) == ""
    long = _line("The Docker extension makes it easy to build, manage and deploy containers", 100, 400, 700, 14, area)
    words = teach.pick_text([long], 100 + 700 * 0.55, 407)
    assert 0 < len(words) <= 40 and words in long["text"]


def test_window_spot():
    assert teach.window_spot(576, 221, (0, 29, 1440, 842)) == "40% across, 23% down the window"


def test_electron_click_is_written_as_click_text():
    events = [
        {"type": "app", "t": 0.0, "app": "Code", "bundle": "com.microsoft.VSCode", "window": "Welcome"},
        {"type": "click", "t": 1.0, "button": "left", "count": 1, "app": "Code", "pid": 7, "role": "AXGroup",
         "kind": "group", "label": "", "near_text": "Search Extensions in Marketplace",
         "at": "12% across, 10% down the window", "x": 171, "y": 114, "mods": []},
        {"type": "char", "t": 2.0, "app": "Code", "pid": 7, "field": "", "field_role": "AXTextArea", "text": "d"},
        {"type": "char", "t": 2.1, "app": "Code", "pid": 7, "field": "", "field_role": "AXTextArea", "text": "o"},
        {"type": "click", "t": 4.0, "button": "left", "count": 1, "app": "Code", "pid": 7, "role": "AXGroup",
         "kind": "group", "label": "", "near_text": "Install", "x": 576, "y": 221, "mods": []},
    ]
    lines = teach.action_lines(teach.compress(events))
    text = "\n".join(lines)
    assert "the text 'Search Extensions in Marketplace' on screen" in text
    assert "typed 'do'" in text and "the text 'Install' on screen" in text
    assert "click_text" in teach._TOOLS_TEXT


# --- saved skills: the right app, or none ----------------------------------------------------------

def _skill(root, category, name, title, when, apps):
    path = root / category / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\nwhen: {when}\napps: {apps}\nuses: 2\nwins: 2\nfails: 0\n---\n"
                    "## Steps\n1. Do it.\n")


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(skillbook, "ROOT", tmp_path)
    monkeypatch.setattr(skillbook, "_seed", lambda: None)
    _skill(tmp_path, "apps/claude", "manage-tasks-in-data-labeling-portal", "Manage tasks in Data Labeling Portal",
           "when I need to manage tasks for POV Labs or the Minutes Chrome extension", "Claude Code")
    _skill(tmp_path, "apps/slack", "open-a-channel", "Open a Slack channel or DM", "open a channel", "Slack")
    _skill(tmp_path, "general", "close-all-apps", "Close all apps except browser", "close apps", "")
    return tmp_path


def test_app_names_match_whole_not_substring(library):
    assert skillbook.app_key("Code") == skillbook.app_key("VS Code") == "visual studio code"
    assert [s["name"] for s in skillbook.for_app("Code")] == []           # not 'Claude Code'
    assert [s["name"] for s in skillbook.for_app("Claude")] == ["manage-tasks-in-data-labeling-portal"]
    _skill(library, "apps/visual-studio-code", "install-an-extension", "Install an extension", "install one",
           "Visual Studio Code")
    assert [s["name"] for s in skillbook.for_app("Code")] == ["install-an-extension"]


def test_tutor_and_find_reject_another_apps_skill(library, monkeypatch):
    task = "download Docker extension in VS Code"
    assert tutor._skill_text(task, "Code") == ("", "")
    portal = next(s for s in skillbook.all_skills() if s["name"].startswith("manage-tasks"))
    assert skillbook.app_conflict(portal, task, "Code")
    assert not skillbook.app_conflict(portal, "manage POV Labs tasks in Claude Code", "Google Chrome")
    assert not skillbook.app_conflict(portal, "manage my labeling tasks", "Google Chrome", use_front=False)

    class _Pick:
        def __init__(self, id, confidence):
            self.id, self.confidence, self.probability = id, confidence, confidence

        @property
        def sure(self):
            return True

    def choose(question, options, **kw):
        number = next(k for k, v in options.items() if v.startswith("Manage tasks"))
        return _Pick(number, 0.71)

    monkeypatch.setattr(skillbook.jev, "choose", choose)
    skill, why = skillbook.find(task, "Code")
    assert skill is None and "for claude" in why
    # app words don't count as shared words: 'code' and 'extension' alone chose it on 1 Oct
    assert skillbook._lexical(task, skillbook.for_app("Claude")) == (None, "")


# --- guard and risky words: no false positives on harmless controls -------------------------------

def test_guard_labels():
    assert guard.assess("click_at", {"x": 1, "y": 2, "target": "Uninstall"}).kind == "delete"
    assert guard.assess("ui_act", {"action": "click", "target": "Uninstall"}).kind == "delete"
    for harmless in ("Format Document", "Reset Zoom", "Reset Layout", "Sync Changes", "Push", "Install"):
        assert guard.assess("ui_act", {"action": "click", "target": harmless}) is None, harmless
    assert guard.assess("ui_act", {"action": "click", "target": "Format Disk"}).kind == "delete"
    assert guard.assess("ui_act", {"action": "click", "target": "Discard Changes"}).kind == "delete"


def test_uninstall_counts_as_asked_by_delete():
    assert harness_tools._risky("Uninstall", "install an extension and then delete it") == ""
    assert harness_tools._risky("Uninstall", "install the docker extension") == "uninstall"


# --- the real recogniser, when there is one -------------------------------------------------------

def test_real_ocr_on_a_retina_second_display():
    pytest.importorskip("Vision")
    PIL = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
    except OSError:
        pytest.skip("no system font")
    image = PIL.new("RGB", (3840, 2160), "white")                 # SECOND at 2x
    draw = ImageDraw.Draw(image)
    draw.text((2000, 1000), "Cancel   Uninstall", fill="black", font=font)
    lines = ocr.recognize(image, SECOND)
    line = next(item for item in lines if "Uninstall" in item["text"])
    (x, y), box = ocr.click_point(line, "Uninstall")
    left = 2000 + draw.textlength("Cancel   ", font=font)
    expected = (1440 + (left + draw.textlength("Uninstall", font=font) / 2) / 2, -200 + 1022 / 2)
    assert x == pytest.approx(expected[0], abs=4) and y == pytest.approx(expected[1], abs=4)
    assert ocr.contains(box, (x, y))
