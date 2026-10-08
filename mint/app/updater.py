"""Updates: a downloaded Hey Mint.app installs new releases itself, in place, and keeps its permissions.

    "check for updates"             check    GitHub's latest release against this version
    "update yourself"               install  download now; install by itself the moment Mint is idle
    "which version are you?"        status   this version, the newest one, and what is happening

At launch (unless it looked within the last hour) and every 12 hours Mint asks GitHub for the latest
release (Settings: auto_update, on by default; update_channel "stable", or "beta" for pre-releases).
A newer one is downloaded in the background and installed once the Mac has been left alone for a while.

Asked for (by voice, or Settings' Install button), it downloads at once - retrying with backoff if that
fails - and installs at the first moment Mint itself is idle: asleep (or paused), no tool, task, timer,
sub-agent, voice training, Meet call, meeting or screen recording, or dictation going, not speaking.
It never forces a restart: busy for hours, it waits for hours. The request is kept in update.json, so a
reload picks it up again. Installing:

1. the DMG is checked against its sha256 (latest.json, the .sha256 file and GitHub's own digest must
   agree), mounted read-only, and the app copied out;
2. the copy must pass `codesign --verify --deep --strict`, be Hey Mint, be the version promised, and be
   signed by the same certificate as the running app - macOS keeps Microphone, Accessibility and
   Screen Recording for an app by its designated requirement, so an app signed by anyone else would
   not only lose them, it would not be ours. Only an ad-hoc signed app may be replaced by another
   ad-hoc one (it has no certificate to compare; macOS asks for the permissions again);
3. the new app takes the old one's place in one atomic rename (renamex_np RENAME_SWAP), the old one
   goes to the Trash (a backup), and a small detached script opens the new one once this process and
   the launcher have quit.

Only a packaged "Hey Mint.app" updates itself: a source checkout, or the Mint.app install.sh builds,
is updated with git and install.sh. The next launch shows "Updated to vX" on the island.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import platform
import plistlib
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("mint.app.updater")

REPO = "shivatmax/hey-mint"
API = f"https://api.github.com/repos/{REPO}"
IDENTIFIER = "io.github.shivatmax.heymint"
APP_NAME = "Hey Mint.app"
INTERVAL = 12 * 3600            # between checks
LAUNCH_GAP = 3600               # Mint reloads often (Mint Ear): at launch, only if not checked this hour
IDLE = 10 * 60                  # the Mac left alone this long: a downloaded update installs itself
DATA = Path.home() / "Library" / "Application Support" / "Hey Mint"
STATE_FILE = DATA / "update.json"
CACHE = Path.home() / "Library" / "Caches" / IDENTIFIER / "updates"
RENAME_SWAP = 0x2
POLL = 5.0                      # an update the user asked for: how often to look whether Mint is idle
RETRY = (30, 60, 120, 300, 600, 1800)   # after a failed download / check / install, wait this long (s)
REQUEST_FOR = 7 * 86400         # a request older than this (Mint never idle, never updated) is dropped

_lock = threading.RLock()
_prep_lock = threading.Lock()   # one download/unpack at a time (the auto check and a request may overlap)
_state: dict = {"status": "idle", "detail": "", "progress": 0.0}
_offer: dict | None = None      # the newer release found by the last check
_staged: Path | None = None     # its app, downloaded and verified, ready to swap in
_worker: threading.Thread | None = None
_waiter: threading.Thread | None = None     # an update the user asked for: download, wait for idle, install
_requested: float = 0.0         # when the user asked for the update (0: not asked)
_waiting = ""                   # why the asked-for update is not installing yet ("" when it isn't waiting)
_failures = 0                   # failed tries in a row (the backoff)
_next_try = 0.0                 # no new try before this time
_installing = False
_started = False


class UpdateError(Exception):
    pass


# --- versions --------------------------------------------------------------------------------------

def parse_version(text: str) -> tuple:
    """ "v1.2.3-beta.2" -> a tuple that sorts: pre-releases before their release."""
    text = str(text or "").strip().lstrip("vV")
    main, _, pre = text.partition("-")
    main = main.partition("+")[0]
    numbers = [int(n) if n.isdigit() else 0 for n in main.split(".")[:4]]
    numbers += [0] * (3 - len(numbers))
    tail = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.]", pre) if p)
    return (*numbers, 0 if pre else 1, tail)


def newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


# --- the running app -------------------------------------------------------------------------------

def running_app() -> Path | None:
    """The .app this process runs from: the launcher says so (MINT_APP_PATH), else our own Python."""
    if os.environ.get("MINT_APP_PATH"):
        return Path(os.environ["MINT_APP_PATH"])
    for parent in Path(os.path.realpath(sys.executable)).parents:
        if parent.suffix == ".app":
            return parent
    return None


def _info(app: Path) -> dict:
    try:
        return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}


def current_version(app: Path | None = None) -> str:
    app = app or running_app()
    version = _info(app).get("CFBundleShortVersionString") if app else None
    if version:
        return str(version)
    for folder in Path(__file__).resolve().parents[:3]:      # a checkout: its pyproject.toml
        found = re.search(r'^version = "([^"]+)"', _read(folder / "pyproject.toml"), re.M)
        if found:
            return found.group(1)
    from mint import __version__
    return __version__


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def packaged(app: Path | None) -> tuple[bool, str]:
    """(True, "") when this is a downloaded Hey Mint.app that can replace itself; else why not."""
    if app is None:
        return False, "Mint is running from source, not from Hey Mint.app; update it with git."
    info = _info(app)
    if info.get("MintProjectRoot") or info.get("CFBundleIdentifier") != IDENTIFIER or app.name != APP_NAME:
        return False, f"{app.name} was built on this Mac (install.sh); update it with git and install.sh."
    if not (app / "Contents" / "Resources" / "app" / "mint").is_dir():
        return False, f"{app} is not a packaged Hey Mint."
    if "/AppTranslocation/" in str(app) or str(app).startswith("/Volumes/"):
        return False, "Hey Mint is running from the disk image or a quarantined folder: drag it into Applications first."
    if not os.access(app.parent, os.W_OK) or not os.access(app, os.W_OK):
        return False, f"This account can't replace {app} (no write access); download the new version by hand."
    return True, ""


# --- GitHub ----------------------------------------------------------------------------------------

def _context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url: str, limit: int = 2_000_000) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": f"HeyMint/{current_version()} (+https://github.com/{REPO})",
        "Accept": "application/vnd.github+json" if url.startswith(API) else "*/*"})
    with urllib.request.urlopen(request, timeout=20, context=_context()) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError(f"{url} is larger than expected")
    return data


def _channel() -> str:
    try:
        from mint.core import prefs
        return "beta" if str(prefs.get("update_channel") or "stable").lower() == "beta" else "stable"
    except Exception:
        return "stable"


def auto() -> bool:
    try:
        from mint.core import prefs
        value = prefs.get("auto_update")
    except Exception:
        value = None
    return True if value is None else bool(value)


def fetch_release(channel: str = "stable") -> dict:
    """GitHub's latest release; "beta" also takes pre-releases (the newest of the last ten)."""
    if channel != "beta":
        return json.loads(_get(f"{API}/releases/latest"))
    releases = [r for r in json.loads(_get(f"{API}/releases?per_page=10")) if not r.get("draft")]
    if not releases:
        raise UpdateError("no releases yet")
    return max(releases, key=lambda r: parse_version(r.get("tag_name", "")))


