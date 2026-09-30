"""Mint's own Apple Shortcuts, and which of the user's shortcuts Mint may run.

Some things only the Shortcuts app can do (Image Playground pictures, Do Not Disturb), so Mint
builds a few small shortcuts of its own. A Mac app can't add a shortcut by itself: Mint writes
the plist, signs it (`shortcuts sign --mode anyone`) and opens the signed file, and the user
clicks "Add Shortcut" once. This module keeps the list of those shortcuts in one place:

    Mint Draw / Mint Redraw        pictures with Image Playground (imagegen.py builds them)
    Mint Focus On / Mint Focus Off Do Not Disturb on and off (macctl.py builds them)

`installed()` reads `shortcuts list`; `offer(name)` builds, signs and opens one; `sign()` is the
same signing step for shortcut_maker.py. The user's other shortcuts can each be switched off
for Mint (Settings > Apple Shortcuts, pref "shortcuts_blocked": a list of names, empty = all
allowed); apple_shortcuts.run refuses a blocked one.
"""

from __future__ import annotations

import logging
import plistlib
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger("mint.tools.shortcut_library")

BLOCKED_PREF = "shortcuts_blocked"


# --- The registry ---------------------------------------------------------------------------

def _imagegen_names() -> dict[str, str]:
    try:
        from mint.tools import imagegen
        return dict(imagegen.SHORTCUTS)
    except Exception:
        return {"create": "Mint Draw", "edit": "Mint Redraw"}


def _focus_names() -> dict[str, str]:
    try:
        from mint.tools import mac as macctl
        return dict(macctl.FOCUS_SHORTCUTS)
    except Exception:
        return {"on": "Mint Focus On", "off": "Mint Focus Off"}


def _draw(edit: bool):
    def build() -> dict:
        from mint.tools import imagegen
        return imagegen._workflow(edit)
    return build


def _focus(on: bool):
    def build() -> dict:
        from mint.tools import mac as macctl
        return macctl._focus_workflow(on)
    return build


def registry() -> list[dict]:
    """Mint's shortcuts: [{name, purpose, used_by, build}] (build() -> the workflow plist dict)."""
    draw, focus = _imagegen_names(), _focus_names()
    return [
        {"name": draw["create"], "purpose": "Makes pictures with Image Playground (\"draw a fox in a hat\").",
         "used_by": "make_image", "build": _draw(False)},
        {"name": draw["edit"], "purpose": "Changes a picture with Image Playground (\"give it a red scarf\").",
         "used_by": "make_image", "build": _draw(True)},
        {"name": focus["on"], "purpose": "Turns Do Not Disturb on (macOS has no command for it).",
         "used_by": "mac focus", "build": _focus(True)},
        {"name": focus["off"], "purpose": "Turns Do Not Disturb off.", "used_by": "mac focus", "build": _focus(False)},
    ]


def mint_names() -> list[str]:
    return [entry["name"] for entry in registry()]


def entry(name: str) -> dict | None:
    wanted = name.strip().lower()
    for item in registry():
        if item["name"].lower() == wanted:
            return item
    return None


# --- Installed, signing, offering ------------------------------------------------------------

def installed(fresh: bool = True) -> set[str]:
    """Every shortcut's name, from `shortcuts list` (the Shortcuts library itself is private)."""
    from mint.tools import shortcuts as apple_shortcuts
    try:
        return set(apple_shortcuts.names(fresh=fresh))
    except Exception as error:
        log.info("shortcuts list: %s", error)
        return set()


def sign(workflow: dict, name: str, folder: Path | None = None) -> Path:
    """Write `workflow` as a binary plist and sign it for import. -> the signed '<name>.shortcut';
    raises RuntimeError with what `shortcuts sign` said."""
    folder = folder or Path(tempfile.mkdtemp(prefix="mint-shortcut-"))
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip() or "Shortcut"
    raw, signed = folder / (safe.replace(" ", "_") + ".wflow"), folder / (safe + ".shortcut")
    raw.write_bytes(plistlib.dumps(workflow, fmt=plistlib.FMT_BINARY))
    try:
        done = subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", str(raw), "--output", str(signed)],
                              capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise RuntimeError("shortcuts sign took more than 90 s") from None
    if done.returncode != 0 or not signed.exists():
        raise RuntimeError((done.stderr or done.stdout or "shortcuts sign failed").strip()[:200])
    return signed


