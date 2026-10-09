"""Connectors: the apps and services Mint can work with, what each one enables, and whether it is
set up on this Mac.

This module describes and links what already exists; it does not do the work itself:

    Google / iCloud / Microsoft   macOS Internet Accounts (accounts.py) -> Mail and Calendar
    Mail, Calendar, Reminders     mailtriage.py, skills.py (EventKit), apple_apps.py when present
    Notes, Contacts, Photos,      skills.py, apple_apps.py (notes, contacts, photos, maps, safari, iwork)
      Maps, Safari, iWork
    Apple Shortcuts, Image        apple_shortcuts.py, shortcut_library.py, shortcut_maker.py, imagegen.py
      Playground
    Telegram                      telegram.py (a bot on the user's phone)
    ChatGPT / Claude apps         agentapps.py
    Music / Spotify               music.py (another module; its which_app() gives the status)
    Slack, browsers               apps.open_slack, the browser tool
    Model providers               settings_models.py (API keys)

Every connector has: id, name, icon (an SF Symbol), what it enables (with example requests), how it
connects (built in, a macOS account, app scripting, a URL scheme, Shortcuts, the browser, an API key),
status() (connected / installed but not set up / not installed - cheap: bundle ids, permission checks
that never prompt), connect() (what to do next, done for the user where a Mac app may: open System
Settings ▸ Internet Accounts, ask for Automation, add a shortcut) and, for many, a read-only test().

"All my apps": every other installed app with a scripting dictionary is listed as "scriptable" -
connector_maker.py can make a connector for it from plain words ("integrate Things"). The connectors
it makes are JSON files in ~/Library/Application Support/Mint/connectors/<id>.json.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import plistlib
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("mint.tools.connectors")

SUPPORT = Path.home() / "Library" / "Application Support" / "Mint"
CUSTOM_DIR = SUPPORT / "connectors"
APP_DIRS = [Path("/Applications"), Path.home() / "Applications", Path("/System/Applications")]
INTERNET_ACCOUNTS = "x-apple.systempreferences:com.apple.Internet-Accounts-Settings.extension"
PRIVACY = "x-apple.systempreferences:com.apple.preference.security?Privacy_"

HOW = {"builtin": "Built into Mint", "account": "macOS account", "scripting": "App scripting",
       "url": "URL scheme", "shortcuts": "Shortcuts", "browser": "Browser", "key": "API key"}
STATES = {"connected": "Connected", "setup": "Not set up", "missing": "Not installed",
          "scriptable": "Scriptable: Mint can make a connector"}

# AEDeterminePermissionToAutomateTarget results
AE_OK, AE_DENIED, AE_WOULD_ASK, AE_NOT_RUNNING = 0, -1743, -1744, -600


# --- Installed apps ----------------------------------------------------------------------------------

_apps: dict = {"at": 0.0, "by_id": {}, "scriptable": None}
_apps_lock = threading.Lock()


def _info(app: Path) -> dict | None:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as handle:
            info = plistlib.load(handle)
    except Exception:
        return None
    bundle = str(info.get("CFBundleIdentifier") or "")
    if not bundle:
        return None
    schemes = [str(s) for t in (info.get("CFBundleURLTypes") or []) if isinstance(t, dict)
               for s in (t.get("CFBundleURLSchemes") or [])]
    return {"bundle_id": bundle, "path": str(app),
            "name": str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or app.stem) or app.stem,
            "file_name": app.stem,
            "script_flag": bool(info.get("NSAppleScriptEnabled")) or bool(info.get("OSAScriptingDefinition")),
            "schemes": schemes}


def _web_app(bundle_id: str) -> bool:
    """A browser's web-app shim (~/Applications/Chrome Apps.localized/…): not an app of its own."""
    return bool(re.match(r"(com\.google\.Chrome|com\.brave\.Browser|com\.microsoft\.edgemac|com\.apple\.Safari)"
                         r"(\.beta|\.dev|\.canary)?\.(app|WebApp)\.", bundle_id))


def installed_apps(fresh: bool = False) -> dict[str, dict]:
    """bundle id -> {bundle_id, path, name, file_name, script_flag, schemes} for the apps in /Applications,
    ~/Applications and /System/Applications (and one folder down: Utilities, vendor folders)."""
    with _apps_lock:
        if not fresh and _apps["by_id"] and time.time() - _apps["at"] < 300:
            return _apps["by_id"]
    found: dict[str, dict] = {}
    for folder in APP_DIRS:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            apps = [entry] if entry.suffix == ".app" else (
                sorted(p for p in entry.iterdir() if p.suffix == ".app") if entry.is_dir() and not
                entry.name.startswith(".") else [])
            for app in apps:
                info = _info(app)
                if info and _web_app(info["bundle_id"]):
                    continue
                if info and info["bundle_id"] not in found:
                    found[info["bundle_id"]] = info
    with _apps_lock:
        _apps.update(at=time.time(), by_id=found, scriptable=None)
    return found


def app_for(bundles) -> dict | None:
    """The first installed app among `bundles` (a bundle id or several). Falls back to NSWorkspace for apps
    outside the scanned folders (Finder lives in CoreServices)."""
    if isinstance(bundles, str):
        bundles = (bundles,)
    apps = installed_apps()
    for bundle in bundles:
        if bundle in apps:
            return apps[bundle]
    for bundle in bundles:
        try:
            import AppKit
            url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle)
        except Exception:
            url = None
        if url is not None:
            path = Path(str(url.path()))
            if path.exists() and "/Volumes/" not in str(path) and "/.Trash/" not in str(path):
                info = _info(path)
                if info:
                    with _apps_lock:
                        _apps["by_id"].setdefault(info["bundle_id"], info)
                    return info
    return None


def find_app(words: str) -> dict | None:
    """An installed app by the user's name for it ("things", "vs code", "spotify")."""
    import difflib
    wanted = " ".join(re.sub(r"[^a-z0-9+ ]", " ", words.lower()).split())
    if not wanted:
        return None
    aliases = {"vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
               "things": "things3", "things 3": "things3", "chrome": "google chrome", "outlook": "microsoft outlook",
               "word": "microsoft word", "excel": "microsoft excel", "powerpoint": "microsoft powerpoint",
               "teams": "microsoft teams", "iterm": "iterm", "facetime": "facetime", "apple music": "music"}
    wanted = aliases.get(wanted, wanted)
    apps = list(installed_apps().values())
    extra = app_for(("com.apple.finder",))
    if extra and extra not in apps:
        apps.append(extra)

    def names(app):
        return {app["name"].lower(), app["file_name"].lower(), app["name"].lower().replace(" ", "")}
    for test in (lambda n: wanted in n and n == wanted, lambda n: n.replace(" ", "") == wanted.replace(" ", ""),
                 lambda n: n.startswith(wanted + " ") or n.startswith(wanted), lambda n: wanted in n):
        hits = [a for a in apps if any(test(n) for n in names(a))]
        if hits:
            return sorted(hits, key=lambda a: (a["path"].startswith("/System"), len(a["name"])))[0]
    close = difflib.get_close_matches(wanted, [a["name"].lower() for a in apps], n=1, cutoff=0.88)
    if close:
        return next(a for a in apps if a["name"].lower() == close[0])
    return None


