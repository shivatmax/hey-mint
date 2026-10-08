"""Connectors from plain words: "integrate Things", "connect Notion", "make a connector for TextEdit".

    make    finds the app (or web service) and plans a small connector: 4-10 actions, each an AppleScript
            template (from the app's scripting dictionary), a URL (from the app's own URL schemes) or a web
            page (for a service in the browser), marked read-only or changing, with {placeholders}.
            Every template is checked here: it compiles (osacompile), it only talks to that app, nothing
            dangerous (shell commands that delete or escalate, System Events keystrokes, AppleScriptObjC,
            JavaScript), placeholders are never spliced into code - Mint puts each value in as an AppleScript
            string (or number) literal, or URL-encodes it. Then Mint reads the plan back.
    create  after the user says yes (a new message, the way shortcut_maker waits): one read-only action is
            run live (macOS may ask once whether Mint may control that app) and the connector is saved to
            ~/Library/Application Support/Mint/connectors/<id>.json.
    run     runs one action with arguments. Changing actions need the user's request to ask for them;
            sending, deleting and paying need those very words in that request (or a "yes" after Mint asked).

The library of what exists (and the status of each) is connectors.py; this module is the `connector` tool.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from mint.tools import connectors

log = logging.getLogger("mint.tools.connector_maker")

MODELS = ["gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
MAX_SDEF = 14_000
_state: dict = {"plans": {}, "last": None, "pending": None}
_lock = threading.Lock()

# Known web services: name -> (domain, home page). Anything else: the planner names the domain.
WEB = {"notion": ("notion.so", "https://www.notion.so/"), "todoist": ("todoist.com", "https://app.todoist.com/"),
       "linear": ("linear.app", "https://linear.app/"), "github": ("github.com", "https://github.com/"),
       "trello": ("trello.com", "https://trello.com/"), "figma": ("figma.com", "https://www.figma.com/"),
       "asana": ("asana.com", "https://app.asana.com/"), "jira": ("atlassian.net", "https://id.atlassian.com/"),
       "gmail": ("mail.google.com", "https://mail.google.com/"),
       "google docs": ("docs.google.com", "https://docs.google.com/"),
       "google drive": ("drive.google.com", "https://drive.google.com/"),
       "youtube": ("youtube.com", "https://www.youtube.com/"), "airtable": ("airtable.com", "https://airtable.com/"),
       "clickup": ("clickup.com", "https://app.clickup.com/"), "dropbox": ("dropbox.com", "https://www.dropbox.com/"),
       "slack": ("slack.com", "https://app.slack.com/"), "whatsapp": ("whatsapp.com", "https://web.whatsapp.com/"),
       "spotify": ("spotify.com", "https://open.spotify.com/"), "reddit": ("reddit.com", "https://www.reddit.com/"),
       "x": ("x.com", "https://x.com/"), "twitter": ("x.com", "https://x.com/"),
       "obsidian": ("obsidian.md", "https://obsidian.md/"),
       "canva": ("canva.com", "https://www.canva.com/"), "discord": ("discord.com", "https://discord.com/app"),
       "evernote": ("evernote.com", "https://www.evernote.com/client/web"),
       "onenote": ("onenote.com", "https://www.onenote.com/notebooks"),
       "microsoft onenote": ("onenote.com", "https://www.onenote.com/notebooks"),
       "word": ("office.com", "https://www.office.com/launch/word"),
       "microsoft word": ("office.com", "https://www.office.com/launch/word"),
       "excel": ("office.com", "https://www.office.com/launch/excel"),
       "microsoft excel": ("office.com", "https://www.office.com/launch/excel"),
       "powerpoint": ("office.com", "https://www.office.com/launch/powerpoint"),
       "microsoft powerpoint": ("office.com", "https://www.office.com/launch/powerpoint"),
       "telegram": ("web.telegram.org", "https://web.telegram.org/"),
       "google sheets": ("docs.google.com", "https://docs.google.com/spreadsheets/"),
       "google calendar": ("calendar.google.com", "https://calendar.google.com/")}

# Never in a connector, whatever the plan says.
_NEVER = re.compile(r"\b(run\s+script|load\s+script|store\s+script|administrator\s+privileges|system\s+events|"
                    r"keystroke|key\s+code|osascript|do\s+javascript|execute\s+javascript|call\s+method|"
                    r"current\s+application's|use\s+framework|use\s+scripting\s+additions|with\s+password|"
                    r"security\s+find|keychain)\b", re.I)
_PERMANENT = re.compile(r"\bempty\s+(the\s+)?trash\b|\berase\b|\bsecure\s+empty\b", re.I)
_SHELL = re.compile(r"\bdo\s+shell\s+script\b(.*)", re.I)
# Risky commands in a template (or a link), and risky first words of a title.
_SENDS = re.compile(r"\b(send|reply|forward|post|publish|tweet|share|invite|submit)\b", re.I)
_DELETES = re.compile(r"\b(delete|erase|empty|purge|wipe|remove)\b|\bmove\b[^\n]*\bto\s+(the\s+)?trash\b", re.I)
_PAYS = re.compile(r"\b(pay|payment|purchase|buy|checkout|subscribe|transfer)\b", re.I)
_TITLE_RISK = {"sends": {"send", "reply", "forward", "post", "publish", "tweet", "share", "invite", "submit", "message",
                         "email", "text"},
               "deletes": {"delete", "remove", "erase", "empty", "trash", "clear", "purge", "wipe"},
               "pays": {"pay", "buy", "purchase", "order", "checkout", "subscribe", "transfer"}}
_SYNONYMS = {"add": ("add", "put", "new", "create", "make", "note", "jot"), "create": ("create", "make", "new", "add",
             "start"), "make": ("make", "create", "new", "add"), "new": ("new", "create", "make", "add", "start"),
             "open": ("open", "show", "go to", "bring up", "pull up", "switch to", "take me"),
             "show": ("show", "open", "reveal", "bring up"), "play": ("play", "put on", "start", "listen"),
             "pause": ("pause", "stop"), "set": ("set", "change", "make", "turn"), "move": ("move", "put", "file"),
             "complete": ("complete", "done", "finish", "tick", "check off", "mark"),
             "mark": ("mark", "complete", "done", "tick", "flag"), "search": ("search", "find", "look"),
             "compress": ("compress", "zip", "archive", "pack"), "zip": ("zip", "compress", "archive", "pack"),
             "extract": ("extract", "unzip", "unpack", "decompress", "expand", "open"),
             "unzip": ("unzip", "extract", "unpack", "decompress")}
_CHANGES = re.compile(r"\b(make\s+new|delete|move|duplicate|save|close|quit|send|import|export|empty|add|play|"
                      r"pause|next\s+track|previous\s+track|open|activate|launch|print|mark|complete|reveal|"
                      r"set\s+(?:the\s+)?[\w ]+?\s+of\s+.+?\s+to)\b", re.I)
_LINK_CHANGES = re.compile(r"(add|new|create|compose|edit|update|delete|remove|archive|send|post|share|invite|"
                          r"buy|pay|order|checkout|move|complete|done|play)", re.I)
RISK_WORDS = {"sends": ("send", "message", "text", "reply", "forward", "email", "mail", "post", "publish", "share",
                        "invite", "tweet", "submit"),
              "deletes": ("delete", "remove", "trash", "erase", "clear", "empty", "wipe", "get rid of", "purge"),
              "pays": ("pay", "buy", "purchase", "order", "checkout", "subscribe", "transfer")}
_VERBS = {"add", "put", "new", "create", "make", "open", "show", "reveal", "play", "pause", "stop", "skip", "next",
          "previous", "set", "change", "move", "rename", "file", "mark", "complete", "tick", "start", "launch", "run",
          "append", "write", "jot", "save", "export", "import", "print", "close", "quit", "archive", "flag", "pin",
          "star", "favorite", "favourite", "update", "edit", "replace", "insert", "bring", "go", "switch", "turn",
          "shuffle", "repeat", "like", "love", "rate", "copy", "duplicate", "schedule", "remind", "log", "record",
          "send", "share", "reply", "forward", "post", "delete", "remove", "trash", "clear", "pay", "buy", "order",
          "compress", "zip", "extract", "unzip", "pack", "unpack", "convert", "resize", "rotate", "crop", "encode",
          "upload", "download", "sync", "encrypt", "decrypt", "mount", "eject", "render", "translate", "scan"}
_YES = re.compile(r"^(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|please do|confirm|send it|go for it|right|"
                  r"correct|haan|han ji|ji)\b", re.I)
_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')


class PlanError(ValueError):
    pass


# --- Reading an app ----------------------------------------------------------------------------------

def _attrs(el, *names) -> list[str]:
    return [el.get(n) for n in names if el.get(n)]


def _type_of(el) -> str:
    if el.get("type"):
        return el.get("type")
    inner = [t.get("type") for t in el.findall("type") if t.get("type")]
    return " | ".join(inner) or "any"


def summarize_sdef(xml: str, limit: int = MAX_SDEF) -> str:
    """The app's commands, classes and properties in short lines, for the planner (app-specific suites first;
    the Standard Suite's generic commands last)."""
    try:
        root = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", xml, count=1).encode())
    except ET.ParseError as error:
        return f"(the dictionary could not be read: {error})"
    parts: list[tuple[int, str]] = []
    for suite in root.iter("suite"):
        name = suite.get("name") or "Suite"
        generic = name.lower() in ("standard suite", "text suite", "type definitions", "type names suite")
        lines = [f"Suite: {name}" + (f" - {suite.get('description')}" if suite.get("description") else "")]
        for el in suite:
            tag = el.tag
            if tag in ("command",) and el.get("hidden") != "yes":
                direct = el.find("direct-parameter")
                params = [f"{p.get('name')} {_type_of(p)}{'?' if p.get('optional') == 'yes' else ''}"
                          for p in el.findall("parameter") if p.get("hidden") != "yes"]
                result = el.find("result")
                lines.append(f"  command {el.get('name')}"
                             + (f" [direct: {_type_of(direct)}]" if direct is not None else "")
                             + (f" ({', '.join(params)})" if params else "")
                             + (f" -> {_type_of(result)}" if result is not None else "")
                             + (f" : {el.get('description')[:90]}" if el.get("description") else ""))
            elif tag in ("class", "class-extension") and el.get("hidden") != "yes":
                title = el.get("name") or f"(extends {el.get('extends')})"
                props = [f"{p.get('name')}: {_type_of(p)}{' r/o' if p.get('access') == 'r' else ''}"
                         for p in el.findall("property") if p.get("hidden") != "yes"]
                elems = [e.get("type") for e in el.findall("element") if e.get("type") and e.get("hidden") != "yes"]
                lines.append(f"  class {title}" + (f" (plural {el.get('plural')})" if el.get("plural") else "")
                             + (f" inherits {el.get('inherits')}" if el.get("inherits") else "")
                             + (f" : {el.get('description')[:80]}" if el.get("description") else ""))
                if props:
                    lines.append("    properties: " + "; ".join(props[:40]))
                if elems:
                    lines.append("    elements: " + ", ".join(elems[:30]))
            elif tag == "enumeration":
                values = [e.get("name") for e in el.findall("enumerator") if e.get("name")]
                lines.append(f"  enumeration {el.get('name')}: {', '.join(values[:20])}")
        parts.append((1 if generic else 0, "\n".join(lines)))
    text = "\n".join(p for _, p in sorted(parts, key=lambda x: x[0]))
    if "xi:include" in xml or "{http://www.w3.org/2001/XInclude}include" in xml:
        text += "\n(Also the standard AppleScript suite: open, close, save, count, exists, make, delete, get, set.)"
    return text[:limit] + ("\n…(cut)" if len(text) > limit else "")