def parse_release(release: dict) -> dict:
    """What we need from a release: the DMG, and its sha256 from every place that states it (they must agree)."""
    version = str(release.get("tag_name") or "").lstrip("vV")
    if not re.match(r"^\d+\.\d+", version):
        raise UpdateError(f"release tag {release.get('tag_name')!r} is not a version")
    assets = {a.get("name", ""): a for a in release.get("assets") or []}
    dmgs = [n for n in assets if n.endswith("-arm64.dmg")] or [n for n in assets if n.endswith(".dmg")]
    if not dmgs:
        raise UpdateError(f"v{version} has no DMG yet")
    dmg = assets[dmgs[0]]
    hashes, notes, min_macos = {}, "", ""
    if "latest.json" in assets:
        latest = json.loads(_get(assets["latest.json"]["browser_download_url"]))
        if str(latest.get("version", "")).lstrip("v") != version:
            raise UpdateError(f"latest.json says {latest.get('version')}, the release is v{version}")
        if latest.get("dmg") == dmg["name"] and latest.get("sha256"):
            hashes["latest.json"] = str(latest["sha256"]).lower()
        notes, min_macos = str(latest.get("notes") or ""), str(latest.get("min_macos") or "")
    if dmg["name"] + ".sha256" in assets:
        text = _get(assets[dmg["name"] + ".sha256"]["browser_download_url"], 10_000).decode(errors="replace")
        found = re.match(r"\s*([0-9a-fA-F]{64})\b", text)
        if found:
            hashes[".sha256"] = found.group(1).lower()
    if str(dmg.get("digest") or "").startswith("sha256:"):
        hashes["digest"] = dmg["digest"].split(":", 1)[1].lower()
    if not hashes:
        raise UpdateError(f"v{version} has no checksum to check the download against")
    if len(set(hashes.values())) > 1:
        raise UpdateError(f"v{version}'s checksums disagree ({', '.join(hashes)}); not installing it")
    return {"version": version, "tag": release.get("tag_name"), "dmg": dmg["name"],
            "url": dmg["browser_download_url"], "size": int(dmg.get("size") or 0),
            "sha256": next(iter(hashes.values())), "sources": sorted(hashes),
            "notes": notes or str(release.get("body") or ""), "min_macos": min_macos,
            "page": release.get("html_url") or f"https://github.com/{REPO}/releases",
            "prerelease": bool(release.get("prerelease"))}


# --- saved state -----------------------------------------------------------------------------------

