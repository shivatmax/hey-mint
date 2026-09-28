"""Apple Shortcuts by voice: "run my Good Night shortcut", "turn on the living room lights"
(a Home scene saved as a shortcut), "play my focus playlist".

A Mac app outside Mac Catalyst cannot use HomeKit directly, but the Shortcuts app can - so
anything the user has built there (Home scenes, Spotify, Focus modes, their own
workflows) becomes something Mint can run. `shortcuts list` gives the names; the one meant
is picked by words (and Jev when a TypeSafe key is set); `shortcuts run` runs it, with
optional text input, and its output is returned.
"""

from __future__ import annotations

import difflib
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger("mint.tools.shortcuts")

_cache: dict = {"at": 0.0, "names": []}


def names(fresh: bool = False) -> list[str]:
    if fresh or time.time() - _cache["at"] > 300:
        done = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=20)
        _cache.update(at=time.time(), names=[n.strip() for n in done.stdout.splitlines() if n.strip()])
    return _cache["names"]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in {"the", "my", "a", "shortcut", "run"}}


def pick(wanted: str, options: list[str]) -> str | None:
    wanted = wanted.strip()
    for name in options:
        if name.lower() == wanted.lower():
            return name
    scored = sorted(((len(_words(wanted) & _words(n)) + difflib.SequenceMatcher(None, wanted.lower(), n.lower()).ratio(), n)
                     for n in options), reverse=True)
    if scored and scored[0][0] >= 1.0:
        return scored[0][1]
    try:
        from mint.core import jev
        if jev.available() and options:
            choice = jev.choose(f"Which shortcut does '{wanted}' mean?", {str(i): n for i, n in enumerate(options)})
            if choice is not None and choice.id is not None and choice.probability >= 0.5:
                return options[int(choice.id)]
    except Exception:
        pass
    return None


def run(name: str, text: str = "") -> str:
    options = names()
    if not options:
        return ("There are no shortcuts in the Shortcuts app yet. The user can make one there (for example a Home "
                "scene, a Focus mode or a playlist) and then ask Mint to run it by name.")
    chosen = pick(name, options)
    if chosen is None:
        close = difflib.get_close_matches(name, options, n=5, cutoff=0.2) or options[:8]
        return f"No shortcut matches '{name}'. Some there are: {', '.join(close)}."
    command = ["shortcuts", "run", chosen]
    source = None
    if text:
        source = Path(tempfile.mkstemp(prefix="mint-shortcut-", suffix=".txt")[1])
        source.write_text(text)
        command += ["--input-path", str(source)]
    out = Path(tempfile.mkstemp(prefix="mint-shortcut-out-")[1])
    command += ["--output-path", str(out)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return f"The shortcut '{chosen}' is still running after 2 minutes; left it running."
    finally:
        if source is not None:
            source.unlink(missing_ok=True)
    result = out.read_text(errors="replace").strip() if out.exists() and out.stat().st_size < 200_000 else ""
    out.unlink(missing_ok=True)
    if done.returncode != 0:
        return f"FAILED: the shortcut '{chosen}' stopped with: {(done.stderr or done.stdout).strip()[:300]}"
    return f"Ran the shortcut '{chosen}'." + (f" It returned: {result[:1500]}" if result else "")


def tool(args: dict) -> str:
    action = str(args.get("action") or "run").lower()
    if action == "list":
        options = names(fresh=True)
        return ("Shortcuts: " + ", ".join(options)) if options else run("")
    return run(str(args.get("name") or ""), str(args.get("input") or ""))


PROMPT = """Apple Shortcuts: the user's own shortcuts (Home scenes and lights, Focus modes, Spotify, their \
workflows) run with shortcut action=run name=<what they said>. Lights, the thermostat or a scene -> a shortcut \
(Mint cannot reach HomeKit any other way); if none fits, say they can make one in the Shortcuts app."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="shortcut",
        description=("Run one of the user's Apple Shortcuts by name (Home scenes and lights, Focus, music, their own "
                     "automations), optionally with text input, and get its output; or list them."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["run", "list"]),
            "name": types.Schema(type=S, description="the shortcut, in the user's words"),
            "input": types.Schema(type=S, description="optional text to give the shortcut")}))]


HANDLERS = {"shortcut": tool}
