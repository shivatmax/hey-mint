"""Holds Mint's one connection to the user's own Chrome, so Chrome asks "Allow remote debugging?" once per
Chrome start, not every time Mint restarts. Only Mint gets in: the kernel names the connecting process (its
pid), which must be Mint's own Python running Mint, and it must also show the secret key.

Chrome asks on every new DevTools connection and has no "always allow". This small process keeps the
connection the user allowed and lends it to Mint over a private Unix socket (owner-only, plus a secret key in
a file only the user can read). Mint restarting just reconnects here; Chrome is not asked again. It never
connects to Chrome on its own: only when Mint asks (so Chrome's question always follows Mint saying what it
is), and it exits when Chrome quits.

Protocol, one JSON object per line. The client's first line is a hello:
    {"key": ..., "probe": true}           -> {"chrome": true|false}   (is Chrome's connection open?)
    {"key": ..., "ws": "ws://127.0.0.1:…"} -> {"ok": true, "fresh": bool} or {"ok": false, "step": "denied"|...}
then CDP messages both ways, unchanged. One client at a time; when it leaves, the tabs' debugging sessions it
opened are detached (its tabs stay where they are).

Run by Mint: `python -m mint.chrome_bridge` (detached, survives Mint restarts).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("mint.tools.chrome_bridge")

_OWN_IDS = 1_000_000_000            # ids the bridge uses for its own calls, far above any client's


def paths() -> tuple[Path, Path]:
    from mint.core import config
    return config.PROJECT_ROOT / "chrome-bridge.sock", config.PROJECT_ROOT / "chrome-bridge.key"


def peer_pid(conn: socket.socket) -> int | None:
    """The process on the other end of a Unix socket (macOS LOCAL_PEERPID), from the kernel - not its word."""
    try:
        return int.from_bytes(conn.getsockopt(0, 0x002, 4), sys.byteorder)      # SOL_LOCAL, LOCAL_PEERPID
    except OSError:
        return None


def is_mint(pid: int | None) -> bool:
    """Only Mint itself: the same Python that started this bridge, running Mint (`-m mint ...`)."""
    if not pid:
        return False
    import subprocess
    try:
        args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except Exception:
        return False
    return mint_command(args, sys.base_prefix)


def mint_command(args: str, python_home: str) -> bool:
    """`args` runs Mint (`-m mint...`) on the same Python installation as this bridge (macOS's framework Python
    runs as its Python.app inside that installation, not as the venv's `python`)."""
    parts = args.split()
    if len(parts) < 3 or not os.path.realpath(parts[0]).startswith(os.path.realpath(python_home) + os.sep):
        return False
    at = parts.index("-m") if "-m" in parts else -1
    return at >= 0 and at + 1 < len(parts) and parts[at + 1].split(".")[0] == "mint"


class Bridge:
    def __init__(self, sock_path: Path, key_path: Path, allow=is_mint) -> None:
        self.sock_path, self.key_path, self.allow = sock_path, key_path, allow
        self.key = secrets.token_hex(24)
        self.ws = None
        self.ws_lock = threading.Lock()
        self.client: socket.socket | None = None
        self.client_lock = threading.Lock()
        self.sessions: set[str] = set()
        self.pending: dict[int, str] = {}          # client request id -> method, for the ones worth tracking
        self.quit = threading.Event()

    # Chrome side
    def chrome_open(self) -> bool:
        return self.ws is not None and not getattr(self, "_ws_closed", True)

    def connect_chrome(self, url: str) -> dict:
        with self.ws_lock:
            if self.chrome_open():
                return {"ok": True, "fresh": False}
            from websockets.sync.client import connect
            started = time.monotonic()
            try:
                self.ws = connect(url, open_timeout=120.0, max_size=None, ping_interval=None)
            except Exception as error:
                waited = time.monotonic() - started
                step = "timeout" if waited > 110 or "timed out" in str(error).lower() else "denied"
                log.info("Chrome refused after %.0f s: %s", waited, error)
                return {"ok": False, "step": step}
            self._ws_closed = False
            threading.Thread(target=self._from_chrome, name="bridge-chrome", daemon=True).start()
            return {"ok": True, "fresh": True}

    def _from_chrome(self) -> None:
        try:
            for raw in self.ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if message.get("id", 0) >= _OWN_IDS:
                    continue
                self._track_reply(message)
                self._to_client(raw if isinstance(raw, str) else raw.decode())
        except Exception as error:
            log.info("chrome connection ended with %r", error)
        self._ws_closed = True
        close = getattr(self.ws, "close_rcvd", None) or getattr(self.ws, "close_sent", None)
        log.info("Chrome closed its connection (code %s, reason %r, client %s); the bridge exits",
                 getattr(close, "code", None), getattr(close, "reason", None),
                 "connected" if self.client is not None else "none")
        self.quit.set()
        self._drop_client()

    def _to_chrome(self, text: str) -> None:
        try:
            self.ws.send(text)
        except Exception:
            self._ws_closed = True
            self.quit.set()

    # client side
    def _to_client(self, text: str) -> None:
        with self.client_lock:
            client = self.client
        if client is None:
            return
        try:
            client.sendall(text.encode() + b"\n")
        except OSError:
            self._drop_client(client)

    def _track_request(self, message: dict) -> None:
        if message.get("method") in ("Target.attachToTarget",):
            self.pending[message.get("id")] = message["method"]

    def _track_reply(self, message: dict) -> None:
        method = self.pending.pop(message.get("id"), None)
        if method == "Target.attachToTarget":
            session = (message.get("result") or {}).get("sessionId")
            if session:
                self.sessions.add(session)

    def _drop_client(self, which: socket.socket | None = None) -> None:
        with self.client_lock:
            client = self.client
            if client is None or (which is not None and client is not which):
                return
            self.client = None
            sessions, self.sessions = list(self.sessions), set()
            self.pending.clear()
        try:
            client.close()
        except OSError:
            pass
        if self.chrome_open():                     # its tabs stay; their debugging sessions end
            for n, session in enumerate(sessions):
                self._to_chrome(json.dumps({"id": _OWN_IDS + n, "method": "Target.detachFromTarget",
                                            "params": {"sessionId": session}}))

    def serve_client(self, conn: socket.socket) -> None:
        if not self.allow(peer_pid(conn)):
            log.warning("refused a connection from a process that isn't Mint (pid %s)", peer_pid(conn))
            conn.close()
            return
        reader = conn.makefile("rb")
        try:
            hello = json.loads(reader.readline() or b"{}")
        except ValueError:
            conn.close()
            return
        if not secrets.compare_digest(str(hello.get("key", "")), self.key):
            conn.close()
            return
        if hello.get("probe"):
            conn.sendall(json.dumps({"chrome": self.chrome_open()}).encode() + b"\n")
            conn.close()
            return
        answer = self.connect_chrome(str(hello.get("ws", "")))
        log.info("Mint connected (%s)", "Chrome asked" if answer.get("fresh") else
                 "reusing the allowed connection" if answer.get("ok") else answer.get("step"))
        conn.sendall(json.dumps(answer).encode() + b"\n")
        if not answer.get("ok"):
            conn.close()
            return
        with self.client_lock:
            old, self.client = self.client, conn
        if old is not None:                        # a newer Mint replaces an old one
            try:
                old.close()
            except OSError:
                pass
        try:
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._track_request(json.loads(line))
                except ValueError:
                    continue
                self._to_chrome(line.decode())
        except OSError:
            pass
        self._drop_client(conn)

    def run(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_mask = os.umask(0o177)                 # the socket is the user's only
        try:
            server.bind(str(self.sock_path))
        finally:
            os.umask(old_mask)
        os.chmod(self.sock_path, 0o600)
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.key)
        server.listen(4)
        server.settimeout(1.0)
        log.info("chrome bridge listening")
        try:
            while not self.quit.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self.serve_client, args=(conn,), daemon=True,
                                 name="bridge-client").start()
        finally:
            server.close()
            for path in (self.sock_path, self.key_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", stream=sys.stderr)
    import signal
    sock_path, key_path = paths()
    bridge = Bridge(sock_path, key_path)
    signal.signal(signal.SIGTERM, lambda *_: bridge.quit.set())     # tidy exit: socket and key removed
    bridge.run()


if __name__ == "__main__":
    main()
