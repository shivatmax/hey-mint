"""The notch's settings menu (rolls down under its button, same items as the menu it is given) and the clipboard
window's Copy (the clip goes to the top) and bigger close button; the notch's hover timings."""
import time

import AppKit

try:
    from mint.tools import clipboard as clip_tools
    from mint.ui import gfx, notch, notch_menu
except ImportError:
    from mint import clip_tools, gfx, notch, notch_menu


def _menu():
    menu = AppKit.NSMenu.alloc().init()
    menu.setAutoenablesItems_(False)

    def add(title, key="", on=None, enabled=True, hidden=False):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, key)
        item.setEnabled_(enabled)
        item.setHidden_(hidden)
        if on is not None:
            item.setState_(AppKit.NSControlStateValueOn if on else AppKit.NSControlStateValueOff)
        menu.addItem_(item)
    menu.addItem_(AppKit.NSMenuItem.separatorItem())            # a leading separator is dropped
    add("Go to sleep")
    menu.addItem_(AppKit.NSMenuItem.separatorItem())
    menu.addItem_(AppKit.NSMenuItem.separatorItem())            # two in a row become one
    add("Microphone", on=True)
    add("Spoken replies", on=False)
    add("Secret", hidden=True)
    add("Listening", enabled=False)
    menu.addItem_(AppKit.NSMenuItem.separatorItem())
    add("Settings…", key=",")
    menu.addItem_(AppKit.NSMenuItem.separatorItem())            # a trailing one too
    return menu


def test_menu_rows_mirror_the_menu():
    rows = notch_menu.entries(_menu())
    shape = ["sep" if r.get("sep") else r["title"] for r in rows]
    assert shape == ["Go to sleep", "sep", "Microphone", "Spoken replies", "Listening", "sep", "Settings…"]
    by = {r["title"]: r for r in rows if not r.get("sep")}
    assert by["Microphone"]["on"] and not by["Spoken replies"]["on"]
    assert by["Settings…"]["key"] == "⌘,"
    assert not by["Listening"]["enabled"]
    assert notch_menu.height_of(rows) == 2 * notch_menu.PAD + 5 * notch_menu.ROW + 2 * notch_menu.SEP


def test_notch_timings(monkeypatch):
    assert notch.INNER_OPEN <= 1.0                         # the full notch opens quickly from the small row
    assert notch.AFTER_LEAVE <= 1.0                        # and folds a second after the pointer leaves
    values = {"notch_close_after": 5.0, "notch_open_after": "nonsense"}
    monkeypatch.setattr(notch.prefs, "get", lambda key: values.get(key))
    assert notch._seconds("notch_close_after", 1.0) == 5.0                   # Settings can make it longer
    assert notch._seconds("notch_open_after", 0.8) == 0.8                    # a bad value falls back
    values["notch_close_after"] = 999
    assert notch._seconds("notch_close_after", 1.0) == 30.0


def test_copy_moves_a_clip_to_the_top(monkeypatch, tmp_path):
    monkeypatch.setattr(clip_tools, "STORE", tmp_path)
    monkeypatch.setattr(clip_tools, "_store", lambda: tmp_path)
    monkeypatch.setattr(clip_tools, "_loaded", True)
    monkeypatch.setattr(clip_tools, "_pins", lambda: {})
    now = time.time()
    monkeypatch.setattr(clip_tools, "HISTORY", [{"id": "a", "kind": "text", "text": "new", "label": "new", "at": now},
                                                 {"id": "b", "kind": "text", "text": "old", "label": "old",
                                                  "at": now - 600}])
    before = clip_tools.VERSION[0]
    moved = clip_tools.to_top("b")
    assert [h["id"] for h in clip_tools.HISTORY] == ["b", "a"]
    assert moved["at"] >= now and clip_tools.VERSION[0] > before
    assert clip_tools.to_top("missing") is None


def test_close_button_sizes():
    small = gfx.close_button(None, "close:", "Close")
    big = gfx.close_button(None, "close:", "Close", size=30, rest=0.92, base=0.16)
    assert small.frame().size.width == gfx.CLOSE and big.frame().size.width == 30
    assert big.alphaValue() > small.alphaValue()           # plain to see even when the pointer isn't on the card
    assert big.layer().cornerRadius() == 15


SECRETS = ["AMf-vBykCpa3FN0nNZ0cCy50Xq8eQ0Jk1TRZ3lVb9_kP0QwX2mGkH7nC-dL4",        # an OAuth token
           "S+/BRbGNfhYlTX8FKuBXNtjnoE2q5Zr1v7Wy9Kp3Ls8=",                       # a base64 secret
           "ghp_" + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5",                     # a GitHub token (split: the
                                                                                 # export's leak check)
           "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abc123DEF456ghi",            # a JWT
           "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"]
ORDINARY = ["MINUTE_WORKER_SECRET", "Meeting notes for Thursday: ship it", "https://hey-mint.pages.dev/docs#clipboard",
            "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", "550e8400-e29b-41d4-a716-446655440000", "getUserProfileById",
            "Report_Q3-2026_Final_v2.pdf", "name@example.com", "HeyMint-0.6.4-arm64", "iPhone15ProMax256GB"]


def test_copied_keys_and_tokens_are_never_kept():
    assert all(clip_tools._secret(s) for s in SECRETS)
    assert not any(clip_tools._secret(s) for s in ORDINARY)


def test_keys_kept_before_are_dropped_from_the_saved_history(monkeypatch, tmp_path):
    import json
    (tmp_path / "images").mkdir()
    now = time.time()
    rows = [{"id": "k", "kind": "text", "text": SECRETS[1], "label": "x", "at": now},
            {"id": "n", "kind": "text", "text": "a note", "label": "a note", "at": now}]
    (tmp_path / "history.json").write_text(json.dumps(rows))
    monkeypatch.setattr(clip_tools, "STORE", tmp_path)
    monkeypatch.setattr(clip_tools, "_store", lambda: tmp_path)
    monkeypatch.setattr(clip_tools, "_loaded", False)
    monkeypatch.setattr(clip_tools, "_pins", lambda: {})
    monkeypatch.setattr(clip_tools, "HISTORY", [])
    clip_tools._load()
    assert [h["id"] for h in clip_tools.HISTORY] == ["n"]
    assert SECRETS[1] not in (tmp_path / "history.json").read_text()       # gone from the file too


def test_settings_shows_update_progress_only_while_it_is_on_its_way():
    try:
        from mint.ui import settings as settings_window
        from mint.app import updater
    except ImportError:
        from mint import settings_window, updater
    on_way = settings_window._updating
    assert on_way("downloading", False, False) and on_way("verifying", True, False) and on_way("installing", True, True)
    assert on_way("available", True, False)                     # asked for: about to download
    assert not on_way("error", True, False)                     # failed: the page shows why, with its buttons
    assert not on_way("ready", True, True) and not on_way("available", False, False)
    p = updater.progress()
    assert {"status", "progress", "version", "size", "ready", "requested"} <= set(p)


def test_install_pressed_installs_once_downloaded_even_while_mint_is_awake(monkeypatch):
    try:
        from mint.app import updater
    except ImportError:
        from mint import updater
    assert {"talking", "working on a task"} <= set(updater.SOFT_BUSY)
    assert "running a tool" not in updater.SOFT_BUSY and "speaking" not in updater.SOFT_BUSY
    monkeypatch.setitem(updater._state, "status", "installing")
    assert updater.install_now().startswith("Already installing")       # a second press: no error, no swallow