def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(**changes) -> None:
    data = _load()
    data.update(changes)
    data = {k: v for k, v in data.items() if v is not None}
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, indent=2))
    except OSError as error:
        log.info("update state: %s", error)


def _set(status: str, detail: str = "", progress: float = 0.0) -> None:
    with _lock:
        _state.update(status=status, detail=detail, progress=progress)
    if status == "error":
        log.warning("update: %s", detail)


# --- checking --------------------------------------------------------------------------------------

def check(now: bool = False, background: bool = True) -> str:
    """Ask GitHub for a newer version. Without `now`, at most every 12 hours (the saved last check).
    With automatic updates on (and `background`), a newer one starts downloading in the background."""
    global _offer
    last = float(_load().get("checked") or 0)
    if not now and time.time() - last < INTERVAL:
        return status()
    app = running_app()
    current = current_version(app)
    _set("checking")
    try:
        offer = parse_release(fetch_release(_channel()))
    except Exception as error:
        _set("error", f"Couldn't check for updates: {error}")
        return _state["detail"]
    _save(checked=time.time(), latest=offer["version"], latest_url=offer["url"], latest_page=offer["page"])
    if not newer(offer["version"], current):
        with _lock:
            _offer = None
        _set("current", f"Hey Mint {current} is the newest version.")
        return _state["detail"]
    if offer["min_macos"] and parse_version(platform.mac_ver()[0]) < parse_version(offer["min_macos"]):
        with _lock:
            _offer = None
        _set("blocked", f"Hey Mint {offer['version']} needs macOS {offer['min_macos']} or later.")
        return _state["detail"]
    with _lock:
        _offer = offer
    ok, why = packaged(app)
    if not ok:
        _set("available", f"Hey Mint {offer['version']} is out (this is {current}). Download it, then drag it "
                          "into Applications to replace this one - your settings and permissions stay.")
        return _state["detail"]
    _set("available", f"Hey Mint {offer['version']} is available (this is {current}).")
    if auto() and background:
        _prepare_async()
        return f"Hey Mint {offer['version']} is available; downloading it in the background."
    return _state["detail"] + " Say \"install the update\" to get it."


def status() -> str:
    with _lock:
        state, offer = dict(_state), _offer
    current = current_version()
    checked = float(_load().get("checked") or 0)
    when = time.strftime("%-d %b %H:%M", time.localtime(checked)) if checked else "never"
    if state["status"] == "downloading":
        return f"Downloading Hey Mint {offer and offer['version']}: {state['progress'] * 100:.0f}%."
    line = state["detail"] or (f"Hey Mint {current}; a newer {offer['version']} is available." if offer
                               else f"Hey Mint {current}.")
    auto_line = "on" if auto() else "off"
    return f"{line} Last checked: {when}. Automatic updates: {auto_line}."


def info() -> dict:
    """For Settings: this version, the newest one known, and what the updater is doing."""
    app = running_app()
    ok, why = packaged(app)
    with _lock:
        saved = _load()
        return {"current": current_version(app), "latest": (_offer or {}).get("version") or saved.get("latest"),
                "download": (_offer or {}).get("url") or saved.get("latest_url"),
                "page": (_offer or {}).get("page") or saved.get("latest_page") or f"https://github.com/{REPO}/releases/latest",
                "status": _state["status"], "detail": _state["detail"], "progress": _state["progress"],
                "checked": _load().get("checked"), "ready": _staged is not None, "can_update": ok, "why": why,
                "auto": auto(), "channel": _channel(), "requested": requested(), "waiting": _waiting}


def progress() -> dict:
    """For Settings' progress row, several times a second: cheap (no disk or signature checks)."""
    with _lock:
        offer = _offer or {}
        return {"status": _state["status"], "progress": _state["progress"], "detail": _state["detail"],
                "version": offer.get("version"), "size": offer.get("size") or 0, "ready": _staged is not None,
                "requested": bool(_requested), "waiting": _waiting}


# --- downloading and checking the new app ----------------------------------------------------------

def _download(offer: dict, folder: Path) -> Path:
    """The DMG, streamed to disk; its sha256 must be the one the release states."""
    if offer["size"] and shutil.disk_usage(folder).free < offer["size"] * 5:
        raise UpdateError("not enough free disk space for the update")
    target = folder / offer["dmg"]
    part = target.with_suffix(".part")
    digest, done = hashlib.sha256(), 0
    request = urllib.request.Request(offer["url"], headers={"User-Agent": f"HeyMint/{current_version()}"})
    with urllib.request.urlopen(request, timeout=60, context=_context()) as response, open(part, "wb") as out:
        while block := response.read(1 << 20):
            out.write(block)
            digest.update(block)
            done += len(block)
            if offer["size"]:
                _set("downloading", f"Downloading Hey Mint {offer['version']}…", min(1.0, done / offer["size"]))
    if offer["size"] and done != offer["size"]:
        part.unlink(missing_ok=True)
        raise UpdateError(f"the download stopped at {done} of {offer['size']} bytes")
    if digest.hexdigest() != offer["sha256"]:
        part.unlink(missing_ok=True)
        raise UpdateError("the download does not match the release's sha256; not installing it")
    part.rename(target)
    return target


