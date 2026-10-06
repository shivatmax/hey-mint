"""The Mac's own switches, by voice - the small things people ask for all day.

    brightness      "brighter", "dim the screen", "brightness 70"
    keep_awake      "keep the Mac awake for 2 hours", "stay awake", "you can sleep now"
    focus           "turn on Do Not Disturb", "focus off"
    window          "put this window on the left half", "Chrome left, Slack right", "maximise",
                    "full screen", "centre it", "move it to the other display"
    music           "play the last music", "resume my music"
    night_shift     "night shift on"
    wifi            "turn Wi-Fi off"
    show            "show the desktop", "mission control", "hide the other apps"
    settings        "open the Bluetooth settings"

Brightness uses DisplayServices (the built-in display); keep-awake is `caffeinate`;
windows go through Rectangle when it is installed (its URL actions) and otherwise are
placed by Mint itself through Accessibility. Do Not Disturb runs two shortcuts, "Mint
Focus On" and "Mint Focus Off" (one 'Set Focus' action each): macOS has no command for
Focus, so the first time Mint builds and signs them and the user adds each with one click.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import time

log = logging.getLogger("mint.tools.mac")

_caffeinate: dict = {"proc": None, "until": 0.0}
LAYOUTS = {
    # name -> Rectangle action, and (x, y, w, h) as fractions of the screen's visible area
    "left_half": ("left-half", (0, 0, .5, 1)), "right_half": ("right-half", (.5, 0, .5, 1)),
    "top_half": ("top-half", (0, 0, 1, .5)), "bottom_half": ("bottom-half", (0, .5, 1, .5)),
    "top_left": ("top-left", (0, 0, .5, .5)), "top_right": ("top-right", (.5, 0, .5, .5)),
    "bottom_left": ("bottom-left", (0, .5, .5, .5)), "bottom_right": ("bottom-right", (.5, .5, .5, .5)),
    "left_third": ("first-third", (0, 0, 1 / 3, 1)), "center_third": ("center-third", (1 / 3, 0, 1 / 3, 1)),
    "right_third": ("last-third", (2 / 3, 0, 1 / 3, 1)), "left_two_thirds": ("first-two-thirds", (0, 0, 2 / 3, 1)),
    "right_two_thirds": ("last-two-thirds", (1 / 3, 0, 2 / 3, 1)),
    "maximize": ("maximize", (0, 0, 1, 1)), "almost_maximize": ("almost-maximize", (.05, .05, .9, .9)),
    "center": ("center", None), "restore": ("restore", None), "next_display": ("next-display", None),
    "previous_display": ("previous-display", None), "larger": ("larger", None), "smaller": ("smaller", None),
}
SETTINGS = {
    "displays": "com.apple.Displays-Settings.extension", "sound": "com.apple.Sound-Settings.extension",
    "wifi": "com.apple.wifi-settings-extension", "bluetooth": "com.apple.BluetoothSettings",
    "battery": "com.apple.Battery-Settings.extension", "notifications": "com.apple.Notifications-Settings.extension",
    "focus": "com.apple.Focus-Settings.extension", "privacy": "com.apple.settings.PrivacySecurity.extension",
    "keyboard": "com.apple.Keyboard-Settings.extension", "trackpad": "com.apple.Trackpad-Settings.extension",
    "general": "com.apple.systempreferences.GeneralSettings", "appearance": "com.apple.Appearance-Settings.extension",
    "wallpaper": "com.apple.Wallpaper-Settings.extension", "network": "com.apple.Network-Settings.extension",
    "accessibility": "com.apple.Accessibility-Settings.extension", "users": "com.apple.Users-Groups-Settings.extension",
    "screen_time": "com.apple.Screen-Time-Settings.extension", "storage": "com.apple.settings.Storage",
    "software_update": "com.apple.Software-Update-Settings.extension", "desktop_dock": "com.apple.Desktop-Settings.extension",
    "lock_screen": "com.apple.Lock-Screen-Settings.extension", "control_center": "com.apple.ControlCenter-Settings.extension",
}


def _osa(script: str, timeout: float = 15) -> tuple[bool, str]:
    done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    return done.returncode == 0, (done.stdout.strip() or done.stderr.strip())


# --- Brightness ----------------------------------------------------------------------------

def _ds():
    lib = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
    lib.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
    lib.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
    return lib


def brightness(value: str = "") -> str:
    import Quartz
    ds, display = _ds(), Quartz.CGMainDisplayID()
    now = ctypes.c_float()
    if ds.DisplayServicesGetBrightness(display, ctypes.byref(now)) != 0:
        return "FAILED: this display's brightness can't be set from here (an external monitor?)."
    current = round(now.value * 100)
    text = str(value or "").strip().lower().rstrip("%")
    if text in ("", "status", "?"):
        return f"Brightness is {current}%."
    if text in ("up", "brighter", "increase", "more"):
        target = current + 15
    elif text in ("down", "dimmer", "dim", "decrease", "less", "lower"):
        target = current - 15
    elif text in ("max", "full", "maximum"):
        target = 100
    elif text in ("min", "minimum", "lowest"):
        target = 5
    else:
        try:
            target = float(text.replace("percent", ""))
        except ValueError:
            return f"FAILED: brightness '{value}'? Say up, down or a number 0-100."
    target = max(3, min(100, int(target)))
    ds.DisplayServicesSetBrightness(display, ctypes.c_float(target / 100))
    if target != current:
        from mint.tools import undo
        undo.record("brightness", f"brightness {current}% → {target}%", {"kind": "brightness", "value": current})
    return f"Brightness {current}% → {target}%."


# --- Keep awake ----------------------------------------------------------------------------

def _awake() -> dict:
    """How keep-awake stands now, as the undo step that brings it back."""
    proc = _caffeinate["proc"]
    if proc is None or proc.poll() is not None:
        return {"kind": "keep_awake", "state": "off"}
    left = (_caffeinate["until"] - time.time()) / 60 if _caffeinate["until"] else 0
    return {"kind": "keep_awake", "state": "on", "minutes": max(1, round(left)) if left else 0}


def keep_awake(state: str = "on", minutes: float = 0) -> str:
    before = _awake()
    said = _keep_awake(state, minutes)
    if str(state or "").lower() != "status" and (before["state"] == "on" or _awake()["state"] == "on"):
        from mint.tools import undo
        undo.record("keep_awake", f"keep awake {str(state or 'on').lower()}", before)
    return said


def _keep_awake(state: str = "on", minutes: float = 0) -> str:
    proc = _caffeinate["proc"]
    running = proc is not None and proc.poll() is None
    state = str(state or "on").lower()
    if state in ("off", "stop", "sleep", "false"):
        if running:
            proc.terminate()
        _caffeinate["proc"] = None
        return "The Mac can sleep normally again." if running else "It was not being kept awake."
    if state == "status":
        if not running:
            return "The Mac sleeps normally."
        left = _caffeinate["until"] - time.time()
        return "Keeping the Mac awake" + (f" for another {int(left // 60)} min." if _caffeinate["until"] else " until told otherwise.")
    if running:
        proc.terminate()
    command = ["caffeinate", "-d", "-i", "-s"]
    if minutes and float(minutes) > 0:
        command += ["-t", str(int(float(minutes) * 60))]
        _caffeinate["until"] = time.time() + float(minutes) * 60
    else:
        _caffeinate["until"] = 0.0
    _caffeinate["proc"] = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
    return ("Keeping the Mac and its display awake" + (f" for {minutes:g} min." if minutes else
            " until you say it can sleep (or Mint quits)."))


# --- Focus / Do Not Disturb ---------------------------------------------------------------

FOCUS_SHORTCUTS = {"on": "Mint Focus On", "off": "Mint Focus Off"}


def _shortcuts() -> set[str]:
    done = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=20)
    return {line.strip() for line in done.stdout.splitlines() if line.strip()}


def _focus_workflow(on: bool) -> dict:
    """One 'Set Focus' action: Do Not Disturb on (until turned off) or off."""
    params = {"Operation": "Turn", "Enabled": 1 if on else 0,
              "FocusModes": {"Identifier": "com.apple.donotdisturb.mode.default", "DisplayString": "Do Not Disturb"}}
    if on:
        params["AssertionType"] = "Turned Off"
    return {"WFWorkflowActions": [{"WFWorkflowActionIdentifier": "is.workflow.actions.dnd.set",
                                   "WFWorkflowActionParameters": params}],
            "WFWorkflowClientVersion": "2607.0.2", "WFWorkflowMinimumClientVersion": 900,
            "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4292093695, "WFWorkflowIconGlyphNumber": 59771},
            "WFWorkflowImportQuestions": [], "WFWorkflowInputContentItemClasses": [], "WFWorkflowTypes": [],
            "WFWorkflowOutputContentItemClasses": [], "WFWorkflowHasShortcutInputVariables": False,
            "WFQuickActionSurfaces": []}


def _offer_focus_shortcuts(missing: list[str]) -> str:
    """Build the missing shortcuts, sign them and open each in Shortcuts, where the user adds it with one click."""
    import plistlib
    import tempfile
    from pathlib import Path
    folder = Path(tempfile.mkdtemp(prefix="mint-focus-"))
    opened = []
    for name in missing:
        raw, signed = folder / (name.replace(" ", "_") + ".wflow"), folder / (name + ".shortcut")
        raw.write_bytes(plistlib.dumps(_focus_workflow(name == FOCUS_SHORTCUTS["on"]), fmt=plistlib.FMT_BINARY))
        done = subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", str(raw), "--output", str(signed)],
                              capture_output=True, text=True, timeout=60)
        if done.returncode != 0 or not signed.exists():
            return ("FAILED: macOS has no command for Do Not Disturb, and Mint could not prepare its shortcut "
                    f"({(done.stderr or '').strip()[:160]}). Tell the user; do not try other ways.")
        subprocess.run(["open", str(signed)], check=False)
        opened.append(name)
        time.sleep(1.5)
    return ("NOT DONE YET: macOS has no command for Do Not Disturb, so it goes through two small shortcuts. Opened "
            f"{' and '.join(repr(n) for n in opened)} in Shortcuts: the user clicks 'Add Shortcut' "
            "(once, for each), then asks again. Tell them that in one sentence; do not click it for them and do not "
            "try other ways (no clicking around System Settings or Control Center).")


def focus(state: str = "on") -> str:
    """Do Not Disturb through two Shortcuts ('Set Focus' actions): macOS has no command or API for
    Focus, and on macOS 27 Control Center's menu bar icon has no accessible name to press."""
    state = str(state or "on").lower()
    want = {"on": "on", "enable": "on", "true": "on", "start": "on", "toggle": "on",
            "off": "off", "disable": "off", "false": "off", "stop": "off"}.get(state)
    if state == "status":
        return ("Mint cannot read whether Do Not Disturb is on (macOS keeps that private); the moon in the menu "
                "bar shows it.")
    if want is None:
        return f"FAILED: focus takes on or off, not '{state}'."
    have = _shortcuts()
    missing = [n for n in FOCUS_SHORTCUTS.values() if n not in have]
    if missing:
        return _offer_focus_shortcuts(missing)
    done = subprocess.run(["shortcuts", "run", FOCUS_SHORTCUTS[want]], capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        return f"FAILED: the shortcut '{FOCUS_SHORTCUTS[want]}' did not run: {(done.stderr or '').strip()[:200]}"
    from mint.tools import undo      # macOS doesn't say what it was before: the way back is the opposite
    undo.record("focus", f"Do Not Disturb {want}", {"kind": "focus", "state": "off" if want == "on" else "on"})
    return f"Do Not Disturb is {want}."


# --- Windows -------------------------------------------------------------------------------

def _rectangle() -> bool:
    return os.path.isdir("/Applications/Rectangle.app")


def _activate(app: str) -> str:
    """Bring `app` to the front. -> its real name, or '' if not running."""
    import AppKit
    from mint.tools import appfinder
    name, _ = appfinder.resolve(app) if app else ("", None)
    wanted = (name or app).lower()
    for running in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        title = str(running.localizedName() or "")
        if title.lower() == wanted or (wanted and wanted in title.lower()):
            running.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.35)
            return title
    return ""


