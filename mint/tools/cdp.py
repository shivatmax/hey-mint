"""Chrome DevTools Protocol for Mint's web engine (webgoal.py): one connection to a browser, many tabs.

Two ways in:

* Mint's own Chrome - a profile of its own (~/Library/Application Support/Mint/web-profile), started by Mint and
  driven over --remote-debugging-pipe (file descriptors 3 and 4: no network port anyone else could use). Headless
  (nothing on screen) or as a window the user can watch. Not signed in to the user's accounts.
* The user's own Chrome - with their logins - over the local DevTools WebSocket that Chrome opens once the user
  ticks "Allow remote debugging for this browser instance" at chrome://inspect/#remote-debugging. Chrome then
  asks "Allow remote debugging?" once per browser session; the user clicks Allow. Mint never clicks it.

Each task gets its own tab (a background tab in the user's Chrome, so their window never changes), with focus
emulation so it keeps rendering while hidden. Several tasks run in parallel on one connection.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from mint.core import config

log = logging.getLogger("mint.tools.cdp")

PROFILE = config.PROJECT_ROOT / "web-profile"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_CHROME = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
WINDOW = (1280, 860)
IDLE_QUIT = 10 * 60          # Mint's own Chrome quits after this long with no task (memory back)


class CDPError(Exception):
    pass


class ConsentNeeded(CDPError):
    """The user's Chrome is not open to Mint yet: `how` says what the user must do (once)."""

    def __init__(self, how: str, step: str) -> None:
        super().__init__(how)
        self.how, self.step = how, step


# --- transports ---------------------------------------------------------------------------------------------

