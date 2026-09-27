"""Quitting Mint completely, and starting it at login.

`quit_fully` is what every Quit does (menu bar, the orb's menu, Settings, "quit
Mint" said or typed): stop anything in progress, stop sub-agents, end every
process Mint started (Codex runs, preview servers, scripts), quit the hidden
screen-control engine (Desktop Voice) if it is running, then quit the app the
normal way so the conversation is still folded into memory. If anything hangs,
the process exits anyway after a few seconds - Mint once ignored SIGTERM.

Nothing restarts it afterwards unless "Start at login" is on, and then only at
the next login.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("mint.power")

AGENT = Path.home() / "Library" / "LaunchAgents" / "local.mint.plist"
APP = Path.home() / "Applications" / "Mint.app"
ENGINE = "local.jev-use"              # Desktop Voice, the hidden screen-control engine
HARD_STOP = 15.0            # seconds before the process exits no matter what
_quitting = threading.Event()


def _children(pid: int) -> list[int]:
    """Every process under `pid`, deepest first."""
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return []
    found = []
    for line in out.split():
        if line.isdigit():
            child = int(line)
            found += _children(child) + [child]
    return found


def _end_children() -> int:
    """SIGTERM every process under Mint, then SIGKILL what is left. A child that leads its own
    process group (Codex runs do: start_new_session) gets the whole group, so the shells and
    tools it started go too."""
    total = len(_children(os.getpid()))
    mine = os.getpgid(0)
    groups = set()
    for pid in _direct(os.getpid()):
        try:
            group = os.getpgid(pid)
        except OSError:
            continue
        if group != mine:
            groups.add(group)
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except OSError:
            pass
    kids = _children(os.getpid())
    for pid in kids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(_alive(p) for p in kids):
        time.sleep(0.1)
    for pid in kids:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
    return max(total, len(kids))


def _direct(pid: int) -> list[int]:
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return []
    return [int(x) for x in out.split() if x.isdigit()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _quit_engine() -> bool:
    try:
        import AppKit
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            if (app.bundleIdentifier() or "") == ENGINE:
                app.terminate()
                return True
    except Exception as error:
        log.info("engine quit: %s", error)
    return False


def _shutdown(reason: str) -> None:
    log.info("quitting fully (%s)", reason or "asked")
    print(f"  [quit: {reason or 'asked'} - stopping everything]", flush=True)
    steps = []
    try:
        from . import control
        control.stop()                         # no more clicks or keys from anything in flight
    except Exception:
        pass
    try:
        from .agents.runtime import hub
        stopping = hub.cancel("all")
        steps.append(stopping)
        if stopping.startswith("Stopping"):
            time.sleep(1.0)                    # agents notice run.stop within 0.5 s while the loop runs
    except Exception as error:
        log.info("agents: %s", error)
    try:
        from . import desktop
        desktop.cancel_running()
    except Exception:
        pass
    ended = _end_children()
    if ended:
        steps.append(f"ended {ended} helper process(es)")
    if _quit_engine():
        steps.append("quit the screen-control engine")
    log.info("quit: %s", "; ".join(steps) or "nothing else was running")
    # The normal way out: the app's will-terminate hook folds the conversation into memory.
    from . import ui
    ui.quit_app()
    time.sleep(HARD_STOP)
    log.warning("quit: still running after %.0fs - exiting now", HARD_STOP)
    os._exit(0)


def quit_fully(*_args, reason: str = "") -> None:
    """Quit Mint and everything it started. Safe from any thread; runs once."""
    if _quitting.is_set():
        return
    _quitting.set()
    threading.Thread(target=_shutdown, args=(reason,), name="mint-quit", daemon=True).start()


def quit_later(seconds: float, reason: str = "") -> None:
    """Quit after `seconds`, so a goodbye can be heard first."""
    threading.Timer(seconds, lambda: quit_fully(reason=reason)).start()


# --- start at login ---------------------------------------------------------------------------

def starts_at_login() -> bool:
    return AGENT.exists()


def set_start_at_login(on: bool) -> str:
    uid = os.getuid()
    if on:
        AGENT.parent.mkdir(parents=True, exist_ok=True)
        AGENT.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>local.mint</string>
  <!-- Through LaunchServices, so macOS treats it as Mint.app for permissions. -->
  <key>ProgramArguments</key>  <array><string>/usr/bin/open</string><string>-g</string><string>{APP}</string></array>
  <key>RunAtLoad</key>         <true/>
</dict>
</plist>
""")
        # Registered for the next login only: bootstrapping now would start a second Mint.
        return "Mint will start when you log in."
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(AGENT)], capture_output=True, check=False)
    AGENT.unlink(missing_ok=True)
    return "Mint will not start at login."
