"""Bridge to the Desktop Voice (jev-use) app.

The app takes a plain-English goal on a distributed notification and drives the
screen through the accessibility tree, choosing each action with Jev. It does not
report results back over IPC, so this module streams the app's unified log and
reconstructs the outcome from its own status lines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from mint.core import config

log = logging.getLogger("mint.tools.desktop")

# The app logs `<headline> | <detail>` on every status change, but it updates the
# two independently: at the start of a command the headline is already the new
# one while the detail still holds the PREVIOUS command's text. So the detail
# alone can never be trusted as a finish signal - an idle "Hold ⌃M to speak
# again" is routinely still on screen when a new command begins. The headline is
# always fresh, and a completed command has always logged at least one cycle.
_STOP_HEADLINES = (
    "Command stopped",      # fail()
    "Can't do that here",   # BLOCKED with nothing done
    "Can't find it",        # no target offered three times
    "Cancelled",
    "Already done",
    "Which one:",           # low confidence, asking the user
)
# Headlines that mean work is still in progress.
_PROGRESS_HEADLINES = (
    "Reading", "Choosing", "Looking again", "Waiting for the page",
    "Planning", "Finishing speech", "Probing",
)
# Detail text meaning the app has gone back to idle. Only trusted once a cycle
# has been seen for THIS command.
_IDLE_DETAIL = ("to speak again", "Release to act.", "Stopped", "Not sure enough")

# Returned by _collect when the app refused the goal only because it is still
# starting up; the caller waits and sends it again.
_RETRY = object()

# "Captured Slack 'Slack': 19 controls, …" - how much of the app Accessibility
# exposes. Few controls means the app hides its UI (Slack exposed 19 and no
# channel list), and vision is the only way to find things in it.
_CAPTURED = re.compile(r"^Captured (.+?) '.*?': (\d+) controls")
THIN_APP_CONTROLS = 40

_RESULT = re.compile(r"cycle \d+ result: (.+)$")
_CHOICE = re.compile(r"cycle \d+: ([A-Z_]+) (\d+)%")
_CYCLE = re.compile(r"\bcycle \d+\b")


class DesktopBridge:
    """Sends goals to Desktop Voice and waits for the outcome."""

    def __init__(self) -> None:
        # One command at a time: the app itself serialises, and overlapping goals
        # make the log impossible to attribute to the right request.
        self._lock = asyncio.Lock()
        self.last_app = ""
        self.last_controls = -1

    async def run_goal(self, goal: str, timeout: float | None = None) -> str:
        timeout = timeout or config.DESKTOP_TIMEOUT
        if not config.desktop_engine():
            return ("FAILED: the desktop tool is not available here (it needs a TypeSafe key). "
                    "Do the same with ui_act, or click_text for a visible label.")
        async with self._lock:
            self.last_app, self.last_controls = "", -1
            try:
                result = await asyncio.wait_for(self._run(goal), timeout=timeout)
            except asyncio.TimeoutError:
                result = (f"FAILED: timed out after {timeout:.0f}s waiting for '{goal}'. "
                          "The action may still be running.")
            return result + self._vision_hint(result)

    def _vision_hint(self, result: str) -> str:
        """Point the model at look + click_at when the app hides its controls."""
        if not (0 <= self.last_controls < THIN_APP_CONTROLS):
            return ""
        lowered = result.lower()
        failed = any(mark in lowered for mark in (
            "failed", "no action was taken", "nothing was done", "could not", "stopped",
            "no visible effect", "unlabelled", "timed out", "blocked"))
        if not failed:
            return ""
        return (f" NOTE: {self.last_app} exposes only {self.last_controls} controls to "
                "Accessibility, so the desktop tool cannot see most of it. Use click_text with the "
                "control's label instead (or look + click_at if it has no text).")

    async def _run(self, goal: str) -> str:
        # Start the engine on first use only; it runs headless, with no UI of its own.
        await _start_engine()
        # Start streaming before issuing the command so nothing is missed.
        stream = await asyncio.create_subprocess_exec(
            "/usr/bin/log", "stream",
            "--predicate", f'subsystem == "{config.JEV_SUBSYSTEM}"',
            "--info", "--style", "ndjson",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # `log stream` takes a moment to attach; without this the opening
            # lines of a fast command are lost.
            await asyncio.sleep(0.45)
            for attempt in range(12):
                _post_command(goal)
                outcome = await self._collect(stream, goal)
                if outcome is not _RETRY:
                    return outcome
                # The app is still loading its key (it just launched). This
                # clears within seconds, so wait and send the goal again.
                log.info("Desktop Voice not ready yet; retrying (%d)", attempt + 1)
                await asyncio.sleep(2)
            return ("FAILED: the screen-control engine (Desktop Voice) did not become ready. "
                    "Check TYPESAFE_API_KEY in Mint's settings.")
        finally:
            if stream.returncode is None:
                stream.terminate()
                try:
                    await asyncio.wait_for(stream.wait(), timeout=3)
                except asyncio.TimeoutError:
                    stream.kill()

    async def _collect(self, stream: asyncio.subprocess.Process, goal: str) -> str:
        """Read log lines until the command reaches a terminal state."""
        results: list[str] = []
        actions: list[str] = []
        started = False
        saw_cycle = False
        # The app can refuse a goal before running it (key still loading, no
        # permission, no target app). It then logs "Command stopped" and never
        # logs "Command: …", so this has to be caught before `started`.
        rejected = False
        sent_at = time.monotonic()
        deadline = sent_at + config.DESKTOP_TIMEOUT

        assert stream.stdout is not None
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(stream.stdout.readline(), timeout=6)
            except asyncio.TimeoutError:
                # Gone quiet. If it had already done something, that is the end.
                if started and saw_cycle and results:
                    break
                continue
            if not raw:
                break

            message = _message(raw)
            if message is None:
                continue

            if message.startswith("Command: "):
                # Only follow our own command; ignore anything the user types
                # into the app's own box while we are waiting.
                started = message[len("Command: "):].strip() == goal.strip()
                saw_cycle = False
                continue
            if not started:
                if time.monotonic() - sent_at > 5:
                    continue  # too late to be a refusal of this goal
                if message.startswith("Command stopped"):
                    # fail() logs the headline with the old detail, then the
                    # real reason on the next line.
                    rejected = True
                    continue
                if rejected:
                    if "saved-key prompt" in message or "Keychain" in message:
                        return _RETRY
                    return f"Desktop Voice refused the goal: {message}"
                continue

            if found := _CAPTURED.search(message):
                self.last_app, self.last_controls = found.group(1), int(found.group(2))
                continue
            if found := _RESULT.search(message):
                saw_cycle = True
                results.append(found.group(1).strip())
                continue
            if found := _CHOICE.search(message):
                saw_cycle = True
                actions.append(found.group(1))
                continue
            if _CYCLE.search(message):
                saw_cycle = True
                continue

            headline, _, detail = message.partition(" | ")
            headline, detail = headline.strip(), detail.strip()

            # The headline is always current, so a stop headline is conclusive.
            if headline.startswith(_STOP_HEADLINES):
                return _summarise(headline, detail, results, actions)
            if headline.startswith(_PROGRESS_HEADLINES):
                continue
            # Otherwise the headline is the last result, and an idle detail means
            # the command is over - but only once this command has run a cycle,
            # since the detail may still be left over from the previous one.
            if saw_cycle and any(mark in detail for mark in _IDLE_DETAIL):
                return _summarise(headline, detail, results, actions)

        return _summarise(None, None, results, actions)


def _running(name: str) -> bool:
    import subprocess
    return subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0


_engine = None      # the MintEngine this process started


async def _start_engine() -> None:
    """One engine per Mac: every engine acts on every goal, so never start a second one."""
    global _engine
    found = config.engine()
    if found is None:
        return
    kind, path = found
    if kind == "app":
        if not _running("JevDesktop"):
            proc = await asyncio.create_subprocess_exec("open", "-g", str(path))
            await proc.wait()
            await asyncio.sleep(2.5)
        return
    if (_engine is not None and _engine.poll() is None) or _running(config.ENGINE_NAME):
        return
    import os
    import subprocess
    if _running("JevDesktop"):
        # The separate Desktop Voice app listens for the same goals; the bundled engine replaces it.
        subprocess.run(["pkill", "-x", "JevDesktop"], capture_output=True)
        await asyncio.sleep(0.5)
    # A child of Mint, so macOS counts it as Mint: it uses Mint's Accessibility permission.
    # It quits by itself when this process ends (MINT_PARENT_PID).
    env = dict(os.environ, MINT_PARENT_PID=str(os.getpid()), MINT_HOME=str(config.PROJECT_ROOT))
    _engine = subprocess.Popen([str(path), "-Headless", "YES"], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("started the screen-control engine (%s, pid %d)", path, _engine.pid)
    await asyncio.sleep(1.0)


def stop_engine() -> None:
    """Quit the engine this process started (Mint quitting or handing over to Mint Ear)."""
    global _engine
    if _engine is not None and _engine.poll() is None:
        _engine.terminate()
    _engine = None


def _post_command(goal: str) -> None:
    """Hand a goal to Desktop Voice.

    The same distributed notification jev-use's scripts/say.sh posts, sent
    directly: no subprocess per action, and no dependency on the jev-use
    checkout, which may sit in a privacy-protected folder such as Downloads.
    """
    from Foundation import NSDistributedNotificationCenter

    NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
        config.JEV_COMMAND_NOTIFICATION, goal, None, True)


def cancel_running() -> None:
    """Tell Desktop Voice to abandon the command it is running (Mint's "stop")."""
    from Foundation import NSDistributedNotificationCenter

    NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
        "local.jev-use.cancel", None, None, True)


