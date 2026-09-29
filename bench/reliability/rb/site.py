"""The local test site (bench/reliability/site) on 127.0.0.1, with a few special routes.

- /slow/...    served after SLOW_SECONDS (a slow network page)
- /flaky/...   503 for the first FLAKY_FAILS requests after reset(), then the page
- /record?...  the sign-up form reports its field values here every second (form.html)
- /state       what /record last received, as JSON (for debugging)
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "site"
SLOW_SECONDS = 12.0
FLAKY_FAILS = 2


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.flaky_left = FLAKY_FAILS
        self.record: dict = {}
        self.record_at = 0.0
        self.hits: list[tuple[float, str]] = []

    def reset(self) -> None:
        with self.lock:
            self.flaky_left = FLAKY_FAILS
            self.record = {}
            self.record_at = 0.0
            self.hits = []


class _Handler(SimpleHTTPRequestHandler):
    state: _State

    def log_message(self, format, *args):  # noqa: A002 - the base class's name
        pass

    def do_GET(self):  # noqa: N802 - http.server's name
        path = urllib.parse.urlsplit(self.path).path
        with self.state.lock:
            self.state.hits.append((time.time(), path))
            del self.state.hits[:-500]
        if path == "/record":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            with self.state.lock:
                self.state.record = {k: v[0] for k, v in query.items()}
                self.state.record_at = time.time()
            return self._send(204, b"", "text/plain")
        if path == "/state":
            with self.state.lock:
                body = json.dumps({"record": self.state.record, "at": self.state.record_at}).encode()
            return self._send(200, body, "application/json")
        if path.startswith("/slow/"):
            time.sleep(SLOW_SECONDS)
        if path.startswith("/flaky/"):
            with self.state.lock:
                fail = self.state.flaky_left > 0
                if fail:
                    self.state.flaky_left -= 1
            if fail:
                return self._send(503, b"<h1>503 Service Unavailable</h1><p>Try again in a moment.</p>",
                                  "text/html")
        return super().do_GET()

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class SiteServer:
    def __init__(self, port: int = 8765) -> None:
        self.state = _State()
        handler = type("Handler", (_Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(handler, directory=str(SITE)))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="mintbench-site", daemon=True)

    def start(self) -> "SiteServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def fetched(self, prefix: str, since: float) -> int:
        """How many times a path starting with `prefix` was requested since `since`."""
        with self.state.lock:
            return sum(1 for at, path in self.state.hits if at >= since and path.startswith(prefix))
