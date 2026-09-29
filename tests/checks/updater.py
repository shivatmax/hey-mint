"""Offline checks for mint/app/updater.py: the version order, reading a release, and whole updates of a
dummy Hey Mint.app in a temporary folder (from a DMG made with hdiutil), including the refusals: an
app signed by another certificate, an unsigned one, a bad checksum, the wrong version, a dev install.

Prints PASS/FAIL lines and exits 1 on any failure. Needs macOS (hdiutil, codesign, security,
/usr/bin/openssl). Touches nothing outside its temporary folder: the signing identities live in
throwaway keychains that are deleted at the end (the keychain search list is put back as it was),
and the Trash is a folder in there too.
"""

from __future__ import annotations

import json
import os
import pathlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from mint.app import updater as u  # noqa: E402

failures = 0


def ok(name: str, condition: bool, detail: str = "") -> None:
    global failures
    print(("PASS " if condition else "FAIL ") + name + (f"  ({detail})" if detail and not condition else ""))
    failures += 0 if condition else 1


def raises(name: str, words: str, call) -> None:
    try:
        call()
    except u.UpdateError as error:
        ok(name, words in str(error), str(error))
        return
    ok(name, False, "no error")


def run(*args, **kw) -> subprocess.CompletedProcess:
    done = subprocess.run([str(a) for a in args], capture_output=True, text=True, **kw)
    if done.returncode:
        raise RuntimeError(f"{args[0]}: {done.stderr.strip()[-400:]}")
    return done


# --- versions ------------------------------------------------------------------------------------

for a, b, want in [("0.10.0", "0.9.9", True), ("v1.0.0", "1.0.0-beta.2", True), ("1.0.0-beta.2", "1.0.0", False),
                   ("1.0.0-beta.10", "1.0.0-beta.2", True), ("1.0.0-rc.1", "1.0.0-beta.9", True),
                   ("0.2.0", "0.2.0", False), ("0.2.1", "0.2", True), ("0.2", "0.2.0", False),
                   ("v0.3.0", "0.2.9", True), ("0.2.0", "0.3.0", False)]:
    ok(f"newer({a!r}, {b!r}) is {want}", u.newer(a, b) is want)

# --- reading a release ---------------------------------------------------------------------------

SHA = "ab" * 32
files: dict[str, bytes] = {}


def fake_get(url: str, limit: int = 2_000_000) -> bytes:
    if url not in files:
        raise u.UpdateError(f"404 {url}")
    return files[url]


def release(version="0.3.0", sha256=SHA, sums=SHA, digest=SHA, latest_version=None, dmg=True, pre=False) -> dict:
    base = f"https://example.invalid/v{version}/"
    name = f"Hey-Mint-{version}-arm64.dmg"
    assets = []
    if dmg:
        assets.append({"name": name, "size": 1234, "browser_download_url": base + name,
                       **({"digest": f"sha256:{digest}"} if digest else {})})
    if sums:
        files[base + name + ".sha256"] = f"{sums}  {name}\n".encode()
        assets.append({"name": name + ".sha256", "browser_download_url": base + name + ".sha256"})
    if sha256:
        files[base + "latest.json"] = json.dumps({"version": latest_version or version, "dmg": name, "sha256": sha256,
                                                  "notes": "- One\n- Two", "min_macos": "14.2"}).encode()
        assets.append({"name": "latest.json", "browser_download_url": base + "latest.json"})
    return {"tag_name": f"v{version}", "prerelease": pre, "html_url": base, "body": "", "assets": assets}


real_get = u._get
u._get = fake_get
offer = u.parse_release(release())
ok("release: version, DMG, sha256", offer["version"] == "0.3.0" and offer["dmg"] == "Hey-Mint-0.3.0-arm64.dmg"
   and offer["sha256"] == SHA and offer["size"] == 1234)
