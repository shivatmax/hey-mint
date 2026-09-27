"""App-specific skills: Chrome profiles and Slack.

Both follow one pattern. Anything that can be done exactly and instantly is
done in code: a Chrome profile opens by its directory, a Slack workspace by its
team id. The user's words are mapped onto the real profile or workspace by
exact matching, then by Jev when that is ambiguous. Only the part with no exact
handle - finding a channel by name - uses keystrokes, or Desktop Voice when
keystrokes are not permitted.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

from mint.core import custom
from mint.tools import fastinput
from mint.core import jev

CHROME_STATE = Path.home() / "Library/Application Support/Google/Chrome/Local State"
SLACK_STATE = Path.home() / "Library/Application Support/Slack/storage/root-state.json"


# --- Chrome ------------------------------------------------------------------------

class _NoAccess(Exception):
    pass


def chrome_profiles() -> dict[str, dict]:
    """{profile directory: {name, email}}, from Chrome's own state file."""
    overrides = custom.get().get("chrome_profiles", {})
    try:
        cache = json.loads(CHROME_STATE.read_text()).get("profile", {}).get("info_cache", {})
    except PermissionError as error:
        if overrides:
            return {d: {"name": e, "email": e} for e, d in overrides.items()}
        raise _NoAccess from error
    except FileNotFoundError:
        return {}
    profiles = {d: {"name": p.get("name") or d, "email": p.get("user_name") or ""}
                for d, p in cache.items()}
    # Explicit mappings in custom.json win over Chrome's own names.
    for email, directory in overrides.items():
        profiles.setdefault(directory, {"name": email, "email": email})
    return profiles


def _describe_profile(directory: str, profile: dict) -> str:
    email = f" - {profile['email']}" if profile["email"] else ""
    return f"Chrome profile '{profile['name']}'{email}"


def _ax(element, attribute):
    import ApplicationServices as AX
    err, value = AX.AXUIElementCopyAttributeValue(element, attribute, None)
    return value if err == 0 else None


def _chrome_profile_menu():
    """{title: AX menu item} for Chrome's Profiles menu, or None if unreadable.

    Needs Accessibility. This is the fast path when Chrome's state file is
    protected: the menu lists every profile by name, and pressing an item opens
    or focuses that profile's window.
    """
    import AppKit
    import ApplicationServices as AX

    if not fastinput.has_accessibility():
        return None
    chrome = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                   if a.bundleIdentifier() == "com.google.Chrome"), None)
    if chrome is None:
        subprocess.run(["open", "-a", "Google Chrome"], check=False)
        time.sleep(1.2)
        chrome = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                       if a.bundleIdentifier() == "com.google.Chrome"), None)
        if chrome is None:
            return None
    app = AX.AXUIElementCreateApplication(chrome.processIdentifier())
    bar = _ax(app, "AXMenuBar")
    for top in _ax(bar, "AXChildren") or []:
        if _ax(top, "AXTitle") != "Profiles":
            continue
        menu = (_ax(top, "AXChildren") or [None])[0]
        items = {}
        for item in _ax(menu, "AXChildren") or []:
            title = _ax(item, "AXTitle") or ""
            # Keep profiles; drop the management commands under them.
            if title and not title.endswith("…") and not title.lower().startswith(
                    ("add ", "manage", "edit", "open guest", "guest")):
                items[title] = item
        return items
    return None


def _open_chrome_by_menu(account: str, url: str) -> str | None:
    import ApplicationServices as AX

    items = _chrome_profile_menu()
    if not items:
        return None
    options = {title: f"Chrome profile '{title}'" for title in items}
    title, why = jev.resolve(account, options, custom.get().get("accounts"), what="Chrome profile")
    if title is None:
        return why

    # Reuse before opening: go back to an open tab of this site in this profile.
    from mint.tools import workspace
    if url:
        found = workspace.find_tab(url, profile=title)
        if found is not None:
            workspace.focus(*found)
            return (f"Switched to your already-open tab '{found[1]['title']}' in {title} - "
                    "reused it rather than opening another.")
    else:
        own = [w for w in workspace.chrome_windows() if title.lower() in w["profile"].lower()]
        if own:
            workspace.focus(own[0])
            return f"Switched to your open {title} Chrome window."

    subprocess.run(["open", "-a", "Google Chrome"], check=False)
    AX.AXUIElementPerformAction(items[title], "AXPress")
    time.sleep(0.5)
    if url:
        if "://" not in url:
            url = "https://" + url
        # Chrome opens links in the profile whose window was focused last.
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)
        time.sleep(0.8)
    _close_profile_picker()
    return f"Opened Chrome as {title}" + (f" at {url}" if url else "") + f" ({why})."


