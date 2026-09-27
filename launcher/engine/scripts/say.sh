#!/bin/bash
# Send a typed command to the running Desktop Voice app. Usage: scripts/say.sh "Open Finder"
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -z "${DEVELOPER_DIR:-}" && -d /Applications/Xcode.app/Contents/Developer ]]; then
  export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi
if [[ ! -x .build/say || scripts/say.swift -nt .build/say ]]; then
  xcrun swiftc -O scripts/say.swift -o .build/say
fi
.build/say "$@"