# --- Scripting dictionaries ---------------------------------------------------------------------------

_osa_fn = None


def _osa():
    global _osa_fn
    if _osa_fn is None:
        osa = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/Carbon.framework/Frameworks/OpenScripting.framework/OpenScripting")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        fn = osa.OSACopyScriptingDefinitionFromURL
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p)]
        fn.restype = ctypes.c_int32
        cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _osa_fn = (fn, cf)
    return _osa_fn


def scripting_definition(path: str) -> str | None:
    """The app's sdef XML, or None if it has none. OpenScripting's OSACopyScriptingDefinitionFromURL (what the
    `sdef` tool uses; `sdef` itself needs a full Xcode), then `sdef` as a fallback."""
    try:
        import objc
        from Foundation import NSURL
        fn, cf = _osa()
        url = NSURL.fileURLWithPath_(str(path))
        out = ctypes.c_void_p()
        if fn(objc.pyobjc_id(url), 0, ctypes.byref(out)) == 0 and out.value:
            try:
                data = ctypes.string_at(cf.CFDataGetBytePtr(out), cf.CFDataGetLength(out))
            finally:
                cf.CFRelease(out)
            text = data.decode("utf-8", "replace")
            if "<dictionary" in text:
                return text
    except Exception as error:
        log.info("sdef %s: %s", path, error)
    try:
        done = subprocess.run(["sdef", str(path)], capture_output=True, text=True, timeout=10)
        if done.returncode == 0 and "<dictionary" in done.stdout:
            return done.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def is_scriptable(app: dict) -> bool:
    return bool(app.get("script_flag")) or scripting_definition(app["path"]) is not None


# --- Permission checks that never prompt --------------------------------------------------------------

class _AEDesc(ctypes.Structure):
    _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]


_cs = None


def _fourcc(text: str) -> int:
    return int.from_bytes(text.encode(), "big")


_stuck: set = set()          # bundles whose permission check never came back (macOS hangs on some quit apps)
AUTOMATION_WAIT = 3.0


def automation(bundle_id: str, ask: bool = False) -> int:
    """AEDeterminePermissionToAutomateTarget: 0 allowed, -1743 denied, -1744 macOS would ask, -600 the app
    isn't running (unknown). ask=True shows macOS's prompt (blocks until answered; not on the main thread).
    Without ask it gives up after 3 s and says unknown: for an app that was just quit (Spotify) macOS can hang in
    there for good, which froze Settings' apps list and the library."""
    if ask:
        return _automation(bundle_id, True)
    if bundle_id in _stuck:
        return AE_NOT_RUNNING
    out: list = []
    done = threading.Event()

    def run():
        out.append(_automation(bundle_id, False))
        _stuck.discard(bundle_id)
        done.set()
    _stuck.add(bundle_id)
    threading.Thread(target=run, daemon=True, name="automation-check").start()
    if not done.wait(AUTOMATION_WAIT):
        log.info("automation %s: no answer from macOS", bundle_id)
        return AE_NOT_RUNNING
    return out[0]


def _automation(bundle_id: str, ask: bool) -> int:
    global _cs
    try:
        if _cs is None:
            cs = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreServices.framework/CoreServices")
            cs.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_AEDesc)]
            cs.AEDeterminePermissionToAutomateTarget.argtypes = [ctypes.POINTER(_AEDesc), ctypes.c_uint32,
                                                                 ctypes.c_uint32, ctypes.c_bool]
            cs.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
            cs.AEDisposeDesc.argtypes = [ctypes.POINTER(_AEDesc)]
            _cs = cs
        desc, raw = _AEDesc(), bundle_id.encode()
        _cs.AECreateDesc(_fourcc("bund"), raw, len(raw), ctypes.byref(desc))
        try:
            return int(_cs.AEDeterminePermissionToAutomateTarget(ctypes.byref(desc), _fourcc("****"),
                                                                  _fourcc("****"), bool(ask)))
        finally:
            _cs.AEDisposeDesc(ctypes.byref(desc))
    except Exception as error:
        log.info("automation %s: %s", bundle_id, error)
        return AE_NOT_RUNNING


def _objc_class(framework: str, name: str):
    import objc
    try:
        return objc.lookUpClass(name)
    except objc.nosuchclass_error:
        # NSBundle, not objc.loadBundle (that wraps every class there is: ~90-230 MB kept, 6 Oct)
        from Foundation import NSBundle
        NSBundle.bundleWithPath_(f"/System/Library/Frameworks/{framework}.framework").load()
        return objc.lookUpClass(name)


def privacy_status(kind: str) -> int:
    """0 not asked yet, 1 restricted, 2 denied, 3 allowed, 4 limited / write-only; -1 unknown.
    kind: calendars, reminders, contacts, photos."""
    try:
        if kind in ("calendars", "reminders"):
            import EventKit
            return int(EventKit.EKEventStore.authorizationStatusForEntityType_(0 if kind == "calendars" else 1))
        if kind == "contacts":
            return int(_objc_class("Contacts", "CNContactStore").authorizationStatusForEntityType_(0))
        if kind == "photos":
            return int(_objc_class("Photos", "PHPhotoLibrary").authorizationStatusForAccessLevel_(2))
    except Exception as error:
        log.info("privacy %s: %s", kind, error)
    return -1


def request_privacy(kind: str) -> str:
    """Ask macOS for Calendars / Reminders / Contacts / Photos (its own prompt, once); if it was turned off,
    open that Privacy & Security page. Blocks up to a minute; call off the main thread."""
    status = privacy_status(kind)
    pane = {"calendars": "Calendars", "reminders": "Reminders", "contacts": "Contacts", "photos": "Photos"}[kind]
    if status in (3, 4):
        return f"{pane}: already allowed."
    if status in (1, 2):
        open_url(PRIVACY + pane)
        return f"{pane} access is off for Mint. Opened Privacy & Security ▸ {pane}: switch Mint on there."
    try:
        if kind in ("calendars", "reminders"):
            import EventKit

            from mint.tools import everyday as skills
            _store, problem = skills._event_store(EventKit.EKEntityTypeEvent if kind == "calendars"
                                                  else EventKit.EKEntityTypeReminder)
            return problem or f"{pane}: allowed."
        import objc
        done, result = threading.Event(), {"ok": False}
        if kind == "contacts":
            objc.registerMetaDataForSelector(b"CNContactStore", b"requestAccessForEntityType:completionHandler:", {
                "arguments": {3: {"callable": {"retval": {"type": b"v"}, "arguments": {
                    0: {"type": b"^v"}, 1: {"type": b"Z"}, 2: {"type": b"@"}}}}}})
            store = _objc_class("Contacts", "CNContactStore").alloc().init()
            store.requestAccessForEntityType_completionHandler_(
                0, lambda ok, error: (result.update(ok=bool(ok)), done.set()))
        else:
            cls = _objc_class("Photos", "PHPhotoLibrary")
            objc.registerMetaDataForSelector(b"PHPhotoLibrary", b"requestAuthorizationForAccessLevel:handler:", {
                "arguments": {3: {"callable": {"retval": {"type": b"v"}, "arguments": {
                    0: {"type": b"^v"}, 1: {"type": b"q"}}}}}})
            cls.requestAuthorizationForAccessLevel_handler_(
                2, lambda status: (result.update(ok=int(status) in (3, 4)), done.set()))
        done.wait(60)
        return f"{pane}: allowed." if result["ok"] else f"{pane}: not allowed (approve macOS's prompt and try again)."
    except Exception as error:
        log.info("request %s: %s", kind, error)
        open_url(PRIVACY + pane)
        return f"Opened Privacy & Security ▸ {pane}: switch Mint on there."


