"""The Codex agent: OpenAI Codex, run headless, as one of Mint's sub-agents.

Codex plans, writes and runs code by itself, so this is not a model loop like
the other agents - it starts `codex exec` in the run's project folder and turns
its JSON event stream into hub events (thinking, running a command, a file
written, done). It signs in with the user's ChatGPT account.

The CLI must be the one inside the ChatGPT app: an older stand-alone CLI
(0.145) was refused with "The 'gpt-6-luna' model is not supported when using
Codex with a ChatGPT account", while the app's (0.155) ran it.

Changes mid-run ("make the header blue") stop the current turn and resume the
same Codex session with the new instructions; after a run has finished they
resume it too, so follow-ups keep Codex's memory of the project.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path

log = logging.getLogger("mint.agents")

# The ChatGPT app moved its CLI into codex-cli/bin/ (0.158, Sep 2026); the old path is kept for older
# app versions. After that move Mint fell back to a stand-alone 0.145 on PATH, which refuses GPT-6 Luna -
# so the newest CLI found wins, whatever its path.
APP_BINARIES = ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                str(Path.home() / "Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
                "/Applications/ChatGPT.app/Contents/Resources/codex",
                str(Path.home() / "Applications/ChatGPT.app/Contents/Resources/codex"),
                "/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex",
                "/Applications/Codex.app/Contents/Resources/codex")
MIN_VERSION = (0, 155)       # older CLIs answer "gpt-6-luna is not supported when using Codex with a ChatGPT account"
_versions: dict[str, tuple] = {}
TIME_LIMIT = 25 * 60
EFFORT = {"none": "low", "low": "low", "medium": "medium"}   # Codex has no "none"
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next", "dist-cache"}


def version(path: str) -> tuple:
    """(major, minor, patch) of a Codex CLI, from `codex --version` ("codex-cli 0.158.0-alpha.2"); () if unknown."""
    if path not in _versions:
        import subprocess
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
            found = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
            _versions[path] = tuple(int(n) for n in found.groups()) if found else ()
        except (OSError, subprocess.SubprocessError):
            _versions[path] = ()
    return _versions[path]


def binary() -> str | None:
    """The newest Codex CLI on this Mac (the ChatGPT app's, or one on PATH)."""
    found = [p for p in APP_BINARIES if os.access(p, os.X_OK)]
    on_path = shutil.which("codex")
    if on_path and on_path not in found:
        found.append(on_path)
    if not found:
        return None
    return max(found, key=lambda p: (version(p), -found.index(p)))


def problem() -> str:
    """Why Codex cannot run here, or ''."""
    exe = binary()
    if exe is None:
        return "Codex is not installed (it comes with the ChatGPT app). The user needs to install the ChatGPT app."
    found = version(exe)
    if found and found[:2] < MIN_VERSION:
        return (f"The only Codex found ({exe}, version {'.'.join(map(str, found))}) is too old for GPT-6 Luna. "
                "The user needs to update the ChatGPT app (it brings a newer Codex).")
    return ""


def project_folder(root: Path, task: str, run_id: str) -> Path:
    """A fresh, readable folder for a new build: ~/Documents/Mint/agents/codex/<words>-<n>."""
    words = re.findall(r"[a-z0-9]+", task.lower())
    stop = {"a", "an", "the", "build", "make", "create", "with", "and", "for", "of", "to", "in", "on",
            "page", "please", "that", "single", "one", "using", "based", "from", "html", "simple", "website",
            "site", "web", "summarising", "summarizing", "about", "these", "this", "five", "most", "research",
            "findings", "chatgpt", "codex", "provided", "given", "modern", "clean", "responsive", "new"}
    slug = "-".join([w for w in words if w not in stop and len(w) > 2][:4]) or "project"
    # Run numbers start again when Mint restarts: never reuse a folder that has
    # work in it (a new build overwrote the previous site, 24 Sep).
    folder, n = root / slug, 1
    while folder.exists() and any(folder.iterdir()):
        n += 1
        folder = root / f"{slug}-{n}"
    return folder


def _snapshot(folder: Path) -> dict[str, float]:
    found = {}
    for path in folder.rglob("*"):
        if any(part in _SKIP_DIRS or part.startswith(".") for part in path.relative_to(folder).parts):
            continue
        if path.is_file():
            try:
                found[str(path.relative_to(folder))] = path.stat().st_mtime
            except OSError:
                pass
    return found


def _command_words(command: str) -> str:
    """'/bin/zsh -lc "python3 -m http.server"' -> 'python3 -m http.server', short."""
    match = re.match(r"""^\S*(?:zsh|bash|sh)\s+-l?c\s+(['"])(.*)\1$""", command.strip(), re.S)
    inner = match.group(2) if match else command
    first = inner.strip().splitlines()[0] if inner.strip() else inner
    return first[:70]


def build_prompt(task: str, context: str, folder: Path) -> str:
    return (f"{task}\n\n" + (f"Context from the user (via Mint, their voice assistant):\n{context}\n\n" if context else "")
            + f"Work in {folder} (it is your working directory). Make it complete and working - no "
            "placeholders or TODOs. Check your work (open files, run what can be run). Do not start "
            "long-running servers; Mint will serve and open the result itself. Finish with a short "
            "summary: what you built, the main files, and how to open or run it.")


async def run(hub, run, folder: Path) -> tuple[str, str]:
    """Run (or continue) one Codex task. -> (status, result). Emits hub events."""
    exe = binary()
    if exe is None or problem():
        return "failed", problem()
    folder.mkdir(parents=True, exist_ok=True)
    before = _snapshot(folder)
    model = (run.agent.get("models") or ["gpt-6-luna"])[0]
    prompt = build_prompt(run.task, run.context, folder)
    if getattr(run, "thread_id", None) and run.inbox:
        # A follow-up to a finished run: continue the same Codex session.
        prompt = "[Changes from the user, via Mint]\n" + "\n".join(run.inbox)
        run.inbox.clear()
    last_message, error_text, started = "", "", time.monotonic()
    run.model = f"codex/{model}"

    scratch = Path(tempfile.gettempdir()) / f"mint-codex-{run.id}"
    scratch.mkdir(exist_ok=True)
    out_file, err_file = scratch / "last-message.txt", scratch / "stderr.txt"
    while True:
        out_file.unlink(missing_ok=True)
        args = [exe, "exec", "--json", "--skip-git-repo-check", "-s", "workspace-write", "-C", str(folder),
                "-m", model, "-c", f'model_reasoning_effort="{EFFORT.get(run.effort, "low")}"',
                "-o", str(out_file)]
        if getattr(run, "thread_id", None):
            args += ["resume", run.thread_id]
        args.append(prompt)
        log.info("codex: %s", " ".join(args[:-1]))
        # stderr to a file: an unread pipe fills up and stalls a long run.
        with open(err_file, "wb") as err:
            process = await asyncio.create_subprocess_exec(
                *args, cwd=str(folder), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=err, start_new_session=True, limit=16 * 1024 * 1024)
        interrupted = False
        try:
            while True:
                if run.stop or (run.inbox and getattr(run, "thread_id", None)):
                    interrupted = not run.stop
                    _kill(process)
                    break
                if time.monotonic() - started > TIME_LIMIT:
                    _kill(process)
                    return "failed", f"Codex took longer than {TIME_LIMIT // 60} minutes and was stopped."
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), 0.5)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
                message = _event(hub, run, line.decode(errors="replace"))
                if message:
                    if message.startswith("ERROR:"):
                        error_text = message[6:]
                    else:
                        last_message = message
        except asyncio.CancelledError:
            _kill(process)
            raise
        await process.wait()
        if run.stop:
            return "stopped", "Stopped on request."
        if interrupted:
            updates = "\n".join(run.inbox)
            run.inbox.clear()
            prompt = ("[Updated instructions from the user, via Mint - they override the earlier ones where "
                      f"they conflict]\n{updates}\nContinue from where you are.")
            hub.emit("progress", run, "changing course")
            continue
        break

    try:
        stderr = err_file.read_text(errors="replace")
    except OSError:
        stderr = ""
    try:
        final = out_file.read_text().strip()
    except OSError:
        final = ""
    shutil.rmtree(scratch, ignore_errors=True)
    final = final or last_message
    after = _snapshot(folder)
    changed = sorted(p for p, t in after.items() if before.get(p) != t)
    run.files = list(dict.fromkeys(run.files + changed))
    took = int(time.monotonic() - started)
    if process.returncode != 0 and not final:
        reason = error_text or stderr.strip().splitlines()[-1:] or ["no output"]
        reason = reason if isinstance(reason, str) else reason[0]
        return "failed", f"Codex stopped with an error after {took}s: {reason[:300]}"
    files = ", ".join(changed[:8]) + (f" and {len(changed) - 8} more" if len(changed) > 8 else "")
    page = next((p for p in changed if p.endswith("index.html")), None) or \
        next((p for p in changed if p.endswith(".html")), None)
    show = f" To show it: preview_site with path {folder / page}." if page else ""
    # Where and what first: the hub cuts long results, and Codex's own summary can be long.
    return "done", (f"Codex ({model}) finished in {took}s, in {folder}. "
                    + (f"Files changed: {files}." if changed else "No files changed.") + show
                    + f"\nIts summary: {final[:1000]}")


