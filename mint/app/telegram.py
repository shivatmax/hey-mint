"""Telegram remote control: text or voice-note Mint from your phone, and watch it work.

The Mac asks Telegram for new messages (Bot API long polling over HTTPS). Nothing listens for
connections and there is no server in between: the user's own bot (made with @BotFather) is
the whole channel, and its token lives in Mint's .env (mode 600) like the other keys.

Pairing: while nobody is paired, Settings shows a 6-digit code. The first Telegram user who
sends it to the bot in a private chat becomes the only one Mint listens to. Everyone else gets
one polite "this bot is private" and is then ignored; groups and channels are never answered.

A request from the phone goes into the running session exactly as if it were typed
(Mint.inject_text), so live.request() holds the user's own words and every confirmation rule
for risky actions applies unchanged. While it runs, one "working" message is edited in place
with the steps (a spinner, the time, the app; at most one edit per 1.5 s) and buttons under it
(⏸ Pause · ⏹ Stop · 📸 Screen), Mint's replies follow as formatted messages, island cards come
as pictures, and at the end screenshots and files it made in that request are sent back.

It should feel like an app inside Telegram: a command menu (setMyCommands), buttons under
messages (callback queries: only the paired user, answered with a toast, audited, and a press
that sat queued while the Mac slept is not acted on), a quick-reply keyboard (/keyboard),
"typing…" while Mint works, and alerts from trackers and agents with buttons.

Pause: Mint cannot freeze a step halfway, so ⏸ stops the step it is on (its plan is kept, as a
paused task, and the autopilot is held) and holds the phone's messages; ▶️ asks Mint to carry on
from where it stopped.

Files both ways. A document, photo (the largest size), video or audio file sent from the phone
(forwarded ones too) is fetched with getFile - the Bot API gives bots files up to 20 MB - and saved
under its own name, never overwriting, in the "From phone" folder of Mint's storage. With a caption
("summarise this") the caption runs as a request that names the saved path; without one the file
comes back as a card with buttons that fit it (Summarise, Translate, Copy text…). Album items
(media_group_id) are gathered into one request. The other way: /clip sends the Mac's clipboard
(never a concealed item or anything that looks like a secret), /last the last screenshot, /files
what Mint made lately, /send a file under home (not hidden, Library or secret files; up to 50 MB,
a folder zipped only when asked), and /paste puts text on the Mac's clipboard, as from the phone.

The session tells this module what is happening through on_event(kind, data); gate(name, args)
lets it refuse sends, deletes and purchases while a phone request runs in read-only mode.
Every remote request, command, button, file and refusal goes to ~/Library/Application Support/Mint/remote.log
(file names and sizes, never their contents).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html as _html
import itertools
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import zipfile
from pathlib import Path

log = logging.getLogger("mint.app.telegram")
# httpx logs every request URL at INFO, and a Bot API URL holds the token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
API = "https://api.telegram.org"
SUPPORT = Path.home() / "Library" / "Application Support" / "Mint"
STATE = SUPPORT / "telegram.json"
AUDIT = SUPPORT / "remote.log"

POLL_WAIT = 25              # seconds Telegram holds getUpdates open when nothing comes
EDIT_EVERY = 1.5            # seconds between edits of the working message
IDLE_EDIT = 5.0             # nothing new in it: refresh its clock and spinner this often
TYPING_EVERY = 4.5          # Telegram shows "typing…" for 5 s per chat action
STALE = 600                 # a message older than this (the Mac was asleep) is not run
QUIET_END = 90              # after Mint's reply, this long with nothing more ends the request
SILENT_END = 240            # no event at all for this long: give up mirroring
LONGEST = 30 * 60           # a request mirrored for this long is closed
MAX_FILE = 20 * 1024 * 1024    # the most a bot may download (getFile)
MAX_UPLOAD = 50 * 1024 * 1024  # the most a bot may send (sendDocument)
MAX_FILES = 5
ALBUM_WAIT = 1.2            # an album's items come as separate messages: gather them for this long
INBOX = "From phone"        # the folder in Mint's storage that files from the phone go to
MAX_TEXT = 4000             # Telegram's limit is 4096 characters
MAX_CAPTION = 1000          # and 1024 for a caption
MAX_TRIES = 5               # wrong codes per person before they are ignored
MAX_ALL_TRIES = 20          # wrong codes in all before the code is replaced
KEEP_BUTTONS = 40           # "Run again" and alert buttons remembered (older ones say they expired)

PREFS = {"telegram_enabled": False, "telegram_read_only": False, "telegram_notify": True, "agent_telegram": "away"}

SPINNER = "◐◓◑◒"

# The command menu (setMyCommands): what the ☰ button next to the message box lists.
COMMANDS = [("status", "What Mint is doing now"), ("screenshot", "A picture of the screen"),
            ("stop", "Stop whatever Mint is doing"), ("pause", "Pause Mint's work and hold my messages"),
            ("resume", "Carry on after a pause"), ("briefing", "Brief me: my day"),
            ("missed", "What did I miss? (notifications)"), ("clip", "Send me the Mac's clipboard"),
            ("last", "The last screenshot"), ("files", "Files Mint made lately"),
            ("send", "Send me a file from the Mac: /send name or path"),
            ("paste", "Put text on the Mac's clipboard: /paste text"), ("clipboard", "My last copies"),
            ("trackers", "What Mint is watching for me"), ("undo", "What Mint can undo"),
            ("chatgpt", "Is the ChatGPT app busy or done?"), ("claude", "Claude Code sessions"),
            ("agents", "Claude Code and Codex: what they do, talk to them"),
            ("meet", "Start a Google Meet with Mint: it shares the screen, you talk"),
            ("keyboard", "Show or hide the quick buttons"), ("help", "Everything I can do")]
STRANGER_COMMANDS = [("start", "Pair this chat with Mint on your Mac")]

# The quick-reply keyboard: each button sends its words, which mean this command.
KEYS = {"📋 What did I miss?": "missed", "🗓 My day": "briefing", "📸 Screen": "screenshot", "⏹ Stop": "stop",
        "🧠 Status": "status", "📋 Clipboard": "clip", "🖼 Last screenshot": "last"}
KEYBOARD = {"keyboard": [[{"text": "📋 What did I miss?"}, {"text": "🗓 My day"}],
                         [{"text": "📸 Screen"}, {"text": "⏹ Stop"}, {"text": "🧠 Status"}],
                         [{"text": "📋 Clipboard"}, {"text": "🖼 Last screenshot"}]],
            "is_persistent": True, "resize_keyboard": True, "input_field_placeholder": "Ask Mint anything…"}

BRIEF = "Brief me on my day."
MISSED = "What did I miss? Check my notifications."
RESUME_TASK = ("Carry on with the task you were doing when I paused you from my phone: resume its plan from the step "
               "you stopped at. Check where things stand first; don't redo steps that are done.")
RESUME_TEXT = ("Carry on with what I asked before I paused you from my phone, from where you stopped. Check where "
               "things stand first; don't redo anything already done.")

HELP = ("🌿 <b>Mint on your phone</b>\n"
        "Send a request as text or a voice note 🎙 and I'll do it on your Mac, showing each step here.\n\n"
        "<b>Right now</b>\n"
        "/status · what Mint is doing, and whether the screen is locked\n"
        "/screenshot · a picture of the screen\n"
        "/stop · stop whatever Mint is doing\n"
        "/pause · pause Mint's work and hold my messages\n"
        "/resume · carry on after a pause\n\n"
        "<b>Your Mac</b>\n"
        "/briefing · brief me: my day\n"
        "/missed · what did I miss? (notifications)\n"
        "/clipboard · my last copies (tap one to copy it)\n"
        "/trackers · what Mint is watching for me\n"
        "/undo · what Mint can undo\n"
        "/meet · a Google Meet with Mint: it shares the Mac's screen and you talk to it\n"
        "/chatgpt · /claude · is the ChatGPT app or Claude Code busy or done\n\n"
        "<b>Your coding agents</b>\n"
        "/agents · your Claude Code and Codex sessions; 💬 tells one what to do\n"
        "While you're away they message you here: ✅ Allow / ⛔ Deny a permission, or ↩️ reply to one of their "
        "messages (<i>push</i>) and I type it into that session\n\n"
        "<b>Files</b>\n"
        "📎 Send a file or photo: with a caption (<i>summarise this</i>) I do it; without one I save it in "
        "<i>From phone</i> and offer what fits (up to 20 MB)\n"
        "/clip · send me what's on the Mac's clipboard\n"
        "/last · the last screenshot (/last 3 for three)\n"
        "/files · files Mint made lately, to send here\n"
        "/send <i>name or path</i> · a file from the Mac (up to 50 MB; <i>as zip</i> for a folder)\n"
        "/paste <i>text</i> · put text on the Mac's clipboard\n\n"
        "<b>This chat</b>\n"
        "/keyboard · show or hide the quick buttons\n"
        "/unpair · disconnect this chat from Mint\n"
        "/help · this list\n\n"
        "<i>About pause: Mint can't freeze a step halfway, so ⏸ Pause stops the step it is on, keeps its plan, holds "
        "Mint's autopilot and ignores new messages from here; ▶️ Resume asks Mint to carry on from where it "
        "stopped.</i>")

PAUSED_NOTE = "Remote control is paused. Send /resume to carry on."

LOCKED_NOTE = ("🔒 The Mac's screen is locked: clicking, typing and apps on screen may fail. "
               "Files, the web, agents and reminders still work.")

# Tools that are bookkeeping, not steps the user would recognise.
_QUIET = {"plan_task", "step_done", "task", "express", "move_orb", "show_chat", "memory_used", "skill_result",
          "find_skill", "show_card", "clear_marks"}

# Read-only from Telegram: the words that mean a send, a delete or a purchase.
_RO_WORDS = re.compile(
    r"\b(send|sending|sent|submit|post|publish|tweet|reply|forward|delete|deleting|remove|erase|wipe|trash|"
    r"discard|empty|unsubscribe|buy|purchase|pay|payment|checkout|check out|place (?:the |my |an? )?order|"
    r"order now)\b", re.I)
_MESSAGING = ("messages", "slack", "whatsapp", "telegram", "discord", "teams", "signal", "mail", "outlook",
              "spark", "messenger", "wechat", "line", "skype", "zoom")
# Where a text is checked for those words, per tool: (argument names, only for these actions).
_RO_TEXT = {
    "ui_act": (("target", "text"), None), "click_text": (("text",), None), "menu": (("path",), None),
    "browser": (("target", "text"), {"click", "fill", "select", "js"}), "desktop": (("goal",), None),
    "run_applescript": (("script",), None), "shortcut": (("name", "input"), {"run"}),
    "delegate_task": (("task",), None), "delegate_tasks": (("tasks",), None), "message_agent": (("message",), None),
    "answer_agent": (("answer",), None), "automation": (("do", "name"), {"create"}),
    "system_action": (("action",), None), "run_routine": (("name",), None),
}
_RO_ACTIONS = {"notifications": {"dismiss", "clear_all"}, "file_action": {"trash"}, "tidy": {"remove_duplicates"},
               "clipboard": {"forget_secret", "clear"}, "automation": {"delete"},
               "make_shortcut": {"create", "add"}, "connector": {"create", "remove", "connect", "run"}}
_RO_TOOLS = {"forget", "delete_skill"}

_EXTS = ("png|jpe?g|gif|heic|webp|pdf|docx?|xlsx?|csv|pptx?|key|pages|numbers|txt|md|html?|rtf|json|"
         "mp4|mov|m4v|m4a|mp3|wav|aiff?|zip")

# A file from the phone without a caption: buttons that fit it, each a request to Mint ({what} = its path).
# Worded so the words Mint's send and risk checks look for ("send", "text …", "delete"…) never appear.
_FILE_ASKS = {
    "sum": ("📄 Summarise", "Summarise {what}."),
    "watch": ("📄 Summarise", "Watch {what} and tell me what it's about."),
    "data": ("📄 Summarise", "Summarise the data in {what}."),
    "tr": ("🌐 Translate", "Translate {what} into Hindi, as a Word doc."),
    "trpic": ("🌐 Translate", "Translate the words in the picture {what} into Hindi."),
    "sheet": ("📊 To spreadsheet", "Make a spreadsheet of the tables in {what}."),
    "copy": ("🔤 Copy text", "Copy all the words in {what} to my clipboard."),
    "ocr": ("🔤 Copy text (OCR)", "Read the words in {what} with OCR and copy them to my clipboard."),
    "cap": ("🎬 Captions", "Add captions to {what}."),
    "tx": ("🗒 Transcribe", "Transcribe {what}."),
    "what": ("📄 What's in it?", "What's in {what}?"),
}
_FILE_BUTTONS = {"document": ("sum", "tr", "sheet", "copy"), "image": ("ocr", "trpic", "sheet"),
                 "video": ("watch", "cap", "tx"), "audio": ("tx", "sum"), "sheet": ("data",),
                 "other": ("what",), "many": ("sum", "sheet")}
_ARCHIVES = (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".dmg", ".pkg")


# --- the Bot API ----------------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, description: str, code: int = 0, retry_after: float = 0):
        super().__init__(description)
        self.code, self.retry_after = code, retry_after


class Bot:
    """The few Bot API calls Mint needs. `api` points at a local mock in tests."""

    def __init__(self, token: str, api: str = API, wait: float = POLL_WAIT):
        import httpx
        self.token, self.api = token, api.rstrip("/")
        self.http = httpx.Client(timeout=httpx.Timeout(wait + 15, connect=10))

    def _clean(self, text: str) -> str:
        return str(text).replace(self.token, "<token>") if self.token else str(text)

    def call(self, method: str, files: dict | None = None, **params):
        import httpx
        url = f"{self.api}/bot{self.token}/{method}"
        try:
            if files:
                form = {k: (json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)) for k, v in params.items()}
                response = self.http.post(url, data=form, files=files)
            else:
                response = self.http.post(url, json=params)
        except (httpx.HTTPError, RuntimeError) as error:     # RuntimeError: closed by refresh()
            raise ApiError(f"network: {self._clean(error)[:160]}") from None
        try:
            data = response.json()
        except ValueError:
            raise ApiError(f"HTTP {response.status_code}", response.status_code) from None
        if not data.get("ok"):
            retry = (data.get("parameters") or {}).get("retry_after", 0)
            raise ApiError(self._clean(data.get("description", "error"))[:200],
                           int(data.get("error_code") or response.status_code), float(retry or 0))
        return data.get("result")

    def download(self, file_path: str) -> bytes:
        import httpx
        try:
            response = self.http.get(f"{self.api}/file/bot{self.token}/{file_path}")
            response.raise_for_status()
        except (httpx.HTTPError, RuntimeError) as error:
            raise ApiError(f"download: {self._clean(error)[:160]}") from None
        return response.content

    def close(self) -> None:
        try:
            self.http.close()
        except Exception:
            pass


# --- the running session ------------------------------------------------------------------------

class SessionHost:
    """Mint's running session, reached from these threads through its asyncio loop."""

    def __init__(self, mint=None):
        self._mint = mint

    @property
    def mint(self):
        if self._mint is not None:
            return self._mint
        try:
            from mint.agents.runtime import hub
            return hub.mint
        except Exception:
            return None

    def ready(self) -> bool:
        mint = self.mint
        return mint is not None and getattr(mint, "loop", None) is not None

    def _run(self, coroutine, wait: float = 20.0):
        return asyncio.run_coroutine_threadsafe(coroutine, self.mint.loop).result(wait)

    def inject(self, text: str, asked: str | None = None) -> None:
        """`asked` is only the user's own words: they, not a file name in `text`, decide what counts as asked."""
        try:
            self._run(self.mint.inject_text(text, asked=asked))
        except TypeError:                            # an older session without `asked`
            self._run(self.mint.inject_text(text))

    def stop(self, source: str = "telegram") -> None:
        """Mint's own stop: cuts the running tool, holds the autopilot and keeps a task plan as paused."""
        self._run(self.mint.stop_everything(source))

    def doing(self) -> dict:
        mint = self.mint
        if mint is None:
            return {"running": False}
        facts = {"running": getattr(mint, "loop", None) is not None,
                 "connected": getattr(mint, "session", None) is not None,
                 "asleep": bool(getattr(mint, "asleep", False)), "paused": bool(getattr(mint, "paused", False)),
                 "busy": bool(getattr(mint, "_busy", False))}
        task = getattr(mint, "task", None)
        if task:
            try:
                from mint.app import tasks
                done, total = tasks.progress(task)
                facts["task"] = f"{task.get('goal', '')} ({done}/{total} steps done)"
            except Exception:
                facts["task"] = str(task.get("goal", ""))
        return facts


