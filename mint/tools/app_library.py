"""The app library: every app Mint can work with, which of them are on this Mac, and what to offer next.

Settings ▸ Accounts & connections and the library window (library_window.py) show it in plain words:

    Connected      working now: a built-in connector that is set up, a connector the user made
                   (connector_maker.py), or an app the user connected here
    On your Mac    installed apps Mint can work with that aren't connected yet: Connect
    Suggested      a few very common apps (Notion, Slack, Zoom…) that aren't on this Mac: Get it opens the
                   App Store or the maker's download page; "Works in your browser" when Mint can use the web app
    Accounts       Google, iCloud, Microsoft, Telegram remote control - not set up yet: Set up
    More on your   other installed apps with a scripting dictionary (connectors.scriptable_apps): Connect makes a
      Mac          connector for them

CATALOG is the curated list (bundle ids, brand slug for brands.py, category, one line of what Mint does with it,
where to get it, its web app, and how Mint works it: a built-in connector, the app's scripting dictionary, its
links, its window, or the browser). Everything else is computed:

    rows()          every entry with its state, from connectors.py (cached; a warm call is a few ms)
    groups()        rows() sorted into the groups above
    search(q)       rows matching the words, best first
    connect(id)     what the Connect button does (off the main thread: it may wait for macOS or the planner)

groups() and search() are pure when given their inputs (installed bundle ids, connector states, …): the tests
use that. Connecting an app that needs no setup is remembered in
~/Library/Application Support/Mint/connected-apps.json.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("mint.tools.app_library")

MINE_PATH = Path.home() / "Library" / "Application Support" / "Mint" / "connected-apps.json"
MAX_SUGGESTED = 6
CACHE_SECONDS = 60

# How Mint works an app, in plain words.
HOW_WORDS = {
    "connector": "Built into Mint",
    "scripting": "Mint talks to the app directly",
    "links": "Mint opens the right page in the app",
    "screen": "Mint uses the app's window, like you would",
    "browser": "Works in your browser",
    "account": "Through your Mac's Internet Accounts",
}
BUILT_IN = "Built in"
ACCOUNTS = "Accounts"
MORE = "More on your Mac"
CATEGORIES = (BUILT_IN, "Chat & calls", "Notes & docs", "Tasks", "Office", "Music & video", "Design", "Browsers",
              "Developer", "AI", ACCOUNTS)
# state -> what the row's button says (and the order a search lists them in)
STATES = {"connected": "Connected", "ready": "Connect", "setup": "Set up", "get": "Get it", "web": "Use in browser"}
_STATE_ORDER = {"connected": 0, "ready": 1, "setup": 2, "get": 3, "web": 4}
# Never offered, whatever they can do: passwords, Mint itself, system utilities nobody "connects".
NEVER = {"io.github.shivatmax.heymint", "com.1password.1password", "com.agilebits.onepassword7",
         "com.bitwarden.desktop", "com.lastpass.LastPass", "com.dashlane.dashlanephonefinal",
         "com.apple.BluetoothFileExchange", "com.apple.SystemProfiler", "com.apple.ScreenSharing",
         "com.apple.ScriptEditor2", "com.apple.Automator", "com.apple.systempreferences", "com.apple.AppStore",
         "com.apple.keychainaccess", "com.apple.Passwords"}


@dataclass(frozen=True)
class App:
    id: str
    name: str
    category: str
    what: str                      # one line: what Mint can do with it
    bundles: tuple = ()            # its Mac app (any of these); empty = a web app or an account
    how: str = "screen"            # connector | scripting | links | screen | browser | account
    connector: str = ""            # its connectors.LIBRARY id, when Mint has one built in
    slug: str = ""                 # brands.py / Simple Icons name for its logo
    get: str = ""                  # where to get the Mac app
    web: str = ""                  # its web app, when Mint can use it in the browser
    common: int = 0                # 1 = most common: suggested (in this order) when it isn't installed
    color: tuple = (0.45, 0.48, 0.55)   # brand colour, for a drawn tile when there is no logo
    aliases: tuple = ()

    @property
    def kind(self) -> str:
        """app (has a Mac app), web (browser only) or service (an account or a bridge)."""
        return "app" if self.bundles else "web" if self.web else "service"


def _mas(app_id: int) -> str:
    return f"macappstore://apps.apple.com/app/id{app_id}"


A = App
CATALOG: tuple[App, ...] = (
    # --- built into macOS ---
    A("mail", "Mail", BUILT_IN, "Reads and sums up your email, and drafts replies for you to send.",
      ("com.apple.mail",), "connector", "mail", "mail", color=(0.2, 0.55, 1.0), aliases=("email", "apple mail")),
    A("calendar", "Calendar", BUILT_IN, "Shows your day, adds events and finds free time.", ("com.apple.iCal",),
      "connector", "calendar", "calendar_app", color=(0.95, 0.27, 0.23), aliases=("calendars", "apple calendar")),
    A("reminders", "Reminders", BUILT_IN, "Adds reminders with times and lists, and ticks them off.",
      ("com.apple.reminders",), "connector", "reminders", color=(1.0, 0.58, 0.0), aliases=("to-dos",)),
    A("notes", "Notes", BUILT_IN, "Makes notes, and finds and reads the ones you have.", ("com.apple.Notes",),
      "connector", "notes", color=(1.0, 0.8, 0.0), aliases=("apple notes",)),
    A("contacts", "Contacts", BUILT_IN, "Looks up numbers, emails, birthdays and addresses.",
      ("com.apple.AddressBook",), "connector", "contacts", color=(0.6, 0.45, 0.3), aliases=("address book",)),
    A("messages", "Messages", BUILT_IN, "Tells you who wrote and what. It sends only when you ask.",
      ("com.apple.MobileSMS",), "connector", "messages", color=(0.2, 0.8, 0.35), aliases=("imessage", "sms")),
    A("facetime", "FaceTime", BUILT_IN, "Starts a call by name or number; you press Call.", ("com.apple.FaceTime",),
      "connector", "facetime", color=(0.2, 0.8, 0.35)),
    A("safari", "Safari", BUILT_IN, "Opens, reads and uses web pages for you.", ("com.apple.Safari",), "connector",
      "safari", "safari", color=(0.1, 0.55, 1.0)),
    A("finder", "Finder", BUILT_IN, "Finds, opens, moves and tidies your files and folders.", ("com.apple.finder",),
      "connector", "finder", color=(0.25, 0.6, 1.0), aliases=("files", "folders")),
    A("photos", "Photos", BUILT_IN, "Finds photos by what's in them, the place or the date.", ("com.apple.Photos",),
      "connector", "photos", color=(1.0, 0.6, 0.2), aliases=("pictures",)),
    A("maps", "Maps", BUILT_IN, "Directions and travel times.", ("com.apple.Maps",), "connector", "maps",
      color=(0.3, 0.75, 0.4), aliases=("apple maps", "directions")),
    A("shortcuts", "Shortcuts", BUILT_IN, "Runs your shortcuts, and makes new ones from plain words.",
      ("com.apple.shortcuts",), "connector", "shortcuts", "shortcuts", color=(0.9, 0.3, 0.55),
      aliases=("apple shortcuts",)),
    A("image_playground", "Image Playground", BUILT_IN, "Draws pictures on this Mac and changes them.",
      ("com.apple.GenerativePlaygroundApp",), "connector", "image_playground", color=(0.6, 0.4, 1.0)),
    # --- chat and calls ---
    A("slack", "Slack", "Chat & calls", "Opens channels and DMs by name and reads what's new.",
      ("com.tinyspeck.slackmacgap",), "connector", "slack", "slack", "https://slack.com/downloads/mac",
      "https://app.slack.com/", 2, (0.29, 0.08, 0.29)),
    A("whatsapp", "WhatsApp", "Chat & calls", "Opens a chat and writes a message for you to send.",
      ("net.whatsapp.WhatsApp", "desktop.WhatsApp"), "connector", "whatsapp", "whatsapp",
      "https://www.whatsapp.com/download", "https://web.whatsapp.com/", 5, (0.15, 0.83, 0.4)),
    A("zoom", "Zoom", "Chat & calls", "Records your calls and writes up notes and action items.", ("us.zoom.xos",),
      "connector", "meetings", "zoom", "https://zoom.us/download", "", 3, (0.04, 0.36, 1.0)),
    A("teams", "Microsoft Teams", "Chat & calls", "Records your calls and writes up notes and action items.",
      ("com.microsoft.teams2", "com.microsoft.teams"), "connector", "meetings", "microsoftteams",
      "https://www.microsoft.com/microsoft-teams/download-app", "https://teams.microsoft.com/", 7,
      (0.38, 0.39, 0.65), ("teams",)),
    A("meet", "Google Meet", "Chat & calls", "Records your calls and writes up notes and action items.", (),
      "browser", "meetings", "meet", "", "https://meet.google.com/", 0, (0.0, 0.53, 0.32), ("meet",)),
    A("discord", "Discord", "Chat & calls", "Opens your servers and chats, and reads what's new.",
      ("com.hnc.Discord",), "links", "", "discord", "https://discord.com/download", "https://discord.com/app", 9,
      (0.35, 0.4, 0.95)),
    A("telegram_app", "Telegram", "Chat & calls", "Opens your chats and reads what's new.",
      ("ru.keepcoder.Telegram", "org.telegram.desktop"), "links", "", "telegram", "https://macos.telegram.org/",
      "https://web.telegram.org/", 10, (0.15, 0.65, 0.89)),
    A("signal", "Signal", "Chat & calls", "Opens a chat and writes a message for you to send.",
      ("org.whispersystems.signal-desktop",), "screen", "", "signal", "https://signal.org/download/macos/", "", 0,
      (0.23, 0.46, 0.94)),
    # --- notes and documents ---
    A("notion", "Notion", "Notes & docs", "Opens pages and searches your workspace.", ("notion.id",), "connector",
      "notion", "notion", "https://www.notion.com/desktop", "https://www.notion.so/", 1, (0.1, 0.1, 0.1)),
    A("obsidian", "Obsidian", "Notes & docs", "Opens and searches your notes; they're files Mint can read.",
      ("md.obsidian",), "connector", "obsidian", "obsidian", "https://obsidian.md/download", "", 0,
      (0.49, 0.23, 0.93)),
    A("evernote", "Evernote", "Notes & docs", "Finds and opens your notes.", ("com.evernote.Evernote",), "screen", "",
      "evernote", "https://evernote.com/download", "https://www.evernote.com/client/web", 0, (0.0, 0.66, 0.29)),
    A("bear", "Bear", "Notes & docs", "Makes notes and finds the ones you have.", ("net.shinyfrog.bear",), "links",
      "", "bear", "https://bear.app/", "", 0, (0.85, 0.25, 0.2)),
    A("onenote", "Microsoft OneNote", "Notes & docs", "Finds and opens your notebooks.",
      ("com.microsoft.onenote.mac",), "screen", "", "microsoftonenote", _mas(784801555),
      "https://www.onenote.com/notebooks", 0, (0.47, 0.22, 0.6), ("onenote",)),
    A("google_docs", "Google Docs", "Notes & docs", "Writes and edits documents in your browser.", (), "browser", "",
      "googledocs", "", "https://docs.google.com/", 0, (0.26, 0.52, 0.96), ("docs", "gdocs")),
    A("google_drive", "Google Drive", "Notes & docs", "Finds and opens your Drive files.", ("com.google.drivefs",),
      "screen", "", "googledrive", "https://www.google.com/drive/download/", "https://drive.google.com/", 0,
      (0.26, 0.52, 0.96), ("drive",)),
    A("dropbox", "Dropbox", "Notes & docs", "Finds and opens your Dropbox files.", ("com.getdropbox.dropbox",),
      "screen", "", "dropbox", "https://www.dropbox.com/install", "https://www.dropbox.com/home", 0,
      (0.0, 0.38, 1.0)),
    # --- tasks ---
    A("todoist", "Todoist", "Tasks", "Adds tasks and opens your projects.", ("com.todoist.mac.Todoist",),
      "connector", "todoist", "todoist", "https://todoist.com/downloads", "https://app.todoist.com/", 12,
      (0.89, 0.26, 0.2)),
    A("things", "Things", "Tasks", "Adds to-dos and reads your Today list.", ("com.culturedcode.ThingsMac",),
      "connector", "things", "things", "https://culturedcode.com/things/", "", 0, (0.2, 0.5, 0.95), ("things 3",)),
    A("linear", "Linear", "Tasks", "Opens issues and projects, and reads what's on screen.", ("com.linear",),
      "links", "", "linear", "https://linear.app/download", "https://linear.app/", 16, (0.37, 0.42, 0.82)),
    A("omnifocus", "OmniFocus", "Tasks", "Adds tasks and reads your lists.",
      ("com.omnigroup.OmniFocus4", "com.omnigroup.OmniFocus3"), "scripting", "", "omnifocus",
      "https://www.omnigroup.com/omnifocus", "", 0, (0.55, 0.3, 0.85)),
    A("fantastical", "Fantastical", "Tasks", "Adds events and reminders from plain words.",
      ("com.flexibits.fantastical2.mac",), "scripting", "", "fantastical", "https://flexibits.com/fantastical", "", 0,
      (0.85, 0.2, 0.25)),
    A("trello", "Trello", "Tasks", "Opens your boards and cards.", (), "browser", "", "trello", "",
      "https://trello.com/", 0, (0.0, 0.47, 0.75)),
    A("asana", "Asana", "Tasks", "Opens your tasks and projects.", (), "browser", "", "asana", "",
      "https://app.asana.com/", 0, (0.94, 0.42, 0.45)),
    A("jira", "Jira", "Tasks", "Opens your issues and boards.", (), "browser", "", "jira", "",
      "https://home.atlassian.com/", 0, (0.0, 0.32, 0.8)),
    # --- office ---
    A("word", "Microsoft Word", "Office", "Opens, writes and edits documents.", ("com.microsoft.Word",), "scripting",
      "", "microsoftword", _mas(462054704), "https://www.office.com/launch/word", 6, (0.17, 0.34, 0.6), ("word",)),
    A("excel", "Microsoft Excel", "Office", "Opens spreadsheets and fills them in.", ("com.microsoft.Excel",),
      "scripting", "", "microsoftexcel", _mas(462058435), "https://www.office.com/launch/excel", 8,
      (0.13, 0.45, 0.27), ("excel",)),
    A("powerpoint", "Microsoft PowerPoint", "Office", "Opens and builds slides.", ("com.microsoft.Powerpoint",),
      "scripting", "", "microsoftpowerpoint", _mas(462062816), "https://www.office.com/launch/powerpoint", 0,
      (0.82, 0.28, 0.15), ("powerpoint",)),
    A("outlook", "Microsoft Outlook", "Office", "Reads your work email and calendar.", ("com.microsoft.Outlook",),
      "connector", "microsoft", "microsoftoutlook", _mas(985367838), "https://outlook.office.com/", 13,
      (0.0, 0.47, 0.83), ("outlook",)),
    A("pages", "Pages", "Office", "Writes and edits documents, and saves them as PDF.", ("com.apple.iWork.Pages",),
      "connector", "iwork", "pages", _mas(409201541), "", 0, (1.0, 0.6, 0.1)),
    A("keynote", "Keynote", "Office", "Makes slides from your notes.", ("com.apple.iWork.Keynote",), "connector",
      "iwork", "keynote", _mas(409183694), "", 0, (0.1, 0.55, 1.0)),
    A("numbers", "Numbers", "Office", "Makes spreadsheets and fills them in.", ("com.apple.iWork.Numbers",),
      "connector", "iwork", "numbers", _mas(409203825), "", 0, (0.2, 0.75, 0.3)),
    A("libreoffice", "LibreOffice", "Office", "Opens and edits documents and spreadsheets.",
      ("org.libreoffice.script",), "screen", "", "libreoffice", "https://www.libreoffice.org/download/", "", 0,
      (0.1, 0.65, 0.35)),
    A("google_sheets", "Google Sheets", "Office", "Makes and fills in spreadsheets in your browser.", (), "browser",
      "", "googlesheets", "", "https://sheets.google.com/", 0, (0.06, 0.62, 0.35), ("sheets",)),
    # --- music and video ---
    A("spotify", "Spotify", "Music & video", "Plays, pauses and picks songs and playlists.", ("com.spotify.client",),
      "connector", "spotify", "spotify", "https://www.spotify.com/download/mac/", "https://open.spotify.com/", 4,
      (0.11, 0.73, 0.33)),
    A("music", "Music", "Music & video", "Plays songs, albums and playlists.", ("com.apple.Music",), "connector",
      "music", color=(0.98, 0.25, 0.35), aliases=("apple music", "itunes")),
    A("podcasts", "Podcasts", "Music & video", "Plays and pauses your shows.", ("com.apple.podcasts",), "connector",
      "podcasts", color=(0.6, 0.35, 0.95)),
    A("tv", "TV", "Music & video", "Plays and pauses what you're watching.", ("com.apple.TV",), "scripting", "",
      color=(0.15, 0.15, 0.15), aliases=("apple tv",)),
    A("vlc", "VLC", "Music & video", "Opens, plays and pauses videos.", ("org.videolan.vlc",), "scripting", "",
      "vlcmediaplayer", "https://www.videolan.org/vlc/", "", 0, (1.0, 0.53, 0.0)),
    A("youtube", "YouTube", "Music & video", "Finds and plays videos, and downloads them when you ask.", (),
      "browser", "", "youtube", "", "https://www.youtube.com/", 0, (1.0, 0.0, 0.0)),
    # --- design ---
    A("figma", "Figma", "Design", "Opens your design files and reads what's on screen.", ("com.figma.Desktop",),
      "screen", "", "figma", "https://www.figma.com/downloads/", "https://www.figma.com/", 11, (0.95, 0.31, 0.12)),
    A("canva", "Canva", "Design", "Opens your designs and starts new ones.", ("com.canva.CanvaDesktop",), "screen",
      "", "canva", "https://www.canva.com/download/mac/", "https://www.canva.com/", 0, (0.0, 0.77, 0.8)),
    A("photoshop", "Adobe Photoshop", "Design", "Opens pictures and runs simple edits.", ("com.adobe.Photoshop",),
      "scripting", "", "adobephotoshop", "https://www.adobe.com/products/photoshop.html", "", 0, (0.0, 0.2, 0.4),
      ("photoshop",)),
    # --- browsers ---
    A("chrome", "Google Chrome", "Browsers", "Browses, clicks and fills in pages for you.", ("com.google.Chrome",),
      "connector", "chrome", "googlechrome", "https://www.google.com/chrome/", "", 0, (0.26, 0.52, 0.96),
      ("chrome",)),
    A("brave", "Brave", "Browsers", "Browses, clicks and fills in pages for you.", ("com.brave.Browser",),
      "connector", "chrome", "brave", "https://brave.com/download/", "", 0, (0.98, 0.33, 0.14)),
    A("edge", "Microsoft Edge", "Browsers", "Browses, clicks and fills in pages for you.", ("com.microsoft.edgemac",),
      "connector", "chrome", "microsoftedge", "https://www.microsoft.com/edge/download", "", 0, (0.0, 0.47, 0.83),
      ("edge",)),
    A("arc", "Arc", "Browsers", "Browses, clicks and fills in pages for you.", ("company.thebrowser.Browser",),
      "connector", "chrome", "arc", "https://arc.net/", "", 0, (0.99, 0.25, 0.4)),
    A("firefox", "Firefox", "Browsers", "Opens pages and reads them to you.", ("org.mozilla.firefox",), "screen", "",
      "firefoxbrowser", "https://www.mozilla.org/firefox/mac/", "", 0, (1.0, 0.45, 0.1)),
    # --- developer ---
    A("vscode", "VS Code", "Developer", "Opens your projects; Mint's coding agents work in them.",
      ("com.microsoft.VSCode",), "connector", "vscode", "visualstudiocode", "https://code.visualstudio.com/", "", 0,
      (0.0, 0.48, 0.8), ("visual studio code", "code")),
    A("cursor", "Cursor", "Developer", "Opens your projects and files.", ("com.todesktop.230313mzl4w4u92",), "screen",
      "", "cursor", "https://cursor.com/download", "", 0, (0.1, 0.1, 0.1)),
    A("terminal", "Terminal", "Developer", "Tells you when a long command finishes; runs commands you ask for.",
      ("com.apple.Terminal",), "connector", "terminal", color=(0.15, 0.15, 0.15)),
    A("iterm", "iTerm", "Developer", "Tells you when a long command finishes; runs commands you ask for.",
      ("com.googlecode.iterm2",), "connector", "terminal", "iterm2", "https://iterm2.com/", "", 0,
      (0.1, 0.1, 0.1), ("iterm2",)),
    A("github", "GitHub", "Developer", "Opens your repositories, issues and pull requests.",
      ("com.github.GitHubClient",), "screen", "", "github", "https://desktop.github.com/", "https://github.com/", 0,
      (0.1, 0.1, 0.1), ("github desktop",)),
    # --- AI ---
    A("chatgpt", "ChatGPT", "AI", "Asks ChatGPT for you and reads its answers.", ("com.openai.chat",), "connector",
      "ai_apps", "openai", "https://chatgpt.com/download", "https://chatgpt.com/", 14, (0.07, 0.07, 0.07)),
    A("codex", "Codex", "AI", "Follows Codex's coding sessions and tells you when they're done.",
      ("com.openai.codex",), "connector", "ai_apps", "openai", "https://chatgpt.com/codex", "", 0,
      (0.07, 0.07, 0.07)),
    A("claude", "Claude", "AI", "Asks Claude for you and reads its answers.", ("com.anthropic.claudefordesktop",),
      "connector", "ai_apps", "anthropic", "https://claude.ai/download", "https://claude.ai/", 15,
      (0.85, 0.47, 0.34)),
    A("gemini", "Gemini", "AI", "Asks Gemini for you and reads its answers.", ("com.google.GeminiMacOS",), "screen",
      "", "gemini", "https://gemini.google.com/", "https://gemini.google.com/", 0, (0.3, 0.45, 0.95)),
    # --- accounts and bridges (no app of their own) ---
    A("google", "Google account", ACCOUNTS, "Gmail and Google Calendar, through your Mac's Internet Accounts.", (),
      "account", "google", "google", color=(0.26, 0.52, 0.96), aliases=("gmail", "google calendar")),
    A("icloud", "iCloud", ACCOUNTS, "Your iCloud mail, calendars, notes and iCloud Drive files.", (), "account",
      "icloud", "icloud", color=(0.3, 0.6, 1.0), aliases=("icloud drive",)),
    A("microsoft", "Microsoft account", ACCOUNTS, "Outlook and work email and calendars, through Internet Accounts.",
      (), "account", "microsoft", "microsoft", color=(0.0, 0.47, 0.83), aliases=("exchange", "office 365")),
    A("telegram", "Telegram remote control", ACCOUNTS, "Text or voice-note Mint from your phone; alerts come back "
      "there.", (), "account", "telegram", "telegram", color=(0.15, 0.65, 0.89), aliases=("telegram bot",)),
)
BY_ID = {a.id: a for a in CATALOG}


# --- Building the rows (pure) -----------------------------------------------------------------------------

def _friendly(detail: str) -> str:
    """A connector's status detail in a non-technical user's words."""
    detail = str(detail or "").strip()
    words = {"Can make a connector": "Connect to set it up", "Needs permission once": "Needs your OK once",
             "Automation is off for Mint": "Turned off in Privacy & Security", "Not installed": "Not on this Mac",
             "Desktop app": "Ready", "Open": "Ready", "Allow Calendars to check": "Not set up yet"}
    if detail in words:
        return words[detail]
    if " + " in detail or not detail:            # an aggregate ("Chrome + Brave"): this row is one of them
        return "Ready"
    return detail


def _custom_for(app: App, customs: list[dict]) -> dict | None:
    for item in customs:
        if app.bundles and item.get("bundle_id") in app.bundles:
            return item
        domain = str(item.get("domain") or "").lower()
        if not app.bundles and domain and app.web and domain in app.web.lower():
            return item
        if app.bundles and not item.get("bundle_id") and domain and app.web and domain in app.web.lower():
            return item                                # a web connector for an app that isn't installed
    return None


def _row(app: App, state: str, detail: str, path: str = "", bundle: str = "", extra: bool = False) -> dict:
    action = {"connected": "", "ready": "connect", "setup": "connect", "get": "get", "web": "web"}[state]
    return {"id": app.id, "name": app.name, "category": app.category, "what": app.what, "how": app.how,
            "how_words": HOW_WORDS.get(app.how, ""), "kind": app.kind, "state": state, "detail": detail,
            "action": action, "button": STATES[state] if action else "", "installed": bool(path or bundle),
            "path": path, "bundle_id": bundle, "get_url": app.get, "web_url": app.web,
            "browser_ok": bool(app.web), "connector": app.connector, "slug": app.slug, "color": app.color,
            "common": app.common, "extra": extra, "aliases": list(app.aliases)}


def build(installed: dict[str, str], states: dict[str, dict], mine: dict[str, dict], customs: list[dict],
          extras: list[dict] = ()) -> list[dict]:
    """Every catalog entry (and every extra scriptable app) as a row with its state. Pure.

    installed: bundle id -> app path; states: connectors.LIBRARY id -> {state, detail}; mine: app id -> what the
    user connected here; customs: connectors made by connector_maker; extras: other scriptable apps (dicts with
    bundle_id, name, path)."""
    rows: list[dict] = []
    for app in CATALOG:
        status = states.get(app.connector) if app.connector else None
        custom = _custom_for(app, customs)
        bundle = next((b for b in app.bundles if b in installed), "")
        path = installed.get(bundle, "") if bundle else ""
        if app.kind == "service":
            s = status or {"state": "setup", "detail": "Not set up"}
            connected = s.get("state") == "connected"
            rows.append(_row(app, "connected" if connected else "setup", _friendly(s.get("detail"))))
            continue
        if bundle:
            if custom:
                state, detail = "connected", f"Your connector · {len(custom.get('actions') or [])} actions"
            elif app.id in mine:
                state, detail = "connected", "Ready"
            elif status and status.get("state") == "connected":
                state, detail = "connected", _friendly(status.get("detail"))
            elif status:
                state, detail = "ready", _friendly(status.get("detail"))
            else:
                state, detail = "ready", HOW_WORDS.get(app.how, "")
            if state == "ready" and detail in ("Ready", "Not on this Mac"):
                detail = HOW_WORDS.get(app.how, "")
            if detail.lower() == app.name.lower():          # Finder's status is its own name
                detail = "Ready"
            rows.append(_row(app, state, detail, path, bundle))
        elif custom or app.id in mine:
            rows.append(_row(app, "connected", "In your browser"))
        elif app.bundles:
            rows.append(_row(app, "get", "Not on this Mac"))
        else:
            rows.append(_row(app, "web", HOW_WORDS["browser"]))
    known = {b for app in CATALOG for b in app.bundles}
    seen = set()
    for item in extras or ():
        bundle = str(item.get("bundle_id") or "")
        if not bundle or bundle in known or bundle in NEVER or bundle in seen:
            continue
        seen.add(bundle)
        app = App("app:" + bundle, str(item.get("name") or bundle), MORE,
                  "Connect reads what Mint can do with it and shows you before anything is saved.",
                  (bundle,), "scripting")
        custom = _custom_for(app, customs)
        if custom:
            state, detail = "connected", f"Your connector · {len(custom.get('actions') or [])} actions"
        elif app.id in mine:
            state, detail = "connected", "Ready"
        else:
            state, detail = "ready", "On your Mac"
        rows.append(_row(app, state, detail, str(item.get("path") or ""), bundle, extra=True))
    return rows


def _connected_key(row: dict) -> tuple:
    """Connected: your own apps first, then what's built in, then accounts."""
    return ({BUILT_IN: 1, ACCOUNTS: 2}.get(row["category"], 0), row["name"].lower())


