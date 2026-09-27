"""Tools for long tasks that cross apps.

* wait_until_done - wait for an app to finish (ChatGPT still writing its
  answer, a page loading) before reading the result. Without it the model read
  a half-written answer, or gave up and told the user to wait. It watches the
  window's text through Accessibility until it stops changing and no Stop
  button is left. Short waits answer the call; long ones hand back at once and
  Mint is told when the app is done, so it keeps listening meanwhile.
* preview_site - serve a built page or site from this Mac on localhost, open
  it, and check it loads (status, title, missing local files), so the model can
  then look at it.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import html
import http.server
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from google.genai import types

from mint.app import control

log = logging.getLogger("mint.tools.work")

STRING = {"type": types.Type.STRING}
SYNC_WAIT = 20.0          # seconds a wait may hold the call before it continues in the background
STABLE = 4.0              # the window must stay unchanged this long to count as finished
IDLE = 20.0               # nothing changed at all for this long: it is not working on anything
POLL = 1.5
_BUSY = re.compile(r"^(stop|stop (generating|streaming|response|responding|answering|thinking|research)|"
                   r"cancel (generation|response)|stop answer)$", re.I)


def _fn(name, description, properties, required=None):
    return types.FunctionDeclaration(
        name=name, description=description,
        parameters=types.Schema(type=types.Type.OBJECT,
                                properties={k: types.Schema(**v) for k, v in properties.items()},
                                required=required or []))


def declarations() -> list[types.FunctionDeclaration]:
    return [
        _fn("wait_until_done",
            "Wait until an app has FINISHED what it is doing before you use the result: ChatGPT (or any AI "
            "chat) still writing its answer, a page loading, something generating. Call it right after "
            "sending a prompt, BEFORE read_window. It watches the window until the text stops changing and "
            "the Stop button is gone. Short waits return the finished state; long ones return at once and "
            "you get a message when the app is done - then continue with the next step.",
            {"app": {**STRING, "description": "The app to watch, e.g. 'Google Chrome'. Default: the one in front."},
             "until_text": {**STRING, "description": "Optional: wait until this text appears instead."},
             "timeout_minutes": {"type": types.Type.NUMBER, "description": "Give up after this long. Default 10."}}),
        _fn("preview_site",
            "Show a web page or site that was built on this Mac (e.g. by Codex): serves its folder at "
            "http://localhost, opens it in the browser, and checks it loads (title, missing files). Then "
            "use look to check it renders properly.",
            {"path": {**STRING, "description": "The HTML file or its folder, e.g. ~/Documents/Mint/agents/codex/x/index.html."}},
            ["path"]),
    ]


# --- reading a window --------------------------------------------------------------------

def _ax(element, attribute):
    import ApplicationServices as AX
    err, value = AX.AXUIElementCopyAttributeValue(element, attribute, None)
    return value if err == 0 else None


def _find_app(name: str):
    import AppKit
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    if not name.strip():
        return workspace.frontmostApplication()
    wanted = name.strip().lower()
    apps = [a for a in workspace.runningApplications() if a.activationPolicy() == 0]
    exact = [a for a in apps if (a.localizedName() or "").lower() == wanted]
    loose = [a for a in apps if wanted in (a.localizedName() or "").lower()]
    return (exact or loose or [None])[0]


def _front_window(app):
    import ApplicationServices as AX
    element = AX.AXUIElementCreateApplication(app.processIdentifier())
    AX.AXUIElementSetAttributeValue(element, "AXManualAccessibility", True)
    return _ax(element, "AXFocusedWindow") or _ax(element, "AXMainWindow")


def _window_state(window) -> tuple[str, bool, str]:
    """(signature of the window's text, a Stop button is showing, the text)."""
    if window is None or _ax(window, "AXRole") is None:
        return "", False, ""
    lines, busy, stack, deadline = [], False, [window], time.monotonic() + 3.0
    while stack and time.monotonic() < deadline:
        node = stack.pop()
        role = _ax(node, "AXRole") or ""
        if role == "AXButton" and not busy:
            for attribute in ("AXTitle", "AXDescription", "AXHelp"):
                label = _ax(node, attribute)
                if isinstance(label, str) and _BUSY.match(label.strip()):
                    busy = True
                    break
        if role in ("AXStaticText", "AXHeading", "AXTextArea", "AXTextField", "AXCell", "AXListItem"):
            value = _ax(node, "AXValue") or _ax(node, "AXTitle") or ""
            if isinstance(value, str) and value.strip():
                lines.append(" ".join(value.split()))
        stack.extend(reversed(list(_ax(node, "AXChildren") or [])))
    text = "\n".join(lines)
    return hashlib.sha1(text.encode()).hexdigest(), busy, text


class _Watch:
    def __init__(self, app, until_text: str, timeout: float) -> None:
        self.app, self.until, self.timeout = app, until_text.strip().lower(), timeout
        self.name = app.localizedName() or "The app"
        self.started = time.monotonic()
        self.generation = control.generation()
        self.last_sig, self.since, self.changed = None, time.monotonic(), False
        self.text = ""
        # Watch THIS window. Reading "the focused window" each time switched to
        # a new localhost tab opened meanwhile, and the answer was lost.
        self.window = _front_window(app)

    def step(self) -> str | None:
        """None while still working; otherwise how it ended: done | stopped | timeout | gone."""
        if control.generation() != self.generation:
            return "stopped"
        if self.app.isTerminated():
            return "gone"
        if self.window is None or _ax(self.window, "AXRole") is None:     # closed: take the current one
            self.window = _front_window(self.app)
        sig, busy, text = _window_state(self.window)
        self.text = text or self.text
        now = time.monotonic()
        if os.environ.get("MINT_DEBUG"):
            last = text.strip().splitlines()[-1][:70] if text.strip() else ""
            print(f"  [wait {now - self.started:5.1f}s busy={busy} chars={len(text)} "
                  f"{'CHANGED' if sig != self.last_sig else 'same'} last={last!r}]", flush=True)
        if self.until:
            if self.until in text.lower():
                return "done"
        else:
            if sig != self.last_sig:
                if self.last_sig is not None:
                    self.changed = True
                self.last_sig, self.since = sig, now
            # Finished = no Stop button and nothing changed for a while. A window
            # that never changed at all is given a little longer, in case the
            # app had not started yet.
            quiet = now - self.since
            if not busy and quiet >= STABLE and self.changed:
                return "done"
            if not busy and not self.changed and now - self.started > IDLE:
                return "idle"
        if now - self.started > self.timeout:
            return "timeout"
        return None

    def report(self, how: str) -> str:
        took = int(time.monotonic() - self.started)
        tail = self.text[-700:].strip()
        if how == "done":
            what = (f"'{self.until}' appeared" if self.until else "its window stopped changing")
            return (f"{self.name} is done ({took}s: {what}). The end of its window now reads:\n...{tail}\n"
                    "Read the full result with read_window if you need it, then carry on.")
        if how == "idle":
            return (f"{self.name} did not change at all in {took}s and shows no Stop button, so it does not seem "
                    "to be working on anything - the prompt may not have been sent. Check with look before "
                    "going on; never make up the result.")
        if how == "timeout":
            return f"{self.name} was still busy after {took}s; stopped waiting. Check it with look or read_window."
        if how == "gone":
            return f"{self.name} quit while waiting."
        return "Stopped waiting (the user said stop)."


def _notify_mint(text: str) -> None:
    from mint.agents.runtime import hub
    if hub.loop is None:
        return
    asyncio.run_coroutine_threadsafe(hub.tell_mint(text, wake=True), hub.loop)


_watching = 0


def busy() -> bool:
    """A background screen watch is running (Mint must stay loaded to deliver it)."""
    return _watching > 0


def wait_until_done(args: dict) -> str:
    app = _find_app(str(args.get("app", "") or ""))
    if app is None:
        return f"No running app called '{args.get('app')}'."
    minutes = float(args.get("timeout_minutes") or 10)
    watch = _Watch(app, str(args.get("until_text", "") or ""), max(0.5, min(minutes, 30)) * 60)
    while time.monotonic() - watch.started < SYNC_WAIT:
        how = watch.step()
        if how:
            return watch.report(how)
        time.sleep(POLL)

    def later():
        global _watching
        _watching += 1
        try:
            while True:
                how = watch.step()
                if how:
                    break
                time.sleep(POLL * 2)
        finally:
            _watching -= 1
        if how != "stopped":
            _notify_mint(f"(A message from Mint's own screen watcher, not from the user.) {watch.report(how)}")
    threading.Thread(target=later, daemon=True, name="wait-until-done").start()
    return (f"{watch.name} is still working after {int(SYNC_WAIT)}s. You will get a message the moment it is "
            "done - do NOT read or act on its result before that. Tell the user in a few words that you are "
            "waiting for it, then stop and wait.")


# --- serving a built site -----------------------------------------------------------------

_servers: dict[str, tuple[http.server.ThreadingHTTPServer, int]] = {}


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")   # show the latest build, not a cached one
        super().end_headers()


def _serve(folder: Path) -> int:
    key = str(folder)
    if key in _servers:
        return _servers[key][1]
    handler = functools.partial(_Quiet, directory=key)
    for port in list(range(8000, 8010)) + list(range(8765, 8800)):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True, name=f"preview-{port}").start()
        _servers[key] = (server, port)
        return port
    raise OSError("no free port between 8000 and 8800")