class _Transport:
    """Request/response matching and events, over any byte channel (pipe or socket)."""

    def __init__(self) -> None:
        self._next = 0
        self._lock = threading.Lock()
        self._waiting: dict[int, list] = {}
        self._handlers: dict[str, list] = {}
        self.closed = threading.Event()

    def on(self, method: str, fn) -> None:
        self._handlers.setdefault(method, []).append(fn)

    def call(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 15.0):
        with self._lock:
            self._next += 1
            ident = self._next
            slot = [threading.Event(), None]
            self._waiting[ident] = slot
        message = {"id": ident, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        if self.closed.is_set():
            raise CDPError("the browser connection is closed")
        self._send(json.dumps(message))
        if not slot[0].wait(timeout):
            with self._lock:
                self._waiting.pop(ident, None)
            raise CDPError(f"{method}: no answer from the browser in {timeout:g} s")
        reply = slot[1]
        if reply is None:
            raise CDPError(f"{method}: the browser closed")
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result") or {}

    def post(self, method: str, params: dict | None = None, session: str | None = None) -> None:
        """Send without waiting for the answer (safe from an event handler, which runs on the reader)."""
        with self._lock:
            self._next += 1
            ident = self._next
        message = {"id": ident, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        if not self.closed.is_set():
            self._send(json.dumps(message))

    def _dispatch(self, message: dict) -> None:
        if "id" in message:
            with self._lock:
                slot = self._waiting.pop(message["id"], None)
            if slot is not None:
                slot[1] = message
                slot[0].set()
            return
        for fn in list(self._handlers.get(message.get("method", ""), [])):
            try:
                fn(message.get("params") or {}, message.get("sessionId"))
            except Exception:
                log.debug("cdp handler failed", exc_info=True)

    def _closed(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        with self._lock:
            slots, self._waiting = list(self._waiting.values()), {}
        for slot in slots:
            slot[0].set()

    def _send(self, text: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self._closed()


class Pipe(_Transport):
    """--remote-debugging-pipe: NUL-separated JSON on two file descriptors."""

    def __init__(self, read_fd: int, write_fd: int) -> None:
        super().__init__()
        self._rfd, self._wfd = read_fd, write_fd
        self._out: queue.Queue = queue.Queue()
        threading.Thread(target=self._read_loop, name="web-cdp-read", daemon=True).start()
        threading.Thread(target=self._write_loop, name="web-cdp-write", daemon=True).start()

    def _send(self, text: str) -> None:
        self._out.put(text.encode() + b"\0")

    def _write_loop(self) -> None:
        while not self.closed.is_set():
            data = self._out.get()
            if data is None:
                break
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(self._wfd, view):]
            except OSError:
                break
        self._closed()

    def _read_loop(self) -> None:
        buf = bytearray()
        while True:
            try:
                chunk = os.read(self._rfd, 1 << 16)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while (end := buf.find(b"\0")) >= 0:
                raw, buf = bytes(buf[:end]), buf[end + 1:]
                try:
                    self._dispatch(json.loads(raw))
                except Exception:
                    log.debug("cdp message", exc_info=True)
        self._closed()

    def close(self) -> None:
        self._out.put(None)
        for fd in (self._rfd, self._wfd):
            try:
                os.close(fd)
            except OSError:
                pass
        self._closed()


class Socket(_Transport):
    """The DevTools WebSocket of a running Chrome (127.0.0.1 only)."""

    def __init__(self, url: str, open_timeout: float = 60.0) -> None:
        super().__init__()
        from websockets.sync.client import connect
        self._ws = connect(url, open_timeout=open_timeout, max_size=None, ping_interval=None,
                           additional_headers=None)
        self._send_lock = threading.Lock()
        threading.Thread(target=self._read_loop, name="web-cdp-socket", daemon=True).start()

    def _send(self, text: str) -> None:
        with self._send_lock:
            try:
                self._ws.send(text)
            except Exception as error:
                self._closed()
                raise CDPError(f"the browser connection dropped: {error}") from None

    def _read_loop(self) -> None:
        try:
            for raw in self._ws:
                try:
                    self._dispatch(json.loads(raw))
                except Exception:
                    log.debug("cdp message", exc_info=True)
        except Exception:
            log.debug("cdp socket closed", exc_info=True)
        self._closed()

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass
        self._closed()


def chrome_window_titles() -> list[str]:
    """The titles of the user's Chrome windows, as macOS shows them (they carry the profile name)."""
    try:
        from AppKit import NSRunningApplication
        from mint.screen import axkit
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.google.Chrome")
        titles = []
        for app in apps or []:
            for window in axkit.top_level(app.processIdentifier()):
                title = axkit.attr(window, "AXTitle")
                if title:
                    titles.append(str(title))
        return titles
    except Exception:
        log.debug("could not read Chrome's window titles", exc_info=True)
        return []


def _several_profiles() -> bool:
    try:
        from mint.tools import apps
        return len(apps.chrome_profiles()) > 1
    except Exception:
        return True                            # can't tell: naming the profile does no harm


def _same_site(a: str, b: str) -> bool:
    """mail.google.com and google.com are one site; google.com and evil-google.com are not."""
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


class Bridged(_Transport):
    """The user's Chrome through chrome_bridge: the connection Chrome allowed outlives Mint's restarts."""

    def __init__(self, ws_url: str, open_timeout: float = 125.0) -> None:
        super().__init__()
        import socket
        sock_path, key = _bridge_address()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(5.0)
        self._sock.connect(str(sock_path))
        self._sock.sendall(json.dumps({"key": key, "ws": ws_url}).encode() + b"\n")
        self._sock.settimeout(open_timeout)               # Chrome may be asking the user right now
        self._reader = self._sock.makefile("rb")
        answer = json.loads(self._reader.readline() or b"{}")
        self._sock.settimeout(None)
        if not answer.get("ok"):
            self._sock.close()
            step = answer.get("step") or "denied"
            raise ConsentNeeded(MESSAGES.get(step, MESSAGES["denied"]), step)
        self.fresh = bool(answer.get("fresh"))
        self._send_lock = threading.Lock()
        threading.Thread(target=self._read_loop, name="web-cdp-bridge", daemon=True).start()

    def _send(self, text: str) -> None:
        with self._send_lock:
            try:
                self._sock.sendall(text.encode() + b"\n")
            except OSError as error:
                self._closed()
                raise CDPError(f"the browser connection dropped: {error}") from None

    def _read_loop(self) -> None:
        try:
            for raw in self._reader:
                if raw.strip():
                    try:
                        self._dispatch(json.loads(raw))
                    except Exception:
                        log.debug("cdp message", exc_info=True)
        except Exception:
            log.debug("bridge closed", exc_info=True)
        self._closed()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._closed()


def _bridge_address() -> tuple[Path, str]:
    from mint.tools import chrome_bridge
    sock_path, key_path = chrome_bridge.paths()
    return sock_path, key_path.read_text().strip()


def _bridge_ask(hello: dict, timeout: float = 2.0) -> dict | None:
    import socket
    try:
        sock_path, key = _bridge_address()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            s.sendall(json.dumps({**hello, "key": key}).encode() + b"\n")
            return json.loads(s.makefile("rb").readline() or b"{}")
    except (OSError, ValueError):
        return None


def _start_bridge(wait: float = 6.0) -> bool:
    """The bridge is running (started now if needed): a process of its own that outlives Mint."""
    if _bridge_ask({"probe": True}) is not None:
        return True
    import subprocess as sp
    import sys
    package = __name__.rsplit(".", 1)[0]
    log_path = config.PROJECT_ROOT / "chrome-bridge.log"
    try:
        with open(log_path, "ab") as out:
            sp.Popen([sys.executable, "-m", f"{package}.chrome_bridge"], stdin=sp.DEVNULL, stdout=out,
                     stderr=out, start_new_session=True, close_fds=True,
                     env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)})
    except Exception:
        log.exception("could not start the Chrome bridge")
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.15)
        if _bridge_ask({"probe": True}) is not None:
            return True
    return False