def _close_profile_picker() -> None:
    """Close Chrome's "Who's using Chrome?" window if switching profiles left one open.

    In testing it appeared in front after a profile switch, and text typed for a
    new Google Doc went into it instead.
    """
    import AppKit
    import ApplicationServices as AX

    chrome = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                   if a.bundleIdentifier() == "com.google.Chrome"), None)
    if chrome is None:
        return
    app = AX.AXUIElementCreateApplication(chrome.processIdentifier())
    for window in _ax(app, "AXWindows") or []:
        if (_ax(window, "AXTitle") or "").startswith("Who's using Chrome"):
            close = _ax(window, "AXCloseButton")
            if close is not None:
                AX.AXUIElementPerformAction(close, "AXPress")


def _reuse_open(account: str, url: str) -> str | None:
    """Switch to what is already open for this account, instead of opening more."""
    from mint.tools import workspace

    windows = workspace.chrome_windows()
    profiles = {w["profile"]: w for w in windows if w["profile"]}
    if not profiles:
        return None
    options = {name: f"Chrome profile '{name}'" for name in profiles}
    profile, _ = jev.resolve(account, options, custom.get().get("accounts"), what="Chrome profile")
    if profile is None:
        return None
    if url:
        found = workspace.find_tab(url, profile=profile)
        if found is not None:
            workspace.focus(*found)
            return (f"Switched to your already-open tab '{found[1]['title'][:60]}' in {profile} - "
                    "reused it rather than opening another.")
        # The profile is open but not this site: a new tab in THAT window.
        workspace.focus(profiles[profile])
        if workspace.new_tab_here(url if "://" in url else "https://" + url):
            return f"Opened {url} in a new tab of your {profile} window."
        return None
    workspace.focus(profiles[profile])
    return f"Switched to your open {profile} Chrome window."