# --- one request from the phone -----------------------------------------------------------------

class Request:
    """What was asked from Telegram and what Mint has done for it so far."""

    def __init__(self, text: str, kind: str, message_id: int | None, locked: bool = False, asked: str = ""):
        self.text, self.kind, self.message_id, self.locked = text, kind, message_id, locked
        self.asked = asked or text          # the user's own words ("Carry on…" is sent after a pause)
        self.rid = ""                       # its "Run again" button
        self.share = ""                     # its "📤 Share" button: files it named but didn't make
        self.share_n = 0
        self.wall = time.time()
        self.started = self.last_event = time.monotonic()
        self.last_reply = 0.0
        self.status_id: int | None = None
        self.steps: list[str] = []          # "✓ Opening Safari"
        self.current = ""                   # the step running now
        self.app = ""                       # the app the steps are in
        self.plan: list[list] = []          # [text, "·" | "✓" | "✗"]
        self.plan_goal = ""
        self.results: list[str] = []        # tool results, to find the files they name
        self.sent: set[str] = set()         # files already sent back
        self.cards: set[str] = set()        # cards already sent (by what they say)
        self.scenes: set[int] = set()       # island cards seen (up before it began, or sent)
        self.scan_until = 0.0               # look for new island cards until then
        self.confirmed = False              # the session has taken it ("[typed: …]")
        self.dirty = False
        self.shown = ""
        self.frame = 0                      # the spinner turns one frame per edit
        self.last_edit = 0.0
        self.finished = ""

    def start_tool(self, name: str, args: dict) -> None:
        if name == "plan_task":
            self.plan_goal = str(args.get("goal", ""))[:80]
            self.plan = [[str(s)[:80], "·"] for s in (args.get("steps") or []) if str(s).strip()][:12]
            self.dirty = True
            return
        if name in _QUIET:
            return
        app = args.get("app") or (args.get("name") if name in ("open_app", "focus_app", "quit_app") else "")
        if app and isinstance(app, str):
            self.app = app[:30]
        self.current = _caption(name, args)
        self.dirty = True

    def end_tool(self, name: str, args: dict, result: str) -> None:
        self.results.append(result)
        if name == "step_done":
            number = str(args.get("step", "")).strip().rstrip(".")
            if number.isdigit() and 0 < int(number) <= len(self.plan):
                self.plan[int(number) - 1][1] = "✗" if args.get("failed") else "✓"
                self.dirty = True
            return
        if name in _QUIET:
            return
        caption = self.current or _caption(name, args)
        self.steps.append(("✗ " if _looks_failed(result) else "✓ ") + caption)
        self.current = ""
        self.dirty = True

    def render(self) -> str:
        """The working message, as Telegram HTML: a header with a spinner, the words, the plan and the steps."""
        took = _took(time.monotonic() - self.started)
        count = f" · {len(self.steps)} step{'' if len(self.steps) == 1 else 's'}" if self.steps else ""
        if not self.finished:
            app = self.app or _frontmost_app()
            header = f"{SPINNER[self.frame % len(SPINNER)]} <b>Working</b> · {took}" + (f" · {esc(app)}" if app else "")
        else:
            header = {"done": f"✓ <b>Done</b> · {took}{count}", "stopped": f"⏹ <b>Stopped</b> · {took}{count}",
                      "paused": f"⏸ <b>Paused</b> · {took}{count}", "local": "↪ <b>Carried on at the Mac</b>",
                      "next": "↪ <b>Replaced by your next message</b>", "quiet": f"✓ <b>Finished</b> · {took}{count}",
                      "failed": "✗ <b>Could not reach Mint</b>"}.get(self.finished, esc(self.finished))
        top = [header, f"<i>“{esc(_short(self.asked, 120))}”</i>"]
        if self.locked:
            top.append(f"<i>{esc(LOCKED_NOTE)}</i>")
        if self.plan:
            done = sum(1 for _, mark in self.plan if mark == "✓")
            top += ["", f"<b>Plan</b> · {done}/{len(self.plan)}" + (f" · {esc(self.plan_goal)}" if self.plan_goal
                                                                    else "")]
            first_open = next((i for i, (_, mark) in enumerate(self.plan) if mark == "·"), None)
            for i, (step, mark) in enumerate(self.plan):
                shown = "▸" if i == first_open and not self.finished else mark
                top.append(f"{shown} {i + 1}. {esc(step)}")
        tail = []
        if self.current and not self.finished:
            tail.append(f"▸ <i>{esc(self.current)}…</i>")
        elif not self.steps and not self.finished:
            tail.append("▸ <i>Thinking…</i>")
        if self.finished == "paused":
            tail.append("\n<i>Tap ▶️ Resume to carry on from here.</i>")
        steps = [esc(s) for s in self.steps[-14:]]
        while True:
            lines = list(top)
            if steps or tail:
                lines.append("")
            if len(self.steps) > len(steps):
                lines.append(f"<i>… {len(self.steps) - len(steps)} earlier steps</i>")
            text = "\n".join(lines + steps + tail)
            if len(text) <= MAX_TEXT or not steps:
                return text if len(text) <= MAX_TEXT else _fit(text)
            steps = steps[1:]

    def buttons(self) -> dict:
        """Under the working message: what makes sense now."""
        again = [("🔁 Run again", f"again:{self.rid}")] if self.rid else []
        if not self.finished:
            return keys([("⏸ Pause", "pause"), ("⏹ Stop", "stop"), ("📸 Screen", "shot")])
        if self.finished == "paused":
            return keys([("▶️ Resume", "resume"), ("⏹ Stop", "stop"), ("📸 Screen", "shot")])
        if self.finished in ("done", "quiet"):
            share = [("📤 Share" if self.share_n == 1 else f"📤 Share {self.share_n}", f"share:{self.share}")] \
                if self.share else []
            return keys(again + share + [("↩️ Undo", "undo"), ("📸 Screen", "shot")])
        if self.finished in ("stopped", "failed"):
            return keys(again + [("📸 Screen", "shot")])
        return {"inline_keyboard": []}


# --- the bridge ---------------------------------------------------------------------------------