def open_for_import(path: Path) -> None:
    """Show the signed shortcut in Shortcuts; the user clicks "Add Shortcut" (Mint never can)."""
    subprocess.run(["open", str(path)], check=False)


def offer(name: str) -> str:
    """Build, sign and open one of Mint's shortcuts for the user's one click."""
    item = entry(name)
    if item is None:
        return f"FAILED: '{name}' is not one of Mint's shortcuts ({', '.join(mint_names())})."
    if item["name"] in installed():
        return f"'{item['name']}' is already in Shortcuts."
    try:
        signed = sign(item["build"](), item["name"])
    except Exception as error:
        return f"FAILED: could not prepare '{item['name']}' ({error})."
    open_for_import(signed)
    return (f"NOT DONE YET: opened '{item['name']}' in Shortcuts. The user clicks 'Add Shortcut' there (once). "
            "Tell them in one sentence; do not click it for them.")


def offer_missing() -> str:
    missing = [n for n in mint_names() if n not in installed()]
    if not missing:
        return "All of Mint's shortcuts are in Shortcuts already."
    said = []
    for name in missing:
        said.append(offer(name))
        time.sleep(1.5)                      # one sheet at a time, or Shortcuts shows only the last
    failed = [s for s in said if s.startswith("FAILED")]
    opened = [n for n, s in zip(missing, said) if not s.startswith("FAILED")]
    words = ""
    if opened:
        words = (f"NOT DONE YET: opened {', '.join(repr(n) for n in opened)} in Shortcuts; the user clicks "
                 "'Add Shortcut' on each (once).")
    return " ".join([words] + failed).strip()


# --- The user's shortcuts: which Mint may run -----------------------------------------------------

def blocked() -> list[str]:
    from mint.core import prefs
    value = prefs.get(BLOCKED_PREF) or []
    return [str(v) for v in value] if isinstance(value, list) else []


def allowed(name: str) -> bool:
    return name.strip().lower() not in {b.lower() for b in blocked()}


def set_allowed(name: str, allow: bool) -> None:
    from mint.core import prefs
    names = [b for b in blocked() if b.lower() != name.strip().lower()]
    if not allow:
        names.append(name.strip())
    prefs.set(BLOCKED_PREF, sorted(names, key=str.lower))


def user_shortcuts(fresh: bool = False) -> list[str]:
    mine = {n.lower() for n in mint_names()}
    return sorted((n for n in installed(fresh) if n.lower() not in mine), key=str.lower)


def run(name: str, text: str = "") -> str:
    """Run a shortcut now (Settings' Run button): its output, or 'done'."""
    command = ["shortcuts", "run", name]
    work = Path(tempfile.mkdtemp(prefix="mint-shortcut-run-"))
    out = work / "out"
    if text:
        source = work / "in.txt"
        source.write_text(text)
        command += ["--input-path", str(source)]
    command += ["--output-path", str(out)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "still running after 2 minutes"
    if done.returncode != 0:
        return "failed: " + ((done.stderr or done.stdout).strip()[:200] or f"exit {done.returncode}")
    result = ""
    if out.exists() and out.stat().st_size < 200_000:
        result = out.read_text(errors="replace").strip()
    return result[:500] or "done"


# --- For the make_shortcut tool: action=library / add ------------------------------------------------

def library_text() -> str:
    have = installed()
    lines = [f"{item['name']}: {'added' if item['name'] in have else 'NOT added'} - {item['purpose']}"
             for item in registry()]
    theirs = user_shortcuts()
    off = [n for n in theirs if not allowed(n)]
    words = "Mint's shortcuts:\n" + "\n".join(lines)
    words += f"\nThe user's shortcuts ({len(theirs)}): " + (", ".join(theirs[:60]) or "none")
    if len(theirs) > 60:
        words += f" … and {len(theirs) - 60} more"
    if off:
        words += "\nSwitched off for Mint (Settings > Apple Shortcuts): " + ", ".join(off)
    return words
