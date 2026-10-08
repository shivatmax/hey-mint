"""Settings never blocks the main thread, settings listeners run off it, and the heartbeat Mint.app's
watchdog reads. (A hung main thread froze all of Mint, which has no Dock icon or Force Quit entry.)"""
import inspect
import os
import threading
import time

import pytest

try:
    from mint.app import ear, power
    from mint.core import prefs
    from mint.ui import settings as settings_window
    from mint.ui import settings_connectors, settings_models
except ImportError:
    from mint import ear, power, prefs, settings_connectors, settings_models, settings_window

AppKit = pytest.importorskip("AppKit")


def _window():
    win = settings_window.SettingsWindow({"audio_status": lambda: "ok"})
    win.target = settings_window._SettingsTarget.alloc().initWithOwner_(win)     # show() makes it
    return win


def _texts(view, out=None):
    out = [] if out is None else out
    if isinstance(view, AppKit.NSTextField):
        out.append(str(view.stringValue()))
    for sub in view.subviews():
        _texts(sub, out)
    return out


def _column():
    return settings_window._SettingsFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 500, 10))


USAGE = {"rows": [{"model": "gemini-test", "requests": 3, "input": 120, "output": 40, "thinking": 0}],
         "daily": [(f"2026-10-{d:02d}", d * 10) for d in range(1, 15)]}


# --- every page reads its facts off the main thread -------------------------------------------------

def test_every_page_reads_off_the_main_thread():
    pure = {"shortcuts"}                        # prefs and key names only
    for key, _, _ in settings_window.PAGES:
        builder = getattr(settings_window.SettingsWindow, f"_page_{key}")
        assert list(inspect.signature(builder).parameters)[1:] == ["page", "facts"], key
        assert key in pure or hasattr(settings_window.SettingsWindow, f"_facts_{key}"), key


BLOCKING = ("subprocess.", "shortcut_library.installed(", "user_shortcuts(", "connectors.snapshot(",
            "usage.totals(", "usage.daily(", "updater.info(", "updater.install(", "telegram.status(",
            "telegram.unpair(", "audio_devices.", "skillbook.all_skills(", "membank.blocks(", "registry.load(",
            "catalog.settings(", "gemini_keys.status(", "active_phrases(", "voices.catalog(", "starts_at_login(")


@pytest.mark.parametrize("source", [
    *[getattr(settings_window.SettingsWindow, name) for name in dir(settings_window.SettingsWindow)
      if name.startswith("_page_")],
    settings_window.SettingsWindow._telegram_card, settings_window.SettingsWindow._wake_section,
    settings_window.SettingsWindow._mic_test_section, settings_window.SettingsWindow._devices_section,
    settings_window.SettingsWindow._calls_section, settings_window.SettingsWindow._apple_shortcuts_sections,
    settings_window.SettingsWindow._email_card, settings_window.SettingsWindow._looks_more_rows,
    settings_connectors.page, settings_models.page, settings_models._gemini_section,
    settings_models._providers_section, settings_models._backups_section, settings_models._agents_section,
], ids=lambda f: f.__name__)
def test_page_builders_do_not_call_slow_things(source):
    """What can block (a command, CoreAudio, another module's lock, files, the network) belongs in the
    page's _facts_ reader, which runs on a thread - not in the builder, which runs on the main thread.
    Calls inside a nested def are fine (they run later, in handlers that hand off to a thread)."""
    lines = inspect.getsource(source).splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip())
    body, nested = [], None
    for line in lines[1:]:
        depth = len(line) - len(line.lstrip())
        if nested is not None and line.strip() and depth <= nested:
            nested = None
        if nested is None and line.lstrip().startswith("def ") and depth > indent:
            nested = depth
            continue
        if nested is None:
            body.append(line)
    text = "\n".join(body)
    found = [call for call in BLOCKING if call in text]
    assert not found, f"{source.__name__} calls {found} on the main thread"


def test_slow_page_shows_loading_then_fills_in(monkeypatch):
    monkeypatch.setattr(settings_window, "LOAD_WAIT", 0.05)
    release = threading.Event()

    def slow(self):
        release.wait(5)
        return USAGE
    monkeypatch.setattr(settings_window.SettingsWindow, "_facts_usage", slow)
    win = _window()
    win.page_key = "usage"
    started = time.monotonic()
    words = _texts(win._build_page(_column(), 480).doc)
    assert time.monotonic() - started < 1.0, "the main thread waited for a slow read"
    assert "Loading…" in words
    release.set()
    win._jobs["usage"]["done"].wait(5)
    words = _texts(win._build_page(_column(), 480).doc)
    assert "Loading…" not in words
    assert "gemini-test" in words and "Total" in words


