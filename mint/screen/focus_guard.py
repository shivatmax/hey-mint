"""Focus guard: while Mint acts on an app in the background, no other app takes the front.

Pressing a button through Accessibility, opening a file or launching an app in the background does not ask
macOS to bring anything forward - but many apps bring themselves forward anyway (Safari after a value write,
Chrome and Calculator when they launch, a link that hands off to another app). The user, typing in their own
window, suddenly types into something else. A lease watches for that:

    with focus_guard.lease(target_pid) as held:     # at most 5 s
        ...the background action...
    result += held.note                              # '' or " (TextEdit took focus; put Notes back in front.)"

While a lease is open, an app activating itself - the target (unless allow_front=True: Mint means to bring
it forward), or any third app - makes the guard re-activate the app that was in front when the lease began.
It never fights the user: once they have typed or clicked during the lease (keyboard / mouse idle shorter
than the lease's age), it does nothing more. Mint's own windows are left alone, and it gives up after two
restores in one lease, so an app that insists wins rather than flickering.

The NSWorkspace activation observer runs on its own operation queue, never on the main thread, and the
restore itself on a short-lived thread. At the end of a lease the front app is checked once more, which also
covers a process without a running main loop (where no notification arrives).

Ideas from Cua Driver's focus-steal preventer and FocusGuard (MIT): a lease with a deadline, a short settle
before it ends, restore the prior front app. The code is Mint's own.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("mint.screen.focus_guard")

LEASE = 5.0            # the longest a lease guards, whatever the caller does
SETTLE = 0.06          # after the action: an activation already on its way is still caught
MAX_RESTORES = 2       # per lease: an app that keeps coming back wins, no flicker war
RESTORE_WAIT = 0.3     # for the polite re-activation before trying AppleScript


class Lease:
    def __init__(self, target_pid, allow_front: bool, previous: dict | None, started: float, seconds: float,
                 only_target: bool = False):
        self.target_pid = int(target_pid) if target_pid else None
        self.allow_front = allow_front
        self.only_target = only_target    # a third app coming forward is the action's result (a link, "Show in
                                          # Finder"): only the target pushing itself forward is undone
        self.previous = previous          # {pid, name, bundle, app} in front when the lease began
        self.started = started
        self.until = started + seconds
        self.closed = False
        self.restored = 0
        self.user_active = False
        self.stolen_by = ""

    @property
    def note(self) -> str:
        """For the action's result: what the guard did ('' when nothing)."""
        if not self.restored or not self.previous:
            return ""
        return f" ({self.stolen_by or 'Another app'} took focus; put {self.previous['name'] or 'the previous app'} " \
               "back in front.)"


def _hands_idle() -> float:
    """Seconds since the user last touched the keyboard or mouse."""
    try:
        import Quartz
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState,
                                                                   Quartz.kCGAnyInputEventType))
    except Exception:
        return 0.0                    # unknown: treat as "the user is active" - never fight


def _front() -> dict | None:
    import AppKit
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None
    return {"pid": int(app.processIdentifier()), "name": str(app.localizedName() or ""),
            "bundle": str(app.bundleIdentifier() or ""), "app": app}


def _reactivate(previous: dict) -> bool:
    """Bring the user's app back. From a background process macOS's cooperative activation often refuses
    NSRunningApplication.activate; an AppleScript `activate` from Mint's own process works (see
    ground.bring_forward), so that is the second try."""
    import AppKit
    app = previous.get("app")
    if app is None or app.isTerminated():
        return False
    pid = previous["pid"]
    if pid == os.getpid():
        from PyObjCTools import AppHelper
        AppHelper.callAfter(lambda: AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True))
        return True
    workspace = AppKit.NSWorkspace.sharedWorkspace()

    def in_front() -> bool:
        front = workspace.frontmostApplication()
        return front is not None and front.processIdentifier() == pid

    app.activateWithOptions_(0)
    deadline = time.monotonic() + RESTORE_WAIT
    while time.monotonic() < deadline:
        if in_front():
            return True
        time.sleep(0.03)
    bundle = previous.get("bundle") or ""
    if bundle:
        quoted = bundle.replace("\\", "\\\\").replace('"', '\\"')
        script = AppKit.NSAppleScript.alloc().initWithSource_(
            f'with timeout of 2 seconds\ntell application id "{quoted}" to activate\nend timeout')
        script.executeAndReturnError_(None)
    return in_front()


def _spawn(fn) -> None:
    threading.Thread(target=fn, name="focus-guard-restore", daemon=True).start()


