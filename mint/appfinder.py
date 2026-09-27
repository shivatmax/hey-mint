"""Which installed app the user means.

Speech-to-text hears app names badly: in testing "Z code" (ZCode) came through
as "Xcode", which is not installed, and `open -a Xcode` simply failed. Here the
name is resolved against the apps that actually exist - exact, then alias,
then a unique match on words or letters - and only then does Jev choose from
the real list (with "none of these" allowed), so it can never invent an app.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import AppKit

from . import custom, jev

_FOLDERS = [Path("/Applications"), Path("/Applications/Utilities"), Path.home() / "Applications",
            Path("/System/Applications"), Path("/System/Applications/Utilities")]
_cache: tuple[float, dict[str, str]] = (0.0, {})


def installed() -> dict[str, str]:
    """{display name: path} for every app installed or running. Cached 60 s."""
    global _cache
    if time.monotonic() - _cache[0] < 60 and _cache[1]:
        return _cache[1]
    apps: dict[str, str] = {}
    for folder in _FOLDERS:
        try:
            for path in folder.glob("*.app"):
                apps.setdefault(path.stem, str(path))
        except OSError:
            continue
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular and app.bundleURL():
            apps.setdefault(app.localizedName() or Path(app.bundleURL().path()).stem, app.bundleURL().path())
    _cache = (time.monotonic(), apps)
    return apps


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def resolve(name: str) -> tuple[str | None, str]:
    """-> (app name to open, how it was found) or (None, why not)."""
    said = custom.alias(name.strip())
    apps = installed()
    if not said:
        return None, "No app name given."
    # Exact, ignoring case, spaces and punctuation: "zcode" = "ZCode", "vs code" ≠ though.
    squashed = _squash(said)
    for app in apps:
        if _squash(app) == squashed:
            return app, "exact"
    # Common spoken names for apps whose real names differ.
    spoken = {"vs code": "Visual Studio Code", "vscode": "Visual Studio Code", "code": "Visual Studio Code",
              "codex": "ChatGPT", "chat gpt": "ChatGPT", "claude desktop": "Claude",
              "settings": "System Settings", "system preferences": "System Settings"}
    if said.lower() in spoken and spoken[said.lower()] in apps:
        return spoken[said.lower()], "known name"
    # Partial names: "visual studio" in "Visual Studio Code". The other way round
    # only when nearly the whole name: "Photos" must not answer "Photoshop".
    hits = [app for app in apps if len(squashed) >= 3 and (
        squashed in _squash(app)
        or (_squash(app) in squashed and len(_squash(app)) >= 0.85 * len(squashed)))]
    if len(hits) == 1:
        return hits[0], "partial name"
    # A misheard letter or two: "xcode" for "zcode" is one edit away; "photoshop"
    # from "photos" is three, and is a different app.
    near = [app for app in apps if _edits(squashed, _squash(app)) <= max(1, len(squashed) // 5)]
    if len(near) == 1:
        return near[0], "one letter off - probably misheard"

    # Jev timed out choosing among ~150 apps; a shortlist of the 40 closest
    # spellings keeps it around a second and still holds misheard names.
    shortlist = sorted(apps, key=lambda a: -_overlap(squashed, _squash(a)))[:40]
    options = {str(i): app for i, app in enumerate(shortlist)}
    pick = jev.choose(f"Which installed Mac app does the user mean by '{said}'? It came from "
                      "speech recognition, so it may be misheard (e.g. 'Xcode' for 'ZCode').",
                      options)
    if pick is not None and pick.sure:
        return options[pick.id], f"closest installed app (Jev, {pick.confidence:.2f})"
    close = sorted(apps, key=lambda a: -_overlap(squashed, _squash(a)))[:4]
    return None, f"No installed app matches '{said}'. Closest: {', '.join(close)}."


def _overlap(a: str, b: str) -> float:
    """Letter-pair overlap, a cheap similarity for the error message."""
    pairs = lambda s: {s[i:i + 2] for i in range(len(s) - 1)}
    x, y = pairs(a), pairs(b)
    return len(x & y) / (len(x | y) or 1)


def _edits(a: str, b: str) -> int:
    """Levenshtein distance, stopping early once it exceeds 3."""
    if abs(len(a) - len(b)) > 3:
        return 4
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if min(current) > 3:
            return 4
        previous = current
    return previous[-1]