def _place_ax(fractions) -> bool:
    """Place the front window without Rectangle: Accessibility position and size."""
    import AppKit
    import ApplicationServices as AX
    import Quartz
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    element = AX.AXUIElementCreateApplication(front.processIdentifier())
    err, window = AX.AXUIElementCopyAttributeValue(element, "AXFocusedWindow", None)
    if err != 0 or window is None:
        return False
    screen = AppKit.NSScreen.mainScreen()
    full, visible = screen.frame(), screen.visibleFrame()
    top = full.size.height - (visible.origin.y + visible.size.height)      # flip to top-left origin
    fx, fy, fw, fh = fractions
    x, y = visible.origin.x + fx * visible.size.width, top + fy * visible.size.height
    w, h = fw * visible.size.width, fh * visible.size.height
    AX.AXUIElementSetAttributeValue(window, "AXPosition", AX.AXValueCreate(AX.kAXValueCGPointType, Quartz.CGPoint(x, y)))
    AX.AXUIElementSetAttributeValue(window, "AXSize", AX.AXValueCreate(AX.kAXValueCGSizeType, Quartz.CGSize(w, h)))
    return True


def _fullscreen() -> str:
    import AppKit
    import ApplicationServices as AX
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    element = AX.AXUIElementCreateApplication(front.processIdentifier())
    err, window = AX.AXUIElementCopyAttributeValue(element, "AXFocusedWindow", None)
    if err != 0 or window is None:
        return "FAILED: no window in front."
    _, now = AX.AXUIElementCopyAttributeValue(window, "AXFullScreen", None)
    AX.AXUIElementSetAttributeValue(window, "AXFullScreen", not bool(now))
    from mint.tools import undo
    undo.record("window", f"{front.localizedName()} {'out of' if now else 'into'} full screen",
                {"kind": "window_fullscreen", "app": str(front.localizedName() or ""), "on": bool(now)})
    return f"{front.localizedName()} is {'out of' if now else 'in'} full screen."


