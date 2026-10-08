"""The app library (app_library.py): the catalog, the groups Settings shows (Connected, On your Mac, Suggested,
Accounts, More on your Mac), search, and what Connect / Get it / Use in browser do - all with a fake Mac."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mint's tools import macOS frameworks")

try:
    from mint.tools import app_library, connectors
except ImportError:
    from mint import app_library, connectors

GROUPS = ("connected", "on_mac", "get", "browser", "accounts", "more_on_mac")


@pytest.fixture(autouse=True)
def _private(tmp_path, monkeypatch):
    """Nothing is written to the real Mint folder, nothing opens."""
    monkeypatch.setattr(app_library, "MINE_PATH", tmp_path / "connected-apps.json")
    opened = []
    monkeypatch.setattr(connectors, "open_url", opened.append)
    monkeypatch.setattr(app_library, "invalidate", lambda: None)      # the fake Mac stays the Mac
    app_library._cache.update(rows=None, at=0.0, fresh=False)
    yield opened
    app_library._cache.update(rows=None, at=0.0, fresh=False)


def _fake(installed=(), states=None, mine=None, customs=(), extras=()):
    """Rows for a fake Mac, and make them what all_rows() answers."""
    rows = app_library.build({b: f"/Applications/{b}.app" for b in installed}, states or {}, mine or {},
                             list(customs), list(extras))
    app_library._cache.update(rows=rows, at=9e12, fresh=False)
    return rows


# --- the catalog ------------------------------------------------------------------------------------------

def test_every_entry_is_complete():
    for app in app_library.CATALOG:
        assert app.id and app.name and app.what, app
        assert app.category in app_library.CATEGORIES, app
        assert app.how in app_library.HOW_WORDS, app
        assert len(app.what) <= 90 and app.what.endswith("."), app
        if app.kind == "app" and not all(b.startswith("com.apple.") for b in app.bundles):
            assert app.get.startswith(("https://", "macappstore://")), f"{app.name} has nowhere to get it"
        if app.web:
            assert app.web.startswith("https://"), app
        if app.kind == "service":
            assert app.connector in connectors.BY_ID, app


def test_no_duplicates_in_the_catalog():
    ids = [a.id for a in app_library.CATALOG]
    assert len(ids) == len(set(ids))
    bundles = [b for a in app_library.CATALOG for b in a.bundles]
    assert len(bundles) == len(set(bundles))
    ranks = [a.common for a in app_library.CATALOG if a.common]
    assert len(ranks) == len(set(ranks)), "two apps share a popularity rank"


def test_connectors_named_by_the_catalog_exist():
    for app in app_library.CATALOG:
        if app.connector:
            assert app.connector in connectors.BY_ID, app


# --- the groups -------------------------------------------------------------------------------------------

def test_each_app_is_in_exactly_one_group():
    g = app_library.groups(installed={"notion.id", "com.apple.mail"}, states={"mail": {"state": "connected"}})
    seen = [r["id"] for k in GROUPS for r in g[k]]
    assert len(seen) == len(set(seen))
    assert set(seen) == {a.id for a in app_library.CATALOG}


def test_suggestions_are_capped_popular_and_not_installed():
    g = app_library.groups(installed={"notion.id", "com.tinyspeck.slackmacgap"})
    names = [r["name"] for r in g["suggested"]]
    assert 0 < len(names) <= app_library.MAX_SUGGESTED
    assert "Notion" not in names and "Slack" not in names
    ranks = [r["common"] for r in g["suggested"]]
    assert ranks == sorted(ranks) and all(ranks)
    assert all((r["state"], r["button"]) in (("get", "Get it"), ("web", "Use in browser")) for r in g["suggested"])
    assert app_library.groups(installed=set(), max_suggested=3)["counts"]["suggested"] == 3
    assert app_library.groups(installed=set())["suggested"][0]["name"] == "Notion"


def test_no_app_without_real_support():
    """Every entry works through something real: a built-in connector with tools, a connector planned from the
    app's scripting or links and tested, the web app (a planned, checked web connector), or an account."""
    for app in app_library.CATALOG:
        assert app.how in ("connector", "scripting", "links", "browser", "account"), app.id
        if app.how == "browser":
            assert app.web.startswith("https://"), app.id
    gone = {"signal", "bear", "things", "omnifocus", "fantastical", "libreoffice", "photoshop", "firefox", "cursor",
            "gemini"}
    assert not gone & {a.id for a in app_library.CATALOG}


