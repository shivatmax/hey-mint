"""`mint --doctor`: a plain checklist of what Mint needs, one line each, with what to do about anything wrong.

    ✓  fine            !  worth a look (Mint still works)            ✗  broken: Mint can't do its job

Checks: macOS and Apple silicon, the Gemini key (present, and accepted by Google - the one network call, at most
NET_TIMEOUT seconds; offline is a "!"), the voice model pool (live_models), permissions (the welcome window's own
checks), the runtime (.venv's Python, the app, the running process), updates (updater), coding agents (Claude Code
hooks and status line, Codex), Telegram and email control (on / off only) and disk space. Keys are shown only as
••••last4. Read-only: nothing is written, nothing is asked of macOS. Each check is on its own, so one that fails
never stops the rest. run() returns 1 when any line is ✗, else 0.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from mint.core import config

OK, WARN, BAD = "✓", "!", "✗"
NET_TIMEOUT = 6.0                  # seconds for the one network check (the Gemini key)
LOW_DISK = 2 * 1024 ** 3           # warn under this much free space in ~
HOME = Path.home()
SUPPORT = HOME / "Library" / "Application Support"
RUNTIMES = (("Mint", SUPPORT / "Mint"), ("Hey Mint", SUPPORT / "Hey Mint"))     # install.sh's, and the DMG's
APPS = (HOME / "Applications" / "Mint.app", Path("/Applications/Hey Mint.app"), HOME / "Applications" / "Hey Mint.app")
MINT_BUNDLES = ("local.jarvis", "io.github.shivatmax.heymint")
PRIVACY = "System Settings ▸ Privacy & Security ▸ "


def _mask(key: str) -> str:
    key = str(key or "").strip()
    return "••••" + key[-4:] if len(key) >= 8 else "••••"


def _short(path) -> str:
    text = str(path)
    home = str(HOME)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _run(argv: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# --- the system ------------------------------------------------------------------------------------------

def check_macos():
    version = platform.mac_ver()[0] or "unknown"
    parts = [int(p) if p.isdigit() else 0 for p in version.split(".")] + [0, 0]
    arm = platform.machine() == "arm64" or _run(["sysctl", "-n", "sysctl.proc_translated"]) == "1"
    chip = "Apple silicon" if arm else platform.machine() or "an unknown chip"
    if not arm:
        return BAD, f"{version} on {chip}", "Mint needs a Mac with Apple silicon (M1 or newer)"
    if tuple(parts[:2]) < (14, 2):
        return BAD, f"{version} on {chip}", \
            "Mint needs macOS 14.2 or newer: System Settings ▸ General ▸ Software Update"
    return OK, f"{version} on {chip}", ""


# --- the Gemini key ----------------------------------------------------------------------------------------

def _env_file() -> Path:
    return config.PROJECT_ROOT / ".env"


def _in_env_file(name: str) -> bool:
    try:
        text = _env_file().read_text()
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if line.partition("=")[0].strip() == name and line.partition("=")[2].strip().strip("'\""):
            return True
    return False


def _verify(key: str, timeout: float = NET_TIMEOUT) -> tuple[str, str]:
    """The welcome window's own check (onboarding._verify_key), given at most `timeout` seconds."""
    from mint.ui import onboarding
    answer: list = []
    worker = threading.Thread(target=lambda: answer.append(onboarding._verify_key(key)), daemon=True,
                              name="doctor-key")
    worker.start()
    worker.join(timeout)
    return answer[0] if answer else ("offline", f"no answer from Google in {timeout:.0f} s")


def check_key():
    from mint.core import gemini_keys
    have = gemini_keys.keys()
    if not have:
        return (BAD, "not set",
                "paste it in the welcome window or Settings ▸ Accounts & keys (free at aistudio.google.com/apikey)")
    env, key = have[0]
    where = ".env" if _in_env_file(env) else "the environment"
    more = f", key 2 {_mask(have[1][1])}" if len(have) > 1 else ""
    verdict, words = _verify(key)
    shown = f"{_mask(key)} from {where}{more}"
    if verdict == "offline":
        return WARN, f"{shown}; couldn't reach Google to check it", "check the internet connection, then run this again"
    if verdict != "ok":
        return BAD, f"{shown}; {words}", "paste a new key in Settings ▸ Accounts & keys"
    try:
        loose = _env_file().exists() and _env_file().stat().st_mode & 0o077
    except OSError:
        loose = False
    if loose:
        return WARN, f"{shown}, accepted by Google; but .env can be read by other users", \
            f"chmod 600 '{_short(_env_file())}'"
    return OK, f"{shown}, accepted by Google", ""