def _popular_key(row: dict) -> tuple:
    return (row["common"] == 0, row["common"], row["category"] == BUILT_IN, row["name"].lower())


def sort_groups(rows: list[dict], max_suggested: int = MAX_SUGGESTED) -> dict:
    """rows -> {connected, on_mac, suggested, accounts, more_on_mac, browser, get, counts}. Pure."""
    connected = sorted((r for r in rows if r["state"] == "connected"), key=_connected_key)
    on_mac = sorted((r for r in rows if r["state"] == "ready" and not r["extra"]), key=_popular_key)
    more = sorted((r for r in rows if r["state"] == "ready" and r["extra"]), key=lambda r: r["name"].lower())
    get = sorted((r for r in rows if r["state"] == "get"), key=_popular_key)
    suggested = [r for r in get if r["common"] > 0][:max(0, max_suggested)]
    browser = sorted((r for r in rows if r["state"] == "web"), key=lambda r: r["name"].lower())
    accounts = sorted((r for r in rows if r["state"] == "setup"), key=lambda r: r["name"].lower())
    groups = {"connected": connected, "on_mac": on_mac, "suggested": suggested, "accounts": accounts,
              "more_on_mac": more, "get": get, "browser": browser}
    groups["counts"] = {k: len(v) for k, v in groups.items()}
    return groups


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+]+", str(text).lower())


