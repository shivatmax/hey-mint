"""Every connector the user makes is checked for real before it counts (connector_maker.py): a read-only action is
run live, or - for one that only opens links, web pages or hands files over - each link scheme must open that app
and the website must answer. And the planner plans for exactly the app asked for (the Telegram app, not the bot)."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mint's tools import macOS frameworks")

try:
    from mint.tools import connector_maker, connectors
except ImportError:
    from mint import connector_maker, connectors

APP = {"bundle_id": "com.linear", "path": "/Applications/Linear.app", "name": "Linear", "file_name": "Linear",
       "schemes": ["linear"]}


def _url(aid, template, changes=False):
    return {"id": aid, "title": aid.replace("_", " "), "kind": "url", "template": template, "params": [],
            "changes": changes, "risk": []}


def _item(**extra):
    item = {"id": "linear", "name": "Linear", "bundle_id": "com.linear", "kind": "url", "test": "",
            "actions": [_url("my_issues", "linear://my-issues"), _url("new_issue", "linear://new", True)]}
    item.update(extra)
    return item


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(connectors, "app_for", lambda bundles: APP if "com.linear" in bundles else None)


def test_link_scheme_that_opens_the_app_passes(installed):
    ok, words = connector_maker.check_links(_item(), owner=lambda s: "/Applications/Linear.app")
    assert ok and words == "linear: links open Linear"


def test_link_scheme_nobody_opens_fails(installed):
    ok, words = connector_maker.check_links(_item(), owner=lambda s: "")
    assert not ok and "nothing on this Mac opens linear: links" in words


def test_link_scheme_owned_by_another_app_fails(installed):
    ok, words = connector_maker.check_links(_item(), owner=lambda s: "/Applications/Other.app")
    assert not ok and "open Other, not Linear" in words


def test_app_gone_fails(monkeypatch):
    monkeypatch.setattr(connectors, "app_for", lambda bundles: None)
    ok, words = connector_maker.check_links(_item(), owner=lambda s: "x")
    assert not ok and "isn't on this Mac" in words


def test_web_connector_checks_the_site():
    item = {"id": "notion", "name": "Notion", "bundle_id": "", "kind": "browser", "domain": "notion.so",
            "site": "https://www.notion.so/", "actions": [dict(_url("search", "https://www.notion.so/search"),
                                                            kind="web")]}
    asked = []
    ok, words = connector_maker.check_links(item, answers=lambda url: asked.append(url) or (True, "200"))
    assert ok and words == "www.notion.so answers" and asked == ["https://www.notion.so/"]
    ok, words = connector_maker.check_links(item, answers=lambda url: (False, "timed out"))
    assert not ok and "didn't answer (timed out)" in words


def test_test_runs_a_check_for_link_connectors_and_records_it(installed, monkeypatch):
    monkeypatch.setattr(connector_maker, "_scheme_owner", lambda s: "/Applications/Linear.app")
    monkeypatch.setattr(connector_maker, "check_links",
                        lambda item: (True, "linear: links open Linear"))
    item = _item()
    ok, words = connector_maker.test(item, save=False)
    assert ok and words == "linear: links open Linear"
    assert item["tested"]["ok"] and item["tested"]["action"] == "links"


def test_test_runs_a_read_only_action_even_when_the_plan_named_none(monkeypatch):
    ran = []
    monkeypatch.setattr(connectors, "osascript", lambda script, timeout=30: ran.append(script) or (True, ""))
    read = {"id": "list_docs", "title": "List documents", "kind": "applescript", "params": [], "changes": False,
            "risk": [], "template": 'tell application "TextEdit" to get name of every document'}
    item = {"id": "textedit", "name": "TextEdit", "test": "", "actions": [read]}
    ok, words = connector_maker.test(item, save=False)
    assert ok and ran and item["tested"]["action"] == "list_docs"
    assert words.startswith("ran, nothing to report")


def test_plan_for_a_bundle_is_that_app_not_the_built_in_bot(monkeypatch):
    """"integrate Telegram" alone is the Telegram bot (a built-in account); with the app's bundle id it is the app."""
    telegram = {"bundle_id": "ru.keepcoder.Telegram", "path": "/Applications/Telegram.app", "name": "Telegram",
                "file_name": "Telegram", "schemes": ["tg"]}
    monkeypatch.setattr(connectors, "app_for", lambda bundles: telegram)
    monkeypatch.setattr(connectors, "scripting_definition", lambda path: "")
    monkeypatch.setattr(connector_maker, "_ask_planner", lambda prompt: {"refuse": "planned"})
    assert connector_maker.plan("integrate Telegram").get("kind") == "builtin"
    result = connector_maker.plan("integrate Telegram", bundle_id="ru.keepcoder.Telegram")
    assert result["kind"] == "app" and result["bundle_id"] == "ru.keepcoder.Telegram"
    assert result["refuse"] == "planned"


def test_plan_says_how_it_will_be_checked():
    words = connector_maker.plan_text({"id": "p", "name": "Linear", "app": "Linear", "kind": "app", "how": "url",
                                       "summary": "Issues.", "actions": [_url("my_issues", "linear://x")],
                                       "test": ""})
    assert "After saving, Mint checks that its links open Linear." in words


def test_the_session_knows_the_users_connectors(monkeypatch):
    monkeypatch.setattr(connectors, "custom_connectors", lambda: [_item()])
    words = connector_maker.prompt_addendum()
    assert "Linear [linear]: my_issues, new_issue" in words


# --- built-in connectors: every one has a check now ---------------------------------------------------------

def _builtin(cid, state="connected", detail="Ready"):
    c = connectors.get(cid)
    return c, (lambda deep=False: {"state": state, "detail": detail})


def test_every_built_in_connector_has_a_test():
    assert all(c.testable for c in connectors.LIBRARY)


def test_basics_for_an_installed_app_with_links(monkeypatch):
    c, status = _builtin("slack")
    app = {"bundle_id": "com.tinyspeck.slackmacgap", "path": "/Applications/Slack.app", "name": "Slack",
           "schemes": ["slack"]}
    monkeypatch.setattr(type(c), "status", lambda self, deep=False: status())
    monkeypatch.setattr(type(c), "app", lambda self: app)
    words = connectors.check_basics(c, owner=lambda s: "/Applications/Slack.app")
    assert words == "Checked: Slack is installed, its slack: links open it (Connected - Ready)."
    assert connectors.check_basics(c, owner=lambda s: "").startswith("Didn't work: its slack: links don't open")


def test_basics_for_a_scriptable_app_asks_macos_without_prompting(monkeypatch):
    c, status = _builtin("iwork")
    app = {"bundle_id": "com.apple.iWork.Pages", "path": "/Applications/Pages.app", "name": "Pages", "schemes": []}
    monkeypatch.setattr(type(c), "status", lambda self, deep=False: status())
    monkeypatch.setattr(type(c), "app", lambda self: app)
    assert "macOS lets Mint control it" in connectors.check_basics(c, control=lambda b: connectors.AE_OK)
    assert "Automation off" in connectors.check_basics(c, control=lambda b: connectors.AE_DENIED)


def test_basics_say_what_is_missing(monkeypatch):
    c, status = _builtin("telegram", "setup", "Add a bot token")
    monkeypatch.setattr(type(c), "status", lambda self, deep=False: status())
    assert connectors.check_basics(c) == "Not ready yet: Add a bot token."
    c, _ = _builtin("notion")
    monkeypatch.setattr(type(c), "app", lambda self: None)
    assert connectors.check_basics(c) == "Didn't work: Notion isn't installed on this Mac."
