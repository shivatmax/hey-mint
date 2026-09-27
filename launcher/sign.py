"""Sign Mint.app with a persistent local signing identity.

A stable signature is what keeps macOS privacy grants (Microphone,
Accessibility) across rebuilds; an ad-hoc signature changes every build and
macOS forgets them. The identity is a self-signed code-signing certificate in
its own keychain, made the first time (no system trust settings change).
Adapted from jev-use's scripts/sign-local.py (MIT); it keeps the same folder,
so a Mac that already has jev-use's identity keeps its grants.

Helpers in Contents/MacOS (the screen-control engine) are signed first, with
the same identity, then the app.

Usage: sign.py <bundle> <identifier>
"""

import os
import secrets
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

bundle, identifier = Path(sys.argv[1]), sys.argv[2]
folder = Path.home() / "Library/Application Support/Jev Desktop/Signing"
keychain = folder / "signing.keychain-db"
password_file = folder / "keychain-password"
identity_file = folder / "identity"
password = password_file.read_text() if password_file.exists() else secrets.token_urlsafe(32)


def run(*args: str) -> str:
    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode:
        # Never echo arguments: one of them may be the keychain password.
        raise RuntimeError(f"{Path(args[0]).name}: {done.stderr.replace(password, '[redacted]').strip()}")
    return done.stdout.strip()


def create_identity() -> None:
    os.umask(0o077)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if keychain.exists():
        raise RuntimeError(f"Local signing setup is incomplete in {folder}. Keep the keychain and repair it by hand.")
    password_file.write_text(password)
    previous = shlex.split(run("security", "list-keychains", "-d", "user"))
    try:
        run("security", "create-keychain", "-p", password, str(keychain))
        with tempfile.TemporaryDirectory(prefix="mint-signing-") as temporary:
            temp = Path(temporary)
            private_key, certificate, archive = (temp / name for name in ("key.pem", "cert.pem", "identity.p12"))
            run("/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
                "-keyout", str(private_key), "-out", str(certificate), "-days", "3650",
                "-subj", "/CN=Jev Desktop Local Development/",
                "-addext", "basicConstraints=critical,CA:FALSE",
                "-addext", "keyUsage=critical,digitalSignature",
                "-addext", "extendedKeyUsage=critical,codeSigning")
            run("/usr/bin/openssl", "pkcs12", "-export", "-inkey", str(private_key), "-in", str(certificate),
                "-out", str(archive), "-passout", f"file:{password_file}")
            run("security", "import", str(archive), "-k", str(keychain), "-P", password,
                "-x", "-T", "/usr/bin/codesign")
            run("security", "set-key-partition-list", "-S", "apple-tool:", "-s", "-k", password, str(keychain))
            fingerprint = run("/usr/bin/openssl", "x509", "-in", str(certificate), "-noout", "-fingerprint", "-sha1")
            identity_file.write_text(fingerprint.partition("=")[2].replace(":", "").strip())
    finally:
        run("security", "list-keychains", "-d", "user", "-s", *previous)
    print(f"Created a persistent local signing identity in {folder}.")


def helpers() -> list[Path]:
    """Executables in Contents/MacOS other than the main one."""
    main = bundle / "Contents" / "MacOS"
    names = subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleExecutable",
                            str(bundle / "Contents" / "Info.plist")], capture_output=True, text=True).stdout.strip()
    return sorted(p for p in main.iterdir() if p.is_file() and p.name != names and os.access(p, os.X_OK))


def sign_all(sign_args: list[str]) -> None:
    for helper in helpers():
        run("codesign", "--force", *sign_args, "--identifier", f"{identifier}.{helper.name.lower()}", str(helper))
    run("codesign", "--force", *sign_args, "--identifier", identifier, str(bundle))


try:
    if not identity_file.exists():
        try:
            create_identity()
        except Exception as error:
            print(f"No persistent identity ({str(error).replace(password, '[redacted]')}); signing ad hoc. "
                  "macOS will ask for permissions again after each rebuild.", file=sys.stderr)
            sign_all(["--sign", "-"])
            sys.exit(0)
    run("security", "unlock-keychain", "-p", password, str(keychain))
    previous = shlex.split(run("security", "list-keychains", "-d", "user"))
    try:
        # codesign resolves the certificate chain through the search list.
        run("security", "list-keychains", "-d", "user", "-s", *previous, str(keychain))
        sign_all(["--sign", identity_file.read_text().strip(), "--keychain", str(keychain), "--timestamp=none"])
    finally:
        run("security", "list-keychains", "-d", "user", "-s", *previous)
    print("Signed with the persistent local identity.")
except Exception as error:
    print(f"Signing failed: {str(error).replace(password, '[redacted]')}", file=sys.stderr)
    sys.exit(1)
finally:
    if keychain.exists():
        subprocess.run(["security", "lock-keychain", str(keychain)], capture_output=True)
