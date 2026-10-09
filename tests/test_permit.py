"""Permissions asked for the moment a request needs them (permit.py), and Telegram's search box found by its words."""
import time

try:
    from mint.core import permit
    from mint.screen import ground
except ImportError:
    from mint import ground, permit


def _fresh(monkeypatch, status="denied"):
    asks = []
    monkeypatch.setattr(permit, "_times", {})
    monkeypatch.setattr(permit, "_watching", set())
    monkeypatch.setattr(permit, "_asked", set())
    monkeypatch.setattr(permit.permissions, "status", lambda kind, asked=(): status)
    monkeypatch.setattr(permit.permissions, "ask", lambda kind, asked, done=None: asks.append(kind))
    monkeypatch.setattr(permit.permissions, "open_pane", lambda kind: asks.append("pane:" + kind))
    return asks


def test_clicking_and_typing_need_accessibility(monkeypatch):
    _fresh(monkeypatch, "denied")
    assert permit.missing("ui_act", {"action": "type"}) == "accessibility"
    assert permit.missing("click_text", {}) == "accessibility"
    assert permit.missing("open_app", {}) is None and permit.missing("look", {}) is None
    _fresh(monkeypatch, "allowed")
    assert permit.missing("ui_act", {}) is None


def test_a_tool_that_ran_into_a_missing_permission_is_caught():
    assert permit.from_result("Cannot click: Mint lacks Accessibility permission.") == "accessibility"
    assert permit.from_result("Cannot see the screen: Mint lacks Screen Recording permission. Do not") == "screen"
    assert permit.from_result("Cannot scroll without Accessibility permission. Tell the user") == "accessibility"
    assert permit.from_result("Typed 'BotFather' into the search field") is None


def test_asks_at_once_then_says_plainly_and_asks_again(monkeypatch):
    asks = _fresh(monkeypatch)
    monkeypatch.setattr(permit, "_watch", lambda kind, resume: None)
    first = permit.request("accessibility", "ui_act")
    assert first.startswith("NEEDS PERMISSION") and "nothing was done" in first and asks == ["accessibility"]
    same_breath = permit.request("accessibility", "click_text")      # a second tool a moment later: no second box
    assert same_breath.startswith("NEEDS PERMISSION") and asks == ["accessibility"]
    monkeypatch.setattr(permit, "AGAIN_AFTER", 0.0)
    again = permit.request("accessibility", "ui_act")                # asked again after a "no"
    assert again.startswith("STILL NO PERMISSION") and "can't do this without" in again
    assert asks == ["accessibility", "accessibility"]                # System Settings opened again


def test_carries_on_once_it_is_switched_on(monkeypatch):
    _fresh(monkeypatch)
    state = {"now": "denied"}
    monkeypatch.setattr(permit.permissions, "status", lambda kind, asked=(): state["now"])
    resumed = []
    permit.request("accessibility", "ui_act", resume=resumed.append)
    state["now"] = "allowed"
    deadline = time.monotonic() + 4
    while not resumed and time.monotonic() < deadline:
        time.sleep(0.1)
    assert resumed == ["accessibility"]
    assert "accessibility" not in permit._times                       # a later miss asks afresh


def test_search_box_found_by_its_placeholder():
    assert ground._shown_as("Q Search (9K)") == "search"              # Telegram's, ⌘K read as 9K
    assert ground._shown_as("Search (⌘K)") == "search"
    assert ground._shown_as("Saved Messages") == "saved messages"


def test_mint_knows_its_permissions_from_the_start():
    note = permit.prompt_note({"accessibility": "denied", "screen": "allowed", "microphone": "allowed", "input": "ask"})
    assert "on: Screen Recording, Microphone." in note
    assert "Off: Accessibility (" in note and "can't click, type" in note and "Input Monitoring" in note
    assert "System Settings open at the right switch" in note and "you can't click it" in note
    assert "Off:" not in permit.prompt_note({k: "allowed" for k in permit._KNOWN})