ok("release: all three checksums read", offer["sources"] == [".sha256", "digest", "latest.json"], offer["sources"])
ok("release: notes and minimum macOS from latest.json", offer["notes"].startswith("- One") and offer["min_macos"] == "14.2")
ok("release: one checksum is enough", u.parse_release(release(sha256="", sums="", digest=SHA))["sha256"] == SHA)
raises("release: checksums that disagree are refused", "disagree", lambda: u.parse_release(release(sums="cd" * 32)))
raises("release: no checksum is refused", "no checksum", lambda: u.parse_release(release(sha256="", sums="", digest="")))
raises("release: no DMG yet", "no DMG", lambda: u.parse_release(release(dmg=False)))
raises("release: latest.json for another version", "latest.json says",
       lambda: u.parse_release(release(latest_version="0.2.9")))
raises("release: a tag that isn't a version", "not a version",
       lambda: u.parse_release({"tag_name": "nightly", "assets": []}))

files["https://api.github.com/repos/shivatmax/hey-mint/releases?per_page=10"] = json.dumps(
    [release("0.3.0"), release("0.4.0-beta.1", pre=True), {**release("0.5.0"), "draft": True}]).encode()
u.API = "https://api.github.com/repos/shivatmax/hey-mint"
ok("beta channel: newest pre-release, drafts skipped", u.fetch_release("beta")["tag_name"] == "v0.4.0-beta.1")
u._get = real_get

# --- whole updates of a dummy app -----------------------------------------------------------------

TMP = pathlib.Path(tempfile.mkdtemp(prefix="mint-updater-check-", dir="/tmp"))
TRASH = TMP / "Trash"
TRASH.mkdir()
u.CACHE = TMP / "cache"
u.STATE_FILE = TMP / "update.json"
u.auto = lambda: False                       # no background download: the checks drive it
quits, relaunches, cards_shown = [], [], []
u._quit = lambda seconds: quits.append(seconds)
u._relaunch = lambda app, pids, opener="/usr/bin/open": relaunches.append(str(app))


def fake_trash(path: pathlib.Path, name: str = "") -> pathlib.Path:
    """Like the Trash: a clash gets a new name (each scenario trashes a "Hey Mint 1.0.0.app")."""
    target = TRASH / (name or path.name)
    n = 2
    while target.exists():
        target = TRASH / f"{pathlib.Path(name or path.name).stem} {n}.app"
        n += 1
    return pathlib.Path(shutil.move(str(path), str(target)))


u._trash = fake_trash
from mint.tools import cards  # noqa: E402

cards.show = lambda title, **kw: cards_shown.append((title, kw))

keychains: list[pathlib.Path] = []
search_list = [line.strip().strip('"') for line in run("security", "list-keychains", "-d", "user").stdout.splitlines()
               if line.strip()]


def identity(name: str) -> tuple[str, pathlib.Path]:
    """A self-signed code-signing identity in its own throwaway keychain (like make_release_cert.sh)."""
    folder = TMP / name
    folder.mkdir()
    run("/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "30",
        "-keyout", folder / "key.pem", "-out", folder / "cert.pem", "-subj", f"/CN={name}/",
        "-addext", "basicConstraints=critical,CA:FALSE", "-addext", "keyUsage=critical,digitalSignature",
        "-addext", "extendedKeyUsage=critical,codeSigning")
    run("/usr/bin/openssl", "pkcs12", "-export", "-inkey", folder / "key.pem", "-in", folder / "cert.pem",
        "-out", folder / "id.p12", "-passout", "pass:check")
    keychain = folder / "id.keychain-db"
    keychains.append(keychain)
    run("security", "create-keychain", "-p", "check", keychain)
    run("security", "list-keychains", "-d", "user", "-s", *search_list)      # create-keychain added it
    run("security", "unlock-keychain", "-p", "check", keychain)
    run("security", "import", folder / "id.p12", "-k", keychain, "-P", "check", "-T", "/usr/bin/codesign")
    run("security", "set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", "check", keychain)
    sha1 = run("/usr/bin/openssl", "x509", "-in", folder / "cert.pem", "-noout", "-fingerprint", "-sha1").stdout
    return sha1.split("=", 1)[1].replace(":", "").strip(), keychain


