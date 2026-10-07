"""Mint's web engine in the user's own Chrome through Chrome's scripting ("Allow JavaScript from Apple Events").

That switch is the one way into the user's Chrome that is allowed once and stays allowed: Chrome keeps it on across
restarts and never asks per connection (remote debugging asks "Allow remote debugging?" on every connection). The
same page snapshot and Jev loop run here (webgoal.Task); this module only stands in for the CDP tab:

- one long-lived JXA process per Mint (`osascript -l JavaScript`), talking JSON lines over its stdin/stdout, so
  each call is one Apple Event (~10-30 ms), not a new osascript (~150+ ms);
- a background tab of the window that is signed in to the site (its tabs there), so the user's view stays;
- input as page events: clicks, Enter, select-all, and typing through execCommand('insertText') (which fires the
  same input events as typing; a value setter if a page refuses it);
- waits polled from here (a mutation counter on the page): background tabs get no animation frames and slow timers.

Mint never turns the switch on itself (a Chrome security setting): it tells the user where it is, once.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.parse

from mint.tools.cdp import CDPError

log = logging.getLogger("mint.tools.chrome_script")

SWITCH = "View > Developer > Allow JavaScript from Apple Events"
SETUP_STEPS = ("In Chrome's menu bar, choose View, then Developer, then 'Allow JavaScript from Apple Events'. "
               "You only do this once - Chrome remembers it, and I won't need to ask again.")

_JXA = r"""
ObjC.import('Foundation');
function handle(chrome, m) {
  if (m.op === 'ping') return 'pong';
  if (m.op === 'windows') return chrome.windows().map(w => ({id: w.id(), tabs: w.tabs().map(t => t.url()),
                                                              title: w.activeTab().title()}));
  if (m.op === 'newwindow') { const w = chrome.Window().make(); return w.id(); }
  if (m.op === 'probe') { const w = chrome.windows[0]; return w.activeTab.execute({javascript: '1+1'}); }
  const w = chrome.windows.byId(m.w);
  if (m.op === 'new') {
    const before = w.activeTabIndex();
    w.tabs.push(chrome.Tab({url: m.url}));
    const tabs = w.tabs();
    const id = tabs[tabs.length - 1].id();
    if (!m.front) w.activeTabIndex = before;
    return id;
  }
  const t = w.tabs.byId(m.t);
  if (m.op === 'js') return t.execute({javascript: m.code});
  if (m.op === 'state') return {url: t.url(), loading: t.loading(), title: t.title()};
  if (m.op === 'go') { t.url = m.url; return true; }
  if (m.op === 'close') { t.close(); return true; }
  if (m.op === 'show') { const tabs = w.tabs(); for (let i = 0; i < tabs.length; i++) if (tabs[i].id() === m.t)
                           w.activeTabIndex = i + 1; return true; }
  throw new Error('unknown op ' + m.op);
}
function run() {
  const chrome = Application('Google Chrome');
  const input = $.NSFileHandle.fileHandleWithStandardInput;
  const output = $.NSFileHandle.fileHandleWithStandardOutput;
  let buffer = '';
  while (true) {
    const data = input.availableData;
    if (data.length === 0) break;
    buffer += $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js;
    let nl;
    while ((nl = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, nl);
      buffer = buffer.slice(nl + 1);
      let reply;
      try { reply = {ok: true, value: handle(chrome, JSON.parse(line))}; }
      catch (e) { reply = {ok: false, error: String(e)}; }
      output.writeData($(JSON.stringify(reply) + '\n').dataUsingEncoding($.NSUTF8StringEncoding));
    }
  }
}
"""

# Page side: input as events, and a counter of DOM changes for waits polled from Mint.
INPUT_JS = r"""(() => {
  if (window.__mintInput) return;
  window.__mintChanges = 0;
  new MutationObserver(() => { window.__mintChanges++; })
    .observe(document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
  let pressed = null;
  const mouse = (el, type, x, y) => el.dispatchEvent(new (type.startsWith('pointer') ? PointerEvent : MouseEvent)(
    type, {bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, view: window, button: 0,
           buttons: type.endsWith('down') ? 1 : 0, pointerId: 1, pointerType: 'mouse', isPrimary: true}));
  const key = (el, type, k, code, kc) => el.dispatchEvent(new KeyboardEvent(type, {bubbles: true, cancelable: true,
    composed: true, key: k, code: code, keyCode: kc, which: kc}));
  window.__mintInput = (c) => {
    const at = c.x !== undefined ? document.elementFromPoint(c.x, c.y) : null;
    const active = document.activeElement || document.body;
    if (c.op === 'moved' && at) { mouse(at, 'pointerover', c.x, c.y); mouse(at, 'mouseover', c.x, c.y);
                                  mouse(at, 'pointermove', c.x, c.y); mouse(at, 'mousemove', c.x, c.y); }
    else if (c.op === 'pressed' && at) {
      pressed = at; mouse(at, 'pointerdown', c.x, c.y); mouse(at, 'mousedown', c.x, c.y);
      const focusable = at.closest('input,textarea,select,button,a[href],[tabindex],[contenteditable=""],[contenteditable="true"]');
      if (focusable) focusable.focus({preventScroll: true});
    } else if (c.op === 'released') {
      const el = pressed && pressed.isConnected ? pressed : at; pressed = null;
      if (!el) return false;
      mouse(el, 'pointerup', c.x, c.y); mouse(el, 'mouseup', c.x, c.y); mouse(el, 'click', c.x, c.y);
    } else if (c.op === 'wheel') { window.scrollBy(0, c.dy); }
    else if (c.op === 'enter') {
      const go = key(active, 'keydown', 'Enter', 'Enter', 13);
      key(active, 'keypress', 'Enter', 'Enter', 13);
      if (go && active.form && active.tagName === 'INPUT') {
        if (active.form.requestSubmit) active.form.requestSubmit(); else active.form.submit();
      }
      key(active, 'keyup', 'Enter', 'Enter', 13);
    } else if (c.op === 'selectall') {
      if (active.select) active.select(); else document.execCommand('selectAll');
    } else if (c.op === 'text') {
      const before = active.value;
      const done = document.execCommand('insertText', false, c.text);
      if ((!done || (before !== undefined && active.value === before && c.text)) && 'value' in active) {
        const proto = active.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(active, c.text);
        active.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: c.text}));
        active.dispatchEvent(new Event('change', {bubbles: true}));
      }
    }
    return true;
  };
})()"""


class _Runner:
    """The JXA process (one per Mint, restarted if it dies)."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def ask(self, message: dict, timeout: float = 10.0):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.proc = subprocess.Popen(["osascript", "-l", "JavaScript", "-e", _JXA], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            proc = self.proc
            try:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                self.proc = None
                raise CDPError(f"Chrome scripting stopped: {error}") from None
            done: list = []
            reader = threading.Thread(target=lambda: done.append(proc.stdout.readline()), daemon=True)
            reader.start()
            reader.join(timeout)
            if not done or not done[0]:
                proc.kill()
                self.proc = None
                raise CDPError(f"Chrome didn't answer a script in {timeout:g} s")
        reply = json.loads(done[0])
        if not reply.get("ok"):
            raise CDPError(_plain(reply.get("error", "")))
        return reply.get("value")

    def close(self) -> None:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill()
            self.proc = None