# --- Checking templates ---------------------------------------------------------------------------------

def as_literal(value, kind: str = "text") -> str:
    """A value as an AppleScript literal: a quoted string (backslashes and quotes escaped) or a plain number."""
    if kind == "number":
        try:
            number = float(str(value).strip())
        except ValueError:
            raise PlanError(f"'{value}' is not a number") from None
        if not math.isfinite(number):
            raise PlanError("that number is not finite")
        return str(int(number)) if number == int(number) else repr(number)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _unsplice(template: str, params: dict[str, dict]) -> str:
    """Placeholders the planner put inside a string literal ("Hello {name}") become concatenation
    ("Hello " & {name}), so a value is never parsed as code."""
    def fix(match: re.Match) -> str:
        literal = match.group(0)
        inner = literal[1:-1]
        if not any(f"{{{p}}}" in inner for p in params):
            return literal
        pieces, last = [], 0
        for m in _PARAM.finditer(inner):
            if m.group(1) not in params:
                continue
            if m.start() > last:
                pieces.append(f'"{inner[last:m.start()]}"')
            ref = f"{{{m.group(1)}}}"
            pieces.append(f"({ref} as text)" if params[m.group(1)].get("type") == "number" else ref)
            last = m.end()
        if last < len(inner):
            pieces.append(f'"{inner[last:]}"')
        return "(" + " & ".join(pieces) + ")" if len(pieces) > 1 else pieces[0]
    return _LITERAL.sub(fix, template)