def search(query: str, rows: list[dict] | None = None) -> list[dict]:
    """Rows matching `query` (name, aliases, category, what it does), best first: a name that starts with it, a
    word of the name, an alias, anywhere in the name, then the category and the description. Pure with `rows`."""
    rows = list(rows if rows is not None else all_rows())
    q = " ".join(_words(query))
    if not q:
        return sorted(rows, key=lambda r: (_STATE_ORDER[r["state"]], r["name"].lower()))
    scored = []
    for r in rows:
        name = " ".join(_words(r["name"]))
        aliases = [" ".join(_words(a)) for a in r.get("aliases") or []]
        if name.startswith(q) or name.replace(" ", "") == q.replace(" ", ""):
            score = 0
        elif any(w.startswith(q) for w in name.split()) or any(a.startswith(q) for a in aliases):
            score = 1
        elif q in name or any(q in a for a in aliases):
            score = 2
        elif q in " ".join(_words(r["category"])):
            score = 3
        elif all(any(w.startswith(t) for w in _words(r["what"])) for t in q.split()):
            score = 4
        else:
            continue
        scored.append((score, _STATE_ORDER[r["state"]], r["name"].lower(), r))
    return [s[-1] for s in sorted(scored, key=lambda s: s[:3])]


def groups(installed=None, states: dict | None = None, mine: dict | None = None, customs: list | None = None,
           extras: list | None = None, max_suggested: int = MAX_SUGGESTED) -> dict:
    """The groups for Settings and the library. With no arguments: this Mac (cached, off the main thread the first
    time). With arguments (tests): pure - `installed` is a set of bundle ids or {bundle id: path}."""
    if installed is None and states is None and mine is None and customs is None and extras is None:
        return sort_groups(all_rows(), max_suggested)
    if installed is None:
        installed = {}
    if not isinstance(installed, dict):
        installed = {b: "" for b in installed}
    return sort_groups(build(installed, states or {}, mine or {}, customs or [], extras or []), max_suggested)


