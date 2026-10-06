"""The guard: nothing is deleted, overwritten, moved away or changed on your system without your yes.

Before Mint (or one of its helper agents) does something that deletes or alters - moves files to the Trash,
overwrites a file, moves or renames files, deletes notes, reminders or events, clears notifications, runs a script
or shell command that removes, overwrites or reconfigures something, presses ⌘⌫, clicks "Delete" - it stops and
asks, in plain words:

    🗑  Mint wants to move 3 files to the Trash
        ~/Downloads/old-report.pdf
        ~/Downloads/scan 2.png
        ~/Downloads/build (a folder, 132 items)
                                       [ Don't ]  [ Yes, do it ]      (or say "yes" / "no")

At the Mac this is a card (under the notch in notch mode, by the orb otherwise) and you can say "yes" or "no".
When the request came from your phone, or you are away from the Mac, the same question comes on Telegram with
✅ Yes / ❌ No buttons. When it came by email (email_remote), the question is emailed in that thread and a reply
with YES or NO answers it. The first answer wins; no answer in time means no. Saying "stop" means no.

Levels (Settings ▸ General ▸ Ask before deleting or changing, pref "guard"):
    all      deleting, and changing or moving existing things, and risky system commands (default)
    delete   deleting and risky system commands only
    off      the guard never asks (Mint's older rule still applies: risky words need your request)

Whatever the level, Mint's own files always ask first: its app-support folder (.venv, settings.json, memory,
skills), Mint.app, and Claude Code's / Codex's config (~/.claude, ~/.codex) - by a tool or a shell command.

Two more rules: after BREAKER "no"s (or no answers) in a row for one request, the guard stops asking and tells the
model to drop that task and ask the user what to do instead; and when several questions are open at once (parallel
jobs), "yes to all" - or Telegram's "Allow all" button - answers every one.

Claude Code is guarded too when it is connected (agent_hooks): a destructive shell command (rm -rf, git reset
--hard, push --force...) makes Claude Code ask first even where you allowed it, and that question reaches the
notch and Telegram like any approval.

API:
    assess(name, args) -> Danger | None        what a tool call would delete or change (no side effects)
    shell_danger(command) -> str               why a shell command is risky ('' when it isn't)
    await check(name, args, source) -> str     '' to go ahead, else the refusal for the model
    ask(danger, source, timeout) -> bool       blocking (helper agents' threads)
    answer(ident, yes) / heard(text)           a button, a Telegram press, or the user's words
    protected(path) -> str                     what of Mint's own a path is ('' for anything else)
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mint.core.guard")

HOME = str(Path.home())
WAIT_MAC = 90.0            # seconds to answer at the Mac
WAIT_PHONE = 300.0         # ... when asked on Telegram
BREAKER = 3                # this many "no"s / no answers in a row for one request: stop asking about it
BREAKER_WINDOW = 600.0     # ... counted within this many seconds
YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|allow( it)?|confirm(ed)?|haan|ha+n?|"
                 r"theek hai|kar do)\b", re.I)
NO = re.compile(r"^\W*(no|nope|nah|don'?t|do not|cancel|stop|never ?mind|wait|mat( karo)?|nahi+n?)\b", re.I)
ALL = re.compile(r"\b(all|everything|both|each of them|every one|sab)\b", re.I)   # "yes to all", "no, none of them"


@dataclass
class Danger:
    kind: str                      # delete / change / system / protect (Mint's own files: asked at every level)
    title: str                     # "move 3 files to the Trash"
    lines: list = field(default_factory=list)   # what exactly (paths, the command)
    mono: bool = False             # the lines are code

    @property
    def icon(self) -> str:
        return {"delete": "trash.fill", "change": "pencil.circle.fill"}.get(self.kind, "exclamationmark.shield.fill")


# --- what is dangerous ---------------------------------------------------------------------------------

_SHELL = [
    (r"(^|[;&|`(\s])(sudo)\s", "runs as administrator (sudo)", "system"),
    (r"(^|[;&|`(\s])rm\s", "deletes files (rm)", "delete"),
    (r"(^|[;&|`(\s])(rmdir|unlink|shred|srm)\s", "deletes files", "delete"),
    (r"(^|[;&|`(\s])trash\s", "moves files to the Trash", "delete"),
    (r"\bfind\b[^;&|]*\s-(delete|exec\s+rm)\b", "deletes the files it finds", "delete"),
    (r"(^|[;&|`(\s])(dd|mkfs\S*|newfs\S*)\s", "writes straight to a disk", "system"),
    (r"\bdiskutil\s+(erase\w*|partition\w*|zero\w*|secureerase|apfs\s+delete\w*)", "erases a disk", "system"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-\w*f|checkout\s+--\s|restore\s|branch\s+-D|stash\s+(drop|clear))",
     "throws away git work", "delete"),
    (r"\bgit\s+push\b[^;&|]*(--force\b|-f\b|--force-with-lease|--delete\b|\s:\S)", "rewrites or deletes on the remote",
     "delete"),
    (r"(^|[;&|`(\s])(chmod|chown|chflags)\s+-R\b", "changes permissions on a whole folder", "system"),
    (r"(^|[;&|`(\s])(killall|pkill)\s|(^|[;&|`(\s])kill\s+-9\b", "force-quits programs", "system"),
    (r"\blaunchctl\s+(unload|remove|bootout|disable)\b", "turns off a background service", "system"),
    (r"\bdefaults\s+(write|delete)\b", "changes system or app settings", "system"),
    (r"\bcrontab\s+-r\b", "deletes your scheduled jobs", "delete"),
    (r"\b(networksetup|systemsetup|pmset|scutil|spctl|csrutil|nvram|tmutil\s+(delete|disable))\b",
     "changes system settings", "system"),
    (r"\b(brew|pip3?|npm|pnpm|yarn)\s+(uninstall|remove|rm)\b|\bbrew\s+cleanup\b", "uninstalls software", "delete"),
    (r"\bdocker\s+(rm|rmi|volume\s+rm|system\s+prune|image\s+prune)\b|\bkubectl\s+delete\b", "deletes containers or data",
     "delete"),
    (r"\b(drop\s+(table|database|schema)|truncate\s+table|delete\s+from)\b", "deletes database data", "delete"),
    (r"(^|[;&|`(\s])(mv)\s", "moves or renames files", "change"),
    (r"(^|[;&|`(\s])truncate\s|(^|[^>&0-9])>\s*[~/\w.]", "overwrites a file", "change"),
    (r"\b(shutdown|reboot|halt)\b", "restarts or shuts down the Mac", "system"),
]

_SCRIPT = [
    (r"\bempty\s+(the\s+)?trash\b", "empties the Trash", "delete"),
    (r"\bdelete\b", "deletes something", "delete"),
    (r"\bmove\b[^\n]*\bto\s+(the\s+)?trash\b", "moves things to the Trash", "delete"),
    (r"with administrator privileges", "runs as administrator", "system"),
    (r"\b(restart|shut down|log out)\b", "restarts, shuts down or logs out", "system"),
]

# "format" only for disks (VS Code's "Format Document" just re-indents, and undoes); "reset" not for
# views ("Reset Zoom", "Reset Layout", "Reset Filters").
_LABEL = re.compile(r"\b(delete|remove|erase|empty trash|move to (the )?trash|discard|uninstall|"
                    r"reset(?! (zoom|view|layout|filters?|search|sort(ing)?)\b)|wipe|"
                    r"clear (all|history|data)|format (the |this )?(disk|drive|volume|card|partition)|deactivate|"
                    r"close account|revoke)\b", re.I)
_KEYS = {"cmd+delete": "moves the selection to the Trash (Finder) or deletes it",
         "cmd+backspace": "moves the selection to the Trash (Finder) or deletes it",
         "cmd+shift+delete": "empties the Trash", "cmd+shift+backspace": "empties the Trash",
         "cmd+option+shift+delete": "empties the Trash without asking",
         "cmd+option+delete": "deletes the selection at once"}


def _short(path) -> str:
    text = str(path or "")
    return "~" + text[len(HOME):] if text.startswith(HOME) else text


def _expand(path) -> Path:
    return Path(os.path.expanduser(str(path or ""))).resolve() if path else Path("")


def _about(path) -> str:
    """'~/x.pdf (2.3 MB)' / '~/build (a folder, 132 items)'."""
    p = _expand(path)
    try:
        if p.is_dir():
            count = sum(1 for _ in itertools.islice(p.rglob("*"), 5001))
            return f"{_short(p)} (a folder, {count if count <= 5000 else 'over 5000'} items)"
        if p.is_file():
            size = p.stat().st_size
            for unit in ("bytes", "KB", "MB", "GB"):
                if size < 1024 or unit == "GB":
                    return f"{_short(p)} ({size:.0f} {unit})" if unit == "bytes" else f"{_short(p)} ({size:.1f} {unit})"
                size /= 1024
    except OSError:
        pass
    return _short(p)


def _paths(value) -> list:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    text = str(value or "").strip()
    if text.startswith("["):
        try:
            import json
            return [str(v) for v in json.loads(text)]
        except ValueError:
            pass
    return [line.strip() for line in text.splitlines() if line.strip()]


# --- Mint's own files: asked about at every level, "off" too ---------------------------------------------

def _own_roots() -> list[tuple[str, bool, str]]:
    """(path, everything inside counts too, what it is)."""
    home = Path.home()
    app = home / "Library" / "Application Support" / "Mint"
    roots = []
    try:
        from mint.core import config
        project = Path(config.PROJECT_ROOT)
    except Exception:
        project = app
    for base in dict.fromkeys((app, project)):
        roots += [(str(base / ".venv"), True, "Mint's Python environment"),
                  (str(base / "settings.json"), False, "Mint's settings"),
                  (str(base / "memory"), True, "Mint's memory"), (str(base / "skills"), True, "Mint's skills"),
                  (str(base / "mint"), True, "Mint's program files")]
    roots += [(str(Path("/Applications/Mint.app")), True, "the Mint app"),
              (str(home / "Applications" / "Mint.app"), True, "the Mint app"),
              (str(home / ".claude"), True, "Claude Code's settings"), (str(home / ".claude.json"), False,
                                                                       "Claude Code's settings"),
              (str(home / ".codex"), True, "Codex's settings"),
              (str(app), True, "Mint's own folder (its memory, skills and settings)")]
    return roots


def protected(path) -> str:
    """What of Mint's own `path` is ('' for anything else). A folder that holds one of them counts too: deleting
    ~/Library deletes Mint's memory."""
    raw = str(path or "").strip().strip("'\"")
    if not raw:
        return ""
    raw = re.split(r"[*?\[]", raw.replace("${HOME}", "~").replace("$HOME", "~"), 1)[0]   # a glob: its folder
    full = os.path.normpath(os.path.expanduser(raw))
    if not os.path.isabs(full):
        return ""
    if full.lower().endswith("/mint.app") or "/mint.app/" in full.lower() + "/":
        return "the Mint app"
    for root, inside, what in _own_roots():
        if full == root or (inside and full.startswith(root + os.sep)):
            return what
        if root.startswith(full.rstrip(os.sep) + os.sep):
            return f"a folder holding {what}"
    return ""