def _extract(dmg: Path, folder: Path) -> Path:
    """Mount the DMG read-only (no Finder window) and copy the app out of it."""
    mounts = folder / "mnt"
    mounts.mkdir(exist_ok=True)
    out = subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-noverify",
                          "-mountrandom", str(mounts), "-plist", str(dmg)],
                         capture_output=True, timeout=180, stdin=subprocess.DEVNULL)
    if out.returncode:
        raise UpdateError(f"couldn't open the DMG: {out.stderr.decode(errors='replace').strip()[:200]}")
    points = [e["mount-point"] for e in plistlib.loads(out.stdout).get("system-entities", []) if e.get("mount-point")]
    if not points:
        raise UpdateError("the DMG mounted without a volume")
    try:
        source = Path(points[0]) / APP_NAME
        if not source.is_dir():
            raise UpdateError(f"the DMG has no {APP_NAME}")
        target = folder / APP_NAME
        if target.exists():
            shutil.rmtree(target)
        done = subprocess.run(["ditto", "--noqtn", str(source), str(target)], capture_output=True, timeout=600)
        if done.returncode:
            raise UpdateError(f"couldn't copy the app out: {done.stderr.decode(errors='replace').strip()[:200]}")
        return target
    finally:
        for point in points:
            subprocess.run(["hdiutil", "detach", point, "-force"], capture_output=True, timeout=60)


def signer(app: Path) -> dict:
    """Who signed an app: ad hoc, or the certificate (its SHA-256) and the designated requirement."""
    details = subprocess.run(["codesign", "-dvv", str(app)], capture_output=True, text=True, timeout=60).stderr
    authorities = re.findall(r"^Authority=(.+)$", details, re.M)
    adhoc = "Signature=adhoc" in details or not authorities
    requirement = subprocess.run(["codesign", "-d", "-r-", str(app)], capture_output=True, text=True, timeout=60)
    found = re.search(r"^designated => (.+)$", requirement.stdout + requirement.stderr, re.M)
    leaf = ""
    if not adhoc:
        with tempfile.TemporaryDirectory(prefix="mint-certs-") as temp:
            subprocess.run(["codesign", "-d", f"--extract-certificates={temp}/cert", str(app)],
                           capture_output=True, timeout=60)
            first = Path(temp) / "cert0"
            leaf = hashlib.sha256(first.read_bytes()).hexdigest() if first.exists() else ""
    return {"adhoc": adhoc, "authority": authorities[0] if authorities else "",
            "leaf": leaf, "requirement": found.group(1).strip() if found else ""}


def verify(new: Path, current: Path, version: str = "") -> dict:
    """Refuse an app that is broken, isn't Hey Mint, isn't the promised version, or is signed by someone else."""
    done = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(new)],
                          capture_output=True, text=True, timeout=600)
    if done.returncode:
        raise UpdateError(f"the new app's signature is broken: {done.stderr.strip()[-300:]}")
    info = _info(new)
    if info.get("CFBundleIdentifier") != _info(current).get("CFBundleIdentifier"):
        raise UpdateError(f"the download is {info.get('CFBundleIdentifier')!r}, not Hey Mint")
    if version and str(info.get("CFBundleShortVersionString")) != version:
        raise UpdateError(f"the download is version {info.get('CFBundleShortVersionString')}, not {version}")
    mine, theirs = signer(current), signer(new)
    if mine["adhoc"]:
        if not theirs["adhoc"]:
            # Nothing to compare an ad-hoc app with; from now on the certificate is pinned.
            log.info("update: ad-hoc -> signed by %s", theirs["authority"])
        return theirs
    if theirs["adhoc"]:
        raise UpdateError("the new app is not signed (this one is, by "
                          f"{mine['authority']}); not installing it")
    if not theirs["leaf"] or theirs["leaf"] != mine["leaf"]:
        raise UpdateError(f"the new app is signed by a different certificate ({theirs['authority']!r}, "
                          f"this one by {mine['authority']!r}); not installing it")
    if mine["requirement"]:
        # Exactly what macOS checks before handing over this app's permissions.
        done = subprocess.run(["codesign", "--verify", f"-R={mine['requirement']}", str(new)],
                              capture_output=True, text=True, timeout=600)
        if done.returncode:
            raise UpdateError("the new app doesn't meet this app's designated requirement; not installing it")
    return theirs


def prepare(offer: dict, app: Path) -> Path:
    """Download, check and unpack a release; returns the verified app, ready to swap in."""
    with _prep_lock:          # a second caller waits, then finds the app ready (and checks it again)
        return _prepare(offer, app)


