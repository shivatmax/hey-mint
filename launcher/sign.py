"""Sign Mint.app with the persistent local identity that jev-use created.

A stable signature is what keeps macOS privacy grants (Microphone,
Accessibility) across rebuilds; an ad-hoc signature changes every build and
macOS forgets them. Falls back to ad hoc when the identity does not exist yet.

Usage: sign.py <bundle> <identifier>
"""

import shlex
import subprocess
import sys
from pathlib import Path

bundle, identifier = sys.argv[1], sys.argv[2]
folder = Path.home() / "Library/Application Support/Jev Desktop/Signing"
keychain = folder / "signing.keychain-db"
password_file = folder / "keychain-password"
identity_file = folder / "identity"


def run(*args: str) -> str:
    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode:
        password = password_file.read_text() if password_file.exists() else "\0"
        # Never echo arguments: one of them may be the keychain password.
        raise RuntimeError(f"{Path(args[0]).name}: {done.stderr.replace(password, '[redacted]').strip()}")
    return done.stdout.strip()


if not (identity_file.exists() and password_file.exists() and keychain.exists()):
    run("codesign", "--force", "--sign", "-", "--identifier", identifier, bundle)
    print("Signed ad hoc (build jev-use first to get a persistent identity).")
    sys.exit(0)

password = password_file.read_text()
try:
    run("security", "unlock-keychain", "-p", password, str(keychain))
    previous = shlex.split(run("security", "list-keychains", "-d", "user"))
    try:
        # codesign resolves the certificate chain through the search list.
        run("security", "list-keychains", "-d", "user", "-s", *previous, str(keychain))
        run("codesign", "--force", "--sign", identity_file.read_text().strip(),
            "--keychain", str(keychain), "--timestamp=none", "--identifier", identifier, bundle)
    finally:
        run("security", "list-keychains", "-d", "user", "-s", *previous)
    print("Signed with the persistent local identity.")
except Exception as error:
    print(f"Signing failed: {error}", file=sys.stderr)
    sys.exit(1)
finally:
    subprocess.run(["security", "lock-keychain", str(keychain)], capture_output=True)