class Bridge:
    """The poller, the mirror and the pairing state. One per app (`bridge` below); tests make their own."""

    def __init__(self, host=None, api: str = API, state_path: Path = STATE, audit_path: Path = AUDIT,
                 setting=None, token=None, poll_wait: float = POLL_WAIT, edit_every: float = EDIT_EVERY,
                 inbox: Path | None = None):
        self.host = host
        self.inbox = Path(inbox) if inbox else None     # where files from the phone go (tests); else storage
        self.api, self.state_path, self.audit_path = api, Path(state_path), Path(audit_path)
        self._setting = setting
        self._token = token or (lambda: os.environ.get(TOKEN_ENV, "").strip())
        self.poll_wait, self.edit_every = poll_wait, edit_every
        self.events: queue.Queue = queue.Queue()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.quit = threading.Event()
        self.request: Request | None = None
        self.held: dict | None = None       # work paused from the phone: {request, asked, kind, task}
        self.bot: Bot | None = None
        self.bot_name = ""
        self.running = False
        self.error = ""
        self.state_now = ("", "")           # the session's last UI state and note
        self.notice_until = 0.0
        self.announced_at = 0.0
        self.typing_at = 0.0
        self.draw = card_png                # island card -> PNG (tests swap it)
        self.again: dict[str, str] = {}     # "Run again" buttons: id -> the words
        self.alerts: dict[str, dict] = {}   # alert buttons: id -> the tracker that finished
        self.status_ids: list[int] = []     # /status messages (their buttons refresh them)
        self.received: dict[str, list] = {} # files from the phone: id -> their saved paths (for the buttons)
        self.shares: dict[str, list] = {}   # "📤" buttons: id -> paths to send here
        self.albums: dict[str, dict] = {}   # media_group_id -> the items gathered so far
        self.pasteboard = None              # the pasteboard /clip reads (tests: a private one)
        self._ids = itertools.count(1)
        self._threads: list[threading.Thread] = []
        self._state = self._load()
        self.agent_msgs: dict = {}          # coding agents (telegram_agents): message id -> session
        self.agent_btns: dict = {}
        self.agent_follow: dict = {}

    # settings and state -----------------------------------------------------------------------

    def setting(self, key: str):
        if self._setting is not None:
            return self._setting(key)
        from mint.core import prefs
        value = prefs.get(key)
        return PREFS.get(key) if value is None else value

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        with self.lock:
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._state, indent=1))
                os.chmod(tmp, 0o600)
                tmp.replace(self.state_path)
            except OSError as error:
                log.warning("saving telegram state: %s", error)

    def _set(self, **values) -> None:
        with self.lock:
            self._state.update(values)
            self._save()

    def paused(self) -> bool:
        return bool(self._state.get("paused"))

    def paired(self) -> dict | None:
        with self.lock:
            if self._state.get("user_id"):
                return {k: self._state.get(k) for k in ("user_id", "chat_id", "name", "paired_at")}
        return None

    def pairing_code(self, new: bool = False) -> str:
        """The code Settings shows while nobody is paired ('' once someone is)."""
        with self.lock:
            if self._state.get("user_id"):
                return ""
            if new or not re.fullmatch(r"\d{6}", str(self._state.get("code", ""))):
                self._state["code"] = f"{secrets.randbelow(10 ** 6):06d}"
                self._state["tries"], self._state["all_tries"] = {}, 0
                self._save()
            return self._state["code"]

    def unpair(self) -> None:
        with self.lock:
            chat = self._state.get("chat_id")
            name = self._state.get("name", "")
            for key in ("user_id", "chat_id", "name", "paired_at", "strangers", "paused", "keyboard"):
                self._state.pop(key, None)
            self._save()
        self.held = None
        self.pairing_code(new=True)
        self.audit("unpair", f"was {name}" if name else "")
        if chat and self.bot is not None:
            self._send_quietly(chat, "Unpaired: this chat no longer controls Mint.",
                               reply_markup={"remove_keyboard": True})
            for method, params in (("deleteMyCommands", {"scope": {"type": "chat", "chat_id": chat}}),
                                   ("setChatMenuButton", {"chat_id": chat, "menu_button": {"type": "default"}})):
                try:
                    self._call(method, **params)
                except ApiError as error:
                    log.info("telegram %s: %s", method, error)

    def audit(self, kind: str, text: str = "", outcome: str = "") -> None:
        """One line per remote event in remote.log (mode 600, rolled over at 1 MB)."""
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {kind:<10} "
                + (json.dumps(text, ensure_ascii=False) if text else "")
                + (f"  -> {outcome}" if outcome else "") + "\n")
        with self.lock:
            try:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                if self.audit_path.exists() and self.audit_path.stat().st_size > 1_000_000:
                    self.audit_path.replace(self.audit_path.with_suffix(".log.1"))
                with open(self.audit_path, "a") as out:
                    out.write(line)
                os.chmod(self.audit_path, 0o600)
            except OSError as error:
                log.warning("remote.log: %s", error)

    def status(self) -> dict:
        """For Settings: is it on, who is paired, the code to show, and the last problem."""
        who = self.paired()
        return {"token_set": bool(self._token()), "enabled": bool(self.setting("telegram_enabled")),
                "running": self.running, "bot": self.bot_name, "error": self.error,
                "paired": who, "code": "" if who else self.pairing_code(),
                "read_only": bool(self.setting("telegram_read_only")), "notify": bool(self.setting("telegram_notify")),
                "paused": self.paused()}

    # threads -------------------------------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            self.wake.set()
            return
        for target, name in ((self._poll_loop, "telegram-poll"), (self._mirror_loop, "telegram-mirror")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop_threads(self) -> None:
        self.quit.set()
        self.wake.set()
        for thread in self._threads:
            thread.join(timeout=self.poll_wait + 20)
        self._threads = []
        if self.bot is not None:
            self.bot.close()
            self.bot = None

    def _poll_loop(self) -> None:
        while not self.quit.is_set():
            try:
                self._poll()
            except Exception:
                log.exception("telegram poller")        # never die: try again shortly
                self.wake.wait(5)
                self.wake.clear()

    def _poll(self) -> None:
        pause = 0.0
        while not self.quit.is_set():
            token = self._token()
            if not (token and self.setting("telegram_enabled")):
                if self.setting("telegram_enabled") and not token and not getattr(self, "_said_no_token", False):
                    # Switched on but the token is gone (seen: a reinstall overwrote .env): say so, don't sit silent.
                    self._said_no_token = True
                    self.error = "Remote control is on but there is no bot token - add it again in Settings."
                    print("  [telegram: switched on, but no bot token - add it in Settings ▸ Accounts & keys]",
                          flush=True)
                if self.bot is not None:
                    self.bot.close()
                    self.bot, self.bot_name = None, ""
                self.running, self.error = False, ""
                self.wake.wait(5)
                self.wake.clear()
                continue
            if self.bot is None or self.bot.token != token:
                if self.bot is not None:
                    self.bot.close()
                self.bot = Bot(token, self.api, self.poll_wait)
                try:
                    me = self.bot.call("getMe")
                    self.bot_name = "@" + str(me.get("username", ""))
                    with self.lock:
                        if self._state.get("bot_id") != me.get("id"):
                            # Another bot: its update numbers start again (an old offset would skip them).
                            self._state.update(bot_id=me.get("id"), offset=0)
                            self._save()
                    print(f"  [telegram: connected as {self.bot_name}]", flush=True)
                except ApiError as error:
                    self._trouble(error)
                    self.bot = None
                    self.wake.wait(60 if error.code in (401, 404) else 15)
                    self.wake.clear()
                    continue
                self._menu()
            try:
                updates = self.bot.call("getUpdates", offset=int(self._state.get("offset", 0)),
                                        timeout=int(self.poll_wait), allowed_updates=["message", "callback_query"])
                self.running, self.error, pause = True, "", 0.0
            except ApiError as error:
                if self.quit.is_set() or self.bot.token != self._token() or not self.setting("telegram_enabled"):
                    continue                    # refresh() cut the poll short: a new token, or switched off
                self._trouble(error)
                pause = error.retry_after or min(60.0, max(2.0, pause * 2))
                self.wake.wait(pause)
                self.wake.clear()
                continue
            for update in updates or []:
                if not self.setting("telegram_enabled") or self.bot.token != self._token():
                    break                       # switched off or a new token while this poll was open
                with self.lock:
                    self._state["offset"] = int(update.get("update_id", 0)) + 1
                    self._save()
                try:
                    self.handle(update)
                except Exception:
                    log.exception("telegram update failed")

    def _trouble(self, error: ApiError) -> None:
        words = {401: "Telegram rejected the bot token - check it in Settings ▸ Accounts & keys.",
                 404: "Telegram rejected the bot token - check it in Settings ▸ Accounts & keys.",
                 409: "Another program is reading this bot's messages (a webhook or a second copy of Mint)."}
        message = words.get(error.code, str(error))
        if message != self.error:
            print(f"  [telegram: {message}]", flush=True)
        self.running, self.error = False, message

    def _menu(self) -> None:
        """The ☰ command menu: /start for everyone, the full list only in the paired chat."""
        who = self.paired()
        calls = [("setMyCommands", {"commands": _commands(STRANGER_COMMANDS)})]
        if who:
            calls += [("setMyCommands", {"commands": _commands(COMMANDS),
                                         "scope": {"type": "chat", "chat_id": who["chat_id"]}}),
                      ("setChatMenuButton", {"chat_id": who["chat_id"], "menu_button": {"type": "commands"}})]
        for method, params in calls:
            try:
                self._call(method, **params)
            except ApiError as error:
                log.info("telegram %s: %s", method, error)

    # incoming messages ----------------------------------------------------------------------

    def handle(self, update: dict) -> None:
        if isinstance(update.get("callback_query"), dict):
            self._button(update["callback_query"])
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat, sender = message.get("chat") or {}, message.get("from") or {}
        # Only one-to-one chats: never act on (or answer) groups, channels or bots.
        if chat.get("type") != "private" or sender.get("is_bot") or not sender.get("id") \
                or chat.get("id") != sender.get("id"):
            return
        with self.lock:
            owner, owner_chat = self._state.get("user_id"), self._state.get("chat_id")
        if not owner:
            self._pairing(message, sender)
            return
        if sender["id"] != owner or chat["id"] != owner_chat:
            self._stranger(sender["id"])
            return
        text = str(message.get("text") or message.get("caption") or "").strip()
        age = time.time() - float(message.get("date") or time.time())
        attached = _attachment(message)
        if attached:
            # A file (or photo, video, audio file; forwarded too): saved even while paused or when it is old,
            # but its caption only runs as a request when neither.
            self._receive(message, attached, text, age)
            return
        if text in KEYS:
            text = "/" + KEYS[text]             # a quick-reply button: its command
        if text.startswith("/"):
            self._command(text, message, age)
            return
        if message.get("reply_to_message"):
            from mint.app import telegram_agents
            if telegram_agents.on_reply(self, message, text, age):
                return                          # a reply to a coding agent: typed into that session
        if self.paused():
            self.reply(PAUSED_NOTE)
            self.audit("paused", text, "not run")
            return
        if age > STALE:
            self.reply(f"This came while the Mac was asleep or offline ({_took(age)} ago), so I didn't run it. "
                       "Send it again if you still want it.")
            self.audit("stale", text, f"not run ({int(age)} s old)")
            return
        media = message.get("voice")
        if media:
            threading.Thread(target=self._voice, args=(message, media), name="telegram-voice", daemon=True).start()
        elif text:
            self.run(text, message, "text")
        else:
            self.reply("I can take text messages, voice notes, files and photos.")

    def _pairing(self, message: dict, sender: dict) -> None:
        text = str(message.get("text") or "").strip()
        code = self.pairing_code()
        uid = str(sender["id"])
        attempt = re.sub(r"^/start\s*", "", text).replace(" ", "")
        with self.lock:
            tries = self._state.setdefault("tries", {})
            if tries.get(uid, 0) >= MAX_TRIES:
                return
            if re.fullmatch(r"\d{6}", attempt) and secrets.compare_digest(attempt, code):
                name = " ".join(filter(None, (sender.get("first_name"), sender.get("last_name")))) or "someone"
                if sender.get("username"):
                    name += f" (@{sender['username']})"
                self._state.update(user_id=sender["id"], chat_id=message["chat"]["id"], name=name,
                                   paired_at=time.strftime("%Y-%m-%d %H:%M"), strangers=[], keyboard=True)
                for key in ("code", "tries", "all_tries"):
                    self._state.pop(key, None)
                self._save()
                paired = True
            else:
                paired = False
                if re.fullmatch(r"\d{6}", attempt):
                    tries[uid] = tries.get(uid, 0) + 1
                    self._state["all_tries"] = int(self._state.get("all_tries", 0)) + 1
                    self._save()
        if paired:
            self.audit("paired", name)
            print(f"  [telegram: paired with {name}]", flush=True)
            self._menu()
            self.welcome(fresh=True)
            self.keyboard(True)
            return
        if re.fullmatch(r"\d{6}", attempt):
            self.audit("bad code", f"user {uid}", f"try {tries.get(uid)}")
            if int(self._state.get("all_tries", 0)) >= MAX_ALL_TRIES:
                self.pairing_code(new=True)          # too many guesses: Settings shows a fresh code
                self.audit("new code", "", "too many wrong codes")
            if tries.get(uid, 0) < MAX_TRIES:
                self._send_quietly(message["chat"]["id"], "That code doesn't match. Check Settings ▸ Accounts & "
                                                          "keys ▸ Telegram on the Mac.")
            return
        self._stranger(sender["id"], unpaired=True)

    def _stranger(self, uid: int, unpaired: bool = False) -> None:
        """One polite answer per person, then silence."""
        with self.lock:
            told = self._state.setdefault("strangers", [])
            if uid in told:
                return
            told.append(uid)
            del told[:-200]
            self._save()
        self.audit("stranger", f"user {uid}", "told once, then ignored")
        self._send_quietly(uid, "This Mint isn't paired yet. Send the 6-digit code from Mint's Settings ▸ Accounts "
                                "& keys ▸ Telegram." if unpaired else "Sorry, this is a private bot.")

    def _command(self, text: str, message: dict, age: float) -> None:
        parts = text.split(None, 1)             # the rest keeps its own lines (/paste)
        word = parts[0].lower().split("@")[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        self.audit("command", word)             # never the rest: /paste carries the user's text
        info = {"/clipboard": "clipboard", "/trackers": "trackers", "/undo": "undo", "/chatgpt": "chatgpt",
                "/claude": "claude"}
        if word == "/start":
            self.welcome()
        elif word == "/help":
            self.help()
        elif word == "/status":
            self.show_status()
        elif word == "/pause":
            self.pause()
        elif word == "/resume":
            self.resume(stale=age > STALE)
        elif word == "/unpair":
            self.unpair()
        elif word == "/keyboard":
            self.keyboard(not self._state.get("keyboard"))
        elif word in info:
            _later(self.show_info, info[word])     # reads the Mac (AX, files): off the poller
        elif word == "/files":
            _later(self.show_files)
        elif word == "/agents":
            from mint.app import telegram_agents
            _later(telegram_agents.command, self)
        elif word == "/meet" and age > STALE:
            self.reply(f"/meet came while the Mac was asleep or offline ({_took(age)} ago), so I didn't start a call.")
        elif word == "/meet" and self.paused() and rest.lower() not in ("end", "stop", "leave", "off", "status"):
            self.reply(PAUSED_NOTE)
        elif word == "/meet":
            from mint.app import meet_call
            _later(meet_call.telegram_command, self, rest)
        elif age > STALE:
            self.reply(f"{word} came while the Mac was asleep or offline ({_took(age)} ago), so I didn't do it.")
        elif word == "/clip":
            _later(self.send_clipboard)
        elif word == "/last":
            _later(self.send_last, rest)
        elif word == "/send":
            _later(self.send_named, rest)
        elif word == "/paste":
            _later(self.paste, rest)
        elif word == "/stop":
            self.stop()
        elif word in ("/screenshot", "/briefing", "/missed") and self.paused():
            self.reply(PAUSED_NOTE)
        elif word == "/screenshot":
            _later(self.screenshot)
        elif word == "/briefing":
            self.run(BRIEF, message, "command")
        elif word == "/missed":
            self.run(MISSED, message, "command")
        else:
            self.reply("I don't know that command. /help lists them.")

    # buttons -------------------------------------------------------------------------------

    def _button(self, query: dict) -> None:
        """A press on a button under one of Mint's messages: only the paired user, answered with a toast."""
        sender, message = query.get("from") or {}, query.get("message") or {}
        chat = message.get("chat") or {}
        data = str(query.get("data") or "")
        with self.lock:
            owner, owner_chat = self._state.get("user_id"), self._state.get("chat_id")
        if not owner or sender.get("id") != owner or chat.get("id") != owner_chat or chat.get("type") != "private":
            self._answer(query, "Sorry, this is a private bot.")
            self.audit("stranger", f"user {sender.get('id')}", "button ignored")
            return
        toast, act, used = self._plan(data, message)
        if not self._answer(query, toast):
            # Pressed while the Mac was asleep or offline: Telegram no longer takes an answer, so it is too old to act on.
            self.audit("stale", f"button {data}", "not done (pressed too long ago)")
            return
        self.audit("button", data, "done" if act else toast)
        if act is None:
            return
        if used:
            self._mark_used(message, data, used)
        try:
            act()
        except Exception as error:
            log.exception("telegram button %s", data)
            self.reply(f"That didn't work: {str(error)[:160]}")

    def _plan(self, data: str, message: dict):
        """(toast, what to do or None, the pressed button's new label or '') for a button's data."""
        name, _, arg = data.partition(":")
        mid = message.get("message_id")
        paused = self.paused()
        wait = "⏸ Paused - tap ▶️ Resume first."
        refresh = (lambda: self.show_status(edit=mid)) if mid in self.status_ids else None

        def then(*steps):
            return lambda: [step() for step in steps if step is not None]

        if name == "noop":
            return "Already done.", None, ""
        if name == "gd":                        # the guard's question: yes / no
            from mint.core import guard
            return guard.telegram_plan(arg)
        if name in ("ag", "agto", "agopen"):
            from mint.app import telegram_agents
            return telegram_agents.plan(self, name, arg, message)
        if name == "mt":                        # a Google Meet call's buttons
            from mint.app import meet_call
            return meet_call.telegram_plan(self, arg)
        if name == "stop":
            return "⏹ Stopping…", then(self.stop, refresh), ""
        if name == "pause":
            if paused and self.held is None and self.request is None:
                return "Already paused. ▶️ Resume carries on.", then(refresh), ""
            toast = ("⏸ Paused: Mint stopped at its current step and keeps its plan. ▶️ Resume carries on."
                     if self._working() else "⏸ Paused: I'll hold your messages until ▶️ Resume.")
            return toast, then(lambda: self.pause(quiet=True), refresh), ""
        if name == "resume":
            return "▶️ Carrying on…", then(self.resume, refresh), "" if refresh else "✓ Resumed"
        if name == "status":
            return "🧠 Status", lambda: self.show_status(edit=mid if mid in self.status_ids else None), ""
        if name == "info":
            return "🔄", lambda: _later(self.show_info, arg, mid), ""
        if name == "help":
            return "❓ Help", self.help, ""
        if name == "kb":
            on = not self._state.get("keyboard")
            return ("⌨️ Quick buttons on" if on else "⌨️ Quick buttons hidden"), lambda: self.keyboard(on), ""
        if name in ("shot", "again", "brief", "missed", "undo!") and paused:
            return wait, None, ""
        if name == "shot":
            return "📸 Taking a screenshot…", lambda: _later(self.screenshot), ""
        if name in ("brief", "missed"):
            words = BRIEF if name == "brief" else MISSED
            return "🗓 Getting your day…" if name == "brief" else "📋 Checking what you missed…", \
                lambda: self.run(words, {}, "button"), ""
        if name == "again":
            words = self.again.get(arg)
            if not words:
                return "That button has expired - send the request again.", None, ""
            return "🔁 Running it again…", lambda: self.run(words, {}, "button"), "✓ Ran again"
        if name == "undo":
            return "↩️ What Mint can undo", lambda: _later(self.show_info, "undo"), ""
        if name == "undo!":
            if self.setting("telegram_read_only"):
                return ("🔒 Read-only: undo is off from the phone (it can delete what Mint made). Do it at the Mac, "
                        "or turn read-only off in Settings."), None, ""
            words = f"Undo the last thing you did{': ' + self.again[arg] if arg in self.again else ''}."
            return "↩️ Asking Mint to undo it…", lambda: self.run(words, {}, "button"), "✓ Asked Mint to undo"
        if name == "clip":
            return "📋 Putting it back on the Mac's clipboard…", lambda: self._put_back(arg), "✓ On the Mac"
        if name == "fa":                        # a button under a file from the phone
            fid, _, act = arg.partition(":")
            paths = self.received.get(fid)
            if not paths:
                return "That button has expired - send the file again.", None, ""
            if act == "show":
                return "📂 Showing it in Finder…", lambda: self._reveal(paths), "✓ Shown on the Mac"
            if act not in _FILE_ASKS:
                return "That button no longer works.", None, ""
            if paused:
                return wait, None, ""
            label, words = _FILE_ASKS[act]
            words = words.format(what=_what(paths))
            return f"{label}…", lambda: self.run(words, {}, "button"), f"✓ {label.split(' ', 1)[-1]}"
        if name in ("share", "file"):           # 📤 files named by a request, or one from /files or /send
            paths = self.shares.get(arg)
            if not paths:
                return "That button has expired.", None, ""
            return "📤 Sending it…", lambda: _later(self._share, paths), "✓ Sent"
        if name == "zip":
            paths = self.shares.get(arg)
            if not paths:
                return "That button has expired.", None, ""
            return "🗜 Zipping it…", lambda: _later(self._send_zipped, Path(paths[0])), "✓ Zipped"
        if name == "last":
            return "🖼 Getting it…", lambda: _later(self.send_last, arg), ""
        if name in ("send", "open"):
            item = self.alerts.get(arg)
            if not item:
                return "That button has expired.", None, ""
            if name == "send":
                return "📎 Sending it…", lambda: _later(self._send_alert_file, item), "✓ Sent"
            return "🪟 Opening it on the Mac…", lambda: self._open_on_mac(item), "✓ Opened on the Mac"
        return "That button no longer works.", None, ""

    def _answer(self, query: dict, text: str) -> bool:
        """The toast; False when Telegram says the press is too old to answer (stale: don't act on it)."""
        try:
            self._call("answerCallbackQuery", patient=False, callback_query_id=str(query.get("id", "")),
                       text=text[:190])
        except ApiError as error:
            words = str(error).lower()
            if "too old" in words or "query id is invalid" in words:
                return False
            log.info("telegram answer: %s", error)
        return True

    def _mark_used(self, message: dict, data: str, label: str) -> None:
        """The pressed button turns into a done label (inert), the others stay."""
        rows = ((message.get("reply_markup") or {}).get("inline_keyboard")) or []
        changed = [[{"text": label, "callback_data": "noop"} if b.get("callback_data") == data else b for b in row]
                   for row in rows]
        if changed == rows or not message.get("message_id"):
            return
        try:
            self._call("editMessageReplyMarkup", patient=False, chat_id=self._chat(),
                       message_id=message["message_id"], reply_markup={"inline_keyboard": changed})
        except ApiError as error:
            log.info("telegram buttons: %s", error)

    # what the commands and buttons do ------------------------------------------------------

    def welcome(self, fresh: bool = False) -> None:
        who = self.paired() or {}
        head = ("✅ <b>Paired with Mint</b>\nFrom now on, what you send here goes to Mint on your Mac."
                if fresh else f"🌿 <b>Mint is connected</b>\nPaired with {esc(who.get('name') or 'you')}"
                              f"{' since ' + esc(who['paired_at']) if who.get('paired_at') else ''}.")
        text = (f"{head}\n\n"
                "<b>Try</b>\n"
                "• <i>what's on my calendar today?</i>\n"
                "• <i>find my tax pdf and send it to me</i>\n"
                "• <i>let me know when the download finishes</i>\n"
                "• a voice note 🎙\n"
                "• a file or photo 📎 with what to do: <i>summarise this</i>\n\n"
                "<b>While Mint works</b> you see each step here, with ⏸ ⏹ 📸 buttons under it. Screenshots and "
                "files it makes come back to this chat.\n\n"
                "<b>Quick share</b>: /clip the Mac's clipboard · /last the last screenshot · /files what Mint made · "
                "/send <i>a file</i> · /paste <i>text</i> onto the Mac\n\n"
                "Tap ☰ Menu for every command, or /help."
                + ("\n\n<i>🔒 Read-only is on: nothing is sent, deleted or bought from here.</i>"
                   if self.setting("telegram_read_only") else ""))
        self.html(text, markup=keys([("🧠 Status", "status"), ("📸 Screen", "shot")],
                                    [("🗓 My day", "brief"), ("📋 What did I miss?", "missed")],
                                    [("🖼 Last screenshot", "last"), ("❓ Help", "help")]))

    def help(self) -> None:
        extra = ("\n\n<i>🔒 Read-only is on: nothing is sent, deleted or bought from here, and ↩️ Undo is off.</i>"
                 if self.setting("telegram_read_only") else "")
        on = bool(self._state.get("keyboard"))
        self.html(HELP + extra, markup=keys([("🧠 Status", "status"),
                                            ("⌨️ Hide quick buttons" if on else "⌨️ Quick buttons", "kb")]))

    def keyboard(self, on: bool) -> None:
        self._set(keyboard=bool(on))
        if on:
            self.html("⌨️ <b>Quick buttons are on</b> - they stay under the message box. /keyboard hides them.",
                      markup=KEYBOARD)
        else:
            self.html("⌨️ Quick buttons hidden. /keyboard brings them back.", markup={"remove_keyboard": True})

    def stop(self) -> None:
        host = self._host()
        request = self.request
        held, self.held = self.held, None
        if held and held.get("request") is not None:
            self._post("_end", "stopped", held["request"])     # the paused message: nothing to resume now
        if host is None or not host.ready():
            self.reply("Mint isn't running right now.")
            return
        try:
            host.stop("telegram")
        except Exception as error:
            log.info("telegram stop: %s", error)
            self.reply(f"Could not stop Mint: {str(error)[:120]}")
            return
        if request is not None:
            self._post("_end", "stopped", request)
        self.reply("⏹ Stopped." + (" (Still paused: /resume to send me requests again.)" if self.paused() else ""))

    def pause(self, quiet: bool = False) -> None:
        """Stop the step Mint is on, keep its plan (a paused task) and hold the phone's messages until resume.
        Mint can't freeze a step halfway, so this is Mint's own stop - which also holds its autopilot."""
        host = self._host()
        request = self.request
        facts = self._facts()
        working = request is not None or bool(facts.get("busy")) or bool(facts.get("task"))
        self._set(paused=True)
        if not working or self.held is not None or host is None or not host.ready():
            self.audit("pause", "", "messages held")
            if not quiet:
                self.html("⏸ <b>Paused</b>: I'll ignore your messages until /resume. (/stop and /status still work.)",
                          markup=keys([("▶️ Resume", "resume")]))
            return
        asked = request.asked if request is not None else _latest_request()
        with self.lock:
            if self.request is request:
                self.request = None
        self.held = {"request": request, "asked": asked, "kind": request.kind if request is not None else "text",
                     "task": bool((request is not None and request.plan) or facts.get("task"))}
        if request is not None:
            self._post("_end", "paused", request)
        try:
            host.stop("telegram pause")
        except Exception as error:
            log.info("telegram pause: %s", error)
            self.reply(f"Could not pause Mint: {str(error)[:120]}")
            return
        self.audit("pause", asked, "stopped at its step, plan kept, messages held")
        if not quiet:
            self.html("⏸ <b>Paused</b>\nMint stopped at the step it was on (a step can't be frozen halfway) and kept "
                      "its plan. New messages from here wait too.\n<i>▶️ Resume carries on from where it stopped.</i>",
                      markup=keys([("▶️ Resume", "resume"), ("⏹ Stop", "stop")]))

    def _facts(self) -> dict:
        host = self._host()
        try:
            return host.doing() if host is not None and host.ready() else {}
        except Exception:
            return {}

    def _working(self) -> bool:
        """A phone request is running, or Mint is busy with a tool or a task plan."""
        facts = self._facts()
        return self.request is not None or bool(facts.get("busy")) or bool(facts.get("task"))

    def resume(self, stale: bool = False) -> None:
        held = self.held
        if held and stale:
            self.reply("That /resume came while the Mac was asleep or offline, so I didn't restart the paused work. "
                       "Send /resume again to carry on with it.")
            return
        self._set(paused=False)
        self.held = None
        if not held:
            self.audit("resume", "", "listening again")
            self.reply("▶️ Listening again.")
            return
        old = held.get("request")
        asked = held.get("asked") or ""
        text = (RESUME_TASK if held.get("task") else RESUME_TEXT) + (f" What I asked: “{asked}”" if asked else "")
        self.audit("resume", asked, "carrying on")
        self.run(text, {"message_id": old.message_id if old is not None else None}, held.get("kind") or "text",
                 asked=asked or "Carry on", reuse=old)

    def show_status(self, edit: int | None = None) -> None:
        text = self.describe()
        markup = keys([("▶️ Resume", "resume") if self.paused() else ("⏸ Pause", "pause"), ("⏹ Stop", "stop"),
                       ("📸 Screen", "shot")],
                      [("🗓 My day", "brief"), ("📋 Missed", "missed"), ("🔄 Refresh", "status")])
        if edit and self._edit_message(edit, text, markup):
            return
        sent = self.html(text, markup=markup)
        if isinstance(sent, dict) and sent.get("message_id"):
            self.status_ids = (self.status_ids + [sent["message_id"]])[-20:]

    def describe(self) -> str:
        """/status: what Mint is doing, and the things that change what a remote request can do."""
        host = self._host()
        facts = host.doing() if host is not None else {"running": False}
        if not facts.get("running"):
            lines = ["🧠 <b>Mint isn't running.</b>"]
        else:
            mood = ("paused (microphone off)" if facts.get("paused") else "asleep" if facts.get("asleep")
                    else "awake")
            lines = [f"🧠 <b>Mint is {mood}</b>" + ("" if facts.get("connected") else ", reconnecting to Gemini") + "."]
            state, note = self.state_now
            request = self.request
            if request is not None:
                doing = request.current or (request.steps[-1][2:] if request.steps else "thinking")
                lines.append(f"▸ Working on <i>“{esc(_short(request.asked, 80))}”</i> for "
                             f"{_took(time.monotonic() - request.started)} · {len(request.steps)} steps")
                lines.append(f"   now: {esc(doing)}")
            elif facts.get("busy") or state in ("working", "thinking"):
                lines.append("▸ Busy: " + esc(note or state))
            else:
                lines.append("💤 Not doing anything right now.")
            if self.held:
                lines.append(f"⏸ Paused: <i>“{esc(_short(self.held.get('asked') or 'its work', 80))}”</i> - "
                             "▶️ Resume carries on.")
            if facts.get("task"):
                lines.append(f"📋 Task: {esc(facts['task'])}")
        locked = screen_locked()
        lines.append(esc(LOCKED_NOTE) if locked else "🔓 Screen: unlocked." if locked is False else "Screen: unknown.")
        lines.append("📡 Remote control: " + ("paused" if self.paused() else "on")
                     + (" · read-only" if self.setting("telegram_read_only") else "")
                     + (" · alerts on" if self.setting("telegram_notify") else ""))
        lines.append(f"<i>as of {time.strftime('%H:%M:%S')}</i>")
        return "\n".join(lines)

    def show_info(self, what: str, edit: int | None = None) -> None:
        """/clipboard, /trackers, /undo, /chatgpt, /claude: read straight from the Mac, not through Mint."""
        makers = {"clipboard": self._clipboard_card, "trackers": self._trackers_card, "undo": self._undo_card,
                  "chatgpt": lambda: self._app_card("chatgpt"), "claude": lambda: self._app_card("claude")}
        if what not in makers:
            return
        if what in ("chatgpt", "claude"):
            self._action("typing")
        try:
            text, markup = makers[what]()
        except Exception as error:
            log.exception("telegram %s", what)
            text, markup = f"Couldn't read that: {esc(str(error)[:160])}", None
        self.audit("info", what)
        if edit and self._edit_message(edit, text, markup or {"inline_keyboard": []}):
            return
        self.html(text, markup=markup)

    def _clipboard_card(self):
        from mint.tools import clipboard as clip_tools
        clip_tools._load()
        rows = list(clip_tools.HISTORY[:8])
        if not rows:
            return "📋 <b>Clipboard</b>\nNothing copied yet (hidden items and secrets are never kept).", None
        lines, buttons = ["📋 <b>Clipboard</b> · newest first · tap a text to copy it", ""], []
        for n, row in enumerate(rows, 1):
            age = _ago(time.time() - float(row.get("at") or time.time()))
            kind = row.get("kind")
            if kind == "text":
                words = str(row.get("text") or "")
                if _secret(words):
                    lines.append(f"{n}. 🔒 <i>hidden (looks like a secret)</i>")
                    continue
                shown = words.strip() if len(words) <= 300 else _short(words, 300)
                lines.append(f"{n}. <code>{esc(shown)}</code> · <i>{age}</i>")
            elif kind == "image":
                lines.append(f"{n}. 🖼 {esc(row.get('label') or 'a picture')} · <i>{age}</i>")
            else:
                lines.append(f"{n}. 📁 {esc(row.get('label') or 'files')} · <i>{age}</i>")
            if len(buttons) < 5 and row.get("id"):
                buttons.append((f"📋 {n}", f"clip:{row['id']}"))
        lines.append("\n<i>📋 n puts that one back on the Mac's clipboard.</i>")
        return "\n".join(lines), keys(buttons, [("🔄 Refresh", "info:clipboard")])

    def _put_back(self, clip_id: str) -> None:
        from mint.tools import clipboard as clip_tools
        clip_tools._load()
        index = next((i for i, row in enumerate(clip_tools.HISTORY, 1) if row.get("id") == clip_id), 0)
        if not index:
            self.reply("That copy isn't in the clipboard history any more.")
            return
        result = str(clip_tools.clipboard({"action": "restore", "index": index}))
        self.audit("clipboard", f"put back #{index}", result[:80])
        if result.startswith(("FAILED", "Could not", "NOT")):
            self.reply(result[:300])

    def _trackers_card(self):
        from mint.tools import trackers
        live = trackers.active()
        if not live:
            return ("👁 <b>Trackers</b>\nNot watching anything right now. Ask me: <i>let me know when the download "
                    "finishes</i>.", None)
        lines = [f"👁 <b>Watching</b> · {len(live)}", ""]
        for t in live[:15]:
            took = int(time.time() - float(t.get("created") or time.time()))
            extra = " · ".join(str(x) for x in (t.get("progress_text"), t.get("detail")) if x)
            lines.append(f"• <b>{esc(t.get('label', ''))}</b> · {esc(t.get('kind', ''))} · {took // 60} min"
                         + (f" · {esc(extra)}" if extra else ""))
        lines.append("\n<i>I'll tell you here when each one finishes.</i>")
        return "\n".join(lines), keys([("🔄 Refresh", "info:trackers")])

    def _undo_card(self):
        from mint.tools import undo
        listing = undo.listing()
        rows = listing.splitlines()
        if not rows or not rows[0].startswith("What Mint can undo"):
            return f"↩️ {esc(listing)}", None
        lines = ["↩️ <b>What Mint can undo</b> · newest first", ""]
        for row in rows[1:]:
            lines.append(esc(row))
        newest = re.match(r"1\. (.*) - [^-]*$", rows[1]) if len(rows) > 1 else None
        markup = None
        if self.setting("telegram_read_only"):
            lines.append("\n<i>🔒 Read-only is on: undo from the phone is off.</i>")
        elif newest and "(can't be undone" not in rows[1]:
            summary = newest.group(1).strip()
            rid = self._keep(self.again, summary)
            markup = keys([(f"↩️ Undo: {_short(summary, 34)}", f"undo!:{rid}")])
        return "\n".join(lines), markup

    def _app_card(self, key: str):
        from mint.tools import agentapps
        result = str(agentapps.status(key))
        name = "ChatGPT" if key == "chatgpt" else "Claude Code"
        if key == "claude" and "Chat view" in result:
            # Only Claude Code is Mint's business: never report on (or pass on) Claude's Chat view.
            result = ("Claude is showing its Chat view; I only report on Claude Code. Switch it to Code at the Mac."
                      + (" Something is running in Claude Code." if "running in the Code view" in result else ""))
        icon = "🤖" if key == "chatgpt" else "✳️"
        lines = [f"{icon} <b>{name}</b>"] + [esc(part) for part in re.split(r"(?<=[.!])\s+(?=[A-Z])", result)
                                             if part.strip()]
        return "\n".join(lines), keys([("🔄 Refresh", f"info:{key}")])

    def screenshot(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="mint-tg-"))
        path = folder / "screen.jpg"
        try:
            self._action("upload_photo")
            done = subprocess.run(["screencapture", "-x", "-m", "-t", "jpg", str(path)], capture_output=True,
                                  text=True, timeout=20)
            if done.returncode != 0 or not path.exists() or path.stat().st_size == 0:
                self.reply("Could not take a screenshot" + (f": {done.stderr.strip()[:120]}" if done.stderr else "")
                           + ". Mint may need Screen Recording permission.")
                self.audit("screenshot", "", "failed")
                return
            app = _frontmost_app()
            caption = (f"📸 <b>Your screen</b> · {time.strftime('%H:%M')}" + (f" · {esc(app)} in front" if app else "")
                       + ("\n" + esc(LOCKED_NOTE) if screen_locked() else ""))
            self.send_file(path, photo=True, caption=caption,
                           markup=keys([("🔄 Again", "shot"), ("🧠 Status", "status")]))
            self.audit("screenshot", "", "sent")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _voice(self, message: dict, media: dict) -> None:
        if int(media.get("file_size") or 0) > MAX_FILE:
            self.reply("That voice note is too long for me - keep it under a few minutes.")
            return
        try:
            self._action("typing")
            bot = self.bot
            info = bot.call("getFile", file_id=media["file_id"])
            audio = bot.download(info["file_path"])
            text = transcribe(audio, Path(str(info.get("file_path", "voice.oga"))).suffix or ".oga")
        except Exception as error:
            log.info("telegram voice: %s", error)
            self.reply(f"Could not hear that voice note: {str(error)[:160]}")
            self.audit("voice", "", f"failed: {str(error)[:120]}")
            return
        if not text:
            self.reply("🎙 I couldn't hear any words in that.")
            self.audit("voice", "", "no words")
            return
        self.html(f"🎙 <i>{esc(text)}</i>")
        self.run(text, message, "voice")

    def _alert_buttons(self, title: str) -> dict:
        """Under a tracker's alert: send the file here, or open it (or its app) on the Mac."""
        item: dict = {}
        try:
            from mint.tools import trackers
            for _ in range(4):                  # the tracker files its result just after it announces
                found = trackers.recent(60)
                if found and str(found.get("label", ""))[:60] and str(found.get("label", ""))[:60] in title:
                    item = found
                    break
                time.sleep(0.1)
        except Exception:
            item = {}
        row = []
        if item:
            aid = self._keep(self.alerts, item)
            target = Path(str(item["open"])).expanduser() if item.get("open") else None
            if target is not None and target.is_file() and self._may_send(target, None):
                row.append(("📤 Share", f"send:{aid}"))
            if target is not None and target.exists():
                row.append(("📂 Open on Mac", f"open:{aid}"))
            elif item.get("app") or item.get("kind") in ("claude", "claude_app", "chatgpt"):
                row.append(("🪟 Show on Mac", f"open:{aid}"))
        return keys(row + [("📸 Screen", "shot")])

    def _send_alert_file(self, item: dict) -> None:
        path = Path(str(item.get("open") or "")).expanduser()
        if not (path.is_file() and self._may_send(path, None)):
            self.reply("I can't send that one (it's gone, too big, or not a file I may send).")
            return
        self.audit("send file", _home(path))
        self.send_file(path, caption=_file_caption(path))

    def _open_on_mac(self, item: dict) -> None:
        target = str(item.get("open") or "")
        app = {"claude": "Claude", "claude_app": "Claude", "chatgpt": "ChatGPT"}.get(str(item.get("kind")), "") \
            or str(item.get("app") or "")
        command = ["open", os.path.expanduser(target)] if target else ["open", "-a", app] if app else None
        self.audit("open", target or app)
        if command:
            subprocess.run(command, capture_output=True, timeout=15)

    # files from the phone -------------------------------------------------------------------

    def _receive(self, message: dict, attached: dict, caption: str, age: float) -> None:
        """A file came (on the poller): fetch it off the poller; an album's items are gathered first."""
        item = {"file": attached, "caption": caption, "age": age, "message_id": message.get("message_id")}
        group = str(message.get("media_group_id") or "")
        if not group:
            _later(self._take, [item])
            return
        with self.lock:
            album = self.albums.setdefault(group, {"items": []})
            album["items"].append(item)
            if album.get("timer") is not None:
                album["timer"].cancel()
            timer = threading.Timer(ALBUM_WAIT, self._album_done, args=(group,))
            timer.daemon = True
            album["timer"] = timer
        timer.start()

    def _album_done(self, group: str) -> None:
        with self.lock:
            album = self.albums.pop(group, None)
        if album and album["items"]:
            self._take(album["items"])

    def _take(self, items: list[dict]) -> None:
        """Save the files; then their caption runs as one request naming them, or they get buttons."""
        self._action("typing")
        saved: list[Path] = []
        for item in items:
            path, why = self._download(item["file"])
            if path is not None:
                saved.append(path)
            elif why == "big":
                f = item["file"]
                self.html(f"📎 <b>{esc(f['name'])}</b>" + (f" is {_size(f['size'])}" if f.get("size") else " is too big")
                          + ": Telegram bots can't fetch files over 20 MB.\nUse AirDrop or iCloud Drive to get it onto "
                            "the Mac.")
            else:
                self.reply(f"📎 Could not get {item['file']['name']}: {why[:160]}")
        if not saved:
            return
        caption = next((i["caption"] for i in items if i["caption"]), "")
        age = max(i["age"] for i in items)
        note = ""
        if caption and age > STALE:
            note = (f"<i>This came while the Mac was asleep or offline ({_took(age)} ago), so I didn't run "
                    f"“{esc(_short(caption, 120))}”. Tap a button, or send the request again.</i>")
            self.audit("stale", caption, f"file kept, request not run ({int(age)} s old)")
        elif caption and self.paused():
            note = f"<i>{esc(PAUSED_NOTE)} (Your file is kept.)</i>"
            self.audit("paused", caption, "file kept, request not run")
        elif caption:
            self.html(_saved_line(saved))
            self.run(_file_prompt(saved, caption), {"message_id": items[0]["message_id"]}, "file", asked=caption)
            return
        self._saved_card(saved, note)

    def _download(self, attached: dict) -> tuple[Path | None, str]:
        """(the saved file, '') or (None, 'big' | why). Telegram hands bots files up to 20 MB."""
        name, size = attached["name"], int(attached.get("size") or 0)
        if size > MAX_FILE:
            self.audit("file in", name, f"refused: {_size(size)}, over 20 MB")
            return None, "big"
        try:
            info = self._call("getFile", file_id=attached["id"]) or {}
            if int(info.get("file_size") or 0) > MAX_FILE:
                raise ApiError("file is too big")
            bot = self.bot
            if bot is None:
                raise ApiError("not connected")
            data = bot.download(str(info.get("file_path") or ""))
            if len(data) > MAX_FILE:
                raise ApiError("file is too big")
            path = _save_new(self._inbox_dir(), _safe_name(name, attached.get("mime", "")), data)
        except (ApiError, OSError) as error:
            if "too big" in str(error).lower():
                self.audit("file in", name, "refused: over 20 MB")
                return None, "big"
            log.info("telegram file: %s", error)
            self.audit("file in", name, f"failed: {str(error)[:120]}")
            return None, str(error)
        self.audit("file in", path.name, f"saved {_size(len(data))} in {_home(path.parent)}")
        return path, ""

    def _inbox_dir(self) -> Path:
        if self.inbox is not None:
            self.inbox.mkdir(parents=True, exist_ok=True)
            return self.inbox
        from mint.core import config
        return config.storage(INBOX)

    def _saved_card(self, paths: list[Path], note: str = "") -> None:
        """Saved without a caption: where it went, and buttons that fit it."""
        kind = _file_kind(paths)
        fid = self._keep(self.received, [str(p) for p in paths])
        many = len(paths) > 1
        lines = [f"📥 <b>Saved {len(paths)} files from your phone</b>" if many else "📥 <b>Saved from your phone</b>"]
        for path in paths[:10]:
            lines.append(f"{_icon(path)} {esc(path.name)} · {_size(_bytes(path))}")
        if len(paths) > 10:
            lines.append(f"<i>… and {len(paths) - 10} more</i>")
        lines += [f"<i>{esc(_home(paths[0].parent))}</i>", "",
                  note or ("What should I do with them?" if many else "What should I do with it?")]
        acts = _FILE_BUTTONS[kind]
        if kind == "many" and all(_file_kind([p]) == "image" for p in paths):
            acts = ("ocr", "sheet")
        buttons = [(_FILE_ASKS[a][0], f"fa:{fid}:{a}") for a in acts] + [("📂 Show on Mac", f"fa:{fid}:show")]
        self.html("\n".join(lines), markup=keys(*[buttons[i:i + 2] for i in range(0, len(buttons), 2)]))

    def _reveal(self, paths: list[str]) -> None:
        there = [p for p in paths if Path(p).exists()][:10]
        if not there:
            self.reply("That file isn't there any more.")
            return
        self.audit("open", "; ".join(_home(Path(p)) for p in there), "shown in Finder")
        subprocess.run(["open", "-R", *there], capture_output=True, timeout=15)

    # quick share: Mac -> phone ----------------------------------------------------------------

    def _share(self, paths: list[str]) -> None:
        for raw in paths[:MAX_FILES]:
            path = Path(raw)
            checked, why = self._check_send(path)
            if checked is None or not checked.is_file() or checked.stat().st_size > MAX_UPLOAD:
                self.reply(f"I can't send {path.name}: {why or 'it is gone, a folder, or over 50 MB'}.")
                self.audit("send file", _home(path), f"refused: {why or 'gone or too big'}")
                continue
            self.audit("send file", _home(checked))
            self.send_file(checked, caption=_file_caption(checked))

    def show_files(self) -> None:
        """/files: what Mint made lately (made_files.json), newest first, each with a 📤 button."""
        try:
            from mint.tools import harness as harness_tools
            made = [p for p in harness_tools._made() if p.is_file() and self._may_send(p, None, size=False)][:8]
        except Exception:
            made = []
        if not made:
            self.html("🗂 <b>Files Mint made</b>\nNothing yet. Ask me for a document, a spreadsheet or a picture, "
                      "or /send <i>a file</i> from the Mac.")
            self.audit("files", "", "none")
            return
        lines, buttons = ["🗂 <b>Files Mint made</b> · newest first · tap 📤 n to get one here", ""], []
        for n, path in enumerate(made, 1):
            size = _bytes(path)
            ago = _ago(time.time() - path.stat().st_mtime)
            lines.append(f"{n}. <b>{esc(path.name)}</b> · {_size(size)} · <i>{ago}</i>\n"
                         f"      <i>{esc(_home(path.parent))}</i>")
            if size <= MAX_UPLOAD:
                buttons.append((f"📤 {n}", f"file:{self._keep(self.shares, [str(path)])}"))
        self.audit("files", f"{len(made)} listed")
        self.html("\n".join(lines), markup=keys(*[buttons[i:i + 4] for i in range(0, len(buttons), 4)]))

    def send_clipboard(self) -> None:
        """/clip: what is on the Mac's clipboard now. Never a concealed item (password managers) or a secret."""
        self._action("typing")
        try:
            now = self._clipboard_now()
        except Exception as error:
            log.info("telegram clip: %s", error)
            self.reply(f"Couldn't read the Mac's clipboard: {str(error)[:120]}")
            return
        kind = now["kind"]
        history = keys([("📋 History", "info:clipboard")])
        if kind == "secret":
            self.html("🔒 What's on the Mac's clipboard is a password or another hidden item, so I won't send it.")
            self.audit("clip", "hidden or secret item", "refused")
        elif kind == "empty":
            self.reply("📋 The Mac's clipboard is empty.")
            self.audit("clip", "empty")
        elif kind == "text":
            words = now["text"]
            tag = "pre" if "\n" in words.strip() else "code"
            body = (f"📋 <b>Mac clipboard</b> · {len(words)} characters · tap to copy\n"
                    f"<{tag}>{esc(words.strip())}</{tag}>")
            if len(body) <= MAX_TEXT:
                self.html(body, markup=history)
            else:
                self._send_bytes(words.encode(), "Clipboard.txt", caption=f"📋 <b>Mac clipboard</b> · {len(words)} "
                                 "characters (too long for a message)", markup=history)
            self.audit("clip", f"text, {len(words)} characters", "sent")
        elif kind == "image":
            png = now["png"]
            self._send_bytes(png, "Clipboard.png", photo=True, caption="📋 <b>Mac clipboard</b> · a picture",
                             markup=history)
            self.audit("clip", f"picture, {_size(len(png))}", "sent")
        elif kind == "files":
            sent, refused = 0, []
            for raw in now["files"][:MAX_FILES]:
                path, why = self._check_send(Path(raw))
                if path is None or not path.is_file() or path.stat().st_size > MAX_UPLOAD:
                    refused.append(f"{Path(raw).name} ({why or ('a folder' if Path(raw).is_dir() else 'over 50 MB')})")
                    continue
                self.send_file(path, caption=_file_caption(path))
                sent += 1
            if refused:
                self.reply("📋 Not sent: " + "; ".join(refused))
            if len(now["files"]) > MAX_FILES:
                self.reply(f"📋 The first {MAX_FILES} of {len(now['files'])} copied files.")
            self.audit("clip", f"{len(now['files'])} file(s)", f"{sent} sent, {len(refused)} refused")
        else:
            self.reply("📋 The clipboard holds something I can't send here.")
            self.audit("clip", "other", "not sent")

    def _clipboard_now(self) -> dict:
        """{kind: empty | secret | files | text | image | other, ...}, read as the clipboard history reads it."""
        from mint.tools import clipboard as clip_tools
        from mint.tools.everyday import BOARD_LOCK
        board = (self.pasteboard or clip_tools._board)()
        locked = BOARD_LOCK.acquire(timeout=3)
        try:
            kinds = set(clip_tools._types(board))
            if not kinds:
                return {"kind": "empty"}
            if kinds & clip_tools._CONCEALED:
                return {"kind": "secret"}
            files = clip_tools._files(board)
            if files:
                return {"kind": "files", "files": files}
            text = board.stringForType_("public.utf8-plain-text")
            if text is not None and str(text).strip():
                return {"kind": "secret"} if _secret(str(text)) else {"kind": "text", "text": str(text)}
            png = board.dataForType_("public.png")
            if png is None and board.dataForType_("public.tiff") is not None:
                image = clip_tools._image(board)
                rep = clip_tools._bitmap(image) if image is not None else None
                png = rep.representationUsingType_properties_(4, None) if rep is not None else None   # 4 = PNG
            if png is not None:
                return {"kind": "image", "png": bytes(png)}
            return {"kind": "other"}
        finally:
            if locked:
                BOARD_LOCK.release()

    def send_last(self, arg: str = "") -> None:
        """/last (or /last 3): the newest screenshots, from the clipboard history (numbered there), else the
        screenshot folder."""
        count = max(1, min(int(arg), 5)) if str(arg).strip().isdigit() else 1
        found: list[tuple[Path, str, float]] = []
        try:
            from mint.tools import clipboard as clip_tools
            clip_tools._load()
            for h in clip_tools.HISTORY:
                if h.get("source") != "screenshot" or h.get("kind") != "image":
                    continue
                file = Path(str(h["file"])) if h.get("file") else None
                picture = Path(str(h.get("image") or ""))
                if file is not None and file.is_file() and self._may_send(file, None):
                    found.append((file, str(h.get("label") or ""), float(h.get("at") or 0)))
                elif h.get("image") and picture.is_file():       # Mint's own copy, in its clipboard store
                    found.append((picture, str(h.get("label") or ""), float(h.get("at") or 0)))
                if len(found) >= count:
                    break
            if not found:
                folder = clip_tools._save_folder()
                shots = sorted((p for p in folder.glob("Screen*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                found = [(p, p.stem, p.stat().st_mtime) for p in shots[:count] if self._may_send(p, None)]
        except Exception as error:
            log.info("telegram last: %s", error)
        if not found:
            self.reply("🖼 No screenshots yet. Take one with ⇧⌘3 or ⇧⌘4, or send /screenshot for the screen now.")
            self.audit("last", "", "none")
            return
        self._action("upload_photo")
        for n, (path, label, at) in enumerate(reversed(found)):     # oldest first: the newest ends at the bottom
            title = (label.split(" · ")[0] or "Screenshot").capitalize()
            where = f"\n<i>{esc(_home(path))}</i>" if Path.home() in path.parents and "/Library/" not in str(path) \
                else ""
            markup = keys([("📸 Screen now", "shot"), ("📋 Clipboard", "info:clipboard")]) \
                if n == len(found) - 1 else None
            self.send_file(path, photo=True, caption=f"🖼 <b>{esc(title)}</b> · {_ago(time.time() - at)}{where}",
                           markup=markup)
        self.audit("last", f"{len(found)} screenshot(s)", "sent")

    def send_named(self, arg: str) -> None:
        """/send <name or path> [as zip]: one file under home (not hidden, Library or secret), up to 50 MB."""
        arg = arg.strip().strip("\"'“”")
        if not arg:
            self.html("📤 <b>/send</b> <i>name or path</i> sends a file from the Mac, e.g. <i>/send tax 2025.pdf</i> "
                      "or <i>/send ~/Desktop/report.docx</i>. For a folder add <i>as zip</i>.")
            return
        zipped = re.search(r"\s+(?:as (?:a )?zip|zipped|zip)$", arg, re.I)
        if zipped:
            arg = arg[:zipped.start()].strip()
        if arg.startswith(("~", "/")):
            found = [Path(os.path.expanduser(arg))]
        else:
            self._action("typing")
            try:
                found = _find_named(arg)
            except Exception as error:
                log.info("telegram find: %s", error)
                found = []
            exact = [p for p in found if p.name.lower() == arg.lower()]
            found = exact[:1] if len(exact) == 1 else found
        if not found:
            self.html(f"📤 I couldn't find “{esc(arg)}” on the Mac. Try part of the name, or a path like "
                      "<i>~/Desktop/report.pdf</i>.")
            self.audit("send", arg, "not found")
            return
        if len(found) == 1:
            self._send_checked(found[0], bool(zipped))
            return
        lines, buttons = [f"📤 <b>{len(found)} found</b> for “{esc(arg)}” · tap one to get it", ""], []
        for n, path in enumerate(found[:8], 1):
            lines.append(f"{n}. <b>{esc(path.name)}</b>{'/' if path.is_dir() else ''} · <i>{esc(_home(path.parent))}</i>")
            buttons.append((f"{'🗜' if path.is_dir() else '📤'} {n}",
                            f"{'zip' if path.is_dir() else 'file'}:{self._keep(self.shares, [str(path)])}"))
        self.audit("send", arg, f"{len(found)} found, asked which")
        self.html("\n".join(lines), markup=keys(*[buttons[i:i + 4] for i in range(0, len(buttons), 4)]))

    def _send_checked(self, raw: Path, zipped: bool = False) -> None:
        path, why = self._check_send(raw)
        if path is None:
            self.html(f"🔒 I can't send <b>{esc(raw.name or str(raw))}</b>: {esc(why)}.")
            self.audit("send", _home(raw), f"refused: {why}")
            return
        if path.is_dir():
            if zipped:
                self._send_zipped(path)
                return
            sid = self._keep(self.shares, [str(path)])
            self.html(f"📁 <b>{esc(path.name)}</b> is a folder. I send a folder only as a zip: tap 🗜, or send "
                      f"<i>/send … as zip</i>.", markup=keys([("🗜 Zip and send", f"zip:{sid}")]))
            self.audit("send", _home(path), "a folder: offered a zip")
            return
        size = _bytes(path)
        if size > MAX_UPLOAD:
            self.html(f"📎 <b>{esc(path.name)}</b> is {_size(size)}: Telegram bots can send files up to 50 MB. Use "
                      "AirDrop or iCloud Drive for this one.")
            self.audit("send", _home(path), f"refused: {_size(size)}, over 50 MB")
            return
        self.audit("send file", _home(path))
        self.send_file(path, caption=_file_caption(path))

    def _check_send(self, raw: Path) -> tuple[Path | None, str]:
        """(the real path, '') if it may go to the phone, else (None, why): only under home, and never a
        secret, Library or hidden file (the same check as Mint's own writes, plus hidden names)."""
        try:
            real = Path(os.path.expanduser(str(raw))).resolve()
        except (OSError, RuntimeError):
            return None, "that path can't be read"
        home = Path.home()
        if home not in real.parents:
            return None, "it's outside your home folder" if real != home else "that's your whole home folder"
        try:
            from mint.tools.harness import _blocked
            why = _blocked(real, write=True)
        except Exception:
            why = "it could not be checked"
        if why:
            if "outside" in why:
                return None, "it's outside your home folder"
            if "system or hidden" in why:
                return None, "it's in Library or a hidden folder"
            return None, "it holds keys, passwords or app data"
        if any(part.startswith(".") for part in real.relative_to(home).parts):
            return None, "it's a hidden file"
        if not real.exists():
            return None, "there's no such file"
        return real, ""

    def _send_zipped(self, folder: Path) -> None:
        """A folder, zipped (only when asked), without hidden, secret or linked files; up to 50 MB."""
        path, why = self._check_send(folder)
        if path is None or not path.is_dir():
            self.reply(f"I can't zip {folder.name}: {why or 'it is not a folder'}.")
            return
        from mint.tools.harness import _blocked
        self._action("upload_document")
        work = Path(tempfile.mkdtemp(prefix="mint-tg-zip-"))
        target = work / f"{_safe_name(path.name)}.zip"
        left_out, total = 0, 0
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
                for root, dirs, files in os.walk(path):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and not os.path.islink(os.path.join(root, d))]
                    for name in sorted(files):
                        full = Path(root) / name
                        if name.startswith(".") or full.is_symlink() or _blocked(full, write=True):
                            left_out += 1
                            continue
                        total += _bytes(full)
                        if total > 4 * MAX_UPLOAD:
                            break
                        out.write(full, str(full.relative_to(path.parent)))
            size = _bytes(target)
            if total > 4 * MAX_UPLOAD or size > MAX_UPLOAD:
                self.html(f"🗜 <b>{esc(path.name)}</b> is too big to send zipped (bots can send up to 50 MB). Use "
                          "AirDrop or iCloud Drive.")
                self.audit("send zip", _home(path), "refused: over 50 MB")
                return
            self.audit("send zip", _home(path), f"{_size(size)}, {left_out} hidden or private files left out")
            self.send_file(target, caption=f"🗜 <b>{esc(target.name)}</b> · {_size(size)}\n<i>{esc(_home(path))}</i>"
                           + (f"\n<i>{left_out} hidden or private files left out.</i>" if left_out else ""))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def paste(self, text: str) -> None:
        """/paste <text>: onto the Mac's clipboard, in the clipboard history as from the phone."""
        if not text:
            self.html("📋 <b>/paste</b> <i>text</i> puts that text on the Mac's clipboard, ready to paste.")
            return
        try:
            from mint.tools import clipboard as clip_tools
            from mint.tools.everyday import BOARD_LOCK
            clip_tools._load()
            with BOARD_LOCK:
                clip_tools._pending_label.update(source="phone")
                try:
                    clip_tools._put_text(text)
                    # The history's watcher would see this change as a new copy of "you": it is the phone's.
                    clip_tools._own_counts.add(int(clip_tools._board().changeCount()))
                finally:
                    if clip_tools._pending_label.get("source") == "phone":
                        clip_tools._pending_label.pop("source", None)
        except Exception as error:
            log.info("telegram paste: %s", error)
            self.reply(f"Couldn't put that on the Mac's clipboard: {str(error)[:120]}")
            self.audit("paste", f"{len(text)} characters", "failed")
            return
        self.audit("paste", f"{len(text)} characters", "on the Mac's clipboard")      # never the words
        self.html(f"📋 On the Mac's clipboard: {len(text)} characters, ready to paste.",
                  markup=keys([("📋 History", "info:clipboard")]))

    # running a request ---------------------------------------------------------------------

    def _host(self):
        if self.host is None:
            self.host = SessionHost()
        return self.host

    def run(self, text: str, message: dict, kind: str, asked: str = "", reuse: Request | None = None) -> None:
        """Into the running session as if typed; the mirror follows it until [done]."""
        host = self._host()
        if not host.ready():
            self.reply("Mint isn't running right now, so I couldn't do that.")
            self.audit(kind, text, "Mint not running")
            return
        request = Request(text, kind, message.get("message_id"), locked=bool(screen_locked()), asked=asked)
        request.rid = self._keep(self.again, request.asked)
        if reuse is not None and reuse.status_id:
            # Resumed: the paused message carries on, with the steps done so far.
            request.status_id, request.steps = reuse.status_id, list(reuse.steps)
            request.plan, request.plan_goal = [list(row) for row in reuse.plan], reuse.plan_goal
            request.app, request.sent = reuse.app, set(reuse.sent)
        with self.lock:
            old, self.request = self.request, request
        if old is not None:
            self._post("_end", "next", old)
        self._post("_begin", None, request)
        self.audit(kind, text, "sent to Mint" + (" (screen locked)" if request.locked else ""))
        try:
            host.inject(text, asked=request.asked or text)
        except Exception as error:
            log.info("telegram inject: %s", error)
            self._post("_end", "failed", request)
            self.audit(kind, text, f"failed: {str(error)[:120]}")

    def gate(self, name: str, args: dict) -> str:
        """'' to go ahead; a refusal for a send, delete or purchase while a read-only phone request runs."""
        if self.request is None or not self.setting("telegram_read_only"):
            return ""
        why = _read_only_block(name, args or {})
        if not why:
            return ""
        self.audit("blocked", f"{name}: {why}", "read-only")
        return (f"NOT DONE (read-only from Telegram): {why}. This request came from the user's phone and remote "
                "control is read-only, so nothing is sent, deleted or bought. Tell the user in one sentence; they "
                "can do it at the Mac, or turn read-only off in Settings ▸ Accounts & keys ▸ Telegram.")

    # events from the session -----------------------------------------------------------------

    def event(self, kind: str, data: dict) -> None:
        """Any thread; cheap. Mirrored on the mirror thread, in order."""
        if kind == "state":
            self.state_now = (str(data.get("name", "")), str(data.get("note", "")))
            return
        if self.bot is None:
            return                              # not connected: nothing to mirror to
        self.events.put((kind, data, self.request))

    def _post(self, kind: str, data, request: Request | None) -> None:
        self.events.put((kind, data, request))

    def _mirror_loop(self) -> None:
        while not self.quit.is_set():
            try:
                kind, data, request = self.events.get(timeout=0.25)
            except queue.Empty:
                kind = None
            if kind is not None:
                try:
                    self._apply(kind, data, request)
                except Exception:
                    log.exception("telegram mirror: %s", kind)
            try:
                self._tick()
            except Exception:
                log.exception("telegram mirror tick")

    def _apply(self, kind: str, data, request: Request | None) -> None:
        now = time.monotonic()
        if kind == "announce":
            self.announced_at = now
            if self.paired() and self.setting("telegram_notify"):
                title, words = str(data.get("title", "")).strip(), str(data.get("text", "")).strip()
                self.html(f"🔔 <b>{esc(title)}</b>" + (f"\n{esc(words)}" if words else ""),
                          markup=self._alert_buttons(title))
            return
        if kind == "notice":
            # Mint was told something to pass on (an agent finished, an automation ran): its next
            # words are that news. A tracker has just announced itself: that was the news.
            if now - self.announced_at > 10:
                self.notice_until = now + 120
            return
        if kind == "_begin":
            request.scenes = {id(scene) for scene in _island_cards()}
            if request.status_id:
                self._edit(request, force=True)
            else:
                request.status_id = self._send_status(request)
                request.shown = request.render() + json.dumps(request.buttons())
                request.frame += 1
                request.last_edit = now
            self._action("typing")
            self.typing_at = now
            return
        if kind == "_end":
            self.finish(request, data)
            return
        if request is None or request.finished:
            if kind == "reply" and self.notice_until > now and self.setting("telegram_notify"):
                self.notice_until = 0.0
                if self.paired():
                    self.html(f"🔔 {md_html(str(data.get('text', '')))}",
                              markup=keys([("🧠 Status", "status"), ("📸 Screen", "shot")]))
            return
        request.last_event = now
        if kind == "request":
            text = " ".join(str(data.get("text", "")).split())
            if not request.confirmed and text == " ".join(request.text.split()):
                request.confirmed = True
            else:
                self.finish(request, "local")        # typed at the Mac meanwhile: that is its own request
        elif kind == "tool_start":
            name, args = str(data.get("name", "")), dict(data.get("args") or {})
            request.start_tool(name, args)
            if name == "show_card":
                self._send_card("info", args, request)
        elif kind == "tool_end":
            name, args, result = str(data.get("name", "")), dict(data.get("args") or {}), str(data.get("result", ""))
            request.end_tool(name, args, result)
            request.scan_until = now + 3.0       # a card it put on the island shows up a moment later
            if name == "screenshot":
                for path in _paths(result):
                    if path.suffix.lower() in (".png", ".jpg", ".jpeg") and self._may_send(path, request):
                        request.sent.add(str(path))
                        self.send_file(path, photo=True, caption=f"📸 <b>Screenshot</b> · {esc(path.name)}")
        elif kind == "reply":
            text = str(data.get("text", "")).strip()
            if text:
                request.last_reply = now
                self.say(text)
        elif kind == "done":
            self.finish(request, "done")
        elif kind == "stop":
            self.finish(request, "stopped")

    def _tick(self) -> None:
        request = self.request
        if request is None or request.finished:
            return
        now = time.monotonic()
        if request.scan_until > now:
            self._scan_cards(request)
        if request.last_reply and now - max(request.last_reply, request.last_event) > QUIET_END:
            self.finish(request, "quiet")
        elif now - request.last_event > SILENT_END or now - request.started > LONGEST:
            self.finish(request, "quiet")
        else:
            if now - self.typing_at >= TYPING_EVERY:
                self.typing_at = now
                self._action("typing")
            if now - request.last_edit >= (self.edit_every if request.dirty else max(self.edit_every, IDLE_EDIT)):
                self._edit(request)

    def _edit(self, request: Request, force: bool = False) -> None:
        bot = self.bot                          # switched off meanwhile: None
        if request.status_id is None or bot is None:
            return
        text, markup = request.render(), request.buttons()
        shown = text + json.dumps(markup)
        if shown == request.shown:
            return
        if force:
            wait = self.edit_every - (time.monotonic() - request.last_edit)
            if wait > 0:
                time.sleep(wait)
        elif time.monotonic() - request.last_edit < self.edit_every:
            return
        request.last_edit = time.monotonic()
        try:
            self._call("editMessageText", patient=False, chat_id=self._chat(), message_id=request.status_id,
                       text=text, parse_mode="HTML", reply_markup=markup)
            request.shown = shown
            request.frame += 1
        except ApiError as error:
            if "not modified" in str(error):
                request.shown = shown
            elif error.retry_after:
                request.last_edit = time.monotonic() + error.retry_after
            else:
                log.info("telegram edit: %s", error)
        request.dirty = False

    def finish(self, request: Request | None, how: str) -> None:
        # A paused request can still end (stopped for good); anything else ends once.
        if request is None or (request.finished and not (request.finished == "paused" and how == "stopped")):
            return
        with self.lock:
            if self.request is request:
                self.request = None
        request.finished = how
        self._scan_cards(request)
        made = self._made_files(request) if how in ("done", "quiet") else []
        request.sent.update(str(path) for path in made)
        if how in ("done", "quiet"):
            named = self._named_files(request)
            if named:
                request.share, request.share_n = self._keep(self.shares, [str(p) for p in named]), len(named)
        self._edit(request, force=True)
        for path in made:
            self.send_file(path, caption=_file_caption(path))

    def _scan_cards(self, request: Request) -> None:
        """Cards a tool put on the island during this request (the schedule, notifications, clips...) go to
        the phone too, as pictures."""
        for scene in _island_cards():
            if id(scene) in request.scenes:
                continue
            request.scenes.add(id(scene))
            kind = {"schedule": "schedule", "info": "info"}.get(getattr(scene, "key", ""))
            data = getattr(scene, "data", None)
            if kind and isinstance(data, dict):
                self._send_card(kind, data, request)

    def _send_card(self, kind: str, data: dict, request: Request | None = None) -> None:
        data = _card_data(data) if kind == "info" else data
        key = _card_key(kind, data)
        if request is not None:
            if key in request.cards:
                return
            request.cards.add(key)
        text = _card_html(data) if kind == "info" else _schedule_html(data)
        png = None
        try:
            png = self.draw(kind, data) if self.draw else None
        except Exception:
            log.info("telegram card picture", exc_info=True)
        if png:
            self._action("upload_photo")
            fits = len(text) <= MAX_CAPTION
            if self._send_bytes(png, "card.png", photo=True, caption=text if fits else ""):
                if not fits:
                    self.html(text)
                return
        self.html(text)

    def _made_files(self, request: Request) -> list[Path]:
        """Files Mint wrote during this request: its made-files list, and paths named in tool results."""
        found: list[Path] = []
        try:
            from mint.tools.harness import MADE
            found += [Path(p) for p in json.loads(MADE.read_text())]
        except Exception:
            pass
        for result in request.results:
            found += _paths(result)
        chosen, seen, too_big = [], set(), []
        for path in found:
            key = str(path)
            if key in seen or key in request.sent:
                continue
            seen.add(key)
            try:
                if not path.is_file() or path.stat().st_mtime < request.wall - 2:
                    continue
            except OSError:
                continue
            if not self._may_send(path, request, size=False):
                continue
            if path.stat().st_size > MAX_UPLOAD:
                too_big.append(path)
                continue
            chosen.append(path)
        if too_big:
            self.send("📎 Too big to send here (over 50 MB): " + ", ".join(_home(p) for p in too_big[:3]))
        return chosen[:MAX_FILES]

    def _named_files(self, request: Request) -> list[Path]:
        """Files the request's tools named (found, opened, converted…) that were not sent: a 📤 Share button offers
        them. Only a few: a search that listed twenty files is not something to share."""
        found: list[Path] = []
        for result in request.results:
            for path in _paths(result):
                if str(path) not in request.sent and path not in found and self._may_send(path, request):
                    found.append(path)
        return found if len(found) <= 3 else []

    def _may_send(self, path: Path, request: Request | None, size: bool = True) -> bool:
        """Only the user's own files under home, never secrets or app data, and within Telegram's limit."""
        try:
            real = path.expanduser().resolve()
            if Path.home() not in real.parents:
                return False
            from mint.tools.harness import _blocked
            if _blocked(real, write=True):      # also Library and hidden folders: backups, Mint's own files
                return False
            return not size or real.stat().st_size <= MAX_UPLOAD
        except Exception:
            return False

    # sending -------------------------------------------------------------------------------

    def _chat(self):
        with self.lock:
            return self._state.get("chat_id")

    def _keep(self, table: dict, value) -> str:
        """A short id for a button to find `value` by later (the newest KEEP_BUTTONS are kept)."""
        key = format(next(self._ids), "x")
        table[key] = value
        while len(table) > KEEP_BUTTONS:
            table.pop(next(iter(table)))
        return key

    def _call(self, method: str, files: dict | None = None, patient: bool = True, **params):
        """One Bot API call. HTML Telegram can't parse goes again as plain text; a flood wait is waited out
        (when `patient`)."""
        bot = self.bot
        if bot is None:
            raise ApiError("not connected")
        for _ in range(3):
            try:
                return bot.call(method, files=files, **params)
            except ApiError as error:
                if params.get("parse_mode") and "parse" in str(error).lower():
                    log.info("telegram %s: HTML refused (%s); sending plain text", method, error)
                    params.pop("parse_mode")
                    for field in ("text", "caption"):
                        if field in params:
                            params[field] = _plain(params[field])
                    continue
                if patient and error.retry_after and error.retry_after < 30:
                    time.sleep(error.retry_after)
                    continue
                raise
        raise ApiError(f"{method}: gave up")

    def reply(self, text: str) -> None:
        self.send(text)

    def send(self, text: str, markup: dict | None = None, html: bool = False, **extra) -> dict | None:
        """A message to the paired chat, split cleanly when long; buttons go under the last part."""
        chat = self._chat()
        if not chat or self.bot is None:
            return None
        parts = _split_html(text) if html else _split(text, MAX_TEXT)
        result = None
        for n, part in enumerate(parts):
            params = dict(extra) if n == 0 else {}
            if html and _balanced(part):
                params["parse_mode"] = "HTML"
            elif html:
                part = _plain(part)
            if markup is not None and n == len(parts) - 1:
                params["reply_markup"] = markup
            result = self._send_quietly(chat, part, **params)
        return result

    def html(self, text: str, markup: dict | None = None, **extra) -> dict | None:
        return self.send(text, markup=markup, html=True, **extra)

    def say(self, text: str, markup: dict | None = None) -> None:
        """Mint's own words: light Markdown made into Telegram HTML, long ones split between paragraphs."""
        parts = _split(text.strip(), MAX_TEXT - 800)       # room for the tags and escapes
        for n, part in enumerate(parts):
            self.html(md_html(part), markup=markup if n == len(parts) - 1 else None)

    def _send_quietly(self, chat, text: str | None, action: str = "", **extra) -> dict | None:
        try:
            if action:
                return self._call("sendChatAction", chat_id=chat, action=action)
            return self._call("sendMessage", chat_id=chat, text=text, **extra)
        except ApiError as error:
            log.info("telegram send: %s", error)
            return None

    def _action(self, action: str) -> None:
        """'typing…' / 'sending a photo…' at the top of the chat (5 s each)."""
        chat = self._chat()
        if chat and self.bot is not None:
            self._send_quietly(chat, None, action=action)

    def _edit_message(self, message_id: int, text: str, markup: dict) -> bool:
        try:
            self._call("editMessageText", patient=False, chat_id=self._chat(), message_id=message_id, text=text,
                       parse_mode="HTML", reply_markup=markup)
            return True
        except ApiError as error:
            if "not modified" in str(error):
                return True
            log.info("telegram edit: %s", error)
            return False

    def _send_status(self, request: Request) -> int | None:
        extra = ({"reply_parameters": {"message_id": request.message_id, "allow_sending_without_reply": True}}
                 if request.message_id else {})
        sent = self.html(request.render(), markup=request.buttons(), **extra)
        return sent.get("message_id") if isinstance(sent, dict) else None

    def send_file(self, path: Path, photo: bool = False, caption: str = "", markup: dict | None = None) -> None:
        if not self._chat() or self.bot is None:
            return
        try:
            data = path.read_bytes()
        except OSError as error:
            self.send(f"📎 Could not send {path.name}: {str(error)[:120]}")
            return
        self._send_bytes(data, path.name, photo=photo, caption=caption, markup=markup)

    def _send_bytes(self, data: bytes, name: str, photo: bool = False, caption: str = "",
                    markup: dict | None = None) -> bool:
        chat = self._chat()
        if not chat or self.bot is None:
            return False
        photo = photo and len(data) <= 10 * 1024 * 1024
        method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
        self._action("upload_photo" if photo else "upload_document")
        params: dict = {"chat_id": chat}
        if caption and len(caption) <= MAX_CAPTION:
            params.update(caption=caption, parse_mode="HTML")
        elif caption:
            params["caption"] = _short(_plain(caption), MAX_CAPTION)
        if markup is not None:
            params["reply_markup"] = markup
        try:
            self._call(method, files={field: (name, data)}, **params)
            return True
        except ApiError as error:
            log.info("telegram %s: %s", method, error)
            self.send(f"📎 Could not send {name}: {str(error)[:120]}")
            return False


# --- formatting -----------------------------------------------------------------------------------

def esc(text) -> str:
    """Text for Telegram HTML: &, < and > escaped (quotes are fine outside tags)."""
    return _html.escape(str(text), quote=False)


def _plain(text: str) -> str:
    """Telegram HTML back to the text it shows (for when HTML is refused)."""
    return _html.unescape(re.sub(r"<[^>]+>", "", str(text)))


_TAG = re.compile(r"<(/?)([a-z-]+)(?:\s[^<>]*)?>")


def _balanced(text: str) -> bool:
    """Every tag closed in order and no stray < or >: safe to send (or to send as one part of a split)."""
    stack = []
    for match in _TAG.finditer(text):
        if match.group(1):
            if not stack or stack.pop() != match.group(2):
                return False
        else:
            stack.append(match.group(2))
    return not stack and "<" not in _TAG.sub("", text) and ">" not in _TAG.sub("", text)


def _inline(text: str) -> str:
    out = []
    for piece in re.split(r"(`[^`\n]+`)", text):
        if len(piece) > 2 and piece.startswith("`") and piece.endswith("`"):
            out.append(f"<code>{esc(piece[1:-1])}</code>")
            continue
        piece = esc(piece)
        piece = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
                       lambda m: f'<a href="{_html.escape(_html.unescape(m.group(2)), quote=True)}">{m.group(1)}</a>',
                       piece)
        piece = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", piece)
        out.append(piece)
    return "".join(out)


def md_html(text: str) -> str:
    """Mint's words (which may carry light Markdown: **bold**, `code`, # headings, - lists, [links](url)) as
    Telegram HTML, everything else escaped."""
    lines = []
    for line in str(text).split("\n"):
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        heading = re.match(r"#{1,6}\s+(.+)", stripped)
        bullet = re.match(r"[-*•]\s+(.+)", stripped)
        if heading:
            lines.append(f"<b>{_inline(heading.group(1).strip('* '))}</b>")
        elif bullet:
            lines.append(f"{indent}• {_inline(bullet.group(1))}")
        else:
            lines.append(_inline(line))
    out = "\n".join(lines)
    return out if _balanced(out) else esc(text)


def _split(text: str, limit: int, marks: tuple = ("\n\n", "\n", ". ", " ")) -> list[str]:
    """Long text in parts of at most `limit`, cut between paragraphs, else lines, sentences, words."""
    parts = []
    text = str(text)
    while len(text) > limit:
        cut = 0
        for mark in marks:
            at = text.rfind(mark, limit // 3, limit)
            if at > 0:
                cut = at + len(mark)
                break
        cut = cut or limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n ")
    if text or not parts:
        parts.append(text)
    return parts


def _split_html(text: str) -> list[str]:
    """Our HTML never spans lines with a tag, so it splits cleanly between lines."""
    return _split(text, MAX_TEXT, ("\n\n", "\n"))


def _fit(text: str) -> str:
    return text if len(text) <= MAX_TEXT else esc(_short(_plain(text), MAX_TEXT - 200))


def keys(*rows) -> dict:
    """Inline buttons: rows of (label, callback data)."""
    return {"inline_keyboard": [[{"text": label, "callback_data": data[:64]} for label, data in row]
                                for row in rows if row]}


def _commands(pairs) -> list[dict]:
    return [{"command": name, "description": words} for name, words in pairs]


def _card_data(data: dict) -> dict:
    """show_card's arguments as the island's InfoCard takes them (cards.tool does the same)."""
    items = [{k: str(item.get(k) or "") for k in ("title", "detail", "trailing", "path", "icon")}
             for item in (data.get("items") or []) if isinstance(item, dict) and item.get("title")]
    number = data.get("number")
    if isinstance(number, float) and number.is_integer():
        number = int(number)
    return {**data, "items": items[:12], "number": number}


def _card_key(kind: str, data: dict) -> str:
    if kind == "schedule":
        return f"schedule:{data.get('day')}:{len(data.get('events') or [])}"
    items = data.get("items") or []
    first = items[0].get("title", "") if items and isinstance(items[0], dict) else ""
    return f"info:{data.get('title')}:{data.get('number')}:{first}"


def _card_html(data: dict) -> str:
    """An island card (show_card) as a tidy block."""
    lines = [f"🗂 <b>{esc(data.get('title', ''))}</b>".rstrip()]
    if data.get("subtitle"):
        lines.append(f"<i>{esc(data['subtitle'])}</i>")
    if data.get("number") not in (None, ""):
        number = data["number"]
        if isinstance(number, float) and number.is_integer():
            number = int(number)
        lines.append(f"<b>{esc(number)}</b> {esc(data.get('unit', ''))}".rstrip())
    items = data.get("items") or []
    if items:
        lines.append("")
    for item in items[:15]:
        if isinstance(item, dict):
            words = f"<b>{esc(item.get('title', ''))}</b>" + "".join(
                f" · {esc(item[k])}" for k in ("detail",) if item.get(k)) + (
                f" · <i>{esc(item['trailing'])}</i>" if item.get("trailing") else "")
        else:
            words = esc(item)
        lines.append(f"• {words}")
    more = max(0, len(items) - 15) + int(data.get("more") or 0)
    if more:
        lines.append(f"<i>… and {more} more</i>")
    return "\n".join(lines)


_WEATHER = (("bolt", "⛈"), ("snow", "❄️"), ("sleet", "🌨"), ("rain", "🌧"), ("drizzle", "🌦"), ("fog", "🌫"),
            ("haze", "🌫"), ("smoke", "🌫"), ("sun.max", "☀️"), ("cloud.sun", "⛅"), ("cloud", "☁️"))


def _clock(value) -> str:
    return value.strftime("%-I:%M%p").lower() if isinstance(value, dt.datetime) else str(value or "")


def _schedule_html(data: dict) -> str:
    """The briefing's schedule card (the day, the weather, today's events, reminders, headlines) as a block."""
    head = f"🗓 <b>{esc(data.get('day', ''))}</b>" + (f" · {esc(data['date'])}" if data.get("date") else "")
    weather = data.get("weather") or {}
    if weather:
        icon = next((e for word, e in _WEATHER if word in str(weather.get("symbol", ""))), "🌡")
        head += f" · {icon} {esc(weather.get('temp', ''))}°" + (f" ({esc(weather['range'])})"
                                                               if weather.get("range") else "")
    lines = [head, ""]
    now = dt.datetime.now()
    events = [e for e in (data.get("events") or []) if isinstance(e, dict)][:8]
    upcoming = next((e for e in events if not e.get("all_day") and isinstance(e.get("start"), dt.datetime)
                     and e["start"] > now), None)
    for event in events:
        start, end = event.get("start"), event.get("end")
        when = "all day" if event.get("all_day") else _clock(start)
        title = esc(event.get("title", ""))
        live = isinstance(start, dt.datetime) and isinstance(end, dt.datetime) and start <= now < end \
            and not event.get("all_day")
        past = isinstance(end, dt.datetime) and end <= now and not event.get("all_day")
        tail = ""
        if live:
            tail = " · <b>now</b>"
        elif event is upcoming:
            minutes = int((start - now).total_seconds() // 60)
            tail = f" · <i>in {minutes} min</i>" if minutes < 60 else f" · <i>in {minutes // 60} h {minutes % 60} min</i>"
        lines.append(f"<code>{esc(when):>7}</code>  " + (f"<s>{title}</s>" if past else title) + tail)
    if not events:
        lines.append("<i>Nothing on the calendar today.</i>")
    if data.get("reminders"):
        lines += ["", "⏰ " + " · ".join(esc(r) for r in data["reminders"][:4])]
    if data.get("news"):
        lines += ["", "<b>Headlines</b>"] + [f"• {esc(n)}" for n in data["news"][:3]]
    return "\n".join(lines)


def _file_caption(path: Path) -> str:
    try:
        size = _size(path.stat().st_size)
    except OSError:
        size = ""
    return f"📎 <b>{esc(path.name)}</b>" + (f" · {size}" if size else "") + f"\n<i>{esc(_home(path.parent))}</i>"


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


# --- island cards as pictures -----------------------------------------------------------------

def _island_cards() -> list:
    """The cards on (or queued for) the island now."""
    try:
        from mint.ui import island
        return [scene for scene, _ in list(island.island.pushed)]
    except Exception:
        return []


def card_png(kind: str, data: dict) -> bytes | None:
    """An island card drawn offscreen by the island's own code, as a PNG. AppKit wants the main thread: run
    there directly, else handed to Mint's main run loop; None when there is none (or it fails)."""
    try:
        import AppKit
    except Exception:
        return None
    if threading.current_thread() is threading.main_thread():
        return _draw_card(kind, data)
    app = AppKit.NSApp()
    if app is None or not app.isRunning():
        return None
    from PyObjCTools import AppHelper
    box: dict = {}
    done = threading.Event()

    def work():
        try:
            box["png"] = _draw_card(kind, data)
        except Exception:
            log.info("card picture", exc_info=True)
        finally:
            done.set()

    AppHelper.callAfter(work)
    return box.get("png") if done.wait(4) else None


def _draw_card(kind: str, data: dict) -> bytes | None:
    """Build the card's views (InfoCard / ScheduleCard) in an offscreen window and paint them: the layers
    first (backgrounds, stripes, chips), then the views (text, symbols) on top, at 2x."""
    import AppKit
    import Quartz
    from mint.ui import island as isl
    AppKit.NSApplication.sharedApplication()     # already there in Mint; a script needs it for windows
    scene = isl.ScheduleCard(data) if kind == "schedule" else isl.InfoCard(data)
    width, height = scene.size()
    pad, scale = 14, 2
    view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (width, height)))
    view.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
    view.setWantsLayer_(True)
    view.layer().setBackgroundColor_(isl._cg(isl.SURFACE))
    view.layer().setCornerRadius_(isl.CARD_RADIUS)
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (width, height)), AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
    window.setReleasedWhenClosed_(False)
    window.setContentView_(view)
    face = (isl.island.face_right, isl.island.face_top)
    isl.island.face_top = False              # no live face in a picture's corner: the text may use the width
    try:
        scene.build(view, width, height)
    finally:
        isl.island.face_right, isl.island.face_top = face
    view.layoutSubtreeIfNeeded()
    view.display()
    w, h = int((width + 2 * pad) * scale), int((height + 2 * pad) * scale)
    space = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    context = Quartz.CGBitmapContextCreate(None, w, h, 8, 0, space, Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextSetRGBFillColor(context, 0.13, 0.14, 0.16, 1.0)
    Quartz.CGContextFillRect(context, Quartz.CGRectMake(0, 0, w, h))
    Quartz.CGContextScaleCTM(context, scale, scale)
    Quartz.CGContextTranslateCTM(context, pad, pad)
    view.layer().renderInContext_(context)
    graphics = AppKit.NSGraphicsContext.graphicsContextWithCGContext_flipped_(context, False)
    AppKit.NSGraphicsContext.saveGraphicsState()
    try:
        AppKit.NSGraphicsContext.setCurrentContext_(graphics)
        view.displayRectIgnoringOpacity_inContext_(view.bounds(), graphics)
    finally:
        AppKit.NSGraphicsContext.restoreGraphicsState()
    image = Quartz.CGBitmapContextCreateImage(context)
    window.setContentView_(None)
    window.close()
    if image is None:
        return None
    rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None)
    return bytes(png) if png is not None else None


# --- helpers ------------------------------------------------------------------------------------

def _later(fn, *args) -> None:
    threading.Thread(target=fn, args=args, name="telegram-work", daemon=True).start()


def _caption(name: str, args: dict) -> str:
    try:
        from mint.ui import activity
        return activity.phrase(name, args)
    except Exception:
        return name.replace("_", " ").capitalize()


def _latest_request() -> str:
    try:
        from mint.app import live
        return live.request() or ""
    except Exception:
        return ""


def _secret(text: str) -> bool:
    try:
        from mint.knowledge.skills import has_secret
        return bool(has_secret(text))
    except Exception:
        return False


def _looks_failed(result: str) -> bool:
    lowered = result.lower()
    return lowered.startswith(("not done", "not run", "failed")) or any(
        mark in lowered[:200] for mark in ("could not", "cannot", "failed", "not allowed", "timed out",
                                           "nothing was done", "refused"))


def _took(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} s" if seconds < 600 else f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} h"


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return "just now" if seconds < 60 else f"{seconds // 60} min ago" if seconds < 3600 else \
        f"{seconds // 3600} h ago" if seconds < 86400 else f"{seconds // 86400} d ago"


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _home(path: Path) -> str:
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home) else text