def _prepare(offer: dict, app: Path) -> Path:
    global _staged
    folder = CACHE / offer["version"]
    ready = folder / APP_NAME
    if ready.is_dir():
        try:                                  # downloaded before (Mint unloaded meanwhile): check it again
            verify(ready, app, offer["version"])
            with _lock:
                _staged = ready
            return ready
        except UpdateError:
            shutil.rmtree(folder, ignore_errors=True)
    for old in CACHE.glob("*") if CACHE.exists() else []:
        if old.name != offer["version"]:
            shutil.rmtree(old, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    dmg = _download(offer, folder)
    _set("verifying", f"Checking Hey Mint {offer['version']}…", 1.0)
    try:
        new = _extract(dmg, folder)
    finally:
        dmg.unlink(missing_ok=True)
    try:
        verify(new, app, offer["version"])
    except UpdateError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(new)], capture_output=True, timeout=120)
    with _lock:
        _staged = new
    _set("ready", f"Hey Mint {offer['version']} is ready to install.", 1.0)
    return new


def _prepare_async() -> None:
    """The automatic check's download, in the background (installing is the idle loop's business)."""
    global _worker
    with _lock:
        offer = _offer
        if offer is None or (_worker is not None and _worker.is_alive()):
            return
        if _staged is not None and _staged.parent.name == offer["version"]:
            return

        def work():
            try:
                prepare(offer, running_app())
            except Exception as error:
                _set("error", f"The update to {offer['version']} failed: {error}")

        _worker = threading.Thread(target=work, name="mint-update", daemon=True)
        _worker.start()


# --- installing ------------------------------------------------------------------------------------

def _trash(path: Path, name: str = "") -> Path:
    """The Trash, as Finder would (put back works); renamed so the old version is easy to find."""
    from Foundation import NSURL, NSFileManager
    ok, result, error = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(path)), None, None)
    if not ok:
        raise UpdateError(f"couldn't move the old app to the Trash: {error}")
    moved = Path(result.path())
    if name and not (moved.parent / name).exists():
        try:
            moved = moved.rename(moved.parent / name)
        except OSError:
            pass
    return moved


def _swap(new: Path, target: Path) -> Path:
    """Put `new` where `target` is in one atomic rename; returns the old app, set aside (hidden)."""
    staging = target.with_name(f".{target.name}.update")
    old = target.with_name(f".{target.name}.old")
    for leftover in (staging, old):
        if leftover.exists():
            shutil.rmtree(leftover)
    try:
        os.rename(new, staging)
    except OSError:                                 # another volume: copy it over
        done = subprocess.run(["ditto", str(new), str(staging)], capture_output=True, timeout=600)
        if done.returncode:
            shutil.rmtree(staging, ignore_errors=True)
            raise UpdateError(f"couldn't copy the new app next to the old one: {done.stderr.decode()[:200]}")
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    if libc.renamex_np(os.fsencode(staging), os.fsencode(target), RENAME_SWAP) == 0:
        return staging                              # now holds the old app
    error = ctypes.get_errno()
    if error in (1, 13):                            # EPERM, EACCES: macOS said no
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("macOS didn't let Hey Mint replace itself (Privacy & Security ▸ App Management); "
                          "download the new version by hand")
    # No swap on this volume: two renames, undone if the second fails.
    os.rename(target, old)
    try:
        os.rename(staging, target)
    except OSError:
        os.rename(old, target)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return old


RELAUNCH = """#!/bin/sh
# Hey Mint's updater: wait for the old Mint (and its launcher) to quit, then open the new one.
trap '' HUP INT TERM
app="$1"; opener="$2"; shift 2
waited=0
for pid in "$@"; do
  while kill -0 "$pid" 2>/dev/null; do
    sleep 0.3; waited=$((waited + 1))
    [ "$waited" -gt 400 ] && exit 1          # still running after ~2 minutes: leave it be
  done
done
/usr/bin/xattr -dr com.apple.quarantine "$app" 2>/dev/null
"$opener" "$app"
"""


def _relaunch(app: Path, pids: list[int], opener: str = "/usr/bin/open") -> None:
    """A detached script (its parent is launchd, so quitting Mint's children leaves it alone)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    script = CACHE / "relaunch.sh"
    script.write_text(RELAUNCH)
    script.chmod(0o700)
    subprocess.run(["/bin/sh", "-c", 'nohup /bin/sh "$@" >/dev/null 2>&1 &', "sh", str(script), str(app), opener,
                    *map(str, pids)], stdin=subprocess.DEVNULL, start_new_session=True, timeout=10)


def _quit(seconds: float) -> None:
    from mint.app import power
    power.quit_later(seconds, reason="installing an update")


def _pids() -> list[int]:
    """This process, and Mint Ear (the launcher) when it started us: the new app opens after both are gone."""
    from mint.app import ear
    return [os.getpid()] + ([os.getppid()] if ear.managed() and os.getppid() > 1 else [])


def install_now(relaunch: bool = True, quit_after: float = 6.0) -> str:
    """Swap the verified new app in, then quit; the detached script opens it. Once at a time: the idle
    loop and a request's waiter may both find the moment."""
    global _installing
    with _lock:
        if _installing:
            return "Already installing the update."
        _installing = True
    try:
        return _install_now(relaunch, quit_after)
    finally:
        # Released however it ends: a failure can be tried again; after a success nothing is staged any
        # more, so a second call has nothing to install while Mint quits.
        with _lock:
            _installing = False