def _plain(error: str) -> str:
    if "Allow JavaScript" in error or "turned off" in error or "(12)" in error:
        return "Chrome's 'Allow JavaScript from Apple Events' is off"
    if "-1743" in error or "Not authorized" in error or "not allowed" in error:
        return "macOS hasn't allowed Mint to control Chrome (System Settings > Privacy & Security > Automation)"
    return error[:200]


_runner = _Runner()


def available(make_window: bool = False) -> bool:
    """Chrome's scripting switch is on (tried on a real tab - no prompt). It can only be tried in a window:
    with `make_window` (the user asked for a task in their Chrome), one is opened if Chrome has none."""
    for attempt in range(2):
        try:
            return str(_runner.ask({"op": "probe"}, timeout=6)) in ("2", "2.0")
        except Exception as error:
            if attempt == 0 and make_window and "Invalid index" in str(error):
                try:
                    _runner.ask({"op": "newwindow"}, timeout=6)
                    time.sleep(0.5)
                    continue
                except Exception:
                    pass
            log.info("Chrome scripting not available: %s", error)
            return False
    return False


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url or "").hostname or "").lower().removeprefix("www.")


class Browser:
    """Stands in for cdp.Browser (kind 'script'): the user's Chrome through its scripting."""

    kind = "script"

    def __init__(self) -> None:
        self.tabs: set = set()
        self.last_pick: dict | None = None
        self.last_used = time.monotonic()

    @property
    def alive(self) -> bool:
        return True

    def window_for(self, url: str) -> dict:
        """The window signed in to the site (it has a tab there), else the frontmost; one is opened if none."""
        windows = _runner.ask({"op": "windows"}) or []
        if not windows:
            _runner.ask({"op": "newwindow"})
            windows = _runner.ask({"op": "windows"}) or []
            if not windows:
                raise CDPError("Chrome has no window")
        host = _host(url)
        from mint.tools.cdp import _same_site
        scored = [(sum(1 for t in w["tabs"] if host and _same_site(_host(t), host)), -i, w)
                  for i, w in enumerate(windows)]
        best = max(scored, key=lambda item: item[:2])
        self.last_pick = {"site": host, "windows": len(windows), "tabs_on_site": best[0]}
        return best[2]

    def new_tab(self, url: str = "about:blank", visible: bool = False) -> "Tab":
        window = self.window_for(url)
        tab_id = _runner.ask({"op": "new", "w": window["id"], "url": url or "about:blank", "front": visible})
        tab = Tab(self, window["id"], tab_id)
        tab.profile = _profile_of(window.get("title", ""))
        self.tabs.add(tab_id)
        try:
            tab.wait_loaded()
            tab.prepare()
        except Exception:
            tab.close()
            raise
        return tab

    def close(self) -> None:
        pass