def _paths(result: str) -> list[Path]:
    """Existing files named in a tool result ('Saved as ~/Desktop/Screenshot … .png.')."""
    home = re.escape(str(Path.home()))
    found = []
    for match in re.finditer(rf"((?:~|{home})/[^\n\"“”]*?\.(?:{_EXTS}))(?![A-Za-z0-9])", result, re.I):
        path = Path(match.group(1)).expanduser()
        if path.is_file():
            found.append(path)
    return found


def _attachment(message: dict) -> dict | None:
    """The file in a message, as {id, name, size, mime}: a document, the largest photo, a video, an audio file or
    a round video note (forwarded ones look the same). Voice notes are requests, not files: not here."""
    stamp = time.strftime("%Y-%m-%d at %H.%M.%S", time.localtime(float(message.get("date") or time.time())))

    def pick(media: dict, name: str, mime: str) -> dict:
        return {"id": str(media["file_id"]), "name": name, "size": int(media.get("file_size") or 0),
                "mime": str(media.get("mime_type") or mime)}

    document = message.get("document")         # an animation (GIF) comes with its document too
    if isinstance(document, dict) and document.get("file_id"):
        return pick(document, str(document.get("file_name") or f"File {stamp}"), "")
    photos = [p for p in (message.get("photo") or []) if isinstance(p, dict) and p.get("file_id")]
    if photos:
        best = max(photos, key=lambda p: (int(p.get("width") or 0) * int(p.get("height") or 0),
                                          int(p.get("file_size") or 0)))
        return pick(best, f"Photo {stamp}.jpg", "image/jpeg")
    for key, label, mime in (("video", "Video", "video/mp4"), ("audio", "Audio", "audio/mpeg"),
                             ("video_note", "Video note", "video/mp4")):
        media = message.get(key)
        if isinstance(media, dict) and media.get("file_id"):
            name = media.get("file_name") or " - ".join(str(x) for x in (media.get("performer"), media.get("title"))
                                                        if x) or f"{label} {stamp}"
            return pick(media, str(name), mime)
    return None