def _install_now(relaunch: bool, quit_after: float) -> str:
    global _staged, _offer, _requested
    app = running_app()
    with _lock:
        staged, offer = _staged, _offer
    if staged is None or offer is None:
        raise UpdateError("no update is ready to install")
    ok, why = packaged(app)
    if not ok:
        raise UpdateError(why)
    try:
        verify(staged, app, offer["version"])    # again, just before the swap
    except UpdateError:
        with _lock:
            _staged = None
        shutil.rmtree(staged.parent, ignore_errors=True)
        raise
    before = current_version(app)
    _set("installing", f"Installing Hey Mint {offer['version']}…", 1.0)
    old = _swap(staged, app)
    try:
        _trash(old, f"Hey Mint {before}.app")
    except Exception as error:                    # the new one is in place; the old stays hidden beside it
        log.warning("update: old app not trashed (%s); it is at %s", error, old)
    _save(updated={"from": before, "to": offer["version"], "at": time.time(), "notes": offer["notes"][:600]},
          requested=None)
    with _lock:
        _staged, _offer, _requested = None, None, 0.0
    shutil.rmtree(CACHE / offer["version"], ignore_errors=True)
    _set("installed", f"Hey Mint {offer['version']} is installed; restarting.")
    log.info("update: %s -> %s installed at %s", before, offer["version"], app)
    if relaunch:
        _relaunch(app, _pids())
        _quit(quit_after)
    return f"Updating to Hey Mint {offer['version']}. I'll be back in a few seconds."


def install(wait: bool = False) -> str:
    """The user asked for the update (Settings' Install button, "update yourself"): download it now and
    install it by itself at the first moment Mint is idle - at once when it already is. `wait`: download
    and install right here, idle or not (no caller uses it any more; kept for scripts)."""
    app = running_app()
    ok, why = packaged(app)
    if not ok:
        return why
    if not wait:
        return request()
    with _lock:
        offer = _offer
    if offer is None:
        text = check(now=True, background=False)
        with _lock:
            offer = _offer
        if offer is None:
            return text
    try:
        prepare(offer, app)
        return install_now()
    except Exception as error:
        _set("error", f"The update to {offer['version']} failed: {error}")
        return _state["detail"]


# --- asked for: install at the first idle moment ---------------------------------------------------

def requested() -> bool:
    """The user asked for an update that isn't installed yet."""
    with _lock:
        return bool(_requested)


def request() -> str:
    """Remember that the user asked, start the waiter (download, then install once Mint is idle) and say
    what happens. Never restarts mid-conversation or mid-task; the waiter waits as long as it takes."""
    global _requested, _failures, _next_try
    app = running_app()
    ok, why = packaged(app)
    if not ok:
        return why
    with _lock:
        offer = _offer
    if offer is None:
        text = check(now=True, background=False)
        with _lock:
            offer = _offer
        if offer is None:
            if _state["status"] in ("current", "blocked"):
                return text                     # the newest already, or not for this macOS: nothing to wait for
            with _lock:                         # the check failed (offline?): the waiter tries again
                _requested = _requested or time.time()
                _next_try = time.time() + RETRY[0]
            _ensure_waiter()
            return text + " I'll keep trying; it installs itself once it's downloaded and Mint is idle."
    with _lock:
        _requested = _requested or time.time()
        _failures, _next_try = 0, 0.0           # asked again: try at once
        ready = _staged is not None and _staged.parent.name == offer["version"]
    _save(requested={"version": offer["version"], "at": _requested})
    log.info("update: %s asked for; installs when Mint is idle", offer["version"])
    reason = busy_reason() if ready else "downloading"
    _ensure_waiter()
    if ready and not reason:
        return f"Installing Hey Mint {offer['version']} - Mint restarts in a few seconds."
    if ready:
        return f"Hey Mint {offer['version']} is downloaded; it installs itself as soon as Mint is idle."
    size = f" ({offer['size'] / 1e6:.0f} MB)" if offer["size"] else ""
    return f"Downloading Hey Mint {offer['version']}{size}; it installs itself as soon as Mint is idle."


def _live_session():
    """The running session, if it is loaded (looked up, not imported: the updater must not start one)."""
    module = sys.modules.get(f"{__package__}.session")
    return getattr(getattr(module, "Mint", None), "live", None)