def window(layout: str, app: str = "", app2: str = "") -> str:
    layout = str(layout or "").strip().lower().replace(" ", "_").replace("-", "_")
    layout = {"left": "left_half", "right": "right_half", "top": "top_half", "bottom": "bottom_half",
              "max": "maximize", "maximise": "maximize", "full": "maximize", "centre": "center",
              "side_by_side": "split", "split_screen": "split", "other_display": "next_display"}.get(layout, layout)
    if app2 or layout == "split":
        if not (app and app2):
            return "FAILED: side by side needs two apps (app and app2)."
        from mint.tools import undo
        with undo.together("window", f"{app} left and {app2} right"):
            first = window("left_half", app)
            second = window("right_half", app2)
        return f"{first} {second}"
    if layout in ("fullscreen", "full_screen", "native_fullscreen"):
        if app and not _activate(app):
            return f"FAILED: {app} is not running."
        return _fullscreen()
    if layout not in LAYOUTS:
        return f"FAILED: unknown layout '{layout}'. Layouts: {', '.join(LAYOUTS)}, fullscreen, split."
    name = ""
    if app:
        name = _activate(app)
        if not name:
            return f"FAILED: {app} is not running (open_app it first)."
    action, fractions = LAYOUTS[layout]
    before = window_place()
    if _rectangle():
        subprocess.run(["open", "-g", f"rectangle://execute-action?name={action}"], check=False)
        time.sleep(0.3)
        how = "Rectangle"
    elif fractions is not None and _place_ax(fractions):
        how = "Accessibility"
    else:
        return f"FAILED: '{layout}' needs Rectangle (not installed)."
    import AppKit
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if before:
        from mint.tools import undo
        undo.record("window", f"{before['app']} window → {layout.replace('_', ' ')}", before)
    return f"{name or (front.localizedName() if front else 'The window')} → {layout.replace('_', ' ')} ({how})."