def _safe_name(name: str, mime: str = "") -> str:
    """The sender's file name, made safe to save: no folders, no control characters or colons, not hidden, a
    sane length; an extension from the type when it has none."""
    name = unicodedata.normalize("NFC", str(name or "")).replace("\\", "/").rsplit("/", 1)[-1]
    name = " ".join(re.sub(r"[\x00-\x1f\x7f:]", " ", name).split()).strip(". ")
    stem, ext = os.path.splitext(name)
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext or ""):
        stem, ext = name, ""
    if not ext and mime:
        ext = mimetypes.guess_extension(mime.split(";")[0].strip()) or ""
    stem = stem.strip(". ") or "File"
    while len(stem.encode()) > 180:
        stem = stem[:-1]
    return stem.rstrip(". ") + ext


def _save_new(folder: Path, name: str, data: bytes) -> Path:
    """Write `data` as folder/name - never over another file: 'report 2.pdf', 'report 3.pdf'…"""
    folder.mkdir(parents=True, exist_ok=True)
    stem, ext = os.path.splitext(name)
    for n in range(1, 1000):
        path = folder / (name if n == 1 else f"{stem} {n}{ext}")
        if path.resolve().parent != folder.resolve():
            raise OSError("not a safe file name")
        try:
            with open(path, "xb") as out:          # x: fails if it exists (no race with an album's twin)
                out.write(data)
            return path
        except FileExistsError:
            continue
    raise OSError("too many files with that name")