def session_busy(mint) -> str:
    """Why this session isn't idle enough to restart ("" when it is): awake and talking, running a tool,
    speaking, a plan under way, a timer, a sub-agent, voice training, a Mint Meet call."""
    if mint is None:
        return ""
    quiet = time.monotonic() - max(float(getattr(mint, "_last_active", 0) or 0),
                                   float(getattr(mint, "_last_voice", 0) or 0))
    # Asleep, or paused; without the wake word (never asleep): quiet for two minutes.
    resting = (getattr(mint, "asleep", False) or getattr(mint, "paused", False)
               or (not getattr(mint, "hands_free", True) and quiet >= 120))
    if not resting:
        return "talking"
    tool = getattr(mint, "_tool_task", None)
    if getattr(mint, "_busy", False) or (tool is not None and not tool.done()):
        return "running a tool"
    audio = getattr(mint, "audio", None)
    if getattr(mint, "_turn_open", False) or getattr(audio, "playing", False):
        return "speaking"
    if getattr(mint, "_pending_text", None) or getattr(mint, "_pending_wake", None):
        return "about to answer"
    task = getattr(mint, "task", None)
    if task:
        try:
            from mint.app import tasks
            fresh = time.time() - float(task.get("updated") or task.get("started") or 0) < 600
            if fresh and tasks.remaining(task):
                return "working on a task"
        except Exception:
            pass
    if getattr(mint, "_timers", None):
        return "a timer is running"
    if getattr(mint, "enroller", None) is not None or getattr(mint, "_recalibrating", False):
        return "training the voice"
    if getattr(mint, "meet", None) is not None:
        return "in a Meet call"
    try:
        from mint.agents.runtime import hub
        if any(run.active for run in hub.runs.values()):
            return "a sub-agent is working"
        if hub._outbox or hub._finished:
            return "a sub-agent's report is waiting"
    except Exception:
        pass
    return ""


def _others_busy() -> str:
    """Recording a meeting or the screen, learning a task, dictating, watching the screen for the user.
    (Trackers and automations are not here: they pick up again after the restart.)"""
    def ask(load, call: str) -> bool:
        try:
            return bool(getattr(load(), call)())
        except Exception:
            return False

    def meetings():
        from mint.tools import meetings
        return meetings

    def dictation():
        from mint.voice import dictation
        return dictation

    def screenrec():
        from mint.tools import screenrec
        return screenrec

    def teach():
        from mint.knowledge import teach
        return teach

    def work_tools():
        from mint.tools import work as work_tools
        return work_tools

    for load, call, reason in ((meetings, "busy", "recording a meeting"), (dictation, "capturing", "dictating"),
                               (screenrec, "busy", "recording the screen"), (teach, "recording", "learning a task"),
                               (work_tools, "busy", "watching the screen")):
        if ask(load, call):
            return reason
    return ""


def busy_reason() -> str:
    """Why Mint can't restart for an update right now ("" when it can)."""
    return session_busy(_live_session()) or _others_busy()


def _fail(version: str, error) -> None:
    global _failures, _next_try
    with _lock:
        _failures += 1
        delay = RETRY[min(_failures, len(RETRY)) - 1]
        _next_try = time.time() + delay
    when = f"{delay // 60} min" if delay >= 60 else f"{delay} s"
    _set("error", f"The update to {version or 'the new version'} failed: {error}. Trying again in {when}.")


def _tick() -> str:
    """One look by the waiter. "done": nothing (more) to do; "retry": a failure, backing off; "wait":
    downloaded, Mint busy; "installed": swapped in, Mint quits and the new one opens."""
    global _requested, _waiting, _failures
    if not requested():
        return "done"
    app = running_app()
    if not packaged(app)[0]:
        with _lock:
            _requested = 0.0
        _save(requested=None)
        return "done"
    with _lock:
        offer, staged, installing = _offer, _staged, _installing
    if installing:
        return "wait"                               # (the idle loop got there first; quitting, or it failed)
    if time.time() < _next_try:
        return "retry"
    if offer is None or staged is None or staged.parent.name != offer["version"]:
        version = (offer or {}).get("version", "")
        try:
            if offer is None:                       # (picked up after a reload, or the last check failed)
                text = check(now=True, background=False)
                with _lock:
                    offer = _offer
                if offer is None:
                    if _state["status"] in ("current", "blocked"):
                        with _lock:
                            _requested = 0.0
                        _save(requested=None)
                        return "done"
                    raise UpdateError(text)
                version = offer["version"]
            prepare(offer, app)
        except Exception as error:
            _fail(version, error)
            return "retry"
        with _lock:
            _failures, _waiting = 0, ""
        _save(requested={"version": offer["version"], "at": _requested})
    reason = busy_reason()
    if reason:
        if reason != _waiting:
            log.info("update: %s downloaded; waiting (%s)", offer["version"], reason)
            _set("ready", f"Hey Mint {offer['version']} is downloaded; it installs itself as soon as Mint is "
                          f"idle (now: {reason}).", 1.0)
        _waiting = reason
        return "wait"
    _waiting = ""
    try:
        install_now(quit_after=2.0)
    except Exception as error:
        _fail(offer["version"], error)
        return "retry"
    return "installed"


def _wait_loop() -> None:
    while True:
        try:
            outcome = _tick()
        except Exception as error:                  # never let the waiter die quietly
            log.warning("update waiter: %s", error)
            outcome = "retry"
        if outcome in ("done", "installed"):
            return
        time.sleep(POLL)