def _ax_window(app_name: str = "", title: str = ""):
    """(app, window) through Accessibility: `app_name`'s window called `title`, else its focused one;
    the front app's by default."""
    import AppKit
    import ApplicationServices as AX
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    app = next((a for a in workspace.runningApplications() if str(a.localizedName() or "") == app_name), None) \
        if app_name else workspace.frontmostApplication()
    if app is None:
        return None, None
    element = AX.AXUIElementCreateApplication(app.processIdentifier())
    if title:
        _, windows = AX.AXUIElementCopyAttributeValue(element, "AXWindows", None)
        for candidate in windows or []:
            if str(AX.AXUIElementCopyAttributeValue(candidate, "AXTitle", None)[1] or "") == title:
                return app, candidate
    err, window = AX.AXUIElementCopyAttributeValue(element, "AXFocusedWindow", None)
    return app, (window if err == 0 else None)


def window_place(app_name: str = "", title: str = "") -> dict | None:
    """Where a window is now, as the undo step that puts it back there."""
    try:
        import ApplicationServices as AX
        app, window = _ax_window(app_name, title)
        if window is None:
            return None
        _, pos = AX.AXUIElementCopyAttributeValue(window, "AXPosition", None)
        _, size = AX.AXUIElementCopyAttributeValue(window, "AXSize", None)
        if pos is None or size is None:
            return None
        p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)[1]
        s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)[1]
        name = AX.AXUIElementCopyAttributeValue(window, "AXTitle", None)[1]
        return {"kind": "window_frame", "app": str(app.localizedName() or ""), "title": str(name or ""),
                "x": float(p.x), "y": float(p.y), "w": float(s.width), "h": float(s.height)}
    except Exception as error:
        log.info("window place: %s", error)
        return None