# --- voice models ----------------------------------------------------------------------------------------

def check_models():
    from mint.voice import live_models
    models = [m for m in live_models.pool() if not str(m).startswith("_")]
    if not models:
        return BAD, "no voice models in the pool", "clear voice_models in Settings ▸ the settings file"
    now = time.time()
    span = getattr(live_models, "_span", lambda s: f"{int(s)} s")
    resting = [(m, live_models.benched(m, now)) for m in models]
    ready = [live_models.label(m) for m, left in resting if not left]
    benched = [f"{live_models.label(m)} ({span(left)})" for m, left in resting if left]
    if not ready:
        return BAD, f"all {len(models)} benched: {', '.join(benched)}", \
            "they come back on their own; if it lasts, check the key's quota at aistudio.google.com"
    if benched:
        return WARN, f"{len(ready)} of {len(models)} ready ({', '.join(ready)}); benched: {', '.join(benched)}", \
            "Mint uses the ready ones meanwhile; the benched come back on their own"
    return OK, f"{len(ready)} ready: {', '.join(ready)}", ""


# --- permissions ------------------------------------------------------------------------------------------

def _inside_mint() -> bool:
    return os.environ.get("__CFBundleIdentifier", "") in MINT_BUNDLES


def _host() -> str:
    names = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "vscode": "VS Code", "WarpTerminal": "Warp",
             "ghostty": "Ghostty"}
    term = os.environ.get("TERM_PROGRAM", "")
    return names.get(term, term or "this terminal")


def _permission(kind: str, title: str, required: bool = False):
    """The welcome window's permission check (Onboarding._status). macOS answers for the app this runs in:
    Mint.app when Mint runs it, else the terminal - so from a terminal a "no" is a "!" and says whose it is."""
    from mint.ui import onboarding
    status = onboarding.Onboarding._status(SimpleNamespace(asked=set()), kind)
    inside = _inside_mint()
    whose = "" if inside else f" (for {_host()}, which runs this check; Mint.app has its own)"
    if status == "allowed":
        return OK, "allowed" + whose, ""
    words = "not allowed" if status == "denied" else "not allowed yet"
    if inside:
        return (BAD if required else WARN), words, f"{PRIVACY}{title}: turn Mint on"
    return WARN, words + whose, f"to see Mint's own: {PRIVACY}{title}"


def check_microphone():
    return _permission("microphone", "Microphone", required=True)


def check_accessibility():
    return _permission("accessibility", "Accessibility")


def check_screen():
    return _permission("screen", "Screen Recording")


def check_automation():
    # macOS has no way to read this without asking (and the welcome window doesn't ask): it asks the first time
    # Mint controls each app (Notes, Mail, System Events...).
    return WARN, "not checked: macOS asks the first time Mint controls each app", f"{PRIVACY}Automation"


# --- Mint itself ------------------------------------------------------------------------------------------