def _ensure_waiter() -> None:
    global _waiter
    with _lock:
        if _waiter is not None and _waiter.is_alive():
            return
        _waiter = threading.Thread(target=_wait_loop, name="mint-update-waiter", daemon=True)
        _waiter.start()


def _resume_request() -> None:
    """At launch: an update asked for before a reload (Mint Ear, a crash) carries on; one that is now
    installed, or too old, is forgotten."""
    global _requested
    saved = _load().get("requested")
    if not isinstance(saved, dict):
        return
    at = float(saved.get("at") or 0)
    if time.time() - at > REQUEST_FOR or not newer(str(saved.get("version") or ""), current_version()):
        _save(requested=None)
        return
    with _lock:
        _requested = at
    log.info("update: %s was asked for; carrying on", saved.get("version"))
    _ensure_waiter()


# --- in the background -----------------------------------------------------------------------------

def _idle_seconds() -> float:
    try:
        import Quartz
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType))
    except Exception:
        return 0.0


def _busy() -> bool:
    """Recording a meeting or the screen, or tracking something: not the moment to restart."""
    try:
        from mint.tools import meetings
        from mint.tools import screenrec
        from mint.knowledge import teach
        from mint.tools import trackers
        return meetings.busy() or screenrec.busy() or teach.recording() or trackers.busy()
    except Exception:
        return False


def idle_enough() -> bool:
    """Nobody has touched the Mac for IDLE seconds and nothing is being recorded or tracked."""
    return _idle_seconds() >= IDLE and not _busy()


def _welcome_back() -> None:
    """After an update: "Updated to vX" on the island, once."""
    done = _load().get("updated")
    if not done:
        return
    _save(updated=None)
    if done.get("to") != current_version() or time.time() - float(done.get("at") or 0) > 86400:
        return
    log.info("update: now %s (from %s)", done["to"], done.get("from"))
    print(f"  {time.strftime('%H:%M:%S')} [updated to {done['to']}]", flush=True)
    notes = [line.strip("-* ").replace("**", "") for line in str(done.get("notes") or "").splitlines()
             if line.strip().startswith(("-", "*"))]
    from mint.tools import cards
    cards.show(f"Updated to v{done['to']}", subtitle=f"from {done.get('from')}, permissions kept",
               items=[{"title": n[:80]} for n in notes[:3]], icon="arrow.down.circle.fill", tint="mint",
               seconds=15)


def start(can_install=None) -> None:
    """Once, when Mint starts: the "Updated" card, then checks at launch and every 12 hours.
    A downloaded update installs itself once nobody has touched the Mac for 10 minutes, nothing is
    being recorded or tracked, and `can_install()` (the session: not mid-conversation) agrees.
    One the user asked for doesn't wait for the Mac: it installs as soon as Mint is idle (request())."""
    global _started
    if _started:
        return
    _started = True

    def ready() -> bool:
        return idle_enough() and (can_install is None or bool(can_install()))

    def loop():
        global _staged
        time.sleep(8)                                # the island is up
        try:
            _welcome_back()
        except Exception as error:
            log.info("update card: %s", error)
        shutil.rmtree(CACHE / current_version(), ignore_errors=True)   # what we just installed
        try:
            _resume_request()                        # asked for before a reload: carry on now
        except Exception as error:
            log.info("update request: %s", error)
        time.sleep(52)
        if not packaged(running_app())[0]:
            return
        last = float(_load().get("checked") or 0)
        first = True
        while True:
            try:
                if auto():
                    due = time.time() - last >= (LAUNCH_GAP if first else INTERVAL)
                    if due:
                        check(now=True)
                        last, first = time.time(), False
                    with _lock:
                        staged = _staged
                    if staged is not None and ready():
                        install_now(quit_after=2.0)
                        return
            except Exception as error:
                _set("error", f"Update: {error}")
                with _lock:
                    _staged = None                         # checked again (or fetched again) next time
            time.sleep(60)

    threading.Thread(target=loop, name="mint-updates", daemon=True).start()


# --- the tool ---------------------------------------------------------------------------------------

def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="update",
        description=("Hey Mint's own updates. check: ask GitHub whether a newer version is out. install (\"update "
                     "yourself\", \"install the update\"): start downloading it now; Mint installs it by itself the "
                     "moment it is idle (asleep, nothing running) and is back a few seconds later, keeping its "
                     "permissions - say one short line and carry on. status: which version this is, the newest "
                     "one, and whether an update is downloading, waiting or ready. Actions: check, install, status."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["check", "install", "status"])},
            required=["action"]))]


def tool(args: dict) -> str:
    action = str(args.get("action") or "status").lower()
    if action == "check":
        return check(now=True)
    if action == "install":
        text = install()
        if requested():                 # asked mid-conversation: it waits for Mint to be idle
            return (text + " Tell the user in one short line - e.g. \"Downloading the update - I'll install it "
                    "when I'm idle.\" - and carry on; nothing to wait for.")
        return text
    return status()


HANDLERS = {"update": tool}