# A command that deletes, moves or writes over something: then its paths are checked against Mint's own.
_SHELL_WRITES = re.compile(r"(?:^|[;&|`(\s])(rm|rmdir|unlink|shred|srm|trash|mv|cp|ditto|rsync|tee|truncate|ln|"
                           r"install|chmod|chown|chflags|sed\s+-i|find)\s", re.I)
_COPIES = {"cp", "ditto", "rsync", "ln", "install", "tee"}       # only the last path is written to
_REDIRECT = re.compile(r"(?<![0-9&])>>?\s*(\"[^\"]*\"|'[^']*'|(?:\\ |[^\s;&|<>()`])+)")
_SHELL_OWN = re.compile(r"Application(\\ | |%20)Support/Mint\b|\bMint\.app\b|(~|\$\{?HOME\}?|/Users/[^/\s]+)/"
                        r"\.(claude|codex)\b|\.claude\.json\b", re.I)


def _shell_own(text: str) -> str:
    """What of Mint's own a shell command would delete or write over ('' for nothing)."""
    targets = [m.group(1) for m in _REDIRECT.finditer(text)]
    verbs = {v.split()[0].lower() for v in _SHELL_WRITES.findall(text)}
    if verbs and (verbs != {"find"} or re.search(r"\s-(delete|exec)\b", text)):
        tokens = [next(t for t in group if t) for group in
                  re.findall(r'"([^"]*)"|\'([^\']*)\'|((?:\\ |[^\s;&|<>()`])+)', text) if any(group)]
        tokens = [t.replace("\\ ", " ") for t in tokens if not t.startswith("-")]
        targets += tokens[-1:] if verbs <= _COPIES else tokens
    for target in targets:
        found = protected(target)
        if found:
            return found
    m = _SHELL_OWN.search(text) if (verbs or targets) and not verbs <= _COPIES else None
    if m is None:
        return ""
    return "Claude Code's or Codex's settings" if re.search(r"claude|codex", m.group(0), re.I) else "Mint's own files"


