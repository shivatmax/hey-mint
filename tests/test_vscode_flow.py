"""The VS Code extension run (5 Oct): hidden search inputs, Install buttons told apart by their row, a useful
ui_elements list, searches typed again, and no skill learned from a run the user stopped."""
import pytest

try:
    from mint.screen import ground
except ImportError:
    from mint import ground
try:
    from mint.knowledge import learner
except ImportError:
    from mint import learner


def _el(role, label, box, interactive=None, **kw):
    e = {"role": role, "label": label, "box": box, "kind": ground.ROLE_WORDS.get(role, "x"), "value": "",
         "hint": "", "enabled": True, "focused": False, "in_dialog": False, "dialog": None, "ref": None,
         "depth": 3, "interactive": role in ground.INTERACTIVE if interactive is None else interactive}
    e.update(kw)
    return e


def test_hidden_input_is_named_from_its_placeholder():
    placeholder = _el("AXStaticText", "Search Extensions in Marketplace", (60, 100, 220, 20))
    far = _el("AXStaticText", "Installed", (60, 400, 80, 18))
    hidden = [_el("AXTextArea", "", (64, 108, 1, 1), hidden=True)]
    ground._name_hidden(hidden, [placeholder, far])
    assert hidden[0]["label"] == "Search Extensions in Marketplace"
    assert hidden[0]["box"] == placeholder["box"]                 # the click goes where a person clicks


def test_install_button_is_told_apart_by_its_row():
    rows = [_el("AXGroup", "Rainbow CSV, 3.1.0, mechatroner", (10, 100, 300, 60)),
            _el("AXGroup", "CSV Colorful Table, 0.0.3, publisher", (10, 170, 300, 60))]
    installs = [_el("AXButton", "Install", (250, 130, 50, 20)), _el("AXButton", "Install", (250, 200, 50, 20))]
    elements = rows + installs
    ground._containers(elements)
    assert installs[0]["within"].startswith("Rainbow CSV") and installs[1]["within"].startswith("CSV Colorful")
    pick, why = ground.choose_by_text("Install CSV Colorful Table", elements)
    assert pick is installs[1], why
    pick, _ = ground.choose_by_text("Install", elements)
    assert pick is None                                          # two of them: ambiguous, not a guess
    assert "in 'CSV Colorful Table" in ground.describe({**installs[1], "id": 7, "in_dialog": False}, (0, 0, 800, 600))


def test_summary_lists_named_things_first(monkeypatch):
    junk = [_el("AXButton", "", (5, 5, 20, 20)) for _ in range(80)] + \
        [_el("AXStaticText", "", (5, 30, 20, 20), interactive=False) for _ in range(40)]
    row = _el("AXGroup", "CSV Colorful Table, 0.0.3, Display CSV files as a colorful table", (10, 600, 300, 60))
    text = _el("AXStaticText", "CSV Colorful Table", (20, 605, 120, 18), interactive=False)
    button = _el("AXButton", "Install", (250, 630, 50, 20), within="CSV Colorful Table, 0.0.3")
    elements = junk + [row, text, button]
    for i, e in enumerate(elements, 1):
        e["id"] = i
    inv = {"app": "Code", "title": "Extensions", "window": (0, 0, 800, 700), "dialog": None, "elements": elements}
    monkeypatch.setattr(ground, "inventory", lambda app=None: inv)
    out = ground.summary()
    assert "button 'Install' in 'CSV Colorful Table" in out
    assert "group 'CSV Colorful Table, 0.0.3" in out
    assert "text 'CSV Colorful Table'" not in out                # the row's own text, not repeated
    assert "button (" not in out                                 # unnamed buttons are not listed


def test_learner_skips_stopped_or_unfinished_runs():
    L = learner.Learner() if hasattr(learner, "Learner") else learner.learner.__class__()
    steps = [{"role": "tool", "text": f"ui_act(click) -> Clicked thing {i}"} for i in range(25)]
    plan = [{"role": "tool", "text": "plan_task(x) -> Task started with 4 steps."}]
    stopped = [{"role": "user", "text": "install the colorful csv extension"}] + plan + steps + \
        [{"role": "user", "text": "Stop."}]
    assert L.worth_learning(stopped) == ""
    unfinished = [{"role": "user", "text": "install the colorful csv extension"}] + plan + steps
    assert L.worth_learning(unfinished) == ""
    finished = unfinished + [{"role": "tool", "text": "step_done(4) -> All steps finished. Tell the user."}]
    assert L.worth_learning(finished) != ""


@pytest.mark.parametrize("name,bundle,dedupe", [
    ("Code", "com.microsoft.VSCode", False), ("Finder", "com.apple.finder", False),
    ("Slack", "com.tinyspeck.slackmacgap", True), ("ChatGPT", "com.openai.codex", True),
    ("Google Chrome", "com.google.Chrome", True), ("Messages", "com.apple.MobileSMS", True),
    ("Terminal", "com.apple.Terminal", False)])
def test_duplicate_send_guard_only_for_messaging_apps(name, bundle, dedupe):
    try:
        from mint.app import session
    except ImportError:
        from mint import session
    assert session._is_messaging(name, bundle) is dedupe


class _App:
    def __init__(self, name, path):
        self.name, self.path = name, path

    def localizedName(self):
        return self.name

    def bundleURL(self):
        path = self.path

        class URL:
            def lastPathComponent(self):
                return path.rsplit("/", 1)[-1]
        return URL()


@pytest.mark.parametrize("asked,app,same", [
    ("Visual Studio Code", _App("Code", "/Applications/Visual Studio Code.app"), True),
    ("VS Code", _App("Code", "/Applications/Visual Studio Code.app"), True),
    ("Code", _App("Code", "/Applications/Visual Studio Code.app"), True),
    ("Chrome", _App("Google Chrome", "/Applications/Google Chrome.app"), True),
    ("Claude", _App("Claude", "/Applications/Claude.app"), True),
    ("Claude Code", _App("Claude", "/Applications/Claude.app"), False),
    ("Photos", _App("Photoshop", "/Applications/Adobe Photoshop.app"), False)])
def test_app_names(asked, app, same):
    assert ground.same_app(asked, app) is same


def test_terminal_is_read_from_its_newest_lines():
    """5 Oct: Terminal's 1.1 MB scrollback was read from the top, so the output of the command Mint had just run
    ("Cycle Count: 112") never reached it."""
    try:
        from mint.tools import documents
    except ImportError:
        try:
            from mint.office import documents
        except ImportError:
            from mint import documents
    text = "Last login: Mon Oct 5\n" + "old line\n" * 100000 + \
        "user@mac ~ % system_profiler SPPowerDataType | grep -A2 Health\n      Health Information:\n" \
        "          Cycle Count: 112\n\n\n          Condition: Normal\nuser@mac ~ % "
    tail = documents.terminal_tail(text, 300)
    assert tail.endswith("user@mac ~ %") and "Cycle Count: 112" in tail and "Condition: Normal" in tail
    assert "Last login" not in tail and "\n\n\n" not in tail and len(tail) <= 300