def test_web_apps_are_offered_in_the_browser_not_as_a_download():
    g = app_library.groups(installed=set())
    linear = next(r for r in g["suggested"] + g["browser"] if r["id"] == "linear")
    assert linear["state"] == "web" and linear["button"] == "Use in browser"
    installed = app_library.groups(installed={"com.linear": "/Applications/Linear.app"})
    row = next(r for r in installed["on_mac"] if r["id"] == "linear")
    assert row["button"] == "Connect" and row["how_words"] == "Works in your browser"


def test_suggestions_say_when_they_work_in_the_browser():
    g = app_library.groups(installed=set())
    notion = next(r for r in g["suggested"] if r["id"] == "notion")
    assert notion["state"] == "web"
    zoom = next(r for r in g["suggested"] if r["id"] == "zoom")
    assert notion["browser_ok"] and notion["web_url"].startswith("https://")
    assert not zoom["browser_ok"]


def test_installed_apps_need_connect_until_connected():
    g = app_library.groups(installed={"com.figma.Desktop": "/Applications/com.figma.Desktop.app",
                                      "com.apple.iCal": "", "com.spotify.client": ""},
                           states={"calendar": {"state": "setup", "detail": "Allow Calendars once"},
                                   "spotify": {"state": "connected", "detail": "Ready"}})
    on_mac = {r["id"]: r for r in g["on_mac"]}
    assert on_mac["figma"]["button"] == "Connect" and on_mac["figma"]["installed"]
    assert on_mac["calendar"]["detail"] == "Allow Calendars once"
    assert [r["id"] for r in g["connected"]] == ["spotify"]
    assert on_mac["figma"]["path"] == "/Applications/com.figma.Desktop.app"


def test_not_installed_is_never_connected_even_if_the_connector_says_so():
    """Slack's connector calls itself connected in the browser; without the app it is offered, not listed."""
    g = app_library.groups(installed=set(), states={"slack": {"state": "connected", "detail": "In the browser"}})
    assert "slack" not in [r["id"] for r in g["connected"]]
    assert "slack" in [r["id"] for r in g["suggested"]]


def test_connected_through_mine_or_a_custom_connector():
    g = app_library.groups(installed={"com.hnc.Discord", "com.linear"}, mine={"discord": {"how": "screen"},
                                                                                 "trello": {"how": "browser"}},
                           customs=[{"id": "linear", "bundle_id": "com.linear", "actions": [{}, {}, {}]}])
    connected = {r["id"]: r for r in g["connected"]}
    assert set(connected) >= {"discord", "linear", "trello"}
    assert connected["linear"]["detail"] == "Your connector · 3 actions"
    assert connected["trello"]["detail"] == "In your browser"


def test_aggregate_connector_details_read_as_ready():
    g = app_library.groups(installed={"com.google.Chrome"},
                           states={"chrome": {"state": "connected", "detail": "Chrome + Brave"},
                                   "finder": {"state": "connected", "detail": "Finder"}})
    chrome = next(r for r in g["connected"] if r["id"] == "chrome")
    assert chrome["detail"] == "Ready"


def test_accounts_need_setting_up():
    g = app_library.groups(installed=set(), states={"google": {"state": "connected", "detail": "1 Google account"}})
    assert "google" in [r["id"] for r in g["connected"]]
    assert {"icloud", "microsoft", "telegram"} <= {r["id"] for r in g["accounts"]}
    assert all(r["button"] == "Set up" for r in g["accounts"])


def test_more_on_your_mac_skips_known_and_private_apps():
    extras = [{"bundle_id": "com.aone.keka", "name": "Keka", "path": "/Applications/Keka.app"},
              {"bundle_id": "com.aone.keka", "name": "Keka", "path": "/Applications/Keka.app"},
              {"bundle_id": "com.1password.1password", "name": "1Password", "path": "/x"},
              {"bundle_id": "com.figma.Desktop", "name": "Figma", "path": "/x"}]
    g = app_library.groups(installed=set(), extras=extras)
    assert [r["name"] for r in g["more_on_mac"]] == ["Keka"]
    assert g["more_on_mac"][0]["category"] == app_library.MORE and g["more_on_mac"][0]["extra"]
    assert not g["on_mac"]


# --- search -----------------------------------------------------------------------------------------------

