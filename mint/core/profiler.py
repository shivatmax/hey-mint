"""Where Mint's time goes, from inside: `kill -USR2 <Mint's pid>` samples every thread's Python stack for a minute
and writes PROJECT_ROOT/profile-<time>.txt.

Native tools (`sample`) only show the interpreter and PyObjC for Python code; this names the Mint functions behind
the time - which ticker, which loop. Nothing runs until the signal: no thread, no cost. The main thread's numbers
are exact (idle, it sits in the run loop); a background loop sleeping in C (time.sleep) still shows as "busy" on
its own line, so read those threads' shares as "alive", not as CPU.

The report: per thread, how many samples were busy (not parked in a wait), and the Mint functions on top of the
busy stacks, with their callers.
"""

from __future__ import annotations

import collections
import os
import signal
import sys
import threading
import time
import traceback

RATE = 200                   # samples a second
SECONDS = 60.0
_WAITS = ("wait", "sleep", "select", "poll", "recv", "accept", "read", "get", "acquire", "join", "_worker",
          "run_forever", "_run_once", "runEventLoop", "run")
_running = threading.Event()


def _innermost_mint(frames) -> str:
    for frame in reversed(frames):
        name = frame.filename
        if "/mint/" in name and "profiler.py" not in name:
            return f"{name.split('/mint/', 1)[1]}:{frame.lineno} {frame.name}"
    return ""


def _chain(frames, depth: int = 3) -> str:
    own = [f for f in frames if "/mint/" in f.filename and "profiler.py" not in f.filename]
    return " < ".join(f"{f.name}" for f in reversed(own[-depth:]))


def run(seconds: float = SECONDS, path: str | None = None) -> str:
    """Sample for `seconds` (blocking; call from a thread) and write the report. -> its path."""
    from mint.core import config
    names = {t.ident: t.name for t in threading.enumerate()}
    me = threading.get_ident()
    total = collections.Counter()
    busy = collections.Counter()
    tops = collections.Counter()
    chains = collections.Counter()
    end = time.monotonic() + seconds
    samples = 0
    while time.monotonic() < end:
        for ident, frame in sys._current_frames().items():
            if ident == me:
                continue
            thread = names.get(ident) or str(ident)
            total[thread] += 1
            stack = traceback.extract_stack(frame)
            leaf = stack[-1].name if stack else ""
            if any(w == leaf or leaf.endswith(w) for w in _WAITS):
                continue
            busy[thread] += 1
            top = _innermost_mint(stack) or f"{stack[-1].filename.rsplit('/', 1)[-1]}:{leaf}" if stack else "?"
            tops[(thread, top)] += 1
            chains[(thread, _chain(stack))] += 1
        samples += 1
        time.sleep(1 / RATE)
        names = {t.ident: t.name for t in threading.enumerate()}
    out = [f"Mint profile: {seconds:.0f} s, {samples} samples per thread, pid {os.getpid()}", "",
           "Busy share per thread (samples not parked in a wait):"]
    for thread, n in busy.most_common(15):
        out.append(f"  {100 * n / max(1, total[thread]):5.1f}%  {thread}")
    out += ["", "Hottest Mint functions (share of all samples of that thread):"]
    for (thread, top), n in tops.most_common(40):
        out.append(f"  {100 * n / max(1, total[thread]):5.1f}%  [{thread[:24]}] {top}")
    out += ["", "Hottest call chains:"]
    for (thread, chain), n in chains.most_common(25):
        out.append(f"  {100 * n / max(1, total[thread]):5.1f}%  [{thread[:24]}] {chain}")
    path = path or str(config.PROJECT_ROOT / f"profile-{time.strftime('%Y%m%d-%H%M%S')}.txt")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    return path


def _on_signal(signum, frame) -> None:
    if _running.is_set():
        return
    _running.set()

    def go() -> None:
        try:
            path = run()
            print(f"  [profile written: {path}]", flush=True)
        finally:
            _running.clear()
    threading.Thread(target=go, name="mint-profiler", daemon=True).start()


def install() -> None:
    """`kill -USR2 <pid>` starts a one-minute profile."""
    try:
        signal.signal(signal.SIGUSR2, _on_signal)
    except (ValueError, OSError):
        pass                      # not the main thread / not supported: no profiler
