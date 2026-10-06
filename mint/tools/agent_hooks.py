"""Claude Code -> Mint, through Claude Code's own hooks: approvals from the notch, and instant updates.

agent_watch.py already sees every session from its log. What a log can't do is let you answer a
permission request ("Claude wants to run npm test") from the notch. Claude Code's PermissionRequest
hook can: Claude Code runs our small hook script, which asks Mint over a private Unix socket and prints
Mint's answer. Claude Code shows its own prompt at the same time and takes whichever answer comes
first, so Mint never holds anything up. If Mint is not running, the script prints nothing and exits:
Claude Code just asks in the terminal as usual.

The hooks are opt-in (Settings > Agents > Connect Claude Code). Installing them:
  * writes the script to ~/Library/Application Support/Mint/hooks/
  * backs ~/.claude/settings.json up next to itself (settings.json.mint-backup-YYYYmmdd-HHMMSS)
  * merges Mint's entries into its "hooks" (nothing else is touched; Mint's old entries are replaced)
and shows exactly what will be added before writing. Removing them takes out only Mint's entries.

Events used: PermissionRequest (waits for your click), Notification (it needs you), UserPromptSubmit and
Stop (instant start / end), SessionStart (which terminal it runs in, so "Open" goes to the right window).

The socket is in Mint's Application Support folder (mode 600, folder 700) and only takes connections
from this user (getpeereid). Nothing is sent anywhere else. Decisions are made only by a click on the
notch's buttons; Mint itself never approves anything.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import select
import shutil
import socket
import threading
import time
import uuid

log = logging.getLogger("mint.tools.agent_hooks")

SUPPORT = os.path.expanduser("~/Library/Application Support/Mint")
SOCK = os.path.join(SUPPORT, "agents.sock")
HOOK_DIR = os.path.join(SUPPORT, "hooks")
HOOK_SH = os.path.join(HOOK_DIR, "mint-agent-hook")
HOOK_PY = os.path.join(HOOK_DIR, "mint_agent_hook.py")
SETTINGS = os.path.expanduser("~/.claude/settings.json")
MARK = "mint-agent-hook"                     # how Mint's own entries are recognised in settings.json
WAIT = 590                                   # seconds an approval stays answerable from the notch
EVENTS = (("PermissionRequest", WAIT + 10), ("Notification", 5), ("UserPromptSubmit", 5), ("Stop", 5),
          ("SessionStart", 5), ("SessionEnd", 5), ("PreToolUse", 5))
MATCHERS = {"PreToolUse": "Bash"}            # the guard looks at shell commands only (cheap: no other tool runs it)

_pending: dict = {}           # approval id -> {"conn", "session", "suggestions", "created"}
_lock = threading.Lock()
_started = False


def _guard_patterns():
    from mint.core.guard import _SHELL
    return _SHELL


# --- the hook script (runs inside Claude Code's hook, under any python3 it finds) ---------------------

HOOK_SCRIPT = r'''#!/usr/bin/env python3
# Mint's Claude Code hook. Forwards the event to Mint over a local socket; for a permission request it
# waits for your click in the notch and prints the decision. Never blocks: no Mint, no answer, no output.
import json, os, re, socket, subprocess, sys

SOCK = os.path.expanduser("~/Library/Application Support/Mint/agents.sock")
SETTINGS = os.path.expanduser("~/Library/Application Support/Mint/settings.json")
GUARD = %(GUARD)s
# Mint's own files and Claude Code's / Codex's settings: a command that deletes or writes over them always asks,
# even with the guard off (the same floor as Mint's own guard).
OWN = re.compile(r"Application(\\ | )Support/Mint\b|\bMint\.app\b|(~|\$\{?HOME\}?|/Users/[^/\s]+)/\.(claude|codex)\b|"
                 r"\.claude\.json\b", re.I)
WRITES = re.compile(r"(^|[;&|`(\s])(rm|rmdir|unlink|shred|srm|trash|mv|truncate|chmod|chown|chflags|sed\s+-i)\s|"
                    r"(^|[^0-9&])>>?\s*\S", re.I)


def guard(payload):
    """Mint's guard (PreToolUse, Bash): a command that deletes or changes things makes Claude Code ask you first,
    even where you allowed it. Local and instant: Mint itself isn't asked."""
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if OWN.search(command) and WRITES.search(command):
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": "Mint's guard: this command deletes or writes over Mint's own files or "
                                        "Claude Code's / Codex's settings. It needs your OK."}}) + "\n")
        return
    try:
        level = (json.load(open(SETTINGS)).get("guard") or "all")
    except Exception:
        level = "all"
    if level == "off":
        return
    for pattern, why, kind in GUARD:
        if (level == "all" or kind != "change") and re.search(pattern, command, re.I | re.M):
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "ask",
                "permissionDecisionReason": "Mint's guard: this command " + why + ". It needs your OK."}}) + "\n")
            return


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read() or b"{}")
    except Exception:
        return
    event = payload.get("hook_event_name", "")
    if event == "PreToolUse":
        guard(payload)
        return
    if not os.path.exists(SOCK):
        return
    env = os.environ
    payload["term_program"] = env.get("TERM_PROGRAM", "")
    payload["bundle_id"] = env.get("__CFBundleIdentifier", "")
    payload["iterm_session_id"] = env.get("ITERM_SESSION_ID", "")
    payload["term_session_id"] = env.get("TERM_SESSION_ID", "")
    payload["pid"] = os.getppid()
    if event in ("SessionStart", "PermissionRequest", "UserPromptSubmit"):
        try:
            tty = subprocess.run(["ps", "-o", "tty=", "-p", str(os.getppid())], capture_output=True,
                                 text=True, timeout=1).stdout.strip()
            if tty and tty not in ("??", "-"):
                payload["tty"] = "/dev/" + tty
        except Exception:
            pass
    wait = event == "PermissionRequest"
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(%(WAIT)d if wait else 0.5)
        s.connect(SOCK)
        s.sendall((json.dumps(payload) + "\n").encode())
        if not wait:
            s.close()
            return
        data, parent = b"", os.getppid()
        s.settimeout(1.0)
        for _ in range(%(WAIT)d):
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                if os.getppid() != parent:
                    return                       # Claude Code went on without us (answered in the terminal)
                continue
            if not chunk:
                break
            data += chunk
            if data.endswith(b"\n"):
                break
        s.close()
        answer = json.loads(data.decode() or "{}")
    except Exception:
        return
    decision = answer.get("decision")
    if decision in ("allow", "always"):
        out = {"behavior": "allow"}
        rule = answer.get("rule")
        if decision == "always" and rule:
            out["updatedPermissions"] = {"add": [rule], "remove": []}
            out["rule"] = rule
    elif decision == "deny":
        out = {"behavior": "deny", "message": "Denied from Mint's notch."}
    else:
        return
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                         "decision": out}}) + "\n")