def shell_danger(command: str) -> tuple[str, str]:
    """(why, kind) for the riskiest thing a shell command does, ('', '') when nothing."""
    text = str(command or "")
    own = _shell_own(text)
    if own:
        return f"deletes or writes over {own}", "protect"
    for pattern, why, kind in _SHELL:
        if re.search(pattern, text, re.I | re.M):
            return why, kind
    return "", ""


def _script_danger(script: str) -> tuple[str, str]:
    text = str(script or "")
    for m in re.finditer(r'do shell script\s+"((?:[^"\\]|\\.)*)"', text, re.I):
        why, kind = shell_danger(m.group(1).encode().decode("unicode_escape", "ignore"))
        if why:
            return why, kind
    for pattern, why, kind in _SCRIPT:
        if re.search(pattern, text, re.I):
            return why, kind
    return "", ""


def _own_danger(name: str, a: dict, action: str) -> Danger | None:
    """A file tool about to delete, move or write over Mint's own files (asked at every level)."""
    if name == "file_action" and action in ("trash", "move", "rename"):
        paths = _paths(a.get("path")) + ([str(a.get("to"))] if action == "move" and a.get("to") else [])
        verb = {"trash": "move {} to the Trash", "move": "move {}", "rename": "rename {}"}[action]
    elif name == "write_file":
        paths, verb = [str(a.get("path") or "")], "write over {}"
    elif name in ("tidy", "merge_folders"):
        paths = [str(a.get("folder") or "")] + _paths(a.get("folders")) + [str(a.get("into") or "")]
        verb = "move files in {}"
    else:
        return None
    for path in paths:
        what = protected(path)
        if what:
            return Danger("protect", verb.format(what), [_about(path)])
    return None