def needs_allow() -> bool:
    """Connecting to the user's Chrome now would make Chrome ask 'Allow remote debugging?' (no connection
    it allowed is being kept)."""
    if connected():
        return False
    answer = _bridge_ask({"probe": True})
    return not (answer or {}).get("chrome")


# --- browsers -----------------------------------------------------------------------------------------------

def chrome_path() -> str | None:
    for path in (CHROME, shutil.which("google-chrome") or ""):
        if path and os.path.exists(path):
            return path
    return None


class Browser:
    """One browser connection. kind: 'headless' / 'window' (Mint's own Chrome) or 'chrome' (the user's)."""

    def __init__(self, kind: str, transport: _Transport, proc=None) -> None:
        self.kind, self.t, self.proc = kind, transport, proc
        self.tabs: set[str] = set()
        self.last_used = time.monotonic()
        self.lock = threading.Lock()
        self.last_pick: dict | None = None        # the user's Chrome: how the last tab's profile was chosen

    @property
    def alive(self) -> bool:
        if self.t.closed.is_set():
            return False
        return self.proc is None or self.proc.poll() is None

    def call(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 15.0):
        self.last_used = time.monotonic()
        return self.t.call(method, params, session, timeout)

    def new_tab(self, url: str = "about:blank", visible: bool = False) -> "Tab":
        params = {"url": "about:blank"}
        if self.kind == "chrome" and not visible:
            params["background"] = True               # the user's own window stays as it is
            context = self.profile_for(url)
            if context:
                params["browserContextId"] = context
        target = self.call("Target.createTarget", params)["targetId"]
        session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        tab = Tab(self, target, session)
        if self.kind == "chrome":
            tab.profile = self.profile_name(target)
        self.tabs.add(target)
        try:
            tab.prepare(visible)
            if url and url != "about:blank":
                tab.go(url)
        except Exception:
            tab.close()
            raise
        return tab

    def profile_for(self, url: str) -> str | None:
        """With several Chrome profiles open, the one signed in to this site (its tabs there, its cookies for
        it - only counted, never read). Chrome's own default is whichever profile was used last, which may be
        another account. -> a browser context id, or None for Chrome's default."""
        host = (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
        if not host:
            return None
        try:
            infos = self.call("Target.getTargets").get("targetInfos", [])
        except CDPError:
            return None
        contexts: dict[str, float] = {}
        for info in infos:
            ctx = info.get("browserContextId")
            if not ctx or info.get("type") != "page":
                continue
            tab_host = (urllib.parse.urlsplit(info.get("url", "")).hostname or "").lower().removeprefix("www.")
            contexts[ctx] = contexts.get(ctx, 0) + (5 if tab_host and _same_site(tab_host, host) else 0)
        self.last_pick = {"site": host, "profiles": len(contexts)}
        if len(contexts) < 2:
            return None
        for ctx in contexts:
            try:
                cookies = self.call("Storage.getCookies", {"browserContextId": ctx}).get("cookies", [])
            except CDPError:
                continue
            contexts[ctx] += sum(1 for c in cookies if _same_site(str(c.get("domain", "")).lstrip(".").lower(), host))
        ranked = sorted(contexts.items(), key=lambda kv: -kv[1])
        self.last_pick["scores"] = sorted(contexts.values(), reverse=True)
        if ranked[0][1] == 0 or ranked[0][1] == ranked[1][1]:
            return None                                # nothing to go on, or a tie: Chrome's default
        log.info("user's Chrome: %s goes to the profile signed in there (%s)", host, ranked[0][0][:8])
        return ranked[0][0]

    def profile_name(self, target: str) -> str:
        """The name of the Chrome profile a tab is in ('' if unknown), so the user hears which account was
        used. Chrome's window titles end with the profile ('Inbox - Google Chrome – Work'); a window showing
        a tab of the same profile gives the name."""
        try:
            infos = self.call("Target.getTargets").get("targetInfos", [])
        except CDPError:
            return ""
        context = next((i.get("browserContextId") for i in infos if i.get("targetId") == target), None)
        if not context or not _several_profiles():
            return ""                          # one profile: nothing worth saying
        titles = {i.get("title", "") for i in infos
                  if i.get("browserContextId") == context and i.get("type") == "page" and i.get("targetId") != target}
        for window in chrome_window_titles():
            page, sep, profile = window.rpartition(" - Google Chrome – ")
            if sep and page in titles and profile:
                return profile
        return ""

    def close(self) -> None:
        self.t.close()
        if self.proc is not None and self.proc.poll() is None:
            try:
                import signal
                os.kill(self.proc.pid, signal.SIGTERM)
                self.proc.wait(5)
            except Exception:
                try:
                    os.kill(self.proc.pid, 9)
                except OSError:
                    pass


class Tab:
    def __init__(self, browser: Browser, target: str, session: str) -> None:
        self.b, self.target, self.session = browser, target, session
        self.closed = False
        self.profile = ""                     # the user's Chrome: the profile it opened in, by its name

    def call(self, method: str, params: dict | None = None, timeout: float = 15.0):
        if self.closed:
            raise CDPError("the tab was closed")
        return self.b.call(method, params, self.session, timeout)

    def prepare(self, visible: bool) -> None:
        self.call("Page.enable")
        self.call("Runtime.enable")
        # Keep rendering (animation frames, menus) while the tab is hidden or the browser is headless.
        self.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        if self.b.kind != "chrome":
            width, height = WINDOW
            self.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                             "deviceScaleFactor": 1, "mobile": False})
        if self.b.kind == "headless":
            # Sites turn away "HeadlessChrome": present as the ordinary Chrome it is.
            agent = self.call("Runtime.evaluate", {"expression": "navigator.userAgent",
                                                   "returnByValue": True})["result"].get("value", "")
            if "Headless" in agent:
                self.call("Emulation.setUserAgentOverride", {"userAgent": agent.replace("HeadlessChrome",
                                                                                        "Chrome")})
        if visible:
            front = _front_app()
            try:
                self.b.call("Target.activateTarget", {"targetId": self.target})
            except CDPError:
                pass
            if self.b.kind == "window":
                _give_back(front, 0.3)

    def go(self, url: str, wait: float = 15.0) -> None:
        reply = self.call("Page.navigate", {"url": url})
        if reply.get("errorText"):
            raise CDPError(f"could not open {url}: {reply['errorText']}")
        self.wait_loaded(wait)

    def wait_loaded(self, timeout: float = 15.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if self.js("document.readyState") in ("interactive", "complete"):
                    return
            except CDPError:
                pass
            time.sleep(0.03)

    def js(self, expression: str, timeout: float = 10.0, await_promise: bool = False):
        r = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                           "awaitPromise": await_promise}, timeout)
        if r.get("exceptionDetails"):
            raise CDPError("page script: " + str(r["exceptionDetails"].get("text") or "")[:160])
        return (r.get("result") or {}).get("value")

    def show(self) -> None:
        """Bring this tab to the front of its window (the user wants to watch or take over)."""
        self.b.call("Target.activateTarget", {"targetId": self.target})

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.b.tabs.discard(self.target)
        try:
            self.b.call("Target.closeTarget", {"targetId": self.target}, timeout=5)
        except CDPError:
            pass