try:
    main()
except Exception:
    pass
sys.exit(0)
''' % {"WAIT": WAIT, "GUARD": repr([list(x) for x in _guard_patterns()])}

# The command Claude Code runs: /bin/sh, so a missing python never shows an error; Mint's own Python
# first (always there with Mint), then the system's only if developer tools are installed (a bare
# /usr/bin/python3 would pop up the "install command line developer tools" dialog).
HOOK_WRAPPER = r'''#!/bin/sh
# Mint's Claude Code hook (see mint_agent_hook.py next to this). Always exits 0.
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$HOME/Library/Application Support/Mint/.venv/bin/python"
if [ ! -x "$PY" ]; then
  if command -v xcode-select >/dev/null 2>&1 && xcode-select -p >/dev/null 2>&1; then PY=/usr/bin/python3; else exit 0; fi
fi
exec "$PY" "$DIR/mint_agent_hook.py" 2>/dev/null
'''


# --- the socket server ---------------------------------------------------------------------------------

def start() -> None:
    """Listen for the hook (idempotent). Cheap: one thread asleep in accept()."""
    global _started
    if _started:
        return
    _started = True
    if installed():
        _write_script()                      # keep the script in step with this version of Mint
    threading.Thread(target=_serve, name="mint-agent-hooks", daemon=True).start()
    threading.Thread(target=_watch_pending, name="mint-agent-approvals", daemon=True).start()


def _serve() -> None:
    try:
        os.makedirs(SUPPORT, exist_ok=True)
        os.chmod(SUPPORT, 0o700)
    except OSError:
        pass
    try:
        if os.path.exists(SOCK):
            os.unlink(SOCK)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCK)
        os.chmod(SOCK, 0o600)
        server.listen(16)
    except OSError:
        log.exception("agent hook socket failed")
        return
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            time.sleep(1)
            continue
        if not _same_user(conn):
            conn.close()
            continue
        threading.Thread(target=_client, args=(conn,), daemon=True).start()


def _same_user(conn) -> bool:
    try:
        libc = ctypes.CDLL(None)
        uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
        if libc.getpeereid(conn.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            return False
        return uid.value == os.getuid()
    except Exception:
        return False


def _client(conn) -> None:
    conn.settimeout(5)
    data = b""
    try:
        while b"\n" not in data and len(data) < 2_000_000:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        payload = json.loads(data.split(b"\n", 1)[0].decode() or "{}")
    except Exception:
        conn.close()
        return
    if not isinstance(payload, dict):
        conn.close()
        return
    from mint.tools import agent_watch
    if payload.get("hook_event_name") == "PermissionRequest":
        ident = uuid.uuid4().hex[:12]
        payload["_mint_id"] = ident
        suggestions = [x.get("rule") for x in (payload.get("permission_suggestions") or [])
                       if isinstance(x, dict) and x.get("rule")]
        session = "claude:" + str(payload.get("session_id") or "")
        with _lock:
            older = [i for i, item in _pending.items() if item["session"] == session]
        for old in older:                        # a newer request: the older one was answered elsewhere
            _finish(old, None)
        with _lock:
            _pending[ident] = {"conn": conn, "session": "claude:" + str(payload.get("session_id") or ""),
                               "suggestions": suggestions, "created": time.time()}
        print(f"  [agents: Claude Code asks to use {payload.get('tool_name')}]", flush=True)
        agent_watch.ingest_hook(payload)
        return                                   # held open until a click, Claude Code's own answer, or WAIT
    conn.close()
    agent_watch.ingest_hook(payload)


def _watch_pending() -> None:
    """A held approval ends when the hook goes away (answered in the terminal: Claude Code stops the
    hook) or after WAIT. Either way the notch stops offering the buttons."""
    while True:
        time.sleep(0.5)
        with _lock:
            items = list(_pending.items())
        for ident, item in items:
            gone = time.time() - item["created"] > WAIT
            if not gone:
                try:
                    readable, _, _ = select.select([item["conn"]], [], [], 0)
                    if readable and not item["conn"].recv(1, socket.MSG_PEEK):
                        gone = True              # the other end closed
                except OSError:
                    gone = True
            if gone:
                _finish(ident, None)


def decide(session_key: str, decision: str) -> bool:
    """A click on the notch: "allow", "always" or "deny" for that session's open request."""
    if decision not in ("allow", "always", "deny"):
        return False
    with _lock:
        ident = next((i for i, item in _pending.items() if item["session"] == session_key), None)
    if ident is None:
        return False
    return _finish(ident, decision)


def rule_for(session_key: str) -> str:
    """The rule "Always" would add (Claude Code's own first suggestion), or ""."""
    with _lock:
        for item in _pending.values():
            if item["session"] == session_key and item["suggestions"]:
                return item["suggestions"][0]
    return ""


def pending(session_key: str) -> bool:
    with _lock:
        return any(item["session"] == session_key for item in _pending.values())


def _finish(ident: str, decision: str | None) -> bool:
    with _lock:
        item = _pending.pop(ident, None)
    if item is None:
        return False
    sent = False
    if decision:
        answer = {"decision": decision}
        if decision == "always" and item["suggestions"]:
            answer["rule"] = item["suggestions"][0]
        try:
            item["conn"].sendall((json.dumps(answer) + "\n").encode())
            sent = True
        except OSError:
            sent = False
    try:
        item["conn"].close()
    except OSError:
        pass
    from mint.tools import agent_watch
    agent_watch.watcher.approval_closed(item["session"], ident, decision if sent else "")
    if decision:
        print(f"  [agents: {decision} sent to Claude Code]" if sent else "  [agents: Claude Code had already moved on]",
              flush=True)
    return sent


# --- installing into ~/.claude/settings.json -------------------------------------------------------------

def _command() -> str:
    return f'/bin/sh "{HOOK_SH}"'


def _write_script() -> None:
    os.makedirs(HOOK_DIR, exist_ok=True)
    for path, text in ((HOOK_PY, HOOK_SCRIPT), (HOOK_SH, HOOK_WRAPPER)):
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read() == text:
                    continue
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o755)


def _load_settings() -> dict:
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _ours(group) -> bool:
    return isinstance(group, dict) and any(MARK in str(h.get("command") or "")
                                           for h in group.get("hooks") or [] if isinstance(h, dict))


def installed() -> bool:
    try:
        hooks = _load_settings().get("hooks") or {}
    except (OSError, ValueError):
        return False
    groups = hooks.get("PermissionRequest") or [] if isinstance(hooks, dict) else []
    return any(_ours(g) for g in groups)


def _merged(settings: dict, add: bool) -> dict:
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event in list(hooks):
        groups = hooks[event]
        if isinstance(groups, list):
            kept = [g for g in groups if not _ours(g)]
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if add:
        for event, timeout in EVENTS:
            group = {"hooks": [{"type": "command", "command": _command(), "timeout": timeout}]}
            if event in MATCHERS:
                group = {"matcher": MATCHERS[event], **group}
            hooks.setdefault(event, []).append(group)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def preview(add: bool = True) -> str:
    """What connecting (or disconnecting) would change, in plain words, for the confirmation."""
    events = ", ".join(e for e, _ in EVENTS)
    if add:
        return (f"Mint will add one hook command to {len(EVENTS)} Claude Code events ({events}) in "
                f"~/.claude/settings.json:\n\n    {_command()}\n\nYour other settings and hooks stay as they are. "
                f"A backup is saved next to the file first. Claude Code still asks in the terminal as always; "
                f"you can answer either there or in the notch.")
    return "Mint will remove only its own hook entries from ~/.claude/settings.json (a backup is saved first)."


def install() -> str:
    return _apply(True)


def uninstall() -> str:
    return _apply(False)


def _apply(add: bool) -> str:
    try:
        settings = _load_settings()
    except ValueError:
        return "~/.claude/settings.json isn't valid JSON, so Mint left it alone. Fix it in an editor first."
    except OSError as exc:
        return f"Couldn't read ~/.claude/settings.json: {exc}"
    new = _merged(settings, add)
    if new == settings:
        return "Already connected." if add else "Mint's hooks weren't there."
    if add:
        _write_script()
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    backup = ""
    if os.path.exists(SETTINGS):
        backup = SETTINGS + time.strftime(".mint-backup-%Y%m%d-%H%M%S")
        shutil.copy2(SETTINGS, backup)
    tmp = SETTINGS + ".mint-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(new, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, SETTINGS)
    where = f" (backup: {os.path.basename(backup)})" if backup else ""
    print(f"  [agents: Claude Code hooks {'connected' if add else 'removed'}{where}]", flush=True)
    return ("Connected. New Claude Code sessions ask for approval in the notch too." if add
            else "Disconnected. Claude Code is back to how it was.") + where
