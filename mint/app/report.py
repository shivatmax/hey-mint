"""Report a problem: a GitHub issue started for the user, in their browser.

The issue link carries only the app version, macOS version and the models in use - never what
was said or anything from the log. The recent errors from the log, cleaned of paths, emails,
keys and everything the user or Mint said, go on the clipboard so the user can paste them in
if they want; they see it all before submitting.
"""

from __future__ import annotations

import platform
import re
import urllib.parse
from pathlib import Path

REPO = "https://github.com/shivatmax/hey-mint"
LOG = Path.home() / "Library" / "Logs" / "Mint" / "mint.log"
_SECRETS = re.compile(r"(AIza[0-9A-Za-z_\-]{20,}|sk-[0-9A-Za-z_\-]{16,}|ts_[0-9A-Za-z_\-]{16,}|"
                      r"[0-9A-Za-z_\-]{32,})")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_SPOKEN = re.compile(r"^\s*(\d\d:\d\d:\d\d\s+)?(you|mint|typed|request)\b|\[(typed|request|dictation|you)",
                     re.IGNORECASE)


def _version() -> str:
    from mint import __version__
    return __version__


def excerpt(lines: int = 400, keep: int = 40) -> str:
    """The recent warnings, errors and tool failures from the log, with anything personal removed."""
    try:
        text = LOG.read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return ""
    picked = [line for line in text
              if re.search(r"error|traceback|exception|failed|warning|refused|timed out|\bFAILED\b", line, re.I)
              and not _SPOKEN.search(line)]
    home = str(Path.home())
    cleaned = []
    for line in picked[-keep:]:
        line = line.replace(home, "~")
        line = _EMAIL.sub("<email>", line)
        line = _SECRETS.sub("<redacted>", line)
        cleaned.append(line[:300])
    return "\n".join(cleaned)


def issue_url(summary: str = "") -> str:
    from mint.core import config
    body = ("### What happened\n\n<!-- What did you ask or do, and what went wrong? -->\n\n"
            "### What you expected\n\n\n"
            "### Log excerpt (optional)\n\n<!-- The recent errors are on your clipboard (paths, emails and "
            "keys removed, nothing you said). Paste them here if you're happy to share them. -->\n\n"
            "### Your Mac\n\n"
            f"- Hey Mint {_version()}\n- macOS {platform.mac_ver()[0]} ({platform.machine()})\n"
            f"- Live model: {str(config.MODEL).removeprefix('models/')}\n")
    query = urllib.parse.urlencode({"title": summary or "Problem: ", "body": body, "labels": "bug"})
    return f"{REPO}/issues/new?{query}"


def open_issue(summary: str = "") -> str:
    """Put the cleaned log excerpt on the clipboard and open a new issue in the browser."""
    import AppKit
    log = excerpt()
    if log:
        board = AppKit.NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_("```\n" + log + "\n```", AppKit.NSPasteboardTypeString)
    AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(issue_url(summary)))
    return ("Opened a new GitHub issue in your browser" + (" and copied the recent errors (nothing personal) to "
            "the clipboard - paste them in if you like" if log else "") + ". Nothing is sent until you submit it.")