def _profile_of(active_title: str) -> str:
    from mint.tools import cdp
    if not active_title or not cdp._several_profiles():
        return ""
    for title in cdp.chrome_window_titles():
        page, sep, profile = title.rpartition(" - Google Chrome – ")
        if sep and page == active_title and profile:
            return profile
    return ""


class Tab:
    """Stands in for cdp.Tab: js(), go(), call() for the few Input methods webgoal uses, close()."""

    def __init__(self, browser: Browser, window: int, tab: int) -> None:
        self.b, self.window, self.tab = browser, window, tab
        self.closed = False
        self.profile = ""

    def _ask(self, op: str, timeout: float = 10.0, **extra):
        if self.closed:
            raise CDPError("the tab is closed")
        self.b.last_used = time.monotonic()
        return _runner.ask({"op": op, "w": self.window, "t": self.tab, **extra}, timeout=timeout)

    def js(self, expression: str, timeout: float = 10.0, await_promise: bool = False):
        if await_promise:
            raise CDPError("promises are waited for with settle() in Chrome scripting")
        code = ("(function(){try{return JSON.stringify({v:(" + expression + ")})}"
                "catch(e){return JSON.stringify({e:String(e)})}})()")
        raw = self._ask("js", timeout, code=code)
        if raw in (None, ""):
            return None
        try:
            out = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if "e" in out:
            raise CDPError(f"page script: {out['e'][:160]}")
        return out.get("v")

    def prepare(self, visible: bool = False) -> None:
        self.js(INPUT_JS)

    def settle(self, autocomplete: bool = False, cap: float = 0.45) -> None:
        """After an input: wait for the page to start changing (<= 250 ms) and then 60 ms of quiet, capped -
        polled from here, since a background tab has no animation frames and slow timers."""
        began = time.monotonic()
        try:
            last_count = self.js("window.__mintChanges||0")
        except CDPError:
            return
        last_change, seen = began, False
        while time.monotonic() - began < cap:
            time.sleep(0.04)
            try:
                count = self.js("window.__mintChanges||0")
            except CDPError:
                return
            now = time.monotonic()
            if count != last_count:
                last_count, last_change, seen = count, now, True
            elif (seen and now - last_change > 0.06) or (not seen and now - began > 0.25):
                return

    def go(self, url: str, wait: float = 15.0) -> None:
        self._ask("go", url=url)
        time.sleep(0.15)
        self.wait_loaded(wait)
        self.prepare()

    def wait_loaded(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._ask("state") or {}
            if not state.get("loading"):
                return
            time.sleep(0.1)

    def call(self, method: str, params: dict | None = None, timeout: float = 15.0):
        p = params or {}
        if method == "Input.dispatchMouseEvent":
            kind = {"mouseMoved": "moved", "mousePressed": "pressed", "mouseReleased": "released",
                    "mouseWheel": "wheel"}[p["type"]]
            command = {"op": kind, "x": p.get("x"), "y": p.get("y"), "dy": p.get("deltaY", 0)}
        elif method == "Input.dispatchKeyEvent":
            if p.get("type") != "keyDown":
                return {}
            command = {"op": "selectall"} if "selectAll" in (p.get("commands") or []) else \
                {"op": "enter"} if p.get("key") == "Enter" else None
            if command is None:
                return {}
        elif method == "Input.insertText":
            command = {"op": "text", "text": p.get("text", "")}
        else:
            raise CDPError(f"{method} isn't available through Chrome scripting")
        self.js(INPUT_JS)                         # the page may have navigated since: install again (idempotent)
        self.js("window.__mintInput(" + json.dumps(command) + ")")
        return {}

    def show(self) -> None:
        self._ask("show")

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._ask("close")
        except CDPError:
            pass
        self.closed = True
        self.b.tabs.discard(self.tab)


_browser = [None]


def get() -> Browser:
    if _browser[0] is None:
        _browser[0] = Browser()
    return _browser[0]


def shutdown() -> None:
    _runner.close()