# --- This Mac ---------------------------------------------------------------------------------------------

_cache: dict = {"at": 0.0, "rows": None, "fresh": False}
_lock = threading.Lock()


def load_mine() -> dict[str, dict]:
    try:
        data = json.loads(MINE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    apps = data.get("apps") if isinstance(data, dict) else None
    return {str(k): v for k, v in apps.items() if isinstance(v, dict)} if isinstance(apps, dict) else {}


def _save_mine(apps: dict) -> None:
    MINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MINE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"apps": apps}, indent=1, ensure_ascii=False))
    tmp.replace(MINE_PATH)


def _remember(row: dict, how: str, checked: str = "") -> None:
    apps = load_mine()
    apps[row["id"]] = {"name": row["name"], "how": how, "bundle_id": row.get("bundle_id") or "",
                       "web": row.get("web_url") or "", "checked": checked, "at": time.strftime("%Y-%m-%d %H:%M")}
    _save_mine(apps)


def prompt_text() -> str:
    """For the session instructions: the apps the user connected here (through the window or the browser), so
    Mint knows to use them that way. '' when there are none. Cheap: one small file."""
    apps = load_mine()
    window = [a["name"] for a in apps.values() if a.get("how") == "screen"]
    web = [f"{a['name']} ({a['web']})" if a.get("web") else a["name"] for a in apps.values()
           if a.get("how") == "browser"]
    lines = []
    if window:
        lines.append("Apps the user connected that Mint works through their window (open_app, then menu / ui_act; "
                     "look for what's inside): " + ", ".join(window) + ".")
    if web:
        lines.append("Apps the user uses in the browser, signed in there (open_url, then the browser tool): "
                     + ", ".join(web) + ".")
    return "\n".join(lines)


