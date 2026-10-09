"""Settings' pop-ups: options with pictures, closed titles that fit, buttons that fit, the usage-limit rows."""
import pytest

try:
    from mint.core import prefs
    from mint.ui import settings_art
    from mint.ui import settings as settings_window
except ImportError:
    from mint import prefs, settings_art, settings_window

AppKit = pytest.importorskip("AppKit")


@pytest.fixture
def fresh_prefs(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs, "PATH", tmp_path / "settings.json")
    monkeypatch.setattr(prefs, "_values", {})
    monkeypatch.setattr(prefs, "_mtime", None)
    monkeypatch.setattr(prefs, "_listeners", [])
    return prefs


def _window():
    win = settings_window.SettingsWindow({"audio_status": lambda: "ok"})
    win.target = settings_window._SettingsTarget.alloc().initWithOwner_(win)
    return win


def _views(view, out=None):
    out = [] if out is None else out
    out.append(view)
    for sub in view.subviews():
        _views(sub, out)
    return out


def test_popup_takes_two_three_and_four_part_options(fresh_prefs):
    win = _window()
    view = settings_window._SettingsFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 500, 100))
    options = [("a", "Plain"), ("b", "Symbol", "star.fill"), ("c", "Tinted", ("leaf.fill", (0.2, 0.7, 0.4)), "Sub"),
               ("d", "Drawn", lambda: settings_art.orb("rose")), ("e", "Image", settings_art.flag("French")),
               ("f", "Plain")]                       # a repeated title stays its own item
    popup = win._popup(view, "test_key", options, 0, 0, 200)
    assert isinstance(popup, AppKit.NSPopUpButton) and isinstance(popup.cell(), settings_art.MintSettingsPopUpCell)
    assert popup.numberOfItems() == 6
    assert popup.itemAtIndex_(0).image() is None
    assert all(popup.itemAtIndex_(i).image() is not None for i in range(1, 5))
    if hasattr(AppKit.NSMenuItem, "setSubtitle_"):
        assert str(popup.itemAtIndex_(2).subtitle()) == "Sub"
    popup.selectItemAtIndex_(5)
    win._changed(popup)
    assert prefs.get("test_key") == "f"


def test_every_option_list_has_its_pictures():
    for name in ("ECHO", "STRICTNESS", "THEMES", "WHERE", "OPEN_TO", "MOTION", "AGENT_MODES", "WAKE_SENSITIVITY",
                 "FOLLOW_UP", "GUARD_LEVELS", "MEET_SHARE", "AGENT_TELEGRAM", "POSITIONS", "LANGUAGES",
                 "SCREENSHOTS", "CALLS", "UNLOAD", "VOICE_FILTERS", "PERSONALITIES", "STYLES"):
        for option in getattr(settings_window, name):
            assert len(option) >= 3 and settings_art.resolve(option[2]) is not None, (name, option[:2])


def test_device_symbols_follow_the_name():
    sym = settings_art.device_symbol
    assert sym("") == "gearshape.fill"
    assert sym("Sam's AirPods Pro", "Bluetooth", True) == "airpodspro"
    assert sym("AirPods Max", "Bluetooth") == "airpodsmax"
    assert sym("MacBook Air Microphone", "built-in") == "mic.fill"
    assert sym("MacBook Air Speakers", "built-in", True) == "speaker.wave.2.fill"
    assert sym("LG UltraFine", "DisplayPort", True) == "display"
    assert sym("Microsoft Teams Audio", "virtual") == "waveform"
    assert sym("iPhone Microphone", "Continuity") == "iphone"
    assert sym("WH-1000XM5", "Bluetooth", True) == "headphones"


def test_voice_closed_title_keeps_name_and_tag_only():
    button = settings_art.popup(AppKit.NSMakeRect(0, 0, 250, 26))
    item = settings_art.add_item(button, "Sulafat - female, warm", lambda: settings_art.avatar("Sulafat"),
                                 attributed=settings_art.voice_title("Sulafat", "female", "warm"))
    title = settings_art.closed_title(item)
    shown = str(title.string())
    # the name, then the female/male tag (a little capsule image, an attachment) - never the character word
    assert shown.startswith("Sulafat  ") and "warm" not in shown
    assert title.attribute_atIndex_effectiveRange_(AppKit.NSAttachmentAttributeName, len(shown) - 1, None)[0] is not None
    # the name is drawn in the dynamic label colour (never a fixed black on the dark fill)
    attrs = settings_art.closed_title(item).attributesAtIndex_effectiveRange_(0, None)[0]
    assert attrs[AppKit.NSForegroundColorAttributeName] == AppKit.NSColor.labelColor()
    assert settings_art.avatar_color("Sulafat") == settings_art.avatar_color("Sulafat")


def test_popups_and_buttons_grow_to_fit_their_titles():
    button = settings_art.popup(AppKit.NSMakeRect(100, 0, 120, 26))
    settings_art.add_item(button, "Deleting, changes and system commands", ("trash", (1, 0.5, 0)))
    need = settings_art.fit_width(button, 120)
    assert need > 120
    settings_art.fit(button)
    frame = button.frame()
    assert abs(frame.origin.x + frame.size.width - 220) < 0.01 and frame.size.width == need   # right edge kept

    push = settings_art.push_button("Go", AppKit.NSMakeRect(300, 0, 80, 28))
    assert push.frame().size.width == 80                     # the given width is a minimum
    push.setTitle_("Stop showing Claude's usage limits")
    frame = push.frame()
    assert frame.size.width > 80 and abs(frame.origin.x + frame.size.width - 380) < 0.01
    assert settings_art.button_width("Connect Claude Code", 100) > 100


