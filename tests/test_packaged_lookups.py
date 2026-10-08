"""Modules looked up by name at run time must resolve in the packaged layout (mint/ui, mint/tools, ...).

The open notch once lost Search, the shelf and the calendar in the app from the DMG: notch.py imported them as
"mint." + name, a flat name that only exists in the private working copy.
"""
import importlib


def test_notch_finds_its_panes():
    from mint.ui import notch
    n = notch.Notch.__new__(notch.Notch)
    for name in ("notch_shelf", "notch_search", "notch_calendar", "notch_battery", "notch_agents", "notch_tips"):
        assert n._mod(name) is not None, name


def test_sound_mute_checks_point_at_real_modules():
    from mint.ui import sfx
    for name, path in sfx._MODULES.items():
        assert importlib.util.find_spec(path) is not None, (name, path)