def running(bundle_id: str) -> bool:
    try:
        import AppKit
        return bool(AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id))
    except Exception:
        return False


def ask_automation(bundle_id: str, name: str) -> str:
    """Let macOS ask once whether Mint may control `name` (it asks only while the app runs, so it is opened in
    the background first). Off the main thread."""
    code = automation(bundle_id)
    if code == AE_OK:
        return f"{name}: Mint may already control it."
    if code == AE_DENIED:
        open_url(PRIVACY + "Automation")
        return (f"Controlling {name} is turned off for Mint. Opened Privacy & Security ▸ Automation: switch "
                f"{name} on under Mint.")
    if not running(bundle_id):
        subprocess.run(["open", "-g", "-j", "-b", bundle_id], capture_output=True, timeout=15)
        for _ in range(40):
            if running(bundle_id):
                break
            time.sleep(0.25)
        time.sleep(0.8)
    code = automation(bundle_id, ask=True)
    if code == AE_OK:
        return f"{name}: allowed. Mint can use it now."
    if code == AE_DENIED:
        return (f"{name}: not allowed. To change it: System Settings ▸ Privacy & Security ▸ Automation ▸ Mint.")
    return f"{name}: macOS didn't answer (code {code}); try again with {name} open."


# --- Small helpers -------------------------------------------------------------------------------------

def open_url(url: str) -> None:
    try:
        import AppKit
        AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(url))
    except Exception:
        subprocess.Popen(["open", url])


def osascript(script: str, timeout: float = 20) -> tuple[bool, str]:
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"it took more than {int(timeout)} s"
    except OSError as error:
        return False, str(error)
    if done.returncode != 0:
        err = (done.stderr or "").strip()
        if "-1743" in err or "Not authorized" in err:
            return False, "macOS didn't allow Mint to control that app (Privacy & Security ▸ Automation)"
        return False, err[:300] or "the script failed"
    return True, done.stdout.strip()


def _optional(module: str):
    """A Mint module that may not exist yet (apple_apps, music: other sessions build them)."""
    try:
        import importlib
        return importlib.import_module(f"{__package__ or 'mint'}.{module}")
    except Exception:
        return None


def optional_tools() -> set[str]:
    """Tool names from apple_apps.py and music.py, when those modules exist."""
    found: set[str] = set()
    for name in ("apple_apps", "music"):
        module = _optional(name)
        if module is not None:
            found |= set(getattr(module, "HANDLERS", {}) or {})
    return found


# --- The library ----------------------------------------------------------------------------------------

@dataclass
class Connector:
    id: str
    name: str
    icon: str
    enables: str
    examples: tuple = ()
    how: tuple = ("builtin",)
    bundles: tuple = ()                  # its app(s); empty = no app needed (an account, a bridge)
    uses: tuple = ()                     # the Mint tools that do the work
    optional_tools: tuple = ()           # tools from modules other sessions build (apple_apps.py, music.py),
                                         # listed when that module is present
    site: str = ""                       # where to get it when it isn't installed
    settings_page: str = ""              # its settings live on another Settings page
    makeable: bool = False               # connector_maker can add actions for it
    check: Callable | None = None        # (connector, deep) -> (state, detail)
    do_connect: Callable | None = None   # (connector) -> words
    do_test: Callable | None = None      # (connector) -> words
    group: str = ""
    extra: dict = field(default_factory=dict)

    # --- status ---
    def app(self) -> dict | None:
        return app_for(self.bundles) if self.bundles else None

    def status(self, deep: bool = False) -> dict:
        """-> {state: connected|setup|missing, detail}. deep=True may ask running apps (osascript, a few
        seconds at most); the default only reads bundle ids and permission states."""
        try:
            state, detail = (self.check or _app_status)(self, deep)
        except Exception as error:
            log.info("status %s: %s", self.id, error)
            state, detail = ("setup", f"Couldn't check ({str(error)[:60]})")
        return {"state": state, "detail": detail}

    def connect(self) -> str:
        """Do what can be done from here and say what the user does next. Off the main thread (it may wait
        for macOS's permission prompt)."""
        if self.do_connect:
            return self.do_connect(self)
        app = self.app()
        if self.bundles and app is None:
            if self.site:
                open_url(self.site)
                return f"{self.name} isn't installed. Opened {self.site} to get it."
            return f"{self.name} isn't installed on this Mac."
        if "scripting" in self.how and app:
            return ask_automation(app["bundle_id"], app["name"])
        if self.settings_page:
            return f"Set it up in Settings ▸ {_PAGE_TITLES.get(self.settings_page, self.settings_page)}."
        return f"{self.name}: nothing to set up - Mint can use it now."

    def test(self) -> str:
        """A read-only check that it really works: its own test, else check_basics."""
        if self.do_test:
            return self.do_test(self)
        return check_basics(self)

    @property
    def testable(self) -> bool:
        return True

    def tools(self) -> list[str]:
        have = optional_tools()
        return list(self.uses) + [t for t in self.optional_tools if t in have]


_PAGE_TITLES = {"accounts": "Accounts & connections", "models": "Models & agents", "apple_shortcuts": "Shortcuts"}


def _app_status(c: Connector, deep: bool) -> tuple[str, str]:
    """Installed? Then, for scripting, whether macOS lets Mint control it (without asking)."""
    if not c.bundles:
        return "connected", "Ready"
    app = c.app()
    if app is None:
        return "missing", "Not installed"
    if "scripting" in c.how:
        code = automation(app["bundle_id"])
        if code == AE_DENIED:
            return "setup", "Automation is off for Mint"
        if code == AE_WOULD_ASK:
            return "setup", "Needs permission once"
    if c.makeable and not _custom_for(app["bundle_id"]):
        return "setup", "Can make a connector"
    custom = _custom_for(app["bundle_id"])
    if custom:
        return "connected", f"Your connector: {len(custom.get('actions') or [])} actions"
    return "connected", app["name"] if app["name"] != c.name else "Ready"


def _scheme_owner(scheme: str) -> str:
    try:
        import AppKit
        url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(
            AppKit.NSURL.URLWithString_(f"{scheme}://"))
        return str(url.path()) if url is not None else ""
    except Exception:
        return ""