class Guard:
    """The open leases and what to do when an app activates. Clock, idle time, the front app and the restore
    are plain callables, replaced in tests."""

    def __init__(self, clock=time.monotonic, idle=_hands_idle, front=_front, reactivate=_reactivate,
                 spawn=_spawn, own_pid: int | None = None, settle: float = SETTLE) -> None:
        self.clock, self.idle, self.front, self.reactivate, self.spawn = clock, idle, front, reactivate, spawn
        self.settle = settle
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self._leases: list[Lease] = []
        self._lock = threading.Lock()

    def open(self, target_pid=None, allow_front: bool = False, seconds: float = LEASE,
             only_target: bool = False) -> Lease:
        try:
            previous = self.front()
        except Exception:
            previous = None
        lease = Lease(target_pid, allow_front, previous, self.clock(), max(0.0, min(float(seconds), LEASE)),
                      only_target)
        with self._lock:
            self._leases.append(lease)
        return lease

    def close(self, lease: Lease) -> None:
        """End a lease - after one last look at the front app, in case its activation was not announced."""
        try:
            front = self.front()
        except Exception:
            front = None
        if front is not None and not lease.closed:
            self.activated(front["pid"], front["name"], only=lease)
        with self._lock:
            lease.closed = True
            if lease in self._leases:
                self._leases.remove(lease)

    def decide(self, lease: Lease, pid: int, now: float) -> str:
        """'restore', or why not (pure but for the idle reading)."""
        if lease.closed or now > lease.until:
            return "expired"
        if pid == self.own_pid:
            return "mint"
        previous = lease.previous
        if not previous:
            return "no previous app"
        if pid == previous["pid"]:
            return "previous app"
        if lease.allow_front and pid == lease.target_pid:
            return "allowed"
        if lease.only_target and pid != lease.target_pid:
            return "the action's result"
        if lease.user_active:
            return "user"
        try:
            idle = float(self.idle())
        except Exception:
            idle = 0.0
        if idle < now - lease.started:
            lease.user_active = True        # they typed or clicked since the lease began: their choice
            return "user"
        if lease.restored >= MAX_RESTORES:
            return "gave up"
        return "restore"

    def activated(self, pid: int, name: str = "", only: Lease | None = None) -> str:
        """An app became active. Returns what was done ('restore' or why not), for logs and tests."""
        now = self.clock()
        with self._lock:
            self._leases = [lease for lease in self._leases if not lease.closed and now <= lease.until]
            leases = [only] if only is not None else list(self._leases)
        verdict = "no lease"
        for lease in leases:
            verdict = self.decide(lease, pid, now)
            if verdict == "restore":
                lease.restored += 1
                lease.stolen_by = name or lease.stolen_by
                previous = lease.previous
                log.info("focus guard: %s (%s) took focus during a background action; restoring %s",
                         name, pid, previous["name"])
                self.spawn(lambda p=previous: self._restore(p))
                break                       # one restore per activation, whatever the number of leases
        return verdict

    def _restore(self, previous: dict) -> None:
        try:
            if not self.reactivate(previous):
                log.info("focus guard: could not bring %s back", previous.get("name"))
        except Exception:
            log.debug("focus guard restore failed", exc_info=True)


_guard = Guard()
_observer = None
_queue = None
_install_lock = threading.Lock()


def _install() -> None:
    """Watch app activations once per process, on a private queue (never the main thread)."""
    global _observer, _queue
    with _install_lock:
        if _observer is not None:
            return
        try:
            import AppKit
            center = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
            queue = AppKit.NSOperationQueue.alloc().init()
            queue.setName_("mint.screen.focus_guard")
            queue.setMaxConcurrentOperationCount_(1)

            def activated(note) -> None:
                try:
                    app = (note.userInfo() or {}).get(AppKit.NSWorkspaceApplicationKey)
                    if app is not None:
                        _guard.activated(int(app.processIdentifier()), str(app.localizedName() or ""))
                except Exception:
                    log.debug("focus guard observer failed", exc_info=True)

            _observer = center.addObserverForName_object_queue_usingBlock_(
                AppKit.NSWorkspaceDidActivateApplicationNotification, None, queue, activated)
            _queue = queue
        except Exception:
            log.warning("focus guard unavailable", exc_info=True)


@contextmanager
def lease(target_pid=None, allow_front: bool = False, seconds: float = LEASE, guard: Guard | None = None,
          only_target: bool = False):
    """Guard the front app while a background action runs (see the module docstring). Never raises
    because of the guard itself; the action's own exceptions pass through."""
    guard = guard or _guard
    if guard is _guard:
        _install()
    held = guard.open(target_pid, allow_front, seconds, only_target)
    try:
        yield held
    finally:
        if not held.closed:
            if guard.settle:
                time.sleep(guard.settle)
            guard.close(held)