def put_window(spec: dict):
    """Move a window back to a saved place (undo); -> (what happened, the place it left, for redo)."""
    import ApplicationServices as AX
    import Quartz
    app, window = _ax_window(spec.get("app", ""), spec.get("title", ""))
    if window is None:
        return f"FAILED: {spec.get('app') or 'that'} window isn't open any more."
    now = window_place(spec.get("app", ""), spec.get("title", ""))
    size = AX.AXValueCreate(AX.kAXValueCGSizeType, Quartz.CGSize(spec["w"], spec["h"]))
    AX.AXUIElementSetAttributeValue(window, "AXSize", size)
    AX.AXUIElementSetAttributeValue(window, "AXPosition", AX.AXValueCreate(
        AX.kAXValueCGPointType, Quartz.CGPoint(spec["x"], spec["y"])))
    AX.AXUIElementSetAttributeValue(window, "AXSize", size)     # again: a move across displays can clamp it
    return f"{spec.get('app')} window back where it was.", now


def set_fullscreen(app_name: str, on: bool) -> str:
    import ApplicationServices as AX
    app, window = _ax_window(app_name)
    if window is None:
        return f"FAILED: {app_name or 'that'} window isn't open any more."
    AX.AXUIElementSetAttributeValue(window, "AXFullScreen", bool(on))
    return f"{app_name} {'back in' if on else 'out of'} full screen."


# --- Music, Night Shift, Wi-Fi, views, Settings --------------------------------------------

def music(action: str = "resume") -> str:
    """Resume whichever music app was used (Spotify or Music)."""
    import AppKit
    running = {str(a.localizedName()) for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()}
    player = "Spotify" if "Spotify" in running else "Music"
    verb = {"resume": "play", "play": "play", "pause": "pause", "next": "next track",
            "previous": "previous track", "last": "play"}.get(str(action).lower(), "play")
    ok, out = _osa(f'tell application "{player}" to {verb}', timeout=15)
    if not ok:
        return f"FAILED: {player} did not respond: {out[:120]}"
    ok, now = _osa(f'tell application "{player}" to if player state is playing then return (name of current track) & " - " & (artist of current track)', timeout=8)
    return f"{player}: {verb}." + (f" Now playing {now}." if ok and now else "")