def check_basics(c: "Connector", owner: Callable[[str], str] = _scheme_owner,
                 control: Callable[[str], int] | None = None, app: dict | None = None) -> str:
    """The test for a connector without one of its own - nothing is run in the app, nothing opens: the app is
    installed, macOS lets Mint control it (for scripting), its links open it, and its live status."""
    control = control or automation
    live = c.status(deep=True)
    status = f"{STATES[live['state']]} - {live['detail']}"
    waiting = live["state"] != "connected"
    if not c.bundles:
        return f"Not ready yet: {live['detail']}." if waiting else f"Checked: {status}."
    app = app or c.app()
    if app is None:
        return f"Didn't work: {c.name} isn't installed on this Mac."
    found = [f"{app['name'].strip(chr(0x200e))} is installed"]
    problem = ""
    if "scripting" in c.how:
        code = control(app["bundle_id"])
        if code == AE_DENIED:
            problem = "macOS has Automation off for Mint and this app (Privacy & Security ▸ Automation)"
        elif code == AE_WOULD_ASK:
            problem = "macOS will ask once whether Mint may control it - press Connect"
        else:
            found.append("macOS lets Mint control it" if code == AE_OK else "Mint can control it while it's open")
    schemes = [x for x in app.get("schemes") or [] if x.lower() not in ("http", "https", "file")]
    if "url" in c.how and schemes:
        path = owner(schemes[0])
        if path and Path(path).resolve() == Path(app["path"]).resolve():
            found.append(f"its {schemes[0]}: links open it")
        else:
            problem = problem or f"its {schemes[0]}: links don't open {app['name']}"
    if problem:
        return f"Didn't work: {problem}."
    if waiting:
        return "Not ready yet: " + ", ".join(found) + f", but: {live['detail']}."
    return "Checked: " + ", ".join(found) + f" ({status})."


def _custom_for(bundle_id: str) -> dict | None:
    for item in custom_connectors():
        if item.get("bundle_id") == bundle_id:
            return item
    return None


def _script_test(script: str, shape: Callable[[str], str] = lambda s: s):
    def test(c: Connector) -> str:
        app = c.app()
        if c.bundles and app is None:
            return f"{c.name} isn't installed."
        ok, out = osascript(script)
        return shape(out) if ok else f"Didn't work: {out}"
    return test


def _privacy_check(kind: str):
    def check(c: Connector, deep: bool) -> tuple[str, str]:
        if c.bundles and c.app() is None:
            return "missing", "Not installed"
        status = privacy_status(kind)
        label = kind.capitalize()
        if status == 3:
            return "connected", "Allowed"
        if status == 4:
            return (("setup", "Write-only: allow full access") if kind in ("calendars", "reminders")
                    else ("connected", "Limited access"))
        if status in (1, 2):
            return "setup", f"{label} access is off for Mint"
        if status == 0:
            return "setup", f"Allow {label} once"
        return "connected", "Ready"
    return check


def _privacy_connect(kind: str):
    return lambda c: request_privacy(kind)


# Accounts ------------------------------------------------------------------------------------------

def _calendar_sources() -> list[tuple[str, int]] | None:
    """[(source title, source type)] Calendar syncs; None without Calendar access."""
    if privacy_status("calendars") not in (3, 4):
        return None
    try:
        import EventKit

        from mint.tools import everyday as skills
        store, _ = skills._event_store(EventKit.EKEntityTypeEvent)
        if store is None:
            return None
        return [(str(s.title() or ""), int(s.sourceType())) for s in store.sources() or []]
    except Exception:
        return None


def _mail_accounts(deep: bool) -> list[str] | None:
    if not deep:
        return None
    try:
        from mint.tools import accounts
        return accounts.mail_accounts()
    except Exception:
        return None


def _google_check(c: Connector, deep: bool) -> tuple[str, str]:
    sources = _calendar_sources()
    google = [t for t, kind in (sources or []) if kind == 2 and ("gmail" in t.lower() or "google" in t.lower()
                                                                  or t.lower().endswith(".com"))]
    mail = [a for a in (_mail_accounts(deep) or []) if "google" in a.lower() or "gmail" in a.lower()]
    found = sorted(set(google) | set(mail))
    if found:
        return "connected", f"{len(found)} Google account{'s' * (len(found) != 1)}"
    if sources is None:
        return "setup", "Allow Calendars to check"
    return "setup", "No Google account in Internet Accounts"


def _icloud_check(c: Connector, deep: bool) -> tuple[str, str]:
    drive = (Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs").is_dir()
    sources = _calendar_sources() or []
    calendars = any(t.lower() == "icloud" for t, _ in sources)
    if drive or calendars:
        return "connected", " + ".join(x for x, on in (("iCloud Drive", drive), ("Calendars", calendars)) if on)
    return "setup", "Sign in with your Apple Account"


def _microsoft_check(c: Connector, deep: bool) -> tuple[str, str]:
    words = ("outlook", "hotmail", "live.com", "office", "microsoft", "exchange")
    sources = [t for t, kind in (_calendar_sources() or []) if kind == 1 or any(w in t.lower() for w in words)]
    mail = [a for a in (_mail_accounts(deep) or []) if any(w in a.lower() for w in words)]
    outlook = app_for(("com.microsoft.Outlook",))
    if sources or mail:
        return "connected", f"{len(set(sources) | set(mail))} Microsoft account(s)"
    if outlook:
        return "connected", "Outlook app"
    return "setup", "Add in Internet Accounts"


def _accounts_connect(which: str):
    def connect(c: Connector) -> str:
        open_url(INTERNET_ACCOUNTS)
        return (f"Opened System Settings ▸ Internet Accounts: add {which} and turn on Mail and Calendars. macOS keeps "
                "it in sync; Mint works through Mail and Calendar on this Mac.")
    return connect


def _accounts_test(c: Connector) -> str:
    from mint.tools import accounts
    return accounts.summary()


# Special statuses -------------------------------------------------------------------------------------

def _music_check(c: Connector, deep: bool) -> tuple[str, str]:
    """Installed / allowed as usual; with music.py present, its which_app() says which player Mint uses."""
    state, detail = _app_status(c, deep)
    module = _optional("music")
    which = getattr(module, "which_app", None) if module else None
    player = "Spotify" if c.id == "spotify" else "Music"
    if state == "missing" or not callable(which):
        return state, detail
    if not deep:
        return state, "Open" if running(c.bundles[0]) else detail
    try:
        words = str(which() or "")          # "Spotify: open, paused; Music: installed, not open. Music commands go to X"
    except Exception as error:
        log.info("music.which_app: %s", error)
        return state, detail
    mine = re.search(rf"\b{player}: ([^;.]+)", words)
    chosen = re.search(r"commands go to (\w+)", words)
    bits = [mine.group(1).strip()] if mine else []
    if chosen and chosen.group(1) == player:
        bits.insert(0, "Mint's player")
    words = ", ".join(bits)[:40]
    return state, (words[:1].upper() + words[1:]) if words else detail


def _telegram_check(c: Connector, deep: bool) -> tuple[str, str]:
    try:
        from mint.app import telegram
        state = telegram.status()
    except Exception as error:
        return "setup", f"Couldn't check ({str(error)[:40]})"
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return "setup", "Add a bot token"
    if not state.get("enabled"):
        return "setup", "Remote control is off"
    if not state.get("paired"):
        return "setup", "Pair your phone"
    return "connected", f"Paired with {state['paired'].get('name', 'your phone')}"


def _shortcuts_check(c: Connector, deep: bool) -> tuple[str, str]:
    if c.app() is None:
        return "missing", "Not installed"
    from mint.tools import shortcuts as apple_shortcuts
    from mint.tools import shortcut_library
    have = set(apple_shortcuts._cache["names"]) if not deep and apple_shortcuts._cache["at"] else \
        shortcut_library.installed(fresh=False)
    if not have and not deep:
        return "connected", "Ready"
    missing = [n for n in shortcut_library.mint_names() if n not in have]
    theirs = len([n for n in have if n not in shortcut_library.mint_names()])
    if missing:
        return "setup", f"{len(missing)} of Mint's shortcuts to add"
    return "connected", f"{theirs} of your shortcuts"


def _playground_check(c: Connector, deep: bool) -> tuple[str, str]:
    if c.app() is None:
        return "missing", "Not on this Mac"
    from mint.tools import shortcuts as apple_shortcuts
    from mint.tools import shortcut_library
    have = set(apple_shortcuts._cache["names"]) if not deep and apple_shortcuts._cache["at"] else \
        shortcut_library.installed(fresh=False)
    draw = shortcut_library.registry()[0]["name"]
    if have and draw not in have:
        return "setup", "Add Mint's drawing shortcut"
    return "connected", "Ready" if have else "Ready (via Mint's shortcut)"


def _playground_connect(c: Connector) -> str:
    from mint.tools import shortcut_library
    said = shortcut_library.offer(shortcut_library.registry()[0]["name"])
    return ("Opened Mint's drawing shortcut in Shortcuts: click “Add Shortcut”." if said.startswith("NOT DONE")
            else said.removeprefix("FAILED: "))


def _shortcuts_connect(c: Connector) -> str:
    from mint.tools import shortcut_library
    said = shortcut_library.offer_missing()
    return ("Opened Mint's shortcuts in Shortcuts: click “Add Shortcut” for each." if said.startswith("NOT DONE")
            else said.removeprefix("FAILED: "))


def _shortcuts_test(c: Connector) -> str:
    from mint.tools import shortcuts as apple_shortcuts
    return f"{len(apple_shortcuts.names(fresh=True))} shortcuts in the Shortcuts app."


def _keys_check(c: Connector, deep: bool) -> tuple[str, str]:
    envs = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
            "XAI_API_KEY")
    labels = {"GEMINI": "Gemini", "OPENAI": "OpenAI", "ANTHROPIC": "Anthropic", "OPENROUTER": "OpenRouter",
              "GROQ": "Groq", "XAI": "xAI"}
    have = [labels[e.split("_")[0]] for e in envs if os.environ.get(e)]
    if not have:
        return "setup", "No keys yet"
    return "connected", ", ".join(have)