def fill(template: str, params: dict[str, dict], values: dict, kind: str = "applescript") -> str:
    """Put the values in: AppleScript literals, or URL-encoded for links. Missing required values raise."""
    def value_for(name: str):
        spec = params[name]
        if name in values and str(values[name]).strip() != "":
            return values[name]
        if spec.get("required", True):
            raise PlanError(f"it needs {name} ({spec.get('description') or spec.get('type', 'text')})")
        return "" if spec.get("type") != "number" else 0

    def sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in params:
            return match.group(0)
        value = value_for(name)
        if kind == "applescript":
            return as_literal(value, params[name].get("type", "text"))
        if params[name].get("type") == "number":
            as_literal(value, "number")
        return urllib.parse.quote(str(value), safe="")
    return _PARAM.sub(sub, template)


def _sample(params: dict[str, dict]) -> dict:
    return {n: (1 if p.get("type") == "number" else "sample") for n, p in params.items()}


def _compile(script: str) -> str:
    """'' when it compiles, else osacompile's complaint."""
    with tempfile.TemporaryDirectory(prefix="mint-connector-") as tmp:
        source, out = Path(tmp) / "a.applescript", Path(tmp) / "a.scpt"
        source.write_text(script)
        try:
            done = subprocess.run(["osacompile", "-o", str(out), str(source)], capture_output=True, text=True,
                                  timeout=40)
        except subprocess.TimeoutExpired:
            return "compiling took too long"
        if done.returncode == 0:
            return ""
        line = (done.stderr.strip().split("\n") or [""])[-1]
        return re.sub(r"^.*?(error|execution error):\s*", "", line)[:200] or "it doesn't compile"


def _risks(title: str, template: str) -> list[str]:
    """What an action risks, from its code (commands, link words) and the first word of its title."""
    first = (re.findall(r"[a-z]+", title.lower()) or [""])[0]
    return [name for name, rx in (("sends", _SENDS), ("deletes", _DELETES), ("pays", _PAYS))
            if rx.search(template) or first in _TITLE_RISK[name]]


def check_action(raw: dict, target: dict, compile_it: bool = True) -> dict:
    """One planned action -> a clean action, or raises PlanError with the reason it was left out."""
    if not isinstance(raw, dict):
        raise PlanError("not an action")
    kind = str(raw.get("kind") or "applescript").lower().replace(" ", "")
    kind = {"url": "url", "link": "url", "web": "web", "browser": "web", "files": "files", "file": "files",
            "openwith": "files"}.get(kind, "applescript")
    title = " ".join(str(raw.get("title") or "").split())[:60]
    aid = re.sub(r"[^a-z0-9_]", "_", str(raw.get("id") or title).lower()).strip("_")[:40]
    template = str(raw.get("template") or "").strip()
    if not aid or not title or not template:
        raise PlanError(f"'{title or aid or '?'}' is incomplete")
    params: dict[str, dict] = {}
    for p in raw.get("params") or []:
        if not isinstance(p, dict):
            continue
        name = re.sub(r"[^a-z0-9_]", "_", str(p.get("name") or "").lower()).strip("_")
        if name and re.match(r"[a-z_]", name):
            params[name] = {"type": "number" if str(p.get("type")).lower() in ("number", "integer", "int", "float")
                            else "text", "description": str(p.get("description") or "")[:100],
                            "required": p.get("required", True) is not False}
    used = set(_PARAM.findall(template)) & set(params)
    params = {n: p for n, p in params.items() if n in used}
    declared = raw.get("risk") or []
    declared = [declared] if isinstance(declared, str) else declared
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', template) if kind == "applescript" else template   # not the words in quotes
    risk = sorted((set(_risks(title, code)) | {str(r) for r in declared}) & set(RISK_WORDS))
    if kind == "files":                   # files handed to the app (Open With): it works on them
        changes = True
    elif kind == "applescript":
        changes = bool(raw.get("changes")) or bool(risk) or bool(_CHANGES.search(code))
    else:                                 # a link: opening it is harmless; one that makes or changes something isn't
        changes = bool(risk) or bool(_LINK_CHANGES.search(re.sub(r"^[a-z0-9+.-]+:/*[^/?#]*", "", template)))

    if kind == "applescript":
        if _NEVER.search(template):
            raise PlanError(f"'{title}' uses {_NEVER.search(template).group(0)!r}, which Mint never puts in a "
                            "connector")
        if _PERMANENT.search(code):
            raise PlanError(f"'{title}' deletes for good ({_PERMANENT.search(code).group(0)}); Mint never does that "
                            "through a connector")
        shell = _SHELL.search(template)
        if shell:
            from mint.tools.shortcut_maker import _SHELL_NEVER
            if _SHELL_NEVER.search(shell.group(1)) or _PARAM.search(shell.group(1)):
                raise PlanError(f"'{title}' runs a shell command that could change or delete things")
            changes = True
        allowed = {target.get("name", "").lower(), target.get("file_name", "").lower(),
                   target.get("bundle_id", "").lower()} - {""}
        for m in re.finditer(r"\b(?:application|app)\s+(?:id\s+)?\"([^\"]+)\"", template, re.I):
            if m.group(1).lower() not in allowed:
                raise PlanError(f"'{title}' talks to {m.group(1)}, not {target.get('name')}")
        if not re.search(r"\btell\s+(?:application|app)\b", template, re.I):
            template = f'tell application "{target["file_name"]}"\n{template}\nend tell'
        template = _unsplice(template, params)
        if compile_it:
            problem = _compile(fill(template, params, _sample(params)))
            if problem:
                raise PlanError(f"'{title}' doesn't compile ({problem})")
    elif kind == "files":
        if len(params) != 1 or template.strip() != "{" + next(iter(params)) + "}":
            raise PlanError(f"'{title}' should hand over one value, the files: template {{paths}}")
        if not target.get("name"):
            raise PlanError(f"'{title}' needs an app to hand the files to")
    elif kind == "url":
        scheme = template.split(":", 1)[0].lower()
        if "{" in scheme or scheme not in {s.lower() for s in target.get("schemes") or []}:
            raise PlanError(f"'{title}' uses a link ({scheme}:) that {target.get('name')} doesn't handle")
        fill(template, params, _sample(params), "url")
    else:
        parsed = urllib.parse.urlparse(template)
        domain = str(target.get("domain") or "").lower()
        host = parsed.netloc.lower()
        if parsed.scheme != "https" or "{" in parsed.netloc or not domain or not (host == domain
                                                                                   or host.endswith("." + domain)):
            raise PlanError(f"'{title}' isn't an https page on {domain or 'the service'}")
        fill(template, params, _sample(params), "url")
    asks = [str(w).lower().strip() for w in (raw.get("asks") or []) if str(w).strip()][:8]
    return {"id": aid, "title": title, "description": str(raw.get("description") or "")[:200], "kind": kind,
            "template": template, "params": [{"name": n, **p} for n, p in params.items()],
            "changes": changes, "risk": risk, "asks": asks}