# --- getting a browser ------------------------------------------------------------------------------------

_browsers: dict[str, Browser] = {}
_get_lock = threading.Lock()


def get(kind: str) -> Browser:
    """A live connection of this kind, started or reconnected as needed. May raise ConsentNeeded."""
    with _get_lock:
        found = _browsers.get(kind)
        if found is not None and found.alive:
            return found
        if found is not None:
            found.close()
        if kind == "chrome":
            browser = _attach_user_chrome()
        else:
            other = "window" if kind == "headless" else "headless"
            mate = _browsers.get(other)
            if mate is not None and mate.alive:
                # One profile, one Chrome: switching mode restarts it when no task is using it.
                if mate.tabs:
                    raise CDPError(f"Mint's browser is busy as a {other} browser; try again when its task ends.")
                mate.close()
                _browsers.pop(other, None)
            browser = _launch_own(kind == "headless")
        _browsers[kind] = browser
        _start_reaper()
        return browser


def _launch_own(headless: bool) -> Browser:
    path = chrome_path()
    if not path:
        raise CDPError("Google Chrome isn't installed - Mint's web engine uses it. Install it from google.com/chrome.")
    PROFILE.mkdir(parents=True, exist_ok=True)
    width, height = WINDOW
    args = [path, f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check", "--lang=en-US",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows", "--disable-features=Translate,MediaRouter",
            "--password-store=basic", f"--window-size={width},{height}"]
    if headless:
        args.append("--headless=new")
    elif os.environ.get("MINT_WEB_WINDOW_POS"):           # tests: a real window, placed off screen
        args.append(f"--window-position={os.environ['MINT_WEB_WINDOW_POS']}")
    front = None if headless else _front_app()
    to_r, to_w = os.pipe()
    from_r, from_w = os.pipe()
    # Out of the way of 3 and 4 before the child gets them there (posix_spawn, as Google Meet starts its Chrome:
    # Chrome is its own "responsible process" for macOS privacy, not Mint).
    hi_r = fcntl.fcntl(to_r, fcntl.F_DUPFD_CLOEXEC, 20)
    hi_w = fcntl.fcntl(from_w, fcntl.F_DUPFD_CLOEXEC, 20)
    os.close(to_r)
    os.close(from_w)
    try:
        from mint.app.meet_call import spawn
        proc = spawn(args + ["--remote-debugging-pipe", "about:blank"], {3: hi_r, 4: hi_w})
    except Exception as error:
        os.close(from_r)
        os.close(to_w)
        raise CDPError(f"Mint's Chrome did not start: {error}") from None
    finally:
        os.close(hi_r)
        os.close(hi_w)
    pipe = Pipe(from_r, to_w)
    browser = Browser("headless" if headless else "window", pipe, proc)
    for _ in range(100):                         # the pipe answers once the browser is up
        try:
            browser.call("Browser.getVersion", timeout=1.0)
            _give_back(front)
            return browser
        except CDPError:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
    browser.close()
    raise CDPError("Mint's Chrome did not start.")