def _meetings_check(c: Connector, deep: bool) -> tuple[str, str]:
    have = [n for n, ids in (("Zoom", ("us.zoom.xos",)), ("Teams", ("com.microsoft.teams2", "com.microsoft.teams")))
            if app_for(ids)]
    return "connected", " + ".join(have + ["Meet in the browser"])


def _slack_check(c: Connector, deep: bool) -> tuple[str, str]:
    state, detail = _app_status(c, deep)
    try:
        from mint.core import custom
        spaces = custom.get().get("slack_workspaces") or {}
    except Exception:
        spaces = {}
    if state == "missing":
        return "connected", "In the browser (app not installed)"
    return state, f"{len(spaces)} workspace name{'s' * (len(spaces) != 1)} set" if spaces else "Desktop app"


def _ai_apps_check(c: Connector, deep: bool) -> tuple[str, str]:
    have = [n for n, b in (("ChatGPT", ("com.openai.codex", "com.openai.chat")),
                           ("Claude", ("com.anthropic.claudefordesktop",))) if app_for(b)]
    return ("connected", " + ".join(have)) if have else ("missing", "Neither app installed")


def _any_installed(names_ids: tuple):
    def check(c: Connector, deep: bool) -> tuple[str, str]:
        have = [n for n, b in names_ids if app_for(b)]
        if not have:
            return "missing", "Not installed"
        state, detail = _app_status(Connector(c.id, c.name, c.icon, c.enables, how=c.how,
                                              bundles=tuple(b for n, b in names_ids if app_for(b))), deep)
        return state, " + ".join(have) if state == "connected" else detail
    return check


def _messages_test(c: Connector) -> str:
    ok, out = osascript('tell application "Messages" to count chats')
    return f"Messages: {out} conversations (read-only)." if ok else f"Didn't work: {out}"


def _calendar_test(c: Connector) -> str:
    sources = _calendar_sources()
    if sources is None:
        return "Calendar access isn't allowed for Mint yet."
    from mint.tools import everyday as skills
    return skills.calendar_events(1)[:300]


def _reminders_test(c: Connector) -> str:
    if privacy_status("reminders") not in (3, 4):
        return "Reminders access isn't allowed for Mint yet."
    import EventKit

    from mint.tools import everyday as skills
    store, problem = skills._event_store(EventKit.EKEntityTypeReminder)
    if store is None:
        return problem or "No access."
    lists = store.calendarsForEntityType_(EventKit.EKEntityTypeReminder) or []
    return f"{len(lists)} reminder lists."


def _contacts_test(c: Connector) -> str:
    if privacy_status("contacts") not in (3, 4):
        return "Contacts access isn't allowed for Mint yet."
    ok, out = osascript('tell application "Contacts" to count people')
    return f"{out} contacts." if ok else f"Didn't work: {out}"