# --- Planning ----------------------------------------------------------------------------------------------

_PLAN = """You design a small "connector" so a voice assistant on macOS (Mint) can use {target}. \
Answer ONLY JSON:
{{"name": "<short connector name, e.g. Things>",
  "summary": "<one plain sentence: what Mint can do with it>",
  "examples": ["<2-3 short things a user might say>"],
  "actions": [
    {{"id": "snake_case_id", "title": "<plain words, e.g. List today's to-dos>",
      "description": "<one sentence, what it does and what it returns>",
      "kind": "{kinds}",
      "template": "<the AppleScript, or the URL>",
      "params": [{{"name": "snake_case", "type": "text|number", "description": "...", "required": true}}],
      "changes": false,
      "risk": [],
      "asks": ["<verbs a request for this uses, e.g. add, put, new>"]}}
  ],
  "test": "<id of ONE read-only action with no params, harmless, that proves it works (a count, a name)>",
  "refuse": "<only if nothing safe and useful can be made: why>"}}

Rules:
- 4 to 10 actions, the most useful everyday ones: reading/listing/searching first, then safe creating or opening.
- AppleScript: talk ONLY to this app: tell application "{app_name}" ... end tell. Use only terms from its \
dictionary below (plus standard get/count/make/exists). Return plain text: build a string (e.g. join names with \
linefeed) - never return a raw object reference. Keep lists short (first 20 items). Each script under 25 lines.
- Placeholders: {{param}} stands for a value; use it bare, NEVER inside quotes: name:{{title}}, not "{{title}}". \
Mint inserts each value as a quoted AppleScript string (or a number).
- URLs: only this app's own schemes ({schemes}) or https pages on {domain}; {{param}} values are URL-encoded by Mint.
- Jobs done ON files the user gives the app (compress or extract with an archiver, open in a viewer or editor, add to a library, convert, upload): kind "files", template "{{file_path}}" and one text param file_path (full paths, one per line). Mint hands them to the app like Finder's Open With and answers its save question - prefer this over AppleScript for such jobs (a sandboxed app's scripted commands on paths often do nothing).
- "changes": true for anything that creates, edits, moves, plays, opens windows or otherwise changes state.
- "risk": list "sends" (sends/posts/shares anything to other people), "deletes", "pays" when it does that. \
Avoid such actions unless they are the app's whole point; never do more than the title says.
- Never: emptying the Trash or erasing anything for good, do shell script, System Events, keystrokes, run script, JavaScript, passwords, anything as administrator.
{extra}
The user said: "{request}"

{about}"""


def _strip_words(text: str) -> str:
    text = " ".join(str(text or "").split()).strip(" .!?")
    text = re.sub(r"^(please\s+|can you\s+|could you\s+|hey mint,?\s+)*", "", text, flags=re.I)
    text = re.sub(r"^(integrate|connect|hook up|link|add|set up|setup|make (a |me a )?connector (for|to)|"
                  r"build (a )?connector (for|to))\s+(to\s+|with\s+)?", "", text, flags=re.I)
    text = re.sub(r"\s+(to|with|into|for)\s+(mint|you)$", "", text, flags=re.I)
    text = re.sub(r"^(the|my)\s+", "", text, flags=re.I)
    text = re.sub(r"\s+(app|application|desktop app)$", "", text, flags=re.I)
    return text.strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:30] or "connector"


def _ask_planner(prompt: str) -> dict:
    from mint.core import llm
    text, _model = llm.generate(prompt, MODELS, json_mode=True)
    answer = llm.parse_json(text)
    if not isinstance(answer, dict):
        raise PlanError("the planner gave no plan")
    return answer


