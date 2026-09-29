"""Talking to the running Mint: send a request with `mint --say`, then follow mint.log until it is done.

There is no machine-readable "request finished" line in the log (see README, "Hooks"), so a turn
counts as finished when, after Mint logged the request ("[typed: …]" or "[request: …]"):
  - it has spoken a reply ("mint: …") and nothing significant was logged for QUIET seconds
    (autopilot re-prompts ~2 s after a turn ends, so QUIET must be longer than that), or
  - nothing significant was logged for IDLE_CAP seconds (a silent turn), or
  - the turn timeout passed ("timeout").
Orb, island, wake-word and model-warning lines are noise and do not count as activity.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
RUNTIME = Path(os.environ.get("MINT_RUNTIME", HOME / "Library/Application Support/Mint"))
PY = Path(os.environ.get("MINT_PY", RUNTIME / ".venv/bin/python"))
LOG = Path(os.environ.get("MINT_LOG", HOME / "Library/Logs/Mint/mint.log"))

QUIET = 7.0            # seconds of no activity after a reply = done
IDLE_CAP = 45.0        # seconds of no activity at all (no reply either) = done, silently
RECEIPT_TIMEOUT = 60.0  # Mint may be unloaded (Mint Ear starts it again), so allow a while

# Every tool Mint declares (tools.tools(), 114 on 2026-09-29, plus create_event once its declaration lands).
# Lines "[name] result" with these names are tool calls; other bracketed lines are Mint's own status.
TOOLS = set("""
open_app open_url open_folder scroll press_key set_volume media_key frontmost_app list_windows quit_app type_text
get_selected_text get_status set_timer notify system_action calendar_events create_reminder create_event create_note compose_email
list_emails read_email open_chrome open_slack list_accounts read_window list_open switch_to export_doc_pdf plan_task
step_done create_pdf run_routine desktop set_preference chat_action show_chat stop_listening look ui_act ui_elements
click_text click_at find_skill skill_result create_skill update_skill list_skills delete_skill remember recall
update_memory forget list_memories memory_used show_skills_and_memory show_on_screen mark_area clear_marks move_orb
screen_share_visibility express set_voice list_agents delegate_task delegate_tasks agent_status message_agent
answer_agent stop_agent create_agent wait_until_done preview_site edit_selection make_spreadsheet tidy teach tutor
meeting briefing shortcut find_screenshot translate_screen mail mac screen_record track show_card dictation edit_video
video_info ocr_copy data_to_sheet convert_document task recall_history automation watch_video screenshot clipboard
read_file write_file find_files file_action web_search read_url browser scroll_to menu wait_for_text pointer quit_mint
run_applescript fix_hearing
""".split())

_STAMPED = re.compile(r"^\s{2}(\d\d:\d\d:\d\d) (.*)$")
_TOOL = re.compile(r"^\[([a-z_]+)\] ?(.*)$", re.S)
_NOISE = re.compile(
    r"^\[(island|orb|awake|asleep|Hey Mint|ignored|paused|resumed|learning|silent|spoken replies|"
    r"dropped on Mint|goodbye|you interrupted|stop_listening)|^\(not for me")
_NOISE_UNSTAMPED = re.compile(r"^\s*\[(orb|learning|island)|google_genai|AFC|^\s*$")
_ERROR_RESULT = re.compile(r"^(FAILED|REFUSED|NOT RUN|NOT SENT|ERROR|Could not|Error)\b")
_ERROR_LINE = re.compile(r"Traceback|\bERROR\b|session dropped|tool batch failed|server cancelled|"
                         r"reconnect keeps failing|session ended")


@dataclass
class Turn:
    say: str
    status: str = "pending"            # done | done-silent | timeout | not-received | sent (no wait)
    seconds: float = 0.0
    received: bool = False
    reply: str = ""
    tools: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    other_requests: list[str] = field(default_factory=list)   # requests from someone else meanwhile
    lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"say": self.say, "status": self.status, "seconds": round(self.seconds, 1),
                "received": self.received, "reply": self.reply, "tools": self.tools, "errors": self.errors,
                "other_requests": self.other_requests}


def log_size() -> int:
    try:
        return LOG.stat().st_size
    except OSError:
        return 0


def read_from(offset: int) -> str:
    try:
        with open(LOG, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size < offset:              # the log was rotated or truncated
                offset = 0
            f.seek(offset)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def send(text: str) -> None:
    subprocess.run([str(PY), "-m", "mint", "--say", text], cwd=str(RUNTIME), check=True,
                   capture_output=True, timeout=30)


def mint_running() -> bool:
    """Is Mint's Python loaded (not just Mint Ear)?"""
    out = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    return any(" -m mint" in line and not any(flag in line for flag in ("--say", "--script", "--tool", "--keys"))
               for line in out.splitlines())


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def parse(text: str, turn: Turn, said: str) -> dict:
    """Fold new log text into `turn`. Returns {"activity": bool, "reply": bool, "receipt": bool}."""
    seen = {"activity": False, "reply": False, "receipt": False, "done": False}
    want = _norm(said)[:60]
    current_tool: dict | None = None
    for raw in text.splitlines():
        turn.lines.append(raw)
        stamped = _STAMPED.match(raw)
        if not stamped:
            if current_tool is not None and raw.strip() and not raw.startswith("  ["):
                current_tool["result"] = (current_tool["result"] + "\n" + raw)[:600]
                continue
            if _ERROR_LINE.search(raw) and "WARNING google_genai" not in raw:
                turn.errors.append(raw.strip()[:300])
            if not _NOISE_UNSTAMPED.search(raw):
                seen["activity"] = seen["activity"] or bool(raw.strip())
            continue
        current_tool = None
        clock, body = stamped.groups()
        if body.startswith("[typed: ") or body.startswith("[request: "):
            got = _norm(body.split(": ", 1)[1].rstrip("]"))
            if want and (got.startswith(want[:40]) or want.startswith(got[:40])):
                seen["receipt"] = True
            elif not body.startswith("[request: "):
                turn.other_requests.append(body[:160])
            seen["activity"] = True
            continue
        if body.startswith("[typed while reconnecting"):
            if want[:30] in _norm(body):
                seen["receipt"] = True
            seen["activity"] = True
            continue
        if body.startswith("you:"):
            turn.other_requests.append(body[:160])      # someone spoke to Mint during the run
            seen["activity"] = True
            continue
        if body.strip() == "[done]":
            seen["done"] = True                          # Mint says the request is finished
            continue
        if body.startswith("mint:"):
            turn.reply = (turn.reply + " " + body[5:].strip()).strip()
            seen["reply"] = seen["activity"] = True
            continue
        tool = _TOOL.match(body)
        if tool and tool.group(1) in TOOLS:
            name, result = tool.groups()
            current_tool = {"t": clock, "name": name, "result": result[:600]}
            turn.tools.append(current_tool)
            if _ERROR_RESULT.match(result.strip()):
                turn.errors.append(f"{name}: {result.strip()[:240]}")
            seen["activity"] = True
            continue
        if _NOISE.match(body):
            continue
        if _ERROR_LINE.search(body):
            turn.errors.append(body[:300])
        seen["activity"] = True                          # [task], [autopilot], [compacting], …
    return seen