L = Connector
LIBRARY: list[Connector] = [
    # Accounts
    L("google", "Google (Gmail + Calendar)", "at.circle", "Read, triage and draft Gmail in Mail, and plan in Google "
      "Calendar - all through macOS, no sign-in with Mint.",
      ("what's new in my Gmail?", "put lunch with Sam on my calendar at 1 tomorrow"), ("account",),
      uses=("mail", "calendar_events", "create_event"), optional_tools=("calendar_manage",),
      check=_google_check, do_connect=_accounts_connect("your Google account"), do_test=_accounts_test,
      group="Accounts"),
    L("icloud", "iCloud", "icloud", "Your iCloud mail, calendars, reminders, notes and iCloud Drive files.",
      ("what's on my iCloud calendar?", "find the tax PDF in iCloud Drive"), ("account",),
      uses=("find_files", "calendar_events"), check=_icloud_check,
      do_connect=lambda c: (open_url("x-apple.systempreferences:com.apple.systempreferences.AppleIDSettings"),
                            "Opened System Settings ▸ Apple Account: sign in and turn on iCloud Drive, Calendars, "
                            "Reminders and Notes.")[1], group="Accounts"),
    L("microsoft", "Microsoft (Outlook)", "briefcase", "Outlook.com and work Exchange mail and calendars, "
      "through Internet Accounts (or the Outlook app).",
      ("read my latest work email", "am I free at 3 on Thursday?"), ("account",), uses=("mail", "calendar_events"),
      check=_microsoft_check, do_connect=_accounts_connect("Microsoft Exchange or Outlook.com"), group="Accounts"),
    # Apple apps
    L("mail", "Mail", "envelope", "Read, sum up and triage your inbox; draft replies you send yourself.",
      ("anything important in my email?", "draft a reply to Nina saying yes"), ("builtin", "scripting"),
      ("com.apple.mail",), uses=("mail", "list_emails", "read_email", "compose_email"),
      do_test=_script_test('tell application "Mail" to get name of every account',
                           lambda s: f"Mail accounts: {s or 'none'}"), group="Apple"),
    L("calendar", "Calendar", "calendar", "See your day, add events, find free time; move, edit or delete events "
      "when you ask.", ("what's on today?", "move my 3pm to Friday"), ("builtin",), ("com.apple.iCal",),
      uses=("calendar_events", "create_event", "briefing"), optional_tools=("calendar_manage",),
      check=_privacy_check("calendars"), do_connect=_privacy_connect("calendars"), do_test=_calendar_test,
      group="Apple"),
    L("reminders", "Reminders", "checklist", "Add reminders with times and lists; tick off, change or clear them.",
      ("remind me to call Mum at 6", "what's on my shopping list?"), ("builtin",), ("com.apple.reminders",),
      uses=("create_reminder",), optional_tools=("reminders_manage",), check=_privacy_check("reminders"),
      do_connect=_privacy_connect("reminders"), do_test=_reminders_test, group="Apple"),
    L("notes", "Notes", "note.text", "Make notes; search, read, add to, rename and file them.",
      ("note down these three ideas", "what did I write about the trip?"), ("builtin", "scripting"),
      ("com.apple.Notes",), uses=("create_note",), optional_tools=("notes",),
      do_test=_script_test('tell application "Notes" to count notes', lambda s: f"{s} notes."), group="Apple"),
    L("contacts", "Contacts", "person.crop.circle", "Look people up: numbers, emails, birthdays, addresses.",
      ("what's Ravi's number?", "when is Anna's birthday?"), ("builtin",), ("com.apple.AddressBook",),
      optional_tools=("contacts",), check=_privacy_check("contacts"), do_connect=_privacy_connect("contacts"),
      do_test=_contacts_test, group="Apple"),
    L("messages", "Messages", "message", "Read who wrote and what (read-only). Mint never sends a message unless "
      "you ask it to in that request, and shows it first.", ("did anyone text me?", "open my chat with Sam"),
      ("scripting",), ("com.apple.MobileSMS",), uses=("notifications",), do_test=_messages_test, group="Apple"),
    L("facetime", "FaceTime", "video", "Start FaceTime calls by name or number (you press Call).",
      ("FaceTime Mum", "start a FaceTime audio call with Ravi"), ("url",), ("com.apple.FaceTime",),
      uses=("open_url",), group="Apple"),
    L("safari", "Safari", "safari", "Open, read and use web pages; your Reading List and bookmarks.",
      ("read this page to me", "add this to my reading list"), ("builtin", "scripting"), ("com.apple.Safari",),
      uses=("browser", "open_url", "read_url"), optional_tools=("safari",),
      do_test=_script_test('tell application "Safari" to count windows', lambda s: f"{s} Safari windows."),
      group="Apple"),
    L("chrome", "Chrome and friends", "globe", "Browse, click and fill in pages; open the right Chrome profile.",
      ("open my work Gmail in Chrome", "fill this form with my details"), ("builtin", "browser"),
      ("com.google.Chrome", "com.brave.Browser", "com.microsoft.edgemac", "company.thebrowser.Browser"),
      uses=("browser", "open_chrome"), check=_any_installed((("Chrome", "com.google.Chrome"),
                                                            ("Brave", "com.brave.Browser"),
                                                            ("Edge", "com.microsoft.edgemac"),
                                                            ("Arc", "company.thebrowser.Browser"))),
      site="https://www.google.com/chrome/", group="Apps"),
    L("finder", "Finder and files", "folder", "Find, open, move, rename, convert and tidy files and folders.",
      ("find the invoice I downloaded yesterday", "tidy my Downloads"), ("builtin",), ("com.apple.finder",),
      uses=("find_files", "file_action", "tidy", "open_folder"),
      do_test=_script_test('tell application "Finder" to get name of startup disk', lambda s: f"Startup disk: {s}"),
      group="Apple"),
    L("music", "Music", "music.note", "Play, pause and pick songs, albums and playlists.",
      ("play some jazz", "what's this song?"), ("scripting",), ("com.apple.Music",), uses=("media_key",),
      optional_tools=("music",),
      check=_music_check, do_test=_script_test('tell application "Music" to get player state as text',
                                               lambda s: f"Music is {s}."), group="Apple"),
    L("spotify", "Spotify", "waveform", "Play, pause, skip and pick what's playing in Spotify.",
      ("play my Discover Weekly", "skip this song"), ("scripting", "url"), ("com.spotify.client",),
      uses=("media_key",), optional_tools=("music",), check=_music_check, site="https://www.spotify.com/download/mac/",
      do_test=_script_test('tell application "Spotify" to get player state as text', lambda s: f"Spotify is {s}."),
      group="Apps"),
    L("podcasts", "Podcasts", "mic", "Play and pause podcasts; open a show.", ("resume my podcast",),
      ("url",), ("com.apple.podcasts",), uses=("media_key", "open_app"), group="Apple"),
    L("photos", "Photos", "photo.on.rectangle", "Find photos by what's in them, place or date, and export them.",
      ("find photos of the beach from June", "export my last 5 photos to the desktop"), ("builtin",),
      ("com.apple.Photos",), optional_tools=("photos",), check=_privacy_check("photos"),
      do_connect=_privacy_connect("photos"), group="Apple"),
    L("maps", "Maps", "map", "Directions and travel times.", ("how long to the airport?",
                                                              "directions to Blue Tokai by car"),
      ("builtin", "url"), ("com.apple.Maps",), uses=("open_url",), optional_tools=("maps",), group="Apple"),
    L("iwork", "Pages, Keynote, Numbers", "doc.richtext", "Make and edit documents, slides and spreadsheets.",
      ("make a Keynote from these notes", "export this Pages file as PDF"), ("builtin", "scripting"),
      ("com.apple.iWork.Pages", "com.apple.iWork.Keynote", "com.apple.iWork.Numbers"), uses=("export_doc_pdf",),
      optional_tools=("iwork",), check=_any_installed((("Pages", "com.apple.iWork.Pages"),
                                                    ("Keynote", "com.apple.iWork.Keynote"),
                                                    ("Numbers", "com.apple.iWork.Numbers"))),
      site="macappstore://apps.apple.com/app/id409201541", group="Apple"),
    L("shortcuts", "Apple Shortcuts", "square.stack.3d.up", "Run your shortcuts (Home scenes, Focus), and make new "
      "ones from plain words.", ("run Good Night", "make a shortcut that turns on dark mode"), ("shortcuts",),
      ("com.apple.shortcuts",), uses=("shortcut", "make_shortcut"), settings_page="apple_shortcuts",
      check=_shortcuts_check, do_connect=_shortcuts_connect, do_test=_shortcuts_test, group="Apple"),
    L("image_playground", "Image Playground", "wand.and.stars", "Draw pictures and change them, on this Mac.",
      ("draw a fox in a scarf", "make it snowy"), ("shortcuts",), ("com.apple.GenerativePlaygroundApp",),
      uses=("make_image",), check=_playground_check, do_connect=_playground_connect, group="Apple"),
    # Chat and meetings
    L("slack", "Slack", "number", "Open workspaces, channels and DMs by name; read what's new on screen.",
      ("open the on-call channel", "go to my DM with Nina"), ("url", "browser"), ("com.tinyspeck.slackmacgap",),
      uses=("open_slack",), check=_slack_check, site="https://slack.com/downloads/mac", group="Chat"),
    L("whatsapp", "WhatsApp", "phone.bubble", "Open a chat and write a message for you to send; read what's on "
      "screen. Mint never presses Send unless you asked in that request.", ("open WhatsApp with Mum",),
      ("url",), ("net.whatsapp.WhatsApp", "desktop.WhatsApp"), uses=("open_url", "open_app"), makeable=True,
      site="https://www.whatsapp.com/download", group="Chat"),
    L("telegram", "Telegram (remote control)", "paperplane", "Text or voice-note Mint from your phone through your "
      "own Telegram bot; alerts come back there.", ("(from your phone) what's on my screen?",), ("key",),
      uses=("telegram",), settings_page="accounts", check=_telegram_check, group="Chat"),
    L("meetings", "Zoom, Teams, Meet", "person.2.wave.2", "Record any call without a bot, then a transcript, notes "
      "and action items.", ("record this meeting", "what did we decide in the Acme call?"), ("builtin", "url"),
      uses=("meeting",), check=_meetings_check, group="Chat"),
    # Work apps
    L("notion", "Notion", "doc.text", "Open pages and search your workspace (app or browser).",
      ("open my Notion roadmap", "search Notion for onboarding"), ("url", "browser"), ("notion.id",),
      uses=("open_url", "browser"), makeable=True, site="https://www.notion.com/desktop", group="Work"),
    L("things", "Things", "checkmark.circle", "Add to-dos, see Today and your projects.",
      ("add milk to Things", "what's in my Things Today?"), ("scripting", "url"), ("com.culturedcode.ThingsMac",),
      makeable=True, site="https://culturedcode.com/things/", group="Work"),
    L("todoist", "Todoist", "checklist.checked", "Add tasks and open your projects (app, browser, or an API key).",
      ("add 'send invoice' to Todoist",), ("url", "browser", "key"), ("com.todoist.mac.Todoist",), makeable=True,
      site="https://todoist.com/downloads", group="Work"),
    L("obsidian", "Obsidian", "diamond", "Open and search your vaults; notes are Markdown files Mint can read.",
      ("open today's daily note",), ("url",), ("md.obsidian",), uses=("read_file", "write_file"), makeable=True,
      site="https://obsidian.md/download", group="Work"),
    L("vscode", "VS Code", "chevron.left.forwardslash.chevron.right", "Open projects and files; Mint's coding agents "
      "work in your repos.", ("open the jarvis project in VS Code",), ("url",), ("com.microsoft.VSCode",),
      uses=("open_app", "delegate_task"), makeable=True, site="https://code.visualstudio.com/", group="Work"),
    L("terminal", "Terminal", "terminal", "Watch a long command and tell you when it's done; run commands you ask for.",
      ("tell me when the build finishes",), ("scripting",), ("com.apple.Terminal", "com.googlecode.iterm2"),
      uses=("track",), do_test=_script_test('tell application "Terminal" to count windows',
                                            lambda s: f"{s} Terminal windows."), group="Work"),
    L("ai_apps", "ChatGPT and Claude apps", "bubble.left.and.text.bubble.right", "Ask them, read their replies, and "
      "follow their chats and Codex / Claude Code sessions.", ("ask ChatGPT to review this",
                                                              "what did Claude say?"), ("builtin",),
      ("com.openai.codex", "com.openai.chat", "com.anthropic.claudefordesktop"), uses=("agent_app",), check=_ai_apps_check,
      site="https://chatgpt.com/download", group="Work"),
    L("model_keys", "AI model providers", "cpu", "OpenAI, Anthropic, OpenRouter, Groq, xAI, Ollama for Mint's "
      "agents.", ("have an agent research this with Claude",), ("key",), uses=("delegate_task",),
      settings_page="models", check=_keys_check, group="Work"),
]
BY_ID = {c.id: c for c in LIBRARY}


