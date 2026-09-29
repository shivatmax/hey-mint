"""Google (Gmail and Google Calendar), the local way: through macOS Internet Accounts.

Adding a Google account in System Settings ▸ Internet Accounts syncs its mail into the Mail app
and its calendars into Calendar. Mint already reads, triages and drafts through Mail
(mailtriage) and schedules through Calendar (skills, EventKit), so nothing else is needed: no
sign-in with Mint, no server, no Google Cloud project. This module only says what is connected
and opens the right settings page.
"""

from __future__ import annotations

import subprocess

INTERNET_ACCOUNTS = "x-apple.systempreferences:com.apple.Internet-Accounts-Settings.extension"


def google_calendars() -> dict[str, list[str]] | None:
    """{account: [calendar titles]} for Google accounts Calendar syncs; None without Calendar access."""
    try:
        import EventKit
        status = EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent)
        if status not in (3, 4):
            return None
        from mint.tools import everyday as skills
        store, _ = skills._event_store(EventKit.EKEntityTypeEvent)
        found: dict[str, list[str]] = {}
        for calendar in store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or []:
            source = calendar.source()
            title = str(source.title() or "")
            if source.sourceType() == EventKit.EKSourceTypeCalDAV and ("gmail" in title.lower()
                                                                        or "google" in title.lower() or "@" in title):
                found.setdefault(title, []).append(str(calendar.title()))
        return found
    except Exception:
        return {}


def mail_accounts() -> list[str] | None:
    """Mail's account names, or None when Mail isn't open (asking would open it)."""
    running = subprocess.run(["pgrep", "-x", "Mail"], capture_output=True).returncode == 0
    if not running:
        return None
    try:
        out = subprocess.run(["osascript", "-e", 'tell application "Mail" to get name of every account'],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        return [a.strip() for a in out.split(",") if a.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return None


def summary() -> str:
    calendars = google_calendars()
    mail = mail_accounts()
    parts = []
    if calendars is None:
        parts.append("Google Calendar: allow Calendars for Mint in Privacy & Security to check")
    elif calendars:
        parts.append("Google Calendar: " + ", ".join(f"{a} ({len(c)} calendar{'s' * (len(c) != 1)})"
                                                     for a, c in calendars.items()))
    else:
        parts.append("Google Calendar: not connected")
    if mail is None:
        parts.append("Mail: open the Mail app once to check its accounts")
    else:
        google = [a for a in mail if "google" in a.lower() or "gmail" in a.lower()]
        parts.append("Gmail in Mail: " + (", ".join(google) if google else
                                          ("not added" + (f" (Mail has {', '.join(mail)})" if mail else ""))))
    return " · ".join(parts)


def open_internet_accounts() -> None:
    subprocess.Popen(["open", INTERNET_ACCOUNTS])