def assess(name: str, args: dict) -> Danger | None:
    """What this tool call would delete or change, or None when it is harmless (no side effects here)."""
    a = args or {}
    action = str(a.get("action") or "").lower()
    own = _own_danger(name, a, action)
    if own is not None:
        return own
    if name == "file_action" and action == "trash":
        paths = _paths(a.get("path"))
        return Danger("delete", f"move {len(paths)} item{'s' if len(paths) != 1 else ''} to the Trash"
                      if len(paths) != 1 else "move this to the Trash", [_about(p) for p in paths[:6]])
    if name == "file_action" and action in ("move", "rename"):
        return Danger("change", "rename this" if action == "rename" else "move this",
                      [_about(a.get("path")), "→ " + _short(os.path.expanduser(str(a.get("to") or "")))])
    if name == "write_file" and str(a.get("mode") or "").lower() in ("overwrite", "replace"):
        p = _expand(a.get("path"))
        if p.exists():
            what = "replace part of" if str(a.get("mode")).lower() == "replace" else "overwrite"
            return Danger("change", f"{what} a file that already exists", [_about(p)])
    if name == "tidy" and action == "remove_duplicates":
        return Danger("delete", "move the duplicate files to the Trash", [_short(a.get("folder") or "")])
    if name == "tidy" and action == "apply":
        return Danger("change", "move files into the tidy plan's folders", [_short(a.get("folder") or "")])
    if name == "merge_folders":
        lines = [_about(p) for p in _paths(a.get("folders"))[:5]] + ["→ " + _short(a.get("into") or "")]
        if a.get("remove_empty"):
            lines.append("and delete the folders left empty")
        return Danger("delete" if a.get("remove_empty") else "change", "merge folders (files are moved)", lines)
    if name in ("notes", "reminders_manage", "calendar_manage") and action == "delete":
        what = {"notes": "note", "reminders_manage": "reminder", "calendar_manage": "event"}[name]
        target = a.get("note") or a.get("reminder") or a.get("event") or ""
        return Danger("delete", f"delete a {what}", [str(target)])
    if name == "notes" and action in ("replace", "move"):
        return Danger("change", "replace a note's text" if action == "replace" else "move a note",
                      [str(a.get("note") or "")])
    if name == "calendar_manage" and action in ("move", "update", "rename"):
        return Danger("change", "change a calendar event (invitees may be told)", [str(a.get("event") or "")])
    if name == "notifications" and action == "clear_all":
        return Danger("delete", "clear all your notifications", [])
    if name == "clipboard" and action in ("clear", "forget_secret"):
        return Danger("delete", "clear the clipboard history" if action == "clear" else "forget a saved secret", [])
    if name in ("delete_skill", "forget") or (name == "automation" and action == "delete") or \
            (name == "connector" and action == "remove"):
        what = {"delete_skill": "a skill", "forget": "a memory", "automation": "an automation",
                "connector": "a connector"}[name]
        return Danger("delete", f"delete {what}", [str(a.get("name") or a.get("what") or "")])
    if name == "undo" and action in ("last", "redo"):
        return Danger("change", "undo what it did" if action == "last" else "redo it",
                      [str(a.get("match") or "the last change")])
    if name == "update" and action == "install":
        return Danger("system", "install a Mint update and restart Mint", [])
    if name == "run_applescript":
        why, kind = _script_danger(a.get("script"))
        if why:
            lines = [ln.rstrip() for ln in str(a.get("script") or "").strip().splitlines()][:8]
            return Danger(kind, f"run a script that {why}", lines, mono=True)
    if name == "run_shell":                   # only with --allow-shell
        why, kind = shell_danger(a.get("command"))
        if why:
            lines = [ln.rstrip() for ln in str(a.get("command") or "").strip().splitlines()][:8]
            return Danger(kind, f"run a command that {why}", lines, mono=True)
    if name == "press_key":
        combo = "+".join(sorted([m.lower() for m in _paths(a.get("modifiers"))] if isinstance(a.get("modifiers"), list)
                                else [m for m in re.split(r"[+, ]+", str(a.get("modifiers") or "").lower()) if m])
                         + [str(a.get("key") or "").lower()])
        combo = combo.replace("command", "cmd").replace("alt", "option")
        for keys, why in _KEYS.items():
            if sorted(keys.split("+")) == sorted(combo.split("+")):
                return Danger("delete", f"press {keys.replace('cmd', '⌘').replace('+', '')}, which {why}", [])
    if name in ("ui_act", "click_text", "menu", "browser", "pointer", "click_at"):
        if name == "browser" and action not in ("click",):
            return None
        if name == "ui_act" and action not in ("click", "double_click", ""):
            return None
        if name == "menu" and action not in ("click", ""):
            return None
        label = str(a.get("target") or a.get("text") or a.get("path") or "")
        m = _LABEL.search(label)
        if m:
            where = f" in {a.get('app')}" if a.get("app") else ""
            return Danger("delete", f"click “{label[:60]}”{where}", [])
    if name == "mac":
        control, value = str(a.get("control") or "").lower(), str(a.get("value") or "").lower()
        if control in ("wifi", "bluetooth") and value in ("off", "false", "0", "disable"):
            return Danger("system", f"turn {control.replace('wifi', 'Wi-Fi').title()} off (a phone connection to "
                                    f"this Mac may drop)", [])
    return None


def level() -> str:
    try:
        from mint.core import prefs
        return str(prefs.get("guard") or "all")
    except Exception:
        return "all"


def wanted(danger: Danger | None) -> bool:
    if danger is not None and danger.kind == "protect":
        return True                       # Mint's own files: at every level, "off" too
    lv = level()
    return danger is not None and lv != "off" and (lv == "all" or danger.kind in ("delete", "system"))


# --- asking ----------------------------------------------------------------------------------------------