def test_search():
    rows = app_library.build({}, {}, {}, [])
    assert app_library.search("notion", rows)[0]["name"] == "Notion"
    assert app_library.search("Teams", rows)[0]["id"] == "teams"
    assert app_library.search("vs code", rows)[0]["id"] == "vscode"
    micro = [r["name"] for r in app_library.search("micro", rows)]
    assert micro and all(n.startswith("Microsoft") for n in micro)
    assert app_library.search("zzzz nothing", rows) == []
    assert len(app_library.search("", rows)) == len(rows)
    ids = [r["id"] for r in app_library.search("chat", rows)]
    assert len(ids) == len(set(ids))


# --- the buttons ------------------------------------------------------------------------------------------

def test_get_it_opens_where_to_get_it(_private):
    _fake()
    words = app_library.connect("zoom")
    assert _private == [next(a.get for a in app_library.CATALOG if a.id == "zoom")]
    assert "Zoom" in words


def _web_plan(name="Trello"):
    return {"id": "w1", "name": name, "kind": "web", "summary": "Boards.",
            "actions": [{"title": "Open my boards", "risk": [], "changes": False}]}


def test_use_in_browser_makes_a_checked_web_connector(_private, monkeypatch):
    try:
        from mint.tools import connector_maker
    except ImportError:
        from mint import connector_maker
    _fake()
    asked = []
    monkeypatch.setattr(connector_maker, "plan", lambda words, bundle_id="", web=False: asked.append((words, web))
                        or _web_plan())
    monkeypatch.setattr(connector_maker, "create",
                        lambda pid: "DONE: saved the Trello connector. Checked: all 1 pages on trello.com are there.")
    assert app_library.use_in_browser("trello", confirm=lambda plan: False) == "Not connected."
    assert _private == []                                 # nothing opens before the user says yes
    words = app_library.use_in_browser("trello", confirm=lambda plan: True)
    assert asked == [("integrate Trello", True)] * 2
    assert words.startswith("Connected Trello in your browser: 1 things") and "trello.com are there" in words
    assert _private == ["https://trello.com/"]


def test_use_in_browser_that_cannot_be_planned_is_not_connected(_private, monkeypatch):
    try:
        from mint.tools import connector_maker
    except ImportError:
        from mint import connector_maker
    _fake()
    monkeypatch.setattr(connector_maker, "plan", lambda words, bundle_id="", web=False: {"error": "no site"})
    assert app_library.use_in_browser("trello") == "Couldn't connect Trello: no site"
    assert _private == [] and app_library.load_mine() == {}


def test_web_connectors_belong_to_their_own_entry():
    """Word, Excel and PowerPoint share office.com: a connector made for Excel connects Excel only."""
    excel = {"id": "microsoft_excel", "name": "Microsoft Excel", "domain": "office.com", "actions": [{}]}
    rows = _fake(customs=[excel])
    state = {r["id"]: r["state"] for r in rows}
    assert state["excel"] == "connected" and state["word"] == "web" and state["powerpoint"] == "web"


def _refuse(monkeypatch):
    try:
        from mint.tools import connector_maker
    except ImportError:
        from mint import connector_maker
    monkeypatch.setattr(connector_maker, "plan", lambda words, bundle_id="", web=False: {"refuse": "no links"})


def test_connect_an_app_mint_uses_through_its_window(_private, monkeypatch):
    """An app with nothing to script or link to: connected only after the real check - Mint read the app's menus
    through Accessibility."""
    _fake(installed={"com.figma.Desktop"})
    _refuse(monkeypatch)
    checked = []
    monkeypatch.setattr(app_library, "check_window",
                        lambda row: checked.append(row["bundle_id"]) or (True, "Mint can read and use its menus (File)"))
    words = app_library.connect("figma")
    assert checked == ["com.figma.Desktop"]
    assert words.startswith("Connected Figma: Mint can read and use its menus (File)")
    mine = app_library.load_mine()["figma"]
    assert mine["how"] == "screen" and "File" in mine["checked"]


def test_window_app_is_not_connected_without_accessibility(_private, monkeypatch):
    try:
        from mint.core import permissions
    except ImportError:
        from mint import permissions
    _fake(installed={"com.figma.Desktop"})
    _refuse(monkeypatch)
    panes = []
    monkeypatch.setattr(permissions, "open_pane", panes.append)
    monkeypatch.setattr(app_library, "check_window", lambda row: (False, "accessibility"))
    words = app_library.connect("figma")
    assert words.startswith("Not connected yet") and "Accessibility" in words
    assert panes == ["accessibility"] and app_library.load_mine() == {}