def installed_bundles() -> dict[str, str]:
    """bundle id -> path for the catalog's apps that are on this Mac (folder scan, then LaunchServices)."""
    from mint.tools import connectors
    apps = connectors.installed_apps()
    found = {}
    for app in CATALOG:
        for bundle in app.bundles:
            if bundle in apps:
                found[bundle] = apps[bundle]["path"]
            elif bundle not in found:
                info = connectors.app_for((bundle,))
                if info is not None:
                    found[bundle] = info["path"]
    return found


def all_rows(fresh: bool = False, deep: bool = False) -> list[dict]:
    """Every row for this Mac. Cached for a minute; `fresh` reads again; `deep` also scans for scriptable apps
    (a fraction of a second; its answer is kept by connectors.py). Not on the main thread when cold."""
    with _lock:
        rows, at, stale = _cache["rows"], _cache["at"], _cache["fresh"]
    if rows is not None and not fresh and not stale and not deep and time.time() - at < CACHE_SECONDS:
        return rows
    from mint.tools import connectors
    started = time.monotonic()
    snap = connectors.snapshot(deep=False, max_age=0 if (fresh or stale) else 120)
    states = {r["id"]: {"state": r["state"], "detail": r["detail"]} for r in snap["library"]}
    try:
        extras = connectors.scriptable_apps(fresh=fresh) if deep else (connectors._apps.get("scriptable") or [])
    except Exception as error:
        log.info("scriptable apps: %s", error)
        extras = []
    rows = build(installed_bundles(), states, load_mine(), connectors.custom_connectors(), extras)
    with _lock:
        _cache.update(at=time.time(), rows=rows, fresh=False)
    log.debug("app library: %d rows in %.0f ms", len(rows), 1000 * (time.monotonic() - started))
    return rows