@dataclass
class Pending:
    ident: str
    danger: Danger
    who: str                        # "Mint" / "Astra (helper agent)"
    phone: bool
    event: threading.Event = field(default_factory=threading.Event)
    answer: bool | None = None
    how: str = ""
    started: float = field(default_factory=time.monotonic)
    timeout: float = WAIT_MAC


_pending: dict = {}
_lock = threading.Lock()
_ids = itertools.count(1)


def current() -> Pending | None:
    with _lock:
        return next(iter(_pending.values()), None)


def answer(ident: str, yes: bool, how: str = "button") -> bool:
    with _lock:
        p = _pending.get(ident)
    if p is None or p.event.is_set():
        return False
    p.answer, p.how = bool(yes), how
    p.event.set()
    return True


def heard(text: str) -> bool:
    """The user's words while a question is open: "yes" / "no" answer it; with several open, "yes to all" /
    "no to all" answers every one."""
    p = current()
    if p is None or not text:
        return False
    if ALL.search(text) and (NO.search(text) or YES.search(text)):
        with _lock:
            open_now = [q for q in _pending.values() if not q.event.is_set()]
        if len(open_now) > 1:
            yes = not NO.search(text)
            return any([answer(q.ident, yes, "voice") for q in open_now])
    if NO.search(text):
        return answer(p.ident, False, "voice")
    if YES.search(text):
        return answer(p.ident, True, "voice")
    return False


def _from_phone() -> bool:
    try:
        from mint.app import telegram
        return telegram.bridge.request is not None
    except Exception:
        return False


def _away() -> bool:
    try:
        from mint.app.telegram_agents import _away as away
        return away()
    except Exception:
        return False


# --- the circuit breaker: three "no"s for one request and the guard stops asking ------------------------

_refused: dict = {}          # request key -> (refusals in a row, when the last one was)


def _request_key(who: str) -> str:
    """One request: the user's words it came from (a background job's own task), else who is asking."""
    try:
        from mint.app import live
        words = live.request() or ""
    except Exception:
        words = ""
    return " ".join(words.lower().split())[:200] or f"who:{who}"


def tripped(key: str) -> bool:
    with _lock:
        count, at = _refused.get(key, (0, 0.0))
    return count >= BREAKER and time.monotonic() - at < BREAKER_WINDOW


def _tally(key: str, ok: bool) -> None:
    now = time.monotonic()
    with _lock:
        if ok:
            _refused.pop(key, None)
            return
        count, at = _refused.get(key, (0, 0.0))
        _refused[key] = ((count if now - at < BREAKER_WINDOW else 0) + 1, now)
        while len(_refused) > 50:
            _refused.pop(min(_refused, key=lambda k: _refused[k][1]))


STOP_TASK = ("NOT DONE, and the guard will not ask again: the user turned down {n} of these in a row for this "
             "request. Stop this task now - don't retry it or try another way. Ask the user in one short sentence "
             "what they would like to do instead.")


def ask(danger: Danger, who: str = "Mint", timeout: float | None = None) -> bool:
    """Blocking: show the question (card at the Mac, Telegram when from the phone or away) and wait.
    After BREAKER refusals in a row for the same request: False at once, nothing shown."""
    key = _request_key(who)
    if tripped(key):
        print(f"  [guard: not asking again ({BREAKER} refusals for this request): {danger.title}]", flush=True)
        _audit("skip", f"{who}: {danger.title} (after {BREAKER} refusals)")
        return False
    phone = _from_phone()
    mail = _from_email()
    p = Pending(ident=format(next(_ids), "x"), danger=danger, who=who, phone=phone or mail or _away())
    p.timeout = timeout or (WAIT_PHONE if p.phone else WAIT_MAC)
    with _lock:
        _pending[p.ident] = p
    print(f"  [guard: {who} wants to {danger.title} - asking]", flush=True)
    _audit("ask", f"{who}: {danger.title} | " + " | ".join(map(str, danger.lines[:3])))
    try:
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_card_show, p)
        except Exception:
            log.debug("no guard card", exc_info=True)
        if p.phone:
            threading.Thread(target=_telegram_ask, args=(p,), daemon=True).start()
        if mail:
            threading.Thread(target=_email_ask, args=(p,), daemon=True).start()
        deadline = time.monotonic() + p.timeout
        while not p.event.wait(0.25):
            if time.monotonic() > deadline:
                p.how = "no answer"
                break
            try:
                from mint.app import control
                if control.stopped():
                    p.answer, p.how = False, "stop"
                    break
            except Exception:
                pass
        ok = bool(p.answer)
    finally:
        with _lock:
            _pending.pop(p.ident, None)
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_card_done, p)
        except Exception:
            pass
        _telegram_done(p)
        if mail:
            _email_done(p)
    print(f"  [guard: {'yes' if ok else 'no'} ({p.how or 'answered'})]", flush=True)
    _audit("answer", f"{'yes' if ok else 'no'} via {p.how or '?'}: {danger.title}")
    _tally(key, ok)
    return ok


async def check(name: str, args: dict, who: str = "Mint") -> str:
    """'' to go ahead, else what to tell the model (the user said no, or didn't answer)."""
    danger = assess(name, args)
    if not wanted(danger):
        return ""
    import asyncio
    ok = await asyncio.to_thread(ask, danger, who)
    if ok:
        return ""
    if tripped(_request_key(who)):
        return STOP_TASK.format(n=BREAKER)
    return (f"NOT DONE: the guard asked the user before you {danger.title}, and they said no or didn't answer. "
            "Nothing was changed. Tell them in one short sentence that you didn't do it; don't try another way.")


