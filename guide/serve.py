"""Preview the guide locally with Cloudflare's clean URLs (/docs -> docs.html) and _redirects.
Run: python3 guide/serve.py [port]"""
import http.server
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REDIRECTS = {}
for line in (HERE / "_redirects").read_text().splitlines() if (HERE / "_redirects").exists() else []:
    parts = line.split()
    if len(parts) >= 2:
        REDIRECTS[parts[0]] = parts[1]


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in REDIRECTS:
            self.send_response(301)
            self.send_header("Location", REDIRECTS[path])
            self.end_headers()
            return
        if path != "/" and "." not in path.rsplit("/", 1)[-1] and (HERE / (path.lstrip("/") + ".html")).exists():
            self.path = path + ".html"
        super().do_GET()


port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