def invalidate() -> None:
    """After a connect or a new connector: the next read looks again."""
    with _lock:
        _cache["fresh"] = True


def warm(deep: bool = True, then: Callable | None = None) -> None:
    """Read this Mac in the background (Settings' reader, the library window); `then()` afterwards."""
    def run() -> None:
        try:
            all_rows(deep=deep)
        except Exception:
            log.exception("app library")
        if then is not None:
            then()
    threading.Thread(target=run, daemon=True, name="app-library").start()


def cached() -> list[dict] | None:
    """The last rows read, without reading (None before the first read): for drawing on the main thread."""
    with _lock:
        return _cache["rows"]


def get(app_id: str, rows: list[dict] | None = None) -> dict | None:
    rows = rows if rows is not None else all_rows()
    wanted = str(app_id or "").strip().lower()
    for r in rows:
        if r["id"].lower() == wanted:
            return r
    hits = search(wanted, rows)
    return hits[0] if hits and " ".join(_words(hits[0]["name"])) == " ".join(_words(wanted)) else None


# --- Connecting ------------------------------------------------------------------------------------------

def plan_summary(plan: dict) -> tuple[str, str]:
    """A connector plan (connector_maker.plan) as an alert: (title, body) in plain words."""
    name = plan.get("app") or plan.get("name") or "this app"
    lines = [str(plan.get("summary") or "").strip(), "", "Mint will be able to:"]
    for action in plan.get("actions") or []:
        flag = (" (sends)" if "sends" in action["risk"] else " (deletes)" if "deletes" in action["risk"] else
                " (pays)" if "pays" in action["risk"] else " (changes things)" if action["changes"] else "")
        lines.append(f"• {action['title']}{flag}")
    lines += ["", "Anything that changes things only runs when you ask for it."]
    return f"Connect {name}?", "\n".join(lines).strip()