def _front_app():
    try:
        from AppKit import NSWorkspace
        return NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return None


def _give_back(app, delay: float = 0.6) -> None:
    """Mint's Chrome window opened: the app the user was in gets the keyboard back (they can watch it work
    without it taking their typing)."""
    if app is None:
        return

    def later() -> None:
        time.sleep(delay)
        try:
            if not app.isTerminated():
                app.activateWithOptions_(0)
        except Exception:
            pass
    threading.Thread(target=later, daemon=True, name="web-give-back").start()


def consent_state() -> str:
    """'ready' (the box is ticked and Chrome is listening), 'toggle' (the user must tick the box once at
    chrome://inspect/#remote-debugging), 'closed' (Chrome isn't running), 'blocked' (macOS doesn't let Mint
    read Chrome's settings folder, so it can't tell), 'missing' (Chrome isn't installed)."""
    if not chrome_path():
        return "missing"
    if not _chrome_running():
        return "closed"
    try:
        return "ready" if _active_port() is not None else "toggle"
    except PermissionError:
        return "blocked"


def connected() -> bool:
    """Mint is already connected to the user's Chrome (so Chrome won't ask again)."""
    found = _browsers.get("chrome")
    return found is not None and found.alive


def _chrome_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True, timeout=3).returncode == 0
    except Exception:
        return False