def open_chrome(account: str, url: str = "", fallback=None) -> str:
    """Open Chrome as a particular account (profile), optionally at a URL.

    Three routes, fastest first: Chrome's own state file (instant, often
    protected by macOS); the Profiles menu read through Accessibility (under a
    second); and Desktop Voice choosing from that menu (works without
    Accessibility, verified, but 15-30 seconds).
    """
    # Reuse first, on every route: an open window of this profile, or an open
    # tab of this site in it. Profiles are read from the open windows' titles.
    reused = _reuse_open(account, url)
    if reused is not None:
        return reused

    try:
        profiles = chrome_profiles()
    except _NoAccess:
        profiles = None

    if not profiles:
        result = _open_chrome_by_menu(account, url)
        if result is not None:
            return result
        if fallback is None:
            return ("FAILED: cannot see Chrome's profiles: macOS protects Chrome's settings, and Mint "
                    "does not have Accessibility to read the Profiles menu.")
        target = custom.get().get("accounts", {}).get(account.strip().lower(), account)
        result = fallback(f"In the Profiles menu, choose the profile for {target}")
        # Choosing the profile that is already in front changes nothing on
        # screen, which Desktop Voice reports as "no visible effect". Read raw,
        # that made the model tell the user the switch had failed.
        import re
        if found := re.search(r"Profiles › ([^→;]+?)\s*→", result):
            profile = found.group(1).strip()
            if "window is now" in result:
                result = f"Switched Chrome to the '{profile}' profile"
            else:
                result = (f"Chose '{profile}' in Chrome's Profiles menu; the window did not change, "
                          f"so that profile was already the one in front")
        if url:
            if "://" not in url:
                url = "https://" + url
            subprocess.run(["open", "-a", "Google Chrome", url], check=False)
            result += f"; then opened {url}"
        return result

    options = {d: _describe_profile(d, p) for d, p in profiles.items()}
    directory, why = jev.resolve(account, options, custom.get().get("accounts"), what="Chrome profile")
    if directory is None:
        return why

    args = ["open", "-na", "Google Chrome", "--args", f"--profile-directory={directory}"]
    if url:
        if "://" not in url:
            url = "https://" + url
        args.append(url)
    done = subprocess.run(args, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        return f"FAILED: could not open Chrome: {done.stderr.strip()[:160]}"
    profile = profiles[directory]
    who = profile["email"] or profile["name"]
    return f"Opened Chrome as {who}" + (f" at {url}" if url else "") + f" ({why})."


# --- Slack -------------------------------------------------------------------------

def slack_workspaces() -> dict[str, dict]:
    """{team id: {name, domain}}, from the Slack app's own state."""
    try:
        state = json.loads(SLACK_STATE.read_text())
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return {}
    teams = state.get("workspaces") or {}
    return {tid: {"name": t.get("name") or tid, "domain": t.get("domain") or ""}
            for tid, t in teams.items()}


def _front_is(bundle_name: str, wait: float = 4.0) -> bool:
    import AppKit
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None and bundle_name.lower() in (front.localizedName() or "").lower():
            return True
        time.sleep(0.1)
    return False


def _slack_window_title() -> str:
    """Slack's front window title, e.g. 'on-call (Channel) - Acme - Slack'."""
    import AppKit
    import ApplicationServices as AX

    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if (app.localizedName() or "") == "Slack":
            element = AX.AXUIElementCreateApplication(app.processIdentifier())
            err, window = AX.AXUIElementCopyAttributeValue(element, "AXFocusedWindow", None)
            if err == 0 and window is not None:
                err, title = AX.AXUIElementCopyAttributeValue(window, "AXTitle", None)
                if err == 0 and title:
                    return str(title)
    return ""


def open_slack(workspace: str = "", channel: str = "", fallback=None) -> str:
    """Open Slack, optionally in a workspace and at a channel or DM.

    `fallback(goal)` runs a goal through Desktop Voice; it is used when this
    process may not send keystrokes.
    """
    steps = []
    teams = slack_workspaces()
    aliases = custom.get().get("slack_workspaces")

    if workspace:
        options = {tid: f"Slack workspace '{t['name']}' ({t['domain']}.slack.com)" for tid, t in teams.items()}
        team, why = jev.resolve(workspace, options, aliases, what="Slack workspace")
        if team is None:
            return why
        # An exact, instant switch - no clicking through the workspace list.
        subprocess.run(["open", f"slack://open?team={urllib.parse.quote(team)}"], check=False)
        steps.append(f"switched to the {teams[team]['name']} workspace")
    else:
        subprocess.run(["open", "-a", "Slack"], check=False)
        steps.append("opened Slack")

    if not channel:
        return "Slack: " + ", ".join(steps) + "."

    channel = custom.alias(channel).lstrip("#")

    # Saved channels open by exact deep link: instant, no keystrokes, nothing
    # to misfire. custom.json: "slack_channels": {"oncall-support":
    # {"team": "T…", "id": "C…"}}. The quick switcher below is the fallback.
    saved = custom.get().get("slack_channels", {})
    match = next((spec for name, spec in saved.items()
                  if _compact(name) == _compact(channel)), None)
    if match and match.get("id"):
        team = match.get("team") or ""
        link = f"slack://channel?team={urllib.parse.quote(team)}&id={urllib.parse.quote(match['id'])}"
        subprocess.run(["open", link], check=False)
        time.sleep(1.2)
        title = _slack_window_title() if fastinput.has_accessibility() else ""
        if title and _compact(channel) not in _compact(title.split(" - ")[0]):
            return ("Slack: " + ", ".join(steps) + f"; opened the saved link for {channel}, but the "
                    f"window shows '{title.split(' - ')[0]}' - the saved channel id may be wrong.")
        steps.append(f"opened #{channel}")
        return "Slack: " + ", ".join(steps) + "."

    if not _front_is("Slack"):
        return "Slack: " + ", ".join(steps) + f", but Slack did not come to the front, so {channel} was not opened."
    time.sleep(0.6)  # let a workspace switch finish drawing

    if fastinput.has_accessibility():
        # The quick switcher is Slack's own way to jump anywhere by name.
        return _slack_quick_switch(channel, steps)

    if fallback is None:
        return "Slack: " + ", ".join(steps) + f"; cannot open {channel} without Accessibility."
    result = fallback(f"open the {channel} channel")
    # Slack's app exposes almost nothing to Accessibility (19 controls and no
    # channel list in testing), so Desktop Voice usually cannot find channels.
    # Only claim success if the window title shows the channel.
    wanted = channel.lower().replace(" ", "-")
    if f"window is now" in result and wanted in result.lower().replace(" ", "-"):
        return "Slack: " + ", ".join(steps) + f"; then {result}"
    return ("Slack: " + ", ".join(steps) + f". FAILED: could not open the {channel} channel: Slack's app "
            "does not expose its channel list, so it can only be reached with the quick switcher, "
            "which needs Accessibility for Mint. Tell the user to choose Grant Accessibility… in "
            "the Mint menu. Say plainly that the channel was not opened.")


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _slack_quick_switch(channel: str, steps: list[str]) -> str:
    """Jump to a channel with Slack's quick switcher, and prove it worked.

    Slack's switcher is fuzzy but not forgiving about separators: in testing
    "on-call" found nothing for a channel named "oncall-support", and Return
    then opened the top suggestion - a DM. So each attempt is verified against
    the window title; a miss is undone with Go Back, and retried with the
    separators removed ("oncall"), which does match.
    """
    import AppKit

    from mint.tools import everyday as skills

    slack = next((a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                  if (a.localizedName() or "") == "Slack"), None)
    if slack is None:
        return "Slack: " + ", ".join(steps) + ", but Slack is not running."
    pid = slack.processIdentifier()

    class _LostFocus(Exception):
        pass

    def key(name, modifiers=None):
        # Slack's Electron app ignores keys posted straight to its process, so
        # these go through the system - which means they go to whatever app is
        # in front. In testing the user switched apps mid-sequence and the rest
        # of the keys typed into that app. Check before every single key.
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None or front.processIdentifier() != pid:
            raise _LostFocus
        fastinput.press_key(name, modifiers)

    before = _slack_window_title()
    attempts = list(dict.fromkeys([channel, _compact(channel)]))
    landed = ""
    try:
        for query in attempts:
            slack.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            if not _front_is("Slack", wait=1.5):
                raise _LostFocus
            # A Slack modal ("Set yourself to active?") swallowed the quick-switch
            # key in testing, and the Return then pressed the modal's button.
            key("escape")
            time.sleep(0.25)
            key("k", ["command"])
            time.sleep(0.5)
            key("a", ["command"])                # clear anything already typed
            front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is None or front.processIdentifier() != pid:
                raise _LostFocus
            skills.type_text(query)              # pastes, then restores the user's clipboard
            time.sleep(1.1)                      # results filter as you type
            key("return")
            time.sleep(0.9)
            landed = _slack_window_title()
            shown = landed.split(" - ")[0]
            print(f"  [slack switcher: '{query}' -> '{shown or '(no title)'}']", flush=True)
            if landed and _compact(query) and _compact(query) in _compact(shown):
                steps.append(f"opened {shown}")
                return "Slack: " + ", ".join(steps) + "."
            if landed and landed != before:
                # Wrong place: go back to where the user was before retrying.
                key("[", ["command"])
                time.sleep(0.6)
    except _LostFocus:
        return ("Slack: " + ", ".join(steps) + f". FAILED: stopped before opening {channel}: another app "
                "came to the front, and continuing would have typed into it.")

    shown = landed.split(" - ")[0] if landed else "nothing"
    return ("Slack: " + ", ".join(steps) + f". FAILED: could not find a channel matching '{channel}' "
            f"(Slack's quick switcher went to {shown}, so I went back). Ask the user for the exact "
            "channel name; they can save it as an alias with Customise in the Mint menu.")


def list_accounts() -> str:
    """Every Chrome profile and Slack workspace Mint can open."""
    lines = []
    try:
        for d, p in chrome_profiles().items():
            lines.append(_describe_profile(d, p))
    except _NoAccess:
        lines.append("Chrome profiles: not readable until Mint is allowed to access other apps' data")
    for tid, t in slack_workspaces().items():
        lines.append(f"Slack workspace '{t['name']}'")
    return "; ".join(lines) or "No Chrome profiles or Slack workspaces found."