def _coding(win, facts):
    column = settings_window._SettingsFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 500, 10))
    page = settings_window._Page(win, column, 480)
    win._coding_section(page, facts)
    return column


def test_usage_limits_rows(fresh_prefs, monkeypatch):
    import time
    now = time.time()
    facts = {"here": {"claude": True, "codex": True}, "hooks": False, "statusline": False, "mcp": {},
             "limits": {"claude": {"5h": 23.0, "week": 91.0, "5h_resets": now + 3600, "at": now - 60, "live": True}}}
    win = _window()
    doc = _coding(win, facts)
    meters = [v for v in _views(doc) if isinstance(v, settings_art.UsageMeter)]
    assert [(m.label, m.used) for m in meters] == [("5 hours", 23.0), ("Week", 91.0)]
    assert meters[0].note.startswith("resets in") and str(meters[1].accessibilityValue()).startswith("91% used")
    texts = [str(v.stringValue()) for v in _views(doc) if isinstance(v, AppKit.NSTextField)]
    assert "Codex" in texts and "Live" in texts
    assert "Not seen yet" not in texts                         # Codex's limits unknown: no row, no old number
    assert settings_art.tone_rgb(10) == settings_art.GREEN and settings_art.tone_rgb(70) == settings_art.AMBER
    assert settings_art.tone_rgb(90) == settings_art.RED
    assert settings_art.until(now + 90 * 60, now) == "resets in 1 h 30 min"
    assert settings_art.until(now - 5, now) == "has reset"


def test_coding_agents_only_what_is_here(fresh_prefs, monkeypatch):
    win = _window()
    texts = lambda doc: [str(v.stringValue()) for v in _views(doc) if isinstance(v, AppKit.NSTextField)]
    buttons = lambda doc: [str(v.title()) for v in _views(doc) if isinstance(v, AppKit.NSButton)]
    doc = _coding(win, {"here": {"claude": False, "codex": False}, "limits": {}})
    assert "Not on this Mac" in texts(doc) and "Pop up when one finishes" not in texts(doc)
    doc = _coding(win, {"here": {"claude": False, "codex": True}, "limits": {}, "hooks": False})
    assert "Codex" in texts(doc) and "Claude Code" not in texts(doc) and "Connect" not in buttons(doc)
    doc = _coding(win, {"here": {"claude": True, "codex": False}, "limits": {}, "hooks": True, "statusline": False})
    assert "Disconnect" in buttons(doc) and "Its usage limits" in texts(doc) and "Codex" not in texts(doc)
    doc = _coding(win, {"here": {"claude": True, "codex": False}, "limits": {}, "hooks": False, "statusline": True})
    assert "Connect" in buttons(doc) and "Its usage limits" not in texts(doc)    # set up, nothing fresh: nothing


def test_every_drawn_picture_draws(monkeypatch):
    failed = []
    monkeypatch.setattr(settings_art.log, "debug", lambda *a, **k: failed.append(a) if k.get("exc_info") else None)
    images = ([settings_art.orb(t) for t in ("mint", "blue", "aurora", "sunset", "rose", "mono")]
              + [settings_art.where(n) for n in (False, True)]
              + [settings_art.position(p) for p in settings_art.SPOTS]
              + [settings_art.flag(lang) for lang in ("auto", "Hindi", "French")]
              + [settings_art.avatar("Puck"), settings_art.symbol("star.fill", (1, 0, 0))])
    canvas = AppKit.NSImage.alloc().initWithSize_(AppKit.NSMakeSize(40, 40))
    canvas.lockFocus()
    try:
        for image in images:
            image.drawInRect_(AppKit.NSMakeRect(0, 0, 30, 20))
    finally:
        canvas.unlockFocus()
    assert not failed


# --- agents wear their critter ------------------------------------------------------------------------

def _settings_models():
    try:
        from mint.ui import settings_models
    except ImportError:
        from mint import settings_models
    return settings_models


def test_agent_rows_show_critter_faces_not_dots(fresh_prefs):
    settings_models = _settings_models()
    win = _window()
    column = settings_window._SettingsFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 500, 10))
    page = settings_window._Page(win, column, 480)
    agents = [{"name": "Astra", "role": "Research", "models": ["gemini/x"], "color": "#8B7CFF"},
              {"name": "Nova", "role": "Writes", "models": ["gemini/x"], "color": "#FF6B9A"}]
    facts = {"agents": agents, "pals": {"Astra": ("owl", (0.5, 0.5, 1.0))}}
    settings_models._agents_section(win, page, facts)
    texts = [str(v.stringValue()) for v in _views(column) if isinstance(v, AppKit.NSTextField)]
    assert "Astra" in texts and "Nova" in texts and not any(t.startswith("●") for t in texts)
    faces = [v for v in _views(column) if str(v.accessibilityLabel() or "") == "owl character"]
    assert len(faces) == 1                     # Nova has no pal here: a plain dot in its colour instead


def test_character_and_colour_pickers_update_live():
    settings_models = _settings_models()
    form = settings_models._Form.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 460, 200))
    state, y = settings_models._character_rows(form, 0, "owl", "#8B7CFF")
    assert y > 60 and state["pal"] == "owl" and state["color"] == "#8B7CFF"
    tiles = {str(v.accessibilityLabel()): v for v in _views(form) if isinstance(v, settings_models._SettingsPickTile)}
    tiles["bear"].pick()
    tiles["Colour #FF6B9A"].pick()
    assert state["pal"] == "bear" and state["color"] == "#FF6B9A"
    assert tiles["bear"].layer().borderWidth() == 2.0 and tiles["owl"].layer().borderWidth() < 1
    state["well"].setColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.0, 0.5, 1.0, 1.0))
    settings_models._well_changed(state)
    assert state["color"] == "#0080FF"