def _menus(bundle: str, launch: bool = True, wait: float = 12.0) -> list[str] | None:
    """The titles of the app's menus, read through Accessibility (None: it isn't running / didn't start).
    Starts the app hidden in the background when it isn't running."""
    import subprocess

    import AppKit

    from mint.screen import axkit

    def running():
        return AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
    if not running() and launch:
        subprocess.run(["open", "-g", "-j", "-b", bundle], capture_output=True, timeout=15)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        apps = running()
        if apps:
            element = axkit.bound(axkit.AX.AXUIElementCreateApplication(apps[0].processIdentifier()))
            bar = axkit.attr(element, "AXMenuBar")
            titles = [str(axkit.attr(c, "AXTitle") or "") for c in axkit.children(bar)] if bar is not None else []
            titles = [t for t in titles if t and t != "Apple"]
            if titles:
                return titles
        time.sleep(0.5)
    return [] if running() else None


def check_window(row: dict, menus: Callable = _menus, allowed: Callable | None = None) -> tuple[bool, str]:
    """The real check for an app Mint works through its window: macOS lets Mint use other apps' controls
    (Accessibility), and Mint can read this app's menus. -> (ok, what was found / what is in the way)."""
    from mint.core import permissions
    allowed = allowed or permissions.status
    name = row["name"]
    if allowed("accessibility") != "allowed":
        return False, "accessibility"
    if not row.get("bundle_id"):
        return False, f"{name} isn't on this Mac."
    titles = menus(row["bundle_id"])
    if titles is None:
        return False, f"{name} didn't start, so Mint couldn't check it. Open it once yourself, then press Connect."
    if not titles:
        return False, f"{name} started, but it doesn't show Mint its menus or buttons - Mint can only look at it."
    shown = ", ".join(titles[:4]) + ("…" if len(titles) > 4 else "")
    words = f"Mint can read and use its menus ({shown})"
    if allowed("screen") != "allowed":
        words += "; allow Screen Recording too, so Mint can also see what's inside its window"
    return True, words


def _connect_window(row: dict, say: Callable) -> str:
    name = row["name"]
    say(f"Checking that Mint can use {name} (it opens in the background)…")
    ok, words = check_window(row)
    if words == "accessibility":
        from mint.core import permissions
        try:
            permissions.open_pane("accessibility")
        except Exception:
            log.debug("accessibility pane", exc_info=True)
        return (f"Not connected yet: Mint needs Accessibility to use {name}'s window. Turn Mint on in the "
                "System Settings window that opened, then press Connect again.")
    if not ok:
        return f"Not connected: {words}"
    _remember(row, "screen", checked=words)
    return f"Connected {name}: {words}. Ask, e.g. “in {name}, …”."


def _make(row: dict, confirm: Callable | None, say: Callable) -> str:
    """Plan a connector for an installed app, show it, save it (connector_maker). An app with nothing Mint
    can script or link to falls back to its window - checked for real (check_window) before it counts."""
    from mint.tools import connector_maker
    say(f"Reading what {row['name']} can do…")
    try:
        plan = connector_maker.plan(f"integrate {row['name']}", bundle_id=row.get("bundle_id") or "")
    except Exception as error:
        log.info("plan %s: %s", row["name"], error)
        plan = {"error": str(error)}
    if plan.get("kind") == "builtin":
        return str(plan.get("note") or f"{row['name']} is built in.")
    if plan.get("actions"):
        if confirm is not None and not confirm(plan):
            return "Not connected."
        say(f"Saving {row['name']} and testing it once (macOS may ask)…")
        said = connector_maker.create(plan["id"])
        if not said.startswith("DONE"):
            return said.removeprefix("FAILED: ")
        test = re.search(r"(Test “[^”]*”: |Checked: )(.*?)(?= Changing actions|$)", said)
        return (f"Connected {row['name']}: {len(plan['actions'])} things Mint can do with it."
                + (f" {test.group(1)}{test.group(2).strip()}" if test else ""))
    if plan.get("error") and "Could not plan it right now" in plan["error"]:
        return f"Couldn't connect {row['name']} right now: {plan['error']}"
    if row["how"] == "scripting" and row["extra"]:
        return f"Couldn't connect {row['name']}: {plan.get('error') or plan.get('refuse') or 'no plan'}"
    return _connect_window(row, say)