def test_window_app_that_hides_its_controls_is_not_connected(_private, monkeypatch):
    _fake(installed={"com.figma.Desktop"})
    _refuse(monkeypatch)
    monkeypatch.setattr(app_library, "check_window", lambda row: (False, "Figma doesn't show Mint its menus"))
    assert app_library.connect("figma").startswith("Not connected: Figma doesn't show")
    assert app_library.load_mine() == {}


def test_check_window_reads_the_menus():
    row = {"name": "Figma", "bundle_id": "com.figma.Desktop"}
    yes = {"accessibility": "allowed", "screen": "allowed"}.get
    ok, words = app_library.check_window(row, menus=lambda b: ["Figma", "File", "Edit", "View", "Help"], allowed=yes)
    assert ok and words == "Mint can read and use its menus (Figma, File, Edit, View…)"
    ok, words = app_library.check_window(row, menus=lambda b: ["File"], allowed={"accessibility": "allowed"}.get)
    assert ok and "Screen Recording" in words
    assert app_library.check_window(row, menus=lambda b: [], allowed=yes)[0] is False
    ok, words = app_library.check_window(row, menus=lambda b: None, allowed=yes)
    assert not ok and "didn't start" in words
    assert app_library.check_window(row, menus=lambda b: ["File"], allowed=lambda k: "ask") == (False, "accessibility")


def test_an_app_with_no_links_falls_back_to_its_window(_private, monkeypatch):
    """The Telegram app when the planner finds no links it can use: not connected unless the window check passes."""
    _fake(installed={"ru.keepcoder.Telegram"})
    _refuse(monkeypatch)
    monkeypatch.setattr(app_library, "check_window", lambda row: (False, "Telegram didn't start"))
    assert app_library.connect("telegram_app") == "Not connected: Telegram didn't start"
    assert app_library.load_mine() == {}
    monkeypatch.setattr(app_library, "check_window", lambda row: (True, "Mint can read and use its menus (File)"))
    assert app_library.connect("telegram_app").startswith("Connected Telegram")


def test_connect_plans_for_exactly_that_app(_private, monkeypatch):
    """The Telegram app is planned as the app (its bundle id), never as the Telegram bot remote control."""
    try:
        from mint.tools import connector_maker
    except ImportError:
        from mint import connector_maker
    _fake(installed={"ru.keepcoder.Telegram"})
    seen = []
    monkeypatch.setattr(connector_maker, "plan", lambda words, bundle_id="", web=False:
                        seen.append((words, bundle_id)) or {"refuse": "x"})
    monkeypatch.setattr(app_library, "check_window", lambda row: (False, "x"))
    app_library.connect("telegram_app")
    assert seen == [("integrate Telegram", "ru.keepcoder.Telegram")]


def test_prompt_names_connected_apps(_private):
    assert app_library.prompt_text() == ""
    _fake(installed={"com.figma.Desktop"})
    app_library._remember(app_library.get("figma"), "screen", checked="menus")
    app_library._remember(app_library.get("trello"), "browser")
    words = app_library.prompt_text()
    assert "through their window" in words and "Figma" in words
    assert "Trello (https://trello.com/)" in words


def test_connect_plans_a_connector_and_asks_first(monkeypatch):
    try:
        from mint.tools import connector_maker
    except ImportError:
        from mint import connector_maker
    _fake(installed={"com.linear"})
    plan = {"id": "p1", "name": "Linear", "app": "Linear", "summary": "Open issues.",
            "actions": [{"title": "Open my issues", "risk": [], "changes": False},
                        {"title": "New issue", "risk": [], "changes": True}]}
    monkeypatch.setattr(connector_maker, "plan", lambda words, bundle_id="", web=False: plan)
    created = []
    monkeypatch.setattr(connector_maker, "create", lambda pid: created.append(pid) or "DONE: saved")
    asked = []
    assert app_library.connect("linear", confirm=lambda p: asked.append(p) or False) == "Not connected."
    assert asked == [plan] and created == []
    assert app_library.connect("linear", confirm=lambda p: True).startswith("Connected Linear")
    assert created == ["p1"]
    title, body = app_library.plan_summary(plan)
    assert title == "Connect Linear?" and "• New issue (changes things)" in body and "• Open my issues\n" in body


def test_groups_from_this_mac_are_fast_when_warm():
    import time
    app_library.groups()                       # cold: reads this Mac
    started = time.monotonic()
    for _ in range(5):
        app_library.groups()
    assert (time.monotonic() - started) / 5 < 0.3