# --- Custom connectors (made by connector_maker) --------------------------------------------------------

def custom_connectors() -> list[dict]:
    out = []
    try:
        files = sorted(CUSTOM_DIR.glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            data = json.loads(path.read_text())
        except Exception as error:
            log.info("connector %s: %s", path.name, error)
            continue
        if isinstance(data, dict) and data.get("id") and isinstance(data.get("actions"), list):
            data["file"] = str(path)
            out.append(data)
    return out


def custom(cid: str) -> dict | None:
    cid = str(cid or "").strip().lower()
    for item in custom_connectors():
        if item["id"] == cid or item.get("name", "").lower() == cid:
            return item
    return None


def save_custom(data: dict) -> Path:
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    path = CUSTOM_DIR / f"{data['id']}.json"
    clean = {k: v for k, v in data.items() if k != "file"}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1, ensure_ascii=False))
    tmp.replace(path)
    return path


def remove_custom(cid: str) -> str:
    item = custom(cid)
    if item is None:
        return f"There is no connector called '{cid}'."
    path = Path(item["file"])
    try:                                                     # to the Trash, so it can be put back
        import AppKit
        ok, _ = AppKit.NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            AppKit.NSURL.fileURLWithPath_(str(path)), None, None)[:2]
        if not ok:
            raise OSError("trash failed")
    except Exception:
        path.rename(path.with_suffix(".removed"))
    return f"Removed the {item['name']} connector."


def custom_status(item: dict) -> dict:
    if item.get("bundle_id") and app_for(item["bundle_id"]) is None:
        return {"state": "missing", "detail": "App not installed"}
    tested = item.get("tested") or {}
    if tested and not tested.get("ok"):
        return {"state": "setup", "detail": "Last test failed"}
    return {"state": "connected", "detail": f"{len(item['actions'])} actions" + (
        f" · tested ✓ {str(tested.get('at') or '')[:10]}".rstrip() if tested else " · not tested yet")}


# --- All my apps --------------------------------------------------------------------------------------

def _covered() -> set[str]:
    ids = {b for c in LIBRARY for b in c.bundles}
    ids |= {str(i.get("bundle_id")) for i in custom_connectors()}
    return ids


_SKIP = {"com.apple.systempreferences", "com.apple.AppStore", "com.apple.ScriptEditor2", "com.apple.Automator",
         "com.apple.ActivityMonitor", "com.apple.Console", "com.apple.Terminal", "com.apple.finder"}


def scriptable_apps(fresh: bool = False) -> list[dict]:
    """Installed apps with a scripting dictionary that the library doesn't cover: Mint can make a connector."""
    apps = installed_apps(fresh)
    with _apps_lock:
        cached = _apps.get("scriptable")
    if cached is not None and not fresh:
        found = cached
    else:
        found = [a for a in apps.values() if is_scriptable(a)]
        with _apps_lock:
            _apps["scriptable"] = found
    covered = _covered() | _SKIP
    return sorted((a for a in found if a["bundle_id"] not in covered), key=lambda a: a["name"].lower())


# --- Snapshot for Settings and the tool ---------------------------------------------------------------