def _bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _icon(path: Path) -> str:
    return {"image": "🖼", "video": "🎬", "audio": "🎵", "sheet": "📊", "other": "📦"}.get(_file_kind([path]), "📄")


def _file_kind(paths: list[Path]) -> str:
    """Which buttons fit: the island's drop kinds (document, image, video, audio, sheet), 'other' or 'many'."""
    if len(paths) > 1:
        return "many"
    if not paths or paths[0].suffix.lower() in _ARCHIVES:
        return "other"
    try:
        from mint.ui.island import drop_kind
        kind = drop_kind([str(paths[0])], "")
    except Exception:
        kind = "document"
    return kind if kind in _FILE_BUTTONS else "other"


def _what(paths: list) -> str:
    """The saved files as a request names them."""
    shown = [_home(Path(p)) for p in paths]
    return shown[0] if len(shown) == 1 else "these files: " + "; ".join(shown)


def _file_prompt(paths: list[Path], caption: str) -> str:
    """The request for a file with a caption: where it was saved, then the user's own words, unchanged (the risk
    and send checks judge what the user said). Worded so it adds no word those checks look for."""
    if len(paths) == 1:
        return f"The user sent a file from their phone: {_home(paths[0])}. Their request: {caption}"
    return (f"The user sent {len(paths)} files from their phone: {'; '.join(_home(p) for p in paths)}. "
            f"Their request: {caption}")


