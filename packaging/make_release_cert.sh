#!/bin/bash
# Make the certificate that signs every Hey Mint release, so updates keep macOS permissions.
#
#   packaging/make_release_cert.sh                  # writes to ~/hey-mint-release-cert
#   packaging/make_release_cert.sh /path/to/folder  # or there (it must not exist yet)
#
# Why: macOS remembers Microphone, Accessibility and Screen Recording for an app by its
# "designated requirement". Ad-hoc signing makes that the build's own hash, so every update
# looks like a new app and loses them. Signed with the same certificate every time, it is
#   identifier "io.github.shivatmax.heymint" and certificate root = H"<this cert's SHA-1>"
# for every release, and the permissions carry over. A self-signed certificate does this
# for free; it just isn't trusted by Gatekeeper (users still click "Open Anyway" once, on the
# first download - never for updates, which Hey Mint installs itself).
#
# It makes the key and certificate with /usr/bin/openssl, packs them in a password-protected
# .p12, and checks that macOS can import it and sign with it - in a throwaway keychain that is
# deleted at the end (your login keychain is not touched). Then it prints the three GitHub
# secrets for .github/workflows/release.yml.
#
# BACK IT UP. Keep hey-mint-release.p12 and its password together somewhere safe and
# private (a password manager item with the file attached, plus an offline copy). GitHub
# secrets cannot be read back. If it is lost, a new certificate means a new designated
# requirement: installed copies refuse to auto-update to builds signed with it (they check the
# signer), so everyone downloads the new version by hand once and grants the permissions again.
# Never commit it; anyone with it can sign apps that inherit Hey Mint's permissions.
set -euo pipefail

NAME="Hey Mint Release"
DAYS=7300                       # 20 years: a new certificate means everyone re-grants permissions
OUT="${1:-$HOME/hey-mint-release-cert}"
OPENSSL=/usr/bin/openssl        # LibreSSL: its .p12 files import into the macOS keychain

if [[ -e "$OUT" ]]; then
  echo "$OUT already exists. A second certificate would not match the first; use the one you have," >&2
  echo "or pass a new folder if you really mean to start over." >&2
  exit 1
fi
umask 077
mkdir -p "$OUT"
WORK="$(mktemp -d /tmp/hey-mint-cert.XXXXXX)"
KEYCHAIN="$WORK/check.keychain-db"
PREVIOUS_KEYCHAINS=()
while IFS= read -r line; do
  line="${line#"${line%%[![:space:]]*}"}"; line="${line%\"}"; line="${line#\"}"
  [[ -n "$line" ]] && PREVIOUS_KEYCHAINS+=("$line")
done < <(security list-keychains -d user)
cleanup() {
  security delete-keychain "$KEYCHAIN" >/dev/null 2>&1 || true
  # create-keychain adds to the search list: put it back exactly as it was.
  [[ ${#PREVIOUS_KEYCHAINS[@]} -gt 0 ]] && security list-keychains -d user -s "${PREVIOUS_KEYCHAINS[@]}" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

PASSWORD="$("$OPENSSL" rand -base64 24 | tr -d '/+=' | cut -c1-28)"

echo "Making the \"$NAME\" code-signing certificate…"
"$OPENSSL" req -x509 -newkey rsa:3072 -nodes -sha256 -days "$DAYS" \
  -keyout "$WORK/key.pem" -out "$OUT/hey-mint-release.cer.pem" \
  -subj "/CN=$NAME/O=Hey Mint/" \
  -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning" 2>/dev/null
printf '%s' "$PASSWORD" > "$WORK/pass"
"$OPENSSL" pkcs12 -export -name "$NAME" -inkey "$WORK/key.pem" -in "$OUT/hey-mint-release.cer.pem" \
  -out "$OUT/hey-mint-release.p12" -passout "file:$WORK/pass"
rm -f "$WORK/key.pem"
SHA1="$("$OPENSSL" x509 -in "$OUT/hey-mint-release.cer.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d ':')"
base64 -i "$OUT/hey-mint-release.p12" | tr -d '\n' > "$OUT/MACOS_CERT_P12.base64.txt"
printf '%s\n' "$PASSWORD" > "$OUT/MACOS_CERT_PASSWORD.txt"
printf '%s\n' "$SHA1" > "$OUT/MACOS_SIGN_IDENTITY.txt"

echo "Checking that macOS imports it and signs with it (a throwaway keychain)…"
security create-keychain -p "$PASSWORD" "$KEYCHAIN"
security unlock-keychain -p "$PASSWORD" "$KEYCHAIN"
security import "$OUT/hey-mint-release.p12" -k "$KEYCHAIN" -P "$PASSWORD" -T /usr/bin/codesign >/dev/null
security set-key-partition-list -S apple-tool:,apple: -s -k "$PASSWORD" "$KEYCHAIN" >/dev/null
cp /usr/bin/true "$WORK/probe"
codesign --force --sign "$SHA1" --keychain "$KEYCHAIN" --timestamp=none "$WORK/probe"
codesign --verify --strict "$WORK/probe"
requirement="$(codesign -d -r- "$WORK/probe" 2>&1 | sed -n 's/^designated => //p')"
# One certificate is both root and leaf; codesign names it either way, pinned by its SHA-1.
[[ "$requirement" == *"= H\"$(echo "$SHA1" | tr 'A-F' 'a-f')\""* ]] || {
  echo "Unexpected designated requirement: $requirement" >&2; exit 1; }

cat <<EOF

Done. The files are in $OUT:
  hey-mint-release.p12            the certificate and its private key (password-protected)
  hey-mint-release.cer.pem        the certificate alone (public; safe to share)
  MACOS_CERT_P12.base64.txt       \\
  MACOS_CERT_PASSWORD.txt          > the three GitHub secrets, one per file
  MACOS_SIGN_IDENTITY.txt         /

Add them at github.com/shivatmax/hey-mint ▸ Settings ▸ Secrets and variables ▸ Actions ▸
New repository secret (copy a file's contents: pbcopy < "$OUT/MACOS_CERT_P12.base64.txt"):

  MACOS_CERT_P12        $(cat "$OUT/MACOS_CERT_P12.base64.txt")

  MACOS_CERT_PASSWORD   $PASSWORD
  MACOS_SIGN_IDENTITY   $SHA1

Every release signed with it will have this designated requirement:
  identifier "io.github.shivatmax.heymint" and certificate ${requirement##*certificate }

Now back it up: put hey-mint-release.p12 and the password in your password manager (and an
offline copy), then delete this folder:  rm -rf "$OUT"
Losing it means every user downloads the next version by hand and grants permissions again.
EOF