def _active_port() -> tuple[int, str] | None:
    """Chrome's debugging port and browser path, from the file Chrome writes when the box is ticked.
    Raises PermissionError when macOS keeps Mint out of Chrome's folder."""
    try:
        lines = (USER_CHROME / "DevToolsActivePort").read_text().splitlines()
        port, path = int(lines[0].strip()), lines[1].strip()
    except PermissionError:
        raise
    except (OSError, ValueError, IndexError):
        return None
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return None                          # stale file from an earlier run
    return port, path


def open_chrome(wait: float = 10.0) -> bool:
    """Start the user's Chrome in the background (it doesn't take the screen), for a task in their Chrome."""
    if _chrome_running():
        return True
    try:
        subprocess.run(["open", "-g", "-a", "Google Chrome"], capture_output=True, timeout=8)
    except Exception:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _chrome_running():
            time.sleep(1.0)                      # its debugging port (if the box is ticked) opens a moment later
            return True
        time.sleep(0.25)
    return False


def wait_ready(timeout: float, stopped=lambda: False) -> bool:
    """Wait for the user to tick the box (polled, no prompt). False on timeout or stop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stopped():
            return False
        try:
            if consent_state() == "ready":
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# Plain words for people who have never heard of "remote debugging": what to click, where, and why.
BOX = "Allow remote debugging for this browser instance"
SETUP_STEPS = (f"I opened a Chrome page for you. Tick the box that says '{BOX}'. Then Chrome asks once "
               "'Allow remote debugging?' - click Allow. That lets me use your Chrome with your logins; you can "
               "untick it any time on the same page.")
ALLOW_STEP = ("Chrome will ask 'Allow remote debugging?' in a moment - click Allow so I can use your Chrome "
              "(once - it asks again only after Chrome itself restarts).")
TOGGLE_HELP = SETUP_STEPS
MESSAGES = {
    "missing": "Google Chrome isn't installed. I can do this in my own browser instead, but you won't be signed in.",
    "closed": "Google Chrome didn't open. Open Chrome, then ask me again.",
    "blocked": ("Your Mac isn't letting me see Chrome's settings yet. Open System Settings, Privacy & Security, "
                "Full Disk Access, and turn on Mint - then ask me again."),
    "denied": "You said no in Chrome, so I didn't use it. Ask again if you change your mind.",
    "timeout": "Chrome's 'Allow' question wasn't answered, so I stopped. Ask again when you're ready.",
}


QUIET_AFTER_NO = 120.0       # after the user says no (or doesn't answer), Chrome isn't asked again this soon
_refused = [0.0, ""]


def _refusal(step: str) -> ConsentNeeded:
    _refused[:] = [time.monotonic(), step]
    return ConsentNeeded(MESSAGES[step], step)


def _attach_user_chrome() -> Browser:
    if _refused[0] and time.monotonic() - _refused[0] < QUIET_AFTER_NO:
        # A retry right after a no would put Chrome's question up again (and again): wait for the user.
        raise ConsentNeeded(MESSAGES[_refused[1]] + " (Not asking Chrome again for a couple of minutes.)",
                            _refused[1])
    state = consent_state()
    if state == "closed" and open_chrome():
        state = consent_state()
    if state == "toggle":
        raise ConsentNeeded(SETUP_STEPS, "toggle")
    if state != "ready":
        raise ConsentNeeded(MESSAGES.get(state, MESSAGES["closed"]), state)
    port, path = _active_port()
    url = f"ws://127.0.0.1:{port}{path}"
    if _start_bridge():
        try:
            return Browser("chrome", Bridged(url))         # Chrome asks only if the bridge has no connection
        except ConsentNeeded as error:
            raise _refusal(error.step if error.step in MESSAGES else "denied") from None
        except Exception:
            log.warning("the Chrome bridge failed; connecting directly", exc_info=True)
    started = time.monotonic()
    try:
        # Chrome holds the handshake until the user answers its "Allow remote debugging?" prompt.
        sock = Socket(url, open_timeout=120.0)
    except Exception as error:
        waited = time.monotonic() - started
        step = "timeout" if waited > 110 or "timed out" in str(error).lower() else "denied"
        log.info("user's Chrome refused the connection after %.0f s: %s", waited, error)
        raise _refusal(step) from None
    return Browser("chrome", sock)


def open_consent_page() -> bool:
    """Open chrome://inspect/#remote-debugging in the user's Chrome, in front, for them to tick the box
    themselves (an open one is brought forward instead of a second tab)."""
    url = "chrome://inspect/#remote-debugging"
    script = ('tell application "Google Chrome"\n activate\n if (count of windows) = 0 then make new window\n'
              ' repeat with w in windows\n set i to 0\n repeat with t in tabs of w\n set i to i + 1\n'
              ' if URL of t starts with "chrome://inspect" then\n set active tab index of w to i\n'
              ' set index of w to 1\n return\n end if\n end repeat\n end repeat\n'
              f' tell front window to make new tab with properties {{URL:"{url}"}}\n'
              'end tell')
    try:
        return subprocess.run(["osascript", "-e", script], capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


_reaper = [None]


def _start_reaper() -> None:
    if _reaper[0] is not None:
        return

    def run() -> None:
        while True:
            time.sleep(30)
            with _get_lock:
                for kind, b in list(_browsers.items()):
                    if b.kind != "chrome" and not b.tabs and time.monotonic() - b.last_used > IDLE_QUIT:
                        log.info("closing Mint's idle %s browser", kind)
                        b.close()
                        _browsers.pop(kind, None)
    _reaper[0] = threading.Thread(target=run, name="web-cdp-reaper", daemon=True)
    _reaper[0].start()


def shutdown() -> None:
    """Mint is quitting: close its own Chrome (the user's Chrome is only disconnected)."""
    try:
        from mint.tools import chrome_script
        chrome_script.shutdown()
        from mint.tools.live_decide import decider
        decider.close()
    except Exception:
        pass
    with _get_lock:
        for b in _browsers.values():
            b.close()
        _browsers.clear()