def _message(raw: bytes) -> str | None:
    """Pull the log message out of one ndjson line, if it is a status line."""
    try:
        entry = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    message = entry.get("eventMessage")
    if not isinstance(message, str):
        return None
    return message.strip()


def _summarise(
    headline: str | None, detail: str | None, results: list[str], actions: list[str]
) -> str:
    """Turn the collected log lines into one sentence the model can act on."""
    headline = (headline or "").strip()
    detail = (detail or "").strip()
    # Desktop Voice judges effect by window title, focus and controls, none of
    # which change when a web page scrolls - so every web scroll reads "no
    # visible effect". A pixel diff showed 34% of a Wikipedia page changing on
    # such a "no effect" scroll. Passing the false negative on made the model
    # scroll again, so drop it for scrolls.
    results = [
        r.replace(" → no visible effect", "") if r.startswith("Scrolled") else r
        for r in results
    ]
    steps = "; ".join(results) if results else ", ".join(actions)

    if headline.startswith("Which one:"):
        question = headline[len("Which one:"):].strip().rstrip("?")
        asked = f"Ambiguous - could be: {question}. Ask the user which one they meant."
        return f"{steps}. Then stopped. {asked}" if steps else asked

    if headline.startswith(("Command stopped", "Can't do that here", "Can't find it")):
        reason = detail or headline
        return f"FAILED after: {steps}. Reason: {reason}" if steps else f"FAILED: nothing was done. {reason}"

    if headline.startswith("Cancelled"):
        return "The command was cancelled."

    if headline.startswith("Already done"):
        return "Nothing to do; it was already in that state."

    if not steps:
        return "FAILED: no action was taken; the screen offered nothing that matched."

    if detail.startswith("Stopped"):
        return f"{steps}. Then stopped: {detail}"
    return steps


bridge = DesktopBridge()