def _saved_line(paths: list[Path]) -> str:
    names = ", ".join(f"{esc(p.name)} ({_size(_bytes(p))})" for p in paths[:5]) + (" …" if len(paths) > 5 else "")
    return f"📥 Saved {names} in <i>{esc(_home(paths[0].parent))}</i>"


def _find_named(query: str) -> list[Path]:
    """/send by name: Spotlight by file name under home (find_files' search), hidden and secret files left out."""
    from mint.tools.harness import _search_paths
    return _search_paths(query, None, "any", 8, content=False)


def _frontmost() -> str:
    try:
        import AppKit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName() or "") if app else ""
    except Exception:
        return ""


def _frontmost_app() -> str:
    """The app in front, for the working message ('' when it is Mint itself)."""
    app = _frontmost()
    return "" if app.lower() in ("mint", "python", "python3") else app


def _messaging(app: str) -> bool:
    app = app.lower()
    return bool(app) and any(re.search(rf"\b{m}\b", app) for m in _MESSAGING)


def _read_only_block(name: str, args: dict) -> str:
    """Why this call would send, delete or buy something ('' if it would not)."""
    action = str(args.get("action", "")).lower()
    if name in _RO_TOOLS:
        return f"{name.replace('_', ' ')} deletes something"
    if action in _RO_ACTIONS.get(name, ()):
        return f"{name.replace('_', ' ')} {action.replace('_', ' ')} deletes something"
    if name == "notifications" and action == "reply" and str(args.get("send", "")).lower() in ("true", "1", "yes"):
        return "replying to a notification sends a message"
    if name in _RO_TEXT:
        fields, actions = _RO_TEXT[name]
        if actions is None or action in actions:
            words = " ".join(json.dumps(args.get(f), ensure_ascii=False) if isinstance(args.get(f), (list, dict))
                             else str(args.get(f) or "") for f in fields)
            hit = _RO_WORDS.search(words)
            if hit:
                return f"“{hit.group(0)}” in {name.replace('_', ' ')}"
    press_return = str(args.get("press_return", "")).lower() in ("true", "1", "yes")
    if name in ("type_text", "ui_act") and press_return:
        app = str(args.get("app") or "") or _frontmost()
        if _messaging(app) or not app:
            return f"pressing Return in {app or 'the app in front'} would send it"
    if name == "press_key":
        key = str(args.get("key", "")).lower()
        mods = [str(m).lower() for m in (args.get("modifiers") or [])]
        if key in ("return", "enter") and _messaging(_frontmost()):
            return f"Return in {_frontmost()} would send a message"
        if key in ("delete", "backspace", "forward_delete") and any(m in ("cmd", "command") for m in mods):
            return "⌘⌫ moves things to the Bin"
    if name in ("click_at", "pointer") and ("click" in action or name == "click_at"):
        app = _frontmost()
        if _messaging(app):
            return f"a blind click in {app} could press Send"
    return ""