_snap: dict = {"at": 0.0, "deep": False, "rows": None}


def snapshot(deep: bool = False, max_age: float = 0.0) -> dict:
    """{library: [row], custom: [row], scriptable: [app]}; row = {id, name, icon, enables, examples, how, state,
    detail, path, bundle_id, tools, testable, settings_page, makeable, custom}. max_age reuses a recent one."""
    if max_age and _snap["rows"] is not None and time.time() - _snap["at"] < max_age and (_snap["deep"] or not deep):
        return _snap["rows"]
    rows = []
    for c in LIBRARY:
        status = c.status(deep)
        app = c.app() if c.bundles else None
        rows.append({"id": c.id, "name": c.name, "icon": c.icon, "enables": c.enables, "examples": list(c.examples),
                     "how": [HOW[h] for h in c.how], "state": status["state"], "detail": status["detail"],
                     "path": app["path"] if app else "", "bundle_id": app["bundle_id"] if app else "",
                     "tools": c.tools(), "testable": c.testable, "settings_page": c.settings_page,
                     "makeable": c.makeable, "site": c.site, "custom": False, "group": c.group})
    customs = []
    for item in custom_connectors():
        status = custom_status(item)
        app = app_for(item["bundle_id"]) if item.get("bundle_id") else None
        customs.append({"id": item["id"], "name": item.get("name") or item["id"], "icon": "puzzlepiece.extension",
                        "enables": item.get("summary") or "", "examples": item.get("examples") or [],
                        "how": [HOW.get(item.get("kind") or "scripting", "App scripting")],
                        "state": status["state"], "detail": status["detail"], "path": app["path"] if app else "",
                        "bundle_id": item.get("bundle_id") or "", "tools": ["connector"], "testable": True,
                        "settings_page": "", "makeable": False, "site": item.get("site") or "", "custom": True,
                        "actions": [a.get("title") or a.get("id") for a in item["actions"]]})
    try:
        scriptable = scriptable_apps() if deep or _apps.get("scriptable") is not None else []
    except Exception as error:
        log.info("scriptable: %s", error)
        scriptable = []
    result = {"library": rows, "custom": customs, "scriptable": scriptable}
    _snap.update(at=time.time(), deep=deep, rows=result)
    return result


ALIASES = {"gmail": "google", "google calendar": "google", "google account": "google", "outlook": "microsoft",
           "exchange": "microsoft", "hotmail": "microsoft", "office 365": "microsoft", "icloud drive": "icloud",
           "apple mail": "mail", "email": "mail", "calendars": "calendar", "apple calendar": "calendar",
           "apple notes": "notes", "address book": "contacts", "imessage": "messages", "sms": "messages",
           "google chrome": "chrome", "brave": "chrome", "edge": "chrome", "arc": "chrome", "browser": "chrome",
           "files": "finder", "apple music": "music", "itunes": "music", "zoom": "meetings", "teams": "meetings",
           "microsoft teams": "meetings", "google meet": "meetings", "meet": "meetings", "things 3": "things",
           "visual studio code": "vscode", "vs code": "vscode", "code": "vscode", "iterm": "terminal",
           "iterm2": "terminal", "chatgpt": "ai_apps", "claude": "ai_apps", "codex": "ai_apps", "pages": "iwork",
           "keynote": "iwork", "numbers": "iwork", "iwork": "iwork", "shortcuts": "shortcuts",
           "image playground": "image_playground", "telegram": "telegram", "api keys": "model_keys",
           "openai": "model_keys", "anthropic": "model_keys", "apple maps": "maps", "photo": "photos"}


def get(cid: str) -> Connector | None:
    cid = str(cid or "").strip().lower()
    if cid in BY_ID:
        return BY_ID[cid]
    words = " ".join(re.sub(r"[^a-z0-9 ]", " ", cid).split())
    words = re.sub(r"\b(my|the)\s+", "", words)
    words = re.sub(r"\s+(accounts?|app|apps|application)$", "", words).strip()
    if words in BY_ID:
        return BY_ID[words]
    if words in ALIASES:
        return BY_ID[ALIASES[words]]
    for c in LIBRARY:
        names = {c.name.lower(), c.id.replace("_", " ")} | {w.strip().lower() for w in re.split(r"[(),+]| and ",
                                                                                                 c.name) if w.strip()}
        if words.strip() in names:
            return c
    return None


def list_text(deep: bool = True) -> str:
    snap = snapshot(deep=deep)
    by = {"connected": [], "setup": [], "missing": []}
    for row in snap["library"]:
        by[row["state"]].append(f"{row['name']} ({row['detail']})")
    lines = ["Connected: " + "; ".join(by["connected"])]
    if by["setup"]:
        lines.append("On this Mac, not set up yet: " + "; ".join(by["setup"]))
    if by["missing"]:
        lines.append("Not installed: " + ", ".join(r.split(" (")[0] for r in by["missing"]))
    if snap["custom"]:
        lines.append("The user's own connectors: " + "; ".join(
            f"{r['name']} [{r['id']}]: {', '.join(r['actions'][:10])}" for r in snap["custom"]))
    if snap["scriptable"]:
        names = [a["name"] for a in snap["scriptable"]]
        lines.append(f"Scriptable apps Mint can make a connector for ({len(names)}): " + ", ".join(names[:40]))
    return "\n".join(lines)


def status_text(cid: str) -> str:
    c = get(cid)
    if c is None:
        item = custom(cid)
        if item is None:
            return f"FAILED: no connector called '{cid}'. action=list shows them."
        s = custom_status(item)
        acts = "; ".join(f"{a['id']}: {a.get('title', '')}{' (changes things)' if a.get('changes') else ''}"
                         for a in item["actions"])
        return f"{item['name']} (your connector): {STATES[s['state']]} - {s['detail']}. Actions: {acts}"
    s = c.status(deep=True)
    tools = c.tools()
    return (f"{c.name}: {STATES[s['state']]} - {s['detail']}. Connects by {', '.join(HOW[h] for h in c.how)}. "
            f"{c.enables} For example: {'; '.join(c.examples)}."
            + (f" Tools: {', '.join(tools)}." if tools else "")
            + (" connector action=make can add more actions for it." if c.makeable else ""))


def table(deep: bool = True) -> str:
    snap = snapshot(deep=deep)
    rows = [("Connector", "How", "Status", "Detail")]
    for r in snap["library"]:
        rows.append((r["name"], "/".join(r["how"]), STATES[r["state"]], r["detail"]))
    for r in snap["custom"]:
        rows.append((r["name"] + " (yours)", "/".join(r["how"]), STATES[r["state"]], r["detail"]))
    widths = [max(len(str(r[i])) for r in rows) for i in range(3)]
    out = [" | ".join(str(r[i]).ljust(widths[i]) for i in range(3)) + " | " + r[3] for r in rows]
    out.insert(1, "-" * (sum(widths) + 30))
    if snap["scriptable"]:
        out.append("")
        out.append(f"Scriptable (Mint can make a connector), {len(snap['scriptable'])}: "
                   + ", ".join(a["name"] for a in snap["scriptable"]))
    return "\n".join(out)


if __name__ == "__main__":
    print(table())