def _check(url: str, folder: Path) -> str:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as error:
        return f"The page answered HTTP {error.code}."
    except Exception as error:
        return f"The page did not load: {error}"
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    title = html.unescape(title.group(1).strip()) if title else "(no title)"
    refs = set(re.findall(r"""(?:src|href)\s*=\s*["']([^"'#?]+)""", body, re.I))
    local = sorted(r for r in refs if not re.match(r"^(https?:|//|data:|mailto:|tel:|javascript:)", r, re.I))
    missing = [r for r in local if not (folder / r.lstrip("/")).exists()]
    scripts = len(re.findall(r"<script", body, re.I))
    note = (f" MISSING local files it refers to: {', '.join(missing[:6])}." if missing else
            (f" All {len(local)} local files it refers to exist." if local else ""))
    return f"HTTP {status}, title '{title[:80]}', {len(body) // 1024 or 1} KB, {scripts} script tag(s).{note}"


def preview_site(args: dict) -> str:
    raw = str(args.get("path", "")).strip()
    if not raw:
        return "No path given."
    path = Path(raw).expanduser()
    if not path.exists():
        return f"FAILED: {path} does not exist."
    if path.is_dir():
        folder = path
        pages = [p for p in (folder / "index.html",) if p.exists()] or sorted(folder.glob("*.html")) \
            or sorted(folder.rglob("index.html"))
        if not pages:
            return f"FAILED: no HTML page in {folder}."
        page = pages[0]
    else:
        folder, page = path.parent, path
    try:
        port = _serve(folder)
    except OSError as error:
        return f"FAILED: could not start a local server: {error}"
    url = f"http://localhost:{port}/{page.relative_to(folder).as_posix()}"
    check = _check(url, folder)
    from mint.tools import macos
    opened = macos.open_url(url)
    title = re.search(r"title '([^']*)'", check)
    shown = _showing(title.group(1) if title else "")
    print(f"  [preview_site: {folder} -> {url}; {check}; {shown}]", flush=True)
    return (f"Serving {folder} at {url} ({opened}). Check: {check} {shown} Now use look to see whether it "
            "renders properly, and tell the user what you see.")


def _showing(title: str) -> str:
    """Is the browser in front really showing the page? In testing the address sat
    typed in a new tab, unopened, and the model reported the site as checked."""
    import AppKit
    import ApplicationServices as AX
    if not title or title == "(no title)":
        return ""
    for _ in range(12):
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None:
            element = AX.AXUIElementCreateApplication(front.processIdentifier())
            window = _ax(element, "AXFocusedWindow") or _ax(element, "AXMainWindow")
            if title[:40].lower() in str(_ax(window, "AXTitle") or "").lower():
                return "The browser in front is showing it."
        time.sleep(0.5)
    return ("WARNING: the browser in front is NOT showing this page (its window title does not match) - "
            "look at the screen and open it before saying it was checked.")


HANDLERS = {"wait_until_done": wait_until_done, "preview_site": preview_site}