def _night_status(client) -> dict:
    """CBBlueLightClient's status struct: enabled (bytes 1), schedule mode (4-8: 0 none, 1 sunset, 2 custom)."""
    import objc
    lib = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.dylib")
    lib.sel_registerName.restype = ctypes.c_void_p
    lib.sel_registerName.argtypes = [ctypes.c_char_p]
    lib.objc_msgSend.restype = ctypes.c_bool
    lib.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    buf = (ctypes.c_uint8 * 64)()
    lib.objc_msgSend(objc.pyobjc_id(client), lib.sel_registerName(b"getBlueLightStatus:"), ctypes.addressof(buf))
    raw = bytes(buf)
    return {"enabled": bool(raw[1]), "mode": int.from_bytes(raw[4:8], "little")}


def night_shift(state: str = "on") -> str:
    import objc
    from Foundation import NSBundle
    # (NSBundle: objc.loadBundle wrapped every class in the process and kept them - ~90-230 MB, 6 Oct)
    NSBundle.bundleWithPath_("/System/Library/PrivateFrameworks/CoreBrightness.framework").load()
    client = objc.lookUpClass("CBBlueLightClient").alloc().init()
    if str(state).lower() == "status":
        status = _night_status(client)
        schedule = {0: "no schedule", 1: "sunset to sunrise", 2: "a custom schedule"}.get(status["mode"], "a schedule")
        return f"Night Shift is {'on' if status['enabled'] else 'off'} ({schedule})."
    on = str(state).lower() not in ("off", "disable", "false")
    was = _night_status(client)["enabled"]
    ok = client.setEnabled_(on)
    if ok and was != on:
        from mint.tools import undo
        undo.record("night_shift", f"Night Shift {'on' if on else 'off'}", {"kind": "night_shift", "on": was})
    return f"Night Shift {'on' if on else 'off'}." if ok else "FAILED: Night Shift could not be switched."


def wifi(state: str = "on") -> str:
    done = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True)
    lines = done.stdout.splitlines()
    device = next((lines[i + 1].split(": ")[1] for i, line in enumerate(lines)
                   if line.strip() in ("Hardware Port: Wi-Fi", "Hardware Port: AirPort") and i + 1 < len(lines)), "en0")
    if str(state).lower() == "status":
        out = subprocess.run(["networksetup", "-getairportpower", device], capture_output=True, text=True).stdout
        return out.strip() or "Unknown."
    on = str(state).lower() not in ("off", "disable", "false")
    was = subprocess.run(["networksetup", "-getairportpower", device], capture_output=True, text=True).stdout
    subprocess.run(["networksetup", "-setairportpower", device, "on" if on else "off"], check=False)
    if was.strip().endswith(("On", "Off")) and was.strip().endswith("On") != on:
        from mint.tools import undo
        undo.record("wifi", f"Wi-Fi {'on' if on else 'off'}", {"kind": "wifi", "on": not on})
    return f"Wi-Fi {'on' if on else 'off'}."


def show(what: str) -> str:
    from mint.tools import fastinput
    what = str(what or "").lower().replace(" ", "_")
    if what in ("desktop", "show_desktop"):
        fastinput.press_key("f11", [])            # Show Desktop (standard shortcut)
        return "Showing the desktop (again to bring the windows back)."
    if what in ("mission_control", "all_windows"):
        subprocess.run(["open", "-a", "Mission Control"], check=False)
        return "Mission Control."
    if what in ("launchpad", "apps", "app_library"):
        subprocess.run(["open", "-a", "Launchpad"], check=False) if os.path.exists(
            "/System/Applications/Launchpad.app") else subprocess.run(["open", "-a", "Apps"], check=False)
        return "Showing the apps."
    if what in ("hide_others", "hide_other_apps"):
        fastinput.press_key("h", ["command", "option"])
        return "Hid the other apps."
    if what in ("minimize", "minimise"):
        fastinput.press_key("m", ["command"])
        return "Minimised the front window."
    return f"FAILED: unknown view '{what}'. desktop, mission_control, launchpad, hide_others, minimize."