def check_sync(name: str, args: dict, who: str) -> str:
    danger = assess(name, args)
    if not wanted(danger):
        return ""
    if ask(danger, who):
        return ""
    if tripped(_request_key(who)):
        return STOP_TASK.format(n=BREAKER)
    return f"NOT DONE: the user didn't allow it ({danger.title}). Nothing was changed; don't try another way."


def _audit(kind: str, text: str) -> None:
    try:
        path = Path.home() / "Library" / "Application Support" / "Mint" / "guard.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {kind}: {text[:400]}\n")
    except OSError:
        pass


# --- email ---------------------------------------------------------------------------------------------

def _from_email() -> bool:
    try:
        from mint.app import email_remote
        return email_remote.from_email()
    except Exception:
        return False


def _email_ask(p: Pending) -> None:
    try:
        from mint.app import email_remote
        email_remote.guard_ask(p)
    except Exception:
        log.exception("guard email ask")


def _email_done(p: Pending) -> None:
    try:
        from mint.app import email_remote
        email_remote.guard_done(p)
    except Exception:
        log.debug("guard email done", exc_info=True)


# --- Telegram ------------------------------------------------------------------------------------------

_tg_msgs: dict = {}


def _telegram_ask(p: Pending) -> None:
    try:
        from mint.app import telegram
        b = telegram.bridge
        if b.bot is None or not b._state.get("user_id"):
            return
        esc = telegram.esc
        icon = {"delete": "🗑", "change": "✏️"}.get(p.danger.kind, "⚠️")
        body = "\n".join(esc(line) for line in p.danger.lines[:8])
        if body:
            body = f"\n<pre>{body}</pre>" if p.danger.mono else "\n" + body
        mins = int(p.timeout // 60)
        text = (f"{icon} <b>{esc(p.who)} wants to {esc(p.danger.title)}</b>{body}\n\n"
                f"<i>Nothing happens unless you say yes (within {mins} min).</i>")
        _tg_msgs[p.ident] = None                 # on Telegram from now: counted for "Allow all"
        sent = b.html(text, markup=_tg_markup(p.ident))
        if sent and sent.get("message_id"):
            _tg_msgs[p.ident] = sent["message_id"]
            _tg_refresh(skip=p.ident)
        else:
            _tg_msgs.pop(p.ident, None)
    except Exception:
        _tg_msgs.pop(p.ident, None)
        log.exception("guard telegram ask")


def _tg_open() -> list[str]:
    """The questions open on Telegram right now."""
    with _lock:
        return [i for i in list(_tg_msgs) if i in _pending and not _pending[i].event.is_set()]


def _tg_markup(ident: str) -> dict:
    """Yes / No - and "Allow all (N)" while two or more questions wait (parallel jobs)."""
    from mint.app import telegram
    rows = [[("✅ Yes, do it", f"gd:{ident}:y"), ("❌ No", f"gd:{ident}:n")]]
    waiting = len(_tg_open())
    if waiting > 1:
        rows.append([(f"✅ Allow all ({waiting})", "gd:all:y")])
    return telegram.keys(*rows)


def _tg_refresh(skip: str = "") -> None:
    """The other open questions get, update or lose their "Allow all" button as the count changes."""
    try:
        from mint.app import telegram
        b = telegram.bridge
        for ident in _tg_open():
            mid = _tg_msgs.get(ident)
            if ident != skip and mid:
                b._call("editMessageReplyMarkup", patient=False, chat_id=b._chat(), message_id=mid,
                        reply_markup=_tg_markup(ident))
    except Exception:
        log.debug("guard telegram refresh", exc_info=True)


def _telegram_done(p: Pending) -> None:
    mid = _tg_msgs.pop(p.ident, None)
    if not mid:
        return
    _tg_refresh()
    try:
        from mint.app import telegram
        b = telegram.bridge
        label = "✅ Allowed" if p.answer else ("⌛ No answer - not done" if p.how == "no answer" else "❌ Not done")
        b._call("editMessageReplyMarkup", patient=False, chat_id=b._chat(), message_id=mid,
                reply_markup={"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]})
    except Exception:
        log.debug("guard telegram done", exc_info=True)


def telegram_plan(arg: str):
    """telegram.py's button handler for gd:<id>:y|n -> (toast, act, used label)."""
    ident, _, choice = arg.partition(":")
    if ident == "all":                      # "Allow all (N)": every question open on Telegram
        idents = _tg_open()
        if not idents:
            return "Those questions are closed.", None, ""
        return (f"✅ Doing all {len(idents)}", (lambda: [answer(i, True, "telegram") for i in idents]),
                "✅ Allowed all")
    with _lock:
        p = _pending.get(ident)
    if p is None:
        return "That question is closed.", None, ""
    yes = choice == "y"
    return ("✅ Doing it" if yes else "❌ Not doing it"), (lambda: answer(ident, yes, "telegram")), \
        ("✅ Allowed" if yes else "❌ Not done")


# --- the card at the Mac --------------------------------------------------------------------------------

card = None            # (notch.py dresses `card.panel` as part of the notch while it shows)


class _GuardCard:
    W = 440

    def __init__(self) -> None:
        import AppKit

        from mint.ui import gfx
        from mint.ui.notch_agents import MintAgentAct, _attr, _button_like, _label
        self.AppKit = AppKit
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, self.W, 160),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered, False)
        panel.setLevel_(AppKit.NSStatusWindowLevel + 1)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        try:
            from mint.ui import effects
            panel.setSharingType_(effects.SHARING)
        except Exception:
            pass
        root = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.W, 160))
        root.setWantsLayer_(True)
        root.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(0.04, 0.98).CGColor())
        root.layer().setCornerRadius_(20)
        root.layer().setBorderWidth_(1)
        root.layer().setBorderColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.08).CGColor())
        panel.setContentView_(root)
        self.panel, self.root, self.gfx, self._attr = panel, root, gfx, _attr
        import Quartz
        self.Quartz = Quartz
        self.badge = Quartz.CALayer.layer()
        self.badge.setBounds_(Quartz.CGRectMake(0, 0, 34, 34))
        self.badge.setCornerRadius_(17)
        root.layer().addSublayer_(self.badge)
        self.glyph = Quartz.CALayer.layer()
        self.glyph.setBounds_(Quartz.CGRectMake(0, 0, 18, 18))
        self.glyph_mask = Quartz.CALayer.layer()
        self.glyph_mask.setFrame_(Quartz.CGRectMake(0, 0, 18, 18))
        self.glyph_mask.setContentsGravity_(Quartz.kCAGravityResizeAspect)
        self.glyph.setMask_(self.glyph_mask)
        self.glyph.setBackgroundColor_(gfx.cg((1, 1, 1)))
        self.badge.addSublayer_(self.glyph)
        self.title = _label(root, 13.5, AppKit.NSFontWeightSemibold, 0.97, lines=2)
        self.body = _label(root, 11.5, AppKit.NSFontWeightRegular, 0.7, lines=8)
        self.hint = _label(root, 10.5, AppKit.NSFontWeightMedium, 0.4)
        self.track = Quartz.CALayer.layer()
        self.track.setBackgroundColor_(gfx.cg((1, 1, 1), 0.1))
        self.track.setCornerRadius_(1)
        root.layer().addSublayer_(self.track)
        self.bar = Quartz.CALayer.layer()
        self.bar.setAnchorPoint_(Quartz.CGPointMake(0, 0.5))
        self.bar.setCornerRadius_(1)
        root.layer().addSublayer_(self.bar)
        self.acts = [MintAgentAct.alloc().initWithFn_(lambda: self._press(False)),
                     MintAgentAct.alloc().initWithFn_(lambda: self._press(True))]
        self.no = _button_like(root, "Don't", self.acts[0], primary=False)
        self.yes = _button_like(root, "Yes, do it", self.acts[1], primary=True)
        self.pending = None

    def _press(self, yes: bool) -> None:
        if self.pending is not None:
            answer(self.pending.ident, yes, "click")

    def show(self, p: Pending) -> None:
        AppKit, Quartz, gfx = self.AppKit, self.Quartz, self.gfx
        self.pending = p
        d = p.danger
        rgb = {"delete": (1.0, 0.36, 0.36), "change": (1.0, 0.72, 0.26)}.get(d.kind, (1.0, 0.55, 0.3))
        W, pad = self.W, 18
        self.title.setStringValue_(f"{p.who} wants to {d.title}")
        if d.mono:
            self.body.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(10.5, AppKit.NSFontWeightRegular))
        else:
            self.body.setFont_(AppKit.NSFont.systemFontOfSize_weight_(11.5, AppKit.NSFontWeightRegular))
        self.body.setStringValue_("\n".join(str(x) for x in d.lines[:8]))
        with _lock:
            waiting = sum(1 for q in _pending.values() if not q.event.is_set())
        self.hint.setStringValue_("Say “yes” or “no”" + (f" · “yes to all” for all {waiting}" if waiting > 1 else "")
                                  + (" · also asked on Telegram" if p.phone else ""))
        text_w = W - 2 * pad - 46
        self.title.setFrameSize_(AppKit.NSMakeSize(text_w, 40))
        title_h = min(40, self.title.cell().cellSizeForBounds_(AppKit.NSMakeRect(0, 0, text_w, 400)).height)
        body_h = 0.0
        if d.lines:
            body_h = min(130, self.body.cell().cellSizeForBounds_(AppKit.NSMakeRect(0, 0, text_w, 400)).height + 2)
        H = max(110, 18 + title_h + 6 + body_h + 14 + 26 + 18)
        self.panel.setContentSize_(AppKit.NSMakeSize(W, H))
        self.root.setFrame_(AppKit.NSMakeRect(0, 0, W, H))
        top = H - 18
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.badge.setPosition_(Quartz.CGPointMake(pad + 17, top - 17))
        self.badge.setBackgroundColor_(gfx.cg(rgb, 0.22))
        self.badge.setBorderWidth_(1)
        self.badge.setBorderColor_(gfx.cg(rgb, 0.5))
        self.glyph.setPosition_(Quartz.CGPointMake(17, 17))
        self.glyph_mask.setContents_(gfx.symbol(d.icon, 18, "bold"))
        self.glyph.setBackgroundColor_(gfx.cg(gfx.light(rgb)))
        self.track.setFrame_(Quartz.CGRectMake(pad, 8, W - 2 * pad, 2))
        self.bar.setBounds_(Quartz.CGRectMake(0, 0, W - 2 * pad, 2))
        self.bar.setPosition_(Quartz.CGPointMake(pad, 9))
        self.bar.setBackgroundColor_(gfx.cg(rgb, 0.8))
        Quartz.CATransaction.commit()
        x = pad + 46
        self.title.setFrame_(AppKit.NSMakeRect(x, top - title_h, text_w, title_h))
        self.body.setFrame_(AppKit.NSMakeRect(x, top - title_h - 6 - body_h, text_w, body_h))
        self.body.setHidden_(not d.lines)
        bx = W - pad
        for b in (self.yes, self.no):
            bx -= b.frame().size.width
            b.setFrameOrigin_(AppKit.NSMakePoint(bx, 18))
            bx -= 8
        self.yes.layer().setBackgroundColor_(gfx.cg(rgb if d.kind == "delete" else (0.96, 0.96, 0.97)))
        self.yes.setAttributedTitle_(self._attr([("Yes, do it", AppKit.NSFont.systemFontOfSize_weight_(
            12, AppKit.NSFontWeightSemibold), AppKit.NSColor.colorWithWhite_alpha_(0.06, 1), None)]))
        self.hint.setFrame_(AppKit.NSMakeRect(pad, 22, bx - pad - 4, 15))
        # the countdown: the bar shrinks to nothing as the time to answer runs out
        shrink = Quartz.CABasicAnimation.animationWithKeyPath_("bounds.size.width")
        shrink.setFromValue_(W - 2 * pad)
        shrink.setToValue_(0.0)
        shrink.setDuration_(p.timeout)
        shrink.setFillMode_(Quartz.kCAFillModeForwards)
        shrink.setRemovedOnCompletion_(False)
        self.bar.addAnimation_forKey_(shrink, "countdown")
        self._place()
        panel = self.panel
        if not panel.isVisible():
            panel.setAlphaValue_(0.0)
            panel.orderFrontRegardless()
            AppKit.NSAnimationContext.beginGrouping()
            AppKit.NSAnimationContext.currentContext().setDuration_(0.22)
            panel.animator().setAlphaValue_(1.0)
            AppKit.NSAnimationContext.endGrouping()
            pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
            pop.setFromValue_(0.9)
            pop.setToValue_(1.0)
            pop.setDamping_(12)
            pop.setStiffness_(260)
            pop.setDuration_(pop.settlingDuration())
            self.root.layer().addAnimation_forKey_(pop, "pop")
        attention = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.rotation.z")
        attention.setValues_([0, 0.18, -0.16, 0.1, -0.06, 0])
        attention.setDuration_(0.6)
        self.glyph.addAnimation_forKey_(attention, "wiggle")
        try:
            AppKit.NSSound.soundNamed_("Funk").play()
        except Exception:
            pass

    def _place(self) -> None:
        AppKit = self.AppKit
        frame = self.panel.frame()
        try:
            from mint.ui import notch
            from mint.core import prefs
            if prefs.get("notch_mode") and getattr(notch.notch, "panel", None) is not None:
                n = notch.notch
                self.panel.setFrameOrigin_(AppKit.NSMakePoint(n.cx - frame.size.width / 2,
                                                              n.top - n.nh - 10 - frame.size.height))
                return
            hud = notch.notch.hud
            window = getattr(hud, "_orb_window", None)
            area = AppKit.NSScreen.mainScreen().visibleFrame()
            if window is not None and window.isVisible():
                f = window.frame()
                x = min(max(area.origin.x + 12, f.origin.x + f.size.width / 2 - frame.size.width / 2),
                        area.origin.x + area.size.width - frame.size.width - 12)
                above = f.origin.y + f.size.height + 6
                y = above if above + frame.size.height < area.origin.y + area.size.height - 8 else \
                    f.origin.y - frame.size.height - 6
                self.panel.setFrameOrigin_(AppKit.NSMakePoint(x, y))
                return
            self.panel.setFrameOrigin_(AppKit.NSMakePoint(area.origin.x + area.size.width / 2 - frame.size.width / 2,
                                                          area.origin.y + area.size.height - frame.size.height - 40))
        except Exception:
            log.debug("guard card place", exc_info=True)

    def hide(self, p: Pending) -> None:
        if self.pending is not p:
            return
        self.pending = None
        AppKit = self.AppKit
        panel = self.panel
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.16)
        panel.animator().setAlphaValue_(0.0)
        AppKit.NSAnimationContext.endGrouping()
        from PyObjCTools import AppHelper
        AppHelper.callLater(0.2, lambda: self.pending is None and panel.orderOut_(None))


def _card_show(p: Pending) -> None:
    global card
    try:
        if card is None:
            card = _GuardCard()
        if not p.event.is_set():
            card.show(p)
    except Exception:
        log.exception("guard card failed")


def _card_done(p: Pending) -> None:
    if card is not None:
        try:
            card.hide(p)
        except Exception:
            log.debug("guard card hide", exc_info=True)