def test_failed_read_says_so_with_try_again(monkeypatch):
    def broken(self):
        raise RuntimeError("the disk said no")
    monkeypatch.setattr(settings_window.SettingsWindow, "_facts_usage", broken)
    win = _window()
    win.page_key = "usage"
    page = win._build_page(_column(), 480)
    words = _texts(page.doc)
    assert any("couldn't load" in w and "the disk said no" in w for w in words)
    buttons = [str(v.title()) for v in page.doc.subviews()[0].subviews() if isinstance(v, AppKit.NSButton)]
    assert "Try again" in buttons


def test_a_builder_that_raises_shows_a_message_not_half_a_page(monkeypatch):
    monkeypatch.setattr(settings_window.SettingsWindow, "_facts_usage", lambda self: {"rows": None})
    win = _window()
    win.page_key = "usage"
    words = _texts(win._build_page(_column(), 480).doc)
    assert any(w.startswith("This page couldn't load") for w in words)


def test_stuck_read_is_logged_with_its_stack(monkeypatch, caplog):
    monkeypatch.setattr(settings_window, "LOAD_WAIT", 0.01)
    release = threading.Event()

    def stuck_reading_usage(self):
        release.wait(5)
        return USAGE
    monkeypatch.setattr(settings_window.SettingsWindow, "_facts_usage", stuck_reading_usage)
    win = _window()
    win.page_key = "usage"
    win._build_page(_column(), 480)
    job = win._jobs["usage"]
    with caplog.at_level("WARNING", logger="mint.settings"):
        win._check_slow("usage", job)
    release.set()
    assert job["slow"]
    assert "stuck_reading_usage" in caplog.text


def test_try_again_starts_a_new_read(monkeypatch):
    monkeypatch.setattr(settings_window.SettingsWindow, "_facts_usage", lambda self: USAGE)
    win = _window()
    win.page_key = "usage"
    win._facts("usage")
    first = win._jobs["usage"]
    win._retry("usage")                         # no window: refresh() does nothing
    win._facts("usage")
    assert win._jobs["usage"] is not first


# --- fewer pages, basic and advanced ----------------------------------------------------------------

def test_merged_pages_keep_their_old_keys():
    """Voice & wake word + Audio, Shortcuts + Apple Shortcuts, Accounts & keys + Connectors are one page each now;
    show("audio") and the connectors' Settings… buttons (settings_page="apple_shortcuts") still land there."""
    keys = [key for key, _, _ in settings_window.PAGES]
    assert not {"audio", "apple_shortcuts", "connectors"} & set(keys)
    assert dict((k, t) for k, t, _ in settings_window.PAGES)["voice"] == "Microphone & voice"
    assert dict((k, t) for k, t, _ in settings_window.PAGES)["accounts"] == "Accounts & connections"
    for old, new in {"audio": "voice", "apple_shortcuts": "shortcuts", "connectors": "accounts", "voice": "voice",
                     "models": "models", "": "general", "gone": "general"}.items():
        assert settings_window.page_for(old) == new, old
    assert prefs.DEFAULTS["settings_advanced"] is False


VOICE = {"active": ["Hey Mint"], "stale": False, "enrolled": False, "enrolled_at": "", "status": "",
         "inputs": [("", "System default")], "outputs": [("", "System default")], "mic_users": []}


def _views(view, out=None):
    out = [] if out is None else out
    out.append(view)
    for sub in view.subviews():
        _views(sub, out)
    return out