def settings(pane: str) -> str:
    key = str(pane or "").lower().strip().replace(" ", "_").replace("-", "_")
    ident = SETTINGS.get(key) or next((v for k, v in SETTINGS.items() if key and (key in k or k in key)), None)
    if ident is None:
        subprocess.run(["open", "-a", "System Settings"], check=False)
        return f"Opened System Settings (no page called '{pane}'; pages: {', '.join(SETTINGS)})."
    subprocess.run(["open", f"x-apple.systempreferences:{ident}"], check=False)
    return f"Opened Settings ▸ {key.replace('_', ' ').title()}."


# --- The tool ------------------------------------------------------------------------------

PROMPT = """Mac switches: brightness, keep awake, Do Not Disturb, window layouts (halves, thirds, quarters, \
maximise, full screen, two apps side by side, other display - Rectangle does it when installed), resume music, \
Night Shift, Wi-Fi, show desktop / Mission Control, a Settings page -> the `mac` tool. Volume and mute: \
set_volume / system_action. Screenshots: screenshot. Screen recordings: screen_record."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="mac",
        description=("The Mac's own switches: brightness (up/down/0-100), keep_awake (on with minutes, off, status), "
                     "focus (Do Not Disturb on/off), window (layout of the front window or of `app`: left_half, "
                     "right_half, top_half, bottom_half, top_left/top_right/bottom_left/bottom_right, left_third, "
                     "center_third, right_third, left_two_thirds, right_two_thirds, maximize, almost_maximize, "
                     "center, restore, fullscreen, next_display, larger, smaller; split = `app` left and `app2` "
                     "right), music (resume/pause/next/previous in Spotify or Music), night_shift (on/off), wifi "
                     "(on/off/status), show (desktop, mission_control, launchpad, hide_others, minimize), settings "
                     "(open a System Settings page: displays, sound, wifi, bluetooth, battery, notifications, focus, "
                     "privacy, keyboard, trackpad, general, appearance, wallpaper, network, storage...)."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "control": types.Schema(type=S, enum=["brightness", "keep_awake", "focus", "window", "music",
                                                  "night_shift", "wifi", "show", "settings"]),
            "value": types.Schema(type=S, description="brightness: up/down/number; keep_awake/focus/night_shift/"
                                                      "wifi: on/off/status; window: the layout; music: resume/"
                                                      "pause/next/previous; show: what; settings: the page"),
            "minutes": types.Schema(type=types.Type.NUMBER, description="keep_awake: how long (0 = until told)"),
            "app": types.Schema(type=S, description="window: which app's window (default the front one)"),
            "app2": types.Schema(type=S, description="window split: the app for the right side")},
            required=["control"]))]


def tool(args: dict) -> str:
    control = str(args.get("control") or "").lower()
    value = str(args.get("value") or "")
    try:
        if control == "brightness":
            return brightness(value)
        if control == "keep_awake":
            return keep_awake(value or "on", float(args.get("minutes") or 0))
        if control == "focus":
            return focus(value or "on")
        if control == "window":
            return window(value, str(args.get("app") or ""), str(args.get("app2") or ""))
        if control == "music":
            return music(value or "resume")
        if control == "night_shift":
            return night_shift(value or "on")
        if control == "wifi":
            return wifi(value or "status")
        if control == "show":
            return show(value)
        if control == "settings":
            return settings(value)
    except Exception as error:
        log.exception("mac %s failed", control)
        return f"FAILED: {control}: {error}"
    return f"FAILED: unknown control '{control}'."


HANDLERS = {"mac": tool}