def plan(words: str, bundle_id: str = "", web: bool = False) -> dict:
    """-> {id, name, kind (app|web|builtin|none), actions, dropped, test, summary, ...} or {error}/{refuse}.

    bundle_id: exactly this installed app (the app library's Connect) - the Telegram app, not the Telegram bot.
    web: the service in the browser, even when its Mac app is installed (the app library's Use in browser)."""
    request = " ".join(str(words or "").split())
    what = _strip_words(request)
    if not what:
        return {"error": "Say which app or service to connect, e.g. “integrate Things”."}
    lib = connectors.get(what)
    app = connectors.find_app(what)
    if bundle_id:
        app = connectors.app_for(bundle_id) or app
        if lib is not None and bundle_id not in lib.bundles:
            lib = None
    if web:
        app, lib = None, None
    if app is None and lib is not None and lib.bundles:
        app = lib.app()
    if lib is not None and not lib.bundles:                 # an account, a bridge, keys: nothing to make
        return {"kind": "builtin", "library": lib.id, "name": lib.name, "note": connectors.status_text(lib.id)}
    if app is None and lib is not None and lib.bundles and what.lower() not in WEB:
        return {"kind": "none", "name": lib.name,
                "error": f"{lib.name} isn't installed on this Mac" + (f" - get it from {lib.site}" if lib.site else "")
                         + ", then ask again."}
    result: dict = {"request": request, "what": what, "dropped": [], "actions": [], "error": "", "refuse": "",
                    "note": ""}
    if lib is not None and not lib.makeable:
        result["note"] = (f"Mint already works with {lib.name} through its own tools ({', '.join(lib.tools())}); "
                          "this connector adds actions of its own.")
    if app is not None:
        target = dict(app)
        sdef = connectors.scripting_definition(app["path"])
        schemes = [s for s in target.get("schemes") or [] if s.lower() not in ("http", "https", "file")]
        target["schemes"] = schemes
        domain = next((d for k, (d, _) in WEB.items() if k == what.lower() or k == app["name"].lower()), "")
        target["domain"] = domain
        kinds = "|".join(k for k, on in (("applescript", sdef), ("url", schemes), ("web", domain), ("files", True))
                         if on)
        about = ((f"Its scripting dictionary:\n{summarize_sdef(sdef)}\n" if sdef else "It has no AppleScript "
                  "dictionary: use " + ("URL and " if schemes else "") + "files actions only (files it is given); if "
                  "it doesn't work on files either, answer only {\"refuse\": \"<why>\"}.\n")
                 + (f"Its URL schemes: {', '.join(schemes)}. Only use URL formats you are sure this app "
                    "supports.\n" if schemes else ""))
        prompt = _PLAN.format(target=f"the Mac app {app['name']} (bundle id {app['bundle_id']})", kinds=kinds,
                              app_name=app["file_name"], schemes=", ".join(schemes) or "none",
                              domain=domain or "its own website", extra="", request=request[:300], about=about)
        result.update(kind="app", app=app["name"], app_file=app["file_name"], bundle_id=app["bundle_id"],
                      path=app["path"],
                      how="scripting" if sdef else "url")
    else:
        key = what.lower()
        domain, home = WEB.get(key, ("", ""))
        prompt = _PLAN.format(target=f"the web service {what} in the user's browser", kinds="web", app_name="-",
                              schemes="none", domain=domain or "the service's own domain",
                              extra=("- This is a web service, not a Mac app: every action is kind web - an https "
                                     "page to open (search pages, new-item pages, dashboards). Also give "
                                     "\"domain\" (e.g. notion.so) and \"home\" (its https home page). If it is "
                                     "really a desktop app (not usable in a browser), answer only {\"refuse\": "
                                     "\"<name> isn't installed on this Mac\"}.\n"),
                              request=request[:300], about="")
        target = {"name": what, "domain": domain}
        result.update(kind="web", app="", bundle_id="", path="", how="browser", domain=domain, site=home)
    try:
        answer = _ask_planner(prompt)
    except Exception as error:
        log.info("plan: %s", error)
        return {**result, "error": f"Could not plan it right now ({str(error)[:120]})."}
    if str(answer.get("refuse") or "").strip() and not answer.get("actions"):
        return {**result, "refuse": str(answer["refuse"]).strip()[:200]}
    if result["kind"] == "web":
        guess = str(answer.get("domain") or "").lower().removeprefix("www.").strip("/ ")
        if not target["domain"] and re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", guess):
            target["domain"] = result["domain"] = guess
        home = str(answer.get("home") or "")
        if not result.get("site") and home.startswith("https://"):
            result["site"] = home
        if not target["domain"]:
            return {**result, "error": f"{what} isn't an app on this Mac, and Mint couldn't tell which website it is."}
    seen = set()
    for raw in (answer.get("actions") or [])[:12]:
        try:
            action = check_action(raw, target)
        except PlanError as error:
            result["dropped"].append(str(error))
            continue
        if action["id"] in seen:
            continue
        seen.add(action["id"])
        result["actions"].append(action)
    if result["kind"] == "web" and result["actions"]:
        result["actions"] = _reachable(result["actions"], result["dropped"])
        result["actions"] = _only_what_can_be_checked(result, target)
    if not result["actions"]:
        result["error"] = "None of the planned actions passed the checks" + (
            f": {'; '.join(result['dropped'][:3])}" if result["dropped"] else ".")
        return result
    result["actions"] = result["actions"][:10]
    test = str(answer.get("test") or "")
    readable = [a for a in result["actions"] if a["kind"] == "applescript" and not a["changes"]
                and not any(p["required"] for p in a["params"])]
    result["test"] = test if any(a["id"] == test for a in readable) else (readable[0]["id"] if readable else "")
    name = " ".join(str(answer.get("name") or result.get("app") or what).split())[:40] or what
    result.update(id=uuid.uuid4().hex[:8], name=name, cid=_slug(name),
                  summary=str(answer.get("summary") or "")[:200],
                  examples=[str(e)[:80] for e in (answer.get("examples") or [])][:3], made=time.time())
    with _lock:
        result["asked"] = _request()
        _state["plans"][result["id"]] = result
        _state["last"] = result["id"]
    return result


def plan_text(result: dict) -> str:
    if result.get("kind") == "builtin":
        return f"BUILT IN: {result['note']} Use connector action=connect name={result['library']} to set it up."
    if result.get("refuse"):
        return f"REFUSED: no connector for that ({result['refuse']})."
    if result.get("error"):
        return f"FAILED: {result['error']}"
    how = {"scripting": "App scripting", "url": "its links", "browser": "the browser"}[result["how"]]
    lines = [f"Plan '{result['id']}' for a {result['name']} connector (through {how}): {result['summary']}"]
    for n, a in enumerate(result["actions"], 1):
        needs = ", ".join(p["name"] for p in a["params"])
        flag = ("sends" if "sends" in a["risk"] else "deletes" if "deletes" in a["risk"] else
                "pays" if "pays" in a["risk"] else "changes things" if a["changes"] else
                "gives it files" if a["kind"] == "files" else
                "opens a link" if a["kind"] != "applescript" else "read-only")
        lines.append(f"{n}. {a['title']} ({flag}{'; needs ' + needs if needs else ''})")
    if result.get("test"):
        title = next(a["title"] for a in result["actions"] if a["id"] == result["test"])
        lines.append(f"After saving, Mint tests: {title}.")
    else:
        lines.append("After saving, Mint checks that its links open " + (result.get("app") or "the app") + "."
                     if result.get("kind") == "app" else "After saving, Mint checks that the site answers.")
    if result.get("dropped"):
        lines.append("Left out: " + "; ".join(result["dropped"][:4]))
    if result.get("kind") == "web":
        lines.append(f"{result['name']} is a web service: these open pages in the browser. Reading your data "
                     "directly would need its API key, which this connector doesn't use.")
    if result.get("note"):
        lines.append(result["note"])
    return "\n".join(lines)


# --- Saving, testing, running ----------------------------------------------------------------------------

def _request() -> str:
    try:
        from mint.app import live
        return " ".join((live.request() or "").lower().replace("’", "'").split())
    except Exception:
        return ""


def _script_of(action: dict, values: dict) -> str:
    params = {p["name"]: p for p in action["params"]}
    return fill(action["template"], params, values, "applescript" if action["kind"] == "applescript" else "url")


