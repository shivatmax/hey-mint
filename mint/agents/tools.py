"""What sub-agents can do. Each tool is a JSON schema plus a plain function
returning a string (never raising for an expected failure).

Files are confined to the agent's workspace. `ask_user` and `report_progress`
are handled by the runtime (they talk to Mint, not to the world).
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/17.0 Safari/605.1.15")

SCHEMAS = {
    "web_search": {
        "description": "Search the web. Returns titles, URLs and snippets. Then open the best with fetch_url.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "max_results": {"type": "integer", "description": "1-10, default 6"}},
            "required": ["query"]}},
    "fetch_url": {
        "description": "Read a web page as plain text (first ~8000 characters by default).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["url"]}},
    "watch_video": {
        "description": ("Watch a video (YouTube, X, Vimeo, Loom, TikTok link or a file path) fast: returns what it "
                        "is, key moments with timestamps, its look and feel, the answer to `question`, and the "
                        "timestamped transcript. Cite times as m:ss."),
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string"}, "question": {"type": "string"}}, "required": ["source"]}},
    "write_file": {
        "description": "Write a text file in your workspace (creates folders). Overwrites.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative path, e.g. 'report.md' or 'env/grid.py'"},
            "content": {"type": "string"}}, "required": ["path", "content"]}},
    "read_file": {
        "description": "Read a text file from your workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    "list_files": {
        "description": "List the files in your workspace.",
        "parameters": {"type": "object", "properties": {}}},
    "create_pdf": {
        "description": "Render a PDF from Markdown-ish text (headings with #, bullets). Returns the path.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "content": {"type": "string"}}, "required": ["title", "content"]}},
    "classify": {
        "description": "Pick which of several options best fits a question, with a confidence (fast classifier).",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
            "required": ["question", "options"]}},
    "run_command": {
        "description": "Run a shell command in your workspace (60 s limit). Only for building/testing your files.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    "ask_user": {
        "description": ("Ask the user a question through Mint and wait for the answer. Only when you truly "
                        "cannot proceed well without it; one clear question."),
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    "report_progress": {
        "description": "Tell Mint (and the user) briefly what you are doing now. Use at milestones, not every step.",
        "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},
}


def schemas(names: list[str]) -> list[dict]:
    return [{"name": n, **SCHEMAS[n]} for n in names if n in SCHEMAS]


# --- implementations ----------------------------------------------------------------

def _inside(workspace: Path, relative: str) -> Path:
    target = (workspace / relative.lstrip("/")).resolve()
    if workspace.resolve() not in (target, *target.parents):
        raise ValueError("outside the workspace")
    return target


def run(name: str, args: dict, workspace: Path, team_root: Path | None = None) -> str:
    """`team_root`: the mission folder - read_file and list_files may look anywhere
    in it (teammates' files); writing stays inside the agent's own workspace."""
    readable = team_root if team_root is not None and (workspace.resolve() == team_root.resolve()
                                                         or team_root.resolve() in workspace.resolve().parents) \
        else workspace
    try:
        if name == "web_search":
            return web_search(str(args.get("query", "")), int(args.get("max_results") or 6))
        if name == "fetch_url":
            return fetch_url(str(args.get("url", "")), int(args.get("max_chars") or 8000))
        if name == "watch_video":
            from mint.tools import video
            return video.watch_for_agent(str(args.get("source", "")), str(args.get("question", "")), workspace)
        if name == "write_file":
            path = _inside(workspace, str(args["path"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(args.get("content", "")))
            return f"Wrote {path} ({len(str(args.get('content', '')))} characters)."
        if name == "read_file":
            try:
                path = _inside(workspace, str(args["path"]))
            except ValueError:
                path = _inside(readable, str(args["path"]))
            if not path.exists() and readable is not workspace:
                path = _inside(readable, str(args["path"]))
            if not path.exists():
                return f"No such file: {args['path']}"
            text = path.read_text(errors="replace")
            return text[:20000] + ("\n…(truncated)" if len(text) > 20000 else "")
        if name == "list_files":
            files = [str(p.relative_to(workspace)) for p in sorted(workspace.rglob("*")) if p.is_file()]
            listing = "\n".join(files[:300]) or "(your workspace is empty)"
            if readable is not workspace:
                team_files = [str(p.relative_to(readable)) for p in sorted(readable.rglob("*"))
                              if p.is_file() and workspace.resolve() not in p.resolve().parents]
                if team_files:
                    listing += ("\n\nMission folder (teammates' files, readable; paths from the mission root):\n"
                                + "\n".join(team_files[:200]))
            return listing
        if name == "create_pdf":
            from mint.tools import documents
            return documents.create_pdf(str(args.get("title", "Document")), str(args.get("content", "")),
                                        open_after=False)
        if name == "classify":
            from mint.core import jev
            options = [str(o) for o in args.get("options") or []]
            pick = jev.choose(str(args.get("question", "")), {str(i): o for i, o in enumerate(options)})
            if pick is None:
                return "The classifier is unreachable right now."
            if pick.id is None:
                return f"None of the options fits (confidence {pick.confidence:.2f})."
            return f"{options[int(pick.id)]} (confidence {pick.confidence:.2f})"
        if name == "run_command":
            done = subprocess.run(str(args["command"]), shell=True, cwd=workspace, capture_output=True,
                                  text=True, timeout=60)
            out = (done.stdout + ("\n" + done.stderr if done.stderr else ""))[-6000:]
            return f"exit {done.returncode}\n{out}"
    except subprocess.TimeoutExpired:
        return "The command took longer than 60 s and was stopped."
    except Exception as error:
        return f"{name} failed: {str(error)[:300]}"
    return f"Unknown tool {name}."


def web_search(query: str, max_results: int = 6) -> str:
    import httpx

    max_results = max(1, min(10, max_results))
    if os.environ.get("EXA_API_KEY"):
        response = httpx.post("https://api.exa.ai/search", timeout=30,
                              headers={"x-api-key": os.environ["EXA_API_KEY"]},
                              json={"query": query, "numResults": max_results,
                                    "contents": {"text": {"maxCharacters": 600}}})
        response.raise_for_status()
        rows = [(r.get("title", ""), r.get("url", ""), (r.get("text") or "")[:400])
                for r in response.json().get("results", [])]
        return _format(rows, "Exa")
    if os.environ.get("TAVILY_API_KEY"):
        response = httpx.post("https://api.tavily.com/search", timeout=30,
                              json={"api_key": os.environ["TAVILY_API_KEY"], "query": query,
                                    "max_results": max_results, "include_answer": True})
        response.raise_for_status()
        data = response.json()
        rows = [(r.get("title", ""), r.get("url", ""), (r.get("content") or "")[:400]) for r in data.get("results", [])]
        answer = data.get("answer")
        return (f"Answer: {answer}\n\n" if answer else "") + _format(rows, "Tavily")
    # No key: DuckDuckGo's HTML page.
    response = httpx.get("https://html.duckduckgo.com/html/", params={"q": query}, timeout=20,
                         headers={"User-Agent": UA}, follow_redirects=True)
    response.raise_for_status()
    rows = []
    for match in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)'
                             r'(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)', response.text, re.S):
        href, title, _, snippet = match.groups()
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href:        # ads
            continue
        rows.append((_strip(title), href, _strip(snippet or "")))
        if len(rows) >= max_results:
            break
    return _format(rows, "DuckDuckGo")


def _format(rows, source) -> str:
    if not rows:
        return f"No results ({source})."
    return f"Results ({source}):\n" + "\n".join(f"{i}. {t}\n   {u}\n   {s}" for i, (t, u, s) in enumerate(rows, 1))


def _strip(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def fetch_url(url: str, max_chars: int = 8000) -> str:
    import httpx

    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    response = httpx.get(url, timeout=25, headers={"User-Agent": UA}, follow_redirects=True)
    if response.status_code >= 400:
        return f"HTTP {response.status_code} for {url}"
    kind = response.headers.get("content-type", "")
    if "html" not in kind and "text" not in kind and "json" not in kind:
        return f"{url} is {kind or 'not text'}; cannot read it as text."
    body = response.text
    if "html" in kind:
        body = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", body)
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", response.text)
        body = re.sub(r"(?i)<br\s*/?>|</p>|</h[1-6]>|</li>|</div>", "\n", body)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
        body = re.sub(r"[ \t\r\f\v]+", " ", body)
        body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
        if title:
            body = f"# {_strip(title.group(1))}\n\n{body}"
    max_chars = max(500, min(max_chars, 30000))
    return f"{url}\n\n{body[:max_chars]}" + ("\n…(truncated)" if len(body) > max_chars else "")


def describe_args(name: str, args: dict) -> str:
    """A short phrase for the HUD: 'searching "…"', 'writing report.md'."""
    if name == "web_search":
        return f"searching “{str(args.get('query', ''))[:40]}”"
    if name == "fetch_url":
        host = urllib.parse.urlparse(str(args.get("url", ""))).netloc or str(args.get("url", ""))[:30]
        return f"reading {host.removeprefix('www.')}"
    if name == "write_file":
        return f"writing {str(args.get('path', ''))[:40]}"
    if name == "read_file":
        return f"reading {str(args.get('path', ''))[:40]}"
    if name == "create_pdf":
        return "making a PDF"
    if name == "watch_video":
        return "watching a video"
    if name == "run_command":
        return f"running {str(args.get('command', ''))[:30]}"
    return name.replace("_", " ")


def safe_json(value) -> str:
    try:
        return json.dumps(value)[:200]
    except Exception:
        return str(value)[:200]