def check_runtime():
    found = []
    for name, root in RUNTIMES:
        python = root / ".venv" / "bin" / "python"
        if not python.exists():
            continue
        version = _run([str(python), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
        found.append((name, root, version))
    if not found:
        return BAD, "no Mint runtime in ~/Library/Application Support", \
            "install it: curl -fsSL https://hey-mint.pages.dev/install.sh | bash"
    words = "; ".join(f"{name}: Python {version or '?'} ({_short(root / '.venv')})" for name, root, version in found)
    broken = [name for name, _root, version in found if not version]
    old = [name for name, _root, version in found
           if version and tuple(int(p) for p in version.split(".")[:2]) < (3, 11)]
    if broken:
        return BAD, words, f"{' and '.join(broken)}'s Python doesn't start: install Mint again"
    if old:
        return WARN, words, "Mint wants Python 3.11 or newer: install Mint again"
    return OK, words, ""


def check_app():
    found = []
    for app in APPS:
        if app.is_dir():
            try:
                version = plistlib.loads((app / "Contents" / "Info.plist").read_bytes()).get(
                    "CFBundleShortVersionString", "")
            except (OSError, ValueError, plistlib.InvalidFileException):
                version = ""
            found.append(f"{_short(app)}" + (f" {version}" if version else ""))
    if not found:
        return WARN, "no Mint.app or Hey Mint.app", \
            "install it: curl -fsSL https://hey-mint.pages.dev/install.sh | bash"
    return OK, ", ".join(found), ""


def _mint_pids() -> list[int]:
    """Mint's Python (python -m mint --hands-free). Shells whose command line merely contains the words are not
    Mint: only a Python process counts, and never this one."""
    pids = []
    for line in _run(["pgrep", "-fl", "mint --hands-free"]).splitlines():
        pid, _, command = line.strip().partition(" ")
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        program = os.path.basename(command.split(" -m ")[0].strip()).lower()
        if "python" in program and " -m mint" in command:
            pids.append(int(pid))
    return pids


def check_process():
    pids = _mint_pids()
    if not pids:
        return WARN, "not running", "open Mint.app (or Hey Mint.app)"
    many = "" if len(pids) == 1 else f" ({len(pids)} copies: quit and open it again)"
    return (WARN if many else OK), f"running (pid {', '.join(map(str, pids))}){many}", \
        ("menu bar icon ▸ Quit, then open it" if many else "")


def check_updates():
    from mint.app import updater
    info = updater.info()
    current, latest = info.get("current") or "?", info.get("latest") or ""
    checked = info.get("checked")
    when = time.strftime("%-d %b", time.localtime(float(checked))) if checked else "never"
    words = (f"{current}, {info.get('channel', 'stable')} channel, automatic updates "
             f"{'on' if info.get('auto') else 'off'}, last checked {when}")
    if not info.get("can_update"):
        words += " (this copy updates with git and install.sh)"
    if latest and updater.newer(latest, current):
        hint = "say \"update yourself\", or Settings ▸ Updates & Help" if info.get("can_update") else \
            "get it at https://github.com/shivatmax/hey-mint/releases"
        return WARN, f"{words}; {latest} is out", hint
    return OK, words, ""


# --- coding agents ------------------------------------------------------------------------------------------

def check_claude_hooks():
    from mint.tools import agent_hooks
    if not (HOME / ".claude").exists():
        return WARN, "Claude Code not found (~/.claude)", "only needed to follow Claude Code from the notch"
    if agent_hooks.installed():
        return OK, "hooks installed (approvals from the notch)", ""
    return WARN, "hooks not installed", "Settings ▸ Appearance & Sound ▸ Claude mode ▸ Connect Claude Code"


def check_statusline():
    from mint.tools import agent_hooks
    installed = getattr(agent_hooks, "statusline_installed", None)
    if installed is None:
        return WARN, "this version of Mint has no status line", "update Mint"
    if installed():
        return OK, "Mint's status line installed (Claude's usage limits)", ""
    return WARN, "not installed", "Settings ▸ Appearance & Sound ▸ Claude mode ▸ Show Claude's usage limits"


def check_codex():
    sessions = HOME / ".codex" / "sessions"
    if sessions.is_dir():
        return OK, f"sessions folder found ({_short(sessions)})", ""
    if (HOME / ".codex").exists():
        return WARN, "~/.codex has no sessions folder yet", "run Codex once; its sessions show up by themselves"
    return WARN, "Codex not found (~/.codex)", "only needed to follow Codex sessions"


def check_mcp():
    try:
        from mint.tools import agent_mcp
    except ImportError:
        return WARN, "this version of Mint has no MCP server", "update Mint"
    apps = [a for a in ("claude", "codex") if agent_mcp.available(a)]
    if not apps:
        return OK, "no Claude Code or Codex command-line tool here (not needed)", ""
    on = [agent_mcp.APPS[a] for a in apps if agent_mcp.connected(a)]
    off = [agent_mcp.APPS[a] for a in apps if agent_mcp.APPS[a] not in on]
    if not off:
        return OK, f"{' and '.join(on)} can ask Mint about their work (check_my_work)", ""
    return WARN, (f"connected to {', '.join(on)}; not to {', '.join(off)}" if on else
                  f"not connected to {' or '.join(off)}"), \
        "optional: Settings ▸ Appearance & Sound ▸ Claude mode ▸ Let agents ask Mint"


def check_downloads():
    from mint.tools import video_download as vd
    yt = vd._ytdlp()
    if yt is None:
        return BAD, "yt-dlp is missing", "reinstall Mint"
    runtime = vd.js_runtime()
    ffmpeg = vd._ffmpeg()
    parts = [f"yt-dlp {yt.version.__version__}"]
    parts.append(f"{next(iter(runtime))} for YouTube" if runtime else "no JavaScript engine")
    parts.append("ffmpeg" if ffmpeg else "no ffmpeg")
    if runtime and ffmpeg:
        return OK, ", ".join(parts), ""
    return WARN, ", ".join(parts), ("YouTube in full quality and joined picture + sound need both; reinstall Mint "
                                     "(they come with it)")


# --- remote control ---------------------------------------------------------------------------------------

def check_telegram():
    from mint.core import prefs
    if not prefs.get("telegram_enabled"):
        return OK, "off", ""
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():          # telegram.TOKEN_ENV
        return BAD, "on, but there is no bot token", "Settings ▸ Accounts & keys ▸ Telegram remote control"
    return OK, "on" + (" (read-only)" if prefs.get("telegram_read_only") else ""), ""


def check_email():
    from mint.core import prefs
    if not prefs.get("email_enabled"):
        return OK, "off", ""
    if not str(prefs.get("email_address") or "").strip():
        return WARN, "on, but no address is set", "Settings ▸ Accounts & keys ▸ Email control"
    return OK, "on" + (" (read-only)" if prefs.get("email_read_only") else ""), ""


# --- disk -----------------------------------------------------------------------------------------------------

def check_disk():
    free = shutil.disk_usage(HOME).free
    if free < LOW_DISK:
        return WARN, f"{_size(free)} free", "free some space: models, recordings and updates need room"
    return OK, f"{_size(free)} free", ""


def check_folder():
    sizes = []
    for name, root in RUNTIMES:
        if not root.is_dir():
            continue
        out = _run(["du", "-sk", str(root)], timeout=NET_TIMEOUT)
        kb = out.split()[0] if out.split() else ""
        sizes.append(f"{name} {_size(int(kb) * 1024)}" if kb.isdigit() else f"{name} (too big to measure quickly)")
    if not sizes:
        return WARN, "no support folder yet", "open Mint once"
    return OK, ", ".join(sizes), ""


CHECKS = (
    ("macOS", check_macos),
    ("Gemini key", check_key),
    ("Voice models", check_models),
    ("Microphone", check_microphone),
    ("Accessibility", check_accessibility),
    ("Screen Recording", check_screen),
    ("Automation", check_automation),
    ("Runtime", check_runtime),
    ("App", check_app),
    ("Mint process", check_process),
    ("Updates", check_updates),
    ("Claude Code", check_claude_hooks),
    ("Status line", check_statusline),
    ("Codex", check_codex),
    ("Agents ask Mint", check_mcp),
    ("Video downloads", check_downloads),
    ("Telegram", check_telegram),
    ("Email control", check_email),
    ("Disk", check_disk),
    ("Mint's folder", check_folder),
)


def line(mark: str, title: str, words: str, hint: str = "") -> str:
    return f"{mark} {title:<17} {words}" + (f"  → {hint}" if hint and mark != OK else "")


def run(verbose: bool = False) -> int:
    """Print the checklist; 1 when anything is ✗, else 0."""
    try:
        from mint.app import updater
        version = updater.current_version()
    except Exception:
        version = "?"
    print(f"Mint doctor  v{version}  (data: {_short(config.PROJECT_ROOT)})", flush=True)
    installed = [root for _name, root in RUNTIMES if (root / "mint").is_dir()]
    if installed and config.PROJECT_ROOT.resolve() not in [root.resolve() for root in installed]:
        # A source checkout has its own .env and settings; the app reads the runtime's.
        there = _short(installed[0]).replace(" ", "\\ ")
        print(f"  This is a checkout with its own key and settings. The app's: cd {there} && "
              f".venv/bin/python -m mint --doctor", flush=True)
    print(flush=True)
    marks = []
    for title, check in CHECKS:
        started = time.monotonic()
        try:
            mark, words, hint = check()
        except Exception as error:
            mark, words, hint = WARN, f"couldn't check ({type(error).__name__}: {str(error)[:100]})", \
                "run with --verbose and report it in Settings ▸ Updates & Help"
        marks.append(mark)
        took = f"  [{time.monotonic() - started:.1f} s]" if verbose else ""
        print(line(mark, title, words, hint) + took, flush=True)
    if verbose:
        try:
            from mint.voice import live_models
            print("\nVoice models:\n  " + live_models.status().replace("\n", "\n  "), flush=True)
        except Exception as error:
            print(f"\nVoice models: {error}", flush=True)
    bad, warn = marks.count(BAD), marks.count(WARN)
    if not bad and not warn:
        print("\nAll good.", flush=True)
    else:
        parts = ([f"{bad} problem{'s' * (bad != 1)} (✗)"] if bad else []) + \
                ([f"{warn} to look at (!)"] if warn else [])
        print("\n" + ", ".join(parts) + ".", flush=True)
    return 1 if bad else 0