def test_advanced_rows_show_only_when_asked(fresh_prefs, monkeypatch):
    monkeypatch.setattr(settings_window.SettingsWindow, "_facts_general", lambda self: {"login": False,
                                                                                       "can_restart": False})
    monkeypatch.setattr(settings_window.SettingsWindow, "_facts_voice", lambda self: dict(VOICE))
    win = _window()
    for key, technical in (("general", "Ask before deleting or changing"), ("voice", "Echo cancellation")):
        win.page_key = key
        words = _texts(win._build_page(_column(), 480).doc)
        assert technical not in words, key
        assert "Show advanced settings" in words if key == "general" else "More settings" in words
    win.page_key = "voice"
    buttons = [str(v.title()) for v in _views(win._build_page(_column(), 480).doc) if isinstance(v, AppKit.NSButton)]
    assert "Test microphone" in buttons and "Train my voice…" in buttons and "Test" not in buttons
    assert "Test this phrase" not in buttons and "Record 4 takes" not in buttons     # the custom wake phrase

    prefs.set("settings_advanced", True)
    win.page_key = "general"
    assert "Ask before deleting or changing" in _texts(win._build_page(_column(), 480).doc)


def test_one_calls_choice_sets_both_prefs(fresh_prefs, monkeypatch):
    monkeypatch.setattr(settings_window.SettingsWindow, "_facts_voice", lambda self: dict(VOICE))
    win = _window()
    win.page_key = "voice"
    popup = next(v for v in _views(win._build_page(_column(), 480).doc)
                 if isinstance(v, AppKit.NSPopUpButton) and v.numberOfItems() == 3
                 and str(v.itemTitleAtIndex_(2)) == "Do nothing special")
    assert str(popup.titleOfSelectedItem()) == "Keep listening for the wake word"      # the defaults
    for index, expected in ((1, (True, False)), (2, (False, False)), (0, (True, True))):
        popup.selectItemAtIndex_(index)
        win._changed(popup)
        assert (prefs.get("share_mic"), prefs.get("listen_in_calls")) == expected, index


# --- settings listeners run off the main thread -----------------------------------------------------

@pytest.fixture
def fresh_prefs(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs, "PATH", tmp_path / "settings.json")
    monkeypatch.setattr(prefs, "_values", {})
    monkeypatch.setattr(prefs, "_mtime", None)
    monkeypatch.setattr(prefs, "_listeners", [])
    return prefs


def test_listeners_of_a_main_thread_change_run_on_a_worker_in_order(fresh_prefs):
    assert threading.current_thread() is threading.main_thread()
    heard, done = [], threading.Event()

    def listener(key, value):
        time.sleep(0.2)                         # a listener doing real work (reloading a model…)
        heard.append((key, value, threading.current_thread() is threading.main_thread()))
        if len(heard) == 2:
            done.set()
    prefs.on_change(listener)
    started = time.monotonic()
    prefs.set("theme", "rose")
    prefs.set("theme", "mono")
    assert time.monotonic() - started < 0.15, "the main thread waited for a listener"
    assert prefs.get("theme") == "mono"          # saved at once, before the listeners ran
    assert done.wait(3)
    assert heard == [("theme", "rose", False), ("theme", "mono", False)]


def test_listeners_of_a_change_on_another_thread_run_right_there(fresh_prefs):
    heard = []
    prefs.on_change(lambda key, value: heard.append(threading.current_thread().name))
    worker = threading.Thread(target=lambda: prefs.set("face", False), name="a-tool")
    worker.start()
    worker.join()
    assert heard == ["a-tool"]


# --- the heartbeat ----------------------------------------------------------------------------------

@pytest.fixture
def beat_file(monkeypatch, tmp_path):
    path = tmp_path / "heartbeat"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    monkeypatch.setitem(ear._beat, "fd", fd)
    monkeypatch.setitem(ear._beat, "count", 0)
    monkeypatch.setitem(ear._beat, "allow", ear.ALLOW)
    yield path
    os.close(fd)


def test_beats_are_what_the_ear_reads(beat_file):
    ear.beat()
    ear.beat()
    assert beat_file.read_text().split() == [str(os.getpid()), "2", str(ear.ALLOW)]
    ear.grace(75)                               # quitting: the Ear waits longer
    pid, count, allow = beat_file.read_text().split()
    assert (pid, count, allow) == (str(os.getpid()), "3", "75")


def test_beat_rewrites_in_place(beat_file):
    for _ in range(200):
        ear.beat()
    text = beat_file.read_text()
    assert text.split()[1] == "200" and len(text) == 40


def test_restart_needs_mint_app(monkeypatch):
    monkeypatch.delenv("MINT_EAR_SOCKET", raising=False)
    assert not power.can_restart()
    assert "isn't running from Mint.app" in power.restart("test")
    assert not power._quitting.is_set()