def _execute(item: dict, action: dict, values: dict) -> tuple[bool, str]:
    try:
        filled = _script_of(action, values)
    except PlanError as error:
        return False, str(error)
    if action["kind"] == "applescript":
        return connectors.osascript(filled, timeout=30)
    if action["kind"] == "files":
        from mint.tools import handoff
        name = action["params"][0]["name"]
        said = handoff.hand_to_app({"paths": str(values.get(name) or ""), "app": item.get("app") or item["name"],
                                    "how": "open", "job": action["title"]})
        return not said.startswith(("FAILED", "NOT DONE")), said
    connectors.open_url(filled)
    return True, f"opened {filled[:120]}"


def _scheme_owner(scheme: str) -> str:
    """The app that opens `scheme:` links on this Mac ('' when none does)."""
    try:
        import AppKit
        url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(
            AppKit.NSURL.URLWithString_(f"{scheme}://"))
        return str(url.path()) if url is not None else ""
    except Exception:
        return ""


def _site_answers(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0 (Macintosh) Mint"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, str(response.status)
    except urllib.error.HTTPError as error:          # 401/403: there, it wants a sign-in - which the browser has
        return error.code < 500 and error.code not in (404, 410), str(error.code)
    except Exception as error:
        return False, str(getattr(error, "reason", error))[:80]


def _page_of(action: dict) -> str:
    params = {p["name"]: p for p in action["params"]}
    return fill(action["template"], params, _sample(params), "url")


def _reachable(actions: list[dict], dropped: list[str], answers=None) -> list[dict]:
    """Web actions whose page is really there: each is fetched once with sample values (in parallel); a page the
    site says doesn't exist (404/410) or a site that doesn't answer is left out. A sign-in page is fine."""
    import concurrent.futures
    answers = answers or _site_answers
    web = [a for a in actions if a["kind"] == "web"]

    def probe(action):
        try:
            return answers(_page_of(action))
        except Exception as error:
            return False, str(error)[:60]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = dict(zip((id(a) for a in web), pool.map(probe, web)))
    kept = []
    for action in actions:
        ok, code = results.get(id(action), (True, ""))
        if ok:
            kept.append(action)
        else:
            dropped.append(f"'{action['title']}': its page isn't there ({code})")
    return kept


def _loads_anything(host: str, answers=None) -> bool:
    """True for a web app that answers any address (it routes after sign-in): a made-up page loads too."""
    return bool((answers or _site_answers)(f"https://{host}/mint-check-{uuid.uuid4().hex[:10]}")[0])


def _only_what_can_be_checked(result: dict, target: dict, answers=None) -> list[dict]:
    """On a site that loads any address, a deep link can't be checked - a wrong one would open a "not found" page
    after sign-in. Only its home page stays (Mint works inside it with the browser tool)."""
    actions = result["actions"]
    pages = [a for a in actions if a["kind"] == "web"]
    if not pages:
        return actions
    host = urllib.parse.urlparse(_page_of(pages[0])).netloc
    if not _loads_anything(host, answers):
        return actions
    home = result.get("site") or f"https://{host}/"
    kept = [a for a in actions if a["kind"] != "web"]
    try:
        kept.insert(0, check_action({"id": "open_home", "title": f"Open {result.get('name') or target.get('name')}",
                                     "kind": "web", "template": home, "changes": False}, target))
    except PlanError as error:
        result["dropped"].append(str(error))
    left = [a["title"] for a in pages if a["template"].rstrip("/") != home.rstrip("/")]
    if left:
        result["dropped"].append(f"{host} loads any address, so these links can't be checked: " + ", ".join(left[:5]))
        result["note"] = (f"{host} opens any address, so Mint keeps only its home page and works inside it with the "
                          "browser (search, open, read) - no link that could land on a missing page.")
    return kept


def check_links(item: dict, owner=_scheme_owner, answers=_site_answers) -> tuple[bool, str]:
    """The check for a connector whose actions open links, pages or hand files over (nothing to run read-only):
    the app is still installed, each of its link schemes opens that app, its website answers."""
    found: list[str] = []
    ok = True
    app = connectors.app_for(item["bundle_id"]) if item.get("bundle_id") else None
    if item.get("bundle_id") and app is None:
        return False, f"{item.get('name') or 'the app'} isn't on this Mac any more"
    schemes = []
    for action in item["actions"]:
        match = re.match(r"([a-z][a-z0-9+.-]*):", str(action.get("template") or ""), re.I)
        if action["kind"] == "url" and match and match.group(1).lower() not in ("http", "https", "file"):
            if match.group(1).lower() not in schemes:
                schemes.append(match.group(1).lower())
    for scheme in schemes:
        path = owner(scheme)
        if not path:
            ok = False
            found.append(f"nothing on this Mac opens {scheme}: links")
        elif app and Path(path).resolve() != Path(app["path"]).resolve():
            ok = False
            found.append(f"{scheme}: links open {Path(path).stem}, not {item.get('name')}")
        else:
            found.append(f"{scheme}: links open {Path(path).stem}")
    if any(a["kind"] == "files" for a in item["actions"]) and app:
        found.append(f"files open in {app['name']}")
    pages = [a for a in item["actions"] if a["kind"] == "web"]
    if pages:
        missing = []
        _reachable(pages, missing, answers=answers)
        host = urllib.parse.urlparse(_page_of(pages[0])).netloc
        if missing:
            ok = False
            found.append(f"{len(missing)} of {len(pages)} pages on {host} aren't there: " + "; ".join(missing[:2]))
        elif _loads_anything(host, answers):
            # a web app that routes after sign-in: its home page answers; a deep link couldn't be told apart from a
            # made-up one (plan keeps none for such a site - an older connector might still have some)
            homes = all(a["id"] == "open_home" or urllib.parse.urlparse(_page_of(a)).path in ("", "/") for a in pages)
            found.append(f"{host} opens" if homes else
                         f"{host} opens; its other links can't be checked (it loads any address)")
        else:
            found.append(f"all {len(pages)} pages on {host} are there")
    if not found:
        return True, "nothing to check - its actions only open pages"
    return ok, "; ".join(found)


def test(item: dict, save: bool = True) -> tuple[bool, str]:
    """Check that the connector really works: its read-only action run live (macOS may ask once for
    Automation), or - for one that only opens links, pages or hands over files - check_links."""
    action = next((a for a in item["actions"] if a["id"] == item.get("test")), None)
    if action is None or action["changes"] or action["kind"] != "applescript":
        action = next((a for a in item["actions"] if a["kind"] == "applescript" and not a["changes"]
                       and not any(p.get("required") for p in a["params"])), None)
    if action is None:
        ok, out = check_links(item)
        item["tested"] = {"ok": ok, "at": time.strftime("%Y-%m-%d %H:%M"), "action": "links", "result": out[:300]}
        if save and item.get("file"):
            connectors.save_custom(item)
        return ok, out
    ok, out = _execute(item, action, {})
    out = out.strip() or "ran, nothing to report (it answered with no text)"
    item["tested"] = {"ok": ok, "at": time.strftime("%Y-%m-%d %H:%M"), "action": action["id"], "result": out[:300]}
    if save and item.get("file"):
        connectors.save_custom(item)
    return ok, out


def create(plan_id: str = "", run_test: bool = True) -> str:
    with _lock:
        result = _state["plans"].get(plan_id or _state.get("last") or "")
    if result is None or not result.get("actions"):
        return "FAILED: there is no connector plan to save yet - plan it first (action=make), read it back, then create."
    cid = result["cid"] if result["cid"] not in connectors.BY_ID else f"my_{result['cid']}"   # not a library id
    existing = connectors.custom(cid)
    if existing and existing.get("bundle_id") != result.get("bundle_id"):
        cid = f"{cid}_{result['id'][:4]}"
    item = {"id": cid, "name": result["name"], "summary": result["summary"], "examples": result.get("examples") or [],
            "kind": result["how"], "app": result.get("app") or "", "app_file": result.get("app_file") or "",
            "bundle_id": result.get("bundle_id") or "",
            "domain": result.get("domain") or "", "site": result.get("site") or "", "actions": result["actions"],
            "test": result.get("test") or "", "made": time.strftime("%Y-%m-%d %H:%M"), "version": 1}
    words = ""
    if run_test:
        ok, out = test(item, save=False)
        what = next((a["title"] for a in item["actions"] if a["id"] == item["tested"]["action"]), "links")
        words = (f" Test “{what}”: " if what != "links" else " Checked: ") + (
            out[:200] if ok else f"didn't work - {out[:200]}")
    path = connectors.save_custom(item)
    result["created"] = cid
    changing = [a["title"] for a in item["actions"] if a["changes"]]
    return (f"DONE: saved the {item['name']} connector [{cid}] with {len(item['actions'])} actions ({path.name})."
            + words + (f" Changing actions ({', '.join(changing[:4])}) run only when the user asks for them."
                       if changing else ""))


def _match_action(item: dict, wanted: str) -> dict | None:
    wanted = str(wanted or "").strip().lower()
    for a in item["actions"]:
        if a["id"] == wanted or a["title"].lower() == wanted:
            return a
    words = set(re.findall(r"[a-z0-9]+", wanted))
    scored = sorted(((len(words & set(re.findall(r"[a-z0-9]+", f"{a['id'].replace('_', ' ')} {a['title'].lower()}"))),
                      a) for a in item["actions"]), key=lambda x: -x[0])
    return scored[0][1] if scored and scored[0][0] else None


def _allowed(item: dict, action: dict, values: dict) -> str:
    """'' when the action may run now; else what to say. Read-only always runs. Changing: the request asks for
    it (its verbs), or this is a yes after Mint asked. Sending / deleting / paying: those words, or that yes."""
    if not action["changes"]:
        return ""
    request = _request()
    from mint.tools.harness import _asked
    key = (item["id"], action["id"], json.dumps(values, sort_keys=True))
    pending = _state.get("pending")
    if pending and pending["key"] == key and request and request != pending["asked"] and _YES.match(request):
        _state["pending"] = None
        return ""
    def says(word: str) -> bool:                    # a whole word ("note" is not "notes"), not "don't <word>"
        return bool(re.search(r"\b" + re.escape(word) + r"\b", request)) and _asked(request, word)
    if action["risk"]:
        needed = [w for r in action["risk"] for w in RISK_WORDS[r]]
        ok = bool(request) and any(says(w) for w in needed)
    else:
        # Verbs only: the first word of the title and id, and of each of the planner's "asks" phrases that starts
        # with a verb ("desktop" or "notes" in a request doesn't ask for a change).
        heads = [action["title"], action["id"].replace("_", " ")] + list(action.get("asks") or [])
        verbs = {h.split()[0].lower() for h in heads if h.split() and h.split()[0].lower() in _VERBS}
        if not verbs:                               # a verb Mint has no list for: the title's own first word
            verbs = {h.split()[0].lower() for h in heads[:2] if h.split() and h.split()[0].isalpha()}
        verbs |= {s for v in list(verbs) for s in _SYNONYMS.get(v, ())}
        ok = bool(request) and any(says(w) for w in verbs)
    if ok:
        return ""
    _state["pending"] = {"key": key, "asked": request}
    what = ", ".join(action["risk"]) or "changes things"
    return (f"NOT DONE: '{action['title']}' {what}, and the user's request didn't ask for that. Ask them "
            f"(“Should I {action['title'].lower()}?”) and run it again only after their yes.")


def run(cid: str, wanted: str, values: dict | None = None) -> str:
    item = connectors.custom(cid)
    if item is None:
        lib = connectors.get(cid)
        if lib is not None:
            return (f"{lib.name} is built in: use its own tools ({', '.join(lib.tools()) or 'see connector status'}). "
                    "connector action=run is for connectors the user made.")
        return f"FAILED: no connector called '{cid}'. action=list shows them."
    action = _match_action(item, wanted)
    if action is None:
        return (f"FAILED: {item['name']} has no action '{wanted}'. It has: "
                + "; ".join(f"{a['id']} ({a['title']})" for a in item["actions"]))
    values = {str(k).lower(): v for k, v in (values or {}).items()}
    names = [p["name"] for p in action["params"]]
    if len(names) == 1 and names[0] not in values and "value" in values:
        values[names[0]] = values.pop("value")
    try:                                            # the saved file is re-checked: no edits by hand slip through
        action = check_action(action, _target(item), compile_it=False)
    except PlanError as error:
        return f"REFUSED: {error}."
    blocked = _allowed(item, action, values)
    if blocked:
        return blocked
    ok, out = _execute(item, action, values)
    if not ok:
        return f"FAILED: {item['name']} · {action['title']}: {out}"
    return f"DONE: {item['name']} · {action['title']}: {out[:3000] or 'done'}" + _after(item, action)


def _after(item: dict, action: dict) -> str:
    """An app often answers a scripted job with a question (Keka: where to save the .zip when it may not write
    next to the file): answer it for the job asked, or say what it asks."""
    if action.get("kind") != "applescript" or not action.get("changes") or not item.get("bundle_id"):
        return ""
    try:
        from mint.tools import handoff
        app = connectors.app_for(item["bundle_id"])
        if app is None:
            return ""
        said, _pressed = handoff._answer(app, handoff._job(action.get("title", "") + " " + _request()), wait=4.0)
        return f" {said}" if said else ""
    except Exception:
        log.debug("after the connector", exc_info=True)
        return ""


def _target(item: dict) -> dict:
    app = connectors.app_for(item["bundle_id"]) if item.get("bundle_id") else None
    return {"name": item.get("app") or item["name"], "file_name": item.get("app_file") or item.get("app") or "",
            "bundle_id": item.get("bundle_id") or "", "domain": item.get("domain") or "",
            "schemes": list(app.get("schemes") or []) if app else []}


# --- The tool ------------------------------------------------------------------------------------------------

def _not_agreed_yet(plan_id: str) -> str:
    """By voice, a connector is saved only after the user answers the plan (same rule as shortcut_maker)."""
    with _lock:
        result = _state["plans"].get(plan_id or _state.get("last") or "")
    if result is None:
        return ""
    now = _request()
    if result.get("asked") and now == result["asked"]:
        return ("NOT YET: the user hasn't answered the plan. Read the actions back, ask whether to save it, and wait "
                "for their yes (a new message) before action=create.")
    return ""


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    pairs = re.findall(r"([A-Za-z_][\w]*)\s*[=:]\s*(\"[^\"]*\"|'[^']*'|[^;,]+)", text)
    if pairs:
        return {k: v.strip().strip("\"'") for k, v in pairs}
    return {"value": text}


def tool(args: dict) -> str:
    action = str(args.get("action") or "list").lower()
    name = str(args.get("name") or args.get("connector") or "").strip()
    try:
        if action == "list":
            return connectors.list_text()
        if action == "status":
            return connectors.status_text(name) if name else connectors.list_text()
        if action == "connect":
            lib = connectors.get(name)
            if lib is not None:
                return lib.connect()
            item = connectors.custom(name)
            if item and item.get("bundle_id"):
                return connectors.ask_automation(item["bundle_id"], item.get("app") or item["name"])
            return f"FAILED: no connector called '{name}'. For a new app: action=make name=<app>."
        if action in ("make", "plan"):
            result = plan(str(args.get("name") or args.get("description") or ""))
            words = plan_text(result)
            if result.get("actions"):
                words += (f"\nRead the actions back briefly and ask whether to save it. Only after a yes: connector "
                          f"action=create plan_id={result['id']}.")
            return words
        if action == "create":
            waiting = _not_agreed_yet(str(args.get("plan_id") or ""))
            return waiting or create(str(args.get("plan_id") or ""))
        if action == "run":
            return run(str(args.get("connector") or args.get("name") or ""), str(args.get("do") or ""),
                       _args(args.get("args")))
        if action == "test":
            item = connectors.custom(name)
            if item is not None:
                ok, out = test(item)
                return ("DONE: " if ok else "FAILED: ") + f"{item['name']} test: {out[:400]}"
            lib = connectors.get(name)
            if lib is None:
                return f"FAILED: no connector called '{name}'."
            words = lib.test()
            return ("FAILED: " if words.startswith("Didn't work") else "NOT DONE: " if words.startswith("Not ready")
                    else "DONE: ") + words
        if action == "remove":
            from mint.tools.harness import _asked
            request = _request()
            if request and not any(_asked(request, w) for w in ("remove", "delete", "forget", "get rid", "uninstall",
                                                                  "disconnect")):
                return "NOT DONE: the user didn't ask to remove it. Ask first."
            return connectors.remove_custom(name)
        return f"FAILED: connector has no action '{action}' (list, status, connect, make, create, run, test, remove)."
    except PlanError as error:
        return f"FAILED: {error}"
    except Exception as error:
        log.exception("connector")
        return f"FAILED: {error}"


def prompt_addendum() -> str:
    """The user's own connectors, for the system prompt (optional; '' when there are none)."""
    items = connectors.custom_connectors()
    if not items:
        return ""
    return "The user's connectors (connector action=run connector=<id> do=<action>): " + "; ".join(
        f"{i['name']} [{i['id']}]: " + ", ".join(a["id"] for a in i["actions"]) for i in items) + "."


PROMPT = """Connectors - the apps and services Mint works with: "what can you connect to?", "which apps can you \
use?" -> connector action=list. "Is my Google connected?", "set up Contacts", "connect my calendar" -> \
action=status / action=connect name=<it> (connect opens the right settings or asks macOS for permission). \
"integrate Things", "connect Notion", "make a connector for <app>" -> action=make name=<app or service>: read the \
plan back in a sentence or two (what it can do, which actions change things) and ask whether to save it; ONLY \
after a yes (a new message) -> action=create plan_id=<id>. A saved connector: action=run connector=<id> \
do=<action id> args=<JSON of its params>. Actions that change things run only when the user asked for them in \
this request (otherwise ask first); never send messages, emails or payments, or delete anything, through a \
connector unless the user asked for exactly that. action=remove only when the user asks. Mail, Calendar, \
Reminders, Notes, Slack, Spotify and the other built-in ones keep using their own tools (status names them)."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="connector",
        description=("Connectors: list the apps and services Mint can work with and their status, set one up "
                     "(connect), make a new connector for any Mac app or web service from plain words (make = plan "
                     "and read back; create = save after the user agrees), run an action of a saved connector, "
                     "test or remove one."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["list", "status", "connect", "make", "create", "run", "test",
                                                 "remove"]),
            "name": types.Schema(type=S, description="status/connect/test/remove: the connector; make: the app or "
                                                     "service in the user's words"),
            "plan_id": types.Schema(type=S, description="create: the id from make (empty = the last plan)"),
            "connector": types.Schema(type=S, description="run: the saved connector's id"),
            "do": types.Schema(type=S, description="run: the action id"),
            "args": types.Schema(type=S, description="run: the action's values as JSON, e.g. {\"title\": \"milk\"}")},
            required=["action"]))]


HANDLERS = {"connector": tool}


if __name__ == "__main__":
    import sys
    print(plan_text(plan(" ".join(sys.argv[1:]) or "integrate TextEdit")))