def run_turn(say: str, timeout: float, quiet: float = QUIET, idle_cap: float = IDLE_CAP,
             wait: bool = True, on_poll=None) -> Turn:
    """Send one request and follow the log until it is finished (see the module doc)."""
    turn = Turn(say=say)
    offset = log_size()
    started = time.monotonic()
    send(say)
    if not wait:
        turn.status = "sent"
        return turn
    last_activity = started
    replied = False
    finished = False
    buffer = ""
    while True:
        time.sleep(0.5)
        now = time.monotonic()
        chunk = read_from(offset)
        if chunk:
            offset += len(chunk.encode("utf-8"))
            # Only whole lines: a line being written stays for the next poll.
            buffer += chunk
            complete, _, buffer = buffer.rpartition("\n")
            if complete:
                seen = parse(complete + "\n", turn, say)
                turn.received = turn.received or seen["receipt"]
                if seen["activity"]:
                    last_activity = now
                replied = replied or (turn.received and seen["reply"])
                finished = finished or (turn.received and seen["done"])
        if on_poll:
            on_poll()
        turn.seconds = now - started
        if not turn.received:
            if turn.seconds > RECEIPT_TIMEOUT:
                turn.status = "not-received"
                return turn
            continue
        if finished and now - last_activity >= 1.5:
            turn.status = "done"
            return turn
        if replied and now - last_activity >= quiet:
            turn.status = "done"
            return turn
        if now - last_activity >= idle_cap:
            turn.status = "done-silent"
            return turn
        if turn.seconds > timeout:
            turn.status = "timeout"
            return turn


def stop_and_settle(settle: float = 4.0, cap: float = 30.0) -> list[str]:
    """Say "stop", then wait until the log has been quiet for `settle` seconds."""
    offset = log_size()
    send("stop")
    started = last = time.monotonic()
    lines: list[str] = []
    while time.monotonic() - started < cap:
        time.sleep(0.5)
        chunk = read_from(offset)
        if chunk:
            offset += len(chunk.encode("utf-8"))
            for raw in chunk.splitlines():
                lines.append(raw)
                stamped = _STAMPED.match(raw)
                if stamped and not _NOISE.match(stamped.group(2)):
                    last = time.monotonic()
        if time.monotonic() - last >= settle:
            break
    return lines
