"""Tier 0: launching, window and clipboard queries. No permission needed for most."""

from __future__ import annotations

import subprocess
from pathlib import Path

import AppKit
import Quartz

from . import config


def _osascript(script: str, timeout: float = 8) -> tuple[bool, str]:
    done = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    output = (done.stdout or done.stderr or "").strip()
    return done.returncode == 0, output


def open_app(name: str) -> str:
    """Launch or switch to an application by name."""
    done = subprocess.run(["open", "-a", name], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        detail = (done.stderr or "").strip()
        return f"Could not open '{name}'. {detail or 'No application by that name.'}"
    # `open -a` launches the app, but macOS may leave the previous app in front
    # (in testing ChatGPT opened behind Claude, and typing then went to Claude).
    from . import ground
    if not ground.bring_forward(name, wait=4.0):
        return (f"FAILED: {name} opened but macOS kept another app in front, so anything typed now "
                f"would go to the wrong app. Try ui_act with app='{name}', or ask the user to click it.")
    return f"Opened {name} and it is in front"


def open_url(url: str, browser: str = "") -> str:
    """Open a web address in the right browser (see browser_choice): a tab already showing it, else a new tab."""
    from . import browser_choice
    return browser_choice.open_url(url, browser)


def open_folder(name: str) -> str:
    """Open a folder in Finder. Accepts a path or a common folder name."""
    candidate = Path(name).expanduser()
    if not candidate.exists():
        candidate = Path.home() / name
    if not candidate.exists():
        return f"No folder called '{name}'."
    subprocess.run(["open", str(candidate)], capture_output=True, check=False)
    return f"Opened {candidate}"


def frontmost_app() -> str:
    """Which application is in front right now."""
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return "Nothing is in front."
    return f"{app.localizedName()} is in front."


def list_windows() -> str:
    """Visible windows, as app name and window title."""
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    info = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    seen = []
    for window in info:
        owner = window.get("kCGWindowOwnerName") or ""
        title = window.get("kCGWindowName") or ""
        layer = window.get("kCGWindowLayer", 0)
        # Layer 0 is ordinary app windows; higher layers are menus and overlays.
        if layer != 0 or not owner:
            continue
        seen.append(f"{owner}: {title}" if title else owner)
    if not seen:
        return "No ordinary windows are open."
    return "; ".join(list(dict.fromkeys(seen))[:25])


def read_clipboard() -> str:
    from .skills import BOARD_LOCK
    with BOARD_LOCK:
        text = AppKit.NSPasteboard.generalPasteboard().stringForType_(AppKit.NSPasteboardTypeString)
    if not text:
        return "The clipboard holds no text."
    text = str(text)
    return text if len(text) <= 4000 else text[:4000] + " … (truncated)"


def write_clipboard(text: str) -> str:
    from .skills import BOARD_LOCK
    with BOARD_LOCK:
        board = AppKit.NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(text, AppKit.NSPasteboardTypeString)
    return f"Copied {len(text)} characters to the clipboard."


def quit_app(name: str) -> str:
    """Ask an application to quit. Unsaved work may prompt the user."""
    ok, output = _osascript(f'tell application "{name}" to quit')
    if not ok:
        return f"Could not quit '{name}'. {output}"
    return f"Asked {name} to quit."


def run_shell(command: str) -> str:
    """Run a shell command. Disabled unless MINT_ALLOW_SHELL is set."""
    if not config.ALLOW_SHELL:
        return (
            "Shell access is turned off. The user can enable it by setting "
            "MINT_ALLOW_SHELL=1 before starting Mint."
        )
    try:
        done = subprocess.run(
            ["/bin/bash", "-lc", command], capture_output=True, text=True,
            timeout=config.SHELL_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {config.SHELL_TIMEOUT}s."
    output = (done.stdout or "") + (done.stderr or "")
    output = output.strip() or "(no output)"
    if len(output) > 4000:
        output = output[:4000] + " … (truncated)"
    return f"Exit {done.returncode}. {output}"