def connect(app_id: str, confirm: Callable[[dict], bool] | None = None,
            say: Callable[[str], None] | None = None) -> str:
    """What a row's button does; returns what happened, in a sentence. Off the main thread: it may wait for
    macOS's permission prompt or the planner.

    ready    the built-in connector's connect() (asks macOS once / opens the right System Settings page), or -
             for an app Mint has no connector for yet - connector_maker plans one, `confirm(plan)` shows it
             (plan_summary) and it is saved; an app Mint works through its window is remembered as connected
    setup    an account: Internet Accounts / its Settings page
    get      opens the App Store or the download page
    web      opens the web app and remembers it as connected (Mint uses it in the browser)"""
    say = say or (lambda words: None)
    row = get(app_id, all_rows())
    if row is None:
        return f"Mint doesn't know an app called “{app_id}”."
    name = row["name"]
    try:
        from mint.tools import connectors
        if row["state"] == "connected":
            return f"{name} is already connected."
        if row["action"] == "get":
            connectors.open_url(row["get_url"])
            return f"Opened the page to get {name}. Once it's installed, press Connect."
        if row["action"] == "web":
            return use_in_browser(row["id"])
        lib = connectors.BY_ID.get(row["connector"]) if row["connector"] else None
        if lib is not None and lib.settings_page:
            from mint.tools import setup_guide
            if setup_guide._open(lib.settings_page, lib.id):
                return f"Opened Settings at {name}."
            return lib.connect()
        if lib is not None and not lib.makeable:
            return lib.connect()
        if row["how"] in ("scripting", "links") or lib is not None:
            return _make(row, confirm, say)
        return _connect_window(row, say)
    except Exception as error:
        log.exception("connect %s", app_id)
        return f"Couldn't connect {name}: {error}"
    finally:
        invalidate()


def check(app_id: str) -> str:
    """What a connected row's Test button does: a real check of how Mint works it, in a sentence starting with
    ✓ (works) or ✗ (what is in the way). Off the main thread (it may start the app in the background)."""
    row = get(app_id, all_rows())
    if row is None:
        return f"✗ Mint doesn't know an app called “{app_id}”."
    from mint.tools import connector_maker
    from mint.tools import connectors
    name = row["name"]
    try:
        custom = _custom_for(BY_ID[row["id"]], connectors.custom_connectors()) if row["id"] in BY_ID else None
        mine = load_mine().get(row["id"])
        if custom is not None:
            ok, words = connector_maker.test(custom)
        elif row.get("connector") and connectors.get(row["connector"]) is not None:
            lib = connectors.get(row["connector"])
            app = connectors.app_for(row["bundle_id"]) if row.get("bundle_id") else None
            words = lib.do_test(lib) if lib.do_test else connectors.check_basics(lib, app=app)
            words = str(words or "")
            ok = not words.startswith(("Didn't work", "Not ready", "FAILED")) and "isn't installed" not in words
            words = words.removeprefix("Checked: ").removeprefix("Didn't work: ")
        elif mine and mine.get("how") == "browser":
            url = row.get("web_url") or mine.get("web") or ""
            ok, code = connector_maker._site_answers(url)
            host = url.split("/")[2] if url.count("/") >= 2 else url
            words = f"{host} answers - Mint uses {name} in your browser" if ok else f"{host} didn't answer ({code})"
        elif mine:
            ok, words = check_window(row)
            if words == "accessibility":
                words = f"Mint needs Accessibility (Settings ▸ Permissions & Privacy) to use {name}'s window"
        else:
            return f"✗ {name} isn't connected yet."
    except Exception as error:
        log.exception("check %s", app_id)
        ok, words = False, str(error)
    words = " ".join(str(words).split()).rstrip(".")
    return f"✓ {name} works: {words}." if ok else f"✗ {name}: {words}."


def use_in_browser(app_id: str) -> str:
    """Opens the web app and remembers it: Mint uses it in the browser, where the user is signed in."""
    row = get(app_id, all_rows())
    if row is None or not row.get("web_url"):
        return f"{app_id} has no web app Mint knows."
    from mint.tools import connectors
    _remember(row, "browser")
    invalidate()
    connectors.open_url(row["web_url"])
    return f"Opened {row['name']} in your browser. Sign in there once; Mint uses it there when you ask."


def disconnect(app_id: str) -> str:
    """Forget an app connected here (a connector the user made is removed in Settings ▸ Your connectors)."""
    apps = load_mine()
    row = get(app_id, all_rows()) or {"id": app_id, "name": app_id}
    if row["id"] not in apps:
        return f"{row['name']} wasn't connected here."
    apps.pop(row["id"])
    _save_mine(apps)
    invalidate()
    return f"Disconnected {row['name']}."


def summary_text() -> str:
    """For a voice tool: the groups in a few lines."""
    g = groups()
    lines = ["Connected: " + (", ".join(r["name"] for r in g["connected"]) or "nothing yet")]
    if g["on_mac"]:
        lines.append("On this Mac, ready to connect: " + ", ".join(r["name"] for r in g["on_mac"]))
    if g["suggested"]:
        lines.append("Popular apps Mint works with, not installed: " + ", ".join(
            r["name"] + (" (also in the browser)" if r["browser_ok"] else "") for r in g["suggested"]))
    if g["accounts"]:
        lines.append("Accounts not set up: " + ", ".join(r["name"] for r in g["accounts"]))
    if g["more_on_mac"]:
        lines.append(f"Other apps Mint can be connected to ({len(g['more_on_mac'])}): "
                     + ", ".join(r["name"] for r in g["more_on_mac"][:20]))
    return "\n".join(lines)


if __name__ == "__main__":
    t = time.monotonic()
    print(summary_text())
    print(f"({1000 * (time.monotonic() - t):.0f} ms)")