def _kill(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except ProcessLookupError:
            pass


def _event(hub, run, line: str) -> str:
    """One JSON line from `codex exec --json` -> hub events. Returns an agent
    message's text, 'ERROR:<text>', or ''."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return ""
    kind = event.get("type", "")
    item = event.get("item") or {}
    itype = item.get("type", "")
    if kind == "thread.started":
        run.thread_id = event.get("thread_id")
    elif kind == "turn.started":
        run.steps += 1
        run.doing = "thinking"
        hub.emit("thinking", run)
    elif kind == "item.started" and itype == "command_execution":
        run.doing = "running " + _command_words(item.get("command", ""))
        hub.emit("tool", run, run.doing, tool="run_command")
    elif kind == "item.completed" and itype == "file_change":
        for change in item.get("changes") or []:
            path = str(change.get("path", ""))
            if path:
                name = Path(path).name
                run.doing = f"wrote {name}"
                hub.emit("file", run, name)
    elif kind == "item.completed" and itype == "agent_message":
        text = str(item.get("text", "")).strip()
        if text:
            run.doing = text.splitlines()[0][:90]
            hub.emit("progress", run, run.doing)
        return text
    elif kind == "item.completed" and itype == "reasoning":
        hub.emit("thinking", run)
    elif kind == "turn.failed":
        return "ERROR:" + str((event.get("error") or {}).get("message", ""))[:400]
    elif kind == "error":
        message = str(event.get("message", ""))
        # Warnings about hooks and model metadata come through as errors too.
        if "status" in message or "invalid_request" in message or "unauthorized" in message.lower():
            return "ERROR:" + message[:400]
    elif kind == "turn.completed":
        usage = event.get("usage") or {}
        log.info("codex turn usage: %s", usage)
    return ""