_SEND_ASKED = re.compile(r"\b(send|reply|respond|post|dm|forward|message (?!box)|text (?:him|her|them|"
                         r"[A-Z]|back))", re.IGNORECASE)
_SEND_WORDS = re.compile(r"\b(press(?:es|ing)? (?:the )?(?:return|enter)|hit (?:return|enter)|\bsend\b|submit)",
                         re.IGNORECASE)


def send_guard(name: str, args: dict, request: str) -> str:
    """For EVERY request, not only the phone's: in a messaging app (Telegram, Slack, Messages…), nothing that would
    send - Return, a Send button, a blind click - unless the user asked to send, reply or message someone. Seen: asked
    only to read a Telegram chat, the desktop tool typed a search into the message box and pressed Return, sending it."""
    if _SEND_ASKED.search(request or ""):
        return ""
    why = ""
    action = str(args.get("action", "")).lower()
    front = _frontmost()
    press_return = str(args.get("press_return", "")).lower() in ("true", "1", "yes")
    if name in ("type_text", "ui_act") and press_return:
        app = str(args.get("app") or "") or front
        if _messaging(app):
            why = f"pressing Return in {app} would send a message"
    elif name == "press_key":
        if str(args.get("key", "")).lower() in ("return", "enter") and _messaging(front):
            why = f"Return in {front} would send a message"
    elif name == "desktop":
        goal = str(args.get("goal") or args.get("task") or args.get("instruction") or "")
        app = str(args.get("app") or "") or front
        if _messaging(app) and _SEND_WORDS.search(goal):
            why = f"that would press Return or Send in {app}, which sends a message"
    elif name in ("click_at", "pointer") and ("click" in action or name == "click_at") and _messaging(front):
        why = f"a blind click in {front} could press Send"
    elif name in ("click_text", "ui_act") and _messaging(front):
        target = str(args.get("text") or args.get("target") or args.get("name") or "").strip().lower()
        if target in ("send", "send message", "reply", "post"):
            why = f"clicking {target} in {front} sends a message"
    if not why:
        return ""
    return (f"REFUSED: {why}, and the user did not ask to send anything. To find a chat, use the app's search with "
            "click_text or ui_act without pressing Return; to read, use read_window or look. Ask the user if they "
            "want something sent.")


def screen_locked() -> bool | None:
    """True while the login window covers the screen (locked or asleep behind it); None if unknown."""
    try:
        import Quartz
        info = Quartz.CGSessionCopyCurrentDictionary()
        if info is None:
            return None
        return bool(info.get("CGSSessionScreenIsLocked", False))
    except Exception:
        return None


def ffmpeg() -> str | None:
    """The app is not started from a shell, so Homebrew's folders may not be on PATH."""
    for candidate in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def to_pcm(audio: bytes, suffix: str = ".oga") -> bytes:
    """A voice note (Opus in Ogg) -> 16 kHz mono 16-bit PCM, what dictation hears."""
    tool = ffmpeg()
    if tool is None:
        raise RuntimeError("voice notes need ffmpeg (brew install ffmpeg)")
    with tempfile.NamedTemporaryFile(suffix=suffix) as source:
        source.write(audio)
        source.flush()
        done = subprocess.run([tool, "-v", "error", "-i", source.name, "-ac", "1", "-ar", "16000", "-f", "s16le",
                               "-acodec", "pcm_s16le", "-"], capture_output=True, timeout=120)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg could not read it: {done.stderr.decode(errors='replace').strip()[:160]}")
    return done.stdout


def transcribe(audio: bytes, suffix: str = ".oga") -> str:
    """Word for word, through the same fast Gemini models dictation hears with. '' when nothing was said."""
    from google.genai import types
    from mint.voice import dictation
    from mint.core import llm
    from mint.core import prefs
    pcm = to_pcm(audio, suffix)
    if not dictation._spoken(pcm):
        return ""
    words = [w.strip() for w in str(prefs.get("dictation_words") or "").split(",") if w.strip()]
    hint = f" Words they use, spelled exactly: {', '.join(words[:60])}." if words else ""
    heard, _ = llm.generate([types.Part.from_bytes(data=dictation._wav(pcm), mime_type="audio/wav"),
                             "This is a voice message to a computer assistant. Transcribe it word for word. "
                             "English words stay in Latin letters, whatever the accent (never transliterate English "
                             "into Devanagari); Hindi mixed with English (Hinglish) is written in Latin letters too; "
                             "only a message entirely in Hindi is written in Devanagari." + hint +
                             " If nothing is said, return an empty string. Return only the transcript."],
                            dictation.HEAR)
    heard = heard.strip().strip('"').strip()
    return "" if heard.lower() in ("", "(empty)", "empty", '""') else heard


# --- what the rest of Mint calls ------------------------------------------------------------------

bridge = Bridge()


def start(mint=None) -> None:
    """Once, when the session starts. The bridge only talks to Telegram while a token is set and
    telegram_enabled is on; switching either in Settings takes effect within seconds."""
    if mint is not None:
        bridge.host = SessionHost(mint)
    bridge.start()
    try:
        from mint.app import telegram_agents
        telegram_agents.attach(bridge)      # Claude Code / Codex: alerts here, answers from here
    except Exception:
        log.debug("telegram agents", exc_info=True)
    try:
        from mint.core import prefs
        prefs.on_change(lambda key, _value: refresh() if key.startswith("telegram_") else None)
    except Exception:
        pass
    _email("start", mint)                   # email remote control: idle until it is set up and on


def _email(name: str, *args):
    """Email remote control (email_remote.py) shares this module's hooks into the session: the same events,
    the same read-only gate. Never raises."""
    try:
        from mint.app import email_remote
        return getattr(email_remote, name)(*args)
    except Exception:
        log.debug("email %s", name, exc_info=True)
        return None


def refresh() -> None:
    """The token or a switch changed (Settings): stop acting at once, reconnect after the open poll."""
    bridge.wake.set()
    bot = bridge.bot
    if bot is not None and (bot.token != bridge._token() or not bridge.setting("telegram_enabled")):
        # The poll in progress ends within POLL_WAIT; what it brings back is not acted on.
        bridge.running = False


def on_event(kind: str, data: dict | None = None) -> None:
    """The session's hook: request, state, tool_start, tool_end, reply, done, stop, announce, notice.
    Never raises and never blocks."""
    try:
        bridge.event(kind, data or {})
    except Exception:
        log.debug("telegram event %s", kind, exc_info=True)
    _email("on_event", kind, data or {})


def gate(name: str, args: dict) -> str:
    """'' to run the tool, else the refusal to return instead (read-only phone requests)."""
    try:
        refused = bridge.gate(name, args)
    except Exception:
        log.debug("telegram gate", exc_info=True)
        refused = ""
    return refused or _email("gate", name, args) or ""


def status() -> dict:
    return bridge.status()


def pairing_code(new: bool = False) -> str:
    return bridge.pairing_code(new)


def unpair() -> None:
    bridge.unpair()