def make_app(folder: pathlib.Path, version: str, signer=None, dev: bool = False) -> pathlib.Path:
    app = folder / u.APP_NAME
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources" / "app" / "mint").mkdir(parents=True)
    (app / "Contents" / "Resources" / "app" / "mint" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    shutil.copy("/usr/bin/true", app / "Contents" / "MacOS" / "Mint")
    info = {"CFBundleIdentifier": u.IDENTIFIER, "CFBundleName": "Hey Mint", "CFBundleExecutable": "Mint",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": version, "CFBundleVersion": "1"}
    if dev:
        info["MintProjectRoot"] = str(folder)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    if signer:
        run("codesign", "--force", "--sign", signer[0], "--keychain", signer[1], "--timestamp=none", app)
    else:
        run("codesign", "--force", "--sign", "-", app)
    return app


def make_release(name: str, version: str, signer=None, plist_version: str = "", bad_sha: bool = False) -> dict:
    """A DMG with the app (and an Applications link, like build_app.sh), its .sha256 and latest.json."""
    folder = TMP / name
    stage = folder / "stage"
    stage.mkdir(parents=True)
    make_app(stage, plist_version or version, signer)
    os.symlink("/Applications", stage / "Applications")
    dmg = folder / f"Hey-Mint-{version}-arm64.dmg"
    run("hdiutil", "create", "-quiet", "-volname", "Hey Mint", "-srcfolder", stage, "-ov", "-format", "UDZO", dmg)
    sha = run("shasum", "-a", "256", dmg).stdout.split()[0]
    (folder / (dmg.name + ".sha256")).write_text(f"{'0' * 64 if bad_sha else sha}  {dmg.name}\n")
    (folder / "latest.json").write_text(json.dumps({"version": version, "dmg": dmg.name,
                                                    "sha256": "0" * 64 if bad_sha else sha, "notes": "- Faster\n- Kinder"}))
    url = folder.as_uri() + "/"
    return {"tag_name": f"v{version}", "html_url": url, "body": "", "prerelease": False, "assets": [
        {"name": dmg.name, "size": dmg.stat().st_size, "browser_download_url": url + dmg.name},
        {"name": dmg.name + ".sha256", "browser_download_url": url + dmg.name + ".sha256"},
        {"name": "latest.json", "browser_download_url": url + "latest.json"}]}


def reset() -> None:
    with u._lock:
        u._offer, u._staged = None, None
    u._state.update(status="idle", detail="", progress=0.0)
    shutil.rmtree(u.CACHE, ignore_errors=True)
    u.STATE_FILE.unlink(missing_ok=True)
    relaunches.clear()
    quits.clear()


def scenario(name: str, current_signer, new_signer, want: str, **release_args) -> pathlib.Path:
    """Install `name` over a dummy app; `want` is "installed" or words from the refusal."""
    reset()
    apps = TMP / f"{name}-Applications"
    apps.mkdir()
    current = make_app(apps, "1.0.0", current_signer, dev=release_args.pop("dev", False))
    u.running_app = lambda: current
    rel = make_release(name, "1.1.0", new_signer, **release_args)
    u.fetch_release = lambda channel="stable": rel
    trash_before = set(TRASH.iterdir())
    check = u.check(now=True)
    answer = u.install(wait=True)
    version = u._info(current).get("CFBundleShortVersionString")
    left = [p.name for p in apps.iterdir()]
    if want == "installed":
        ok(f"{name}: check finds 1.1.0", "1.1.0" in check, check)
        ok(f"{name}: installed", answer.startswith("Updating to Hey Mint 1.1.0"), answer)
        ok(f"{name}: the app in place is 1.1.0", version == "1.1.0", version)
        ok(f"{name}: nothing left beside it", left == [u.APP_NAME], left)
        trashed = set(TRASH.iterdir()) - trash_before
        ok(f"{name}: the old one is in the Trash",
           len(trashed) == 1 and next(iter(trashed)).name.startswith("Hey Mint 1.0.0")
           and u._info(next(iter(trashed))).get("CFBundleShortVersionString") == "1.0.0", str(trashed))
        ok(f"{name}: relaunch and quit asked for", relaunches == [str(current)] and len(quits) == 1)
        ok(f"{name}: signature still valid in place",
           subprocess.run(["codesign", "--verify", "--deep", "--strict", current]).returncode == 0)
        ok(f"{name}: the download is cleaned up", not (u.CACHE / "1.1.0").exists())
    else:
        ok(f"{name}: refused ({want})", want in answer, answer)
        ok(f"{name}: the app in place is untouched", version == "1.0.0" and left == [u.APP_NAME], f"{version} {left}")
        ok(f"{name}: nothing trashed, no restart", set(TRASH.iterdir()) == trash_before and not relaunches and not quits)
    return current


try:
    A, B = identity("Hey Mint Release Check A"), identity("Hey Mint Release Check B")

    started = time.time()
    installed = scenario("adhoc-to-adhoc", None, None, "installed")
    u.running_app = lambda: installed
    u._welcome_back()
    ok("after the update: an 'Updated to v1.1.0' card", cards_shown and cards_shown[-1][0] == "Updated to v1.1.0",
       str(cards_shown))
    ok("after the update: the card shows once", "updated" not in u._load())

    same = scenario("same-certificate", A, A, "installed")
    requirement = run("codesign", "-d", "-r-", same).stdout + run("codesign", "-d", "-r-", same).stderr
    ok("same certificate: the designated requirement pins it", f'H"{A[0].lower()}"' in requirement, requirement)
    scenario("other-certificate", A, B, "different certificate")
    scenario("signed-to-unsigned", A, None, "not signed")
    scenario("bad-checksum", A, A, "does not match", bad_sha=True)
    scenario("wrong-version-inside", A, A, "not 1.1.0", plist_version="1.0.5")
    scenario("dev-install", A, A, "install.sh", dev=True)
    scenario("adhoc-to-signed", None, A, "installed")
    print(f"     (scenarios took {time.time() - started:.1f} s)")

    # The relaunch script: detached from us, waits for the pids, strips quarantine, then opens the app.
    target = make_app(TMP / "relaunch", "1.1.0")
    subprocess.run(["xattr", "-w", "com.apple.quarantine", "0083;00000000;Check;", str(target)], check=True)
    opened = TMP / "opened.txt"
    opener = TMP / "fake-open.sh"
    opener.write_text(f'#!/bin/sh\necho "$1" > "{opened}"\n')
    opener.chmod(0o755)
    sleeper = subprocess.Popen(["sleep", "2"])
    import importlib
    fresh = importlib.reload(u)                  # the real _relaunch again (the scenarios faked it)
    fresh.CACHE = TMP / "cache"
    fresh._relaunch(target, [sleeper.pid], opener=str(opener))
    time.sleep(0.5)
    script = subprocess.run(["pgrep", "-f", str(TMP / "cache" / "relaunch.sh")], capture_output=True, text=True).stdout.split()
    parents = [subprocess.run(["ps", "-o", "ppid=", "-p", p], capture_output=True, text=True).stdout.strip()
               for p in script]
    ok("relaunch: the script runs detached (its parent is launchd)", parents == ["1"], str(parents))
    ok("relaunch: it waits for the old process", not opened.exists())
    sleeper.wait()
    for _ in range(40):
        if opened.exists():
            break
        time.sleep(0.25)
    ok("relaunch: then opens the new app", opened.exists() and opened.read_text().strip() == str(target))
    quarantine = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(target)], capture_output=True)
    ok("relaunch: quarantine stripped", quarantine.returncode != 0)
finally:
    for keychain in keychains:
        subprocess.run(["security", "delete-keychain", str(keychain)], capture_output=True)
    subprocess.run(["security", "list-keychains", "-d", "user", "-s", *search_list], capture_output=True)
    shutil.rmtree(TMP, ignore_errors=True)

print("ALL PASS" if not failures else f"{failures} FAILED")
sys.exit(1 if failures else 0)
